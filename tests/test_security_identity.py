# -*- coding: utf-8 -*-
"""隔离数据库上的凭证生命周期与社区授权回归。"""
import pytest


def _login(client, user_id):
    from flask import g
    g.pop('_login_user', None)
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = 'identity-csrf'


def test_deleted_user_token_rejected_even_if_id_reused(app, client, db_session):
    from core.db_models import User
    from core.usage import create_api_token
    admin = User(username='identity-admin', role='admin', password_hash='unused')
    owner = User(username='identity-owner', role='user', password_hash='unused')
    db_session.add_all([admin, owner])
    db_session.commit()
    owner_id = owner.id
    plain = create_api_token(owner_id)
    headers = {'Authorization': f'Bearer {plain}'}
    assert client.get('/mp/api/v1/me', headers=headers).status_code == 200
    _login(client, admin.id)
    assert client.post(f'/admin/user/{owner_id}/delete', data={'csrf_token': 'identity-csrf'}).status_code == 302
    assert db_session.get(User, owner_id) is None
    assert client.get('/mp/api/v1/elders', headers=headers).status_code == 401
    db_session.add(User(id=owner_id, username='replacement', role='user', password_hash='unused'))
    db_session.commit()
    assert client.get('/mp/api/v1/me', headers=headers).status_code == 401


@pytest.mark.parametrize('role', ['guest', 'user', 'caregiver', 'community'])
@pytest.mark.parametrize('path', ['/api/community/risk-map-v2', '/api/v1/community/risk-map-v2', '/api/alert/comprehensive', '/api/v1/alert/comprehensive'])
def test_patient_aggregate_rejects_untrusted_role(app, client, db_session, monkeypatch, role, path):
    from core.db_models import User
    if role == 'guest':
        client.get('/guest')
        with client.session_transaction() as session:
            session['_csrf_token'] = 'identity-csrf'
    else:
        user = User(username='identity-' + role, role=role, password_hash='unused', community='都昌')
        db_session.add(user)
        db_session.commit()
        _login(client, user.id)
    def forbidden_service():
        pytest.fail('拒绝必须发生在病例查询、服务构造与缓存访问之前')
    monkeypatch.setattr('services.community_risk_service.get_community_service', forbidden_service)
    response = client.post(path, json={}, headers={'X-CSRF-Token': 'identity-csrf'})
    assert response.status_code == 403


def test_family_delete_preserves_nullable_operational_references(app, client, db_session):
    from core.db_models import User, FamilyMember, Notification, Pair, UsageEvent
    owner = User(username='family-owner', password_hash='unused')
    db_session.add(owner)
    db_session.commit()
    member = FamilyMember(user_id=owner.id, name='家庭成员')
    db_session.add(member)
    db_session.commit()
    member_id = member.id
    pair = Pair(caregiver_id=owner.id, member_id=member_id, community_code='村A', elder_code='family-elder', short_code='family-code')
    notification = Notification(user_id=owner.id, member_id=member_id, title='提醒')
    usage = UsageEvent(user_id=owner.id, member_id=member_id, event_type='page_view')
    db_session.add_all([pair, notification, usage])
    db_session.commit()
    _login(client, owner.id)
    response = client.post(f'/family-members/{member_id}/delete', data={'csrf_token': 'identity-csrf'})
    assert response.status_code == 302
    assert db_session.get(FamilyMember, member_id) is None
    for row in (pair, notification, usage):
        db_session.refresh(row)
        assert row.member_id is None


def test_token_expiry_missing_owner_and_null_expiry_fail_closed(app, client, db_session):
    from datetime import timedelta
    from core.db_models import ApiToken, User
    from core.time_utils import utcnow, ensure_utc_aware
    from core.usage import create_api_token, verify_api_token
    app.config['API_TOKEN_TTL_DAYS'] = 2
    owner = User(username='expiry-owner', password_hash='unused')
    db_session.add(owner)
    db_session.commit()
    plain = create_api_token(owner.id)
    record = verify_api_token(plain)
    assert record is not None
    assert timedelta(days=1) < ensure_utc_aware(record.expires_at) - utcnow() <= timedelta(days=2)
    record.expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert verify_api_token(plain) is None
    record.expires_at = None
    db_session.commit()
    assert verify_api_token(plain) is None
    with pytest.raises(ValueError):
        create_api_token(owner.id + 999)
    assert ApiToken.query.count() == 1


