# -*- coding: utf-8 -*-
"""Security helpers."""
import hashlib
import logging
import os
import secrets
from functools import wraps

from flask import abort, current_app, flash, has_app_context, jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)
from flask_login import current_user
from flask_limiter.util import get_remote_address

from core.constants import GUEST_ID_PREFIX


def _is_guest_rate_limit_subject(user):
    """游客：is_guest、role=guest、或 id 前缀 guest:。"""
    if getattr(user, 'is_guest', False):
        return True
    if getattr(user, 'role', None) == 'guest':
        return True
    uid = str(getattr(user, 'id', '') or '')
    return uid.startswith(GUEST_ID_PREFIX)


def _client_ip_for_rate_limit():
    """解析受信客户端 IP；失败或空则回退 get_remote_address()。"""
    try:
        # core.audit 已 import hash_identifier 与 db，必须 lazy 以免环依赖
        from core.audit import _get_client_ip
        client_ip = _get_client_ip()
    except Exception:
        client_ip = None
    if not client_ip:
        client_ip = get_remote_address()
    return client_ip


def _client_ip_rate_key():
    """未登录与游客共用的 IP 限流键。

    必须带 ``ip:`` 前缀：Flask-Limiter 按字符串分桶，
    若未登录返回裸 IP、游客返回 ``ip:IP``，同一客户端会落两套桶
    （匿名 weather 与 guest 会话可各刷一份配额）。
    """
    return f"ip:{_client_ip_for_rate_limit()}"


def rate_limit_key():
    """按正式用户或受信客户端 IP 分桶。

    正式登录用户：``user:<id>``。
    游客（is_guest / role=guest / id 前缀 guest:）与未登录：``ip:<client_ip>``。
    游客每次 GET /guest 会换新 id，不能按 user id 限流。
    """
    if getattr(current_user, 'is_authenticated', False) and not _is_guest_rate_limit_subject(current_user):
        uid = str(getattr(current_user, 'id', '') or '')
        return f"user:{uid}"
    return _client_ip_rate_key()


def _is_guest_subject():
    """判断当前主体是否为游客（与 rate_limit_key 判定对齐，含兜底）。"""
    if not getattr(current_user, 'is_authenticated', False):
        return False
    return _is_guest_rate_limit_subject(current_user)


def _wants_api_error_payload():
    """API 路径或显式要 JSON 时返回结构化 403。"""
    if request.path.startswith('/api/'):
        return True
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    if best == 'application/json' and (
        request.accept_mimetypes[best] > request.accept_mimetypes['text/html']
    ):
        return True
    return False


def reject_guest(view_func):
    """拒绝游客访问高成本/计费能力。

    用法：与 @login_required 组合（推荐 @login_required 在上）：
        @login_required
        @reject_guest
        def expensive_api(): ...

    行为：
    - 未登录：不拦截，交给 @login_required（或后续逻辑）
    - 游客：API → 403 JSON；页面 → flash + 跳转 dashboard
    - 正式用户：放行
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if _is_guest_subject():
            message = '游客不可用，请注册登录'
            if _wants_api_error_payload():
                return jsonify({
                    'success': False,
                    'error': 'guest_not_allowed',
                    'message': message,
                }), 403
            flash(message, 'error')
            try:
                return redirect(url_for('user.user_dashboard'))
            except Exception:
                # 无 dashboard 端点时回退硬 403
                abort(403)
        return view_func(*args, **kwargs)

    return wrapped


# 别名：语义上表示「需要正式登录用户」
real_login_required = reject_guest


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
