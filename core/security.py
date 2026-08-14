# -*- coding: utf-8 -*-
"""Security helpers."""
import hashlib
import logging
import os
import secrets

from flask import abort, current_app, has_app_context, jsonify, request, session

logger = logging.getLogger(__name__)
from flask_login import current_user
from flask_limiter.util import get_remote_address

from core.constants import GUEST_ID_PREFIX


def _is_guest_rate_limit_subject(user):
    """识别显式游客标记、游客角色或游客 ID 前缀。"""
    if getattr(user, 'is_guest', False):
        return True
    if getattr(user, 'role', None) == 'guest':
        return True
    uid = str(getattr(user, 'id', '') or '')
    return uid.startswith(GUEST_ID_PREFIX)


def _client_ip_for_rate_limit():
    """按受信代理边界解析客户端 IP，解析失败时安全回退。"""
    try:
        # 延迟导入避免 core.audit 与本模块形成加载环。
        from core.audit import _get_client_ip

        client_ip = _get_client_ip()
    except Exception:
        client_ip = None
    return client_ip or get_remote_address()


def rate_limit_key():
    """正式用户按账号分桶，游客与匿名请求按受信客户端 IP 分桶。"""
    if (
        getattr(current_user, 'is_authenticated', False)
        and not _is_guest_rate_limit_subject(current_user)
    ):
        uid = str(getattr(current_user, 'id', '') or '')
        return f'user:{uid}'
    return f'ip:{_client_ip_for_rate_limit()}'


def generate_csrf_token():
    """生成/获取CSRF Token"""
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def validate_csrf(request_token=None):
    """Validate CSRF token from header/form/JSON payload."""
    token = session.get('_csrf_token')
    if not token:
        return False
    if request_token is None:
        request_token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        if not request_token and request.is_json:
            payload = request.get_json(silent=True) or {}
            request_token = payload.get('csrf_token')
    if not request_token:
        return False
    return secrets.compare_digest(request_token, token)


def csrf_failure_response():
    """CSRF失败响应"""
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'CSRF token missing or invalid'}), 400
    abort(400)


_AUTO_PEPPER: str | None = None  # 进程级自动生成的 pepper 回退


def _pair_token_pepper():
    global _AUTO_PEPPER
    if has_app_context():
        pepper = current_app.config.get('PAIR_TOKEN_PEPPER')
        if pepper:
            return pepper
    env_pepper = os.getenv('PAIR_TOKEN_PEPPER') or ''
    if env_pepper:
        return env_pepper
    # 未配置 pepper —— 自动生成进程级随机 pepper，确保同一进程内哈希一致
    if _AUTO_PEPPER is None:
        _AUTO_PEPPER = secrets.token_urlsafe(32)
        logger.warning(
            "PAIR_TOKEN_PEPPER 未配置，已自动生成进程级随机 pepper。"
            "重启后哈希将变化，生产环境必须显式配置此变量。"
        )
    return _AUTO_PEPPER


def hash_pair_token(token):
    """Hash pair token with a stable pepper (never store plain token)."""
    if not token:
        return None
    pepper = _pair_token_pepper()
    payload = f"{token}{pepper}".encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def verify_pair_token(token, token_hash):
    """Verify token against stored hash."""
    if not token or not token_hash:
        return False
    computed_hash = hash_pair_token(token)
    if not computed_hash:
        return False
    return secrets.compare_digest(computed_hash, token_hash)


def hash_identifier(value):
    """Hash a sensitive identifier (e.g., IP) before persistence."""
    if not value:
        return None
    pepper = _pair_token_pepper()
    payload = f"{value}{pepper}".encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def hash_short_code(value):
    """Hash short code before persistence."""
    return hash_identifier(value)
