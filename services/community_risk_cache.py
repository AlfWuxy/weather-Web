# -*- coding: utf-8 -*-
"""社区风险分析结果缓存。"""
import hashlib
import json
import logging
import time

from flask import current_app

from core.weather import _get_redis_client, _redis_get_json, _redis_set_json

logger = logging.getLogger(__name__)

_LOCAL_COMMUNITY_RISK_CACHE = {}
_LOCAL_CACHE_MAX_ITEMS = 128
_WEATHER_SIGNATURE_KEYS = (
    'temperature',
    'temperature_max',
    'temperature_min',
    'humidity',
    'aqi',
    'weather_condition',
    'wind_speed',
)
_LAG_TEMPERATURE_KEYS = (
    'lag_temperatures',
    'temperature_lags',
    'temperature_history',
    'historical_temperatures',
)


def clear_local_community_risk_cache():
    """清空进程内缓存，便于测试隔离。"""
    _LOCAL_COMMUNITY_RISK_CACHE.clear()


def _normalize_cache_value(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        normalized = []
        for item in value:
            if item is None:
                continue
            normalized.append(_normalize_cache_value(item))
        return normalized
    if isinstance(value, dict):
        return {
            str(key): _normalize_cache_value(item)
            for key, item in sorted(value.items())
            if item is not None
        }
    return value


def build_community_weather_signature(weather_data):
    """提取社区风险计算实际依赖的天气字段，供缓存键复用。"""
    if not isinstance(weather_data, dict):
        return {}

    signature = {}
    for key in _WEATHER_SIGNATURE_KEYS:
        value = weather_data.get(key)
        if value is not None:
            signature[key] = _normalize_cache_value(value)

    for key in _LAG_TEMPERATURE_KEYS:
        value = weather_data.get(key)
        if isinstance(value, (list, tuple)) and value:
            signature['lag_temperatures'] = _normalize_cache_value(value)
            break

    return signature


def _normalize_window_days(window_days):
    try:
        return max(7, min(int(window_days or 30), 120))
    except (TypeError, ValueError):
        return 30


def _normalize_disease_filter(value):
    value = (value or '').strip()
    return '' if value in ('', 'all', '全部') else value


def build_community_risk_cache_params(
    analysis_date=None,
    window_days=30,
    disease_filter='',
    city='',
    weather_data=None,
):
    """统一生成 API 与预计算任务共享的社区风险缓存参数。"""
    if hasattr(analysis_date, 'isoformat'):
        analysis_date = analysis_date.isoformat()
    elif analysis_date is None:
        analysis_date = ''
    else:
        analysis_date = str(analysis_date)

    return {
        'analysis_date': analysis_date,
        'window_days': _normalize_window_days(window_days),
        'disease_filter': _normalize_disease_filter(disease_filter),
        'city': str(city or '').strip(),
        'weather': build_community_weather_signature(weather_data),
    }


def _now_ts():
    return time.time()


def _cache_ttl_seconds():
    ttl = current_app.config.get('COMMUNITY_RISK_CACHE_TTL_SECONDS', 600)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = 600
    return max(ttl, 60)


def _cache_lock_seconds():
    seconds = current_app.config.get('COMMUNITY_RISK_CACHE_LOCK_SECONDS', 20)
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 20
    return max(seconds, 5)


def _cache_wait_seconds():
    seconds = current_app.config.get('COMMUNITY_RISK_CACHE_WAIT_SECONDS', 1.2)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 1.2
    return min(max(seconds, 0.0), 5.0)


def _cleanup_local_cache(now_ts=None):
    now_ts = now_ts if now_ts is not None else _now_ts()
    expired_keys = [
        key for key, item in _LOCAL_COMMUNITY_RISK_CACHE.items()
        if item.get('expires_at', 0) <= now_ts
    ]
    for key in expired_keys:
        _LOCAL_COMMUNITY_RISK_CACHE.pop(key, None)

    if len(_LOCAL_COMMUNITY_RISK_CACHE) <= _LOCAL_CACHE_MAX_ITEMS:
        return

    sorted_items = sorted(
        _LOCAL_COMMUNITY_RISK_CACHE.items(),
        key=lambda item: item[1].get('stored_at', 0)
    )
    trim_count = max(len(sorted_items) - _LOCAL_CACHE_MAX_ITEMS, 0)
    for key, _item in sorted_items[:trim_count]:
        _LOCAL_COMMUNITY_RISK_CACHE.pop(key, None)


def _build_cache_key(cache_params):
    normalized = json.dumps(
        cache_params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':')
    )
    digest = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    return f'community_risk:v2:{digest}'


def _get_from_local_cache(cache_key):
    now_ts = _now_ts()
    _cleanup_local_cache(now_ts)
    item = _LOCAL_COMMUNITY_RISK_CACHE.get(cache_key)
    if not item:
        return None
    if item.get('expires_at', 0) <= now_ts:
        _LOCAL_COMMUNITY_RISK_CACHE.pop(cache_key, None)
        return None
    return item.get('payload')


def _set_local_cache(cache_key, payload, ttl_seconds):
    now_ts = _now_ts()
    _cleanup_local_cache(now_ts)
    _LOCAL_COMMUNITY_RISK_CACHE[cache_key] = {
        'payload': payload,
        'stored_at': now_ts,
        'expires_at': now_ts + ttl_seconds,
    }


def _wait_for_redis_cache(client, cache_key, ttl_seconds):
    """等待其他请求填充缓存，避免多人同时重复计算。"""
    if client is None:
        return None

    deadline = _now_ts() + _cache_wait_seconds()
    while _now_ts() < deadline:
        time.sleep(0.12)
        payload = _redis_get_json(client, cache_key, None)
        if payload is not None:
            _set_local_cache(cache_key, payload, ttl_seconds)
            return payload
    return None


def get_or_build_community_risk_result(cache_params, builder):
    """读取缓存；未命中时只让一个请求优先计算。"""
    ttl_seconds = _cache_ttl_seconds()
    cache_key = _build_cache_key(cache_params)

    local_payload = _get_from_local_cache(cache_key)
    if local_payload is not None:
        return local_payload, True

    redis_client = _get_redis_client()
    redis_payload = _redis_get_json(redis_client, cache_key, None)
    if redis_payload is not None:
        _set_local_cache(cache_key, redis_payload, ttl_seconds)
        return redis_payload, True

    has_lock = False
    lock_key = f'{cache_key}:lock'
    if redis_client is not None:
        try:
            has_lock = bool(redis_client.set(lock_key, '1', nx=True, ex=_cache_lock_seconds()))
        except Exception as exc:
            logger.warning("社区风险缓存加锁失败，退回本地计算: %s", exc)
            has_lock = False

        if not has_lock:
            waited_payload = _wait_for_redis_cache(redis_client, cache_key, ttl_seconds)
            if waited_payload is not None:
                return waited_payload, True

    try:
        payload = builder()
        _set_local_cache(cache_key, payload, ttl_seconds)
        _redis_set_json(redis_client, cache_key, ttl_seconds, payload)
    finally:
        if redis_client is not None and has_lock:
            try:
                redis_client.delete(lock_key)
            except Exception:
                logger.debug("社区风险缓存解锁失败: %s", lock_key)

    return payload, False
