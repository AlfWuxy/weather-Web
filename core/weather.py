# -*- coding: utf-8 -*-
"""Weather-related helpers."""
from datetime import datetime, timedelta
import json
import logging
import time

from flask import current_app, session, has_app_context, has_request_context, request
from flask_login import current_user

from core.constants import DEFAULT_CITY_LABEL, WEATHER_CACHE_TTL_MINUTES
from core.guest import is_guest_user
from core.extensions import db
from core.db_models import Community, ForecastCache, WeatherCache, WeatherData
from core.time_utils import today_local, utcnow, ensure_utc_aware
from utils.parsers import parse_bool, safe_json_loads

logger = logging.getLogger(__name__)
_weather_fetcher = None
_REDIS_CLIENT_KEY = 'redis_client'
_REDIS_UNAVAILABLE_KEY = 'redis_unavailable'
_REDIS_COOLDOWN_SECONDS = 60


def register_weather_fetcher(fetcher):
    """Register a weather fetcher for dependency injection."""
    global _weather_fetcher
    _weather_fetcher = fetcher
    if has_app_context():
        current_app.extensions['weather_fetcher'] = fetcher
    return fetcher


def get_weather_fetcher():
    if has_app_context():
        return current_app.extensions.get('weather_fetcher') or _weather_fetcher
    return _weather_fetcher


def _redis_in_cooldown():
    if not has_app_context():
        return True
    unavailable_until = current_app.extensions.get(_REDIS_UNAVAILABLE_KEY)
    if not unavailable_until:
        return False
    if isinstance(unavailable_until, (int, float)):
        if time.time() < unavailable_until:
            return True
        current_app.extensions.pop(_REDIS_UNAVAILABLE_KEY, None)
        return False
    current_app.extensions[_REDIS_UNAVAILABLE_KEY] = time.time() + _REDIS_COOLDOWN_SECONDS
    return True


def _mark_redis_unavailable():
    if has_app_context():
        current_app.extensions[_REDIS_UNAVAILABLE_KEY] = time.time() + _REDIS_COOLDOWN_SECONDS


def _mark_redis_available():
    if has_app_context():
        current_app.extensions.pop(_REDIS_UNAVAILABLE_KEY, None)


def _get_redis_client():
    if not has_app_context():
        return None
    if _redis_in_cooldown():
        return None
    if _REDIS_CLIENT_KEY in current_app.extensions:
        return current_app.extensions.get(_REDIS_CLIENT_KEY)
    redis_url = (
        current_app.config.get('WEATHER_CACHE_REDIS_URL')
        or current_app.config.get('REDIS_URL')
        or ''
    )
    redis_url = redis_url.strip() if isinstance(redis_url, str) else redis_url
    if not redis_url:
        _mark_redis_unavailable()
        return None
    try:
        import redis  # type: ignore
    except ImportError:
        logger.warning("redis 未安装，跳过 Redis 缓存。")
        _mark_redis_unavailable()
        return None
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        logger.warning("Redis 初始化失败: %s", exc)
        _mark_redis_unavailable()
        return None
    current_app.extensions[_REDIS_CLIENT_KEY] = client
    _mark_redis_available()
    return client


def _redis_cache_key(prefix, *parts):
    safe_parts = [str(part).strip() for part in parts if part is not None]
    return ':'.join([prefix] + safe_parts)


def _redis_get_json(client, key, default):
    if client is None:
        return None
    try:
        payload = client.get(key)
        _mark_redis_available()
    except Exception as exc:
        logger.warning("Redis 读取失败，已跳过: %s", exc)
        _mark_redis_unavailable()
        return None
    if not payload:
        return None
    return safe_json_loads(payload, default)


def _redis_set_json(client, key, ttl_seconds, payload):
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps(payload, ensure_ascii=False))
        _mark_redis_available()
    except Exception as exc:
        logger.warning("Redis 写入失败，已忽略: %s", exc)
        _mark_redis_unavailable()

def is_demo_mode():
    """Check if demo mode is enabled via config, session, or query param."""
    if not has_app_context():
        return False
    if current_app.config.get('DEMO_MODE'):
        return True
    if not has_request_context():
        return False
    demo_arg = request.args.get('demo')
    if demo_arg is not None:
        enabled = parse_bool(demo_arg, default=False)
        if enabled:
            session['demo_mode'] = True
        else:
            session.pop('demo_mode', None)
        return enabled
    return bool(session.get('demo_mode'))


def get_demo_weather_data():
    """固定的演示天气（热浪日）。"""
    return {
        'temperature': 37,
        'temperature_max': 39,
        'temperature_min': 29,
        'humidity': 70,
        'pressure': 1005,
        'weather_condition': '高温',
        'wind_speed': 1.5,
        'pm25': 55,
        'aqi': 90,
        'is_mock': True,
        'is_demo': True
    }


def get_demo_forecast_data(days=7):
    """演示用天气预报数据。"""
    base = get_demo_weather_data()
    forecast = []
    for offset in range(days):
        entry = dict(base)
        entry['forecast_date'] = (today_local() + timedelta(days=offset)).isoformat()
        entry['temperature_max'] = base['temperature_max'] - (offset % 3)
        entry['temperature_min'] = base['temperature_min'] - (offset % 2)
        forecast.append(entry)
    return forecast


