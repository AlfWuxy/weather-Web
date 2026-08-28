# -*- coding: utf-8 -*-
"""Weather-related helpers."""
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import time

from flask import current_app, session, has_app_context, has_request_context, request
from flask_login import current_user

from core.constants import DEFAULT_CITY_LABEL, WEATHER_CACHE_TTL_MINUTES
from core.guest import is_guest_user
from core.extensions import db
from core.db_models import Community, ForecastCache, WeatherCache, WeatherData
from core.time_utils import (
    ensure_utc_aware,
    local_datetime_to_utc,
    today_local,
    utcnow,
)
from utils.parsers import parse_bool, safe_json_loads

logger = logging.getLogger(__name__)
_weather_fetcher = None
_REDIS_CLIENT_KEY = 'redis_client'
_REDIS_UNAVAILABLE_KEY = 'redis_unavailable'
_REDIS_COOLDOWN_SECONDS = 60
_LIVE_WEATHER_SOURCES = frozenset({'QWeather', 'Open-Meteo'})
_WEATHER_VALUE_RANGES = {
    'temperature': (-90.0, 60.0),
    'temperature_max': (-90.0, 60.0),
    'temperature_min': (-90.0, 60.0),
    'humidity': (0.0, 100.0),
    'pressure': (800.0, 1100.0),
    'wind_speed': (0.0, 150.0),
    'pm25': (0.0, 1000.0),
    'aqi': (0.0, 500.0),
}


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


def _weather_field(weather_data, field, default=None):
    """同时读取 dict 与 ORM 风格天气对象。"""
    if isinstance(weather_data, dict):
        return weather_data.get(field, default)
    return getattr(weather_data, field, default)


def normalize_weather_observed_at(value):
    """把上游观测时间标准化为 UTC ISO 8601；无效值返回 None。"""
    if value is None:
        return None
    datetime_input = isinstance(value, datetime)
    if datetime_input:
        parsed = value
    else:
        raw = str(value).strip()
        # 只有日期不构成观测时刻，避免午夜附近误判为新鲜实况。
        if not raw or ('T' not in raw and ' ' not in raw):
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    try:
        if parsed.tzinfo is None:
            parsed = (
                parsed.replace(tzinfo=timezone.utc)
                if datetime_input
                else local_datetime_to_utc(parsed)
            )
        else:
            parsed = parsed.astimezone(timezone.utc)
    except Exception:
        return None
    return parsed.isoformat(timespec='seconds')


def _weather_freshness_minutes(config_key, default):
    value = current_app.config.get(config_key, default) if has_app_context() else default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(parsed) or parsed < 0:
        return float(default)
    return parsed


def is_weather_observation_fresh(
    weather_data,
    now=None,
    max_age_minutes=None,
    future_tolerance_minutes=None,
):
    """校验观测时间存在、可解析，且没有过旧或明显来自未来。"""
    observed_at = normalize_weather_observed_at(
        _weather_field(weather_data, 'observed_at')
    )
    if observed_at is None:
        return False
    observed_dt = datetime.fromisoformat(observed_at)
    current_time = ensure_utc_aware(now) if now is not None else utcnow()
    if max_age_minutes is None:
        max_age_minutes = _weather_freshness_minutes(
            'WEATHER_OBSERVATION_MAX_AGE_MINUTES',
            120,
        )
    if future_tolerance_minutes is None:
        future_tolerance_minutes = _weather_freshness_minutes(
            'WEATHER_OBSERVATION_FUTURE_TOLERANCE_MINUTES',
            15,
        )
    age = current_time - observed_dt
    return (
        age <= timedelta(minutes=float(max_age_minutes))
        and age >= -timedelta(minutes=float(future_tolerance_minutes))
    )


def is_air_quality_observation_fresh(
    weather_data,
    now=None,
    max_age_minutes=None,
    future_tolerance_minutes=None,
):
    """独立校验空气质量观测时刻，禁止借用天气实况时间。"""
    if max_age_minutes is None:
        max_age_minutes = _weather_freshness_minutes(
            'AIR_QUALITY_OBSERVATION_MAX_AGE_MINUTES',
            120,
        )
    if future_tolerance_minutes is None:
        future_tolerance_minutes = _weather_freshness_minutes(
            'WEATHER_OBSERVATION_FUTURE_TOLERANCE_MINUTES',
            15,
        )
    return is_weather_observation_fresh(
        {'observed_at': _weather_field(weather_data, 'air_observed_at')},
        now=now,
        max_age_minutes=max_age_minutes,
        future_tolerance_minutes=future_tolerance_minutes,
    )

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
        # 仅允许管理员通过 URL 参数切换 demo 模式
        if hasattr(current_user, 'role') and current_user.is_authenticated and current_user.role == 'admin':
            if enabled:
                session['demo_mode'] = True
            else:
                session.pop('demo_mode', None)
            return enabled
        # 非管理员忽略 demo 参数
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
        'is_demo': True,
        'data_source': 'Demo'
    }


