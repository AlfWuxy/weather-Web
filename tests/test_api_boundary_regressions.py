# -*- coding: utf-8 -*-
"""API 与预报输入边界回归测试。"""

from datetime import date
import json
import math
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _create_mp_user_and_token(app, db_session, *, uid="UID_KEEP", push_enabled=True):
    from core.db_models import User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    with app.app_context():
        app.config["WXPUSHER_APP_TOKEN"] = "AT_test-wxpusher-token"
        user = User(
            username="mp_boundary_user",
            role="user",
            wxpusher_uid=uid,
            push_enabled=push_enabled,
            wxpusher_consent_version=(
                app.config["WX_MINIPROGRAM_PRIVACY_VERSION"]
                if push_enabled
                else None
            ),
            wxpusher_consented_at=utcnow() if push_enabled else None,
        )
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()
        return user.id, create_api_token(user.id, name="boundary")


def test_mp_me_patch_preserves_omitted_fields_and_disables_push_when_uid_removed(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(app, db_session)
    headers = {"Authorization": f"Bearer {token}"}

    from core.db_models import User

    with app.app_context():
        original_consent_time = db_session.get(User, user_id).wxpusher_consented_at

    uid_response = client.patch(
        "/mp/api/v1/me",
        json={"wxpusher_uid": "UID_CHANGED_WITH_CURRENT_RECEIPT"},
        headers=headers,
    )
    assert uid_response.status_code == 200
    assert uid_response.get_json()["data"]["push_enabled"] is True
    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid == "UID_CHANGED_WITH_CURRENT_RECEIPT"
        assert user.wxpusher_consented_at == original_consent_time

    disable_response = client.patch(
        "/mp/api/v1/me",
        json={"push_enabled": "false"},
        headers=headers,
    )
    assert disable_response.status_code == 200
    assert disable_response.get_json()["data"] == {
        "wxpusher_uid": "UID_CHANGED_WITH_CURRENT_RECEIPT",
        "push_enabled": False,
        "wxpusher_available": True,
        "required_wxpusher_consent_version": app.config["WX_MINIPROGRAM_PRIVACY_VERSION"],
        "wxpusher_reconsent_required": False,
    }

    enable_response = client.patch(
        "/mp/api/v1/me",
        json={
            "push_enabled": True,
            "wxpusher_consent": True,
            "wxpusher_consent_version": app.config["WX_MINIPROGRAM_PRIVACY_VERSION"],
        },
        headers=headers,
    )
    assert enable_response.status_code == 200
    assert enable_response.get_json()["data"]["push_enabled"] is True

    remove_uid_response = client.patch(
        "/mp/api/v1/me",
        json={"wxpusher_uid": ""},
        headers=headers,
    )
    assert remove_uid_response.status_code == 200
    assert remove_uid_response.get_json()["data"] == {
        "wxpusher_uid": None,
        "push_enabled": False,
        "wxpusher_available": True,
        "required_wxpusher_consent_version": app.config["WX_MINIPROGRAM_PRIVACY_VERSION"],
        "wxpusher_reconsent_required": False,
    }

    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid is None
        assert user.push_enabled is False
        assert user.wxpusher_consent_version == app.config["WX_MINIPROGRAM_PRIVACY_VERSION"]
        assert user.wxpusher_consented_at is not None


def test_mp_me_patch_rejects_ambiguous_boolean_without_partial_update(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(app, db_session)

    response = client.patch(
        "/mp/api/v1/me",
        json={"wxpusher_uid": "UID_CHANGED", "push_enabled": "yes"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_push_enabled"

    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid == "UID_KEEP"
        assert user.push_enabled is True


def test_mp_me_patch_requires_explicit_wxpusher_consent_without_partial_update(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(app, db_session, push_enabled=False)

    response = client.patch(
        '/mp/api/v1/me',
        json={'wxpusher_uid': 'UID_CHANGED', 'push_enabled': True},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert response.status_code == 400
    assert response.get_json()['error'] == 'wxpusher_consent_required'

    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid == 'UID_KEEP'
        assert user.push_enabled is False


@pytest.mark.parametrize("submitted_version", (None, "privacy-old"))
def test_mp_me_patch_rejects_missing_or_stale_wxpusher_consent_version(
    app,
    client,
    db_session,
    submitted_version,
):
    user_id, token = _create_mp_user_and_token(app, db_session, push_enabled=False)
    payload = {
        "wxpusher_uid": "UID_CHANGED",
        "push_enabled": True,
        "wxpusher_consent": True,
    }
    if submitted_version is not None:
        payload["wxpusher_consent_version"] = submitted_version

    response = client.patch(
        "/mp/api/v1/me",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "wxpusher_consent_version_mismatch"
    assert body["data"]["required_wxpusher_consent_version"] == app.config[
        "WX_MINIPROGRAM_PRIVACY_VERSION"
    ]
    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid == "UID_KEEP"
        assert user.push_enabled is False
        assert user.wxpusher_consent_version is None
        assert user.wxpusher_consented_at is None


def test_mp_me_returns_wxpusher_version_and_flags_stale_enabled_receipt(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(app, db_session)
    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        user.wxpusher_consent_version = "privacy-old"
        db_session.commit()

    response = client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["required_wxpusher_consent_version"] == app.config[
        "WX_MINIPROGRAM_PRIVACY_VERSION"
    ]
    assert data["wxpusher_reconsent_required"] is True


def test_mp_me_patch_requires_reconsent_for_stale_enabled_receipt(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(app, db_session)
    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        user.wxpusher_consent_version = "privacy-old"
        db_session.commit()

    rejected = client.patch(
        "/mp/api/v1/me",
        json={"wxpusher_uid": "UID_STALE_ATTEMPT"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["error"] == "wxpusher_consent_required"

    accepted = client.patch(
        "/mp/api/v1/me",
        json={
            "wxpusher_uid": "UID_RECONSENTED",
            "wxpusher_consent": True,
            "wxpusher_consent_version": app.config["WX_MINIPROGRAM_PRIVACY_VERSION"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert accepted.status_code == 200
    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid == "UID_RECONSENTED"
        assert user.wxpusher_consent_version == app.config[
            "WX_MINIPROGRAM_PRIVACY_VERSION"
        ]
        assert user.wxpusher_consented_at is not None


def test_mp_me_patch_rejects_enable_when_wxpusher_is_unavailable_without_partial_update(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(
        app,
        db_session,
        push_enabled=False,
    )
    app.config['WXPUSHER_APP_TOKEN'] = ''

    response = client.patch(
        '/mp/api/v1/me',
        json={
            'wxpusher_uid': 'UID_CHANGED',
            'push_enabled': True,
            'wxpusher_consent': True,
        },
        headers={'Authorization': f'Bearer {token}'},
    )

    assert response.status_code == 503
    assert response.get_json()['error'] == 'wxpusher_unavailable'

    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.wxpusher_uid == 'UID_KEEP'
        assert user.push_enabled is False


def test_mp_me_patch_allows_disable_when_wxpusher_is_unavailable(
    app,
    client,
    db_session,
):
    user_id, token = _create_mp_user_and_token(app, db_session)
    app.config['WXPUSHER_APP_TOKEN'] = ''

    response = client.patch(
        '/mp/api/v1/me',
        json={'push_enabled': False},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert response.status_code == 200
    assert response.get_json()['data'] == {
        'wxpusher_uid': 'UID_KEEP',
        'push_enabled': False,
        'wxpusher_available': False,
        'required_wxpusher_consent_version': app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
        'wxpusher_reconsent_required': False,
    }

    from core.db_models import User

    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.push_enabled is False


def test_mp_events_reports_usage_write_failure(app, client, db_session, monkeypatch):
    import blueprints.mp_api as mp_api_module

    _, token = _create_mp_user_and_token(app, db_session)
    monkeypatch.setattr(mp_api_module, "log_usage_event", lambda *args, **kwargs: None)

    response = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "success": False,
        "error": "event_write_failed",
    }


def test_web_events_reports_usage_write_failure(
    authenticated_client,
    monkeypatch,
):
    import services.api_service as api_service_module

    monkeypatch.setattr(api_service_module, "log_usage_event", lambda *args, **kwargs: None)
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "event-boundary-csrf"

    response = authenticated_client.post(
        "/api/v1/events",
        json={"event_type": "template_copy"},
        headers={"X-CSRF-Token": "event-boundary-csrf"},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "success": False,
        "error": "event_write_failed",
    }


def test_usage_event_metadata_keeps_only_anonymous_dimensions(app, db_session):
    """自由文本和个人信息不得进入第一方产品分析事件。"""
    from core.db_models import UsageEvent
    from core.usage import log_usage_event

    with app.app_context():
        event = log_usage_event(
            'template_view',
            source='private@example.com',
            meta={
                'name': '某位老人',
                'phone': '13800000000',
                'location_query': '某村某组',
                'error': 'private upstream response',
                'actions_done_count': 3,
                'has_note': True,
                'relay_stage': 'caregiver',
                'updated_fields': ['age', 'phone', 'age'],
                'from': 'family_share',
                'article': 'heat_alert',
                'arbitrary': {'nested': 'private'},
            },
        )

        stored = db_session.get(UsageEvent, event.id)
        assert stored.source == 'web'
        assert json.loads(stored.meta_json) == {
            'actions_done_count': 3,
            'article': 'heat_alert',
            'from': 'family_share',
            'has_error': True,
            'has_note': True,
            'location_scope': 'duchang_county',
            'relay_stage': 'caregiver',
        }
        assert '某位老人' not in stored.meta_json
        assert '13800000000' not in stored.meta_json
        assert '某村某组' not in stored.meta_json
        assert 'updated_fields' not in stored.meta_json


@pytest.mark.parametrize("invalid_temp", [float("nan"), float("inf"), float("-inf")])
def test_forecast_api_rejects_nonfinite_temperatures(
    authenticated_client,
    monkeypatch,
    invalid_temp,
):
    import services.forecast_service as forecast_module

    monkeypatch.setattr(forecast_module, "get_forecast_service", lambda: object())
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "forecast-boundary-csrf"

    response = authenticated_client.post(
        "/api/forecast/7day",
        json={"forecast_temps": [invalid_temp, 21, 22, 23, 24, 25, 26]},
        headers={"X-CSRF-Token": "forecast-boundary-csrf"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_forecast_temps"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"temperature": float("nan")}, "invalid_temperature"),
        (
            {"temperature": 20, "lag_temperatures": [20, float("inf")]},
            "invalid_lag_temperatures",
        ),
    ],
)
def test_forecast_daily_api_rejects_nonfinite_temperatures_before_prediction(
    authenticated_client,
    monkeypatch,
    payload,
    expected_error,
):
    import services.forecast_service as forecast_module

    monkeypatch.setattr(
        forecast_module,
        "get_forecast_service",
        lambda: (_ for _ in ()).throw(AssertionError("无效输入不应触发预测")),
    )
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "forecast-daily-boundary-csrf"

    response = authenticated_client.post(
        "/api/forecast/daily",
        json=payload,
        headers={"X-CSRF-Token": "forecast-daily-boundary-csrf"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_error


def test_forecast_daily_api_preserves_legacy_lag_fallback_values(
    authenticated_client,
    monkeypatch,
):
    import services.forecast_service as forecast_module

    captured = {}

    class ForecastStub:
        def predict_daily_visits(self, temperature, lag_temps, month, dow):
            captured.update(
                temperature=temperature,
                lag_temps=lag_temps,
                month=month,
                dow=dow,
            )
            return {"point_estimate": 1}

    monkeypatch.setattr(forecast_module, "get_forecast_service", ForecastStub)
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "forecast-daily-compatibility-csrf"

    response = authenticated_client.post(
        "/api/forecast/daily",
        json={"temperature": "bad", "lag_temperatures": [20, None, "bad"]},
        headers={"X-CSRF-Token": "forecast-daily-compatibility-csrf"},
    )

    assert response.status_code == 200
    assert captured["temperature"] == "bad"
    assert captured["lag_temps"] == [20, None, "bad"]


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"temperature": float("inf")}, "invalid_temperature"),
        (
            {"temperature": 20, "lag_temperatures": [20, float("nan")]},
            "invalid_lag_temperatures",
        ),
    ],
)
def test_dlnm_api_rejects_nonfinite_temperatures_before_calculation(
    authenticated_client,
    monkeypatch,
    payload,
    expected_error,
):
    import services.dlnm_risk_service as dlnm_module

    monkeypatch.setattr(
        dlnm_module,
        "get_dlnm_service",
        lambda: (_ for _ in ()).throw(AssertionError("无效输入不应触发风险计算")),
    )
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "dlnm-boundary-csrf"

    response = authenticated_client.post(
        "/api/dlnm/risk",
        json=payload,
        headers={"X-CSRF-Token": "dlnm-boundary-csrf"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_error


@pytest.mark.parametrize("invalid_temp", [float("nan"), float("inf"), float("-inf")])
def test_forecast_service_rejects_nonfinite_direct_input(invalid_temp):
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)

    with pytest.raises(ValueError, match="finite"):
        service.generate_7day_forecast(
            [invalid_temp, 21, 22, 23, 24, 25, 26],
            start_date=date(2026, 7, 15),
        )


def test_forecast_normalization_never_propagates_nonfinite_optional_values():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    normalized = service._normalize_forecast_entry(
        {
            "temperature": float("nan"),
            "temperature_max": float("inf"),
            "temperature_min": float("-inf"),
            "humidity": float("nan"),
        }
    )

    assert math.isfinite(normalized["temp"])
    assert normalized["temp_max"] is None
    assert normalized["temp_min"] is None
    assert normalized["humidity"] is None


def test_miniprogram_temperature_views_preserve_zero_celsius():
    care_logic = (PROJECT_ROOT / "miniprogram/pages/elders/care-logic.js").read_text(
        encoding="utf-8"
    )
    public_format = (PROJECT_ROOT / "miniprogram/utils/format.js").read_text(
        encoding="utf-8"
    )

    # 新小程序由共享纯函数归一化温度。这里只锁定结构，0°C 行为由 Node 测试执行验证。
    assert "if (value === '' || value == null) return null;" in care_logic
    assert "temperatureMax: tmax" in care_logic
    assert "temperatureMin: tmin" in care_logic
    assert "if (value === null || value === undefined || value === '') return null;" in public_format
    assert "temperatureText: formatTemperature(temperature)" in public_format
