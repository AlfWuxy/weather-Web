# -*- coding: utf-8 -*-
"""Authentication hooks."""
import hashlib
import hmac

from flask import current_app, has_app_context

from core.constants import GUEST_ID_PREFIX
from core.guest import build_guest_user
from core.db_models import User


def password_session_stamp(password_hash):
    """HMAC-SHA256(SECRET_KEY, password_hash) 的前 16 位十六进制。

    戳绑定服务器密钥。只拿到 password_hash 时不能离线拼出会话身份。
    缺少应用上下文或 SECRET_KEY 时抛出 RuntimeError，禁止退回无密钥摘要。
    """
    if not has_app_context():
        raise RuntimeError('会话密码戳需要应用上下文')
    key = current_app.config.get('SECRET_KEY')
    if isinstance(key, str):
        key_bytes = key.encode('utf-8')
    elif isinstance(key, (bytes, bytearray)):
        key_bytes = bytes(key)
    else:
        raise RuntimeError('SECRET_KEY 未配置，无法计算会话密码戳')
    if not key_bytes:
        raise RuntimeError('SECRET_KEY 未配置，无法计算会话密码戳')
    message = (password_hash or '').encode('utf-8')
    return hmac.new(key_bytes, message, hashlib.sha256).hexdigest()[:16]


def register_user_loader(login_manager):
    """Register the login manager user loader."""
    @login_manager.user_loader
    def load_user(user_id):
        if not user_id:
            return None
        if isinstance(user_id, str) and user_id.startswith(GUEST_ID_PREFIX):
            return build_guest_user(user_id)
        try:
            # SQLAlchemy 2.x: 使用 session.get() 替代废弃的 query.get()
            from core.extensions import db
            # 正式账号只接受 get_id() 生成的 "{id}:{password_stamp}" 格式。
            raw = str(user_id)
            uid_part, separator, stamp_part = raw.partition(':')
            if (
                separator != ':'
                or ':' in stamp_part
                or not uid_part.isascii()
                or not uid_part.isdigit()
            ):
                return None
            user = db.session.get(User, int(uid_part))
            if not user:
                return None
            try:
                expected_raw = f'{user.id}:{password_session_stamp(user.password_hash)}'
            except RuntimeError:
                return None
            # 拒绝旧纯数字会话、无密钥 sha256 戳、畸形 ID 与改密前的密码戳。
            if not hmac.compare_digest(raw, expected_raw):
                return None
            return user
        except (OverflowError, TypeError, ValueError):
            return None
