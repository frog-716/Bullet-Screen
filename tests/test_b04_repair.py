import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
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


BILIBILI = load_module("bilibili_server_b04_repair", REPO_ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_b04_repair", DOUYIN_ROOT / "server.py")
LIVE = load_module("live_intelligence_b04_repair", DOUYIN_ROOT / "live_intelligence.py")
ADAPTER = load_module("douyin_adapter_b04_repair", DOUYIN_ROOT / "douyin_adapter.py")
GOVERN = load_module("govern_databases_b04_repair", REPO_ROOT / "scripts" / "govern_databases.py")


class SchemaSignatureTests(unittest.TestCase):
    def test_v3_with_missing_index_fails_closed_for_bilibili_and_douyin(self):
        for store_class, index_name in (
            (BILIBILI.EventStore, "idx_events_type"),
            (LIVE.LiveEventStore, "idx_live_events_type"),
        ):
            with self.subTest(store=store_class.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "store.sqlite3"
                    store = store_class(path)
                    store.connection.execute(f"DROP INDEX {index_name}")
                    store.connection.commit()
                    store.close()
                    with self.assertRaises(store_class.SchemaVersionError):
                        store_class(path)

    def test_v3_with_missing_foreign_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            store = LIVE.LiveEventStore(path)
            store.connection.execute("PRAGMA foreign_keys=OFF")
            store.connection.execute("ALTER TABLE live_events RENAME TO live_events_old")
            store.connection.execute(
                """CREATE TABLE live_events(
                  id INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                  session_id INTEGER NOT NULL, provider TEXT NOT NULL,
                  room_id TEXT NOT NULL, event_type TEXT NOT NULL,
                  event_time TEXT NOT NULL, user_id TEXT, user_name TEXT,
                  content TEXT, metadata_json TEXT, topic TEXT, intent TEXT,
                  sentiment TEXT, purchase_intent TEXT
                )"""
            )
            store.connection.execute("INSERT INTO live_events SELECT * FROM live_events_old")
            store.connection.execute("DROP TABLE live_events_old")
            store.connection.execute("CREATE INDEX idx_live_events_session_time ON live_events(session_id, event_time)")
            store.connection.execute("CREATE INDEX idx_live_events_type ON live_events(session_id, event_type)")
            store.connection.commit()
            store.close()
            with self.assertRaises(LIVE.SchemaVersionError):
                LIVE.LiveEventStore(path)

    def test_v3_with_renamed_required_column_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            store = BILIBILI.EventStore(path)
            store.connection.execute("ALTER TABLE sessions RENAME COLUMN room_title TO title")
            store.connection.commit()
            store.close()
            with self.assertRaises(BILIBILI.SchemaVersionError):
                BILIBILI.EventStore(path)


def make_legacy_live(path, *, title="title", content="comment", version=2):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    GOVERN.create_douyin_schema(connection)
    connection.execute(f"PRAGMA user_version={version}")
    connection.execute(
        "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at) VALUES(?,?,?,?,?)",
        ("douyin", "room", title, "https://example.test/room", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content) VALUES(?,?,?,?,?,?,?)",
        ("event-1", 1, "douyin", "room", "comment", "2026-01-01T00:00:01+00:00", content),
    )
    connection.execute(
        "INSERT INTO live_metric_snapshots(session_id,recorded_at,online) VALUES(?,?,?)",
        (1, "2026-01-01T00:00:02+00:00", None),
    )
    connection.commit()
    connection.close()


def make_bilibili_v2(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE sessions(
          id INTEGER PRIMARY KEY, room_id TEXT NOT NULL, room_title TEXT NOT NULL,
          started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL DEFAULT 'running'
        );
        CREATE TABLE events(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          event_time TEXT NOT NULL, uid INTEGER, uname TEXT, text TEXT, gift_name TEXT,
          gift_num INTEGER, amount INTEGER, popularity INTEGER, received_at TEXT, raw_json TEXT,
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
        CREATE INDEX idx_capture_gaps_session_time ON capture_gaps(session_id, gap_start, gap_end);"""
    )
    connection.execute("INSERT INTO sessions(room_id,room_title,started_at) VALUES(?,?,?)", ("6", "title", "2026-01-01T00:00:00+00:00"))
    connection.execute(
        "INSERT INTO events(session_id,event_type,event_time,text,received_at,raw_json) VALUES(?,?,?,?,?,?)",
        (1, "danmaku", "2026-01-01T00:00:01+00:00", "legacy", "2026-01-01T00:00:01+00:00", "{}"),
    )
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()


class MigrationRepairTests(unittest.TestCase):
    def test_database_lock_is_visible_to_an_os_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "store.sqlite3"
            lock_path = database.with_name(database.name + ".lock")
            code = (
                "import fcntl, pathlib, sys, time; "
                "p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); "
                "h=p.open('a+'); fcntl.flock(h.fileno(), fcntl.LOCK_EX); print('locked', flush=True); time.sleep(1.5)"
            )
            child = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(RuntimeError):
                    GOVERN._acquire_database_lock(database)
            finally:
                child.terminate()
                child.wait(timeout=3)
                if child.stdout:
                    child.stdout.close()

    def test_same_target_migration_is_idempotent_and_session_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            make_legacy_live(source)
            target_connection = sqlite3.connect(target)
            GOVERN.create_douyin_schema(target_connection)
            target_connection.close()

            source_connection = sqlite3.connect(source)
            target_connection = sqlite3.connect(target)
            source_connection.row_factory = sqlite3.Row
            target_connection.row_factory = sqlite3.Row
            GOVERN.migrate_legacy_live(source_connection, target_connection, True)
            source_connection.close()
            target_connection.close()
            source_connection = sqlite3.connect(source)
            target_connection = sqlite3.connect(target)
            source_connection.row_factory = sqlite3.Row
            target_connection.row_factory = sqlite3.Row
            try:
                GOVERN.migrate_legacy_live(source_connection, target_connection, True)
            finally:
                source_connection.close()
                target_connection.close()
            check = sqlite3.connect(target)
            self.assertEqual(check.execute("SELECT count(*) FROM live_sessions").fetchone()[0], 1)
            self.assertEqual(check.execute("SELECT count(*) FROM live_events").fetchone()[0], 1)
            self.assertEqual(check.execute("SELECT count(*) FROM live_metric_snapshots").fetchone()[0], 1)
            check.close()

            conflict = root / "conflict.sqlite3"
            target_connection = sqlite3.connect(conflict)
            GOVERN.create_douyin_schema(target_connection)
            target_connection.execute(
                "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at) VALUES(?,?,?,?,?)",
                ("douyin", "room", "different", "https://example.test/room", "2026-01-01T00:00:00+00:00"),
            )
            target_connection.commit()
            target_connection.close()
            with self.assertRaises(GOVERN.MigrationConflict):
                source_connection = sqlite3.connect(source)
                target_connection = sqlite3.connect(conflict)
                source_connection.row_factory = sqlite3.Row
                target_connection.row_factory = sqlite3.Row
                try:
                    GOVERN.migrate_legacy_live(source_connection, target_connection, True)
                finally:
                    source_connection.close()
                    target_connection.close()

    def test_apply_migration_uses_isolated_candidates_and_failure_keeps_active_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            make_bilibili_v2(bilibili)
            make_legacy_live(douyin)
            original = {path: path.read_bytes() for path in (bilibili, douyin)}

            result = GOVERN.apply_migration(bilibili, douyin, root / "apply-workspace")
            self.assertEqual(result["status"], "applied")
            self.assertTrue(Path(result["backup"]["bilibili"]).is_file())
            self.assertTrue(Path(result["backup"]["douyin"]).is_file())
            for path in (bilibili, douyin):
                connection = sqlite3.connect(path)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                connection.close()
            second = GOVERN.apply_migration(bilibili, douyin, root / "apply-workspace-repeat")
            self.assertEqual(second["status"], "applied")
            connection = sqlite3.connect(bilibili)
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            connection.close()

            for stage in ("copy", "verify-before", "migration", "verify-after", "switch-before", "switch"):
                with self.subTest(stage=stage):
                    bili = root / f"failure-{stage}-bili.sqlite3"
                    dou = root / f"failure-{stage}-douyin.sqlite3"
                    make_bilibili_v2(bili)
                    make_legacy_live(dou)
                    before = {path: path.read_bytes() for path in (bili, dou)}
                    with self.assertRaises(GOVERN.RehearsalFailure):
                        GOVERN.apply_migration(bili, dou, root / f"workspace-{stage}", fail_stage=stage)
                    self.assertEqual(before, {path: path.read_bytes() for path in (bili, dou)})

    def test_interrupted_switch_recovers_original_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bilibili = root / "bilibili.sqlite3"
            douyin = root / "douyin.sqlite3"
            make_legacy_live(bilibili)
            make_legacy_live(douyin)
            original = {path: path.read_bytes() for path in (bilibili, douyin)}
            workspace = root / "interrupted"
            workspace.mkdir()
            original_dir = workspace / "original"
            original_dir.mkdir()
            original_bili = original_dir / "bilibili.sqlite3"
            original_douyin = original_dir / "douyin.sqlite3"
            original_bili.write_bytes(original[bilibili])
            original_douyin.write_bytes(original[douyin])
            (workspace / "migration-state.json").write_text(json.dumps({
                "status": "switching",
                "databases": [
                    {"name": "bilibili", "active": str(bilibili), "original": str(original_bili)},
                    {"name": "douyin", "active": str(douyin), "original": str(original_douyin)},
                ],
            }))
            bilibili.write_bytes(b"partial candidate")
            self.assertTrue(GOVERN.recover_migration(workspace))
            self.assertEqual(original[bilibili], bilibili.read_bytes())
            self.assertEqual(original[douyin], douyin.read_bytes())


class CoverageRepairTests(unittest.TestCase):
    def test_timezone_normalization_and_four_state_coverage(self):
        store = LIVE.LiveEventStore(":memory:")
        session = store.start_session("douyin", "room", "title", "url")
        start = "2026-09-21T09:00:00+08:00"
        end = "2026-09-21T10:00:00+08:00"
        store.connection.execute(
            "UPDATE live_sessions SET started_at=?,ended_at=?,status='stopped' WHERE id=?",
            ("2026-09-21T00:00:00Z", "2026-09-21T02:00:00Z", session),
        )
        store.connection.commit()
        self.assertEqual(store.coverage(session, start, end)["coverage_state"], "unknown")
        connecting_gap = store.open_gap("douyin", "room", session, "run-1", "connecting", "2026-09-21T00:00:00Z")
        store.close_gap(connecting_gap, "2026-09-21T00:01:00Z")
        store.insert_snapshot(session, {"online": None})
        store.connection.execute("UPDATE live_metric_snapshots SET recorded_at=? WHERE session_id=?", ("2026-09-21T01:15:00+00:00", session))
        store.connection.commit()
        self.assertEqual(store.coverage(session, start, end)["coverage_state"], "reliable_no_events")
        store.insert_event(session, {"provider": "douyin", "room_id": "room", "type": "comment", "content": "hi", "timestamp": "2026-09-21T01:30:00Z", "metadata": {"source": "fetch_protobuf"}})
        self.assertEqual(store.coverage(session, start, end)["coverage_state"], "reliable_with_data")
        gap = store.open_gap("douyin", "room", session, "run-1", "stale", started_at="2026-09-21T01:45:00Z")
        self.assertEqual(store.coverage(session, start, end)["coverage_state"], "gap")
        store.close_gap(gap, ended_at="2026-09-21T01:50:00Z")
        closed_coverage = store.coverage(session, start, end)
        self.assertEqual(closed_coverage["coverage_state"], "gap")
        self.assertFalse(closed_coverage["has_open_gap"])
        store.close()


class HttpRepairTests(unittest.TestCase):
    def test_duplicate_content_length_is_rejected(self):
        class Headers:
            def get(self, key, default=None):
                return None

            def get_all(self, key):
                return ["2", "2"] if key == "Content-Length" else ["application/json"]

        class Request:
            headers = Headers()

        with self.assertRaises(BILIBILI.RequestError) as error:
            BILIBILI.AppHandler._read_json_body(Request())
        self.assertEqual(error.exception.status, 400)

    def test_total_body_deadline_limits_trickling_stream(self):
        class SlowStream:
            def read(self, size):
                time.sleep(0.03)
                return b"{"[:size]

        with self.assertRaises(BILIBILI.RequestError) as error:
            BILIBILI.read_body_with_deadline(SlowStream(), 4, 0.05)
        self.assertEqual(error.exception.status, 408)


class BilibiliCompatibilityContractTests(unittest.TestCase):
    def test_legacy_packet_guard_and_stopped_session_contract_match(self):
        stores = [BILIBILI.EventStore(":memory:"), DOUYIN_SERVER.EventStore(":memory:")]
        try:
            collectors = [BILIBILI.Collector(stores[0]), DOUYIN_SERVER.Collector(stores[1])]
            packet = BILIBILI.PacketCodec.pack(BILIBILI.json_bytes({"cmd": "DANMU_MSG", "info": [0, "hello"]}), 5, 1)
            for collector in collectors:
                session = collector.store.start_session("room", "title")
                collector.session_id = session
                collector.status_name = "stopped"
                collector._handle_packet(packet)
                self.assertEqual(collector.store.recent_events(session, None), [])
                status = collector.status()
                self.assertFalse(status["connected"])
                self.assertIsNone(status["session_id"])
                self.assertIn("data_source", status)
        finally:
            for store in stores:
                store.close()


class DouyinFreshnessRepairTests(unittest.TestCase):
    def test_empty_and_malformed_responses_do_not_count_as_activity(self):
        adapter = ADAPTER.DouyinPublicAdapter("https://live.douyin.com/123456", mode="auto")
        self.assertEqual(list(adapter._parse_payload(b"{}", "response")), [])
        self.assertEqual(list(adapter._parse_payload(b"not-json", "response")), [])

    def test_only_valid_emitted_event_refreshes_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = ADAPTER.DouyinCollector(Path(directory) / "douyin.sqlite3", mode="auto")
            context = ADAPTER.RunContext(1, "run-1", "douyin", "123456", "https://live.douyin.com/123456", "", "auto")
            collector.status_name = "connected"
            collector.session_id = collector.store.start_session("douyin", "room", "title", "url")
            collector._context = context
            collector._last_valid_monotonic = 0.0
            collector._ingest(context, {"type": "live_status", "content": "页面仍打开", "metadata": {"source": "playwright"}})
            self.assertEqual(collector._last_valid_monotonic, 0.0)
            collector._ingest(context, {"type": "comment", "user_name": "u", "content": "有效事件", "metadata": {"source": "fetch_protobuf"}})
            self.assertGreater(collector._last_valid_monotonic, 0.0)
            collector.store.close()

    def test_event_quiet_does_not_become_stale_until_protocol_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = ADAPTER.DouyinCollector(Path(directory) / "douyin.sqlite3", mode="auto")
            context = ADAPTER.RunContext(1, "run-1", "douyin", "123456", "https://live.douyin.com/123456", "", "auto")
            collector._context = context
            collector.session_id = collector.store.start_session("douyin", "room", "title", "url")
            collector.status_name = "connected"
            collector._connected_monotonic = time.monotonic()
            self.assertTrue(collector._mark_protocol(context))
            collector._last_valid_monotonic = time.monotonic() - ADAPTER.EVENT_QUIET_AFTER_SECONDS - 1
            self.assertEqual(collector.status()["status"], "connected")
            self.assertEqual(collector.status()["activity_state"], "quiet")
            collector._last_protocol_monotonic = time.monotonic() - ADAPTER.PROTOCOL_STALE_AFTER_SECONDS - 1
            self.assertEqual(collector.status()["status"], "stale")
            collector.store.close()


class UnknownValueContractTests(unittest.TestCase):
    def test_bilibili_parser_keeps_unknown_distinct_from_zero(self):
        missing_price = BILIBILI.parse_business_event(BILIBILI.json_bytes({"cmd": "SEND_GIFT", "data": {"num": 2}}))
        zero_price = BILIBILI.parse_business_event(BILIBILI.json_bytes({"cmd": "SEND_GIFT", "data": {"num": 2, "price": 0}}))
        missing_online = BILIBILI.parse_business_event(BILIBILI.json_bytes({"cmd": "WATCHED_CHANGE", "data": {}}))
        zero_online = BILIBILI.parse_business_event(BILIBILI.json_bytes({"cmd": "WATCHED_CHANGE", "data": {"num": 0}}))
        self.assertIsNone(missing_price["amount"])
        self.assertEqual(zero_price["amount"], 0)
        self.assertIsNone(missing_online["popularity"])
        self.assertEqual(zero_online["popularity"], 0)

    def test_chart_code_does_not_coerce_unknown_online_to_zero(self):
        self.assertNotIn("Number(point.online)||0", (REPO_ROOT / "bilibili" / "app.js").read_text())
        self.assertNotIn("Number(point.online)||0", (REPO_ROOT / "douyin" / "app.js").read_text())


if __name__ == "__main__":
    unittest.main()
