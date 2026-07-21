# -*- coding: utf-8 -*-
"""Request/response hooks and template helpers."""
import os
import json
import logging
import secrets
import time
from datetime import datetime

from flask import (
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for as flask_url_for,
)
from flask_login import current_user

from core.metric_explanations import (
    get_metric_explanation_groups,
    get_metric_explanations,
)
from core.logging_privacy import formal_request_log_event, sanitize_request_path
from core.security import csrf_failure_response, generate_csrf_token, validate_csrf
from core.weather import (
    get_location_options,
    get_user_location_value,
    normalize_location_name
)

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 10 * 1024
MAX_JSON_DEPTH = 5

# 正式微信运行态仅保留公开、聚合或研究管理端点。下列白名单需要人工审查后扩展，
# 其余同蓝图新端点会默认关闭，避免新增私密 Web 功能时漏配门禁。
FORMAL_WEB_ALLOWED_USER_ENDPOINTS = frozenset({
    'user.community_dashboard',
    'user.community_detail',
    'user.community_wechat',
    'user.community_announce',
    'user.community_risk',
    'user.heat_exposure_gis',
})

FORMAL_WEB_ALLOWED_ANALYSIS_ENDPOINTS = frozenset({
    'analysis.analysis_history',
    'analysis.analysis_heatmap',
    'analysis.analysis_lag',
    'analysis.analysis_community_compare',
    'analysis.alerts_history',
    'analysis.alerts_accuracy',
    'analysis.reports_center',
    'analysis.reports_export',
    'analysis.pilot_dashboard',
    'analysis.pilot_review_delivery',
    'analysis.pilot_export_csv',
    'analysis.model_quality',
})

FORMAL_WEB_ALLOWED_API_ENDPOINTS = frozenset({
    'api.api_v1_current_weather',
    'api.api_current_weather',
    'api.api_v1_weather_nowcast',
    'api.api_weather_nowcast',
    'api.api_v1_community_risk_map',
    'api.api_community_risk_map',
    'api.api_v1_disease_weather_stats',
    'api.api_disease_weather_stats',
    'api.api_v1_ml_predict_community',
    'api.api_ml_predict_community',
    'api.api_v1_ml_status',
    'api.api_ml_status',
    'api.api_v1_dlnm_summary',
    'api.api_dlnm_summary',
    'api.api_v1_community_risk_map_v2',
    'api.api_community_risk_map_v2',
    'api.api_v1_community_vulnerability',
    'api.api_community_vulnerability',
    'api.api_v1_community_list',
    'api.api_community_list',
    'api.api_v1_chronic_population',
    'api.api_chronic_population',
    'api.api_v1_chronic_rules_version',
    'api.api_chronic_rules_version',
})


def _formal_web_gate_kind(endpoint):
    """返回正式微信态端点的门禁类型，None 表示可继续处理。"""
    endpoint = str(endpoint or '')
    blueprint = endpoint.partition('.')[0]
    if endpoint == 'public.register':
        return 'html'
    if blueprint in {'health', 'tools'}:
        return 'html'
    if blueprint == 'user':
        return None if endpoint in FORMAL_WEB_ALLOWED_USER_ENDPOINTS else 'html'
    if blueprint == 'analysis':
        return None if endpoint in FORMAL_WEB_ALLOWED_ANALYSIS_ENDPOINTS else 'html'
    if blueprint == 'api':
        return None if endpoint in FORMAL_WEB_ALLOWED_API_ENDPOINTS else 'json'
    return None


def _redact_sensitive_path(path):
    """结构化日志不得记录行动链接或点击追踪 token。"""
    return sanitize_request_path(path)


def _exceeds_json_depth(value, max_depth, current_depth=1):
    if current_depth > max_depth:
        return True
    if isinstance(value, dict):
        for item in value.values():
            if _exceeds_json_depth(item, max_depth, current_depth + 1):
                return True
    elif isinstance(value, list):
        for item in value:
            if _exceeds_json_depth(item, max_depth, current_depth + 1):
                return True
    return False


def _valid_key_length(value):
    return isinstance(value, str) and 20 <= len(value) <= 100


