# -*- coding: utf-8 -*-
"""API routes."""
import json
import logging
import math

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from core.constants import DEFAULT_CITY_LABEL
from core.guest import is_guest_user
from core.notifications import create_notification
from core.security import _is_guest_subject, csrf_failure_response, validate_csrf
from core.time_utils import now_local, today_local
from core.weather import (
    ensure_user_location_valid,
    get_qweather_forecast_with_cache,
    get_weather_fetcher,
    get_weather_with_cache,
    is_air_quality_available,
    is_qweather_production_ready,
    normalize_location_name,
    weather_source_label
)
from core.db_models import Community, FamilyMember, Pair
from core.extensions import db
from core.usage import log_usage_event
from utils.parsers import parse_date, parse_int, safe_json_loads
from utils.error_handlers import handle_api_exception
from utils.validators import sanitize_input

logger = logging.getLogger(__name__)


GENERIC_ERROR_MESSAGE = '服务暂时不可用，请稍后再试'

# 社区画像敏感字段：未登录与游客（is_guest）一律剥离，与正式用户分叉
_COMMUNITY_LIST_PUBLIC_KEYS = ('name', 'vulnerability_level')
_COMMUNITY_RISK_MAP_PUBLIC_KEYS = (
    'name', 'latitude', 'longitude', 'risk_level',
)
# vulnerability 单村匿名公开键：与 list 对齐，仅名称 + 粗粒度等级
_COMMUNITY_VULN_PUBLIC_KEYS = ('name', 'vulnerability_level')
# 禁止出现在 vulnerability 脱敏视图的敏感键（白名单投影后作二次断言）
_COMMUNITY_DEMOGRAPHIC_KEYS = frozenset({
    'id',
    'location',
    'population',
    'elderly_ratio',
    'chronic_disease_ratio',
    'vulnerability_index',
    'vulnerability_details',
    'green_space_ratio',
    'heat_island_index',
    'medical_accessibility',
    'baseline_visits',
    'latitude',
    'longitude',
})


def _is_anonymous_or_guest():
    """未登录或 is_guest：与正式用户分叉，不得看社区人口/老龄/慢病等画像。

    规则（与 V1-A09 / P4 约定一致）：
    - 未登录（``current_user.is_authenticated`` 为假）→ True
    - 游客（``is_guest_user`` / ``is_guest`` / role=guest / id 前缀 guest:）→ True
      **与未登录完全同一策略**，避免 ``is_authenticated=True`` 的 guest 绕过脱敏
    - 正式登录用户 → False
    """
    if not getattr(current_user, 'is_authenticated', False):
        return True
    # is_guest_user 覆盖 GuestUser.is_guest；_is_guest_subject 再兜 role / id 前缀
    if is_guest_user(current_user) or _is_guest_subject():
        return True
    return False


def _viewer_can_see_community_demographics():
    """是否可看社区人口/老龄/慢病等画像字段（正式用户 True）。"""
    return not _is_anonymous_or_guest()


def _redact_community_list_item(item):
    """社区 list 条目脱敏：仅保留 name（及可选 vulnerability_level）。"""
    if not isinstance(item, dict):
        return item
    return {k: item.get(k) for k in _COMMUNITY_LIST_PUBLIC_KEYS if k in item}


def _redact_community_risk_map_item(item):
    """risk-map 条目脱敏：保留地名/坐标/风险等级，去掉 population 与精确 VI。"""
    if not isinstance(item, dict):
        return item
    return {k: item.get(k) for k in _COMMUNITY_RISK_MAP_PUBLIC_KEYS if k in item}


def _community_vulnerability_summary(profile, community_name):
    """单村 vulnerability 脱敏摘要：仅 name + vulnerability_level。"""
    if not isinstance(profile, dict):
        profile = {}
    details = profile.get('vulnerability_details')
    level = None
    if isinstance(details, dict):
        level = details.get('level') or details.get('vulnerability_level')
    if level is None:
        level = profile.get('vulnerability_level') or profile.get('risk_level')
    out = {'name': profile.get('name') or community_name}
    if level is not None:
        out['vulnerability_level'] = level
    # 白名单二次裁剪，防止误塞敏感键
    return {k: out[k] for k in _COMMUNITY_VULN_PUBLIC_KEYS if k in out}


def _redact_community_profile(profile, community_name=None):
    """单村 vulnerability profile 脱敏：白名单仅 name + vulnerability_level。

    说明：
    - 不用「删敏感键留其余」：旧实现会漏出 id / location / baseline_visits 等。
    - vulnerability_level 从 vulnerability_details.level 或顶层字段提取。
    """
    return _community_vulnerability_summary(profile, community_name)

INPUT_EXCEPTIONS = (ValueError, KeyError, TypeError, json.JSONDecodeError)
SERVICE_EXCEPTIONS = (RuntimeError, FileNotFoundError, OSError, TimeoutError)
API_EXCEPTIONS = INPUT_EXCEPTIONS + SERVICE_EXCEPTIONS


def _weather_unavailable_response(weather_data=None, message=None):
    """风险计算只允许使用真实和风实况；不可用时停止生成结论。"""
    source = weather_source_label(weather_data)
    payload = {
        'success': False,
        'error': 'weather_unavailable',
        'message': message or '天气正在更新，风险等级暂不显示，请稍后再试。',
        'weather_source': source or 'unknown',
        'is_mock': bool(weather_data.get('is_mock')) if isinstance(weather_data, dict) else None,
    }
    return jsonify(payload), 503


