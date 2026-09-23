import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "douyin"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOVERN = load_module("govern_final_gate_repair", ROOT / "scripts" / "govern_databases.py")
LIFECYCLE = load_module("data_lifecycle_final_gate_repair", ROOT / "scripts" / "data_lifecycle.py")
BILIBILI = load_module("bilibili_final_gate_repair", ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_final_gate_repair", ROOT / "douyin" / "server.py")
LIVE = load_module("live_final_gate_repair", ROOT / "douyin" / "live_intelligence.py")
ADAPTER = load_module("adapter_final_gate_repair", ROOT / "douyin" / "douyin_adapter.py")
V4_FIXTURES = load_module("v4_fixtures_final_gate_repair", ROOT / "tests" / "test_v4_design.py")
B04_FIXTURES = load_module("b04_fixtures_final_gate_repair", ROOT / "tests" / "test_b04_repair.py")


class MigrationRecoveryLockTests(unittest.TestCase):
    def test_v4_recovery_does_not_touch_active_or_journal_while_other_process_holds_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            workspace = root / "migration"
            workspace.mkdir()
            original = workspace / "original.sqlite3"
            candidate = workspace / "candidate.sqlite3"
            active_before = {bili: b"active-bili", douyin: b"active-douyin"}
            for path, data in active_before.items():
                path.write_bytes(data)
            original.write_bytes(b"old-original")
            candidate.write_bytes(b"candidate")
            journal = workspace / "migration-state.json"
            journal.write_text(json.dumps({
                "status": "switching",
                "databases": [{
                    "name": "bilibili", "active": str(bili),
                    "candidate": str(candidate), "original": str(original),
                }, {
                    "name": "douyin", "active": str(douyin),
                    "candidate": str(candidate), "original": str(original),
                }],
            }), encoding="utf-8")
            journal_before = journal.read_bytes()
            lock = GOVERN._acquire_database_lock(douyin)
            try:
                script = (
                    "import sys; from pathlib import Path; "
                    "from scripts.govern_databases import apply_v4_migration; "
                    "apply_v4_migration(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script, str(bili), str(douyin), str(workspace)],
                    cwd=ROOT, text=True, capture_output=True, timeout=10,
                )
            finally:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual({path: path.read_bytes() for path in active_before}, active_before)
            self.assertEqual(journal.read_bytes(), journal_before)


class DurableMigrationCandidateTests(unittest.TestCase):
    def _wal_v3_pair(self, root):
        bili = root / "bilibili.sqlite3"
        douyin = root / "douyin.sqlite3"
        V4_FIXTURES.make_bilibili_v3(bili)
        V4_FIXTURES.make_douyin_v3(douyin)
        open_connections = []
        for path, table, values in (
            (bili, "events", (1, "danmaku", "2026-01-01T00:00:03+00:00", 17, "wal-user", "wal-visible")),
            (douyin, "live_events", ("wal-event", 1, "douyin", "douyin-room", "comment", "2026-01-01T00:00:03+00:00", "17", "wal-user", "wal-visible", "{}")),
        ):
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA journal_mode=WAL")
            if table == "events":
                connection.execute(
                    "INSERT INTO events(session_id,event_type,event_time,uid,uname,text) VALUES(?,?,?,?,?,?)", values
                )
            else:
                connection.execute(
                    "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,content,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            connection.commit()
            wal_path = path.with_name(path.name + "-wal")
            self.assertGreater(wal_path.stat().st_size, 0)
            open_connections.append(connection)
        return bili, douyin, open_connections

    def test_candidate_connections_close_and_active_verification_precedes_applied_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bili, douyin, open_sources = self._wal_v3_pair(root)
            workspace = root / "migration"
            raw_connect = GOVERN.connect
            raw_replace = GOVERN.os.replace
            raw_verify = GOVERN._verify_v4_migration_target
            tracked = {}
            observations = []

            class TrackedConnection:
                def __init__(self, connection, path):
                    self.connection = connection
                    self.path = Path(path)
                    self.closed = False

                def close(self):
                    self.closed = True
                    return self.connection.close()

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            def tracking_connect(path):
                connection = raw_connect(path)
                wrapped = TrackedConnection(connection, path)
                tracked[str(Path(path))] = wrapped
                return wrapped

            def checking_replace(source, destination):
                source_path = Path(source)
                if source_path.parent.name == "staging":
                    observations.append(("switch", tracked[str(source_path)].closed))
                return raw_replace(source, destination)

            def checking_verify(connection, session_table, event_table):
                path = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
                if path in {bili.resolve(), douyin.resolve()}:
                    journal = json.loads((workspace / "migration-state.json").read_text(encoding="utf-8"))
                    observations.append(("active-verify", journal["status"]))
                return raw_verify(connection, session_table, event_table)

            try:
                with mock.patch.object(GOVERN, "connect", side_effect=tracking_connect), \
                     mock.patch.object(GOVERN.os, "replace", side_effect=checking_replace), \
                     mock.patch.object(GOVERN, "_verify_v4_migration_target", side_effect=checking_verify):
                    result = GOVERN.apply_v4_migration(bili, douyin, workspace)
            finally:
                for connection in open_sources:
                    connection.close()

            self.assertEqual(result["status"], "applied")
            self.assertEqual([value for kind, value in observations if kind == "switch"], [True, True])
            active_verifications = [value for kind, value in observations if kind == "active-verify"]
            self.assertEqual(active_verifications, ["switching", "switching"])
            final_state = json.loads(Path(result["state"]).read_text(encoding="utf-8"))
            self.assertEqual(final_state["status"], "applied")

    def test_wal_committed_rows_survive_candidate_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bili, douyin, open_sources = self._wal_v3_pair(root)
            try:
                result = GOVERN.apply_v4_migration(bili, douyin, root / "migration")
            finally:
                for connection in open_sources:
                    connection.close()
            self.assertEqual(result["status"], "applied")
            for path, table, column, expected in (
                (bili, "events", "text", "wal-visible"),
                (douyin, "live_events", "content", "wal-visible"),
            ):
                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(connection.execute(f"SELECT {column} FROM {table} WHERE {column}=?", (expected,)).fetchone()[0], expected)
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                finally:
                    connection.close()


class BilibiliUnverifiedCaptureCoverageTests(unittest.TestCase):
    def test_failed_auth_does_not_turn_connecting_window_into_reliable_capture(self):
        for label, module in (("official", BILIBILI), ("compatibility", DOUYIN_SERVER)):
            with self.subTest(implementation=label):
                store = module.EventStore(":memory:")
                collector = module.Collector(store)
                now = datetime.now(timezone.utc)
                session_start = (now - timedelta(minutes=10)).isoformat()
                query_start = (now + timedelta(seconds=2)).isoformat()
                query_end = (now + timedelta(seconds=30)).isoformat()
                context = module.RunContext(1, "unverified-run", "bilibili", "room", "")
                session_id = store.start_session("room", "title")
                store.connection.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?", (session_start, session_id)
                )
                store.connection.commit()
                store.open_gap("bilibili", "room", session_id, context.run_id, "connecting", session_start)
                collector._context = context
                collector.session_id = session_id
                collector.status_name = "authenticating"

                try:
                    packet = module.PacketCodec.pack(module.json_bytes({"code": -1}), 8, 1)
                    with self.assertRaises(module.ProtocolError):
                        collector._handle_packet(context, packet)
                    collector._finish_run(context, session_id, "error")
                    store.connection.execute(
                        "UPDATE sessions SET ended_at=? WHERE id=?",
                        ((now + timedelta(minutes=1)).isoformat(), session_id),
                    )
                    store.connection.execute(
                        "INSERT INTO metric_snapshots(session_id,recorded_at) VALUES(?,?)",
                        (session_id, (now + timedelta(seconds=10)).isoformat()),
                    )
                    store.connection.commit()

                    coverage = store.coverage(session_id, query_start, query_end)
                    gaps = store.gaps(session_id)
                    self.assertEqual(len(gaps), 1)
                    self.assertEqual(gaps[0]["reason"], "protocol_unavailable")
                    self.assertEqual(coverage["coverage_state"], "unknown")
                    self.assertFalse(coverage["complete"])
                finally:
                    store.close()

    def test_legacy_v3_wal_candidates_are_closed_and_verified_before_applied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bili, douyin = root / "bilibili.sqlite3", root / "douyin.sqlite3"
            B04_FIXTURES.make_bilibili_v2(bili)
            B04_FIXTURES.make_legacy_live(douyin)
            source_connections = []
            for path, insert in (
                (bili, "INSERT INTO events(session_id,event_type,event_time,text) VALUES(1,'danmaku','2026-01-01T00:00:03Z','wal-visible')"),
                (douyin, "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content) VALUES('wal-event',1,'douyin','room','comment','2026-01-01T00:00:03Z','wal-visible')"),
            ):
                connection = sqlite3.connect(path)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(insert)
                connection.commit()
                self.assertGreater(path.with_name(path.name + "-wal").stat().st_size, 0)
                source_connections.append(connection)

            workspace = root / "migration"
            raw_connect = GOVERN.connect
            raw_replace = GOVERN.os.replace
            raw_verify = GOVERN.verify_database
            tracked = {}
            observations = []
            wal_enabled = set()

            class TrackedConnection:
                def __init__(self, connection, path):
                    self.connection = connection
                    self.path = Path(path)
                    self.closed = False

                def close(self):
                    self.closed = True
                    return self.connection.close()

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            def tracking_connect(path):
                connection = raw_connect(path)
                wrapped = TrackedConnection(connection, path)
                tracked.setdefault(str(Path(path).resolve()), []).append(wrapped)
                resolved = str(Path(path).resolve())
                if Path(path).parent.name == "staging" and resolved not in wal_enabled:
                    connection.execute("PRAGMA journal_mode=WAL")
                    wal_enabled.add(resolved)
                return wrapped

            def checking_replace(source, destination):
                source_path = Path(source).resolve()
                if source_path.parent.name == "staging":
                    observations.append(("switch", all(item.closed for item in tracked[str(source_path)])))
                return raw_replace(source, destination)

            def checking_verify(connection):
                path = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
                if path in {bili.resolve(), douyin.resolve()}:
                    state = json.loads((workspace / "migration-state.json").read_text(encoding="utf-8"))
                    observations.append(("active-verify", state["status"]))
                elif path.parent.name == "staging":
                    wal_path = path.with_name(path.name + "-wal")
                    observations.append(("candidate-wal", wal_path.exists() and wal_path.stat().st_size > 0))
                return raw_verify(connection)

            try:
                with mock.patch.object(GOVERN, "connect", side_effect=tracking_connect), \
                     mock.patch.object(GOVERN.os, "replace", side_effect=checking_replace), \
                     mock.patch.object(GOVERN, "verify_database", side_effect=checking_verify):
                    result = GOVERN.apply_migration(bili, douyin, workspace)
            finally:
                for connection in source_connections:
                    connection.close()

            self.assertEqual(result["status"], "applied")
            self.assertEqual([value for kind, value in observations if kind == "switch"], [True, True])
            self.assertEqual([value for kind, value in observations if kind == "active-verify"], ["switching", "switching"])
            self.assertTrue(any(value for kind, value in observations if kind == "candidate-wal"))
            for path, table, column in ((bili, "events", "text"), (douyin, "live_events", "content")):
                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(connection.execute(f"SELECT {column} FROM {table} WHERE {column}='wal-visible'").fetchone()[0], "wal-visible")
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                    self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
                finally:
                    connection.close()


class DurableLifecycleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.profile = self.root / "douyin" / "data" / "browser-profile"
        self.profile.mkdir(parents=True)
        (self.profile / "state.fixture").write_bytes(b"profile-before")
        self.database = self.root / "bilibili" / "data" / "danmaku.sqlite3"
        self.database.parent.mkdir(parents=True)
        self.database.write_bytes(b"db-before")
        self.wal = self.database.with_name(self.database.name + "-wal")
        self.wal.write_bytes(b"wal-before")
        self.shm = self.database.with_name(self.database.name + "-shm")
        self.shm.write_bytes(b"shm-before")
        self.backup = Path(self.temporary.name) / "backup"
        LIFECYCLE.create_backup(self.root, self.backup, self.profile)

    def _restore(self, **kwargs):
        return LIFECYCLE.restore_backup(
            self.root, self.backup, self.profile,
            safety_output=Path(self.temporary.name) / ("safety-" + str(len(list(Path(self.temporary.name).glob("safety-*"))))),
            **kwargs,
        )

    def _journal(self):
        marker = json.loads((self.root / LIFECYCLE.LIFECYCLE_MARKER).read_text(encoding="utf-8"))
        return self.root / LIFECYCLE.LIFECYCLE_DIRNAME / marker["transaction_id"] / "journal.json"

    def test_wal_replace_interruption_rolls_back_every_target(self):
        before = {path: path.read_bytes() for path in (self.database, self.wal, self.shm)}
        with self.assertRaises(LIFECYCLE.SimulatedInterruption):
            self._restore(fail_stage="after-wal-switch")
        self.assertTrue(LIFECYCLE.recover_lifecycle(self.root, self.profile))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_interruption_after_switching_journal_leaves_targets_untouched(self):
        before = {path: path.read_bytes() for path in (self.database, self.wal, self.shm)}
        with self.assertRaises(LIFECYCLE.SimulatedInterruption):
            self._restore(fail_stage="after-switching-journal")
        self.assertTrue(LIFECYCLE.recover_lifecycle(self.root, self.profile))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_profile_interruption_between_remove_and_replace_restores_old_profile(self):
        backup = Path(self.temporary.name) / "backup-profile"
        LIFECYCLE.create_backup(self.root, backup, self.profile, include_profile=True)
        (self.profile / "state.fixture").write_bytes(b"profile-new")
        with self.assertRaises(LIFECYCLE.SimulatedInterruption):
            LIFECYCLE.restore_backup(
                self.root, backup, self.profile, include_profile=True,
                safety_output=Path(self.temporary.name) / "profile-safety",
                fail_stage="profile-after-remove",
            )
        self.assertTrue(LIFECYCLE.recover_lifecycle(self.root, self.profile))
        self.assertEqual((self.profile / "state.fixture").read_bytes(), b"profile-new")

    def test_applied_journal_is_reverified_and_bad_target_rolls_back(self):
        before = self.database.read_bytes()
        with self.assertRaises(LIFECYCLE.SimulatedInterruption):
            self._restore(fail_stage="after-applied")
        journal_path = self._journal()
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["status"], "applied")
        self.database.write_bytes(b"damaged-after-applied-marker")
        self.assertTrue(LIFECYCLE.recover_lifecycle(self.root, self.profile))
        self.assertEqual(self.database.read_bytes(), before)

    def test_applied_journal_with_all_desired_targets_finishes_recovery(self):
        self.database.write_bytes(b"changed-after-backup")
        with self.assertRaises(LIFECYCLE.SimulatedInterruption):
            self._restore(fail_stage="after-applied")
        self.assertTrue(LIFECYCLE.recover_lifecycle(self.root, self.profile))
        self.assertEqual(self.database.read_bytes(), b"db-before")
        self.assertFalse(LIFECYCLE.has_pending_lifecycle(self.root))

    def test_final_verify_failure_rolls_back_to_pre_restore_targets(self):
        self.database.write_bytes(b"changed-before-restore")
        before = {path: path.read_bytes() for path in (self.database, self.wal, self.shm)}
        with mock.patch.object(LIFECYCLE, "_all_operations_match", side_effect=[False, True]):
            with self.assertRaisesRegex(LIFECYCLE.LifecycleError, "最终目标校验失败"):
                self._restore()
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(LIFECYCLE.has_pending_lifecycle(self.root))

    def test_restore_fsyncs_replaced_file_and_parent_before_progress_journal(self):
        events = []
        raw_replace = LIFECYCLE.os.replace
        raw_sync_file = LIFECYCLE._fsync_file
        raw_sync_directory = LIFECYCLE._fsync_directory
        targets = {path.resolve() for path in (self.database, self.wal, self.shm)}

        def record_replace(source, destination):
            destination = Path(destination).resolve()
            if destination in targets:
                events.append(("replace", destination))
            elif destination.name == "journal.json":
                events.append(("journal", destination))
            return raw_replace(source, destination)

        def record_file_sync(path):
            path = Path(path).resolve()
            if path in targets:
                events.append(("file-sync", path))
            return raw_sync_file(path)

        def record_directory_sync(path):
            path = Path(path).resolve()
            if path == self.database.parent.resolve():
                events.append(("directory-sync", path))
            return raw_sync_directory(path)

        with mock.patch.object(LIFECYCLE.os, "replace", side_effect=record_replace), \
             mock.patch.object(LIFECYCLE, "_fsync_file", side_effect=record_file_sync), \
             mock.patch.object(LIFECYCLE, "_fsync_directory", side_effect=record_directory_sync):
            self._restore()

        self.assertIn(("replace", self.database.resolve()), events, repr(events))
        for target in targets:
            replace_index = next(i for i, item in enumerate(events) if item == ("replace", target))
            file_index = next(i for i, item in enumerate(events) if i > replace_index and item == ("file-sync", target))
            directory_index = next(i for i, item in enumerate(events) if i > file_index and item == ("directory-sync", self.database.parent.resolve()))
            journal_index = next(i for i, item in enumerate(events) if i > directory_index and item[0] == "journal")
            self.assertLess(replace_index, file_index)
            self.assertLess(file_index, directory_index)
            self.assertLess(directory_index, journal_index)


