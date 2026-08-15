# -*- coding: utf-8 -*-
"""网页账号、手机号与微信小程序身份串联回归测试。"""

from contextlib import contextmanager
from datetime import timedelta
import threading


class _WechatResponse:
    status_code = 200

    def __init__(self, openid):
        self.openid = openid

    def json(self):
        return {"openid": self.openid, "session_key": "test-only-session-key"}


def _configure_wechat(app):
    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-test-secret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
        WX_MINIPROGRAM_SESSION_TTL_SECONDS=3600,
        ACCOUNT_LINK_CODE_PEPPER="l" * 64,
        ACCOUNT_LINK_CODE_TTL_SECONDS=600,
        RATE_LIMIT_MP_LINK="100 per minute",
        RATE_LIMIT_ACCOUNT_LINK="100 per minute",
    )


def _wechat_login(app, client, monkeypatch, openid):
    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse(openid),
    )
    response = client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "wx-code", "privacy_consent_version": "privacy-v1"},
    )
    assert response.status_code == 200
    return response.get_json()["data"]["session_token"]


def _login_web(client, username, password):
    csrf = f"csrf-{username}"
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302, 303)
    return csrf


def _generate_link_code(client, csrf, password="long-web-password"):
    response = client.post(
        "/account-link/code",
        data={
            "csrf_token": csrf,
            "current_password": password,
            "miniprogram_privacy_consent": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302, 303)
    with client.session_transaction() as flask_session:
        return flask_session["last_mini_link_code"]


def test_phone_normalization_and_phone_login_requires_verification(
    app,
    client,
    db_session,
):
    from core.db_models import User
    from core.time_utils import utcnow
    from services.cross_platform_identity import normalize_phone

    assert normalize_phone("138 0013 8000") == "+8613800138000"
    assert normalize_phone("0086-138-0013-8000") == "+8613800138000"
    assert normalize_phone("+1 (202) 555-0148") == "+12025550148"

    user = User(
        username="phone_login_user",
        role="user",
        phone_normalized="+8613800138000",
    )
    user.set_password("long-test-password")
    db_session.add(user)
    db_session.commit()

    csrf = "phone-login-csrf"
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf
    response = client.post(
        "/login",
        data={
            "username": "13800138000",
            "password": "long-test-password",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert user.phone_verified_at is None
    with client.session_transaction() as flask_session:
        assert "_user_id" not in flask_session

    username_client = app.test_client()
    _login_web(
        username_client,
        "phone_login_user",
        "long-test-password",
    )
    with username_client.session_transaction() as flask_session:
        assert "_user_id" in flask_session

    user.phone_verified_at = utcnow()
    db_session.commit()
    verified_phone_client = app.test_client()
    with verified_phone_client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = "verified-phone-csrf"
    verified_response = verified_phone_client.post(
        "/login",
        data={
            "username": "13800138000",
            "password": "long-test-password",
            "csrf_token": "verified-phone-csrf",
        },
        follow_redirects=False,
    )
    assert verified_response.status_code in (301, 302, 303)


def test_one_time_code_links_wechat_identity_and_rotates_session(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import (
        MiniProgramIdentity,
        MiniProgramLinkChallenge,
        MiniProgramSession,
        UsageEvent,
        User,
    )
    from core.extensions import db

    _configure_wechat(app)
    web_user = User(username="linked_web_user", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    target_user_id = int(web_user.id)

    csrf = _login_web(client, "linked_web_user", "long-web-password")
    link_code = _generate_link_code(client, csrf)
    assert len(link_code) == 8
    assert link_code.isdigit()

    mini_client = app.test_client()
    old_token = _wechat_login(app, mini_client, monkeypatch, "openid-link-success")
    source_identity = MiniProgramIdentity.query.one()
    source_user_id = int(source_identity.user_id)
    source_user = db.session.get(User, source_user_id)
    source_username = source_user.username
    from core.time_utils import utcnow

    # 模拟历史异常残留的孤立证明时间，退休流程必须显式清除且不能转移。
    source_user.wxpusher_uid_verified_at = utcnow()
    db.session.commit()
    assert source_username.startswith("wx_")
    assert source_user.account_origin == "miniprogram_placeholder"
    assert source_user_id != target_user_id

    linked = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {old_token}"},
    )

    assert linked.status_code == 200
    linked_data = linked.get_json()["data"]
    new_token = linked_data["session_token"]
    assert new_token != old_token
    assert linked_data["linked"] is True
    assert linked_data["linked_account"]["username"] == "linked_web_user"

    db.session.remove()
    identity = MiniProgramIdentity.query.one()
    assert identity.user_id == target_user_id
    retired_source = db.session.get(User, source_user_id)
    assert retired_source.deleted_at is not None
    assert retired_source.username.startswith("retired_wx_")
    assert retired_source.username != source_username
    assert retired_source.account_origin == "retired_miniprogram"
    assert retired_source.last_login is None
    assert retired_source.email is None
    assert retired_source.phone_normalized is None
    assert retired_source.wxpusher_uid is None
    assert retired_source.wxpusher_uid_verified_at is None
    assert db.session.get(User, target_user_id).wxpusher_uid_verified_at is None
    assert retired_source.health_sensitive_consent_version is None
    assert UsageEvent.query.filter_by(user_id=source_user_id).count() == 0
    assert UsageEvent.query.filter_by(
        user_id=None,
        event_type="wechat_login_success",
    ).count() == 1
    sessions = MiniProgramSession.query.all()
    assert len(sessions) == 1
    assert sessions[0].user_id == target_user_id
    challenge = MiniProgramLinkChallenge.query.one()
    assert challenge.consumed_at is not None
    assert challenge.consumed_identity_id == identity.id

    old_me = mini_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {old_token}"},
    )
    new_me = mini_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert old_me.status_code == 401
    assert new_me.status_code == 200
    assert new_me.get_json()["data"]["display_name"] == "linked_web_user"

    reused = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert reused.status_code == 400
    assert reused.get_json()["error"] == "invalid_link_code"


def test_linked_web_and_miniprogram_share_the_same_health_diary_owner(
    app,
    client,
    db_session,
    monkeypatch,
):
    """网页与小程序绑定后，双方通过真实路由读写同一账号日记。"""
    from core.db_models import HealthDiary, User

    _configure_wechat(app)
    web_user = User(username="cross_platform_diary", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    web_user_id = int(web_user.id)

    csrf = _login_web(client, web_user.username, "long-web-password")
    web_created = client.post(
        "/health-diary",
        data={
            "csrf_token": csrf,
            "entry_date": "2026-07-30",
            "severity": "轻微",
            "symptoms": "网页记录的乏力",
            "notes": "网页端先补水",
        },
        follow_redirects=False,
    )
    assert web_created.status_code in (301, 302, 303)
    assert HealthDiary.query.filter_by(
        user_id=web_user_id,
        notes="网页端先补水",
    ).count() == 1

    link_code = _generate_link_code(client, csrf)
    mini_client = app.test_client()
    temporary_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-cross-platform-diary",
    )
    linked = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {temporary_token}"},
    )
    assert linked.status_code == 200
    linked_token = linked.get_json()["data"]["session_token"]
    linked_headers = {"Authorization": f"Bearer {linked_token}"}

    consent = mini_client.post(
        "/mp/api/v1/health-consent",
        headers=linked_headers,
        json={
            "consent": True,
            "health_consent_version": "privacy-v1",
        },
    )
    assert consent.status_code == 200

    mini_list = mini_client.get(
        "/mp/api/v1/health/diary",
        headers=linked_headers,
    )
    assert mini_list.status_code == 200
    assert any(
        item["notes"] == "网页端先补水"
        for item in mini_list.get_json()["data"]["items"]
    )

    mini_created = mini_client.post(
        "/mp/api/v1/health/diary",
        headers=linked_headers,
        json={
            "entry_date": "2026-07-30",
            "severity": "mild",
            "symptoms": "小程序记录的口渴",
            "notes": "小程序端再补水",
        },
    )
    assert mini_created.status_code == 201
    assert HealthDiary.query.filter_by(
        user_id=web_user_id,
        notes="小程序端再补水",
    ).count() == 1

    web_list = client.get("/health-diary")
    assert web_list.status_code == 200
    html = web_list.get_data(as_text=True)
    assert "网页记录的乏力" in html
    assert "小程序记录的口渴" in html


def test_account_delete_wins_link_race_without_identity_residue(
    app,
    client,
    db_session,
    monkeypatch,
):
    """注销持有 source owner 锁时，绑定等待并在锁内重验后失败。"""
    from blueprints import mp_api
    from core.db_models import (
        MiniProgramIdentity,
        MiniProgramLinkChallenge,
        MiniProgramSession,
        User,
    )
    from core.extensions import db
    from core.time_utils import utcnow
    from services import cross_platform_identity as link_service

    _configure_wechat(app)
    target_user = User(username="delete_race_target", role="user")
    target_user.set_password("long-web-password")
    db_session.add(target_user)
    db_session.commit()
    target_user_id = int(target_user.id)
    csrf = _login_web(client, target_user.username, "long-web-password")
    link_code = _generate_link_code(client, csrf)
    target_challenge_id = int(MiniProgramLinkChallenge.query.one().id)

    mini_client = app.test_client()
    old_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-delete-link-race",
    )
    identity = MiniProgramIdentity.query.one()
    identity_id = int(identity.id)
    source_user_id = int(identity.user_id)
    identity_openid_hash = str(identity.openid_hash)
    db_session.add(
        MiniProgramLinkChallenge(
            user_id=source_user_id,
            code_hash="source-delete-race-challenge-hash",
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=10),
            auth_version_at_create=1,
        )
    )
    db_session.commit()
    db.session.remove()

    delete_locked = threading.Event()
    release_delete = threading.Event()
    link_waiting_for_source = threading.Event()
    outcomes = {}
    original_anonymize = mp_api._anonymize_miniprogram_owner
    original_owner_lock = link_service.push_owner_lock

    def blocked_anonymize(user):
        delete_locked.set()
        assert release_delete.wait(timeout=5)
        return original_anonymize(user)

    @contextmanager
    def observed_owner_lock(user_id):
        if (
            threading.current_thread().name == "link-after-delete"
            and int(user_id) == source_user_id
        ):
            link_waiting_for_source.set()
        with original_owner_lock(user_id):
            yield

    monkeypatch.setattr(
        mp_api,
        "_anonymize_miniprogram_owner",
        blocked_anonymize,
    )
    monkeypatch.setattr(
        link_service,
        "push_owner_lock",
        observed_owner_lock,
    )

    def delete_account():
        with app.test_client() as thread_client:
            outcomes["delete"] = thread_client.delete(
                "/mp/api/v1/me",
                headers={"Authorization": f"Bearer {old_token}"},
                json={
                    "confirm": True,
                    "user_id": source_user_id,
                    "wechat_code": "fresh-delete-code",
                },
            )

    def link_account():
        with app.app_context():
            try:
                outcomes["link"] = link_service.consume_account_link_challenge(
                    code=link_code,
                    identity_id=identity_id,
                    authenticated_user_id=source_user_id,
                    identity_openid_hash=identity_openid_hash,
                )
            except link_service.AccountLinkError as exc:
                outcomes["link_error"] = exc

    deleter = threading.Thread(target=delete_account, name="delete-before-link")
    linker = threading.Thread(target=link_account, name="link-after-delete")
    deleter.start()
    assert delete_locked.wait(timeout=5)
    linker.start()
    assert link_waiting_for_source.wait(timeout=5)
    assert linker.is_alive()

    release_delete.set()
    deleter.join(timeout=5)
    linker.join(timeout=5)

    assert not deleter.is_alive()
    assert not linker.is_alive()
    assert outcomes["delete"].status_code == 200
    assert "link" not in outcomes
    assert outcomes["link_error"].code == "miniprogram_session_required"

    db.session.remove()
    deleted_source = db.session.get(User, source_user_id)
    assert deleted_source.deleted_at is not None
    assert MiniProgramIdentity.query.filter_by(id=identity_id).count() == 0
    assert MiniProgramIdentity.query.filter_by(user_id=target_user_id).count() == 0
    assert MiniProgramSession.query.filter_by(user_id=source_user_id).count() == 0
    assert MiniProgramSession.query.filter_by(identity_id=identity_id).count() == 0
    assert MiniProgramLinkChallenge.query.filter_by(
        user_id=source_user_id,
    ).count() == 0
    untouched_target_challenge = db.session.get(
        MiniProgramLinkChallenge,
        target_challenge_id,
    )
    assert untouched_target_challenge.consumed_at is None
    assert untouched_target_challenge.consumed_identity_id is None


