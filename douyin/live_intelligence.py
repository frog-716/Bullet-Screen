#!/usr/bin/env python3
"""Provider-neutral event, analysis, and local persistence primitives."""

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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
        return time.time()


def _clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def analyze_text(content: str) -> Dict[str, str]:
    """Small, deterministic Chinese-first analyzer; replaceable by an LLM later."""

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

    if any(word in text for word in ("怎么买", "怎么下单", "链接", "下单", "想买", "来一件", "拍一个")):
        intent = "purchase_consultation"
    elif any(word in text for word in ("多少钱", "价格", "几块", "几元", "多少米", "贵不贵")):
        intent = "price_consultation"
    elif topic == "complaint" or any(word in text for word in ("不满意", "有问题", "坏了")):
        intent = "complaint"
    elif topic in ("product", "logistics") or any(word in text for word in ("怎么用", "适合", "能不能")):
        intent = "product_question"
    else:
        intent = "small_talk"

    if any(word in text for word in ("骗人", "假的", "差评", "投诉", "失望", "垃圾", "没收到", "太慢", "不满意")):
        sentiment = "negative"
    elif any(word in text for word in ("喜欢", "不错", "好用", "满意", "推荐", "绝了", "支持")):
        sentiment = "positive"
    else:
        sentiment = "neutral"

    if intent == "purchase_consultation" or any(word in text for word in ("马上买", "现在拍", "要两件", "下单")):
        purchase_intent = "high"
    elif intent in ("price_consultation", "product_question") or topic == "promotion":
        purchase_intent = "medium"
    else:
        purchase_intent = "low"

    return {
        "topic": topic,
        "intent": intent,
        "sentiment": sentiment,
        "purchase_intent": purchase_intent,
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

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
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
            CREATE INDEX IF NOT EXISTS idx_live_events_session_time ON live_events(session_id, event_time);
            CREATE INDEX IF NOT EXISTS idx_live_events_type ON live_events(session_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_live_metrics_session_time ON live_metric_snapshots(session_id, recorded_at);
            """
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
        metadata = event.get("metadata") or {}
        with self.lock:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO live_events(
                event_id,session_id,provider,room_id,event_type,event_time,user_id,user_name,
                content,metadata_json,topic,intent,sentiment,purchase_intent)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.get("event_id"), session_id, event.get("provider", "douyin"), event.get("room_id", ""),
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
                    session_id, utc_now(), safe_int(metrics.get("online")), metrics.get("comment_rate", 0),
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
        return {
            "event_id": row["event_id"], "provider": row["provider"], "room_id": row["room_id"],
            "timestamp": timestamp, "event_time": timestamp, "timestamp_utc": timestamp_utc,
            "type": row["event_type"], "event_type": row["event_type"],
            "user": {"id": row["user_id"] or "", "name": row["user_name"] or "匿名用户"},
            "user_id": row["user_id"] or "", "user_name": row["user_name"] or "匿名用户",
            "content": row["content"] or "", "text": row["content"] or "", "metadata": metadata,
            "topic": row["topic"] or "other", "intent": row["intent"] or "small_talk",
            "sentiment": row["sentiment"] or "neutral", "purchase_intent": row["purchase_intent"] or "low",
            "analysis": {
                "topic": row["topic"] or "other", "intent": row["intent"] or "small_talk",
                "sentiment": row["sentiment"] or "neutral", "purchase_intent": row["purchase_intent"] or "low",
            },
        }

    def recent_events(self, session_id: Optional[int], limit: int = 200) -> List[Dict[str, Any]]:
        limit = min(max(safe_int(limit, 200), 1), 1000)
        with self.lock:
            if session_id:
                rows = self.connection.execute(
                    "SELECT * FROM live_events WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)
                ).fetchall()
            else:
                rows = self.connection.execute("SELECT * FROM live_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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

    def close(self) -> None:
        with self.lock:
            self.connection.close()


class SignalEngine:
    def __init__(self, analyzer=analyze_text) -> None:
        self.analyzer = analyzer

    @staticmethod
    def _window(events: Sequence[Dict[str, Any]], seconds: int) -> List[Dict[str, Any]]:
        cutoff = time.time() - seconds
        return [event for event in events if event_seconds(event.get("timestamp")) >= cutoff]

    @staticmethod
    def _rate(count: int, seconds: int) -> float:
        return round(count * 60 / max(seconds, 1), 1)

    @staticmethod
    def _ratio(count: int, total: int) -> float:
        return round(count / total, 3) if total else 0.0

    def build(self, events: Sequence[Dict[str, Any]], online: int = 0, snapshots: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
        recent = list(events)
        windows: Dict[str, Dict[str, Any]] = {}
        for seconds, label in ((10, "10s"), (60, "60s"), (300, "5m")):
            window = self._window(recent, seconds)
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

        five_minute = self._window(recent, 300)
        comments_5m = [event for event in five_minute if event.get("type") == "comment"]
        topics = Counter(event.get("topic", "other") for event in comments_5m)
        questions = Counter(re.sub(r"[\s！？?!。,.，、]+", "", str(event.get("content") or "")) for event in comments_5m if event.get("content") and (event.get("is_question") == "true" or "?" in str(event.get("content")) or "？" in str(event.get("content"))))
        keywords: Counter = Counter()
        for event in comments_5m:
            for token in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,}", str(event.get("content") or "")):
                if token not in STOP_WORDS:
                    keywords[token] += 1

        signals: List[Dict[str, Any]] = []
        current = windows["60s"]
        if current["high_purchase_ratio"] >= 0.2 and current["comments"] >= 2:
            signals.append({"level": "opportunity", "title": "购买意图集中", "detail": "价格、下单或购买方式相关问题正在出现。", "recommendation": "主播重复价格、优惠和下单路径。"})
        if current["negative_ratio"] >= 0.25 and current["comments"] >= 3:
            signals.append({"level": "risk", "title": "负面情绪升高", "detail": "负面评论占比超过最近评论的四分之一。", "recommendation": "优先回应物流、售后或产品疑虑，并给出明确处理时效。"})
        if questions:
            top_question = questions.most_common(1)[0][0]
            if top_question:
                signals.append({"level": "attention", "title": "观众问题集中", "detail": "高频问题：" + top_question, "recommendation": "把该问题整理成一句固定口播或屏幕贴片。"})
        if not signals:
            signals.append({"level": "neutral", "title": "暂无强信号", "detail": "继续积累事件，等待趋势变化。", "recommendation": "保持观察评论速度、购买意图和负面反馈。"})

        return {
            "windows": windows,
            "current": current,
            "topics": [{"topic": key, "label": TOPIC_LABELS.get(key, key), "count": value} for key, value in topics.most_common(6)],
            "keywords": [[key, value] for key, value in keywords.most_common(12)],
            "hot_questions": [{"question": key, "count": value} for key, value in questions.most_common(5) if key],
            "signals": signals,
            "summary": {
                "top_topics": [TOPIC_LABELS.get(key, key) for key, _ in topics.most_common(3)],
                "purchase_intent": current["purchase_ratio"],
                "sentiment": {"positive": current["positive_ratio"], "neutral": round(max(0, 1 - current["positive_ratio"] - current["negative_ratio"]), 3), "negative": current["negative_ratio"]},
            },
            "trend": list(snapshots or []),
        }
