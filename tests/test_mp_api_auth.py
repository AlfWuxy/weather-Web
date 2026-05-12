# -*- coding: utf-8 -*-


def test_mp_api_requires_token(client):
    resp = client.get("/mp/api/v1/me")
    assert resp.status_code == 401


def test_mp_api_me_and_patch(app, client, db_session):
    from core.db_models import ApiToken, User
    from core.time_utils import utcnow
    from core.usage import create_api_token

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

    # update push settings
    resp2 = client.patch(
        "/mp/api/v1/me",
        json={"wxpusher_uid": "UID_X", "push_enabled": True},
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
        token_row.revoked_at = utcnow()
        db_session.commit()

    resp3 = client.get("/mp/api/v1/me", headers={"Authorization": f"Bearer {plain}"})
    assert resp3.status_code == 401


def test_mp_api_rate_limit_key_uses_bearer_token(app):
    from blueprints.mp_api import _mp_rate_limit_key

    with app.test_request_context("/mp/api/v1/me", headers={"Authorization": "Bearer token-a"}):
        key_a = _mp_rate_limit_key()

    with app.test_request_context("/mp/api/v1/me", headers={"Authorization": "Bearer token-b"}):
        key_b = _mp_rate_limit_key()

    assert key_a.startswith("mp-token:")
    assert key_b.startswith("mp-token:")
    assert key_a != key_b


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
        json={"event_type": "template_view", "meta": {"payload": "x" * 3000}},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "meta_too_large"
