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


def _cfg(key: str, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


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
    except Exception:
        return []

    normalized: List[Dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        # Field names vary across providers/versions; be forgiving.
        normalized.append(
            {
                "title": item.get("title") or item.get("name") or "",
                "type": item.get("typeName") or item.get("type") or item.get("typeId") or "",
                "level": item.get("level") or item.get("severity") or item.get("severityColor") or "",
                "text": item.get("text") or item.get("description") or "",
                "start_time": item.get("startTime") or item.get("start") or item.get("pubTime") or "",
                "end_time": item.get("endTime") or item.get("end") or "",
                "raw": item,
            }
        )
    return normalized

