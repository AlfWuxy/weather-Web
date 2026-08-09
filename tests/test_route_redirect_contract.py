# -*- coding: utf-8 -*-
"""Web 登录、游客与跨运行态跳转合同。"""

import html
import re
from urllib.parse import parse_qs, urlsplit

import pytest


PROTECTED_PATHS = (
    "/forecast-7day",
    "/profile",
    "/dashboard",
)


def _set_runtime_mode(app, mode):
    app.config["WECHAT_FORMAL_RUNTIME"] = mode != "web"
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = mode == "dual"


def _guest_href(response):
    body = response.get_data(as_text=True)
    match = re.search(r'<a\s+href="([^"]+)"[^>]*>游客体验</a>', body)
    assert match is not None
    return html.unescape(match.group(1))


@pytest.mark.parametrize(
    "target",
    (
        "/",
        "/dashboard",
        "/profile",
        "/risk?location=duchang&view=compact",
        "/forecast-7day?location=duchang&view=compact",
    ),
)
def test_safe_next_accepts_only_registered_get_targets_and_preserves_query(app, target):
    from services.public_service import _safe_next_url

    with app.test_request_context("/login"):
        assert _safe_next_url(target) == target


@pytest.mark.parametrize(
    "target",
    (
        "https://evil.example/profile",
        "//evil.example/profile",
        "//[",
        "\\\\evil.example\\profile",
        "/\\evil.example/profile",
        "profile",
        "/logout",
        "/logout?next=/forecast-7day",
        "/missing-route",
        "/profile\r\nLocation: https://evil.example",
    ),
)
def test_safe_next_rejects_external_unknown_and_post_only_targets(app, target):
    from services.public_service import _safe_next_url

    with app.test_request_context("/login"):
        assert _safe_next_url(target) is None


def test_login_guest_link_preserves_only_validated_next(client):
    target = "/profile?tab=privacy&view=compact"
    response = client.get("/login", query_string={"next": target})
    href = _guest_href(response)

    assert urlsplit(href).path == "/guest"
    assert parse_qs(urlsplit(href).query) == {"next": [target]}

    invalid = client.get("/login", query_string={"next": "/logout"})
    assert parse_qs(urlsplit(_guest_href(invalid)).query) == {}


@pytest.mark.parametrize("target", ("/logout", "/missing-route", "https://evil.example"))
def test_guest_invalid_next_falls_back_to_dashboard(client, target):
    response = client.get("/guest", query_string={"next": target}, follow_redirects=False)

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/dashboard")
    assert "/forecast-7day" not in response.headers["Location"]


@pytest.mark.parametrize("mode", ("web", "dual", "mini_only"))
@pytest.mark.parametrize(
    ("path", "expected_kind"),
    (
        ("/forecast-7day", "private"),
        ("/profile", "private"),
        ("/dashboard", "private"),
        ("/risk", "public"),
        ("/missing-route", "missing"),
        ("/logout", "post_only"),
    ),
)
def test_anonymous_web_route_matrix_has_distinct_destinations(
    app,
    client,
    monkeypatch,
    mode,
    path,
    expected_kind,
):
    _set_runtime_mode(app, mode)
    monkeypatch.setattr(
        "blueprints.public.render_public_risk_page",
        lambda _location: ("risk", 200),
    )

    response = client.get(path, follow_redirects=False)
    location = response.headers.get("Location", "")

    if expected_kind == "private" and mode == "mini_only":
        assert response.status_code == 303
        assert location.endswith("/action")
    elif expected_kind == "private":
        assert response.status_code in (301, 302, 303)
        assert "/login" in location
        assert "next=" in location
    elif expected_kind == "public":
        assert response.status_code == 200
        assert location == ""
    elif expected_kind == "missing":
        assert response.status_code == 404
        assert location == ""
    else:
        assert response.status_code == 405
        assert location == ""

    if path != "/forecast-7day":
        assert "/forecast-7day" not in location


@pytest.mark.parametrize("mode", ("web", "dual", "mini_only"))
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_authenticated_web_route_matrix_never_collapses_to_forecast(
    app,
    authenticated_client,
    mode,
    path,
):
    _set_runtime_mode(app, mode)

    response = authenticated_client.get(path, follow_redirects=False)
    location = response.headers.get("Location", "")

    if mode == "mini_only":
        assert response.status_code == 303
        assert location.endswith("/action")
    else:
        assert response.status_code == 200
        assert location == ""
    if path != "/forecast-7day":
        assert "/forecast-7day" not in location


@pytest.mark.parametrize("mode", ("web", "dual", "mini_only"))
@pytest.mark.parametrize(
    ("path", "dual_expected"),
    (
        ("/forecast-7day", "page"),
        ("/profile", "dashboard"),
        ("/dashboard", "page"),
    ),
)
def test_guest_web_route_matrix_never_uses_forecast_as_fallback(
    app,
    client,
    db_session,
    mode,
    path,
    dual_expected,
):
    _set_runtime_mode(app, mode)
    assert client.get("/guest", follow_redirects=False).status_code in (301, 302, 303)

    response = client.get(path, follow_redirects=False)
    location = response.headers.get("Location", "")

    if mode == "mini_only":
        assert response.status_code == 303
        assert location.endswith("/action")
    elif dual_expected == "dashboard":
        assert response.status_code in (301, 302, 303)
        assert location.endswith("/dashboard")
    else:
        assert response.status_code == 200
        assert location == ""
    if path != "/forecast-7day":
        assert "/forecast-7day" not in location


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/account-link"),
        ("POST", "/account-link/phone"),
        ("POST", "/account-link/code"),
    ),
)
def test_guest_account_link_routes_require_real_account_and_never_use_forecast(
    app,
    client,
    db_session,
    method,
    path,
):
    """游客不能进入或写入跨端账号绑定流程。"""
    from core.db_models import MiniProgramLinkChallenge

    _set_runtime_mode(app, "dual")
    assert client.get("/guest", follow_redirects=False).status_code in (301, 302, 303)
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = "guest-account-link-csrf"

    response = client.open(
        path,
        method=method,
        data={
            "csrf_token": "guest-account-link-csrf",
            "phone": "13900000000",
            "current_password": "unused-guest-password",
            "miniprogram_privacy_consent": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers.get("Location", "")
    assert location.endswith("/register")
    assert "/forecast-7day" not in location
    assert MiniProgramLinkChallenge.query.count() == 0


@pytest.mark.parametrize(
    ("target", "suffix"),
    (
        ("/logout", "logout"),
        ("/missing-route", "missing"),
        ("https://evil.example", "external"),
    ),
)
def test_login_invalid_next_uses_role_default_and_never_forecast(
    client,
    db_session,
    target,
    suffix,
):
    from core.db_models import User

    user = User(username=f"strict-next-{suffix}", role="user")
    user.set_password("testpass")
    db_session.add(user)
    db_session.commit()
    csrf = f"csrf-{suffix}"
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf

    response = client.post(
        "/login",
        data={
            "username": user.username,
            "password": "testpass",
            "csrf_token": csrf,
            "next": target,
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/dashboard")
    assert "/forecast-7day" not in response.headers["Location"]
