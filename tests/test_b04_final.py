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


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIVE = load_module("live_intelligence_b04_final", DOUYIN_ROOT / "live_intelligence.py")
BILIBILI = load_module("bilibili_server_b04_final", REPO_ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_b04_final", DOUYIN_ROOT / "server.py")
GOVERN = load_module("govern_databases_b04_final", REPO_ROOT / "scripts" / "govern_databases.py")


class SchemaVersionGateTests(unittest.TestCase):
    def _database_with_version(self, path, version):
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
        connection.close()

    def test_fresh_and_memory_databases_initialize_at_supported_version(self):
        with tempfile.TemporaryDirectory() as directory:
            for store_class, path in (
                (BILIBILI.EventStore, Path(directory) / "bilibili.sqlite3"),
                (LIVE.LiveEventStore, Path(directory) / "douyin.sqlite3"),
            ):
                store = store_class(path)
                self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], store_class.SCHEMA_VERSION)
                store.close()

        for store_class in (BILIBILI.EventStore, LIVE.LiveEventStore):
            memory_store = store_class(":memory:")
            self.assertEqual(memory_store.connection.execute("PRAGMA user_version").fetchone()[0], store_class.SCHEMA_VERSION)
            memory_store.close()

    def test_old_unknown_and_future_versions_fail_closed_for_both_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = (
                (BILIBILI.EventStore, BILIBILI.SCHEMA_VERSION - 1, Path(directory) / "bili-old.sqlite3"),
                (BILIBILI.EventStore, 97, Path(directory) / "bili-unknown.sqlite3"),
                (BILIBILI.EventStore, BILIBILI.SCHEMA_VERSION + 1, Path(directory) / "bili-future.sqlite3"),
                (LIVE.LiveEventStore, LIVE.LiveEventStore.SCHEMA_VERSION - 1, Path(directory) / "douyin-old.sqlite3"),
                (LIVE.LiveEventStore, 97, Path(directory) / "douyin-unknown.sqlite3"),
                (LIVE.LiveEventStore, LIVE.LiveEventStore.SCHEMA_VERSION + 1, Path(directory) / "douyin-future.sqlite3"),
            )
            for store_class, version, path in cases:
                with self.subTest(store=store_class.__name__, version=version):
                    self._database_with_version(path, version)
                    with self.assertRaises(store_class.SchemaVersionError):
                        store_class(path)


class GapLedgerTests(unittest.TestCase):
    def _assert_gap_contract(self, store, session_id, provider, room_id):
        start = "2026-09-21T10:00:00+00:00"
        end = "2026-09-21T10:05:00+00:00"
        gap_id = store.open_gap(provider, room_id, session_id, "run-1", "stale", started_at=start)
        self.assertEqual(store.open_gap(provider, room_id, session_id, "run-1", "stale", started_at="2026-09-21T10:01:00+00:00"), gap_id)
        coverage = store.coverage(session_id, start, end)
        self.assertFalse(coverage["complete"])
        self.assertTrue(coverage["has_open_gap"])
        self.assertEqual(len(coverage["gaps"]), 1)
        self.assertEqual(coverage["gaps"][0]["reason"], "stale")
        store.close_gap(gap_id, ended_at="2026-09-21T10:03:00+00:00")
        coverage_after_close = store.coverage(session_id, start, end)
        self.assertFalse(coverage_after_close["complete"])
        self.assertFalse(coverage_after_close["has_open_gap"])
        self.assertEqual(len(store.gaps(session_id)), 1)

    def test_douyin_gap_is_deduplicated_closed_and_does_not_change_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LIVE.LiveEventStore(Path(directory) / "douyin.sqlite3")
            session = store.start_session("douyin", "room", "title", "url")
            self._assert_gap_contract(store, session, "douyin", "room")
            self.assertEqual(store.recent_events(session, limit=None), [])
            store.close()

    def test_bilibili_gap_has_provider_room_and_run_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BILIBILI.EventStore(Path(directory) / "bilibili.sqlite3")
            session = store.start_session("room", "title")
            self._assert_gap_contract(store, session, "bilibili", "room")
            gap = store.gaps(session)[0]
            self.assertEqual((gap["provider"], gap["room_id"], gap["run_id"]), ("bilibili", "room", "run-1"))
            store.close()


