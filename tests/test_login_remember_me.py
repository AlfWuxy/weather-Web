# -*- coding: utf-8 -*-
"""Regression tests for login 'remember me' functionality."""
import pytest


def _set_csrf(client, token):
    with client.session_transaction() as session:
        session['_csrf_token'] = token


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


def test_unknown_login_still_runs_dummy_password_hash(
    client,
    db_session,
    monkeypatch,
):
    calls = []

    def fake_check_password_hash(stored_hash, password):
        calls.append((stored_hash, password))
        return False

    monkeypatch.setattr(
        'services.public_service.check_password_hash',
        fake_check_password_hash,
    )
    csrf = 'unknown-login-csrf'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    response = client.post(
        '/login',
        data={
            'username': 'definitely-missing-user',
            'password': 'wrong-password',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][1] == 'wrong-password'


def test_legacy_phone_shaped_username_remains_reachable_without_verified_owner(
    client,
    db_session,
):
    from core.db_models import User

    legacy_user = User(username='13800138000', role='user')
    legacy_user.set_password('LegacyPhonePassword1!')
    db_session.add(legacy_user)
    db_session.commit()
    legacy_user_id = int(legacy_user.id)
    _set_csrf(client, 'legacy-phone-username-csrf')

    response = client.post(
        '/login',
        data={
            'username': '13800138000',
            'password': 'LegacyPhonePassword1!',
            'csrf_token': 'legacy-phone-username-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    with client.session_transaction() as session:
        assert session['_user_id'].startswith(f'{legacy_user_id}:')


def test_exact_case_keeps_legacy_case_collisions_reachable(client, db_session):
    from core.db_models import User

    upper_user = User(username='Alice', role='user')
    upper_user.set_password('UpperAlicePassword1!')
    lower_user = User(username='alice', role='user')
    lower_user.set_password('LowerAlicePassword1!')
    db_session.add_all([upper_user, lower_user])
    db_session.commit()
    upper_user_id = int(upper_user.id)
    lower_user_id = int(lower_user.id)

    upper_client = client.application.test_client()
    lower_client = client.application.test_client()
    _set_csrf(upper_client, 'upper-alice-csrf')
    _set_csrf(lower_client, 'lower-alice-csrf')

    upper_response = upper_client.post(
        '/login',
        data={
            'username': 'Alice',
            'password': 'UpperAlicePassword1!',
            'csrf_token': 'upper-alice-csrf',
        },
        follow_redirects=False,
    )
    lower_response = lower_client.post(
        '/login',
        data={
            'username': 'alice',
            'password': 'LowerAlicePassword1!',
            'csrf_token': 'lower-alice-csrf',
        },
        follow_redirects=False,
    )

    assert upper_response.status_code in (302, 303)
    assert lower_response.status_code in (302, 303)
    with upper_client.session_transaction() as session:
        assert session['_user_id'].startswith(f'{upper_user_id}:')
    with lower_client.session_transaction() as session:
        assert session['_user_id'].startswith(f'{lower_user_id}:')


def test_ambiguous_case_insensitive_login_does_not_choose_an_account(
    client,
    db_session,
):
    from core.db_models import User

    first_user = User(username='CaseUser', role='user')
    first_user.set_password('SharedPassword1!')
    second_user = User(username='caseuser', role='user')
    second_user.set_password('SharedPassword1!')
    db_session.add_all([first_user, second_user])
    db_session.commit()
    _set_csrf(client, 'ambiguous-case-csrf')

    response = client.post(
        '/login',
        data={
            'username': 'CASEUSER',
            'password': 'SharedPassword1!',
            'csrf_token': 'ambiguous-case-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    with client.session_transaction() as session:
        assert '_user_id' not in session


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


def test_formal_dual_runtime_login_lands_on_care_workspace(app, client, db_session):
    """微信正式双端态登录后进入家庭照护工作台。"""
    from core.db_models import User

    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WEB_PRIVATE_FEATURES_ENABLED'] = True
    user = User(username='formal-dual-user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'csrf-formal-dual-user'
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
    assert response.headers['Location'].endswith('/pairs')


def test_formal_miniprogram_only_login_keeps_account_link_landing(
    app,
    client,
    db_session,
):
    """双端开关关闭时保留原有最小账号绑定落点。"""
    from core.db_models import User

    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WEB_PRIVATE_FEATURES_ENABLED'] = False
    user = User(username='formal-mini-only-user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'csrf-formal-mini-only-user'
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
    assert response.headers['Location'].endswith('/account-link')


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

    assert list(fake_redis.values.values()) == [2]
    only_key = next(iter(fake_redis.values))
    assert only_key.startswith('login_failures:')
    assert 'caseuser' not in only_key.lower()


def test_login_lockout_unifies_username_and_phone_formats(
    client,
    db_session,
    monkeypatch,
):
    """同一账号的用户名和手机号写法必须共用一个不含明文的失败桶。"""
    from core.db_models import User
    from core.time_utils import utcnow

    user = User(
        username='phonebucket',
        role='user',
        phone_normalized='+8613800138000',
        phone_verified_at=utcnow(),
    )
    user.set_password('correct-password')
    db_session.add(user)
    db_session.commit()

    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        'core.weather._get_redis_client',
        lambda: fake_redis,
    )
    csrf = 'phone-bucket-csrf'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    for identifier in (
        'phonebucket',
        '13800138000',
        '+86 138-0013-8000',
    ):
        client.post(
            '/login',
            data={
                'username': identifier,
                'password': 'wrong-password',
                'csrf_token': csrf,
            },
            follow_redirects=False,
        )

    assert list(fake_redis.values.values()) == [3]
    assert all('13800138000' not in key for key in fake_redis.values)