def weather_source_label(weather_data):
    """返回显式天气来源标签，缺少 provenance 时保持未知。"""
    if weather_data is None:
        return ''
    source = str(
        _weather_field(weather_data, 'data_source')
        or _weather_field(weather_data, 'source')
        or ''
    ).strip()
    if source:
        return source
    if _weather_field(weather_data, 'is_demo'):
        return 'Demo'
    if _weather_field(weather_data, 'is_mock'):
        return 'Mock'
    return ''


def _finite_weather_value(weather_data, field):
    """读取并校验天气数值；缺失、非有限与明显越界都返回 None。"""
    if weather_data is None:
        return None
    try:
        value = float(_weather_field(weather_data, field))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    lower, upper = _WEATHER_VALUE_RANGES[field]
    if not lower <= value <= upper:
        return None
    return value


def is_live_observational_weather(weather_data):
    """判断数据能否作为真实实况展示，允许 QWeather 与 Open-Meteo。"""
    if weather_data is None:
        return False
    if _weather_field(weather_data, 'is_mock') or _weather_field(weather_data, 'is_demo'):
        return False
    if weather_source_label(weather_data) not in _LIVE_WEATHER_SOURCES:
        return False
    if not is_weather_observation_fresh(weather_data):
        return False
    if _finite_weather_value(weather_data, 'temperature') is None:
        return False
    for field in ('temperature_max', 'temperature_min', 'humidity', 'pressure', 'wind_speed'):
        if (
            _weather_field(weather_data, field) is not None
            and _finite_weather_value(weather_data, field) is None
        ):
            return False
    tmax = _finite_weather_value(weather_data, 'temperature_max')
    tmin = _finite_weather_value(weather_data, 'temperature_min')
    if tmax is not None and tmin is not None and tmax < tmin:
        return False
    return True


def is_heat_action_weather_ready(weather_data):
    """判断实况是否足以进入不含疾病模型的基础温湿热行动计算。"""
    if not is_live_observational_weather(weather_data):
        return False
    values = {
        field: _finite_weather_value(weather_data, field)
        for field in ('temperature', 'temperature_max', 'temperature_min', 'humidity')
    }
    return (
        all(value is not None for value in values.values())
        and values['temperature_max'] >= values['temperature_min']
    )


def is_air_quality_available(weather_data):
    """判断 AQI 与 PM2.5 是否为可展示、可触发提醒的真实和风观测。"""
    if not is_live_observational_weather(weather_data):
        return False
    if weather_source_label(weather_data) != 'QWeather':
        return False
    if _weather_field(weather_data, 'air_quality_available') is not True:
        return False
    if not is_air_quality_observation_fresh(weather_data):
        return False
    if (
        _weather_field(weather_data, 'aqi_estimated')
        or _weather_field(weather_data, 'air_quality_estimated')
    ):
        return False
    return (
        _finite_weather_value(weather_data, 'aqi') is not None
        and _finite_weather_value(weather_data, 'pm25') is not None
    )


def normalize_health_model_weather(weather_data):
    """归一化健康模型天气；不可信空气字段统一清空。"""
    if not isinstance(weather_data, dict):
        return None
    if not is_qweather_production_ready(weather_data):
        return None
    normalized = dict(weather_data)
    if not is_air_quality_available(weather_data):
        normalized['aqi'] = None
        normalized['pm25'] = None
        normalized['air_observed_at'] = None
        normalized['air_quality_available'] = False
        normalized['aqi_estimated'] = False
        normalized['air_quality_estimated'] = False
    return normalized


def is_qweather_online_weather(weather_data):
    """校验新鲜和风实况的来源与基础观测。"""
    return (
        is_live_observational_weather(weather_data)
        and weather_source_label(weather_data) == 'QWeather'
    )


