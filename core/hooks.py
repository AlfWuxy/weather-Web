# -*- coding: utf-8 -*-
"""Request/response hooks and template helpers."""
import os
import json
import logging
import secrets
import time
from datetime import datetime

from flask import g, request, session, url_for as flask_url_for
from flask_login import current_user

from core.metric_explanations import (
    get_metric_explanation_groups,
    get_metric_explanations,
)
from core.security import csrf_failure_response, generate_csrf_token, validate_csrf
from core.weather import (
    get_location_options,
    get_user_location_value,
    normalize_location_name
)

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 10 * 1024
MAX_JSON_DEPTH = 5


def _redact_sensitive_path(path):
    """结构化日志不得记录行动链接或点击追踪 token。"""
    path = str(path or '')
    for prefix in ('/e/', '/t/'):
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix):]
        suffix = ''
        if '/' in remainder:
            _, tail = remainder.split('/', 1)
            suffix = f'/{tail}'
        return f'{prefix}<token>{suffix}'
    return path


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
        g.request_id = request.headers.get('X-Request-Id') or secrets.token_hex(8)
        g.request_start = time.perf_counter()
        g.external_api_timings = []

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
        """结构化请求日志"""
        if not app.config.get('FEATURE_STRUCTURED_LOGS'):
            return response
        try:
            duration_ms = None
            if hasattr(g, 'request_start'):
                duration_ms = round((time.perf_counter() - g.request_start) * 1000, 2)
            user_id = None
            user_role = None
            if current_user.is_authenticated:
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
                'notifications': app.config.get('FEATURE_NOTIFICATIONS')
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
