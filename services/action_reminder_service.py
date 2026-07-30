# -*- coding: utf-8 -*-
"""按都昌日期稳定选择一条易执行的家庭提醒。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "action_reminder_templates.json"
)
_VALID_RISK_LEVELS = {"low", "medium", "high", "extreme"}
_VALID_WEATHER_TAGS = {"heat", "cold", "storm", "rain", "general"}
_VALID_AUDIENCES = {
    "older_adult",
    "family_caregiver",
    "family_group",
    "neighbor_helper",
}
_RISK_ALIASES = {
    "低": "low",
    "低风险": "low",
    "low": "low",
    "中": "medium",
    "中风险": "medium",
    "需留意": "medium",
    "medium": "medium",
    "mid": "medium",
    "高": "high",
    "高风险": "high",
    "high": "high",
    "极高": "extreme",
    "极高风险": "extreme",
    "严重": "extreme",
    "extreme": "extreme",
}
_FALLBACK_TEMPLATE = {
    "id": "dc-general-fallback",
    "risk_level": "low",
    "weather_tags": ["general"],
    "audience": "family_group",
    "message": "今天方便时问问家里老人，水、常用药和电话是否都在手边。",
    "follow_up_question": "谁方便联系一下？",
}


def normalize_reminder_risk_level(value) -> str:
    """把网页、小程序共用的风险文案归一为四档。"""
    text = str(value or "").strip().lower()
    if text in _RISK_ALIASES:
        return _RISK_ALIASES[text]
    if "极" in text or "severe" in text:
        return "extreme"
    if "高" in text:
        return "high"
    if "中" in text or "留意" in text:
        return "medium"
    return "low"


def infer_weather_tags(current=None, warnings=None) -> list[str]:
    """从已持久化天气快照推断场景，不新增任何天气请求。"""
    weather = current if isinstance(current, dict) else {}
    warning_rows = warnings if isinstance(warnings, list) else []
    text_parts = [
        weather.get("weather_condition"),
        weather.get("condition"),
        weather.get("text"),
        weather.get("textDay"),
    ]
    for warning in warning_rows:
        if isinstance(warning, dict):
            text_parts.extend(
                [
                    warning.get("title"),
                    warning.get("type"),
                    warning.get("text"),
                ]
            )
    text = " ".join(str(value or "") for value in text_parts).lower()

    tags = []
    try:
        temperature_max = float(
            weather.get("temperature_max", weather.get("temperature"))
        )
    except (TypeError, ValueError):
        temperature_max = None
    try:
        temperature_min = float(
            weather.get("temperature_min", weather.get("temperature"))
        )
    except (TypeError, ValueError):
        temperature_min = None

    if (
        (temperature_max is not None and temperature_max >= 32)
        or any(word in text for word in ("高温", "炎热", "heat", "hot"))
    ):
        tags.append("heat")
    if (
        (temperature_min is not None and temperature_min <= 8)
        or any(word in text for word in ("低温", "寒潮", "降温", "cold", "冻"))
    ):
        tags.append("cold")
    if any(
        word in text
        for word in ("雷", "大风", "台风", "冰雹", "storm", "thunder", "gale")
    ):
        tags.append("storm")
    if any(
        word in text
        for word in ("雨", "积水", "洪", "rain", "shower", "drizzle")
    ):
        tags.append("rain")
    return tags or ["general"]


def _validate_template(raw, position) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"第 {position} 条提醒不是对象")
    template_id = str(raw.get("id") or "").strip()
    risk_level = str(raw.get("risk_level") or "").strip()
    audience = str(raw.get("audience") or "").strip()
    message = str(raw.get("message") or "").strip()
    question = str(raw.get("follow_up_question") or "").strip()
    weather_tags = raw.get("weather_tags")
    if not template_id or not message or not question:
        raise ValueError(f"第 {position} 条提醒缺少必要文本")
    if risk_level not in _VALID_RISK_LEVELS:
        raise ValueError(f"第 {position} 条提醒风险等级无效")
    if audience not in _VALID_AUDIENCES:
        raise ValueError(f"第 {position} 条提醒受众无效")
    if not isinstance(weather_tags, list) or not weather_tags:
        raise ValueError(f"第 {position} 条提醒天气标签为空")
    normalized_tags = [str(tag).strip() for tag in weather_tags]
    if any(tag not in _VALID_WEATHER_TAGS for tag in normalized_tags):
        raise ValueError(f"第 {position} 条提醒天气标签无效")
    return {
        "id": template_id,
        "risk_level": risk_level,
        "weather_tags": normalized_tags,
        "audience": audience,
        "message": message,
        "follow_up_question": question,
    }


@lru_cache(maxsize=1)
def load_action_reminder_templates() -> tuple[dict, ...]:
    """启动后只解析一次模板库，返回不可变容器防止调用方增删。"""
    try:
        raw_templates = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw_templates, list):
            raise ValueError("提醒模板根节点必须是列表")
        templates = tuple(
            _validate_template(item, index + 1)
            for index, item in enumerate(raw_templates)
        )
        ids = [item["id"] for item in templates]
        if len(templates) < 100:
            raise ValueError("提醒模板少于 100 条")
        if len(ids) != len(set(ids)):
            raise ValueError("提醒模板 ID 重复")
        return templates
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("今日行动提醒模板加载失败，使用内置安全提醒")
        return (dict(_FALLBACK_TEMPLATE),)


def _duchang_date_key(value=None) -> str:
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        return value.isoformat()
    else:
        text = str(value).strip()
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except (TypeError, ValueError):
            moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local_moment = moment.astimezone(timezone.utc)
    # 都昌县使用 UTC+8；只移日期，不依赖服务器本地时区。
    shifted = datetime.fromtimestamp(
        local_moment.timestamp() + 8 * 60 * 60,
        tz=timezone.utc,
    )
    return shifted.date().isoformat()


def select_action_reminder(
    *,
    date_value=None,
    risk_level=None,
    weather_tags=None,
    audience="family_group",
) -> dict:
    """同一天同一上下文结果稳定，跨日会轮换候选提醒。"""
    templates = load_action_reminder_templates()
    normalized_risk = normalize_reminder_risk_level(risk_level)
    normalized_audience = (
        audience if audience in _VALID_AUDIENCES else "family_group"
    )
    normalized_tags = [
        str(tag).strip()
        for tag in (weather_tags or ["general"])
        if str(tag).strip() in _VALID_WEATHER_TAGS
    ] or ["general"]

    exact = [
        item
        for item in templates
        if item["risk_level"] == normalized_risk
        and item["audience"] == normalized_audience
        and set(item["weather_tags"]).intersection(normalized_tags)
    ]
    same_context = [
        item
        for item in templates
        if item["risk_level"] == normalized_risk
        and set(item["weather_tags"]).intersection(normalized_tags)
    ]
    shareable_context = [
        item
        for item in same_context
        if item["audience"]
        in {"older_adult", "family_caregiver", "family_group"}
    ]
    same_risk_general = [
        item
        for item in templates
        if item["risk_level"] == normalized_risk
        and "general" in item["weather_tags"]
    ]
    # 家庭群复制入口允许轮换“对老人说”和“请家属做”两类短句，
    # 这样同一风险场景也能每天变化，同时避开仅适合邻里志愿者的措辞。
    if normalized_audience == "family_group":
        candidates = (
            shareable_context
            or exact
            or same_context
            or same_risk_general
            or list(templates)
        )
    else:
        candidates = exact or same_context or same_risk_general or list(templates)
    day_key = _duchang_date_key(date_value)
    context_seed = "|".join(
        (
            normalized_risk,
            ",".join(sorted(normalized_tags)),
            normalized_audience,
        )
    )
    digest = hashlib.sha256(context_seed.encode("utf-8")).digest()
    context_offset = int.from_bytes(digest[:8], "big")
    day_ordinal = date.fromisoformat(day_key).toordinal()
    selected = candidates[(day_ordinal + context_offset) % len(candidates)]
    return {
        **selected,
        "date": day_key,
        "text": f"{selected['message']}\n{selected['follow_up_question']}",
    }