def is_qweather_production_ready(weather_data):
    """判断和风实况是否完整到可进入持久化与健康风险链。"""
    if not (
        is_qweather_online_weather(weather_data)
        and is_heat_action_weather_ready(weather_data)
    ):
        return False
    if any(
        _finite_weather_value(weather_data, field) is None
        for field in ('pressure', 'wind_speed')
    ):
        return False
    try:
        quality_version = int(_weather_field(weather_data, 'quality_version'))
    except (TypeError, ValueError):
        return False
    if quality_version < 1:
        return False
    condition = str(_weather_field(weather_data, 'weather_condition') or '').strip()
    return condition.lower() not in {'', '未知', 'unknown', 'none', 'n/a', '--'}


def is_complete_qweather_weather(weather_data):
    """兼容旧调用：完整和风天气等同生产门禁通过。"""
    return is_qweather_production_ready(weather_data)


def compact_assessment_weather_condition(weather_data):
    """生成可查询的紧凑天气摘要，严格适配历史 VARCHAR(100)。"""
    source = weather_data if isinstance(weather_data, dict) else {}

    def bounded(name, minimum, maximum, digits=1):
        try:
            value = float(source.get(name))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            return None
        return round(value, digits)

    summary = {
        't': bounded('temperature', -100, 100),
        'hi': bounded('temperature_max', -100, 100),
        'lo': bounded('temperature_min', -100, 100),
        'rh': bounded('humidity', 0, 100),
        'aqi': bounded('aqi', 0, 1000, 0),
        'wx': str(
            source.get('weather_condition')
            or source.get('condition')
            or ''
        )[:12],
    }
    compact = json.dumps(
        {key: value for key, value in summary.items() if value not in (None, '')},
        ensure_ascii=False,
        separators=(',', ':'),
    )
    if len(compact) <= 100:
        return compact

    # 极端输入下只保留核心数值，确保仍是完整 JSON。
    return json.dumps(
        {
            key: summary[key]
            for key in ('t', 'hi', 'lo', 'rh')
            if summary[key] is not None
        },
        separators=(',', ':'),
    )


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
    canonical_location = current_app.config.get('QWEATHER_CANONICAL_LOCATION')
    if canonical_location:
        # 网站只服务都昌县：保留县内社区名称，移除北京、上海等旧测试城市。
        options.update(current_app.config.get('COMMUNITY_COORDS_GCJ', {}).keys())
    else:
        options.update(current_app.config.get('CITY_LOCATION_MAP', {}).keys())
    try:
        communities = Community.query.with_entities(Community.name).all()
        options.update([c[0] for c in communities if c and c[0]])
    except Exception as exc:
        logger.warning("Failed to load community locations: %s", exc)
    options = {opt.strip() for opt in options if opt and isinstance(opt, str)}
    default_city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    options.update({default_city, DEFAULT_CITY_LABEL})
    ordered = []
    preferred = (default_city, DEFAULT_CITY_LABEL)
    if not canonical_location:
        preferred += ('北京', '上海', '广州', '深圳')
    for item in preferred:
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
    # Allow passing raw QWeather location id (digits) or lon,lat.
    if location.isdigit():
        return location
    if ',' in location:
        parts = [p.strip() for p in location.split(',')]
        if len(parts) == 2:
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    return f'{lon},{lat}'
            except (TypeError, ValueError):
                pass
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
    """返回规范化定位；普通请求不在读取路径中隐式修改账号。

    注意：
    - 游客定位继续保存在当前浏览器会话
    - 正式账号只通过显式的定位更新入口持久化，避免与账号注销并发后恢复数据
    """
    location = get_user_location_value()
    normalized = normalize_location_name(location)
    if normalized != location and current_user.is_authenticated:
        if is_guest_user(current_user):
            from core.guest import build_guest_profile
            profile = build_guest_profile()
            profile['community'] = normalized
            session['guest_profile'] = profile
    return normalized


def resolve_weather_city_label(location):
    """显示天气来源城市"""
    if current_app.config.get('QWEATHER_CANONICAL_LOCATION'):
        return DEFAULT_CITY_LABEL
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


def _weather_cache_location(location):
    """把所有县内页面请求归并到唯一的都昌县天气缓存。"""
    normalized = normalize_location_name(location)
    if current_app.config.get('QWEATHER_CANONICAL_LOCATION'):
        return DEFAULT_CITY_LABEL
    return normalized


