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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from live_intelligence import LiveEventStore, SignalEngine, china_timestamp, normalize_event, safe_int, utc_now

try:
    import brotli  # type: ignore
except ImportError:  # pragma: no cover - optional runtime capability
    brotli = None


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent / "data" / "browser-profile"


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
    # Preserve only the public navigation hint used by links from the live
    # homepage. Arbitrary query strings may contain signed or session-scoped
    # parameters and must not be persisted.
    safe_query = []
    for key in ("show_type",):
        value = (query.get(key) or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value):
            safe_query.append((key, value))
    suffix = "?" + urllib.parse.urlencode(safe_query) if safe_query else ""
    return room_id, "https://live.douyin.com/" + room_id + suffix


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


def _protobuf_fields(data: bytes) -> List[Tuple[int, int, Any]]:
    """Decode protobuf wire fields without importing generated schemas."""

    fields: List[Tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        key, offset = _decode_varint(data, offset)
        field_number, wire_type = key >> 3, key & 7
        if not field_number:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated fixed64 field")
            value, offset = data[offset:offset + 8], offset + 8
        elif wire_type == 2:
            size, offset = _decode_varint(data, offset)
            if offset + size > len(data):
                raise ValueError("truncated bytes field")
            value, offset = data[offset:offset + size], offset + size
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated fixed32 field")
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise ValueError("unsupported protobuf wire type")
        fields.append((field_number, wire_type, value))
    return fields


def _field_values(fields: Sequence[Tuple[int, int, Any]], field_number: int, wire_type: Optional[int] = None) -> List[Any]:
    return [value for number, kind, value in fields if number == field_number and (wire_type is None or kind == wire_type)]


def _field_varint(fields: Sequence[Tuple[int, int, Any]], field_number: int, default: int = 0) -> int:
    values = _field_values(fields, field_number, 0)
    return int(values[-1]) if values else default


def _field_bytes(fields: Sequence[Tuple[int, int, Any]], field_number: int) -> bytes:
    values = _field_values(fields, field_number, 2)
    return bytes(values[-1]) if values else b""


def _field_text(fields: Sequence[Tuple[int, int, Any]], field_number: int, default: str = "") -> str:
    value = _field_bytes(fields, field_number)
    if not value:
        return default
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return default
    if "\x00" in text:
        return default
    return text[:500]


def _nested_fields(fields: Sequence[Tuple[int, int, Any]], field_number: int) -> List[Tuple[int, int, Any]]:
    value = _field_bytes(fields, field_number)
    if not value:
        return []
    try:
        return _protobuf_fields(value)
    except ValueError:
        return []


def _platform_timestamp(value: int) -> str:
    number = int(value or 0)
    if 1_000_000_000_000 <= number < 10_000_000_000_000:
        seconds = number / 1000.0
    elif 1_000_000_000 <= number < 10_000_000_000:
        seconds = float(number)
    else:
        return ""


def _timestamp_seconds(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _common_timestamp(fields: Sequence[Tuple[int, int, Any]], response_now: int = 0) -> Tuple[str, str]:
    common = _nested_fields(fields, 1)
    created_at = _platform_timestamp(_field_varint(common, 4))
    if created_at:
        return created_at, "message_common.create_time"
    observed_at = _platform_timestamp(response_now)
    return observed_at, "response.now" if observed_at else "collector_clock"


def _user_fields(fields: Sequence[Tuple[int, int, Any]], field_number: int) -> Tuple[str, str]:
    user = _nested_fields(fields, field_number)
    user_id = str(_field_varint(user, 1) or "")
    nickname = re.sub(r"\s+", " ", _field_text(user, 3, "匿名用户")).strip()[:120] or "匿名用户"
    return user_id, nickname


def _decode_im_response(payload: bytes) -> Dict[str, Any]:
    """Decode the page's `/webcast/im/fetch/` Response envelope."""

    fields = _protobuf_fields(payload)
    messages = []
    for encoded in _field_values(fields, 1, 2):
        try:
            envelope = _protobuf_fields(bytes(encoded))
        except ValueError:
            continue
        method = _field_text(envelope, 1)
        body = _field_bytes(envelope, 2)
        msg_id = _field_varint(envelope, 3)
        if method and body and msg_id:
            messages.append({"method": method, "payload": body, "msg_id": msg_id})
    return {
        "messages": messages,
        "now": _field_varint(fields, 4),
        "fetch_interval": _field_varint(fields, 3),
        "history_no_more": bool(_field_varint(fields, 12)),
    }


def _decode_im_event(message: Dict[str, Any], response_now: int = 0) -> Optional[Dict[str, Any]]:
    """Decode stable live-event fields; omit unknown payloads instead of guessing."""

    method = str(message.get("method") or "")
    msg_id = safe_int(message.get("msg_id"))
    try:
        fields = _protobuf_fields(bytes(message.get("payload") or b""))
    except ValueError:
        return None
    timestamp, timestamp_source = _common_timestamp(fields, response_now)
    metadata: Dict[str, Any] = {
        "source": "fetch_protobuf", "transport": "http_long_poll",
        "method": method[:120], "event_id": str(msg_id),
        "timestamp_source": timestamp_source, "complete": False,
    }

    event_type = ""
    user_id, user_name, content = "", "匿名用户", ""
    if method in ("WebcastChatMessage", "WebcastEmojiChatMessage"):
        event_type = "comment"
        user_id, user_name = _user_fields(fields, 2)
        content = re.sub(r"\s+", " ", _field_text(fields, 3)).strip()[:500]
        if not content:
            return None
    elif method == "WebcastMemberMessage":
        event_type = "entry"
        user_id, user_name = _user_fields(fields, 2)
        content = "进入直播间"
        metadata.update({"member_count": _field_varint(fields, 3), "enter_type": _field_varint(fields, 9)})
    elif method == "WebcastGiftMessage":
        event_type = "gift"
        user_id, user_name = _user_fields(fields, 7)
        gift = _nested_fields(fields, 15)
        gift_name = re.sub(r"\s+", " ", _field_text(gift, 16, "礼物")).strip()[:120] or "礼物"
        repeat_count = max(1, _field_varint(fields, 5), _field_varint(fields, 6))
        content = "%s × %s" % (gift_name, repeat_count)
        metadata.update({
            "gift_id": str(_field_varint(fields, 2) or _field_varint(gift, 5) or ""),
            "gift_name": gift_name, "gift_count": repeat_count,
            "repeat_end": bool(_field_varint(fields, 9)), "count_semantics": "cumulative_combo",
        })
    elif method == "WebcastLikeMessage":
        event_type = "like"
        user_id, user_name = _user_fields(fields, 5)
        count, total = _field_varint(fields, 2), _field_varint(fields, 3)
        content = "点赞 × %s" % max(1, count)
        metadata.update({"like_count": count, "like_total": total, "count_semantics": "batch_delta"})
    elif method == "WebcastSocialMessage":
        action = _field_varint(fields, 4)
        if action not in (1, 2):
            return None
        event_type = "follow" if action == 1 else "share"
        user_id, user_name = _user_fields(fields, 2)
        content = "关注了主播" if event_type == "follow" else "分享了直播间"
        metadata["action"] = action
    elif method == "WebcastFansclubMessage":
        event_type = "follow"
        user_id, user_name = _user_fields(fields, 4)
        content = re.sub(r"\s+", " ", _field_text(fields, 3, "加入或升级粉丝团")).strip()[:500]
        metadata["fansclub_action"] = _field_varint(fields, 2)
    elif method in ("WebcastRoomStatsMessage", "WebcastRoomUserSeqMessage"):
        event_type = "viewer_change"
        online = _field_varint(fields, 5) if method == "WebcastRoomStatsMessage" else _field_varint(fields, 3)
        display_value = _field_text(fields, 2) if method == "WebcastRoomStatsMessage" else _field_text(fields, 4)
        if not online:
            return None
        content = str(online)
        metadata.update({"online": online, "display_value": display_value, "semantics": "protocol_online_count"})
    elif method == "WebcastControlMessage":
        status = _field_varint(fields, 2)
        event_type = "live_status"
        content = "直播已结束" if status == 3 else "直播状态变化：%s" % status
        metadata.update({"control_status": status, "status": "offline" if status == 3 else "online"})
    else:
        return None

    return {
        "type": event_type, "user_id": user_id, "user_name": user_name,
        "content": content, "timestamp": timestamp or None, "metadata": metadata,
    }


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
        self.room_id = parse_room_input(room_url)[0]
        self.cookie = cookie
        self.mode = mode if mode in ("auto", "playwright", "demo") else "auto"
        self._seen_dom = set()
        self._seen_protocol = set()
        self._recent_protocol_comments: List[Tuple[str, str]] = []
        self._protocol_dom_validated = False
        self._last_dom_online: Optional[int] = None

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

        headless = os.environ.get("DOUYIN_HEADLESS", "0").lower() not in ("0", "false", "no")
        profile_dir = os.environ.get("DOUYIN_PROFILE_DIR", str(DEFAULT_PROFILE_DIR)).strip()
        executable_path = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
        with sync_playwright() as playwright:
            launch_kwargs: Dict[str, Any] = {
                "headless": headless,
                "args": ["--disk-cache-size=52428800", "--media-cache-size=10485760"],
            }
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            if profile_dir:
                context = playwright.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
            else:
                browser = playwright.chromium.launch(**launch_kwargs)
                browser_user_agent = os.environ.get("DOUYIN_USER_AGENT", USER_AGENT).strip() or USER_AGENT
                context_kwargs: Dict[str, Any] = {"user_agent": browser_user_agent}
                if os.environ.get("DOUYIN_BLOCK_SERVICE_WORKERS", "0").lower() in ("1", "true", "yes"):
                    context_kwargs["service_workers"] = "block"
                context = browser.new_context(**context_kwargs)
            try:
                cookies = _cookie_pairs(self.cookie)
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                self._apply_browser_compatibility(page, os.environ.get("DOUYIN_USER_AGENT", USER_AGENT).strip() or USER_AGENT)
                capture_enabled = False
                capture_started_at = 0.0
                protocol_state: Dict[str, Any] = {"active": False, "responses": 0, "messages": 0}

                def handle_frame(payload: Any) -> None:
                    if not capture_enabled:
                        return
                    for candidate in self._parse_payload(payload, "websocket"):
                        emit(candidate)

                def handle_websocket(websocket: Any) -> None:
                    if "douyin" not in str(websocket.url).lower():
                        return
                    websocket.on("framereceived", handle_frame)

                def handle_response(response: Any) -> None:
                    try:
                        if not capture_enabled:
                            return
                        content_type = str(response.headers.get("content-type", ""))
                        parsed_url = urllib.parse.urlparse(str(response.url))
                        if parsed_url.path == "/webcast/im/fetch/" and "protobuffer" in content_type:
                            decoded = _decode_im_response(response.body())
                            messages = list(decoded["messages"])
                            protocol_state["responses"] += 1
                            protocol_state["messages"] += len(messages)
                            first_response = not protocol_state["active"]
                            protocol_state["active"] = True
                            emitted_current = 0
                            for message in messages:
                                msg_id = safe_int(message.get("msg_id"))
                                if not msg_id or msg_id in self._seen_protocol:
                                    continue
                                self._seen_protocol.add(msg_id)
                                candidate = _decode_im_event(message, safe_int(decoded.get("now")))
                                if not candidate:
                                    continue
                                # A first fetch may mix history with newly
                                # observed events. Only accept platform-dated
                                # messages at/after navigation; older or
                                # timestamp-less messages remain seeded only.
                                if first_response and _timestamp_seconds(str(candidate.get("timestamp") or "")) < capture_started_at - 2.0:
                                    continue
                                emit(candidate)
                                emitted_current += 1
                                if candidate.get("type") == "comment":
                                    pair = (str(candidate.get("user_name") or ""), str(candidate.get("content") or ""))
                                    self._recent_protocol_comments = (self._recent_protocol_comments + [pair])[-100:]
                            if first_response:
                                emit({
                                    "type": "live_status",
                                    "content": "protobuf 长轮询已接通；首包按平台时间过滤历史消息",
                                    "timestamp": _platform_timestamp(safe_int(decoded.get("now"))) or None,
                                    "metadata": {
                                        "source": "fetch_protobuf", "transport": "http_long_poll",
                                        "status": "online", "seeded_messages": len(messages),
                                        "emitted_current_messages": emitted_current, "complete": False,
                                    },
                                })
                            return
                        if response.request.resource_type not in ("xhr", "fetch"):
                            return
                        if "json" not in content_type:
                            return
                        body = response.body()
                        for candidate in self._parse_payload(body, "response"):
                            emit(candidate)
                    except Exception as error:
                        protocol_state["error"] = type(error).__name__
                        return

                page.on("websocket", handle_websocket)
                page.on("response", handle_response)
                warmup_url = os.environ.get("DOUYIN_WARMUP_URL", "https://live.douyin.com/").strip()
                if warmup_url:
                    try:
                        page.goto(warmup_url, wait_until="domcontentloaded", timeout=60000)
                        warmup_wait = max(0.0, min(float(os.environ.get("DOUYIN_WARMUP_SECONDS", "8")), 30.0))
                        if stop_event.wait(warmup_wait):
                            return
                    except PlaywrightTimeoutError:
                        pass
                capture_started_at = time.time()
                capture_enabled = True
                try:
                    page.goto(self.room_url, wait_until="domcontentloaded", timeout=60000)
                except PlaywrightTimeoutError:
                    # The page can still have an active live socket after a document timeout.
                    pass
                title = page.title() or "抖音直播间"
                offline_markers = ("直播已结束", "主播暂时离开", "直播间不存在")
                ready_markers = ("在线观众", "聊天功能", "更多直播", "原画", "高清", "超清")
                initial_body = ""
                for _ in range(60):
                    try:
                        initial_body = page.locator("body").inner_text(timeout=1500)
                    except Exception:
                        initial_body = ""
                    if any(marker in initial_body for marker in offline_markers):
                        on_state("offline", title)
                        emit({"type": "live_status", "content": "直播状态：已结束或暂时不可用", "metadata": {"source": "dom", "status": "offline"}})
                        return
                    if any(marker in initial_body for marker in ready_markers):
                        break
                    if stop_event.wait(0.5):
                        return
                if not any(marker in initial_body for marker in ready_markers):
                    raise AdapterError("浏览器页面未加载直播内容；请使用 DOUYIN_HEADLESS=0 或配置 DOUYIN_PROFILE_DIR")
                self._assert_target_room(page)
                on_state("connected", title)
                emit({
                    "type": "live_status",
                    "content": "目标页面已确认；等待 protobuf 实时事件流",
                    "metadata": {"source": "playwright", "status": "online", "observed_room_id": self.room_id},
                })
                # Seed identities from the already-rendered chat backlog. Existing
                # rows predate this connection and must not inflate live rates.
                self._read_dom_fallback(page, emit, emit_existing=False)
                started_polling = time.monotonic()
                while not stop_event.wait(2.0):
                    self._assert_target_room(page)
                    if protocol_state["active"]:
                        self._read_dom_fallback(page, lambda _candidate: None)
                        match_count = self._protocol_dom_match_count(page)
                        if match_count and not self._protocol_dom_validated:
                            self._protocol_dom_validated = True
                            emit({
                                "type": "live_status",
                                "content": "协议评论已与页面可见评论完成精确对照",
                                "metadata": {
                                    "source": "protocol_validation", "status": "online",
                                    "protocol_dom_exact_matches": match_count,
                                },
                            })
                    elif time.monotonic() - started_polling >= 20:
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

    def _assert_target_room(self, page: Any) -> None:
        try:
            observed = parse_room_input(str(page.url))[0]
        except AdapterError as error:
            raise AdapterError("目标直播页已离开房间 URL，停止采集以防串房") from error
        if observed != self.room_id:
            raise AdapterError("目标直播页已跳转到其他房间，停止采集以防串房")

    def _read_dom_fallback(
        self,
        page: Any,
        emit: Callable[[Dict[str, Any]], None],
        emit_existing: bool = True,
    ) -> None:
        try:
            values = page.evaluate(
                """() => {
                  const body = document.body?.innerText || '';
                  const onlineMatch = body.match(/在线观众\\s*[·•.]?\\s*([\\d,.]+(?:万|亿)?)/);
                  const items = Array.from(document.querySelectorAll('[class*="webcast-chatroom___item"]'))
                    .filter(node => node.getClientRects().length > 0)
                    .map(node => ({
                      event_id: node.getAttribute('data-id') || '',
                      user_name: (node.querySelector('[class*="v8LY0gZF"]')?.innerText || '').trim().replace(/[：:]$/, ''),
                      content: (node.querySelector('[class*="cL385mHb"]')?.innerText || '').trim(),
                    }))
                    .filter(item => item.content)
                    .slice(-40);
                  return {online_text: onlineMatch ? onlineMatch[1] : '', items};
                }"""
            )
        except Exception:
            return
        if not isinstance(values, dict):
            return
        online_text = str(values.get("online_text") or "").strip()
        online = self._display_count(online_text)
        if online and online != self._last_dom_online:
            self._last_dom_online = online
            emit({
                "type": "viewer_change",
                "content": str(online),
                "metadata": {
                    "source": "dom",
                    "online": online,
                    "display_value": online_text,
                    "approximate": online_text.endswith(("万", "亿")),
                    "semantics": "visible_page_count",
                },
            })
        for item in values.get("items", []):
            if not isinstance(item, dict):
                continue
            text = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            user_name = re.sub(r"\s+", " ", str(item.get("user_name") or "页面可见用户")).strip() or "页面可见用户"
            event_id = re.sub(r"[^0-9A-Za-z_\-]", "", str(item.get("event_id") or ""))[:120]
            identity = event_id or "%s|%s" % (user_name, text)
            if not text or identity in self._seen_dom or len(text) > 240:
                continue
            self._seen_dom.add(identity)
            if not emit_existing:
                continue
            event_type = self._dom_event_type(text)
            metadata = {
                "source": "dom",
                "semantics": "visible_comment" if event_type == "comment" else "visible_%s_notice" % event_type,
                "complete": False,
            }
            if event_id:
                metadata["event_id"] = event_id
            emit({"type": event_type, "user_name": user_name, "content": text, "metadata": metadata})

    def _protocol_dom_match_count(self, page: Any) -> int:
        if not self._recent_protocol_comments:
            return 0
        try:
            items = page.evaluate(
                """() => Array.from(document.querySelectorAll('[class*="webcast-chatroom___item"]'))
                  .filter(node => node.getClientRects().length > 0)
                  .slice(-80)
                  .map(node => ({
                    user: (node.querySelector('[class*="v8LY0gZF"]')?.innerText || '').trim().replace(/[：:]$/, ''),
                    content: (node.querySelector('[class*="cL385mHb"]')?.innerText || '').trim(),
                  }))
                  .filter(item => item.user && item.content)"""
            )
        except Exception:
            return 0
        visible = {
            (re.sub(r"\s+", " ", str(item.get("user") or "")).strip(), re.sub(r"\s+", " ", str(item.get("content") or "")).strip())
            for item in items if isinstance(item, dict)
        }
        return len(set(self._recent_protocol_comments) & visible)

    @staticmethod
    def _dom_event_type(text: str) -> str:
        if text == "来了" or re.fullmatch(r"(?:加入了|进入了?)直播间", text):
            return "entry"
        if re.fullmatch(r"(?:为主播)?点赞了?", text):
            return "like"
        if re.match(r"^(?:送出|送出了|送了)\s*", text):
            return "gift"
        if re.fullmatch(r"关注了主播", text):
            return "follow"
        if re.fullmatch(r"分享了直播间", text):
            return "share"
        return "comment"

    @staticmethod
    def _display_count(value: Any) -> int:
        text = str(value or "").replace(",", "").strip()
        if not text:
            return 0
        multiplier = 1
        if text.endswith("万"):
            multiplier, text = 10000, text[:-1]
        elif text.endswith("亿"):
            multiplier, text = 100000000, text[:-1]
        try:
            return int(float(text) * multiplier)
        except ValueError:
            return 0

    @staticmethod
    def _apply_browser_compatibility(page: Any, user_agent: str) -> None:
        """Use optional stealth helpers when present; keep the core dependency-free."""

        try:
            from playwright_stealth import Stealth  # type: ignore
            Stealth(
                navigator_platform_override="MacIntel",
                navigator_languages_override=("zh-CN", "zh"),
                navigator_user_agent_override=user_agent,
            ).apply_stealth_sync(page)
            return
        except Exception:
            pass
        try:
            page.add_init_script(
                """Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});"""
            )
        except Exception:
            return


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
        requested_mode = mode or self.mode
        if self.mode == "demo":
            selected_mode = "demo"
        elif requested_mode == "demo":
            raise AdapterError("真实服务禁止写入 demo 事件；请单独使用 server.py --mode demo")
        else:
            selected_mode = requested_mode
        with self.lock:
            self.room_id, self.room_url, self.cookie = room_id, room_url, str(cookie or "")
            self.room_title = "抖音直播间 " + room_id
            self.last_error = ""
            self.last_event_at = ""
            self.online = 0
            self.live_status = "unknown"
            self.diagnostics = {
                "adapter": "DouyinPublicAdapter",
                "mode": selected_mode,
                "cookie_present": bool(self.cookie),
                "persistent_profile": True,
            }
            self.status_name = "connecting"
            self.last_session_id = None
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
            if session_id or current_thread:
                self.status_name = "idle"
                self.live_status = "unknown"
                self.online = 0
                self.last_event_at = ""
                self.diagnostics.pop("transport", None)
                self.diagnostics.pop("protocol_verified", None)
                self.diagnostics.pop("protocol_dom_exact_matches", None)

    def data_session_id(self) -> Optional[int]:
        with self.lock:
            return self.session_id

    def recent_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        session_id = self.data_session_id()
        return self.store.recent_events(session_id, limit) if session_id else []

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "available": True, "provider": "douyin", "connected": self.status_name == "connected",
                "status": self.status_name, "room_id": self.room_id, "room_url": self.room_url,
                "room_title": self.room_title, "session_id": self.session_id, "live_status": self.live_status,
                "online": self.online, "last_event_at": china_timestamp(self.last_event_at),
                "last_event_at_utc": self.last_event_at,
                "adapter_mode": self.diagnostics.get("mode", self.mode),
                "last_error": self.last_error, "diagnostics": {key: value for key, value in self.diagnostics.items() if key != "cookie"},
            }

    def metrics(self) -> Dict[str, Any]:
        events = self.recent_events(1000)
        session_id = self.data_session_id()
        snapshots = self.store.recent_snapshots(session_id, 60) if session_id else []
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
                    self.live_status = "error"
                    self.online = 0
                    self.last_error = str(error)
        finally:
            with self.lock:
                session_id = self.session_id
                if self.status_name == "connected":
                    self.status_name = "offline"
                    self.live_status = "offline"
                    self.online = 0
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
        if metadata.get("source") == "fetch_protobuf":
            with self.lock:
                self.diagnostics["transport"] = "http_long_poll_protobuf"
                self.diagnostics["protocol_verified"] = True
        if metadata.get("source") == "protocol_validation":
            with self.lock:
                self.diagnostics["protocol_dom_exact_matches"] = safe_int(metadata.get("protocol_dom_exact_matches"))
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
