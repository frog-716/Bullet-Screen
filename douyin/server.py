#!/usr/bin/env python3
"""Live Intelligence local collector service.

The browser is intentionally only a dashboard. This process owns credentials,
the Bilibili REST/WebSocket protocol, SQLite persistence, and the small local
HTTP API consumed by the dashboard.
"""

import argparse
import base64
import fcntl
import hashlib
import hmac
import http.cookiejar
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from douyin_adapter import BusyError, DouyinCollector, SnapshotUnstableError, _decode_im_event, _decode_im_response, parse_room_input, snapshot_window, snapshot_window_seconds
from live_intelligence import SignalEngine, SignalFeedbackError, _window_bounds, add_signal_feedback, analyze_text, evaluate_coverage_window, load_signals, normalize_event, persist_signal


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from schema_v4 import SCHEMA_VERSION as V4_SCHEMA_VERSION, V4_TABLES, create_signal_schema, verify_signal_schema
from scripts.data_lifecycle import (
    LifecycleError,
    acquire_profile_lock,
    assert_no_pending_lifecycle,
    release_lifecycle_lock,
    resolve_profile_path,
)

DEFAULT_DB = ROOT / "data" / "danmaku.sqlite3"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/onboarding.js": "onboarding.js", "/snapshot_client.js": "snapshot_client.js", "/signal_ui.js": "signal_ui.js", "/styles.css": "styles.css"}
PUBLIC_API_PATHS = {"/api/health", "/api/bootstrap"}
PROTECTED_API_PATHS = {"/api/status", "/api/metrics", "/api/events", "/api/snapshot", "/api/connect", "/api/disconnect", "/api/signals/feedback"}
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
FRESHNESS_TIMEOUT_SECONDS = 45.0
MAX_WS_FRAME_BYTES = 1 * 1024 * 1024
MAX_WS_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_WS_HANDSHAKE_BYTES = 16 * 1024
MAX_PACKET_BODY_BYTES = 4 * 1024 * 1024
MAX_PACKET_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PACKET_COUNT = 512
MAX_PACKET_DEPTH = 4
SCHEMA_VERSION = V4_SCHEMA_VERSION
CORE_SCHEMA_TABLES = {"sessions", "events", "metric_snapshots", "capture_gaps"}
LEGACY_SCHEMA_TABLES = {"live_sessions", "live_events", "live_metric_snapshots"}
SCHEMA_COLUMNS = {
    "sessions": {
        "id": ("INTEGER", 0, 1, None), "room_id": ("TEXT", 1, 0, None),
        "room_title": ("TEXT", 1, 0, None), "started_at": ("TEXT", 1, 0, None),
        "ended_at": ("TEXT", 0, 0, None), "status": ("TEXT", 1, 0, "'running'"),
    },
    "events": {
        "id": ("INTEGER", 0, 1, None), "session_id": ("INTEGER", 1, 0, None),
        "event_type": ("TEXT", 1, 0, None), "event_time": ("TEXT", 1, 0, None),
        "uid": ("INTEGER", 0, 0, None), "uname": ("TEXT", 0, 0, None),
        "text": ("TEXT", 0, 0, None), "gift_name": ("TEXT", 0, 0, None),
        "gift_num": ("INTEGER", 0, 0, None), "amount": ("INTEGER", 0, 0, None),
        "popularity": ("INTEGER", 0, 0, None),
    },
    "metric_snapshots": {
        "id": ("INTEGER", 0, 1, None), "session_id": ("INTEGER", 1, 0, None),
        "recorded_at": ("TEXT", 1, 0, None), "online": ("INTEGER", 0, 0, "0"),
        "likes": ("INTEGER", 0, 0, "0"), "danmaku_rate": ("REAL", 0, 0, "0"),
        "total_danmaku": ("INTEGER", 0, 0, "0"),
    },
    "capture_gaps": {
        "id": ("INTEGER", 0, 1, None), "provider": ("TEXT", 1, 0, None),
        "room_id": ("TEXT", 1, 0, None), "session_id": ("INTEGER", 0, 0, None),
        "run_id": ("TEXT", 0, 0, None), "gap_start": ("TEXT", 1, 0, None),
        "gap_end": ("TEXT", 0, 0, None), "reason": ("TEXT", 1, 0, None),
        "source": ("TEXT", 1, 0, "'collector'"), "status": ("TEXT", 1, 0, "'open'"),
    },
}
SCHEMA_INDEXES = {
    "idx_events_session_time": ("events", ("session_id", "event_time")),
    "idx_events_type": ("events", ("session_id", "event_type")),
    "idx_metrics_session_time": ("metric_snapshots", ("session_id", "recorded_at")),
    "idx_capture_gaps_session_time": ("capture_gaps", ("session_id", "gap_start", "gap_end")),
}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
REFERER = "https://live.bilibili.com/"
WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
INVALID_WBI_VALUE_CHARS = re.compile(r"[!'()*]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_utc_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def stale_gap_start(last_valid_at: str, freshness_seconds: float) -> str:
    parsed = parse_utc_timestamp(last_valid_at)
    if parsed is None:
        return utc_now()
    return (parsed + timedelta(seconds=freshness_seconds)).isoformat(timespec="milliseconds")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ProtocolError(RuntimeError):
    pass


class SchemaVersionError(RuntimeError):
    """The database is empty, unsupported, or requires an explicit migration."""


def _schema_default(value: Any) -> Optional[str]:
    return None if value is None else str(value).replace(" ", "").lower()


def verify_schema_signature(connection: sqlite3.Connection) -> None:
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_tables = CORE_SCHEMA_TABLES - tables
    unexpected_tables = tables - CORE_SCHEMA_TABLES - LEGACY_SCHEMA_TABLES - V4_TABLES
    if missing_tables or unexpected_tables:
        raise SchemaVersionError(
            f"Bilibili compatibility schema signature mismatch: missing={sorted(missing_tables)}, unexpected={sorted(unexpected_tables)}"
        )
    legacy_tables = tables & LEGACY_SCHEMA_TABLES
    if legacy_tables and legacy_tables != LEGACY_SCHEMA_TABLES:
        raise SchemaVersionError("Bilibili compatibility legacy schema is incomplete")
    for table, required in SCHEMA_COLUMNS.items():
        actual = {
            row[1]: (str(row[2]).upper(), int(row[3]), int(row[5]), _schema_default(row[4]))
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, expected in required.items():
            if name not in actual or actual[name] != expected:
                raise SchemaVersionError(f"Bilibili compatibility schema mismatch in {table}.{name}")
    for index_name, (table, expected_columns) in SCHEMA_INDEXES.items():
        index_row = next(
            (row for row in connection.execute(f"PRAGMA index_list({table})") if row[1] == index_name),
            None,
        )
        if not index_row:
            raise SchemaVersionError(f"Bilibili compatibility schema missing index {index_name}")
        actual_columns = tuple(
            row[2] for row in connection.execute(f"PRAGMA index_info({index_name})")
        )
        if actual_columns != expected_columns:
            raise SchemaVersionError(f"Bilibili compatibility schema mismatch in index {index_name}")
    expected_foreign_keys = {
        "sessions": set(),
        "events": {("sessions", "session_id", "id")},
        "metric_snapshots": {("sessions", "session_id", "id")},
        "capture_gaps": {("sessions", "session_id", "id")},
    }
    for table, expected in expected_foreign_keys.items():
        actual = {(row[2], row[3], row[4]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
        if actual != expected:
            raise SchemaVersionError(f"Bilibili compatibility schema mismatch in foreign keys for {table}")
    try:
        verify_signal_schema(connection, "sessions", "events")
    except Exception as error:
        raise SchemaVersionError(str(error)) from error


@dataclass(frozen=True)
class RunContext:
    generation: int
    run_id: str
    provider: str
    room_id: str
    sessdata: str = field(repr=False)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    capture_verified: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


def acquire_database_lock(path: Path) -> Any:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise ProtocolError("该数据文件已有服务在使用，请先关闭旧看板服务") from error
    return handle


class WbiSigner:
    @staticmethod
    def mixin_key(img_key: str, sub_key: str) -> str:
        raw = img_key + sub_key
        return "".join(raw[index] for index in WBI_TABLE if index < len(raw))[:32]

    @staticmethod
    def sign(params: Dict[str, Any], img_key: str, sub_key: str, now: Optional[int] = None) -> Dict[str, Any]:
        signed = {key: INVALID_WBI_VALUE_CHARS.sub("", str(value)) for key, value in params.items()}
        signed["wts"] = int(time.time() if now is None else now)
        query = urllib.parse.urlencode(sorted(signed.items()), quote_via=urllib.parse.quote_plus)
        signed["w_rid"] = hashlib.md5((query + WbiSigner.mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
        return signed


class HttpClient:
    def __init__(self, sessdata: str = "", buvid3: str = "", buvid4: str = "") -> None:
        self.cookie_input = sessdata.strip()
        self.cookie_values: Dict[str, str] = {}
        if "=" in self.cookie_input:
            for item in self.cookie_input.split(";"):
                name, separator, value = item.strip().partition("=")
                if separator and name:
                    self.cookie_values[name] = value
        self.sessdata = self.cookie_values.get("SESSDATA", "") or (self.cookie_input if "=" not in self.cookie_input else "")
        self.buvid3 = buvid3.strip()
        self.buvid4 = buvid4.strip()
        self.buvid3 = self.buvid3 or self.cookie_values.get("buvid3", "")
        self.buvid4 = self.buvid4 or self.cookie_values.get("buvid4", "")
        self.uid = 0
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.warmup_attempted = False
        self.api_attempts: List[Dict[str, Any]] = []

    def diagnostics(self) -> Dict[str, Any]:
        return {"buvid3_present": bool(self.buvid3), "buvid4_present": bool(self.buvid4), "sessdata_present": bool(self.sessdata), "uid": self.uid, "warmup_attempted": self.warmup_attempted, "cookie_names": sorted(set(self.cookie_values) | {cookie.name for cookie in self.cookie_jar}), "api_attempts": list(self.api_attempts)}

    @property
    def cookie(self) -> str:
        values = dict(self.cookie_values)
        for cookie in self.cookie_jar:
            values.setdefault(cookie.name, cookie.value)
        if self.buvid3:
            values["buvid3"] = self.buvid3
        if self.buvid4:
            values["buvid4"] = self.buvid4
        if self.sessdata:
            values["SESSDATA"] = self.sessdata
        return "; ".join(f"{name}={value}" for name, value in values.items())

    def warm_up(self) -> None:
        if self.warmup_attempted:
            return
        self.warmup_attempted = True
        request = urllib.request.Request("https://www.bilibili.com/", headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        try:
            with self.opener.open(request, timeout=15) as response:
                response.read(4096)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            # Homepage warm-up is best effort; the API calls below provide the
            # authoritative error and may still work with a supplied Cookie.
            return

    def sync_cookie_identifiers(self) -> None:
        for cookie in self.cookie_jar:
            if cookie.name == "buvid3" and not self.buvid3:
                self.buvid3 = cookie.value
            elif cookie.name == "buvid4" and not self.buvid4:
                self.buvid4 = cookie.value

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15) -> Dict[str, Any]:
        request_headers = {"User-Agent": USER_AGENT, "Referer": REFERER, "Accept": "application/json"}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(300).decode("utf-8", "replace")
            raise ProtocolError(f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProtocolError(f"network error: {error.reason}") from error
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError("Bilibili returned non-JSON response") from error
        if not isinstance(result, dict):
            raise ProtocolError("Bilibili returned an invalid JSON object")
        return result

    def ensure_buvid3(self) -> str:
        self.warm_up()
        self.sync_cookie_identifiers()
        if self.buvid3:
            return self.buvid3
        result = self.get_json("https://api.bilibili.com/x/frontend/finger/spi")
        data = result.get("data") or {}
        self.buvid3 = str(data.get("b_3") or "")
        self.buvid4 = str(data.get("b_4") or "")
        if not self.buvid3:
            raise ProtocolError("buvid3 was not returned by the device fingerprint endpoint")
        return self.buvid3

    def get_room(self, room_id: str) -> Dict[str, Any]:
        self.ensure_buvid3()
        query = urllib.parse.urlencode({"id": room_id})
        result = self.get_json(f"https://api.live.bilibili.com/room/v1/Room/room_init?{query}")
        if safe_int(result.get("code"), -1) != 0:
            raise ProtocolError(f"room lookup failed: {result.get('message') or result.get('code')}")
        data = result.get("data") or {}
        real_room_id = str(data.get("room_id") or room_id)
        base_title = str(data.get("title") or "")
        base_online = safe_int(data.get("online"))
        try:
            base_query = urllib.parse.urlencode({"req_biz": "web_room_componet", "room_ids": real_room_id})
            base_result = self.get_json(f"https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo?{base_query}")
            base_data = ((base_result.get("data") or {}).get("by_room_ids") or {}).get(real_room_id) or {}
            base_title = str(base_data.get("title") or base_title)
            base_online = safe_int(base_data.get("online"), base_online)
        except ProtocolError:
            pass
        return {
            "room_id": real_room_id,
            "short_id": str(data.get("short_id") or room_id),
            "title": base_title or f"B站直播间 {room_id}",
            "live_status": safe_int(data.get("live_status")),
            "online": base_online,
        }

    def get_wbi_keys(self) -> Tuple[str, str]:
        result = self.get_json("https://api.bilibili.com/x/web-interface/nav")
        data = result.get("data") or {}
        self.uid = safe_int(data.get("mid"))
        wbi_img = data.get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        if not img_key or not sub_key:
            raise ProtocolError("WBI keys were not returned by nav endpoint")
        return img_key, sub_key

    def get_danmaku_info(self, room_id: str) -> Dict[str, Any]:
        self.ensure_buvid3()
        endpoint = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
        headers = {"Referer": f"https://live.bilibili.com/{room_id}"}
        attempts: List[str] = []
        self.api_attempts = []

        # This is the long-lived public flow documented by the reference
        # implementation. WBI is kept as a compatibility fallback because
        # Bilibili has enabled it for some API edges and accounts.
        direct_query = urllib.parse.urlencode({"id": room_id, "type": 0})
        for label, query in (("direct", direct_query), ("wbi", None)):
            if query is None:
                img_key, sub_key = self.get_wbi_keys()
                query = urllib.parse.urlencode(WbiSigner.sign({"id": room_id, "type": 0, "web_location": "444.8"}, img_key, sub_key))
            result = self.get_json(f"{endpoint}?{query}", headers=headers)
            code = safe_int(result.get("code"), -1)
            self.api_attempts.append({"flow": label, "code": code, "message": str(result.get("message") or code)})
            if code == 0:
                data = result.get("data") or {}
                hosts = data.get("host_list") or []
                if not data.get("token") or not hosts:
                    raise ProtocolError("getDanmuInfo returned no token or host list")
                return {"token": str(data["token"]), "hosts": hosts}
            attempts.append(f"{label}:{result.get('message') or code}")
            if code != -352:
                raise ProtocolError(f"getDanmuInfo failed: {result.get('message') or code}")
        raise ProtocolError("getDanmuInfo rejected by Bilibili (-352) after direct and WBI attempts; add a current SESSDATA or use a permitted network出口 (" + ", ".join(attempts) + ")")


class PacketCodec:
    HEADER = 16

    @staticmethod
    def pack(body: bytes, operation: int, protover: int = 1, sequence: int = 1) -> bytes:
        total = PacketCodec.HEADER + len(body)
        return struct.pack(">IHHII", total, PacketCodec.HEADER, protover, operation, sequence) + body

    @staticmethod
    def preferred_protover() -> int:
        try:
            import brotli  # type: ignore
            return 3
        except ImportError:
            return 2

    @staticmethod
    def auth(room_id: str, token: str, uid: int = 0, buvid3: str = "", protover: Optional[int] = None) -> bytes:
        body = json_bytes({"uid": uid, "roomid": safe_int(room_id), "protover": protover or PacketCodec.preferred_protover(), "buvid": buvid3, "support_ack": True, "queue_uuid": os.urandom(4).hex(), "scene": "", "platform": "web", "type": 2, "key": token})
        return PacketCodec.pack(body, 7, 1)

    @staticmethod
    def heartbeat() -> bytes:
        return PacketCodec.pack(b"", 2, 1)

    @staticmethod
    def decode(data: bytes, depth: int = 0, _budget: Optional[Dict[str, int]] = None) -> Iterable[Tuple[int, int, bytes]]:
        budget = _budget or {"bytes": 0, "packets": 0}
        if depth > MAX_PACKET_DEPTH:
            raise ProtocolError("packet recursion limit exceeded")
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PACKET_TOTAL_BYTES:
            raise ProtocolError("packet input exceeds size budget")
        budget["bytes"] += len(data)
        if budget["bytes"] > MAX_PACKET_TOTAL_BYTES:
            raise ProtocolError("packet expansion exceeds size budget")
        offset = 0
        while offset + PacketCodec.HEADER <= len(data):
            packet_length, header_length, protover, operation, sequence = struct.unpack_from(">IHHII", data, offset)
            if packet_length < header_length or header_length < PacketCodec.HEADER or offset + packet_length > len(data):
                raise ProtocolError("invalid Bilibili packet length")
            body_length = packet_length - header_length
            if body_length > MAX_PACKET_BODY_BYTES:
                raise ProtocolError("Bilibili packet body exceeds size budget")
            budget["packets"] += 1
            if budget["packets"] > MAX_PACKET_COUNT:
                raise ProtocolError("too many packets in one payload")
            payload = data[offset + header_length: offset + packet_length]
            offset += packet_length
            if protover == 2 and operation == 5:
                try:
                    decompressed = zlib_decompress(payload, MAX_PACKET_BODY_BYTES)
                except Exception as error:
                    raise ProtocolError(f"zlib decode failed: {error}") from error
                yield from PacketCodec.decode(decompressed, depth + 1, budget)
            elif protover == 3 and operation == 5:
                try:
                    decompressed = brotli_decompress(payload, MAX_PACKET_BODY_BYTES)
                except Exception as error:
                    raise ProtocolError(f"Brotli decode unavailable or failed: {error}; install the optional 'brotli' package") from error
                yield from PacketCodec.decode(decompressed, depth + 1, budget)
            else:
                yield operation, protover, payload
        if offset != len(data):
            raise ProtocolError("trailing bytes in Bilibili packet")


def zlib_decompress(payload: bytes, limit: int = MAX_PACKET_BODY_BYTES) -> bytes:
    import zlib
    decoder = zlib.decompressobj()
    output = decoder.decompress(payload, limit + 1)
    if len(output) > limit or decoder.unconsumed_tail:
        raise ProtocolError("zlib output exceeds size budget")
    output += decoder.flush(limit + 1 - len(output))
    if len(output) > limit:
        raise ProtocolError("zlib output exceeds size budget")
    return output


def brotli_decompress(payload: bytes, limit: int = MAX_PACKET_BODY_BYTES) -> bytes:
    import brotli  # type: ignore
    decoder = brotli.Decompressor()
    output = bytearray()
    for offset in range(0, len(payload), 64 * 1024):
        output.extend(decoder.process(payload[offset:offset + 64 * 1024]))
        if len(output) > limit:
            raise ProtocolError("Brotli output exceeds size budget")
    return bytes(output)


class WebSocketClient:
    def __init__(self, host: str, port: int, path: str, cookie: str) -> None:
        self.host, self.port, self.path, self.cookie = host, port, path, cookie
        self.sock: Optional[socket.socket] = None
        self._read_buffer = bytearray()
        self._fragment_opcode: Optional[int] = None
        self._fragment_parts: List[bytes] = []
        self._fragment_size = 0

    def connect(self, timeout: float = 15) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            context = ssl.create_default_context()
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            headers = [
                f"GET {self.path} HTTP/1.1", f"Host: {self.host}:{self.port}", "Upgrade: websocket",
                "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                "Origin: https://live.bilibili.com", f"User-Agent: {USER_AGENT}",
            ]
            if self.cookie:
                headers.append(f"Cookie: {self.cookie}")
            self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii", "ignore"))
            response = self._read_until(b"\r\n\r\n", MAX_WS_HANDSHAKE_BYTES)
            header_text = response.decode("latin1", "replace")
            lines = header_text.split("\r\n")
            status_parts = lines[0].split() if lines else []
            if len(status_parts) < 2 or status_parts[0] != "HTTP/1.1" or status_parts[1] != "101":
                raise ProtocolError(f"WebSocket handshake failed: {header_text[:120]}")
            response_headers: Dict[str, str] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if separator:
                    response_headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if response_headers.get("sec-websocket-accept") != expected:
                raise ProtocolError("WebSocket handshake missing a valid Sec-WebSocket-Accept")
            if response_headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in response_headers.get("connection", "").lower():
                raise ProtocolError("WebSocket handshake missing upgrade headers")
            self.sock.settimeout(1.0)
        except Exception:
            self.close()
            try:
                raw.close()
            except OSError:
                pass
            raise

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def receive(self) -> Optional[Tuple[int, bytes]]:
        if not self.sock:
            return None
        try:
            first = self._read_exact(2)
        except socket.timeout:
            return None
        except ProtocolError:
            raise
        if not first:
            return (0x8, b"")
        first_byte, second_byte = first
        fin = bool(first_byte & 0x80)
        if first_byte & 0x70:
            raise ProtocolError("WebSocket RSV bits are not negotiated")
        opcode = first_byte & 0x0F
        if second_byte & 0x80:
            raise ProtocolError("server WebSocket frame must not be masked")
        try:
            length = second_byte & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            if length > MAX_WS_FRAME_BYTES:
                raise ProtocolError("WebSocket frame exceeds size budget")
            if opcode >= 0x8 and (not fin or length > 125):
                raise ProtocolError("invalid WebSocket control frame")
            payload = self._read_exact(length)
        except socket.timeout as error:
            raise ProtocolError("WebSocket frame read timed out") from error
        if opcode == 0x9:
            self._send_frame(0xA, payload)
            return None
        if opcode == 0xA:
            return None
        if opcode == 0x8:
            return opcode, payload
        if opcode == 0x0:
            if self._fragment_opcode is None:
                raise ProtocolError("unexpected WebSocket continuation frame")
            self._fragment_parts.append(payload)
            self._fragment_size += len(payload)
            if self._fragment_size > MAX_WS_MESSAGE_BYTES:
                raise ProtocolError("WebSocket message exceeds size budget")
            if not fin:
                return None
            opcode = self._fragment_opcode
            payload = b"".join(self._fragment_parts)
            self._fragment_opcode = None
            self._fragment_parts = []
            self._fragment_size = 0
            return opcode, payload
        if opcode not in (0x1, 0x2):
            raise ProtocolError("unsupported WebSocket opcode")
        if self._fragment_opcode is not None:
            raise ProtocolError("new WebSocket data frame interrupted a fragmented message")
        if not fin:
            self._fragment_opcode = opcode
            self._fragment_parts = [payload]
            self._fragment_size = len(payload)
            return None
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise ProtocolError("WebSocket is not connected")
        if opcode >= 0x8 and len(payload) > 125:
            raise ProtocolError("control frame exceeds WebSocket size limit")
        if len(payload) > MAX_WS_MESSAGE_BYTES:
            raise ProtocolError("WebSocket message exceeds size budget")
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read_exact(self, length: int) -> bytes:
        if not self.sock:
            return b""
        chunks = []
        remaining = length
        while remaining:
            if self._read_buffer:
                take = min(remaining, len(self._read_buffer))
                chunk = bytes(self._read_buffer[:take])
                del self._read_buffer[:take]
            else:
                chunk = self.sock.recv(remaining)
            if not chunk:
                if chunks:
                    raise ProtocolError("truncated WebSocket frame")
                return b""
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._read_buffer:
            chunk = self.sock.recv(1024) if self.sock else b""
            if not chunk:
                break
            self._read_buffer.extend(chunk)
            if len(self._read_buffer) > limit:
                raise ProtocolError("WebSocket handshake exceeds size budget")
        index = self._read_buffer.find(marker)
        if index < 0:
            raise ProtocolError("incomplete WebSocket handshake")
        end = index + len(marker)
        response = bytes(self._read_buffer[:end])
        del self._read_buffer[:end]
        return response


class EventStore:
    SCHEMA_VERSION = SCHEMA_VERSION
    SchemaVersionError = SchemaVersionError

    def __init__(self, path: Path) -> None:
        path_text = str(path)
        if path_text != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path_text, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._ensure_schema()
        except Exception:
            self.connection.close()
            raise
        self._reconcile_interrupted_sessions()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")

    def _ensure_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_tables = {"sessions", "events", "metric_snapshots", "capture_gaps"}
        if tables and version != self.SCHEMA_VERSION:
            raise SchemaVersionError(f"Bilibili compatibility database schema {version} requires an explicit migration to {self.SCHEMA_VERSION}")
        if tables and not expected_tables.issubset(tables):
            raise SchemaVersionError("Bilibili compatibility database schema is incomplete; refusing to write")
        if version > self.SCHEMA_VERSION:
            raise SchemaVersionError(f"Bilibili compatibility database schema {version} is newer than supported {self.SCHEMA_VERSION}")
        if version != 0 and not tables:
            raise SchemaVersionError(f"Bilibili compatibility database schema {version} has no matching schema; refusing to write")
        if tables:
            verify_schema_signature(self.connection)
            return
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          id INTEGER PRIMARY KEY, room_id TEXT NOT NULL, room_title TEXT NOT NULL,
          started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL DEFAULT 'running'
        );
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, event_type TEXT NOT NULL,
          event_time TEXT NOT NULL, uid INTEGER, uname TEXT,
          text TEXT, gift_name TEXT, gift_num INTEGER, amount INTEGER, popularity INTEGER,
          FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS metric_snapshots(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
          online INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, danmaku_rate REAL DEFAULT 0,
          total_danmaku INTEGER DEFAULT 0, FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS capture_gaps(
          id INTEGER PRIMARY KEY, provider TEXT NOT NULL, room_id TEXT NOT NULL,
          session_id INTEGER, run_id TEXT, gap_start TEXT NOT NULL, gap_end TEXT,
          reason TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'collector',
          status TEXT NOT NULL DEFAULT 'open', FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_session_time ON events(session_id, event_time);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(session_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_metrics_session_time ON metric_snapshots(session_id, recorded_at);
        CREATE INDEX IF NOT EXISTS idx_capture_gaps_session_time ON capture_gaps(session_id, gap_start, gap_end);
        """)
        create_signal_schema(self.connection, "sessions", "events", user_version=self.SCHEMA_VERSION)
        self.connection.commit()

    def _reconcile_interrupted_sessions(self) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE sessions SET ended_at=COALESCE(ended_at, ?), status='interrupted' WHERE status='running'",
                (utc_now(),),
            )
            self.connection.commit()

    def start_session(self, room_id: str, title: str) -> int:
        with self.lock:
            cursor = self.connection.execute("INSERT INTO sessions(room_id, room_title, started_at) VALUES(?,?,?)", (room_id, title, utc_now()))
            self.connection.commit()
            return int(cursor.lastrowid)

    def end_session(self, session_id: int, status: str = "stopped") -> None:
        with self.lock:
            self.connection.execute("UPDATE sessions SET ended_at=?, status=? WHERE id=?", (utc_now(), status, session_id))
            self.connection.commit()

    def insert_event(self, session_id: int, event: Dict[str, Any]) -> None:
        with self.lock:
            self.connection.execute("""INSERT INTO events(session_id,event_type,event_time,uid,uname,text,gift_name,gift_num,amount,popularity)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (session_id, event.get("type", "unknown"), event.get("event_time") or utc_now(), event.get("uid"), event.get("uname"), event.get("text"), event.get("gift_name"), event.get("gift_num"), event.get("amount"), event.get("popularity")))
            self.connection.commit()

    def insert_snapshot(self, session_id: int, metrics: Dict[str, Any]) -> None:
        with self.lock:
            self.connection.execute("INSERT INTO metric_snapshots(session_id,recorded_at,online,likes,danmaku_rate,total_danmaku) VALUES(?,?,?,?,?,?)", (session_id, utc_now(), metrics.get("online") if metrics.get("online") is None else safe_int(metrics.get("online")), metrics.get("likes", 0), metrics.get("rate", 0), metrics.get("total", 0)))
            self.connection.commit()

    def recent_events(self, session_id: Optional[int], limit: Optional[int] = 100, since: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if since:
            clauses.append("event_time>=?")
            params.append(since)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(safe_int(limit, 100), 1))
        with self.lock:
            rows = self.connection.execute(f"SELECT id,event_time,event_type,uid,uname,text,gift_name,gift_num,amount,popularity FROM events{where} ORDER BY id DESC{limit_sql}", params).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["_db_event_id"] = int(item.pop("id"))
            item["event_id"] = f"bilibili:event:{item['_db_event_id']}"
            if item.get("event_type") == "gift":
                item["value_contract"] = {
                    "quantity": item.get("gift_num"), "quantity_unit": "item",
                    "raw_platform_value": item.get("amount"), "currency": None,
                    "estimated_value": None, "estimated": False,
                    "value_semantics": "platform_amount_without_currency",
                }
            result.append(item)
        return result

    def recent_snapshots(self, session_id: Optional[int], limit: int = 60) -> List[Dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.connection.execute("SELECT recorded_at,online,likes,danmaku_rate,total_danmaku FROM metric_snapshots WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
            else:
                rows = self.connection.execute("SELECT recorded_at,online,likes,danmaku_rate,total_danmaku FROM metric_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def open_gap(self, provider: str, room_id: str, session_id: Optional[int], run_id: Optional[str], reason: str, started_at: Optional[str] = None, source: str = "collector") -> int:
        with self.lock:
            if session_id is not None and not self.connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                return 0
            existing = self.connection.execute(
                """SELECT id FROM capture_gaps
                WHERE provider=? AND room_id=? AND session_id IS ? AND run_id IS ?
                  AND reason=? AND status='open' ORDER BY id DESC LIMIT 1""",
                (provider, room_id, session_id, run_id, reason),
            ).fetchone()
            if existing:
                return int(existing[0])
            cursor = self.connection.execute(
                """INSERT INTO capture_gaps(provider,room_id,session_id,run_id,gap_start,reason,source,status)
                VALUES(?,?,?,?,?,?,?,'open')""",
                (provider, room_id, session_id, run_id, started_at or utc_now(), reason, source),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def close_gap(self, gap_id: int, ended_at: Optional[str] = None) -> bool:
        with self.lock:
            cursor = self.connection.execute("UPDATE capture_gaps SET gap_end=?, status='closed' WHERE id=? AND status='open'", (ended_at or utc_now(), gap_id))
            self.connection.commit()
            return cursor.rowcount == 1

    def close_open_gaps(
        self,
        session_id: int,
        run_id: Optional[str] = None,
        ended_at: Optional[str] = None,
        *,
        preserve_connecting: bool = False,
        connecting_failure_reason: Optional[str] = None,
    ) -> int:
        with self.lock:
            timestamp = ended_at or utc_now()
            closed = 0
            if connecting_failure_reason:
                scope = "session_id=? AND status='open' AND reason='connecting'"
                parameters: Tuple[Any, ...] = (connecting_failure_reason, timestamp, session_id)
                if run_id is not None:
                    scope += " AND run_id=?"
                    parameters += (run_id,)
                cursor = self.connection.execute(
                    f"UPDATE capture_gaps SET reason=?,gap_end=?,status='closed' WHERE {scope}",
                    parameters,
                )
                closed += cursor.rowcount
            excluded = " AND reason!='connecting'" if preserve_connecting else ""
            if run_id is None:
                cursor = self.connection.execute(
                    "UPDATE capture_gaps SET gap_end=?, status='closed' WHERE session_id=? AND status='open'" + excluded,
                    (timestamp, session_id),
                )
            else:
                cursor = self.connection.execute(
                    "UPDATE capture_gaps SET gap_end=?, status='closed' WHERE session_id=? AND run_id=? AND status='open'" + excluded,
                    (timestamp, session_id, run_id),
                )
            self.connection.commit()
            return closed + cursor.rowcount

    def gaps(self, session_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM capture_gaps" + (" WHERE session_id=?" if session_id is not None else "") + " ORDER BY id", (session_id,) if session_id is not None else ()).fetchall()
        return [dict(row) for row in rows]

    def coverage(self, session_id: int, start: str, end: str) -> Dict[str, Any]:
        gaps = self.gaps(session_id)
        with self.lock:
            session = self.connection.execute("SELECT started_at,ended_at FROM sessions WHERE id=?", (session_id,)).fetchone()
            event_times = [row[0] for row in self.connection.execute("SELECT event_time FROM events WHERE session_id=?", (session_id,))]
            snapshot_times = [row[0] for row in self.connection.execute("SELECT recorded_at FROM metric_snapshots WHERE session_id=?", (session_id,))]
        return evaluate_coverage_window(
            session_id=session_id, start=start, end=end,
            session_started_at=session["started_at"] if session else None,
            session_ended_at=session["ended_at"] if session else None,
            gaps=gaps,
            events=[{"time": value, "trusted": True} for value in event_times],
            snapshot_times=snapshot_times,
        )

    def close(self) -> None:
        with self.lock:
            self.connection.close()


def decode_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ProtocolError("invalid protobuf varint")


def parse_protobuf_fields(data: bytes) -> Dict[int, List[Any]]:
    fields: Dict[int, List[Any]] = {}
    offset = 0
    while offset < len(data):
        key, offset = decode_varint(data, offset)
        field_number, wire_type = key >> 3, key & 7
        if field_number <= 0:
            raise ProtocolError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = decode_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ProtocolError("truncated protobuf fixed64")
            value, offset = data[offset:offset + 8], offset + 8
        elif wire_type == 2:
            size, offset = decode_varint(data, offset)
            if size > len(data) - offset:
                raise ProtocolError("truncated protobuf bytes")
            value, offset = data[offset:offset + size], offset + size
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ProtocolError("truncated protobuf fixed32")
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise ProtocolError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(field_number, []).append(value)
    return fields


def parse_interact_word_v2(data: Any) -> Optional[Dict[str, Any]]:
    if isinstance(data, dict):
        uid, uname, msg_type = safe_int(data.get("uid")), str(data.get("uname") or ""), safe_int(data.get("msg_type"), 1)
        if uid or uname:
            return {"uid": uid, "uname": uname or "匿名用户", "msg_type": msg_type}
        if data.get("pb"):
            return parse_interact_word_v2(data["pb"])
    if isinstance(data, str):
        try:
            fields = parse_protobuf_fields(base64.b64decode(data))
            uid = safe_int((fields.get(1) or [0])[0])
            uname_value = (fields.get(2) or [b""])[0]
            uname = uname_value.decode("utf-8", "replace") if isinstance(uname_value, bytes) else str(uname_value)
            msg_type = safe_int((fields.get(5) or [1])[0], 1)
            if uid or uname:
                return {"uid": uid, "uname": uname or "匿名用户", "msg_type": msg_type}
        except (ValueError, ProtocolError):
            return None
    return None


def parse_business_event(body: bytes) -> Optional[Dict[str, Any]]:
    try:
        message = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict):
        return None
    command = str(message.get("cmd") or "")
    data = message.get("data") or {}
    event: Dict[str, Any] = {"raw": message, "event_time": utc_now()}
    if command.startswith("DANMU_MSG"):
        info = message.get("info") or []
        user = info[2] if len(info) > 2 and isinstance(info[2], list) else []
        event.update(type="danmaku", text=str(info[1] if len(info) > 1 else ""), uid=safe_int(user[0] if user else 0), uname=str(user[1] if len(user) > 1 else "匿名用户"))
        return event
    if command.startswith("SEND_GIFT"):
        gift_num = safe_int(data.get("num"), 1)
        platform_amount = optional_int(data.get("price"))
        event.update(
            type="gift", uid=safe_int(data.get("uid")), uname=str(data.get("uname") or "匿名用户"),
            gift_name=str(data.get("giftName") or "礼物"), gift_num=gift_num, amount=platform_amount,
            value_contract={"quantity": gift_num, "quantity_unit": "item", "quantity_semantics": "platform_reported", "unit_price": platform_amount, "currency": None, "estimated_value": None, "estimated": False, "value_semantics": "platform_amount_without_currency"},
        )
        return event
    if command.startswith("SUPER_CHAT_MESSAGE"):
        user = data.get("user_info") or {}
        event.update(type="sc", uid=safe_int(user.get("uid") or data.get("uid")), uname=str(user.get("uname") or data.get("uname") or "匿名用户"), text=str(data.get("message") or ""), amount=optional_int(data.get("price")))
        return event
    if command.startswith("INTERACT_WORD"):
        interact = parse_interact_word_v2(data) or {"uid": safe_int(data.get("uid")), "uname": str(data.get("uname") or "匿名用户"), "msg_type": safe_int(data.get("msg_type"), 1)}
        event.update(type="entry" if safe_int(interact.get("msg_type"), 1) == 1 else "interact", uid=safe_int(interact.get("uid")), uname=str(interact.get("uname") or "匿名用户"), text="进入直播间" if safe_int(interact.get("msg_type"), 1) == 1 else "互动")
        return event
    if command.startswith("WATCHED_CHANGE"):
        event.update(type="online", popularity=optional_int(data.get("num")))
        return event
    if command.startswith("LIKE_INFO_V3") or command.startswith("LIKE_INFO"):
        event.update(type="like", uid=safe_int(data.get("uid")), uname=str(data.get("uname") or "匿名用户"), text="点赞")
        return event
    return None


class Collector:
    provider_name = "bilibili"

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.signals = SignalEngine()
        self.command_lock = threading.RLock()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.socket: Optional[WebSocketClient] = None
        self._context: Optional[RunContext] = None
        self.generation = 0
        self.session_id: Optional[int] = None
        self.last_session_id: Optional[int] = None
        self.room_id = ""
        self.room_title = ""
        self.status_name = "idle"
        self.last_error = ""
        self.online = 0
        self.online_observed = False
        self.online_event_seen = False
        self.likes = 0
        self.total = 0
        self.started_at = ""
        self.last_valid_at = ""
        self._last_valid_monotonic = 0.0
        self.last_snapshot = 0.0
        self.minute_events: List[float] = []
        self.sessdata = ""
        self.buvid3 = ""
        self.diagnostics: Dict[str, Any] = {}

    def start(self, room_id: str, sessdata: str = "") -> RunContext:
        with self.command_lock:
            with self.lock:
                if self.thread and self.thread.is_alive():
                    raise BusyError("previous collector run is still stopping")
                self.thread = None
                self.generation += 1
                context = RunContext(self.generation, uuid.uuid4().hex, "bilibili", str(room_id).strip(), sessdata.strip())
                self._context = context
                self.room_id, self.sessdata, self.last_error = context.room_id, context.sessdata, ""
                self.room_title = ""
                self.last_session_id = None
                self.online = 0
                self.online_observed = False
                self.online_event_seen = False
                self.likes = 0
                self.total = 0
                self.minute_events = []
                self.last_valid_at = ""
                self._last_valid_monotonic = 0.0
                self.last_snapshot = 0.0
                self.diagnostics = {}
                self.status_name = "connecting"
                self.stop_event = context.cancel
                self.thread = threading.Thread(target=self._run, args=(context,), name=f"bili-collector-{context.generation}", daemon=True)
                self.thread.start()
                return context

    def stop(self, timeout: float = 2.0) -> bool:
        with self.command_lock:
            with self.lock:
                context, thread = self._context, self.thread
                if context is None or thread is None:
                    if context is not None:
                        self.status_name = "stopped"
                    return True
                context.cancel.set()
                self.stop_event = context.cancel
                self.status_name = "stopping"
                current_socket = self.socket
                self.socket = None
            if current_socket:
                current_socket.close()
            if thread is threading.current_thread():
                return False
            thread.join(max(0.0, timeout))
            with self.lock:
                if thread.is_alive():
                    if self._context is context:
                        self.status_name = "stopping"
                    return False
                if self._context is context:
                    self.status_name = "stopped"
                    self.thread = None
                return True

    def is_current_run(self, context: RunContext) -> bool:
        with self.lock:
            return self._context is context and not context.cancel.is_set()

    def _owns_active_run(self, context: RunContext) -> bool:
        with self.lock:
            return self._context is context and not context.cancel.is_set() and self.status_name in {"connecting", "authenticating", "connected", "stale"}

    def _mark_valid(self, context: RunContext) -> bool:
        with self.lock:
            if self._context is not context or context.cancel.is_set():
                return False
            self.last_valid_at = utc_now()
            self._last_valid_monotonic = time.monotonic()
            if self.status_name == "stale":
                self.status_name = "connected"
                self.last_error = ""
            if self.session_id:
                self.store.close_open_gaps(self.session_id, context.run_id, preserve_connecting=True)
            return True

    def _expire_stale_locked(self) -> None:
        if self.status_name == "connected" and self._last_valid_monotonic and time.monotonic() - self._last_valid_monotonic > FRESHNESS_TIMEOUT_SECONDS:
            self.status_name = "stale"
            self.last_error = "connection stale: no valid protocol frame"
            context = self._context
            if context and self.session_id:
                self.store.open_gap(
                    "bilibili", self.room_id, self.session_id, context.run_id, "stale",
                    started_at=stale_gap_start(self.last_valid_at, FRESHNESS_TIMEOUT_SECONDS),
                )

    def data_session_id(self) -> Optional[int]:
        with self.lock:
            return self.session_id if self.status_name in {"connecting", "authenticating", "connected", "stale"} else None

    def status(self) -> Dict[str, Any]:
        with self.lock:
            self._expire_stale_locked()
            connected = self.status_name == "connected" and self.session_id is not None
            context = self._context
            online = self.online if connected and self.online_observed else None
            return {"available": True, "connected": connected, "status": self.status_name, "data_source": "bilibili_websocket" if connected else "none", "room_id": self.room_id, "room_title": self.room_title, "session_id": self.session_id if connected else None, "generation": context.generation if context else 0, "run_id": context.run_id if context else None, "worker_alive": bool(self.thread and self.thread.is_alive()), "online": online, "online_known": online is not None, "online_measure": {"kind": "online_people", "value": online, "unit": "people", "source": "bilibili_protocol"} if online is not None else None, "likes": self.likes if connected else 0, "total": self.total if connected else 0, "rate": self._rate() if connected else 0, "last_valid_at": self.last_valid_at, "freshness_timeout_seconds": FRESHNESS_TIMEOUT_SECONDS, "last_error": self.last_error, "diagnostics": self.diagnostics}

    def metrics(self) -> Dict[str, Any]:
        with self.lock:
            self._expire_stale_locked()
            ranking: Dict[str, int] = {}
            keywords: Dict[str, int] = {}
            gift_quantity = 0
            session_id = self.session_id if self.status_name == "connected" else None
            if session_id:
                rows = self.store.recent_events(session_id, None)
                for row in rows:
                    if row.get("uname") and row.get("event_type") in ("danmaku", "sc"):
                        ranking[row["uname"]] = ranking.get(row["uname"], 0) + 1
                    if row.get("event_type") == "danmaku":
                        for word in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", row.get("text") or ""):
                            keywords[word] = keywords.get(word, 0) + 1
                    if row.get("event_type") == "gift":
                        gift_quantity += max(safe_int(row.get("gift_num")), 0)
            trend = self.store.recent_snapshots(session_id, 60)
            current = self.status()
            signal_records: List[Dict[str, Any]] = []
            if session_id and current.get("run_id"):
                reference = utc_now()
                start, end = snapshot_window(reference, 60)
                coverage = self.store.coverage(session_id, start, end)
                with self.store.lock:
                    previous = load_signals(self.store.connection, session_id, self.provider_name, self.room_id, current["run_id"], "events")
                normalized_events = []
                for row in rows:
                    if row.get("event_type") != "danmaku":
                        continue
                    normalized_events.append({
                        "event_id": row.get("event_id"), "_db_event_id": row.get("_db_event_id"),
                        "type": "comment", "timestamp_utc": row.get("event_time"),
                        "user_id": row.get("uid"), "user_name": row.get("uname"),
                        "content": row.get("text") or "", "analysis": analyze_text(row.get("text") or ""),
                    })
                analysis = self.signals.build(
                    normalized_events, current.get("online"), trend,
                    provider=self.provider_name, room_id=self.room_id, session_id=session_id,
                    run_id=current["run_id"], as_of=reference, coverage=coverage["coverage_state"], previous=previous,
                )
                with self.store.lock:
                    for signal in analysis.get("signals", []):
                        persist_signal(self.store.connection, signal, "events")
                    window_start, window_end = _window_bounds(reference, 60)
                    signal_records = load_signals(self.store.connection, session_id, self.provider_name, self.room_id, current["run_id"], "events", window_start, window_end)
            if current["connected"]:
                trend.append({"recorded_at": utc_now(), "online": current["online"], "likes": current["likes"], "danmaku_rate": current["rate"], "total_danmaku": current["total"]})
            return {**current, "ranking": sorted(ranking.items(), key=lambda item: item[1], reverse=True)[:10], "keywords": sorted(keywords.items(), key=lambda item: item[1], reverse=True)[:10], "gift_quantity": gift_quantity, "revenue": None, "revenue_currency": None, "revenue_semantics": "unknown_currency_not_estimated", "trend": trend, "signals": signal_records}

    def snapshot(self, window: Any = "300s", limit: int = 100) -> Dict[str, Any]:
        seconds = snapshot_window_seconds(window)
        for _attempt in range(3):
            with self.lock:
                self._expire_stale_locked()
                context = self._context
                status_name = self.status_name
                session_id = self.session_id
                room_id = self.room_id
                room_title = self.room_title
                worker_alive = bool(self.thread and self.thread.is_alive())
                last_valid_at = self.last_valid_at or None
                as_of = utc_now()
                start, end = snapshot_window(as_of, seconds)
                coverage_end = (datetime.fromisoformat(end) + timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
                identity = (
                    context.generation if context else 0,
                    context.run_id if context else None,
                    session_id,
                    room_id,
                    status_name,
                    worker_alive,
                    last_valid_at,
                )
                metrics = self.metrics()
                events = self.store.recent_events(session_id, limit, since=start) if session_id else []
                coverage = self.store.coverage(session_id, start, coverage_end) if session_id else {
                    "session_id": None, "start": start, "end": end,
                    "coverage_state": "unknown", "complete": False,
                    "has_open_gap": False, "event_count": 0, "snapshot_count": 0, "gaps": [],
                }
                coverage["end"] = end
                coverage["as_of"] = as_of
                after_context = self._context
                after_identity = (
                    after_context.generation if after_context else 0,
                    after_context.run_id if after_context else None,
                    self.session_id,
                    self.room_id,
                    self.status_name,
                    bool(self.thread and self.thread.is_alive()),
                    self.last_valid_at or None,
                )
                if identity != after_identity:
                    continue
                metrics = {
                    **metrics,
                    "provider": self.provider_name,
                    "room_id": room_id,
                    "session_id": session_id,
                    "run_id": context.run_id if context else None,
                    "generation": context.generation if context else 0,
                    "status": status_name,
                    "as_of": as_of,
                    "last_valid_at": last_valid_at,
                }
                snapshot_signals = [{**signal, "generation": context.generation if context else 0} for signal in metrics.get("signals", [])]
                metrics["signals"] = snapshot_signals
                return {
                    "provider": self.provider_name,
                    "room_id": room_id,
                    "room_title": room_title,
                    "session_id": session_id,
                    "run_id": context.run_id if context else None,
                    "generation": context.generation if context else 0,
                    "status": status_name,
                    "worker_alive": worker_alive,
                    "data_source": "bilibili_websocket" if session_id else "none",
                    "as_of": as_of,
                    "last_valid_at": last_valid_at,
                    "freshness": {
                        "state": "stale" if status_name == "stale" else status_name,
                        "stale": status_name == "stale",
                        "last_valid_at": last_valid_at,
                        "timeout_seconds": FRESHNESS_TIMEOUT_SECONDS,
                    },
                    "coverage": coverage,
                    "metrics": metrics,
                    "signals": snapshot_signals,
                    "events": {
                        "items": events,
                        "session_id": session_id,
                        "run_id": context.run_id if context else None,
                        "generation": context.generation if context else 0,
                        "as_of": as_of,
                        "data_source": "bilibili_websocket" if session_id else "none",
                        "count": len(events),
                    },
                    "error": self.last_error or None,
                    "diagnostics": dict(self.diagnostics),
                }
        raise SnapshotUnstableError("collector identity changed while creating snapshot")

    def _run(self, context: RunContext) -> None:
        client = HttpClient(sessdata=context.sessdata)
        session_id: Optional[int] = None
        terminal_status = "stopped"
        try:
            room = client.get_room(context.room_id)
            if context.cancel.is_set():
                return
            danmaku = client.get_danmaku_info(room["room_id"])
            self.buvid3 = client.buvid3
            with self.lock:
                if not self._owns_active_run(context):
                    return
                self.diagnostics = client.diagnostics()
                self.room_id, self.room_title = room["room_id"], room["title"]
                parsed_online = optional_int(room.get("online"))
                self.online = parsed_online if parsed_online is not None else 0
                self.online_observed = parsed_online is not None
                session_id = self.store.start_session(self.room_id, self.room_title)
                self.session_id = session_id
                self.store.open_gap("bilibili", self.room_id, session_id, context.run_id, "connecting")
                self.started_at = utc_now()
                self.status_name = "connecting"
            last_error = None
            for host_info in danmaku["hosts"]:
                if context.cancel.is_set():
                    return
                host = str(host_info.get("host") or "")
                port = safe_int(host_info.get("wss_port"), 443)
                if not host:
                    continue
                ws: Optional[WebSocketClient] = None
                try:
                    ws = WebSocketClient(host, port, "/sub", client.cookie)
                    ws.connect()
                    with self.lock:
                        if not self._owns_active_run(context):
                            return
                        self.socket = ws
                        self.status_name = "authenticating"
                    ws.send_binary(PacketCodec.auth(room["room_id"], danmaku["token"], uid=client.uid, buvid3=self.buvid3))
                    self._receive_loop(context, ws)
                    return
                except Exception as error:
                    last_error = str(error)
                    if context.cancel.is_set():
                        return
                finally:
                    if ws:
                        ws.close()
                    with self.lock:
                        if self._context is context and self.socket is ws:
                            self.socket = None
            raise ProtocolError(last_error or "all Bilibili WebSocket hosts failed")
        except Exception as error:
            with self.lock:
                if self._context is context and not context.cancel.is_set():
                    terminal_status = "error"
                    self.status_name = "error"
                    self.last_error = str(error)
                elif self._context is context:
                    self.status_name = "stopping"
        finally:
            self._finish_run(context, session_id, terminal_status)

    def _finish_run(self, context: RunContext, session_id: Optional[int], terminal_status: str) -> None:
        with self.lock:
            owner = self._context is context
            if owner:
                self.socket = None
                if self.session_id == session_id:
                    self.session_id = None
                if session_id:
                    self.last_session_id = session_id
                if self.status_name in {"connecting", "authenticating", "connected", "stale", "stopping"}:
                    self.status_name = "error" if terminal_status == "error" else "stopped"
        if session_id:
            self.store.close_open_gaps(
                session_id,
                context.run_id,
                connecting_failure_reason=None if context.capture_verified.is_set() else "protocol_unavailable",
            )
            self.store.end_session(session_id, terminal_status)

    def _receive_loop(self, context: RunContext, ws: WebSocketClient) -> None:
        last_heartbeat = 0.0
        while not context.cancel.is_set():
            now = time.time()
            if now - last_heartbeat >= 30:
                ws.send_binary(PacketCodec.heartbeat())
                last_heartbeat = now
            self._record_heartbeat_snapshot(context)
            received = ws.receive()
            if received is None:
                continue
            opcode, payload = received
            if opcode == 0x8:
                raise ProtocolError("Bilibili WebSocket closed the connection")
            if opcode not in (0x1, 0x2):
                continue
            self._handle_packet(context, payload)

    def _record_heartbeat_snapshot(self, context: RunContext) -> None:
        with self.lock:
            if not self._owns_active_run(context) or not self.session_id or time.time() - self.last_snapshot < 30:
                return
            session_id = self.session_id
        snapshot = self.status()
        with self.lock:
            if self._context is not context or context.cancel.is_set() or self.session_id != session_id:
                return
            self.store.insert_snapshot(session_id, snapshot)
            self.last_snapshot = time.time()

    def _handle_packet(self, context_or_payload: Any, payload: Optional[bytes] = None) -> None:
        legacy = payload is None
        context = None if legacy else context_or_payload
        payload = context_or_payload if legacy else payload
        if context is not None and not self._owns_active_run(context):
            return
        packets = list(PacketCodec.decode(payload))
        activity_marked = False

        def mark_packet_activity() -> bool:
            if context is not None:
                return self._mark_valid(context)
            with self.lock:
                if self.status_name not in {"connecting", "authenticating", "connected", "stale"} or self.session_id is None:
                    return False
                self.last_valid_at = utc_now()
                self._last_valid_monotonic = time.monotonic()
                return True

        for operation, _protover, body in packets:
            if operation == 8:
                try:
                    auth_result = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ProtocolError("Bilibili WebSocket authentication response is not valid JSON") from error
                if not isinstance(auth_result, dict) or isinstance(auth_result.get("code"), bool) or "code" not in auth_result or safe_int(auth_result.get("code"), -1) != 0:
                    raise ProtocolError("Bilibili WebSocket authentication response did not explicitly confirm code=0")
                if not mark_packet_activity():
                    return
                activity_marked = True
                with self.lock:
                    if (context is None or self._context is context) and self.status_name == "authenticating":
                        self.status_name = "connected"
                        if context is not None and self.session_id:
                            context.capture_verified.set()
                            self.store.close_open_gaps(self.session_id, context.run_id)
                continue
            if not activity_marked:
                if not mark_packet_activity():
                    return
                activity_marked = True
            if operation == 3 and len(body) >= 4:
                with self.lock:
                    if (context is None or self._context is context) and not self.online_event_seen:
                        self.online = struct.unpack_from(">I", body, 0)[0]
                        self.online_observed = True
                continue
            if operation != 5:
                continue
            event = parse_business_event(body)
            with self.lock:
                if context is not None and not self._owns_active_run(context):
                    continue
                session_id = self.session_id
            if not event or not session_id:
                continue
            event_type = event.get("type")
            if event_type == "online":
                with self.lock:
                    popularity = optional_int(event.get("popularity"))
                    if popularity is not None:
                        self.online = popularity
                        self.online_observed = True
                        self.online_event_seen = True
            elif event_type == "like":
                with self.lock:
                    self.likes += 1
            elif event_type not in ("unknown", None):
                with self.lock:
                    self.total += 1 if event_type in ("danmaku", "gift", "sc") else 0
                    self.minute_events.append(time.time())
                self.store.insert_event(session_id, event)
            if time.time() - self.last_snapshot >= 30:
                self.store.insert_snapshot(session_id, self.status())
                self.last_snapshot = time.time()

    def _rate(self) -> int:
        cutoff = time.time() - 60
        self.minute_events = [stamp for stamp in self.minute_events if stamp >= cutoff]
        return len(self.minute_events)


class RequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def read_body_with_deadline(stream: Any, length: int, timeout: float) -> bytes:
    deadline = time.monotonic() + max(float(timeout), 0.001)
    chunks: List[bytes] = []
    remaining = length
    while remaining:
        if time.monotonic() >= deadline:
            raise RequestError(408, "request body timeout")
        try:
            chunk = stream.read(min(8192, remaining))
        except (socket.timeout, TimeoutError) as error:
            raise RequestError(408, "request body timeout") from error
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining:
        if time.monotonic() >= deadline:
            raise RequestError(408, "request body timeout")
        raise RequestError(400, "incomplete request body")
    return b"".join(chunks)


class AppHandler(SimpleHTTPRequestHandler):
    collector: Any
    capability_token = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["directory"] = str(ROOT)
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def log_message(self, format_string: str, *args: Any) -> None:
        if self.path.startswith("/api/"):
            return
        super().log_message(format_string, *args)

    def end_headers(self) -> None:
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_port(self) -> int:
        return int(self.server.server_address[1])

    def _local_host(self, value: str) -> bool:
        try:
            parsed = urllib.parse.urlsplit("//" + value.strip())
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return False
        if hostname not in {"127.0.0.1", "localhost"}:
            return False
        return port == self._request_port() if port is not None else self._request_port() == 80

    def _local_origin(self, value: str) -> bool:
        if value == "null":
            return False
        try:
            parsed = urllib.parse.urlparse(value)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return False
        return parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost"} and port == self._request_port()

    def _check_request_boundary(self) -> bool:
        if not self._local_host(self.headers.get("Host", "")):
            self._send_json({"error": "invalid local host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and not self._local_origin(origin):
            self._send_json({"error": "invalid local origin"}, 403)
            return False
        return True

    def _authorize_api(self, path: str) -> bool:
        if not self._check_request_boundary():
            return False
        if path in PUBLIC_API_PATHS:
            return True
        if path not in PROTECTED_API_PATHS:
            self._send_json({"error": "not found"}, 404)
            return False
        provided = self.headers.get("X-Bullet-Screen-Token", "")
        if not self.capability_token or not hmac.compare_digest(provided, self.capability_token):
            self._send_json({"error": "local capability token required"}, 401)
            return False
        return True

    def _static_file(self) -> Optional[Path]:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        relative = STATIC_FILES.get(path)
        if not relative:
            return None
        candidate = (ROOT / relative).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _serve_static(self, head_only: bool = False) -> None:
        candidate = self._static_file()
        if not candidate:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(candidate.stat().st_size))
        self.end_headers()
        if not head_only:
            with candidate.open("rb") as stream:
                self.wfile.write(stream.read())

    def _read_json_body(self) -> Dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "transfer encoding is not supported")
        content_lengths = self.headers.get_all("Content-Length") or []
        if len(content_lengths) != 1 or not re.fullmatch(r"(?:0|[1-9][0-9]*)", content_lengths[0].strip()):
            raise RequestError(400, "invalid content length")
        length = int(content_lengths[0])
        if length > MAX_REQUEST_BYTES:
            raise RequestError(413, "request body too large")
        content_types = self.headers.get_all("Content-Type") or []
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise RequestError(415, "application/json is required")
        body = read_body_with_deadline(self.rfile, length, REQUEST_TIMEOUT_SECONDS)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestError(400, "invalid JSON") from error
        if not isinstance(payload, dict):
            raise RequestError(400, "JSON object is required")
        return payload

    def do_OPTIONS(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorize_api(parsed.path):
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if self._authorize_api(parsed.path):
                self.send_error(405)
            return
        if not self._check_request_boundary():
            return
        self._serve_static(head_only=True)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "service": getattr(self.collector, "provider_name", "live-intelligence-local")})
            return
        if parsed.path == "/api/bootstrap":
            self._send_json({"token": self.capability_token})
            return
        if parsed.path == "/api/status":
            self._send_json(self.collector.status())
            return
        if parsed.path == "/api/metrics":
            self._send_json(self.collector.metrics())
            return
        if parsed.path == "/api/snapshot":
            query = urllib.parse.parse_qs(parsed.query)
            window = (query.get("window") or ["300s"])[0]
            limit = min(max(safe_int((query.get("limit") or [100])[0], 100), 1), 500)
            try:
                self._send_json(self.collector.snapshot(window=window, limit=limit))
            except SnapshotUnstableError as error:
                self._send_json({"error": str(error), "status": "snapshot_unstable"}, 409)
            return
        if parsed.path == "/api/events":
            query = urllib.parse.parse_qs(parsed.query)
            limit = min(max(safe_int((query.get("limit") or [100])[0], 100), 1), 500)
            if getattr(self.collector, "provider_name", "") == "bilibili":
                session_id = self.collector.live_session_id()
                events = self.collector.store.recent_events(session_id, limit) if session_id else []
                if session_id != self.collector.live_session_id():
                    session_id, events = None, []
                self._send_json({"events": events, "session_id": session_id, "data_source": "bilibili_websocket" if session_id else "none"})
            elif hasattr(self.collector, "recent_events"):
                events = self.collector.recent_events(limit)
                status = self.collector.status()
                session_id = status.get("session_id") if status.get("connected") else None
                self._send_json({"events": events, "session_id": session_id, "data_source": "douyin_adapter" if session_id else "none"})
            else:
                events = self.collector.store.recent_events(self.collector.data_session_id(), limit)
                self._send_json({"events": events, "session_id": self.collector.data_session_id(), "data_source": "bilibili_websocket"})
            return
        if not self._check_request_boundary():
            return
        self._serve_static()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorize_api(parsed.path):
            return
        if parsed.path not in ("/api/connect", "/api/disconnect", "/api/signals/feedback"):
            return
        try:
            payload = self._read_json_body()
            if parsed.path == "/api/signals/feedback":
                signal_id = str(payload.get("signal_id") or "").strip()
                feedback_type = str(payload.get("feedback_type") or "").strip()
                if not signal_id:
                    self._send_json({"error": "signal_id is required"}, 400)
                    return
                try:
                    feedback = add_signal_feedback(self.collector.store.connection, signal_id, feedback_type, payload.get("note"))
                except SignalFeedbackError as error:
                    self._send_json({"error": str(error)}, 400)
                    return
                self._send_json({"feedback": feedback}, 201)
            elif parsed.path == "/api/connect":
                room_input = str(payload.get("url") or payload.get("room_id") or "").strip()
                if not room_input:
                    self._send_json({"error": "请提供抖音直播间 URL 或 room_id"}, 400)
                    return
                if isinstance(self.collector, DouyinCollector):
                    self.collector.start(room_input, str(payload.get("cookie") or payload.get("sessdata") or ""), str(payload.get("mode") or "") or None)
                else:
                    if not room_input.isdigit():
                        self._send_json({"error": "B 站 room_id must be numeric"}, 400)
                        return
                    self.collector.start(room_input, str(payload.get("sessdata") or ""))
                self._send_json(self.collector.status(), 202)
            else:
                stopped = self.collector.stop()
                self._send_json(self.collector.status(), 200 if stopped else 202)
        except BusyError:
            self._send_json({"error": "previous collector run is still stopping", "status": self.collector.status()}, 409)
        except RequestError as error:
            self._send_json({"error": error.message}, error.status)
        except (socket.timeout, TimeoutError):
            self._send_json({"error": "request timeout"}, 408)
        except Exception:
            self._send_json({"error": "internal server error"}, 500)


def run_self_test() -> None:
    import tempfile
    signed = WbiSigner.sign({"id": "7734200", "type": 0, "web_location": "444.8"}, "a" * 32, "b" * 32, now=1700000000)
    assert signed["w_rid"] == "0219b0595ed0f2a70c0415d0b620ebbd"
    full_cookie_client = HttpClient("SESSDATA=secret; bili_jct=csrf; DedeUserID=1")
    assert full_cookie_client.sessdata == "secret" and "bili_jct=csrf" in full_cookie_client.cookie and full_cookie_client.diagnostics()["sessdata_present"]
    class FakeHttpClient(HttpClient):
        def __init__(self) -> None:
            super().__init__(buvid3="test-buvid")
            self.calls: List[str] = []

        def get_json(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15) -> Dict[str, Any]:
            self.calls.append(url)
            if "w_rid=" not in url:
                return {"code": -352, "message": "-352"}
            return {"code": 0, "data": {"token": "token", "host_list": [{"host": "example.invalid", "wss_port": 443}]}}

        def get_wbi_keys(self) -> Tuple[str, str]:
            return "a" * 32, "b" * 32

    fake_client = FakeHttpClient()
    fake_danmaku = fake_client.get_danmaku_info("6")
    assert fake_danmaku["token"] == "token" and len(fake_client.calls) == 2 and "id=6&type=0" in fake_client.calls[0]
    assert [attempt["flow"] for attempt in fake_client.diagnostics()["api_attempts"]] == ["direct", "wbi"]
    with tempfile.TemporaryDirectory() as temporary:
        store = EventStore(Path(temporary) / "test.sqlite3")
        session = store.start_session("6", "test")
        store.insert_event(session, {"type": "danmaku", "event_time": utc_now(), "uid": 1, "uname": "tester", "text": "hello", "raw": {"cmd": "DANMU_MSG"}})
        assert len(store.recent_events(session, 10)) == 1
        auth = PacketCodec.auth("6", "token", buvid3="buvid")
        assert struct.unpack_from(">IHHII", auth)[:4] == (len(auth), 16, 1, 7)
        inner = PacketCodec.pack(json_bytes({"cmd": "WATCHED_CHANGE", "data": {"num": 7}}), 5, 1)
        outer = PacketCodec.pack(__import__("zlib").compress(inner), 5, 2)
        decoded = list(PacketCodec.decode(outer))
        assert decoded and json.loads(decoded[0][2])["data"]["num"] == 7
        assert parse_business_event(json_bytes({"cmd": "DANMU_MSG", "info": [None, "hi", [12, "name"]]}))["uname"] == "name"
        collector = Collector(store)
        collector.session_id = session
        collector.status_name = "connected"
        danmaku_packet = PacketCodec.pack(json_bytes({"cmd": "DANMU_MSG", "info": [None, "through collector", [12, "name"]]}), 5, 1)
        collector._handle_packet(PacketCodec.pack(__import__("zlib").compress(danmaku_packet), 5, 2))
        assert len(store.recent_events(session, 10)) == 2 and len(store.recent_snapshots(session, 10)) == 1
        online_packet = PacketCodec.pack(struct.pack(">I", 999999), 3, 1) + PacketCodec.pack(json_bytes({"cmd": "WATCHED_CHANGE", "data": {"num": 7}}), 5, 1)
        collector._handle_packet(online_packet)
        assert collector.online == 7 and collector.online_event_seen
        collector.session_id = None
        collector.status_name = "stopped"
        assert collector.metrics()["ranking"] == []
        def varint(value: int) -> bytes:
            output = bytearray()
            while value > 127:
                output.append((value & 127) | 128)
                value >>= 7
            output.append(value)
            return bytes(output)
        interact_proto = varint(8) + varint(123) + varint(18) + varint(4) + b"name" + varint(40) + varint(1)
        interact_event = parse_business_event(json_bytes({"cmd": "INTERACT_WORD_V2", "data": base64.b64encode(interact_proto).decode("ascii")}))
        assert interact_event and interact_event["type"] == "entry" and interact_event["uname"] == "name"
        wrapped_interact = parse_business_event(json_bytes({"cmd": "INTERACT_WORD_V2", "data": {"pb": base64.b64encode(interact_proto).decode("ascii")}}))
        assert wrapped_interact and wrapped_interact["uid"] == 123 and wrapped_interact["uname"] == "name"
        store.close()
    assert parse_room_input("https://live.douyin.com/123456")[0] == "123456"
    assert analyze_text("这个多少钱，怎么买？")["purchase_intent"] == "high"
    normalized = normalize_event("123456", "comment", "u1", "测试用户", "支持油皮吗？")
    assert normalized["type"] == "comment" and normalized["analysis"]["topic"] == "product"
    def proto_field(number: int, value: Any, wire_type: int = 2) -> bytes:
        if wire_type == 0:
            return varint(number << 3) + varint(int(value))
        encoded = bytes(value)
        return varint((number << 3) | 2) + varint(len(encoded)) + encoded
    proto_user = proto_field(1, 7, 0) + proto_field(3, "协议用户".encode("utf-8"))
    proto_common = proto_field(4, 1787510000000, 0)
    proto_chat = proto_field(1, proto_common) + proto_field(2, proto_user) + proto_field(3, "真实评论".encode("utf-8"))
    proto_envelope = proto_field(1, b"WebcastChatMessage") + proto_field(2, proto_chat) + proto_field(3, 99, 0)
    proto_response = proto_field(1, proto_envelope) + proto_field(4, 1787510000123, 0)
    decoded_response = _decode_im_response(proto_response)
    decoded_event = _decode_im_event(decoded_response["messages"][0], decoded_response["now"])
    assert decoded_event and decoded_event["type"] == "comment"
    assert decoded_event["user_name"] == "协议用户" and decoded_event["content"] == "真实评论"
    assert decoded_event["metadata"]["event_id"] == "99" and decoded_event["metadata"]["source"] == "fetch_protobuf"
    with tempfile.TemporaryDirectory() as temporary:
        douyin = DouyinCollector(Path(temporary) / "douyin.sqlite3", mode="demo")
        douyin.start("123456")
        time.sleep(2.4)
        demo_metrics = douyin.metrics()
        assert demo_metrics["provider"] == "douyin" and demo_metrics["current"]["comments"] >= 1
        assert isinstance(demo_metrics["signals"], list) and douyin.recent_events(20)
        douyin.stop()
    print("self-test: PASS (sqlite, Bilibili codec, Douyin fetch protobuf, event analysis)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Intelligence local collector and dashboard server")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--provider", choices=("douyin", "bilibili"), default="douyin")
    parser.add_argument("--mode", choices=("auto", "playwright", "demo"), default="auto", help="Douyin adapter mode")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return
    database_lock = None
    profile_lock = None
    collector = None
    server = None
    profile_override = Path(os.environ["DOUYIN_PROFILE_DIR"]).expanduser() if os.environ.get("DOUYIN_PROFILE_DIR") else None
    profile_path = None
    try:
        assert_no_pending_lifecycle(PROJECT_ROOT)
        if args.provider == "douyin":
            db_path = Path(":memory:") if args.mode == "demo" else Path(args.db).resolve()
            if args.mode != "demo":
                profile_path = resolve_profile_path(PROJECT_ROOT, profile_override)
                database_lock = acquire_database_lock(db_path)
                profile_lock = acquire_profile_lock(profile_path)
                assert_no_pending_lifecycle(PROJECT_ROOT)
            collector = DouyinCollector(db_path, mode=args.mode)
        else:
            db_path = Path(args.db).resolve()
            database_lock = acquire_database_lock(db_path)
            assert_no_pending_lifecycle(PROJECT_ROOT)
            collector = Collector(EventStore(db_path))
        handler = type("BoundAppHandler", (AppHandler,), {"collector": collector, "capability_token": secrets.token_urlsafe(32)})
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
        print(f"Live Intelligence local service: http://127.0.0.1:{server.server_address[1]}/", flush=True)
        print(f"Provider: {args.provider} · mode: {args.mode}")
        print(f"SQLite: {'memory only' if args.provider == 'douyin' and args.mode == 'demo' else Path(args.db).resolve()}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    except LifecycleError as error:
        raise SystemExit(str(error)) from error
    finally:
        if collector is not None:
            collector.stop()
            if hasattr(collector, "store"):
                collector.store.close()
        if server is not None:
            server.server_close()
        if profile_lock is not None:
            release_lifecycle_lock(profile_lock)
        if database_lock is not None:
            fcntl.flock(database_lock.fileno(), fcntl.LOCK_UN)
            database_lock.close()


if __name__ == "__main__":
    main()
