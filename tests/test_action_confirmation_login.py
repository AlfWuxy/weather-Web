# -*- coding: utf-8 -*-
"""公开查阅凭证不能替代正式行动确认登录。"""
from datetime import timedelta
from urllib.parse import urlsplit, parse_qs

import pytest

from core.db_models import DailyStatus, Pair, User
from core.security import hash_short_code
from core.time_utils import utcnow


def _csrf(client):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'confirm-login-csrf'
    return 'confirm-login-csrf'


@pytest.mark.parametrize('path', ['/action/confirm', '/e/test-token/checkin'])
def test_anonymous_confirmation_json_stops_before_pair_lookup(app, client, db_session, monkeypatch, path):
    app.config['WECHAT_FORMAL_RUNTIME'] = False
    monkeypatch.setattr('services.public_service._resolve_pair_from_session_or_code',
                        lambda *a, **kw: pytest.fail('匿名确认不得进入家庭数据解析'))
    response = client.post(path, json={'short_code': '12345678', 'actions_done': ['drink_water'],
                                      'csrf_token': _csrf(client)})
    assert response.status_code == 401
    assert response.json['error'] == 'login_required'
    assert response.json['login_url'].startswith('/login?next=')
    assert DailyStatus.query.count() == 0


def test_html_confirmation_login_returns_to_get_not_write(app, client, db_session):
    response = client.post('/e/test-token/checkin', data={'short_code': '12345678', 'csrf_token': _csrf(client)})
    assert response.status_code == 302
    login = urlsplit(response.location)
    assert login.path == '/login'
    destination = parse_qs(login.query)['next'][0]
    assert urlsplit(destination).path == '/e/test-token'
    assert parse_qs(urlsplit(destination).query)['short_code'] == ['12345678']


def test_authenticated_stranger_cannot_confirm_unbound_pair(app, client, db_session):
    owner = User(username='confirm-owner', role='caregiver')
    actor = User(username='confirm-other', role='user')
    owner.set_password('password-owner'); actor.set_password('password-other')
    db_session.add_all([owner, actor]); db_session.flush()
    pair = Pair(caregiver_id=owner.id, community_code='都昌', location_query='都昌',
                elder_code='confirm-elder', short_code='12345678', short_code_hash=hash_short_code('12345678'),
                short_code_expires_at=utcnow()+timedelta(days=2), status='active')
    db_session.add(pair); db_session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = actor.get_id(); sess['_fresh'] = True
    response = client.post('/action/confirm', json={'short_code': '12345678', 'actions_done': ['drink_water'],
                                                  'csrf_token': _csrf(client)})
    assert response.status_code == 403
    assert response.json['error'] == 'pair_authorization_required'
    assert DailyStatus.query.count() == 0


def test_guest_confirmation_is_not_formal_login(app, client, db_session, monkeypatch):
    monkeypatch.setattr('services.public_service._resolve_pair_from_session_or_code',
                        lambda *a, **kw: pytest.fail('游客确认不得解析家庭资料'))
    client.get('/guest')
    response = client.post('/action/confirm', json={'csrf_token': _csrf(client), 'short_code': '12345678'})
    assert response.status_code == 401


def test_login_return_requires_reentry_and_then_allows_bound_confirmation(app, client, db_session, monkeypatch):
    from services import public_service
    owner=User(username='reentry-owner',role='caregiver');owner.set_password('reentry-owner-password')
    actor=User(username='reentry-actor',role='user');actor.set_password('reentry-actor-password')
    db_session.add_all([owner,actor]);db_session.flush()
    pair=Pair(caregiver_id=owner.id,community_code='都昌',location_query='都昌',elder_code='reentry-elder',
        short_code='44332211',short_code_hash=hash_short_code('44332211'),
        short_code_expires_at=utcnow()+timedelta(days=1),status='active')
    db_session.add(pair);db_session.commit();pair_id=pair.id
    def context(bound_pair,status_date):
        status=public_service._get_or_create_daily_status(bound_pair,status_date,None)
        return status,[{'id':'drink_water','title':'喝水','detail':'少量多次'}],[],None,None,None,[]
    monkeypatch.setattr(public_service,'_build_action_context',context)
    blocked=client.post('/action/confirm',data={'short_code':'44332211','csrf_token':_csrf(client)})
    next_url=parse_qs(urlsplit(blocked.location).query)['next'][0]
    login=client.post('/login?next='+next_url,data={'username':'reentry-actor','password':'reentry-actor-password','csrf_token':_csrf(client)})
    assert login.status_code==302
    assert urlsplit(login.location).path=='/action'
    # 登录清除旧绑定；必须先用短码重新进入，不能自动重放确认。
    assert DailyStatus.query.filter_by(pair_id=pair_id).count()==0
    entered=client.post('/action',data={'short_code':'44332211','csrf_token':_csrf(client)})
    assert entered.status_code==200
    confirmed=client.post('/action/confirm',json={'short_code':'44332211','actions_done':['drink_water'],'csrf_token':_csrf(client)})
    assert confirmed.status_code==200 and confirmed.json['ok'] is True
    status=DailyStatus.query.filter_by(pair_id=pair_id).one()
    assert status.confirmed_at is not None and status.actions_done_count==1
