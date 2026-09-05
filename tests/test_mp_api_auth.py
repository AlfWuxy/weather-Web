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
        rotated_body = rotated.get_json()
        assert rotated_body["success"] is False
        assert rotated_body["error"] == "rate_limited"
        assert rotated.content_type.startswith("application/json")
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
    assert today.get('has_official_warning') is False
    assert alerts_response.get_json()['data']['has_official_warning'] is False


def test_mp_api_post_and_patch_without_csrf_still_json(app, client, db_session):
    from core.usage import create_api_token
    from core.db_models import User

    with app.app_context():
        user = User(username="mp_csrf_user", role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        plain = create_api_token(user.id, name="csrf")

    events = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    me_patch = client.patch(
        "/mp/api/v1/me",
        json={"push_enabled": False},
        headers={"Authorization": f"Bearer {plain}"},
    )
    for resp in (events, me_patch):
        assert resp.status_code != 400
        assert resp.is_json
        assert resp.content_type.startswith("application/json")
        assert isinstance(resp.get_json().get("success"), bool)
    assert events.status_code == 200
    assert events.get_json()["success"] is True
    assert me_patch.status_code == 200
    assert me_patch.get_json()["success"] is True


def test_mp_api_429_json_body_success_false(app, client, db_session):
    from core.db_models import User
    from core.extensions import limiter
    from core.usage import create_api_token

    with app.app_context():
        user = User(username="mp_429_json_user", role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        plain = create_api_token(user.id, name="limit")

    app.config["RATE_LIMIT_MP_READ"] = "1 per minute"
    limiter.reset()
    ip = {"REMOTE_ADDR": "203.0.113.30"}
    try:
        first = client.get(
            "/mp/api/v1/me",
            headers={"Authorization": f"Bearer {plain}"},
            environ_overrides=ip,
        )
        second = client.get(
            "/mp/api/v1/me",
            headers={"Authorization": f"Bearer {plain}"},
            environ_overrides=ip,
        )
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.is_json
        body = second.get_json()
        assert body["success"] is False
        assert body["error"] == "rate_limited"
    finally:
        limiter.reset()
