# -*- coding: utf-8 -*-

import os
import threading
import time as stdlib_time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import qweather_auth


@pytest.fixture(autouse=True)
def clear_token_cache():
    qweather_auth.invalidate_qweather_token()
    yield
    qweather_auth.invalidate_qweather_token()


@pytest.fixture
def jwt_material(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_path = tmp_path / "qweather-ed25519-private.pem"
    key_path.write_bytes(private_pem)
    key_path.chmod(0o600)
    config = {
        "QWEATHER_AUTH_MODE": "jwt",
        "QWEATHER_JWT_KID": "KID1234567",
        "QWEATHER_JWT_PROJECT_ID": "PROJECT1234",
        "QWEATHER_JWT_PRIVATE_KEY_PATH": str(key_path),
    }
    return config, key_path, public_pem


def test_api_key_mode_sends_only_api_key_header():
    config = {
        "QWEATHER_AUTH_MODE": "api_key",
        "QWEATHER_KEY": "unit-test-key",
    }

    headers = qweather_auth.get_qweather_request_headers(
        config,
        api_base="https://unit-test.qweatherapi.com/v7",
    )

    assert headers == {"X-QW-Api-Key": "unit-test-key"}


def test_jwt_header_signature_and_claims(jwt_material, monkeypatch):
    config, _key_path, public_pem = jwt_material
    now = int(stdlib_time.time())
    monkeypatch.setattr(qweather_auth.time, "time", lambda: now)

    headers = qweather_auth.get_qweather_request_headers(
        config,
        api_base="https://unit-test.qweatherapi.com/v7",
    )

    assert set(headers) == {"Authorization"}
    token = headers["Authorization"].removeprefix("Bearer ")
    token_header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, public_pem, algorithms=["EdDSA"])
    assert token_header["alg"] == "EdDSA"
    assert token_header["kid"] == "KID1234567"
    assert claims == {
        "sub": "PROJECT1234",
        "iat": now - 30,
        "exp": now + 900,
    }


def test_jwt_cache_refreshes_one_minute_before_expiry(jwt_material, monkeypatch):
    config, _key_path, _public_pem = jwt_material
    clock = {"now": 1000}
    generated = []

    def fake_generate(_path, _kid, _project_id, now):
        generated.append(now)
        return f"token-{len(generated)}", now + 900

    monkeypatch.setattr(qweather_auth.time, "time", lambda: clock["now"])
    monkeypatch.setattr(qweather_auth, "_generate_jwt", fake_generate)

    first = qweather_auth.get_qweather_request_headers(config)
    clock["now"] = 1839
    second = qweather_auth.get_qweather_request_headers(config)
    clock["now"] = 1840
    third = qweather_auth.get_qweather_request_headers(config)

    assert first == second == {"Authorization": "Bearer token-1"}
    assert third == {"Authorization": "Bearer token-2"}
    assert generated == [1000, 1840]


def test_concurrent_requests_sign_only_once(jwt_material, monkeypatch):
    config, _key_path, _public_pem = jwt_material
    calls = []
    calls_lock = threading.Lock()

    def fake_generate(_path, _kid, _project_id, now):
        with calls_lock:
            calls.append(now)
        stdlib_time.sleep(0.02)
        return "shared-token", now + 900

    monkeypatch.setattr(qweather_auth, "_generate_jwt", fake_generate)

    with ThreadPoolExecutor(max_workers=8) as executor:
        headers = list(executor.map(lambda _index: qweather_auth.get_qweather_request_headers(config), range(16)))

    assert headers == [{"Authorization": "Bearer shared-token"}] * 16
    assert len(calls) == 1


@pytest.mark.parametrize("mode", [0o400, 0o644])
def test_jwt_rejects_private_key_without_exact_permissions(jwt_material, mode):
    config, key_path, _public_pem = jwt_material
    key_path.chmod(mode)

    with pytest.raises(qweather_auth.QWeatherAuthError, match="qweather_jwt_key_permissions"):
        qweather_auth.get_qweather_request_headers(config)


def test_jwt_rejects_symlink_private_key(jwt_material, tmp_path):
    config, key_path, _public_pem = jwt_material
    linked_key = tmp_path / "linked-qweather-private.pem"
    linked_key.symlink_to(key_path)
    linked_config = {
        **config,
        "QWEATHER_JWT_PRIVATE_KEY_PATH": str(linked_key),
    }

    with pytest.raises(qweather_auth.QWeatherAuthError, match="qweather_jwt_key_not_regular"):
        qweather_auth.get_qweather_request_headers(linked_config)


