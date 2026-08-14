# -*- coding: utf-8 -*-
"""限流键：正式用户 user:<id>；游客与匿名 ip:<受信客户端IP>。"""
from flask_login import UserMixin, current_user, login_user


class _FormalUser(UserMixin):
    def __init__(self, user_id, role='user'):
        self.id = user_id
        self.role = role
        self.is_guest = False


class _PseudoGuest(UserMixin):
    """漏标 is_guest、仅靠 role / id 前缀识别的伪游客。"""
    def __init__(self, user_id, role='guest'):
        self.id = user_id
        self.role = role


def test_guest_same_ip_rotating_id_keeps_ip_bucket(app):
    """游客、同 IP、轮换 guest id → 键不变，格式 ip:...，键中无 guest id。"""
    from core.guest import GuestUser
    from core.security import rate_limit_key

    same_ip = {'REMOTE_ADDR': '203.0.113.10'}
    profile = {'username': '游客'}

    with app.app_context():
        with app.test_request_context('/', environ_base=same_ip):
            guest_a = GuestUser('guest:token-a', profile)
            login_user(guest_a)
            assert current_user.is_authenticated is True
            assert getattr(current_user, 'is_guest', False) is True
            key_a = rate_limit_key()

        with app.test_request_context('/', environ_base=same_ip):
            guest_b = GuestUser('guest:token-b', profile)
            login_user(guest_b)
            assert current_user.id == 'guest:token-b'
            key_b = rate_limit_key()

        with app.test_request_context('/', environ_base=same_ip):
            anon_key = rate_limit_key()

    assert key_a == 'ip:203.0.113.10'
    assert key_b == key_a
    assert anon_key == key_a
    assert 'guest:token-a' not in key_a
    assert 'guest:token-b' not in key_b


def test_different_ips_use_different_buckets(app):
    """不同客户端 IP 分到不同桶。"""
    from core.guest import GuestUser
    from core.security import rate_limit_key

    profile = {'username': '游客'}

    with app.app_context():
        with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.10'}):
            login_user(GuestUser('guest:token-a', profile))
            key_a = rate_limit_key()

        with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.11'}):
            login_user(GuestUser('guest:token-b', profile))
            key_b = rate_limit_key()

        with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.11'}):
            anon_key = rate_limit_key()

    assert key_a == 'ip:203.0.113.10'
    assert key_b == 'ip:203.0.113.11'
    assert key_a != key_b
    assert anon_key == key_b


def test_formal_user_uses_user_id_bucket(app):
    """正式用户 → user:<id>。"""
    from core.security import rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/',
            environ_base={'REMOTE_ADDR': '203.0.113.10'},
        ):
            login_user(_FormalUser(42))
            assert current_user.is_authenticated is True
            assert getattr(current_user, 'is_guest', False) is False
            key = rate_limit_key()

    assert key == 'user:42'
    assert not key.startswith('ip:')


def test_trusted_proxy_xff_becomes_client_ip_key(app):
    """受信代理：REMOTE_ADDR 在 TRUSTED_PROXY_CIDRS 内时采用 XFF 客户端 IP。"""
    from core.security import rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '203.0.113.10'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
    ):
        key = rate_limit_key()

    assert key == 'ip:203.0.113.10'


def test_untrusted_xff_is_ignored(app):
    """未受信 XFF：remote_addr 不在受信 CIDR 时忽略 XFF，键仍为 ip:<remote_addr>。"""
    from core.security import rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '203.0.113.10'},
        environ_base={'REMOTE_ADDR': '198.51.100.23'},
    ):
        key = rate_limit_key()

    assert key == 'ip:198.51.100.23'
    assert '203.0.113.10' not in key


def test_guest_prefix_and_role_without_is_guest_use_ip(app):
    """role=guest 或 id 前缀 guest:（即使漏标 is_guest）仍走 IP 桶。"""
    from core.security import rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/',
            environ_base={'REMOTE_ADDR': '198.51.100.7'},
        ):
            login_user(_PseudoGuest('guest:forged-no-flag', role='guest'))
            key = rate_limit_key()

    assert key == 'ip:198.51.100.7'
    assert 'guest:forged-no-flag' not in key


def test_default_limiter_reuses_rate_limit_key(app):
    """默认 Limiter 与 rate_limit_key 同一套分桶，而不是裸 get_remote_address。"""
    from core.extensions import limiter
    from core.security import rate_limit_key

    key_func = getattr(limiter, '_key_func', None) or getattr(limiter, 'key_func', None)
    assert key_func is not None

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '203.0.113.10'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
    ):
        assert key_func() == rate_limit_key() == 'ip:203.0.113.10'

    with app.app_context():
        with app.test_request_context(
            '/',
            environ_base={'REMOTE_ADDR': '203.0.113.10'},
        ):
            login_user(_FormalUser(7))
            assert key_func() == rate_limit_key() == 'user:7'
