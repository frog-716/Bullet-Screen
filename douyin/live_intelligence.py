#!/usr/bin/env python3
"""Provider-neutral event, analysis, and local persistence primitives."""

import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from schema_v4 import (
    SCHEMA_VERSION as V4_SCHEMA_VERSION,
    V4_TABLES,
    create_signal_schema,
    signal_id_for,
    verify_signal_schema,
)


EVENT_TYPES = {
    "comment",
    "like",
    "gift",
    "follow",
    "share",
    "viewer_change",
    "entry",
    "live_status",
}

TOPIC_LABELS = {
    "price": "价格",
    "purchase": "购买方式",
    "promotion": "优惠活动",
    "product": "产品效果",
    "logistics": "物流售后",
    "complaint": "负面反馈",
    "small_talk": "闲聊",
    "other": "其他",
}

INTENT_LABELS = {
    "purchase_consultation": "购买咨询",
    "price_consultation": "价格咨询",
    "product_question": "产品问题",
    "complaint": "投诉",
    "small_talk": "闲聊",
}

SENTIMENT_LABELS = {"positive": "正面", "neutral": "中性", "negative": "负面"}

STOP_WORDS = {
    "这个", "那个", "可以", "有没有", "请问", "主播", "直播", "真的", "感觉", "一下",
    "现在", "已经", "还是", "怎么", "什么", "支持", "一下子", "大家", "帮忙", "看看",
}

