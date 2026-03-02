# -*- coding: utf-8 -*-
"""QWeather warning (official alerts) fetch + normalization.

Pilot strategy:
- Prefer official warnings (QWeather /warning/now)
- Caller may fall back to threshold rules if no warnings
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import requests
from flask import current_app, has_app_context

from services.external_api import record_external_api_timing as _record_external_api_timing

logger = logging.getLogger(__name__)

_CAP_SEVERITY_ALLOWED = {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}
_CAP_CERTAINTY_ALLOWED = {"Observed", "Likely", "Possible", "Unlikely", "Unknown"}
_CAP_URGENCY_ALLOWED = {"Immediate", "Expected", "Future", "Past", "Unknown"}
_LEVEL_TO_CAP_SEVERITY = {
    "红": "Severe",
    "橙": "Moderate",
    "黄": "Minor",
    "蓝": "Minor",
}


def _cfg(key: str, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def _normalize_cap_enum(value, allowed, default):
    text = str(value or "").strip()
    if not text:
        return default
    # Direct CAP value
    cap = text[:1].upper() + text[1:].lower()
    if cap in allowed:
        return cap
    # 中文兜底
    lowered = text.lower()
    if lowered in ("极高", "特别严重"):
        return "Extreme"
    if lowered in ("高", "严重"):
        return "Severe"
    if lowered in ("中", "中等"):
        return "Moderate"
    if lowered in ("低", "一般"):
        return "Minor"
    return default


def _level_to_cap_severity(level):
    level_text = str(level or "")
    for key, cap_value in _LEVEL_TO_CAP_SEVERITY.items():
        if key in level_text:
            return cap_value
    return "Unknown"


def get_qweather_warnings(location_code: str) -> List[Dict[str, Any]]:
    """Fetch QWeather warnings and normalize fields.

    Returns a list of dicts (may be empty). Never raises for network/parse issues.
    """
    location_code = (str(location_code).strip() if location_code is not None else "")
    if not location_code:
        return []

    qweather_key = (_cfg("QWEATHER_KEY") or "").strip()
    api_base = (_cfg("QWEATHER_API_BASE") or "").strip()
    if not qweather_key or not api_base:
        return []

    url = f"{api_base.rstrip('/')}/warning/now"
    params = {"key": qweather_key, "location": location_code}

    start_ts = time.perf_counter()
    try:
        resp = requests.get(url, params=params, timeout=10)
        _record_external_api_timing("qweather_warning_now", (time.perf_counter() - start_ts) * 1000, resp.status_code)
        if resp.status_code != 200:
            logger.info("QWeather warning http=%s for location=%s", resp.status_code, location_code)
            return []
        payload = resp.json()
    except Exception as exc:
        logger.info("QWeather warning fetch failed for %s: %s", location_code, exc)
        return []

    try:
        if str(payload.get("code")) != "200":
            return []
        raw_list = payload.get("warning") or payload.get("warnings") or []
        if not isinstance(raw_list, list):
            return []
    except (AttributeError, TypeError, ValueError):
        logger.debug("预警数据解析异常", exc_info=True)
        return []

    normalized: List[Dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        level_text = item.get("level") or item.get("severity") or item.get("severityColor") or ""
        cap_severity = _normalize_cap_enum(
            item.get("severity") or item.get("severityColor"),
            _CAP_SEVERITY_ALLOWED,
            _level_to_cap_severity(level_text),
        )
        cap_certainty = _normalize_cap_enum(
            item.get("certainty"),
            _CAP_CERTAINTY_ALLOWED,
            "Likely",
        )
        cap_urgency = _normalize_cap_enum(
            item.get("urgency"),
            _CAP_URGENCY_ALLOWED,
            "Expected",
        )
        instruction = item.get("instruction") or item.get("instructionText") or ""
        # Field names vary across providers/versions; be forgiving.
        normalized.append(
            {
                "title": item.get("title") or item.get("name") or "",
                "type": item.get("typeName") or item.get("type") or item.get("typeId") or "",
                "level": level_text,
                "text": item.get("text") or item.get("description") or "",
                "start_time": item.get("startTime") or item.get("start") or item.get("pubTime") or "",
                "end_time": item.get("endTime") or item.get("end") or "",
                # CAP-style normalized semantics for downstream decision engines.
                "severity": cap_severity,
                "certainty": cap_certainty,
                "urgency": cap_urgency,
                "response": item.get("responseType") or item.get("response") or "",
                "instruction": instruction,
                "raw": item,
            }
        )
    return normalized