def _validate_qweather_for_risk(weather_data, context, require_air_quality=False):
    """校验风险计算输入，防止 demo/mock/fallback 数据污染结果。"""
    production_ready = is_qweather_production_ready(weather_data)
    air_ready = is_air_quality_available(weather_data)
    if production_ready and (air_ready or not require_air_quality):
        return None
    logger.warning(
        "%s rejected weather data: source=%s is_mock=%s production_ready=%s air_ready=%s",
        context,
        weather_source_label(weather_data),
        weather_data.get('is_mock') if isinstance(weather_data, dict) else None,
        production_ready,
        air_ready,
    )
    return _weather_unavailable_response(weather_data)


def _server_weather_for_risk(data):
    """生产风险链只信任服务端天气；测试环境保留显式 fixture 注入。"""
    if (
        current_app.config.get('TESTING')
        and isinstance(data, dict)
        and isinstance(data.get('weather'), dict)
    ):
        return dict(data['weather'])
    city = sanitize_input((data or {}).get('city'), max_length=100)
    if city:
        city = normalize_location_name(city)
    elif current_user.is_authenticated:
        city = ensure_user_location_valid()
    else:
        city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    weather_data, _ = get_weather_with_cache(city)
    return weather_data

PILOT_EVENT_TYPES = {
    'pair_created',
    'elder_profile_created',
    'elder_profile_updated',
    'template_view',
    'template_copy',
    'push_sent',
    'push_failed',
    'push_click',
    'feedback_submitted',
    'help_flagged',
    'checkin_confirmed',
    'wxoa_land',
}
_PILOT_EVENT_TYPES = PILOT_EVENT_TYPES
CLIENT_EVENT_TYPES = {
    'template_view',
    'template_copy',
    'feedback_submitted',
}
EVENT_META_MAX_CHARS = 2048


