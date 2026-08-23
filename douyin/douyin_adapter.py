#!/usr/bin/env python3
"""Douyin public-room adapter and collector.

The real adapter observes a normal browser session. It deliberately avoids
hard-coding a private signing endpoint; protocol-specific decoding can be
added later without changing the event pipeline.
"""

import base64
import gzip
import json
import os
import re
import threading
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from live_intelligence import LiveEventStore, SignalEngine, normalize_event, safe_int, utc_now

try:
    import brotli  # type: ignore
except ImportError:  # pragma: no cover - optional runtime capability
    brotli = None


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


class AdapterError(RuntimeError):
    pass


def parse_room_input(value: str) -> Tuple[str, str]:
    raw = str(value or "").strip()
    if raw.isdigit():
        return raw, "https://live.douyin.com/" + raw
    parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
    host = parsed.netloc.lower().split(":", 1)[0]
    if not (host == "douyin.com" or host.endswith(".douyin.com")):
        raise AdapterError("请输入抖音公开直播间 URL 或数字 room_id")
    query = urllib.parse.parse_qs(parsed.query)
    candidates = list(parsed.path.split("/")) + query.get("room_id", []) + query.get("live_id", [])
    room_id = next((part for part in candidates if part.isdigit()), "")
    if not room_id:
        raise AdapterError("无法从抖音 URL 解析 room_id；请使用 live.douyin.com/<room_id>")
    # Do not persist arbitrary query strings: live URLs can contain signed or
    # session-scoped parameters. A canonical public room URL is sufficient.
    return room_id, "https://live.douyin.com/" + room_id