class CompleteWindowCoverageTests(unittest.TestCase):
    stores = (
        ("bilibili", BILIBILI.EventStore, "sessions", "events", "metric_snapshots"),
        ("compat-bilibili", DOUYIN_SERVER.EventStore, "sessions", "events", "metric_snapshots"),
        ("douyin", LIVE.LiveEventStore, "live_sessions", "live_events", "live_metric_snapshots"),
    )
    session_start = "2026-09-23T10:00:00Z"
    capture_start = "2026-09-23T10:00:05Z"
    window_end = "2026-09-23T10:05:00Z"
    session_end = "2026-09-23T10:10:00Z"

    def _store(self, store_class, session_table, event_table, snapshot_table):
        store = store_class(":memory:")
        if session_table == "live_sessions":
            session_id = store.start_session("douyin", "room", "fixture", "https://example.test/live")
        else:
            session_id = store.start_session("room", "fixture")
        store.connection.execute(
            f"UPDATE {session_table} SET started_at=?,ended_at=?,status='stopped' WHERE id=?",
            (self.session_start, self.session_end, session_id),
        )
        store.connection.commit()
        gap_id = store.open_gap("douyin" if session_table == "live_sessions" else "bilibili", "room", session_id, "run-1", "connecting", self.session_start)
        store.close_gap(gap_id, self.capture_start)
        return store, session_id

    def _add_event(self, store, session_id, session_table, event_table):
        if event_table == "events":
            store.connection.execute(
                "INSERT INTO events(session_id,event_type,event_time,uid,uname,text) VALUES(?,?,?,?,?,?)",
                (session_id, "danmaku", "2026-09-23T10:02:00Z", 7, "fixture", "hello"),
            )
        else:
            store.connection.execute(
                "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                ("fixture-event", session_id, "douyin", "room", "comment", "2026-09-23T10:02:00Z", "hello", json.dumps({"source": "fetch_protobuf"})),
            )
        store.connection.commit()

    def _add_snapshot(self, store, session_id, snapshot_table):
        if snapshot_table == "metric_snapshots":
            store.connection.execute(
                "INSERT INTO metric_snapshots(session_id,recorded_at) VALUES(?,?)",
                (session_id, "2026-09-23T10:02:00Z"),
            )
        else:
            store.connection.execute(
                "INSERT INTO live_metric_snapshots(session_id,recorded_at) VALUES(?,?)",
                (session_id, "2026-09-23T10:02:00Z"),
            )
        store.connection.commit()

    def test_all_store_entries_require_complete_session_and_trusted_window(self):
        for label, store_class, session_table, event_table, snapshot_table in self.stores:
            with self.subTest(store=label):
                store, session_id = self._store(store_class, session_table, event_table, snapshot_table)
                try:
                    self._add_event(store, session_id, session_table, event_table)
                    self._add_snapshot(store, session_id, snapshot_table)
                    partial = store.coverage(session_id, "2026-09-23T09:59:00Z", "2026-09-23T10:00:00Z")
                    self.assertEqual(partial["coverage_state"], "unknown")

                    complete = store.coverage(session_id, self.capture_start, self.window_end)
                    self.assertEqual(complete["coverage_state"], "reliable_with_data")

                    store.connection.execute(f"DELETE FROM {event_table} WHERE session_id=?", (session_id,))
                    store.connection.commit()
                    no_events = store.coverage(session_id, self.capture_start, self.window_end)
                    self.assertEqual(no_events["coverage_state"], "reliable_no_events")

                    gap_id = store.open_gap("douyin" if session_table == "live_sessions" else "bilibili", "room", session_id, "run-1", "stale", "2026-09-23T10:02:00Z")
                    store.close_gap(gap_id, "2026-09-23T10:02:30Z")
                    gap_window = store.coverage(session_id, self.capture_start, self.window_end)
                    self.assertEqual(gap_window["coverage_state"], "gap")
                finally:
                    store.close()

    def test_douyin_dom_only_events_and_synthetic_snapshots_cannot_prove_reliability(self):
        store = LIVE.LiveEventStore(":memory:")
        session_id = store.start_session("douyin", "room", "fixture", "https://example.test/live")
        store.connection.execute("UPDATE live_sessions SET started_at=? WHERE id=?", (self.session_start, session_id))
        store.connection.commit()
        store.open_gap("douyin", "room", session_id, "run-dom", "connecting", self.session_start)
        event1 = LIVE.normalize_event("room", "comment", "u1", "u1", "想买", metadata={"source": "dom"}, timestamp="2026-09-23T10:02:00Z", provider="douyin")
        event2 = LIVE.normalize_event("room", "comment", "u2", "u2", "想买", metadata={"source": "dom"}, timestamp="2026-09-23T10:02:01Z", provider="douyin")
        store.insert_event(session_id, event1)
        store.insert_event(session_id, event2)
        self._add_snapshot(store, session_id, "live_metric_snapshots")
        try:
            coverage = store.coverage(session_id, self.capture_start, self.window_end)
            self.assertNotIn(coverage["coverage_state"], {"reliable_with_data", "reliable_no_events"})
            signals = LIVE.replay_signals(
                store.recent_events(session_id, None), provider="douyin", room_id="room",
                session_id=session_id, run_id="run-dom", as_of="2026-09-23T10:03:00Z",
                coverage="reliable_with_data",
            )
            self.assertTrue(signals)
            self.assertEqual(signals[0]["coverage"], "unknown")
            self.assertNotEqual(signals[0]["strength"], "strong")
        finally:
            store.close()

    def test_synthetic_snapshot_without_protocol_evidence_is_unknown(self):
        store = LIVE.LiveEventStore(":memory:")
        session_id = store.start_session("douyin", "room", "fixture", "https://example.test/live")
        store.connection.execute(
            "UPDATE live_sessions SET started_at=?,ended_at=?,status='stopped' WHERE id=?",
            (self.session_start, self.session_end, session_id),
        )
        store.connection.commit()
        self._add_snapshot(store, session_id, "live_metric_snapshots")
        try:
            coverage = store.coverage(session_id, self.capture_start, self.window_end)
            self.assertEqual(coverage["coverage_state"], "unknown")
            self.assertFalse(coverage["complete"])
        finally:
            store.close()

    def test_run_ending_before_protocol_records_unavailable_interval(self):
        collector = ADAPTER.DouyinCollector(":memory:")
        session_id = collector.store.start_session("douyin", "room", "fixture", "https://example.test/live")
        collector.store.connection.execute(
            "UPDATE live_sessions SET started_at=?,ended_at=? WHERE id=?",
            (self.session_start, self.session_end, session_id),
        )
        collector.store.connection.commit()
        context = ADAPTER.RunContext(1, "run-unknown", "douyin", "room", "https://example.test/live", "", "live")
        collector._context = context
        collector.session_id = session_id
        collector.status_name = "connecting"
        collector.store.open_gap("douyin", "room", session_id, context.run_id, "connecting", self.session_start)
        self._add_snapshot(collector.store, session_id, "live_metric_snapshots")
        try:
            collector._finish_run(context, "stopped")
            collector.store.connection.execute(
                "UPDATE live_sessions SET ended_at=? WHERE id=?", (self.session_end, session_id)
            )
            collector.store.connection.execute(
                "UPDATE capture_gaps SET gap_start=?,gap_end=? WHERE session_id=?",
                (self.session_start, self.session_end, session_id),
            )
            collector.store.connection.commit()
            gaps = collector.store.gaps(session_id)
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["reason"], "protocol_unavailable")
            self.assertIsNotNone(gaps[0]["gap_end"])
            coverage = collector.store.coverage(session_id, self.session_start, self.window_end)
            self.assertEqual(coverage["coverage_state"], "gap")
        finally:
            collector.store.close()

    def test_protocol_verified_douyin_events_can_support_reliable_coverage_and_signal(self):
        store = LIVE.LiveEventStore(":memory:")
        session_id = store.start_session("douyin", "room", "fixture", "https://example.test/live")
        store.connection.execute("UPDATE live_sessions SET started_at=?,ended_at=?,status='stopped' WHERE id=?", (self.session_start, self.session_end, session_id))
        store.connection.commit()
        gap_id = store.open_gap("douyin", "room", session_id, "run-protocol", "connecting", self.session_start)
        store.close_gap(gap_id, self.capture_start)
        for uid in ("u1", "u2"):
            event = LIVE.normalize_event("room", "comment", uid, uid, "想买", metadata={"source": "fetch_protobuf", "event_id": uid}, timestamp="2026-09-23T10:02:00Z", provider="douyin")
            store.insert_event(session_id, event)
        self._add_snapshot(store, session_id, "live_metric_snapshots")
        try:
            coverage = store.coverage(session_id, self.capture_start, self.window_end)
            self.assertEqual(coverage["coverage_state"], "reliable_with_data")
            signal = LIVE.replay_signals(
                store.recent_events(session_id, None), provider="douyin", room_id="room",
                session_id=session_id, run_id="run-protocol", as_of="2026-09-23T10:03:00Z",
                coverage=coverage["coverage_state"],
            )[0]
            self.assertEqual(signal["strength"], "strong")
        finally:
            store.close()


class ReplayVersionBoundaryTests(unittest.TestCase):
    def test_only_implemented_rules_v2_can_replay(self):
        events = [
            LIVE.normalize_event("room", "comment", uid, uid, "想买", metadata={"event_id": uid}, timestamp="2026-09-23T10:04:30Z", provider="douyin")
            for uid in ("u1", "u2")
        ]
        signals = LIVE.replay_signals(events, rule_version="rules-v2", as_of="2026-09-23T10:05:00Z")
        self.assertEqual(signals[0]["rule_version"], "rules-v2")
        for unsupported in ("rules-v3", "rules-v999", "anything"):
            with self.subTest(rule_version=unsupported):
                with self.assertRaisesRegex(ValueError, "unsupported rule_version"):
                    LIVE.replay_signals(events, rule_version=unsupported, as_of="2026-09-23T10:05:00Z")


if __name__ == "__main__":
    unittest.main()