def test_old_token_cannot_authenticate_reused_legacy_user_id(app, client, db_session):
    from datetime import timedelta
    from core.db_models import User
    from core.time_utils import utcnow
    from core.usage import create_api_token, verify_api_token
    owner = User(username='legacy-reused-owner', password_hash='unused')
    db_session.add(owner)
    db_session.commit()
    plain = create_api_token(owner.id)
    record = verify_api_token(plain)
    record.created_at = utcnow() - timedelta(days=90)
    db_session.commit()
    assert verify_api_token(plain) is None


def test_sqlite_foreign_keys_reject_orphan_and_keep_business_dependencies(app, client, db_session):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from core.db_models import ApiToken, User, Pair
    from core.usage import create_api_token, verify_api_token
    assert db_session.execute(text('PRAGMA foreign_keys')).scalar() == 1
    db_session.add(ApiToken(user_id=999, token_hash='orphan'))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    admin = User(username='fk-admin', password_hash='unused', role='admin')
    owner = User(username='fk-owner', password_hash='unused')
    db_session.add_all([admin, owner])
    db_session.commit()
    plain = create_api_token(owner.id)
    db_session.add(Pair(caregiver_id=owner.id, community_code='村A', elder_code='fk-elder', short_code='fk-code'))
    db_session.commit()
    _login(client, admin.id)
    response = client.post(f'/admin/user/{owner.id}/delete', data={'csrf_token': 'identity-csrf'})
    assert response.status_code == 302
    assert db_session.get(User, owner.id) is not None
    assert Pair.query.count() == 1
    assert verify_api_token(plain) is not None  # 整个删号事务回滚，不能假称已删除


def test_user_lists_and_revokes_only_own_tokens(app, client, db_session):
    from core.db_models import ApiToken, User
    from core.usage import create_api_token, verify_api_token
    owner = User(username='revoke-owner', password_hash='unused')
    other = User(username='revoke-other', password_hash='unused')
    db_session.add_all([owner, other])
    db_session.commit()
    first = create_api_token(owner.id, name='我的手机')
    second = create_api_token(owner.id, name='备用设备')
    foreign = create_api_token(other.id, name='别人的设备')
    first_id = verify_api_token(first).id
    foreign_id = verify_api_token(foreign).id
    _login(client, owner.id)
    body = client.get('/profile').get_data(as_text=True)
    assert '我的手机' in body and '备用设备' in body and '别人的设备' not in body
    assert first not in body and second not in body
    client.post('/profile', data={'form_id': 'revoke_api_token', 'token_id': foreign_id, 'csrf_token': 'identity-csrf'})
    assert verify_api_token(foreign) is not None
    client.post('/profile', data={'form_id': 'revoke_api_token', 'token_id': first_id, 'csrf_token': 'identity-csrf'})
    assert verify_api_token(first) is None and verify_api_token(second) is not None
    client.post('/profile', data={'form_id': 'revoke_all_api_tokens', 'csrf_token': 'identity-csrf'})
    assert verify_api_token(second) is None and verify_api_token(foreign) is not None
    assert ApiToken.query.count() == 3


def test_password_change_and_admin_reset_revoke_tokens(app, client, db_session):
    from core.db_models import User
    from core.usage import create_api_token, verify_api_token
    owner = User(username='password_owner', age=30)
    owner.set_password('OldPass123!')
    admin = User(username='password-admin', password_hash='unused', role='admin')
    db_session.add_all([owner, admin])
    db_session.commit()
    plain = create_api_token(owner.id)
    _login(client, owner.id)
    client.post('/profile', data={'form_id': 'password', 'old_password': 'OldPass123!', 'new_password': 'NewPass456!', 'csrf_token': 'identity-csrf'})
    assert owner.check_password('NewPass456!')
    assert verify_api_token(plain) is None
    replacement = create_api_token(owner.id)
    _login(client, admin.id)
    client.post(f'/admin/user/{owner.id}/edit', data={'username': owner.username, 'age': '30', 'password': 'ResetPass789!', 'role': 'user', 'csrf_token': 'identity-csrf'})
    assert owner.check_password('ResetPass789!')
    assert verify_api_token(replacement) is None


