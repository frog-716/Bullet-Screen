#!/usr/bin/env python3
"""Explicit, local-only backup, restore, and cleanup for Bullet-Screen data."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DATABASES = {
    "bilibili": "bilibili/data/danmaku.sqlite3",
    "douyin": "douyin/data/danmaku.sqlite3",
}
PROFILE_ALIAS = "douyin/data/browser-profile"
LIFECYCLE_DIRNAME = ".bullet-screen-data-lifecycle"
LIFECYCLE_MARKER = ".bullet-screen-data-lifecycle-in-progress"
BACKUP_FORMAT = "bullet-screen-local-backup-v1"
CONFIRMATIONS = {
    "restore": "RESTORE",
    "history": "CLEAR-HISTORY",
    "profile": "CLEAR-PROFILE",
    "cache": "CLEAR-CACHE",
}
CACHE_PATHS = (
    "__pycache__",
    ".pytest_cache",
    "bilibili/__pycache__",
    "bilibili/.pytest_cache",
    "douyin/__pycache__",
    "douyin/.pytest_cache",
    "scripts/__pycache__",
    "tests/__pycache__",
    "tests/.pytest_cache",
)
PROFILE_CACHE_PATHS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/Media Cache",
    "ShaderCache",
    "GrShaderCache",
)


class LifecycleError(RuntimeError):
    """A requested operation is unsafe or its input is invalid."""


class SimulatedInterruption(BaseException):
    """Test-only interruption that deliberately leaves the durable journal."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def profile_lock_path(profile_path: Path) -> Path:
    profile_path = Path(profile_path).absolute()
    return profile_path.parent / ("." + profile_path.name + ".bullet-screen.lock")


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _assert_no_symlink_components(path: Path, stop: Optional[Path] = None) -> None:
    """Reject links in the existing tail of a path instead of following them."""
    absolute = Path(os.path.abspath(str(path)))
    boundary = Path(os.path.abspath(str(stop))) if stop is not None else Path(absolute.anchor)
    try:
        relative = absolute.relative_to(boundary)
    except ValueError as error:
        raise LifecycleError("目标路径不在允许的目录内") from error
    current = boundary
    if _is_symlink(current):
        raise LifecycleError("安全检查失败：目标路径包含符号链接")
    for part in relative.parts:
        current = current / part
        if _is_symlink(current):
            raise LifecycleError("安全检查失败：目标路径包含符号链接")


def _checked_profile_path(root: Path, override: Optional[Path] = None) -> Path:
    raw = Path(override or os.environ.get("DOUYIN_PROFILE_DIR") or (root / PROFILE_ALIAS)).expanduser()
    raw = Path(os.path.abspath(str(raw)))
    if _is_symlink(raw):
        raise LifecycleError("Profile 路径本身是符号链接；为保护登录数据，已停止")
    return raw.parent.resolve() / raw.name


def resolve_profile_path(root: Path, override: Optional[Path] = None) -> Path:
    return _checked_profile_path(Path(root).resolve(), override)


def _assert_no_pending_marker(root: Path) -> None:
    marker = root / LIFECYCLE_MARKER
    if marker.exists() or _is_symlink(marker):
        raise LifecycleError("上次本地数据操作尚未完成；请先运行 data_lifecycle.py recover")


def _assert_default_database_paths() -> None:
    if os.environ.get("BULLET_SCREEN_DB"):
        raise LifecycleError(
            "检测到 BULLET_SCREEN_DB 自定义数据库路径；本工具目前只管理项目默认数据库，"
            "为避免漏备份或清错目标，已停止。"
        )


def has_pending_lifecycle(root: Path) -> bool:
    marker = Path(root).resolve() / LIFECYCLE_MARKER
    return marker.exists() or _is_symlink(marker)


def assert_no_pending_lifecycle(root: Path) -> None:
    """Called by services before opening a store or a persistent Profile."""
    _assert_no_pending_marker(Path(root).resolve())


def _open_lock(path: Path, *, create: bool) -> object:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_symlink(path):
        raise LifecycleError("安全检查失败：运行锁是符号链接")
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except OSError as error:
        raise LifecycleError("无法安全检查运行锁：" + str(path)) from error
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LifecycleError("运行锁不是普通文件；为保护数据已停止")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise LifecycleError("服务或浏览器正在使用相关数据；请先正常停止后再操作") from error
    return os.fdopen(descriptor, "r+")


@contextlib.contextmanager
def _locked_targets(root: Path, providers: Iterable[str], profile_path: Optional[Path] = None) -> Iterator[None]:
    handles: List[object] = []
    targets = []
    for provider in set(providers):
        database = root / DATABASES[provider]
        targets.append(database.with_name(database.name + ".lock"))
    if profile_path is not None:
        targets.append(profile_lock_path(profile_path))
    try:
        for target in sorted(set(targets), key=lambda item: str(item)):
            handles.append(_open_lock(target, create=True))
        if profile_path is not None:
            _assert_profile_not_open(profile_path)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def acquire_profile_lock(profile_path: Path) -> object:
    """Lifetime lock shared by the collector and login helper."""
    return _open_lock(profile_lock_path(profile_path), create=True)


