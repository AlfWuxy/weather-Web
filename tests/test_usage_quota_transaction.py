# -*- coding: utf-8 -*-
"""用量清理和配额写入不得提交或回滚调用方事务。"""
from datetime import timedelta

import pytest


def _user(username):
    from core.db_models import User

    return User(username=username, password_hash='unused')


def _flush_open_work(db_session, user):
    """放入已 flush、尚未 commit 的业务行和审计行。"""
    from core.db_models import AuditLog, Notification

    note = Notification(user_id=user.id, title='half-finished')
    audit = AuditLog(action='half_done_business')
    db_session.add_all([note, audit])
    db_session.flush()
    return note.id, audit.id


def _assert_open_work_survives(db_session, note_id, audit_id):
    from core.db_models import AuditLog, Notification

    assert db_session.get(Notification, note_id) is not None
    assert db_session.get(AuditLog, audit_id) is not None
    db_session.rollback()
    assert db_session.get(Notification, note_id) is None
    assert db_session.get(AuditLog, audit_id) is None


def test_idle_prune_uses_its_own_short_transaction(app, db_session):
    from core.db_models import UsageEvent
    from core.time_utils import utcnow
    from core.usage import prune_expired_usage_events

    user = _user('usage-idle-prune')
    db_session.add(user)
    db_session.commit()
    old = UsageEvent(
        user_id=user.id,
        event_type='stale',
        created_at=utcnow() - timedelta(days=120),
    )
    db_session.add(old)
    db_session.commit()
    old_id = old.id

    assert prune_expired_usage_events(commit=False) == 1
    db_session.rollback()
    assert db_session.get(UsageEvent, old_id) is None


def test_prune_does_not_commit_or_rollback_open_work(app, db_session):
    from core.db_models import ApiToken, UsageEvent
    from core.time_utils import utcnow
    from core.usage import prune_expired_api_tokens, prune_expired_usage_events

    user = _user('usage-open-prune')
    db_session.add(user)
    db_session.commit()
    past = utcnow() - timedelta(days=120)
    old_event = UsageEvent(user_id=user.id, event_type='stale', created_at=past)
    old_token = ApiToken(
        user_id=user.id,
        token_hash='expired-token-hash',
        created_at=past,
        expires_at=past,
    )
    db_session.add_all([old_event, old_token])
    db_session.commit()
    note_id, audit_id = _flush_open_work(db_session, user)

    assert prune_expired_usage_events(commit=True) >= 0
    assert prune_expired_api_tokens(user_id=user.id, commit=True) >= 0
    _assert_open_work_survives(db_session, note_id, audit_id)


def test_prune_failure_keeps_open_audit(app, db_session, monkeypatch):
    from core.usage import prune_expired_api_tokens

    user = _user('usage-prune-failure')
    db_session.add(user)
    db_session.commit()
    note_id, audit_id = _flush_open_work(db_session, user)

    def boom(*args, **kwargs):
        raise RuntimeError('prune failed')

    monkeypatch.setattr('core.usage._token_delete_statement', boom)
    assert prune_expired_api_tokens(user_id=user.id, commit=True) == 0
    _assert_open_work_survives(db_session, note_id, audit_id)


def test_log_usage_does_not_commit_open_work(app, db_session):
    from core.db_models import UsageEvent
    from core.usage import log_usage_event

    user = _user('usage-open-log')
    db_session.add(user)
    db_session.commit()
    note_id, audit_id = _flush_open_work(db_session, user)

    event = log_usage_event('open_transaction', user_id=user.id)
    event_id = event.id if event is not None else None
    _assert_open_work_survives(db_session, note_id, audit_id)
    if event_id is not None:
        assert db_session.get(UsageEvent, event_id) is None


def test_idle_usage_event_survives_session_rollback(app, db_session):
    from core.db_models import UsageEvent
    from core.usage import log_usage_event

    user = _user('usage-idle-log')
    db_session.add(user)
    db_session.commit()
    event = log_usage_event('idle_transaction', user_id=user.id)
    assert event is not None and event.id is not None
    event_id = event.id
    db_session.rollback()
    assert db_session.get(UsageEvent, event_id) is not None


def test_create_api_token_does_not_commit_open_work(app, db_session):
    from core.usage import create_api_token, verify_api_token

    user = _user('usage-open-token')
    db_session.add(user)
    db_session.commit()
    note_id, audit_id = _flush_open_work(db_session, user)

    plain = create_api_token(user.id, name='inside-request')
    assert plain
    _assert_open_work_survives(db_session, note_id, audit_id)
    assert verify_api_token(plain) is None


def test_idle_api_token_still_commits(app, db_session):
    from core.usage import create_api_token, verify_api_token

    user = _user('usage-idle-token')
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(user.id, name='idle')
    db_session.rollback()
    assert verify_api_token(plain) is not None


