# -*- coding: utf-8 -*-
"""微信小程序服务端登录与可撤销签名会话。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

import requests
from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import or_

from core.db_models import MiniProgramIdentity, MiniProgramSession, User
from core.extensions import db
from core.time_utils import ensure_utc_aware, utcnow


WECHAT_CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
SESSION_SALT = "yilao-miniprogram-session-v1"


class MiniProgramAuthError(RuntimeError):
    """只携带稳定错误码和可公开消息，避免泄露上游认证材料。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _required_config(name: str) -> str:
    value = str(current_app.config.get(name) or "").strip()
    if not value:
        raise MiniProgramAuthError(
            "wechat_auth_not_configured",
            "微信小程序登录尚未配置完整，请联系管理员。",
            503,
        )
    return value


def current_privacy_version() -> str:
    return str(
        current_app.config.get("WX_MINIPROGRAM_PRIVACY_VERSION")
        or "2026-07-18"
    ).strip()


def _session_ttl_seconds() -> int:
    try:
        value = int(current_app.config.get("WX_MINIPROGRAM_SESSION_TTL_SECONDS", 604800))
    except (TypeError, ValueError):
        value = 604800
    return max(300, min(value, 2592000))


def _max_active_sessions() -> int:
    try:
        value = int(current_app.config.get("WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS", 5))
    except (TypeError, ValueError):
        value = 5
    return max(1, min(value, 20))


def _serializer() -> URLSafeTimedSerializer:
    secret = _required_config("WX_MINIPROGRAM_SESSION_SECRET")
    return URLSafeTimedSerializer(secret_key=secret, salt=SESSION_SALT)


