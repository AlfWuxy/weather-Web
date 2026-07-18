# -*- coding: utf-8 -*-
"""候选发布配置 readiness 测试。"""

import os

from scripts.validate_release_env import (
    validate_release_env,
    validate_wechat_release_form,
)


def _write_env(tmp_path, extra=""):
    path = tmp_path / ".env"
    path.write_text(
        "PUBLIC_BASE_URL=https://api.example.com\n"
        "ALLOW_INSECURE_PUBLIC_BASE_URL=\n"
        "ALLOW_WEATHER_UNAVAILABLE=1\n"
        "QWEATHER_AUTH_MODE=disabled\n"
        + extra,
        encoding="utf-8",
    )
    return path


def test_validator_allows_explicit_pending_wechat_for_preview(tmp_path):
    result = validate_release_env(_write_env(tmp_path), require_wechat=False)

    assert result["ok"] is True
    assert result["wechat_ready"] is False
    assert result["warnings"]


def test_validator_rejects_server_only_wechat_materials_for_preview(tmp_path):
    server_only = _write_env(
        tmp_path,
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n",
    )
    result = validate_release_env(server_only, require_wechat=False)

    assert result["ok"] is False
    assert any("不能脱离" in error for error in result["errors"])


def test_validator_requires_all_wechat_values_for_formal_release(tmp_path):
    partial = _write_env(tmp_path, "WX_MINIPROGRAM_APPID=wx123456\n")
    result = validate_release_env(partial, require_wechat=True)

    assert result["ok"] is False
    assert any("同时填写" in error for error in result["errors"])

    ready = _write_env(
        tmp_path,
        "WX_MINIPROGRAM_APPID=wx123456\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://weather.example.com/v7\n",
    )
    ready_result = validate_release_env(ready, require_wechat=True)
    assert ready_result["ok"] is True
    assert ready_result["wechat_ready"] is True


def test_validator_rejects_incomplete_qweather_or_insecure_public_url(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "PUBLIC_BASE_URL=http://api.example.com\n"
        "ALLOW_INSECURE_PUBLIC_BASE_URL=\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=secret\n"
        "QWEATHER_API_BASE=\n",
        encoding="utf-8",
    )

    result = validate_release_env(path)

    assert result["ok"] is False
    assert len(result["errors"]) == 2


def test_validator_requires_weather_or_explicit_degraded_mode(tmp_path):
    disabled = tmp_path / "disabled.env"
    disabled.write_text(
        "PUBLIC_BASE_URL=https://api.example.com\n"
        "QWEATHER_AUTH_MODE=disabled\n",
        encoding="utf-8",
    )
    disabled_result = validate_release_env(disabled)

    assert disabled_result["ok"] is False
    assert disabled_result["weather_ready"] is False

    degraded_formal = _write_env(
        tmp_path,
        "WX_MINIPROGRAM_APPID=wx123456\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n",
    )
    formal_result = validate_release_env(degraded_formal, require_wechat=True)
    assert formal_result["ok"] is False
    assert any("天气同步" in error for error in formal_result["errors"])

    ready = tmp_path / "ready.env"
    ready.write_text(
        "PUBLIC_BASE_URL=https://api.example.com\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://weather.example.com/v7\n",
        encoding="utf-8",
    )
    ready_result = validate_release_env(ready)

    assert ready_result["ok"] is True
    assert ready_result["weather_ready"] is True


def test_validator_matches_qweather_jwt_host_path_and_private_key_rules(tmp_path):
    private_key = tmp_path / "qweather-private.pem"
    private_key.write_text("private-key-material", encoding="utf-8")
    private_key.chmod(0o600)

    def jwt_env(name, api_base, key_path=private_key):
        path = tmp_path / name
        path.write_text(
            "PUBLIC_BASE_URL=https://api.example.com\n"
            "QWEATHER_AUTH_MODE=jwt\n"
            f"QWEATHER_API_BASE={api_base}\n"
            "QWEATHER_JWT_KID=test-kid\n"
            "QWEATHER_JWT_PROJECT_ID=test-project\n"
            f"QWEATHER_JWT_PRIVATE_KEY_PATH={key_path}\n",
            encoding="utf-8",
        )
        return path

    ready = validate_release_env(
        jwt_env("ready.env", "https://unit-test.qweatherapi.com/v7")
    )
    assert ready["ok"] is True
    assert ready["weather_ready"] is True

    invalid_host = validate_release_env(
        jwt_env("invalid-host.env", "https://example.com/v7")
    )
    assert invalid_host["ok"] is False
    assert invalid_host["weather_ready"] is False
    assert any("JWT Host" in error for error in invalid_host["errors"])

    missing_key = validate_release_env(
        jwt_env(
            "missing-key.env",
            "https://unit-test.qweatherapi.com/v7",
            tmp_path / "missing.pem",
        )
    )
    assert missing_key["ok"] is False
    assert any("不存在" in error for error in missing_key["errors"])

    private_key.chmod(0o644)
    open_permissions = validate_release_env(
        jwt_env("open-key.env", "https://unit-test.qweatherapi.com/v7")
    )
    assert open_permissions["ok"] is False
    assert any("0600" in error for error in open_permissions["errors"])


def test_wechat_release_form_requires_private_complete_personal_form(tmp_path):
    missing = validate_wechat_release_form(
        tmp_path / ".env.wechat-release",
        require_ready=True,
    )
    assert missing["ok"] is False

    form = tmp_path / ".env.wechat-release"
    form.write_text(
        "WECHAT_SUBJECT_TYPE=personal\n"
        "WECHAT_MINIPROGRAM_NAME=宜老天气通\n"
        "WECHAT_OPERATOR_NAME=测试运营者\n"
        "WECHAT_CONTACT_EMAIL=operator@example.com\n"
        "WECHAT_EFFECTIVE_DATE=2026-07-18\n"
        "WECHAT_CATEGORY_CONFIRMED=1\n"
        "WX_MINIPROGRAM_APPID=wx12345678\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        "WX_MINIPROGRAM_PRIVACY_VERSION=2026-07-18\n"
        "WECHAT_FORM_READY=1\n",
        encoding="utf-8",
    )
    form.chmod(0o644)
    insecure = validate_wechat_release_form(form, require_ready=True)
    assert insecure["ok"] is False
    assert any("0600" in error for error in insecure["errors"])

    os.chmod(form, 0o600)
    ready = validate_wechat_release_form(form, require_ready=True)
    assert ready == {
        "ok": True,
        "form_ready": True,
        "category_confirmed": True,
        "warnings": [],
        "errors": [],
    }
