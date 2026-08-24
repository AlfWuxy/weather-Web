# -*- coding: utf-8 -*-
"""注册专用限流：校验通过才扣；用户名/邮箱占用也扣。不要 follow_redirects。"""
from pathlib import Path

from flask_login import UserMixin, current_user, login_user
from utils.validators import validate_password

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_HTML = REPO_ROOT / 'templates' / 'register.html'
LOGIN_HTML = REPO_ROOT / 'templates' / 'login.html'
VALID_PASSWORD = 'password1234'  # 12 位；默认 validate_password 仍接受更短


class _FormalUser(UserMixin):
    def __init__(self, user_id, role='user'):
        self.id = user_id
        self.role = role
        self.is_guest = False


def _csrf(client, token='register-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def _prepare_register_limits(app, limit='2 per hour'):
    from core.extensions import db, limiter

    app.config['RATE_LIMIT_REGISTER'] = limit
    with app.app_context():
        db.create_all()
        limiter.reset()


def _post_register(client, username, password=VALID_PASSWORD, csrf='register-csrf', ip=None, **extra):
    data = {
        'username': username,
        'password': password,
        'confirm_password': extra.pop('confirm_password', password),
        'csrf_token': csrf,
    }
    data.update(extra)
    kwargs = {'data': data, 'follow_redirects': False}
    if ip:
        kwargs['environ_base'] = {'REMOTE_ADDR': ip}
    return client.post('/register', **kwargs)


def test_validate_password_default_still_six():
    valid, result = validate_password('password123')
    assert valid is True
    assert result == 'password123'

    valid, _ = validate_password('123')
    assert valid is False

    valid, msg = validate_password('password123', min_length=12)
    assert valid is False
    assert '12' in msg


def test_register_template_minlength_login_has_none():
    register_src = REGISTER_HTML.read_text(encoding='utf-8')
    login_src = LOGIN_HTML.read_text(encoding='utf-8')
    assert 'name="password"' in register_src
    assert 'minlength="12"' in register_src
    assert 'name="password"' in login_src
    assert 'minlength' not in login_src


def test_get_register_repeated_stays_200(app, client):
    _prepare_register_limits(app, limit='1 per hour')
    for _ in range(8):
        resp = client.get('/register')
        assert resp.status_code == 200


def test_invalid_register_post_does_not_consume_quota(app, client):
    _prepare_register_limits(app)
    csrf = _csrf(client)
    username = 'samequota'

    for _ in range(5):
        short_name = _post_register(client, 'ab', csrf=csrf)
        assert short_name.status_code == 302
        assert short_name.status_code != 429
        assert '/register' in (short_name.headers.get('Location') or '')

        short_password = _post_register(client, username, password='123456', csrf=csrf)
        assert short_password.status_code == 302
        assert short_password.status_code != 429
        assert '/register' in (short_password.headers.get('Location') or '')

    first = _post_register(client, username, csrf=csrf)
    second = _post_register(client, username, csrf=csrf)
    third = _post_register(client, username, csrf=csrf)
    assert first.status_code == 302
    assert '/login' in (first.headers.get('Location') or '')
    assert second.status_code == 302
    assert '/register' in (second.headers.get('Location') or '')
    assert third.status_code == 429


def test_valid_posts_exhaust_register_quota(app, client):
    _prepare_register_limits(app)
    csrf = _csrf(client)

    first = _post_register(client, 'quota_user_a', csrf=csrf)
    second = _post_register(client, 'quota_user_a', csrf=csrf)
    third = _post_register(client, 'quota_user_a', csrf=csrf)

    assert first.status_code == 302
    assert '/login' in (first.headers.get('Location') or '')
    assert second.status_code == 302
    assert '/register' in (second.headers.get('Location') or '')
    assert third.status_code == 429

    still_get = client.get('/register')
    assert still_get.status_code == 200


def test_username_taken_still_counts(app, client):
    from core.db_models import User
    from core.extensions import db

    _prepare_register_limits(app)
    with app.app_context():
        taken = User(username='taken_name', role='user')
        taken.set_password(VALID_PASSWORD)
        db.session.add(taken)
        db.session.commit()

    csrf = _csrf(client)
    first = _post_register(client, 'taken_name', csrf=csrf)
    second = _post_register(client, 'taken_name', csrf=csrf)
    third = _post_register(client, 'taken_name', csrf=csrf)

    assert first.status_code == 302
    assert '/register' in (first.headers.get('Location') or '')
    assert second.status_code == 302
    assert third.status_code == 429


def test_email_taken_still_counts(app, client):
    from core.db_models import User
    from core.extensions import db

    _prepare_register_limits(app)
    with app.app_context():
        taken = User(username='email_owner', email='dup@example.com', role='user')
        taken.set_password(VALID_PASSWORD)
        db.session.add(taken)
        db.session.commit()

    csrf = _csrf(client)
    first = _post_register(client, 'email_try_one', csrf=csrf, email='dup@example.com')
    second = _post_register(client, 'email_try_one', csrf=csrf, email='dup@example.com')
    third = _post_register(client, 'email_try_one', csrf=csrf, email='dup@example.com')

    assert first.status_code == 302
    assert '/register' in (first.headers.get('Location') or '')
    assert second.status_code == 302
    assert third.status_code == 429


def test_same_ip_different_username_uses_separate_bucket(app, client):
    _prepare_register_limits(app)
    csrf = _csrf(client)
    ip = '203.0.113.40'

    assert _post_register(client, 'bucket_alice', csrf=csrf, ip=ip).status_code == 302
    assert _post_register(client, 'bucket_alice', csrf=csrf, ip=ip).status_code == 302
    assert _post_register(client, 'bucket_alice', csrf=csrf, ip=ip).status_code == 429

    other = _post_register(client, 'bucket_bob', csrf=csrf, ip=ip)
    assert other.status_code == 302
    assert other.status_code != 429


def test_same_username_different_ip_uses_separate_bucket(app, client):
    _prepare_register_limits(app)
    csrf = _csrf(client)

    assert _post_register(client, 'shared_name', csrf=csrf, ip='203.0.113.41').status_code == 302
    assert _post_register(client, 'shared_name', csrf=csrf, ip='203.0.113.41').status_code == 302
    assert _post_register(client, 'shared_name', csrf=csrf, ip='203.0.113.41').status_code == 429

    other_ip = _post_register(client, 'shared_name', csrf=csrf, ip='203.0.113.42')
    assert other_ip.status_code == 302
    assert other_ip.status_code != 429


def test_register_key_uses_hashed_username_not_plaintext(app):
    from core.security import hash_identifier, rate_limit_key, register_rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/register',
            method='POST',
            data={'username': 'alice'},
            environ_base={'REMOTE_ADDR': '203.0.113.10'},
        ):
            key = register_rate_limit_key()
            digest = hash_identifier('alice')
            global_key = rate_limit_key()

    assert key == f'register:203.0.113.10:{digest}'
    assert key.startswith('register:')
    assert 'alice' not in key
    assert key != global_key
    assert not key.startswith('ip:')
    assert not key.startswith('user:')


