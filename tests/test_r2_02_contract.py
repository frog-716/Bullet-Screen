import importlib.util
import sys
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


BILI = load_module("bilibili_r2_02_contract", ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_r2_02_contract", ROOT / "douyin" / "server.py")
LIVE = load_module("live_r2_02_contract", ROOT / "douyin" / "live_intelligence.py")


class ThreeEntrySignalContractTests(unittest.TestCase):
    def _as_of(self):
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _assert_signal_contract(self, store, event_table, session_id, provider, room_id, run_id, events):
        as_of = self._as_of()
        normalized = []
        for item in events:
            if event_table == "live_events":
                normalized.append(item)
            else:
                analysis = LIVE.analyze_text(item["text"] or "")
                normalized.append({
                    "event_id": item["event_id"], "_db_event_id": item["_db_event_id"],
                    "type": "comment", "timestamp_utc": item["event_time"],
                    "user_id": item["uid"], "user_name": item["uname"],
                    "content": item["text"] or "", "analysis": analysis,
                })
        coverage = store.coverage(session_id, *LIVE._window_bounds(as_of, 60))
        result = LIVE.SignalEngine().build(
            normalized, None, [], provider=provider, room_id=room_id,
            session_id=session_id, run_id=run_id, as_of=as_of,
            coverage=coverage["coverage_state"],
        )
        self.assertTrue(result["signals"])
        signal = result["signals"][0]
        for key in ("signal_id", "signal_type", "rule_version", "status", "strength", "coverage", "event_count", "unique_user_count", "reason", "evidence_event_ids"):
            self.assertIn(key, signal)
        self.assertEqual(signal["provider"], provider)
        self.assertEqual(signal["session_id"], session_id)
        self.assertEqual(signal["run_id"], run_id)
        LIVE.persist_signal(store.connection, signal, event_table)
        stored = LIVE.load_signals(store.connection, session_id, provider, room_id, run_id, event_table)
        self.assertEqual(stored[0]["session_id"], session_id)
        self.assertEqual(stored[0]["rule_version"], "rules-v2")
        return stored[0]

    def test_bilibili_server_store_contract(self):
        store = BILI.EventStore(":memory:")
        session = store.start_session("room", "fixture")
        now = self._as_of()
        for uid in (1, 2):
            store.insert_event(session, {"type": "danmaku", "event_time": now, "uid": uid, "uname": f"u{uid}", "text": "想买"})
        stored = self._assert_signal_contract(store, "events", session, "bilibili", "room", "run-bili", store.recent_events(session, None))
        self.assertEqual(len(stored["evidence"]), 2)
        store.close()

    def test_douyin_server_provider_bilibili_store_contract(self):
        store = DOUYIN_SERVER.EventStore(":memory:")
        session = store.start_session("room", "fixture")
        now = self._as_of()
        for uid in (1, 2):
            store.insert_event(session, {"type": "danmaku", "event_time": now, "uid": uid, "uname": f"u{uid}", "text": "想买"})
        stored = self._assert_signal_contract(store, "events", session, "bilibili", "room", "run-compat", store.recent_events(session, None))
        self.assertEqual(len(stored["evidence"]), 2)
        store.close()

    def test_douyin_normalized_store_contract(self):
        store = LIVE.LiveEventStore(":memory:")
        session = store.start_session("douyin", "room", "fixture", "https://example.test/room")
        now = self._as_of()
        for uid in ("u1", "u2"):
            item = LIVE.normalize_event("room", "comment", uid, uid, "想买", timestamp=now, metadata={"event_id": uid})
            self.assertTrue(store.insert_event(session, item))
        stored = self._assert_signal_contract(store, "live_events", session, "douyin", "room", "run-douyin", store.recent_events(session, None))
        self.assertEqual(len(stored["evidence"]), 2)
        store.close()


if __name__ == "__main__":
    unittest.main()