def _parse_positive_event_id(value, error_code):
    """严格解析客户端埋点关联 ID，拒绝布尔值和小数被隐式转换。"""
    if isinstance(value, bool):
        raise ValueError(error_code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise ValueError(error_code)
    if parsed <= 0:
        raise ValueError(error_code)
    return parsed


def _handle_api_error(exc, context_msg, include_details=None):
    """统一处理API异常（兼容旧调用）"""
    return handle_api_exception(
        exc,
        context_msg,
        log=logger,
        include_details=include_details,
    )


def _api_csrf_protect():
    if request.method == 'POST' and not validate_csrf():
        return csrf_failure_response()


def _normalize_sunshine_seconds(payload):
    """Normalize sunshine duration to seconds.

    Accepted input fields (API contract):
    - sunshine_duration_seconds (preferred)
    - sunshine_duration_hours

    Legacy compatibility:
    - sunshine_hours is treated as *hours* only.
    - values > 24 are considered ambiguous and rejected to avoid unit confusion.
    """
    default_seconds = 20000.0
    if not isinstance(payload, dict):
        return default_seconds

    if payload.get('sunshine_duration_seconds') is not None:
        raw = payload.get('sunshine_duration_seconds')
        as_hours = False
    elif payload.get('sunshine_duration_hours') is not None:
        raw = payload.get('sunshine_duration_hours')
        as_hours = True
    elif payload.get('sunshine_hours') is not None:
        raw = payload.get('sunshine_hours')
        as_hours = True
    else:
        raw = default_seconds
        as_hours = False

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default_seconds

    if as_hours:
        if value > 24.0:
            raise ValueError(
                "sunshine_hours 已废弃且存在单位歧义；请改用 "
                "sunshine_duration_seconds 或 sunshine_duration_hours"
            )
        value *= 3600.0

    value = max(0.0, min(value, 86400.0))
    return value


# ======================== 天气/社区基础API ========================

def _api_current_weather():
    """获取当前天气（调用实时API）"""
    location = sanitize_input(request.args.get('location', '都昌'), max_length=100)
    location = normalize_location_name(location)

    # 使用带缓存的天气获取函数
    weather_data, from_cache = get_weather_with_cache(location)

    if weather_data:
        air_quality_available = is_air_quality_available(weather_data)
        return jsonify({
            'success': True,
            'data': {
                'temperature': weather_data.get('temperature'),
                'temperature_max': weather_data.get('temperature_max'),
                'temperature_min': weather_data.get('temperature_min'),
                'humidity': weather_data.get('humidity'),
                'pressure': weather_data.get('pressure'),
                'condition': weather_data.get('weather_condition'),
                'wind_speed': weather_data.get('wind_speed'),
                'aqi': weather_data.get('aqi') if air_quality_available else None,
                'pm25': weather_data.get('pm25') if air_quality_available else None,
                'air_quality_available': air_quality_available,
                'observed_at': weather_data.get('observed_at'),
                'location': weather_data.get('location'),
                'is_mock': weather_data.get('is_mock', False),
                'data_source': weather_source_label(weather_data),
                'from_cache': from_cache
            }
        })

    return jsonify({'success': False, 'message': '暂无天气数据'})


def _api_weather_nowcast():
    """获取未来小时级降水时间轴（短临预报）"""
    location = sanitize_input(request.args.get('location'), max_length=100)
    if location:
        location = normalize_location_name(location)
    else:
        location = ensure_user_location_valid()

    try:
        hours = int(request.args.get('hours', 6))
    except Exception:
        hours = 6
    hours = max(1, min(hours, 24))

    weather_service = get_weather_fetcher()
    if weather_service is None or not hasattr(weather_service, 'get_short_term_nowcast'):
        return jsonify({'success': False, 'message': '短临服务未启用', 'data': {'available': False, 'timeline': []}})

    try:
        nowcast = weather_service.get_short_term_nowcast(location, hours=hours)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        logger.warning("Nowcast fetch failed: %s", exc)
        nowcast = {'available': False, 'timeline': [], 'reason': 'fetch_failed'}

    return jsonify({'success': True, 'data': nowcast})


def api_v1_current_weather():
    """获取当前天气（v1）"""
    return _api_current_weather()


def api_current_weather():
    """获取当前天气（兼容）"""
    return api_v1_current_weather()


def _api_community_risk_map():
    """获取社区风险地图数据。

    匿名/游客：保留 name、risk_level 色阶、地图所需 lat/lon；去掉 population
    与精确 vulnerability_index（与 list 同一主体判定）。
    正式用户：附带 population、vulnerability_index。
    """
    communities = Community.query.all()
    # 正式用户可见人口/精确 VI；匿名与游客必须去 population
    rich = not _is_anonymous_or_guest()
    data = []

    for community in communities:
        item = {
            'name': community.name,
            'latitude': community.latitude,
            'longitude': community.longitude,
            'risk_level': community.risk_level,
        }
        if rich:
            item['vulnerability_index'] = community.vulnerability_index
            item['population'] = community.population
        else:
            # 兜底白名单（防后续误加敏感键）
            item = _redact_community_risk_map_item(item)
        data.append(item)

    return jsonify({'success': True, 'data': data})


def api_v1_community_risk_map():
    """获取社区风险地图数据（v1）"""
    return _api_community_risk_map()


def api_community_risk_map():
    """获取社区风险地图数据（兼容）"""
    return api_v1_community_risk_map()


def _api_disease_weather_stats():
    """疾病与天气相关性统计"""
    # 这里应该实现复杂的统计分析
    return jsonify({'success': True, 'data': {}})


def api_v1_disease_weather_stats():
    """疾病与天气相关性统计（v1）"""
    return _api_disease_weather_stats()


def api_disease_weather_stats():
    """疾病与天气相关性统计（兼容）"""
    return api_v1_disease_weather_stats()


# ======================== ML预测API ========================

def _api_ml_predict():
    """使用机器学习模型进行疾病风险预测（多分类版本）"""
    try:
        from services.ml_prediction_service import get_ml_service
        ml_service = get_ml_service()

        data = request.get_json() or {}

        # 获取用户信息
        user_info = {
            'age': data.get('age') or current_user.age or 40,
            'gender': data.get('gender') or current_user.gender or '男'
        }

        # 先校验旧单位输入，生产计算不采信客户端天气值。
        _normalize_sunshine_seconds(data)
        trusted_weather = _server_weather_for_risk(data)
        invalid_weather_response = _validate_qweather_for_risk(
            trusted_weather,
            'ml_predict',
            require_air_quality=True,
        )
        if invalid_weather_response:
            return invalid_weather_response
        sunshine_seconds = _normalize_sunshine_seconds(trusted_weather)
        # 天气特征全部来自通过门禁的服务端实况。
        weather_info = {
            # 温度相关
            'temperature': trusted_weather.get('temperature'),
            'tmean': trusted_weather.get('temperature'),
            'tmin': trusted_weather.get('temperature_min'),
            'tmax': trusted_weather.get('temperature_max'),
            'feels_like': trusted_weather.get('feels_like'),
            # 湿度
            'humidity': trusted_weather.get('humidity'),
            # 风速
            'wind_speed': trusted_weather.get('wind_speed'),
            # 降水量
            'precipitation': trusted_weather.get('precipitation', 0),
            # 训练特征沿用 sunshine_hours 字段名，但单位统一为秒
            'sunshine_hours': sunshine_seconds,
            'sunshine_duration_seconds': sunshine_seconds,
            # 空气质量
            'aqi': trusted_weather.get('aqi'),
            # 时间
            'month': data.get('month', now_local().month)
        }

        # 执行预测
        result = ml_service.predict_disease_risk(user_info, weather_info)

        if not current_app.config.get('FEATURE_EXPLAIN_OUTPUT'):
            if isinstance(result, dict):
                result.pop('explain', None)
                result.pop('rule_version', None)
                result.pop('triggered_rules', None)

        return jsonify(result)

    except INPUT_EXCEPTIONS as exc:
        # 输入参数错误或数据格式问题
        return handle_api_exception(exc, "ML疾病风险预测参数错误", log=logger, status_code=400)
    except SERVICE_EXCEPTIONS as exc:
        # 运行或依赖异常
        return handle_api_exception(exc, "ML疾病风险预测失败", log=logger)


@login_required
def api_v1_ml_predict():
    """使用机器学习模型进行疾病风险预测（v1）"""
    return _api_ml_predict()


def api_ml_predict():
    """使用机器学习模型进行疾病风险预测（兼容）"""
    return api_v1_ml_predict()


def _api_ml_predict_community():
    """使用机器学习模型进行社区风险预测（多分类版本）"""
    try:
        from services.ml_prediction_service import get_ml_service
        ml_service = get_ml_service()

        data = request.get_json() or {}

        # 获取社区信息
        community_id = data.get('community_id')
        if community_id:
            community = db.session.get(Community, community_id)
            if community:
                community_info = {
                    'name': community.name,
                    'elderly_ratio': community.elderly_ratio,
                    'chronic_disease_ratio': community.chronic_disease_ratio,
                    'population': community.population
                }
            else:
                return jsonify({'success': False, 'error': '社区不存在'})
        else:
            community_info = {
                'name': data.get('name', '未知社区'),
                'elderly_ratio': data.get('elderly_ratio', 0.2),
                'chronic_disease_ratio': data.get('chronic_disease_ratio', 0.1),
                'population': data.get('population', 100)
            }

        _normalize_sunshine_seconds(data)
        trusted_weather = _server_weather_for_risk(data)
        invalid_weather_response = _validate_qweather_for_risk(
            trusted_weather,
            'ml_predict_community',
            require_air_quality=True,
        )
        if invalid_weather_response:
            return invalid_weather_response
        sunshine_seconds = _normalize_sunshine_seconds(trusted_weather)
        # 生产社区预测不使用客户端伪造的天气特征。
        weather_info = {
            # 温度相关
            'temperature': trusted_weather.get('temperature'),
            'tmean': trusted_weather.get('temperature'),
            'tmin': trusted_weather.get('temperature_min'),
            'tmax': trusted_weather.get('temperature_max'),
            'feels_like': trusted_weather.get('feels_like'),
            # 湿度
            'humidity': trusted_weather.get('humidity'),
            # 风速
            'wind_speed': trusted_weather.get('wind_speed'),
            # 降水量
            'precipitation': trusted_weather.get('precipitation', 0),
            # 日照时长（秒）
            'sunshine_hours': sunshine_seconds,
            'sunshine_duration_seconds': sunshine_seconds,
            # 空气质量
            'aqi': trusted_weather.get('aqi'),
            # 时间
            'month': data.get('month', now_local().month)
        }

        # 执行预测
        result = ml_service.predict_community_risk(community_info, weather_info)

        return jsonify(result)

    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "ML社区风险预测失败", log=logger)