def canonical_weather_location(location=None):
    """返回天气数据身份；启用 canonical 时始终是都昌县。"""
    if has_app_context() and current_app.config.get('QWEATHER_CANONICAL_LOCATION'):
        return DEFAULT_CITY_LABEL
    return normalize_location_name(location)


def _canonicalize_weather_payload(weather_data):
    """复制天气 payload，并把天气地点身份收敛到 canonical。"""
    if not isinstance(weather_data, dict):
        return weather_data
    result = dict(weather_data)
    if has_app_context() and current_app.config.get('QWEATHER_CANONICAL_LOCATION'):
        result['location'] = DEFAULT_CITY_LABEL
        result['weather_location'] = DEFAULT_CITY_LABEL
    return result


def _weather_payload_for_caller(weather_data, audience_location):
    """天气身份与受众地点分离，受众备注不进入共享 canonical 缓存。"""
    result = _canonicalize_weather_payload(weather_data)
    if not isinstance(result, dict):
        return result
    if has_app_context() and current_app.config.get('QWEATHER_CANONICAL_LOCATION'):
        audience = str(audience_location or '').strip()
        if audience and audience != DEFAULT_CITY_LABEL:
            result['audience_location'] = audience
    return result


def _cached_weather_payload_usable(weather_data):
    """真实缓存必须通过观测时间门；显式演示数据只用于演示页面。"""
    if not isinstance(weather_data, dict) or not weather_data:
        return False
    if weather_source_label(weather_data) in _LIVE_WEATHER_SOURCES:
        return is_weather_observation_fresh(weather_data)
    return bool(weather_data.get('is_mock') or weather_data.get('is_demo'))


def get_weather_with_cache(location, ttl_minutes=None, cache_only=True):
    """获取带缓存的天气数据，普通调用默认只读真实缓存。"""
    if is_demo_mode():
        # 演示页面可以继续展示本地样例；后台 cache-only 任务仍拒绝 mock。
        if cache_only and not has_request_context():
            return {}, False
        return get_demo_weather_data(), False
    # HTTP 请求一律只读。显式联网刷新只允许后台任务或命令行诊断使用。
    cache_only = bool(cache_only) or has_request_context()
    audience_location = normalize_location_name(location)
    location = _weather_cache_location(location)
    if ttl_minutes is None:
        ttl_minutes = current_app.config.get('WEATHER_CACHE_TTL_MINUTES', WEATHER_CACHE_TTL_MINUTES)
    ttl_seconds = max(int(ttl_minutes * 60), 60)
    redis_client = _get_redis_client()
    redis_key = _redis_cache_key('weather:current', location)
    redis_payload = _redis_get_json(redis_client, redis_key, {})
    if redis_payload is not None and _cached_weather_payload_usable(redis_payload):
        return _weather_payload_for_caller(redis_payload, audience_location), True
    now = utcnow()
    cache = None
    try:
        cache = WeatherCache.query.filter_by(location=location).order_by(
            WeatherCache.fetched_at.desc(),
            WeatherCache.id.desc()
        ).first()
        if cache and cache.fetched_at:
            cached_weather = safe_json_loads(cache.payload, {})
            cached_weather_is_real = not bool(cache.is_mock) and _cached_weather_payload_usable(
                cached_weather
            )
            # 确保从数据库读取的 datetime 是 UTC aware 的
            if now - ensure_utc_aware(cache.fetched_at) <= timedelta(minutes=ttl_minutes):
                if not cache_only or cached_weather_is_real:
                    return _weather_payload_for_caller(
                        cached_weather,
                        audience_location,
                    ), True
            # 实况超过新鲜度期限后停止参与页面展示和风险计算。
            # 离线小程序快照由独立持久层保留原始时间并明确标记 stale。
    except Exception as exc:
        logger.warning("天气缓存不可用，已跳过缓存: %s", exc)
        db.session.rollback()
    if cache_only:
        # cache-only 缺少真实缓存时直接返回，不调用 fetcher，也不写入 fallback。
        return {}, False
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
    return _weather_payload_for_caller(weather_data, audience_location), False


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
        'air_quality_available': False,
        'air_observed_at': None,
        'location': DEFAULT_CITY_LABEL,
        'is_mock': True,
        'data_source': 'Mock',
    }


