# -*- coding: utf-8 -*-
"""求助通知 outbox：与工单同事务写入，失败重试，不丢求助。"""
from __future__ import annotations

import logging
from datetime import timedelta

from flask import current_app, has_app_context, url_for

from core.db_models import Notification, NotificationOutbox, User
from core.extensions import db
from core.time_utils import today_local, utcnow

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BACKOFF_SECONDS = (30, 60, 120, 300, 600, 1800, 3600, 7200)


def _dedupe_key(help_row, event_type, recipient_user_id, channel):
    if event_type == 'remind':
        day = str(today_local())
        return f'help:{help_row.public_id}:remind:{recipient_user_id}:{channel}:{day}'
    return f'help:{help_row.public_id}:{event_type}:{recipient_user_id}:{channel}'


def enqueue_help_notification(help_row, event, *, recipient_user_id, event_type, channel):
    if not recipient_user_id:
        return None
    key = _dedupe_key(help_row, event_type, recipient_user_id, channel)
    existing = NotificationOutbox.query.filter_by(dedupe_key=key).first()
    if existing:
        return existing
    now = utcnow()
    row = NotificationOutbox(
        help_request_id=help_row.id,
        help_event_id=getattr(event, 'id', None),
        recipient_user_id=recipient_user_id,
        channel=channel,
        event_type=event_type,
        dedupe_key=key,
        status='pending',
        attempt_count=0,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _detail_path(help_row):
    """受权详情入口，不含 Bearer、姓名或健康详情。"""
    try:
        return url_for('user.help_request_detail', public_id=help_row.public_id)
    except Exception:
        return f'/caregiver/help/{help_row.public_id}'


def _send_in_app(row, help_row):
    import json

    titles = {
        'created': '家人发出求助',
        'remind': '家人再次提醒求助',
        'acknowledged': '家属已收到求助',
        'resolved': '求助已标记解决',
    }
    messages = {
        'created': '请打开照护工作台确认收到。收到不等于老人已经安全。',
        'remind': '未结求助仍待接收或处理。',
        'acknowledged': '家属已确认收到，正在处理。',
        'resolved': '家属已记录处理结果。这不代表健康改善。',
    }
    notice = Notification(
        user_id=row.recipient_user_id,
        category='help_requested',
        title=titles.get(row.event_type, '求助更新'),
        message=messages.get(row.event_type, '求助状态有更新。'),
        level='warning' if row.event_type in {'created', 'remind'} else 'info',
        action_url=_detail_path(help_row),
        meta=json.dumps({
            'type': row.event_type,
            'help_id': help_row.public_id,
            'pair_id': help_row.pair_id,
        }, ensure_ascii=False),
        created_at=utcnow(),
    )
    db.session.add(notice)
    db.session.flush()


def _send_wxpusher(row, help_row):
    if has_app_context() and (
        current_app.config.get('TESTING')
        or current_app.config.get('HELP_NOTIFY_SANDBOX', True)
    ):
        # 本轮默认沙箱，不向真实用户发送
        return 'sandbox'
    caregiver = db.session.get(User, row.recipient_user_id)
    if not caregiver or not getattr(caregiver, 'push_enabled', False):
        return 'skipped_disabled'
    wx_uid = (getattr(caregiver, 'wxpusher_uid', None) or '').strip()
    if not wx_uid:
        return 'skipped_no_uid'
    from services.push.wxpusher import send as wxpusher_send

    result = wxpusher_send(
        wx_uid,
        title='家人发出求助' if row.event_type in {'created', 'remind'} else '求助有更新',
        content='请打开已登录的照护工作台查看。链接不含长期口令。',
        url=None,
    )
    if result.get('ok'):
        return 'accepted'
    raise RuntimeError(result.get('error') or 'wxpusher_failed')


def process_outbox_batch(limit=20, now=None):
    now = now or utcnow()
    rows = (
        NotificationOutbox.query.filter(
            NotificationOutbox.status.in_(['pending', 'failed']),
            (NotificationOutbox.next_attempt_at.is_(None))
            | (NotificationOutbox.next_attempt_at <= now),
        )
        .order_by(NotificationOutbox.id.asc())
        .limit(limit)
        .all()
    )
    processed = 0
    for row in rows:
        from core.db_models import HelpRequest

        help_row = db.session.get(HelpRequest, row.help_request_id) if row.help_request_id else None
        row.status = 'sending'
        row.attempt_count = (row.attempt_count or 0) + 1
        row.updated_at = now
        db.session.flush()
        try:
            if row.channel == 'in_app':
                if help_row:
                    _send_in_app(row, help_row)
                row.status = 'accepted'
                row.provider_accepted_at = utcnow()
                row.last_error_type = None
            elif row.channel == 'wxpusher':
                outcome = _send_wxpusher(row, help_row) if help_row else 'skipped'
                if outcome in {'sandbox', 'skipped', 'skipped_disabled', 'skipped_no_uid', 'accepted'}:
                    row.status = 'accepted'
                    row.provider_accepted_at = utcnow()
                    row.last_error_type = None if outcome == 'accepted' else outcome
                else:
                    raise RuntimeError(outcome)
            else:
                row.status = 'dead'
                row.last_error_type = 'unknown_channel'
            processed += 1
        except Exception as exc:
            row.last_error_type = type(exc).__name__
            if row.attempt_count >= MAX_ATTEMPTS:
                row.status = 'dead'
            else:
                row.status = 'failed'
                delay = BACKOFF_SECONDS[min(row.attempt_count - 1, len(BACKOFF_SECONDS) - 1)]
                row.next_attempt_at = utcnow() + timedelta(seconds=delay)
            logger.info('outbox send failed id=%s type=%s', row.id, row.last_error_type)
        row.updated_at = utcnow()
        db.session.flush()
    db.session.commit()
    return processed
