# -*- coding: utf-8 -*-
"""游客/匿名事件在个人配额之外，按受信 IP 计 UTC 日配额。"""
from datetime import timedelta

import pytest


def _user(username):
    from core.db_models import User

    return User(username=username, password_hash='unused')


def _tighten(app, daily=2, global_limit=1000, max_per_user=100):
    app.config['USAGE_EVENT_DAILY_PER_USER'] = daily
    app.config['USAGE_EVENT_GLOBAL_DAILY_LIMIT'] = global_limit
    app.config['USAGE_EVENT_MAX_PER_USER'] = max_per_user
    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'


def _post(app, ip, user_id, event_type='guest_quota', headers=None):
    from core.usage import log_usage_event

    with app.test_request_context(
        '/',
        headers=headers or {},
        environ_base={'REMOTE_ADDR': ip},
    ):
        return log_usage_event(event_type, user_id=user_id, enforce_quota=True)


def _count(db_session, event_type):
    from core.db_models import UsageEvent

    db_session.rollback()
    return UsageEvent.query.filter_by(event_type=event_type).count()


def test_guest_and_anonymous_share_one_ip_daily_quota(app, db_session):
    """同一 IP 上轮换 guest id 与匿名写入共用日配额，且不写入 user_id。"""
    from core.usage import UsageQuotaError

    _tighten(app, daily=2)
    first = _post(app, '203.0.113.10', 'guest:rotate-a', 'guest_share')
    second = _post(app, '203.0.113.10', None, 'guest_share')
    assert first is not None and first.user_id is None
    assert second is not None and second.user_id is None
    with pytest.raises(UsageQuotaError):
        _post(app, '203.0.113.10', 'guest:rotate-b', 'guest_share')
    assert _count(db_session, 'guest_share') == 2


def test_guest_daily_quota_follows_trusted_client_ip(app, db_session):
    """不同 IP 各有配额；伪造 XFF 不能换桶，受信代理则按客户端 IP。"""
    from core.usage import UsageQuotaError

    _tighten(app, daily=1)
    assert _post(app, '203.0.113.21', 'guest:ip-a', 'guest_ip') is not None
    with pytest.raises(UsageQuotaError):
        _post(
            app,
            '203.0.113.21',
            'guest:ip-a-spoof',
            'guest_ip',
            headers={'X-Forwarded-For': '198.51.100.8'},
        )
    assert _post(app, '203.0.113.22', 'guest:ip-b', 'guest_ip') is not None

    assert _post(
        app,
        '127.0.0.1',
        'guest:via-proxy',
        'guest_ip_proxy',
        headers={'X-Forwarded-For': '203.0.113.30'},
    ) is not None
    with pytest.raises(UsageQuotaError):
        _post(app, '203.0.113.30', 'guest:direct-same', 'guest_ip_proxy')
    assert _count(db_session, 'guest_ip') == 2
    assert _count(db_session, 'guest_ip_proxy') == 1


def test_site_daily_cap_still_limits_fresh_guest_ips(app, db_session):
    """按 IP 的日配额是额外限制，全站日上限仍然生效。"""
    from core.usage import UsageQuotaError

    _tighten(app, daily=10, global_limit=2)
    assert _post(app, '203.0.113.41', 'guest:global-a', 'guest_global') is not None
    assert _post(app, '203.0.113.42', 'guest:global-b', 'guest_global') is not None
    with pytest.raises(UsageQuotaError):
        _post(app, '203.0.113.43', 'guest:global-c', 'guest_global')
    assert _count(db_session, 'guest_global') == 2


