# -*- coding: utf-8 -*-
"""统一求助状态机。网页与小程序只做认证适配。服务内部不 commit。"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from core.db_models import (
    ActionEvent,
    ApiIdempotencyKey,
    DailyStatus,
    FamilyMember,
    HelpRequest,
    HelpRequestEvent,
    NotificationOutbox,
    Pair,
)
from core.extensions import db
from core.time_utils import today_local, utcnow
from services.family_access import (
    FAMILY_PRIMARY_ROLES,
    FamilyAccessError,
    ROLE_DOCTOR_SUPPORT,
    ROLE_OWNER,
    ROLE_VOLUNTEER,
    can_access_pair,
    ensure_space_for_pair,
    membership_role_for,
    require_pair_access,
    visible_pair_ids_for_user,
)
from services.notification_outbox import enqueue_help_notification

SCHEMA_VERSION = '2026-09-06.help-family-v2'
OPEN_STATUSES = ('pending_ack', 'acknowledged', 'in_progress')
TERMINAL_STATUSES = ('resolved', 'cancelled')
CATEGORIES = frozenset({'cannot_complete', 'need_checkin', 'need_cooling', 'other'})
RESOLUTION_DEFINITIONS = {
    'assisted': {'success': True, 'label': '已协助处理'},
    'transferred_confirmed': {'success': False, 'label': '已转交并确认接收'},
    'withdrawn': {'success': False, 'label': '本人撤回'},
    'unreachable': {'success': False, 'label': '联系不上'},
    'declined': {'success': False, 'label': '暂不受理'},
    'false_alarm': {'success': False, 'label': '误报'},
    'other': {'success': False, 'label': '其他结果'},
}
RESOLUTION_ALIASES = {
    'reached_elder': 'assisted',
    'action_done': 'assisted',
    'referred': 'transferred_confirmed',
}
RESOLUTION_CODES = frozenset(RESOLUTION_DEFINITIONS) | frozenset(RESOLUTION_ALIASES)
CANCEL_REASONS = frozenset({'misclick', 'duplicate', 'elder_ok', 'withdrawn', 'other'})
ORIGIN_CHANNELS = frozenset({'web', 'miniprogram', 'web_shortcode', 'elder_mode', 'device'})
SUPPORT_ROLES = frozenset({'doctor', 'volunteer'})

STATUS_LABELS = {
    'pending_ack': '等待接手',
    'acknowledged': '已接手',
    'in_progress': '处理中',
    'resolved': '已结束',
    'cancelled': '已取消',
}

EVENT_LABELS = {
    'created': '已发起求助',
    'remind': '再次提醒联系人',
    'acknowledged': '已接手',
    'started': '开始处理',
    'resolved': '已记录处理结果',
    'cancelled': '已取消',
    'support_requested': '已请求协助',
}

ACTIONS_BY_STATUS = {
    'pending_ack': ('ack', 'cancel'),
    'acknowledged': ('start', 'resolve', 'cancel', 'request_support'),
    'in_progress': ('resolve', 'cancel', 'request_support'),
    'resolved': (),
    'cancelled': (),
}


def normalize_resolution_code(code):
    code = (code or '').strip()
    return RESOLUTION_ALIASES.get(code, code)


def resolution_success(code):
    normalized = normalize_resolution_code(code)
    return bool(RESOLUTION_DEFINITIONS.get(normalized, {}).get('success'))


def status_label_for(help_row):
    if help_row.status == 'pending_ack':
        if help_row.requested_support_role == 'doctor':
            return '等待医生接手'
        if help_row.requested_support_role == 'volunteer':
            return '等待协助者接手'
        return '等待接手'
    if help_row.status == 'resolved':
        normalized = normalize_resolution_code(help_row.resolution_code)
        return RESOLUTION_DEFINITIONS.get(normalized, {}).get('label') or '已结束'
    return STATUS_LABELS.get(help_row.status, help_row.status)


def _help_actor_role(user, pair, help_row=None):
    role = membership_role_for(user, pair)
    if role == ROLE_DOCTOR_SUPPORT or getattr(user, 'role', None) == 'doctor':
        return 'doctor'
    if role == ROLE_VOLUNTEER:
        return 'volunteer'
    if role == ROLE_OWNER:
        return 'caregiver'
    return role or 'caregiver'


def _doctor_can_see(user, help_row):
    return (
        getattr(user, 'role', None) == 'doctor'
        and help_row is not None
        and help_row.requested_support_role == 'doctor'
    )


def sanitize_proxy_basis(value):
    text = (value or '').strip()
    return text[:120] if text else None


class HelpRequestError(Exception):
    def __init__(self, code, message, status_code=400, extra=None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


def capabilities():
    return {
        'schema_version': SCHEMA_VERSION,
        'api_contract': 'v1',
        'server_time': utcnow().isoformat(),
        'features': {
            'help_requests': True,
            'family_invites': True,
            'pending_open': True,
            'scripts': True,
            'notification_outbox': True,
            'doctor_content': True,
            'device_link': True,
            'not_emergency_channel': True,
        },
        'resolution_codes': RESOLUTION_DEFINITIONS,
        'disclaimer': '平台求助队列不是实时急救通道。无人接手时保持等待，不会假装已有人响应。',
    }


def _public_id():
    return secrets.token_hex(16)


def _hash_payload(payload):
    canonical = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _load_idempotency(scope, key):
    if not key:
        return None
    return ApiIdempotencyKey.query.filter_by(scope=scope, key=key).first()


def _store_idempotency(scope, key, request_hash, resource_type, public_id, response):
    if not key:
        return
    row = ApiIdempotencyKey(
        scope=scope,
        key=key,
        request_hash=request_hash,
        resource_type=resource_type,
        resource_public_id=public_id,
        response_json=json.dumps(response, ensure_ascii=False),
        created_at=utcnow(),
    )
    db.session.add(row)
    db.session.flush()


def _idempotency_scope(user):
    return f'user:{getattr(user, "id", "anon")}'


def _check_idempotency(user, key, payload):
    if not key:
        return None
    scope = _idempotency_scope(user)
    existing = _load_idempotency(scope, key)
    if not existing:
        return None
    request_hash = _hash_payload(payload)
    if existing.request_hash != request_hash:
        raise HelpRequestError('idempotency_mismatch', '同一请求编号不能改内容重试。', 409)
    return json.loads(existing.response_json or '{}')


def _open_for_pair(pair_id):
    return HelpRequest.query.filter(
        HelpRequest.pair_id == pair_id,
        HelpRequest.status.in_(OPEN_STATUSES),
    ).order_by(HelpRequest.id.desc()).first()


def open_help_for_pair(pair_id):
    return _open_for_pair(pair_id)


def apply_pair_help_stage(user, pair, stage, *, origin_channel='web', commit=True):
    """把旧的 pair events / 网页 action-log 接到同一求助状态机。"""
    open_row = _open_for_pair(pair.id if pair else None)
    if stage == 'help_acknowledged':
        if not open_row:
            raise HelpRequestError('not_found', '没有未结求助。', 404)
        return ack_help_request(
            user,
            open_row.public_id,
            expected_version=open_row.version,
            origin_channel=origin_channel,
            commit=commit,
        )
    if stage == 'closed':
        if not open_row:
            raise HelpRequestError('not_found', '没有未结求助。', 404)
        if open_row.status == 'pending_ack':
            raise HelpRequestError(
                'invalid_transition',
                '需要先接手，才能记录处理结果。收到求助不等于已经解决。',
                409,
                extra={'latest': serialize_help(open_row, user=user, pair=pair)},
            )
        return resolve_help_request(
            user,
            open_row.public_id,
            expected_version=open_row.version,
            resolution_code='assisted',
            origin_channel=origin_channel,
            commit=commit,
        )
    raise HelpRequestError('invalid_stage', '不支持的处理动作。', 400)


def _append_event(request_row, *, actor_user_id, actor_role, from_status, to_status, event_type, channel, meta=None):
    event = HelpRequestEvent(
        help_request_id=request_row.id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        from_status=from_status,
        to_status=to_status,
        event_type=event_type,
        channel=channel,
        meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
        created_at=utcnow(),
    )
    db.session.add(event)
    db.session.flush()
    return event


def _project_daily(pair, help_row):
    """DailyStatus 只做兼容投影，不再作为权威状态。"""
    local_date = today_local()
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=local_date).first()
    if status is None:
        status = DailyStatus(
            pair_id=pair.id,
            status_date=local_date,
            community_code=pair.community_code,
            help_flag=False,
            actions_done_count=0,
            relay_stage='none',
        )
        db.session.add(status)
    if help_row.status in OPEN_STATUSES:
        status.help_flag = True
        if help_row.status == 'pending_ack':
            status.help_acknowledged_at = None
            status.closed_at = None
        elif help_row.status in {'acknowledged', 'in_progress'}:
            if not status.help_acknowledged_at:
                status.help_acknowledged_at = help_row.acknowledged_at or utcnow()
            status.closed_at = None
    elif help_row.status == 'resolved':
        status.help_flag = True
        if not status.help_acknowledged_at:
            status.help_acknowledged_at = help_row.acknowledged_at
        status.closed_at = help_row.resolved_at or utcnow()
    elif help_row.status == 'cancelled':
        status.help_flag = False
        status.closed_at = help_row.cancelled_at or utcnow()
    db.session.flush()
    return status


def _link_action_event(pair, help_row, stage, actor_role, channel):
    from services.action_events import InvalidTransition, record_event

    try:
        event = record_event(
            pair,
            stage,
            actor_role,
            'miniprogram' if channel == 'miniprogram' else (
                'elder_mode' if channel == 'elder_mode' else 'web_shortcode' if channel == 'web_shortcode' else 'manual'
            ),
            commit=False,
            sync_help_request=False,
        )
    except InvalidTransition:
        return None
    if event is not None:
        event.help_request_id = help_row.id
        db.session.flush()
    return event


def serialize_help(help_row, *, include_actions=True, user=None, pair=None):
    actions = []
    if include_actions and user is not None and pair is not None:
        for name in ACTIONS_BY_STATUS.get(help_row.status, ()):
            if name == 'ack' and _can_ack(user, pair, help_row):
                actions.append('ack')
            elif name == 'start' and _can_ack(user, pair, help_row):
                actions.append('start')
            elif name == 'resolve' and _can_resolve(user, pair, help_row):
                actions.append('resolve')
            elif name == 'cancel' and can_access_pair(user, pair, 'cancel'):
                actions.append('cancel')
            elif name == 'request_support' and can_access_pair(user, pair, 'create_help'):
                actions.append('request_support')
    elder_label = ''
    if pair is not None and getattr(pair, 'member_id', None):
        member = db.session.get(FamilyMember, pair.member_id)
        if member:
            elder_label = member.relation or member.name or ''
    outbox_rows = NotificationOutbox.query.filter_by(help_request_id=help_row.id).all()
    notify_status = 'none'
    if outbox_rows:
        if any(row.status == 'dead' for row in outbox_rows):
            notify_status = 'needs_manual'
        elif any(row.status in {'pending', 'sending'} for row in outbox_rows):
            notify_status = 'queued'
        elif any(row.status == 'accepted' for row in outbox_rows):
            notify_status = 'accepted'
        else:
            notify_status = 'failed'
    normalized = normalize_resolution_code(help_row.resolution_code) if help_row.resolution_code else None
    return {
        'id': help_row.public_id,
        'pair_id': help_row.pair_id,
        'elder_label': elder_label,
        'status': help_row.status,
        'status_label': status_label_for(help_row),
        'version': help_row.version,
        'category': help_row.category,
        'origin_channel': help_row.origin_channel,
        'is_proxy': bool(help_row.is_proxy),
        'proxy_basis': help_row.proxy_basis,
        'actor_role': help_row.actor_role,
        'assignee_user_id': help_row.assignee_user_id,
        'requested_support_role': help_row.requested_support_role,
        'is_test': bool(help_row.is_test),
        'legacy_source': help_row.legacy_source,
        'created_at': help_row.created_at.isoformat() if help_row.created_at else None,
        'updated_at': help_row.updated_at.isoformat() if help_row.updated_at else None,
        'acknowledged_at': help_row.acknowledged_at.isoformat() if help_row.acknowledged_at else None,
        'started_at': help_row.started_at.isoformat() if help_row.started_at else None,
        'resolved_at': help_row.resolved_at.isoformat() if help_row.resolved_at else None,
        'resolution_code': normalized,
        'outcome_success': resolution_success(normalized) if normalized else None,
        'outcome_label': RESOLUTION_DEFINITIONS.get(normalized, {}).get('label') if normalized else None,
        'notification_status': notify_status,
        'allowed_actions': actions,
        'not_emergency_channel': True,
        'schema_version': SCHEMA_VERSION,
    }


def _can_ack(user, pair, help_row):
    if help_row.requested_support_role == 'doctor':
        if getattr(user, 'role', None) == 'doctor':
            return True
        return membership_role_for(user, pair) == ROLE_DOCTOR_SUPPORT
    if help_row.requested_support_role == 'volunteer':
        return membership_role_for(user, pair) == ROLE_VOLUNTEER
    if pair.caregiver_id == getattr(user, 'id', None):
        return True
    return membership_role_for(user, pair) in FAMILY_PRIMARY_ROLES


def _can_resolve(user, pair, help_row):
    if help_row.status == 'pending_ack':
        return False
    if help_row.assignee_user_id and help_row.assignee_user_id == getattr(user, 'id', None):
        return True
    if pair.caregiver_id == getattr(user, 'id', None):
        return True
    return can_access_pair(user, pair, 'resolve')


def create_help_request(
    user,
    pair,
    *,
    category='cannot_complete',
    origin_channel='miniprogram',
    idempotency_key=None,
    is_proxy=False,
    actor_role=None,
    actor_user_id=None,
    proxy_basis=None,
    commit=False,
    skip_access_check=False,
):
    if category not in CATEGORIES:
        raise HelpRequestError('invalid_category', '求助类别无效。', 400)
    if origin_channel not in ORIGIN_CHANNELS:
        raise HelpRequestError('invalid_channel', '来源通道无效。', 400)
    payload = {
        'pair_id': pair.id if pair else None,
        'category': category,
        'origin_channel': origin_channel,
    }
    cached = _check_idempotency(user, idempotency_key, payload)
    if cached is not None:
        return cached, False

    if not skip_access_check:
        require_pair_access(user, pair, 'create_help')
    if skip_access_check and actor_role == 'elder' and actor_user_id is None:
        resolved_actor = None
    else:
        resolved_actor = actor_user_id if actor_user_id is not None else getattr(user, 'id', None)
    space = ensure_space_for_pair(pair)
    existing = _open_for_pair(pair.id)
    now = utcnow()
    if existing:
        event = _append_event(
            existing,
            actor_user_id=resolved_actor,
            actor_role=actor_role or 'elder_proxy',
            from_status=existing.status,
            to_status=existing.status,
            event_type='remind',
            channel=origin_channel,
        )
        enqueue_help_notification(
            existing,
            event,
            recipient_user_id=pair.caregiver_id,
            event_type='remind',
            channel='in_app',
        )
        existing.updated_at = now
        db.session.flush()
        body = serialize_help(existing, user=user, pair=pair)
        body['replayed'] = True
        _store_idempotency(
            _idempotency_scope(user),
            idempotency_key,
            _hash_payload(payload),
            'help_request',
            existing.public_id,
            body,
        )
        if commit:
            db.session.commit()
        return body, False

    help_row = HelpRequest(
        public_id=_public_id(),
        family_space_id=space.id,
        pair_id=pair.id,
        status='pending_ack',
        origin_channel=origin_channel,
        actor_user_id=resolved_actor,
        actor_role=actor_role or ('elder_proxy' if is_proxy else 'elder'),
        is_proxy=bool(is_proxy),
        category=category,
        version=1,
        proxy_basis=sanitize_proxy_basis(proxy_basis) if is_proxy else None,
        is_test=bool(getattr(pair, 'is_test', False)),
        created_at=now,
        updated_at=now,
    )
    db.session.add(help_row)
    try:
        with db.session.begin_nested():
            db.session.flush()
    except IntegrityError as exc:
        raced = _open_for_pair(pair.id)
        if raced:
            body = serialize_help(raced, user=user, pair=pair)
            body['replayed'] = True
            return body, False
        raise HelpRequestError('conflict', '已有未结求助，请刷新后重试。', 409) from exc

    event = _append_event(
        help_row,
        actor_user_id=resolved_actor,
        actor_role=help_row.actor_role,
        from_status=None,
        to_status='pending_ack',
        event_type='created',
        channel=origin_channel,
    )
    _link_action_event(pair, help_row, 'help_requested', 'elder', origin_channel)
    _project_daily(pair, help_row)
    enqueue_help_notification(
        help_row,
        event,
        recipient_user_id=pair.caregiver_id,
        event_type='created',
        channel='in_app',
    )
    enqueue_help_notification(
        help_row,
        event,
        recipient_user_id=pair.caregiver_id,
        event_type='created',
        channel='wxpusher',
    )
    body = serialize_help(help_row, user=user, pair=pair)
    body['replayed'] = False
    _store_idempotency(
        _idempotency_scope(user),
        idempotency_key,
        _hash_payload(payload),
        'help_request',
        help_row.public_id,
        body,
    )
    if commit:
        db.session.commit()
    return body, True


def get_help_request(user, public_id):
    help_row = HelpRequest.query.filter_by(public_id=public_id).first()
    if not help_row:
        raise HelpRequestError('not_found', '求助不存在。', 404)
    pair = db.session.get(Pair, help_row.pair_id)
    if not _doctor_can_see(user, help_row):
        try:
            require_pair_access(user, pair, 'read')
        except FamilyAccessError as exc:
            raise HelpRequestError('not_found', '求助不存在。', 404) from exc
    events = (
        HelpRequestEvent.query.filter_by(help_request_id=help_row.id)
        .order_by(HelpRequestEvent.id.asc())
        .all()
    )
    body = serialize_help(help_row, user=user, pair=pair)
    body['events'] = [
        {
            'type': item.event_type,
            'type_label': EVENT_LABELS.get(item.event_type, '进度更新'),
            'from_status': item.from_status,
            'from_status_label': STATUS_LABELS.get(item.from_status) if item.from_status else None,
            'to_status': item.to_status,
            'to_status_label': STATUS_LABELS.get(item.to_status) if item.to_status else None,
            'channel': item.channel,
            'created_at': item.created_at.isoformat() if item.created_at else None,
        }
        for item in events
    ]
    return body


def list_help_requests(user, *, status='open', cursor=None, limit=20, requested_support_role=None):
    limit = max(1, min(int(limit or 20), 50))
    pair_ids = visible_pair_ids_for_user(user.id)
    if getattr(user, 'role', None) == 'doctor':
        doctor_ids = [
            row[0]
            for row in db.session.query(HelpRequest.pair_id).filter(
                HelpRequest.requested_support_role == 'doctor',
                HelpRequest.status.in_(OPEN_STATUSES),
            ).distinct().all()
        ]
        pair_ids = set(pair_ids) | set(doctor_ids)
    query = HelpRequest.query.filter(HelpRequest.pair_id.in_(pair_ids or [-1]))
    if getattr(user, 'role', None) == 'doctor' and not visible_pair_ids_for_user(user.id):
        query = query.filter(HelpRequest.requested_support_role == 'doctor')
    if requested_support_role:
        query = query.filter(HelpRequest.requested_support_role == requested_support_role)
    if status == 'open':
        query = query.filter(HelpRequest.status.in_(OPEN_STATUSES))
    elif status in OPEN_STATUSES + TERMINAL_STATUSES:
        query = query.filter_by(status=status)
    elif status not in {None, '', 'all'}:
        raise HelpRequestError('invalid_status', '状态筛选无效。', 400)
    if cursor:
        try:
            cursor_id = int(cursor)
        except (TypeError, ValueError) as exc:
            raise HelpRequestError('invalid_cursor', '分页游标无效。', 400) from exc
        query = query.filter(HelpRequest.id < cursor_id)
    rows = query.order_by(HelpRequest.updated_at.desc(), HelpRequest.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for help_row in rows:
        pair = db.session.get(Pair, help_row.pair_id)
        items.append(serialize_help(help_row, user=user, pair=pair))
    next_cursor = str(rows[-1].id) if has_more and rows else None
    count_query = HelpRequest.query.filter(
        HelpRequest.pair_id.in_(pair_ids or [-1]),
        HelpRequest.status.in_(OPEN_STATUSES),
    )
    pending_query = HelpRequest.query.filter(
        HelpRequest.pair_id.in_(pair_ids or [-1]),
        HelpRequest.status == 'pending_ack',
    )
    if requested_support_role:
        count_query = count_query.filter(HelpRequest.requested_support_role == requested_support_role)
        pending_query = pending_query.filter(HelpRequest.requested_support_role == requested_support_role)
    elif getattr(user, 'role', None) == 'doctor' and not visible_pair_ids_for_user(user.id):
        count_query = count_query.filter(HelpRequest.requested_support_role == 'doctor')
        pending_query = pending_query.filter(HelpRequest.requested_support_role == 'doctor')
    return {
        'schema_version': SCHEMA_VERSION,
        'items': items,
        'next_cursor': next_cursor,
        'open_count': count_query.count() if pair_ids else 0,
        'pending_ack_count': pending_query.count() if pair_ids else 0,
    }


def _load_for_write(user, public_id, action, expected_version):
    help_row = HelpRequest.query.filter_by(public_id=public_id).with_for_update().first()
    if not help_row:
        raise HelpRequestError('not_found', '求助不存在。', 404)
    pair = db.session.get(Pair, help_row.pair_id)
    allowed = False
    if action == 'ack':
        allowed = _can_ack(user, pair, help_row)
    elif action == 'resolve':
        if help_row.status == 'pending_ack':
            visible = (
                _can_ack(user, pair, help_row)
                or pair.caregiver_id == getattr(user, 'id', None)
                or can_access_pair(user, pair, 'read')
                or _doctor_can_see(user, help_row)
            )
            if visible:
                raise HelpRequestError(
                    'invalid_transition',
                    '需要先接手，才能记录处理结果。收到求助不等于已经解决。',
                    409,
                    extra={'latest': serialize_help(help_row, user=user, pair=pair)},
                )
        allowed = _can_resolve(user, pair, help_row)
    elif action == 'cancel':
        allowed = can_access_pair(user, pair, 'cancel')
    elif action == 'request_support':
        allowed = can_access_pair(user, pair, 'create_help')
    else:
        allowed = can_access_pair(user, pair, action)
    if not allowed:
        raise HelpRequestError('not_found', '求助不存在。', 404)
    if expected_version is not None and int(expected_version) != help_row.version:
        raise HelpRequestError(
            'version_conflict',
            '状态已更新，请先查看最新进度。',
            409,
            extra={'latest': serialize_help(help_row, user=user, pair=pair)},
        )
    return help_row, pair


def ack_help_request(user, public_id, *, expected_version, idempotency_key=None, origin_channel='web', commit=False):
    payload = {'id': public_id, 'op': 'ack', 'expected_version': expected_version}
    cached = _check_idempotency(user, idempotency_key, payload)
    if cached is not None:
        return cached
    help_row, pair = _load_for_write(user, public_id, 'ack', expected_version)
    if help_row.status != 'pending_ack':
        raise HelpRequestError(
            'invalid_transition',
            '当前不是待接收状态。',
            409,
            extra={'latest': serialize_help(help_row, user=user, pair=pair)},
        )
    now = utcnow()
    from_status = help_row.status
    help_row.status = 'acknowledged'
    help_row.acknowledged_by_user_id = user.id
    help_row.acknowledged_at = now
    help_row.assignee_user_id = user.id
    help_row.version += 1
    help_row.updated_at = now
    event = _append_event(
        help_row,
        actor_user_id=user.id,
        actor_role=_help_actor_role(user, pair, help_row),
        from_status=from_status,
        to_status='acknowledged',
        event_type='acknowledged',
        channel=origin_channel,
    )
    _link_action_event(pair, help_row, 'help_acknowledged', 'caregiver', origin_channel)
    _project_daily(pair, help_row)
    enqueue_help_notification(
        help_row,
        event,
        recipient_user_id=help_row.actor_user_id or pair.caregiver_id,
        event_type='acknowledged',
        channel='in_app',
    )
    body = serialize_help(help_row, user=user, pair=pair)
    _store_idempotency(
        _idempotency_scope(user),
        idempotency_key,
        _hash_payload(payload),
        'help_request',
        help_row.public_id,
        body,
    )
    if commit:
        db.session.commit()
    return body


def start_help_request(user, public_id, *, expected_version, idempotency_key=None, origin_channel='web', commit=False):
    payload = {'id': public_id, 'op': 'start', 'expected_version': expected_version}
    cached = _check_idempotency(user, idempotency_key, payload)
    if cached is not None:
        return cached
    help_row, pair = _load_for_write(user, public_id, 'ack', expected_version)
    if help_row.status not in {'acknowledged', 'in_progress'}:
        raise HelpRequestError('invalid_transition', '需要先接收求助再开始处理。', 409)
    now = utcnow()
    from_status = help_row.status
    if help_row.status != 'in_progress':
        help_row.status = 'in_progress'
        help_row.started_by_user_id = user.id
        help_row.started_at = now
        help_row.version += 1
        help_row.updated_at = now
        _append_event(
            help_row,
            actor_user_id=user.id,
            actor_role='caregiver',
            from_status=from_status,
            to_status='in_progress',
            event_type='started',
            channel=origin_channel,
        )
        _project_daily(pair, help_row)
    body = serialize_help(help_row, user=user, pair=pair)
    _store_idempotency(
        _idempotency_scope(user),
        idempotency_key,
        _hash_payload(payload),
        'help_request',
        help_row.public_id,
        body,
    )
    if commit:
        db.session.commit()
    return body


def resolve_help_request(
    user,
    public_id,
    *,
    expected_version,
    resolution_code,
    idempotency_key=None,
    origin_channel='web',
    commit=False,
):
    if resolution_code not in RESOLUTION_CODES:
        raise HelpRequestError('invalid_resolution', '结案必须选择具体结果，不同结果不会都记成成功解决。', 400)
    resolution_code = normalize_resolution_code(resolution_code)
    if resolution_code not in RESOLUTION_DEFINITIONS:
        raise HelpRequestError('invalid_resolution', '结案结果无效。', 400)
    payload = {
        'id': public_id,
        'op': 'resolve',
        'expected_version': expected_version,
        'resolution_code': resolution_code,
    }
    cached = _check_idempotency(user, idempotency_key, payload)
    if cached is not None:
        return cached
    help_row, pair = _load_for_write(user, public_id, 'resolve', expected_version)
    if help_row.status not in {'acknowledged', 'in_progress'}:
        raise HelpRequestError('invalid_transition', '当前不能结案。', 409)
    now = utcnow()
    from_status = help_row.status
    help_row.status = 'resolved'
    help_row.resolved_by_user_id = user.id
    help_row.resolved_at = now
    help_row.resolution_code = resolution_code
    help_row.version += 1
    help_row.updated_at = now
    event = _append_event(
        help_row,
        actor_user_id=user.id,
        actor_role=_help_actor_role(user, pair, help_row),
        from_status=from_status,
        to_status='resolved',
        event_type='resolved',
        channel=origin_channel,
        meta={'resolution_code': resolution_code, 'outcome_success': resolution_success(resolution_code)},
    )
    _link_action_event(pair, help_row, 'closed', 'caregiver', origin_channel)
    _project_daily(pair, help_row)
    enqueue_help_notification(
        help_row,
        event,
        recipient_user_id=help_row.actor_user_id or pair.caregiver_id,
        event_type='resolved',
        channel='in_app',
    )
    body = serialize_help(help_row, user=user, pair=pair)
    _store_idempotency(
        _idempotency_scope(user),
        idempotency_key,
        _hash_payload(payload),
        'help_request',
        help_row.public_id,
        body,
    )
    if commit:
        db.session.commit()
    return body


def cancel_help_request(
    user,
    public_id,
    *,
    expected_version,
    reason_code,
    idempotency_key=None,
    origin_channel='web',
    commit=False,
):
    if reason_code not in CANCEL_REASONS:
        raise HelpRequestError('invalid_cancel_reason', '取消原因无效。', 400)
    payload = {
        'id': public_id,
        'op': 'cancel',
        'expected_version': expected_version,
        'reason_code': reason_code,
    }
    cached = _check_idempotency(user, idempotency_key, payload)
    if cached is not None:
        return cached
    help_row, pair = _load_for_write(user, public_id, 'cancel', expected_version)
    if help_row.status in TERMINAL_STATUSES:
        raise HelpRequestError('invalid_transition', '该求助已经结束。', 409)
    now = utcnow()
    from_status = help_row.status
    help_row.status = 'cancelled'
    help_row.cancelled_by_user_id = user.id
    help_row.cancelled_at = now
    help_row.cancel_reason_code = reason_code
    help_row.version += 1
    help_row.updated_at = now
    _append_event(
        help_row,
        actor_user_id=user.id,
        actor_role='caregiver',
        from_status=from_status,
        to_status='cancelled',
        event_type='cancelled',
        channel=origin_channel,
        meta={'reason_code': reason_code},
    )
    _project_daily(pair, help_row)
    body = serialize_help(help_row, user=user, pair=pair)
    _store_idempotency(
        _idempotency_scope(user),
        idempotency_key,
        _hash_payload(payload),
        'help_request',
        help_row.public_id,
        body,
    )
    if commit:
        db.session.commit()
    return body


def request_support(user, public_id, *, support_role, expected_version, idempotency_key=None, origin_channel='web', commit=False):
    """家属提出协助后回到等待接手，对方明确接受前不转移跟进责任。"""
    support_role = (support_role or '').strip()
    if support_role not in SUPPORT_ROLES:
        raise HelpRequestError('invalid_support_role', '只能向医生或已授权协助者请求接手。')
    payload = {
        'id': public_id,
        'op': 'request_support',
        'expected_version': expected_version,
        'support_role': support_role,
    }
    cached = _check_idempotency(user, idempotency_key, payload)
    if cached is not None:
        return cached
    help_row, pair = _load_for_write(user, public_id, 'request_support', expected_version)
    if help_row.status not in {'acknowledged', 'in_progress'}:
        raise HelpRequestError('invalid_transition', '需要先由家属接手，再提出协助请求。', 409)
    now = utcnow()
    from_status = help_row.status
    help_row.status = 'pending_ack'
    help_row.requested_support_role = support_role
    help_row.assignee_user_id = None
    help_row.version += 1
    help_row.updated_at = now
    event = _append_event(
        help_row,
        actor_user_id=user.id,
        actor_role=_help_actor_role(user, pair, help_row),
        from_status=from_status,
        to_status='pending_ack',
        event_type='support_requested',
        channel=origin_channel,
        meta={'support_role': support_role},
    )
    _project_daily(pair, help_row)
    enqueue_help_notification(
        help_row,
        event,
        recipient_user_id=pair.caregiver_id,
        event_type='support_requested',
        channel='in_app',
    )
    body = serialize_help(help_row, user=user, pair=pair)
    _store_idempotency(
        _idempotency_scope(user),
        idempotency_key,
        _hash_payload(payload),
        'help_request',
        help_row.public_id,
        body,
    )
    if commit:
        db.session.commit()
    return body
