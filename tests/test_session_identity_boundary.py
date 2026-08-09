# -*- coding: utf-8 -*-
"""Web 身份切换时的精确会话清理合同。"""


IDENTITY_SCOPED_KEYS = (
    "pair_session_id",
    "pair_session_code",
    "pair_token",
    "last_api_token_plain",
    "last_mini_link_code",
    "last_mini_link_expires_at",
    "pair_link_token",
    "pair_link_id",
    "created_pair_id",
    "demo_mode",
)
GUEST_KEYS = ("guest_profile", "guest_assessment", "guest_id")
PRESERVED_FLASH = ("info", "切换身份后仍需显示")


def _create_user(db_session, username):
    from core.db_models import User

    user = User(username=username, role="user")
    user.set_password("testpass")
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username, *, remember=False, csrf="identity-boundary-csrf"):
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf
    return client.post(
        "/login",
        data={
            "username": username,
            "password": "testpass",
            "csrf_token": csrf,
            "remember": "1" if remember else "",
        },
        follow_redirects=False,
    )


def _seed_identity_state(client, csrf):
    with client.session_transaction() as flask_session:
        for key in IDENTITY_SCOPED_KEYS:
            flask_session[key] = f"stale-{key}"
        flask_session["guest_profile"] = {"username": "旧游客"}
        flask_session["guest_assessment"] = {"risk": "stale"}
        flask_session["guest_id"] = "guest-stale"
        flask_session["_csrf_token"] = csrf
        flask_session.setdefault("_flashes", []).append(PRESERVED_FLASH)


def _assert_identity_state_removed(flask_session, *, guest_recreated=False):
    for key in IDENTITY_SCOPED_KEYS:
        assert key not in flask_session
    if guest_recreated:
        assert flask_session["guest_profile"]["username"] == "游客"
        assert flask_session["guest_id"] != "guest-stale"
        assert "guest_assessment" not in flask_session
    else:
        for key in GUEST_KEYS:
            assert key not in flask_session


def _assert_csrf_and_flash_preserved(flask_session, csrf):
    assert flask_session["_csrf_token"] == csrf
    assert PRESERVED_FLASH in flask_session.get("_flashes", [])


def _cookie_headers(response):
    return "\n".join(response.headers.getlist("Set-Cookie"))


def test_logout_clears_every_identity_key_and_remember_cookie(client, db_session):
    user = _create_user(db_session, "identity-logout-user")
    csrf = "identity-logout-csrf"
    login_response = _login(client, user.username, remember=True, csrf=csrf)
    assert "remember_token=" in _cookie_headers(login_response)
    _seed_identity_state(client, csrf)

    response = client.post(
        "/logout",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/")
    assert "remember_token=;" in _cookie_headers(response)
    with client.session_transaction() as flask_session:
        _assert_identity_state_removed(flask_session)
        _assert_csrf_and_flash_preserved(flask_session, csrf)


def test_switching_real_accounts_clears_old_state_and_old_remember_cookie(
    client,
    db_session,
):
    first = _create_user(db_session, "identity-first-user")
    second = _create_user(db_session, "identity-second-user")
    csrf = "identity-switch-csrf"
    first_response = _login(client, first.username, remember=True, csrf=csrf)
    assert "remember_token=" in _cookie_headers(first_response)
    _seed_identity_state(client, csrf)

    response = _login(client, second.username, remember=False, csrf=csrf)

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/dashboard")
    assert "remember_token=;" in _cookie_headers(response)
    with client.session_transaction() as flask_session:
        _assert_identity_state_removed(flask_session)
        _assert_csrf_and_flash_preserved(flask_session, csrf)
        assert str(flask_session["_user_id"]).startswith(f"{second.id}:")


def test_entering_guest_replaces_stale_guest_and_pair_state(client):
    csrf = "identity-guest-csrf"
    _seed_identity_state(client, csrf)

    response = client.get("/guest", follow_redirects=False)

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/dashboard")
    with client.session_transaction() as flask_session:
        _assert_identity_state_removed(flask_session, guest_recreated=True)
        _assert_csrf_and_flash_preserved(flask_session, csrf)


def test_guest_to_real_login_removes_guest_identity_and_pair_state(client, db_session):
    user = _create_user(db_session, "identity-guest-to-real")
    csrf = "identity-guest-to-real-csrf"
    assert client.get("/guest", follow_redirects=False).status_code in (301, 302, 303)
    _seed_identity_state(client, csrf)

    response = _login(client, user.username, csrf=csrf)

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/dashboard")
    with client.session_transaction() as flask_session:
        _assert_identity_state_removed(flask_session)
        _assert_csrf_and_flash_preserved(flask_session, csrf)
        assert str(flask_session["_user_id"]).startswith(f"{user.id}:")
