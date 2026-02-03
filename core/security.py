# -*- coding: utf-8 -*-
"""Security helpers."""
import hashlib
import os
import secrets

from flask import abort, current_app, has_app_context, jsonify, request, session
from flask_login import current_user
from flask_limiter.util import get_remote_address


def rate_limit_key():
    """按用户或IP进行限流"""
    if current_user.is_authenticated:
        return str(getattr(current_user, 'id', 'anonymous'))
    return get_remote_address()


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


def _pair_token_pepper():
    if has_app_context():
        pepper = current_app.config.get('PAIR_TOKEN_PEPPER')
        if pepper:
            return pepper
    return os.getenv('PAIR_TOKEN_PEPPER') or ''


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
