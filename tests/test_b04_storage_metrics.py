import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


LIVE = load_module("live_intelligence_b04", DOUYIN_ROOT / "live_intelligence.py")
BILIBILI = load_module("bilibili_server_b04", REPO_ROOT / "bilibili" / "server.py")
GOVERN = load_module("govern_databases_b04", REPO_ROOT / "scripts" / "govern_databases.py")
ADAPTER = load_module("douyin_adapter_b04", DOUYIN_ROOT / "douyin_adapter.py")


class StorageMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "events.sqlite3"
        self.store = LIVE.LiveEventStore(self.db_path)
        self.session_a = self.store.start_session("douyin", "room-1", "A", "https://example.test/a")
        self.session_b = self.store.start_session("douyin", "room-1", "B", "https://example.test/b")
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def event(self, **kwargs):
        values = {
            "room_id": "room-1",
            "event_type": "comment",
            "user_id": "user-1",
            "user_name": "测试用户",
            "content": "怎么买",
            "timestamp": self.timestamp,
            "provider": "douyin",
        }
        values.update(kwargs)
        return LIVE.normalize_event(**values)

    def test_time_window_reads_more_than_the_display_limit(self):
        for index in range(1500):
            self.assertTrue(self.store.insert_event(self.session_a, self.event(metadata={"event_id": f"source-{index}"})))

        events = self.store.recent_events(self.session_a, limit=None, since=self.timestamp)
        analysis = LIVE.SignalEngine().build(events, online=None)

        self.assertEqual(len(events), 1500)
        self.assertEqual(analysis["windows"]["5m"]["comments"], 1500)

    def test_event_identity_is_scoped_and_same_second_without_source_id_is_not_deduped(self):
        source_a = self.event(metadata={"event_id": "platform-42"})
        source_b = self.event(metadata={"event_id": "platform-42"})
        self.assertTrue(self.store.insert_event(self.session_a, source_a))
        self.assertTrue(self.store.insert_event(self.session_b, source_b))
        source_cross_platform = self.event(provider="bilibili", metadata={"event_id": "platform-42"})
        self.assertTrue(self.store.insert_event(self.session_a, source_cross_platform))

        same_second_a = self.event(metadata={})
        same_second_b = self.event(metadata={})
        self.assertTrue(self.store.insert_event(self.session_a, same_second_a))
        self.assertTrue(self.store.insert_event(self.session_a, same_second_b))

        events = self.store.recent_events(None, limit=None)
        self.assertEqual(len(events), 5)

    def test_same_source_id_with_different_content_is_an_explicit_conflict(self):
        self.assertTrue(self.store.insert_event(self.session_a, self.event(metadata={"event_id": "conflict-1"})))
        with self.assertRaises(LIVE.EventConflictError):
            self.store.insert_event(
                self.session_a,
                self.event(content="不同内容", metadata={"event_id": "conflict-1"}),
            )

    def test_question_analysis_survives_store_round_trip(self):
        self.assertTrue(self.store.insert_event(self.session_a, self.event()))
        event = self.store.recent_events(self.session_a, limit=None)[0]
        self.assertEqual(event["analysis"]["is_question"], "true")
        self.assertEqual(event["is_question"], "true")

    def test_unknown_online_is_not_persisted_as_real_zero(self):
        self.store.insert_snapshot(self.session_a, {"online": None})
        snapshot = self.store.recent_snapshots(self.session_a, 1)[0]
        self.assertIsNone(snapshot["online"])

    def test_foreign_keys_are_enabled_and_reject_orphan_rows(self):
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time) VALUES(?,?,?,?,?,?)",
                ("orphan", 99999, "douyin", "room-1", "comment", self.timestamp),
            )
            self.store.connection.commit()
        self.store.connection.rollback()


