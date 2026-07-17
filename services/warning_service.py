# -*- coding: utf-8 -*-
"""QWeather warning (official alerts) fetch + normalization.

Pilot strategy:
- Prefer official warnings (QWeather weatheralert v1)
- Caller may fall back to threshold rules if no warnings
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Dict, List
from urllib.parse import urlsplit

import requests
from flask import current_app, has_app_context

from services.external_api import record_external_api_timing as _record_external_api_timing
from services.qweather_auth import (
    QWeatherAuthError,
    get_qweather_request_headers,
    invalidate_qweather_token,
    is_qweather_configured,
)
from services.qweather_budget import get_qweather_redis_client, reserve_qweather_request
from utils.parsers import parse_int

logger = logging.getLogger(__name__)

_CACHE_MISS = object()
_LOCAL_WARNING_CACHE = {}
_LOCAL_WARNING_CACHE_LOCK = threading.Lock()
_LOCAL_WARNING_CACHE_MAX_ITEMS = 128

_CAP_SEVERITY_ALLOWED = {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}
_CAP_CERTAINTY_ALLOWED = {"Observed", "Likely", "Possible", "Unlikely", "Unknown"}
_CAP_URGENCY_ALLOWED = {"Immediate", "Expected", "Future", "Past", "Unknown"}
_LEVEL_TO_CAP_SEVERITY = {
    "红": "Severe",
    "橙": "Moderate",
    "黄": "Minor",
    "蓝": "Minor",
}
_COLOR_CODE_TO_LEVEL = {
    "red": "红色",
    "orange": "橙色",
    "yellow": "黄色",
    "blue": "蓝色",
}


def _cfg(key: str, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def _warning_cache_ttl_seconds():
    minutes = max(
        parse_int(_cfg("QWEATHER_WARNING_CACHE_TTL_MINUTES", 30), default=30),
        10,
    )
    return minutes * 60


def _canonical_location(location_code):
    """当前产品只提供都昌县天气，所有村庄共用县级预警。"""
    canonical = _cfg("QWEATHER_CANONICAL_LOCATION") or _cfg("DEFAULT_LOCATION")
    return str(canonical or location_code or "").strip()


def _warning_cache_key(location_code):
    # v2 避免继续命中旧 /warning/now 写入的空缓存。
    return f"weather:qweather_warnings:v2:{location_code}"


def _get_cached_warnings(location_code):
    cache_key = _warning_cache_key(location_code)
    client = get_qweather_redis_client()
    if client is not None:
        try:
            payload = client.get(cache_key)
            if payload is not None:
                parsed = json.loads(payload)
                if isinstance(parsed, list):
                    return parsed
        except Exception as exc:
            logger.warning("和风预警 Redis 缓存读取失败: %s", exc)

    now = time.monotonic()
    with _LOCAL_WARNING_CACHE_LOCK:
        item = _LOCAL_WARNING_CACHE.get(cache_key)
        if item and item[0] > now:
            return item[1]
        if item:
            _LOCAL_WARNING_CACHE.pop(cache_key, None)
    return _CACHE_MISS


def _set_cached_warnings(location_code, warnings):
    cache_key = _warning_cache_key(location_code)
    ttl_seconds = _warning_cache_ttl_seconds()
    client = get_qweather_redis_client()
    if client is not None:
        try:
            client.setex(cache_key, ttl_seconds, json.dumps(warnings, ensure_ascii=False))
        except Exception as exc:
            logger.warning("和风预警 Redis 缓存写入失败: %s", exc)

    expires_at = time.monotonic() + ttl_seconds
    with _LOCAL_WARNING_CACHE_LOCK:
        _LOCAL_WARNING_CACHE[cache_key] = (expires_at, warnings)
        if len(_LOCAL_WARNING_CACHE) > _LOCAL_WARNING_CACHE_MAX_ITEMS:
            oldest_key = min(
                _LOCAL_WARNING_CACHE,
                key=lambda key: _LOCAL_WARNING_CACHE[key][0],
            )
            _LOCAL_WARNING_CACHE.pop(oldest_key, None)


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


def _format_coordinate(value: float) -> str:
    """weatheralert v1 路径最多允许两位小数。"""
    return f"{value:.2f}"


def _weatheralert_v1_url(api_base: str, location_code: str):
    """把 canonical lon,lat 转为 weatheralert v1 的 lat/lon 路径。"""
    try:
        longitude_text, latitude_text = [part.strip() for part in location_code.split(",", 1)]
        longitude = float(longitude_text)
        latitude = float(latitude_text)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (math.isfinite(longitude) and math.isfinite(latitude)):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None

    parsed = urlsplit(api_base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return (
        f"{origin}/weatheralert/v1/current/"
        f"{_format_coordinate(latitude)}/{_format_coordinate(longitude)}"
    )


def _event_name(item: Dict[str, Any]) -> str:
    for event_type in (item.get("eventType"), item.get("event")):
        if isinstance(event_type, dict):
            value = event_type.get("name") or event_type.get("code")
        else:
            value = event_type
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _message_type_details(item: Dict[str, Any]):
    message_type = item.get("messageType")
    if isinstance(message_type, dict):
        code = str(message_type.get("code") or "").strip()
        supersedes = message_type.get("supersedes")
    else:
        code = str(message_type or "").strip()
        supersedes = []
    if not isinstance(supersedes, list):
        supersedes = []
    return code, [str(value).strip() for value in supersedes if str(value).strip()]


def _metadata_attributions(payload) -> List[str]:
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    values = metadata.get("attributions") if isinstance(metadata, dict) else None
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _official_color_level(item: Dict[str, Any]) -> str:
    color = item.get("color")
    color_code = color.get("code") if isinstance(color, dict) else color
    normalized = str(color_code or "").strip().lower()
    return _COLOR_CODE_TO_LEVEL.get(normalized, str(color_code or "").strip())


def _response_text(item: Dict[str, Any]) -> str:
    value = item.get("response") or item.get("responseType") or item.get("responseTypes") or ""
    if isinstance(value, list):
        return ",".join(str(part).strip() for part in value if str(part).strip())
    return str(value or "").strip()


def _extract_warning_list(payload):
    """同时接受 weatheralert v1 和旧 v7 响应，返回 (列表, 是否有效)。"""
    if not isinstance(payload, dict):
        return [], False
    if "alerts" in payload:
        alerts = payload.get("alerts")
        return (alerts, True) if isinstance(alerts, list) else ([], False)

    code = str(payload.get("code"))
    if code == "401":
        invalidate_qweather_token()
    if code != "200":
        return [], False
    warnings = payload.get("warning")
    if warnings is None:
        warnings = payload.get("warnings")
    if warnings is None:
        warnings = []
    return (warnings, True) if isinstance(warnings, list) else ([], False)


def _warning_result(*, available: bool, status: str, warnings=None) -> Dict[str, Any]:
    """构造稳定的预警可用性结果。"""
    return {
        "available": available,
        "status": status,
        "warnings": warnings if isinstance(warnings, list) else [],
    }


def get_qweather_warnings_result(location_code: str) -> Dict[str, Any]:
    """获取官方预警，并区分“无预警”和“上游不可用”。"""
    location_code = (str(location_code).strip() if location_code is not None else "")
    if not location_code:
        return _warning_result(available=False, status="invalid_location")

    location_code = _canonical_location(location_code)

    api_base = (_cfg("QWEATHER_API_BASE") or "").strip()
    if not api_base or not is_qweather_configured():
        return _warning_result(available=False, status="not_configured")

    url = _weatheralert_v1_url(api_base, location_code)
    if not url:
        logger.warning("QWeather 预警 canonical 坐标无效: %s", location_code)
        return _warning_result(available=False, status="invalid_location")

    cached = _get_cached_warnings(location_code)
    if cached is not _CACHE_MISS:
        return _warning_result(available=True, status="ok", warnings=cached)

    params = {"localTime": "true", "lang": "zh"}

    try:
        headers = get_qweather_request_headers(api_base=api_base)
    except QWeatherAuthError as exc:
        logger.warning("QWeather warning auth failed: %s", exc)
        return _warning_result(available=False, status="auth_error")

    if not reserve_qweather_request("weatheralert_v1_current"):
        logger.warning("和风天气月度额度保护：跳过官方预警请求")
        return _warning_result(available=False, status="budget_blocked")

    start_ts = time.perf_counter()
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        _record_external_api_timing(
            "qweather_weatheralert_v1",
            (time.perf_counter() - start_ts) * 1000,
            resp.status_code,
        )
    except requests.RequestException as exc:
        logger.info("QWeather warning network failed for %s: %s", location_code, exc)
        return _warning_result(available=False, status="network_error")
    except Exception as exc:
        logger.info("QWeather warning fetch failed for %s: %s", location_code, exc)
        return _warning_result(available=False, status="network_error")

    if resp.status_code != 200:
        if resp.status_code == 401:
            invalidate_qweather_token()
        logger.info("QWeather warning http=%s for location=%s", resp.status_code, location_code)
        status = "auth_error" if resp.status_code == 401 else "http_error"
        return _warning_result(available=False, status=status)

    try:
        payload = resp.json()
    except Exception as exc:
        logger.info("QWeather warning parse failed for %s: %s", location_code, exc)
        return _warning_result(available=False, status="parse_error")

    raw_list, valid_payload = _extract_warning_list(payload)
    if not valid_payload:
        is_auth_error = (
            isinstance(payload, dict)
            and str(payload.get("code")) == "401"
        )
        status = "auth_error" if is_auth_error else "parse_error"
        return _warning_result(available=False, status=status)
    attributions = _metadata_attributions(payload)

    normalized: List[Dict[str, Any]] = []
    malformed_item = False
    for item in raw_list:
        if not isinstance(item, dict):
            malformed_item = True
            continue

        message_type, supersedes = _message_type_details(item)
        status = str(item.get("status") or "").strip()
        if message_type.lower() == "cancel" or status.lower() in {"draft", "test", "exercise"}:
            continue

        event_name = _event_name(item)
        level_text = (
            item.get("level")
            or item.get("severityColor")
            or _official_color_level(item)
            or ""
        )
        cap_severity = _normalize_cap_enum(
            item.get("severity"),
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
        instruction = str(item.get("instruction") or item.get("instructionText") or "").strip()
        title = str(item.get("headline") or item.get("title") or item.get("name") or event_name or "天气预警").strip()
        warning_type = str(
            event_name
            or item.get("typeName")
            or item.get("type")
            or item.get("typeId")
            or "天气预警"
        ).strip()
        # 字段名在 v1 与旧 v7 间不同，统一输出稳定契约并保留 raw。
        normalized.append(
            {
                "title": title,
                "type": warning_type,
                "level": str(level_text or "").strip(),
                "text": str(item.get("description") or item.get("text") or "").strip(),
                "start_time": str(
                    item.get("onsetTime")
                    or item.get("effectiveTime")
                    or item.get("startTime")
                    or item.get("start")
                    or item.get("pubTime")
                    or item.get("issuedTime")
                    or ""
                ).strip(),
                "end_time": str(
                    item.get("expireTime") or item.get("endTime") or item.get("end") or ""
                ).strip(),
                # CAP-style normalized semantics for downstream decision engines.
                "severity": cap_severity,
                "certainty": cap_certainty,
                "urgency": cap_urgency,
                "response": _response_text(item),
                "instruction": instruction,
                "source_id": str(item.get("id") or item.get("warningId") or "").strip(),
                "message_type": message_type,
                "supersedes": supersedes,
                "status": status,
                "attributions": attributions,
                "raw": item,
            }
        )
    if malformed_item:
        return _warning_result(
            available=False,
            status="parse_error",
            warnings=normalized,
        )

    _set_cached_warnings(location_code, normalized)
    return _warning_result(available=True, status="ok", warnings=normalized)


def get_qweather_warnings(location_code: str) -> List[Dict[str, Any]]:
    """兼容旧调用，仅返回预警列表。"""
    return get_qweather_warnings_result(location_code)["warnings"]
