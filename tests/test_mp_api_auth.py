# -*- coding: utf-8 -*-

import json

import pytest


def _create_mp_identity(app, db_session, username):
    from core.db_models import User
    from core.usage import create_api_token

    with app.app_context():
        user = User(username=username, role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        return user.id, create_api_token(user.id, name="mp-test")


def _create_member_pair(app, db_session, username, *, status="active"):
    from core.db_models import FamilyMember, Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from core.usage import create_api_token

    with app.app_context():
        user = User(username=username, role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.flush()
        member = FamilyMember(
            user_id=user.id,
            name="妈妈",
            relation="母亲",
            age=68,
            gender="女性",
            chronic_diseases=json.dumps(["高血压"], ensure_ascii=False),
            created_at=utcnow(),
        )
        db_session.add(member)
        db_session.flush()
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            member_id=member.id,
            elder_code=f"elder-{username}",
            short_code="42424242",
            short_code_hash=hash_short_code("42424242"),
            status=status,
            last_active_at=utcnow(),
            created_at=utcnow(),
        )
        db_session.add(pair)
        db_session.commit()
        token = create_api_token(user.id, name="mp-test")
        return user.id, member.id, pair.id, token


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
        json={"event_type": "template_view", "meta": {"payload": "x" * 3000}},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "meta_too_large"


def test_mp_write_endpoints_reject_non_object_json(app, client, db_session):
    _, _, pair_id, token = _create_member_pair(app, db_session, "mp_json_object_user")
    headers = {"Authorization": f"Bearer {token}"}

    responses = [
        client.patch("/mp/api/v1/me", json=[], headers=headers),
        client.post("/mp/api/v1/elders", json=[], headers=headers),
        client.patch(f"/mp/api/v1/elders/{pair_id}", json=[], headers=headers),
        client.post("/mp/api/v1/events", json=[], headers=headers),
    ]

    for response in responses:
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_payload"


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("age", True, "invalid_age"),
        ("age", 0, "invalid_age"),
        ("age", 151, "invalid_age"),
        ("age", "68.5", "invalid_age"),
        ("gender", 1, "invalid_gender"),
        ("gender", "不透露", "invalid_gender"),
        ("chronic_diseases", "高血压", "invalid_chronic_diseases"),
        ("chronic_diseases", [1], "invalid_chronic_diseases"),
        ("chronic_diseases", [""], "invalid_chronic_diseases"),
        ("location_query", "   ", "invalid_location_query"),
        ("location_query", 123, "invalid_location_query"),
    ],
)
def test_mp_elder_create_strictly_rejects_invalid_profile_without_writes(
    app,
    client,
    db_session,
    field,
    value,
    expected_error,
):
    user_id, token = _create_mp_identity(app, db_session, f"mp_create_{field}_{expected_error}")
    payload = {
        "name": "妈妈",
        "relation": "母亲",
        "age": 68,
        "gender": "女性",
        "location_query": "都昌",
        "chronic_diseases": ["高血压"],
    }
    payload[field] = value

    response = client.post(
        "/mp/api/v1/elders",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_error

    from core.db_models import FamilyMember, Pair

    with app.app_context():
        assert FamilyMember.query.filter_by(user_id=user_id).count() == 0
        assert Pair.query.filter_by(caregiver_id=user_id).count() == 0


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (
            {"location_query": "新地点", "age": "bad"},
            "invalid_age",
        ),
        (
            {"location_query": "新地点", "gender": "不透露"},
            "invalid_gender",
        ),
        (
            {"location_query": "新地点", "chronic_diseases": "糖尿病"},
            "invalid_chronic_diseases",
        ),
        (
            {"location_query": "   ", "chronic_diseases": ["糖尿病"]},
            "invalid_location_query",
        ),
    ],
)
def test_mp_elder_patch_validation_failure_never_partially_updates(
    app,
    client,
    db_session,
    payload,
    expected_error,
):
    _, member_id, pair_id, token = _create_member_pair(
        app,
        db_session,
        f"mp_patch_{expected_error}",
    )

    response = client.patch(
        f"/mp/api/v1/elders/{pair_id}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_error

    from core.db_models import FamilyMember, Pair

    with app.app_context():
        db_session.expire_all()
        pair = db_session.get(Pair, pair_id)
        member = db_session.get(FamilyMember, member_id)
        assert pair.location_query == "都昌"
        assert pair.community_code == "都昌"
        assert member.age == 68
        assert member.gender == "女性"
        assert json.loads(member.chronic_diseases) == ["高血压"]


def test_mp_inactive_pair_is_rejected_by_patch_alerts_and_events(
    app,
    client,
    db_session,
):
    _, _, pair_id, token = _create_member_pair(
        app,
        db_session,
        "mp_inactive_pair_user",
        status="inactive",
    )
    headers = {"Authorization": f"Bearer {token}"}

    responses = [
        client.patch(
            f"/mp/api/v1/elders/{pair_id}",
            json={"location_query": "新地点"},
            headers=headers,
        ),
        client.get(f"/mp/api/v1/alerts?pair_id={pair_id}", headers=headers),
        client.post(
            "/mp/api/v1/events",
            json={"event_type": "template_copy", "pair_id": pair_id},
            headers=headers,
        ),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.get_json()["error"] == "not_found"


def test_mp_events_only_accepts_client_events_and_strict_owned_relations(
    app,
    client,
    db_session,
):
    user_id, member_id, pair_id, token = _create_member_pair(
        app,
        db_session,
        "mp_strict_event_user",
    )
    headers = {"Authorization": f"Bearer {token}"}

    for internal_event_type in (
        "pair_created",
        "push_click",
        "help_flagged",
        "checkin_confirmed",
        "wxoa_land",
    ):
        internal_event = client.post(
            "/mp/api/v1/events",
            json={"event_type": internal_event_type, "pair_id": pair_id},
            headers=headers,
        )
        assert internal_event.status_code == 400
        assert internal_event.get_json()["error"] == "invalid_event_type"

    invalid_pair = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy", "pair_id": "1.5"},
        headers=headers,
    )
    assert invalid_pair.status_code == 400
    assert invalid_pair.get_json()["error"] == "invalid_pair_id"

    missing_pair = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy", "pair_id": 999999},
        headers=headers,
    )
    assert missing_pair.status_code == 404
    assert missing_pair.get_json()["error"] == "not_found"

    missing_member = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy", "member_id": 999999},
        headers=headers,
    )
    assert missing_member.status_code == 404
    assert missing_member.get_json()["error"] == "member_not_found"

    from core.db_models import FamilyMember
    from core.time_utils import utcnow

    with app.app_context():
        other_member = FamilyMember(
            user_id=user_id,
            name="爸爸",
            relation="父亲",
            age=70,
            gender="男性",
            created_at=utcnow(),
        )
        db_session.add(other_member)
        db_session.commit()
        other_member_id = other_member.id

    mismatch = client.post(
        "/mp/api/v1/events",
        json={
            "event_type": "template_copy",
            "pair_id": pair_id,
            "member_id": other_member_id,
        },
        headers=headers,
    )
    assert mismatch.status_code == 400
    assert mismatch.get_json()["error"] == "pair_member_mismatch"

    accepted = client.post(
        "/mp/api/v1/events",
        json={
            "event_type": "template_copy",
            "pair_id": pair_id,
            "member_id": member_id,
        },
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["success"] is True


def test_mp_elders_does_not_create_trigger_from_mock_weather(app, client, db_session, monkeypatch):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from core.usage import create_api_token

    with app.app_context():
        user = User(username="mp_mock_weather_user", role="user")
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