class BilibiliContractTests(unittest.TestCase):
    def test_gift_event_exposes_quantity_and_value_semantics_separately(self):
        event = BILIBILI.parse_business_event(
            b'{"cmd":"SEND_GIFT","data":{"uid":7,"uname":"u","giftName":"rose","num":3,"price":1000}}'
        )
        self.assertEqual(event["gift_num"], 3)
        self.assertEqual(event["value_contract"]["quantity"], 3)
        self.assertEqual(event["value_contract"]["quantity_semantics"], "platform_reported")
        self.assertIsNone(event["value_contract"]["currency"])
        self.assertIsNone(event["value_contract"]["estimated_value"])

    def test_bilibili_store_preserves_unknown_online(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BILIBILI.EventStore(Path(directory) / "bili.sqlite3")
            session = store.start_session("room-1", "测试")
            store.insert_snapshot(session, {"online": None})
            self.assertIsNone(store.recent_snapshots(session, 1)[0]["online"])
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            store.close()

    def test_bilibili_metrics_do_not_present_platform_amount_as_currency(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BILIBILI.EventStore(Path(directory) / "bili.sqlite3")
            session = store.start_session("room-1", "测试")
            store.insert_event(session, {"type": "gift", "uid": 7, "uname": "u", "gift_name": "rose", "gift_num": 3, "amount": 1000})
            collector = BILIBILI.Collector(store)
            collector.session_id = session
            collector.status_name = "connected"
            collector.online_observed = True
            metrics = collector.metrics()
            self.assertEqual(metrics["gift_quantity"], 3)
            self.assertIsNone(metrics["revenue"])
            self.assertEqual(metrics["revenue_semantics"], "unknown_currency_not_estimated")
            store.close()


class HeartbeatSnapshotTests(unittest.TestCase):
    def test_demo_adapter_exposes_a_heartbeat_hook_for_quiet_period_snapshots(self):
        adapter = ADAPTER.DouyinPublicAdapter("https://live.douyin.com/123", "", "demo")
        stop_event = __import__("threading").Event()
        heartbeat_count = []

        def stop_after_heartbeat():
            heartbeat_count.append(True)
            stop_event.set()

        adapter.run(stop_event, lambda _event: None, lambda _name, _title: None, on_heartbeat=stop_after_heartbeat)
        self.assertEqual(len(heartbeat_count), 1)

    def test_online_contract_distinguishes_observed_zero_from_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = ADAPTER.DouyinCollector(Path(directory) / "douyin.sqlite3", mode="demo")
            collector.session_id = collector.store.start_session("douyin", "room", "title", "url")
            collector.status_name = "connected"
            collector.online = 42
            collector.online_observed = True
            self.assertEqual(collector.status()["online"], 42)
            collector.online = 0
            self.assertEqual(collector.status()["online"], 0)
            collector.online_observed = False
            self.assertIsNone(collector.status()["online"])
            collector.store.close()


class MigrationSafetyTests(unittest.TestCase):
    def test_read_only_connect_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                GOVERN.connect(missing)
            self.assertFalse(missing.exists())

    def test_backup_is_content_verified_and_existing_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.sqlite3"
            backup_path = Path(directory) / "backup.sqlite3"
            source = sqlite3.connect(source_path)
            source.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
            source.execute("INSERT INTO sample(value) VALUES('safe')")
            source.commit()
            GOVERN.backup(source, backup_path)
            GOVERN.verify_backup(source, backup_path)
            with self.assertRaises(FileExistsError):
                GOVERN.backup(source, backup_path)
            source.close()

    def test_migration_does_not_silently_skip_conflicting_event_id(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.sqlite3"
            target_path = Path(directory) / "target.sqlite3"
            source = sqlite3.connect(source_path)
            target = sqlite3.connect(target_path)
            source.row_factory = sqlite3.Row
            target.row_factory = sqlite3.Row
            GOVERN.create_douyin_schema(source)
            GOVERN.create_douyin_schema(target)
            source.execute(
                "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at) VALUES(?,?,?,?,?)",
                ("douyin", "room", "source", "url", "2026-01-01T00:00:00+00:00"),
            )
            target.execute(
                "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at) VALUES(?,?,?,?,?)",
                ("douyin", "room", "target", "url", "2026-01-01T00:00:00+00:00"),
            )
            source.execute(
                "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content) VALUES(?,?,?,?,?,?,?)",
                ("same-id", 1, "douyin", "room", "comment", "2026-01-01T00:00:01+00:00", "source"),
            )
            target.execute(
                "INSERT INTO live_events(event_id,session_id,provider,room_id,event_type,event_time,content) VALUES(?,?,?,?,?,?,?)",
                ("same-id", 1, "douyin", "room", "comment", "2026-01-01T00:00:01+00:00", "target"),
            )
            source.commit()
            target.commit()
            with self.assertRaises(GOVERN.MigrationConflict):
                GOVERN.migrate_legacy_live(source, target, True)
            source.close()
            target.close()


if __name__ == "__main__":
    unittest.main()
