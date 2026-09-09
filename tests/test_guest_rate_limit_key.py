# -*- coding: utf-8 -*-
"""Guest 限流键护栏：已认证游客必须按 IP，轮换 guest id 不得换桶。"""


def test_guest_authenticated_rate_limit_key_is_ip_and_stable_across_id_rotation(app):
    """GuestUser is_authenticated 时 rate_limit_key 以 ip: 开头；同 IP 轮换 guest id 键不变。"""
    from flask_login import current_user, login_user

    from core.guest import GuestUser
    from core.security import rate_limit_key

    same_ip = {"REMOTE_ADDR": "203.0.113.10"}
    other_ip = {"REMOTE_ADDR": "203.0.113.11"}
    profile = {"username": "游客"}

    with app.app_context():
        with app.test_request_context("/", environ_base=same_ip):
            guest_a = GuestUser("guest:token-a", profile)
            login_user(guest_a)
            assert current_user.is_authenticated is True
            assert getattr(current_user, "is_guest", False) is True
            key_a = rate_limit_key()

        with app.test_request_context("/", environ_base=same_ip):
            # 模拟 GET /guest 再签新 id（同 IP）
            guest_b = GuestUser("guest:token-b", profile)
            login_user(guest_b)
            assert current_user.is_authenticated is True
            assert current_user.id == "guest:token-b"
            key_b = rate_limit_key()

        with app.test_request_context("/", environ_base=other_ip):
            guest_c = GuestUser("guest:token-c", profile)
            login_user(guest_c)
            assert current_user.is_authenticated is True
            key_other_ip = rate_limit_key()

    assert key_a.startswith("ip:")
    assert key_b.startswith("ip:")
    assert key_other_ip.startswith("ip:")
    # 同 IP：轮换 guest id 不得产生新桶
    assert key_a == key_b
    assert key_a == "ip:203.0.113.10"
    # 不同 IP：应分桶
    assert key_other_ip == "ip:203.0.113.11"
    assert key_other_ip != key_a
    # 键中不得出现可轮换 guest id
    assert "guest:token-a" not in key_a
    assert "guest:token-b" not in key_b
    assert "guest:token-c" not in key_other_ip


def test_guest_prefix_id_without_is_guest_still_uses_ip(app):
    """仅 id 前缀 guest:（或 role=guest）时也应走 IP 维，防漏标 is_guest 的伪主体。"""
    from flask_login import login_user

    from core.security import rate_limit_key

    # 伪造「已登录」主体：有 id 前缀 guest:，无 is_guest 属性
    class _PseudoGuest:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        id = "guest:forged-no-flag"
        role = "guest"

        def get_id(self):
            return self.id

    with app.app_context():
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "198.51.100.7"},
        ):
            login_user(_PseudoGuest())
            key = rate_limit_key()

    assert key.startswith("ip:")
    assert key == "ip:198.51.100.7"
    assert "guest:forged-no-flag" not in key
