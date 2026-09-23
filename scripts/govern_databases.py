#!/usr/bin/env python3
"""Govern the two local SQLite stores without losing historical live data.

The legacy v2 -> v3 and explicit v3 -> v4 migrations are separate.  Apply
requires an explicit target version, retains a source backup, and switches
only verified isolated candidates; any later cleanup is a separate approved
operation after independent verification.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BILIBILI_DB = ROOT / "bilibili" / "data" / "danmaku.sqlite3"
DEFAULT_DOUYIN_DB = ROOT / "douyin" / "data" / "danmaku.sqlite3"
LEGACY_LIVE_TABLES = ("live_metric_snapshots", "live_events", "live_sessions")
# Keep the original v2 -> v3 rehearsal contract intact; v4 is an explicit,
# separate migration that only adds the signal domain tables.
SCHEMA_VERSION = 3
V4_SCHEMA_VERSION = 4
from schema_v4 import V4_TABLES, create_signal_schema, verify_signal_schema


class MigrationConflict(RuntimeError):
    """A source row and an existing target row share an identity but differ."""


class RehearsalFailure(RuntimeError):
    """Synthetic failure injected into an isolated migration rehearsal."""


class SchemaVersionError(RuntimeError):
    """A migration source or target has an unsupported schema version."""


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {path}")
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def backup(connection: sqlite3.Connection, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        connection.backup(target)
    finally:
        target.close()


def _acquire_database_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError(f"database is already in use: {path}") from error
    return handle


def _acquire_database_locks(paths: Iterable[Path]):
    """Acquire a database set in one canonical order, unwinding partial locks."""
    handles = []
    ordered = sorted({Path(path) for path in paths}, key=lambda path: str(path.resolve()))
    try:
        for path in ordered:
            handles.append(_acquire_database_lock(path))
    except BaseException:
        _release_database_locks(handles)
        raise
    return handles


def _release_database_locks(handles) -> None:
    for handle in reversed(handles):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _copy_database_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        source_sidecar = source.with_name(source.name + suffix)
        if source_sidecar.exists():
            shutil.copy2(source_sidecar, destination.with_name(destination.name + suffix))


def database_signature(connection: sqlite3.Connection) -> Dict[str, Tuple[int, str]]:
    signature: Dict[str, Tuple[int, str]] = {}
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in tables:
        table = row[0]
        digest = hashlib.sha256()
        count = 0
        for values in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            digest.update(repr(tuple(values)).encode("utf-8"))
            digest.update(b"\n")
            count += 1
        signature[table] = (count, digest.hexdigest())
    return signature


def _verify_table_columns(connection: sqlite3.Connection, table: str, required: Iterable[str]) -> None:
    actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    missing = set(required) - actual
    if missing:
        raise RuntimeError(f"schema verification failed for {table}: missing {sorted(missing)}")


def _verify_index(connection: sqlite3.Connection, table: str, name: str, expected_columns: Tuple[str, ...]) -> None:
    index_row = next((row for row in connection.execute(f"PRAGMA index_list({table})") if row[1] == name), None)
    if not index_row:
        raise RuntimeError(f"schema verification failed: missing index {name}")
    actual_columns = tuple(row[2] for row in connection.execute(f"PRAGMA index_info({name})"))
    if actual_columns != expected_columns:
        raise RuntimeError(f"schema verification failed: index {name} has {actual_columns}")


def verify_schema_structure(connection: sqlite3.Connection) -> None:
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if {"sessions", "events", "metric_snapshots", "capture_gaps"}.issubset(tables):
        required_columns = {
            "sessions": ("id", "room_id", "room_title", "started_at", "ended_at", "status"),
            "events": ("id", "session_id", "event_type", "event_time", "uid", "uname", "text", "gift_name", "gift_num", "amount", "popularity"),
            "metric_snapshots": ("id", "session_id", "recorded_at", "online", "likes", "danmaku_rate", "total_danmaku"),
            "capture_gaps": ("id", "provider", "room_id", "session_id", "run_id", "gap_start", "gap_end", "reason", "source", "status"),
        }
        indexes = (
            ("events", "idx_events_session_time", ("session_id", "event_time")),
            ("events", "idx_events_type", ("session_id", "event_type")),
            ("metric_snapshots", "idx_metrics_session_time", ("session_id", "recorded_at")),
            ("capture_gaps", "idx_capture_gaps_session_time", ("session_id", "gap_start", "gap_end")),
        )
        foreign_key_tables = ("events", "metric_snapshots", "capture_gaps")
    elif {"live_sessions", "live_events", "live_metric_snapshots", "capture_gaps"}.issubset(tables):
        required_columns = {
            "live_sessions": ("id", "provider", "room_id", "room_title", "room_url", "started_at", "ended_at", "status"),
            "live_events": ("id", "event_id", "session_id", "provider", "room_id", "event_type", "event_time", "user_id", "user_name", "content", "metadata_json", "topic", "intent", "sentiment", "purchase_intent"),
            "live_metric_snapshots": ("id", "session_id", "recorded_at", "online", "comment_rate", "like_rate", "gift_rate", "active_users", "heat_score", "purchase_ratio", "positive_ratio", "negative_ratio"),
            "capture_gaps": ("id", "provider", "room_id", "session_id", "run_id", "gap_start", "gap_end", "reason", "source", "status"),
        }
        indexes = (
            ("live_events", "idx_live_events_session_time", ("session_id", "event_time")),
            ("live_events", "idx_live_events_type", ("session_id", "event_type")),
            ("live_metric_snapshots", "idx_live_metrics_session_time", ("session_id", "recorded_at")),
            ("capture_gaps", "idx_capture_gaps_session_time", ("session_id", "gap_start", "gap_end")),
        )
        foreign_key_tables = ("live_events", "live_metric_snapshots", "capture_gaps")
        unique_event_id = any(
            int(row[2]) == 1 and tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]})")) == ("event_id",)
            for row in connection.execute("PRAGMA index_list(live_events)")
        )
        if not unique_event_id:
            raise RuntimeError("schema verification failed: live_events.event_id is not unique")
    elif set(LEGACY_LIVE_TABLES).issubset(tables):
        return verify_schema_structure_for_legacy(connection)
    else:
        raise RuntimeError(f"schema verification failed: unsupported tables {sorted(tables)}")
    for table, required in required_columns.items():
        _verify_table_columns(connection, table, required)
    for table, name, expected_columns in indexes:
        _verify_index(connection, table, name, expected_columns)
    session_table = "sessions" if "sessions" in tables else "live_sessions"
    expected_foreign_keys = {
        "events": ("sessions", "session_id", "id"),
        "metric_snapshots": ("sessions", "session_id", "id"),
        "capture_gaps": (session_table, "session_id", "id"),
        "live_events": ("live_sessions", "session_id", "id"),
        "live_metric_snapshots": ("live_sessions", "session_id", "id"),
    }
    for table in foreign_key_tables:
        actual = {(row[2], row[3], row[4]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
        expected = {expected_foreign_keys[table]}
        if actual != expected:
            raise RuntimeError(f"schema verification failed: foreign keys for {table} are {actual}")


def verify_schema_structure_for_legacy(connection: sqlite3.Connection) -> None:
    required_columns = {
        "live_sessions": ("id", "provider", "room_id", "room_title", "room_url", "started_at", "ended_at", "status"),
        "live_events": ("id", "event_id", "session_id", "provider", "room_id", "event_type", "event_time", "user_id", "user_name", "content", "metadata_json", "topic", "intent", "sentiment", "purchase_intent"),
        "live_metric_snapshots": ("id", "session_id", "recorded_at", "online", "comment_rate", "like_rate", "gift_rate", "active_users", "heat_score", "purchase_ratio", "positive_ratio", "negative_ratio"),
    }
    for table, required in required_columns.items():
        _verify_table_columns(connection, table, required)


def verify_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"integrity_check failed: {integrity}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"foreign_key_check failed: {foreign_key_errors[:3]}")


def verify_database(connection: sqlite3.Connection) -> None:
    verify_integrity(connection)
    verify_schema_structure(connection)


def verify_backup(source: sqlite3.Connection, destination: Path) -> None:
    target = connect(destination)
    try:
        verify_integrity(target)
        if database_signature(source) != database_signature(target):
            raise RuntimeError(f"backup verification failed: {destination}")
    finally:
        target.close()


def create_capture_gap_schema(connection: sqlite3.Connection, session_table: str) -> None:
    if session_table not in {"sessions", "live_sessions"}:
        raise ValueError(f"unsupported session table: {session_table}")
    connection.executescript(
        f"""CREATE TABLE IF NOT EXISTS capture_gaps(
          id INTEGER PRIMARY KEY, provider TEXT NOT NULL, room_id TEXT NOT NULL,
          session_id INTEGER, run_id TEXT, gap_start TEXT NOT NULL, gap_end TEXT,
          reason TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'collector',
          status TEXT NOT NULL DEFAULT 'open',
          FOREIGN KEY(session_id) REFERENCES {session_table}(id)
        );
        CREATE INDEX IF NOT EXISTS idx_capture_gaps_session_time
          ON capture_gaps(session_id, gap_start, gap_end);"""
    )
    connection.commit()


def _fail_if_requested(fail_stage: str | None, stage: str) -> None:
    if fail_stage == stage:
        raise RehearsalFailure(stage)


def _write_manifest(path: Path, payload: Dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def rehearse_migration(
    bilibili_path: Path,
    douyin_path: Path,
    workspace: Path,
    fail_stage: str | None = None,
) -> Dict[str, object]:
    """Copy, verify, migrate, verify, and atomically switch only a rehearsal manifest."""

    bilibili_path = Path(bilibili_path)
    douyin_path = Path(douyin_path)
    workspace = Path(workspace)
    if not bilibili_path.exists() or not douyin_path.exists():
        missing = bilibili_path if not bilibili_path.exists() else douyin_path
        raise FileNotFoundError(f"database does not exist: {missing}")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"rehearsal workspace is not empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    backup_dir = workspace / "backup"
    staging_dir = workspace / "staging"
    switch_dir = workspace / "switch"
    backup_dir.mkdir()
    staging_dir.mkdir()
    switch_dir.mkdir()

    source_dir = workspace / "source"
    source_dir.mkdir()
    source_locks = []
    source_bilibili = None
    source_douyin = None
    try:
        for source_path in (bilibili_path, douyin_path):
            source_locks.append(_acquire_database_lock(source_path))
        source_bilibili_path = source_dir / "bilibili.sqlite3"
        source_douyin_path = source_dir / "douyin.sqlite3"
        _copy_database_snapshot(bilibili_path, source_bilibili_path)
        _copy_database_snapshot(douyin_path, source_douyin_path)
        source_bilibili = connect(source_bilibili_path)
        source_douyin = connect(source_douyin_path)
        backup(source_bilibili, backup_dir / "bilibili.sqlite3")
        backup(source_douyin, backup_dir / "douyin.sqlite3")
        backup_bilibili = backup_dir / "bilibili.sqlite3"
        backup_douyin = backup_dir / "douyin.sqlite3"
        verify_backup(source_bilibili, backup_bilibili)
        verify_backup(source_douyin, backup_douyin)
        backup(source_bilibili, staging_dir / "bilibili.sqlite3")
        backup(source_douyin, staging_dir / "douyin.sqlite3")
    finally:
        if source_bilibili is not None:
            source_bilibili.close()
        if source_douyin is not None:
            source_douyin.close()
        for lock in reversed(source_locks):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
    _fail_if_requested(fail_stage, "copy")

    staged_bilibili = connect(staging_dir / "bilibili.sqlite3")
    staged_douyin = connect(staging_dir / "douyin.sqlite3")
    try:
        verify_database(staged_bilibili)
        verify_database(staged_douyin)
    finally:
        staged_bilibili.close()
        staged_douyin.close()
    _fail_if_requested(fail_stage, "verify-before")

    staged_bilibili = connect(staging_dir / "bilibili.sqlite3")
    staged_douyin = connect(staging_dir / "douyin.sqlite3")
    try:
        create_douyin_schema(staged_douyin)
        create_capture_gap_schema(staged_bilibili, "sessions" if table_exists(staged_bilibili, "sessions") else "live_sessions")
        rebuild_douyin_events(staged_douyin, True)
        legacy_counts = migrate_legacy_live(staged_bilibili, staged_douyin, True)
        bilibili_events = rebuild_bilibili_events(staged_bilibili, True)
        staged_bilibili.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        staged_douyin.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        staged_bilibili.commit()
        staged_douyin.commit()
    finally:
        staged_bilibili.close()
        staged_douyin.close()
    _fail_if_requested(fail_stage, "migration")

    staged_bilibili = connect(staging_dir / "bilibili.sqlite3")
    staged_douyin = connect(staging_dir / "douyin.sqlite3")
    try:
        verify_database(staged_bilibili)
        verify_database(staged_douyin)
        staged_signatures = {
            "bilibili": database_signature(staged_bilibili),
            "douyin": database_signature(staged_douyin),
        }
    finally:
        staged_bilibili.close()
        staged_douyin.close()
    _fail_if_requested(fail_stage, "verify-after")

    _fail_if_requested(fail_stage, "switch-before")
    manifest = switch_dir / "active.json"
    temporary_payload = {
        "status": "ready",
        "schema_version": SCHEMA_VERSION,
        "backup": {"bilibili": str(backup_bilibili), "douyin": str(backup_douyin)},
        "candidate": {"bilibili": str(staging_dir / "bilibili.sqlite3"), "douyin": str(staging_dir / "douyin.sqlite3")},
        "signatures": staged_signatures,
        "legacy_counts": legacy_counts,
        "bilibili_events_rebuilt": bilibili_events,
    }
    temporary_manifest = manifest.with_suffix(manifest.suffix + ".tmp")
    with temporary_manifest.open("w", encoding="utf-8") as stream:
        json.dump(temporary_payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    _fail_if_requested(fail_stage, "switch")
    os.replace(temporary_manifest, manifest)
    return {"status": "switched-simulation", "switch_manifest": str(manifest), "workspace": str(workspace)}


def rebuild_bilibili_events(connection: sqlite3.Connection, apply: bool) -> bool:
    if not table_exists(connection, "events"):
        return False
    old_columns = columns(connection, "events")
    removable = {"received_at", "raw_json"}.intersection(old_columns)
    if not removable:
        return False
    if not apply:
        return True
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute(
            """CREATE TABLE events__governed(
              id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, event_type TEXT NOT NULL,
              event_time TEXT NOT NULL, uid INTEGER, uname TEXT, text TEXT,
              gift_name TEXT, gift_num INTEGER, amount INTEGER, popularity INTEGER,
              FOREIGN KEY(session_id) REFERENCES sessions(id)
            )"""
        )
        connection.execute(
            """INSERT INTO events__governed(
              id,session_id,event_type,event_time,uid,uname,text,gift_name,gift_num,amount,popularity)
              SELECT id,session_id,event_type,event_time,uid,uname,text,gift_name,gift_num,amount,popularity
              FROM events"""
        )
        connection.execute("DROP TABLE events")
        connection.execute("ALTER TABLE events__governed RENAME TO events")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_session_time ON events(session_id, event_time)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(session_id, event_type)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    return True


def create_douyin_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """CREATE TABLE IF NOT EXISTS live_sessions(
          id INTEGER PRIMARY KEY, provider TEXT NOT NULL, room_id TEXT NOT NULL,
          room_title TEXT NOT NULL, room_url TEXT NOT NULL, started_at TEXT NOT NULL,
          ended_at TEXT, status TEXT NOT NULL DEFAULT 'running'
        );
        CREATE TABLE IF NOT EXISTS live_events(
          id INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, session_id INTEGER NOT NULL,
          provider TEXT NOT NULL, room_id TEXT NOT NULL, event_type TEXT NOT NULL,
          event_time TEXT NOT NULL, user_id TEXT, user_name TEXT, content TEXT,
          metadata_json TEXT, topic TEXT, intent TEXT, sentiment TEXT,
          purchase_intent TEXT, FOREIGN KEY(session_id) REFERENCES live_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS live_metric_snapshots(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
          online INTEGER DEFAULT 0, comment_rate REAL DEFAULT 0, like_rate REAL DEFAULT 0,
          gift_rate REAL DEFAULT 0, active_users INTEGER DEFAULT 0, heat_score REAL DEFAULT 0,
          purchase_ratio REAL DEFAULT 0, positive_ratio REAL DEFAULT 0,
          negative_ratio REAL DEFAULT 0, FOREIGN KEY(session_id) REFERENCES live_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS capture_gaps(
          id INTEGER PRIMARY KEY, provider TEXT NOT NULL, room_id TEXT NOT NULL,
          session_id INTEGER, run_id TEXT, gap_start TEXT NOT NULL, gap_end TEXT,
          reason TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'collector',
          status TEXT NOT NULL DEFAULT 'open', FOREIGN KEY(session_id) REFERENCES live_sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_live_events_session_time ON live_events(session_id, event_time);
        CREATE INDEX IF NOT EXISTS idx_live_events_type ON live_events(session_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_live_metrics_session_time ON live_metric_snapshots(session_id, recorded_at);
        CREATE INDEX IF NOT EXISTS idx_capture_gaps_session_time ON capture_gaps(session_id, gap_start, gap_end);"""
    )
    connection.commit()


def rebuild_douyin_events(connection: sqlite3.Connection, apply: bool) -> bool:
    if not table_exists(connection, "live_events"):
        return False
    if "received_at" not in columns(connection, "live_events"):
        return False
    if not apply:
        return True
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute(
            """CREATE TABLE live_events__governed(
              id INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, session_id INTEGER NOT NULL,
              provider TEXT NOT NULL, room_id TEXT NOT NULL, event_type TEXT NOT NULL,
              event_time TEXT NOT NULL, user_id TEXT, user_name TEXT, content TEXT,
              metadata_json TEXT, topic TEXT, intent TEXT, sentiment TEXT,
              purchase_intent TEXT, FOREIGN KEY(session_id) REFERENCES live_sessions(id)
            )"""
        )
        connection.execute(
            """INSERT INTO live_events__governed(
              id,event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,
              content,metadata_json,topic,intent,sentiment,purchase_intent)
              SELECT id,event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,
              content,metadata_json,topic,intent,sentiment,purchase_intent FROM live_events"""
        )
        connection.execute("DROP TABLE live_events")
        connection.execute("ALTER TABLE live_events__governed RENAME TO live_events")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_live_events_session_time ON live_events(session_id, event_time)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_live_events_type ON live_events(session_id, event_type)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    return True


def migrate_legacy_live(source: sqlite3.Connection, target: sqlite3.Connection, apply: bool) -> Tuple[int, int, int]:
    if not all(table_exists(source, table) for table in LEGACY_LIVE_TABLES):
        return 0, 0, 0
    if not apply:
        return (
            source.execute("SELECT count(*) FROM live_sessions").fetchone()[0],
            source.execute("SELECT count(*) FROM live_events").fetchone()[0],
            source.execute("SELECT count(*) FROM live_metric_snapshots").fetchone()[0],
        )

    session_map: Dict[int, int] = {}
    for row in source.execute("SELECT id,provider,room_id,room_title,room_url,started_at,ended_at,status FROM live_sessions ORDER BY id"):
        existing = target.execute(
            """SELECT id,provider,room_id,room_title,room_url,started_at,ended_at,status
            FROM live_sessions WHERE provider=? AND room_id=? AND started_at=?""",
            (row["provider"], row["room_id"], row["started_at"]),
        ).fetchone()
        if existing:
            expected_session = tuple(row[key] for key in ("provider", "room_id", "room_title", "room_url", "started_at", "ended_at", "status"))
            actual_session = tuple(existing[key] for key in ("provider", "room_id", "room_title", "room_url", "started_at", "ended_at", "status"))
            if actual_session != expected_session:
                raise MigrationConflict(
                    f"conflicting live_sessions identity: {row['provider']}:{row['room_id']}:{row['started_at']}"
                )
            session_map[row["id"]] = int(existing[0])
            continue
        cursor = target.execute(
            """INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at,ended_at,status)
            VALUES(?,?,?,?,?,?,?)""",
            tuple(row[key] for key in ("provider", "room_id", "room_title", "room_url", "started_at", "ended_at", "status")),
        )
        session_map[row["id"]] = int(cursor.lastrowid)

    event_count = 0
    for row in source.execute(
        """SELECT event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,content,
        metadata_json,topic,intent,sentiment,purchase_intent FROM live_events ORDER BY id"""
    ):
        existing = target.execute(
            """SELECT session_id,provider,room_id,event_type,event_time,user_id,user_name,content,
            metadata_json,topic,intent,sentiment,purchase_intent FROM live_events WHERE event_id=?""",
            (row["event_id"],),
        ).fetchone()
        expected = (
            session_map[row["session_id"]], row["provider"], row["room_id"], row["event_type"], row["event_time"],
            row["user_id"], row["user_name"], row["content"], row["metadata_json"], row["topic"], row["intent"],
            row["sentiment"], row["purchase_intent"],
        )
        if existing:
            if tuple(existing) != expected:
                raise MigrationConflict(f"conflicting live_events.event_id: {row['event_id']}")
            continue
        target.execute(
            """INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,
            content,metadata_json,topic,intent,sentiment,purchase_intent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["event_id"], session_map[row["session_id"]], row["provider"], row["room_id"], row["event_type"], row["event_time"], row["user_id"], row["user_name"], row["content"], row["metadata_json"], row["topic"], row["intent"], row["sentiment"], row["purchase_intent"]),
        )
        event_count += 1

    snapshot_count = 0
    for row in source.execute(
        """SELECT session_id,recorded_at,online,comment_rate,like_rate,gift_rate,active_users,
        heat_score,purchase_ratio,positive_ratio,negative_ratio FROM live_metric_snapshots ORDER BY id"""
    ):
        existing = target.execute(
            """SELECT online,comment_rate,like_rate,gift_rate,active_users,heat_score,
            purchase_ratio,positive_ratio,negative_ratio FROM live_metric_snapshots
            WHERE session_id=? AND recorded_at=?""",
            (session_map[row["session_id"]], row["recorded_at"]),
        ).fetchone()
        expected_snapshot = (
            row["online"], row["comment_rate"], row["like_rate"], row["gift_rate"], row["active_users"],
            row["heat_score"], row["purchase_ratio"], row["positive_ratio"], row["negative_ratio"],
        )
        if existing:
            if tuple(existing) != expected_snapshot:
                raise MigrationConflict(
                    f"conflicting live_metric_snapshots identity: {row['session_id']}:{row['recorded_at']}"
                )
            continue
        target.execute(
            """INSERT INTO live_metric_snapshots(session_id,recorded_at,online,comment_rate,like_rate,gift_rate,
            active_users,heat_score,purchase_ratio,positive_ratio,negative_ratio) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (session_map[row["session_id"]], row["recorded_at"], row["online"], row["comment_rate"], row["like_rate"], row["gift_rate"], row["active_users"], row["heat_score"], row["purchase_ratio"], row["positive_ratio"], row["negative_ratio"]),
        )
        snapshot_count += 1

    if table_exists(source, "capture_gaps") and table_exists(target, "capture_gaps"):
        for row in source.execute(
            """SELECT provider,room_id,session_id,run_id,gap_start,gap_end,reason,source,status
            FROM capture_gaps ORDER BY id"""
        ):
            mapped_session_id = session_map.get(row["session_id"]) if row["session_id"] is not None else None
            existing = target.execute(
                """SELECT 1 FROM capture_gaps
                WHERE provider=? AND room_id=? AND session_id IS ? AND run_id IS ?
                  AND gap_start=? AND gap_end IS ? AND reason=? AND source=? AND status=?""",
                (
                    row["provider"], row["room_id"], mapped_session_id, row["run_id"],
                    row["gap_start"], row["gap_end"], row["reason"], row["source"], row["status"],
                ),
            ).fetchone()
            if existing:
                continue
            target.execute(
                """INSERT INTO capture_gaps(
                  provider,room_id,session_id,run_id,gap_start,gap_end,reason,source,status
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    row["provider"], row["room_id"], mapped_session_id, row["run_id"],
                    row["gap_start"], row["gap_end"], row["reason"], row["source"], row["status"],
                ),
            )
    target.commit()
    return len(session_map), event_count, snapshot_count


def drop_legacy_tables(connection: sqlite3.Connection) -> None:
    for table in LEGACY_LIVE_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.commit()


def _copy_bundle(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_file_durable(source, destination)
    for suffix in ("-wal", "-shm"):
        source_sidecar = source.with_name(source.name + suffix)
        destination_sidecar = destination.with_name(destination.name + suffix)
        if source_sidecar.exists():
            _copy_file_durable(source_sidecar, destination_sidecar)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_file_durable(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    _fsync_file(destination)
    _fsync_directory(destination.parent)


def _remove_bundle(path: Path) -> None:
    changed = False
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
            changed = True
    if changed:
        _fsync_directory(path.parent)


def _restore_bundle(original: Path, active: Path) -> None:
    _remove_bundle(active)
    _copy_file_durable(original, active)
    for suffix in ("-wal", "-shm"):
        original_sidecar = original.with_name(original.name + suffix)
        active_sidecar = active.with_name(active.name + suffix)
        if original_sidecar.exists():
            _copy_file_durable(original_sidecar, active_sidecar)
        elif active_sidecar.exists():
            active_sidecar.unlink()
            _fsync_directory(active.parent)


def _seal_candidate(path: Path, verify: Callable[[sqlite3.Connection], None]) -> None:
    """Close/checkpoint a migrated candidate and verify its durable main file."""
    connection = connect(path)
    try:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        connection.commit()
        if mode == "wal":
            busy, log_frames, checkpointed_frames = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if busy or (log_frames >= 0 and checkpointed_frames != log_frames):
                raise RuntimeError(f"candidate WAL checkpoint incomplete: {path}")
            final_mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
            if final_mode != "delete":
                raise RuntimeError(f"candidate journal mode could not be made self-contained: {path}")
        verify(connection)
    finally:
        connection.close()

    wal_path = path.with_name(path.name + "-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise RuntimeError(f"candidate still has non-empty WAL after close: {path}")
    for sidecar in (wal_path, path.with_name(path.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    _fsync_file(path)
    _fsync_directory(path.parent)

    verification = connect(path)
    try:
        verify(verification)
    finally:
        verification.close()
    if wal_path.exists() and wal_path.stat().st_size:
        raise RuntimeError(f"candidate verification left a non-empty WAL: {path}")
    for sidecar in (wal_path, path.with_name(path.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    _fsync_file(path)
    _fsync_directory(path.parent)


def _seal_v4_candidate(path: Path, session_table: str, event_table: str) -> None:
    _seal_candidate(path, lambda connection: _verify_v4_migration_target(connection, session_table, event_table))


def _write_state(path: Path, payload: Dict[str, object]) -> None:
    _write_manifest(path, payload)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _recover_migration_locked(workspace: Path) -> bool:
    """Roll back an interrupted switch; caller must own every active DB lock."""
    state_path = Path(workspace) / "migration-state.json"
    if not state_path.exists():
        return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") not in {"switching", "rollback-needed"}:
        return False
    databases = state.get("databases", [])
    if not databases or any(not Path(item["original"]).exists() for item in databases):
        raise RuntimeError("interrupted migration has no complete original database bundle")
    for item in databases:
        active = Path(item["active"])
        original = Path(item["original"])
        _restore_bundle(original, active)
    state["status"] = "rolled-back"
    _write_state(state_path, state)
    return True


def recover_migration(workspace: Path) -> bool:
    """Recover an interrupted switch while holding its active database locks."""
    state_path = Path(workspace) / "migration-state.json"
    if not state_path.exists():
        return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") not in {"switching", "rollback-needed"}:
        return False
    databases = state.get("databases", [])
    if not databases or any("active" not in item or "original" not in item for item in databases):
        raise RuntimeError("interrupted migration journal has incomplete database identities")
    active_paths = tuple(sorted({Path(item["active"]).resolve() for item in databases}, key=str))
    handles = _acquire_database_locks(active_paths)
    try:
        current_state = json.loads(state_path.read_text(encoding="utf-8"))
        current_paths = tuple(sorted(
            {Path(item["active"]).resolve() for item in current_state.get("databases", [])},
            key=str,
        ))
        if current_state.get("status") not in {"switching", "rollback-needed"} or current_paths != active_paths:
            raise RuntimeError("migration journal changed while acquiring database locks")
        return _recover_migration_locked(workspace)
    finally:
        _release_database_locks(handles)


def apply_migration(
    bilibili_path: Path,
    douyin_path: Path,
    workspace: Path,
    fail_stage: str | None = None,
) -> Dict[str, object]:
    """Migrate isolated candidates and atomically switch them with rollback state."""

    bilibili_path = Path(bilibili_path)
    douyin_path = Path(douyin_path)
    workspace = Path(workspace)
    if not bilibili_path.exists() or not douyin_path.exists():
        missing = bilibili_path if not bilibili_path.exists() else douyin_path
        raise FileNotFoundError(f"database does not exist: {missing}")
    if workspace.exists() and any(workspace.iterdir()):
        if (workspace / "migration-state.json").exists():
            if recover_migration(workspace):
                raise RehearsalFailure("recovered-interrupted-switch")
        raise FileExistsError(f"migration workspace is not empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    source_dir = workspace / "source"
    staging_dir = workspace / "staging"
    original_dir = workspace / "original"
    source_dir.mkdir()
    staging_dir.mkdir()
    original_dir.mkdir()
    lock_handles = []
    source_connections = []
    staged_connections = []
    try:
        lock_handles.extend(_acquire_database_locks((bilibili_path, douyin_path)))
        source_paths = [source_dir / "bilibili.sqlite3", source_dir / "douyin.sqlite3"]
        for source, destination in zip((bilibili_path, douyin_path), source_paths):
            _copy_database_snapshot(source, destination)
        _fail_if_requested(fail_stage, "copy")
        for source_path in source_paths:
            connection = connect(source_path)
            source_connections.append(connection)
            verify_database(connection)
        _fail_if_requested(fail_stage, "verify-before")
        staging_paths = [staging_dir / "bilibili.sqlite3", staging_dir / "douyin.sqlite3"]
        for connection, destination in zip(source_connections, staging_paths):
            backup(connection, destination)
        for staging_path in staging_paths:
            staged_connections.append(connect(staging_path))
        staged_bilibili, staged_douyin = staged_connections
        create_douyin_schema(staged_douyin)
        create_capture_gap_schema(staged_bilibili, "sessions" if table_exists(staged_bilibili, "sessions") else "live_sessions")
        rebuild_douyin_events(staged_douyin, True)
        migrate_legacy_live(staged_bilibili, staged_douyin, True)
        rebuild_bilibili_events(staged_bilibili, True)
        staged_bilibili.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        staged_douyin.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        staged_bilibili.commit()
        staged_douyin.commit()
        _fail_if_requested(fail_stage, "migration")
        for connection in staged_connections:
            verify_database(connection)
        _fail_if_requested(fail_stage, "verify-after")
        for connection in reversed(staged_connections):
            connection.close()
        staged_connections.clear()
        for candidate in staging_paths:
            _seal_candidate(candidate, verify_database)
        original_paths = [original_dir / "bilibili.sqlite3", original_dir / "douyin.sqlite3"]
        for source, original in zip((bilibili_path, douyin_path), original_paths):
            _copy_bundle(source, original)
        state_path = workspace / "migration-state.json"
        state = {
            "status": "prepared",
            "schema_version": SCHEMA_VERSION,
            "databases": [
                {"name": "bilibili", "active": str(bilibili_path), "candidate": str(staging_paths[0]), "original": str(original_paths[0])},
                {"name": "douyin", "active": str(douyin_path), "candidate": str(staging_paths[1]), "original": str(original_paths[1])},
            ],
        }
        _write_state(state_path, state)
        _fail_if_requested(fail_stage, "switch-before")
        state["status"] = "switching"
        state["switched"] = []
        _write_state(state_path, state)
        try:
            for item in state["databases"]:
                active = Path(item["active"])
                candidate = Path(item["candidate"])
                try:
                    _remove_bundle(active)
                    os.replace(candidate, active)
                    state["switched"].append(item["name"])
                    _fsync_file(active)
                    _fsync_directory(active.parent)
                except Exception:
                    _restore_bundle(Path(item["original"]), active)
                    raise
                _write_state(state_path, state)
                if fail_stage == "switch":
                    raise RehearsalFailure("switch")
            for active in (bilibili_path, douyin_path):
                check = connect(active)
                try:
                    verify_database(check)
                finally:
                    check.close()
            state["status"] = "applied"
            _write_state(state_path, state)
        except Exception:
            for item in state["databases"]:
                if item["name"] in state["switched"]:
                    active = Path(item["active"])
                    original = Path(item["original"])
                    _restore_bundle(original, active)
            state["status"] = "rolled-back"
            _write_state(state_path, state)
            raise
        return {
            "status": "applied",
            "backup": {"bilibili": str(original_paths[0]), "douyin": str(original_paths[1])},
            "state": str(state_path),
        }
    finally:
        for connection in reversed(staged_connections):
            connection.close()
        for connection in reversed(source_connections):
            connection.close()
        _release_database_locks(lock_handles)


def _database_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _verify_v3_migration_source(connection: sqlite3.Connection) -> None:
    version = _database_version(connection)
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(f"v3 migration requires source schema {SCHEMA_VERSION}, got {version}")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables & V4_TABLES:
        raise SchemaVersionError("v3 source already contains v4 signal tables")
    verify_database(connection)


def _verify_v4_migration_target(connection: sqlite3.Connection, session_table: str, event_table: str) -> None:
    if _database_version(connection) != V4_SCHEMA_VERSION:
        raise SchemaVersionError("v4 target does not declare schema version 4")
    verify_database(connection)
    try:
        verify_signal_schema(connection, session_table, event_table)
    except Exception as error:
        raise SchemaVersionError(str(error)) from error


def migrate_v3_to_v4_connection(connection: sqlite3.Connection, session_table: str, event_table: str) -> str:
    """Upgrade one isolated v3 connection by adding only empty v4 tables."""

    connection.execute("PRAGMA foreign_keys=ON")
    version = _database_version(connection)
    if version == V4_SCHEMA_VERSION:
        _verify_v4_migration_target(connection, session_table, event_table)
        return "already-v4"
    if version > V4_SCHEMA_VERSION:
        raise SchemaVersionError(f"database schema {version} is newer than supported {V4_SCHEMA_VERSION}")
    _verify_v3_migration_source(connection)
    before = database_signature(connection)
    try:
        create_signal_schema(connection, session_table, event_table, user_version=V4_SCHEMA_VERSION)
        _verify_v4_migration_target(connection, session_table, event_table)
        after = database_signature(connection)
        if any(after.get(table) != signature for table, signature in before.items()):
            raise RuntimeError("v3 data changed during v3 to v4 migration")
    except Exception:
        connection.rollback()
        raise
    return "migrated"


def apply_v4_migration(
    bilibili_path: Path,
    douyin_path: Path,
    workspace: Path,
    fail_stage: str | None = None,
) -> Dict[str, object]:
    """Migrate v3 fixtures through isolated candidates and a recoverable switch."""

    bilibili_path = Path(bilibili_path)
    douyin_path = Path(douyin_path)
    workspace = Path(workspace)
    if not bilibili_path.exists() or not douyin_path.exists():
        missing = bilibili_path if not bilibili_path.exists() else douyin_path
        raise FileNotFoundError(f"database does not exist: {missing}")
    if workspace.exists() and any(workspace.iterdir()):
        if (workspace / "migration-state.json").exists():
            if recover_migration(workspace):
                raise RehearsalFailure("recovered-interrupted-switch")
        raise FileExistsError(f"migration workspace is not empty: {workspace}")

    lock_handles = []
    source_connections = []
    staged_connections = []
    try:
        lock_handles.extend(_acquire_database_locks((bilibili_path, douyin_path)))
        versions = []
        for path, session_table, event_table in (
            (bilibili_path, "sessions", "events"),
            (douyin_path, "live_sessions", "live_events"),
        ):
            connection = connect(path)
            try:
                version = _database_version(connection)
                versions.append(version)
                if version == V4_SCHEMA_VERSION:
                    _verify_v4_migration_target(connection, session_table, event_table)
            finally:
                connection.close()
        if versions == [V4_SCHEMA_VERSION, V4_SCHEMA_VERSION]:
            return {"status": "already-v4"}
        if any(version == V4_SCHEMA_VERSION for version in versions):
            raise SchemaVersionError("cannot migrate a mixed v3/v4 database pair")

        workspace.mkdir(parents=True, exist_ok=True)
        source_dir = workspace / "source"
        staging_dir = workspace / "staging"
        original_dir = workspace / "original"
        source_dir.mkdir()
        staging_dir.mkdir()
        original_dir.mkdir()
        source_paths = [source_dir / "bilibili.sqlite3", source_dir / "douyin.sqlite3"]
        for source, destination in zip((bilibili_path, douyin_path), source_paths):
            _copy_bundle(source, destination)
        _fail_if_requested(fail_stage, "copy")
        for source_path in source_paths:
            connection = connect(source_path)
            source_connections.append(connection)
            _verify_v3_migration_source(connection)
        _fail_if_requested(fail_stage, "verify-before")

        staging_paths = [staging_dir / "bilibili.sqlite3", staging_dir / "douyin.sqlite3"]
        for connection, destination in zip(source_connections, staging_paths):
            backup(connection, destination)
        for connection in reversed(source_connections):
            connection.close()
        source_connections.clear()

        for staging_path in staging_paths:
            staged_connections.append(connect(staging_path))
        migrate_v3_to_v4_connection(staged_connections[0], "sessions", "events")
        migrate_v3_to_v4_connection(staged_connections[1], "live_sessions", "live_events")
        _fail_if_requested(fail_stage, "migration")
        for connection, session_table, event_table in zip(
            staged_connections,
            ("sessions", "live_sessions"),
            ("events", "live_events"),
        ):
            _verify_v4_migration_target(connection, session_table, event_table)
        _fail_if_requested(fail_stage, "verify-after")

        for connection in reversed(staged_connections):
            connection.commit()
            connection.close()
        staged_connections.clear()
        for candidate, session_table, event_table in zip(
            staging_paths,
            ("sessions", "live_sessions"),
            ("events", "live_events"),
        ):
            _seal_v4_candidate(candidate, session_table, event_table)

        original_paths = [original_dir / "bilibili.sqlite3", original_dir / "douyin.sqlite3"]
        for source, original in zip((bilibili_path, douyin_path), original_paths):
            _copy_bundle(source, original)
        state_path = workspace / "migration-state.json"
        state = {
            "status": "prepared",
            "schema_version": V4_SCHEMA_VERSION,
            "databases": [
                {"name": "bilibili", "active": str(bilibili_path), "candidate": str(staging_paths[0]), "original": str(original_paths[0])},
                {"name": "douyin", "active": str(douyin_path), "candidate": str(staging_paths[1]), "original": str(original_paths[1])},
            ],
        }
        _write_state(state_path, state)
        _fail_if_requested(fail_stage, "switch-before")
        state["status"] = "switching"
        state["switched"] = []
        _write_state(state_path, state)
        try:
            for item in state["databases"]:
                active = Path(item["active"])
                candidate = Path(item["candidate"])
                try:
                    _remove_bundle(active)
                    os.replace(candidate, active)
                    state["switched"].append(item["name"])
                    _fsync_file(active)
                    _fsync_directory(active.parent)
                except Exception:
                    _restore_bundle(Path(item["original"]), active)
                    raise
                _write_state(state_path, state)
                if fail_stage == "switch":
                    raise RehearsalFailure("switch")
            for active in (bilibili_path, douyin_path):
                connection = connect(active)
                try:
                    session_table, event_table = (
                        ("sessions", "events") if active == bilibili_path
                        else ("live_sessions", "live_events")
                    )
                    _verify_v4_migration_target(connection, session_table, event_table)
                finally:
                    connection.close()
            state["status"] = "applied"
            _write_state(state_path, state)
        except Exception:
            for item in state["databases"]:
                if item["name"] in state["switched"]:
                    _restore_bundle(Path(item["original"]), Path(item["active"]))
            state["status"] = "rolled-back"
            _write_state(state_path, state)
            raise
        return {"status": "applied", "backup": {"bilibili": str(original_paths[0]), "douyin": str(original_paths[1])}, "state": str(state_path)}
    finally:
        for connection in reversed(staged_connections):
            connection.close()
        for connection in reversed(source_connections):
            connection.close()
        _release_database_locks(lock_handles)


def main() -> None:
    parser = argparse.ArgumentParser(description="Govern Bullet-Screen SQLite databases")
    parser.add_argument("--bilibili", type=Path, default=DEFAULT_BILIBILI_DB)
    parser.add_argument("--douyin", type=Path, default=DEFAULT_DOUYIN_DB)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="apply the migration; without it only report changes")
    parser.add_argument("--target-version", type=int, choices=(3, 4), help="explicit migration target; required with --apply")
    args = parser.parse_args()

    if args.apply:
        if args.target_version is None:
            parser.error("--apply requires --target-version 3 or 4")
        workspace = args.backup_dir or args.bilibili.parent / ".bullet-screen-db-migration"
        if args.target_version == V4_SCHEMA_VERSION:
            result = apply_v4_migration(args.bilibili, args.douyin, workspace)
        else:
            result = apply_migration(args.bilibili, args.douyin, workspace)
        if result["status"] == "already-v4":
            print("database migration: ALREADY V4 (no changes)")
            return
        print(f"migration state: {result['state']}")
        print(f"backup: {result['backup']}")
        print("database migration: APPLIED")
        return

    bilibili = connect(args.bilibili)
    douyin = connect(args.douyin)
    try:
        backup_dir = None
        douyin_events = rebuild_douyin_events(douyin, False)
        legacy_counts = migrate_legacy_live(bilibili, douyin, False)
        bilibili_events = rebuild_bilibili_events(bilibili, False)
        print(f"legacy live rows: sessions={legacy_counts[0]} events={legacy_counts[1]} snapshots={legacy_counts[2]}")
        print(f"bilibili events rebuild: {'yes' if bilibili_events else 'no'}")
        print(f"douyin events rebuild: {'yes' if douyin_events else 'no'}")
        print("database migration: DRY RUN (use --apply)")
    finally:
        bilibili.close()
        douyin.close()


if __name__ == "__main__":
    main()