@login_required
def api_v1_ml_predict_community():
    """使用机器学习模型进行社区风险预测（v1）"""
    return _api_ml_predict_community()


def api_ml_predict_community():
    """使用机器学习模型进行社区风险预测（兼容）"""
    return api_v1_ml_predict_community()


def _api_ml_status():
    """获取ML模型状态"""
    try:
        from services.ml_prediction_service import get_ml_service
        ml_service = get_ml_service()
        raw_status = ml_service.get_model_status()
        raw_status = raw_status if isinstance(raw_status, dict) else {}
        public_fields = (
            'model_loaded',
            'loaded',
            'model_name',
            'model_type',
            'accuracy',
            'f1_score',
            'classes',
            'description',
            'sklearn_compatible',
        )
        status = {
            field: raw_status.get(field)
            for field in public_fields
            if field in raw_status
        }
        status['availability'] = (
            'available'
            if bool(raw_status.get('model_loaded', raw_status.get('loaded')))
            else 'unavailable'
        )
        return jsonify({'success': True, 'status': status})
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "ML模型状态获取失败", log=logger)


def api_v1_ml_status():
    """获取ML模型状态（v1）"""
    return _api_ml_status()


def api_ml_status():
    """获取ML模型状态（兼容）"""
    return api_v1_ml_status()


# ======================== DLNM风险预测API ========================

