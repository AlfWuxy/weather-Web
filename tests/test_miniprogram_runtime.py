# -*- coding: utf-8 -*-
"""微信小程序运行时：快照、认证、owner scope 与输入边界。"""

import json
from datetime import timedelta

import pytest

from core.time_utils import utcnow


CURRENT = {
    "temperature": 36.0,
    "temperature_max": 38.0,
    "temperature_min": 28.0,
    "humidity": 72.0,
    "weather_condition": "晴",
    "is_mock": False,
    "data_source": "QWeather",
}

FORECAST = [
    {
        "date": "2026-07-17",
        "temperature_mean": 35.0,
        "temperature_max": 39.0,
        "temperature_min": 29.0,
        "humidity": 70.0,
        "is_mock": False,
        "data_source": "QWeather",
    }
]


def _user_and_token(db_session, username):
    from core.db_models import User
    from core.usage import create_api_token

    user = User(username=username, role="user")
    user.set_password("safe-test-password")
    db_session.add(user)
    db_session.commit()
    return user, create_api_token(user.id, name="runtime-test")


def _pair(db_session, user, code, *, member=None):
    from core.db_models import Pair
    from core.security import hash_short_code

    record = Pair(
        caregiver_id=user.id,
        member_id=member.id if member else None,
        community_code="都昌县",
        location_query="都昌县",
        elder_code=f"elder-{code}",
        short_code=code,
        short_code_hash=hash_short_code(code),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(record)
    db_session.commit()
    return record


def _persist_snapshot(app, *, fetched_at=None):
    from services.miniprogram_service import persist_snapshot

    with app.app_context():
        record = persist_snapshot(
            CURRENT,
            FORECAST,
            [],
            fetched_at=fetched_at or utcnow(),
        )
        return record.snapshot_id


def test_snapshot_fresh_at_2959_and_stale_at_3001(app, db_session):
    from services.miniprogram_service import snapshot_payload

    fetched_at = utcnow()
    snapshot_id = _persist_snapshot(app, fetched_at=fetched_at)
    with app.app_context():
        from core.db_models import MiniProgramSnapshot

        record = MiniProgramSnapshot.query.filter_by(snapshot_id=snapshot_id).one()
        fresh = snapshot_payload(record, now=fetched_at + timedelta(minutes=29, seconds=59))
        stale = snapshot_payload(record, now=fetched_at + timedelta(minutes=30, seconds=1))

    assert fresh["ttl_seconds"] == 1800
    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert fresh["snapshot_id"] == stale["snapshot_id"]


def test_bootstrap_is_database_only_and_keeps_same_snapshot_id(
    app,
    client,
    db_session,
    monkeypatch,
):
    snapshot_id = _persist_snapshot(app)
    monkeypatch.setattr(
        "requests.get",
        lambda *_args, **_kwargs: pytest.fail("bootstrap 不得访问外网"),
    )

    first = client.get("/mp/api/v1/bootstrap")
    second = client.get("/mp/api/v1/bootstrap")

    assert first.status_code == 200
    assert first.get_json()["data"]["snapshot_id"] == snapshot_id
    assert second.get_json()["data"]["snapshot_id"] == snapshot_id
    assert first.get_json()["data"]["location"]["name"] == "都昌县"
    assert first.get_json()["data"]["required_privacy_consent_version"]
    source_status = first.get_json()["data"]["source_status"]
    assert source_status["budget_guard"] == "enabled"
    assert "limit" not in str(source_status).lower()
    assert "remaining" not in str(source_status).lower()


def test_all_elders_share_one_snapshot_without_weather_fanout(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember

    user, token = _user_and_token(db_session, "shared_snapshot_user")
    for index in range(3):
        member = FamilyMember(user_id=user.id, name=f"老人{index}", relation="家人")
        db_session.add(member)
        db_session.flush()
        _pair(db_session, user, f"7000000{index}", member=member)
    snapshot_id = _persist_snapshot(app)
    monkeypatch.setattr(
        "blueprints.mp_api.get_weather_with_cache",
        lambda *_args, **_kwargs: pytest.fail("elders 不得逐人获取天气"),
    )
    monkeypatch.setattr(
        "blueprints.mp_api.get_qweather_warnings",
        lambda *_args, **_kwargs: pytest.fail("elders 不得逐人获取预警"),
    )

    response = client.get(
        "/mp/api/v1/elders",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    rows = response.get_json()["data"]
    assert len(rows) == 3
    assert {row["snapshot_id"] for row in rows} == {snapshot_id}
    assert {row["location"]["name"] for row in rows} == {"都昌县"}


class _WechatResponse:
    status_code = 200

    def __init__(self, openid="openid-sensitive-value"):
        self.openid = openid

    def json(self):
        return {"openid": self.openid, "session_key": "never-store-session-key"}


def _configure_wechat(app):
    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-appsecret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
        WX_MINIPROGRAM_SESSION_TTL_SECONDS=3600,
    )


def _wechat_login(app, client, monkeypatch, openid="openid-sensitive-value"):
    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse(openid),
    )
    return client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "wx-login-code", "privacy_consent_version": "privacy-v1"},
    )