def get_forecast_with_cache(location, days=7, ttl_minutes=None, cache_only=True):
    """获取带缓存的天气预报，普通调用默认只读。"""
    if is_demo_mode():
        # 演示页面只生成本地样例，不访问外网或写缓存。
        if cache_only and not has_request_context():
            return [], False
        return get_demo_forecast_data(days=days), True
    # 防止未来路由误传 cache_only=False 后重新引入按请求抓取。
    cache_only = bool(cache_only) or has_request_context()
    location = _weather_cache_location(location)
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
            cached_forecast = safe_json_loads(cache.payload, [])
            # 确保从数据库读取的 datetime 是 UTC aware 的
            if now - ensure_utc_aware(cache.fetched_at) <= timedelta(minutes=ttl_minutes):
                return cached_forecast, True
            # 只读链路可复用最后一份缓存，等待后台周期刷新。
            if cache_only and cached_forecast and not bool(cache.is_mock):
                return cached_forecast, True
    except Exception as exc:
        logger.warning("预报缓存不可用，已跳过缓存: %s", exc)
        db.session.rollback()
    if cache_only:
        return [], False
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


def _qweather_forecast_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except Exception:
        return None


def _valid_qweather_only_forecast(forecast_data, days=None, expected_start_date=None):
    """校验缓存是否确实来自和风天气，避免复用 mock 或融合缓存。"""
    if not isinstance(forecast_data, list) or not forecast_data:
        return False
    if days is not None and len(forecast_data) < int(days):
        return False
    if expected_start_date is not None:
        expected_start_date = _qweather_forecast_date(expected_start_date)
    previous_date = None
    for index, item in enumerate(forecast_data[:days or len(forecast_data)]):
        if not isinstance(item, dict):
            return False
        if item.get('is_mock') or item.get('is_demo'):
            return False
        if item.get('data_source') != 'QWeather':
            return False
        tmax = _finite_weather_value(item, 'temperature_max')
        tmin = _finite_weather_value(item, 'temperature_min')
        humidity = _finite_weather_value(item, 'humidity')
        wind_speed = _finite_weather_value(item, 'wind_speed')
        condition = str(item.get('condition') or '').strip()
        try:
            tmean = float(item.get('temperature_mean'))
        except (TypeError, ValueError):
            return False
        if (
            tmax is None
            or tmin is None
            or humidity is None
            or wind_speed is None
            or not math.isfinite(tmean)
            or tmax < tmin
            or not tmin <= tmean <= tmax
            or condition.lower() in {'', '未知', 'unknown', 'none', 'n/a', '--'}
        ):
            return False
        item_date = _qweather_forecast_date(item.get('date') or item.get('forecast_date'))
        if item_date is None:
            return False
        if expected_start_date is not None:
            if item_date != expected_start_date + timedelta(days=index):
                return False
        elif previous_date is not None and item_date != previous_date + timedelta(days=1):
            return False
        previous_date = item_date
    return True


def _parse_qweather_only_cache_payload(payload, days, expected_start_date=None):
    if not isinstance(payload, dict):
        return None, None
    forecast_data = payload.get('daily') or payload.get('forecast')
    meta = payload.get('meta')
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None, None
    if not _valid_qweather_only_forecast(
        forecast_data,
        days=days,
        expected_start_date=expected_start_date,
    ):
        return None, None
    return forecast_data[:days], meta


