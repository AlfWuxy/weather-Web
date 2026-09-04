# -*- coding: utf-8 -*-
"""User-facing shared constants and helpers."""
import secrets
from datetime import timedelta

from flask import current_app, flash, has_app_context, url_for
from flask_login import current_user

from core.extensions import db
from core.db_models import (
    FamilyMember,
    FamilyMemberProfile,
    HealthDiary,
    MedicationReminder,
    Notification,
    Pair,
    PairActionToken,
    PairLink,
    UsageEvent,
)
from core.security import hash_identifier, hash_pair_token, hash_short_code
from core.time_utils import utcnow
from core.daily_tips import HEAT_RISK_LABELS, label_for_heat_level
from utils.validators import sanitize_input

RELAY_STAGE_ORDER = ['none', 'caregiver', 'backup', 'community', 'emergency']
RELAY_STAGE_LABELS = {
    'caregiver': '照护人',
    'backup': '备选联系人',
    'community': '社区',
    'emergency': '紧急'
}
AUTO_ESCALATE_AFTER = timedelta(hours=2)
AUTO_ESCALATE_STAGE = 'backup'
DEFAULT_ACTION_TOKEN_TTL_DAYS = 90
DEFAULT_SHORT_CODE_TTL_DAYS = 90

CARE_ACTION_OPTIONS = [
    {'id': 'remind', 'label': '提醒'},
    {'id': 'neighbor', 'label': '联系邻里'},
    {'id': 'community', 'label': '联系社区'}
]

ANNOUNCE_DISCLAIMER_LINES = [
    '行动/风险提示为通用建议，不提供医疗诊断、处方或治疗建议。',
    '天气与模型数据可能因同步延迟或缺失而偏差，结果仅作行动提醒。',
    '账户及家庭健康资料会按用户主动填写内容保存在服务器；公开传播内容不展示这些资料。',
    '页面内个人阈值与“本机备选联系人”只保存在当前浏览器。'
]
ANNOUNCE_SOURCE_LINES = [
    '天气数据：和风天气（QWeather）API。',
    '行动数据：仅记录短码、社区与行动状态，不含个人身份信息。',
    '社区资源：由社区/管理员维护（避暑点信息）。'
]


def _risk_level_value(label):
    return {
        '低风险': 1,
        '中风险': 2,
        '高风险': 3,
        '极高': 4
    }.get(label, 0)


def _relay_stage_rank(stage):
    if not stage:
        return 0
    try:
        return RELAY_STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def _action_plan(risk_label):
    from core.daily_tips import action_plan_for_risk

    return action_plan_for_risk(risk_label)


def _generate_short_code():
    for _ in range(20):
        code = str(secrets.randbelow(100000000)).zfill(8)
        code_hash = hash_short_code(code)
        exists = Pair.query.filter_by(short_code_hash=code_hash).first()
        if not exists:
            exists = PairLink.query.filter_by(short_code_hash=code_hash).first()
        if not exists:
            return code
    raise RuntimeError('短码生成失败，请重试')


def _generate_elder_code():
    for _ in range(20):
        candidate = secrets.token_urlsafe(8)
        if not Pair.query.filter_by(elder_code=candidate).first():
            return candidate
    raise RuntimeError('老人码生成失败，请重试')