def hash_openid(openid: str) -> str:
    """使用独立 pepper 对 OpenID 做不可逆 HMAC，不复用普通 SHA。"""
    pepper = _required_config("WX_MINIPROGRAM_OPENID_PEPPER")
    return hmac.new(
        pepper.encode("utf-8"),
        str(openid).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _hash_session_token(token: str) -> str:
    secret = _required_config("WX_MINIPROGRAM_SESSION_SECRET")
    return hmac.new(
        secret.encode("utf-8"),
        str(token).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def exchange_wechat_code(code: str) -> str:
    """仅在服务器向微信换取 OpenID；session_key 不落库也不向客户端返回。"""
    appid = _required_config("WX_MINIPROGRAM_APPID")
    appsecret = _required_config("WX_MINIPROGRAM_SECRET")
    clean_code = str(code or "").strip()
    if not clean_code or len(clean_code) > 128:
        raise MiniProgramAuthError("invalid_login_code", "登录凭证无效。", 400)

    try:
        response = requests.get(
            WECHAT_CODE2SESSION_URL,
            params={
                "appid": appid,
                "secret": appsecret,
                "js_code": clean_code,
                "grant_type": "authorization_code",
            },
            timeout=float(current_app.config.get("WX_MINIPROGRAM_AUTH_TIMEOUT", 8)),
        )
    except requests.RequestException as exc:
        raise MiniProgramAuthError(
            "wechat_auth_unavailable",
            "微信登录服务暂时不可用，请稍后重试。",
            503,
        ) from exc

    if response.status_code != 200:
        raise MiniProgramAuthError(
            "wechat_auth_upstream_error",
            "微信登录服务返回异常，请稍后重试。",
            502,
        )
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise MiniProgramAuthError(
            "wechat_auth_invalid_response",
            "微信登录服务响应无法识别，请稍后重试。",
            502,
        ) from exc

    if not isinstance(payload, dict) or payload.get("errcode"):
        raise MiniProgramAuthError(
            "wechat_code_rejected",
            "登录凭证已失效，请重新打开小程序后重试。",
            401,
        )
    openid = str(payload.get("openid") or "").strip()
    if not openid or len(openid) > 128:
        raise MiniProgramAuthError(
            "wechat_auth_invalid_response",
            "微信登录服务响应缺少必要身份信息。",
            502,
        )
    return openid


def _create_wechat_user(openid_hash: str) -> User:
    """创建最小普通用户档案，用户名只使用哈希前缀。"""
    base = f"wx_{openid_hash[:24]}"
    username = base
    suffix = 0
    while User.query.filter_by(username=username).first() is not None:
        suffix += 1
        username = f"{base[:70]}_{suffix}"
    user = User(username=username, role="user", created_at=utcnow())
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(user)
    db.session.flush()
    return user


def login_with_wechat_code(
    code: str,
    privacy_consent_version: str,
    acquisition_source: str = "direct",
) -> dict:
    """校验隐私同意、完成 code2session，并签发可撤销会话。"""
    required_version = current_privacy_version()
    consent_version = str(privacy_consent_version or "").strip()
    if not consent_version or consent_version != required_version:
        raise MiniProgramAuthError(
            "privacy_consent_required",
            "请先阅读并同意当前版本的隐私保护指引。",
            428,
        )

    openid = exchange_wechat_code(code)
    openid_digest = hash_openid(openid)
    now = utcnow()
    normalized_acquisition = (
        "family_share" if acquisition_source == "family_share" else "direct"
    )
    identity = MiniProgramIdentity.query.filter_by(openid_hash=openid_digest).first()

    try:
        if identity is None:
            user = _create_wechat_user(openid_digest)
            identity = MiniProgramIdentity(
                user_id=user.id,
                openid_hash=openid_digest,
                privacy_consent_version=required_version,
                privacy_consented_at=now,
                acquisition_source=normalized_acquisition,
                created_at=now,
                last_login_at=now,
            )
            db.session.add(identity)
            db.session.flush()
        else:
            user = db.session.get(User, identity.user_id)
            if user is None:
                raise MiniProgramAuthError(
                    "wechat_identity_invalid",
                    "微信身份关联异常，请联系管理员。",
                    409,
                )
            identity.privacy_consent_version = required_version
            identity.privacy_consented_at = now
            identity.last_login_at = now
            if identity.acquisition_source not in {
                "direct",
                "family_share",
                "unknown",
            }:
                identity.acquisition_source = "unknown"

        # 每次登录清理本身份已失效会话，避免长期运行后表无限增长。
        MiniProgramSession.query.filter(
            MiniProgramSession.identity_id == identity.id,
            or_(
                MiniProgramSession.expires_at <= now,
                MiniProgramSession.revoked_at.is_not(None),
            ),
        ).delete(synchronize_session=False)

        ttl_seconds = _session_ttl_seconds()
        session_record = MiniProgramSession(
            identity_id=identity.id,
            user_id=user.id,
            token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            privacy_consent_version=required_version,
            expires_at=now + timedelta(seconds=ttl_seconds),
            created_at=now,
            last_used_at=now,
        )
        db.session.add(session_record)
        db.session.flush()
        token = _serializer().dumps(
            {
                "sid": session_record.id,
                "uid": user.id,
                "pv": required_version,
                "nonce": secrets.token_urlsafe(12),
            }
        )
        session_record.token_hash = _hash_session_token(token)
        overflow = (
            MiniProgramSession.query.filter_by(identity_id=identity.id)
            .filter(MiniProgramSession.revoked_at.is_(None))
            .order_by(MiniProgramSession.created_at.desc(), MiniProgramSession.id.desc())
            .offset(_max_active_sessions())
            .all()
        )
        for old_session in overflow:
            old_session.revoked_at = now
        db.session.commit()
    except MiniProgramAuthError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        raise MiniProgramAuthError(
            "wechat_session_create_failed",
            "登录会话创建失败，请稍后重试。",
            503,
        ) from exc

    return {
        "session_token": token,
        "token": token,
        "token_type": "Bearer",
        "expires_at": ensure_utc_aware(session_record.expires_at).isoformat(),
        "expires_in": ttl_seconds,
        "privacy_consent_version": required_version,
        "user": {"id": user.id, "display_name": "微信用户"},
    }


def verify_miniprogram_session(token: str):
    """验证签名、时效、库内撤销状态和隐私版本，返回会话记录。"""
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=_session_ttl_seconds())
    except (SignatureExpired, BadSignature, MiniProgramAuthError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        session_id = int(payload.get("sid"))
        user_id = int(payload.get("uid"))
    except (TypeError, ValueError):
        return None

    record = db.session.get(MiniProgramSession, session_id)
    if record is None or record.revoked_at is not None:
        return None
    identity = db.session.get(MiniProgramIdentity, record.identity_id)
    user = db.session.get(User, record.user_id)
    if (
        identity is None
        or user is None
        or user.deleted_at is not None
        or identity.user_id != record.user_id
    ):
        return None
    now = utcnow()
    if ensure_utc_aware(record.expires_at) <= now:
        return None
    if record.user_id != user_id:
        return None
    required_version = current_privacy_version()
    if payload.get("pv") != record.privacy_consent_version:
        return None
    if record.privacy_consent_version != required_version:
        return None
    expected_hash = _hash_session_token(token)
    if not record.token_hash or not hmac.compare_digest(record.token_hash, expected_hash):
        return None
    return record