class MigrationRehearsalTests(unittest.TestCase):
    def _make_live_db(self, path, content="source"):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        GOVERN.create_douyin_schema(connection)
        connection.execute(
            "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at) VALUES(?,?,?,?,?)",
            ("douyin", "room", "title", "url", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content) VALUES(?,?,?,?,?,?,?)",
            ("event-1", 1, "douyin", "room", "comment", "2026-01-01T00:00:01+00:00", content),
        )
        connection.commit()
        connection.close()

    def _make_empty_live_db(self, path):
        connection = sqlite3.connect(path)
        GOVERN.create_douyin_schema(connection)
        connection.close()

    def test_normal_rehearsal_switches_only_a_manifest_and_keeps_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            workspace = root / "rehearsal"
            self._make_live_db(bilibili)
            self._make_live_db(douyin, content="source")
            before = (GOVERN.database_signature(GOVERN.connect(bilibili)), GOVERN.database_signature(GOVERN.connect(douyin)))
            result = GOVERN.rehearse_migration(bilibili, douyin, workspace)
            after = (GOVERN.database_signature(GOVERN.connect(bilibili)), GOVERN.database_signature(GOVERN.connect(douyin)))
            self.assertEqual(before, after)
            self.assertEqual(result["status"], "switched-simulation")
            self.assertTrue(Path(result["switch_manifest"]).is_file())
            payload = json.loads(Path(result["switch_manifest"]).read_text())
            self.assertEqual(payload["status"], "ready")

    def test_missing_input_and_all_failure_stages_leave_sources_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            self._make_live_db(bilibili)
            self._make_live_db(douyin)
            with self.assertRaises(FileNotFoundError):
                GOVERN.rehearse_migration(root / "missing.sqlite3", douyin, root / "missing-run")
            original = (GOVERN.database_signature(GOVERN.connect(bilibili)), GOVERN.database_signature(GOVERN.connect(douyin)))
            for stage in ("copy", "verify-before", "migration", "verify-after", "switch-before", "switch"):
                with self.subTest(stage=stage):
                    workspace = root / f"failure-{stage}"
                    with self.assertRaises(GOVERN.RehearsalFailure):
                        GOVERN.rehearse_migration(bilibili, douyin, workspace, fail_stage=stage)
                    current = (GOVERN.database_signature(GOVERN.connect(bilibili)), GOVERN.database_signature(GOVERN.connect(douyin)))
                    self.assertEqual(current, original)
                    self.assertFalse((workspace / "switch" / "active.json").exists())

    def test_conflict_and_repeated_run_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            self._make_live_db(bilibili, content="source")
            self._make_live_db(douyin, content="different")
            original = (GOVERN.database_signature(GOVERN.connect(bilibili)), GOVERN.database_signature(GOVERN.connect(douyin)))
            with self.assertRaises(GOVERN.MigrationConflict):
                GOVERN.rehearse_migration(bilibili, douyin, root / "conflict")
            self.assertEqual(original, (GOVERN.database_signature(GOVERN.connect(bilibili)), GOVERN.database_signature(GOVERN.connect(douyin))))

            clean_douyin = root / "clean-douyin.sqlite3"
            self._make_live_db(clean_douyin, content="source")
            first = GOVERN.rehearse_migration(bilibili, clean_douyin, root / "run-1")
            second = GOVERN.rehearse_migration(bilibili, clean_douyin, root / "run-2")
            self.assertEqual(first["status"], second["status"])

    def test_rehearsal_preserves_gap_ledger_when_sessions_are_remapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            self._make_live_db(bilibili)
            self._make_empty_live_db(douyin)
            source = sqlite3.connect(bilibili)
            source.execute(
                """INSERT INTO capture_gaps(
                provider,room_id,session_id,run_id,gap_start,reason,status
                ) VALUES(?,?,?,?,?,?,?)""",
                ("douyin", "room", 1, "run-1", "2026-01-01T00:00:02+00:00", "stale", "open"),
            )
            source.commit()
            source.close()

            result = GOVERN.rehearse_migration(bilibili, douyin, root / "rehearsal")
            payload = json.loads(Path(result["switch_manifest"]).read_text())
            candidate = sqlite3.connect(payload["candidate"]["douyin"])
            try:
                gap = candidate.execute(
                    "SELECT provider,room_id,session_id,run_id,reason,status FROM capture_gaps"
                ).fetchone()
            finally:
                candidate.close()
            self.assertEqual(gap, ("douyin", "room", 1, "run-1", "stale", "open"))

    def test_rehearsal_does_not_touch_source_wal_or_database_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            self._make_live_db(bilibili)
            self._make_empty_live_db(douyin)
            source_connection = sqlite3.connect(bilibili)
            source_connection.execute("PRAGMA journal_mode=WAL")
            source_connection.execute("PRAGMA wal_autocheckpoint=1000000")
            source_connection.execute(
                "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content) VALUES(?,?,?,?,?,?,?)",
                ("event-2", 1, "douyin", "room", "comment", "2026-01-01T00:00:02+00:00", "wal"),
            )
            source_connection.commit()
            source_files = [
                bilibili,
                bilibili.with_name(bilibili.name + "-wal"),
                bilibili.with_name(bilibili.name + "-shm"),
                douyin,
                douyin.with_name(douyin.name + "-wal"),
                douyin.with_name(douyin.name + "-shm"),
            ]
            before = {
                str(path): (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
                for path in source_files if path.exists()
            }
            GOVERN.rehearse_migration(bilibili, douyin, root / "rehearsal")
            after = {
                str(path): (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
                for path in source_files if path.exists()
            }
            source_connection.close()
            self.assertEqual(before, after)


class CrossProcessDatabaseLockTests(unittest.TestCase):
    def test_both_service_entries_have_non_overlapping_database_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            bilibili_lock = BILIBILI.acquire_database_lock(path)
            try:
                with self.assertRaises(BILIBILI.ProtocolError):
                    BILIBILI.acquire_database_lock(path)
            finally:
                bilibili_lock.close()

            douyin_lock = DOUYIN_SERVER.acquire_database_lock(path)
            try:
                with self.assertRaises(DOUYIN_SERVER.ProtocolError):
                    DOUYIN_SERVER.acquire_database_lock(path)
            finally:
                douyin_lock.close()


if __name__ == "__main__":
    unittest.main()
