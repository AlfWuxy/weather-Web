# -*- coding: utf-8 -*-

import pytest


@pytest.fixture(autouse=True)
def isolate_warning_cache(monkeypatch):
    from services import warning_service

    warning_service._LOCAL_WARNING_CACHE.clear()
    monkeypatch.setattr(warning_service, "get_qweather_redis_client", lambda: None)
    monkeypatch.setattr(warning_service, "reserve_qweather_request", lambda _endpoint: True)


def test_warning_service_no_key_returns_empty(app):
    from services.warning_service import get_qweather_warnings

    with app.app_context():
        app.config["QWEATHER_KEY"] = ""
        warnings = get_qweather_warnings("116.20,29.27")
        assert warnings == []


def test_warning_result_marks_unconfigured_service_unavailable(app):
    from services.warning_service import get_qweather_warnings_result

    with app.app_context():
        app.config["QWEATHER_KEY"] = ""
        app.config["QWEATHER_API_BASE"] = "https://unit-test.qweatherapi.com/v7"

        assert get_qweather_warnings_result("116.20,29.27") == {
            "available": False,
            "status": "not_configured",
            "warnings": [],
        }


def test_warning_result_marks_successful_empty_response_available(app, monkeypatch):
    from services import warning_service

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"metadata": {"zeroResult": True}, "alerts": []}

    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResp(),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )

        assert warning_service.get_qweather_warnings_result("都昌县") == {
            "available": True,
            "status": "ok",
            "warnings": [],
        }
        assert warning_service.get_qweather_warnings("都昌县") == []

    assert len(calls) == 1


def test_warning_service_parses_weatheralert_v1_payload(app, monkeypatch):
    from services.warning_service import get_qweather_warnings

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "metadata": {
                    "zeroResult": False,
                    "attributions": [
                        "https://developer.qweather.com/attribution.html",
                        "Alert data may be delayed or out of date.",
                    ],
                },
                "alerts": [
                    {
                        "id": "alert-1",
                        "messageType": {"code": "update", "supersedes": ["alert-0"]},
                        "eventType": {"name": "高温", "code": "11B01"},
                        "headline": "高温黄色预警",
                        "description": "请注意防暑降温",
                        "onsetTime": "2026-07-17T08:00+08:00",
                        "expireTime": "2026-07-17T20:00+08:00",
                        "color": {"code": "Yellow"},
                        "severity": "Severe",
                        "certainty": "Observed",
                        "urgency": "Immediate",
                        "responseTypes": ["Prepare", "Monitor"],
                        "instruction": "减少户外活动",
                    }
                ],
            }

    monkeypatch.setattr("services.warning_service.requests.get", lambda *args, **kwargs: FakeResp())

    with app.app_context():
        app.config["QWEATHER_KEY"] = "x"
        app.config["QWEATHER_API_BASE"] = "https://example.com/v7"

        warnings = get_qweather_warnings("116.20,29.27")
        assert len(warnings) == 1
        item = warnings[0]
        assert item["title"] == "高温黄色预警"
        assert item["type"] == "高温"
        assert item["level"] == "黄色"
        assert item["text"] == "请注意防暑降温"
        assert item["start_time"] == "2026-07-17T08:00+08:00"
        assert item["end_time"] == "2026-07-17T20:00+08:00"
        assert item["severity"] == "Severe"
        assert item["certainty"] == "Observed"
        assert item["urgency"] == "Immediate"
        assert item["response"] == "Prepare,Monitor"
        assert item["instruction"] == "减少户外活动"
        assert item["source_id"] == "alert-1"
        assert item["message_type"] == "update"
        assert item["supersedes"] == ["alert-0"]
        assert item["attributions"][0] == "https://developer.qweather.com/attribution.html"


