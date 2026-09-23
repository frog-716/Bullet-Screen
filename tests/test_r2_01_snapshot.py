import http.client
import importlib.util
import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOUYIN_ROOT = REPO_ROOT / "douyin"
if str(DOUYIN_ROOT) not in sys.path:
    sys.path.insert(0, str(DOUYIN_ROOT))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BILIBILI = load_module("bilibili_server_r2_01", REPO_ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_r2_01", DOUYIN_ROOT / "server.py")
DOUYIN_ADAPTER = load_module("douyin_adapter_r2_01", DOUYIN_ROOT / "douyin_adapter.py")


class SnapshotFixtures:
    @staticmethod
    def bilibili(module):
        store = module.EventStore(":memory:")
        collector = module.Collector(store)
        session_id = store.start_session("100", "测试房间")
        now = datetime.now(timezone.utc)
        capture_started = (now - timedelta(minutes=10)).isoformat()
        protocol_started = (now - timedelta(minutes=9)).isoformat()
        store.connection.execute("UPDATE sessions SET started_at=? WHERE id=?", (capture_started, session_id))
        connection_gap = store.open_gap("bilibili", "100", session_id, "run-1", "connecting", capture_started)
        store.close_gap(connection_gap, protocol_started)
        collector.generation = 1
        collector._context = module.RunContext(1, "run-1", "bilibili", "100", "")
        collector.session_id = session_id
        collector.room_id = "100"
        collector.room_title = "测试房间"
        collector.status_name = "connected"
        collector.last_valid_at = module.utc_now()
        collector.online = 0
        collector.online_observed = False
        store.insert_event(session_id, {"type": "danmaku", "event_time": module.utc_now(), "uid": 1, "uname": "用户", "text": "你好"})
        store.insert_snapshot(session_id, {"online": None, "likes": 0, "rate": 1, "total": 1})
        return collector, store, session_id

    @staticmethod
    def douyin():
        store = DOUYIN_ADAPTER.LiveEventStore(":memory:")
        collector = DOUYIN_ADAPTER.DouyinCollector(":memory:", mode="demo")
        collector.store.close()
        collector.store = store
        session_id = store.start_session("douyin", "200", "测试房间", "https://live.douyin.com/200")
        collector.generation = 1
        collector._context = DOUYIN_ADAPTER.RunContext(1, "run-1", "douyin", "200", "https://live.douyin.com/200", "", "demo")
        collector.session_id = session_id
        collector.room_id = "200"
        collector.room_url = "https://live.douyin.com/200"
        collector.room_title = "测试房间"
        collector.status_name = "connected"
        collector.last_valid_at = DOUYIN_ADAPTER.utc_now()
        collector.online = 0
        collector.online_observed = False
        event = DOUYIN_ADAPTER.normalize_event("200", "comment", "u1", "用户", "你好")
        store.insert_event(session_id, event)
        store.insert_snapshot(session_id, {"online": None, "comment_rate": 1, "like_rate": 0, "gift_rate": 0, "active_users": 1, "heat_score": 1, "purchase_ratio": 0, "positive_ratio": 0, "negative_ratio": 0})
        return collector, store, session_id


class SnapshotContractTests(unittest.TestCase):
    def test_three_entries_return_the_same_snapshot_contract(self):
        cases = (
            ("bilibili", lambda: SnapshotFixtures.bilibili(BILIBILI)),
            ("bilibili", lambda: SnapshotFixtures.bilibili(DOUYIN_SERVER)),
            ("douyin", SnapshotFixtures.douyin),
        )
        required = {
            "provider", "room_id", "session_id", "run_id", "generation", "status", "worker_alive",
            "data_source", "as_of", "last_valid_at", "freshness", "coverage", "metrics", "events",
            "error", "diagnostics",
        }
        for provider, factory in cases:
            collector, store, session_id = factory()
            try:
                snapshot = collector.snapshot()
                self.assertTrue(required.issubset(snapshot.keys()), provider)
                self.assertEqual(snapshot["provider"], provider)
                self.assertEqual(snapshot["session_id"], session_id)
                self.assertEqual(snapshot["generation"], snapshot["metrics"]["generation"])
                self.assertEqual(snapshot["session_id"], snapshot["metrics"]["session_id"])
                self.assertEqual(snapshot["session_id"], snapshot["events"]["session_id"])
                self.assertEqual(snapshot["session_id"], snapshot["coverage"]["session_id"])
                self.assertEqual(snapshot["as_of"], snapshot["metrics"]["as_of"])
                self.assertEqual(snapshot["as_of"], snapshot["events"]["as_of"])
                self.assertEqual(snapshot["as_of"], snapshot["coverage"]["as_of"])
                self.assertEqual(snapshot["events"]["generation"], snapshot["generation"])
            finally:
                store.close()

    def test_snapshot_retries_when_generation_changes_during_read(self):
        collector, store, _ = SnapshotFixtures.bilibili(BILIBILI)
        try:
            original = store.recent_events
            changed = {"value": False}

            def mutate_generation(session_id, limit=None, since=None):
                result = original(session_id, limit, since)
                if not changed["value"]:
                    changed["value"] = True
                    collector.generation = 2
                    collector._context = BILIBILI.RunContext(2, "run-2", "bilibili", "100", "")
                return result

            store.recent_events = mutate_generation
            snapshot = collector.snapshot()
            self.assertEqual(snapshot["generation"], 2)
            self.assertEqual(snapshot["run_id"], "run-2")
        finally:
            store.close()

    def test_snapshot_retries_when_session_changes_during_read(self):
        collector, store, first_session = SnapshotFixtures.bilibili(BILIBILI)
        second_session = store.start_session("200", "新房间")
        store.insert_event(second_session, {"type": "danmaku", "event_time": BILIBILI.utc_now(), "uid": 2, "uname": "新用户", "text": "新会话"})
        try:
            original = store.recent_events
            changed = {"value": False}

            def mutate_session(session_id, limit=None, since=None):
                result = original(session_id, limit, since)
                if not changed["value"]:
                    changed["value"] = True
                    collector.session_id = second_session
                    collector.room_id = "200"
                    collector._context = BILIBILI.RunContext(1, "run-2", "bilibili", "200", "")
                return result

            store.recent_events = mutate_session
            snapshot = collector.snapshot()
            self.assertNotEqual(snapshot["session_id"], first_session)
            self.assertEqual(snapshot["session_id"], second_session)
            self.assertEqual(snapshot["room_id"], "200")
            self.assertTrue(any(item.get("text") == "新会话" for item in snapshot["events"]["items"]))
        finally:
            store.close()

    def test_stale_snapshot_exposes_gap_and_freshness(self):
        collector, store, session_id = SnapshotFixtures.bilibili(BILIBILI)
        try:
            collector.status_name = "stale"
            store.open_gap("bilibili", "100", session_id, "run-1", "stale", started_at=collector.last_valid_at)
            snapshot = collector.snapshot()
            self.assertEqual(snapshot["status"], "stale")
            self.assertTrue(snapshot["freshness"]["stale"])
            self.assertEqual(snapshot["coverage"]["coverage_state"], "gap")
            self.assertTrue(snapshot["coverage"]["has_open_gap"])
        finally:
            store.close()

    def test_empty_reliable_window_is_not_unknown(self):
        store = BILIBILI.EventStore(":memory:")
        collector = BILIBILI.Collector(store)
        session_id = store.start_session("100", "空窗口")
        now = datetime.now(timezone.utc)
        capture_started = (now - timedelta(minutes=10)).isoformat()
        protocol_started = (now - timedelta(minutes=9)).isoformat()
        store.connection.execute("UPDATE sessions SET started_at=? WHERE id=?", (capture_started, session_id))
        connection_gap = store.open_gap("bilibili", "100", session_id, "run-1", "connecting", capture_started)
        store.close_gap(connection_gap, protocol_started)
        collector.generation = 1
        collector._context = BILIBILI.RunContext(1, "run-1", "bilibili", "100", "")
        collector.session_id = session_id
        collector.room_id = "100"
        collector.status_name = "connected"
        collector.last_valid_at = BILIBILI.utc_now()
        store.insert_snapshot(session_id, {"online": 0, "likes": 0, "rate": 0, "total": 0})
        try:
            snapshot = collector.snapshot()
            self.assertEqual(snapshot["coverage"]["coverage_state"], "reliable_no_events")
            self.assertEqual(snapshot["coverage"]["event_count"], 0)
        finally:
            store.close()

    def test_unknown_online_stays_unknown_and_observed_zero_stays_zero(self):
        collector, store, _ = SnapshotFixtures.bilibili(BILIBILI)
        try:
            unknown = collector.snapshot()
            self.assertIsNone(unknown["metrics"]["online"])
            collector.online_observed = True
            zero = collector.snapshot()
            self.assertEqual(zero["metrics"]["online"], 0)
        finally:
            store.close()


class SnapshotHttpTests(unittest.TestCase):
    def test_snapshot_route_returns_the_contract_for_both_http_handlers(self):
        payload = {
            "provider": "bilibili", "room_id": "100", "session_id": None, "run_id": None,
            "generation": 0, "status": "idle", "worker_alive": False, "data_source": "none",
            "as_of": "2026-09-21T00:00:00+00:00", "last_valid_at": None,
            "freshness": {"stale": False}, "coverage": {"coverage_state": "unknown"},
            "metrics": {"session_id": None, "generation": 0, "as_of": "2026-09-21T00:00:00+00:00"},
            "events": {"items": [], "session_id": None, "generation": 0}, "error": None, "diagnostics": {},
        }

        class FakeCollector:
            provider_name = "bilibili"

            def snapshot(self, **kwargs):
                return payload

        for module in (BILIBILI, DOUYIN_SERVER):
            handler = type("R201Handler", (module.AppHandler,), {"collector": FakeCollector(), "capability_token": "test-token"})
            server = module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                connection.request("GET", "/api/snapshot?window=60s", headers={
                    "Host": f"127.0.0.1:{server.server_address[1]}",
                    "X-Bullet-Screen-Token": "test-token",
                })
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(body["generation"], 0)
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                connection.request("GET", "/snapshot_client.js", headers={
                    "Host": f"127.0.0.1:{server.server_address[1]}",
                })
                static_response = connection.getresponse()
                static_body = static_response.read().decode("utf-8")
                connection.close()
                self.assertEqual(static_response.status, 200)
                self.assertIn("SnapshotClient", static_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
