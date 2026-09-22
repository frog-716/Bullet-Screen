import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "douyin"))

import douyin_adapter as adapter  # noqa: E402


def varint(value):
    value = int(value)
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def field_varint(number, value):
    return varint(number << 3) + varint(value)


def field_bytes(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def user_fields(user_id=7):
    return field_varint(1, user_id) + field_bytes(3, "fixture-user".encode())


def comment_payload():
    return (
        field_bytes(1, field_varint(4, int(time.time())))
        + field_bytes(2, user_fields())
        + field_bytes(3, "fixture comment".encode())
    )


def entry_payload():
    return field_bytes(2, user_fields()) + field_varint(3, 1)


def viewer_payload():
    return field_varint(5, 321)


def message(method, payload, message_id):
    envelope = (
        field_bytes(1, method.encode())
        + field_bytes(2, payload)
        + field_varint(3, message_id)
    )
    return field_bytes(1, envelope)


def response(messages):
    return b"".join(messages) + field_varint(4, int(time.time()))


class FakeRequest:
    resource_type = "xhr"


class FakeResponse:
    status = 200
    request = FakeRequest()
    headers = {"content-type": "application/protobuffer"}

    def __init__(self, payload):
        self._payload = payload
        self.url = "https://live.douyin.com/webcast/im/fetch/"

    def body(self):
        return self._payload


class FakeLocator:
    def inner_text(self, timeout=None):
        return "在线观众 更多直播"


class FakeWebSocket:
    def __init__(self):
        self.url = "wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/"
        self._frame_handler = None

    def on(self, event, callback):
        if event == "framereceived":
            self._frame_handler = callback

    def emit_frame(self, payload):
        if self._frame_handler:
            self._frame_handler(payload)


class FakePage:
    def __init__(self, stop_event, response_payload=None, websocket_frame=None):
        self.url = "about:blank"
        self._stop_event = stop_event
        self._response_payload = response_payload
        self._websocket_frame = websocket_frame
        self._handlers = {}

    def on(self, event, callback):
        self._handlers.setdefault(event, []).append(callback)

    def goto(self, url, **kwargs):
        self.url = url
        if url.endswith("123456"):
            if self._response_payload is not None:
                for callback in self._handlers.get("response", []):
                    callback(FakeResponse(self._response_payload))
            if self._websocket_frame is not None:
                websocket = FakeWebSocket()
                for callback in self._handlers.get("websocket", []):
                    callback(websocket)
                websocket.emit_frame(self._websocket_frame)
            threading.Timer(0.05, self._stop_event.set).start()
        return None

    def title(self):
        return "fixture room"

    def locator(self, selector):
        return FakeLocator()

    def evaluate(self, script):
        return {"online_text": "", "items": []}


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def close(self):
        return None


class FakeChromium:
    def __init__(self, context):
        self.context = context

    def launch_persistent_context(self, profile_dir, **kwargs):
        return self.context


class FakePlaywright:
    def __init__(self, context):
        self.chromium = FakeChromium(context)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None


def run_fake_adapter(response_payload=None, websocket_frame=None):
    stop_event = threading.Event()
    page = FakePage(stop_event, response_payload, websocket_frame)
    context = FakeContext(page)
    fake_playwright = FakePlaywright(context)
    emitted = []
    activities = []
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
        instance.run(stop_event, emitted.append, lambda name, title: states.append(name), lambda: activities.append(True))
    return emitted, activities, states


class DouyinFreshnessFixTests(unittest.TestCase):
    def test_empty_valid_envelope_does_not_refresh(self):
        emitted, activities, states = run_fake_adapter(response([]))
        self.assertEqual(activities, [])
        self.assertEqual(states, ["connected"])
        self.assertEqual([event["type"] for event in emitted], ["live_status", "live_status"])

    def test_malformed_envelope_does_not_refresh(self):
        emitted, activities, states = run_fake_adapter(b"not-a-protobuf")
        self.assertEqual(activities, [])
        self.assertEqual(states, ["connected"])
        self.assertEqual([event["type"] for event in emitted], ["live_status"])

    def test_unknown_only_messages_do_not_refresh(self):
        payload = response([message("UnknownMessage", field_varint(1, 1), 1)])
        emitted, activities, states = run_fake_adapter(payload)
        self.assertEqual(activities, [])
        self.assertEqual(states, ["connected"])
        self.assertEqual([event["type"] for event in emitted], ["live_status", "live_status"])

    def test_valid_comment_refreshes_once(self):
        emitted, activities, _ = run_fake_adapter(response([message("WebcastChatMessage", comment_payload(), 1)]))
        self.assertEqual(len(activities), 1)
        self.assertEqual([event["type"] for event in emitted], ["comment", "live_status", "live_status"])

    def test_valid_viewer_change_refreshes_once(self):
        emitted, activities, _ = run_fake_adapter(response([message("WebcastRoomStatsMessage", viewer_payload(), 2)]))
        self.assertEqual(len(activities), 1)
        self.assertIn("viewer_change", [event["type"] for event in emitted])

    def test_valid_entry_refreshes_once(self):
        emitted, activities, _ = run_fake_adapter(response([message("WebcastMemberMessage", entry_payload(), 3)]))
        self.assertEqual(len(activities), 1)
        self.assertIn("entry", [event["type"] for event in emitted])

    def test_mixed_known_and_unknown_messages_refresh_once(self):
        payload = response([
            message("UnknownMessage", field_varint(1, 1), 4),
            message("WebcastChatMessage", comment_payload(), 5),
        ])
        emitted, activities, _ = run_fake_adapter(payload)
        self.assertEqual(len(activities), 1)
        self.assertIn("comment", [event["type"] for event in emitted])

    def test_unknown_websocket_frame_does_not_refresh(self):
        emitted, activities, _ = run_fake_adapter(websocket_frame=b"unknown-websocket-frame")
        self.assertEqual(activities, [])
        self.assertEqual([event["type"] for event in emitted], ["live_status"])

    def test_page_ready_alone_does_not_refresh(self):
        emitted, activities, states = run_fake_adapter()
        self.assertEqual(activities, [])
        self.assertEqual(states, ["connected"])
        self.assertEqual([event["type"] for event in emitted], ["live_status"])


if __name__ == "__main__":
    unittest.main()
