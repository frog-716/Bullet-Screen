#!/usr/bin/env python3
"""Contract tests for diagnostics emitted by the formal Douyin adapter path."""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "douyin"))

import test_r2_03b_2b_fix as fixtures  # noqa: E402
import douyin_adapter as adapter  # noqa: E402


def run_with_diagnostics(
    response_payload=None,
    response_url=None,
    response_payloads=None,
    response_status=200,
    content_type="application/protobuffer",
    content_encoding="",
    body_override=None,
    body_error=False,
):
    stop_event = threading.Event()
    if response_payloads is None:
        page = fixtures.FakePage(stop_event, response_payload)
    else:
        class SequencePage(fixtures.FakePage):
            def goto(self, url, **kwargs):
                self.url = url
                if url.endswith("123456"):
                    for payload in response_payloads:
                        for callback in self._handlers.get("response", []):
                            callback(fixtures.FakeResponse(payload))
                    threading.Timer(0.05, self._stop_event.set).start()
                return None

        page = SequencePage(stop_event, response_payloads[0] if response_payloads else None)
    fake_playwright = fixtures.FakePlaywright(fixtures.FakeContext(page))
    emitted = []
    states = []
    diagnostics = []
    instance = adapter.DouyinPublicAdapter("https://live.douyin.com/123456", mode="playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.TimeoutError = TimeoutError
    sync_api.sync_playwright = None
    playwright_package = types.ModuleType("playwright")
    playwright_package.sync_api = sync_api

    original_response = fixtures.FakeResponse
    response_patch = patch.object(fixtures, "FakeResponse") if any(
        (response_url, response_status != 200, content_type != "application/protobuffer", content_encoding, body_override is not None, body_error)
    ) else nullcontext()
    with response_patch as response_type, patch.dict(
        os.environ, {"DOUYIN_WARMUP_SECONDS": "0", "DOUYIN_PROFILE_DIR": "/tmp/fake-douyin-profile"}
    ), patch.dict(
        sys.modules, {"playwright": playwright_package, "playwright.sync_api": sync_api}
    ), patch.object(sync_api, "sync_playwright", return_value=fake_playwright):
        if hasattr(response_type, "side_effect"):
            class UrlResponse(original_response):
                def __init__(self, payload):
                    super().__init__(payload)
                    if response_url:
                        self.url = response_url
                    self.status = response_status
                    self.headers = {
                        "content-type": content_type,
                        "content-encoding": content_encoding,
                    }

                def body(self):
                    if body_error:
                        raise RuntimeError("fixture body read failure")
                    return body_override if body_override is not None else super().body()

            response_type.side_effect = UrlResponse
        instance.run(
            stop_event,
            emitted.append,
            lambda name, title: states.append(name),
            None,
            None,
            None,
            diagnostics.append,
        )
    return emitted, states, diagnostics


class ProtocolDiagnosticsTests(unittest.TestCase):
    def test_valid_event_response_counts_messages_and_events(self):
        _, _, records = run_with_diagnostics(
            fixtures.response([fixtures.message("WebcastChatMessage", fixtures.comment_payload(), 1)])
        )
        self.assertEqual(len(records), 1)
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_total"], 1)
        self.assertEqual(stats["protocol_responses_valid"], 1)
        self.assertEqual(stats["protocol_responses_with_events"], 1)
        self.assertEqual(stats["protocol_messages_total"], 1)
        self.assertEqual(stats["protocol_events_emitted"], 1)
        self.assertTrue(stats["last_protocol_response_at"])
        self.assertTrue(stats["last_event_response_at"])

    def test_stage_metadata_identifies_content_type_before_decode(self):
        _, _, records = run_with_diagnostics(
            fixtures.response([]), content_type="application/octet-stream"
        )
        stats = records[-1]
        self.assertEqual(stats["protocol_http_status_counts"], {"200": 1})
        self.assertEqual(stats["protocol_content_type_counts"], {"application/octet-stream": 1})
        self.assertEqual(stats["protocol_content_encoding_counts"], {"none": 1})
        self.assertEqual(stats["protocol_malformed_stages"]["content_type"], 1)
        self.assertEqual(stats["protocol_decode_success"], 0)
        self.assertEqual(stats["protocol_decode_failure"], 0)

    def test_stage_metadata_identifies_empty_body(self):
        _, _, records = run_with_diagnostics(fixtures.response([]), body_override=b"")
        stats = records[-1]
        self.assertEqual(stats["protocol_malformed_stages"]["body_empty"], 1)
        self.assertEqual(stats["protocol_decode_success"], 0)
        self.assertEqual(stats["protocol_decode_failure"], 0)

    def test_valid_decode_has_no_malformed_stage(self):
        _, _, records = run_with_diagnostics(fixtures.response([]))
        stats = records[-1]
        self.assertEqual(stats["protocol_decode_success"], 1)
        self.assertEqual(stats["protocol_decode_failure"], 0)
        self.assertTrue(all(value == 0 for value in stats["protocol_malformed_stages"].values()))

    def test_empty_response_is_valid_and_counted_without_event(self):
        _, _, records = run_with_diagnostics(fixtures.response([]))
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_total"], 1)
        self.assertEqual(stats["protocol_responses_valid"], 1)
        self.assertEqual(stats["protocol_responses_empty"], 1)
        self.assertEqual(stats["protocol_responses_with_events"], 0)
        self.assertEqual(stats["protocol_messages_total"], 0)
        self.assertEqual(stats["protocol_events_emitted"], 0)
        self.assertTrue(stats["last_protocol_response_at"])
        self.assertTrue(stats["last_empty_envelope_at"])
        self.assertFalse(stats["last_event_response_at"])

    def test_consecutive_empty_responses_accumulate_without_events(self):
        _, _, records = run_with_diagnostics(response_payloads=[fixtures.response([]), fixtures.response([]), fixtures.response([])])
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_total"], 3)
        self.assertEqual(stats["protocol_responses_valid"], 3)
        self.assertEqual(stats["protocol_responses_empty"], 3)
        self.assertEqual(stats["protocol_messages_total"], 0)
        self.assertEqual(stats["protocol_events_emitted"], 0)

    def test_unknown_only_response_is_valid_but_not_eventful(self):
        _, _, records = run_with_diagnostics(
            fixtures.response([fixtures.message("UnknownMessage", fixtures.field_varint(1, 1), 1)])
        )
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_valid"], 1)
        self.assertEqual(stats["protocol_responses_unknown_only"], 1)
        self.assertEqual(stats["protocol_responses_with_events"], 0)
        self.assertEqual(stats["protocol_messages_total"], 1)
        self.assertEqual(stats["protocol_events_emitted"], 0)

    def test_malformed_response_is_counted_without_protocol_activity(self):
        _, _, records = run_with_diagnostics(b"not-a-protobuf")
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_total"], 1)
        self.assertEqual(stats["protocol_responses_valid"], 0)
        self.assertEqual(stats["protocol_responses_malformed"], 1)
        self.assertEqual(stats["protocol_decode_failure"], 1)
        self.assertEqual(stats["protocol_malformed_stages"]["envelope_decode"], 1)
        self.assertFalse(stats["last_protocol_response_at"])

    def test_stage_metadata_identifies_http_status(self):
        _, _, records = run_with_diagnostics(fixtures.response([]), response_status=503)
        stats = records[-1]
        self.assertEqual(stats["protocol_http_status_counts"], {"503": 1})
        self.assertEqual(stats["protocol_malformed_stages"]["http_status"], 1)

    def test_stage_metadata_identifies_body_read_failure(self):
        _, _, records = run_with_diagnostics(fixtures.response([]), body_error=True)
        stats = records[-1]
        self.assertEqual(stats["protocol_malformed_stages"]["body_read"], 1)
        self.assertEqual(stats["protocol_decode_failure"], 0)

    def test_source_rejected_response_is_counted_without_protocol_activity(self):
        _, _, records = run_with_diagnostics(
            fixtures.response([]), response_url="https://example.invalid/webcast/im/fetch/"
        )
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_total"], 1)
        self.assertEqual(stats["protocol_responses_source_rejected"], 1)
        self.assertEqual(stats["protocol_responses_valid"], 0)
        self.assertFalse(stats["last_protocol_response_at"])

    def test_mixed_known_and_unknown_response_is_eventful_once(self):
        payload = fixtures.response([
            fixtures.message("UnknownMessage", fixtures.field_varint(1, 1), 1),
            fixtures.message("WebcastChatMessage", fixtures.comment_payload(), 2),
        ])
        _, _, records = run_with_diagnostics(payload)
        stats = records[-1]
        self.assertEqual(stats["protocol_responses_valid"], 1)
        self.assertEqual(stats["protocol_responses_unknown_only"], 0)
        self.assertEqual(stats["protocol_responses_with_events"], 1)
        self.assertEqual(stats["protocol_messages_total"], 2)
        self.assertEqual(stats["protocol_events_emitted"], 1)

    def test_diagnostics_are_safe_statistics_only(self):
        _, _, records = run_with_diagnostics(fixtures.response([]))
        encoded = json.dumps(records[-1], ensure_ascii=False)
        self.assertNotIn("fixture comment", encoded)
        self.assertNotIn("cookie", encoded.lower())
        self.assertNotIn("token", encoded.lower())

    def test_empty_diagnostics_do_not_change_freshness_callback_behavior(self):
        _, _, records = run_with_diagnostics(fixtures.response([]))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[-1]["protocol_responses_empty"], 1)

    def test_status_and_snapshot_expose_only_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            collector = adapter.DouyinCollector(Path(temporary) / "douyin.sqlite3", mode="demo")
            collector.start("123456", mode="demo")
            time.sleep(0.05)
            status = collector.status()
            snapshot = collector.snapshot(window="60s", limit=5)
            for payload in (status["diagnostics"], snapshot["diagnostics"]):
                self.assertIn("protocol_responses_total", payload)
                self.assertIn("last_protocol_response_at", payload)
                self.assertNotIn("cookie", payload)
                self.assertNotIn("raw_payload", payload)
            self.assertTrue(collector.stop(timeout=2))
            collector.store.close()

    def test_collector_restart_resets_protocol_diagnostics_for_new_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            collector = adapter.DouyinCollector(Path(temporary) / "douyin.sqlite3", mode="demo")
            first = collector.start("123456", mode="demo")
            time.sleep(0.05)
            self.assertEqual(collector.status()["generation"], first.generation)
            self.assertTrue(collector.stop(timeout=2))
            second = collector.start("654321", mode="demo")
            current = collector.status()
            self.assertEqual(current["generation"], second.generation)
            self.assertEqual(current["room_id"], "654321")
            self.assertEqual(current["diagnostics"]["protocol_responses_total"], 0)
            self.assertEqual(current["diagnostics"]["protocol_events_emitted"], 0)
            self.assertTrue(collector.stop(timeout=2))
            collector.store.close()


if __name__ == "__main__":
    unittest.main()