def test_token_quota_does_not_rollback_open_audit(app, db_session):
    from core.usage import UsageQuotaError, create_api_token

    app.config['API_TOKEN_MAX_ACTIVE'] = 1
    user = _user('usage-quota-audit')
    db_session.add(user)
    db_session.commit()
    assert create_api_token(user.id, name='first')
    note_id, audit_id = _flush_open_work(db_session, user)

    with pytest.raises(UsageQuotaError):
        create_api_token(user.id, name='second')
    _assert_open_work_survives(db_session, note_id, audit_id)


def _assert_count_and_insert_share_immediate_transaction(statements):
    """BEGIN IMMEDIATE 之后、下一次写锁之前，计数和插入在同一窗口。"""
    begin_at = None
    saw_count = False
    for statement in statements:
        compact = ' '.join(statement.upper().split())
        if 'BEGIN IMMEDIATE' in compact:
            begin_at = True
            saw_count = False
            continue
        if not begin_at:
            continue
        if 'FROM API_TOKENS' in compact and 'COUNT' in compact:
            saw_count = True
        if compact.startswith('INSERT INTO API_TOKENS'):
            assert saw_count, statements
            return
    raise AssertionError(statements)


def test_idle_token_quota_counts_and_inserts_in_one_immediate_transaction(app, db_session):
    from sqlalchemy import event

    from core.db_models import ApiToken
    from core.extensions import db
    from core.usage import UsageQuotaError, create_api_token

    app.config['API_TOKEN_MAX_ACTIVE'] = 2
    app.config['API_TOKEN_MAX_PER_USER'] = 20
    user = _user('usage-token-immediate')
    db_session.add(user)
    db_session.commit()
    statements = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', _record)
    try:
        assert create_api_token(user.id, name='one')
        _assert_count_and_insert_share_immediate_transaction(statements)
        assert create_api_token(user.id, name='two')
        with pytest.raises(UsageQuotaError):
            create_api_token(user.id, name='three')
    finally:
        event.remove(db.engine, 'before_cursor_execute', _record)

    db_session.rollback()
    assert ApiToken.query.filter_by(user_id=user.id).count() == 2


def test_open_transaction_rechecks_token_quota_inside_savepoint(app, db_session):
    from core.db_models import ApiToken
    from core.usage import UsageQuotaError, create_api_token, verify_api_token

    app.config['API_TOKEN_MAX_ACTIVE'] = 2
    user = _user('usage-token-savepoint')
    db_session.add(user)
    db_session.commit()
    note_id, audit_id = _flush_open_work(db_session, user)

    first = create_api_token(user.id, name='one')
    second = create_api_token(user.id, name='two')
    assert verify_api_token(first) is not None
    assert verify_api_token(second) is not None
    with pytest.raises(UsageQuotaError):
        create_api_token(user.id, name='three')
    assert ApiToken.query.filter_by(user_id=user.id).count() == 2
    _assert_open_work_survives(db_session, note_id, audit_id)
    assert verify_api_token(first) is None
    assert ApiToken.query.filter_by(user_id=user.id).count() == 0


def test_concurrent_token_creates_cannot_pass_active_quota(app, db_session):
    import threading

    from core.db_models import ApiToken
    from core.extensions import db
    from core.usage import UsageQuotaError, create_api_token

    app.config['API_TOKEN_MAX_ACTIVE'] = 1
    user = _user('usage-token-race')
    db_session.add(user)
    db_session.commit()
    user_id = user.id
    db.session.remove()

    barrier = threading.Barrier(2)
    outcomes = []
    outcome_lock = threading.Lock()

    def _attempt():
        with app.app_context():
            try:
                barrier.wait(5)
                try:
                    plain = create_api_token(user_id, name='race')
                    result = ('ok', bool(plain))
                except UsageQuotaError:
                    result = ('quota', None)
                except Exception as exc:
                    result = ('error', f'{type(exc).__name__}: {exc}')
            except Exception as exc:
                result = ('error', f'{type(exc).__name__}: {exc}')
            finally:
                db.session.remove()
            with outcome_lock:
                outcomes.append(result)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
        assert not thread.is_alive()

    assert sorted(kind for kind, _detail in outcomes) == ['ok', 'quota'], outcomes
    assert ApiToken.query.filter_by(user_id=user_id, revoked_at=None).count() == 1


def test_sqlite_token_lock_fails_closed_without_insert(app, db_session, monkeypatch):
    from core.db_models import ApiToken
    from core.usage import create_api_token

    user = _user('usage-token-lock-fail')
    db_session.add(user)
    db_session.commit()

    def _reject(_conn):
        raise RuntimeError('cannot start a transaction within a transaction')

    monkeypatch.setattr('core.usage._sqlite_immediate_lock', _reject)
    with pytest.raises(RuntimeError):
        create_api_token(user.id, name='blocked')
    db_session.rollback()
    assert ApiToken.query.filter_by(user_id=user.id).count() == 0
