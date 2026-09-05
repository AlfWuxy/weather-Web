# -*- coding: utf-8 -*-
"""Append-only 老人行动链（ActionEvent）。"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from flask import current_app, has_app_context, jsonify
from sqlalchemy import func

from core.db_models import ActionEvent, CommunityDaily, DailyStatus, Pair, User
from core.extensions import db
from core.time_utils import ensure_utc_aware, today_local, utcnow
from utils.parsers import safe_json_loads

logger = logging.getLogger(__name__)

STAGES = (
    'delivered',
    'seen',
    'understood',
    'action_selected',
    'self_reported',
    'help_requested',
    'help_acknowledged',
    'caregiver_verified',
    'closed',
)

ALLOWED_ACTORS = {
    'delivered': frozenset({'system', 'caregiver'}),
    'seen': frozenset({'system'}),
    'understood': frozenset({'elder'}),
    'action_selected': frozenset({'elder'}),
    'self_reported': frozenset({'elder'}),
    'help_requested': frozenset({'elder'}),
    'help_acknowledged': frozenset({'caregiver', 'community'}),
    'caregiver_verified': frozenset({'caregiver'}),
    'closed': frozenset({'caregiver', 'community'}),
}

ALLOWED_CHANNELS = frozenset({
    'web_shortcode',
    'web_token',
    'elder_mode',
    'miniprogram',
    'wxpusher',
    'manual',
})

# 需要当日已存在的前驱 stage；None 表示允许无前驱。
REQUIRED_PREDECESSORS = {
    'delivered': None,
    'seen': None,
    'understood': frozenset({'seen'}),
    'action_selected': frozenset({'seen', 'understood'}),
    'self_reported': frozenset({'seen', 'understood', 'action_selected'}),
    'help_requested': frozenset({'seen', 'understood', 'action_selected'}),
    'help_acknowledged': frozenset({'help_requested'}),
    'caregiver_verified': frozenset({
        'delivered',
        'seen',
        'understood',
        'action_selected',
        'self_reported',
        'help_requested',
        'help_acknowledged',
        'caregiver_verified',
    }),
    'closed': frozenset({'caregiver_verified', 'help_acknowledged'}),
}

META_WHITELIST = frozenset({
    'teachback_action_id',
    'verified_without_self_report',
    'misclick_suspect',
})

IDEMPOTENCY_WINDOW = timedelta(seconds=60)
MISCLICK_WINDOW = timedelta(seconds=60)

FUNNEL_PRIMARY_STAGES = (
    'delivered',
    'seen',
    'understood',
    'action_selected',
    'self_reported',
    'caregiver_verified',
)
FUNNEL_HELP_STAGES = (
    'help_requested',
    'help_acknowledged',
    'closed',
)

LIMITATION_SENTENCE = '不代表老人安全或健康改善；未点击不等于未理解'


class InvalidTransition(Exception):
    """非法行动链转移。"""

    def __init__(self, from_stage, to_stage):
        self.from_stage = from_stage
        self.to_stage = to_stage
        super().__init__(f'invalid_transition:{from_stage}->{to_stage}')

    def to_response(self):
        return jsonify({
            'error': 'invalid_transition',
            'from': self.from_stage,
            'to': self.to_stage,
        }), 400


def _local_date_from(now):
    if now is None:
        return today_local()
    aware = ensure_utc_aware(now)
    if aware is None:
        return today_local()
    if has_app_context():
        from core.time_utils import utc_to_local_date
        local_date = utc_to_local_date(aware)
        return local_date or today_local()
    return aware.date()


def _sanitize_meta(meta):
    if not meta:
        return {}
    if not isinstance(meta, dict):
        return {}
    cleaned = {}
    if 'teachback_action_id' in meta and meta.get('teachback_action_id') is not None:
        cleaned['teachback_action_id'] = str(meta.get('teachback_action_id'))[:32]
    if 'verified_without_self_report' in meta:
        cleaned['verified_without_self_report'] = bool(meta.get('verified_without_self_report'))
    if 'misclick_suspect' in meta:
        cleaned['misclick_suspect'] = bool(meta.get('misclick_suspect'))
    return cleaned


def _event_meta(event):
    return safe_json_loads(event.meta_json, {}) if event and event.meta_json else {}


def _stages_present(pair_id, local_date):
    rows = db.session.query(ActionEvent.stage).filter(
        ActionEvent.pair_id == pair_id,
        ActionEvent.local_date == local_date,
    ).distinct()
    return {row[0] for row in rows}


def _latest_stage(pair_id, local_date):
    event = ActionEvent.query.filter_by(
        pair_id=pair_id,
        local_date=local_date,
    ).order_by(ActionEvent.created_at.desc(), ActionEvent.id.desc()).first()
    return event.stage if event else None


def _get_or_create_daily_status(pair, local_date):
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=local_date).first()
    if status:
        return status
    status = DailyStatus(
        pair_id=pair.id,
        status_date=local_date,
        community_code=pair.community_code,
        help_flag=False,
        actions_done_count=0,
        relay_stage='none',
    )
    db.session.add(status)
    return status


def _apply_daily_status(pair, status, stage, action_id, now):
    if stage == 'understood' and not status.understood_at:
        status.understood_at = now
    if stage == 'self_reported':
        if not status.confirmed_at:
            status.confirmed_at = now
        if action_id and action_id != 'undecided':
            done_ids = {
                row[0]
                for row in db.session.query(ActionEvent.action_id).filter(
                    ActionEvent.pair_id == pair.id,
                    ActionEvent.local_date == status.status_date,
                    ActionEvent.stage == 'self_reported',
                    ActionEvent.action_id.isnot(None),
                    ActionEvent.action_id != 'undecided',
                ).all()
            }
            done_ids.add(action_id)
            status.actions_done_count = len(done_ids)
    if stage == 'help_requested':
        status.help_flag = True
        if not status.relay_stage or status.relay_stage == 'none':
            status.relay_stage = 'caregiver'
    if stage == 'help_acknowledged' and not status.help_acknowledged_at:
        status.help_acknowledged_at = now
    if stage == 'caregiver_verified' and not status.verified_at:
        status.verified_at = now
    if stage == 'closed' and not status.closed_at:
        status.closed_at = now


def _find_idempotent(pair_id, local_date, stage, actor_role, action_id, now):
    window_start = now - IDEMPOTENCY_WINDOW
    query = ActionEvent.query.filter(
        ActionEvent.pair_id == pair_id,
        ActionEvent.local_date == local_date,
        ActionEvent.stage == stage,
        ActionEvent.actor_role == actor_role,
        ActionEvent.created_at >= window_start,
    )
    if action_id:
        query = query.filter(ActionEvent.action_id == action_id)
    else:
        query = query.filter(ActionEvent.action_id.is_(None))
    return query.order_by(ActionEvent.created_at.desc(), ActionEvent.id.desc()).first()


def record_event(
    pair,
    stage,
    actor_role,
    channel,
    *,
    action_id=None,
    script_version=None,
    alert_id=None,
    delivery_id=None,
    meta=None,
    now=None,
    commit=True,
    sync_help_request=True,
):
    """写入一条行动事件。非法转移抛出 InvalidTransition，不落库。

    commit=False 时只 flush，供外层与求助/通知同事务提交。
    sync_help_request=False 时不回写 HelpRequest，避免与求助服务互相递归。
    """
    if pair is None:
        raise InvalidTransition(None, stage)
    stage = str(stage or '').strip()
    actor_role = str(actor_role or '').strip()
    channel = str(channel or '').strip()
    if stage not in ALLOWED_ACTORS:
        raise InvalidTransition(None, stage)
    if actor_role not in ALLOWED_ACTORS[stage]:
        raise InvalidTransition(_latest_stage(pair.id, _local_date_from(now)), stage)
    if channel not in ALLOWED_CHANNELS:
        raise InvalidTransition(_latest_stage(pair.id, _local_date_from(now)), stage)

    now = ensure_utc_aware(now) or utcnow()
    local_date = _local_date_from(now)
    action_id = str(action_id).strip()[:32] if action_id else None
    script_version = str(script_version).strip()[:16] if script_version else None

    existing = _find_idempotent(pair.id, local_date, stage, actor_role, action_id, now)
    if existing:
        if sync_help_request:
            _sync_help_lifecycle(pair, stage, actor_role, channel, commit=False)
        return existing

    present = _stages_present(pair.id, local_date)
    required = REQUIRED_PREDECESSORS[stage]
    if required is not None and present.isdisjoint(required):
        raise InvalidTransition(_latest_stage(pair.id, local_date), stage)

    cleaned = _sanitize_meta(meta)
    if stage == 'caregiver_verified' and 'self_reported' not in present:
        cleaned['verified_without_self_report'] = True
    if stage in {'understood', 'self_reported'}:
        recent_help = ActionEvent.query.filter(
            ActionEvent.pair_id == pair.id,
            ActionEvent.local_date == local_date,
            ActionEvent.stage == 'help_requested',
            ActionEvent.created_at >= now - MISCLICK_WINDOW,
        ).first()
        if recent_help:
            cleaned['misclick_suspect'] = True
    if stage == 'action_selected' and action_id and 'teachback_action_id' not in cleaned:
        cleaned['teachback_action_id'] = action_id

    event = ActionEvent(
        pair_id=pair.id,
        local_date=local_date,
        stage=stage,
        actor_role=actor_role,
        channel=channel,
        script_version=script_version,
        action_id=action_id,
        alert_id=alert_id,
        delivery_id=delivery_id,
        meta_json=json.dumps(cleaned, ensure_ascii=False) if cleaned else None,
        created_at=now,
    )
    db.session.add(event)
    status = _get_or_create_daily_status(pair, local_date)
    _apply_daily_status(pair, status, stage, action_id, now)
    pair.last_active_at = now
    db.session.flush()
    if sync_help_request:
        _sync_help_lifecycle(pair, stage, actor_role, channel, commit=False)
    if commit:
        db.session.commit()
    return event


def _sync_help_lifecycle(pair, stage, actor_role, channel, *, commit=False):
    """把旧 ActionEvent 求助阶段接到同一 HelpRequest，不另开事务。"""
    if stage not in {'help_requested', 'help_acknowledged', 'closed'}:
        return
    from core.db_models import User
    from services.help_request_service import apply_pair_help_stage, create_help_request

    caregiver = db.session.get(User, pair.caregiver_id) if pair.caregiver_id else None
    origin = 'miniprogram' if channel == 'miniprogram' else (
        'elder_mode' if channel == 'elder_mode' else 'web_shortcode'
    )
    if stage == 'help_requested':
        create_help_request(
            caregiver,
            pair,
            origin_channel=origin,
            is_proxy=actor_role != 'elder',
            actor_role='elder' if actor_role == 'elder' else 'elder_proxy',
            skip_access_check=True,
            commit=commit,
        )
        return
    try:
        apply_pair_help_stage(
            caregiver,
            pair,
            stage,
            origin_channel=origin,
            commit=commit,
        )
    except Exception:
        logger.info('action_event 同步求助状态跳过 pair=%s stage=%s', getattr(pair, 'id', None), stage)


def today_state(pair, local_date=None):
    """返回当日各 stage 是否存在。"""
    local_date = local_date or today_local()
    state = {stage: False for stage in STAGES}
    if pair is None:
        state['unknown'] = True
        return state
    present = _stages_present(pair.id, local_date)
    for stage in STAGES:
        state[stage] = stage in present
    state['unknown'] = not bool(present)
    return state


def is_test_pair(pair, caregiver=None):
    if pair is None:
        return False
    if getattr(pair, 'is_test', False):
        return True
    elder_code = (getattr(pair, 'elder_code', None) or '').strip().lower()
    if elder_code.startswith('qa_'):
        return True
    user = caregiver
    if user is None and getattr(pair, 'caregiver_id', None):
        user = db.session.get(User, pair.caregiver_id)
    username = (getattr(user, 'username', None) or '').strip().lower()
    return username.startswith('qa_')


def _active_pair_query(include_test=False):
    query = Pair.query.filter(Pair.status == 'active')
    if include_test:
        return query
    query = query.outerjoin(User, User.id == Pair.caregiver_id).filter(
        Pair.is_test.is_(False),
        ~func.lower(func.coalesce(Pair.elder_code, '')).like('qa\\_%', escape='\\'),
        ~func.lower(func.coalesce(User.username, '')).like('qa\\_%', escape='\\'),
    )
    return query


def active_analysis_pair_ids(include_test=False):
    return {row[0] for row in _active_pair_query(include_test).with_entities(Pair.id).all()}


def pair_hash(pair_id):
    secret = ''
    if has_app_context():
        secret = current_app.config.get('SECRET_KEY') or ''
    payload = f'{secret}{pair_id}'.encode('utf-8')
    return hashlib.sha256(payload).hexdigest()[:12]


def _minutes_between(start, end):
    start = ensure_utc_aware(start)
    end = ensure_utc_aware(end)
    if not start or not end or end < start:
        return None
    return (end - start).total_seconds() / 60.0


def _median(values):
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return round(statistics.median(cleaned), 2)


def _first_event_times(events, stage):
    times = {}
    for event in events:
        if event.stage != stage:
            continue
        created = ensure_utc_aware(event.created_at)
        current = times.get(event.pair_id)
        if current is None or (created and created < current):
            times[event.pair_id] = created
    return times


def funnel(date_from, date_to, include_test=False):
    """按阶段去重 pair 数。分母 = 当前 active pairs。"""
    pair_ids = active_analysis_pair_ids(include_test=include_test)
    denominator = len(pair_ids)
    events = []
    if pair_ids:
        events = ActionEvent.query.filter(
            ActionEvent.pair_id.in_(pair_ids),
            ActionEvent.local_date >= date_from,
            ActionEvent.local_date <= date_to,
        ).all()

    pairs_by_stage = defaultdict(set)
    misclick_count = 0
    verified_without_self_report_pairs = set()
    complete_events = 0
    for event in events:
        pairs_by_stage[event.stage].add(event.pair_id)
        payload = _event_meta(event)
        if payload.get('misclick_suspect'):
            misclick_count += 1
        if event.stage == 'caregiver_verified' and payload.get('verified_without_self_report'):
            verified_without_self_report_pairs.add(event.pair_id)
        channel_ok = bool((event.channel or '').strip())
        script_ok = True
        if event.stage in {'delivered', 'seen'}:
            script_ok = bool((event.script_version or '').strip())
        if channel_ok and script_ok:
            complete_events += 1

    touched = set()
    for stage_pairs in pairs_by_stage.values():
        touched |= set(stage_pairs)
    unknown_count = len(pair_ids - touched)

    open_help_count = 0
    help_by_date = defaultdict(lambda: {'requested': set(), 'closed': set(), 'acked': set()})
    for event in events:
        bucket = help_by_date[event.local_date]
        if event.stage == 'help_requested':
            bucket['requested'].add(event.pair_id)
        elif event.stage == 'closed':
            bucket['closed'].add(event.pair_id)
        elif event.stage == 'help_acknowledged':
            bucket['acked'].add(event.pair_id)
    open_pairs = set()
    for bucket in help_by_date.values():
        open_pairs |= (bucket['requested'] - bucket['closed'])
    open_help_count = len(open_pairs)

    seen_times = _first_event_times(events, 'seen')
    self_times = _first_event_times(events, 'self_reported')
    help_times = _first_event_times(events, 'help_requested')
    ack_times = _first_event_times(events, 'help_acknowledged')
    verified_times = _first_event_times(events, 'caregiver_verified')
    closed_times = _first_event_times(events, 'closed')

    seen_to_self = [
        _minutes_between(seen_times[pair_id], self_times[pair_id])
        for pair_id in seen_times.keys() & self_times.keys()
    ]
    help_to_ack = [
        _minutes_between(help_times[pair_id], ack_times[pair_id])
        for pair_id in help_times.keys() & ack_times.keys()
    ]
    to_closed = []
    for pair_id, closed_at in closed_times.items():
        start = ack_times.get(pair_id) or verified_times.get(pair_id)
        to_closed.append(_minutes_between(start, closed_at))

    def _stage_row(stage):
        count = len(pairs_by_stage.get(stage, set()))
        rate = round(count / denominator, 4) if denominator else 0.0
        return {
            'stage': stage,
            'pairs': count,
            'denominator': denominator,
            'rate': rate,
        }

    event_total = len(events)
    completeness = round(complete_events / event_total, 4) if event_total else 0.0

    return {
        'date_from': date_from,
        'date_to': date_to,
        'denominator': denominator,
        'unknown_count': unknown_count,
        'stages': {stage: _stage_row(stage) for stage in STAGES},
        'primary': [_stage_row(stage) for stage in FUNNEL_PRIMARY_STAGES],
        'help': [_stage_row(stage) for stage in FUNNEL_HELP_STAGES],
        'median_seen_to_self_reported_minutes': _median(seen_to_self),
        'median_help_requested_to_ack_minutes': _median(help_to_ack),
        'median_to_closed_minutes': _median(to_closed),
        'open_help_count': open_help_count,
        'misclick_count': misclick_count,
        'verified_without_self_report_count': len(verified_without_self_report_pairs),
        'event_source_completeness': completeness,
        'event_count': event_total,
    }


def fill_community_daily_action_columns(record, active_pair_ids, status_date, statuses):
    """写入 CommunityDaily 新增行动链指标；confirm_rate = self_report_rate。"""
    total = len(active_pair_ids)
    understood = sum(1 for status in statuses if getattr(status, 'understood_at', None))
    self_report = sum(1 for status in statuses if status.confirmed_at)
    verified = sum(1 for status in statuses if getattr(status, 'verified_at', None))
    open_help = sum(
        1
        for status in statuses
        if status.help_flag and not getattr(status, 'closed_at', None)
    )
    touched_ids = set()
    if active_pair_ids:
        touched_ids = {
            row[0]
            for row in db.session.query(ActionEvent.pair_id).filter(
                ActionEvent.pair_id.in_(active_pair_ids),
                ActionEvent.local_date == status_date,
            ).distinct()
        }
    unknown = max(total - len(touched_ids), 0)
    self_report_rate = round((self_report / total), 4) if total else 0.0
    record.understood_rate = round((understood / total), 4) if total else 0.0
    record.self_report_rate = self_report_rate
    record.verified_rate = round((verified / total), 4) if total else 0.0
    record.open_help_count = open_help
    record.unknown_count = unknown
    record.confirm_rate = self_report_rate
    return record


def record_seen(pair, channel, *, now=None):
    """页面渲染成功时写入 seen；失败不阻断页面。"""
    if pair is None:
        return None
    try:
        return record_event(pair, 'seen', 'system', channel, now=now)
    except InvalidTransition:
        return None
    except Exception:
        logger.debug('record seen failed', exc_info=True)
        db.session.rollback()
        return None
