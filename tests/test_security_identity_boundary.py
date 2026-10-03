# -*- coding: utf-8 -*-
"""生产版本凭证管理与患者派生数据访问边界。"""
import pytest


def _user(db_session, name, role='user', authorized=None):
    from core.db_models import User
    user = User(username=name, password_hash='unused', role=role, authorized_community=authorized)
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, user):
    from flask import g
    g.pop('_login_user', None)
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'identity-boundary-csrf'


@pytest.mark.parametrize('role', ['guest', 'user', 'caregiver', 'community'])
@pytest.mark.parametrize('path', ['/api/community/risk-map-v2', '/api/v1/community/risk-map-v2', '/api/alert/comprehensive', '/api/v1/alert/comprehensive'])
def test_patient_api_denies_untrusted_roles_before_query(app, client, db_session, monkeypatch, role, path):
    if role == 'guest':
        client.get('/guest')
        with client.session_transaction() as session:
            session['_csrf_token'] = 'identity-boundary-csrf'
    else:
        _login(client, _user(db_session, 'api-' + role, role))
    def deny_service():
        pytest.fail('未经授权不能进入患者服务、查询或缓存')
    monkeypatch.setattr('services.community_risk_service.get_community_service', deny_service)
    response = client.post(path, json={}, headers={'X-CSRF-Token': 'identity-boundary-csrf'})
    assert response.status_code == 403


@pytest.mark.parametrize('role', ['user', 'caregiver', 'community'])
def test_patient_page_denies_unassigned_users(client, db_session, role):
    from sqlalchemy import event
    from core.extensions import db
    _login(client, _user(db_session, 'page-' + role, role))
    queries = []
    def record_query(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement.lower())
    event.listen(db.engine, 'before_cursor_execute', record_query)
    try:
        response = client.get('/community-risk')
    finally:
        event.remove(db.engine, 'before_cursor_execute', record_query)
    assert response.status_code == 403
    assert '需要社区数据授权' in response.get_data(as_text=True)
    assert not any('medical_records' in query for query in queries)


def test_patient_html_and_sql_respect_assigned_scope(app, client, db_session):
    from datetime import date, datetime
    from core.db_models import Community, MedicalRecord
    from services.community_risk_service import CommunityRiskService
    user = _user(db_session, 'assigned-page', 'community', '村A')
    db_session.add_all([Community(name='村A'), Community(name='村B'),
        MedicalRecord(community='村A', disease_category='本村类别', visit_time=datetime(2025, 10, 15)),
        MedicalRecord(community='村B', disease_category='外村秘密类别', visit_time=datetime(2025, 10, 15))])
    db_session.commit()
    _login(client, user)
    response = client.get('/community-risk')
    assert response.status_code == 200
    assert '本村类别' in response.get_data(as_text=True)
    assert '外村秘密类别' not in response.get_data(as_text=True)
    service = CommunityRiskService()
    counts = service._collect_medical_counts(date(2025, 10, 30), 30, community_scope=('村A',))
    assert counts['total_records'] == 1
    assert counts['counts_by_community'] == {'村A': 1}


def test_token_management_is_owner_scoped(client, db_session):
    from core.usage import create_api_token, verify_api_token
    user = _user(db_session, 'token-owner')
    other = _user(db_session, 'token-other')
    one = create_api_token(user.id, name='我的旧设备')
    two = create_api_token(user.id, name='备用设备')
    foreign = create_api_token(other.id, name='别人旧设备')
    one_id, foreign_id = verify_api_token(one).id, verify_api_token(foreign).id
    _login(client, user)
    body = client.get('/profile').get_data(as_text=True)
    assert '我的旧设备' in body and '备用设备' in body and '别人旧设备' not in body
    assert one not in body and two not in body
    for token_id in (foreign_id, one_id):
        assert client.post('/profile', data={'form_id': 'revoke_api_token', 'token_id': token_id, 'csrf_token': 'identity-boundary-csrf'}).status_code == 302
    assert verify_api_token(one) is None
    assert verify_api_token(two) is not None and verify_api_token(foreign) is not None
    client.post('/profile', data={'form_id': 'revoke_all_api_tokens', 'csrf_token': 'identity-boundary-csrf'})
    assert verify_api_token(two) is None and verify_api_token(foreign) is not None


def test_admin_delete_token_only_user_removes_bearer_even_if_id_reused(client, db_session):
    from core.db_models import User
    from core.usage import create_api_token, verify_api_token
    admin = _user(db_session, 'delete-admin', 'admin')
    user = _user(db_session, 'delete-owner')
    user_id = user.id
    plain = create_api_token(user_id)
    assert verify_api_token(plain) is not None
    _login(client, admin)
    response = client.post(f'/admin/user/{user_id}/delete', data={'csrf_token': 'identity-boundary-csrf'})
    assert response.status_code == 302
    assert db_session.get(User, user_id) is None
    db_session.add(User(id=user_id, username='replacement', password_hash='unused'))
    db_session.commit()
    assert verify_api_token(plain) is None


@pytest.mark.parametrize('stale_consent', [False, True])
def test_legacy_read_only_token_can_revoke_only_itself(client, db_session, stale_consent):
    from core.usage import create_api_token, verify_api_token
    user = _user(db_session, 'legacy-logout')
    plain = create_api_token(user.id, scopes=['miniprogram:read'])
    other = create_api_token(user.id)
    if stale_consent:
        verify_api_token(plain).privacy_consent_version = 'outdated-version'
        db_session.commit()
    headers = {'Authorization': f'Bearer {plain}'}
    assert client.post('/mp/api/v1/events', json={}, headers=headers).status_code in (403, 428)
    response = client.post('/mp/api/v1/auth/logout', headers=headers)
    assert response.status_code == 200
    assert response.get_json()['data']['revoked'] is True
    assert verify_api_token(plain) is None
    assert verify_api_token(other) is not None
    assert client.get('/mp/api/v1/me', headers=headers).status_code == 401


def test_patient_api_scope_cache_does_not_reuse_admin_result(app, client, db_session, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache
    calls = []
    class FakeService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None, community_scope=None):
            calls.append(community_scope)
            return {'rankings': [{'scope': '*' if community_scope is None else community_scope[0]}]}
    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: FakeService())
    monkeypatch.setattr('services.api_service.get_weather_with_cache', lambda _city: ({'temperature': 25}, True))
    monkeypatch.setattr('services.api_service.is_qweather_production_ready', lambda _weather: True)
    monkeypatch.setattr('services.api_service.normalize_health_model_weather', lambda weather: weather)
    users = [_user(db_session, 'cache-admin', 'admin'), _user(db_session, 'cache-a', 'community', '村A'), _user(db_session, 'cache-b', 'community', '村B')]
    clear_local_community_risk_cache()
    try:
        for user, expected in zip(users, ['*', '村A', '村B']):
            _login(client, user)
            payload = {'city': '都昌', 'analysis_date': '2025-10-30', 'disease': '呼吸'}
            first = client.post('/api/community/risk-map-v2', json=payload, headers={'X-CSRF-Token': 'identity-boundary-csrf'})
            assert first.status_code == 200
            assert first.get_json()['cache_hit'] is False
            assert first.get_json()['rankings'] == [{'scope': expected}]
            repeat = client.post('/api/v1/community/risk-map-v2', json=payload, headers={'X-CSRF-Token': 'identity-boundary-csrf'})
            assert repeat.get_json()['cache_hit'] is True
        assert calls == [None, ('村A',), ('村B',)]
    finally:
        clear_local_community_risk_cache()
