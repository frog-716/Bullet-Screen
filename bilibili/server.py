#!/usr/bin/env python3
"""BiliDanmaku local collector.

The browser is intentionally only a dashboard. This process owns credentials,
the Bilibili REST/WebSocket protocol, SQLite persistence, and the small local
HTTP API consumed by the dashboard.
"""

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import random
import re
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "danmaku.sqlite3"
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


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ProtocolError(RuntimeError):
    pass


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
    def decode(data: bytes, depth: int = 0) -> Iterable[Tuple[int, int, bytes]]:
        if depth > 4:
            raise ProtocolError("packet recursion limit exceeded")
        offset = 0
        while offset + PacketCodec.HEADER <= len(data):
            packet_length, header_length, protover, operation, sequence = struct.unpack_from(">IHHII", data, offset)
            if packet_length < header_length or header_length < PacketCodec.HEADER or offset + packet_length > len(data):
                raise ProtocolError("invalid Bilibili packet length")
            payload = data[offset + header_length: offset + packet_length]
            offset += packet_length
            if protover == 2 and operation == 5:
                try:
                    decompressed = zlib_decompress(payload)
                except Exception as error:
                    raise ProtocolError(f"zlib decode failed: {error}") from error
                yield from PacketCodec.decode(decompressed, depth + 1)
            elif protover == 3 and operation == 5:
                try:
                    decompressed = brotli_decompress(payload)
                except Exception as error:
                    raise ProtocolError(f"Brotli decode unavailable or failed: {error}; install the optional 'brotli' package") from error
                yield from PacketCodec.decode(decompressed, depth + 1)
            else:
                yield operation, protover, payload
        if offset != len(data):
            raise ProtocolError("trailing bytes in Bilibili packet")


def zlib_decompress(payload: bytes) -> bytes:
    import zlib
    return zlib.decompress(payload)


def brotli_decompress(payload: bytes) -> bytes:
    import brotli  # type: ignore
    return brotli.decompress(payload)


class WebSocketClient:
    def __init__(self, host: str, port: int, path: str, cookie: str) -> None:
        self.host, self.port, self.path, self.cookie = host, port, path, cookie
        self.sock: Optional[socket.socket] = None

    def connect(self, timeout: float = 15) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
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
        response = self._read_until(b"\r\n\r\n", 16384)
        if not response.startswith(b"HTTP/1.1 101"):
            raise ProtocolError(f"WebSocket handshake failed: {response[:120].decode('latin1', 'replace')}")
        self.sock.settimeout(1.0)

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
        if not first:
            return (0x8, b"")
        first_byte, second_byte = first
        opcode = first_byte & 0x0F
        length = second_byte & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        masked = bool(second_byte & 0x80)
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 0x9:
            self._send_frame(0xA, payload)
            return None
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise ProtocolError("WebSocket is not connected")
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
            chunk = self.sock.recv(remaining)
            if not chunk:
                return b""
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        data = b""
        while marker not in data and len(data) < limit:
            chunk = self.sock.recv(1024) if self.sock else b""
            if not chunk:
                break
            data += chunk
        return data


class EventStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
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
        CREATE INDEX IF NOT EXISTS idx_events_session_time ON events(session_id, event_time);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(session_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_metrics_session_time ON metric_snapshots(session_id, recorded_at);
        """)
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
            self.connection.execute("INSERT INTO metric_snapshots(session_id,recorded_at,online,likes,danmaku_rate,total_danmaku) VALUES(?,?,?,?,?,?)", (session_id, utc_now(), metrics.get("online", 0), metrics.get("likes", 0), metrics.get("rate", 0), metrics.get("total", 0)))
            self.connection.commit()

    def recent_events(self, session_id: Optional[int], limit: int = 100) -> List[Dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.connection.execute("SELECT event_time,event_type,uid,uname,text,gift_name,gift_num,amount,popularity FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
            else:
                rows = self.connection.execute("SELECT event_time,event_type,uid,uname,text,gift_name,gift_num,amount,popularity FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_snapshots(self, session_id: Optional[int], limit: int = 60) -> List[Dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.connection.execute("SELECT recorded_at,online,likes,danmaku_rate,total_danmaku FROM metric_snapshots WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
            else:
                rows = self.connection.execute("SELECT recorded_at,online,likes,danmaku_rate,total_danmaku FROM metric_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

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
        event.update(type="gift", uid=safe_int(data.get("uid")), uname=str(data.get("uname") or "匿名用户"), gift_name=str(data.get("giftName") or "礼物"), gift_num=safe_int(data.get("num"), 1), amount=safe_int(data.get("price")))
        return event
    if command.startswith("SUPER_CHAT_MESSAGE"):
        user = data.get("user_info") or {}
        event.update(type="sc", uid=safe_int(user.get("uid") or data.get("uid")), uname=str(user.get("uname") or data.get("uname") or "匿名用户"), text=str(data.get("message") or ""), amount=safe_int(data.get("price")))
        return event
    if command.startswith("INTERACT_WORD"):
        interact = parse_interact_word_v2(data) or {"uid": safe_int(data.get("uid")), "uname": str(data.get("uname") or "匿名用户"), "msg_type": safe_int(data.get("msg_type"), 1)}
        event.update(type="entry" if safe_int(interact.get("msg_type"), 1) == 1 else "interact", uid=safe_int(interact.get("uid")), uname=str(interact.get("uname") or "匿名用户"), text="进入直播间" if safe_int(interact.get("msg_type"), 1) == 1 else "互动")
        return event
    if command.startswith("WATCHED_CHANGE"):
        event.update(type="online", popularity=safe_int(data.get("num")))
        return event
    if command.startswith("LIKE_INFO_V3") or command.startswith("LIKE_INFO"):
        event.update(type="like", uid=safe_int(data.get("uid")), uname=str(data.get("uname") or "匿名用户"), text="点赞")
        return event
    return None


class Collector:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.socket: Optional[WebSocketClient] = None
        self.session_id: Optional[int] = None
        self.last_session_id: Optional[int] = None
        self.room_id = ""
        self.room_title = ""
        self.status_name = "idle"
        self.last_error = ""
        self.online = 0
        self.online_event_seen = False
        self.likes = 0
        self.total = 0
        self.started_at = ""
        self.last_snapshot = 0.0
        self.minute_events: List[float] = []
        self.sessdata = ""
        self.buvid3 = ""
        self.diagnostics: Dict[str, Any] = {}

    def start(self, room_id: str, sessdata: str = "") -> None:
        self.stop()
        with self.lock:
            self.room_id, self.sessdata, self.last_error = str(room_id).strip(), sessdata.strip(), ""
            self.room_title = ""
            self.last_session_id = None
            self.online = 0
            self.online_event_seen = False
            self.likes = 0
            self.total = 0
            self.minute_events = []
            self.last_snapshot = 0.0
            self.diagnostics = {}
            self.status_name = "connecting"
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, name="bili-collector", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            current_socket = self.socket
            session_id = self.session_id
            self.socket = None
        if current_socket:
            current_socket.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        if session_id:
            self.store.end_session(session_id)
        with self.lock:
            if session_id:
                self.last_session_id = session_id
                self.last_error = ""
            self.thread = None
            self.session_id = None
            self.status_name = "idle"

    def data_session_id(self) -> Optional[int]:
        with self.lock:
            return self.session_id or self.last_session_id

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {"available": True, "connected": self.status_name == "connected", "status": self.status_name, "room_id": self.room_id, "room_title": self.room_title, "session_id": self.session_id, "online": self.online, "likes": self.likes, "total": self.total, "rate": self._rate(), "last_error": self.last_error, "diagnostics": self.diagnostics}

    def metrics(self) -> Dict[str, Any]:
        with self.lock:
            ranking: Dict[str, int] = {}
            keywords: Dict[str, int] = {}
            revenue = 0.0
            session_id = self.session_id or self.last_session_id
            if session_id:
                rows = self.store.recent_events(session_id, 500)
                for row in rows:
                    if row.get("uname") and row.get("event_type") in ("danmaku", "sc"):
                        ranking[row["uname"]] = ranking.get(row["uname"], 0) + 1
                    if row.get("event_type") == "danmaku":
                        for word in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", row.get("text") or ""):
                            keywords[word] = keywords.get(word, 0) + 1
                    if row.get("event_type") == "gift":
                        revenue += safe_int(row.get("amount")) / 1000
                    elif row.get("event_type") == "sc":
                        revenue += safe_int(row.get("amount"))
            trend = self.store.recent_snapshots(session_id, 60)
            current = self.status()
            if current["connected"]:
                trend.append({"recorded_at": utc_now(), "online": current["online"], "likes": current["likes"], "danmaku_rate": current["rate"], "total_danmaku": current["total"]})
            return {**current, "ranking": sorted(ranking.items(), key=lambda item: item[1], reverse=True)[:10], "keywords": sorted(keywords.items(), key=lambda item: item[1], reverse=True)[:10], "revenue": round(revenue, 2), "trend": trend}

    def _run(self) -> None:
        client = HttpClient(sessdata=self.sessdata)
        try:
            room = client.get_room(self.room_id)
            danmaku = client.get_danmaku_info(room["room_id"])
            self.buvid3 = client.buvid3
            with self.lock:
                self.diagnostics = client.diagnostics()
                self.room_id, self.room_title = room["room_id"], room["title"]
                self.online = safe_int(room.get("online"))
                self.session_id = self.store.start_session(self.room_id, self.room_title)
                self.started_at = utc_now()
                self.status_name = "connecting"
            last_error = None
            for host_info in danmaku["hosts"]:
                if self.stop_event.is_set():
                    return
                host = str(host_info.get("host") or "")
                port = safe_int(host_info.get("wss_port"), 443)
                if not host:
                    continue
                try:
                    ws = WebSocketClient(host, port, "/sub", client.cookie)
                    ws.connect()
                    with self.lock:
                        self.socket = ws
                        self.status_name = "connected"
                    ws.send_binary(PacketCodec.auth(self.room_id, danmaku["token"], uid=client.uid, buvid3=self.buvid3))
                    self._receive_loop(ws)
                    return
                except Exception as error:
                    last_error = str(error)
                    if self.stop_event.is_set():
                        return
            raise ProtocolError(last_error or "all Bilibili WebSocket hosts failed")
        except Exception as error:
            with self.lock:
                if self.stop_event.is_set():
                    self.status_name = "stopped"
                else:
                    self.status_name = "error"
                    self.last_error = str(error)
                self.diagnostics = client.diagnostics()
        finally:
            with self.lock:
                current_session = self.session_id
                self.socket = None
                if self.status_name == "connected":
                    self.status_name = "stopped"
                self.session_id = None
                if current_session:
                    self.last_session_id = current_session
            if current_session:
                self.store.end_session(current_session, "error" if self.last_error else "stopped")

    def _receive_loop(self, ws: WebSocketClient) -> None:
        last_heartbeat = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            if now - last_heartbeat >= 30:
                ws.send_binary(PacketCodec.heartbeat())
                last_heartbeat = now
            received = ws.receive()
            if received is None:
                continue
            opcode, payload = received
            if opcode == 0x8:
                raise ProtocolError("Bilibili WebSocket closed the connection")
            if opcode not in (0x1, 0x2):
                continue
            self._handle_packet(payload)

    def _handle_packet(self, payload: bytes) -> None:
        for operation, _protover, body in PacketCodec.decode(payload):
            if operation == 8:
                try:
                    auth_result = json.loads(body.decode("utf-8", "replace")) if body else {}
                    if isinstance(auth_result, dict) and safe_int(auth_result.get("code"), 0) != 0:
                        raise ProtocolError(f"Bilibili WebSocket authentication failed: {auth_result.get('message') or auth_result.get('code')}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                continue
            if operation == 3 and len(body) >= 4:
                with self.lock:
                    if not self.online_event_seen:
                        self.online = struct.unpack_from(">I", body, 0)[0]
                continue
            if operation != 5:
                continue
            event = parse_business_event(body)
            with self.lock:
                session_id = self.session_id
            if not event or not session_id:
                continue
            event_type = event.get("type")
            if event_type == "online":
                with self.lock:
                    self.online = event.get("popularity", 0)
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


class AppHandler(SimpleHTTPRequestHandler):
    collector: Collector

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["directory"] = str(ROOT)
        super().__init__(*args, **kwargs)

    def log_message(self, format_string: str, *args: Any) -> None:
        if self.path.startswith("/api/"):
            return
        super().log_message(format_string, *args)

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "service": "bili-danmaku-local"})
            return
        if parsed.path == "/api/status":
            self._send_json(self.collector.status())
            return
        if parsed.path == "/api/metrics":
            self._send_json(self.collector.metrics())
            return
        if parsed.path == "/api/events":
            query = urllib.parse.parse_qs(parsed.query)
            limit = min(max(safe_int((query.get("limit") or [100])[0], 100), 1), 500)
            self._send_json({"events": self.collector.store.recent_events(self.collector.data_session_id(), limit)})
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/api/connect", "/api/disconnect"):
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = min(safe_int(self.headers.get("Content-Length"), 0), 128 * 1024)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/api/connect":
                room_id = str(payload.get("room_id") or "").strip()
                if not room_id or not room_id.isdigit():
                    self._send_json({"error": "room_id must be numeric"}, 400)
                    return
                self.collector.start(room_id, str(payload.get("sessdata") or ""))
                self._send_json(self.collector.status(), 202)
            else:
                self.collector.stop()
                self._send_json(self.collector.status())
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self._send_json({"error": f"invalid JSON: {error}"}, 400)
        except Exception as error:
            self._send_json({"error": str(error)}, 500)


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
        danmaku_packet = PacketCodec.pack(json_bytes({"cmd": "DANMU_MSG", "info": [None, "through collector", [12, "name"]]}), 5, 1)
        collector._handle_packet(PacketCodec.pack(__import__("zlib").compress(danmaku_packet), 5, 2))
        assert len(store.recent_events(session, 10)) == 2 and len(store.recent_snapshots(session, 10)) == 1
        online_packet = PacketCodec.pack(struct.pack(">I", 999999), 3, 1) + PacketCodec.pack(json_bytes({"cmd": "WATCHED_CHANGE", "data": {"num": 7}}), 5, 1)
        collector._handle_packet(online_packet)
        assert collector.online == 7 and collector.online_event_seen
        collector.session_id = None
        collector.last_session_id = session
        assert ("name", 1) in collector.metrics()["ranking"]
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
    print("self-test: PASS (sqlite, packet codec, zlib recursion, DANMU_MSG parser)")


def main() -> None:
    parser = argparse.ArgumentParser(description="BiliDanmaku local collector and dashboard server")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        return
    store = EventStore(Path(args.db))
    collector = Collector(store)
    handler = type("BoundAppHandler", (AppHandler,), {"collector": collector})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"BiliDanmaku local service: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    print(f"SQLite: {Path(args.db).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
