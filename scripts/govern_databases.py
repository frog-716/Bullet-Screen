#!/usr/bin/env python3
"""Govern the two local SQLite stores without losing historical live data.

The migration removes fields that no current reader uses and moves the
legacy normalized Douyin tables out of the Bilibili database. It is explicit:
without --apply it only reports what would change.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BILIBILI_DB = ROOT / "bilibili" / "data" / "danmaku.sqlite3"
DEFAULT_DOUYIN_DB = ROOT / "douyin" / "data" / "danmaku.sqlite3"
LEGACY_LIVE_TABLES = ("live_metric_snapshots", "live_events", "live_sessions")


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def backup(connection: sqlite3.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        connection.backup(target)
    finally:
        target.close()


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
        CREATE INDEX IF NOT EXISTS idx_live_events_session_time ON live_events(session_id, event_time);
        CREATE INDEX IF NOT EXISTS idx_live_events_type ON live_events(session_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_live_metrics_session_time ON live_metric_snapshots(session_id, recorded_at);"""
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
            "SELECT id FROM live_sessions WHERE provider=? AND room_id=? AND started_at=?",
            (row["provider"], row["room_id"], row["started_at"]),
        ).fetchone()
        if existing:
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
        if target.execute("SELECT 1 FROM live_events WHERE event_id=?", (row["event_id"],)).fetchone():
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
        target.execute(
            """INSERT INTO live_metric_snapshots(session_id,recorded_at,online,comment_rate,like_rate,gift_rate,
            active_users,heat_score,purchase_ratio,positive_ratio,negative_ratio) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (session_map[row["session_id"]], row["recorded_at"], row["online"], row["comment_rate"], row["like_rate"], row["gift_rate"], row["active_users"], row["heat_score"], row["purchase_ratio"], row["positive_ratio"], row["negative_ratio"]),
        )
        snapshot_count += 1
    target.commit()
    return len(session_map), event_count, snapshot_count


def drop_legacy_tables(connection: sqlite3.Connection) -> None:
    for table in LEGACY_LIVE_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Govern Bullet-Screen SQLite databases")
    parser.add_argument("--bilibili", type=Path, default=DEFAULT_BILIBILI_DB)
    parser.add_argument("--douyin", type=Path, default=DEFAULT_DOUYIN_DB)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="apply the migration; without it only report changes")
    args = parser.parse_args()

    bilibili = connect(args.bilibili)
    douyin = connect(args.douyin)
    try:
        backup_dir = None
        if args.apply:
            backup_dir = args.backup_dir or Path(tempfile.mkdtemp(prefix="Bullet-Screen-db-backup-"))
            backup(bilibili, backup_dir / "bilibili.sqlite3")
            backup(douyin, backup_dir / "douyin.sqlite3")
            print(f"backup: {backup_dir}")
            create_douyin_schema(douyin)
            douyin_events = rebuild_douyin_events(douyin, True)
        else:
            douyin_events = rebuild_douyin_events(douyin, False)
        legacy_counts = migrate_legacy_live(bilibili, douyin, args.apply)
        bilibili_events = rebuild_bilibili_events(bilibili, args.apply)
        if args.apply:
            if all(table_exists(bilibili, table) for table in LEGACY_LIVE_TABLES):
                drop_legacy_tables(bilibili)
            bilibili.execute("PRAGMA user_version=2")
            douyin.execute("PRAGMA user_version=2")
            bilibili.commit()
            douyin.commit()
        print(f"legacy live rows: sessions={legacy_counts[0]} events={legacy_counts[1]} snapshots={legacy_counts[2]}")
        print(f"bilibili events rebuild: {'yes' if bilibili_events else 'no'}")
        print(f"douyin events rebuild: {'yes' if douyin_events else 'no'}")
        print("database migration: APPLIED" if args.apply else "database migration: DRY RUN (use --apply)")
    finally:
        bilibili.close()
        douyin.close()


if __name__ == "__main__":
    main()