def get_qweather_forecast_with_cache(
    location,
    days=7,
    ttl_minutes=None,
    cache_only=True,
    fetcher=None,
    force_refresh=False,
):
    """获取和风-only天气预报，普通调用默认只读真实缓存。"""
    try:
        days = max(1, min(int(days or 7), 7))
    except Exception:
        days = 7
    # 请求上下文始终只读；已配置和风的后台同步可在 DEMO_MODE 下显式刷新。
    request_bound = has_request_context()
    cache_only = bool(cache_only) or request_bound
    # HTTP 永远不能借 force_refresh 打开上游；该开关只供受管后台周期使用。
    force_refresh = bool(force_refresh) and not request_bound and not cache_only
    if is_demo_mode() and cache_only:
        return [], False, {'source': 'QWeather', 'error': 'demo_mode'}

    location = _weather_cache_location(location)
    if ttl_minutes is None:
        ttl_minutes = current_app.config.get('FORECAST_CACHE_TTL_MINUTES', 20)
    ttl_seconds = max(int(ttl_minutes * 60), 60)
    cache_location = f'qweather-only:{location}'
    expected_start_date = today_local()
    redis_client = _get_redis_client()
    redis_key = _redis_cache_key('weather:qweather_forecast', location, days)
    if not force_refresh:
        redis_payload = _redis_get_json(redis_client, redis_key, {})
        forecast_data, meta = _parse_qweather_only_cache_payload(
            redis_payload,
            days,
            expected_start_date=expected_start_date,
        )
        if forecast_data is not None:
            return forecast_data, True, meta

    now = utcnow()
    cache = None
    try:
        cache = ForecastCache.query.filter_by(location=cache_location, days=days).order_by(
            ForecastCache.fetched_at.desc(),
            ForecastCache.id.desc()
        ).first()
        if cache and cache.fetched_at and not bool(cache.is_mock):
            forecast_data, meta = _parse_qweather_only_cache_payload(
                safe_json_loads(cache.payload, {}),
                days,
                expected_start_date=expected_start_date,
            )
            if forecast_data is not None and not force_refresh:
                cache_fetched_at = ensure_utc_aware(cache.fetched_at)
                meta = dict(meta or {})
                meta.setdefault('fetched_at', cache_fetched_at.isoformat())
                meta.setdefault(
                    'expires_at',
                    (cache_fetched_at + timedelta(seconds=ttl_seconds)).isoformat(),
                )
                if now - ensure_utc_aware(cache.fetched_at) <= timedelta(minutes=ttl_minutes):
                    return forecast_data, True, meta
                # 日期仍有效时允许 HTTP 复用过期缓存，等待后台同步刷新。
                if cache_only:
                    stale_meta = dict(meta or {})
                    stale_meta['stale'] = True
                    return forecast_data, True, stale_meta
    except Exception as exc:
        logger.warning("和风-only预报缓存不可用，已跳过缓存: %s", exc)
        db.session.rollback()

    if cache_only:
        return [], False, {'source': 'QWeather', 'error': 'cache_miss'}

    weather_service = fetcher or get_weather_fetcher()
    meta = {'source': 'QWeather'}
    forecast_data = []
    try:
        if weather_service is None or not hasattr(weather_service, 'get_qweather_daily_forecast'):
            raise RuntimeError("QWeather forecast fetcher not configured")
        result = weather_service.get_qweather_daily_forecast(location, days=days)
        if isinstance(result, dict):
            forecast_data = result.get('daily') or []
            meta = result.get('meta') or meta
            if not result.get('success') and not forecast_data:
                return [], False, meta
        else:
            forecast_data = result or []
    except Exception as exc:
        logger.warning("获取和风-only预报失败: %s", exc)
        return [], False, {'source': 'QWeather', 'error': 'fetch_failed'}

    if not _valid_qweather_only_forecast(
        forecast_data,
        days=days,
        expected_start_date=expected_start_date,
    ):
        meta.setdefault('error', 'qweather_data_incomplete')
        return [], False, meta

    source_fetched_at = utcnow()
    source_expires_at = source_fetched_at + timedelta(seconds=ttl_seconds)
    meta = dict(meta or {})
    meta['fetched_at'] = source_fetched_at.isoformat()
    meta['expires_at'] = source_expires_at.isoformat()

    cache_payload = {
        'daily': forecast_data[:days],
        'meta': meta,
    }
    try:
        _redis_set_json(redis_client, redis_key, ttl_seconds, cache_payload)
        if cache:
            cache.payload = json.dumps(cache_payload, ensure_ascii=False)
            cache.fetched_at = source_fetched_at
            cache.is_mock = False
        else:
            cache = ForecastCache(
                location=cache_location,
                days=days,
                fetched_at=source_fetched_at,
                payload=json.dumps(cache_payload, ensure_ascii=False),
                is_mock=False
            )
            db.session.add(cache)
        db.session.commit()
    except Exception as exc:
        logger.warning("和风-only预报缓存写入失败，已忽略: %s", exc)
        db.session.rollback()
    return forecast_data[:days], False, meta