def test_wechat_login_hashes_openid_and_issues_expiring_signed_session(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, MiniProgramSession
    from services.miniprogram_auth import hash_openid, verify_miniprogram_session

    response = _wechat_login(app, client, monkeypatch)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["session_token"] == data["token"]
    assert data["user"] == {"id": data["user"]["id"], "display_name": "微信用户"}
    assert "username" not in data["user"]
    serialized = str(response.get_json())
    assert "server-only-appsecret" not in serialized
    assert "openid-sensitive-value" not in serialized
    assert "never-store-session-key" not in serialized

    with app.app_context():
        identity = MiniProgramIdentity.query.one()
        session = MiniProgramSession.query.one()
        assert identity.openid_hash == hash_openid("openid-sensitive-value")
        assert "openid-sensitive-value" not in identity.openid_hash
        assert session.token_hash != data["session_token"]
        assert verify_miniprogram_session(data["session_token"]).id == session.id


def test_privacy_consent_is_required_before_code_exchange(app, client, db_session, monkeypatch):
    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: pytest.fail("未同意隐私指引时不得换取 OpenID"),
    )
    response = client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "wx-login-code", "privacy_consent_version": "old-version"},
    )
    assert response.status_code == 428
    assert response.get_json()["error"] == "privacy_consent_required"
    assert response.get_json()["data"]["required_privacy_consent_version"] == "privacy-v1"


def test_logout_revokes_session_and_next_request_is_unauthorized(
    app,
    client,
    db_session,
    monkeypatch,
):
    login = _wechat_login(app, client, monkeypatch, openid="logout-openid")
    token = login.get_json()["data"]["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    logout = client.post("/mp/api/v1/auth/logout", headers=headers)
    after = client.get("/mp/api/v1/me", headers=headers)

    assert logout.status_code == 200
    assert logout.get_json()["data"]["revoked"] is True
    assert after.status_code == 401


def test_expired_session_is_rejected(app, client, db_session, monkeypatch):
    from core.db_models import MiniProgramSession

    login = _wechat_login(app, client, monkeypatch, openid="expired-openid")
    token = login.get_json()["data"]["session_token"]
    with app.app_context():
        record = MiniProgramSession.query.one()
        record.expires_at = utcnow() - timedelta(seconds=1)
        db_session.commit()

    response = client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_owner_scope_and_input_boundaries_for_diary_medication_and_actions(
    app,
    client,
    db_session,
):
    from core.db_models import FamilyMember

    owner, owner_token = _user_and_token(db_session, "runtime_owner")
    outsider, outsider_token = _user_and_token(db_session, "runtime_outsider")
    member = FamilyMember(user_id=owner.id, name="自己的老人", relation="父亲")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "71111111", member=member)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    created = client.post(
        "/mp/api/v1/health/diary",
        headers=owner_headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "有点乏力",
            "notes": "已补水",
        },
    )
    assert created.status_code == 201
    notes_only = client.post(
        "/mp/api/v1/health/diary",
        headers=owner_headers,
        json={"pair_id": pair.id, "severity": "none", "symptoms": "", "notes": "状态正常"},
    )
    assert notes_only.status_code == 201
    assert client.get(
        f"/mp/api/v1/health/diary?pair_id={pair.id}", headers=outsider_headers
    ).status_code in {400, 404}

    too_long = client.post(
        "/mp/api/v1/health/diary",
        headers=owner_headers,
        json={"pair_id": pair.id, "severity": "mild", "symptoms": "x" * 501},
    )
    assert too_long.status_code == 400
    assert too_long.get_json()["error"] == "symptoms_too_long"

    medication = client.post(
        "/mp/api/v1/medications",
        headers=owner_headers,
        json={
            "pair_id": pair.id,
            "medicine_name": "日常药物",
            "dosage": "1片",
            "frequency": "daily",
            "time_of_day": "08:00",
            "weather_triggers": {
                "high_temp": 35,
                "low_temp": 5,
                "high_humidity": 85,
                "high_aqi": 150,
            },
        },
    )
    assert medication.status_code == 201
    assert medication.get_json()["data"]["medication"]["weather_triggers"]["high_temp"] == 35.0
    medication_id = medication.get_json()["data"]["medication"]["id"]
    invalid_trigger = client.post(
        "/mp/api/v1/medications",
        headers=owner_headers,
        json={
            "pair_id": pair.id,
            "medicine_name": "无效阈值",
            "weather_triggers": {"high_humidity": 101},
        },
    )
    assert invalid_trigger.status_code == 400
    assert invalid_trigger.get_json()["error"] == "invalid_weather_triggers"
    assert client.delete(
        f"/mp/api/v1/medications/{medication_id}", headers=outsider_headers
    ).status_code == 404
    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=outsider_headers,
        json={"actions_done": ["hydrate"]},
    ).status_code == 404
    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=owner_headers,
        json={"actions_done": ["hydrate"]},
    ).status_code == 200


