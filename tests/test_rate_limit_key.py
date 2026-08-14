# -*- coding: utf-8 -*-
"""限流键：正式用户 user:<id>；游客与匿名 ip:<受信客户端 IP>。"""
from flask_login import UserMixin, current_user, login_user


class _FormalUser(UserMixin):
    def __init__(self, user_id, role='user'):
        self.id = user_id
        self.role = role
        self.is_guest = False


class _PseudoGuest(UserMixin):
    """覆盖漏标 is_guest、仅靠角色或 ID 前缀识别的游客。"""

    def __init__(self, user_id, role='guest'):
        self.id = user_id
        self.role = role


def test_guest_same_ip_rotating_id_keeps_ip_bucket(app):
    """同 IP 游客轮换账号 ID 时仍复用同一个 IP 桶。"""
    from core.guest import GuestUser
    from core.security import rate_limit_key

    same_ip = {'REMOTE_ADDR': '203.0.113.10'}
    profile = {'username': '游客'}

    with app.app_context():
        with app.test_request_context('/', environ_base=same_ip):
            login_user(GuestUser('guest:token-a', profile))
            key_a = rate_limit_key()

        with app.test_request_context('/', environ_base=same_ip):
            login_user(GuestUser('guest:token-b', profile))
            key_b = rate_limit_key()

        with app.test_request_context('/', environ_base=same_ip):
            anon_key = rate_limit_key()

    assert key_a == key_b == anon_key == 'ip:203.0.113.10'
    assert 'guest:token-a' not in key_a
    assert 'guest:token-b' not in key_b


def test_different_ips_use_different_buckets(app):
    """不同客户端 IP 必须进入不同限流桶。"""
    from core.guest import GuestUser
    from core.security import rate_limit_key

    profile = {'username': '游客'}
    with app.app_context():
        with app.test_request_context(
            '/', environ_base={'REMOTE_ADDR': '203.0.113.10'}
        ):
            login_user(GuestUser('guest:token-a', profile))
            key_a = rate_limit_key()

        with app.test_request_context(
            '/', environ_base={'REMOTE_ADDR': '203.0.113.11'}
        ):
            login_user(GuestUser('guest:token-b', profile))
            key_b = rate_limit_key()

    assert key_a == 'ip:203.0.113.10'
    assert key_b == 'ip:203.0.113.11'
    assert key_a != key_b


def test_formal_user_uses_user_id_bucket(app):
    """正式用户使用带命名空间的稳定账号桶。"""
    from core.security import rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/', environ_base={'REMOTE_ADDR': '203.0.113.10'}
        ):
            login_user(_FormalUser(42))
            assert current_user.is_authenticated is True
            key = rate_limit_key()

    assert key == 'user:42'


def test_trusted_proxy_xff_becomes_client_ip_key(app):
    """受信代理提供的 XFF 可以参与客户端 IP 分桶。"""
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
    """未受信来源伪造 XFF 时继续使用直连地址。"""
    from core.security import rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '203.0.113.10'},
        environ_base={'REMOTE_ADDR': '198.51.100.23'},
    ):
        key = rate_limit_key()

    assert key == 'ip:198.51.100.23'


def test_guest_prefix_and_role_without_is_guest_use_ip(app):
    """游客角色或游客 ID 前缀在漏标时仍走 IP 桶。"""
    from core.security import rate_limit_key

    with app.app_context():
        with app.test_request_context(
            '/', environ_base={'REMOTE_ADDR': '198.51.100.7'}
        ):
            login_user(_PseudoGuest('guest:forged-no-flag', role='guest'))
            key = rate_limit_key()

    assert key == 'ip:198.51.100.7'


def test_default_limiter_reuses_rate_limit_key(app):
    """默认 Limiter 与路由装饰器必须复用同一分桶规则。"""
    from core.extensions import limiter
    from core.security import rate_limit_key

    key_func = getattr(limiter, '_key_func', None) or getattr(
        limiter, 'key_func', None
    )
    assert key_func is not None

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '203.0.113.10'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
    ):
        assert key_func() == rate_limit_key() == 'ip:203.0.113.10'
