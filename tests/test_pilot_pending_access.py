"""待关联入口不扩大机构权限；测试账户与机构均为合成。"""
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from flask import g

from core.db_models import User
from core.guest import GuestUser
from core.pilot_models import PilotInstitution, PilotMembership


API = '/api/v1/workbench'


@pytest.fixture
def pending_pilot(app, db_session):
    app.config['FEATURE_INSTITUTION_WORKBENCH'] = True
    account = User(username='synthetic-pending-account', password_hash='unused', role='user')
    guest = GuestUser('guest:synthetic-pending-guest', {'username': '合成游客'})
    institution = PilotInstitution(name='合成其他机构不应显示', region_code='360428',
                                   latitude=29.2, longitude=116.3, enabled=True)
    db_session.add_all([account, institution])
    db_session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), db=db_session,
                           account=account, guest=guest, institution=institution)


def login(env, account):
    with env.client.session_transaction() as session:
        session['_user_id'] = account.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'synthetic-pending-csrf'
    # 外层数据库 fixture 保留应用上下文，需要清除前一请求的账户缓存。
    g.pop('_login_user', None)


def test_signed_in_unlinked_account_sees_pending_page_only(pending_pilot):
    env = pending_pilot
    login(env, env.account)
    response = env.client.get('/workbench')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '等待关联合作机构' in body
    assert '账户已登录' in body
    assert '名称、所在地' in body and '保存安排' in body
    assert '返回首页' in body
    assert env.institution.name not in body and env.institution.id not in body
    assert 'workbench.js' not in body and 'id="wb-upload-form"' not in body
    assert 'href="/workbench"' not in body
    assert 'no-store' in response.headers['Cache-Control']
    assert env.client.get(API + '/institutions').json['institutions'] == []
    assert env.client.get(f'{API}/institutions/{env.institution.id}/overview').status_code == 404
    assert env.client.post(f'{API}/institutions/{env.institution.id}/datasets', json={},
                           headers={'X-CSRF-Token': 'synthetic-pending-csrf'}).status_code == 404


def test_anonymous_keeps_login_redirect_and_api_unauthorized(pending_pilot):
    env = pending_pilot
    response = env.client.get('/workbench')
    assert response.status_code == 302
    assert urlparse(response.headers['Location']).path == '/login'
    assert env.client.get(API + '/institutions').status_code == 401


def test_guest_still_forbidden(pending_pilot):
    env = pending_pilot
    login(env, env.guest)
    assert env.client.get('/workbench').status_code == 403
    assert env.client.get(API + '/institutions').status_code == 403


@pytest.mark.parametrize('authenticated', [False, True])
def test_disabled_feature_still_404(pending_pilot, authenticated):
    env = pending_pilot
    env.app.config['FEATURE_INSTITUTION_WORKBENCH'] = False
    if authenticated:
        login(env, env.account)
    assert env.client.get('/workbench').status_code == 404
    assert env.client.get(API + '/institutions').status_code == 404


def test_active_institution_keeps_authorized_workbench(pending_pilot):
    env = pending_pilot
    env.db.add(PilotMembership(institution_id=env.institution.id, user_id=env.account.id,
                               role='uploader', active=True))
    env.db.commit()
    login(env, env.account)
    response = env.client.get('/workbench')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="wb-upload-form"' in body
    assert env.institution.name in body
    assert 'pending-workbench-title' not in body
    institutions = env.client.get(API + '/institutions').json['institutions']
    assert [item['id'] for item in institutions] == [env.institution.id]
    assert env.client.get(f'{API}/institutions/{env.institution.id}/overview').status_code == 200


@pytest.mark.parametrize('membership_active,institution_enabled', [(False, True), (True, False)])
def test_inactive_association_has_pending_page_without_access(pending_pilot, membership_active,
                                                            institution_enabled):
    env = pending_pilot
    env.institution.enabled = institution_enabled
    env.db.add(PilotMembership(institution_id=env.institution.id, user_id=env.account.id,
                               role='uploader', active=membership_active))
    env.db.commit()
    login(env, env.account)
    response = env.client.get('/workbench')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'pending-workbench-title' in body
    assert env.institution.name not in body
    assert env.client.get(API + '/institutions').json['institutions'] == []
    assert env.client.get(f'{API}/institutions/{env.institution.id}/overview').status_code == 404