def register_hooks(app):
    """Register app hooks, filters, and context processors."""
    @app.before_request
    def init_request_context():
        """初始化请求上下文（结构化日志使用）"""
        # 请求编号属于服务端审计边界，不能采纳客户端可伪造的请求头。
        g.request_id = secrets.token_hex(8)
        g.request_start = time.perf_counter()
        g.external_api_timings = []

    @app.before_request
    def formal_wechat_web_gate():
        """正式微信态在登录加载、CSRF 与业务查询前关闭 Web 私密入口。"""
        if not app.config.get('WECHAT_FORMAL_RUNTIME'):
            return None
        gate_kind = _formal_web_gate_kind(request.endpoint)
        if gate_kind is None:
            return None
        g.formal_web_gate_blocked = True
        if gate_kind == 'json':
            response = jsonify({
                'success': False,
                'error': 'wechat_formal_web_private_disabled',
                'message': '正式版本请在微信小程序中使用此私密功能。',
            })
            response.status_code = 403
        else:
            response = redirect(flask_url_for('public.action_check'), code=303)
        response.headers['Cache-Control'] = 'no-store, private, max-age=0'
        return response

    @app.before_request
    def csrf_protect():
        """统一CSRF校验"""
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            # MiniProgram API uses Bearer token auth and must not require CSRF.
            if request.path.startswith('/mp/api/'):
                return None
            if not validate_csrf():
                return csrf_failure_response()

    @app.after_request
    def log_request(response):
        """统一安全响应头，并按配置写入结构化请求日志。"""
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault(
            'Content-Security-Policy',
            "base-uri 'self'; frame-ancestors 'none'; object-src 'none'",
        )
        response.headers.setdefault(
            'Permissions-Policy',
            'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
        )
        if request.path.startswith(('/e/', '/t/')):
            # 行动与投递 token 绝不能进入外站 Referer。
            response.headers['Referrer-Policy'] = 'no-referrer'
        else:
            response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        if not app.config.get('DEBUG'):
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains',
            )
        if not app.config.get('FEATURE_STRUCTURED_LOGS'):
            return response
        try:
            duration_ms = None
            if hasattr(g, 'request_start'):
                duration_ms = round((time.perf_counter() - g.request_start) * 1000, 2)
            user_id = None
            user_role = None
            if (
                not getattr(g, 'formal_web_gate_blocked', False)
                and current_user.is_authenticated
            ):
                user_id = getattr(current_user, 'id', None)
                user_role = getattr(current_user, 'role', None)
            log_payload = {
                'request_id': getattr(g, 'request_id', None),
                'user_id': user_id,
                'user_role': user_role,
                'method': request.method,
                'path': _redact_sensitive_path(request.path),
                'endpoint': request.endpoint,
                'status': response.status_code,
                'duration_ms': duration_ms,
                'external_api': getattr(g, 'external_api_timings', [])
            }
            if app.config.get('WECHAT_FORMAL_RUNTIME'):
                logger.info(formal_request_log_event(log_payload))
            else:
                logger.info(json.dumps(log_payload, ensure_ascii=False))
            if getattr(g, 'request_id', None):
                response.headers['X-Request-Id'] = g.request_id
        except Exception as exc:
            logger.debug("结构化日志失败: %s", exc)
        return response

    @app.template_filter('from_json')
    def from_json_filter(value):
        """将JSON字符串转换为Python对象"""
        if not value:
            return []
        raw = value if isinstance(value, (str, bytes, bytearray)) else str(value)
        raw_bytes = raw.encode('utf-8') if isinstance(raw, str) else raw
        if len(raw_bytes) > MAX_JSON_BYTES:
            logger.warning("JSON payload too large: %s bytes", len(raw_bytes))
            return []
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("JSON parse failed: %s, value: %s", exc, str(value)[:100])
            return []
        if _exceeds_json_depth(parsed, MAX_JSON_DEPTH):
            logger.warning("JSON depth exceeds %s", MAX_JSON_DEPTH)
            return []
        return parsed

    def _versioned_static_url_for(endpoint, **values):
        """Append a cache-busting `v=` parameter for static assets.

        This allows us to set longer cache headers on /static without risking
        stale assets after deployments.
        """
        if endpoint == 'static':
            filename = values.get('filename')
            if filename:
                try:
                    file_path = os.path.join(app.static_folder, filename)
                    values['v'] = int(os.stat(file_path).st_mtime)
                except OSError:
                    # If file missing (e.g. optional assets), fall back to plain url_for.
                    pass
        return flask_url_for(endpoint, **values)

    @app.context_processor
    def override_url_for():
        return dict(url_for=_versioned_static_url_for)

    @app.context_processor
    def inject_now():
        """注入当前时间到模板"""
        current_location = normalize_location_name(get_user_location_value())
        payload = {
            'now': lambda: datetime.now(tz=__import__('zoneinfo').ZoneInfo('Asia/Shanghai')),
            'csrf_token': generate_csrf_token,
            'current_location': current_location,
            'location_options': get_location_options(),
            'ai_models': app.config.get('AI_ALLOWED_MODELS', []),
            'metric_explanations': get_metric_explanations(),
            'metric_explanation_groups': get_metric_explanation_groups(),
            'feature_flags': {
                'explain_output': app.config.get('FEATURE_EXPLAIN_OUTPUT'),
                'emergency_triage': app.config.get('FEATURE_EMERGENCY_TRIAGE'),
                'elder_mode': app.config.get('FEATURE_ELDER_MODE'),
                'notifications': app.config.get('FEATURE_NOTIFICATIONS'),
                'heat_exposure_gis': app.config.get('FEATURE_HEAT_EXPOSURE_GIS')
            }
        }
        map_endpoints = {'user.community_risk', 'public.cooling_resources'}
        map_paths = {'/community-risk', '/cooling'}
        needs_map_keys = request.endpoint in map_endpoints or request.path in map_paths
        if needs_map_keys:
            amap_key = app.config.get('AMAP_KEY', '')
            amap_code = app.config.get('AMAP_SECURITY_JS_CODE', '')
            if _valid_key_length(amap_key):
                payload['amap_key'] = amap_key
            elif amap_key:
                logger.warning("Invalid AMAP_KEY length; skipping template injection")
            if _valid_key_length(amap_code):
                payload['amap_security_js_code'] = amap_code
            elif amap_code:
                logger.warning("Invalid AMAP_SECURITY_JS_CODE length; skipping template injection")
        return payload

    app.jinja_env.globals['csrf_token'] = generate_csrf_token

    static_max_age = app.config.get('STATIC_CACHE_MAX_AGE_SECONDS', 0)
    try:
        static_max_age = int(static_max_age) if static_max_age is not None else 0
    except (TypeError, ValueError):
        static_max_age = 0

    if static_max_age > 0:
        def _get_send_file_max_age(filename):  # noqa: ANN001 - Flask hook signature
            # Apply only to Flask's built-in /static endpoint.
            if (request.path or '').startswith('/static/'):
                return static_max_age
            return None
        # Instance override (Flask uses this hook when serving static and send_file).
        app.get_send_file_max_age = _get_send_file_max_age
