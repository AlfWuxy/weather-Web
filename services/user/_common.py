# -*- coding: utf-8 -*-
"""User-facing shared constants and helpers."""
import secrets
from datetime import timedelta

from flask import flash
from flask_login import current_user

from core.extensions import db
from core.db_models import Pair, PairLink
from core.security import hash_pair_token, hash_short_code
from core.time_utils import utcnow
from utils.validators import sanitize_input


HEAT_RISK_LABELS = {
    'low': '低风险',
    'medium': '中风险',
    'high': '高风险',
    'extreme': '极高'
}

RELAY_STAGE_ORDER = ['none', 'caregiver', 'backup', 'community', 'emergency']
RELAY_STAGE_LABELS = {
    'caregiver': '照护人',
    'backup': '备选联系人',
    'community': '社区',
    'emergency': '紧急'
}
AUTO_ESCALATE_AFTER = timedelta(hours=2)
AUTO_ESCALATE_STAGE = 'backup'

CARE_ACTION_OPTIONS = [
    {'id': 'remind', 'label': '提醒'},
    {'id': 'neighbor', 'label': '联系邻里'},
    {'id': 'community', 'label': '联系社区'}
]

ANNOUNCE_DISCLAIMER_LINES = [
    '行动/风险提示为通用建议，不提供医疗诊断、处方或治疗建议。',
    '天气与模型数据可能因同步延迟或缺失而偏差，结果仅作行动提醒。',
    '系统不存精确住址/电话；慢病仅可选登记“类别”用于个性化提醒；备选联系人仅保存在本机。'
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
    if risk_label == '极高':
        return [
            {'id': 'stay_cool', 'title': '留在有降温条件的室内', 'detail': '尽量避免外出，保持室内通风降温。'},
            {'id': 'contact_now', 'title': '立即联系照护人/邻里', 'detail': '提前告知今日风险与行动安排。'},
            {'id': 'cooling_center', 'title': '条件不足时优先去避暑点', 'detail': '优先选择就近、开放的避暑场所。'}
        ]
    if risk_label == '高风险':
        return [
            {'id': 'stay_indoor', 'title': '尽量待在阴凉通风处', 'detail': '避开正午高温时段外出。'},
            {'id': 'hydrate', 'title': '少量多次补水', 'detail': '身边备好水或淡盐饮品。'},
            {'id': 'check_in', 'title': '安排每日确认', 'detail': '与家人/邻里保持联系。'}
        ]
    if risk_label == '中风险':
        return [
            {'id': 'avoid_sun', 'title': '减少连续暴晒', 'detail': '户外活动分段进行。'},
            {'id': 'cooling', 'title': '准备降温物品', 'detail': '风扇、湿毛巾或遮阳物品。'},
            {'id': 'watch_signs', 'title': '关注体感变化', 'detail': '感到不适及时休息。'}
        ]
    return [
        {'id': 'water', 'title': '规律补水', 'detail': '保持日常饮水习惯。'},
        {'id': 'ventilate', 'title': '室内通风', 'detail': '早晚开窗换气。'},
        {'id': 'shade', 'title': '适度遮阳', 'detail': '外出注意遮阳防晒。'}
    ]


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
        status='active',
        last_active_at=utcnow(),
        created_at=utcnow(),
    )
    db.session.add(pair)
    if flush:
        db.session.flush()
    return pair


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