def test_warning_service_keeps_legacy_v7_payload_compatible(app, monkeypatch):
    from services.warning_service import get_qweather_warnings

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "code": "200",
                "warning": [{
                    "title": "高温黄色预警",
                    "typeName": "高温",
                    "level": "黄色",
                    "text": "请注意防暑降温",
                    "startTime": "2026-02-09T08:00+08:00",
                    "endTime": "2026-02-09T20:00+08:00",
                }],
            }

    monkeypatch.setattr("services.warning_service.requests.get", lambda *args, **kwargs: FakeResp())

    with app.app_context():
        app.config["QWEATHER_KEY"] = "x"
        app.config["QWEATHER_API_BASE"] = "https://unit-test.qweatherapi.com/v7"
        app.config["QWEATHER_CANONICAL_LOCATION"] = "116.20,29.27"

        item = get_qweather_warnings("任意村庄")[0]

    assert item["title"] == "高温黄色预警"
    assert item["type"] == "高温"
    assert item["level"] == "黄色"
    assert item["severity"] == "Minor"


def test_warning_service_reuses_duchang_cache_and_hides_key_from_url(app, monkeypatch):
    from services.warning_service import get_qweather_warnings

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"metadata": {"zeroResult": True}, "alerts": []}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResp()

    monkeypatch.setattr("services.warning_service.requests.get", fake_get)

    with app.app_context():
        app.config["QWEATHER_KEY"] = "secret-test-key"
        app.config["QWEATHER_API_BASE"] = "https://unit-test.qweatherapi.com/v7"
        app.config["QWEATHER_CANONICAL_LOCATION"] = "116.20,29.27"

        assert get_qweather_warnings("101010100") == []
        assert get_qweather_warnings("101020100") == []

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://unit-test.qweatherapi.com/weatheralert/v1/current/29.27/116.20"
    assert kwargs["params"] == {"localTime": "true", "lang": "zh"}
    assert kwargs["headers"] == {"X-QW-Api-Key": "secret-test-key"}
    assert "secret-test-key" not in url


def test_warning_service_uses_only_jwt_header(app, monkeypatch):
    from services import warning_service

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"metadata": {"zeroResult": True}, "alerts": []}

    monkeypatch.setattr(
        warning_service,
        "get_qweather_request_headers",
        lambda **_kwargs: {"Authorization": "Bearer unit-test-token"},
    )
    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda url, **kwargs: calls.append((url, kwargs)) or FakeResp(),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_AUTH_MODE="jwt",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_JWT_KID="KID1234567",
            QWEATHER_JWT_PROJECT_ID="PROJECT1234",
            QWEATHER_JWT_PRIVATE_KEY_PATH="/server-only/private.pem",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )
        assert warning_service.get_qweather_warnings("任意村庄") == []

    assert len(calls) == 1
    assert calls[0][0] == "https://unit-test.qweatherapi.com/weatheralert/v1/current/29.27/116.20"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer unit-test-token"}


def test_warning_auth_failure_does_not_use_budget_or_network(app, monkeypatch):
    from services import warning_service
    from services.qweather_auth import QWeatherAuthError

    budget_calls = []
    monkeypatch.setattr(
        warning_service,
        "get_qweather_request_headers",
        lambda **_kwargs: (_ for _ in ()).throw(QWeatherAuthError("qweather_jwt_sign_failed")),
    )
    monkeypatch.setattr(
        warning_service,
        "reserve_qweather_request",
        lambda endpoint: budget_calls.append(endpoint) or True,
    )
    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("认证失败后不应发送网络请求"),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_AUTH_MODE="jwt",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_JWT_KID="KID1234567",
            QWEATHER_JWT_PROJECT_ID="PROJECT1234",
            QWEATHER_JWT_PRIVATE_KEY_PATH="/server-only/private.pem",
        )
        assert warning_service.get_qweather_warnings_result("116.20,29.27") == {
            "available": False,
            "status": "auth_error",
            "warnings": [],
        }

    assert budget_calls == []


def test_warning_invalid_canonical_coordinates_stop_before_auth_budget_and_network(app, monkeypatch):
    from services import warning_service

    monkeypatch.setattr(
        warning_service,
        "get_qweather_request_headers",
        lambda **_kwargs: pytest.fail("无效坐标不应生成认证头"),
    )
    monkeypatch.setattr(
        warning_service,
        "reserve_qweather_request",
        lambda _endpoint: pytest.fail("无效坐标不应消耗额度"),
    )
    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("无效坐标不应发送网络请求"),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="invalid-location",
        )
        assert warning_service.get_qweather_warnings_result("任意村庄") == {
            "available": False,
            "status": "invalid_location",
            "warnings": [],
        }


