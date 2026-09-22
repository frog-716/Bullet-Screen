import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOUYIN_ROOT = REPO_ROOT / "douyin"
sys.path.insert(0, str(DOUYIN_ROOT))
sys.path.insert(0, str(REPO_ROOT))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BILIBILI = load_module("bilibili_server_v4_design", REPO_ROOT / "bilibili" / "server.py")
LIVE = load_module("live_intelligence_v4_design", DOUYIN_ROOT / "live_intelligence.py")
DOUYIN_SERVER = load_module("douyin_server_v4_design", DOUYIN_ROOT / "server.py")
GOVERN = load_module("govern_databases_v4_design", REPO_ROOT / "scripts" / "govern_databases.py")
SCHEMA = load_module("schema_v4_design", REPO_ROOT / "schema_v4.py")


def make_bilibili_v3(path, room="bili-room"):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions(
          id INTEGER PRIMARY KEY, room_id TEXT NOT NULL, room_title TEXT NOT NULL,
          started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL DEFAULT 'running'
        );
        CREATE TABLE events(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          event_time TEXT NOT NULL, uid INTEGER, uname TEXT, text TEXT, gift_name TEXT,
          gift_num INTEGER, amount INTEGER, popularity INTEGER,
          FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE TABLE metric_snapshots(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
          online INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, danmaku_rate REAL DEFAULT 0,
          total_danmaku INTEGER DEFAULT 0, FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE TABLE capture_gaps(
          id INTEGER PRIMARY KEY, provider TEXT NOT NULL, room_id TEXT NOT NULL,
          session_id INTEGER, run_id TEXT, gap_start TEXT NOT NULL, gap_end TEXT,
          reason TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'collector',
          status TEXT NOT NULL DEFAULT 'open', FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE INDEX idx_events_session_time ON events(session_id, event_time);
        CREATE INDEX idx_events_type ON events(session_id, event_type);
        CREATE INDEX idx_metrics_session_time ON metric_snapshots(session_id, recorded_at);
        CREATE INDEX idx_capture_gaps_session_time ON capture_gaps(session_id, gap_start, gap_end);
        PRAGMA user_version=3;
        """
    )
    connection.execute(
        "INSERT INTO sessions(room_id,room_title,started_at,status) VALUES(?,?,?,?)",
        (room, "fixture", "2026-01-01T00:00:00+00:00", "stopped"),
    )
    connection.execute(
        "INSERT INTO events(session_id,event_type,event_time,uid,uname,text,gift_num,amount,popularity) VALUES(?,?,?,?,?,?,?,?,?)",
        (1, "danmaku", "2026-01-01T00:00:01+00:00", 7, "user", "hello", 0, 0, None),
    )
    connection.execute(
        "INSERT INTO metric_snapshots(session_id,recorded_at,online,likes,danmaku_rate,total_danmaku) VALUES(?,?,?,?,?,?)",
        (1, "2026-01-01T00:00:02+00:00", None, 0, 0.0, 0),
    )
    connection.execute(
        "INSERT INTO capture_gaps(provider,room_id,session_id,run_id,gap_start,gap_end,reason,status) VALUES(?,?,?,?,?,?,?,?)",
        ("bilibili", room, 1, "run-fixture", "2026-01-01T00:00:03+00:00", None, "stale", "open"),
    )
    connection.commit()
    connection.close()


def make_douyin_v3(path, room="douyin-room"):
    connection = sqlite3.connect(path)
    GOVERN.create_douyin_schema(connection)
    connection.execute("PRAGMA user_version=3")
    connection.execute(
        "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at,status) VALUES(?,?,?,?,?,?)",
        ("douyin", room, "fixture", "https://example.test/room", "2026-01-01T00:00:00+00:00", "stopped"),
    )
    connection.execute(
        "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at,status) VALUES(?,?,?,?,?,?)",
        ("douyin", room + "-quiet", "fixture quiet", "https://example.test/quiet", "2026-01-01T08:00:00+08:00", "stopped"),
    )
    connection.execute(
        "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,content,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("event-fixture", 1, "douyin", room, "comment", "2026-01-01T00:00:01+00:00", "7", "user", "hello", "{}"),
    )
    connection.execute(
        "INSERT INTO live_metric_snapshots(session_id,recorded_at,online,comment_rate,active_users) VALUES(?,?,?,?,?)",
        (1, "2026-01-01T00:00:02+00:00", None, 0.0, 0),
    )
    connection.execute(
        "INSERT INTO live_metric_snapshots(session_id,recorded_at,online,comment_rate,active_users) VALUES(?,?,?,?,?)",
        (2, "2026-01-01T08:00:02+08:00", 0, 0.0, 0),
    )
    connection.execute(
        "INSERT INTO capture_gaps(provider,room_id,session_id,run_id,gap_start,gap_end,reason,status) VALUES(?,?,?,?,?,?,?,?)",
        ("douyin", room, 1, "run-fixture", "2026-01-01T00:00:03+00:00", None, "stale", "open"),
    )
    connection.commit()
    connection.close()


class V4SchemaContractTests(unittest.TestCase):
    def test_all_three_store_entries_create_the_same_v4_domain_tables(self):
        for store_class in (BILIBILI.EventStore, LIVE.LiveEventStore, DOUYIN_SERVER.EventStore):
            with self.subTest(store=store_class.__module__ + "." + store_class.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    store = store_class(Path(directory) / "store.sqlite3")
                    self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 4)
                    SCHEMA.verify_signal_schema(
                        store.connection,
                        "sessions" if store_class is not LIVE.LiveEventStore else "live_sessions",
                        "events" if store_class is not LIVE.LiveEventStore else "live_events",
                    )
                    store.close()

    def test_signal_id_is_deterministic_for_the_same_rule_window(self):
        first = SCHEMA.signal_id_for("douyin", "room", 3, "run-1", "purchase", "rules-v2", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00")
        second = SCHEMA.signal_id_for("douyin", "room", 3, "run-1", "purchase", "rules-v2", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00")
        different_window = SCHEMA.signal_id_for("douyin", "room", 3, "run-1", "purchase", "rules-v2", "2026-01-01T00:00:01+00:00", "2026-01-01T00:01:00+00:00")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_window)
        self.assertTrue(first.startswith("sig_"))

    def test_v4_constraints_cover_identity_feedback_coverage_and_foreign_keys(self):
        store = LIVE.LiveEventStore(":memory:")
        session_id = store.start_session("douyin", "room", "fixture", "https://example.test/room")
        event = LIVE.normalize_event("room", "comment", "u1", "user", "hello", timestamp="2026-01-01T00:00:01+00:00")
        self.assertTrue(store.insert_event(session_id, event))
        event_row_id = store.connection.execute("SELECT id FROM live_events").fetchone()[0]
        values = ("sig-fixture", "douyin", "room", session_id, "run-1", "purchase", "rules-v2", "2026-01-01T00:01:00+00:00", "2026-01-01T00:01:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", "unknown", 1, 1, "active", "weak", "fixture reason")
        store.connection.execute(
            "INSERT INTO signals(signal_id,provider,room_id,session_id,run_id,signal_type,rule_version,created_at,as_of,window_start,window_end,coverage,event_count,unique_user_count,status,strength,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        store.connection.execute("INSERT INTO signal_evidence(signal_id,event_id) VALUES(?,?)", ("sig-fixture", event_row_id))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("INSERT INTO signal_evidence(signal_id,event_id) VALUES(?,?)", ("sig-fixture", event_row_id))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("INSERT INTO signal_evidence(signal_id,event_id) VALUES(?,?)", ("sig-fixture", 999999))
        duplicate_identity = list(values)
        duplicate_identity[0] = "sig-duplicate"
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO signals(signal_id,provider,room_id,session_id,run_id,signal_type,rule_version,created_at,as_of,window_start,window_end,coverage,event_count,unique_user_count,status,strength,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                duplicate_identity,
            )
        orphan_signal = list(values)
        orphan_signal[0] = "sig-orphan"
        orphan_signal[3] = 999999
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO signals(signal_id,provider,room_id,session_id,run_id,signal_type,rule_version,created_at,as_of,window_start,window_end,coverage,event_count,unique_user_count,status,strength,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                orphan_signal,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("INSERT INTO signal_feedback(feedback_id,signal_id,feedback_type,created_at) VALUES(?,?,?,?)", ("fb-invalid", "sig-fixture", "unknown", "2026-01-01T00:02:00+00:00"))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("INSERT INTO signal_feedback(feedback_id,signal_id,feedback_type,note,created_at) VALUES(?,?,?,?,?)", ("fb-empty-note", "sig-fixture", "note", "", "2026-01-01T00:02:00+00:00"))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("INSERT INTO signals(signal_id,provider,room_id,session_id,run_id,signal_type,rule_version,created_at,as_of,window_start,window_end,coverage,event_count,unique_user_count,status,strength,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(values[:11]) + ("invalid",) + tuple(values[12:]))
        store.connection.execute("INSERT INTO signal_feedback(feedback_id,signal_id,feedback_type,note,created_at) VALUES(?,?,?,?,?)", ("fb-1", "sig-fixture", "useful", None, "2026-01-01T00:02:00+00:00"))
        store.connection.execute("INSERT INTO signal_feedback(feedback_id,signal_id,feedback_type,note,created_at) VALUES(?,?,?,?,?)", ("fb-2", "sig-fixture", "note", "keep", "2026-01-01T00:03:00+00:00"))
        second_session = store.start_session("douyin", "quiet", "quiet", "https://example.test/quiet")
        cascade_signal = list(values)
        cascade_signal[0] = "sig-session-cascade"
        cascade_signal[3] = second_session
        store.connection.execute(
            "INSERT INTO signals(signal_id,provider,room_id,session_id,run_id,signal_type,rule_version,created_at,as_of,window_start,window_end,coverage,event_count,unique_user_count,status,strength,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            cascade_signal,
        )
        store.connection.execute("DELETE FROM live_sessions WHERE id=?", (second_session,))
        self.assertEqual(store.connection.execute("SELECT count(*) FROM signals WHERE signal_id='sig-session-cascade'").fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("DELETE FROM live_events WHERE id=?", (event_row_id,))
        store.connection.execute("DELETE FROM signals WHERE signal_id=?", ("sig-fixture",))
        self.assertEqual(store.connection.execute("SELECT count(*) FROM signal_evidence").fetchone()[0], 0)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM signal_feedback").fetchone()[0], 0)
        store.connection.commit()
        store.close()

    def test_malformed_v4_fails_closed_even_when_user_version_is_four(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            store = LIVE.LiveEventStore(path)
            store.close()
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE signal_feedback RENAME TO signal_feedback_old")
            connection.execute("CREATE TABLE signal_feedback(feedback_id TEXT NOT NULL PRIMARY KEY, signal_id TEXT NOT NULL, feedback_type TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL)")
            connection.execute("INSERT INTO signal_feedback SELECT * FROM signal_feedback_old")
            connection.execute("DROP TABLE signal_feedback_old")
            connection.commit()
            connection.close()
            with self.assertRaises(LIVE.SchemaVersionError):
                LIVE.LiveEventStore(path)


class V3ToV4MigrationTests(unittest.TestCase):
    def test_migration_preserves_v3_rows_and_creates_empty_v4_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "douyin.sqlite3"
            make_douyin_v3(path)
            before = sqlite3.connect(path)
            before.row_factory = sqlite3.Row
            before_rows = {
                "sessions": [tuple(row) for row in before.execute("SELECT * FROM live_sessions")],
                "events": [tuple(row) for row in before.execute("SELECT * FROM live_events")],
                "metrics": [tuple(row) for row in before.execute("SELECT * FROM live_metric_snapshots")],
                "gaps": [tuple(row) for row in before.execute("SELECT * FROM capture_gaps")],
            }
            GOVERN.migrate_v3_to_v4_connection(before, "live_sessions", "live_events")
            self.assertEqual(before.execute("PRAGMA user_version").fetchone()[0], 4)
            for table in ("signals", "signal_evidence", "signal_feedback"):
                self.assertEqual(before.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(before_rows["sessions"], [tuple(row) for row in before.execute("SELECT * FROM live_sessions")])
            self.assertEqual(before_rows["events"], [tuple(row) for row in before.execute("SELECT * FROM live_events")])
            self.assertEqual(before_rows["metrics"], [tuple(row) for row in before.execute("SELECT * FROM live_metric_snapshots")])
            self.assertEqual(before_rows["gaps"], [tuple(row) for row in before.execute("SELECT * FROM capture_gaps")])
            before.close()

    def test_dual_database_migration_switches_only_temp_active_files_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili, douyin = root / "bilibili.sqlite3", root / "douyin.sqlite3"
            make_bilibili_v3(bilibili)
            make_douyin_v3(douyin)
            result = GOVERN.apply_v4_migration(bilibili, douyin, root / "migration-1")
            self.assertEqual(result["status"], "applied")
            for backup_path in (Path(result["backup"]["bilibili"]), Path(result["backup"]["douyin"])):
                self.assertTrue(backup_path.is_file())
                backup_connection = sqlite3.connect(backup_path)
                self.assertEqual(backup_connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(backup_connection.execute("SELECT count(*) FROM sqlite_master WHERE name='signals'").fetchone()[0], 0)
                backup_connection.close()
            second = GOVERN.apply_v4_migration(bilibili, douyin, root / "migration-2")
            self.assertEqual(second["status"], "already-v4")
            for path, event_table, expected_event in (
                (bilibili, "events", "hello"),
                (douyin, "live_events", "hello"),
            ):
                connection = sqlite3.connect(path)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                for table in ("signals", "signal_evidence", "signal_feedback"):
                    self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
                event_column = "text" if event_table == "events" else "content"
                self.assertEqual(connection.execute(f"SELECT {event_column} FROM {event_table} WHERE id=1").fetchone()[0], expected_event)
                connection.close()

    def test_corrupted_candidate_fails_target_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            make_douyin_v3(path)
            connection = sqlite3.connect(path)
            GOVERN.migrate_v3_to_v4_connection(connection, "live_sessions", "live_events")
            connection.execute("DROP INDEX idx_signal_evidence_event")
            connection.commit()
            with self.assertRaises(GOVERN.SchemaVersionError):
                GOVERN._verify_v4_migration_target(connection, "live_sessions", "live_events")
            connection.close()

    def test_failure_injection_keeps_both_active_files_unchanged(self):
        stages = ("copy", "verify-before", "migration", "verify-after", "switch-before", "switch")
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bilibili, douyin = root / "bilibili.sqlite3", root / "douyin.sqlite3"
                make_bilibili_v3(bilibili)
                make_douyin_v3(douyin)
                before = {path: path.read_bytes() for path in (bilibili, douyin)}
                with self.assertRaises(GOVERN.RehearsalFailure):
                    GOVERN.apply_v4_migration(bilibili, douyin, root / "migration", fail_stage=stage)
                self.assertEqual(before, {path: path.read_bytes() for path in (bilibili, douyin)})

    def test_interrupted_switch_recovery_restores_original_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili, douyin = root / "bilibili.sqlite3", root / "douyin.sqlite3"
            original_bilibili, original_douyin = root / "original-bili.sqlite3", root / "original-douyin.sqlite3"
            make_bilibili_v3(bilibili)
            make_douyin_v3(douyin)
            original_bilibili.write_bytes(bilibili.read_bytes())
            original_douyin.write_bytes(douyin.read_bytes())
            workspace = root / "migration"
            workspace.mkdir()
            (workspace / "migration-state.json").write_text(json.dumps({
                "status": "switching",
                "databases": [
                    {"name": "bilibili", "active": str(bilibili), "original": str(original_bilibili)},
                    {"name": "douyin", "active": str(douyin), "original": str(original_douyin)},
                ],
            }))
            bilibili.write_bytes(b"partial candidate")
            douyin.write_bytes(b"partial candidate")
            self.assertTrue(GOVERN.recover_migration(workspace))
            self.assertEqual(bilibili.read_bytes(), original_bilibili.read_bytes())
            self.assertEqual(douyin.read_bytes(), original_douyin.read_bytes())

    def test_v3_source_errors_and_future_version_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            malformed = root / "malformed.sqlite3"
            make_douyin_v3(malformed)
            connection = sqlite3.connect(malformed)
            connection.execute("DROP INDEX idx_live_events_type")
            connection.commit()
            connection.close()
            with self.assertRaises(Exception):
                connection = sqlite3.connect(malformed)
                GOVERN.migrate_v3_to_v4_connection(connection, "live_sessions", "live_events")

            future = root / "future.sqlite3"
            make_douyin_v3(future)
            connection = sqlite3.connect(future)
            connection.execute("PRAGMA user_version=5")
            connection.commit()
            with self.assertRaises(GOVERN.SchemaVersionError):
                GOVERN.migrate_v3_to_v4_connection(connection, "live_sessions", "live_events")
            connection.close()


if __name__ == "__main__":
    unittest.main()
