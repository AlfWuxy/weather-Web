# -*- coding: utf-8 -*-
"""工作台专用预报：验证真实来源、日期与温度，并缓存备用源。"""

import json
import logging
import math
from datetime import datetime, timedelta

import requests
from flask import current_app

from core.time_utils import ensure_utc_aware, today_local, utcnow

logger = logging.getLogger(__name__)
FAILURE_COOLDOWN_SECONDS = 60


def validated_forecast(entries, source, start_date=None, days=7, demo=False):
    """只接收从本地今日开始、日期连续且高低温完整的预报。"""
    start_date = start_date or today_local()
    if not isinstance(entries, list) or len(entries) < days:
        return []
    result = []
    for offset, entry in enumerate(entries[:days]):
        if not isinstance(entry, dict) or entry.get("data_source") != source:
            return []
        if not demo and (entry.get("is_mock") or entry.get("is_demo")):
            return []
        expected_date = (start_date + timedelta(days=offset)).isoformat()
        dates = [entry[key] for key in ("forecast_date", "date") if entry.get(key) is not None]
        if not dates or any(str(value) != expected_date for value in dates):
            return []
        try:
            # 布尔值不能作为温度；缺失或非有限值必须保持未知。
            if any(isinstance(entry.get(key), bool) for key in ("temperature_max", "temperature_min")):
                return []
            high, low = float(entry["temperature_max"]), float(entry["temperature_min"])
        except (KeyError, TypeError, ValueError):
            return []
        if not math.isfinite(high) or not math.isfinite(low) or high < low:
            return []
        result.append({**entry, "forecast_date": expected_date, "temperature_max": high, "temperature_min": low})
    return result


def _fetch_openmeteo(location):
    """只读真实逐日预报，不沿用通用预报链路里的缺温默认值。"""
    from core.weather import get_weather_fetcher

    fetcher = get_weather_fetcher()
    if fetcher is None:
        return []
    # 复用主天气源的地点解析，避免两个来源指向不同位置。
    coordinates = fetcher._parse_lon_lat(fetcher._get_location(location))
    if not coordinates:
        return []
    lon, lat = coordinates
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "celsius",
            "timezone": current_app.config.get("APP_TIMEZONE") or "Asia/Shanghai",
            "forecast_days": 7,
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    if not all(isinstance(values, list) and len(values) >= 7 for values in (dates, highs, lows)):
        return []
    return [
        {"forecast_date": dates[i], "temperature_max": highs[i], "temperature_min": lows[i],
         "data_source": "Open-Meteo", "is_mock": False}
        for i in range(7)
    ]


def _failure_cooldown_active(payload, now):
    """短暂记住当日失败，避免页面刷新不断等待超时；跨午夜立即允许重试。"""
    if not isinstance(payload, dict) or payload.get("unavailable_on") != today_local().isoformat():
        return False
    try:
        retry_after = ensure_utc_aware(datetime.fromisoformat(payload["retry_after"]))
        return now < retry_after <= now + timedelta(seconds=FAILURE_COOLDOWN_SECONDS)
    except (KeyError, TypeError, ValueError):
        return False


def get_openmeteo_forecast_with_cache(location):
    """独立缓存备用源；TTL 有效也必须再次检查日期，不复用跨午夜预报。"""
    from core.db_models import ForecastCache
    from core.extensions import db
    from core.weather import _get_redis_client, _redis_get_json, _redis_set_json

    ttl_minutes = current_app.config.get("FORECAST_CACHE_TTL_MINUTES", 20)
    cache_location = f"heat-workbench-openmeteo:{location}"
    redis_key = f"weather:{cache_location}:7"
    redis_client = _get_redis_client()
    now = utcnow()
    cached = _redis_get_json(redis_client, redis_key, [])
    if _failure_cooldown_active(cached, now):
        return []
    forecast = validated_forecast(cached, "Open-Meteo")
    if forecast:
        return forecast
    cache = None
    try:
        cache = ForecastCache.query.filter_by(location=cache_location, days=7).order_by(
            ForecastCache.fetched_at.desc(), ForecastCache.id.desc()
        ).first()
        if cache and cache.fetched_at and not cache.is_mock:
            cached = json.loads(cache.payload)
            if _failure_cooldown_active(cached, now):
                return []
            age = now - ensure_utc_aware(cache.fetched_at)
            if timedelta(0) <= age <= timedelta(minutes=ttl_minutes):
                forecast = validated_forecast(cached, "Open-Meteo")
                if forecast:
                    return forecast
    except Exception as exc:
        logger.warning("工作台备用预报缓存读取失败: %s", exc)
        db.session.rollback()
    try:
        forecast = validated_forecast(_fetch_openmeteo(location), "Open-Meteo")
    except Exception as exc:
        logger.warning("工作台备用预报不可用: %s", exc)
        forecast = []
    now = utcnow()
    # 异常、空结果和无效温度均进入同一冷却，不将失败伪装成有效预报。
    cache_payload = forecast or {
        "unavailable_on": today_local().isoformat(),
        "retry_after": (now + timedelta(seconds=FAILURE_COOLDOWN_SECONDS)).isoformat(),
    }
    ttl_seconds = max(int(ttl_minutes * 60), 60) if forecast else FAILURE_COOLDOWN_SECONDS
    try:
        _redis_set_json(redis_client, redis_key, ttl_seconds, cache_payload)
        if cache is None:
            cache = ForecastCache(location=cache_location, days=7)
            db.session.add(cache)
        cache.payload = json.dumps(cache_payload, ensure_ascii=False)
        cache.fetched_at = now
        cache.is_mock = False
        db.session.commit()
    except Exception as exc:
        logger.warning("工作台备用预报缓存写入失败: %s", exc)
        db.session.rollback()
    return forecast


def get_workbench_forecast(location):
    """主源与备用源均不合格时返回空预报，演示入口独立标注。"""
    from core.weather import get_demo_forecast_data, get_qweather_forecast_with_cache, is_demo_mode

    if is_demo_mode():
        forecast = validated_forecast(get_demo_forecast_data(7), "Demo", demo=True)
        if forecast:
            return forecast, "demo", "演示数据", "演示预报，仅供功能展示"
    else:
        try:
            primary, _, meta = get_qweather_forecast_with_cache(location, days=7)
            meta = meta or {}
            expired = bool(meta.get("stale"))
            if meta.get("expires_at"):
                expires_at = datetime.fromisoformat(str(meta["expires_at"]).replace("Z", "+00:00"))
                expired = expired or ensure_utc_aware(expires_at) <= utcnow()
            forecast = [] if expired else validated_forecast(primary, "QWeather")
        except Exception as exc:
            logger.warning("工作台主预报不可用: %s", exc)
            forecast = []
        if forecast:
            return forecast, "ok", "和风天气 7 天预报", ""
        forecast = get_openmeteo_forecast_with_cache(location)
        if forecast:
            return forecast, "fallback", "Open-Meteo（备用预报）", "主预报暂不可用，使用 Open-Meteo 备用预报供巡访参考"
    return [], "unavailable", "预报暂不可用", "风险暂不可判定；地图仅供静态参考"