def _valid_openmeteo_only_forecast(
    forecast_data,
    days=None,
    expected_start_date=None,
):
    """校验 Open-Meteo 基础逐日天气，允许缺少健康模型所需字段。"""
    if not isinstance(forecast_data, list) or not forecast_data:
        return False
    if days is not None and len(forecast_data) < int(days):
        return False
    if expected_start_date is not None:
        expected_start_date = _qweather_forecast_date(expected_start_date)
    previous_date = None
    for index, item in enumerate(forecast_data[:days or len(forecast_data)]):
        if not isinstance(item, dict):
            return False
        if item.get('is_mock') or item.get('is_demo'):
            return False
        if item.get('data_source') != 'Open-Meteo':
            return False
        tmax = _finite_weather_value(item, 'temperature_max')
        tmin = _finite_weather_value(item, 'temperature_min')
        precipitation = item.get('precip_probability')
        if precipitation is not None:
            try:
                precipitation = float(precipitation)
            except (TypeError, ValueError):
                return False
        condition = str(item.get('condition') or '').strip()
        if (
            tmax is None
            or tmin is None
            or tmax < tmin
            or (
                precipitation is not None
                and (
                    not math.isfinite(precipitation)
                    or not 0 <= precipitation <= 100
                )
            )
            or condition.lower() in {'', '未知', 'unknown', 'none', 'n/a', '--'}
        ):
            return False
        item_date = _qweather_forecast_date(
            item.get('date') or item.get('forecast_date')
        )
        if item_date is None:
            return False
        if expected_start_date is not None:
            if item_date != expected_start_date + timedelta(days=index):
                return False
        elif previous_date is not None and item_date != previous_date + timedelta(days=1):
            return False
        previous_date = item_date
    return True


def _parse_openmeteo_only_cache_payload(payload, days, expected_start_date=None):
    if not isinstance(payload, dict):
        return None, None
    forecast_data = payload.get('daily') or payload.get('forecast')
    meta = payload.get('meta') or {}
    if not isinstance(meta, dict):
        return None, None
    if not _valid_openmeteo_only_forecast(
        forecast_data,
        days=days,
        expected_start_date=expected_start_date,
    ):
        return None, None
    meta = dict(meta)
    meta['source'] = 'Open-Meteo'
    return forecast_data[:days], meta