def test_miniprogram_logout_revokes_current_token(app, client, db_session):
    from core.db_models import User
    from core.usage import create_api_token, verify_api_token
    owner = User(username='logout-owner', password_hash='unused')
    db_session.add(owner)
    db_session.commit()
    plain = create_api_token(owner.id)
    other = create_api_token(owner.id)
    headers = {'Authorization': f'Bearer {plain}'}
    assert client.post('/mp/api/v1/token/revoke', headers=headers).status_code == 200
    assert client.get('/mp/api/v1/me', headers=headers).status_code == 401
    assert verify_api_token(other) is not None


def test_location_and_profile_cannot_reassign_trusted_scope(app, client, db_session, monkeypatch):
    from core.db_models import Community, User
    from services.user._helpers import _community_access_allowed
    db_session.add_all([Community(name='村A'), Community(name='村B')])
    user = User(username='scope_owner', password_hash='unused', role='community', community='村A', authorized_community='村A')
    admin = User(username='scope-admin', password_hash='unused', role='admin')
    db_session.add_all([user, admin])
    db_session.commit()
    monkeypatch.setattr('services.user.profile_service.normalize_location_name', lambda value: value)
    _login(client, user.id)
    client.post('/profile', data={'community': '村B', 'authorized_community': '村B', 'csrf_token': 'identity-csrf'})
    assert user.community == '村B' and user.authorized_community == '村A'
    client.post('/location', data={'location': '村B', 'authorized_community': '村B', 'csrf_token': 'identity-csrf'})
    assert user.authorized_community == '村A'
    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        assert _community_access_allowed('村A')
        assert not _community_access_allowed('村B')
    assert client.get('/community/村B').status_code == 302
    assert client.get('/community/村B/wechat').status_code == 302
    assert client.get('/community/announce?community=村B').status_code == 302
    _login(client, admin.id)
    response = client.post(f'/admin/user/{user.id}/edit', data={'username': user.username, 'role': 'community', 'community': '村B', 'authorized_community': '村B', 'csrf_token': 'identity-csrf'})
    assert response.status_code == 302
    assert user.authorized_community == '村B'


def test_patient_query_and_cache_isolated_after_admin_warmup(app, client, db_session, monkeypatch):
    from datetime import datetime
    from sqlalchemy import event
    from core.db_models import Community, MedicalRecord, User
    from core.extensions import db
    from services.community_risk_service import CommunityRiskService
    from services.community_risk_cache import clear_local_community_risk_cache
    admin = User(username='scope-global', role='admin', password_hash='unused')
    a = User(username='scope-a', role='community', authorized_community='村A', password_hash='unused')
    b = User(username='scope-b', role='community', authorized_community='村B', password_hash='unused')
    db_session.add_all([admin, a, b, Community(name='村A', population=100), Community(name='村B', population=100)])
    for community, count in [('村A', 2), ('村B', 5)]:
        db_session.add_all([MedicalRecord(community=community, visit_time=datetime(2025, 10, 15), disease_category='呼吸') for _ in range(count)])
    db_session.commit()
    service = CommunityRiskService()
    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: service)
    clear_local_community_risk_cache()
    payload = {'analysis_date': '2025-10-30', 'window_days': 30, 'disease': '呼吸', 'weather': {'temperature': 30, 'humidity': 65, 'aqi': 40, 'data_source': 'QWeather', 'is_mock': False}}
    statements = []
    def record_sql(_conn, _cursor, statement, parameters, *_args):
        if 'FROM medical_records' in statement:
            statements.append((statement, parameters))
    event.listen(db.engine, 'before_cursor_execute', record_sql)
    try:
        for user, total in [(admin, 7), (a, 2), (b, 5)]:
            _login(client, user.id)
            response = client.post('/api/v1/community/risk-map-v2', json=payload, headers={'X-CSRF-Token': 'identity-csrf'})
            assert response.status_code == 200
            body = response.get_json()
            assert body['cache_hit'] is False
            assert sum(row['observed_cases'] for row in body['rankings']) == total
            if user.role == 'community':
                assert len(body['rankings']) == 1
                assert 'medical_records.community IN' in statements[-1][0]
                assert user.authorized_community in statements[-1][1]
            repeat = client.post('/api/community/risk-map-v2', json=payload, headers={'X-CSRF-Token': 'identity-csrf'})
            assert repeat.get_json()['cache_hit'] is True
    finally:
        event.remove(db.engine, 'before_cursor_execute', record_sql)
        clear_local_community_risk_cache()


