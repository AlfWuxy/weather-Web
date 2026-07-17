# -*- coding: utf-8 -*-
"""Pilot-loop helpers: API tokens + usage events.

These are product analytics (打开率/触发/反馈等) rather than security audit logs.
We store only minimal structured metadata; avoid PII.
"""

import json
import logging
import secrets
from datetime import timedelta

from flask import current_app, has_app_context

from core.db_models import ApiToken, UsageEvent, User
from core.extensions import db
from core.security import hash_identifier
from core.time_utils import ensure_utc_aware, utcnow

logger = logging.getLogger(__name__)
DEFAULT_API_TOKEN_SCOPES = (
    "miniprogram:read",
    "miniprogram:write",
    "miniprogram:sensitive",
)

_USAGE_SOURCES = {'web', 'miniprogram', 'cron', 'system'}
_USAGE_META_ENUMS = {
    'via': {'family_members', 'family_member_new', 'family_member_edit', 'mp_api'},
    'channel': {'web', 'wxpusher', 'wechat_miniprogram', 'wechat_official_account'},
    'relay_stage': {'none', 'caregiver', 'backup', 'community', 'emergency'},
    'from': {
        'direct',
        'wechat',
        'wechat_official',
        'wechat_official_account',
        'wechat_miniprogram',
        'family_share',
        'community',
        'community_poster',
        'poster',
        'qr',
    },
    'article': {
        'launch',
        'heat_alert',
        'cold_alert',
        'weather_alert',
        'care_guide',
        'community_guide',
    },
}
_USAGE_META_BOOLEAN_KEYS = {'has_note', 'optin'}
_USAGE_META_INTEGER_LIMITS = {
    'alert_id': (1, 2_147_483_647),
    'actions_done_count': (0, 1000),
    'caregiver_actions_count': (0, 1000),
    'difficulty_len': (0, 300),
}
_USAGE_META_PROFILE_FIELDS = {
    'name',
    'relation',
    'age',
    'gender',
    'chronic_diseases',
    'wxpusher_uid',
    'push_enabled',
}


def _sanitize_usage_meta(meta):
    """仅保留无法回推姓名、电话或自由地点的匿名分析维度。"""
    if not isinstance(meta, dict):
        return None

    safe = {}
    for key, allowed_values in _USAGE_META_ENUMS.items():
        value = meta.get(key)
        if isinstance(value, str) and value in allowed_values:
            safe[key] = value

    for key in _USAGE_META_BOOLEAN_KEYS:
        value = meta.get(key)
        if isinstance(value, bool):
            safe[key] = value

    for key, (minimum, maximum) in _USAGE_META_INTEGER_LIMITS.items():
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            safe[key] = max(minimum, min(value, maximum))

    for key in ('fields', 'updated_fields'):
        value = meta.get(key)
        if isinstance(value, (list, tuple)):
            fields = []
            for item in value:
                if item in _USAGE_META_PROFILE_FIELDS and item not in fields:
                    fields.append(item)
            if fields:
                safe[key] = fields

    # 位置、预警和错误仅记录县域级或布尔级摘要，不保留原文。
    if meta.get('location_query') or meta.get('location_code'):
        safe['location_scope'] = 'duchang_county'
    if meta.get('alert_type'):
        safe['alert_scope'] = 'weather_alert'
    if meta.get('error'):
        safe['has_error'] = True
    return safe or None


def _token_ttl_days(value=None):
    if value is None and has_app_context():
        value = current_app.config.get("API_TOKEN_TTL_DAYS", 30)
    try:
        parsed = int(value if value is not None else 30)
    except (TypeError, ValueError):
        parsed = 30
    return max(1, min(parsed, 365))


def normalize_api_token_scopes(scopes=None):
    """返回稳定、去重的 scope 元组。"""
    values = DEFAULT_API_TOKEN_SCOPES if scopes is None else scopes
    if isinstance(values, str):
        values = values.replace(",", " ").split()
    normalized = []
    for value in values or ():
        scope = str(value or "").strip().lower()
        if scope and scope not in normalized and len(scope) <= 64:
            normalized.append(scope)
    return tuple(normalized)


def api_token_has_scope(record, scope):
    if record is None:
        return False
    required = str(scope or "").strip().lower()
    return required in normalize_api_token_scopes(record.scopes or ())


def create_api_token(
    user_id,
    name=None,
    *,
    scopes=None,
    ttl_days=None,
    privacy_consent_version=None,
):
    """Create an API token for miniprogram binding.

    Returns the *plain token* (display once); only the hash is persisted.
    """
    if not user_id:
        raise ValueError("user_id is required")

    plain = secrets.token_urlsafe(24)
    token_hash = hash_identifier(plain)
    now = utcnow()
    normalized_scopes = normalize_api_token_scopes(scopes)
    if not normalized_scopes:
        raise ValueError("at least one API token scope is required")
    consent_version = privacy_consent_version
    if consent_version is None and has_app_context():
        consent_version = current_app.config.get("WX_MINIPROGRAM_PRIVACY_VERSION")
    consent_version = str(consent_version or "").strip()
    if not consent_version or len(consent_version) > 64:
        raise ValueError("privacy_consent_version is required")
    record = ApiToken(
        user_id=user_id,
        name=name,
        token_hash=token_hash,
        created_at=now,
        expires_at=now + timedelta(days=_token_ttl_days(ttl_days)),
        scopes=" ".join(normalized_scopes),
        privacy_consent_version=consent_version,
    )
    db.session.add(record)
    db.session.commit()
    return plain


def verify_api_token(plain_token):
    """验证短期、具名 scope 的 API token；旧无期限 token 必须轮换。"""
    if not plain_token:
        return None
    token_hash = hash_identifier(plain_token)
    if not token_hash:
        return None
    record = ApiToken.query.filter(
        ApiToken.token_hash == token_hash,
        ApiToken.revoked_at.is_(None),
    ).first()
    if record is None or record.expires_at is None:
        return None
    if ensure_utc_aware(record.expires_at) <= utcnow():
        return None
    user = db.session.get(User, record.user_id)
    if user is None or user.deleted_at is not None:
        return None
    return record


def log_usage_event(event_type, user_id=None, pair_id=None, member_id=None, source='web', meta=None):
    """Best-effort usage event logging (fail open)."""
    if not event_type:
        return None
    try:
        payload = None
        safe_meta = _sanitize_usage_meta(meta)
        if safe_meta is not None:
            payload = json.dumps(
                safe_meta,
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            )
        normalized_source = str(source or '').strip().lower()
        if normalized_source not in _USAGE_SOURCES:
            normalized_source = 'web'
        event = UsageEvent(
            user_id=user_id,
            pair_id=pair_id,
            member_id=member_id,
            event_type=str(event_type)[:50],
            meta_json=payload,
            source=normalized_source,
            created_at=utcnow(),
        )
        db.session.add(event)
        db.session.commit()
        return event
    except Exception as exc:
        logger.debug("usage event write failed: %s", exc)
        db.session.rollback()
        return None
