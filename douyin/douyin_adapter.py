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
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from live_intelligence import EventConflictError, LiveEventStore, SignalEngine, _window_bounds, china_timestamp, load_signals, normalize_event, persist_signal, safe_int, utc_now

try:
    import brotli  # type: ignore
except ImportError:  # pragma: no cover - optional runtime capability
    brotli = None


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent / "data" / "browser-profile"
# Event quietness is intentionally separate from protocol health.  The former
# is a user-facing activity hint; the latter decides whether a capture gap is
# warranted.
EVENT_QUIET_AFTER_SECONDS = 45.0
PROTOCOL_STALE_AFTER_SECONDS = 120.0
# Compatibility alias for callers that still display the old event timeout.
FRESHNESS_TIMEOUT_SECONDS = EVENT_QUIET_AFTER_SECONDS
MAX_RAW_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_DECODED_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_DECOMPRESSION_LAYERS = 4
MAX_DECOMPRESSION_CANDIDATES = 16
MAX_PROTO_NODES = 4096
MAX_PROTOCOL_MESSAGES = 512
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_SEEN_CACHE = 4096
SEEN_CACHE_TTL_SECONDS = 60 * 60
PROTOCOL_DIAGNOSTIC_COUNTERS = (
    "protocol_responses_total",
    "protocol_responses_valid",
    "protocol_responses_empty",
    "protocol_responses_unknown_only",
    "protocol_responses_malformed",
    "protocol_responses_source_rejected",
    "protocol_responses_with_events",
    "protocol_messages_total",
    "protocol_events_emitted",
)
PROTOCOL_DIAGNOSTIC_TIMESTAMPS = (
    "last_protocol_response_at",
    "last_empty_envelope_at",
    "last_event_response_at",
)
PROTOCOL_MALFORMED_STAGES = (
    "http_status",
    "content_type",
    "body_empty",
    "body_read",
    "envelope_decode",
    "envelope_structure",
    "message_decode",
    "other",
)
PROTOCOL_DIAGNOSTIC_FIELDS = (
    *PROTOCOL_DIAGNOSTIC_COUNTERS,
    *PROTOCOL_DIAGNOSTIC_TIMESTAMPS,
    "protocol_http_status_counts",
    "protocol_content_type_counts",
    "protocol_content_encoding_counts",
    "protocol_malformed_stages",
    "protocol_decode_success",
    "protocol_decode_failure",
)


def new_protocol_diagnostics() -> Dict[str, Any]:
    return {
        **{key: 0 for key in PROTOCOL_DIAGNOSTIC_COUNTERS},
        **{key: "" for key in PROTOCOL_DIAGNOSTIC_TIMESTAMPS},
        "protocol_http_status_counts": {},
        "protocol_content_type_counts": {},
        "protocol_content_encoding_counts": {},
        "protocol_malformed_stages": {key: 0 for key in PROTOCOL_MALFORMED_STAGES},
        "protocol_decode_success": 0,
        "protocol_decode_failure": 0,
    }


def stale_gap_start(last_valid_at: str, freshness_seconds: float) -> str:
    try:
        parsed = datetime.fromisoformat(str(last_valid_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed.astimezone(timezone.utc) + timedelta(seconds=freshness_seconds)).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OverflowError):
        return utc_now()


def optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AdapterError(RuntimeError):
    pass


class BusyError(RuntimeError):
    pass


class SnapshotUnstableError(RuntimeError):
    pass


def snapshot_window_seconds(value: Any) -> int:
    text = str(value or "300s").strip().lower()
    if text.endswith("m"):
        seconds = safe_int(text[:-1], 5) * 60
    elif text.endswith("s"):
        seconds = safe_int(text[:-1], 300)
    else:
        seconds = safe_int(text, 300)
    return min(max(seconds, 10), 24 * 60 * 60)


def snapshot_window(as_of: str, seconds: int) -> Tuple[str, str]:
    try:
        end = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        end = end.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        end = datetime.now(timezone.utc)
    return (
        (end - timedelta(seconds=seconds)).isoformat(timespec="milliseconds"),
        end.isoformat(timespec="milliseconds"),
    )


class ParseBudgetExceeded(ValueError):
    pass


@dataclass(frozen=True)
class RunContext:
    generation: int
    run_id: str
    provider: str
    room_id: str
    room_url: str = field(repr=False)
    cookie: str = field(repr=False)
    mode: str
    cancel: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


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


