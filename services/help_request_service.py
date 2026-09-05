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
    FamilyAccessError,
    can_access_pair,
    ensure_space_for_pair,
    require_pair_access,
    visible_pair_ids_for_user,
)
from services.notification_outbox import enqueue_help_notification

SCHEMA_VERSION = '2026-09-06.help-family-v1'
OPEN_STATUSES = ('pending_ack', 'acknowledged', 'in_progress')
TERMINAL_STATUSES = ('resolved', 'cancelled')
CATEGORIES = frozenset({'cannot_complete', 'need_checkin', 'need_cooling', 'other'})
RESOLUTION_CODES = frozenset({'reached_elder', 'action_done', 'referred', 'false_alarm', 'other'})
CANCEL_REASONS = frozenset({'misclick', 'duplicate', 'elder_ok', 'other'})
ORIGIN_CHANNELS = frozenset({'web', 'miniprogram', 'web_shortcode', 'elder_mode'})

STATUS_LABELS = {
    'pending_ack': '待家属接收',
    'acknowledged': '家属已收到，待处理',
    'in_progress': '处理中',
    'resolved': '已解决',
    'cancelled': '已取消',
}

EVENT_LABELS = {
    'created': '已发起求助',
    'remind': '再次提醒家属',
    'acknowledged': '家属已收到',
    'started': '开始处理',
    'resolved': '已标记解决',
    'cancelled': '已取消',
}

ACTIONS_BY_STATUS = {
    'pending_ack': ('ack', 'cancel'),
    'acknowledged': ('start', 'resolve', 'cancel'),
    'in_progress': ('resolve', 'cancel'),
    'resolved': (),
    'cancelled': (),
}


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
        },
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
            ack_help_request(
                user,
                open_row.public_id,
                expected_version=open_row.version,
                origin_channel=origin_channel,
                commit=False,
            )
            open_row = _open_for_pair(pair.id)
        if open_row is None:
            raise HelpRequestError('not_found', '没有未结求助。', 404)
        return resolve_help_request(
            user,
            open_row.public_id,
            expected_version=open_row.version,
            resolution_code='reached_elder',
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
            if name == 'ack' and can_access_pair(user, pair, 'ack'):
                actions.append('ack')
            elif name == 'start' and can_access_pair(user, pair, 'ack'):
                actions.append('start')
            elif name == 'resolve' and can_access_pair(user, pair, 'resolve'):
                actions.append('resolve')
            elif name == 'cancel' and can_access_pair(user, pair, 'cancel'):
                actions.append('cancel')
    elder_label = ''
    if pair is not None and getattr(pair, 'member_id', None):
        member = db.session.get(FamilyMember, pair.member_id)
        if member:
            elder_label = member.name or member.relation or ''
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
    return {
        'id': help_row.public_id,
        'pair_id': help_row.pair_id,
        'elder_label': elder_label,
        'status': help_row.status,
        'status_label': STATUS_LABELS.get(help_row.status, help_row.status),
        'version': help_row.version,
        'category': help_row.category,
        'origin_channel': help_row.origin_channel,
        'is_proxy': bool(help_row.is_proxy),
        'is_test': bool(help_row.is_test),
        'legacy_source': help_row.legacy_source,
        'created_at': help_row.created_at.isoformat() if help_row.created_at else None,
        'updated_at': help_row.updated_at.isoformat() if help_row.updated_at else None,
        'acknowledged_at': help_row.acknowledged_at.isoformat() if help_row.acknowledged_at else None,
        'started_at': help_row.started_at.isoformat() if help_row.started_at else None,
        'resolved_at': help_row.resolved_at.isoformat() if help_row.resolved_at else None,
        'resolution_code': help_row.resolution_code,
        'notification_status': notify_status,
        'allowed_actions': actions,
        'schema_version': SCHEMA_VERSION,
    }


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


def list_help_requests(user, *, status='open', cursor=None, limit=20):
    limit = max(1, min(int(limit or 20), 50))
    pair_ids = visible_pair_ids_for_user(user.id)
    query = HelpRequest.query.filter(HelpRequest.pair_id.in_(pair_ids or [-1]))
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
    return {
        'schema_version': SCHEMA_VERSION,
        'items': items,
        'next_cursor': next_cursor,
        'open_count': HelpRequest.query.filter(
            HelpRequest.pair_id.in_(pair_ids or [-1]),
            HelpRequest.status.in_(OPEN_STATUSES),
        ).count() if pair_ids else 0,
        'pending_ack_count': HelpRequest.query.filter(
            HelpRequest.pair_id.in_(pair_ids or [-1]),
            HelpRequest.status == 'pending_ack',
        ).count() if pair_ids else 0,
    }


def _load_for_write(user, public_id, action, expected_version):
    help_row = HelpRequest.query.filter_by(public_id=public_id).with_for_update().first()
    if not help_row:
        raise HelpRequestError('not_found', '求助不存在。', 404)
    pair = db.session.get(Pair, help_row.pair_id)
    require_pair_access(user, pair, action)
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
    help_row.version += 1
    help_row.updated_at = now
    event = _append_event(
        help_row,
        actor_user_id=user.id,
        actor_role='caregiver',
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
        actor_role='caregiver',
        from_status=from_status,
        to_status='resolved',
        event_type='resolved',
        channel=origin_channel,
        meta={'resolution_code': resolution_code},
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