def test_health_assessment_submit_latest_and_owner_scope(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember

    owner, token = _user_and_token(db_session, "assessment_owner")
    outsider, outsider_token = _user_and_token(db_session, "assessment_outsider")
    member = FamilyMember(
        user_id=owner.id,
        name="评估老人",
        relation="母亲",
        age=72,
        chronic_diseases='["高血压"]',
    )
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "72222222", member=member)
    _persist_snapshot(app)
    monkeypatch.setattr(
        "services.health_risk_service.HealthRiskService.assess_personal_weather_health_risk",
        lambda _self, profile, weather, screening=None: {
            "risk_score": 62.5,
            "risk_level": "中风险",
            "disease_risks": {"heat": 0.6},
            "recommendations": ["减少高温时段外出"],
            "explain": {"reasons": ["高温"]},
            "risk_interval": {"low": 50, "high": 70},
            "model_version": "test-model",
            "rule_version": "test-rule",
        },
    )
    payload = {
        "pair_id": pair.id,
        "outdoor_exposure": "medium",
        "symptom_level": "none",
        "hydration": "normal",
        "medication_adherence": "good",
        "sleep_quality": "fair",
    }
    owner_headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/mp/api/v1/health/assessment", headers=owner_headers, json=payload)

    assert created.status_code == 201
    assessment = created.get_json()["data"]["assessment"]
    assert assessment["member_id"] == member.id
    assert assessment["risk_score"] == 62.5
    assert "weather_condition" not in assessment
    with app.app_context():
        from core.db_models import HealthRiskAssessment

        stored = db_session.get(HealthRiskAssessment, assessment["id"])
        assert len(stored.weather_condition) <= 100
        assert json.loads(stored.weather_condition)["t"] == 36.0
    latest = client.get(
        f"/mp/api/v1/health/assessment?pair_id={pair.id}", headers=owner_headers
    )
    assert latest.get_json()["data"]["latest"]["id"] == assessment["id"]
    assert client.post(
        "/mp/api/v1/health/assessment",
        headers={"Authorization": f"Bearer {outsider_token}"},
        json=payload,
    ).status_code == 404