def test_identity_migration_expires_legacy_without_promoting_location():
    import importlib.util
    from pathlib import Path
    from datetime import datetime, timedelta
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / 'migrations/versions/0011_security_identity.py'
    spec = importlib.util.spec_from_file_location('identity_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, community VARCHAR(100))'))
        connection.execute(sa.text('CREATE TABLE api_tokens (id INTEGER PRIMARY KEY, user_id INTEGER, revoked_at DATETIME)'))
        connection.execute(sa.text("INSERT INTO users VALUES (1, 'untrusted-self-selection')"))
        connection.execute(sa.text('INSERT INTO api_tokens VALUES (1, 1, NULL), (2, 999, NULL)'))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(sa.text('SELECT authorized_community FROM users')).scalar() is None
        rows = connection.execute(sa.text('SELECT user_id, expires_at FROM api_tokens')).all()
        assert len(rows) == 1 and rows[0][0] == 1
        expires = datetime.fromisoformat(rows[0][1]).replace(tzinfo=None)
        assert timedelta(days=6) < expires - datetime.now() < timedelta(days=8)
        migration.upgrade()
        assert connection.execute(sa.text('SELECT expires_at FROM api_tokens')).scalar() == rows[0][1]
    engine.dispose()


@pytest.mark.parametrize('role', ['guest', 'user', 'caregiver', 'community'])
def test_patient_page_rejects_before_medical_query(app, client, db_session, role):
    from core.db_models import User
    from core.extensions import db
    from sqlalchemy import event
    if role == 'guest':
        client.get('/guest')
    else:
        user = User(username='page-' + role, password_hash='unused', role=role)
        db_session.add(user)
        db_session.commit()
        _login(client, user.id)
    def reject_query(_conn, _cursor, statement, *_args):
        assert 'FROM medical_records' not in statement
    event.listen(db.engine, 'before_cursor_execute', reject_query)
    try:
        response = client.get('/community-risk')
        assert response.status_code == 403
        assert '此页面需要社区数据授权' in response.get_data(as_text=True)
        assert 'data-nav-key="community-risk"' not in response.get_data(as_text=True)
        assert client.get('/cooling').status_code == 200
    finally:
        event.remove(db.engine, 'before_cursor_execute', reject_query)


def test_patient_page_disease_options_are_scoped(app, client, db_session):
    from core.db_models import Community, MedicalRecord, User
    user = User(username='page-scoped', password_hash='unused', role='community', authorized_community='村A')
    db_session.add_all([user, Community(name='村A'), Community(name='村B'), MedicalRecord(community='村A', disease_category='本村类别'), MedicalRecord(community='村B', disease_category='外村秘密类别')])
    db_session.commit()
    _login(client, user.id)
    response = client.get('/community-risk')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '本村类别' in body and '外村秘密类别' not in body
    assert 'data-nav-key="community-risk"' in body


def test_guest_events_preserved_as_anonymous_with_foreign_keys(app, client, db_session):
    from core.db_models import UsageEvent
    client.get('/guest')
    with client.session_transaction() as session:
        session['_csrf_token'] = 'identity-csrf'
    assert client.get('/wxoa').status_code == 200
    assert client.post('/api/v1/events', json={'event_type': 'template_view'}, headers={'X-CSRF-Token': 'identity-csrf'}).status_code == 200
    rows = UsageEvent.query.order_by(UsageEvent.id).all()
    assert {row.event_type for row in rows} >= {'wxoa_land', 'template_view'}
    assert all(row.user_id is None for row in rows)
