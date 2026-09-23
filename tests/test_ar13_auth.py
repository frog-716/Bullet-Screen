import importlib.util
import sys
import unittest
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


BILIBILI = load_module("bilibili_ar13_auth", ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_ar13_auth", ROOT / "douyin" / "server.py")


class BilibiliAuthenticationFreshnessContractTests(unittest.TestCase):
    IMPLEMENTATIONS = (
        ("official", BILIBILI),
        ("compatibility", DOUYIN_SERVER),
    )

    def make_collector(self, module, status="authenticating"):
        store = module.EventStore(":memory:")
        collector = module.Collector(store)
        session_id = store.start_session("100", "synthetic room")
        context = module.RunContext(7, "ar13-synthetic-run", "bilibili", "100", "")
        collector._context = context
        collector.session_id = session_id
        collector.room_id = "100"
        collector.status_name = status
        store.open_gap(
            "bilibili", "100", session_id, context.run_id, "connecting",
            started_at=module.utc_now(),
        )
        return store, collector, context, session_id

    def test_failed_auth_does_not_refresh_or_create_trusted_activity(self):
        for label, module in self.IMPLEMENTATIONS:
            with self.subTest(implementation=label):
                store, collector, context, session_id = self.make_collector(module)
                try:
                    now = datetime.now(timezone.utc)
                    session_start = (now - timedelta(minutes=10)).isoformat()
                    store.connection.execute(
                        "UPDATE sessions SET started_at=? WHERE id=?",
                        (session_start, session_id),
                    )
                    store.connection.execute(
                        "UPDATE capture_gaps SET gap_start=? WHERE session_id=?",
                        (session_start, session_id),
                    )
                    store.connection.commit()
                    failed_auth = module.PacketCodec.pack(module.json_bytes({"code": -101}), 8, 1)
                    with self.assertRaises(module.ProtocolError):
                        collector._handle_packet(context, failed_auth)

                    self.assertEqual(collector.last_valid_at, "")
                    self.assertNotEqual(collector.status_name, "connected")
                    self.assertFalse(context.capture_verified.is_set())
                    self.assertEqual(store.gaps(session_id)[0]["status"], "open")
                    self.assertEqual(store.recent_events(session_id, None), [])
                    self.assertEqual(collector.total, 0)
                    self.assertEqual(collector.likes, 0)

                    collector._finish_run(context, session_id, "error")
                    gaps = store.gaps(session_id)
                    self.assertEqual(gaps[0]["reason"], "protocol_unavailable")
                    self.assertEqual(gaps[0]["status"], "closed")
                    store.connection.execute(
                        "UPDATE sessions SET ended_at=? WHERE id=?",
                        ((now + timedelta(minutes=1)).isoformat(), session_id),
                    )
                    store.connection.execute(
                        "INSERT INTO metric_snapshots(session_id,recorded_at) VALUES(?,?)",
                        (session_id, (now + timedelta(seconds=10)).isoformat()),
                    )
                    store.connection.commit()
                    snapshot = collector.snapshot()
                    self.assertEqual(snapshot["status"], "error")
                    self.assertIsNone(snapshot["last_valid_at"])
                    self.assertIsNone(snapshot["metrics"]["last_valid_at"])
                    self.assertEqual(snapshot["metrics"]["total"], 0)
                    self.assertEqual(snapshot["metrics"]["rate"], 0)
                    self.assertEqual(snapshot["metrics"]["signals"], [])
                    self.assertEqual(snapshot["events"]["items"], [])
                    coverage = store.coverage(
                        session_id,
                        (now + timedelta(seconds=2)).isoformat(),
                        (now + timedelta(seconds=30)).isoformat(),
                    )
                    self.assertEqual(coverage["coverage_state"], "unknown")
                    self.assertFalse(coverage["complete"])
                    self.assertNotEqual(
                        store.coverage(
                            session_id,
                            session_start,
                            (now - timedelta(seconds=1)).isoformat(),
                        )["coverage_state"],
                        "reliable_with_data",
                    )
                finally:
                    store.close()

    def test_successful_auth_still_enters_connected_and_records_valid_time(self):
        for label, module in self.IMPLEMENTATIONS:
            with self.subTest(implementation=label):
                store, collector, context, session_id = self.make_collector(module)
                try:
                    packet = module.PacketCodec.pack(module.json_bytes({"code": 0}), 8, 1)
                    collector._handle_packet(context, packet)
                    self.assertEqual(collector.status_name, "connected")
                    self.assertTrue(context.capture_verified.is_set())
                    self.assertTrue(collector.last_valid_at)
                    self.assertFalse(any(gap["status"] == "open" for gap in store.gaps(session_id)))
                finally:
                    store.close()

    def test_valid_business_event_still_refreshes_and_persists(self):
        for label, module in self.IMPLEMENTATIONS:
            with self.subTest(implementation=label):
                store, collector, context, session_id = self.make_collector(module, "connected")
                try:
                    collector.last_valid_at = "before-valid-event"
                    body = module.json_bytes({
                        "cmd": "DANMU_MSG",
                        "info": [None, "synthetic comment", [123, "synthetic viewer"]],
                    })
                    collector._handle_packet(context, module.PacketCodec.pack(body, 5, 1))
                    self.assertNotEqual(collector.last_valid_at, "before-valid-event")
                    self.assertEqual(len(store.recent_events(session_id, None)), 1)
                    self.assertEqual(collector.total, 1)
                finally:
                    store.close()

    def test_valid_heartbeat_still_refreshes_and_unknown_does_not_authenticate(self):
        for label, module in self.IMPLEMENTATIONS:
            with self.subTest(implementation=label):
                store, collector, context, session_id = self.make_collector(module, "connected")
                try:
                    collector.last_valid_at = "before-heartbeat"
                    collector._handle_packet(context, module.PacketCodec.pack(b"\x00\x00\x00\x09", 3, 1))
                    self.assertNotEqual(collector.last_valid_at, "before-heartbeat")

                    collector.status_name = "authenticating"
                    collector.last_valid_at = "before-unknown"
                    collector._handle_packet(context, module.PacketCodec.pack(b"ignored", 99, 1))
                    self.assertEqual(collector.status_name, "authenticating")
                    self.assertFalse(context.capture_verified.is_set())
                    self.assertEqual(store.gaps(session_id)[0]["status"], "open")
                    self.assertEqual(store.recent_events(session_id, None), [])

                    collector.last_valid_at = "before-malformed-auth"
                    malformed_auth = module.PacketCodec.pack(b"not-json", 8, 1)
                    with self.assertRaises(module.ProtocolError):
                        collector._handle_packet(context, malformed_auth)
                    self.assertEqual(collector.last_valid_at, "before-malformed-auth")
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