def test_register_key_empty_username_uses_sentinel(app):
    from core.security import register_rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/register',
            method='POST',
            data={'username': ''},
            environ_base={'REMOTE_ADDR': '198.51.100.9'},
        ):
            key = register_rate_limit_key()

    assert key == 'register:198.51.100.9:empty'


def test_register_key_ignores_logged_in_user_id(app):
    from core.security import rate_limit_key, register_rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/register',
            method='POST',
            data={'username': 'still_ip_user'},
            environ_base={'REMOTE_ADDR': '203.0.113.77'},
        ):
            login_user(_FormalUser(42))
            assert current_user.is_authenticated is True
            register_key = register_rate_limit_key()
            global_key = rate_limit_key()

    assert global_key == 'user:42'
    assert register_key.startswith('register:203.0.113.77:')
    assert 'user:42' not in register_key
    assert register_key != global_key


def test_register_key_trusted_proxy_xff(app):
    from core.security import register_rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.app_context():
        with app.test_request_context(
            '/register',
            method='POST',
            data={'username': 'proxyuser'},
            headers={'X-Forwarded-For': '203.0.113.10'},
            environ_base={'REMOTE_ADDR': '127.0.0.1'},
        ):
            key = register_rate_limit_key()

    assert '203.0.113.10' in key
    assert key.startswith('register:203.0.113.10:')
    assert '127.0.0.1' not in key


def test_register_key_untrusted_xff_ignored(app):
    from core.security import register_rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.app_context():
        with app.test_request_context(
            '/register',
            method='POST',
            data={'username': 'proxyuser'},
            headers={'X-Forwarded-For': '203.0.113.10'},
            environ_base={'REMOTE_ADDR': '198.51.100.23'},
        ):
            key = register_rate_limit_key()

    assert key.startswith('register:198.51.100.23:')
    assert '203.0.113.10' not in key


def test_default_limiter_still_uses_rate_limit_key(app):
    from core.extensions import limiter
    from core.security import rate_limit_key

    key_func = getattr(limiter, '_key_func', None) or getattr(limiter, 'key_func', None)
    assert key_func is not None

    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.10'}):
        assert key_func() == rate_limit_key() == 'ip:203.0.113.10'


def test_six_char_password_can_still_login(app, client):
    from core.db_models import User
    from core.extensions import db

    _prepare_register_limits(app)
    with app.app_context():
        user = User(username='legacy6', role='user')
        user.set_password('123456')
        db.session.add(user)
        db.session.commit()

    csrf = _csrf(client, token='login-csrf')
    resp = client.post(
        '/login',
        data={'username': 'legacy6', 'password': '123456', 'csrf_token': csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.status_code != 429
    location = resp.headers.get('Location') or ''
    assert '/dashboard' in location