def is_douyin_resource_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(str(value or ""))
    if parsed.scheme not in ("https", "wss"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return host == "douyin.com" or host.endswith(".douyin.com")


def is_douyin_protocol_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(str(value or ""))
    return is_douyin_resource_url(value) and parsed.path.startswith(("/webcast", "/aweme/"))


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


def _protobuf_strings(data: bytes, depth: int = 0, _budget: Optional[Dict[str, int]] = None) -> List[str]:
    """Extract printable strings from nested protobuf bytes without a schema."""

    budget = _budget or {"nodes": 0}
    if depth > 3 or not data:
        return []
    if len(data) > MAX_RAW_PAYLOAD_BYTES:
        raise ParseBudgetExceeded("protobuf input exceeds size budget")
    strings: List[str] = []
    offset = 0
    try:
        while offset < len(data):
            budget["nodes"] += 1
            if budget["nodes"] > MAX_PROTO_NODES:
                raise ParseBudgetExceeded("protobuf node budget exceeded")
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
                strings.extend(_protobuf_strings(value, depth + 1, budget))
            elif wire_type == 5:
                offset += 4
            else:
                return strings
            if offset > len(data):
                return strings
    except ValueError:
        return strings
    return list(dict.fromkeys(strings))


def _protobuf_fields(data: bytes, _budget: Optional[Dict[str, int]] = None) -> List[Tuple[int, int, Any]]:
    """Decode protobuf wire fields without importing generated schemas."""

    if len(data) > MAX_RAW_PAYLOAD_BYTES:
        raise ParseBudgetExceeded("protobuf input exceeds size budget")
    budget = _budget or {"nodes": 0}
    fields: List[Tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        key, offset = _decode_varint(data, offset)
        budget["nodes"] += 1
        if budget["nodes"] > MAX_PROTO_NODES:
            raise ParseBudgetExceeded("protobuf field budget exceeded")
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
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    if 1_000_000_000_000 <= number < 10_000_000_000_000:
        seconds = number / 1000.0
    elif 1_000_000_000 <= number < 10_000_000_000:
        seconds = float(number)
    else:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _timestamp_seconds(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


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
        if len(messages) >= MAX_PROTOCOL_MESSAGES:
            raise ParseBudgetExceeded("protocol message budget exceeded")
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
        online_values = _field_values(fields, 5 if method == "WebcastRoomStatsMessage" else 3, 0)
        online = int(online_values[-1]) if online_values else None
        display_value = _field_text(fields, 2) if method == "WebcastRoomStatsMessage" else _field_text(fields, 4)
        content = str(online) if online is not None else "未知"
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


def _bounded_zlib_decompress(payload: bytes, wbits: int) -> bytes:
    decoder = zlib.decompressobj(wbits)
    output = decoder.decompress(payload, MAX_DECODED_PAYLOAD_BYTES + 1)
    if len(output) > MAX_DECODED_PAYLOAD_BYTES or decoder.unconsumed_tail:
        raise ParseBudgetExceeded("decompressed payload exceeds size budget")
    output += decoder.flush(MAX_DECODED_PAYLOAD_BYTES + 1 - len(output))
    if len(output) > MAX_DECODED_PAYLOAD_BYTES:
        raise ParseBudgetExceeded("decompressed payload exceeds size budget")
    return output


def _bounded_brotli_decompress(payload: bytes) -> bytes:
    if brotli is None:
        raise ParseBudgetExceeded("brotli decoder is unavailable")
    decoder = brotli.Decompressor()
    output = bytearray()
    for offset in range(0, len(payload), 64 * 1024):
        output.extend(decoder.process(payload[offset:offset + 64 * 1024]))
        if len(output) > MAX_DECODED_PAYLOAD_BYTES:
            raise ParseBudgetExceeded("decompressed payload exceeds size budget")
    return bytes(output)


def _decompress_candidates(payload: bytes) -> Iterable[bytes]:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_RAW_PAYLOAD_BYTES:
        raise ParseBudgetExceeded("raw payload exceeds size budget")
    seen = set()
    queue = [(bytes(payload), 0)]
    while queue:
        value, depth = queue.pop(0)
        fingerprint = (len(value), value[:32], value[-32:])
        if fingerprint in seen or not value:
            continue
        seen.add(fingerprint)
        if len(seen) > MAX_DECOMPRESSION_CANDIDATES:
            raise ParseBudgetExceeded("too many decompression candidates")
        yield value
        if depth >= MAX_DECOMPRESSION_LAYERS:
            continue
        decoders = (lambda item: _bounded_zlib_decompress(item, 31), lambda item: _bounded_zlib_decompress(item, 15))
        if brotli is not None:
            decoders = (_bounded_brotli_decompress,) + decoders
        for decoder in decoders:
            try:
                decoded = decoder(value)
            except ParseBudgetExceeded:
                raise
            except Exception:
                continue
            if decoded and decoded != value:
                queue.append((decoded, depth + 1))


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
        online = optional_int(_first_value(payload, ("online", "user_count", "total", "num", "count"), None))
        content = "在线人数 %s" % (online if online is not None else "未知")
        metadata["online"] = online
    if event_type == "live_status":
        content = str(content or _first_value(payload, ("status", "status_text"), "直播状态变化"))
    if not event_type:
        return None
    return {"type": event_type, "user_id": user_id, "user_name": user_name, "content": content, "metadata": metadata}


def _walk_json(payload: Any, source: str, method: str = "", depth: int = 0, _budget: Optional[Dict[str, int]] = None) -> Iterable[Dict[str, Any]]:
    budget = _budget or {"nodes": 0}
    if depth > MAX_JSON_DEPTH:
        raise ParseBudgetExceeded("JSON nesting exceeds size budget")
    budget["nodes"] += 1
    if budget["nodes"] > MAX_JSON_NODES:
        raise ParseBudgetExceeded("JSON node budget exceeded")
    if isinstance(payload, dict):
        local_method = str(_first_value(payload, ("method", "event", "cmd", "type"), "") or "")
        candidate = _candidate_from_dict(payload, local_method, source) if local_method and _method_type(local_method) else None
        if candidate:
            yield candidate
        for key, value in payload.items():
            if key in ("data", "messages", "events", "items", "body"):
                yield from _walk_json(value, source, "", depth + 1, budget)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_json(value, source, "", depth + 1, budget)


class DouyinPublicAdapter:
    def __init__(self, room_url: str, cookie: str = "", mode: str = "auto") -> None:
        self.room_url = room_url
        self.room_id = parse_room_input(room_url)[0]
        self.cookie = cookie
        self.mode = mode if mode in ("auto", "playwright", "demo") else "auto"
        self._seen_dom: Dict[str, float] = {}
        self._seen_protocol: Dict[str, float] = {}
        self._recent_protocol_comments: List[Tuple[str, str]] = []
        self._protocol_dom_validated = False
        self._last_dom_online: Optional[int] = None
        self.parse_budget_drops = 0
        self.unknown_protocol_frames = 0
        self.protocol_diagnostics = new_protocol_diagnostics()

    def _protocol_stage(self, stage: str) -> None:
        stages = self.protocol_diagnostics["protocol_malformed_stages"]
        if stage in stages:
            stages[stage] += 1
            self.protocol_diagnostics["protocol_responses_malformed"] += 1

    def _protocol_header_count(self, field: str, value: Any, default: str) -> None:
        text = str(value or "").split(";", 1)[0].strip().lower()[:120] or default
        counts = self.protocol_diagnostics[field]
        counts[text] = counts.get(text, 0) + 1

    def _protocol_response_metadata(self, response: Any) -> None:
        status = safe_int(getattr(response, "status", 0), 0)
        status_key = str(status) if status else "unknown"
        status_counts = self.protocol_diagnostics["protocol_http_status_counts"]
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        headers = getattr(response, "headers", {}) or {}
        self._protocol_header_count("protocol_content_type_counts", headers.get("content-type"), "missing")
        self._protocol_header_count("protocol_content_encoding_counts", headers.get("content-encoding"), "none")
        self.protocol_diagnostics["protocol_responses_total"] += 1

    @staticmethod
    def _remember_seen(cache: Dict[str, float], value: str) -> bool:
        now = time.monotonic()
        previous = cache.get(value)
        if previous is not None and now - previous <= SEEN_CACHE_TTL_SECONDS:
            return False
        cache[value] = now
        expired = [key for key, stamp in cache.items() if now - stamp > SEEN_CACHE_TTL_SECONDS]
        for key in expired:
            cache.pop(key, None)
        while len(cache) > MAX_SEEN_CACHE:
            cache.pop(next(iter(cache)))
        return True

    def run(
        self,
        stop_event: threading.Event,
        emit: Callable[[Dict[str, Any]], None],
        on_state: Callable[[str, str], None],
        on_activity: Optional[Callable[[], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
        on_protocol: Optional[Callable[[], None]] = None,
        on_protocol_diagnostics: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        if self.mode == "demo":
            self._run_demo(stop_event, emit, on_state, on_activity, on_heartbeat, on_protocol, on_protocol_diagnostics)
            return
        self._run_playwright(stop_event, emit, on_state, on_activity, on_heartbeat, on_protocol, on_protocol_diagnostics)

    def _run_demo(
        self,
        stop_event: threading.Event,
        emit: Callable[[Dict[str, Any]], None],
        on_state: Callable[[str, str], None],
        on_activity: Optional[Callable[[], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
        on_protocol: Optional[Callable[[], None]] = None,
        on_protocol_diagnostics: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
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
            if on_heartbeat:
                on_heartbeat()
            if on_protocol:
                on_protocol()
            if on_activity:
                on_activity()
            item = samples[index % len(samples)]
            metadata = item[3] if len(item) > 3 else {"source": "demo"}
            emit({"type": item[0], "user_name": item[1], "content": item[2], "metadata": metadata})
            if index % 5 == 0:
                emit({"type": "viewer_change", "content": str(1280 + index * 7), "metadata": {"source": "demo", "online": 1280 + index * 7}})
            index += 1
        emit({"type": "live_status", "content": "采集已停止", "metadata": {"source": "demo", "status": "stopped"}})

    def _run_playwright(
        self,
        stop_event: threading.Event,
        emit: Callable[[Dict[str, Any]], None],
        on_state: Callable[[str, str], None],
        on_activity: Optional[Callable[[], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
        on_protocol: Optional[Callable[[], None]] = None,
        on_protocol_diagnostics: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
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

                def publish_protocol_diagnostics() -> None:
                    if on_protocol_diagnostics:
                        on_protocol_diagnostics(dict(self.protocol_diagnostics))

                def handle_frame(payload: Any) -> None:
                    if not capture_enabled or not self._target_is_current(page):
                        return
                    candidates = self._parse_payload(payload, "websocket")
                    if candidates and on_activity:
                        on_activity()
                    for candidate in candidates:
                        emit(candidate)

                def handle_websocket(websocket: Any) -> None:
                    if not is_douyin_protocol_url(str(websocket.url)):
                        return
                    websocket.on("framereceived", handle_frame)

                def handle_response(response: Any) -> None:
                    try:
                        if not capture_enabled:
                            return
                        content_type = str(response.headers.get("content-type", ""))
                        parsed_url = urllib.parse.urlparse(str(response.url))
                        is_fetch_response = parsed_url.path == "/webcast/im/fetch/"
                        if not is_fetch_response:
                            return
                        self._protocol_response_metadata(response)
                        if not is_douyin_protocol_url(str(response.url)) or not self._target_is_current(page):
                            self.protocol_diagnostics["protocol_responses_source_rejected"] += 1
                            publish_protocol_diagnostics()
                            return
                        response_status = safe_int(getattr(response, "status", 0), 0)
                        if response_status < 200 or response_status >= 300:
                            self._protocol_stage("http_status")
                            publish_protocol_diagnostics()
                            return
                        try:
                            body = response.body()
                        except Exception:
                            self._protocol_stage("body_read")
                            publish_protocol_diagnostics()
                            return
                        if not body:
                            self._protocol_stage("body_empty")
                            publish_protocol_diagnostics()
                            return
                        if "protobuffer" not in content_type:
                            self._protocol_stage("content_type")
                            publish_protocol_diagnostics()
                            return
                        try:
                            decoded = _decode_im_response(body)
                        except Exception:
                            self.protocol_diagnostics["protocol_decode_failure"] += 1
                            self._protocol_stage("envelope_decode")
                            publish_protocol_diagnostics()
                            return
                        self.protocol_diagnostics["protocol_decode_success"] += 1
                        if not isinstance(decoded, dict) or not isinstance(decoded.get("messages"), list):
                            self._protocol_stage("envelope_structure")
                            publish_protocol_diagnostics()
                            return
                        messages = list(decoded["messages"])
                        self.protocol_diagnostics["protocol_responses_valid"] += 1
                        self.protocol_diagnostics["last_protocol_response_at"] = utc_now()
                        self.protocol_diagnostics["protocol_messages_total"] += len(messages)
                        if not messages:
                            self.protocol_diagnostics["protocol_responses_empty"] += 1
                            self.protocol_diagnostics["last_empty_envelope_at"] = self.protocol_diagnostics["last_protocol_response_at"]
                        if on_protocol:
                            on_protocol()
                        protocol_state["responses"] += 1
                        protocol_state["messages"] += len(messages)
                        first_response = not protocol_state["active"]
                        protocol_state["active"] = True
                        emitted_current = 0
                        for message in messages:
                            msg_id = safe_int(message.get("msg_id"))
                            if not msg_id or not self._remember_seen(self._seen_protocol, str(msg_id)):
                                continue
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
                                self._recent_protocol_comments = (self._recent_protocol_comments + [pair])[-20:]
                        if emitted_current:
                            self.protocol_diagnostics["protocol_responses_with_events"] += 1
                            self.protocol_diagnostics["protocol_events_emitted"] += emitted_current
                            self.protocol_diagnostics["last_event_response_at"] = self.protocol_diagnostics["last_protocol_response_at"]
                        elif messages:
                            self.protocol_diagnostics["protocol_responses_unknown_only"] += 1
                        if emitted_current and on_activity:
                            on_activity()
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
                        publish_protocol_diagnostics()
                        return
                        if response.request.resource_type not in ("xhr", "fetch"):
                            return
                        if "json" not in content_type:
                            return
                        if safe_int(response.headers.get("content-length"), 0) > MAX_RAW_PAYLOAD_BYTES:
                            self.parse_budget_drops += 1
                            return
                        body = response.body()
                        if len(body) > MAX_RAW_PAYLOAD_BYTES:
                            self.parse_budget_drops += 1
                            return
                        candidates = self._parse_payload(body, "response")
                        if candidates and on_activity:
                            on_activity()
                        for candidate in candidates:
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
                    if on_heartbeat:
                        on_heartbeat()
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
            if len(payload.encode("utf-8")) > MAX_RAW_PAYLOAD_BYTES:
                self.parse_budget_drops += 1
                return []
            try:
                decoded = json.loads(payload)
                return list(_walk_json(decoded, source))
            except (json.JSONDecodeError, ParseBudgetExceeded, RecursionError):
                self.parse_budget_drops += 1
                return []

        if not isinstance(payload, (bytes, bytearray)):
            return []
        if len(payload) > MAX_RAW_PAYLOAD_BYTES:
            self.parse_budget_drops += 1
            return []
        candidates: List[Dict[str, Any]] = []
        try:
            for data in _decompress_candidates(bytes(payload)):
                try:
                    text = data.decode("utf-8")
                    decoded = json.loads(text)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    self.unknown_protocol_frames += 1
                    continue
                try:
                    candidates.extend(_walk_json(decoded, source))
                except ParseBudgetExceeded:
                    self.parse_budget_drops += 1
                    return []
        except ParseBudgetExceeded:
            self.parse_budget_drops += 1
            return []
        return candidates

    def _target_is_current(self, page: Any) -> bool:
        try:
            self._assert_target_room(page)
        except AdapterError:
            return False
        return True

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
            if not text or len(text) > 240 or not self._remember_seen(self._seen_dom, identity):
                continue
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
        self.command_lock = threading.RLock()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._context: Optional[RunContext] = None
        self.generation = 0
        self.session_id: Optional[int] = None
        self.last_session_id: Optional[int] = None
        self.room_id = ""
        self.room_url = ""
        self.room_title = ""
        self.status_name = "idle"
        self.live_status = "unknown"
        self.last_error = ""
        self.last_event_at = ""
        self.last_valid_at = ""
        self.last_protocol_at = ""
        self._last_valid_monotonic = 0.0
        self._last_protocol_monotonic = 0.0
        self._connected_monotonic = 0.0
        self.online = 0
        self.online_observed = False
        self.cookie = ""
        self.last_snapshot = 0.0
        self.diagnostics: Dict[str, Any] = {}

    def start(self, room_input: str, cookie: str = "", mode: Optional[str] = None) -> RunContext:
        room_id, room_url = parse_room_input(room_input)
        requested_mode = mode or self.mode
        if self.mode == "demo":
            selected_mode = "demo"
        elif requested_mode == "demo":
            raise AdapterError("真实服务禁止写入 demo 事件；请单独使用 server.py --mode demo")
        else:
            selected_mode = requested_mode
        with self.command_lock:
            with self.lock:
                if self.thread and self.thread.is_alive():
                    raise BusyError("previous collector run is still stopping")
                self.thread = None
                self.generation += 1
                context = RunContext(self.generation, uuid.uuid4().hex, "douyin", room_id, room_url, str(cookie or ""), selected_mode)
                self._context = context
                self.room_id, self.room_url, self.cookie = context.room_id, context.room_url, context.cookie
                self.room_title = "抖音直播间 " + room_id
                self.last_error = ""
                self.last_event_at = ""
                self.last_valid_at = ""
                self.last_protocol_at = ""
                self._last_valid_monotonic = 0.0
                self._last_protocol_monotonic = 0.0
                self._connected_monotonic = 0.0
                self.online = 0
                self.online_observed = False
                self.live_status = "unknown"
                self.diagnostics = {
                    "adapter": "DouyinPublicAdapter",
                    "mode": selected_mode,
                    "cookie_present": bool(self.cookie),
                    "persistent_profile": True,
                    **new_protocol_diagnostics(),
                }
                self.status_name = "connecting"
                self.last_session_id = None
                self.session_id = self.store.start_session("douyin", room_id, self.room_title, room_url)
                self.store.open_gap("douyin", room_id, self.session_id, context.run_id, "connecting")
                self.stop_event = context.cancel
                self.thread = threading.Thread(target=self._run, args=(context,), name=f"douyin-collector-{context.generation}", daemon=True)
                self.thread.start()
                return context

    def stop(self, timeout: float = 3.0) -> bool:
        with self.command_lock:
            with self.lock:
                context, current_thread = self._context, self.thread
                if context is None or current_thread is None:
                    if context is not None:
                        self.status_name = "stopped"
                    return True
                context.cancel.set()
                self.stop_event = context.cancel
                self.status_name = "stopping"
            if current_thread is threading.current_thread():
                return False
            current_thread.join(max(0.0, timeout))
            with self.lock:
                if current_thread.is_alive():
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
            return self._context is context and not context.cancel.is_set() and self.status_name in {"connecting", "connected", "stale"}

    def _mark_protocol(self, context: RunContext) -> bool:
        with self.lock:
            if self._context is not context or context.cancel.is_set():
                return False
            now = utc_now()
            monotonic_now = time.monotonic()
            self.last_protocol_at = now
            self._last_protocol_monotonic = monotonic_now
            if self.status_name == "stale":
                self.status_name = "connected"
                self.live_status = "online"
                self.last_error = ""
            if self.session_id:
                self.store.close_open_gaps(self.session_id, context.run_id)
            return True

    def _mark_valid(self, context: RunContext) -> bool:
        with self.lock:
            if self._context is not context or context.cancel.is_set():
                return False
            now = utc_now()
            monotonic_now = time.monotonic()
            self.last_protocol_at = now
            self._last_protocol_monotonic = monotonic_now
            self.last_valid_at = now
            self._last_valid_monotonic = monotonic_now
            if self.status_name == "stale":
                self.status_name = "connected"
                self.live_status = "online"
                self.last_error = ""
            if self.session_id:
                self.store.close_open_gaps(self.session_id, context.run_id)
            return True

    def _protocol_availability_locked(self) -> str:
        total = safe_int(self.diagnostics.get("protocol_responses_total"), 0)
        if self.last_protocol_at or safe_int(self.diagnostics.get("protocol_responses_valid"), 0) > 0:
            return "true"
        if total > 0:
            return "false"
        return "unknown"

    def _expire_stale_locked(self) -> None:
        protocol_anchor = self._last_protocol_monotonic or self._connected_monotonic
        if self.status_name == "connected" and protocol_anchor and time.monotonic() - protocol_anchor > PROTOCOL_STALE_AFTER_SECONDS:
            self.status_name = "stale"
            self.live_status = "stale"
            self.last_error = "connection stale: no valid protocol response"
            context = self._context
            if context and self.session_id:
                self.store.open_gap(
                    "douyin", self.room_id, self.session_id, context.run_id, "stale",
                    started_at=stale_gap_start(self.last_protocol_at, PROTOCOL_STALE_AFTER_SECONDS),
                )

    def _activity_state_locked(self, now: Optional[float] = None) -> str:
        if self.status_name == "stale":
            return "stale"
        if self.status_name != "connected":
            return self.status_name
        if self._protocol_availability_locked() != "true":
            return "unknown"
        if not self._last_valid_monotonic:
            return "quiet"
        current = time.monotonic() if now is None else now
        return "quiet" if current - self._last_valid_monotonic > EVENT_QUIET_AFTER_SECONDS else "active"

    def _protocol_health_locked(self, now: Optional[float] = None) -> str:
        if self.status_name == "stale":
            return "stale"
        if self.status_name != "connected":
            return self.status_name
        availability = self._protocol_availability_locked()
        if availability == "false":
            return "unavailable"
        if availability == "unknown":
            return "unknown"
        anchor = self._last_protocol_monotonic or self._connected_monotonic
        if not anchor:
            return "unknown"
        current = time.monotonic() if now is None else now
        return "healthy" if current - anchor <= PROTOCOL_STALE_AFTER_SECONDS else "stale"

    def data_session_id(self) -> Optional[int]:
        with self.lock:
            return self.session_id

    def recent_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        session_id = self.data_session_id()
        return self.store.recent_events(session_id, limit) if session_id else []

    def status(self) -> Dict[str, Any]:
        with self.lock:
            self._expire_stale_locked()
            context = self._context
            connected = self.status_name == "connected" and self.session_id is not None
            activity_state = self._activity_state_locked()
            protocol_health = self._protocol_health_locked()
            protocol_available = self._protocol_availability_locked()
            return {
                "available": True, "provider": "douyin", "connected": connected,
                "status": self.status_name, "room_id": self.room_id, "room_url": self.room_url,
                "room_title": self.room_title, "session_id": self.session_id if connected else None, "generation": context.generation if context else 0, "run_id": context.run_id if context else None, "worker_alive": bool(self.thread and self.thread.is_alive()), "live_status": self.live_status,
                "online": self.online if connected and self.online_observed else None,
                "online_known": bool(connected and self.online_observed),
                "online_measure": {"kind": "online_people", "value": self.online, "unit": "people", "source": "douyin_adapter"} if connected and self.online_observed else None,
                "last_event_at": china_timestamp(self.last_event_at),
                "last_event_at_utc": self.last_event_at,
                "last_valid_at": self.last_valid_at,
                "last_protocol_at": self.last_protocol_at,
                "protocol_available": protocol_available,
                "activity_state": activity_state,
                "protocol_health": protocol_health,
                "event_quiet_after_seconds": EVENT_QUIET_AFTER_SECONDS,
                "protocol_stale_after_seconds": PROTOCOL_STALE_AFTER_SECONDS,
                "freshness_timeout_seconds": EVENT_QUIET_AFTER_SECONDS,
                "adapter_mode": self.diagnostics.get("mode", self.mode),
                "last_error": self.last_error, "diagnostics": {key: value for key, value in self.diagnostics.items() if key != "cookie"},
            }

    def metrics(self) -> Dict[str, Any]:
        current = self.status()
        session_id = self.data_session_id() if current["connected"] else None
        events = self.store.recent_events(session_id, limit=None, since=datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat()) if session_id else []
        snapshots = self.store.recent_snapshots(session_id, 60) if session_id else []
        reference = utc_now()
        coverage = {"coverage_state": "unknown"}
        if session_id and current.get("run_id"):
            start, end = snapshot_window(reference, 60)
            coverage = self.store.coverage(session_id, start, end)
        if session_id and current.get("run_id"):
            with self.store.lock:
                previous = load_signals(self.store.connection, session_id, self.provider_name, self.room_id, current["run_id"], "live_events")
        else:
            previous = []
        analysis = self.signals.build(
            events, current["online"] if current["connected"] else None, snapshots,
            provider=self.provider_name, room_id=self.room_id, session_id=session_id,
            run_id=current.get("run_id") or "inactive", as_of=reference,
            coverage=coverage.get("coverage_state", "unknown"), previous=previous,
        )
        if session_id and current.get("run_id"):
            with self.store.lock:
                for signal in analysis.get("signals", []):
                    persist_signal(self.store.connection, signal, "live_events")
                window_start, window_end = _window_bounds(reference, 60)
                analysis["signals"] = load_signals(
                    self.store.connection, session_id, self.provider_name, self.room_id,
                    current["run_id"], "live_events", window_start, window_end,
                )
        current_window = analysis["current"]
        return {
            **current, **analysis,
            "comment_rate": current_window["comment_rate"], "like_rate": current_window["like_rate"],
            "gift_rate": current_window["gift_rate"], "active_users": current_window["active_users"],
            "heat_score": current_window["heat_score"], "purchase_ratio": current_window["purchase_ratio"],
        }

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
                last_protocol_at = self.last_protocol_at or None
                activity_state = self._activity_state_locked()
                protocol_health = self._protocol_health_locked()
                protocol_available = self._protocol_availability_locked()
                as_of = utc_now()
                start, end = snapshot_window(as_of, seconds)
                coverage_end_dt = datetime.fromisoformat(end)
                coverage_end = (coverage_end_dt + timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
                identity = (
                    context.generation if context else 0,
                    context.run_id if context else None,
                    session_id,
                    room_id,
                    status_name,
                    worker_alive,
                    activity_state,
                    protocol_health,
                    last_protocol_at,
                    last_valid_at,
                    protocol_available,
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
                    self._activity_state_locked(),
                    self._protocol_health_locked(),
                    self.last_protocol_at or None,
                    self.last_valid_at or None,
                    self._protocol_availability_locked(),
                )
                if identity != after_identity:
                    continue
                metrics = {
                    **metrics,
                    "provider": self._context.provider if self._context else "douyin",
                    "room_id": room_id,
                    "session_id": session_id,
                    "run_id": context.run_id if context else None,
                    "generation": context.generation if context else 0,
                    "status": status_name,
                    "as_of": as_of,
                    "last_valid_at": last_valid_at,
                    "last_protocol_at": last_protocol_at,
                    "protocol_available": protocol_available,
                    "activity_state": activity_state,
                    "protocol_health": protocol_health,
                    "event_quiet_after_seconds": EVENT_QUIET_AFTER_SECONDS,
                    "protocol_stale_after_seconds": PROTOCOL_STALE_AFTER_SECONDS,
                }
                snapshot_signals = [{**signal, "generation": context.generation if context else 0} for signal in metrics.get("signals", [])]
                metrics["signals"] = snapshot_signals
                return {
                    "provider": "douyin",
                    "room_id": room_id,
                    "room_title": room_title,
                    "session_id": session_id,
                    "run_id": context.run_id if context else None,
                    "generation": context.generation if context else 0,
                    "status": status_name,
                    "worker_alive": worker_alive,
                    "data_source": "douyin_adapter" if session_id else "none",
                    "as_of": as_of,
                    "last_valid_at": last_valid_at,
                    "last_protocol_at": last_protocol_at,
                    "protocol_available": protocol_available,
                    "activity_state": activity_state,
                    "protocol_health": protocol_health,
                    "freshness": {
                        "state": "stale" if status_name == "stale" else activity_state,
                        "stale": status_name == "stale",
                        "activity_state": activity_state,
                        "protocol_health": protocol_health,
                        "protocol_available": protocol_available,
                        "last_protocol_at": last_protocol_at,
                        "last_valid_at": last_valid_at,
                        "event_quiet_after_seconds": EVENT_QUIET_AFTER_SECONDS,
                        "protocol_stale_after_seconds": PROTOCOL_STALE_AFTER_SECONDS,
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
                        "data_source": "douyin_adapter" if session_id else "none",
                        "count": len(events),
                    },
                    "error": self.last_error or None,
                    "diagnostics": {key: value for key, value in self.diagnostics.items() if key != "cookie"},
                }
        raise SnapshotUnstableError("collector identity changed while creating snapshot")

    def _record_heartbeat_snapshot(self, context: RunContext) -> None:
        with self.lock:
            if not self._owns_active_run(context) or not self.session_id:
                return
            session_id = self.session_id
            if time.time() - self.last_snapshot < 30:
                return
        snapshot = self.metrics().get("current")
        if not snapshot:
            return
        with self.lock:
            if self._context is not context or context.cancel.is_set() or self.session_id != session_id:
                return
            self.store.insert_snapshot(session_id, snapshot)
            self.last_snapshot = time.time()

    def _run(self, context: RunContext) -> None:
        terminal_status = "stopped"
        adapter: Optional[DouyinPublicAdapter] = None
        try:
            adapter = DouyinPublicAdapter(context.room_url, context.cookie, context.mode)
            def on_activity() -> None:
                with self.lock:
                    if self._context is context:
                        self.diagnostics.update({
                            "parse_budget_drops": adapter.parse_budget_drops,
                            "unknown_protocol_frames": adapter.unknown_protocol_frames,
                            "seen_protocol_cache_size": len(adapter._seen_protocol),
                            "seen_dom_cache_size": len(adapter._seen_dom),
                        })
            def on_protocol_diagnostics(stats: Dict[str, Any]) -> None:
                with self.lock:
                    if self._context is context:
                        self.diagnostics.update({
                            key: stats[key]
                            for key in PROTOCOL_DIAGNOSTIC_FIELDS
                            if key in stats
                        })
            def on_protocol() -> None:
                self._mark_protocol(context)
            adapter.run(
                context.cancel,
                lambda candidate: self._ingest(context, candidate),
                lambda name, title: self._state(context, name, title),
                on_activity,
                lambda: self._record_heartbeat_snapshot(context),
                on_protocol,
                on_protocol_diagnostics,
            )
        except Exception as error:
            with self.lock:
                if self._context is context and not context.cancel.is_set():
                    terminal_status = "error"
                    self.status_name = "error"
                    self.live_status = "error"
                    self.online = 0
                    self.last_error = str(error)
        finally:
            if adapter is not None:
                with self.lock:
                    if self._context is context:
                        self.diagnostics.update({
                            "parse_budget_drops": adapter.parse_budget_drops,
                            "unknown_protocol_frames": adapter.unknown_protocol_frames,
                            "seen_protocol_cache_size": len(adapter._seen_protocol),
                            "seen_dom_cache_size": len(adapter._seen_dom),
                        })
                        self.diagnostics.update(adapter.protocol_diagnostics)
            self._finish_run(context, terminal_status)

    def _finish_run(self, context: RunContext, terminal_status: str) -> None:
        with self.lock:
            owner = self._context is context
            session_id = self.session_id if owner else None
            protocol_was_verified = bool(self.last_protocol_at)
            if owner:
                if session_id:
                    self.last_session_id = session_id
                self.session_id = None
                if self.status_name in {"connecting", "connected", "stale", "stopping"}:
                    self.status_name = "error" if terminal_status == "error" else "stopped"
                    if terminal_status == "stopped":
                        self.live_status = "unknown"
                        self.online = 0
        if session_id:
            self.store.close_open_gaps(
                session_id,
                context.run_id,
                connecting_failure_reason=None if protocol_was_verified else "protocol_unavailable",
            )
            self.store.end_session(session_id, terminal_status)

    def _state(self, context: RunContext, name: str, title: str) -> None:
        with self.lock:
            if not self._owns_active_run(context):
                return
            self.status_name = name
            if title:
                self.room_title = title[:200]
            self.live_status = "online" if name == "connected" else name
            if name == "connected":
                self._connected_monotonic = time.monotonic()

    def _ingest(self, context: RunContext, candidate: Dict[str, Any]) -> None:
        with self.lock:
            if not self._owns_active_run(context):
                return
            session_id = self.session_id
            room_id = self.room_id
        if not session_id:
            return
        metadata = dict(candidate.get("metadata") or {})
        if metadata.get("source") == "fetch_protobuf":
            with self.lock:
                self.diagnostics["transport"] = "http_long_poll_protobuf"
        if candidate.get("type") == "viewer_change":
            with self.lock:
                if "online" in metadata and metadata.get("online") is not None:
                    self.online = optional_int(metadata.get("online")) or 0
                    self.online_observed = True
                elif candidate.get("content") not in (None, ""):
                    self.online = safe_int(candidate.get("content"), self.online)
                    self.online_observed = True
        event = normalize_event(room_id, candidate.get("type", "live_status"), candidate.get("user_id", ""), candidate.get("user_name", "匿名用户"), candidate.get("content", ""), metadata, candidate.get("timestamp"))
        try:
            inserted = self.store.insert_event(session_id, event)
        except EventConflictError as error:
            with self.lock:
                self.diagnostics["event_conflicts"] = safe_int(self.diagnostics.get("event_conflicts")) + 1
                self.last_error = str(error)
            return
        if not inserted:
            return
        with self.lock:
            source = str(metadata.get("source") or "")
            proves_collection = source in {"fetch_protobuf", "response", "websocket", "protocol_validation", "demo"}
            if proves_collection and (event["type"] != "live_status" or source == "demo"):
                self._mark_valid(context)
            self.last_event_at = event["timestamp"]
            if event["type"] == "live_status":
                self.live_status = "offline" if "结束" in event["content"] or metadata.get("status") == "offline" else "online"
        now = time.time()
        if now - self.last_snapshot >= 30:
            analysis = self.signals.build(self.recent_events(500), self.online)
            self.store.insert_snapshot(session_id, analysis["current"])
            self.last_snapshot = now
