# -*- coding: utf-8 -*-
"""Pilot-loop helpers: API tokens + usage events.

These are product analytics (打开率/触发/反馈等) rather than security audit logs.
We store only minimal structured metadata; avoid PII.
"""

import json
import logging
import secrets
from datetime import timedelta
from flask import current_app
from core.constants import GUEST_ID_PREFIX

from core.db_models import ApiToken, UsageEvent, User
from core.analytics_privacy import sanitize_analytics_meta
from core.extensions import db
from core.security import hash_identifier
from core.time_utils import utcnow

logger = logging.getLogger(__name__)


def create_api_token(user_id, name=None):
    """Create an API token for miniprogram binding.

    Returns the *plain token* (display once); only the hash is persisted.
    """
    if not user_id or db.session.get(User, user_id) is None:
        raise ValueError("existing user_id is required")

    plain = secrets.token_urlsafe(24)
    token_hash = hash_identifier(plain)
    record = ApiToken(
        user_id=user_id,
        name=name,
        token_hash=token_hash,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(days=api_token_ttl_days()),
    )
    db.session.add(record)
    db.session.commit()
    return plain


def verify_api_token(plain_token):
    """Verify a plain token and return ApiToken row if valid (not revoked)."""
    if not plain_token:
        return None
    token_hash = hash_identifier(plain_token)
    if not token_hash:
        return None
    return ApiToken.query.join(User, User.id == ApiToken.user_id).filter(
        ApiToken.token_hash == token_hash,
        ApiToken.revoked_at.is_(None),
        ApiToken.expires_at.isnot(None),
        ApiToken.expires_at > utcnow(),
        # 旧 SQLite 数据若已复用账号 ID，早于当前账号创建时间的凭证仍不可复活。
        ApiToken.created_at.isnot(None),
        User.created_at.isnot(None),
        ApiToken.created_at >= User.created_at,
    ).first()


def api_token_ttl_days():
    """配置无效时使用有限默认值，禁止配置成永久凭证。"""
    try:
        return max(1, min(int(current_app.config.get('API_TOKEN_TTL_DAYS', 30)), 90))
    except (TypeError, ValueError):
        return 30


def revoke_api_tokens(user_id, token_id=None):
    """仅撤销指定所有者的凭证，由调用方与账号变更一起提交。"""
    query = ApiToken.query.filter_by(user_id=user_id, revoked_at=None)
    if token_id is not None:
        query = query.filter_by(id=token_id)
    return query.update({'revoked_at': utcnow()}, synchronize_session='fetch')


def log_usage_event(event_type, user_id=None, pair_id=None, member_id=None, source='web', meta=None):
    """Best-effort usage event logging (fail open)."""
    if not event_type:
        return None
    # 游客只有会话标识，不是 users 表主键；保留匿名事件而不制造孤儿引用。
    if isinstance(user_id, str) and user_id.startswith(GUEST_ID_PREFIX):
        user_id = None
    try:
        payload = None
        safe_meta = sanitize_analytics_meta(event_type, meta)
        if safe_meta is not None:
            payload = json.dumps(safe_meta, ensure_ascii=False)
        event = UsageEvent(
            user_id=user_id,
            pair_id=pair_id,
            member_id=member_id,
            event_type=str(event_type)[:50],
            meta_json=payload,
            source=(str(source)[:20] if source else None),
            created_at=utcnow(),
        )
        db.session.add(event)
        db.session.commit()
        return event
    except Exception as exc:
        logger.debug("埋点写入失败，异常类型=%s", type(exc).__name__)
        db.session.rollback()
        return None
