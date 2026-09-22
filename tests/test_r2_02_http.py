import http.client
import importlib.util
import json
import sys
import threading
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


BILI = load_module("bilibili_r2_02_http", ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_r2_02_http", ROOT / "douyin" / "server.py")
LIVE = load_module("live_r2_02_http", ROOT / "douyin" / "live_intelligence.py")


class FeedbackHttpTests(unittest.TestCase):
    def _exercise(self, module, store, session_id, provider, room_id, event_table, handler):
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if event_table == "events":
            for uid in (1, 2):
                store.insert_event(session_id, {"type": "danmaku", "event_time": now, "uid": uid, "uname": f"u{uid}", "text": "想买"})
            rows = store.recent_events(session_id, None)
            normalized = [{
                "event_id": row["event_id"], "_db_event_id": row["_db_event_id"], "type": "comment",
                "timestamp_utc": row["event_time"], "user_id": row["uid"], "user_name": row["uname"],
                "content": row["text"], "analysis": LIVE.analyze_text(row["text"]),
            } for row in rows]
        else:
            for uid in ("u1", "u2"):
                store.insert_event(session_id, LIVE.normalize_event(room_id, "comment", uid, uid, "想买", timestamp=now, metadata={"event_id": uid}))
            normalized = store.recent_events(session_id, None)
        signal = LIVE.replay_signals(normalized, as_of=now, provider=provider, room_id=room_id, session_id=session_id, run_id="run-http")[0]
        LIVE.persist_signal(store.connection, signal, event_table)
        module.AppHandler.collector = type("CollectorStub", (), {"store": store})()
        module.AppHandler.capability_token = "test-token"
        server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.AppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            unauthorized = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            unauthorized.request("POST", "/api/signals/feedback", body=b"{}", headers={
                "Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "Content-Length": "2",
            })
            self.assertEqual(unauthorized.getresponse().status, 401)
            unauthorized.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            body = json.dumps({"signal_id": signal["signal_id"], "feedback_type": "useful"}).encode()
            connection.request("POST", "/api/signals/feedback", body=body, headers={
                "Host": f"127.0.0.1:{port}", "X-Bullet-Screen-Token": "test-token",
                "Content-Type": "application/json", "Content-Length": str(len(body)),
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            payload = json.loads(response.read())
            self.assertEqual(payload["feedback"]["feedback_type"], "useful")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_bilibili_feedback_api_is_protected_and_persists(self):
        store = BILI.EventStore(":memory:")
        session = store.start_session("room", "fixture")
        self._exercise(BILI, store, session, "bilibili", "room", "events", BILI.AppHandler)
        store.close()

    def test_douyin_provider_bilibili_feedback_api_is_protected_and_persists(self):
        store = DOUYIN_SERVER.EventStore(":memory:")
        session = store.start_session("room", "fixture")
        self._exercise(DOUYIN_SERVER, store, session, "bilibili", "room", "events", DOUYIN_SERVER.AppHandler)
        store.close()


if __name__ == "__main__":
    unittest.main()