CHINA_TIMEZONE = timezone(timedelta(hours=8))
SCHEMA_VERSION = V4_SCHEMA_VERSION
RULE_VERSION = "rules-v2"
TRUSTED_DOYIN_CAPTURE_SOURCES = frozenset({"fetch_protobuf"})
SIGNAL_WINDOW_SECONDS = 60
SIGNAL_TRIGGER_EVENT_COUNT = 2
SIGNAL_TRIGGER_UNIQUE_USERS = 2
SIGNAL_CLEAR_EVENT_COUNT = 1
SIGNAL_CLEAR_UNIQUE_USERS = 1
SIGNAL_COOLDOWN_SECONDS = 30
CORE_SCHEMA_TABLES = {"live_sessions", "live_events", "live_metric_snapshots", "capture_gaps"}
SCHEMA_COLUMNS = {
    "live_sessions": {
        "id": ("INTEGER", 0, 1, None), "provider": ("TEXT", 1, 0, None),
        "room_id": ("TEXT", 1, 0, None), "room_title": ("TEXT", 1, 0, None),
        "room_url": ("TEXT", 1, 0, None), "started_at": ("TEXT", 1, 0, None),
        "ended_at": ("TEXT", 0, 0, None), "status": ("TEXT", 1, 0, "'running'"),
    },
    "live_events": {
        "id": ("INTEGER", 0, 1, None), "event_id": ("TEXT", 1, 0, None),
        "session_id": ("INTEGER", 1, 0, None), "provider": ("TEXT", 1, 0, None),
        "room_id": ("TEXT", 1, 0, None), "event_type": ("TEXT", 1, 0, None),
        "event_time": ("TEXT", 1, 0, None), "user_id": ("TEXT", 0, 0, None),
        "user_name": ("TEXT", 0, 0, None), "content": ("TEXT", 0, 0, None),
        "metadata_json": ("TEXT", 0, 0, None), "topic": ("TEXT", 0, 0, None),
        "intent": ("TEXT", 0, 0, None), "sentiment": ("TEXT", 0, 0, None),
        "purchase_intent": ("TEXT", 0, 0, None),
    },
    "live_metric_snapshots": {
        "id": ("INTEGER", 0, 1, None), "session_id": ("INTEGER", 1, 0, None),
        "recorded_at": ("TEXT", 1, 0, None), "online": ("INTEGER", 0, 0, "0"),
        "comment_rate": ("REAL", 0, 0, "0"), "like_rate": ("REAL", 0, 0, "0"),
        "gift_rate": ("REAL", 0, 0, "0"), "active_users": ("INTEGER", 0, 0, "0"),
        "heat_score": ("REAL", 0, 0, "0"), "purchase_ratio": ("REAL", 0, 0, "0"),
        "positive_ratio": ("REAL", 0, 0, "0"), "negative_ratio": ("REAL", 0, 0, "0"),
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
    "idx_live_events_session_time": ("live_events", ("session_id", "event_time")),
    "idx_live_events_type": ("live_events", ("session_id", "event_type")),
    "idx_live_metrics_session_time": ("live_metric_snapshots", ("session_id", "recorded_at")),
    "idx_capture_gaps_session_time": ("capture_gaps", ("session_id", "gap_start", "gap_end")),
}


class EventConflictError(ValueError):
    """The same source event identity was reused for different event facts."""


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
    unexpected_tables = tables - CORE_SCHEMA_TABLES - V4_TABLES
    if missing_tables or unexpected_tables:
        raise SchemaVersionError(
            f"Douyin schema signature mismatch: missing={sorted(missing_tables)}, unexpected={sorted(unexpected_tables)}"
        )
    for table, required in SCHEMA_COLUMNS.items():
        actual = {
            row[1]: (str(row[2]).upper(), int(row[3]), int(row[5]), _schema_default(row[4]))
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, expected in required.items():
            if name not in actual or actual[name] != expected:
                raise SchemaVersionError(f"Douyin schema signature mismatch in {table}.{name}")
    for index_name, (table, expected_columns) in SCHEMA_INDEXES.items():
        index_row = next(
            (row for row in connection.execute(f"PRAGMA index_list({table})") if row[1] == index_name),
            None,
        )
        if not index_row:
            raise SchemaVersionError(f"Douyin schema signature missing index {index_name}")
        actual_columns = tuple(row[2] for row in connection.execute(f"PRAGMA index_info({index_name})"))
        if actual_columns != expected_columns:
            raise SchemaVersionError(f"Douyin schema signature mismatch in index {index_name}")
    unique_event_id = any(
        int(row[2]) == 1 and tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]})")) == ("event_id",)
        for row in connection.execute("PRAGMA index_list(live_events)")
    )
    if not unique_event_id:
        raise SchemaVersionError("Douyin schema signature missing unique live_events.event_id")
    expected_foreign_keys = {
        "live_sessions": set(),
        "live_events": {("live_sessions", "session_id", "id")},
        "live_metric_snapshots": {("live_sessions", "session_id", "id")},
        "capture_gaps": {("live_sessions", "session_id", "id")},
    }
    for table, expected in expected_foreign_keys.items():
        actual = {(row[2], row[3], row[4]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
        if actual != expected:
            raise SchemaVersionError(f"Douyin schema signature mismatch in foreign keys for {table}")
    try:
        verify_signal_schema(connection, "live_sessions", "live_events")
    except Exception as error:
        raise SchemaVersionError(str(error)) from error


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def china_timestamp(value: Any) -> str:
    """Render stored UTC timestamps explicitly in the dashboard's China timezone."""

    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(CHINA_TIMEZONE).isoformat(timespec="milliseconds")
    except ValueError:
        return text


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def event_seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("-inf")


def parse_utc_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def evaluate_coverage_window(
    *,
    session_id: int,
    start: str,
    end: str,
    session_started_at: Optional[str],
    session_ended_at: Optional[str],
    gaps: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    snapshot_times: Sequence[Any],
) -> Dict[str, Any]:
    """Classify a complete requested interval, not merely rows inside it."""
    start_dt = parse_utc_timestamp(start)
    end_dt = parse_utc_timestamp(end)
    session_start_dt = parse_utc_timestamp(session_started_at)
    session_end_dt = parse_utc_timestamp(session_ended_at) if session_ended_at else end_dt
    if start_dt is None or end_dt is None or end_dt <= start_dt:
        raise ValueError("coverage window must contain valid ordered timestamps")

    overlapping_gaps = []
    for gap in gaps:
        gap_start = parse_utc_timestamp(gap.get("gap_start"))
        gap_end = parse_utc_timestamp(gap.get("gap_end")) if gap.get("gap_end") else None
        if gap_start is not None and gap_start < end_dt and (gap_end is None or gap_end > start_dt):
            overlapping_gaps.append(gap)

    state = "unknown"
    event_count = 0
    snapshot_count = 0
    if session_start_dt is not None and session_end_dt is not None:
        # Before a session exists or after it ended, there is no capture evidence.
        if start_dt >= session_start_dt and end_dt <= session_end_dt:
            if overlapping_gaps:
                state = "gap"
            else:
                connection_gaps = sorted(
                    (gap for gap in gaps if gap.get("reason") == "connecting"),
                    key=lambda gap: parse_utc_timestamp(gap.get("gap_start")) or datetime.max.replace(tzinfo=timezone.utc),
                )
                first_connection_end = (
                    parse_utc_timestamp(connection_gaps[0].get("gap_end")) if connection_gaps else None
                )
                if first_connection_end is not None and start_dt >= first_connection_end:
                    in_window = []
                    for event in events:
                        event_time = parse_utc_timestamp(event.get("time"))
                        if event_time is not None and start_dt <= event_time < end_dt:
                            in_window.append(event)
                    event_count = len(in_window)
                    trusted_events = sum(1 for event in in_window if event.get("trusted") is True)
                    untrusted_events = event_count - trusted_events
                    snapshot_count = sum(
                        1 for value in snapshot_times
                        if (timestamp := parse_utc_timestamp(value)) is not None and start_dt <= timestamp < end_dt
                    )
                    if trusted_events:
                        state = "reliable_with_data"
                    elif untrusted_events:
                        state = "unknown"
                    elif snapshot_count:
                        state = "reliable_no_events"

    return {
        "session_id": session_id,
        "start": start,
        "end": end,
        "coverage_state": state,
        "complete": state in {"reliable_with_data", "reliable_no_events"},
        "has_open_gap": any(gap.get("gap_end") is None for gap in overlapping_gaps),
        "event_count": event_count,
        "snapshot_count": snapshot_count,
        "gaps": overlapping_gaps,
    }


def _clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def analyze_text(content: str) -> Dict[str, Any]:
    """Conservative, deterministic Chinese-first analysis.

    This is a candidate extractor, not a probability model.  Explicit
    negation wins over a positive phrase.  Attribution/quotation is marked
    uncertain and is never promoted to a strong signal by the rule engine.
    """

    text = _clean_text(content)
    topic_rules = (
        ("price", ("多少钱", "价格", "几块", "几元", "多少米", "贵不贵")),
        ("purchase", ("怎么买", "怎么下单", "链接", "下单", "购买", "想买", "来一件")),
        ("promotion", ("优惠", "活动", "折扣", "满减", "优惠券", "券", "赠品")),
        ("product", ("效果", "成分", "怎么用", "适合", "支持油皮", "敏感肌", "尺码", "质量")),
        ("logistics", ("发货", "快递", "物流", "几天到", "售后", "退货", "退款")),
        ("complaint", ("骗人", "假的", "差评", "投诉", "失望", "垃圾", "没收到", "太慢")),
    )
    topic = "other"
    for candidate, words in topic_rules:
        if any(word in text for word in words):
            topic = candidate
            break

    negative_purchase = any(word in text for word in ("不想买", "不买", "不考虑买", "不购买", "不打算买", "不会买"))
    positive_purchase = any(word in text for word in ("怎么买", "怎么下单", "链接", "下单", "想买", "来一件", "拍一个", "马上买", "现在拍"))
    negative_recommendation = any(word in text for word in ("不推荐", "不建议", "不太推荐"))
    positive_recommendation = "推荐" in text and not negative_recommendation
    attributed = bool(
        re.search(r"(?:他说|她说|有人说|别人说|主播说|网友说|听说|据说)", text)
        or re.search(r"[“”‘’\"']", text)
    )
    uncertain = attributed

    if positive_purchase and not negative_purchase:
        intent = "purchase_consultation"
    elif any(word in text for word in ("多少钱", "价格", "几块", "几元", "多少米", "贵不贵")):
        intent = "price_consultation"
    elif topic == "complaint" or any(word in text for word in ("不满意", "有问题", "坏了")):
        intent = "complaint"
    elif topic in ("product", "logistics") or any(word in text for word in ("怎么用", "适合", "能不能")):
        intent = "product_question"
    else:
        intent = "small_talk"

    if any(word in text for word in ("骗人", "假的", "差评", "投诉", "失望", "垃圾", "没收到", "太慢", "不满意", "不好用")) and not uncertain:
        sentiment = "negative"
    elif any(word in text for word in ("喜欢", "不错", "好用", "满意", "推荐", "绝了", "支持")) and not negative_recommendation and not uncertain:
        sentiment = "positive"
    else:
        sentiment = "neutral"

    if positive_purchase and not negative_purchase and not uncertain:
        purchase_intent = "high"
    elif intent in ("price_consultation", "product_question") or topic == "promotion":
        purchase_intent = "medium"
    else:
        purchase_intent = "low"

    if negative_purchase:
        purchase_intent = "low"
        intent = "small_talk"
    if uncertain and purchase_intent == "high":
        purchase_intent = "low"
    return {
        "topic": topic,
        "intent": intent,
        "sentiment": sentiment,
        "purchase_intent": purchase_intent,
        "recommendation": "negative" if negative_recommendation else "positive" if positive_recommendation and not uncertain else "unknown",
        "uncertain": uncertain,
        "quoted_or_attributed": attributed,
        "rule_version": RULE_VERSION,
        "is_question": "true" if ("?" in text or "？" in text or intent not in ("small_talk", "complaint") and any(word in text for word in ("吗", "呢", "怎么", "多少", "能不能", "适合"))) else "false",
    }


def normalize_event(
    room_id: str,
    event_type: str,
    user_id: Any = "",
    user_name: Any = "匿名用户",
    content: Any = "",
    metadata: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
    provider: str = "douyin",
) -> Dict[str, Any]:
    normalized_type = str(event_type or "unknown").strip().lower()
    aliases = {"danmaku": "comment", "chat": "comment", "online": "viewer_change", "status": "live_status"}
    normalized_type = aliases.get(normalized_type, normalized_type)
    if normalized_type not in EVENT_TYPES:
        normalized_type = "live_status" if normalized_type in ("unknown", "") else normalized_type
    event_time = timestamp or utc_now()
    raw_metadata = dict(metadata or {})
    explicit_id = str(raw_metadata.pop("event_id", "") or "")
    sensitive_markers = ("cookie", "token", "signature", "sessionid", "access_key", "authorization", "password")
    safe_metadata: Dict[str, Any] = {}
    for key, value in raw_metadata.items():
        key_text = str(key).lower()
        value_text = str(value).lower() if isinstance(value, str) else ""
        if any(marker in key_text or marker in value_text for marker in sensitive_markers):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe_metadata[str(key)] = value
    content_text = _clean_text(content)
    uid = _clean_text(user_id, 120)
    uname = _clean_text(user_name, 120) or "匿名用户"
    identity = explicit_id or hashlib.sha1(
        "|".join((str(room_id), normalized_type, uid, uname, content_text, event_time[:19])).encode("utf-8")
    ).hexdigest()
    analysis = analyze_text(content_text) if normalized_type == "comment" else {
        "topic": "other", "intent": "small_talk", "sentiment": "neutral", "purchase_intent": "low", "is_question": "false"
    }
    return {
        "event_id": identity,
        "source_event_id": explicit_id,
        "provider": str(provider or "unknown"),
        "room_id": str(room_id),
        "timestamp": event_time,
        "type": normalized_type,
        "user_id": uid,
        "user_name": uname,
        "content": content_text,
        "metadata": safe_metadata,
        "analysis": analysis,
    }


class LiveEventStore:
    """SQLite store for the Douyin subproject's normalized events and metrics."""

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
        expected_tables = {"live_sessions", "live_events", "live_metric_snapshots", "capture_gaps"}
        if tables and version != self.SCHEMA_VERSION:
            raise SchemaVersionError(f"Douyin database schema {version} requires an explicit migration to {self.SCHEMA_VERSION}")
        if tables and not expected_tables.issubset(tables):
            raise SchemaVersionError("Douyin database schema is incomplete; refusing to write")
        if version > self.SCHEMA_VERSION:
            raise SchemaVersionError(f"Douyin database schema {version} is newer than supported {self.SCHEMA_VERSION}")
        if version != 0 and not tables:
            raise SchemaVersionError(f"Douyin database schema {version} has no matching schema; refusing to write")
        if version == self.SCHEMA_VERSION and not tables:
            raise SchemaVersionError("database declares a schema version but has no tables")
        if tables:
            verify_schema_signature(self.connection)
            return
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_sessions(
              id INTEGER PRIMARY KEY,
              provider TEXT NOT NULL,
              room_id TEXT NOT NULL,
              room_title TEXT NOT NULL,
              room_url TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              status TEXT NOT NULL DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS live_events(
              id INTEGER PRIMARY KEY,
              event_id TEXT NOT NULL UNIQUE,
              session_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              room_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              event_time TEXT NOT NULL,
              user_id TEXT,
              user_name TEXT,
              content TEXT,
              metadata_json TEXT,
              topic TEXT,
              intent TEXT,
              sentiment TEXT,
              purchase_intent TEXT,
              FOREIGN KEY(session_id) REFERENCES live_sessions(id)
            );
            CREATE TABLE IF NOT EXISTS live_metric_snapshots(
              id INTEGER PRIMARY KEY,
              session_id INTEGER NOT NULL,
              recorded_at TEXT NOT NULL,
              online INTEGER DEFAULT 0,
              comment_rate REAL DEFAULT 0,
              like_rate REAL DEFAULT 0,
              gift_rate REAL DEFAULT 0,
              active_users INTEGER DEFAULT 0,
              heat_score REAL DEFAULT 0,
              purchase_ratio REAL DEFAULT 0,
              positive_ratio REAL DEFAULT 0,
              negative_ratio REAL DEFAULT 0,
              FOREIGN KEY(session_id) REFERENCES live_sessions(id)
            );
            CREATE TABLE IF NOT EXISTS capture_gaps(
              id INTEGER PRIMARY KEY,
              provider TEXT NOT NULL,
              room_id TEXT NOT NULL,
              session_id INTEGER,
              run_id TEXT,
              gap_start TEXT NOT NULL,
              gap_end TEXT,
              reason TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'collector',
              status TEXT NOT NULL DEFAULT 'open',
              FOREIGN KEY(session_id) REFERENCES live_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_live_events_session_time ON live_events(session_id, event_time);
            CREATE INDEX IF NOT EXISTS idx_live_events_type ON live_events(session_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_live_metrics_session_time ON live_metric_snapshots(session_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_capture_gaps_session_time ON capture_gaps(session_id, gap_start, gap_end);
            """
        )
        create_signal_schema(self.connection, "live_sessions", "live_events", user_version=self.SCHEMA_VERSION)
        self.connection.commit()

    def _reconcile_interrupted_sessions(self) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE live_sessions SET ended_at=COALESCE(ended_at, ?), status='interrupted' WHERE status='running'",
                (utc_now(),),
            )
            self.connection.commit()

    def start_session(self, provider: str, room_id: str, title: str, url: str) -> int:
        with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO live_sessions(provider,room_id,room_title,room_url,started_at) VALUES(?,?,?,?,?)",
                (provider, room_id, title, url, utc_now()),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def end_session(self, session_id: int, status: str = "stopped") -> None:
        with self.lock:
            self.connection.execute("UPDATE live_sessions SET ended_at=?, status=? WHERE id=?", (utc_now(), status, session_id))
            self.connection.commit()

    def insert_event(self, session_id: int, event: Dict[str, Any]) -> bool:
        analysis = event.get("analysis") or {}
        metadata = dict(event.get("metadata") or {})
        metadata["_analysis"] = {
            "version": analysis.get("rule_version", RULE_VERSION),
            "is_question": analysis.get("is_question", "false"),
            "recommendation": analysis.get("recommendation", "unknown"),
            "uncertain": bool(analysis.get("uncertain", False)),
            "quoted_or_attributed": bool(analysis.get("quoted_or_attributed", False)),
        }
        source_event_id = str(event.get("source_event_id") or "")
        if source_event_id:
            metadata["_source_event_id"] = source_event_id
        if source_event_id:
            storage_event_id = ":".join(
                (str(event.get("provider") or "unknown"), str(event.get("room_id") or ""), str(session_id), source_event_id)
            )
        else:
            storage_event_id = f"observation:{uuid.uuid4().hex}"
        fingerprint = (
            str(event.get("provider") or "unknown"), str(event.get("room_id") or ""),
            str(event.get("type") or "unknown"), str(event.get("user_id") or ""),
            str(event.get("user_name") or ""), str(event.get("content") or ""),
        )
        with self.lock:
            existing = self.connection.execute(
                "SELECT provider,room_id,event_type,user_id,user_name,content FROM live_events WHERE event_id=?",
                (storage_event_id,),
            ).fetchone()
            if existing:
                existing_fingerprint = tuple(str(existing[key] or "") for key in ("provider", "room_id", "event_type", "user_id", "user_name", "content"))
                if existing_fingerprint != fingerprint:
                    raise EventConflictError(f"conflicting facts for source event {source_event_id}")
                return False
            cursor = self.connection.execute(
                """INSERT INTO live_events(
                event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,
                content,metadata_json,topic,intent,sentiment,purchase_intent)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    storage_event_id, session_id, event.get("provider", "douyin"), event.get("room_id", ""),
                    event.get("type", "unknown"), event.get("timestamp") or utc_now(),
                    event.get("user_id"), event.get("user_name"), event.get("content"),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), analysis.get("topic"),
                    analysis.get("intent"), analysis.get("sentiment"), analysis.get("purchase_intent"),
                ),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def insert_snapshot(self, session_id: int, metrics: Dict[str, Any]) -> None:
        with self.lock:
            self.connection.execute(
                """INSERT INTO live_metric_snapshots(
                session_id,recorded_at,online,comment_rate,like_rate,gift_rate,active_users,heat_score,
                purchase_ratio,positive_ratio,negative_ratio) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, utc_now(), None if metrics.get("online") is None else safe_int(metrics.get("online")), metrics.get("comment_rate", 0),
                    metrics.get("like_rate", 0), metrics.get("gift_rate", 0), safe_int(metrics.get("active_users")),
                    metrics.get("heat_score", 0), metrics.get("purchase_ratio", 0), metrics.get("positive_ratio", 0),
                    metrics.get("negative_ratio", 0),
                ),
            )
            self.connection.commit()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
        timestamp_utc = str(row["event_time"])
        timestamp = china_timestamp(timestamp_utc)
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        stored_analysis = metadata.get("_analysis") if isinstance(metadata.get("_analysis"), dict) else {}
        is_question = stored_analysis.get("is_question")
        if is_question not in {"true", "false"}:
            is_question = analyze_text(row["content"] or "").get("is_question", "false") if row["event_type"] == "comment" else "false"
        value_contract = None
        if row["event_type"] == "gift":
            value_contract = {
                "quantity": metadata.get("gift_count"), "quantity_unit": "item",
                "raw_platform_value": metadata.get("platform_amount"), "currency": metadata.get("currency"),
                "estimated_value": metadata.get("estimated_value"), "estimated": bool(metadata.get("estimated", False)),
                "value_semantics": metadata.get("value_semantics", "unknown"),
            }
        return {
            "event_id": row["event_id"], "provider": row["provider"], "room_id": row["room_id"],
            "_db_event_id": int(row["id"]),
            "source_event_id": metadata.get("_source_event_id", ""),
            "timestamp": timestamp, "event_time": timestamp, "timestamp_utc": timestamp_utc,
            "type": row["event_type"], "event_type": row["event_type"],
            "user": {"id": row["user_id"] or "", "name": row["user_name"] or "匿名用户"},
            "user_id": row["user_id"] or "", "user_name": row["user_name"] or "匿名用户",
            "content": row["content"] or "", "text": row["content"] or "", "metadata": metadata,
            "topic": row["topic"] or "other", "intent": row["intent"] or "small_talk",
            "sentiment": row["sentiment"] or "neutral", "purchase_intent": row["purchase_intent"] or "low",
            "is_question": is_question,
            "analysis": {
                "topic": row["topic"] or "other", "intent": row["intent"] or "small_talk",
                "sentiment": row["sentiment"] or "neutral", "purchase_intent": row["purchase_intent"] or "low", "is_question": is_question,
                "recommendation": stored_analysis.get("recommendation", "unknown"),
                "uncertain": bool(stored_analysis.get("uncertain", False)),
                "quoted_or_attributed": bool(stored_analysis.get("quoted_or_attributed", False)),
                "rule_version": stored_analysis.get("version", RULE_VERSION),
            },
            "value_contract": value_contract,
        }

    def recent_events(self, session_id: Optional[int], limit: Optional[int] = 200, since: Optional[str] = None) -> List[Dict[str, Any]]:
        limit_sql = ""
        limit_value: Optional[int] = None
        if limit is not None:
            limit_value = min(max(safe_int(limit, 200), 1), 1000)
        with self.lock:
            clauses: List[str] = []
            params: List[Any] = []
            if session_id:
                clauses.append("session_id=?")
                params.append(session_id)
            if since:
                clauses.append("event_time>=?")
                params.append(since)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            if limit_value is not None:
                limit_sql = " LIMIT ?"
                params.append(limit_value)
            rows = self.connection.execute(f"SELECT * FROM live_events{where} ORDER BY id DESC{limit_sql}", params).fetchall()
        return [self._row_to_event(row) for row in reversed(rows)]

    def recent_snapshots(self, session_id: Optional[int], limit: int = 60) -> List[Dict[str, Any]]:
        limit = min(max(safe_int(limit, 60), 1), 300)
        with self.lock:
            if session_id:
                rows = self.connection.execute(
                    "SELECT recorded_at,online,comment_rate,like_rate,gift_rate,active_users,heat_score,purchase_ratio,positive_ratio,negative_ratio FROM live_metric_snapshots WHERE session_id=? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT recorded_at,online,comment_rate,like_rate,gift_rate,active_users,heat_score,purchase_ratio,positive_ratio,negative_ratio FROM live_metric_snapshots ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            recorded_at_utc = str(item.get("recorded_at") or "")
            item["recorded_at_utc"] = recorded_at_utc
            item["recorded_at"] = china_timestamp(recorded_at_utc)
            result.append(item)
        return result

    def open_gap(
        self,
        provider: str,
        room_id: str,
        session_id: Optional[int],
        run_id: Optional[str],
        reason: str,
        started_at: Optional[str] = None,
        source: str = "collector",
    ) -> int:
        with self.lock:
            if session_id is not None and not self.connection.execute("SELECT 1 FROM live_sessions WHERE id=?", (session_id,)).fetchone():
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
            cursor = self.connection.execute(
                "UPDATE capture_gaps SET gap_end=?, status='closed' WHERE id=? AND status='open'",
                (ended_at or utc_now(), gap_id),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def close_open_gaps(
        self,
        session_id: int,
        run_id: Optional[str] = None,
        ended_at: Optional[str] = None,
        *,
        connecting_failure_reason: Optional[str] = None,
    ) -> int:
        with self.lock:
            timestamp = ended_at or utc_now()
            closed = 0
            if connecting_failure_reason:
                if run_id is None:
                    cursor = self.connection.execute(
                        "UPDATE capture_gaps SET reason=?,gap_end=?,status='closed' WHERE session_id=? AND status='open' AND reason='connecting'",
                        (connecting_failure_reason, timestamp, session_id),
                    )
                else:
                    cursor = self.connection.execute(
                        "UPDATE capture_gaps SET reason=?,gap_end=?,status='closed' WHERE session_id=? AND run_id=? AND status='open' AND reason='connecting'",
                        (connecting_failure_reason, timestamp, session_id, run_id),
                    )
                closed += cursor.rowcount
            if run_id is None:
                cursor = self.connection.execute(
                    "UPDATE capture_gaps SET gap_end=?, status='closed' WHERE session_id=? AND status='open'",
                    (timestamp, session_id),
                )
            else:
                cursor = self.connection.execute(
                    "UPDATE capture_gaps SET gap_end=?, status='closed' WHERE session_id=? AND run_id=? AND status='open'",
                    (timestamp, session_id, run_id),
                )
            closed += cursor.rowcount
            self.connection.commit()
            return closed

    def gaps(self, session_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.lock:
            if session_id is None:
                rows = self.connection.execute("SELECT * FROM capture_gaps ORDER BY id").fetchall()
            else:
                rows = self.connection.execute("SELECT * FROM capture_gaps WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [dict(row) for row in rows]

    def coverage(self, session_id: int, start: str, end: str) -> Dict[str, Any]:
        gaps = self.gaps(session_id)
        with self.lock:
            session = self.connection.execute(
                "SELECT started_at,ended_at FROM live_sessions WHERE id=?", (session_id,)
            ).fetchone()
            event_rows = self.connection.execute(
                "SELECT event_time,metadata_json FROM live_events WHERE session_id=?", (session_id,)
            ).fetchall()
            snapshot_times = [row[0] for row in self.connection.execute("SELECT recorded_at FROM live_metric_snapshots WHERE session_id=?", (session_id,))]
        events = []
        for row in event_rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            source = metadata.get("source") if isinstance(metadata, dict) else None
            events.append({"time": row["event_time"], "trusted": source in TRUSTED_DOYIN_CAPTURE_SOURCES})
        return evaluate_coverage_window(
            session_id=session_id,
            start=start,
            end=end,
            session_started_at=session["started_at"] if session else None,
            session_ended_at=session["ended_at"] if session else None,
            gaps=gaps,
            events=events,
            snapshot_times=snapshot_times,
        )

    def close(self) -> None:
        with self.lock:
            self.connection.close()


class SignalPersistenceError(RuntimeError):
    """A signal cannot be safely bound to the supplied session/evidence."""


class SignalFeedbackError(ValueError):
    """Feedback is invalid or points at a signal that does not exist."""


def _event_timestamp(event: Dict[str, Any]) -> Any:
    return event.get("timestamp_utc") or event.get("event_time") or event.get("timestamp")


def _as_utc_text(value: Any) -> str:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        raise ValueError(f"invalid UTC timestamp: {value}")
    return parsed.isoformat(timespec="milliseconds")


def _window_bounds(as_of: Any, seconds: int = SIGNAL_WINDOW_SECONDS) -> Tuple[str, str]:
    parsed = parse_utc_timestamp(as_of)
    if parsed is None:
        raise ValueError("as_of must be a valid timestamp")
    # Align windows so repeated evaluations during one bucket share one ID.
    end_epoch = int(parsed.timestamp() // seconds) * seconds
    end = datetime.fromtimestamp(end_epoch, timezone.utc)
    if parsed == end:
        window_end = end
    else:
        window_end = end + timedelta(seconds=seconds)
    window_start = window_end - timedelta(seconds=seconds)
    return window_start.isoformat(timespec="milliseconds"), window_end.isoformat(timespec="milliseconds")


def _previous_signal(previous: Optional[Sequence[Dict[str, Any]]], signal_type: str = "purchase_intent") -> Optional[Dict[str, Any]]:
    if not previous:
        return None
    if isinstance(previous, dict):
        return previous
    return next((item for item in reversed(list(previous)) if item.get("signal_type") == signal_type), None)


def _candidate_events(events: Sequence[Dict[str, Any]], signal_type: str, start: str, end: str) -> List[Dict[str, Any]]:
    start_dt = parse_utc_timestamp(start)
    end_dt = parse_utc_timestamp(end)
    if start_dt is None or end_dt is None:
        return []
    candidates: List[Dict[str, Any]] = []
    for event in events:
        event_dt = parse_utc_timestamp(_event_timestamp(event))
        if event_dt is None or not start_dt <= event_dt < end_dt or event.get("type", event.get("event_type")) != "comment":
            continue
        analysis = event.get("analysis") or {}
        if analysis.get("uncertain") or analysis.get("quoted_or_attributed"):
            continue
        if signal_type == "purchase_intent" and analysis.get("purchase_intent") == "high":
            candidates.append(event)
        elif signal_type == "recommendation" and analysis.get("recommendation") == "positive":
            candidates.append(event)
    return candidates


def _user_key(event: Dict[str, Any]) -> str:
    return str(event.get("user_id") or event.get("user", {}).get("id") or event.get("user_name") or event.get("user", {}).get("name") or "anonymous")


def _signal_reason(signal_type: str, event_count: int, unique_user_count: int, coverage: str, status: str) -> str:
    label = "购买意图" if signal_type == "purchase_intent" else "正向推荐"
    base = f"最近 {SIGNAL_WINDOW_SECONDS} 秒内 {unique_user_count} 个独立用户的 {event_count} 条消息命中{label}规则。"
    if coverage != "reliable_with_data":
        return f"coverage={coverage}；{base}采集证据不足，不升级为强信号。"
    if status == "cleared":
        return f"{base}低于触发门槛，当前信号已清除。"
    return base


def evaluate_signal_window(
    events: Sequence[Dict[str, Any]],
    *,
    provider: str = "fixture",
    room_id: str = "fixture",
    session_id: int = 1,
    run_id: str = "replay",
    as_of: str,
    rule_version: str = RULE_VERSION,
    coverage: str = "reliable_with_data",
    previous: Optional[Sequence[Dict[str, Any]]] = None,
    window_seconds: int = SIGNAL_WINDOW_SECONDS,
) -> List[Dict[str, Any]]:
    """Evaluate one deterministic window without I/O or wall-clock access."""

    if rule_version != RULE_VERSION:
        raise ValueError(f"unsupported rule_version: {rule_version}")
    if coverage not in {"reliable_with_data", "reliable_no_events", "gap", "unknown"}:
        raise ValueError(f"unsupported coverage: {coverage}")
    as_of_text = _as_utc_text(as_of)
    window_start, window_end = _window_bounds(as_of_text, window_seconds)
    records: List[Dict[str, Any]] = []
    for signal_type in ("purchase_intent", "recommendation"):
        previous_signal = _previous_signal(previous, signal_type)
        candidates = _candidate_events(events, signal_type, window_start, window_end)
        effective_coverage = coverage
        if provider == "douyin":
            verified = [
                event for event in candidates
                if isinstance(event.get("metadata"), dict)
                and event["metadata"].get("source") in TRUSTED_DOYIN_CAPTURE_SOURCES
            ]
            verified_qualifies = (
                len(verified) >= SIGNAL_TRIGGER_EVENT_COUNT
                and len({_user_key(event) for event in verified}) >= SIGNAL_TRIGGER_UNIQUE_USERS
            )
            if verified_qualifies:
                candidates = verified
            else:
                # DOM and synthetic observations can be retained as weak evidence,
                # but cannot promote a Douyin signal or its coverage to reliable.
                effective_coverage = "unknown"
        event_count = len(candidates)
        unique_user_count = len({_user_key(event) for event in candidates})
        qualifies = event_count >= SIGNAL_TRIGGER_EVENT_COUNT and unique_user_count >= SIGNAL_TRIGGER_UNIQUE_USERS
        holdable = event_count >= SIGNAL_CLEAR_EVENT_COUNT and unique_user_count >= SIGNAL_CLEAR_UNIQUE_USERS
        prior_matches = previous_signal and previous_signal.get("signal_type") == signal_type
        prior_active = bool(prior_matches and previous_signal.get("status") == "active")
        prior_time = parse_utc_timestamp(previous_signal.get("as_of")) if previous_signal else None
        current_time = parse_utc_timestamp(as_of_text)
        in_cooldown = bool(prior_active and prior_time and current_time and 0 <= (current_time - prior_time).total_seconds() < SIGNAL_COOLDOWN_SECONDS)
        if not candidates and not prior_active:
            continue
        if candidates and not qualifies and not prior_active:
            # A sample below the trigger threshold is not itself a signal.
            continue
        if qualifies:
            status = "active"
            same_window = bool(previous_signal and previous_signal.get("window_end") == window_end)
            strength = "strong" if effective_coverage == "reliable_with_data" and (not in_cooldown or same_window) else "moderate" if effective_coverage == "reliable_with_data" else "weak"
        elif prior_active and (in_cooldown or holdable):
            status = "active"
            strength = "moderate" if effective_coverage == "reliable_with_data" else "weak"
        else:
            status = "cleared"
            strength = "weak"
        evidence_ids = [str(event.get("event_id") or "") for event in candidates if event.get("event_id")]
        row_ids = [int(event["_db_event_id"]) for event in candidates if event.get("_db_event_id") is not None]
        signal = {
            "signal_id": signal_id_for(provider, room_id, session_id, run_id, signal_type, rule_version, window_start, window_end),
            "provider": provider,
            "room_id": room_id,
            "session_id": session_id,
            "run_id": run_id,
            "signal_type": signal_type,
            "rule_version": rule_version,
            "created_at": as_of_text,
            "as_of": as_of_text,
            "window_start": window_start,
            "window_end": window_end,
            "coverage": effective_coverage,
            "event_count": event_count,
            "unique_user_count": unique_user_count,
            "status": status,
            "strength": strength,
            "reason": _signal_reason(signal_type, event_count, unique_user_count, effective_coverage, status),
            "evidence_event_ids": evidence_ids,
            "evidence_row_ids": row_ids,
            "cooldown": in_cooldown,
        }
        records.append(signal)
    return records


def replay_signals(
    events: Sequence[Dict[str, Any]],
    *,
    rule_version: str = RULE_VERSION,
    as_of: str,
    coverage: str = "reliable_with_data",
    provider: str = "fixture",
    room_id: str = "fixture",
    session_id: int = 1,
    run_id: str = "replay",
    previous: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    return evaluate_signal_window(
        events, provider=provider, room_id=room_id, session_id=session_id,
        run_id=run_id, as_of=as_of, rule_version=rule_version,
        coverage=coverage, previous=previous,
    )


def _event_table_name(event_table: str) -> str:
    if event_table not in {"events", "live_events"}:
        raise ValueError(f"unsupported event table: {event_table}")
    return event_table


def persist_signal(connection: sqlite3.Connection, signal: Dict[str, Any], event_table: str) -> None:
    """Persist one signal and its FK-backed evidence idempotently."""

    event_table = _event_table_name(event_table)
    row_ids = [int(value) for value in signal.get("evidence_row_ids", [])]
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        rows = connection.execute(
            f"SELECT id,session_id FROM {event_table} WHERE id IN ({placeholders})",
            row_ids,
        ).fetchall()
        valid = {int(row[0]) for row in rows if int(row[1]) == int(signal["session_id"])}
        if valid != set(row_ids):
            raise SignalPersistenceError("signal evidence is not contained in its session")
    try:
        connection.execute(
            """INSERT INTO signals(
              signal_id,provider,room_id,session_id,run_id,signal_type,rule_version,
              created_at,as_of,window_start,window_end,coverage,event_count,
              unique_user_count,status,strength,reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(signal_id) DO UPDATE SET
              created_at=excluded.created_at, as_of=excluded.as_of,
              coverage=excluded.coverage, event_count=excluded.event_count,
              unique_user_count=excluded.unique_user_count, status=excluded.status,
              strength=excluded.strength, reason=excluded.reason""",
            (
                signal["signal_id"], signal["provider"], signal["room_id"], signal["session_id"], signal["run_id"],
                signal["signal_type"], signal["rule_version"], signal["created_at"], signal["as_of"],
                signal["window_start"], signal["window_end"], signal["coverage"], signal["event_count"],
                signal["unique_user_count"], signal["status"], signal["strength"], signal["reason"],
            ),
        )
        for row_id in row_ids:
            connection.execute("INSERT OR IGNORE INTO signal_evidence(signal_id,event_id) VALUES(?,?)", (signal["signal_id"], row_id))
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise SignalPersistenceError(str(error)) from error


def _public_evidence_row(row: sqlite3.Row, event_table: str, provider: str) -> Dict[str, Any]:
    if event_table == "live_events":
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        stable_id = str(metadata.get("_source_event_id") or row["stable_event_id"] or "")
        event_type = row["event_type"]
        event_time = row["event_time"]
        user = row["user_name"] or "匿名用户"
        summary = row["content"] or event_type
    else:
        stable_id = f"{provider}:event:{row['id']}"
        event_type = row["event_type"]
        event_time = row["event_time"]
        user = row["uname"] or "匿名用户"
        summary = row["text"] or row["gift_name"] or event_type
    return {"event_id": stable_id, "event_type": event_type, "event_time": event_time, "user": user, "summary": str(summary)[:240]}


def load_signals(
    connection: sqlite3.Connection,
    session_id: int,
    provider: str,
    room_id: str,
    run_id: str,
    event_table: str,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    event_table = _event_table_name(event_table)
    window_filter = ""
    params: List[Any] = [session_id, provider, room_id, run_id]
    if window_start is not None and window_end is not None:
        window_filter = " AND window_start=? AND window_end=?"
        params.extend((window_start, window_end))
    rows = connection.execute(
        f"SELECT * FROM signals WHERE session_id=? AND provider=? AND room_id=? AND run_id=?{window_filter} ORDER BY window_start,signal_type",
        params,
    ).fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if event_table == "live_events":
            query = """SELECT e.id,e.event_id AS stable_event_id,e.event_type,e.event_time,e.user_name,e.content,e.metadata_json
                       FROM signal_evidence se JOIN live_events e ON e.id=se.event_id
                       WHERE se.signal_id=? AND e.session_id=? ORDER BY e.id"""
        else:
            query = """SELECT e.id,e.event_type,e.event_time,e.uname,e.text,e.gift_name
                       FROM signal_evidence se JOIN events e ON e.id=se.event_id
                       WHERE se.signal_id=? AND e.session_id=? ORDER BY e.id"""
        evidence_rows = connection.execute(query, (row["signal_id"], session_id)).fetchall()
        item["evidence"] = [_public_evidence_row(evidence, event_table, provider) for evidence in evidence_rows]
        feedback = connection.execute(
            "SELECT feedback_type,COUNT(*) AS count FROM signal_feedback WHERE signal_id=? GROUP BY feedback_type",
            (row["signal_id"],),
        ).fetchall()
        item["feedback"] = {feedback_row["feedback_type"]: int(feedback_row["count"]) for feedback_row in feedback}
        result.append(item)
    return result


def add_signal_feedback(connection: sqlite3.Connection, signal_id: str, feedback_type: str, note: Optional[str] = None, created_at: Optional[str] = None) -> Dict[str, Any]:
    if feedback_type not in {"useful", "false_positive", "note"}:
        raise SignalFeedbackError("unsupported feedback type")
    if feedback_type == "note" and not str(note or "").strip():
        raise SignalFeedbackError("note feedback requires a note")
    if not connection.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone():
        raise SignalFeedbackError("signal does not exist")
    record = {
        "feedback_id": "fb_" + uuid.uuid4().hex,
        "signal_id": signal_id,
        "feedback_type": feedback_type,
        "note": str(note).strip() if note is not None else None,
        "created_at": _as_utc_text(created_at or utc_now()),
    }
    try:
        connection.execute(
            "INSERT INTO signal_feedback(feedback_id,signal_id,feedback_type,note,created_at) VALUES(?,?,?,?,?)",
            tuple(record[key] for key in ("feedback_id", "signal_id", "feedback_type", "note", "created_at")),
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise SignalFeedbackError(str(error)) from error
    return record


class SignalEngine:
    def __init__(self, analyzer=analyze_text) -> None:
        self.analyzer = analyzer

    @staticmethod
    def _window(events: Sequence[Dict[str, Any]], seconds: int, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
        cutoff = event_seconds(as_of) - seconds if as_of else time.time() - seconds
        end = event_seconds(as_of) if as_of else float("inf")
        return [event for event in events if cutoff <= event_seconds(_event_timestamp(event)) < end]

    @staticmethod
    def _rate(count: int, seconds: int) -> float:
        return round(count * 60 / max(seconds, 1), 1)

    @staticmethod
    def _ratio(count: int, total: int) -> float:
        return round(count / total, 3) if total else 0.0

    def build(
        self,
        events: Sequence[Dict[str, Any]],
        online: Optional[int] = None,
        snapshots: Optional[Sequence[Dict[str, Any]]] = None,
        *,
        provider: str = "fixture",
        room_id: str = "fixture",
        session_id: Optional[int] = None,
        run_id: str = "replay",
        as_of: Optional[str] = None,
        coverage: str = "reliable_with_data",
        previous: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        recent = list(events)
        reference_time = as_of or utc_now()
        windows: Dict[str, Dict[str, Any]] = {}
        for seconds, label in ((10, "10s"), (60, "60s"), (300, "5m")):
            window = self._window(recent, seconds, reference_time)
            comments = [event for event in window if event.get("type") == "comment"]
            likes = sum(1 for event in window if event.get("type") == "like")
            gifts = sum(1 for event in window if event.get("type") == "gift")
            follows = sum(1 for event in window if event.get("type") == "follow")
            shares = sum(1 for event in window if event.get("type") == "share")
            interaction_types = {"comment", "like", "gift", "follow", "share"}
            users = {
                str(event.get("user_id") or event.get("user_name") or "")
                for event in window
                if event.get("type") in interaction_types
            }
            high_purchase = sum(1 for event in comments if event.get("purchase_intent") == "high")
            medium_purchase = sum(1 for event in comments if event.get("purchase_intent") == "medium")
            positive = sum(1 for event in comments if event.get("sentiment") == "positive")
            negative = sum(1 for event in comments if event.get("sentiment") == "negative")
            heat = comments.__len__() * 1.0 + likes * 0.08 + gifts * 3.0 + follows * 0.5 + shares * 0.8 + len(users) * 0.2
            windows[label] = {
                "seconds": seconds, "comments": len(comments), "likes": likes, "gifts": gifts,
                "follows": follows, "shares": shares, "active_users": len(users), "online": online,
                "comment_rate": self._rate(len(comments), seconds), "like_rate": self._rate(likes, seconds),
                "gift_rate": self._rate(gifts, seconds), "purchase_ratio": self._ratio(high_purchase + medium_purchase, len(comments)),
                "high_purchase_ratio": self._ratio(high_purchase, len(comments)), "positive_ratio": self._ratio(positive, len(comments)),
                "negative_ratio": self._ratio(negative, len(comments)), "heat_score": round(heat, 1),
            }

        five_minute = self._window(recent, 300, reference_time)
        comments_5m = [event for event in five_minute if event.get("type") == "comment"]
        topics = Counter(event.get("topic", "other") for event in comments_5m)
        questions = Counter(re.sub(r"[\s！？?!。,.，、]+", "", str(event.get("content") or "")) for event in comments_5m if event.get("content") and (event.get("is_question") == "true" or "?" in str(event.get("content")) or "？" in str(event.get("content"))))
        keywords: Counter = Counter()
        for event in comments_5m:
            for token in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,}", str(event.get("content") or "")):
                if token not in STOP_WORDS:
                    keywords[token] += 1

        signals: List[Dict[str, Any]] = []
        if session_id is not None:
            signals = evaluate_signal_window(
                recent, provider=provider, room_id=room_id, session_id=session_id,
                run_id=run_id, as_of=reference_time, coverage=coverage, previous=previous,
            )
        if not signals and session_id is None:
            signals = []
        if signals:
            legacy_signals = signals
        else:
            legacy_signals = []
        current = windows["60s"]
        if session_id is None:
            if current["high_purchase_ratio"] >= 0.2 and current["comments"] >= 2:
                legacy_signals.append({"level": "opportunity", "title": "购买意图集中", "detail": "价格、下单或购买方式相关问题正在出现。", "recommendation": "主播重复价格、优惠和下单路径。"})
            if current["negative_ratio"] >= 0.25 and current["comments"] >= 3:
                legacy_signals.append({"level": "risk", "title": "负面情绪升高", "detail": "负面评论占比超过最近评论的四分之一。", "recommendation": "优先回应物流、售后或产品疑虑，并给出明确处理时效。"})
        if questions:
            top_question = questions.most_common(1)[0][0]
            if top_question:
                if session_id is None:
                    legacy_signals.append({"level": "attention", "title": "观众问题集中", "detail": "高频问题：" + top_question, "recommendation": "把该问题整理成一句固定口播或屏幕贴片。"})

        return {
            "windows": windows,
            "current": current,
            "topics": [{"topic": key, "label": TOPIC_LABELS.get(key, key), "count": value} for key, value in topics.most_common(6)],
            "keywords": [[key, value] for key, value in keywords.most_common(12)],
            "hot_questions": [{"question": key, "count": value} for key, value in questions.most_common(5) if key],
            "signals": legacy_signals,
            "summary": {
                "top_topics": [TOPIC_LABELS.get(key, key) for key, _ in topics.most_common(3)],
                "purchase_intent": current["purchase_ratio"],
                "sentiment": {"positive": current["positive_ratio"], "neutral": round(max(0, 1 - current["positive_ratio"] - current["negative_ratio"]), 3), "negative": current["negative_ratio"]},
            },
            "trend": list(snapshots or []),
        }