def _configured_ttl_days(key, default):
    if not has_app_context():
        return default
    try:
        value = int(current_app.config.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(1, value)


def _short_code_expires_at():
    return utcnow() + timedelta(days=_configured_ttl_days('SHORT_CODE_TTL_DAYS', DEFAULT_SHORT_CODE_TTL_DAYS))


def _action_token_expires_at():
    return utcnow() + timedelta(days=_configured_ttl_days('PAIR_ACTION_TOKEN_TTL_DAYS', DEFAULT_ACTION_TOKEN_TTL_DAYS))


def _create_pair_record(caregiver_id, location_query, member_id=None, flush=False):
    """创建 Pair 记录，供 Web/小程序统一复用。"""
    location_query = sanitize_input(location_query, max_length=200) or ''
    location_query = location_query.strip()
    if not location_query:
        raise ValueError('location_query is required')

    short_code = _generate_short_code()
    pair = Pair(
        caregiver_id=caregiver_id,
        community_code=location_query[:100],
        location_query=location_query,
        member_id=member_id,
        elder_code=_generate_elder_code(),
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        short_code_expires_at=_short_code_expires_at(),
        status='active',
        last_active_at=utcnow(),
        created_at=utcnow(),
    )
    db.session.add(pair)
    if flush:
        db.session.flush()
    return pair


def unbind_family_member_for_caregiver(user_id, member_id):
    """删除照护人名下的家庭成员，并解绑其所有配对。不提交事务。

    Pair 行保留：member_id 置空、status 设为 inactive。该成员的健康日记、
    用药提醒和通知删除；UsageEvent 保留行但去掉 member_id。
    找不到归属该照护人的成员时返回 False。
    """
    member = FamilyMember.query.filter_by(id=member_id, user_id=user_id).first()
    if not member:
        return False

    HealthDiary.query.filter_by(member_id=member.id, user_id=user_id).delete()
    MedicationReminder.query.filter_by(member_id=member.id, user_id=user_id).delete()
    Notification.query.filter_by(member_id=member.id, user_id=user_id).delete()
    UsageEvent.query.filter_by(member_id=member.id, user_id=user_id).update(
        {UsageEvent.member_id: None},
        synchronize_session=False,
    )
    pairs = Pair.query.filter_by(member_id=member.id, caregiver_id=user_id).all()
    for pair in pairs:
        pair.member_id = None
        pair.status = 'inactive'
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
    if profile:
        db.session.delete(profile)
    db.session.delete(member)
    return True


def unbind_pair_for_caregiver(user_id, pair):
    """解绑照护人名下的一条配对。不提交事务。

    若配对挂了该照护人的家庭成员，语义与网页删除家人相同（删档案并停用
    该成员的全部配对）。仅有地点、没有成员的配对只把本条标为 inactive。
    """
    if pair is None or pair.caregiver_id != user_id:
        return False
    if pair.member_id:
        unbound = unbind_family_member_for_caregiver(user_id, pair.member_id)
        if not unbound:
            pair.member_id = None
            pair.status = 'inactive'
        return True
    pair.status = 'inactive'
    return True


def _derive_pair_action_token(record):
    """从令牌记录与服务端 pepper 派生可重复生成的明文 token。"""
    if not record or not getattr(record, 'id', None) or not getattr(record, 'pair_id', None):
        return None
    return hash_identifier(f'pair-action-record:{record.pair_id}:{record.id}')


def _create_pair_action_token(pair, flush=False):
    """创建或复用行动链接 token，数据库始终只保存哈希。"""
    if not pair or not getattr(pair, 'id', None):
        raise ValueError('pair id is required')

    now = utcnow()
    reusable = PairActionToken.query.filter(
        PairActionToken.pair_id == pair.id,
        PairActionToken.revoked_at.is_(None),
        PairActionToken.expires_at >= now,
    ).order_by(PairActionToken.id.desc()).first()
    if reusable:
        token = _derive_pair_action_token(reusable)
        expected_hash = hash_pair_token(token)
        if (
            token
            and expected_hash
            and reusable.token_hash
            and secrets.compare_digest(expected_hash, reusable.token_hash)
        ):
            return reusable, token

    # 清理已经失效的历史记录，避免长期运行后表持续增长。
    PairActionToken.query.filter(
        PairActionToken.pair_id == pair.id,
        PairActionToken.expires_at < now,
    ).delete(synchronize_session=False)

    # 先写入一次性占位哈希取得记录 ID，再用 ID 派生可重建 token。
    placeholder = secrets.token_urlsafe(32)
    record = PairActionToken(
        pair_id=pair.id,
        token_hash=hash_pair_token(f'pending:{placeholder}'),
        expires_at=_action_token_expires_at(),
        created_at=now,
    )
    db.session.add(record)
    db.session.flush()
    token = _derive_pair_action_token(record)
    record.token_hash = hash_pair_token(token)
    if flush:
        db.session.flush()
    return record, token


def _build_pair_action_link(pair, external=True):
    """为照护提醒生成带 token 的行动链接。"""
    _, token = _create_pair_action_token(pair, flush=True)
    return url_for(
        'public.elder_token_entry',
        token=token,
        short_code=pair.short_code,
        _external=external
    )


def _create_pair_link_record(caregiver_id, community_code, expires_after=None, flush=False):
    """创建 PairLink 记录，统一短码/token 生成逻辑。"""
    community_code = sanitize_input(community_code, max_length=100) or ''
    community_code = community_code.strip()
    if not community_code:
        raise ValueError('community_code is required')

    short_code = _generate_short_code()
    token = secrets.token_urlsafe(16)
    expires_after = expires_after or timedelta(days=3)
    link = PairLink(
        caregiver_id=caregiver_id,
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        token_hash=hash_pair_token(token),
        community_code=community_code,
        expires_at=utcnow() + expires_after,
    )
    db.session.add(link)
    if flush:
        db.session.flush()
    return link, token


def _normalize_code(value):
    if not value:
        return ''
    return sanitize_input(value, max_length=100).strip()


def _require_roles(*roles):
    if getattr(current_user, 'role', None) in roles:
        return True
    flash('权限不足', 'error')
    return False