def _cookie_pairs(cookie: str) -> List[Dict[str, str]]:
    cookies = []
    for item in str(cookie or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and re.fullmatch(r"[A-Za-z0-9_\-]+", name) and value:
            cookies.append({"name": name, "value": value, "domain": ".douyin.com", "path": "/"})
    return cookies


def _decode_varint(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _protobuf_strings(data: bytes, depth: int = 0) -> List[str]:
    """Extract printable strings from nested protobuf bytes without a schema."""

    if depth > 3 or not data:
        return []
    strings: List[str] = []
    offset = 0
    try:
        while offset < len(data):
            key, offset = _decode_varint(data, offset)
            wire_type = key & 7
            if wire_type == 0:
                _, offset = _decode_varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 2:
                size, offset = _decode_varint(data, offset)
                value = data[offset:offset + size]
                if len(value) != size:
                    return strings
                offset += size
                try:
                    decoded = value.decode("utf-8")
                    if len(decoded) >= 2 and "\x00" not in decoded and any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in decoded):
                        strings.append(decoded[:240])
                except UnicodeDecodeError:
                    pass
                strings.extend(_protobuf_strings(value, depth + 1))
            elif wire_type == 5:
                offset += 4
            else:
                return strings
            if offset > len(data):
                return strings
    except ValueError:
        return strings
    return list(dict.fromkeys(strings))


def _decompress_candidates(payload: bytes) -> Iterable[bytes]:
    seen = set()
    queue = [payload]
    while queue:
        value = queue.pop(0)
        fingerprint = (len(value), value[:32])
        if fingerprint in seen or not value:
            continue
        seen.add(fingerprint)
        yield value
        decoders = [gzip.decompress, zlib.decompress]
        if brotli is not None:
            decoders.insert(0, brotli.decompress)
        for decoder in decoders:
            try:
                decoded = decoder(value)
            except Exception:
                continue
            if decoded and decoded != value:
                queue.append(decoded)


def _method_type(method: str) -> str:
    value = str(method or "").lower()
    if any(token in value for token in ("chat", "comment", "screenmessage", "emoji")):
        return "comment"
    if "gift" in value:
        return "gift"
    if "like" in value:
        return "like"
    if any(token in value for token in ("follow", "social", "fansclub")):
        return "follow"
    if "share" in value:
        return "share"
    if any(token in value for token in ("userseq", "roomstats", "roomrank", "online", "viewer")):
        return "viewer_change"
    if any(token in value for token in ("control", "roommessage", "finish", "close")):
        return "live_status"
    return ""


def _first_value(payload: Dict[str, Any], keys: Sequence[str], default: Any = "") -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


def _candidate_from_dict(payload: Dict[str, Any], method: str, source: str) -> Optional[Dict[str, Any]]:
    nested_user = _first_value(payload, ("user", "user_info", "author", "from_user"), {})
    if not isinstance(nested_user, dict):
        nested_user = {}
    inferred = _method_type(method or _first_value(payload, ("type", "event", "cmd", "method"), ""))
    event_type = inferred or str(_first_value(payload, ("event_type", "eventType"), ""))
    content = _first_value(payload, ("content", "comment", "text", "message", "common_text", "description"), "")
    user_id = _first_value(payload, ("user_id", "userId", "uid", "id"), _first_value(nested_user, ("user_id", "id", "uid"), ""))
    user_name = _first_value(payload, ("user_name", "userName", "nickname", "uname"), _first_value(nested_user, ("nickname", "name", "display_name"), "匿名用户"))
    metadata: Dict[str, Any] = {"source": source}
    if method:
        metadata["method"] = str(method)[:120]
    explicit_id = _first_value(payload, ("event_id", "eventId", "msg_id", "message_id"), "")
    if explicit_id:
        metadata["event_id"] = str(explicit_id)
    if event_type == "gift":
        gift_name = _first_value(payload, ("gift_name", "giftName", "name"), "礼物")
        gift_count = safe_int(_first_value(payload, ("count", "num", "repeat_count"), 1), 1)
        content = "%s × %s" % (gift_name, gift_count)
        metadata["gift_name"] = str(gift_name)[:120]
        metadata["gift_count"] = gift_count
    if event_type == "viewer_change":
        content = "在线人数 %s" % safe_int(_first_value(payload, ("online", "user_count", "total", "num", "count"), 0))
        metadata["online"] = safe_int(_first_value(payload, ("online", "user_count", "total", "num", "count"), 0))
    if event_type == "live_status":
        content = str(content or _first_value(payload, ("status", "status_text"), "直播状态变化"))
    if not event_type:
        return None
    return {"type": event_type, "user_id": user_id, "user_name": user_name, "content": content, "metadata": metadata}


def _walk_json(payload: Any, source: str, method: str = "") -> Iterable[Dict[str, Any]]:
    if isinstance(payload, dict):
        local_method = str(_first_value(payload, ("method", "event", "cmd", "type"), method) or method)
        candidate = _candidate_from_dict(payload, local_method, source)
        if candidate:
            yield candidate
        for key, value in payload.items():
            if key not in ("metadata", "raw", "payload"):
                yield from _walk_json(value, source, local_method)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_json(value, source, method)


class DouyinPublicAdapter:
    def __init__(self, room_url: str, cookie: str = "", mode: str = "auto") -> None:
        self.room_url = room_url
        self.cookie = cookie
        self.mode = mode if mode in ("auto", "playwright", "demo") else "auto"
        self._seen_dom = set()

    def run(self, stop_event: threading.Event, emit: Callable[[Dict[str, Any]], None], on_state: Callable[[str, str], None]) -> None:
        if self.mode == "demo":
            self._run_demo(stop_event, emit, on_state)
            return
        self._run_playwright(stop_event, emit, on_state)

    def _run_demo(self, stop_event: threading.Event, emit: Callable[[Dict[str, Any]], None], on_state: Callable[[str, str], None]) -> None:
        on_state("connected", "MVP 演示直播间")
        emit({"type": "live_status", "content": "直播已开始", "metadata": {"source": "demo", "status": "online"}})
        samples = [
            ("comment", "观众A", "这个多少钱？"),
            ("comment", "观众B", "怎么买，链接在哪里？"),
            ("comment", "观众C", "支持油皮吗，效果怎么样"),
            ("like", "观众D", "点赞"),
            ("follow", "观众E", "关注了主播"),
            ("comment", "观众F", "发货要几天？"),
            ("gift", "观众G", "小心心 × 1", {"gift_name": "小心心", "gift_count": 1}),
            ("share", "观众H", "分享了直播间"),
            ("comment", "观众I", "物流太慢了，有点失望"),
        ]
        index = 0
        while not stop_event.wait(1.1):
            item = samples[index % len(samples)]
            metadata = item[3] if len(item) > 3 else {"source": "demo"}
            emit({"type": item[0], "user_name": item[1], "content": item[2], "metadata": metadata})
            if index % 5 == 0:
                emit({"type": "viewer_change", "content": str(1280 + index * 7), "metadata": {"source": "demo", "online": 1280 + index * 7}})
            index += 1
        emit({"type": "live_status", "content": "采集已停止", "metadata": {"source": "demo", "status": "stopped"}})

    def _run_playwright(self, stop_event: threading.Event, emit: Callable[[Dict[str, Any]], None], on_state: Callable[[str, str], None]) -> None:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as error:
            raise AdapterError("真实模式需要 Playwright：pip install playwright && playwright install chromium") from error

        headless = os.environ.get("DOUYIN_HEADLESS", "1").lower() not in ("0", "false", "no")
        profile_dir = os.environ.get("DOUYIN_PROFILE_DIR", "").strip()
        executable_path = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
        with sync_playwright() as playwright:
            launch_kwargs: Dict[str, Any] = {"headless": headless}
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            if profile_dir:
                context = playwright.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
            else:
                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(service_workers="block", user_agent=USER_AGENT)
            try:
                cookies = _cookie_pairs(self.cookie)
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()

                def handle_frame(payload: Any) -> None:
                    for candidate in self._parse_payload(payload, "websocket"):
                        emit(candidate)

                def handle_websocket(websocket: Any) -> None:
                    if "douyin" not in str(websocket.url).lower():
                        return
                    websocket.on("framereceived", handle_frame)

                def handle_response(response: Any) -> None:
                    try:
                        if response.request.resource_type not in ("xhr", "fetch"):
                            return
                        content_type = str(response.headers.get("content-type", ""))
                        if "json" not in content_type:
                            return
                        body = response.body()
                        for candidate in self._parse_payload(body, "response"):
                            emit(candidate)
                    except Exception:
                        return

                page.on("websocket", handle_websocket)
                page.on("response", handle_response)
                try:
                    page.goto(self.room_url, wait_until="domcontentloaded", timeout=60000)
                except PlaywrightTimeoutError:
                    # The page can still have an active live socket after a document timeout.
                    pass
                on_state("connected", page.title() or "抖音直播间")
                emit({"type": "live_status", "content": "页面已打开，等待实时事件", "metadata": {"source": "playwright", "status": "online"}})
                while not stop_event.wait(2.0):
                    self._read_dom_fallback(page, emit)
                    try:
                        body_text = page.locator("body").inner_text(timeout=700)
                        if any(marker in body_text for marker in ("直播已结束", "主播暂时离开", "直播间不存在")):
                            emit({"type": "live_status", "content": "直播状态：已结束或暂时不可用", "metadata": {"source": "dom", "status": "offline"}})
                            on_state("offline", "直播已结束")
                            break
                    except Exception:
                        continue
            finally:
                context.close()
                if not profile_dir:
                    browser.close()

    def _parse_payload(self, payload: Any, source: str) -> Iterable[Dict[str, Any]]:
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                return []
            return _walk_json(decoded, source)
        if not isinstance(payload, (bytes, bytearray)):
            return []
        candidates: List[Dict[str, Any]] = []
        for data in _decompress_candidates(bytes(payload)):
            try:
                text = data.decode("utf-8")
                decoded = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if decoded is not None:
                candidates.extend(_walk_json(decoded, source))
            strings = _protobuf_strings(data)
            method = next((item for item in strings if _method_type(item)), "")
            event_type = _method_type(method)
            if event_type:
                useful = [
                    item for item in strings
                    if item != method
                    and len(item) <= 240
                    and not any(token in item.lower() for token in ("http://", "https://", "wss://", "token", "signature", "sessionid", "cookie", "access_key"))
                    and (any("\u4e00" <= char <= "\u9fff" for char in item) or "?" in item or "？" in item)
                ]
                content = max(useful, key=len, default="")
                candidates.append({
                    "type": event_type,
                    "user_name": "匿名用户",
                    "content": content or ("在线人数变化" if event_type == "viewer_change" else ""),
                    "metadata": {"source": source, "method": method[:120], "frame_size": len(data)},
                })
        return candidates

    def _read_dom_fallback(self, page: Any, emit: Callable[[Dict[str, Any]], None]) -> None:
        try:
            values = page.evaluate(
                """() => Array.from(document.querySelectorAll('[class*="comment"], [class*="Comment"], [data-e2e*="comment"]'))
                .map(node => (node.innerText || node.textContent || '').trim())
                .filter(Boolean).slice(-20)"""
            )
        except Exception:
            return
        for value in values if isinstance(values, list) else []:
            text = re.sub(r"\s+", " ", str(value)).strip()
            if not text or text in self._seen_dom or len(text) > 240:
                continue
            self._seen_dom.add(text)
            emit({"type": "comment", "user_name": "页面可见用户", "content": text, "metadata": {"source": "dom"}})


class DouyinCollector:
    provider_name = "douyin"

    def __init__(self, db_path: Path, mode: str = "auto") -> None:
        self.store = LiveEventStore(db_path)
        self.signals = SignalEngine()
        self.mode = mode
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.session_id: Optional[int] = None
        self.last_session_id: Optional[int] = None
        self.room_id = ""
        self.room_url = ""
        self.room_title = ""
        self.status_name = "idle"
        self.live_status = "unknown"
        self.last_error = ""
        self.last_event_at = ""
        self.online = 0
        self.cookie = ""
        self.last_snapshot = 0.0
        self.diagnostics: Dict[str, Any] = {}

    def start(self, room_input: str, cookie: str = "", mode: Optional[str] = None) -> None:
        self.stop()
        room_id, room_url = parse_room_input(room_input)
        with self.lock:
            self.room_id, self.room_url, self.cookie = room_id, room_url, str(cookie or "")
            self.room_title = "抖音直播间 " + room_id
            self.last_error = ""
            self.last_event_at = ""
            self.online = 0
            self.live_status = "unknown"
            self.diagnostics = {"adapter": "DouyinPublicAdapter", "mode": mode or self.mode, "cookie_present": bool(self.cookie)}
            self.status_name = "connecting"
            self.last_session_id = None
            selected_mode = mode or self.mode
            self.session_id = self.store.start_session("douyin", room_id, self.room_title, room_url)
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(selected_mode,), name="douyin-collector", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            current_thread = self.thread
            session_id = self.session_id
        if current_thread and current_thread is not threading.current_thread():
            current_thread.join(timeout=3)
        if session_id:
            self.store.end_session(session_id, "stopped")
        with self.lock:
            if session_id:
                self.last_session_id = session_id
            self.thread = None
            self.session_id = None
            if self.status_name not in ("error", "offline"):
                self.status_name = "idle"

    def data_session_id(self) -> Optional[int]:
        with self.lock:
            return self.session_id or self.last_session_id

    def recent_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.store.recent_events(self.data_session_id(), limit)

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "available": True, "provider": "douyin", "connected": self.status_name == "connected",
                "status": self.status_name, "room_id": self.room_id, "room_url": self.room_url,
                "room_title": self.room_title, "session_id": self.session_id, "live_status": self.live_status,
                "online": self.online, "last_event_at": self.last_event_at, "adapter_mode": self.diagnostics.get("mode", self.mode),
                "last_error": self.last_error, "diagnostics": {key: value for key, value in self.diagnostics.items() if key != "cookie"},
            }

    def metrics(self) -> Dict[str, Any]:
        events = self.recent_events(1000)
        snapshots = self.store.recent_snapshots(self.data_session_id(), 60)
        analysis = self.signals.build(events, self.online, snapshots)
        current = self.status()
        current_window = analysis["current"]
        return {
            **current, **analysis,
            "comment_rate": current_window["comment_rate"], "like_rate": current_window["like_rate"],
            "gift_rate": current_window["gift_rate"], "active_users": current_window["active_users"],
            "heat_score": current_window["heat_score"], "purchase_ratio": current_window["purchase_ratio"],
        }

    def _run(self, mode: str) -> None:
        try:
            adapter = DouyinPublicAdapter(self.room_url, self.cookie, mode)
            adapter.run(self.stop_event, self._ingest, self._state)
        except Exception as error:
            with self.lock:
                if not self.stop_event.is_set():
                    self.status_name = "error"
                    self.last_error = str(error)
        finally:
            with self.lock:
                session_id = self.session_id
                if self.status_name == "connected":
                    self.status_name = "offline"
                self.session_id = None
                if session_id:
                    self.last_session_id = session_id
            if session_id:
                self.store.end_session(session_id, "error" if self.last_error else "stopped")

    def _state(self, name: str, title: str) -> None:
        with self.lock:
            self.status_name = name
            if title:
                self.room_title = title[:200]
            self.live_status = "online" if name == "connected" else name

    def _ingest(self, candidate: Dict[str, Any]) -> None:
        with self.lock:
            session_id = self.session_id
            room_id = self.room_id
        if not session_id:
            return
        metadata = dict(candidate.get("metadata") or {})
        if candidate.get("type") == "viewer_change":
            self.online = safe_int(metadata.get("online") or candidate.get("content"), self.online)
        event = normalize_event(room_id, candidate.get("type", "live_status"), candidate.get("user_id", ""), candidate.get("user_name", "匿名用户"), candidate.get("content", ""), metadata, candidate.get("timestamp"))
        if not self.store.insert_event(session_id, event):
            return
        with self.lock:
            self.last_event_at = event["timestamp"]
            if event["type"] == "live_status":
                self.live_status = "offline" if "结束" in event["content"] or metadata.get("status") == "offline" else "online"
        now = time.time()
        if now - self.last_snapshot >= 30:
            analysis = self.signals.build(self.recent_events(500), self.online)
            self.store.insert_snapshot(session_id, analysis["current"])
            self.last_snapshot = now