def test_warning_http_401_invalidates_token_once_without_retry(app, monkeypatch):
    from services import warning_service

    calls = []
    invalidations = []
    budget_calls = []

    class FakeResp:
        status_code = 401

    monkeypatch.setattr(
        warning_service,
        "reserve_qweather_request",
        lambda endpoint: budget_calls.append(endpoint) or True,
    )
    monkeypatch.setattr(
        warning_service,
        "invalidate_qweather_token",
        lambda: invalidations.append(True),
    )
    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda url, **kwargs: calls.append((url, kwargs)) or FakeResp(),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )
        assert warning_service.get_qweather_warnings_result("都昌县") == {
            "available": False,
            "status": "auth_error",
            "warnings": [],
        }

    assert len(calls) == 1
    assert invalidations == [True]
    assert budget_calls == ["weatheralert_v1_current"]


def test_warning_malformed_alerts_are_not_cached(app, monkeypatch):
    from services import warning_service

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"alerts": "invalid"}

    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResp(),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )
        assert warning_service.get_qweather_warnings_result("都昌") == {
            "available": False,
            "status": "parse_error",
            "warnings": [],
        }
        assert warning_service.get_qweather_warnings("都昌县") == []

    assert len(calls) == 2


def test_warning_result_marks_budget_block_unavailable(app, monkeypatch):
    from services import warning_service

    monkeypatch.setattr(warning_service, "reserve_qweather_request", lambda _endpoint: False)
    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("额度阻断后不应发送网络请求"),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )

        assert warning_service.get_qweather_warnings_result("都昌县") == {
            "available": False,
            "status": "budget_blocked",
            "warnings": [],
        }


def test_warning_result_distinguishes_http_network_and_parse_failures(app, monkeypatch):
    from services import warning_service

    class HttpErrorResp:
        status_code = 503

    class ParseErrorResp:
        status_code = 200

        def json(self):
            raise ValueError("invalid json")

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )

        monkeypatch.setattr(
            warning_service.requests,
            "get",
            lambda *_args, **_kwargs: HttpErrorResp(),
        )
        assert warning_service.get_qweather_warnings_result("都昌县") == {
            "available": False,
            "status": "http_error",
            "warnings": [],
        }

        def raise_timeout(*_args, **_kwargs):
            raise warning_service.requests.Timeout("offline")

        monkeypatch.setattr(warning_service.requests, "get", raise_timeout)
        assert warning_service.get_qweather_warnings_result("都昌县") == {
            "available": False,
            "status": "network_error",
            "warnings": [],
        }

        monkeypatch.setattr(
            warning_service.requests,
            "get",
            lambda *_args, **_kwargs: ParseErrorResp(),
        )
        assert warning_service.get_qweather_warnings_result("都昌县") == {
            "available": False,
            "status": "parse_error",
            "warnings": [],
        }


def test_warning_cancel_message_is_filtered_and_cached(app, monkeypatch):
    from services import warning_service

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "metadata": {"zeroResult": False},
                "alerts": [{
                    "id": "cancel-1",
                    "messageType": {"code": "cancel", "supersedes": ["alert-1"]},
                    "eventType": {"name": "高温", "code": "11B01"},
                    "headline": "高温预警解除",
                    "severity": "minor",
                }],
            }

    monkeypatch.setattr(
        warning_service.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResp(),
    )

    with app.app_context():
        app.config.update(
            QWEATHER_KEY="x",
            QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
            QWEATHER_CANONICAL_LOCATION="116.20,29.27",
        )
        assert warning_service.get_qweather_warnings("村庄甲") == []
        assert warning_service.get_qweather_warnings("村庄乙") == []

    assert len(calls) == 1


def test_weatheralert_url_rounds_coordinates_to_two_decimals():
    from services.warning_service import _weatheralert_v1_url

    assert _weatheralert_v1_url(
        "https://unit-test.qweatherapi.com/v7",
        "116.2044,29.2761",
    ) == "https://unit-test.qweatherapi.com/weatheralert/v1/current/29.28/116.20"