def test_jwt_rejects_private_key_changed_during_read(jwt_material, monkeypatch):
    config, key_path, _public_pem = jwt_material
    real_read = os.read
    changed = False

    def change_key_after_read(file_descriptor, size):
        nonlocal changed
        chunk = real_read(file_descriptor, size)
        if chunk and not changed:
            changed = True
            with key_path.open("ab") as output:
                output.write(b"\nchanged-during-read")
        return chunk

    monkeypatch.setattr(qweather_auth.os, "read", change_key_after_read)

    with pytest.raises(qweather_auth.QWeatherAuthError, match="qweather_jwt_key_changed"):
        qweather_auth.get_qweather_request_headers(config)


def test_jwt_rejects_private_key_path_replaced_during_read(
    jwt_material,
    tmp_path,
    monkeypatch,
):
    config, key_path, _public_pem = jwt_material
    replacement = tmp_path / "replacement-private.pem"
    replacement.write_bytes(key_path.read_bytes())
    replacement.chmod(0o600)
    real_read = os.read
    changed = False

    def replace_key_after_read(file_descriptor, size):
        nonlocal changed
        chunk = real_read(file_descriptor, size)
        if chunk and not changed:
            changed = True
            os.replace(replacement, key_path)
        return chunk

    monkeypatch.setattr(qweather_auth.os, "read", replace_key_after_read)

    with pytest.raises(qweather_auth.QWeatherAuthError, match="qweather_jwt_key_changed"):
        qweather_auth.get_qweather_request_headers(config)


def test_jwt_same_path_key_rotation_refreshes_cached_signature(
    jwt_material,
    monkeypatch,
):
    config, key_path, first_public_pem = jwt_material
    now = int(stdlib_time.time())
    monkeypatch.setattr(qweather_auth.time, "time", lambda: now)
    original_stat = key_path.stat()

    first_header = qweather_auth.get_qweather_request_headers(config)
    first_token = first_header["Authorization"].removeprefix("Bearer ")

    second_private_key = Ed25519PrivateKey.generate()
    second_private_pem = second_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    second_public_pem = second_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert len(second_private_pem) == original_stat.st_size
    key_path.write_bytes(second_private_pem)
    os.utime(
        key_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    rotated_stat = key_path.stat()
    assert rotated_stat.st_ino == original_stat.st_ino
    assert rotated_stat.st_size == original_stat.st_size
    assert rotated_stat.st_mtime_ns == original_stat.st_mtime_ns

    second_header = qweather_auth.get_qweather_request_headers(config)
    second_token = second_header["Authorization"].removeprefix("Bearer ")

    assert second_token != first_token
    assert jwt.decode(first_token, first_public_pem, algorithms=["EdDSA"])["sub"] == "PROJECT1234"
    assert jwt.decode(second_token, second_public_pem, algorithms=["EdDSA"])["sub"] == "PROJECT1234"


@pytest.mark.parametrize(
    "api_base,error_code",
    [
        ("http://unit-test.qweatherapi.com/v7", "qweather_api_base_invalid"),
        ("https://example.com/v7", "qweather_jwt_host_invalid"),
        ("https://unit-test.qweatherapi.com/weather", "qweather_jwt_base_path_invalid"),
        ("https://user@unit-test.qweatherapi.com/v7", "qweather_api_base_invalid"),
    ],
)
def test_jwt_rejects_untrusted_api_base(jwt_material, api_base, error_code):
    config, _key_path, _public_pem = jwt_material

    with pytest.raises(qweather_auth.QWeatherAuthError, match=error_code):
        qweather_auth.get_qweather_request_headers(config, api_base=api_base)


@pytest.mark.parametrize(
    'api_base,error_code',
    (
        ('https://example.com/v7', 'qweather_jwt_host_invalid'),
        ('https://unit-test.qweatherapi.com/weather', 'qweather_jwt_base_path_invalid'),
        ('https://unit-test.qweatherapi.com:8443/v7', 'qweather_api_base_invalid'),
    ),
)
def test_api_key_rejects_untrusted_api_base(api_base, error_code):
    config = {'QWEATHER_AUTH_MODE': 'api_key', 'QWEATHER_KEY': 'unit-test-key'}

    with pytest.raises(qweather_auth.QWeatherAuthError, match=error_code):
        qweather_auth.get_qweather_request_headers(config, api_base=api_base)


def test_disabled_and_incomplete_modes_fail_closed():
    assert not qweather_auth.is_qweather_configured({"QWEATHER_AUTH_MODE": "disabled"})
    assert not qweather_auth.is_qweather_configured({"QWEATHER_AUTH_MODE": "jwt"})
    with pytest.raises(qweather_auth.QWeatherAuthError, match="qweather_auth_disabled"):
        qweather_auth.get_qweather_request_headers({"QWEATHER_AUTH_MODE": "disabled"})


def test_auth_failure_does_not_use_budget_or_network(monkeypatch, tmp_path):
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_auth_mode = "jwt"
    service.qweather_jwt_kid = "KID1234567"
    service.qweather_jwt_project_id = "PROJECT1234"
    service.qweather_jwt_private_key_path = str(tmp_path / "missing.pem")
    service.api_base_url = "https://unit-test.qweatherapi.com/v7"
    service.canonical_location = "116.20,29.27"
    fallback = {"data_source": "Open-Meteo"}
    budget_calls = []

    monkeypatch.setattr(
        weather_module,
        "reserve_qweather_request",
        lambda endpoint: budget_calls.append(endpoint) or True,
    )
    monkeypatch.setattr(
        weather_module.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("认证失败后不应发送网络请求"),
    )
    monkeypatch.setattr(service, "_get_fallback_weather", lambda *_args: fallback)

    assert service.get_current_weather("都昌") == fallback
    assert budget_calls == []


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("_get_qweather_air_quality", ("116.20,29.27",)),
        ("_get_qweather_hourly_extremes", ("116.20,29.27",)),
        ("_get_qweather_today_extremes", ("116.20,29.27",)),
        ("get_qweather_daily_forecast", ("都昌", 7)),
        ("get_weather_forecast", ("都昌", 7)),
    ],
)
def test_each_weather_path_authenticates_before_budget(
    method_name,
    args,
    monkeypatch,
    tmp_path,
):
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_auth_mode = "jwt"
    service.qweather_jwt_kid = "KID1234567"
    service.qweather_jwt_project_id = "PROJECT1234"
    service.qweather_jwt_private_key_path = str(tmp_path / "missing.pem")
    service.api_base_url = "https://unit-test.qweatherapi.com/v7"
    service.canonical_location = "116.20,29.27"
    service.use_openmeteo_fallback = False
    budget_calls = []

    monkeypatch.setattr(
        weather_module,
        "reserve_qweather_request",
        lambda endpoint: budget_calls.append(endpoint) or True,
    )
    monkeypatch.setattr(
        weather_module.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("认证失败后不应发送网络请求"),
    )

    getattr(service, method_name)(*args)

    assert budget_calls == []