def test_link_refuses_to_merge_existing_private_data(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember, MiniProgramIdentity, User

    _configure_wechat(app)
    web_user = User(username="private_target_user", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    target_user_id = int(web_user.id)
    csrf = _login_web(client, web_user.username, "long-web-password")
    link_code = _generate_link_code(client, csrf)

    mini_client = app.test_client()
    old_token = _wechat_login(app, mini_client, monkeypatch, "openid-private-data")
    identity = MiniProgramIdentity.query.one()
    source_user_id = int(identity.user_id)
    db_session.add(
        FamilyMember(
            user_id=source_user_id,
            name="已有家人",
            relation="家人",
        )
    )
    db_session.commit()

    response = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {old_token}"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "source_account_has_data"
    db_session.refresh(identity)
    assert identity.user_id == source_user_id
    assert db_session.get(User, source_user_id).deleted_at is None
    assert db_session.get(User, target_user_id).deleted_at is None


def test_link_never_retires_legacy_web_user_with_wx_prefix(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, User

    _configure_wechat(app)
    web_user = User(username="legacy_prefix_target", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    target_user_id = int(web_user.id)
    csrf = _login_web(client, web_user.username, "long-web-password")
    link_code = _generate_link_code(client, csrf)

    mini_client = app.test_client()
    old_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-legacy-web-prefix",
    )
    identity = MiniProgramIdentity.query.one()
    source_user = db_session.get(User, identity.user_id)
    source_user.username = "wx_legacy_web_user"
    source_user.account_origin = "web"
    db_session.commit()
    source_user_id = int(source_user.id)
    identity_id = int(identity.id)

    response = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {old_token}"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "source_account_has_data"
    db_session.expire_all()
    preserved_source = db_session.get(User, source_user_id)
    preserved_identity = db_session.get(MiniProgramIdentity, identity_id)
    assert preserved_source.username == "wx_legacy_web_user"
    assert preserved_source.account_origin == "web"
    assert preserved_source.deleted_at is None
    assert preserved_identity.user_id == source_user_id
    assert db_session.get(User, target_user_id).deleted_at is None


def test_binding_anonymizes_source_audit_links(
    app,
    client,
    db_session,
    monkeypatch,
):
    import json

    from core.db_models import AuditLog, MiniProgramIdentity, User
    from core.extensions import db

    _configure_wechat(app)
    app.config["FEATURE_AUDIT_LOGS"] = True
    web_user = User(username="audit_target_user", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    target_user_id = int(web_user.id)
    csrf = _login_web(client, web_user.username, "long-web-password")
    link_code = _generate_link_code(client, csrf)

    mini_client = app.test_client()
    old_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-audit-source",
    )
    identity = MiniProgramIdentity.query.one()
    source_user_id = int(identity.user_id)
    rejected = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": "11111111"},
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert rejected.status_code == 400

    linked = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert linked.status_code == 200

    db.session.remove()
    assert AuditLog.query.filter_by(actor_id=source_user_id).count() == 0
    rejected_audit = AuditLog.query.filter_by(
        action="miniprogram_link_challenge_rejected",
    ).one()
    assert rejected_audit.actor_id is None
    assert rejected_audit.actor_role == "anonymous_miniprogram_identity"
    assert rejected_audit.resource_type is None
    assert rejected_audit.resource_id is None
    assert rejected_audit.extra_data is None
    assert rejected_audit.ip_address is None
    assert rejected_audit.user_agent is None
    assert rejected_audit.request_id is None

    linked_audit = AuditLog.query.filter_by(
        action="miniprogram_account_linked",
    ).one()
    assert linked_audit.actor_id == target_user_id
    assert linked_audit.resource_type == "miniprogram_identity"
    linked_extra = json.loads(linked_audit.extra_data)
    assert linked_extra["source_was_temporary"] is True
    assert "target_user_id" not in linked_extra
    assert set(linked_extra) <= {
        "source_was_temporary",
        "ip_source",
        "via_trusted_proxy",
        "ip_prefix",
    }


def test_expired_code_and_legacy_api_token_are_rejected(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramLinkChallenge, User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    _configure_wechat(app)
    web_user = User(username="expired_target_user", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    csrf = _login_web(client, web_user.username, "long-web-password")
    link_code = _generate_link_code(client, csrf)

    challenge = MiniProgramLinkChallenge.query.one()
    challenge.expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    legacy_token = create_api_token(web_user.id, name="legacy")

    legacy_response = client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {legacy_token}"},
    )
    assert legacy_response.status_code == 403
    assert legacy_response.get_json()["error"] == "miniprogram_session_required"

    mini_client = app.test_client()
    session_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-expired-code",
    )
    expired_response = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert expired_response.status_code == 400
    assert expired_response.get_json()["error"] == "invalid_link_code"


def test_account_link_page_is_private_and_wrong_password_creates_no_code(
    app,
    client,
    db_session,
):
    from core.db_models import MiniProgramLinkChallenge, User

    _configure_wechat(app)
    web_user = User(username="reauth_target_user", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    csrf = _login_web(client, web_user.username, "long-web-password")

    page = client.get("/account-link")
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert page.headers["Pragma"] == "no-cache"
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    response = client.post(
        "/account-link/code",
        data={
            "csrf_token": csrf,
            "current_password": "wrong-current-password",
            "miniprogram_privacy_consent": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert MiniProgramLinkChallenge.query.count() == 0
    with client.session_transaction() as flask_session:
        assert "last_mini_link_code" not in flask_session


def test_account_link_allows_duplicate_pending_phone(
    app,
    client,
    db_session,
):
    from core.db_models import User

    first_user = User(
        username="pending_phone_first",
        role="user",
        phone_normalized="+8613900000001",
    )
    first_user.set_password("first-user-password")
    second_user = User(
        username="pending_phone_second",
        role="user",
    )
    second_user.set_password("second-user-password")
    db_session.add_all((first_user, second_user))
    db_session.commit()

    csrf = _login_web(
        client,
        "pending_phone_second",
        "second-user-password",
    )
    responses = [
        client.post(
            "/account-link/phone",
            data={
                "csrf_token": csrf,
                "phone": "13900000001",
                "current_password": "second-user-password",
            },
            follow_redirects=False,
        )
        for _index in range(2)
    ]

    assert all(
        response.status_code in (301, 302, 303)
        for response in responses
    )
    assert all(
        response.headers["Location"].endswith("/account-link")
        for response in responses
    )
    db_session.expire_all()
    shared_phone_users = User.query.filter_by(
        phone_normalized="+8613900000001",
    ).all()
    assert {user.username for user in shared_phone_users} == {
        "pending_phone_first",
        "pending_phone_second",
    }
    assert all(user.phone_verified_at is None for user in shared_phone_users)


def test_password_change_revokes_existing_link_code(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramLinkChallenge, User

    _configure_wechat(app)
    web_user = User(username="password_change_target", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    csrf = _login_web(client, web_user.username, "long-web-password")
    link_code = _generate_link_code(client, csrf)

    changed = client.post(
        "/profile",
        data={
            "csrf_token": csrf,
            "form_id": "password",
            "old_password": "long-web-password",
            "new_password": "new-long-web-password",
            "confirm_password": "new-long-web-password",
        },
        follow_redirects=False,
    )

    assert changed.status_code in (301, 302, 303)
    challenge = MiniProgramLinkChallenge.query.one()
    assert challenge.revoked_at is not None

    mini_client = app.test_client()
    session_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-password-change",
    )
    response = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": link_code},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_link_code"


def test_password_change_detaches_wechat_until_fresh_relink(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, User
    from core.extensions import db

    _configure_wechat(app)
    web_user = User(username="password_relink_target", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    target_user_id = int(web_user.id)

    csrf = _login_web(client, web_user.username, "long-web-password")
    first_link_code = _generate_link_code(client, csrf)
    mini_client = app.test_client()
    first_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-password-relink",
    )
    linked = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": first_link_code},
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert linked.status_code == 200
    linked_token = linked.get_json()["data"]["session_token"]
    identity_id = int(MiniProgramIdentity.query.one().id)

    changed = client.post(
        "/profile",
        data={
            "csrf_token": csrf,
            "form_id": "password",
            "old_password": "long-web-password",
            "new_password": "new-long-web-password",
            "confirm_password": "new-long-web-password",
        },
        follow_redirects=False,
    )
    assert changed.status_code in (301, 302, 303)

    stale_me = mini_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {linked_token}"},
    )
    assert stale_me.status_code == 401

    blank_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-password-relink",
    )
    db.session.remove()
    detached_identity = db.session.get(MiniProgramIdentity, identity_id)
    placeholder_user = db.session.get(User, detached_identity.user_id)
    target_user = db.session.get(User, target_user_id)
    assert detached_identity.user_id != target_user_id
    assert detached_identity.binding_auth_version == placeholder_user.auth_version
    assert placeholder_user.account_origin == "miniprogram_placeholder"
    assert placeholder_user.deleted_at is None
    assert target_user.username == "password_relink_target"
    assert target_user.auth_version == 2
    assert target_user.deleted_at is None

    relink_client = app.test_client()
    relink_csrf = _login_web(
        relink_client,
        target_user.username,
        "new-long-web-password",
    )
    fresh_link_code = _generate_link_code(
        relink_client,
        relink_csrf,
        password="new-long-web-password",
    )
    relinked = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": fresh_link_code},
        headers={"Authorization": f"Bearer {blank_token}"},
    )
    assert relinked.status_code == 200

    db.session.remove()
    restored_identity = db.session.get(MiniProgramIdentity, identity_id)
    restored_target = db.session.get(User, target_user_id)
    assert restored_identity.user_id == target_user_id
    assert restored_identity.binding_auth_version == restored_target.auth_version
    assert restored_target.deleted_at is None


def test_password_change_allows_different_wechat_to_replace_stale_identity(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, User
    from core.extensions import db

    _configure_wechat(app)
    web_user = User(username="different_wechat_target", role="user")
    web_user.set_password("long-web-password")
    db_session.add(web_user)
    db_session.commit()
    target_user_id = int(web_user.id)

    csrf = _login_web(client, web_user.username, "long-web-password")
    first_code = _generate_link_code(client, csrf)
    first_mini_client = app.test_client()
    first_token = _wechat_login(
        app,
        first_mini_client,
        monkeypatch,
        "openid-original-wechat",
    )
    first_link = first_mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": first_code},
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert first_link.status_code == 200
    first_identity_id = int(MiniProgramIdentity.query.one().id)
    first_linked_token = first_link.get_json()["data"]["session_token"]

    changed = client.post(
        "/profile",
        data={
            "csrf_token": csrf,
            "form_id": "password",
            "old_password": "long-web-password",
            "new_password": "new-long-web-password",
            "confirm_password": "new-long-web-password",
        },
        follow_redirects=False,
    )
    assert changed.status_code in (301, 302, 303)

    second_mini_client = app.test_client()
    second_token = _wechat_login(
        app,
        second_mini_client,
        monkeypatch,
        "openid-replacement-wechat",
    )
    second_identity = MiniProgramIdentity.query.filter(
        MiniProgramIdentity.id != first_identity_id
    ).one()
    second_identity_id = int(second_identity.id)

    relink_client = app.test_client()
    relink_csrf = _login_web(
        relink_client,
        web_user.username,
        "new-long-web-password",
    )
    replacement_code = _generate_link_code(
        relink_client,
        relink_csrf,
        password="new-long-web-password",
    )
    replaced = second_mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": replacement_code},
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert replaced.status_code == 200

    db.session.remove()
    assert db.session.get(MiniProgramIdentity, first_identity_id) is None
    replacement_identity = db.session.get(
        MiniProgramIdentity,
        second_identity_id,
    )
    target_user = db.session.get(User, target_user_id)
    assert replacement_identity.user_id == target_user_id
    assert replacement_identity.binding_auth_version == target_user.auth_version

    stale_me = first_mini_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {first_linked_token}"},
    )
    assert stale_me.status_code == 401

    original_wechat_token = _wechat_login(
        app,
        first_mini_client,
        monkeypatch,
        "openid-original-wechat",
    )
    db.session.remove()
    original_identity = MiniProgramIdentity.query.filter(
        MiniProgramIdentity.id != second_identity_id
    ).one()
    original_placeholder = db.session.get(User, original_identity.user_id)
    assert original_identity.user_id != target_user_id
    assert original_placeholder.account_origin == "miniprogram_placeholder"
    original_me = first_mini_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {original_wechat_token}"},
    )
    assert original_me.status_code == 200
    assert original_me.get_json()["data"]["display_name"] == "微信用户"


def test_invalid_link_code_failures_lock_wechat_identity(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity
    from core.extensions import db

    _configure_wechat(app)
    app.config["ACCOUNT_LINK_FAILURE_MAX"] = 5
    mini_client = app.test_client()
    session_token = _wechat_login(
        app,
        mini_client,
        monkeypatch,
        "openid-link-lock",
    )
    headers = {"Authorization": f"Bearer {session_token}"}

    for index in range(5):
        response = mini_client.post(
            "/mp/api/v1/auth/link-account",
            json={"code": f"{index + 1:08d}"},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_link_code"

    locked = mini_client.post(
        "/mp/api/v1/auth/link-account",
        json={"code": "99999999"},
        headers=headers,
    )
    assert locked.status_code == 429
    assert locked.get_json()["error"] == "account_link_temporarily_locked"

    db.session.remove()
    identity = MiniProgramIdentity.query.one()
    assert identity.link_failed_count == 5
    assert identity.link_locked_until is not None


def test_registration_rejects_phone_shaped_username(app, client, db_session):
    from core.db_models import User

    csrf = "phone-shaped-username-csrf"
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf
    response = client.post(
        "/register",
        data={
            "username": "13800138000",
            "password": "long-registration-password",
            "confirm_password": "long-registration-password",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert User.query.filter_by(username="13800138000").first() is None


def test_registration_rejects_internal_username_namespaces(
    app,
    db_session,
):
    from core.db_models import User

    for index, username in enumerate(
        ("wx_public_user", "retired_wx_public", "deleted_mp_public")
    ):
        test_client = app.test_client()
        csrf = f"reserved-username-csrf-{index}"
        with test_client.session_transaction() as flask_session:
            flask_session["_csrf_token"] = csrf
        response = test_client.post(
            "/register",
            data={
                "username": username,
                "password": "long-registration-password",
                "confirm_password": "long-registration-password",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code in (301, 302, 303)
        assert User.query.filter_by(username=username).first() is None


def test_registration_hides_phone_occupancy_in_response(
    app,
    db_session,
):
    from core.db_models import User

    occupied = User(
        username="occupied_phone_owner",
        phone_normalized="+8613900000001",
    )
    occupied.set_password("existing-password-long")
    db_session.add(occupied)
    db_session.commit()

    def submit(username, phone):
        test_client = app.test_client()
        csrf = f"csrf-{username}"
        with test_client.session_transaction() as flask_session:
            flask_session["_csrf_token"] = csrf
        response = test_client.post(
            "/register",
            data={
                "username": username,
                "password": "registration-password-long",
                "confirm_password": "registration-password-long",
                "phone": phone,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        with test_client.session_transaction() as flask_session:
            flashes = list(flask_session.get("_flashes") or [])
        return response, flashes

    occupied_response, occupied_flashes = submit(
        "phone_probe_occupied",
        "13900000001",
    )
    available_response, available_flashes = submit(
        "phone_probe_available",
        "13900000002",
    )

    assert occupied_response.status_code == available_response.status_code
    assert occupied_response.headers["Location"] == available_response.headers["Location"]
    assert occupied_flashes == available_flashes
    assert "手机号" not in occupied_flashes[0][1]
    duplicate_user = User.query.filter_by(username="phone_probe_occupied").one()
    assert duplicate_user.phone_normalized == "+8613900000001"
    assert duplicate_user.phone_verified_at is None
    assert User.query.filter_by(username="phone_probe_available").one()
    assert User.query.filter_by(
        phone_normalized="+8613900000001",
    ).count() == 2

    def attempt_pending_phone_login(phone, password):
        test_client = app.test_client()
        csrf = f"csrf-{phone}-{password}"
        with test_client.session_transaction() as flask_session:
            flask_session["_csrf_token"] = csrf
        response = test_client.post(
            "/login",
            data={
                "username": phone,
                "password": password,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        with test_client.session_transaction() as flask_session:
            flashes = list(flask_session.get("_flashes") or [])
            logged_in = "_user_id" in flask_session
        return response.status_code, flashes, logged_in

    occupied_phone_result = attempt_pending_phone_login(
        "13900000001",
        "registration-password-long",
    )
    available_phone_result = attempt_pending_phone_login(
        "13900000002",
        "registration-password-long",
    )
    existing_owner_result = attempt_pending_phone_login(
        "13900000001",
        "existing-password-long",
    )
    assert occupied_phone_result == available_phone_result
    assert occupied_phone_result == existing_owner_result
    assert occupied_phone_result[0] == 200
    assert occupied_phone_result[2] is False


def test_registration_post_has_dedicated_rate_limit(app, db_session):
    app.config["RATE_LIMIT_REGISTER"] = "2 per hour"
    test_client = app.test_client()
    csrf = "register-rate-limit-csrf"
    with test_client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf

    responses = [
        test_client.post(
            "/register",
            data={
                "username": f"rate_limit_reg_{index}",
                "password": "registration-password-long",
                "confirm_password": "registration-password-long",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        for index in range(3)
    ]

    assert responses[0].status_code in (301, 302, 303)
    assert responses[1].status_code in (301, 302, 303)
    assert responses[2].status_code == 429
