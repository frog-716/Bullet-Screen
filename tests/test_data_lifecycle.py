import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "data_lifecycle.py"


class DataLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.profile = self.root / "douyin" / "data" / "browser-profile"
        self.profile.mkdir(parents=True)
        (self.profile / "login-state.fake").write_text("synthetic-session", encoding="utf-8")
        self.db_paths = {
            "bilibili": self.root / "bilibili" / "data" / "danmaku.sqlite3",
            "douyin": self.root / "douyin" / "data" / "danmaku.sqlite3",
        }
        for provider, database in self.db_paths.items():
            database.parent.mkdir(parents=True, exist_ok=True)
            database.write_bytes((provider + "-main-fixture").encode())
            database.with_name(database.name + "-wal").write_bytes((provider + "-wal-fixture").encode())
            database.with_name(database.name + "-shm").write_bytes((provider + "-shm-fixture").encode())

    def run_cli(self, *args, expected=0, environment=None):
        env = os.environ.copy()
        if environment:
            env.update(environment)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), "--profile", str(self.profile), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def make_backup(self, output, *extra):
        self.run_cli("backup", "--output", output, *extra)
        return output

    def test_backup_dry_run_does_not_create_package(self):
        output = self.root.parent / "preview-backup"
        result = self.run_cli("backup", "--output", output, "--dry-run")
        self.assertIn("预览", result.stdout)
        self.assertFalse(output.exists())

    def test_interrupted_backup_removes_incomplete_staging_directory(self):
        from unittest.mock import patch

        output = self.root.parent / "interrupted-backup"
        with patch("scripts.data_lifecycle._copy_hash", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                from scripts.data_lifecycle import create_backup
                create_backup(self.root, output, self.profile)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.parent.glob(".bullet-screen-backup-*")))

    def test_backup_refuses_unmanaged_custom_database_path(self):
        output = self.root.parent / "custom-db-backup"
        result = self.run_cli(
            "backup", "--dry-run", "--output", output, expected=2,
            environment={"BULLET_SCREEN_DB": str(self.root.parent / "outside.sqlite3")},
        )
        self.assertIn("BULLET_SCREEN_DB", result.stdout + result.stderr)
        self.assertFalse(output.exists())

    def test_history_clear_refuses_unmanaged_custom_database_path(self):
        original = self.db_paths["bilibili"].read_bytes()
        result = self.run_cli(
            "clear", "--target", "history", "--provider", "all", expected=2,
            environment={"BULLET_SCREEN_DB": str(self.root.parent / "outside.sqlite3")},
        )
        self.assertIn("BULLET_SCREEN_DB", result.stdout + result.stderr)
        self.assertEqual(self.db_paths["bilibili"].read_bytes(), original)

    def test_backup_manifest_lists_files_and_hashes(self):
        output = self.root.parent / "backup"
        self.make_backup(output)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        entries = {item["source"]: item for item in manifest["files"]}
        self.assertIn("bilibili/data/danmaku.sqlite3", entries)
        self.assertIn("bilibili/data/danmaku.sqlite3-wal", entries)
        self.assertIn("bilibili/data/danmaku.sqlite3-shm", entries)
        self.assertEqual(manifest["schema_versions"], {"bilibili": None, "douyin": None})
        for item in manifest["files"]:
            payload = output / item["archive"]
            self.assertEqual(payload.stat().st_size, item["size"])
            self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), item["sha256"])

    def test_backup_copies_database_bundle_as_opaque_files(self):
        output = self.root.parent / "opaque-backup"
        self.make_backup(output)
        for provider, database in self.db_paths.items():
            for suffix in ("", "-wal", "-shm"):
                source = database if not suffix else database.with_name(database.name + suffix)
                relative = "files/" + source.relative_to(self.root).as_posix()
                self.assertEqual((output / relative).read_bytes(), source.read_bytes())

    def test_profile_is_not_backed_up_without_explicit_opt_in(self):
        output = self.root.parent / "backup-no-profile"
        self.make_backup(output)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["profile_included"])
        self.assertFalse(any("browser-profile" in item["source"] for item in manifest["files"]))

    def test_profile_backup_is_explicit_and_marked_sensitive(self):
        output = self.root.parent / "backup-with-profile"
        self.make_backup(output, "--include-profile")
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        profile_entry = next(item for item in manifest["files"] if "browser-profile/" in item["source"])
        self.assertTrue(manifest["profile_included"])
        self.assertEqual(profile_entry["sensitivity"], "douyin-login-state")

    def test_backup_profile_lock_refuses_copy(self):
        from scripts.data_lifecycle import acquire_profile_lock, release_lifecycle_lock

        lock = acquire_profile_lock(self.profile)
        try:
            result = self.run_cli("backup", "--output", self.root.parent / "blocked-profile-backup", "--include-profile", expected=2)
            self.assertIn("正在使用", result.stdout + result.stderr)
        finally:
            release_lifecycle_lock(lock)

    def test_backup_refuses_running_database_lock(self):
        database = self.db_paths["bilibili"]
        lock_path = database.with_name(database.name + ".lock")
        lock_path.touch()
        ready = self.root.parent / "lock-ready"
        release = self.root.parent / "lock-release"
        holder_code = (
            "import fcntl,sys,time; h=open(sys.argv[1],'r+'); "
            "fcntl.flock(h.fileno(),fcntl.LOCK_EX); open(sys.argv[2],'w').close(); "
            "\nwhile not __import__('pathlib').Path(sys.argv[3]).exists(): time.sleep(.01)"
        )
        holder = subprocess.Popen([sys.executable, "-c", holder_code, str(lock_path), str(ready), str(release)])
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "lock holder did not start")
            result = self.run_cli("backup", "--output", self.root.parent / "blocked-backup", expected=2)
            self.assertIn("正在使用", result.stdout + result.stderr)
        finally:
            release.touch()
            holder.wait(timeout=3)

    def test_restore_defaults_to_dry_run(self):
        backup = self.make_backup(self.root.parent / "backup")
        (self.db_paths["bilibili"]).write_bytes(b"current-target")
        before = self.db_paths["bilibili"].read_bytes()
        result = self.run_cli("restore", "--backup", backup)
        self.assertIn("预览", result.stdout)
        self.assertIn("bilibili/data/danmaku.sqlite3", result.stdout)
        self.assertEqual(self.db_paths["bilibili"].read_bytes(), before)

    def test_restore_requires_explicit_apply_and_confirmation(self):
        backup = self.make_backup(self.root.parent / "backup")
        preview = self.run_cli("restore", "--backup", backup, "--confirm", "RESTORE")
        self.assertIn("恢复预览", preview.stdout)
        wrong = self.run_cli("restore", "--backup", backup, "--apply", "--confirm", "CLEAR", expected=2)
        self.assertIn("--confirm RESTORE", wrong.stderr)

    def test_restore_apply_protects_current_targets_first(self):
        backup = self.make_backup(self.root.parent / "backup")
        self.db_paths["bilibili"].write_bytes(b"current-before-restore")
        safety = self.root.parent / "before-restore"
        self.run_cli("restore", "--backup", backup, "--apply", "--confirm", "RESTORE", "--safety-output", safety)
        self.assertEqual(self.db_paths["bilibili"].read_bytes(), b"bilibili-main-fixture")
        safety_manifest = json.loads((safety / "manifest.json").read_text(encoding="utf-8"))
        saved = next(item for item in safety_manifest["files"] if item["source"] == "bilibili/data/danmaku.sqlite3")
        self.assertEqual((safety / saved["archive"]).read_bytes(), b"current-before-restore")

    def test_restore_profile_requires_explicit_opt_in(self):
        backup = self.make_backup(self.root.parent / "backup-with-profile", "--include-profile")
        profile_file = self.profile / "login-state.fake"
        profile_file.write_text("new-local-state", encoding="utf-8")
        self.run_cli("restore", "--backup", backup, "--apply", "--confirm", "RESTORE")
        self.assertEqual(profile_file.read_text(encoding="utf-8"), "new-local-state")
        self.run_cli("restore", "--backup", backup, "--include-profile", "--apply", "--confirm", "RESTORE")
        self.assertEqual(profile_file.read_text(encoding="utf-8"), "synthetic-session")
        self.assertFalse(list(self.root.glob(".bullet-screen-profile-stage-*")))

    def test_cache_clear_rejects_symlinked_profile_ancestor(self):
        outside = self.root.parent / "outside-profile"
        outside_cache = outside / "Default" / "Cache"
        outside_cache.mkdir(parents=True)
        sentinel = outside_cache / "keep.fixture"
        sentinel.write_bytes(b"must survive")
        (self.profile / "Default").symlink_to(outside / "Default", target_is_directory=True)

        result = self.run_cli("clear", "--target", "cache", "--apply", "--confirm", "CLEAR-CACHE", expected=2)

        self.assertIn("符号链接", result.stdout + result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"must survive")

    def test_restore_interruption_is_recoverable(self):
        from scripts import data_lifecycle

        backup = self.make_backup(self.root.parent / "backup")
        original = {name: path.read_bytes() for name, path in self.db_paths.items()}
        with self.assertRaises(data_lifecycle.SimulatedInterruption):
            data_lifecycle.restore_backup(
                self.root,
                backup,
                self.profile,
                safety_output=self.root.parent / "before-interrupted-restore",
                fail_stage="after-first-switch",
            )
        self.assertTrue(data_lifecycle.has_pending_lifecycle(self.root))
        self.assertTrue(data_lifecycle.recover_lifecycle(self.root, self.profile))
        for name, path in self.db_paths.items():
            self.assertEqual(path.read_bytes(), original[name])
        self.assertFalse(data_lifecycle.has_pending_lifecycle(self.root))

    def test_interrupted_prepare_is_recoverable_without_target_changes(self):
        from scripts import data_lifecycle

        backup = self.make_backup(self.root.parent / "backup")
        before = {path: path.read_bytes() for path in self.db_paths.values()}
        with self.assertRaises(data_lifecycle.SimulatedInterruption):
            data_lifecycle.restore_backup(
                self.root,
                backup,
                self.profile,
                safety_output=self.root.parent / "before-prepare-interruption",
                fail_stage="during-prepare",
            )
        self.assertEqual({path: path.read_bytes() for path in self.db_paths.values()}, before)
        self.assertTrue(data_lifecycle.has_pending_lifecycle(self.root))
        self.assertTrue(data_lifecycle.recover_lifecycle(self.root, self.profile))
        self.assertFalse(data_lifecycle.has_pending_lifecycle(self.root))
        self.assertFalse(list((self.root / data_lifecycle.LIFECYCLE_DIRNAME).glob("*/journal.json")))

    def test_restore_switch_error_rolls_back_before_returning(self):
        from scripts import data_lifecycle

        backup = self.make_backup(self.root.parent / "backup")
        before = {
            path: path.read_bytes()
            for database in self.db_paths.values()
            for path in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm"))
        }
        with self.assertRaisesRegex(OSError, "simulated switch failure"):
            data_lifecycle.restore_backup(
                self.root,
                backup,
                self.profile,
                safety_output=self.root.parent / "before-switch-error",
                fail_stage="switch-error-after-first",
            )
        self.assertFalse(data_lifecycle.has_pending_lifecycle(self.root))
        for path, value in before.items():
            self.assertEqual(path.read_bytes(), value)
        self.assertFalse(list((self.root / data_lifecycle.LIFECYCLE_DIRNAME).glob("*/journal.json")))

    def test_restore_rejects_payload_hash_mismatch(self):
        backup = self.make_backup(self.root.parent / "backup")
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        item = manifest["files"][0]
        payload = backup / item["archive"]
        payload.write_bytes(b"x" * item["size"])
        result = self.run_cli("restore", "--backup", backup, expected=2)
        self.assertIn("校验", result.stdout + result.stderr)

    def test_complete_temp_fixture_backup_restore_and_clear_flow(self):
        backup = self.make_backup(self.root.parent / "full-flow-backup", "--include-profile")
        original_db = {provider: path.read_bytes() for provider, path in self.db_paths.items()}
        original_profile = (self.profile / "login-state.fake").read_bytes()
        for path in self.db_paths.values():
            path.write_bytes(b"changed-after-backup")
        (self.profile / "login-state.fake").write_bytes(b"changed-profile")

        self.run_cli("restore", "--backup", backup, "--include-profile")
        self.assertEqual(self.db_paths["bilibili"].read_bytes(), b"changed-after-backup")
        self.run_cli("restore", "--backup", backup, "--include-profile", "--apply", "--confirm", "RESTORE")
        for provider, path in self.db_paths.items():
            self.assertEqual(path.read_bytes(), original_db[provider])
        self.assertEqual((self.profile / "login-state.fake").read_bytes(), original_profile)

        self.run_cli("clear", "--target", "history", "--provider", "all")
        self.run_cli("clear", "--target", "history", "--provider", "all", "--apply", "--confirm", "CLEAR-HISTORY")
        self.assertFalse(any(path.exists() for path in self.db_paths.values()))
        self.assertTrue(self.profile.exists())
        self.run_cli("clear", "--target", "profile")
        self.run_cli("clear", "--target", "profile", "--apply", "--confirm", "CLEAR-PROFILE")
        self.assertFalse(self.profile.exists())

    def test_v2_startup_guidance_executes_in_swift(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main.swift"
            executable = Path(directory) / "guidance-test"
            main.write_text(
                'import Foundation\n'
                'print(LauncherPreflight.startupGuidance(for: "Douyin database schema 2 requires an explicit migration to 4") ?? "none")\n'
                'print(LauncherPreflight.startupGuidance(for: "v4 schema mismatch") ?? "none")\n',
                encoding="utf-8",
            )
            built = subprocess.run(
                ["swiftc", str(ROOT / "macos" / "LauncherLifecycle.swift"), str(main), "-o", str(executable)],
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertIn("发现旧版数据。数据没有被自动修改", lines[0])
            self.assertEqual(lines[1], "none")

    def test_restore_rejects_manifest_path_escape(self):
        backup = self.make_backup(self.root.parent / "backup")
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["source"] = "../../outside"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_cli("restore", "--backup", backup, expected=2)
        self.assertIn("路径", result.stdout + result.stderr)

    def test_backup_rejects_symlink_source(self):
        linked = self.root / "bilibili" / "data" / "danmaku.sqlite3-wal"
        linked.unlink()
        linked.symlink_to(self.root / "outside")
        (self.root / "outside").write_text("outside", encoding="utf-8")
        result = self.run_cli("backup", "--output", self.root.parent / "symlink-backup", expected=2)
        self.assertIn("符号链接", result.stderr + result.stdout)

    def test_restore_rejects_symlink_payload(self):
        backup = self.make_backup(self.root.parent / "backup")
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        item = manifest["files"][0]
        payload = backup / item["archive"]
        payload.unlink()
        payload.symlink_to(self.root / "outside")
        result = self.run_cli("restore", "--backup", backup, expected=2)
        self.assertIn("符号链接", result.stdout + result.stderr)

    def test_clear_cache_removes_browser_cache_but_keeps_profile_login_file(self):
        cache = self.profile / "Default" / "Cache"
        cache.mkdir(parents=True)
        (cache / "cache.fixture").write_bytes(b"cache")
        self.run_cli("clear", "--target", "cache", "--apply", "--confirm", "CLEAR-CACHE")
        self.assertFalse(cache.exists())
        self.assertTrue((self.profile / "login-state.fake").exists())

    def test_clear_history_defaults_to_dry_run(self):
        path = self.db_paths["bilibili"]
        before = path.read_bytes()
        result = self.run_cli("clear", "--target", "history", "--provider", "bilibili")
        self.assertIn("预览", result.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_clear_history_apply_requires_confirmation(self):
        result = self.run_cli("clear", "--target", "history", "--provider", "bilibili", "--apply", expected=2)
        self.assertIn("确认文字", result.stderr)
        self.assertTrue(self.db_paths["bilibili"].exists())

    def test_clear_profile_dry_run_explains_login_logout(self):
        result = self.run_cli("clear", "--target", "profile")
        self.assertIn("退出本机 Douyin 登录状态", result.stdout)
        self.assertTrue((self.profile / "login-state.fake").exists())

    def test_clear_profile_requires_exact_confirmation(self):
        missing = self.run_cli("clear", "--target", "profile", "--apply", expected=2)
        wrong = self.run_cli("clear", "--target", "profile", "--apply", "--confirm", "CLEAR", expected=2)
        self.assertIn("确认", missing.stdout + missing.stderr)
        self.assertIn("确认", wrong.stdout + wrong.stderr)
        self.assertTrue((self.profile / "login-state.fake").exists())

    def test_clear_profile_apply_does_not_clear_history(self):
        database_before = self.db_paths["douyin"].read_bytes()
        self.run_cli("clear", "--target", "profile", "--apply", "--confirm", "CLEAR-PROFILE")
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.db_paths["douyin"].read_bytes(), database_before)

    def test_clear_history_does_not_clear_profile(self):
        self.run_cli("clear", "--target", "history", "--provider", "douyin", "--apply", "--confirm", "CLEAR-HISTORY")
        self.assertFalse(self.db_paths["douyin"].exists())
        self.assertTrue((self.profile / "login-state.fake").exists())

    def test_cache_clear_only_removes_named_regenerable_cache(self):
        cache = self.root / "douyin" / "__pycache__"
        cache.mkdir()
        (cache / "fixture.pyc").write_bytes(b"cache")
        self.run_cli("clear", "--target", "cache", "--apply", "--confirm", "CLEAR-CACHE")
        self.assertFalse(cache.exists())
        self.assertTrue(self.db_paths["douyin"].exists())
        self.assertTrue(self.profile.exists())

    def test_cache_clear_is_dry_run_by_default(self):
        cache = self.root / "tests" / "__pycache__"
        cache.mkdir(parents=True)
        fixture = cache / "x.pyc"
        fixture.write_bytes(b"cache")
        result = self.run_cli("clear", "--target", "cache")
        self.assertIn("预览", result.stdout)
        self.assertTrue(fixture.exists())

    def test_lifecycle_marker_blocks_new_operations(self):
        from scripts.data_lifecycle import LifecycleError, assert_no_pending_lifecycle

        marker = self.root / ".bullet-screen-data-lifecycle-in-progress"
        marker.write_text('{"transaction_id":"fixture"}', encoding="utf-8")
        with self.assertRaises(LifecycleError):
            assert_no_pending_lifecycle(self.root)

    def test_history_clear_refuses_symlink_instead_of_following_it(self):
        outside = self.root.parent / "outside-history"
        outside.write_bytes(b"preserve")
        database = self.db_paths["bilibili"]
        database.unlink()
        database.symlink_to(outside)
        result = self.run_cli("clear", "--target", "history", "--provider", "bilibili", expected=2)
        self.assertIn("符号链接", result.stderr + result.stdout)
        self.assertEqual(outside.read_bytes(), b"preserve")

    def test_data_document_describes_actual_paths_and_uninstall(self):
        document = (ROOT / "docs" / "DATA.md").read_text(encoding="utf-8")
        self.assertIn("bilibili/data/danmaku.sqlite3", document)
        self.assertIn("douyin/data/danmaku.sqlite3", document)
        self.assertIn("douyin/data/browser-profile/", document)
        self.assertIn("卸载 App 不等于删除数据", document)

    def test_old_v3_v4_database_guidance_is_plain_and_conservative(self):
        document = (ROOT / "docs" / "DATA.md").read_text(encoding="utf-8")
        self.assertIn("v3 也不能由当前 v4 服务直接写入", document)
        self.assertIn("v4 文件仍需通过当前结构签名检查", document)
        self.assertIn("不会被服务自动升级", document)

    def test_data_document_does_not_claim_real_v2_to_v4_verified(self):
        document = (ROOT / "docs" / "DATA.md").read_text(encoding="utf-8")
        self.assertIn("没有用真实 v2 数据验证", document)
        self.assertIn("不会自动迁移", document)

    def test_readme_links_data_document_and_removes_obsolete_no_tools_claim(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/DATA.md", readme)
        self.assertNotIn("数据备份、清理和卸载的完整用户流程尚未", readme)

    def test_v2_startup_guidance_is_plain_language(self):
        source = (ROOT / "macos" / "LauncherLifecycle.swift").read_text(encoding="utf-8")
        self.assertIn("发现旧版数据", source)
        self.assertIn("数据没有被自动修改", source)
        self.assertIn("先使用 Demo", source)

    def test_launcher_keeps_old_database_guidance_after_server_exits(self):
        source = (ROOT / "macos" / "BulletScreenLauncher.swift").read_text(encoding="utf-8")
        termination = source.split("private func handleTermination", 1)[1].split("private func cleanupRun", 1)[0]
        self.assertIn("startupGuidance(for: detail)", termination)


if __name__ == "__main__":
    unittest.main()
