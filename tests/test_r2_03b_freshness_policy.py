import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
sys.path.insert(0, str(REPO_ROOT / "douyin"))
sys.path.insert(0, str(TESTS_ROOT))

import douyin_adapter as adapter  # noqa: E402
from test_r2_03b_2b_fix import (  # noqa: E402
    FakeContext,
    FakePage,
    FakePlaywright,
    entry_payload,
    field_varint,
    message,
    response,
    comment_payload,
    viewer_payload,
)


def run_fake_adapter(response_payload=None, websocket_frame=None):
    stop_event = threading.Event()
    page = FakePage(stop_event, response_payload, websocket_frame)
    context = FakeContext(page)
    fake_playwright = FakePlaywright(context)
    emitted = []
    activities = []
    protocols = []
    states = []
    instance = adapter.DouyinPublicAdapter("https://live.douyin.com/123456", mode="playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.TimeoutError = TimeoutError
    sync_api.sync_playwright = None
    playwright_package = types.ModuleType("playwright")
    playwright_package.sync_api = sync_api

    with patch.dict(
        os.environ,
        {"DOUYIN_WARMUP_SECONDS": "0", "DOUYIN_PROFILE_DIR": "/tmp/fake-douyin-profile"},
    ), patch.dict(
        sys.modules,
        {"playwright": playwright_package, "playwright.sync_api": sync_api},
    ), patch.object(sync_api, "sync_playwright", return_value=fake_playwright):
        instance.run(
            stop_event,
            emitted.append,
            lambda name, title: states.append(name),
            lambda: activities.append(True),
            None,
            lambda: protocols.append(True),
        )
    return emitted, activities, protocols, states


class AdapterProtocolActivityTests(unittest.TestCase):
    def test_empty_valid_envelope_updates_protocol_only(self):
        emitted, activities, protocols, _ = run_fake_adapter(response([]))
        self.assertEqual(len(protocols), 1)
        self.assertEqual(activities, [])
        self.assertEqual([event["type"] for event in emitted], ["live_status", "live_status"])

    def test_valid_event_updates_protocol_and_event_activity_once(self):
        emitted, activities, protocols, _ = run_fake_adapter(
            response([message("WebcastChatMessage", comment_payload(), 1)])
        )
        self.assertEqual(len(protocols), 1)
        self.assertEqual(len(activities), 1)
        self.assertIn("comment", [event["type"] for event in emitted])

    def test_valid_non_comment_event_is_valid_capture_activity(self):
        for payload, method, message_id, event_type in (
            (viewer_payload(), "WebcastRoomStatsMessage", 2, "viewer_change"),
            (entry_payload(), "WebcastMemberMessage", 3, "entry"),
        ):
            with self.subTest(event_type=event_type):
                emitted, activities, protocols, _ = run_fake_adapter(
                    response([message(method, payload, message_id)])
                )
                self.assertEqual(len(protocols), 1)
                self.assertEqual(len(activities), 1)
                self.assertIn(event_type, [event["type"] for event in emitted])

    def test_malformed_envelope_updates_neither_activity(self):
        _, activities, protocols, _ = run_fake_adapter(b"not-a-protobuf")
        self.assertEqual(protocols, [])
        self.assertEqual(activities, [])

    def test_unknown_only_message_updates_protocol_but_not_event_activity(self):
        _, activities, protocols, _ = run_fake_adapter(
            response([message("UnknownMessage", field_varint(1, 1), 1)])
        )
        self.assertEqual(len(protocols), 1)
        self.assertEqual(activities, [])

    def test_unknown_websocket_frame_updates_neither_activity(self):
        _, activities, protocols, _ = run_fake_adapter(websocket_frame=b"unknown-websocket-frame")
        self.assertEqual(protocols, [])
        self.assertEqual(activities, [])

    def test_page_ready_alone_updates_neither_activity(self):
        _, activities, protocols, states = run_fake_adapter()
        self.assertEqual(states, ["connected"])
        self.assertEqual(protocols, [])
        self.assertEqual(activities, [])


class CollectorFreshnessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.collector = adapter.DouyinCollector(Path(self.temporary.name) / "douyin.sqlite3", mode="playwright")
        self.context = adapter.RunContext(
            1,
            "run-a",
            "douyin",
            "123456",
            "https://live.douyin.com/123456",
            "",
            "playwright",
        )
        with self.collector.lock:
            self.collector.generation = self.context.generation
            self.collector._context = self.context
            self.collector.room_id = self.context.room_id
            self.collector.room_url = self.context.room_url
            self.collector.status_name = "connected"
            self.collector.session_id = self.collector.store.start_session(
                "douyin", self.context.room_id, "fixture room", self.context.room_url
            )
            self.collector._connected_monotonic = time.monotonic()

    def tearDown(self):
        self.collector.store.close()
        self.temporary.cleanup()

    def test_page_ready_without_protocol_is_not_reported_healthy(self):
        current = self.collector.status()
        self.assertEqual(current["protocol_available"], "unknown")
        self.assertEqual(current["protocol_health"], "unknown")
        self.assertEqual(current["activity_state"], "unknown")

    def test_non_protocol_response_is_explicitly_unavailable(self):
        with self.collector.lock:
            self.collector.diagnostics.update({
                "protocol_responses_total": 1,
                "protocol_responses_valid": 0,
            })
        current = self.collector.status()
        self.assertEqual(current["protocol_available"], "false")
        self.assertEqual(current["protocol_health"], "unavailable")
        self.assertEqual(current["activity_state"], "unknown")

    def test_protocol_health_survives_event_quiet_without_gap(self):
        self.assertTrue(self.collector._mark_protocol(self.context))
        self.collector.store.insert_snapshot(self.collector.session_id, {"online": None})
        with self.collector.lock:
            self.collector._last_valid_monotonic = time.monotonic() - adapter.EVENT_QUIET_AFTER_SECONDS - 1
        current = self.collector.status()
        self.assertEqual(current["status"], "connected")
        self.assertEqual(current["activity_state"], "quiet")
        self.assertEqual(current["protocol_health"], "healthy")
        self.assertFalse(any(gap["status"] == "open" for gap in self.collector.store.gaps(self.collector.session_id)))
        self.assertEqual(self.collector.snapshot(window="60s")["coverage"]["coverage_state"], "reliable_no_events")

    def test_protocol_timeout_opens_gap_without_event_timeout(self):
        self.assertTrue(self.collector._mark_protocol(self.context))
        with self.collector.lock:
            self.collector._last_protocol_monotonic = time.monotonic() - adapter.PROTOCOL_STALE_AFTER_SECONDS - 1
            self.collector._last_valid_monotonic = time.monotonic() - adapter.EVENT_QUIET_AFTER_SECONDS - 1
        current = self.collector.status()
        self.assertEqual(current["status"], "stale")
        self.assertEqual(current["activity_state"], "stale")
        self.assertEqual(current["protocol_health"], "stale")
        self.assertTrue(any(gap["status"] == "open" and gap["reason"] == "stale" for gap in self.collector.store.gaps(self.collector.session_id)))

    def test_valid_event_refreshes_both_protocol_and_event_activity(self):
        self.assertTrue(self.collector._mark_valid(self.context))
        current = self.collector.status()
        self.assertEqual(current["activity_state"], "active")
        self.assertEqual(current["protocol_health"], "healthy")
        self.assertTrue(current["last_protocol_at"])
        self.assertTrue(current["last_valid_at"])

    def test_protocol_recovery_closes_stale_gap_without_fabricating_event(self):
        self.assertTrue(self.collector._mark_protocol(self.context))
        with self.collector.lock:
            self.collector._last_protocol_monotonic = time.monotonic() - adapter.PROTOCOL_STALE_AFTER_SECONDS - 1
        self.assertEqual(self.collector.status()["status"], "stale")
        self.assertTrue(self.collector._mark_protocol(self.context))
        current = self.collector.status()
        self.assertEqual(current["status"], "connected")
        self.assertEqual(current["activity_state"], "quiet")
        self.assertEqual(current["last_valid_at"], "")
        self.assertFalse(any(gap["status"] == "open" for gap in self.collector.store.gaps(self.collector.session_id)))

    def test_snapshot_exposes_both_freshness_clocks(self):
        self.assertTrue(self.collector._mark_protocol(self.context))
        snapshot = self.collector.snapshot(window="60s", limit=10)
        self.assertIn("last_protocol_at", snapshot)
        self.assertIn("last_valid_at", snapshot)
        self.assertEqual(snapshot["protocol_available"], "true")
        self.assertEqual(snapshot["activity_state"], "quiet")
        self.assertEqual(snapshot["protocol_health"], "healthy")
        self.assertEqual(snapshot["freshness"]["activity_state"], "quiet")
        self.assertEqual(snapshot["freshness"]["protocol_health"], "healthy")

    def test_old_context_cannot_refresh_new_run_protocol_health(self):
        self.assertTrue(self.collector._mark_protocol(self.context))
        newer = adapter.RunContext(
            2, "run-b", "douyin", "654321", "https://live.douyin.com/654321", "", "playwright"
        )
        with self.collector.lock:
            self.collector._context = newer
            self.collector.generation = newer.generation
            self.collector.room_id = newer.room_id
            self.collector.room_url = newer.room_url
            self.collector.last_protocol_at = ""
            self.collector._last_protocol_monotonic = 0.0
        self.assertFalse(self.collector._mark_protocol(self.context))
        self.assertEqual(self.collector.status()["last_protocol_at"], "")

    def test_stop_restart_keeps_freshness_identity_separate(self):
        self.assertTrue(self.collector._mark_protocol(self.context))
        newer = adapter.RunContext(
            2, "run-b", "douyin", "654321", "https://live.douyin.com/654321", "", "playwright"
        )
        with self.collector.lock:
            self.collector._context = newer
            self.collector.generation = newer.generation
            self.collector.room_id = newer.room_id
            self.collector.room_url = newer.room_url
            self.collector.session_id = self.collector.store.start_session(
                "douyin", newer.room_id, "new room", newer.room_url
            )
            self.collector.status_name = "connected"
            self.collector.last_protocol_at = ""
            self.collector.last_valid_at = ""
            self.collector._last_protocol_monotonic = 0.0
            self.collector._last_valid_monotonic = 0.0
        self.assertTrue(self.collector._mark_protocol(newer))
        current = self.collector.snapshot(window="60s", limit=10)
        self.assertEqual(current["generation"], 2)
        self.assertEqual(current["run_id"], "run-b")
        self.assertEqual(current["room_id"], "654321")


if __name__ == "__main__":
    unittest.main()
