# -*- coding: utf-8 -*-
"""避暑资源核验与反馈（PRD-03）。"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date as date_type
from datetime import timedelta
from types import SimpleNamespace

from core.db_models import CoolingFeedback, CoolingResource, Pair
from core.extensions import db
from core.guest import is_guest_user
from core.time_utils import ensure_utc_aware, today_local, utc_to_local_date, utcnow
from services.action_events import active_analysis_pair_ids

AMENITY_KEYS = ('ac', 'water', 'seats', 'toilet', 'step_free', 'shade')
FEEDBACK_CODES = frozenset({'reachable', 'need_ride', 'closed', 'not_found'})
VERIFY_METHODS = frozenset({'phone', 'onsite', 'official_doc'})
OPEN_DURING_ALERT_CODES = frozenset({'yes', 'no', 'unknown', 'conditional'})
ALERT_OPEN_NOTE_CODES = frozenset({
    'same_hours',
    'extended',
    'closed_on_alert',
    'staff_dependent',
})
TRANSPORT_CODES = frozenset({'walkable', 'bus', 'ride_needed', 'unknown'})
VERIFY_ROLES = frozenset({'student', 'community', 'admin'})
STATUS_ORDER = {
    'verified': 0,
    'stale': 1,
    'closed_reported': 2,
    'unverified': 3,
}
STALE_AFTER = timedelta(days=30)
VIABLE_OPEN_CODES = frozenset({'yes', 'conditional'})

METHOD_LABELS = {
    'phone': '电话',
    'onsite': '现场',
    'official_doc': '官方文件',
}
TRANSPORT_LABELS = {
    'walkable': '步行可达',
    'bus': '需公交',
    'ride_needed': '需接送',
    'unknown': '交通未知',
}
ALERT_OPEN_LABELS = {
    'yes': '高温预警日开放',
    'no': '高温预警日不开放',
    'unknown': '预警日开放情况未知',
    'conditional': '高温预警日视情况开放',
}
AMENITY_LABELS = {
    'ac': '空调',
    'water': '饮水',
    'seats': '座椅',
    'toilet': '厕所',
    'step_free': '无台阶',
    'shade': '遮阳',
}
AMENITY_ICONS = {
    'ac': 'bi-snow',
    'water': 'bi-droplet',
    'seats': 'bi-person-arms-up',
    'toilet': 'bi-badge-wc',
    'step_free': 'bi-universal-access',
    'shade': 'bi-tree',
}
STATUS_LABELS = {
    'verified': '已核验',
    'stale': '核验已超过 30 天',
    'unverified': '未核验',
    'closed_reported': '有用户反馈已关闭，待复核',
}


def parse_amenities(resource_or_payload):
    if isinstance(resource_or_payload, CoolingResource):
        raw = resource_or_payload.amenities_json
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = None
    else:
        payload = resource_or_payload
    result = {key: None for key in AMENITY_KEYS}
    if not isinstance(payload, dict):
        return result
    for key in AMENITY_KEYS:
        if key not in payload:
            continue
        result[key] = _coerce_tri_bool(payload.get(key))
    return result


def amenities_to_json(amenities):
    return json.dumps(parse_amenities(amenities), ensure_ascii=False)


def _coerce_tri_bool(value):
    if value is True or value is False or value is None:
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('yes', 'true', '1', 'on'):
            return True
        if normalized in ('no', 'false', '0', 'off'):
            return False
        return None
    return None


def _closed_pair_ids_after_verification(resource):
    """不同 pair 的 closed 反馈（晚于 last_verified_at；pair_id 为空不计）。"""
    verified_at = ensure_utc_aware(resource.last_verified_at)
    pair_ids = set()
    rows = CoolingFeedback.query.filter_by(
        resource_id=resource.id,
        code='closed',
    ).all()
    for row in rows:
        if not row.pair_id:
            continue
        created = ensure_utc_aware(row.created_at)
        if verified_at and created is not None and created <= verified_at:
            continue
        pair_ids.add(row.pair_id)
    return pair_ids


def compute_verify_status(resource, now=None):
    """verified / stale（>30 天）/ unverified / closed_reported。"""
    now = ensure_utc_aware(now) or utcnow()
    if len(_closed_pair_ids_after_verification(resource)) >= 2:
        return 'closed_reported'
    verified_at = ensure_utc_aware(resource.last_verified_at)
    if not verified_at:
        return 'unverified'
    if now - verified_at > STALE_AFTER:
        return 'stale'
    return 'verified'


def record_verification(
    resource,
    method,
    open_during_alert,
    alert_note_code,
    amenities,
    transport_need,
    by_role,
    now=None,
):
    """写入核验字段并重算 verify_status。不存姓名。"""
    if resource is None:
        raise ValueError('resource is required')
    method = (method or '').strip()
    if method not in VERIFY_METHODS:
        raise ValueError('invalid verify method')
    open_code = (open_during_alert or '').strip() or None
    if open_code and open_code not in OPEN_DURING_ALERT_CODES:
        raise ValueError('invalid open_during_alert')
    note_code = (alert_note_code or '').strip() or None
    if note_code and note_code not in ALERT_OPEN_NOTE_CODES:
        raise ValueError('invalid alert_open_note_code')
    transport = (transport_need or '').strip() or None
    if transport and transport not in TRANSPORT_CODES:
        raise ValueError('invalid transport_need')
    role = (by_role or '').strip()
    if role not in VERIFY_ROLES:
        raise ValueError('invalid verified_by_role')

    now = ensure_utc_aware(now) or utcnow()
    resource.last_verified_at = now
    resource.verified_by_role = role
    resource.verify_method = method
    resource.open_during_alert = open_code
    resource.alert_open_note_code = note_code
    resource.amenities_json = amenities_to_json(amenities)
    resource.transport_need = transport
    resource.verify_status = compute_verify_status(resource, now)
    db.session.add(resource)
    db.session.commit()
    return resource


def record_feedback(resource, code, pair=None, channel='web'):
    """追加一条封闭码反馈。≥2 条不同 pair 的 closed（晚于核验）→ closed_reported。"""
    if resource is None:
        raise ValueError('resource is required')
    code = (code or '').strip()
    if code not in FEEDBACK_CODES:
        raise ValueError('invalid feedback code')
    channel = (channel or 'web').strip()[:24] or 'web'
    pair_id = getattr(pair, 'id', None) if pair is not None else None
    feedback = CoolingFeedback(
        resource_id=resource.id,
        pair_id=pair_id,
        code=code,
        channel=channel,
        created_at=utcnow(),
    )
    db.session.add(feedback)
    db.session.flush()
    resource.verify_status = compute_verify_status(resource, utcnow())
    db.session.add(resource)
    db.session.commit()
    return feedback


def resolve_feedback_actor(current_user, session):
    """家属登录会话或老人短码/token 会话（pair_session_id）。游客不算登录。"""
    pair_id = session.get('pair_session_id')
    if pair_id:
        pair = db.session.get(Pair, pair_id)
        if pair is not None:
            return SimpleNamespace(ok=True, pair=pair, channel='web_shortcode')
    if getattr(current_user, 'is_authenticated', False) and not is_guest_user(current_user):
        pair = (
            Pair.query.filter_by(caregiver_id=current_user.id, status='active')
            .order_by(Pair.id.asc())
            .first()
        )
        return SimpleNamespace(ok=True, pair=pair, channel='web_caregiver')
    return SimpleNamespace(ok=False, pair=None, channel=None)


def _last_verified_label(resource):
    local_date = utc_to_local_date(resource.last_verified_at)
    if local_date is None:
        return None
    method = METHOD_LABELS.get(resource.verify_method or '', '')
    if method:
        return f'最后核验：{local_date.isoformat()} · {method}'
    return f'最后核验：{local_date.isoformat()}'


def present_cooling_cards(resources, now=None):
    now = ensure_utc_aware(now) or utcnow()
    cards = []
    for resource in resources:
        status = compute_verify_status(resource, now)
        amenities = parse_amenities(resource)
        cards.append(SimpleNamespace(
            resource=resource,
            id=resource.id,
            name=resource.name,
            resource_type=resource.resource_type,
            community_code=resource.community_code,
            address_hint=resource.address_hint,
            open_hours=resource.open_hours,
            has_ac=resource.has_ac,
            is_accessible=resource.is_accessible,
            contact_hint=resource.contact_hint,
            notes=resource.notes,
            latitude=resource.latitude,
            longitude=resource.longitude,
            verify_status=status,
            is_unverified=status == 'unverified',
            is_closed_reported=status == 'closed_reported',
            last_verified_label=_last_verified_label(resource),
            transport_need=resource.transport_need,
            transport_label=TRANSPORT_LABELS.get(resource.transport_need or ''),
            alert_open_label=ALERT_OPEN_LABELS.get(resource.open_during_alert or ''),
            amenities=amenities,
            amenity_items=[
                SimpleNamespace(
                    key=key,
                    label=AMENITY_LABELS[key],
                    icon=AMENITY_ICONS[key],
                    present=amenities.get(key) is True,
                )
                for key in AMENITY_KEYS
                if amenities.get(key) is True
            ],
        ))
    cards.sort(key=lambda card: (
        STATUS_ORDER.get(card.verify_status, 9),
        card.community_code or '',
        card.name or '',
        card.id or 0,
    ))
    return cards


def resource_gaps(date=None, include_test=False, now=None):
    """资源缺口清单。

    户可达比例 `households_with_one_viable_option_ratio` 使用
    `pairs.community_code`（不是 location_query）：与 CoolingResource.community_code
    做精确匹配。默认排除 is_test / qa_ 测试 pair（与行动链漏斗一致）。
    无 active pair 时该比例为 None。
    """
    as_of = date or today_local()
    now = ensure_utc_aware(now) or utcnow()
    resources = CoolingResource.query.filter_by(is_active=True).order_by(
        CoolingResource.id.asc()
    ).all()
    feedbacks = CoolingFeedback.query.all()
    closed_counts = defaultdict(int)
    need_ride_counts = defaultdict(int)
    for row in feedbacks:
        if row.code == 'closed':
            closed_counts[row.resource_id] += 1
        elif row.code == 'need_ride':
            need_ride_counts[row.resource_id] += 1

    status_counts = {
        'verified': 0,
        'stale': 0,
        'unverified': 0,
        'closed_reported': 0,
    }
    verified_within_7d = 0
    cutoff = as_of - timedelta(days=7)
    viable_communities = set()
    rows = []
    dirty = False
    for resource in resources:
        status = compute_verify_status(resource, now)
        if resource.verify_status != status:
            resource.verify_status = status
            dirty = True
        status_counts[status] = status_counts.get(status, 0) + 1
        last_date = utc_to_local_date(resource.last_verified_at)
        if last_date is not None and last_date >= cutoff:
            verified_within_7d += 1
        if status == 'verified' and resource.open_during_alert in VIABLE_OPEN_CODES:
            if resource.community_code:
                viable_communities.add(resource.community_code)
        rows.append({
            'id': resource.id,
            'type': resource.resource_type or '',
            'township': resource.community_code or '',
            'verify_status': status,
            'last_verified_at': (
                ensure_utc_aware(resource.last_verified_at).isoformat()
                if resource.last_verified_at else ''
            ),
            'open_during_alert': resource.open_during_alert or '',
            'transport_need': resource.transport_need or '',
            'closed_feedback_count': int(closed_counts.get(resource.id, 0)),
            'need_ride_feedback_count': int(need_ride_counts.get(resource.id, 0)),
        })
    if dirty:
        db.session.commit()

    total = len(resources)
    pair_ids = active_analysis_pair_ids(include_test=include_test)
    household_ratio = None
    if pair_ids:
        pairs = Pair.query.filter(Pair.id.in_(pair_ids)).all()
        if pairs:
            hits = sum(1 for pair in pairs if pair.community_code in viable_communities)
            household_ratio = hits / len(pairs)

    summary = {
        'unverified_count': status_counts['unverified'],
        'verified_count': status_counts['verified'],
        'stale_count': status_counts['stale'],
        'verified_within_7d_ratio': (verified_within_7d / total) if total else 0.0,
        'closed_reported_count': status_counts['closed_reported'],
        'need_ride_count': int(sum(need_ride_counts.values())),
        'households_with_one_viable_option_ratio': household_ratio,
        'as_of': as_of.isoformat() if isinstance(as_of, date_type) else str(as_of),
    }
    return {'rows': rows, 'summary': summary}


def amenity_csv_value(value):
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return ''


def township_for_resource(resource):
    """台账 township 取资源已有 community_code；没有则空，不写地址里的人名电话。"""
    return (getattr(resource, 'community_code', None) or '').strip()
