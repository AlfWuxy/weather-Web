# -*- coding: utf-8 -*-
"""Regression tests for login 'remember me' functionality."""
import pytest


PAIR_SESSION_KEYS = ('pair_token', 'pair_session_id', 'pair_session_code')


def _extract_set_cookie(resp):
    # Werkzeug headers support getlist; fall back to manual.
    try:
        cookies = resp.headers.getlist('Set-Cookie')
    except Exception:
        cookies = resp.headers.get_all('Set-Cookie', [])
    return "\n".join(cookies or [])


def test_login_sets_remember_cookie_when_checked(client, db_session):
    from core.db_models import User

    user = User(username='remember_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    resp = client.post(
        '/login',
        data={
            'username': 'remember_user',
            'password': 'testpass',
            'csrf_token': csrf,
            'remember': '1',
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    set_cookie = _extract_set_cookie(resp)
    assert 'remember_token=' in set_cookie


def test_login_does_not_set_remember_cookie_by_default(client, db_session):
    from core.db_models import User

    user = User(username='no_remember_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    resp = client.post(
        '/login',
        data={
            'username': 'no_remember_user',
            'password': 'testpass',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    set_cookie = _extract_set_cookie(resp)
    assert 'remember_token=' not in set_cookie


@pytest.mark.parametrize(
    ('role', 'expected_path'),
    [
        ('user', '/pairs'),
        ('caregiver', '/pairs'),
        ('community', '/community'),
        ('admin', '/admin'),
    ],
)
def test_login_uses_role_aware_default_landing(client, db_session, role, expected_path):
    from core.db_models import User

    user = User(username=f'landing-{role}', role=role, community='朝阳社区')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = f'csrf-{role}'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    response = client.post(
        '/login',
        data={
            'username': user.username,
            'password': 'testpass',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers['Location'].endswith(expected_path)


def test_safe_next_keeps_multiple_query_parameters_and_takes_precedence(client, db_session):
    from core.db_models import User

    user = User(username='next-community', role='community')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'csrf-next-query'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    expected = '/forecast-7day?location=duchang&view=compact'
    response = client.post(
        '/login',
        query_string={'next': expected},
        data={
            'username': user.username,
            'password': 'testpass',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers['Location'] == expected


class _FakeRedisPipeline:
    def __init__(self, redis_client):
        self.redis_client = redis_client
        self.ops = []

    def incr(self, key):
        self.ops.append(('incr', key))
        return self

    def expire(self, key, seconds):
        self.ops.append(('expire', key, int(seconds)))
        return self

    def execute(self):
        for op in self.ops:
            if op[0] == 'incr':
                key = op[1]
                self.redis_client.values[key] = int(self.redis_client.values.get(key, 0)) + 1
            elif op[0] == 'expire':
                _, key, seconds = op
                self.redis_client.ttls[key] = seconds
        self.ops.clear()
        return True


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def ttl(self, key):
        return int(self.ttls.get(key, -1))

    def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1

    def pipeline(self):
        return _FakeRedisPipeline(self)


def test_login_lockout_uses_normalized_username_key(client, db_session, monkeypatch):
    from core.db_models import User

    user = User(username='caseuser', role='user')
    user.set_password('correct-password')
    db_session.add(user)
    db_session.commit()

    fake_redis = _FakeRedis()

    def _fake_get_redis_client():
        return fake_redis

    monkeypatch.setattr('core.weather._get_redis_client', _fake_get_redis_client)

    csrf = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    client.post(
        '/login',
        data={
            'username': 'CaseUser',
            'password': 'wrong-password',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    client.post(
        '/login',
        data={
            'username': 'caseuser',
            'password': 'wrong-password',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert fake_redis.values.get('login_failures:caseuser') == 2
    assert 'login_failures:CaseUser' not in fake_redis.values


def test_real_login_clears_guest_session_state(client, db_session):
    from core.db_models import User

    user = User(username='guest_to_real', role='caregiver')
    user.set_password('StrongPass123')
    db_session.add(user)
    db_session.commit()

    csrf = 'csrf-guest-to-real'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf
        session['guest_id'] = 'guest_stale'
        session['guest_profile'] = {'username': '游客'}
        session['guest_assessment'] = {'risk_level': '低风险'}
        session['pair_token'] = 'stale-login-token'
        session['pair_session_id'] = 51
        session['pair_session_code'] = '51000001'

    response = client.post(
        '/login',
        data={
            'username': user.username,
            'password': 'StrongPass123',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/pairs')
    with client.session_transaction() as session:
        assert session.get('_user_id') == user.get_id()
        assert 'guest_id' not in session
        assert 'guest_profile' not in session
        assert 'guest_assessment' not in session
        for key in PAIR_SESSION_KEYS:
            assert key not in session


def test_logout_always_clears_stale_guest_session_state(client, db_session):
    from core.db_models import User

    user = User(username='logout_guest_cleanup', role='user')
    user.set_password('StrongPass123')
    db_session.add(user)
    db_session.commit()

    csrf = 'csrf-logout-cleanup'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['guest_id'] = 'guest_stale'
        session['guest_profile'] = {'username': '游客'}
        session['guest_assessment'] = {'risk_level': '低风险'}
        session['pair_token'] = 'stale-logout-token'
        session['pair_session_id'] = 61
        session['pair_session_code'] = '61000001'

    response = client.post(
        '/logout',
        data={'csrf_token': csrf},
        follow_redirects=False,
    )

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert '_user_id' not in session
        assert 'guest_id' not in session
        assert 'guest_profile' not in session
        assert 'guest_assessment' not in session
        for key in PAIR_SESSION_KEYS:
            assert key not in session


def test_guest_entry_keeps_existing_pair_action_session(client):
    with client.session_transaction() as session:
        session['pair_token'] = 'guest-action-token'
        session['pair_session_id'] = 71
        session['pair_session_code'] = '71000001'

    response = client.get('/guest', follow_redirects=False)

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session['pair_token'] == 'guest-action-token'
        assert session['pair_session_id'] == 71
        assert session['pair_session_code'] == '71000001'
        assert session.get('_user_id', '').startswith('guest:')


def test_legacy_numeric_session_and_remember_cookie_require_relogin(app, db_session):
    from flask_login.utils import encode_cookie

    from core.constants import GUEST_ID_PREFIX
    from core.db_models import User
    from core.extensions import login_manager

    user = User(username='legacy_numeric_identity', role='user')
    user.set_password('StrongPass123')
    db_session.add(user)
    db_session.commit()

    assert login_manager._user_callback(str(user.id)) is None
    assert login_manager._user_callback(user.get_id()) is not None
    stamp = user.get_id().split(':', 1)[1]
    for malformed_id in (
        f'{user.id}:',
        f'0{user.id}:{stamp}',
        f'{user.id}:{stamp}:extra',
        f'9223372036854775808:{stamp}',
    ):
        assert login_manager._user_callback(malformed_id) is None
    with app.test_request_context('/'):
        guest_id = f'{GUEST_ID_PREFIX}loader-control'
        assert login_manager._user_callback(guest_id).get_id() == guest_id

    session_client = app.test_client()
    with session_client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    session_response = session_client.get('/profile', follow_redirects=False)
    assert session_response.status_code == 302
    assert '/login' in (session_response.headers.get('Location') or '')

    remember_client = app.test_client()
    remember_cookie_name = app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    remember_client.set_cookie(remember_cookie_name, encode_cookie(str(user.id)))
    remember_response = remember_client.get('/profile', follow_redirects=False)
    assert remember_response.status_code == 302
    assert '/login' in (remember_response.headers.get('Location') or '')