def test_logged_in_quota_stays_per_user_and_ignores_ip_bucket(app, db_session):
    """正式用户仍按 user_id 计日配额，不占用、也不受游客 IP 桶影响。"""
    from core.usage import UsageQuotaError, log_usage_event

    _tighten(app, daily=2)
    user = _user('usage-ip-formal')
    db_session.add(user)
    db_session.commit()

    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.50'}):
        assert log_usage_event('user_quota', user_id=user.id, enforce_quota=True) is not None
        assert log_usage_event('user_quota', user_id=user.id, enforce_quota=True) is not None
        with pytest.raises(UsageQuotaError):
            log_usage_event('user_quota', user_id=user.id, enforce_quota=True)

    assert _post(app, '203.0.113.50', 'guest:beside-user', 'guest_beside') is not None
    assert _post(app, '203.0.113.50', 'guest:beside-user-2', 'guest_beside') is not None
    with pytest.raises(UsageQuotaError):
        _post(app, '203.0.113.50', 'guest:beside-user-3', 'guest_beside')

    db_session.rollback()
    from core.db_models import UsageEvent

    assert UsageEvent.query.filter_by(event_type='user_quota', user_id=user.id).count() == 2
    assert UsageEvent.query.filter_by(event_type='guest_beside', user_id=None).count() == 2


def test_guest_quota_resets_on_next_utc_day(app, db_session, monkeypatch):
    """IP 桶带 UTC 日期，跨日重新计算，而不是把历史次数一直算进去。"""
    from core.time_utils import utcnow
    from core.usage import UsageQuotaError

    _tighten(app, daily=1)

    def _yesterday():
        return (utcnow() - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

    monkeypatch.setattr('core.usage._utc_day_start', _yesterday)
    assert _post(app, '203.0.113.60', 'guest:yesterday', 'guest_day') is not None
    with pytest.raises(UsageQuotaError):
        _post(app, '203.0.113.60', 'guest:yesterday-2', 'guest_day')

    monkeypatch.undo()
    assert _post(app, '203.0.113.60', 'guest:today', 'guest_day') is not None
    assert _count(db_session, 'guest_day') == 2


def test_guest_enforce_without_ip_is_rejected(app, db_session):
    """enforce_quota 时解析不到 IP 则拒绝，不能退回只受全站日上限。"""
    from core.usage import UsageQuotaError, log_usage_event

    _tighten(app, daily=2)
    with pytest.raises(UsageQuotaError):
        log_usage_event('guest_no_ip', user_id='guest:no-request', enforce_quota=True)
    assert _count(db_session, 'guest_no_ip') == 0


def test_best_effort_guest_events_skip_ip_quota(app, db_session):
    """未开启 enforce_quota 的游客埋点保持原语义，不按 IP 日配额拒绝。"""
    from core.usage import log_usage_event

    _tighten(app, daily=1)
    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.80'}):
        first = log_usage_event('guest_best_effort', user_id='guest:best-a')
        second = log_usage_event('guest_best_effort', user_id='guest:best-b')
    assert first is not None and second is not None
    assert _count(db_session, 'guest_best_effort') == 2


def test_guest_quota_error_keeps_open_caller_transaction(app, db_session):
    """IP 配额拒绝不回滚调用方已经 flush 的业务和审计行。"""
    from core.db_models import AuditLog, Notification
    from core.usage import UsageQuotaError, log_usage_event

    _tighten(app, daily=1)
    user = _user('guest-quota-open')
    db_session.add(user)
    db_session.commit()
    note = Notification(user_id=user.id, title='half-finished')
    audit = AuditLog(action='half_done_business')
    db_session.add_all([note, audit])
    db_session.flush()
    note_id, audit_id = note.id, audit.id

    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.90'}):
        event = log_usage_event('guest_open', user_id='guest:open-a', enforce_quota=True)
        assert event is not None and event.user_id is None
        with pytest.raises(UsageQuotaError):
            log_usage_event('guest_open', user_id='guest:open-b', enforce_quota=True)

    assert db_session.get(Notification, note_id) is not None
    assert db_session.get(AuditLog, audit_id) is not None
    db_session.rollback()
    assert db_session.get(Notification, note_id) is None
    assert db_session.get(AuditLog, audit_id) is None