def test_public_resources_are_aggregated_and_small_samples_suppressed(
    app,
    client,
    db_session,
):
    from core.db_models import Community, CommunityDaily, CoolingResource
    from core.time_utils import today_local

    db_session.add_all(
        [
            Community(
                name="测试社区",
                population=1000,
                elderly_ratio=0.25,
                vulnerability_index=45,
                risk_level="中",
            ),
            CommunityDaily(
                community_code="测试社区",
                date=today_local(),
                total_people=2,
                confirm_rate=0.5,
                escalation_rate=0.5,
            ),
            CoolingResource(
                community_code="测试社区",
                name="社区活动室",
                address_hint="社区内",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/mp/api/v1/public/community")
    assert response.status_code == 200
    data = response.get_json()["data"]
    summary = data["communities"][0]["latest_action_summary"]
    assert summary["sample_suppressed"] is True
    assert summary["confirm_rate"] is None
    assert data["cooling"][0]["name"] == "社区活动室"
    serialized = str(data).lower()
    assert "diagnosis" not in serialized
    assert "medical_history" not in serialized


def test_public_gis_metadata_uses_same_origin_relative_url(app, client, db_session):
    app.config["FEATURE_HEAT_EXPOSURE_GIS"] = True
    response = client.get("/mp/api/v1/public/gis-metadata")
    assert response.status_code == 200
    gis = response.get_json()["data"]
    assert gis["available"] is True
    assert gis["geojson_url"].startswith("/static/")
    assert "://" not in gis["geojson_url"]
    assert gis["size_bytes"] > 0
    assert gis["title"]
    assert app.config["RATE_LIMIT_MP_PUBLIC"] == "600 per minute"


def test_account_delete_rejects_cross_user_and_anonymizes_owner_data(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import HealthDiary, MiniProgramIdentity, MiniProgramSession, User

    login = _wechat_login(app, client, monkeypatch, openid="delete-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    token = data["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    with app.app_context():
        db_session.add(
            HealthDiary(
                user_id=user_id,
                symptoms="待删除",
                severity="mild",
                created_at=utcnow(),
            )
        )
        db_session.commit()

    cross_user = client.delete(
        "/mp/api/v1/me",
        headers=headers,
        json={"confirm": True, "user_id": user_id + 999},
    )
    assert cross_user.status_code == 403

    assert client.delete("/mp/api/v1/me", headers=headers).status_code == 400
    assert client.delete(
        "/mp/api/v1/me", headers=headers, json={"confirm": False}
    ).status_code == 400

    deleted = client.delete(
        "/mp/api/v1/me",
        headers=headers,
        json={"confirm": True, "user_id": user_id},
    )
    assert deleted.status_code == 200
    assert client.get("/mp/api/v1/me", headers=headers).status_code == 401
    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.username.startswith("deleted_mp_")
        assert user.email is None
        assert user.deleted_at is not None
        assert HealthDiary.query.filter_by(user_id=user_id).count() == 0
        assert MiniProgramIdentity.query.filter_by(user_id=user_id).count() == 0
        assert MiniProgramSession.query.filter_by(user_id=user_id).count() == 0


def test_admin_delete_removes_miniprogram_identity_and_invalidates_bearer(
    app,
    admin_client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, MiniProgramSession, User

    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse("admin-delete-target"),
    )
    login = admin_client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "one-time-code", "privacy_consent_version": "privacy-v1"},
    )
    assert login.status_code == 200
    data = login.get_json()["data"]
    token = data["session_token"]
    user_id = data["user"]["id"]

    deleted = admin_client.post(
        f"/admin/user/{user_id}/delete",
        data={"csrf_token": "test-csrf-token"},
    )
    assert deleted.status_code == 302
    assert db_session.get(User, user_id) is None
    assert MiniProgramIdentity.query.filter_by(user_id=user_id).count() == 0
    assert MiniProgramSession.query.filter_by(user_id=user_id).count() == 0
    assert admin_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 401


def test_sync_cycle_calls_each_qweather_endpoint_at_most_once_and_enriches_forecast(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramSnapshot
    from services.pipelines import sync_weather_cache as pipeline

    calls = {"current": [], "forecast": [], "warning": []}

    class WeatherStub:
        def get_current_weather(self, location, *, include_enrichment=True):
            calls["current"].append((location, include_enrichment))
            return {
                "temperature": 36,
                "temperature_max": None,
                "temperature_min": None,
                "humidity": 70,
                "data_source": "QWeather",
                "is_mock": False,
            }

        def get_qweather_daily_forecast(self, location, days=7):
            calls["forecast"].append((location, days))
            return {"success": True, "daily": FORECAST, "meta": {"source": "QWeather"}}

    app.config.update(
        QWEATHER_AUTH_MODE="api_key",
        QWEATHER_KEY="test-only-key",
        QWEATHER_API_BASE="https://qweather.invalid/v7",
        QWEATHER_CANONICAL_LOCATION="116.20,29.27",
    )
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "WeatherService", WeatherStub)
    monkeypatch.setattr(
        "services.warning_service.get_qweather_warnings_result",
        lambda location: calls["warning"].append(location)
        or {"available": True, "status": "ok", "warnings": []},
    )

    result = pipeline.sync_weather_cache(locations=["都昌县", "九江"], update_daily=False)

    assert calls["current"] == [("都昌县", False)]
    assert calls["forecast"] == [("都昌县", 7)]
    assert calls["warning"] == ["116.20,29.27"]
    assert result["locations"] == 1
    record = MiniProgramSnapshot.query.filter_by(snapshot_id=result["snapshot_id"]).one()
    payload = __import__("services.miniprogram_service", fromlist=["snapshot_payload"]).snapshot_payload(record)
    assert payload["current"]["temperature_max"] == 39.0
    assert payload["forecast"][0]["risk_available"] is True
    assert payload["forecast"][0]["risk_score"] is not None
    assert payload["risk"]["summary"]


def test_sync_with_qweather_empty_never_instantiates_fetcher_or_uses_network(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import ForecastCache, MiniProgramSnapshot, WeatherCache
    from services.miniprogram_service import snapshot_payload
    from services.pipelines import sync_weather_cache as pipeline

    app.config.update(QWEATHER_AUTH_MODE="disabled", QWEATHER_KEY="", QWEATHER_API_BASE="")
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(
        pipeline,
        "WeatherService",
        lambda: pytest.fail("QWeather 为空时不应创建联网 fetcher"),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *_args, **_kwargs: pytest.fail("测试禁止外网"),
    )
    old_fetched_at = utcnow() - timedelta(hours=2)
    db_session.add_all(
        [
            WeatherCache(
                location="都昌县",
                fetched_at=old_fetched_at,
                payload=json.dumps(CURRENT, ensure_ascii=False),
                is_mock=False,
            ),
            ForecastCache(
                location="qweather-only:都昌县",
                days=7,
                fetched_at=old_fetched_at,
                payload=json.dumps({"daily": FORECAST}, ensure_ascii=False),
                is_mock=False,
            ),
        ]
    )
    db_session.commit()

    result = pipeline.sync_weather_cache(update_daily=False)

    assert result["locations"] == 1
    assert result["snapshot_id"]
    record = MiniProgramSnapshot.query.filter_by(snapshot_id=result["snapshot_id"]).one()
    payload = snapshot_payload(record)
    assert payload["stale"] is True
    assert record.fetched_at == old_fetched_at.replace(tzinfo=None)
    cached = WeatherCache.query.filter_by(location="都昌县").one()
    assert cached.fetched_at == old_fetched_at.replace(tzinfo=None)


def test_snapshot_retention_keeps_current_and_prunes_old_rows(app, db_session):
    from core.db_models import MiniProgramSnapshot
    from services.miniprogram_service import persist_snapshot

    app.config["MINIPROGRAM_SNAPSHOT_RETENTION"] = 3
    start = utcnow() - timedelta(hours=3)
    snapshot_ids = []
    with app.app_context():
        for index in range(5):
            record = persist_snapshot(
                CURRENT,
                FORECAST,
                [],
                fetched_at=start + timedelta(minutes=30 * index),
            )
            snapshot_ids.append(record.snapshot_id)
        remaining = MiniProgramSnapshot.query.order_by(MiniProgramSnapshot.fetched_at.asc()).all()

    assert len(remaining) == 3
    assert remaining[-1].snapshot_id == snapshot_ids[-1]
    assert snapshot_ids[0] not in {item.snapshot_id for item in remaining}


def test_snapshot_retention_stays_bounded_for_out_of_order_backfill(app, db_session):
    from core.db_models import MiniProgramSnapshot
    from services.miniprogram_service import persist_snapshot

    app.config["MINIPROGRAM_SNAPSHOT_RETENTION"] = 3
    now = utcnow()
    with app.app_context():
        for index in range(3):
            persist_snapshot(CURRENT, FORECAST, [], fetched_at=now + timedelta(minutes=index))
        returned = persist_snapshot(CURRENT, FORECAST, [], fetched_at=now - timedelta(days=2))
        remaining = MiniProgramSnapshot.query.all()

    assert len(remaining) == 3
    assert returned.id in {item.id for item in remaining}


def test_wechat_login_limits_active_sessions_per_identity(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramSession

    _configure_wechat(app)
    app.config["WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS"] = 5
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse("multi-device-openid"),
    )
    tokens = []
    for index in range(6):
        response = client.post(
            "/mp/api/v1/auth/wechat",
            json={"code": f"code-{index}", "privacy_consent_version": "privacy-v1"},
            environ_overrides={"REMOTE_ADDR": f"203.0.113.{index + 1}"},
        )
        assert response.status_code == 200
        tokens.append(response.get_json()["data"]["session_token"])

    with app.app_context():
        active = MiniProgramSession.query.filter(MiniProgramSession.revoked_at.is_(None)).count()
        assert active == 5
    assert client.get(
        "/mp/api/v1/me", headers={"Authorization": f"Bearer {tokens[0]}"}
    ).status_code == 401
    assert client.get(
        "/mp/api/v1/me", headers={"Authorization": f"Bearer {tokens[-1]}"}
    ).status_code == 200
