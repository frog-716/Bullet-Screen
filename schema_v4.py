"""Shared v4 signal schema and identity primitives.

This module owns only the storage contract for signals.  Rule evaluation,
feedback handling, and replay are deliberately outside this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Dict, Iterable, Optional, Tuple


SCHEMA_VERSION = 4
V4_TABLES = {"signals", "signal_evidence", "signal_feedback"}
COVERAGE_VALUES = ("reliable_with_data", "reliable_no_events", "gap", "unknown")
SIGNAL_STATUS_VALUES = ("active", "cleared", "expired")
SIGNAL_STRENGTH_VALUES = ("weak", "moderate", "strong")
FEEDBACK_TYPE_VALUES = ("useful", "false_positive", "note")
SIGNAL_IDENTITY_COLUMNS = (
    "provider",
    "room_id",
    "session_id",
    "run_id",
    "signal_type",
    "rule_version",
    "window_start",
    "window_end",
)


class SchemaSignatureError(RuntimeError):
    """A v4 signal schema is missing or does not match its contract."""


def _identifier(value: str, allowed: Iterable[str]) -> str:
    if value not in set(allowed):
        raise ValueError(f"unsupported SQLite identifier: {value}")
    return value


def signal_id_for(
    provider: str,
    room_id: str,
    session_id: int,
    run_id: str,
    signal_type: str,
    rule_version: str,
    window_start: str,
    window_end: str,
) -> str:
    """Return a stable identity for one rule/window evaluation."""

    identity = [
        str(provider), str(room_id), int(session_id), str(run_id),
        str(signal_type), str(rule_version), str(window_start), str(window_end),
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return "sig_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _time_check(column: str) -> str:
    return (
        f"CHECK(length({column}) >= 20 AND substr({column}, 11, 1) = 'T' "
        f"AND substr({column}, -6) = '+00:00')"
    )


def create_signal_schema(
    connection: sqlite3.Connection,
    session_table: str,
    event_table: str,
    user_version: Optional[int] = None,
) -> None:
    """Create the three provider-neutral v4 entities transactionally."""

    session_table = _identifier(session_table, ("sessions", "live_sessions"))
    event_table = _identifier(event_table, ("events", "live_events"))
    version_sql = "" if user_version is None else f"PRAGMA user_version={int(user_version)};"
    connection.executescript(
        f"""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS signals(
          signal_id TEXT NOT NULL PRIMARY KEY,
          provider TEXT NOT NULL,
          room_id TEXT NOT NULL,
          session_id INTEGER NOT NULL,
          run_id TEXT NOT NULL,
          signal_type TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          created_at TEXT NOT NULL {_time_check('created_at')},
          as_of TEXT NOT NULL {_time_check('as_of')},
          window_start TEXT NOT NULL {_time_check('window_start')},
          window_end TEXT NOT NULL {_time_check('window_end')},
          coverage TEXT NOT NULL CHECK(coverage IN ('reliable_with_data','reliable_no_events','gap','unknown')),
          event_count INTEGER NOT NULL CHECK(event_count >= 0),
          unique_user_count INTEGER NOT NULL CHECK(unique_user_count >= 0),
          status TEXT NOT NULL CHECK(status IN ('active','cleared','expired')),
          strength TEXT NOT NULL CHECK(strength IN ('weak','moderate','strong')),
          reason TEXT NOT NULL,
          CHECK(window_start <= window_end),
          FOREIGN KEY(session_id) REFERENCES {session_table}(id) ON DELETE CASCADE,
          UNIQUE(provider,room_id,session_id,run_id,signal_type,rule_version,window_start,window_end)
        );
        CREATE TABLE IF NOT EXISTS signal_evidence(
          signal_id TEXT NOT NULL,
          event_id INTEGER NOT NULL,
          PRIMARY KEY(signal_id,event_id),
          FOREIGN KEY(signal_id) REFERENCES signals(signal_id) ON DELETE CASCADE,
          FOREIGN KEY(event_id) REFERENCES {event_table}(id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS signal_feedback(
          feedback_id TEXT NOT NULL PRIMARY KEY,
          signal_id TEXT NOT NULL,
          feedback_type TEXT NOT NULL CHECK(feedback_type IN ('useful','false_positive','note')),
          note TEXT,
          created_at TEXT NOT NULL {_time_check('created_at')},
          CHECK(feedback_type != 'note' OR (note IS NOT NULL AND length(trim(note)) > 0)),
          FOREIGN KEY(signal_id) REFERENCES signals(signal_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_signals_session_window
          ON signals(session_id,window_start,window_end);
        CREATE INDEX IF NOT EXISTS idx_signal_evidence_event
          ON signal_evidence(event_id);
        CREATE INDEX IF NOT EXISTS idx_signal_feedback_signal_created
          ON signal_feedback(signal_id,created_at);
        {version_sql}
        COMMIT;
        """
    )


def _actual_columns(connection: sqlite3.Connection, table: str) -> Dict[str, Tuple[str, int, int, Optional[str]]]:
    return {
        row[1]: (str(row[2]).upper(), int(row[3]), int(row[5]), None if row[4] is None else str(row[4]).replace(" ", "").lower())
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _require_columns(connection: sqlite3.Connection, table: str, expected: Dict[str, Tuple[str, int, int, Optional[str]]]) -> None:
    actual = _actual_columns(connection, table)
    if set(actual) != set(expected):
        raise SchemaSignatureError(
            f"v4 schema columns mismatch in {table}: expected={sorted(expected)}, actual={sorted(actual)}"
        )
    for name, definition in expected.items():
        if actual.get(name) != definition:
            raise SchemaSignatureError(f"v4 schema mismatch in {table}.{name}: {actual.get(name)}")


def _require_index(connection: sqlite3.Connection, table: str, name: str, columns: Tuple[str, ...], unique: bool = False) -> None:
    row = next((item for item in connection.execute(f"PRAGMA index_list({table})") if item[1] == name), None)
    if row is None or (unique and int(row[2]) != 1):
        raise SchemaSignatureError(f"v4 schema missing index {name}")
    actual = tuple(item[2] for item in connection.execute(f"PRAGMA index_info({name})"))
    if actual != columns:
        raise SchemaSignatureError(f"v4 schema mismatch in index {name}: {actual}")


def _require_unique_identity(connection: sqlite3.Connection) -> None:
    for row in connection.execute("PRAGMA index_list(signals)"):
        if int(row[2]) != 1:
            continue
        actual = tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]})"))
        if actual == SIGNAL_IDENTITY_COLUMNS:
            return
    raise SchemaSignatureError("v4 schema missing signal identity unique constraint")


def _require_foreign_keys(connection: sqlite3.Connection, session_table: str, event_table: str) -> None:
    expected = {
        "signals": {(session_table, "session_id", "id", "CASCADE")},
        "signal_evidence": {
            ("signals", "signal_id", "signal_id", "CASCADE"),
            (event_table, "event_id", "id", "RESTRICT"),
        },
        "signal_feedback": {("signals", "signal_id", "signal_id", "CASCADE")},
    }
    for table, wanted in expected.items():
        actual = {(row[2], row[3], row[4], row[6].upper()) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
        if actual != wanted:
            raise SchemaSignatureError(f"v4 schema mismatch in foreign keys for {table}: {actual}")


def _require_checks(connection: sqlite3.Connection) -> None:
    sql_by_table = {
        row[0]: " ".join((row[1] or "").lower().split())
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('signals','signal_evidence','signal_feedback')"
        )
    }
    fragments = {
        "signals": (
            "check(coverage in ('reliable_with_data','reliable_no_events','gap','unknown'))",
            "check(event_count >= 0)",
            "check(unique_user_count >= 0)",
            "check(status in ('active','cleared','expired'))",
            "check(strength in ('weak','moderate','strong'))",
            "check(window_start <= window_end)",
            "check(length(created_at) >= 20",
            "check(length(as_of) >= 20",
            "check(length(window_start) >= 20",
            "check(length(window_end) >= 20",
        ),
        "signal_feedback": (
            "check(feedback_type in ('useful','false_positive','note'))",
            "check(feedback_type != 'note' or (note is not null",
            "check(length(created_at) >= 20",
        ),
    }
    for table, required in fragments.items():
        sql = sql_by_table.get(table, "")
        if any(fragment not in sql for fragment in required):
            raise SchemaSignatureError(f"v4 schema missing check constraint in {table}")


def verify_signal_schema(connection: sqlite3.Connection, session_table: str, event_table: str) -> None:
    """Fail closed unless all v4 tables and constraints match exactly."""

    session_table = _identifier(session_table, ("sessions", "live_sessions"))
    event_table = _identifier(event_table, ("events", "live_events"))
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = V4_TABLES - tables
    if missing:
        raise SchemaSignatureError(f"v4 schema missing tables: {sorted(missing)}")
    _require_columns(connection, "signals", {
        "signal_id": ("TEXT", 1, 1, None), "provider": ("TEXT", 1, 0, None),
        "room_id": ("TEXT", 1, 0, None), "session_id": ("INTEGER", 1, 0, None),
        "run_id": ("TEXT", 1, 0, None), "signal_type": ("TEXT", 1, 0, None),
        "rule_version": ("TEXT", 1, 0, None), "created_at": ("TEXT", 1, 0, None),
        "as_of": ("TEXT", 1, 0, None), "window_start": ("TEXT", 1, 0, None),
        "window_end": ("TEXT", 1, 0, None), "coverage": ("TEXT", 1, 0, None),
        "event_count": ("INTEGER", 1, 0, None), "unique_user_count": ("INTEGER", 1, 0, None),
        "status": ("TEXT", 1, 0, None), "strength": ("TEXT", 1, 0, None),
        "reason": ("TEXT", 1, 0, None),
    })
    _require_columns(connection, "signal_evidence", {
        "signal_id": ("TEXT", 1, 1, None), "event_id": ("INTEGER", 1, 2, None),
    })
    _require_columns(connection, "signal_feedback", {
        "feedback_id": ("TEXT", 1, 1, None), "signal_id": ("TEXT", 1, 0, None),
        "feedback_type": ("TEXT", 1, 0, None), "note": ("TEXT", 0, 0, None),
        "created_at": ("TEXT", 1, 0, None),
    })
    _require_index(connection, "signals", "idx_signals_session_window", ("session_id", "window_start", "window_end"))
    _require_index(connection, "signal_evidence", "idx_signal_evidence_event", ("event_id",))
    _require_index(connection, "signal_feedback", "idx_signal_feedback_signal_created", ("signal_id", "created_at"))
    _require_unique_identity(connection)
    _require_foreign_keys(connection, session_table, event_table)
    _require_checks(connection)