def test_weather_service_uses_jwt_for_seven_day_forecast(jwt_material, monkeypatch):
    from services import weather_service as weather_module

    config, _key_path, _public_pem = jwt_material
    service = weather_module.WeatherService()
    service.qweather_auth_mode = "jwt"
    service.qweather_jwt_kid = config["QWEATHER_JWT_KID"]
    service.qweather_jwt_project_id = config["QWEATHER_JWT_PROJECT_ID"]
    service.qweather_jwt_private_key_path = config["QWEATHER_JWT_PRIVATE_KEY_PATH"]
    service.api_base_url = "https://unit-test.qweatherapi.com/v7"
    service.canonical_location = "116.20,29.27"
    requests_seen = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "code": "200",
                "updateTime": "2026-07-17T12:00+08:00",
                "daily": [
                    {
                        "fxDate": f"2026-07-{17 + index:02d}",
                        "tempMax": "35",
                        "tempMin": "27",
                        "humidity": "70",
                        "textDay": "晴",
                    }
                    for index in range(7)
                ],
            }

    def fake_get(url, **kwargs):
        requests_seen.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(weather_module.requests, "get", fake_get)
    monkeypatch.setattr(weather_module, "_record_external_api_timing", lambda *_args: None)
    monkeypatch.setattr(weather_module, "reserve_qweather_request", lambda _endpoint: True)

    result = service.get_qweather_daily_forecast("任意村庄", days=7)

    assert result["success"] is True
    assert len(result["daily"]) == 7
    assert len(requests_seen) == 1
    url, kwargs = requests_seen[0]
    assert url == "https://unit-test.qweatherapi.com/v7/weather/7d"
    assert kwargs["params"] == {"location": "116.20,29.27"}
    assert set(kwargs["headers"]) == {"Authorization"}
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")
