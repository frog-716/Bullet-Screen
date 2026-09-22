import importlib.util
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


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


BILI = load_module("bilibili_r2_02_snapshot", ROOT / "bilibili" / "server.py")
ADAPTER = load_module("adapter_r2_02_snapshot", ROOT / "douyin" / "douyin_adapter.py")
LIVE = load_module("live_r2_02_snapshot", ROOT / "douyin" / "live_intelligence.py")


class SnapshotSignalTests(unittest.TestCase):
    def test_bilibili_snapshot_keeps_signal_in_current_run(self):
        store = BILI.EventStore(":memory:")
        session = store.start_session("room", "fixture")
        collector = BILI.Collector(store)
        collector.room_id = "room"
        collector.session_id = session
        collector.status_name = "connected"
        collector._context = BILI.RunContext(4, "run-bili", "bilibili", "room", "")
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for uid in (1, 2):
            store.insert_event(session, {"type": "danmaku", "event_time": now, "uid": uid, "uname": f"u{uid}", "text": "想买"})
        snapshot = collector.snapshot("60s", 100)
        self.assertEqual(snapshot["session_id"], session)
        self.assertEqual(snapshot["run_id"], "run-bili")
        self.assertEqual(snapshot["generation"], 4)
        self.assertEqual(snapshot["signals"], snapshot["metrics"]["signals"])
        self.assertTrue(snapshot["signals"])
        self.assertEqual(snapshot["signals"][0]["session_id"], session)
        self.assertEqual(snapshot["signals"][0]["generation"], 4)
        store.close()

    def test_douyin_adapter_snapshot_keeps_signal_in_current_run(self):
        collector = ADAPTER.DouyinCollector(Path(":memory:"), mode="demo")
        session = collector.store.start_session("douyin", "room", "fixture", "https://example.test/room")
        collector.room_id = "room"
        collector.session_id = session
        collector.status_name = "connected"
        collector._context = ADAPTER.RunContext(5, "run-douyin", "douyin", "room", "https://example.test/room", "", "demo")
        collector.last_valid_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        collector._last_valid_monotonic = time.monotonic()
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for uid in ("u1", "u2"):
            item = LIVE.normalize_event("room", "comment", uid, uid, "想买", timestamp=now, metadata={"event_id": uid})
            self.assertTrue(collector.store.insert_event(session, item))
        snapshot = collector.snapshot("60s", 100)
        self.assertEqual(snapshot["session_id"], session)
        self.assertEqual(snapshot["run_id"], "run-douyin")
        self.assertEqual(snapshot["generation"], 5)
        self.assertEqual(snapshot["signals"], snapshot["metrics"]["signals"])
        self.assertTrue(snapshot["signals"])
        self.assertEqual(snapshot["signals"][0]["generation"], 5)
        collector.store.close()


if __name__ == "__main__":
    unittest.main()
