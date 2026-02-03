# -*- coding: utf-8 -*-
"""Request/response hooks and template helpers."""
import json
import logging
import secrets
import time
from datetime import datetime

from flask import g, request, session
from flask_login import current_user

from core.security import csrf_failure_response, generate_csrf_token, validate_csrf
from core.weather import (
    get_location_options,
    get_user_location_value,
    normalize_location_name
)

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 10 * 1024
MAX_JSON_DEPTH = 5


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
                'path': request.path,
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

    @app.context_processor
    def inject_now():
        """注入当前时间到模板"""
        current_location = normalize_location_name(get_user_location_value())
        payload = {
            'now': datetime.now,
            'csrf_token': generate_csrf_token,
            'current_location': current_location,
            'location_options': get_location_options(),
            'ai_models': app.config.get('AI_ALLOWED_MODELS', []),
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
