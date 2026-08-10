# -*- coding: utf-8 -*-
from contextlib import contextmanager
from datetime import timedelta
import threading


@contextmanager
def _capture_sql(engine):
    """捕获测试窗口内的 SQL，避免把准备数据查询计入性能断言。"""
    from sqlalchemy import event

    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


def test_mp_api_requires_token(client):
    resp = client.get("/mp/api/v1/me")
    assert resp.status_code == 401


def test_mp_api_me_and_patch(app, client, db_session):
    from core.db_models import ApiToken, User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    app.config["WXPUSHER_APP_TOKEN"] = "AT_private-test-token"

    with app.app_context():
        user = User(username="mp_user", role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        user_id = user.id

        plain = create_api_token(user_id, name="test")

    resp = client.get("/mp/api/v1/me", headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["data"]["username"] == "mp_user"
    assert body["data"]["wxpusher_feature_enabled"] is True
    assert body["data"]["wxpusher_available"] is True
    assert body["data"]["required_wxpusher_consent_version"] == app.config[
        "WX_MINIPROGRAM_PRIVACY_VERSION"
    ]
    assert body["data"]["wxpusher_reconsent_required"] is False
    assert "AT_private-test-token" not in resp.get_data(as_text=True)

    # update push settings
    resp2 = client.patch(
        "/mp/api/v1/me",
        json={
            "wxpusher_uid": "UID_X",
            "push_enabled": True,
            "wxpusher_consent": True,
            "wxpusher_consent_version": app.config["WX_MINIPROGRAM_PRIVACY_VERSION"],
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp2.status_code == 200
    body2 = resp2.get_json()
    assert body2["success"] is True
    assert body2["data"]["wxpusher_uid"] == "UID_X"
    assert body2["data"]["push_enabled"] is True

    # revoke token => unauthorized
    with app.app_context():
        token_row = ApiToken.query.filter_by(user_id=user_id).first()
        # 成功写请求仍在业务事务中同步持久化到期的凭证活跃时间。
        assert token_row.last_used_at is not None
        token_row.revoked_at = utcnow()
        db_session.commit()

    resp3 = client.get("/mp/api/v1/me", headers={"Authorization": f"Bearer {plain}"})
    assert resp3.status_code == 401


def test_mp_api_wxpusher_disabled_masks_uid_and_rejects_collection(
    app,
    client,
    db_session,
):
    from core.db_models import User
    from core.usage import create_api_token

    app.config["FEATURE_WXPUSHER"] = False
    app.config["WXPUSHER_APP_TOKEN"] = ""
    with app.app_context():
        user = User(
            username="mp_wxpusher_disabled",
            role="user",
            wxpusher_uid="UID_HISTORICAL",
            push_enabled=True,
        )
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        user_id = user.id
        plain = create_api_token(user_id, name="disabled-feature")

    headers = {"Authorization": f"Bearer {plain}"}
    response = client.get("/mp/api/v1/me", headers=headers)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["wxpusher_feature_enabled"] is False
    assert data["wxpusher_available"] is False
    assert data["wxpusher_uid"] is None
    assert data["push_enabled"] is False

    for payload in (
        {},
        {"wxpusher_uid": ""},
        {"wxpusher_uid": "UID_NEW"},
        {"push_enabled": False},
        {"push_enabled": True},
        {"wxpusher_uid": "UID_NEW", "push_enabled": True},
        {"wxpusher_consent": False},
        {"wxpusher_consent": True},
        {"wxpusher_consent_version": "privacy-v3"},
    ):
        rejected = client.patch("/mp/api/v1/me", headers=headers, json=payload)
        assert rejected.status_code == 403
        assert rejected.get_json()["error"] == "wxpusher_disabled"

    with app.app_context():
        unchanged = db_session.get(User, user_id)
        assert unchanged.wxpusher_uid == "UID_HISTORICAL"
        assert unchanged.push_enabled is True


def test_api_token_get_persists_last_used_in_tail_transaction(app, client, db_session):
    from core.db_models import ApiToken, User
    from core.extensions import db
    from core.time_utils import utcnow
    from core.usage import create_api_token

    user = User(username="api-last-used", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(user.id, name="last-used")
    record = ApiToken.query.filter_by(user_id=user.id).one()
    old_value = utcnow() - timedelta(days=1)
    record.last_used_at = old_value
    db_session.commit()
    record_id = record.id

    response = client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert response.status_code == 200
    db.session.remove()
    refreshed = db.session.get(ApiToken, record_id)
    assert refreshed.last_used_at > old_value.replace(tzinfo=None)


def test_sensitive_get_scope_denial_does_not_commit_last_used(app, client, db_session):
    from core.db_models import ApiToken, User
    from core.extensions import db
    from core.time_utils import utcnow
    from core.usage import create_api_token

    user = User(username="scope-last-used", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(user.id, name="read-only", scopes=["miniprogram:read"])
    record = ApiToken.query.filter_by(user_id=user.id).one()
    old_value = utcnow() - timedelta(days=1)
    record.last_used_at = old_value
    db_session.commit()
    record_id = record.id

    denied = client.get(
        "/mp/api/v1/health/diary",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert denied.status_code == 403
    db.session.remove()
    refreshed = db.session.get(ApiToken, record_id)
    assert refreshed.last_used_at == old_value.replace(tzinfo=None)


def test_invalid_patch_does_not_commit_credential_last_used(app, client, db_session):
    from core.db_models import ApiToken, User
    from core.extensions import db
    from core.time_utils import utcnow
    from core.usage import create_api_token

    user = User(username="invalid-patch-last-used", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(user.id, name="write-token")
    record = ApiToken.query.filter_by(user_id=user.id).one()
    old_value = utcnow() - timedelta(days=1)
    record.last_used_at = old_value
    db_session.commit()
    record_id = record.id

    denied = client.patch(
        "/mp/api/v1/me",
        json={"push_enabled": "yes"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert denied.status_code == 400
    db.session.remove()
    refreshed = db.session.get(ApiToken, record_id)
    assert refreshed.last_used_at == old_value.replace(tzinfo=None)


def test_miniprogram_session_get_persists_last_used(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramSession
    from core.extensions import db
    from core.time_utils import utcnow

    class WechatResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"openid": "last-used-openid", "session_key": "not-stored"}

    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-secret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
    )
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: WechatResponse(),
    )
    login = client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "wx-code", "privacy_consent_version": "privacy-v1"},
    )
    assert login.status_code == 200
    token = login.get_json()["data"]["session_token"]
    record = MiniProgramSession.query.one()
    old_value = utcnow() - timedelta(days=1)
    record.last_used_at = old_value
    db_session.commit()

    response = client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    record_id = record.id
    db.session.remove()
    refreshed = db.session.get(MiniProgramSession, record_id)
    assert refreshed.last_used_at > old_value.replace(tzinfo=None)


def test_credential_verifiers_fetch_joined_owner_context_in_one_select(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import User
    from core.extensions import db
    from core.usage import create_api_token, verify_api_token
    from services.miniprogram_auth import verify_miniprogram_session

    class WechatResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"openid": "joined-auth-openid", "session_key": "not-stored"}

    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-secret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
    )
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: WechatResponse(),
    )
    user = User(username="joined-api-token", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    user_id = int(user.id)
    api_plain = create_api_token(user_id, name="joined-owner")
    login = client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "joined-code", "privacy_consent_version": "privacy-v1"},
    )
    session_plain = login.get_json()["data"]["session_token"]

    db.session.remove()
    with _capture_sql(db.engine) as api_sql:
        api_record = verify_api_token(api_plain)
    api_selects = [item for item in api_sql if item.lstrip().upper().startswith("SELECT")]
    assert len(api_selects) == 1
    assert getattr(api_record, "_verified_user").id == user_id

    db.session.remove()
    with _capture_sql(db.engine) as session_sql:
        session_record = verify_miniprogram_session(session_plain)
    session_selects = [item for item in session_sql if item.lstrip().upper().startswith("SELECT")]
    assert len(session_selects) == 1
    assert getattr(session_record, "_verified_user").id == session_record.user_id
    assert getattr(session_record, "_verified_identity").id == session_record.identity_id


def test_api_token_one_hundred_gets_write_last_used_at_most_once(
    app,
    client,
    db_session,
):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from core.db_models import ApiToken, User
    from core.extensions import db, limiter
    from core.time_utils import utcnow
    from core.usage import create_api_token

    app.config["RATE_LIMIT_MP_READ"] = "1000 per minute"
    limiter.reset()
    user = User(username="throttled-api-token", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(user.id, name="throttled-reads")
    record = ApiToken.query.filter_by(user_id=user.id).one()
    record.last_used_at = utcnow() - timedelta(days=1)
    db_session.commit()
    db.session.remove()

    commits = []

    def record_commit(_session):
        commits.append(True)

    event.listen(Session, "after_commit", record_commit)
    try:
        with _capture_sql(db.engine) as statements:
            responses = [
                client.get(
                    "/mp/api/v1/me",
                    headers={"Authorization": f"Bearer {plain}"},
                )
                for _index in range(100)
            ]
    finally:
        event.remove(Session, "after_commit", record_commit)
        limiter.reset()

    assert all(response.status_code == 200 for response in responses)
    normalized = [" ".join(item.lower().split()) for item in statements]
    selects = [item for item in normalized if item.startswith("select")]
    last_used_updates = [
        item
        for item in normalized
        if item.startswith("update api_tokens set") and "last_used_at" in item
    ]
    assert len(selects) == 100
    assert len(last_used_updates) == 1
    assert len(commits) == 1


def test_session_get_inside_throttle_window_has_no_update_or_commit(
    app,
    client,
    db_session,
    monkeypatch,
):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from core.db_models import MiniProgramSession
    from core.extensions import db
    from core.time_utils import utcnow

    class WechatResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"openid": "fresh-session-openid", "session_key": "not-stored"}

    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-secret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
    )
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: WechatResponse(),
    )
    login = client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "fresh-session-code", "privacy_consent_version": "privacy-v1"},
    )
    token = login.get_json()["data"]["session_token"]
    record = MiniProgramSession.query.one()
    record.last_used_at = utcnow()
    db_session.commit()
    db.session.remove()
    commits = []

    def record_commit(_session):
        commits.append(True)

    event.listen(Session, "after_commit", record_commit)
    try:
        with _capture_sql(db.engine) as statements:
            response = client.get(
                "/mp/api/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        event.remove(Session, "after_commit", record_commit)

    assert response.status_code == 200
    normalized = [" ".join(item.lower().split()) for item in statements]
    assert len([item for item in normalized if item.startswith("select")]) == 1
    assert not [
        item
        for item in normalized
        if item.startswith("update miniprogram_sessions set") and "last_used_at" in item
    ]
    assert commits == []


def test_locked_write_reauthorization_rejects_concurrent_token_revocation(
    app,
    db_session,
    monkeypatch,
):
    from blueprints import mp_api
    from core.db_models import ApiToken, User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    app.config["WXPUSHER_APP_TOKEN"] = "AT_test-only"
    user = User(username="concurrent-token-revoke", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(user.id, name="concurrent-revoke")
    record = ApiToken.query.filter_by(user_id=user.id).one()
    record_id = int(record.id)
    entered_lock = threading.Event()
    continue_request = threading.Event()
    outcome = {}

    @contextmanager
    def paused_owner_lock(_user_id):
        entered_lock.set()
        assert continue_request.wait(timeout=5)
        yield

    monkeypatch.setattr(mp_api, "push_owner_lock", paused_owner_lock)

    def update_push_setting():
        with app.test_client() as thread_client:
            outcome["response"] = thread_client.patch(
                "/mp/api/v1/me",
                headers={"Authorization": f"Bearer {plain}"},
                json={"push_enabled": False},
            )

    request_thread = threading.Thread(target=update_push_setting)
    request_thread.start()
    assert entered_lock.wait(timeout=5)
    record = db_session.get(ApiToken, record_id)
    record.revoked_at = utcnow()
    db_session.commit()
    continue_request.set()
    request_thread.join(timeout=5)

    assert not request_thread.is_alive()
    assert outcome["response"].status_code == 401
    assert outcome["response"].get_json()["error"] == "unauthorized"


def test_elder_delete_rechecks_health_consent_and_scope_after_owner_lock(
    app,
    client,
    db_session,
    monkeypatch,
):
    """撤回发生在 owner 锁等待期间时，删除必须在二次授权处停止。"""
    from blueprints import mp_api
    from core.db_models import ApiToken, FamilyMember, Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from core.usage import create_api_token

    user = User(
        username="elder-delete-health-withdraw-race",
        role="user",
        health_sensitive_consent_version=app.config[
            "WX_MINIPROGRAM_PRIVACY_VERSION"
        ],
        health_sensitive_consented_at=utcnow(),
    )
    user.set_password("pw123456")
    db_session.add(user)
    db_session.flush()
    member = FamilyMember(
        user_id=user.id,
        name="撤回竞态家人",
        relation="家人",
        age=70,
    )
    db_session.add(member)
    db_session.flush()
    pair = Pair(
        caregiver_id=user.id,
        member_id=member.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code="elder-withdraw-race",
        short_code="78787878",
        short_code_hash=hash_short_code("78787878"),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    pair_id = int(pair.id)
    plain = create_api_token(
        user.id,
        name="health-withdraw-race",
        scopes=["miniprogram:read", "miniprogram:write", "miniprogram:sensitive"],
    )
    token_id = int(ApiToken.query.filter_by(user_id=user.id).one().id)
    headers = {"Authorization": f"Bearer {plain}"}
    entered_owner_lock = threading.Event()
    continue_delete = threading.Event()
    outcome = {}

    @contextmanager
    def paused_owner_lock(_user_id):
        entered_owner_lock.set()
        assert continue_delete.wait(timeout=5)
        yield

    monkeypatch.setattr(mp_api, "push_owner_lock", paused_owner_lock)

    def delete_elder():
        with app.test_client() as thread_client:
            outcome["response"] = thread_client.delete(
                f"/mp/api/v1/elders/{pair_id}",
                headers=headers,
            )

    worker = threading.Thread(target=delete_elder)
    worker.start()
    assert entered_owner_lock.wait(timeout=5)
    withdrawn = client.delete("/mp/api/v1/health-consent", headers=headers)
    assert withdrawn.status_code == 200
    continue_delete.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert outcome["response"].status_code == 428
    assert outcome["response"].get_json()["error"] == (
        "health_sensitive_consent_required"
    )
    db_session.expire_all()
    assert db_session.get(Pair, pair_id).status == "active"

    # 二次鉴权也要复核 sensitive scope，防止锁等待期间凭证权限被收窄。
    refreshed_user = db_session.get(User, user.id)
    refreshed_user.health_sensitive_consent_version = app.config[
        "WX_MINIPROGRAM_PRIVACY_VERSION"
    ]
    refreshed_user.health_sensitive_consented_at = utcnow()
    db_session.commit()

    @contextmanager
    def narrow_scope_before_locked_reauth(_user_id):
        token_record = db_session.get(ApiToken, token_id)
        token_record.scopes = "miniprogram:read miniprogram:write"
        db_session.commit()
        yield

    monkeypatch.setattr(mp_api, "push_owner_lock", narrow_scope_before_locked_reauth)
    scope_denied = client.delete(
        f"/mp/api/v1/elders/{pair_id}",
        headers=headers,
    )
    assert scope_denied.status_code == 403
    assert scope_denied.get_json()["error"] == "insufficient_scope"
    db_session.expire_all()
    assert db_session.get(Pair, pair_id).status == "active"


def test_api_token_requires_expiry_and_sensitive_scope(app, client, db_session):
    from datetime import timedelta

    from core.db_models import ApiToken, User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    user = User(username="scoped_token_user", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(
        user.id,
        name="read-only",
        scopes=["miniprogram:read"],
    )
    headers = {"Authorization": f"Bearer {plain}"}

    assert client.get("/mp/api/v1/me", headers=headers).status_code == 200
    denied = client.get("/mp/api/v1/health/diary", headers=headers)
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "insufficient_scope"

    record = ApiToken.query.filter_by(user_id=user.id).one()
    record.expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert client.get("/mp/api/v1/me", headers=headers).status_code == 401

    # 迁移前的无期限 Token 必须轮换，不能继续访问敏感数据。
    record.expires_at = None
    db_session.commit()
    assert client.get("/mp/api/v1/me", headers=headers).status_code == 401

    record.expires_at = utcnow() + timedelta(days=1)
    record.privacy_consent_version = "outdated-privacy-version"
    db_session.commit()
    privacy_refresh = client.get("/mp/api/v1/me", headers=headers)
    assert privacy_refresh.status_code == 428
    assert privacy_refresh.get_json()["data"]["required_privacy_consent_version"]


def test_health_sensitive_consent_is_independent_versioned_and_persisted_in_utc(
    app,
    client,
    db_session,
    monkeypatch,
):
    """一般隐私同意不能代替健康回执，版本升级后也必须重新确认。"""
    from datetime import datetime, timezone

    from core.db_models import ApiToken, User
    from core.extensions import db
    from core.usage import create_api_token

    app.config["WX_MINIPROGRAM_PRIVACY_VERSION"] = "privacy-health-v1"
    user = User(username="health-consent-independent", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    user_id = int(user.id)
    plain = create_api_token(
        user_id,
        name="health-consent-token",
        scopes=["miniprogram:read", "miniprogram:write", "miniprogram:sensitive"],
    )
    token_record = ApiToken.query.filter_by(user_id=user_id).one()
    assert token_record.privacy_consent_version == "privacy-health-v1"
    headers = {"Authorization": f"Bearer {plain}"}

    me_before = client.get("/mp/api/v1/me", headers=headers)
    assert me_before.status_code == 200
    before_data = me_before.get_json()["data"]
    assert before_data["required_health_consent_version"] == "privacy-health-v1"
    assert before_data["health_consent_current"] is False
    assert before_data["health_consented_at"] is None
    blocked = client.get("/mp/api/v1/elders", headers=headers)
    assert blocked.status_code == 428
    assert blocked.get_json()["error"] == "health_sensitive_consent_required"

    for payload, expected_error in (
        ({}, "health_sensitive_consent_required"),
        ({"consent": False, "health_consent_version": "privacy-health-v1"},
         "health_sensitive_consent_required"),
        ({"consent": "true", "health_consent_version": "privacy-health-v1"},
         "health_sensitive_consent_required"),
        ({"consent": True}, "missing_health_consent_version"),
        ({"consent": True, "health_consent_version": "privacy-health-old"},
         "health_consent_version_mismatch"),
    ):
        rejected = client.post(
            "/mp/api/v1/health-consent",
            headers=headers,
            json=payload,
        )
        assert rejected.status_code == 400
        assert rejected.get_json()["error"] == expected_error

    fixed_utc = datetime(2026, 7, 19, 4, 5, 6, tzinfo=timezone.utc)
    monkeypatch.setattr("blueprints.mp_api.utcnow", lambda: fixed_utc)
    accepted = client.post(
        "/mp/api/v1/health-consent",
        headers=headers,
        json={
            "consent": True,
            "health_consent_version": "privacy-health-v1",
        },
    )
    assert accepted.status_code == 200
    accepted_data = accepted.get_json()["data"]
    assert accepted_data["health_consent_current"] is True
    assert accepted_data["health_consented_at"] == "2026-07-19T04:05:06+00:00"
    db.session.remove()
    stored = db.session.get(User, user_id)
    assert stored.health_sensitive_consent_version == "privacy-health-v1"
    assert stored.health_sensitive_consented_at == fixed_utc.replace(tzinfo=None)

    app.config["WX_MINIPROGRAM_PRIVACY_VERSION"] = "privacy-health-v2"
    token_record = ApiToken.query.filter_by(user_id=user_id).one()
    token_record.privacy_consent_version = "privacy-health-v2"
    db.session.commit()
    upgraded = client.get("/mp/api/v1/health-consent", headers=headers)
    assert upgraded.status_code == 200
    assert upgraded.get_json()["data"]["health_consent_current"] is False
    blocked_again = client.get("/mp/api/v1/elders", headers=headers)
    assert blocked_again.status_code == 428
    assert blocked_again.get_json()["data"]["required_health_consent_version"] == (
        "privacy-health-v2"
    )
    refreshed = client.post(
        "/mp/api/v1/health-consent",
        headers=headers,
        json={"consent": True, "health_consent_version": "privacy-health-v2"},
    )
    assert refreshed.status_code == 200
    assert refreshed.get_json()["data"]["health_consent_current"] is True


def test_health_consent_post_requires_sensitive_write_scope(
    app,
    client,
    db_session,
):
    """只读 Token 和缺少 sensitive scope 的 Token 都不能代用户授权。"""
    from core.db_models import User
    from core.usage import create_api_token

    required_version = app.config["WX_MINIPROGRAM_PRIVACY_VERSION"]
    user = User(username="health-consent-scope", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    user_id = int(user.id)
    read_only = create_api_token(
        user_id,
        name="health-read-only",
        scopes=["miniprogram:read", "miniprogram:sensitive"],
    )
    no_sensitive = create_api_token(
        user_id,
        name="health-no-sensitive",
        scopes=["miniprogram:read", "miniprogram:write"],
    )
    payload = {"consent": True, "health_consent_version": required_version}

    read_status = client.get(
        "/mp/api/v1/health-consent",
        headers={"Authorization": f"Bearer {read_only}"},
    )
    assert read_status.status_code == 200
    assert read_status.get_json()["data"]["health_consent_current"] is False
    for token in (read_only, no_sensitive):
        denied = client.post(
            "/mp/api/v1/health-consent",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        assert denied.status_code == 403
        assert denied.get_json()["error"] == "insufficient_scope"
        denied_withdrawal = client.delete(
            "/mp/api/v1/health-consent",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert denied_withdrawal.status_code == 403
        assert denied_withdrawal.get_json()["error"] == "insufficient_scope"
    db_session.expire_all()
    stored = db_session.get(User, user_id)
    assert stored.health_sensitive_consent_version is None
    assert stored.health_sensitive_consented_at is None


def test_all_sensitive_route_groups_require_health_consent_after_scope(
    app,
    client,
    db_session,
):
    """11 个敏感处理器统一先返回 428，授权后才能进入各自业务校验。"""
    from core.db_models import FamilyMember, Pair, User
    from core.usage import create_api_token

    user = User(username="health-consent-route-matrix", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(
        user.id,
        name="health-route-matrix",
        scopes=["miniprogram:read", "miniprogram:write", "miniprogram:sensitive"],
    )
    headers = {"Authorization": f"Bearer {plain}"}
    route_groups = (
        ("get", "/mp/api/v1/elders"),
        ("post", "/mp/api/v1/elders"),
        ("patch", "/mp/api/v1/elders/999999"),
        ("delete", "/mp/api/v1/elders/999999"),
        ("get", "/mp/api/v1/health/diary"),
        ("get", "/mp/api/v1/medications"),
        ("delete", "/mp/api/v1/medications/999999"),
        ("get", "/mp/api/v1/health/assessment"),
        ("post", "/mp/api/v1/actions/999999/confirm"),
        ("post", "/mp/api/v1/actions/999999/help"),
        ("post", "/mp/api/v1/actions/999999/debrief"),
    )

    for method, path in route_groups:
        blocked = getattr(client, method)(path, headers=headers, json={})
        assert blocked.status_code == 428, (method, path, blocked.get_json())
        body = blocked.get_json()
        assert body["error"] == "health_sensitive_consent_required"
        assert body["data"] == {
            "required_health_consent_version": app.config[
                "WX_MINIPROGRAM_PRIVACY_VERSION"
            ]
        }
        assert "not_found" not in blocked.get_data(as_text=True)

    accepted = client.post(
        "/mp/api/v1/health-consent",
        headers=headers,
        json={
            "consent": True,
            "health_consent_version": app.config["WX_MINIPROGRAM_PRIVACY_VERSION"],
        },
    )
    assert accepted.status_code == 200
    for method, path in route_groups:
        resumed = getattr(client, method)(path, headers=headers, json={})
        assert resumed.status_code != 428, (method, path, resumed.get_json())

    created = client.post(
        "/mp/api/v1/elders",
        headers=headers,
        json={"name": "撤回后保留的家人", "relation": "家人", "age": 70},
    )
    assert created.status_code == 200
    member_id = created.get_json()["data"]["member_id"]
    pair_id = created.get_json()["data"]["pair_id"]

    withdrawn = client.delete("/mp/api/v1/health-consent", headers=headers)
    assert withdrawn.status_code == 200
    assert withdrawn.get_json()["data"] == {
        "required_health_consent_version": app.config[
            "WX_MINIPROGRAM_PRIVACY_VERSION"
        ],
        "health_consent_current": False,
        "health_consented_at": None,
    }
    db_session.expire_all()
    assert db_session.get(FamilyMember, member_id) is not None
    assert db_session.get(Pair, pair_id) is not None

    for method, path in route_groups:
        blocked_again = getattr(client, method)(path, headers=headers, json={})
        assert blocked_again.status_code == 428, (
            method,
            path,
            blocked_again.get_json(),
        )
        assert blocked_again.get_json()["error"] == (
            "health_sensitive_consent_required"
        )


def test_read_only_api_token_cannot_reach_any_authenticated_write_route(
    app,
    client,
    db_session,
):
    from core.db_models import User
    from core.usage import create_api_token

    user = User(username="read_only_write_matrix", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    plain = create_api_token(
        user.id,
        name="read-only-matrix",
        scopes=["miniprogram:read", "miniprogram:sensitive"],
    )
    headers = {"Authorization": f"Bearer {plain}"}
    write_routes = (
        ("post", "/mp/api/v1/auth/logout"),
        ("delete", "/mp/api/v1/health-consent"),
        ("patch", "/mp/api/v1/me"),
        ("delete", "/mp/api/v1/me"),
        ("post", "/mp/api/v1/elders"),
        ("patch", "/mp/api/v1/elders/1"),
        ("delete", "/mp/api/v1/elders/1"),
        ("post", "/mp/api/v1/health/diary"),
        ("post", "/mp/api/v1/medications"),
        ("delete", "/mp/api/v1/medications"),
        ("delete", "/mp/api/v1/medications/1"),
        ("post", "/mp/api/v1/health/assessment"),
        ("post", "/mp/api/v1/actions/1/confirm"),
        ("post", "/mp/api/v1/actions/1/help"),
        ("post", "/mp/api/v1/actions/1/debrief"),
        ("post", "/mp/api/v1/events"),
    )

    for method, path in write_routes:
        response = getattr(client, method)(path, json={}, headers=headers)
        assert response.status_code == 403, (method, path, response.get_json())
        assert response.get_json()["error"] == "insufficient_scope"


def test_elder_profiles_require_sensitive_scope_for_read_and_write(
    app,
    client,
    db_session,
):
    """兼容 Token 缺少 sensitive 时不得读写老人身份和健康资料。"""
    from core.db_models import User
    from core.usage import create_api_token

    user = User(username="elder-sensitive-scope", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    read_token = create_api_token(
        user.id,
        name="elder-read-without-sensitive",
        scopes=["miniprogram:read"],
    )
    write_token = create_api_token(
        user.id,
        name="elder-write-without-sensitive",
        scopes=["miniprogram:read", "miniprogram:write"],
    )

    denied_read = client.get(
        "/mp/api/v1/elders",
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert denied_read.status_code == 403
    assert denied_read.get_json()["error"] == "insufficient_scope"

    write_headers = {"Authorization": f"Bearer {write_token}"}
    for method, path in (
        ("post", "/mp/api/v1/elders"),
        ("patch", "/mp/api/v1/elders/1"),
        ("delete", "/mp/api/v1/elders/1"),
    ):
        denied = getattr(client, method)(path, json={}, headers=write_headers)
        assert denied.status_code == 403, (method, path, denied.get_json())
        assert denied.get_json()["error"] == "insufficient_scope"


def test_profile_requires_privacy_consent_before_generating_api_token(
    app,
    authenticated_client,
    db_session,
):
    from core.db_models import ApiToken

    missing_consent = authenticated_client.post(
        "/profile",
        data={
            "csrf_token": "test-csrf-token",
            "form_id": "api_token",
            "token_name": "未同意设备",
        },
    )
    assert missing_consent.status_code == 302
    assert ApiToken.query.count() == 0

    accepted = authenticated_client.post(
        "/profile",
        data={
            "csrf_token": "test-csrf-token",
            "form_id": "api_token",
            "token_name": "本人手机",
            "miniprogram_privacy_consent": "1",
        },
    )
    assert accepted.status_code == 302
    record = ApiToken.query.one()
    assert record.expires_at is not None
    assert record.scopes
    assert record.privacy_consent_version == app.config["WX_MINIPROGRAM_PRIVACY_VERSION"]


def test_mp_api_rate_limit_key_uses_stable_client_ip(app):
    from blueprints.mp_api import _mp_rate_limit_key

    same_ip = {"REMOTE_ADDR": "203.0.113.10"}
    other_ip = {"REMOTE_ADDR": "203.0.113.11"}
    with app.test_request_context(
        "/mp/api/v1/me",
        headers={"Authorization": "Bearer token-a"},
        environ_base=same_ip,
    ):
        key_a = _mp_rate_limit_key()

    with app.test_request_context(
        "/mp/api/v1/me",
        headers={"Authorization": "Bearer token-b"},
        environ_base=same_ip,
    ):
        key_b = _mp_rate_limit_key()

    with app.test_request_context(
        "/mp/api/v1/me",
        headers={"Authorization": "Bearer token-a"},
        environ_base=other_ip,
    ):
        key_other_ip = _mp_rate_limit_key()

    assert key_a.startswith("mp-ip:")
    assert key_a == key_b
    assert key_other_ip != key_a


def test_mp_api_invalid_bearer_rotation_cannot_bypass_ip_limit(
    app,
    client,
    db_session,
):
    """同一 IP 轮换无效 Bearer 仍应命中同一个外层限流桶。"""
    from core.extensions import limiter

    app.config['RATE_LIMIT_MP_READ'] = '1 per minute'
    limiter.reset()
    same_ip = {'REMOTE_ADDR': '203.0.113.20'}
    other_ip = {'REMOTE_ADDR': '203.0.113.21'}

    try:
        first = client.get(
            '/mp/api/v1/me',
            headers={'Authorization': 'Bearer invalid-a'},
            environ_overrides=same_ip,
        )
        rotated = client.get(
            '/mp/api/v1/me',
            headers={'Authorization': 'Bearer invalid-b'},
            environ_overrides=same_ip,
        )
        separate_ip = client.get(
            '/mp/api/v1/me',
            headers={'Authorization': 'Bearer invalid-c'},
            environ_overrides=other_ip,
        )

        assert first.status_code == 401
        assert rotated.status_code == 429
        assert separate_ip.status_code == 401
    finally:
        limiter.reset()


def test_mp_api_events_rejects_invalid_event_type(app, client, db_session):
    from core.db_models import User
    from core.usage import create_api_token

    with app.app_context():
        user = User(username="mp_event_user", role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        plain = create_api_token(user.id, name="events")

    resp = client.post(
        "/mp/api/v1/events",
        json={"event_type": "free_form_noise"},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_event_type"


def test_mp_api_events_rejects_large_meta(app, client, db_session):
    from core.db_models import User
    from core.usage import create_api_token

    with app.app_context():
        user = User(username="mp_event_meta_user", role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        plain = create_api_token(user.id, name="events")

    resp = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy", "meta": {"payload": "x" * 3000}},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "meta_too_large"


def test_mp_elders_does_not_create_trigger_from_mock_weather(app, client, db_session, monkeypatch):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from core.usage import create_api_token

    with app.app_context():
        user = User(
            username="mp_mock_weather_user",
            role="user",
            health_sensitive_consent_version=app.config[
                "WX_MINIPROGRAM_PRIVACY_VERSION"
            ],
            health_sensitive_consented_at=utcnow(),
        )
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="mp-mock-elder",
            short_code="31313131",
            short_code_hash=hash_short_code("31313131"),
            status="active",
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.commit()
        pair_id = pair.id
        plain = create_api_token(user.id, name="mock-weather")

    monkeypatch.setattr(
        'blueprints.mp_api.resolve_location',
        lambda _label: {'location_code': '101240201', 'provider': 'QWeather'},
    )
    monkeypatch.setattr(
        'blueprints.mp_api.get_weather_with_cache',
        lambda _location: ({
            'temperature': 37,
            'temperature_max': 39,
            'temperature_min': 29,
            'data_source': 'Demo',
            'is_mock': True,
        }, False),
    )

    response = client.get(
        '/mp/api/v1/elders',
        headers={'Authorization': f'Bearer {plain}'},
    )

    assert response.status_code == 200
    today = response.get_json()['data'][0]['today']
    assert today['trigger'] is None
    assert today['weather_available'] is False
    assert today['temperature_max'] is None
    assert today['temperature_min'] is None
    assert today['is_mock'] is True

    monkeypatch.setattr('blueprints.mp_api.get_qweather_warnings', lambda _code: [])
    alerts_response = client.get(
        f'/mp/api/v1/alerts?pair_id={pair_id}',
        headers={'Authorization': f'Bearer {plain}'},
    )
    alert_weather = alerts_response.get_json()['data']['weather']
    assert alert_weather['weather_available'] is False
    assert alert_weather['temperature_max'] is None
    assert alert_weather['temperature_min'] is None