def get_location_options():
    """获取可选地点列表"""
    options = set()
    options.update(current_app.config.get('CITY_LOCATION_MAP', {}).keys())
    try:
        communities = Community.query.with_entities(Community.name).all()
        options.update([c[0] for c in communities if c and c[0]])
    except Exception as exc:
        logger.warning("Failed to load community locations: %s", exc)
    options = {opt.strip() for opt in options if opt and isinstance(opt, str)}
    default_city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    ordered = []
    for item in (default_city, DEFAULT_CITY_LABEL, '北京', '上海', '广州', '深圳'):
        if item in options and item not in ordered:
            ordered.append(item)
            options.discard(item)
    ordered.extend(sorted(options))
    return ordered


def get_user_location_value():
    """获取用户当前定位（不写入）"""
    default_city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    if current_user.is_authenticated:
        if is_guest_user(current_user):
            from core.guest import build_guest_profile
            profile = build_guest_profile()
            return profile.get('community') or default_city
        return current_user.community or default_city
    return default_city


def normalize_location_name(location):
    """校验地点名称，无法识别时回退默认城市"""
    default_city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    if not location or not isinstance(location, str):
        return default_city
    location = location.strip()
    if not location:
        return default_city
    city_map = current_app.config.get('CITY_LOCATION_MAP', {})
    if location in city_map:
        return location
    try:
        if Community.query.filter_by(name=location).first():
            return location
    except Exception as exc:
        logger.warning("Failed to validate community location: %s", exc)
    return default_city


def ensure_user_location_valid():
    """确保用户定位有效，必要时修正到默认城市

    注意：
    - 对于数据库用户，仅修改 current_user.community 属性
    - 仅在 GET 请求且 session 没有其它脏对象时提交，避免误提交其它修改
    - 其他情况下仅 flush，不自动提交
    """
    location = get_user_location_value()
    normalized = normalize_location_name(location)
    if normalized != location and current_user.is_authenticated:
        if is_guest_user(current_user):
            from core.guest import build_guest_profile
            profile = build_guest_profile()
            profile['community'] = normalized
            session['guest_profile'] = profile
        else:
            # 仅修改模型属性，仅在安全场景下显式提交，避免误提交其他修改
            try:
                current_user.community = normalized
                should_commit = False
                if has_request_context() and request.method == 'GET':
                    other_dirty = any(obj is not current_user for obj in db.session.dirty)
                    if not other_dirty and not db.session.new and not db.session.deleted:
                        should_commit = True
                # 标记为已修改（通常自动追踪，但显式刷新确保安全）
                db.session.flush()
                if should_commit:
                    db.session.commit()
            except Exception as exc:
                logger.warning("更新用户定位失败: %s", exc)
                db.session.rollback()
                # 不抛出异常，允许继续使用 normalized 值
    return normalized


def resolve_weather_city_label(location):
    """显示天气来源城市"""
    default_city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    default_location = current_app.config.get('DEFAULT_LOCATION', '116.20,29.27')
    city_map = current_app.config.get('CITY_LOCATION_MAP', {})
    if not location:
        return DEFAULT_CITY_LABEL if default_city in ('都昌', '都昌县') else default_city
    if location in ('都昌', '都昌县'):
        return DEFAULT_CITY_LABEL
    mapped = city_map.get(location)
    if mapped and mapped == default_location:
        return DEFAULT_CITY_LABEL
    if location not in city_map:
        return DEFAULT_CITY_LABEL if default_city in ('都昌', '都昌县') else default_city
    return location


def get_weather_with_cache(location, ttl_minutes=None):
    """获取带缓存的天气数据"""
    if is_demo_mode():
        return get_demo_weather_data(), False
    location = normalize_location_name(location)
    if ttl_minutes is None:
        ttl_minutes = current_app.config.get('WEATHER_CACHE_TTL_MINUTES', WEATHER_CACHE_TTL_MINUTES)
    ttl_seconds = max(int(ttl_minutes * 60), 60)
    redis_client = _get_redis_client()
    redis_key = _redis_cache_key('weather:current', location)
    redis_payload = _redis_get_json(redis_client, redis_key, {})
    if redis_payload is not None:
        return redis_payload, True
    now = utcnow()
    cache = None
    try:
        cache = WeatherCache.query.filter_by(location=location).order_by(
            WeatherCache.fetched_at.desc(),
            WeatherCache.id.desc()
        ).first()
        if cache and cache.fetched_at:
            # 确保从数据库读取的 datetime 是 UTC aware 的
            if now - ensure_utc_aware(cache.fetched_at) <= timedelta(minutes=ttl_minutes):
                return safe_json_loads(cache.payload, {}), True
    except Exception as exc:
        logger.warning("天气缓存不可用，已跳过缓存: %s", exc)
        db.session.rollback()
    weather_service = get_weather_fetcher()
    try:
        if weather_service is None:
            raise RuntimeError("Weather fetcher not configured")
        weather_data = weather_service.get_current_weather(location)
    except Exception as exc:
        logger.warning("获取天气失败，使用默认数据: %s", exc)
        weather_data = None
    if not weather_data:
        weather_data = get_fallback_weather_data()
    try:
        _redis_set_json(redis_client, redis_key, ttl_seconds, weather_data)
        if cache:
            cache.payload = json.dumps(weather_data, ensure_ascii=False)
            cache.fetched_at = now
            cache.is_mock = bool(weather_data.get('is_mock'))
        else:
            cache = WeatherCache(
                location=location,
                fetched_at=now,
                payload=json.dumps(weather_data, ensure_ascii=False),
                is_mock=bool(weather_data.get('is_mock'))
            )
            db.session.add(cache)
        db.session.commit()
    except Exception as exc:
        logger.warning("天气缓存写入失败，已忽略: %s", exc)
        db.session.rollback()
    return weather_data, False


