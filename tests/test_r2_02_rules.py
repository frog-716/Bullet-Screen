import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOUYIN = ROOT / "douyin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DOUYIN))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIVE = load_module("live_intelligence_r2_02", DOUYIN / "live_intelligence.py")


AS_OF = "2026-09-22T12:01:00+00:00"


def event(user, text, timestamp="2026-09-22T12:00:30+00:00", event_id=None):
    metadata = {"source": "fetch_protobuf"}
    if event_id:
        metadata["event_id"] = event_id
    return LIVE.normalize_event(
        "room-1", "comment", user, f"用户{user}", text,
        metadata=metadata, timestamp=timestamp, provider="douyin",
    )


class R202RuleTests(unittest.TestCase):
    def test_negation_and_attribution_are_not_positive_strong_evidence(self):
        self.assertEqual(LIVE.analyze_text("想买") ["purchase_intent"], "high")
        self.assertNotEqual(LIVE.analyze_text("不想买")["purchase_intent"], "high")
        self.assertEqual(LIVE.analyze_text("推荐")["recommendation"], "positive")
        self.assertEqual(LIVE.analyze_text("不推荐")["recommendation"], "negative")
        attributed = LIVE.analyze_text("他说这个不好用")
        self.assertTrue(attributed["uncertain"])
        self.assertNotEqual(attributed["sentiment"], "negative")

    def test_same_user_spam_does_not_meet_unique_user_threshold(self):
        signals = LIVE.replay_signals(
            [event("same", "想买", event_id="e1"), event("same", "想买", event_id="e2")],
            rule_version="rules-v2", as_of=AS_OF,
        )
        self.assertEqual(signals, [])

    def test_two_independent_users_produce_fact_only_signal_with_evidence(self):
        signals = LIVE.replay_signals(
            [event("u1", "想买", event_id="e1"), event("u2", "想买", event_id="e2")],
            rule_version="rules-v2", as_of=AS_OF,
        )
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["rule_version"], "rules-v2")
        self.assertEqual(signal["event_count"], 2)
        self.assertEqual(signal["unique_user_count"], 2)
        self.assertEqual(signal["strength"], "strong")
        self.assertEqual(signal["status"], "active")
        self.assertEqual(signal["evidence_event_ids"], ["e1", "e2"])
        self.assertIn("2 个独立用户", signal["reason"])
        self.assertNotIn("概率", signal["reason"])

    def test_gap_or_unknown_never_produces_strong_signal(self):
        events = [event("u1", "想买", event_id="e1"), event("u2", "想买", event_id="e2")]
        for coverage in ("gap", "unknown"):
            with self.subTest(coverage=coverage):
                signals = LIVE.replay_signals(events, rule_version="rules-v2", as_of=AS_OF, coverage=coverage)
                self.assertTrue(signals)
                self.assertNotEqual(signals[0]["strength"], "strong")
                self.assertIn(coverage, signals[0]["reason"])

    def test_replay_is_deterministic_and_does_not_need_wall_clock(self):
        events = [event("u1", "想买", event_id="e1"), event("u2", "想买", event_id="e2")]
        first = LIVE.replay_signals(events, rule_version="rules-v2", as_of=AS_OF)
        second = LIVE.replay_signals(events, rule_version="rules-v2", as_of=AS_OF)
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "unsupported rule_version"):
            LIVE.replay_signals(events, rule_version="rules-v3", as_of=AS_OF)

    def test_clear_threshold_and_cooldown_are_distinct(self):
        first_window = LIVE.replay_signals(
            [event("u1", "想买", event_id="same-1"), event("u2", "想买", event_id="same-2")],
            as_of="2026-09-22T12:00:40+00:00",
        )[0]
        repeated_window = LIVE.replay_signals(
            [event("u1", "想买", event_id="same-1"), event("u2", "想买", event_id="same-2")],
            as_of="2026-09-22T12:00:50+00:00", previous=[first_window],
        )[0]
        self.assertEqual(repeated_window["strength"], "strong")
        strong = LIVE.replay_signals(
            [event("u1", "想买", event_id="e1"), event("u2", "想买", event_id="e2")],
            as_of=AS_OF,
        )[0]
        one_user = [event("u1", "想买", event_id="e3")]
        held = LIVE.replay_signals(one_user, as_of="2026-09-22T12:01:10+00:00", previous=[strong])
        self.assertEqual(held[0]["status"], "active")
        self.assertNotEqual(held[0]["strength"], "strong")
        cleared = LIVE.replay_signals([], as_of="2026-09-22T12:03:00+00:00", previous=held)
        self.assertEqual(cleared[0]["status"], "cleared")


class R202PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.store = LIVE.LiveEventStore(":memory:")
        self.session = self.store.start_session("douyin", "room-1", "fixture", "https://example.test/room")
        self.events = [event("u1", "想买", event_id="e1"), event("u2", "想买", event_id="e2")]
        for item in self.events:
            self.assertTrue(self.store.insert_event(self.session, item))
        self.db_events = self.store.recent_events(self.session, None)

    def tearDown(self):
        self.store.close()

    def test_persist_is_idempotent_and_evidence_maps_to_public_event_contract(self):
        signal = LIVE.replay_signals(self.db_events, as_of=AS_OF, provider="douyin", room_id="room-1", session_id=self.session, run_id="run-1")[0]
        LIVE.persist_signal(self.store.connection, signal, "live_events")
        LIVE.persist_signal(self.store.connection, signal, "live_events")
        stored = LIVE.load_signals(self.store.connection, self.session, "douyin", "room-1", "run-1", "live_events")
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(stored[0]["evidence"]), 2)
        self.assertEqual(stored[0]["evidence"][0]["event_id"], "e1")
        self.assertIn("event_time", stored[0]["evidence"][0])
        self.assertNotIn("metadata", stored[0]["evidence"][0])

    def test_feedback_accepts_multiple_feedback_records_and_rejects_unknown_signal(self):
        signal = LIVE.replay_signals(self.db_events, as_of=AS_OF, provider="douyin", room_id="room-1", session_id=self.session, run_id="run-1")[0]
        LIVE.persist_signal(self.store.connection, signal, "live_events")
        useful = LIVE.add_signal_feedback(self.store.connection, signal["signal_id"], "useful", created_at=AS_OF)
        note = LIVE.add_signal_feedback(self.store.connection, signal["signal_id"], "note", "需要继续观察", created_at=AS_OF)
        self.assertEqual(useful["feedback_type"], "useful")
        self.assertEqual(note["feedback_type"], "note")
        with self.assertRaises(LIVE.SignalFeedbackError):
            LIVE.add_signal_feedback(self.store.connection, "missing", "useful", created_at=AS_OF)

    def test_evidence_cannot_cross_session(self):
        other = self.store.start_session("douyin", "room-1", "other", "https://example.test/other")
        signal = LIVE.replay_signals(self.db_events, as_of=AS_OF, provider="douyin", room_id="room-1", session_id=other, run_id="run-2")[0]
        with self.assertRaises(LIVE.SignalPersistenceError):
            LIVE.persist_signal(self.store.connection, signal, "live_events")


if __name__ == "__main__":
    unittest.main()
