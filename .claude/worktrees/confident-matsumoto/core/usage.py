# -*- coding: utf-8 -*-
"""Pilot-loop helpers: API tokens + usage events.

These are product analytics (打开率/触发/反馈等) rather than security audit logs.
We store only minimal structured metadata; avoid PII.
"""

import json
import logging
import secrets

from core.db_models import ApiToken, UsageEvent
from core.extensions import db
from core.security import hash_identifier
from core.time_utils import utcnow

logger = logging.getLogger(__name__)


def create_api_token(user_id, name=None):
    """Create an API token for miniprogram binding.

    Returns the *plain token* (display once); only the hash is persisted.
    """
    if not user_id:
        raise ValueError("user_id is required")

    plain = secrets.token_urlsafe(24)
    token_hash = hash_identifier(plain)
    record = ApiToken(
        user_id=user_id,
        name=name,
        token_hash=token_hash,
        created_at=utcnow(),
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
    return ApiToken.query.filter(
        ApiToken.token_hash == token_hash,
        ApiToken.revoked_at.is_(None),
    ).first()


def log_usage_event(event_type, user_id=None, pair_id=None, member_id=None, source='web', meta=None):
    """Best-effort usage event logging (fail open)."""
    if not event_type:
        return None
    try:
        payload = None
        if meta is not None:
            payload = json.dumps(meta, ensure_ascii=False)
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
        logger.debug("usage event write failed: %s", exc)
        db.session.rollback()
        return None