def get_fallback_weather_data():
    """默认天气数据（用于异常兜底）"""
    return {
        'temperature': 20,
        'temperature_max': 25,
        'temperature_min': 15,
        'humidity': 60,
        'pressure': 1013,
        'weather_condition': '未知',
        'wind_speed': 2.0,
        'pm25': 35,
        'aqi': 50,
        'is_mock': True
    }


def get_forecast_with_cache(location, days=7, ttl_minutes=None):
    """获取带缓存的天气预报"""
    if is_demo_mode():
        return get_demo_forecast_data(days=days), True
    location = normalize_location_name(location)
    if ttl_minutes is None:
        ttl_minutes = current_app.config.get('FORECAST_CACHE_TTL_MINUTES', 20)
    ttl_seconds = max(int(ttl_minutes * 60), 60)
    redis_client = _get_redis_client()
    redis_key = _redis_cache_key('weather:forecast', location, days)
    redis_payload = _redis_get_json(redis_client, redis_key, [])
    if redis_payload is not None:
        return redis_payload, True
    now = utcnow()
    cache = None
    try:
        cache = ForecastCache.query.filter_by(location=location, days=days).order_by(
            ForecastCache.fetched_at.desc(),
            ForecastCache.id.desc()
        ).first()
        if cache and cache.fetched_at:
            # 确保从数据库读取的 datetime 是 UTC aware 的
            if now - ensure_utc_aware(cache.fetched_at) <= timedelta(minutes=ttl_minutes):
                return safe_json_loads(cache.payload, []), True
    except Exception as exc:
        logger.warning("预报缓存不可用，已跳过缓存: %s", exc)
        db.session.rollback()
    weather_service = get_weather_fetcher()
    try:
        if weather_service is None:
            raise RuntimeError("Weather fetcher not configured")
        forecast_data = weather_service.get_weather_forecast(location, days=days)
    except Exception as exc:
        logger.warning("获取天气预报失败，使用兜底数据: %s", exc)
        forecast_data = []
    if not forecast_data:
        forecast_data = []
    try:
        _redis_set_json(redis_client, redis_key, ttl_seconds, forecast_data)
        if cache:
            cache.payload = json.dumps(forecast_data, ensure_ascii=False)
            cache.fetched_at = now
            cache.is_mock = bool(forecast_data and forecast_data[0].get('is_mock'))
        else:
            cache = ForecastCache(
                location=location,
                days=days,
                fetched_at=now,
                payload=json.dumps(forecast_data, ensure_ascii=False),
                is_mock=bool(forecast_data and forecast_data[0].get('is_mock'))
            )
            db.session.add(cache)
        db.session.commit()
    except Exception as exc:
        logger.warning("预报缓存写入失败，已忽略: %s", exc)
        db.session.rollback()
    return forecast_data, False


def get_consecutive_hot_days(location, target_date=None, today_max=None, threshold=None, max_days=7):
    """Count consecutive hot days up to target_date."""
    if is_demo_mode():
        return 5
    if not location:
        return 0
    if threshold is None:
        if has_app_context():
            threshold = current_app.config.get('HEAT_HOT_DAY_THRESHOLD', 35)
        else:
            threshold = 35
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 35
    if target_date is None:
        target_date = today_local()

    if today_max is None:
        record = WeatherData.query.filter_by(
            date=target_date,
            location=location
        ).first()
        if record and record.temperature_max is not None:
            today_max = record.temperature_max
    if today_max is None:
        return 0
    try:
        today_max = float(today_max)
    except (TypeError, ValueError):
        return 0
    if today_max < threshold:
        return 0

    count = 1
    if max_days is None or max_days <= 1:
        return count
    lookback = max_days - 1
    records = WeatherData.query.filter(
        WeatherData.location == location,
        WeatherData.date < target_date
    ).order_by(WeatherData.date.desc()).limit(lookback).all()
    expected = target_date - timedelta(days=1)
    for record in records:
        if record.date != expected:
            break
        if record.temperature_max is None or record.temperature_max < threshold:
            break
        count += 1
        expected = expected - timedelta(days=1)
    return count
