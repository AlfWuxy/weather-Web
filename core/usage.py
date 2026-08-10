# -*- coding: utf-8 -*-
"""Pilot-loop helpers: API tokens + usage events.

These are product analytics (打开率/触发/反馈等) rather than security audit logs.
只保存固定枚举元数据，不保存家庭或成员标识，并在 30 天后自动删除事件。
"""

import json
import logging
import secrets
from datetime import timedelta

from flask import current_app, has_app_context
from sqlalchemy import case, or_

from core.db_models import AlertDelivery, ApiToken, UsageEvent, User
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
USAGE_EVENT_RETENTION_DAYS = 30
USAGE_EVENT_RETENTION_BATCH_SIZE = 1000
USAGE_EVENT_RETENTION_MAX_BATCHES = 10
ALERT_DELIVERY_CLICK_RETENTION_DAYS = 30
_VALID_ACTION_EVENT_TYPE = 'checkin_confirmed'
PILOT_EVENT_TYPES = frozenset({
    'pair_created',
    'elder_profile_created',
    'elder_profile_updated',
    'template_view',
    'template_copy',
    'push_sent',
    'push_failed',
    'push_click',
    'feedback_submitted',
    'help_flagged',
    'checkin_confirmed',
    'wxoa_land',
    'wechat_login_success',
})
WEB_CLIENT_PILOT_EVENT_TYPES = frozenset({'template_copy', 'feedback_submitted'})
MINIPROGRAM_CLIENT_PILOT_EVENT_TYPES = frozenset({'template_copy'})
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
    commit=True,
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
    if commit:
        db.session.commit()
    return plain


def verify_api_token(plain_token):
    """验证短期、具名 scope 的 API token；旧无期限 token 必须轮换。"""
    if not plain_token:
        return None
    token_hash = hash_identifier(plain_token)
    if not token_hash:
        return None
    verification_query = (
        db.select(ApiToken, User)
        .join(User, User.id == ApiToken.user_id)
        .where(
            ApiToken.token_hash == token_hash,
            ApiToken.revoked_at.is_(None),
            User.deleted_at.is_(None),
        )
        # 写请求会在同一 Session 内再次复验，不能沿用旧的 ORM 字段值。
        .execution_options(populate_existing=True)
        .limit(1)
    )
    verified = db.session.execute(verification_query).first()
    if verified is None:
        return None
    record, user = verified
    if record.expires_at is None:
        return None
    if ensure_utc_aware(record.expires_at) <= utcnow():
        return None
    record._verified_user = user
    return record


def _lock_active_usage_event_owner_for_write(user_id):
    """与账号注销共用 User 行锁，并在取锁后复核注销墓碑。"""
    if db.engine.dialect.name == 'sqlite':
        # SQLite 忽略 SELECT FOR UPDATE，条件 no-op UPDATE 会取得写锁。
        lock_result = db.session.execute(
            db.update(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .values(last_login=User.last_login)
        )
        if lock_result.rowcount != 1:
            return None
        return db.session.get(User, user_id)

    return db.session.execute(
        db.select(User)
        .where(User.id == user_id, User.deleted_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()


def log_usage_event(event_type, user_id=None, pair_id=None, member_id=None, source='web', meta=None):
    """Best-effort usage event logging；家庭和成员参数仅用于兼容旧调用。"""
    if not event_type:
        return None
    try:
        normalized_event_type = str(event_type)[:50]
        payload = None
        safe_meta = _sanitize_usage_meta(meta)
        # 行动激活只能由至少一项已完成行动构成，空确认不进入激活口径。
        if normalized_event_type == _VALID_ACTION_EVENT_TYPE:
            actions_done_count = (safe_meta or {}).get('actions_done_count')
            if not isinstance(actions_done_count, int) or actions_done_count < 1:
                return None
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
        if (
            user_id is not None
            and _lock_active_usage_event_owner_for_write(user_id) is None
        ):
            # 释放当前锁事务，并丢弃已注销账号的分析事件。
            db.session.rollback()
            return None
        event = UsageEvent(
            user_id=user_id,
            # 保留 pair_id/member_id 参数兼容旧调用，分析事件只做账号级聚合。
            pair_id=None,
            member_id=None,
            event_type=normalized_event_type,
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


def delete_expired_usage_events(
    *,
    now=None,
    batch_size=USAGE_EVENT_RETENTION_BATCH_SIZE,
    max_batches=USAGE_EVENT_RETENTION_MAX_BATCHES,
):
    """按固定 30 天策略分批删除埋点，避免定时任务持有大范围写锁。"""
    reference_time = ensure_utc_aware(now or utcnow())
    cutoff = reference_time - timedelta(days=USAGE_EVENT_RETENTION_DAYS)
    batch_size = max(1, min(int(batch_size), 10_000))
    max_batches = max(1, min(int(max_batches), 100))
    deleted_total = 0
    complete = False

    for _batch_number in range(max_batches):
        expired_ids = [
            row[0]
            for row in db.session.query(UsageEvent.id)
            .filter(
                or_(
                    UsageEvent.created_at.is_(None),
                    UsageEvent.created_at < cutoff,
                )
            )
            .order_by(UsageEvent.created_at.asc(), UsageEvent.id.asc())
            .limit(batch_size)
            .all()
        ]
        if not expired_ids:
            complete = True
            break

        deleted = UsageEvent.query.filter(
            UsageEvent.id.in_(expired_ids),
        ).delete(synchronize_session=False)
        db.session.commit()
        deleted_total += int(deleted or 0)
        if len(expired_ids) < batch_size:
            complete = True
            break

    return {
        'deleted': deleted_total,
        'cutoff': cutoff,
        'complete': complete,
    }


def clear_expired_alert_delivery_clicks(
    *,
    now=None,
    batch_size=USAGE_EVENT_RETENTION_BATCH_SIZE,
    max_batches=USAGE_EVENT_RETENTION_MAX_BATCHES,
):
    """分批清空超过 30 天的点击时间，保留投递幂等记录。"""
    reference_time = ensure_utc_aware(now or utcnow())
    cutoff = reference_time - timedelta(days=ALERT_DELIVERY_CLICK_RETENTION_DAYS)
    batch_size = max(1, min(int(batch_size), 10_000))
    max_batches = max(1, min(int(max_batches), 100))
    cleared_total = 0
    complete = False

    for _batch_number in range(max_batches):
        expired_ids = [
            row[0]
            for row in db.session.query(AlertDelivery.id)
            .filter(AlertDelivery.clicked_at < cutoff)
            .order_by(AlertDelivery.clicked_at.asc(), AlertDelivery.id.asc())
            .limit(batch_size)
            .all()
        ]
        if not expired_ids:
            complete = True
            break

        click_confirmed = AlertDelivery.review_action == 'click_confirmed'
        cleared = AlertDelivery.query.filter(
            AlertDelivery.id.in_(expired_ids),
            AlertDelivery.clicked_at < cutoff,
        ).update(
            {
                AlertDelivery.clicked_at: None,
                # 点击自动确认与 clicked_at 属于同一行为记录；人工复核时间保持不动。
                AlertDelivery.reviewed_at: case(
                    (click_confirmed, None),
                    else_=AlertDelivery.reviewed_at,
                ),
                AlertDelivery.review_action: case(
                    (click_confirmed, None),
                    else_=AlertDelivery.review_action,
                ),
            },
            synchronize_session=False,
        )
        db.session.commit()
        cleared_total += int(cleared or 0)
        if len(expired_ids) < batch_size:
            complete = True
            break

    return {
        'cleared': cleared_total,
        'cutoff': cutoff,
        'complete': complete,
    }
