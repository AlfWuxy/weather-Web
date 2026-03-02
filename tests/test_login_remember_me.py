# -*- coding: utf-8 -*-
"""Regression tests for login 'remember me' functionality."""


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