def get_openmeteo_forecast_with_cache(
    location,
    days=7,
    ttl_minutes=None,
    cache_only=True,
    fetcher=None,
    force_refresh=False,
):
    """获取 Open-Meteo-only 预报；HTTP 调用始终只读独立缓存。"""
    try:
        days = max(1, min(int(days or 7), 7))
    except Exception:
        days = 7
    request_bound = has_request_context()
    cache_only = bool(cache_only) or request_bound
    force_refresh = bool(force_refresh) and not request_bound and not cache_only
    if is_demo_mode() and cache_only:
        return [], False, {'source': 'Open-Meteo', 'error': 'demo_mode'}

    location = _weather_cache_location(location)
    if ttl_minutes is None:
        ttl_minutes = current_app.config.get('FORECAST_CACHE_TTL_MINUTES', 20)
    ttl_seconds = max(int(ttl_minutes * 60), 60)
    cache_location = f'openmeteo-only:{location}'
    expected_start_date = today_local()
    redis_client = _get_redis_client()
    redis_key = _redis_cache_key('weather:openmeteo_forecast', location, days)
    if not force_refresh:
        redis_payload = _redis_get_json(redis_client, redis_key, {})
        forecast_data, meta = _parse_openmeteo_only_cache_payload(
            redis_payload,
            days,
            expected_start_date=expected_start_date,
        )
        if forecast_data is not None:
            return forecast_data, True, meta

    now = utcnow()
    cache = None
    try:
        cache = ForecastCache.query.filter_by(
            location=cache_location,
            days=days,
        ).order_by(
            ForecastCache.fetched_at.desc(),
            ForecastCache.id.desc(),
        ).first()
        if cache and cache.fetched_at and not bool(cache.is_mock):
            forecast_data, meta = _parse_openmeteo_only_cache_payload(
                safe_json_loads(cache.payload, {}),
                days,
                expected_start_date=expected_start_date,
            )
            if forecast_data is not None and not force_refresh:
                cache_fetched_at = ensure_utc_aware(cache.fetched_at)
                meta = dict(meta or {})
                meta.setdefault('fetched_at', cache_fetched_at.isoformat())
                meta.setdefault(
                    'expires_at',
                    (cache_fetched_at + timedelta(seconds=ttl_seconds)).isoformat(),
                )
                if now - cache_fetched_at <= timedelta(minutes=ttl_minutes):
                    return forecast_data, True, meta
                if cache_only:
                    meta['stale'] = True
                    return forecast_data, True, meta
    except Exception as exc:
        logger.warning("Open-Meteo-only预报缓存不可用，已跳过缓存: %s", exc)
        db.session.rollback()

    if cache_only:
        return [], False, {'source': 'Open-Meteo', 'error': 'cache_miss'}

    weather_service = fetcher or get_weather_fetcher()
    meta = {'source': 'Open-Meteo'}
    forecast_data = []
    try:
        if weather_service is None or not hasattr(
            weather_service,
            'get_openmeteo_daily_forecast',
        ):
            raise RuntimeError("Open-Meteo forecast fetcher not configured")
        result = weather_service.get_openmeteo_daily_forecast(location, days=days)
        if isinstance(result, dict):
            forecast_data = result.get('daily') or []
            meta = dict(result.get('meta') or meta)
            if not result.get('success') and not forecast_data:
                meta.setdefault('source', 'Open-Meteo')
                return [], False, meta
        else:
            forecast_data = result or []
    except Exception as exc:
        logger.warning("获取Open-Meteo-only预报失败: %s", exc)
        return [], False, {'source': 'Open-Meteo', 'error': 'fetch_failed'}

    if not _valid_openmeteo_only_forecast(
        forecast_data,
        days=days,
        expected_start_date=expected_start_date,
    ):
        meta.setdefault('source', 'Open-Meteo')
        meta.setdefault('error', 'openmeteo_data_incomplete')
        return [], False, meta

    source_fetched_at = utcnow()
    meta = dict(meta or {})
    meta['source'] = 'Open-Meteo'
    meta['fetched_at'] = source_fetched_at.isoformat()
    meta['expires_at'] = (
        source_fetched_at + timedelta(seconds=ttl_seconds)
    ).isoformat()
    cache_payload = {'daily': forecast_data[:days], 'meta': meta}
    try:
        _redis_set_json(redis_client, redis_key, ttl_seconds, cache_payload)
        if cache:
            cache.payload = json.dumps(cache_payload, ensure_ascii=False)
            cache.fetched_at = source_fetched_at
            cache.is_mock = False
        else:
            cache = ForecastCache(
                location=cache_location,
                days=days,
                fetched_at=source_fetched_at,
                payload=json.dumps(cache_payload, ensure_ascii=False),
                is_mock=False,
            )
            db.session.add(cache)
        db.session.commit()
        meta['cache_persisted'] = True
    except Exception as exc:
        logger.warning("Open-Meteo-only预报缓存写入失败，已忽略: %s", exc)
        db.session.rollback()
        meta['cache_persisted'] = False
        meta['error'] = 'cache_write_failed'
    return forecast_data[:days], False, meta


def get_consecutive_hot_days(
    location,
    target_date=None,
    today_max=None,
    threshold=None,
    max_days=7,
    weather_data=None,
):
    """只从可信 canonical 和风历史计算连续高温；其他来源最多计算当天。"""
    if is_demo_mode():
        return 5
    location = canonical_weather_location(location)
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

    trusted_today = WeatherData.query.filter(
        WeatherData.date == target_date,
        WeatherData.location == location,
        WeatherData.data_source == 'QWeather',
        WeatherData.quality_version >= 1,
    ).order_by(WeatherData.id.desc()).first()
    if today_max is None and trusted_today is not None:
        today_max = trusted_today.temperature_max
    if today_max is None:
        return 0
    try:
        today_max = float(today_max)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(today_max) or today_max < threshold:
        return 0

    count = 1
    allow_history = (
        is_qweather_production_ready(weather_data)
        if weather_data is not None
        else trusted_today is not None
    )
    if not allow_history:
        return count
    if max_days is None or max_days <= 1:
        return count
    lookback = max_days - 1
    records = WeatherData.query.filter(
        WeatherData.location == location,
        WeatherData.date < target_date,
        WeatherData.data_source == 'QWeather',
        WeatherData.quality_version >= 1,
    ).order_by(WeatherData.date.desc()).limit(lookback).all()
    expected = target_date - timedelta(days=1)
    for record in records:
        if record.date != expected:
            break
        try:
            record_max = float(record.temperature_max)
        except (TypeError, ValueError):
            break
        if not math.isfinite(record_max) or record_max < threshold:
            break
        count += 1
        expected = expected - timedelta(days=1)
    return count