def release_lifecycle_lock(handle: object) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _assert_profile_not_open(profile_path: Path) -> None:
    if not profile_path.exists():
        return
    if _is_symlink(profile_path):
        raise LifecycleError("Profile 路径本身是符号链接；为保护登录数据，已停止")
    for marker_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        if (profile_path / marker_name).exists() or _is_symlink(profile_path / marker_name):
            raise LifecycleError("Douyin Profile 仍被 Chromium 使用；请先关闭 Douyin 浏览器")


def _walk_regular_files(directory: Path) -> List[Path]:
    if not directory.exists():
        return []
    if _is_symlink(directory) or not directory.is_dir():
        raise LifecycleError("安全检查失败：预期目录不是普通目录")
    result: List[Path] = []
    for current, directories, filenames in os.walk(str(directory), topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            if _is_symlink(child):
                raise LifecycleError("安全检查失败：Profile 中存在符号链接")
        for name in filenames:
            child = current_path / name
            if _is_symlink(child) or not child.is_file():
                raise LifecycleError("安全检查失败：Profile 中包含链接或非普通文件")
            result.append(child)
    return sorted(result, key=lambda item: item.as_posix())


def _copy_hash(source: Path, destination: Path) -> Tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(str(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LifecycleError("只能安全复制普通文件")
    with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as outgoing:
        os.chmod(destination, 0o600)
        while True:
            block = incoming.read(1024 * 1024)
            if not block:
                break
            outgoing.write(block)
            digest.update(block)
            size += len(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return size, digest.hexdigest()


def _manifest_plan(root: Path, profile_path: Path, include_profile: bool) -> Tuple[Dict[str, object], List[Tuple[Path, str, str]]]:
    files: List[Tuple[Path, str, str]] = []
    bundles: Dict[str, object] = {}
    for provider, relative in DATABASES.items():
        database = root / relative
        _assert_no_symlink_components(database, root)
        if _is_symlink(database):
            raise LifecycleError("数据库路径不能是符号链接")
        present = database.exists()
        sidecars: Dict[str, bool] = {}
        if present and not database.is_file():
            raise LifecycleError("数据库主文件不是普通文件")
        if not present:
            for suffix in ("-wal", "-shm"):
                if (database.parent / (database.name + suffix)).exists():
                    raise LifecycleError("发现 WAL/SHM 但没有主数据库；为避免不完整备份已停止")
            bundles[provider] = {"present": False, "sidecars": {"-wal": False, "-shm": False}}
            continue
        files.append((database, relative, "database"))
        for suffix in ("-wal", "-shm"):
            sidecar = database.parent / (database.name + suffix)
            _assert_no_symlink_components(sidecar, root)
            if _is_symlink(sidecar):
                raise LifecycleError("WAL/SHM 路径不能是符号链接")
            sidecars[suffix] = sidecar.exists()
            if sidecar.exists():
                if not sidecar.is_file():
                    raise LifecycleError("WAL/SHM 不是普通文件")
                files.append((sidecar, relative + suffix, "database-sidecar"))
        bundles[provider] = {"present": True, "sidecars": sidecars}

    profile_included = bool(include_profile and profile_path.exists())
    if include_profile and profile_included:
        for source in _walk_regular_files(profile_path):
            relative = Path(PROFILE_ALIAS) / source.relative_to(profile_path)
            files.append((source, relative.as_posix(), "douyin-profile"))

    preview = {
        "format": BACKUP_FORMAT,
        "created_at": utc_now(),
        "files": [],
        "database_bundles": bundles,
        "schema_versions": {"bilibili": None, "douyin": None},
        "schema_version_note": "未读取 SQLite；仅复制文件，避免打开正在保护的数据库。",
        "profile_requested": bool(include_profile),
        "profile_included": profile_included,
    }
    return preview, files


def _assert_output_outside_root(output: Path, root: Path) -> None:
    resolved = Path(os.path.abspath(str(output)))
    root = root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return
    raise LifecycleError("备份目录必须放在项目目录之外，避免把备份误当成项目数据")


def create_backup(
    root: Path,
    output: Path,
    profile_path: Optional[Path] = None,
    *,
    include_profile: bool = False,
    dry_run: bool = False,
    locks_already_held: bool = False,
) -> Dict[str, object]:
    _assert_default_database_paths()
    root = Path(root).resolve()
    profile_path = _checked_profile_path(root, profile_path)
    output = Path(os.path.abspath(str(Path(output).expanduser())))
    output = output.parent.resolve() / output.name
    _assert_output_outside_root(output, root)
    if output.exists() or _is_symlink(output):
        raise LifecycleError("备份目标已存在；为避免覆盖，请选择一个新目录")
    if dry_run:
        preview, files = _manifest_plan(root, profile_path, include_profile)
        return {"status": "preview", "manifest": preview, "files": [relative for _, relative, _ in files]}

    providers = tuple(DATABASES)
    lock_context = contextlib.nullcontext() if locks_already_held else _locked_targets(
        root, providers, profile_path if include_profile else None
    )
    with lock_context:
        _assert_no_pending_marker(root)
        # Re-list after taking locks; the pre-lock preview is not used as a snapshot.
        manifest, files = _manifest_plan(root, profile_path, include_profile)
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".bullet-screen-backup-", dir=str(output.parent)))
        os.chmod(staging, 0o700)
        try:
            entries = []
            for source, relative, kind in files:
                archive = "files/" + relative
                size, digest = _copy_hash(source, staging / archive)
                item = {
                    "source": relative,
                    "archive": archive,
                    "size": size,
                    "sha256": digest,
                    "kind": kind,
                }
                if kind == "douyin-profile":
                    item["sensitivity"] = "douyin-login-state"
                entries.append(item)
            manifest["files"] = entries
            _write_json_durable(staging / "manifest.json", manifest)
            _verify_backup_contents(staging, manifest)
            os.replace(staging, output)
            _fsync_directory(output.parent)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    return {"status": "created", "output": str(output), "manifest": manifest}


def _write_json_durable(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_archive_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise LifecycleError("备份清单含有无效路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise LifecycleError("备份清单路径越界")
    return path.as_posix()


def _source_kind(source: str) -> str:
    if source.startswith(PROFILE_ALIAS + "/"):
        return "douyin-profile"
    for relative in DATABASES.values():
        if source == relative:
            return "database"
        if source in (relative + "-wal", relative + "-shm"):
            return "database-sidecar"
    raise LifecycleError("备份清单包含不支持的文件路径")


def _read_and_verify_manifest(backup: Path) -> Dict[str, object]:
    backup = Path(os.path.abspath(str(backup.expanduser())))
    backup = backup.parent.resolve() / backup.name
    if _is_symlink(backup) or not backup.is_dir():
        raise LifecycleError("备份目录不存在或不是普通目录")
    manifest_path = backup / "manifest.json"
    if _is_symlink(manifest_path) or not manifest_path.is_file():
        raise LifecycleError("备份缺少安全的 manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LifecycleError("无法读取备份清单") from error
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise LifecycleError("不支持的备份格式")
    entries = manifest.get("files")
    bundles = manifest.get("database_bundles")
    if not isinstance(entries, list) or not isinstance(bundles, dict):
        raise LifecycleError("备份清单结构不完整")
    seen_sources = set()
    seen_archives = set()
    for item in entries:
        if not isinstance(item, dict):
            raise LifecycleError("备份清单含有无效文件项")
        source = _safe_archive_relative(item.get("source"))
        archive_name = _safe_archive_relative(item.get("archive"))
        if source in seen_sources or archive_name in seen_archives:
            raise LifecycleError("备份清单包含重复路径")
        seen_sources.add(source)
        seen_archives.add(archive_name)
        kind = _source_kind(source)
        if item.get("kind") != kind or archive_name != "files/" + source:
            raise LifecycleError("备份清单文件映射不正确")
        archive_path = backup / archive_name
        _assert_no_symlink_components(archive_path, backup)
        if not archive_path.is_file():
            raise LifecycleError("备份文件缺失")
        descriptor = os.open(str(archive_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise LifecycleError("备份项不是普通文件")
        if file_stat.st_size != item.get("size"):
            os.close(descriptor)
            raise LifecycleError("备份文件大小与清单不符")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != item.get("sha256"):
            raise LifecycleError("备份文件校验失败")
    actual_files = {
        path.relative_to(backup).as_posix()
        for path in _walk_regular_files(backup)
    }
    expected_files = {"manifest.json", *seen_archives}
    if actual_files != expected_files:
        raise LifecycleError("备份目录含有未列入清单的文件或缺少文件")
    profile_files = {item for item in seen_sources if item.startswith(PROFILE_ALIAS + "/")}
    if not isinstance(manifest.get("profile_included"), bool):
        raise LifecycleError("Profile 备份标记无效")
    if profile_files and not manifest["profile_included"]:
        raise LifecycleError("备份含 Profile 文件但清单未标记")
    for provider, relative in DATABASES.items():
        bundle = bundles.get(provider)
        if not isinstance(bundle, dict) or not isinstance(bundle.get("present"), bool):
            raise LifecycleError("数据库备份清单不完整")
        sidecars = bundle.get("sidecars")
        if not isinstance(sidecars, dict) or any(not isinstance(sidecars.get(suffix), bool) for suffix in ("-wal", "-shm")):
            raise LifecycleError("数据库 sidecar 清单不完整")
        if bundle["present"] != (relative in seen_sources):
            raise LifecycleError("数据库主文件与清单不一致")
        for suffix in ("-wal", "-shm"):
            if bool(sidecars[suffix]) != (relative + suffix in seen_sources):
                raise LifecycleError("WAL/SHM 文件与清单不一致")
    return manifest


def _verify_backup_contents(backup: Path, manifest: Dict[str, object]) -> None:
    verified = _read_and_verify_manifest(backup)
    if verified != manifest:
        raise LifecycleError("备份清单验证失败")


def _transaction_child(transaction_root: Path, relative: object) -> Path:
    safe = _safe_archive_relative(relative)
    path = transaction_root / safe
    _assert_no_symlink_components(path, transaction_root)
    return path


def _operation_target(root: Path, profile_path: Path, operation: Dict[str, object]) -> Path:
    target = operation.get("target")
    if target == "@profile":
        return profile_path
    if isinstance(target, str) and target.startswith("@profile-cache/"):
        relative = target[len("@profile-cache/"):]
        if relative not in PROFILE_CACHE_PATHS:
            raise LifecycleError("恢复日志中的 Profile 缓存路径不受支持")
        return profile_path / relative
    relative = _safe_archive_relative(target)
    kind = operation.get("kind")
    allowed = list(DATABASES.values()) + [
        database + suffix for database in DATABASES.values() for suffix in ("-wal", "-shm")
    ]
    if kind == "cache-directory":
        allowed += list(CACHE_PATHS)
    if relative not in allowed:
        raise LifecycleError("恢复日志目标路径不受支持")
    return root / relative


def _copy_tree_secure(source: Path, destination: Path) -> None:
    files = _walk_regular_files(source)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for source_file in files:
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _copy_hash(source_file, target)
        os.chmod(target, 0o600)


def _prepare_transaction(
    root: Path,
    profile_path: Path,
    operations: List[Dict[str, object]],
    marker_root: Path,
    *,
    fail_stage: Optional[str] = None,
) -> str:
    transaction_id = uuid.uuid4().hex
    transaction_root = root / LIFECYCLE_DIRNAME / transaction_id
    staging_root = transaction_root / "staging"
    original_root = transaction_root / "original"
    staging_root.mkdir(parents=True, mode=0o700)
    original_root.mkdir(mode=0o700)
    journal_path = transaction_root / "journal.json"
    journal = {
        "format": "bullet-screen-data-lifecycle-journal-v1",
        "transaction_id": transaction_id,
        "status": "preparing",
        "root": str(root),
        "profile_path": str(profile_path),
        "operations": [{"kind": item["kind"], "target": item["target"]} for item in operations],
        "created_at": utc_now(),
    }
    try:
        _write_json_durable(journal_path, journal)
        _write_json_durable(marker_root / LIFECYCLE_MARKER, {"transaction_id": transaction_id})
    except BaseException:
        shutil.rmtree(transaction_root, ignore_errors=True)
        raise

    try:
        for index, operation in enumerate(operations):
            target = _operation_target(root, profile_path, operation)
            if operation["kind"] in ("directory", "cache-directory"):
                _assert_no_symlink_components(target)
                if target.exists() and not target.is_dir():
                    raise LifecycleError("目标不是目录；为保护数据已停止")
                if operation.get("new_present"):
                    staged = staging_root / str(index)
                    if "profile_entries" in operation:
                        staged.mkdir(mode=0o700)
                        seen = set()
                        for item in operation.pop("profile_entries"):
                            relative = _safe_archive_relative(item["relative"])
                            if relative in seen:
                                raise LifecycleError("Profile 备份含重复路径")
                            seen.add(relative)
                            destination = staged / relative
                            _assert_no_symlink_components(destination, staged)
                            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            copied_size, copied_hash = _copy_hash(Path(item["source_path"]), destination)
                            if copied_size != item["expected_size"] or copied_hash != item["expected_sha256"]:
                                raise LifecycleError("恢复来源在校验后发生变化；没有切换任何数据")
                            os.chmod(destination, 0o600)
                    else:
                        _copy_tree_secure(Path(operation["source_path"]), staged)
                    operation["staging"] = str(staged.relative_to(transaction_root))
                if target.exists():
                    original = original_root / str(index)
                    _copy_tree_secure(target, original)
                    operation["original"] = str(original.relative_to(transaction_root))
                    operation["had_original"] = True
                else:
                    operation["had_original"] = False
            else:
                _assert_no_symlink_components(target)
                if target.exists() and (not target.is_file() or _is_symlink(target)):
                    raise LifecycleError("目标文件不是普通文件；为保护数据已停止")
                if operation.get("new_present"):
                    staged = staging_root / str(index)
                    copied_size, copied_hash = _copy_hash(Path(operation["source_path"]), staged)
                    if "expected_size" in operation and copied_size != operation["expected_size"]:
                        raise LifecycleError("恢复来源在校验后发生变化；没有切换任何数据")
                    if "expected_sha256" in operation and copied_hash != operation["expected_sha256"]:
                        raise LifecycleError("恢复来源在校验后发生变化；没有切换任何数据")
                    operation["staging"] = str(staged.relative_to(transaction_root))
                if target.exists():
                    original = original_root / str(index)
                    _copy_hash(target, original)
                    operation["original"] = str(original.relative_to(transaction_root))
                    operation["had_original"] = True
                else:
                    operation["had_original"] = False
            operation.pop("source_path", None)
            if fail_stage == "during-prepare" and index == 0:
                raise SimulatedInterruption("during-prepare")

        journal["status"] = "prepared"
        journal["operations"] = operations
        _write_json_durable(journal_path, journal)
    except SimulatedInterruption:
        raise
    except BaseException:
        _remove_lifecycle_marker(marker_root)
        shutil.rmtree(transaction_root, ignore_errors=True)
        raise
    if fail_stage == "before-switch":
        raise SimulatedInterruption("before-switch")

    journal["status"] = "switching"
    _write_json_durable(journal_path, journal)
    try:
        for index, operation in enumerate(operations):
            target = _operation_target(root, profile_path, operation)
            target.parent.mkdir(parents=True, exist_ok=True)
            if operation["kind"] in ("directory", "cache-directory"):
                if target.exists():
                    if _is_symlink(target):
                        raise LifecycleError("恢复期间发现符号链接目标；已停止")
                    shutil.rmtree(target)
                if operation.get("new_present"):
                    staged = _transaction_child(transaction_root, operation["staging"])
                    os.replace(staged, target)
            else:
                if operation.get("new_present"):
                    staged = _transaction_child(transaction_root, operation["staging"])
                    os.replace(staged, target)
                elif target.exists():
                    if _is_symlink(target):
                        raise LifecycleError("恢复期间发现符号链接目标；已停止")
                    target.unlink()
            journal["completed"] = index + 1
            _write_json_durable(journal_path, journal)
            if fail_stage == "after-first-switch" and index == 0:
                raise SimulatedInterruption("after-first-switch")
            if fail_stage == "switch-error-after-first" and index == 0:
                raise OSError("simulated switch failure")
        journal["status"] = "applied"
        _write_json_durable(journal_path, journal)
    except SimulatedInterruption:
        raise
    except Exception:
        _rollback_transaction(root, profile_path, transaction_root, journal)
        _remove_lifecycle_marker(marker_root)
        shutil.rmtree(transaction_root, ignore_errors=True)
        raise

    _remove_lifecycle_marker(marker_root)
    shutil.rmtree(transaction_root, ignore_errors=True)
    return transaction_id


def _remove_lifecycle_marker(root: Path) -> None:
    marker = root / LIFECYCLE_MARKER
    if _is_symlink(marker):
        raise LifecycleError("恢复标记变成了符号链接；请人工检查后再继续")
    try:
        marker.unlink()
        _fsync_directory(root)
    except FileNotFoundError:
        pass


def _rollback_transaction(root: Path, profile_path: Path, transaction_root: Path, journal: Dict[str, object]) -> None:
    operations = journal.get("operations")
    if not isinstance(operations, list):
        raise LifecycleError("恢复日志损坏；保留现场等待人工检查")
    for operation in reversed(operations):
        if not isinstance(operation, dict):
            raise LifecycleError("恢复日志损坏；保留现场等待人工检查")
        target = _operation_target(root, profile_path, operation)
        _assert_no_symlink_components(target)
        if operation["kind"] in ("directory", "cache-directory"):
            if target.exists():
                if _is_symlink(target):
                    raise LifecycleError("恢复目标出现符号链接；保留日志等待人工检查")
                shutil.rmtree(target)
            if operation.get("had_original"):
                original = _transaction_child(transaction_root, operation.get("original", ""))
                _copy_tree_secure(original, target)
        else:
            if operation.get("had_original"):
                original = _transaction_child(transaction_root, operation.get("original", ""))
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".restore-tmp-" + uuid.uuid4().hex)
                _copy_hash(original, temporary)
                os.replace(temporary, target)
            elif target.exists():
                if _is_symlink(target):
                    raise LifecycleError("恢复目标出现符号链接；保留日志等待人工检查")
                target.unlink()
    _fsync_directory(transaction_root)


def recover_lifecycle(root: Path, profile_path: Optional[Path] = None) -> bool:
    root = Path(root).resolve()
    profile_path = _checked_profile_path(root, profile_path)
    marker_root = root
    marker = marker_root / LIFECYCLE_MARKER
    if not marker.exists():
        if _is_symlink(marker):
            raise LifecycleError("恢复标记是符号链接；拒绝跟随")
        return False
    if _is_symlink(marker):
        raise LifecycleError("恢复标记是符号链接；拒绝跟随")
    try:
        transaction_id = json.loads(marker.read_text(encoding="utf-8"))["transaction_id"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise LifecycleError("恢复标记损坏；保留数据等待人工检查") from error
    if not isinstance(transaction_id, str) or not all(character in "0123456789abcdef" for character in transaction_id):
        raise LifecycleError("恢复标记无效；保留数据等待人工检查")
    transaction_root = root / LIFECYCLE_DIRNAME / transaction_id
    _assert_no_symlink_components(transaction_root, root)
    journal_path = transaction_root / "journal.json"
    if _is_symlink(journal_path) or not journal_path.is_file():
        raise LifecycleError("恢复日志缺失；保留数据等待人工检查")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LifecycleError("恢复日志损坏；保留数据等待人工检查") from error
    if journal.get("transaction_id") != transaction_id or journal.get("root") != str(root):
        raise LifecycleError("恢复日志与当前项目不匹配；保留现场等待人工检查")
    stored_profile = Path(journal.get("profile_path", ""))
    if any(
        operation.get("target") == "@profile"
        or str(operation.get("target", "")).startswith("@profile-cache/")
        for operation in journal.get("operations", [])
    ):
        if stored_profile != profile_path:
            raise LifecycleError("恢复所需的 Profile 路径与原操作不一致；请设置相同的 DOUYIN_PROFILE_DIR")
    providers = set()
    touches_profile = False
    for operation in journal.get("operations", []):
        target = operation.get("target")
        if target == "@profile" or str(target).startswith("@profile-cache/"):
            touches_profile = True
        else:
            if operation.get("kind") == "cache-directory":
                providers.update(DATABASES)
            for provider, relative in DATABASES.items():
                if str(target).startswith(relative):
                    providers.add(provider)
    with _locked_targets(root, providers, profile_path if touches_profile else None):
        status = journal.get("status")
        if status == "preparing":
            print("操作中断在准备阶段，尚未切换目标；已清理临时副本。", file=sys.stdout)
        elif status != "applied":
            _rollback_transaction(root, profile_path, transaction_root, journal)
            print("已恢复到本地数据操作前的状态。", file=sys.stdout)
        else:
            print("数据切换已完成；清理了中断遗留的恢复标记。", file=sys.stdout)
        _remove_lifecycle_marker(marker_root)
        shutil.rmtree(transaction_root, ignore_errors=True)
    return True


def _restore_operations(root: Path, profile_path: Path, backup: Path, manifest: Dict[str, object], include_profile: bool) -> List[Dict[str, object]]:
    entries = {item["source"]: item for item in manifest["files"]}
    operations: List[Dict[str, object]] = []
    for provider, relative in DATABASES.items():
        bundle = manifest["database_bundles"][provider]
        if not bundle["present"]:
            continue
        operations.append({
            "kind": "file", "target": relative, "new_present": True,
            "source_path": str(backup / entries[relative]["archive"]),
            "expected_size": entries[relative]["size"],
            "expected_sha256": entries[relative]["sha256"],
        })
        for suffix in ("-wal", "-shm"):
            source = relative + suffix
            present = bool(bundle["sidecars"][suffix])
            operations.append({
                "kind": "file", "target": source, "new_present": present,
                "source_path": str(backup / entries[source]["archive"]) if present else None,
                "expected_size": entries[source]["size"] if present else None,
                "expected_sha256": entries[source]["sha256"] if present else None,
            })
    if include_profile and manifest.get("profile_included"):
        # The directory source is assembled under the transaction workspace below.
        operations.append({"kind": "directory", "target": "@profile", "new_present": True, "profile_entries": [
            {
                "source_path": str(backup / item["archive"]),
                "relative": item["source"][len(PROFILE_ALIAS) + 1:],
                "expected_size": item["size"],
                "expected_sha256": item["sha256"],
            }
            for item in manifest["files"] if item["kind"] == "douyin-profile"
        ]})
    return operations


def restore_backup(
    root: Path,
    backup: Path,
    profile_path: Optional[Path] = None,
    *,
    include_profile: bool = False,
    safety_output: Optional[Path] = None,
    fail_stage: Optional[str] = None,
) -> Dict[str, object]:
    _assert_default_database_paths()
    root = Path(root).resolve()
    profile_path = _checked_profile_path(root, profile_path)
    backup = Path(os.path.abspath(str(Path(backup).expanduser())))
    backup = backup.parent.resolve() / backup.name
    _assert_output_outside_root(backup, root)
    manifest = _read_and_verify_manifest(backup)
    operations = _restore_operations(root, profile_path, backup, manifest, include_profile)
    if not operations:
        raise LifecycleError("备份中没有可恢复的数据")
    if any(operation["target"] == "@profile" for operation in operations):
        _assert_profile_not_open(profile_path)
    # The pre-restore safety backup includes both databases, even if this
    # particular archive only restores one of them.
    providers = set(DATABASES)
    touches_profile = False
    for operation in operations:
        if operation["target"] == "@profile":
            touches_profile = True
        else:
            for provider, relative in DATABASES.items():
                if str(operation["target"]).startswith(relative):
                    providers.add(provider)
    with _locked_targets(root, providers, profile_path if touches_profile else None):
        _assert_no_pending_marker(root)
        chosen_safety = Path(safety_output) if safety_output else backup.parent / ("before-restore-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        chosen_safety = Path(os.path.abspath(str(chosen_safety.expanduser())))
        _assert_output_outside_root(chosen_safety, root)
        create_backup(root, chosen_safety, profile_path, include_profile=touches_profile, locks_already_held=True)
        transaction_id = _prepare_transaction(root, profile_path, operations, root, fail_stage=fail_stage)
    return {"status": "restored", "transaction_id": transaction_id, "safety_backup": str(chosen_safety)}


def _clear_operations(root: Path, profile_path: Path, target: str, provider: str) -> Tuple[List[Dict[str, object]], set, bool]:
    operations: List[Dict[str, object]] = []
    providers = set()
    touches_profile = False
    if target == "history":
        selected = tuple(DATABASES) if provider == "all" else (provider,)
        for name in selected:
            providers.add(name)
            relative = DATABASES[name]
            database = root / relative
            for candidate in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
                _assert_no_symlink_components(candidate, root)
                if _is_symlink(candidate):
                    raise LifecycleError("安全检查失败：数据库文件是符号链接")
                if candidate.exists():
                    if not candidate.is_file():
                        raise LifecycleError("安全检查失败：数据库目标不是普通文件")
                    operations.append({"kind": "file", "target": candidate.relative_to(root).as_posix(), "new_present": False})
    elif target == "profile":
        touches_profile = True
        if profile_path.exists():
            _walk_regular_files(profile_path)
            operations.append({"kind": "directory", "target": "@profile", "new_present": False})
    elif target == "cache":
        providers.update(DATABASES)
        for relative in CACHE_PATHS:
            candidate = root / relative
            _assert_no_symlink_components(candidate, root)
            if not candidate.exists():
                continue
            if _is_symlink(candidate):
                raise LifecycleError("安全检查失败：缓存目标是符号链接")
            if candidate.is_dir():
                _walk_regular_files(candidate)
                operations.append({"kind": "cache-directory", "target": relative, "new_present": False})
            elif candidate.is_file():
                operations.append({"kind": "file", "target": relative, "new_present": False})
        if profile_path.exists():
            for relative in PROFILE_CACHE_PATHS:
                candidate = profile_path / relative
                if not candidate.exists() and not _is_symlink(candidate):
                    continue
                if _is_symlink(candidate):
                    raise LifecycleError("安全检查失败：浏览器缓存目标是符号链接")
                if candidate.is_dir():
                    _walk_regular_files(candidate)
                    operations.append({"kind": "cache-directory", "target": "@profile-cache/" + relative, "new_present": False})
                elif candidate.is_file():
                    operations.append({"kind": "file", "target": "@profile-cache/" + relative, "new_present": False})
                else:
                    raise LifecycleError("安全检查失败：浏览器缓存目标不是普通文件或目录")
            touches_profile = any(str(item["target"]).startswith("@profile-cache/") for item in operations)
    return operations, providers, touches_profile


def clear_data(
    root: Path,
    target: str,
    profile_path: Optional[Path] = None,
    *,
    provider: str = "all",
    apply: bool = False,
    confirmation: Optional[str] = None,
    fail_stage: Optional[str] = None,
) -> Dict[str, object]:
    if target == "history":
        _assert_default_database_paths()
    root = Path(root).resolve()
    profile_path = _checked_profile_path(root, profile_path)
    if target not in ("history", "profile", "cache"):
        raise LifecycleError("不支持的清理目标")
    if target == "history" and provider not in (*DATABASES, "all"):
        raise LifecycleError("provider 必须为 bilibili、douyin 或 all")
    operations, providers, touches_profile = _clear_operations(root, profile_path, target, provider)
    if not apply:
        return {"status": "preview", "target": target, "files": [str(item["target"]) for item in operations]}
    if confirmation != CONFIRMATIONS[target]:
        raise LifecycleError("确认文字不匹配；没有删除任何内容")
    if target == "profile":
        _assert_profile_not_open(profile_path)
    with _locked_targets(root, providers, profile_path if touches_profile else None):
        _assert_no_pending_marker(root)
        if not operations:
            return {"status": "empty", "target": target, "files": []}
        transaction_id = _prepare_transaction(root, profile_path, operations, root, fail_stage=fail_stage)
    return {"status": "cleared", "target": target, "transaction_id": transaction_id, "files": [str(item["target"]) for item in operations]}


def _cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本机数据的备份、恢复与清理；所有恢复和清理默认只预览")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--profile", type=Path, default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="备份本机数据")
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument("--include-profile", action="store_true", help="把 Douyin 登录 Profile 也放入备份（敏感）")
    backup_parser.add_argument("--dry-run", action="store_true", help="只列出将备份的文件")

    restore_parser = subparsers.add_parser("restore", help="先预览，再显式确认恢复")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--include-profile", action="store_true", help="同时恢复备份中的 Douyin Profile")
    restore_parser.add_argument("--safety-output", type=Path, default=None, help=argparse.SUPPRESS)
    restore_parser.add_argument("--apply", action="store_true")
    restore_parser.add_argument("--confirm", default=None)

    clear_parser = subparsers.add_parser("clear", help="清理本机数据；默认只预览")
    clear_parser.add_argument("--target", choices=("history", "profile", "cache"), required=True)
    clear_parser.add_argument("--provider", choices=("bilibili", "douyin", "all"), default="all")
    clear_parser.add_argument("--apply", action="store_true")
    clear_parser.add_argument("--confirm", default=None)

    recover_parser = subparsers.add_parser("recover", help="恢复中断的数据恢复操作")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _cli()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    profile_path = _checked_profile_path(root, args.profile)
    try:
        if args.command == "backup":
            if args.include_profile and not args.dry_run:
                print("警告：这份备份会包含 Douyin 本机登录状态，请按敏感文件保管。")
            result = create_backup(root, args.output, profile_path, include_profile=args.include_profile, dry_run=args.dry_run)
            if result["status"] == "preview":
                print("备份预览（没有复制文件）：")
                for relative in result["files"]:
                    print("- " + relative)
                if args.include_profile:
                    print("注意：Profile 备份包含 Douyin 本机登录状态。")
            else:
                print("本机数据已备份：" + str(result["output"]))
                print("manifest：" + str(Path(result["output"]) / "manifest.json"))
        elif args.command == "restore":
            _assert_default_database_paths()
            manifest = _read_and_verify_manifest(args.backup)
            operations = _restore_operations(root, profile_path, args.backup, manifest, args.include_profile)
            print("恢复预览（此命令尚未修改现有数据）：")
            print("备份创建时间：" + str(manifest.get("created_at", "未知")))
            for operation in operations:
                if operation["target"] == "@profile":
                    target = "Douyin 本机登录 Profile"
                    action = "替换" if profile_path.exists() else "新增"
                else:
                    destination = _operation_target(root, profile_path, operation)
                    target = str(operation["target"])
                    action = "移除" if not operation.get("new_present") else ("覆盖" if destination.exists() else "新增")
                print("- 将" + action + "：" + target)
            print("目标项目：" + str(root))
            print("恢复前会自动建立当前状态保护备份。")
            if manifest.get("profile_included") and not args.include_profile:
                print("备份含有 Profile；本次未选择恢复它。")
            if not args.apply:
                return 0
            if args.confirm != CONFIRMATIONS["restore"]:
                raise LifecycleError("恢复需要同时提供 --apply --confirm RESTORE")
            if args.include_profile:
                print("警告：已选择替换 Douyin 本机 Profile；原登录状态会由恢复前保护备份保存。")
            result = restore_backup(root, args.backup, profile_path, include_profile=args.include_profile, safety_output=args.safety_output)
            print("恢复完成。当前状态保护备份：" + str(result["safety_backup"]))
        elif args.command == "clear":
            if args.target == "profile" and args.apply:
                print("警告：清除此 Profile 会退出本机 Douyin 登录状态。")
            result = clear_data(
                root, args.target, profile_path, provider=args.provider,
                apply=args.apply, confirmation=args.confirm,
            )
            if result["status"] == "preview":
                print("清理预览（没有删除任何内容）：")
                if args.target == "profile":
                    print("警告：清除此 Profile 会退出本机 Douyin 登录状态。")
                for relative in result["files"]:
                    visible = "Douyin 本机登录 Profile" if relative == "@profile" else str(relative)
                    if str(relative).startswith("@profile-cache/"):
                        visible = "Douyin 浏览器可重建缓存 / " + str(relative).split("/", 1)[1]
                    print("- " + visible)
                print("真正执行时还需 --apply 和对应的 --confirm 确认文字。")
            elif result["status"] == "empty":
                print("没有找到需要清理的目标。")
            else:
                print("已完成清理：" + args.target)
        else:
            if not recover_lifecycle(root, profile_path):
                print("没有待恢复的数据操作。")
        return 0
    except (LifecycleError, OSError, ValueError) as error:
        print("未执行完成：" + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