def _api_dlnm_risk():
    """DLNM风险函数计算"""
    try:
        from services.dlnm_risk_service import get_dlnm_service

        data = request.get_json() or {}

        # 安全的参数获取和类型转换
        try:
            temperature = float(data.get('temperature', 20))
        except (TypeError, ValueError):
            temperature = 20.0
        if not math.isfinite(temperature):
            return jsonify({
                'success': False,
                'error': 'invalid_temperature',
                'message': 'temperature 必须是有限数字'
            }), 400

        disease_type = data.get('disease_type')
        if disease_type and disease_type not in ['respiratory', 'cardiovascular', 'digestive', 'general']:
            disease_type = None

        try:
            age = int(data.get('age')) if data.get('age') is not None else None
        except (TypeError, ValueError):
            age = None

        lag_temps = data.get('lag_temperatures')
        if lag_temps:
            try:
                lag_temps = [float(t) for t in lag_temps]
            except (TypeError, ValueError):
                lag_temps = None
            if lag_temps is not None and not all(math.isfinite(value) for value in lag_temps):
                return jsonify({
                    'success': False,
                    'error': 'invalid_lag_temperatures',
                    'message': 'lag_temperatures 必须是有限数字列表'
                }), 400

        dlnm = get_dlnm_service()
        rr, breakdown = dlnm.calculate_rr(
            temperature,
            lag_temperatures=lag_temps,
            disease_type=disease_type,
            age=age
        )

        # 识别极端天气
        extreme_events = dlnm.identify_extreme_weather_events(temperature)

        return jsonify({
            'success': True,
            'rr': round(rr, 4) if rr else 1.0,
            'breakdown': breakdown,
            'extreme_events': extreme_events,
            'thresholds': dlnm.get_risk_thresholds()
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "DLNM风险计算失败", log=logger)


@login_required
def api_v1_dlnm_risk():
    """DLNM风险函数计算（v1）"""
    return _api_dlnm_risk()


def api_dlnm_risk():
    """DLNM风险函数计算（兼容）"""
    return api_v1_dlnm_risk()


def _api_dlnm_summary():
    """获取DLNM模型摘要"""
    try:
        from services.dlnm_risk_service import get_dlnm_service
        dlnm = get_dlnm_service()
        summary = dlnm.get_model_summary()
        summary = dict(summary) if isinstance(summary, dict) else {}
        summary.pop('profile_path', None)
        return jsonify({
            'success': True,
            'summary': summary
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "DLNM模型摘要获取失败", log=logger)


def api_v1_dlnm_summary():
    """获取DLNM模型摘要（v1）"""
    return _api_dlnm_summary()


def api_dlnm_summary():
    """获取DLNM模型摘要（兼容）"""
    return api_v1_dlnm_summary()


# ======================== 7天预测API ========================

def _api_forecast_7day():
    """获取未来7天健康预测"""
    try:
        from services.forecast_service import get_forecast_service

        forecast_service = get_forecast_service()

        data = request.get_json() or {}
        forecast_context = {}
        forecast_start_date = None

        # 获取天气预报温度
        if 'forecast_temps' in data and data['forecast_temps']:
            forecast_temps = data['forecast_temps']
            # 验证并转换温度数据
            try:
                if not isinstance(forecast_temps, list):
                    raise TypeError('forecast_temps must be a list')
                forecast_temps = [float(t) for t in forecast_temps]
                if not all(math.isfinite(t) for t in forecast_temps):
                    raise ValueError('forecast_temps must contain finite numbers')
            except (TypeError, ValueError):
                return jsonify({
                    'success': False,
                    'error': 'invalid_forecast_temps',
                    'message': 'forecast_temps 必须是 7 个数字'
                }), 400
            if len(forecast_temps) != 7:
                return jsonify({
                    'success': False,
                    'error': 'invalid_forecast_temps_length',
                    'message': 'forecast_temps 必须提供完整 7 天'
                }), 400
        else:
            # 页面默认链路只使用和风 7 日预报，避免融合或 mock 数据影响风险判断。
            city = sanitize_input(data.get('city'), max_length=100)
            if city:
                city = normalize_location_name(city)
            else:
                city = ensure_user_location_valid()
            try:
                weather_forecast, _, forecast_meta = get_qweather_forecast_with_cache(city, days=7)
                # 当前空气质量作为复合暴露的背景场（小时级无稳定AQI预报时）
                current_weather, _ = get_weather_with_cache(city)
                invalid_weather_response = _validate_qweather_for_risk(
                    current_weather,
                    'forecast_7day',
                    require_air_quality=True,
                )
                if invalid_weather_response:
                    return invalid_weather_response
                forecast_context = {
                    'aqi': current_weather.get('aqi'),
                    'pm25': current_weather.get('pm25')
                }
                forecast_temps = [f for f in weather_forecast if isinstance(f, dict)]
                forecast_start_date = today_local()
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                logger.warning("Forecast cache unavailable: %s", exc)
                return jsonify({
                    'success': False,
                    'error': 'forecast_unavailable',
                    'message': '天气预报暂不可用，请稍后重试'
                }), 503
            if len(forecast_temps) != 7:
                logger.warning(
                    "QWeather forecast data incomplete for city=%s, count=%s, meta=%s",
                    city,
                    len(forecast_temps),
                    forecast_meta,
                )
                return jsonify({
                    'success': False,
                    'error': 'forecast_data_incomplete',
                    'message': '和风天气预报数据不完整，暂无法生成7天预测'
                }), 503

        # 生成7天预测
        forecasts, summary = forecast_service.generate_7day_forecast(
            forecast_temps,
            start_date=forecast_start_date,
            context=forecast_context
        )

        return jsonify({
            'success': True,
            'forecasts': forecasts,
            'summary': summary,
            'data_source': 'QWeather' if forecast_start_date else 'provided_forecast_temps'
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "7天预测失败", log=logger)


@login_required
def api_v1_forecast_7day():
    """获取未来7天健康预测（v1）"""
    return _api_forecast_7day()


def api_forecast_7day():
    """获取未来7天健康预测（兼容）"""
    return api_v1_forecast_7day()


def _api_forecast_daily():
    """获取单日门诊预测"""
    try:
        from services.forecast_service import get_forecast_service

        data = request.get_json() or {}

        temperature = data.get('temperature', 20)
        try:
            parsed_temperature = float(temperature)
        except (TypeError, ValueError):
            pass
        else:
            if not math.isfinite(parsed_temperature):
                return jsonify({
                    'success': False,
                    'error': 'invalid_temperature',
                    'message': 'temperature 必须是有限数字'
                }), 400
            temperature = parsed_temperature

        lag_temps = data.get('lag_temperatures')
        if isinstance(lag_temps, list):
            has_nonfinite_lag = False
            for value in lag_temps:
                if value is None:
                    continue
                try:
                    has_nonfinite_lag = not math.isfinite(float(value))
                except (TypeError, ValueError):
                    continue
                if has_nonfinite_lag:
                    break
            if has_nonfinite_lag:
                return jsonify({
                    'success': False,
                    'error': 'invalid_lag_temperatures',
                    'message': 'lag_temperatures 必须是有限数字列表'
                }), 400

        month = data.get('month', now_local().month)
        dow = data.get('day_of_week', now_local().weekday())

        forecast_service = get_forecast_service()
        result = forecast_service.predict_daily_visits(
            temperature,
            lag_temps=lag_temps,
            month=month,
            dow=dow
        )

        return jsonify({
            'success': True,
            'prediction': result
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "单日门诊预测失败", log=logger)


def api_v1_forecast_daily():
    """获取单日门诊预测（v1）"""
    return _api_forecast_daily()


def api_forecast_daily():
    """获取单日门诊预测（兼容）"""
    return api_v1_forecast_daily()


# ======================== 社区风险地图API ========================

def _api_community_risk_map_v2():
    """获取社区风险地图数据（改进版）"""
    try:
        from services.community_risk_service import get_community_service
        from services.community_risk_cache import (
            build_community_risk_cache_params,
            get_or_build_community_risk_result,
        )

        community_service = get_community_service()

        data = request.get_json() or {}
        target_date = parse_date(data.get('analysis_date')) or parse_date(data.get('target_date'))
        window_days = parse_int(data.get('window_days'), 30)
        disease_filter = sanitize_input(data.get('disease'), max_length=100)
        if disease_filter in ('', 'all', '全部'):
            disease_filter = ''

        city = sanitize_input(data.get('city'), max_length=100)
        city = normalize_location_name(city) if city else ''

        # 客户端天气仅供测试注入。生产风险结论必须读取服务端规范缓存，
        # 避免客户端仅声明 data_source=QWeather 就伪造数据来源。
        allow_test_weather = bool(
            current_app.config.get('TESTING')
            and isinstance(data.get('weather'), dict)
        )
        if allow_test_weather:
            weather_data = dict(data['weather'])
            if 'temperature' not in weather_data:
                weather_data['temperature'] = 20
        else:
            canonical_location = (
                current_app.config.get('QWEATHER_CANONICAL_LOCATION')
                or current_app.config.get('DEFAULT_LOCATION')
                or current_app.config.get('DEFAULT_CITY')
                or DEFAULT_CITY_LABEL
            )
            canonical_location = normalize_location_name(canonical_location)
            # 保留既有 city 缓存维度；天气来源固定走服务端规范地点。
            if not city:
                default_city = (
                    current_app.config.get('DEFAULT_CITY')
                    or DEFAULT_CITY_LABEL
                )
                city = normalize_location_name(default_city)
            try:
                weather_data, _ = get_weather_with_cache(canonical_location)
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                logger.warning("Community risk map canonical weather unavailable: %s", exc)
                weather_data = {}

        invalid_weather_response = _validate_qweather_for_risk(weather_data, 'community_risk_map_v2')
        screening_only = invalid_weather_response is not None
        signature_builder = getattr(community_service, 'get_ranking_input_signature', None)
        ranking_input_signature = signature_builder() if callable(signature_builder) else ''

        cache_params = build_community_risk_cache_params(
            analysis_date=target_date,
            window_days=window_days,
            disease_filter=disease_filter,
            city=city,
            weather_data=None if screening_only else weather_data,
            ranking_path='exploratory_only' if screening_only else 'auto',
            input_signature=ranking_input_signature,
        )

        def _build_result():
            if screening_only:
                screening_builder = getattr(
                    community_service,
                    'generate_exploratory_geospatial_screening',
                    None,
                )
                if not callable(screening_builder):
                    return None
                return screening_builder(
                    target_date=target_date,
                    window_days=window_days,
                    disease_filter=disease_filter,
                )
            return community_service.generate_community_risk_map(
                weather_data,
                target_date=target_date,
                window_days=window_days,
                disease_filter=disease_filter
            )

        result, cache_hit = get_or_build_community_risk_result(cache_params, _build_result)
        if result is None:
            if invalid_weather_response is not None:
                return invalid_weather_response
            return jsonify({
                'success': False,
                'error': 'community_risk_unavailable',
                'message': '社区分析暂时不可用，请稍后再试。',
            }), 503

        return jsonify({
            'success': True,
            'cache_hit': cache_hit,
            'ranking_mode': result.get('ranking_mode'),
            'ranking_status': result.get('ranking_status'),
            'ranking_metadata': result.get('ranking_metadata', {}),
            'map_data': result.get('map_data', {}),
            'rankings': result.get('rankings', []),
            'summary': result.get('summary', {}),
            'macro_weather': result.get('macro_weather', {}),
            'layers': result.get('layers', {}),
            'impact_likelihood_matrix': result.get('impact_likelihood_matrix', {}),
            'equity_stratification': result.get('equity_stratification', {}),
            'methodology': result.get('methodology', []),
            'management_suggestions': result.get('management_suggestions', [])
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "社区风险地图生成失败", log=logger)


@login_required
def api_v1_community_risk_map_v2():
    """获取社区风险地图数据（改进版v1）"""
    return _api_community_risk_map_v2()


def api_community_risk_map_v2():
    """获取社区风险地图数据（改进版兼容）"""
    return api_v1_community_risk_map_v2()


def _api_community_vulnerability(community_name):
    """获取单个社区脆弱性指数。

    匿名/游客：403 + 脱敏摘要（仅 name + vulnerability_level），禁止完整 profile。
    正式用户：返回完整画像。
    """
    try:
        from services.community_risk_service import get_community_service

        community_service = get_community_service()
        profile = community_service.get_community_profile(community_name)

        if profile:
            if _is_anonymous_or_guest():
                # 禁止完整 profile；403 仅 name+level 摘要
                summary = _community_vulnerability_summary(
                    profile, community_name
                )
                return jsonify({
                    'success': False,
                    'error': 'login_required_for_community_profile',
                    'message': '登录后可查看完整社区画像',
                    'community': summary,
                }), 403
            return jsonify({
                'success': True,
                'community': profile
            })
        else:
            return jsonify({'success': False, 'error': '社区未找到'})
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "社区脆弱性指数获取失败", log=logger)


def api_v1_community_vulnerability(community_name):
    """获取单个社区脆弱性指数（v1）"""
    return _api_community_vulnerability(community_name)


def api_community_vulnerability(community_name):
    """获取单个社区脆弱性指数（兼容）"""
    return api_v1_community_vulnerability(community_name)


def _api_community_list():
    """获取所有社区列表及脆弱性。

    匿名/游客：每条仅保留 name（及可选 vulnerability_level 粗粒度）；
    去掉 population、elderly_ratio、chronic_disease_ratio、vulnerability_index 精确值。
    正式登录用户：完整画像字段。
    """
    try:
        from services.community_risk_service import get_community_service

        community_service = get_community_service()
        communities = community_service.get_all_communities()

        if _is_anonymous_or_guest():
            communities = [
                _redact_community_list_item(c) for c in communities
            ]

        return jsonify({
            'success': True,
            'communities': communities
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "社区列表获取失败", log=logger)


def api_v1_community_list():
    """获取所有社区列表及脆弱性（v1）"""
    return _api_community_list()


def api_community_list():
    """获取所有社区列表及脆弱性（兼容）"""
    return api_v1_community_list()


# ======================== 慢病风险预测API ========================

def _api_chronic_individual():
    """个体慢病风险预测"""
    try:
        from services.chronic_risk_service import get_chronic_service

        chronic_service = get_chronic_service()

        data = request.get_json() or {}

        # 用户信息
        user_info = {
            'age': data.get('age') or current_user.age or 50,
            'gender': data.get('gender') or current_user.gender or '未知',
            'chronic_diseases': data.get('chronic_diseases') or (
                safe_json_loads(current_user.chronic_diseases, [])
            )
        }

        # 生产慢病风险只读服务端 canonical 天气。
        weather_data = _server_weather_for_risk(data)

        invalid_weather_response = _validate_qweather_for_risk(
            weather_data,
            'chronic_individual',
            require_air_quality=True,
        )
        if invalid_weather_response:
            return invalid_weather_response

        # 预测
        result = chronic_service.predict_individual_risk(user_info, weather_data)

        if not current_app.config.get('FEATURE_EXPLAIN_OUTPUT'):
            if isinstance(result, dict):
                result.pop('explain', None)
                result.pop('rule_version', None)
                result.pop('triggered_rules', None)

        return jsonify({
            'success': True,
            'result': result
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "个体慢病风险预测失败", log=logger)


@login_required
def api_v1_chronic_individual():
    """个体慢病风险预测（v1）"""
    return _api_chronic_individual()


def api_chronic_individual():
    """个体慢病风险预测（兼容）"""
    return api_v1_chronic_individual()


def _api_chronic_population():
    """人群分层慢病风险预测"""
    try:
        from services.chronic_risk_service import get_chronic_service

        chronic_service = get_chronic_service()

        data = request.get_json() or {}

        # 生产人群风险只读服务端 canonical 天气。
        weather_data = _server_weather_for_risk(data)

        invalid_weather_response = _validate_qweather_for_risk(
            weather_data,
            'chronic_population',
            require_air_quality=True,
        )
        if invalid_weather_response:
            return invalid_weather_response

        # 预测
        result = chronic_service.predict_population_risk({}, weather_data)

        return jsonify({
            'success': True,
            'result': result
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "人群慢病风险预测失败", log=logger)


def api_v1_chronic_population():
    """人群分层慢病风险预测（v1）"""
    return _api_chronic_population()


def api_chronic_population():
    """人群分层慢病风险预测（兼容）"""
    return api_v1_chronic_population()


# ======================== AI问答API ========================

def _api_ai_ask():
    """AI问答接口"""
    try:
        from services.ai_question_service import AIQuestionService
        data = request.get_json() or {}

        api_key = (current_app.config.get('SILICONFLOW_API_KEY') or '').strip()
        allowed_models = current_app.config.get('AI_ALLOWED_MODELS', []) or []
        if not api_key or not allowed_models:
            return jsonify({
                'success': False,
                'error': 'ai_service_unavailable',
                'message': 'AI 服务当前未配置，请稍后再试。',
            }), 503

        question = data.get('question', '')
        # 降低 AI 问答最大长度，防止滥用和费用激增
        # 可通过环境变量 AI_QUESTION_MAX_LENGTH 覆盖（默认 800）
        max_question_len = current_app.config.get('AI_QUESTION_MAX_LENGTH', 800)
        question = sanitize_input(question, max_length=max_question_len)
        model = data.get('model')

        if model not in allowed_models:
            return jsonify({'success': False, 'error': '模型不可用'})
        if not question:
            return jsonify({'success': False, 'error': '问题不能为空'})

        api_base = current_app.config.get('SILICONFLOW_API_BASE')
        service = AIQuestionService(
            api_key,
            api_base,
            allowed_models,
            connect_timeout=current_app.config.get('AI_CONNECT_TIMEOUT', 8),
            read_timeout=current_app.config.get('AI_READ_TIMEOUT', 60),
            retries=current_app.config.get('AI_REQUEST_RETRIES', 1),
            max_tokens=current_app.config.get('AI_MAX_TOKENS', 800)
        )
        answer = service.ask(question, model)
        triage = None
        if current_app.config.get('FEATURE_EMERGENCY_TRIAGE'):
            from services.emergency_triage import triage_symptoms
            triage = triage_symptoms(question)
            if triage.get('is_emergency'):
                create_notification(
                    current_user.id,
                    title='AI问答紧急提醒',
                    message='AI问答中出现紧急症状关键词，请优先就医或联系家属。',
                    level='danger',
                    category='triage',
                    meta={'matched_keywords': triage.get('matched_keywords', [])}
                )

        payload = {'success': True, 'answer': answer}
        if triage is not None:
            payload['triage'] = triage
        return jsonify(payload)
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "AI问答失败", log=logger)


@login_required
# 降低 AI 接口限流至按小时计（默认 30/小时），防止费用激增
# 可通过环境变量 RATE_LIMIT_AI 覆盖
def api_v1_ai_ask():
    """AI问答接口（v1）"""
    return _api_ai_ask()


def api_ai_ask():
    """AI问答接口（兼容）"""
    return api_v1_ai_ask()


def _api_chronic_rules_version():
    """获取慢病规则库版本"""
    try:
        from services.chronic_risk_service import get_chronic_service
        chronic_service = get_chronic_service()
        return jsonify({
            'success': True,
            'version': chronic_service.get_rules_version()
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "慢病规则版本获取失败", log=logger)


def api_v1_chronic_rules_version():
    """获取慢病规则库版本（v1）"""
    return _api_chronic_rules_version()


def api_chronic_rules_version():
    """获取慢病规则库版本（兼容）"""
    return api_v1_chronic_rules_version()


# ======================== 综合预警API ========================

def _api_comprehensive_alert():
    """获取综合健康预警"""
    try:
        from services.dlnm_risk_service import get_dlnm_service
        from services.forecast_service import get_forecast_service
        from services.community_risk_service import get_community_service

        dlnm = get_dlnm_service()
        forecast_service = get_forecast_service()
        community_service = get_community_service()

        data = request.get_json() or {}
        city = sanitize_input(data.get('city'), max_length=100)
        if city:
            city = normalize_location_name(city)
        elif current_user.is_authenticated:
            city = ensure_user_location_valid()
        else:
            city = current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL

        # 获取当前天气
        current_weather, _ = get_weather_with_cache(city)
        invalid_weather_response = _validate_qweather_for_risk(
            current_weather,
            'comprehensive_alert',
            require_air_quality=True,
        )
        if invalid_weather_response:
            return invalid_weather_response
        temperature = current_weather.get('temperature', 20)

        # 计算当前风险
        rr, _ = dlnm.calculate_rr(temperature)
        extreme_events = dlnm.identify_extreme_weather_events(temperature)

        # 获取7天预报：综合预警只使用和风天气，避免 mock 或融合缓存抬高风险。
        weather_forecast, _, forecast_meta = get_qweather_forecast_with_cache(city, days=7)
        forecast_temps = [f for f in weather_forecast if isinstance(f, dict)]
        if len(forecast_temps) != 7:
            logger.warning(
                "综合预警和风7日预报不可用: city=%s count=%s meta=%s",
                city,
                len(forecast_temps),
                forecast_meta,
            )
            return jsonify({
                'success': False,
                'error': 'forecast_data_incomplete',
                'message': '和风天气预报数据不完整，暂无法生成综合预警'
            }), 503
        forecasts, summary = forecast_service.generate_7day_forecast(
            forecast_temps,
            start_date=today_local(),
            context={
                'aqi': current_weather.get('aqi'),
                'pm25': current_weather.get('pm25'),
            },
        )

        # 社区风险
        community_result = community_service.generate_community_risk_map(current_weather)

        # 综合预警级别（蓝/黄/橙/红）
        if rr >= 1.4 or summary['high_risk_days'] >= 3:
            alert_level = 'red'
            alert_text = '红色预警'
        elif rr >= 1.25 or summary['high_risk_days'] >= 2:
            alert_level = 'orange'
            alert_text = '橙色预警'
        elif rr >= 1.1 or summary['high_risk_days'] >= 1:
            alert_level = 'yellow'
            alert_text = '黄色预警'
        else:
            alert_level = 'blue'
            alert_text = '蓝色预警'

        return jsonify({
            'success': True,
            'alert': {
                'level': alert_level,
                'text': alert_text,
                'rr': round(rr, 3),
                'extreme_events': extreme_events
            },
            'current_weather': current_weather,
            'forecast_summary': summary,
            'community_summary': community_result['summary'],
            'top_risk_communities': community_result['rankings'][:3],
            'recommendations': summary['recommendations']
        })
    except API_EXCEPTIONS as exc:
        return handle_api_exception(exc, "综合预警生成失败", log=logger)


def api_v1_comprehensive_alert():
    """获取综合健康预警（v1）"""
    return _api_comprehensive_alert()


def api_comprehensive_alert():
    """获取综合健康预警（兼容）"""
    return api_v1_comprehensive_alert()


def _api_usage_event():
    """Write pilot usage event (server-side validation, CSRF-protected)."""
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'success': False, 'error': 'invalid_payload'}), 400
        event_type = sanitize_input(payload.get('event_type'), max_length=50) or ''
        if event_type not in CLIENT_EVENT_TYPES:
            return jsonify({'success': False, 'error': 'invalid event_type'}), 400

        pair_id = payload.get('pair_id')
        member_id = payload.get('member_id')
        meta_value = payload.get('meta')
        if meta_value is not None and not isinstance(meta_value, (dict, list)):
            return jsonify({'success': False, 'error': 'invalid_meta'}), 400
        meta = meta_value
        if meta is not None:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'invalid_meta'}), 400
            if len(meta_json) > EVENT_META_MAX_CHARS:
                return jsonify({'success': False, 'error': 'meta_too_large'}), 400

        resolved_pair_id = None
        pair = None
        if pair_id is not None:
            try:
                pair_id_int = _parse_positive_event_id(
                    pair_id,
                    'invalid_pair_id',
                )
            except ValueError:
                return jsonify({'success': False, 'error': 'invalid_pair_id'}), 400
            q = Pair.query.filter_by(id=pair_id_int, status='active')
            if getattr(current_user, 'role', None) != 'admin':
                q = q.filter_by(caregiver_id=current_user.id)
            pair = q.first()
            if not pair:
                return jsonify({'success': False, 'error': 'pair_not_found'}), 404
            resolved_pair_id = pair.id

        resolved_member_id = None
        member = None
        if member_id is not None:
            try:
                member_id_int = _parse_positive_event_id(
                    member_id,
                    'invalid_member_id',
                )
            except ValueError:
                return jsonify({'success': False, 'error': 'invalid_member_id'}), 400
            member = FamilyMember.query.filter_by(
                id=member_id_int,
                user_id=current_user.id,
            ).first()
            if not member:
                return jsonify({'success': False, 'error': 'member_not_found'}), 404
            resolved_member_id = member.id

        if pair and member and pair.member_id != member.id:
            return jsonify({'success': False, 'error': 'member_pair_mismatch'}), 400

        event = log_usage_event(
            event_type,
            user_id=current_user.id,
            pair_id=resolved_pair_id,
            member_id=resolved_member_id,
            source='web',
            meta=meta,
        )
        if event is None:
            return jsonify({'success': False, 'error': 'event_write_failed'}), 503
        return jsonify({'success': True})
    except INPUT_EXCEPTIONS as exc:
        return handle_api_exception(exc, "usage event 参数错误", log=logger, status_code=400)
    except SERVICE_EXCEPTIONS as exc:
        return handle_api_exception(exc, "usage event 写入失败", log=logger)
