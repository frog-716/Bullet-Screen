import base64
import gzip
import hashlib
import importlib.util
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
DOUYIN_ROOT = REPO_ROOT / "douyin"
sys.path.insert(0, str(DOUYIN_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BILIBILI = load_module("bilibili_server_b03", REPO_ROOT / "bilibili" / "server.py")
DOUYIN_SERVER = load_module("douyin_server_b03", DOUYIN_ROOT / "server.py")
DOUYIN_ADAPTER = load_module("douyin_adapter_b03", DOUYIN_ROOT / "douyin_adapter.py")


class ChunkSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []

    def recv(self, _size):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= _size:
            return chunk
        self.chunks.insert(0, chunk[_size:])
        return chunk[:_size]

    def sendall(self, value):
        self.sent.append(value)

    def close(self):
        return None

    def settimeout(self, _value):
        return None


class ProtocolTests(unittest.TestCase):
    def test_bilibili_auth_requires_explicit_success_object(self):
        for module in (BILIBILI, DOUYIN_SERVER):
            with tempfile.TemporaryDirectory() as temporary:
                collector = module.Collector(module.EventStore(Path(temporary) / "events.sqlite3"))
                collector.session_id = 1
                for body in (b"", b"not-json", b"{}", b"[]", b'{"message":"ok"}', b'{"code":-1}'):
                    with self.subTest(module=module.__name__, body=body):
                        collector.status_name = "authenticating"
                        with self.assertRaises(module.ProtocolError):
                            collector._handle_packet(module.PacketCodec.pack(body, 8, 1))
                        self.assertNotEqual(collector.status_name, "connected")
                collector.status_name = "authenticating"
                collector._handle_packet(module.PacketCodec.pack(b'{"code":0}', 8, 1))
                self.assertEqual(collector.status_name, "connected")
                collector.store.close()

    def test_websocket_reassembles_fragments_and_rejects_oversized_frame(self):
        for module in (BILIBILI, DOUYIN_SERVER):
            with self.subTest(module=module.__name__):
                client = module.WebSocketClient("example.invalid", 443, "/sub", "")
                first = bytes([0x01, 3]) + b"hel"
                second = bytes([0x80, 2]) + b"lo"
                client.sock = ChunkSocket([first, second])
                self.assertIsNone(client.receive())
                self.assertEqual(client.receive(), (0x1, b"hello"))

                client.sock = ChunkSocket([b"HTTP/1.1 101 Switching Protocols\r\n\r\n" + bytes([0x82, 1]) + b"x"])
                client._read_until(b"\r\n\r\n", module.MAX_WS_HANDSHAKE_BYTES)
                self.assertEqual(client.receive(), (0x2, b"x"))

                client.sock = ChunkSocket([bytes([0x01, 1]) + b"a", bytes([0x82, 1]) + b"b"])
                self.assertIsNone(client.receive())
                with self.assertRaises(module.ProtocolError):
                    client.receive()

                client.sock = ChunkSocket([bytes([0x82, 0x7F]) + struct.pack(">Q", module.MAX_WS_FRAME_BYTES + 1)])
                with self.assertRaises(module.ProtocolError):
                    client.receive()

    def test_websocket_handshake_requires_matching_accept(self):
        for module in (BILIBILI, DOUYIN_SERVER):
            with self.subTest(module=module.__name__):
                raw = ChunkSocket([
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: invalid\r\n\r\n"
                ])
                tls = mock.Mock()
                tls.wrap_socket.return_value = raw
                with mock.patch.object(module.socket, "create_connection", return_value=raw), mock.patch.object(module.ssl, "create_default_context", return_value=tls):
                    with self.assertRaises(module.ProtocolError):
                        module.WebSocketClient("example.invalid", 443, "/sub", "").connect()

    def test_packet_decoder_has_total_size_budget(self):
        for module in (BILIBILI, DOUYIN_SERVER):
            with self.subTest(module=module.__name__):
                payload = module.PacketCodec.pack(b"x" * (module.MAX_PACKET_BODY_BYTES + 1), 5, 1)
                with self.assertRaises(module.ProtocolError):
                    list(module.PacketCodec.decode(payload))

    def test_douyin_parse_budget_discards_large_expansion(self):
        adapter = DOUYIN_ADAPTER.DouyinPublicAdapter("1001", mode="demo")
        raw = b'{"type":"comment","content":"' + b"x" * (DOUYIN_ADAPTER.MAX_DECODED_PAYLOAD_BYTES + 1) + b'"}'
        payload = gzip.compress(raw)
        self.assertEqual(list(adapter._parse_payload(payload, "websocket")), [])
        self.assertGreater(adapter.parse_budget_drops, 0)

    def test_douyin_unknown_nested_json_does_not_become_event(self):
        payload = {"data": {"user": {"type": "comment", "content": "not a protocol event"}}}
        adapter = DOUYIN_ADAPTER.DouyinPublicAdapter("1001", mode="demo")
        self.assertEqual(list(adapter._parse_payload(json.dumps(payload), "response")), [])

    def test_douyin_dedup_cache_is_bounded(self):
        adapter = DOUYIN_ADAPTER.DouyinPublicAdapter("1001", mode="demo")
        for index in range(DOUYIN_ADAPTER.MAX_SEEN_CACHE * 3):
            adapter._remember_seen(adapter._seen_protocol, f"message-{index}")
        self.assertLessEqual(len(adapter._seen_protocol), DOUYIN_ADAPTER.MAX_SEEN_CACHE)

    def test_douyin_resource_validation_rejects_lookalike_hosts(self):
        self.assertTrue(DOUYIN_ADAPTER.is_douyin_resource_url("https://live.douyin.com/1001"))
        self.assertTrue(DOUYIN_ADAPTER.is_douyin_resource_url("wss://webcast5-ws-web-lf.douyin.com/webcast/im"))
        self.assertTrue(DOUYIN_ADAPTER.is_douyin_protocol_url("wss://webcast5-ws-web-lf.douyin.com/webcast/im"))
        self.assertFalse(DOUYIN_ADAPTER.is_douyin_protocol_url("https://live.douyin.com/not-a-protocol"))
        self.assertFalse(DOUYIN_ADAPTER.is_douyin_resource_url("https://live.douyin.com.evil.example/1001"))
        self.assertFalse(DOUYIN_ADAPTER.is_douyin_resource_url("http://live.douyin.com/1001"))


if __name__ == "__main__":
    unittest.main()
