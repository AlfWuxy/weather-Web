# -*- coding: utf-8 -*-
"""候选发布配置 readiness 测试。"""

import hashlib
import json
import os
import subprocess
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

import scripts.validate_release_env as release_validator
from scripts.validate_release_env import (
    _qweather_required_month_reserve,
    _validate_qweather_console_baseline,
    _validate_gis_compressed_content,
    probe_persistent_budget_backend,
    seed_persistent_budget_baseline,
    snapshot_wechat_release_form,
    validate_release_env,
    validate_wechat_release_form,
)


_RELEASE_ARTIFACTS = {
    "WECHAT_PRIVACY_DOC_SHA256": "docs/miniprogram/PRIVACY_NOTICE_TEMPLATE.md",
    "WECHAT_AGREEMENT_DOC_SHA256": "docs/miniprogram/USER_AGREEMENT_TEMPLATE.md",
    "WECHAT_LISTING_COPY_SHA256": "docs/miniprogram/LISTING_COPY.md",
    "WECHAT_PRIVACY_PAGE_SHA256": "miniprogram/pages/privacy/index.wxml",
    "WECHAT_AGREEMENT_PAGE_SHA256": "miniprogram/pages/agreement/index.wxml",
}
_EFFECTIVE_DATE_ARTIFACTS = {
    "WECHAT_PRIVACY_DOC_SHA256",
    "WECHAT_AGREEMENT_DOC_SHA256",
    "WECHAT_PRIVACY_PAGE_SHA256",
    "WECHAT_AGREEMENT_PAGE_SHA256",
}
_PRIVACY_VERSION_ARTIFACTS = {
    "WECHAT_PRIVACY_DOC_SHA256",
    "WECHAT_PRIVACY_PAGE_SHA256",
}


def _git(repo, *args):
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release-test@example.com",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_private_project_config(repo, appid="wx12345678"):
    private_config = repo / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.write_text(
        json.dumps({"appid": appid}) + "\n",
        encoding="utf-8",
    )
    private_config.chmod(0o600)
    return private_config


def _prepare_release_repo(tmp_path):
    if not (tmp_path / ".git").exists():
        for index, (key, relative_path) in enumerate(
            _RELEASE_ARTIFACTS.items(),
            start=1,
        ):
            artifact = tmp_path / relative_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "<!-- WECHAT_RELEASE_STATUS: final -->",
                "<!-- WECHAT_MINIPROGRAM_NAME: 宜老天气通 -->",
                "小程序名称：宜老天气通",
                f"release artifact {index}",
            ]
            if key in _EFFECTIVE_DATE_ARTIFACTS:
                lines.append("<!-- WECHAT_EFFECTIVE_DATE: 2026-07-18 -->")
                lines.append("生效日期：2026-07-18")
            if key in _PRIVACY_VERSION_ARTIFACTS:
                lines.append("<!-- WECHAT_PRIVACY_VERSION: 2026-07-18 -->")
                lines.append("隐私版本：2026-07-18")
            artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (tmp_path / "release-marker.txt").write_text("frozen\n", encoding="utf-8")
        (tmp_path / "project.config.json").write_text(
            '{"appid":"touristappid"}\n',
            encoding="utf-8",
        )
        (tmp_path / "miniprogram" / "config.js").write_text(
            "const defaults = {\n"
            "  PRIVACY_CONSENT_VERSION: '2026-07-18',\n"
            "};\n",
            encoding="utf-8",
        )
        (tmp_path / "miniprogram" / "config.runtime.js").write_text(
            "module.exports = {\n"
            "  API_BASE_URL: 'https://yilaoweather.org',\n"
            "};\n",
            encoding="utf-8",
        )
        gis_artifact = tmp_path / release_validator.GIS_FROZEN_ARTIFACT_PATH
        gis_artifact.parent.mkdir(parents=True, exist_ok=True)
        gis_artifact.write_text(
            '{"type":"FeatureCollection","features":[]}' + (" " * 4096),
            encoding="utf-8",
        )
        (tmp_path / ".gitignore").write_text(
            ".env.*\n/project.private.config.json\n",
            encoding="utf-8",
        )
        _git(tmp_path, "init", "--quiet")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "--quiet", "-m", "release fixture")
        _write_private_project_config(tmp_path)

    digests = {
        key: hashlib.sha256((tmp_path / relative_path).read_bytes()).hexdigest()
        for key, relative_path in _RELEASE_ARTIFACTS.items()
    }
    return _git(tmp_path, "rev-parse", "HEAD"), digests


def _write_env(tmp_path, extra=""):
    path = tmp_path / ".env"
    path.write_text(
        "PUBLIC_BASE_URL=https://yilaoweather.org\n"
        "ALLOW_INSECURE_PUBLIC_BASE_URL=\n"
        "WXPUSHER_API_BASE=https://wxpusher.zjiecode.com/api\n"
        "FEATURE_WXPUSHER=0\n"
        "WXPUSHER_APP_TOKEN=\n"
        f"DISPATCH_LOCK_PATH={tmp_path / 'case-weather-dispatch.lock'}\n"
        "ALLOW_WEATHER_UNAVAILABLE=1\n"
        "QWEATHER_AUTH_MODE=disabled\n"
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "QWEATHER_CANONICAL_LOCATION=116.20,29.27\n"
        "QWEATHER_MONTHLY_REQUEST_LIMIT=40000\n"
        "QWEATHER_BUDGET_FAIL_CLOSED=1\n"
        "QWEATHER_REQUIRE_PERSISTENT_BUDGET=1\n"
        "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED=1\n"
        f"QWEATHER_CONSOLE_USAGE_MONTH={release_validator._expected_qweather_usage_month()}\n"
        "QWEATHER_CONSOLE_USAGE_BASELINE=123\n"
        "WEATHER_CACHE_TTL_MINUTES=30\n"
        "FORECAST_CACHE_TTL_MINUTES=30\n"
        "QWEATHER_WARNING_CACHE_TTL_MINUTES=30\n"
        "WEATHER_SYNC_LOCATIONS=都昌县\n"
        + extra,
        encoding="utf-8",
    )
    return path


def _write_wechat_release_form(tmp_path, **overrides):
    head, digests = _prepare_release_repo(tmp_path)
    evidence_root = tmp_path.parent / f"{tmp_path.name}-wechat-evidence"
    evidence_root.mkdir(mode=0o700, exist_ok=True)
    evidence_root.chmod(0o700)
    evidence_file = evidence_root / "category.png"
    evidence_file.write_bytes(b"\x89PNG\r\n\x1a\nrelease-evidence")
    evidence_file.chmod(0o600)
    values = {
        "WECHAT_SUBJECT_TYPE": "personal",
        "WECHAT_MINIPROGRAM_NAME": "宜老天气通",
        "WECHAT_OPERATOR_NAME": "测试运营者",
        "WECHAT_CONTACT_EMAIL": "operator@example.com",
        "WECHAT_EFFECTIVE_DATE": "2026-07-18",
        "WECHAT_REQUEST_DOMAIN": "https://yilaoweather.org",
        "WECHAT_CATEGORY_CONFIRMED": "1",
        "WECHAT_CATEGORY_PATHS_JSON": '["生活服务/天气查询"]',
        "WECHAT_CATEGORY_QUALIFICATION_STATUS": "no_extra_institutional_qualification",
        "WECHAT_CATEGORY_EVIDENCE_ROOT": str(evidence_root.resolve()),
        "WECHAT_CATEGORY_EVIDENCE_REF": "category.png",
        "WECHAT_CATEGORY_EVIDENCE_SHA256": hashlib.sha256(
            evidence_file.read_bytes()
        ).hexdigest(),
        "WECHAT_CATEGORY_CONFIRMED_AT": datetime.now(timezone.utc).isoformat(),
        "WECHAT_RELEASE_VERSION": "1.0.0",
        "WECHAT_TARGET_COMMIT_SHA": head,
        **digests,
        "WX_MINIPROGRAM_APPID": "wx12345678",
        "WX_MINIPROGRAM_SECRET": "1234567890abcdef",
        "WX_MINIPROGRAM_PRIVACY_VERSION": "2026-07-18",
        "FEATURE_WXPUSHER": "0",
        "WXPUSHER_APP_TOKEN": "",
        "FEATURE_HEAT_EXPOSURE_GIS": "1",
        "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED": "1",
        "QWEATHER_CONSOLE_USAGE_MONTH": release_validator._expected_qweather_usage_month(),
        "QWEATHER_CONSOLE_USAGE_BASELINE": "123",
        "WECHAT_FORM_READY": "1",
    }
    values.update(overrides)
    form = tmp_path / ".env.wechat-release"
    form.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    form.chmod(0o600)
    return form


def test_validator_allows_explicit_pending_wechat_for_preview(tmp_path):
    result = validate_release_env(_write_env(tmp_path), require_wechat=False)

    assert result["ok"] is True
    assert result["wechat_ready"] is False
    assert result["warnings"]


def test_validator_supports_wxpusher_only_when_feature_and_token_match(tmp_path):
    enabled = validate_release_env(
        _write_env(
            tmp_path,
            "FEATURE_WXPUSHER=1\n"
            "WXPUSHER_APP_TOKEN=AT_release-test-token\n",
        ),
        require_wechat=False,
    )
    assert enabled["ok"] is True
    assert enabled["wxpusher_ready"] is True

    disabled_with_token = validate_release_env(
        _write_env(tmp_path, "WXPUSHER_APP_TOKEN=AT_release-test-token\n"),
        require_wechat=False,
    )
    assert disabled_with_token["ok"] is False
    assert any("必须清空" in error for error in disabled_with_token["errors"])

    enabled_without_token = validate_release_env(
        _write_env(tmp_path, "FEATURE_WXPUSHER=1\n"),
        require_wechat=False,
    )
    assert enabled_without_token["ok"] is False
    assert any("必须配置" in error for error in enabled_without_token["errors"])


def test_release_validator_cli_parser_smoke_has_no_option_conflicts(tmp_path, capsys):
    exit_code = release_validator.main(["--file", str(_write_env(tmp_path))])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"ok": true' in output


def test_release_env_validator_fails_closed_for_unsafe_file_inputs(tmp_path):
    directory = tmp_path / "release-env-directory"
    directory.mkdir()
    non_utf8 = tmp_path / "release-env-non-utf8"
    non_utf8.write_bytes(b"PUBLIC_BASE_URL=\xff\n")
    oversized = tmp_path / "release-env-oversized"
    oversized.write_bytes(b"X" * ((64 * 1024) + 1))

    for path in (directory, non_utf8, oversized):
        result = validate_release_env(path)
        assert result["ok"] is False
        assert set(result) == {
            "ok",
            "wechat_ready",
            "weather_ready",
            "qweather_mode",
            "wxpusher_ready",
            "warnings",
            "errors",
        }
        assert result["errors"]
        assert str(path) not in "\n".join(result["errors"])


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
        "FEATURE_HEAT_EXPOSURE_GIS=1\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n",
    )
    ready_result = validate_release_env(ready, require_wechat=True)
    assert ready_result["ok"] is True
    assert ready_result["wechat_ready"] is True


@pytest.mark.parametrize(
    "key,wrong_value",
    (
        ("QWEATHER_CANONICAL_LOCATION", "116.21,29.27"),
        ("QWEATHER_MONTHLY_REQUEST_LIMIT", "40001"),
        ("QWEATHER_BUDGET_FAIL_CLOSED", "0"),
        ("QWEATHER_REQUIRE_PERSISTENT_BUDGET", "0"),
        ("WEATHER_CACHE_TTL_MINUTES", "29"),
        ("FORECAST_CACHE_TTL_MINUTES", "31"),
        ("QWEATHER_WARNING_CACHE_TTL_MINUTES", "60"),
        ("WEATHER_SYNC_LOCATIONS", "九江市"),
    ),
)
def test_formal_validator_requires_exact_duchang_budget_values(
    tmp_path,
    key,
    wrong_value,
):
    formal_values = (
        "WX_MINIPROGRAM_APPID=wx123456\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n"
        "FEATURE_HEAT_EXPOSURE_GIS=1\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
        f"{key}={wrong_value}\n"
    )

    result = validate_release_env(
        _write_env(tmp_path, formal_values),
        require_wechat=True,
    )

    assert result["ok"] is False
    assert any(key in error for error in result["errors"])


def test_qweather_month_reserve_covers_30_minute_cycles_and_month_end():
    month_start = datetime.fromisoformat("2026-07-01T00:00:00+08:00")
    last_minute = datetime.fromisoformat("2026-07-31T23:59:00+08:00")

    assert _qweather_required_month_reserve(month_start) == (31 * 48 * 3) + 3
    assert _qweather_required_month_reserve(last_minute) == 6

    month_start_values = {
        "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED": "1",
        "QWEATHER_CONSOLE_USAGE_MONTH": "2026-07",
        "QWEATHER_CONSOLE_USAGE_BASELINE": str(40000 - ((31 * 48 * 3) + 3)),
    }
    assert _validate_qweather_console_baseline(
        month_start_values,
        validation_time=month_start,
    ) == []
    month_start_values["QWEATHER_CONSOLE_USAGE_BASELINE"] = str(
        int(month_start_values["QWEATHER_CONSOLE_USAGE_BASELINE"]) + 1
    )
    assert any(
        "每 30 分钟" in error
        for error in _validate_qweather_console_baseline(
            month_start_values,
            validation_time=month_start,
        )
    )

    month_end_values = {
        "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED": "1",
        "QWEATHER_CONSOLE_USAGE_MONTH": "2026-07",
        "QWEATHER_CONSOLE_USAGE_BASELINE": "39994",
    }
    assert _validate_qweather_console_baseline(
        month_end_values,
        validation_time=last_minute,
    ) == []


@pytest.mark.parametrize(
    'unsafe_ai_config,expected_error',
    (
        ('FEATURE_WEB_AI=1\n', 'FEATURE_WEB_AI'),
        ('SILICONFLOW_API_KEY=server-ai-secret\n', 'SILICONFLOW_API_KEY'),
        ('SILICONFLOW_API_BASE=https://evil.example/v1\n', 'SILICONFLOW_API_BASE'),
    ),
)
def test_formal_validator_requires_web_ai_to_stay_closed(
    tmp_path,
    unsafe_ai_config,
    expected_error,
):
    base = (
        "WX_MINIPROGRAM_APPID=wx123456\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n"
        "FEATURE_HEAT_EXPOSURE_GIS=1\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
    )
    result = validate_release_env(
        _write_env(tmp_path, base + unsafe_ai_config),
        require_wechat=True,
    )

    assert result['ok'] is False
    assert any(expected_error in error for error in result['errors'])


def test_formal_server_validator_requires_gis_and_wxpusher_disabled(tmp_path):
    base = (
        "WX_MINIPROGRAM_APPID=wx123456\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
    )
    first_release = validate_release_env(
        _write_env(tmp_path, base + "FEATURE_HEAT_EXPOSURE_GIS=1\n"),
        require_wechat=True,
    )
    assert first_release["ok"] is True
    assert first_release["wxpusher_ready"] is False

    disabled_gis = validate_release_env(
        _write_env(
            tmp_path,
            base
            + "FEATURE_HEAT_EXPOSURE_GIS=0\n",
        ),
        require_wechat=True,
    )
    assert disabled_gis["ok"] is False
    assert any(
        "FEATURE_HEAT_EXPOSURE_GIS" in error
        for error in disabled_gis["errors"]
    )

    enabled_push = validate_release_env(
        _write_env(
            tmp_path,
            base
            + "FEATURE_WXPUSHER=1\n"
            + "WXPUSHER_APP_TOKEN=AT_release-test-token\n"
            + "FEATURE_HEAT_EXPOSURE_GIS=1\n",
        ),
        require_wechat=True,
    )
    assert enabled_push["ok"] is False
    assert any("FEATURE_WXPUSHER=0" in error for error in enabled_push["errors"])


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
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
        "REDIS_URL=redis://127.0.0.1:6379/0\n",
        encoding="utf-8",
    )
    ready_result = validate_release_env(ready)

    assert ready_result["ok"] is True
    assert ready_result["weather_ready"] is True


def test_validator_requires_persistent_budget_config_for_enabled_qweather(tmp_path):
    missing_redis = tmp_path / "missing-redis.env"
    missing_redis.write_text(
        "PUBLIC_BASE_URL=https://api.example.com\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n",
        encoding="utf-8",
    )
    missing_result = validate_release_env(missing_redis)

    assert missing_result["ok"] is False
    assert missing_result["weather_ready"] is False
    assert any("REDIS_URL" in error for error in missing_result["errors"])

    formal_without_flag = _write_env(
        tmp_path,
        "WX_MINIPROGRAM_APPID=wx123456\n"
        "WX_MINIPROGRAM_SECRET=1234567890abcdef\n"
        f"WX_MINIPROGRAM_OPENID_PEPPER={'p' * 32}\n"
        f"WX_MINIPROGRAM_SESSION_SECRET={'s' * 32}\n"
        "FEATURE_HEAT_EXPOSURE_GIS=1\n"
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
        "QWEATHER_REQUIRE_PERSISTENT_BUDGET=0\n",
    )
    formal_result = validate_release_env(formal_without_flag, require_wechat=True)

    assert formal_result["ok"] is False
    assert any("持久化预算" in error for error in formal_result["errors"])


def test_persistent_budget_probe_uses_short_timeout_and_hides_credentials(tmp_path):
    path = tmp_path / "probe.env"
    secret_url = "redis://:redis-secret@127.0.0.1:6379/0"
    path.write_text(
        "QWEATHER_AUTH_MODE=api_key\n"
        f"REDIS_URL={secret_url}\n",
        encoding="utf-8",
    )
    observed = {}

    class FakeClient:
        def ping(self):
            return True

        def info(self, *, section):
            assert section == "persistence"
            return {
                "aof_enabled": 1,
                "loading": 0,
                "aof_last_write_status": "ok",
                "aof_last_bgrewrite_status": "ok",
            }

        def config_get(self, key):
            assert key == "appendfsync"
            return {"appendfsync": "everysec"}

        def close(self):
            observed["closed"] = True

    class FakeRedis:
        @staticmethod
        def from_url(url, **kwargs):
            observed["url"] = url
            observed["kwargs"] = kwargs
            return FakeClient()

    assert probe_persistent_budget_backend(
        path,
        redis_module=SimpleNamespace(Redis=FakeRedis),
    ) == []
    assert observed["url"] == secret_url
    assert observed["kwargs"]["socket_connect_timeout"] == 2
    assert observed["kwargs"]["socket_timeout"] == 2
    assert observed["closed"] is True

    class FailingRedis:
        @staticmethod
        def from_url(_url, **_kwargs):
            raise RuntimeError("redis-secret")

    errors = probe_persistent_budget_backend(
        path,
        redis_module=SimpleNamespace(Redis=FailingRedis),
    )
    assert errors
    assert "redis-secret" not in " ".join(errors)
    assert secret_url not in " ".join(errors)


@pytest.mark.parametrize(
    "persistence,appendfsync",
    (
        (
            {
                "aof_enabled": 0,
                "loading": 0,
                "aof_last_write_status": "ok",
                "aof_last_bgrewrite_status": "ok",
            },
            "everysec",
        ),
        (
            {
                "aof_enabled": 1,
                "loading": 1,
                "aof_last_write_status": "ok",
                "aof_last_bgrewrite_status": "ok",
            },
            "everysec",
        ),
        (
            {
                "aof_enabled": 1,
                "loading": 0,
                "aof_last_write_status": "err",
                "aof_last_bgrewrite_status": "ok",
            },
            "everysec",
        ),
        (
            {
                "aof_enabled": 1,
                "loading": 0,
                "aof_last_write_status": "ok",
                "aof_last_bgrewrite_status": "err",
            },
            "everysec",
        ),
        (
            {
                "aof_enabled": 1,
                "loading": 0,
                "aof_last_write_status": "ok",
                "aof_last_bgrewrite_status": "ok",
            },
            "no",
        ),
    ),
)
def test_persistent_budget_probe_fails_closed_for_unsafe_aof_state(
    tmp_path,
    persistence,
    appendfsync,
):
    path = tmp_path / "probe.env"
    path.write_text(
        "QWEATHER_AUTH_MODE=api_key\nREDIS_URL=redis://127.0.0.1:6379/0\n",
        encoding="utf-8",
    )

    class FakeClient:
        def ping(self):
            return True

        def info(self, *, section):
            assert section == "persistence"
            return persistence

        def config_get(self, key):
            assert key == "appendfsync"
            return {"appendfsync": appendfsync}

        def close(self):
            return None

    class FakeRedis:
        @staticmethod
        def from_url(_url, **_kwargs):
            return FakeClient()

    errors = probe_persistent_budget_backend(
        path,
        redis_module=SimpleNamespace(Redis=FakeRedis),
    )

    assert errors == ["QWeather 持久化预算 Redis 连通性或 AOF 配置验证失败。"]


def test_persistent_budget_probe_fails_closed_when_config_get_is_forbidden(tmp_path):
    path = tmp_path / "probe.env"
    path.write_text(
        "QWEATHER_AUTH_MODE=api_key\nREDIS_URL=redis://127.0.0.1:6379/0\n",
        encoding="utf-8",
    )

    class FakeClient:
        def ping(self):
            return True

        def info(self, *, section):
            assert section == "persistence"
            return {
                "aof_enabled": 1,
                "loading": 0,
                "aof_last_write_status": "ok",
                "aof_last_bgrewrite_status": "ok",
            }

        def config_get(self, _key):
            raise PermissionError("NOPERM secret-canary")

        def close(self):
            return None

    class FakeRedis:
        @staticmethod
        def from_url(_url, **_kwargs):
            return FakeClient()

    errors = probe_persistent_budget_backend(
        path,
        redis_module=SimpleNamespace(Redis=FakeRedis),
    )

    assert errors
    assert "secret-canary" not in " ".join(errors)


def test_persistent_budget_baseline_uses_atomic_max_and_records_endpoint(tmp_path):
    path = _write_env(
        tmp_path,
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
        "QWEATHER_CONSOLE_USAGE_MONTH=2026-07\n"
        "QWEATHER_CONSOLE_USAGE_BASELINE=123\n",
    )
    observed = {"total": 200, "endpoint": 50}

    class FakeClient:
        def eval(self, script, key_count, total_key, endpoint_key, baseline, ttl):
            assert key_count == 2
            assert "total < baseline" in script
            assert "recorded < baseline" in script
            observed["total_key"] = total_key
            observed["endpoint_key"] = endpoint_key
            observed["ttl"] = int(ttl)
            observed["total"] = max(observed["total"], int(baseline))
            observed["endpoint"] = max(observed["endpoint"], int(baseline))
            return observed["total"]

        def close(self):
            observed["closed"] = True

    class FakeRedis:
        @staticmethod
        def from_url(_url, **_kwargs):
            return FakeClient()

    result = seed_persistent_budget_baseline(
        path,
        redis_module=SimpleNamespace(Redis=FakeRedis),
        validation_time=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    assert result == []
    assert observed["total"] == 200
    assert observed["endpoint"] == 123
    assert observed["total_key"].endswith(":2026-07:total")
    assert observed["endpoint_key"].endswith(":2026-07:endpoints")
    assert observed["ttl"] >= 86400
    assert observed["closed"] is True


def test_persistent_budget_baseline_failure_hides_redis_details(tmp_path):
    path = _write_env(
        tmp_path,
        "QWEATHER_AUTH_MODE=api_key\n"
        "QWEATHER_KEY=server-secret\n"
        "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n",
    )

    class FailingRedis:
        @staticmethod
        def from_url(_url, **_kwargs):
            raise RuntimeError("redis-baseline-secret")

    errors = seed_persistent_budget_baseline(
        path,
        redis_module=SimpleNamespace(Redis=FailingRedis),
    )

    assert errors
    assert "redis-baseline-secret" not in " ".join(errors)


def test_disabled_qweather_probe_does_not_require_redis(tmp_path):
    path = tmp_path / "disabled-probe.env"
    path.write_text("QWEATHER_AUTH_MODE=disabled\n", encoding="utf-8")

    assert probe_persistent_budget_backend(path, redis_module=object()) == []


def test_validator_rejects_untrusted_qweather_api_key_host_and_port(tmp_path):
    def api_key_env(name, api_base):
        path = tmp_path / name
        path.write_text(
            "PUBLIC_BASE_URL=https://api.example.com\n"
            "QWEATHER_AUTH_MODE=api_key\n"
            "QWEATHER_KEY=server-secret\n"
            f"QWEATHER_API_BASE={api_base}\n"
            "REDIS_URL=redis://127.0.0.1:6379/0\n",
            encoding="utf-8",
        )
        return path

    invalid_host = validate_release_env(
        api_key_env("invalid-host.env", "https://weather.example.com/v7")
    )
    assert invalid_host["ok"] is False
    assert invalid_host["weather_ready"] is False
    assert any("API Host" in error for error in invalid_host["errors"])

    invalid_port = validate_release_env(
        api_key_env("invalid-port.env", "https://unit-test.qweatherapi.com:8443/v7")
    )
    assert invalid_port["ok"] is False
    assert invalid_port["weather_ready"] is False
    assert any("端口 443" in error for error in invalid_port["errors"])

    explicit_https_port = validate_release_env(
        api_key_env("explicit-https-port.env", "https://unit-test.qweatherapi.com:443/v7")
    )
    assert explicit_https_port["ok"] is True
    assert explicit_https_port["weather_ready"] is True


def test_validator_rejects_noncanonical_qweather_api_bases_without_crashing(tmp_path):
    invalid_bases = (
        "https://[unit-test.qweatherapi.com/v7",
        "https://@unit-test.qweatherapi.com/v7",
        "https://:@unit-test.qweatherapi.com/v7",
        "https://.qweatherapi.com/v7",
        "https://qweatherapi.com/v7",
        "https://evilqweatherapi.com/v7",
        "https://unit-test.qweatherapi.com.evil.example/v7",
        "https://unit-test.qweatherapi.com:/v7",
        "http://unit-test.qweatherapi.com/v7",
        "https://unit-test.qweatherapi.com/v8",
        "https://unit-test.qweatherapi.com/v7;param",
        "https://unit-test.qweatherapi.com/v7?query=1",
        "https://unit-test.qweatherapi.com/v7#fragment",
    )

    for index, api_base in enumerate(invalid_bases):
        path = tmp_path / f"invalid-{index}.env"
        path.write_text(
            "PUBLIC_BASE_URL=https://api.example.com\n"
            "QWEATHER_AUTH_MODE=api_key\n"
            "QWEATHER_KEY=server-secret\n"
            f"QWEATHER_API_BASE={api_base}\n",
            encoding="utf-8",
        )
        result = validate_release_env(path)
        assert result["ok"] is False, api_base
        assert result["weather_ready"] is False, api_base
        assert result["errors"], api_base


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
            f"QWEATHER_JWT_PRIVATE_KEY_PATH={key_path}\n"
            "WEATHER_CACHE_REDIS_URL=rediss://cache.example:6380/1\n",
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
    assert any("API Host" in error for error in invalid_host["errors"])

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

    private_key.chmod(0o400)
    read_only_permissions = validate_release_env(
        jwt_env("read-only-key.env", "https://unit-test.qweatherapi.com/v7")
    )
    assert read_only_permissions["ok"] is False
    assert any("0600" in error for error in read_only_permissions["errors"])


def test_qweather_jwt_private_key_rejects_symlink_and_read_time_change(
    tmp_path,
    monkeypatch,
):
    private_key = tmp_path / "qweather-private.pem"
    private_key.write_bytes(b"private-key-material")
    private_key.chmod(0o600)
    linked_key = tmp_path / "linked-private.pem"
    linked_key.symlink_to(private_key)

    def validate(key_path):
        path = tmp_path / "jwt.env"
        path.write_text(
            "PUBLIC_BASE_URL=https://api.example.com\n"
            "QWEATHER_AUTH_MODE=jwt\n"
            "QWEATHER_API_BASE=https://unit-test.qweatherapi.com/v7\n"
            "QWEATHER_JWT_KID=test-kid\n"
            "QWEATHER_JWT_PROJECT_ID=test-project\n"
            f"QWEATHER_JWT_PRIVATE_KEY_PATH={key_path}\n"
            "REDIS_URL=redis://127.0.0.1:6379/0\n",
            encoding="utf-8",
        )
        return validate_release_env(path)

    linked_result = validate(linked_key)
    assert linked_result["ok"] is False
    assert any("符号链接" in error for error in linked_result["errors"])

    real_read = os.read
    changed = False
    private_key_inode = private_key.stat().st_ino

    def change_key_after_read(file_descriptor, size):
        nonlocal changed
        chunk = real_read(file_descriptor, size)
        if (
            chunk
            and not changed
            and os.fstat(file_descriptor).st_ino == private_key_inode
        ):
            changed = True
            with private_key.open("ab") as output:
                output.write(b"-changed")
        return chunk

    monkeypatch.setattr(release_validator.os, "read", change_key_after_read)
    changed_result = validate(private_key)
    assert changed_result["ok"] is False
    assert any("读取期间发生变化" in error for error in changed_result["errors"])


def test_frozen_gis_compression_requires_gzip_and_brotli_under_300kib(monkeypatch):
    assert _validate_gis_compressed_content(b" " * 4096) == []

    monkeypatch.setattr(release_validator, "GIS_COMPRESSED_MAX_BYTES", 32)
    errors = _validate_gis_compressed_content(os.urandom(4096))

    assert any("gzip" in error for error in errors)
    assert any("Brotli" in error for error in errors)


def test_wechat_release_form_requires_private_complete_personal_form(tmp_path):
    missing = validate_wechat_release_form(
        tmp_path / ".env.wechat-release",
        require_ready=True,
    )
    assert missing["ok"] is False

    form = _write_wechat_release_form(tmp_path)
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


@pytest.mark.parametrize(
    "overrides,expected_error",
    (
        (
            {"QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED": "0"},
            "仅供本项目",
        ),
        (
            {"QWEATHER_CONSOLE_USAGE_MONTH": "2026-06"},
            "当前北京时间月份",
        ),
        (
            {"QWEATHER_CONSOLE_USAGE_BASELINE": "39998"},
            "最多 3 个正式烟测端点",
        ),
        (
            {"QWEATHER_CONSOLE_USAGE_BASELINE": "01"},
            "必须是 0 至",
        ),
    ),
)
def test_wechat_release_form_requires_current_qweather_console_baseline(
    tmp_path,
    overrides,
    expected_error,
):
    form = _write_wechat_release_form(
        tmp_path,
        **{"QWEATHER_CONSOLE_USAGE_MONTH": "2026-07", **overrides},
    )

    result = validate_wechat_release_form(
        form,
        require_ready=True,
        validation_time=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    assert result["ok"] is False
    assert any(expected_error in error for error in result["errors"])


def test_direct_form_validation_fails_closed_for_unsafe_inputs(tmp_path):
    valid_form = _write_wechat_release_form(tmp_path / "repo")
    cases = []

    directory = tmp_path / "form-directory"
    directory.mkdir(mode=0o700)
    cases.append(directory)

    symlink = tmp_path / "form-symlink"
    symlink.symlink_to(valid_form)
    cases.append(symlink)

    non_utf8 = tmp_path / "form-non-utf8"
    non_utf8.write_bytes(b"WECHAT_FORM_READY=\xff\n")
    non_utf8.chmod(0o600)
    cases.append(non_utf8)

    oversized = tmp_path / "form-oversized"
    oversized.write_bytes(b"A" * ((64 * 1024) + 1))
    oversized.chmod(0o600)
    cases.append(oversized)

    for path in cases:
        result = validate_wechat_release_form(path, require_ready=True)
        assert result["ok"] is False
        assert set(result) == {
            "ok",
            "form_ready",
            "category_confirmed",
            "warnings",
            "errors",
        }
        assert result["errors"]
        assert str(path) not in "\n".join(result["errors"])


def test_direct_form_validation_converts_io_failure_to_fixed_error(tmp_path, monkeypatch):
    form = _write_wechat_release_form(tmp_path)

    def fail_read(_file_descriptor, _size):
        raise OSError("sensitive-path-should-not-escape")

    monkeypatch.setattr(release_validator.os, "read", fail_read)
    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert result["errors"] == ["微信发布私密表单无法安全读取。"]
    assert "sensitive-path-should-not-escape" not in "\n".join(result["errors"])


def test_snapshot_rejects_source_change_and_growth_during_read(tmp_path, monkeypatch):
    change_repo = tmp_path / "change-repo"
    change_repo.mkdir()
    changing_form = _write_wechat_release_form(change_repo)
    real_read = os.read
    changed = False

    def mutate_after_first_read(file_descriptor, size):
        nonlocal changed
        chunk = real_read(file_descriptor, size)
        if chunk and not changed:
            changed = True
            with changing_form.open("ab") as output:
                output.write(b"# changed during read\n")
        return chunk

    monkeypatch.setattr(release_validator.os, "read", mutate_after_first_read)
    changed_snapshot = tmp_path / "changed.snapshot"
    changed_errors = snapshot_wechat_release_form(changing_form, changed_snapshot)

    assert any("读取期间发生变化" in error for error in changed_errors)
    assert not changed_snapshot.exists()

    growth_repo = tmp_path / "growth-repo"
    growth_repo.mkdir()
    growing_form = _write_wechat_release_form(growth_repo)
    grown = False

    def grow_after_first_read(file_descriptor, size):
        nonlocal grown
        chunk = real_read(file_descriptor, size)
        if chunk and not grown:
            grown = True
            with growing_form.open("ab") as output:
                output.write(b"X" * ((64 * 1024) + 1))
        return chunk

    monkeypatch.setattr(release_validator.os, "read", grow_after_first_read)
    grown_snapshot = tmp_path / "grown.snapshot"
    grown_errors = snapshot_wechat_release_form(growing_form, grown_snapshot)

    assert any("大小异常" in error for error in grown_errors)
    assert not grown_snapshot.exists()


def test_snapshot_preserves_all_bytes_across_short_writes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    form = _write_wechat_release_form(repo)
    expected = form.read_bytes()
    snapshot = tmp_path / "wechat-release.snapshot"
    real_write = os.write
    write_sizes = []

    def short_write(file_descriptor, data):
        # 模拟操作系统只写入当前缓冲区的一小部分。
        size = min(7, len(data))
        written = real_write(file_descriptor, data[:size])
        write_sizes.append(written)
        return written

    monkeypatch.setattr(release_validator.os, "write", short_write)

    assert snapshot_wechat_release_form(form, snapshot) == []
    assert len(write_sizes) > 1
    assert snapshot.read_bytes() == expected
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_wechat_release_form_allows_empty_category_evidence_for_preview(tmp_path):
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_CATEGORY_CONFIRMED="0",
        WECHAT_CATEGORY_PATHS_JSON="",
        WECHAT_CATEGORY_QUALIFICATION_STATUS="",
        WECHAT_CATEGORY_EVIDENCE_ROOT="",
        WECHAT_CATEGORY_EVIDENCE_REF="",
        WECHAT_CATEGORY_EVIDENCE_SHA256="",
        WECHAT_CATEGORY_CONFIRMED_AT="",
        WECHAT_RELEASE_VERSION="",
        WECHAT_TARGET_COMMIT_SHA="",
        WECHAT_PRIVACY_DOC_SHA256="",
        WECHAT_AGREEMENT_DOC_SHA256="",
        WECHAT_LISTING_COPY_SHA256="",
        WECHAT_PRIVACY_PAGE_SHA256="",
        WECHAT_AGREEMENT_PAGE_SHA256="",
        WECHAT_FORM_READY="0",
    )

    result = validate_wechat_release_form(form, require_ready=False)

    assert result["ok"] is True
    assert result["form_ready"] is False
    assert result["category_confirmed"] is False
    assert result["warnings"]


def test_wechat_release_form_requires_structured_category_evidence_for_formal_release(
    tmp_path,
):
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_CATEGORY_PATHS_JSON="",
        WECHAT_CATEGORY_QUALIFICATION_STATUS="",
        WECHAT_CATEGORY_EVIDENCE_ROOT="",
        WECHAT_CATEGORY_EVIDENCE_REF="",
        WECHAT_CATEGORY_EVIDENCE_SHA256="",
        WECHAT_CATEGORY_CONFIRMED_AT="",
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    for key in (
        "WECHAT_CATEGORY_PATHS_JSON",
        "WECHAT_CATEGORY_QUALIFICATION_STATUS",
        "WECHAT_CATEGORY_EVIDENCE_ROOT",
        "WECHAT_CATEGORY_EVIDENCE_REF",
        "WECHAT_CATEGORY_EVIDENCE_SHA256",
        "WECHAT_CATEGORY_CONFIRMED_AT",
    ):
        assert any(key in error for error in result["errors"])


def test_wechat_release_form_requires_timezone_on_category_confirmation(tmp_path):
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_CATEGORY_CONFIRMED_AT="2026-07-18T15:30:00",
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("时区" in error for error in result["errors"])


def test_wechat_release_form_rejects_category_requiring_institutional_qualification(
    tmp_path,
):
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_CATEGORY_QUALIFICATION_STATUS="institutional_qualification_required",
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("机构资质" in error for error in result["errors"])


def test_wechat_release_form_rejects_category_evidence_injection_and_oversize(
    tmp_path,
):
    cases = (
        (
            {"WECHAT_CATEGORY_PATHS_JSON": '["生活服务/天气\\n查询"]'},
            "类目路径",
        ),
        (
            {"WECHAT_CATEGORY_EVIDENCE_REF": "../category.png"},
            "证据引用",
        ),
        (
            {"WECHAT_CATEGORY_PATHS_JSON": '["生活服务/' + ("天" * 201) + '"]'},
            "长度",
        ),
    )

    for index, (overrides, expected_error) in enumerate(cases):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        form = _write_wechat_release_form(case_dir, **overrides)
        result = validate_wechat_release_form(form, require_ready=True)
        assert result["ok"] is False
        assert any(expected_error in error for error in result["errors"])


def test_wechat_release_form_verifies_private_category_evidence_file(tmp_path):
    cases = (
        "root-inside-repo",
        "root-permission",
        "file-permission",
        "empty",
        "oversized",
        "digest",
    )
    for name in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        form = _write_wechat_release_form(case_dir)
        evidence_root = case_dir.parent / f"{case_dir.name}-wechat-evidence"
        evidence_file = evidence_root / "category.png"

        if name == "root-inside-repo":
            inside_file = case_dir / "release-marker.txt"
            digest = hashlib.sha256(inside_file.read_bytes()).hexdigest()
            form = _write_wechat_release_form(
                case_dir,
                WECHAT_CATEGORY_EVIDENCE_ROOT=str(case_dir.resolve()),
                WECHAT_CATEGORY_EVIDENCE_REF="release-marker.txt",
                WECHAT_CATEGORY_EVIDENCE_SHA256=digest,
            )
        elif name == "root-permission":
            evidence_root.chmod(0o755)
        elif name == "file-permission":
            evidence_file.chmod(0o644)
        elif name == "empty":
            evidence_file.write_bytes(b"")
        elif name == "oversized":
            with evidence_file.open("r+b") as output:
                output.truncate((20 * 1024 * 1024) + 1)
        elif name == "digest":
            form = _write_wechat_release_form(
                case_dir,
                WECHAT_CATEGORY_EVIDENCE_SHA256="0" * 64,
            )

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any("WECHAT_CATEGORY_EVIDENCE" in error for error in result["errors"])
        assert str(evidence_root) not in "\n".join(result["errors"])


def test_wechat_release_form_rejects_symlinked_category_evidence(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    evidence_root = tmp_path.parent / f"{tmp_path.name}-wechat-evidence"
    outside_file = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside_file.write_bytes(b"outside evidence")
    outside_file.chmod(0o600)
    symlink = evidence_root / "linked.png"
    symlink.symlink_to(outside_file)
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_CATEGORY_EVIDENCE_REF="linked.png",
        WECHAT_CATEGORY_EVIDENCE_SHA256=hashlib.sha256(
            outside_file.read_bytes()
        ).hexdigest(),
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("WECHAT_CATEGORY_EVIDENCE" in error for error in result["errors"])
    assert str(outside_file) not in "\n".join(result["errors"])


def test_wechat_release_form_requires_release_freeze_fields_for_formal_release(
    tmp_path,
):
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_RELEASE_VERSION="",
        WECHAT_TARGET_COMMIT_SHA="",
        WECHAT_PRIVACY_DOC_SHA256="",
        WECHAT_AGREEMENT_DOC_SHA256="",
        WECHAT_LISTING_COPY_SHA256="",
        WECHAT_PRIVACY_PAGE_SHA256="",
        WECHAT_AGREEMENT_PAGE_SHA256="",
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    for key in (
        "WECHAT_RELEASE_VERSION",
        "WECHAT_TARGET_COMMIT_SHA",
        "WECHAT_PRIVACY_DOC_SHA256",
        "WECHAT_AGREEMENT_DOC_SHA256",
        "WECHAT_LISTING_COPY_SHA256",
        "WECHAT_PRIVACY_PAGE_SHA256",
        "WECHAT_AGREEMENT_PAGE_SHA256",
    ):
        assert any(key in error for error in result["errors"])


def test_wechat_release_form_rejects_uppercase_and_short_release_hashes(tmp_path):
    cases = (
        ({"WECHAT_TARGET_COMMIT_SHA": "A" * 40}, "WECHAT_TARGET_COMMIT_SHA"),
        ({"WECHAT_TARGET_COMMIT_SHA": "a" * 39}, "WECHAT_TARGET_COMMIT_SHA"),
        ({"WECHAT_PRIVACY_DOC_SHA256": "B" * 64}, "WECHAT_PRIVACY_DOC_SHA256"),
        ({"WECHAT_PRIVACY_DOC_SHA256": "b" * 63}, "WECHAT_PRIVACY_DOC_SHA256"),
    )

    for index, (overrides, expected_field) in enumerate(cases):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        form = _write_wechat_release_form(case_dir, **overrides)
        result = validate_wechat_release_form(form, require_ready=True)
        assert result["ok"] is False
        assert any(expected_field in error for error in result["errors"])
        error_summary = "\n".join(result["errors"])
        assert all(value not in error_summary for value in overrides.values())


def test_wechat_release_form_requires_current_strict_semver(tmp_path):
    for index, release_version in enumerate(("v1.0.0", "01.0.0", "1.0.1")):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        form = _write_wechat_release_form(
            case_dir,
            WECHAT_RELEASE_VERSION=release_version,
        )
        result = validate_wechat_release_form(form, require_ready=True)
        assert result["ok"] is False
        assert any("WECHAT_RELEASE_VERSION" in error for error in result["errors"])


def test_wechat_release_form_accepts_valid_complete_release_freeze(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH

    result = validate_wechat_release_form(form, require_ready=True)

    assert private_config.stat().st_mode & 0o777 == 0o600
    assert _git(tmp_path, "check-ignore", "--", private_config.name) == private_config.name
    assert result == {
        "ok": True,
        "form_ready": True,
        "category_confirmed": True,
        "warnings": [],
        "errors": [],
    }


def test_wechat_release_form_writes_verified_commit_ticket_once(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    verified_commit = tmp_path / "verified-commit"
    expected_head = _git(tmp_path, "rev-parse", "HEAD")

    result = validate_wechat_release_form(
        form,
        require_ready=True,
        verified_commit_output=verified_commit,
    )
    form.write_text("WECHAT_TARGET_COMMIT_SHA=" + ("f" * 40) + "\n", encoding="utf-8")

    assert result["ok"] is True
    assert verified_commit.read_text(encoding="ascii").strip() == expected_head
    assert verified_commit.stat().st_mode & 0o077 == 0


def test_wechat_release_form_pins_blob_reads_when_head_moves_during_validation(
    tmp_path,
    monkeypatch,
):
    form = _write_wechat_release_form(tmp_path)
    verified_head = _git(tmp_path, "rev-parse", "HEAD")
    verified_commit = tmp_path / "verified-commit"

    (tmp_path / "project.config.json").write_text(
        '{"appid":"wx87654321"}\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", "project.config.json")
    _git(tmp_path, "commit", "--quiet", "-m", "later unsafe project config")
    moved_head = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "--quiet", "--detach", verified_head)

    real_run_git = release_validator._run_git
    head_moved = False

    def move_head_before_first_blob(repo_root, *args):
        nonlocal head_moved
        if not head_moved and args[:2] == ("cat-file", "blob"):
            _git(tmp_path, "update-ref", "HEAD", moved_head)
            head_moved = True
        return real_run_git(repo_root, *args)

    monkeypatch.setattr(release_validator, "_run_git", move_head_before_first_blob)

    result = validate_wechat_release_form(
        form,
        require_ready=True,
        verified_commit_output=verified_commit,
    )

    assert head_moved is True
    assert _git(tmp_path, "rev-parse", "HEAD") == moved_head
    assert result["ok"] is True
    assert verified_commit.read_text(encoding="ascii").strip() == verified_head


def test_wechat_release_form_skips_blob_reads_when_head_cannot_be_resolved(
    tmp_path,
    monkeypatch,
):
    form = _write_wechat_release_form(tmp_path)
    real_run_git = release_validator._run_git
    blob_read_attempted = False

    def fail_head_resolution(repo_root, *args):
        nonlocal blob_read_attempted
        if args == ("rev-parse", "--verify", "HEAD^{commit}"):
            return None
        if args[:2] == ("cat-file", "blob"):
            blob_read_attempted = True
        return real_run_git(repo_root, *args)

    monkeypatch.setattr(release_validator, "_run_git", fail_head_resolution)

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("Git HEAD 无法验证" in error for error in result["errors"])
    assert blob_read_attempted is False


def test_wechat_release_form_rejects_candidate_markers_in_frozen_artifacts(tmp_path):
    for index, marker in enumerate(("候选", "这是候选草稿", "通用候选状态")):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        _prepare_release_repo(case_dir)
        artifact = case_dir / _RELEASE_ARTIFACTS["WECHAT_PRIVACY_DOC_SHA256"]
        artifact.write_text(f"legal copy\n{marker}\n", encoding="utf-8")
        _git(case_dir, "add", str(artifact.relative_to(case_dir)))
        _git(case_dir, "commit", "--quiet", "-m", "candidate marker")
        form = _write_wechat_release_form(case_dir)

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any(
            "WECHAT_PRIVACY_DOC_SHA256" in error and "候选占位" in error
            for error in result["errors"]
        )


def test_wechat_release_form_requires_unique_explicit_final_marker(tmp_path):
    for name, replacement in (
        ("missing", "release artifact 3\n"),
        (
            "duplicate",
            "<!-- WECHAT_RELEASE_STATUS: final -->\n"
            "<!-- WECHAT_RELEASE_STATUS: final -->\n"
            "release artifact 3\n",
        ),
        ("malformed", "<!-- WECHAT_RELEASE_STATUS: FINAL -->\nrelease artifact 3\n"),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        _prepare_release_repo(case_dir)
        listing = case_dir / _RELEASE_ARTIFACTS["WECHAT_LISTING_COPY_SHA256"]
        listing.write_text(replacement, encoding="utf-8")
        _git(case_dir, "add", str(listing.relative_to(case_dir)))
        _git(case_dir, "commit", "--quiet", "-m", f"{name} final marker")
        form = _write_wechat_release_form(case_dir)

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any("正式发布状态 marker" in error for error in result["errors"])


def test_wechat_release_form_binds_exact_name_to_every_frozen_artifact(tmp_path):
    cases = (
        (
            "wrong-marker",
            "<!-- WECHAT_MINIPROGRAM_NAME: 宜老天气通 -->",
            "<!-- WECHAT_MINIPROGRAM_NAME: 其他名称 -->",
            "名称 marker",
        ),
        (
            "missing-visible-name",
            "小程序名称：宜老天气通",
            "小程序名称已冻结",
            "可见正文",
        ),
    )
    for name, old_value, new_value, expected_error in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        _prepare_release_repo(case_dir)
        listing = case_dir / _RELEASE_ARTIFACTS["WECHAT_LISTING_COPY_SHA256"]
        listing.write_text(
            listing.read_text(encoding="utf-8").replace(old_value, new_value),
            encoding="utf-8",
        )
        _git(case_dir, "add", str(listing.relative_to(case_dir)))
        _git(case_dir, "commit", "--quiet", "-m", name)
        form = _write_wechat_release_form(case_dir)

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any(
            "WECHAT_MINIPROGRAM_NAME" in error and expected_error in error
            for error in result["errors"]
        )


def test_wechat_release_form_requires_full_feature_release_flags(tmp_path):
    cases = (
        ("missing-wxpusher-flag", {"FEATURE_WXPUSHER": ""}, "FEATURE_WXPUSHER"),
        ("enabled-wxpusher", {"FEATURE_WXPUSHER": "1"}, "FEATURE_WXPUSHER=0"),
        (
            "disabled-with-token",
            {"WXPUSHER_APP_TOKEN": "AT_release-test-token"},
            "清空 WXPUSHER_APP_TOKEN",
        ),
        (
            "gis-disabled",
            {"FEATURE_HEAT_EXPOSURE_GIS": "0"},
            "FEATURE_HEAT_EXPOSURE_GIS",
        ),
    )
    for name, overrides, expected_field in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        form = _write_wechat_release_form(case_dir, **overrides)

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any(expected_field in error for error in result["errors"])


def test_wechat_release_form_binds_effective_date_and_privacy_markers(tmp_path):
    effective_repo = tmp_path / "effective"
    effective_repo.mkdir()
    effective_form = _write_wechat_release_form(
        effective_repo,
        WECHAT_EFFECTIVE_DATE="2026-07-19",
    )
    effective_result = validate_wechat_release_form(
        effective_form,
        require_ready=True,
    )

    assert effective_result["ok"] is False
    assert any(
        "WECHAT_EFFECTIVE_DATE" in error and "marker" in error
        for error in effective_result["errors"]
    )

    privacy_repo = tmp_path / "privacy"
    privacy_repo.mkdir()
    _prepare_release_repo(privacy_repo)
    privacy_doc = privacy_repo / _RELEASE_ARTIFACTS["WECHAT_PRIVACY_DOC_SHA256"]
    privacy_doc.write_text(
        privacy_doc.read_text(encoding="utf-8").replace(
            "<!-- WECHAT_PRIVACY_VERSION: 2026-07-18 -->",
            "<!-- WECHAT_PRIVACY_VERSION: 2026-07-19 -->",
        ),
        encoding="utf-8",
    )
    _git(privacy_repo, "add", str(privacy_doc.relative_to(privacy_repo)))
    _git(privacy_repo, "commit", "--quiet", "-m", "privacy marker mismatch")
    privacy_form = _write_wechat_release_form(privacy_repo)
    privacy_result = validate_wechat_release_form(
        privacy_form,
        require_ready=True,
    )

    assert privacy_result["ok"] is False
    assert any(
        "WX_MINIPROGRAM_PRIVACY_VERSION" in error and "marker" in error
        for error in privacy_result["errors"]
    )


def test_wechat_release_form_requires_unique_visible_legal_values(tmp_path):
    cases = (
        (
            "missing-date",
            "WECHAT_AGREEMENT_PAGE_SHA256",
            "生效日期：2026-07-18",
            "",
            "WECHAT_EFFECTIVE_DATE",
        ),
        (
            "duplicate-date",
            "WECHAT_PRIVACY_PAGE_SHA256",
            "生效日期：2026-07-18",
            "生效日期：2026-07-18\n生效日期：2026-07-18",
            "WECHAT_EFFECTIVE_DATE",
        ),
        (
            "wrong-privacy",
            "WECHAT_PRIVACY_DOC_SHA256",
            "隐私版本：2026-07-18",
            "隐私版本：2026-07-19",
            "WX_MINIPROGRAM_PRIVACY_VERSION",
        ),
    )
    for name, key, old_value, new_value, expected_field in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        _prepare_release_repo(case_dir)
        artifact = case_dir / _RELEASE_ARTIFACTS[key]
        artifact.write_text(
            artifact.read_text(encoding="utf-8").replace(old_value, new_value),
            encoding="utf-8",
        )
        _git(case_dir, "add", str(artifact.relative_to(case_dir)))
        _git(case_dir, "commit", "--quiet", "-m", name)
        form = _write_wechat_release_form(case_dir)

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any(
            expected_field in error and "唯一可见" in error
            for error in result["errors"]
        )


def test_wechat_release_snapshot_locks_ready_and_credentials_for_one_deploy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    form = _write_wechat_release_form(repo)
    snapshot = tmp_path / "wechat-release.snapshot"

    assert snapshot_wechat_release_form(form, snapshot) == []
    original_snapshot = snapshot.read_text(encoding="utf-8")
    form.write_text(
        original_snapshot.replace("WECHAT_FORM_READY=1", "WECHAT_FORM_READY=0")
        .replace("WX_MINIPROGRAM_APPID=wx12345678", "WX_MINIPROGRAM_APPID=wx87654321")
        .replace(
            "WX_MINIPROGRAM_SECRET=1234567890abcdef",
            "WX_MINIPROGRAM_SECRET=fedcba0987654321",
        ),
        encoding="utf-8",
    )

    result = validate_wechat_release_form(
        snapshot,
        require_ready=False,
        repo_root=repo,
    )

    assert result["ok"] is True
    assert result["form_ready"] is True
    assert "WECHAT_FORM_READY=1" in snapshot.read_text(encoding="utf-8")
    assert "WX_MINIPROGRAM_APPID=wx12345678" in snapshot.read_text(encoding="utf-8")
    assert snapshot.stat().st_mode & 0o077 == 0


def test_wechat_release_form_enforces_category_evidence_24_hour_window(tmp_path):
    validation_time = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    cases = (
        ("2026-07-18T19:00:00+08:00", True, "within"),
        ("2026-07-17T12:00:00Z", True, "exact-boundary"),
        ("2026-07-17T11:59:59Z", False, "too-old"),
        ("2026-07-18T12:00:01Z", False, "future"),
    )
    for confirmed_at, expected_ok, name in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        form = _write_wechat_release_form(
            case_dir,
            WECHAT_CATEGORY_CONFIRMED_AT=confirmed_at,
        )

        result = validate_wechat_release_form(
            form,
            require_ready=True,
            validation_time=validation_time,
        )

        assert result["ok"] is expected_ok
        if not expected_ok:
            assert any(
                "WECHAT_CATEGORY_CONFIRMED_AT" in error
                for error in result["errors"]
            )


def test_wechat_release_form_requires_tourist_appid_in_frozen_project_config(tmp_path):
    _prepare_release_repo(tmp_path)
    (tmp_path / "project.config.json").write_text(
        '{"appid":"wx87654321"}\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", "project.config.json")
    _git(tmp_path, "commit", "--quiet", "-m", "different appid")
    form = _write_wechat_release_form(tmp_path)

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("游客占位 AppID" in error for error in result["errors"])
    assert "wx87654321" not in "\n".join(result["errors"])


def test_wechat_release_form_requires_private_project_config(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.unlink()

    result = validate_wechat_release_form(form, require_ready=True)

    error_summary = "\n".join(result["errors"])
    assert result["ok"] is False
    assert any("私有工程配置不存在" in error for error in result["errors"])
    assert str(private_config) not in error_summary


def test_wechat_release_form_requires_private_project_config_mode_0600(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.chmod(0o640)

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("严格为 0600" in error for error in result["errors"])
    assert str(private_config) not in "\n".join(result["errors"])


def test_wechat_release_form_rejects_private_project_config_symlink(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.unlink()
    symlink_target = tmp_path.parent / f"{tmp_path.name}-private-project-config.json"
    symlink_target.write_text('{"appid":"wx12345678"}\n', encoding="utf-8")
    symlink_target.chmod(0o600)
    private_config.symlink_to(symlink_target)

    result = validate_wechat_release_form(form, require_ready=True)

    error_summary = "\n".join(result["errors"])
    assert result["ok"] is False
    assert any("不能是符号链接" in error for error in result["errors"])
    assert str(private_config) not in error_summary
    assert str(symlink_target) not in error_summary


def test_wechat_release_form_rejects_oversized_private_project_config(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.write_bytes(
        b"{" + b" " * release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_MAX_BYTES
    )
    private_config.chmod(0o600)

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("大小异常" in error for error in result["errors"])


def test_wechat_release_form_rejects_invalid_private_project_config_json(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.write_text("{invalid-json\n", encoding="utf-8")
    private_config.chmod(0o600)

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("有效 JSON 对象" in error for error in result["errors"])
    assert "{invalid-json" not in "\n".join(result["errors"])


def test_wechat_release_form_binds_appid_to_private_project_config(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    private_config = _write_private_project_config(tmp_path, appid="wx87654321")

    result = validate_wechat_release_form(form, require_ready=True)

    error_summary = "\n".join(result["errors"])
    assert result["ok"] is False
    assert any("WX_MINIPROGRAM_APPID" in error for error in result["errors"])
    assert "wx87654321" not in error_summary
    assert str(private_config) not in error_summary


@pytest.mark.parametrize(
    "private_values",
    (
        {"appid": "wx12345678", "appSecret": "test-secret-value"},
        {
            "appid": "wx12345678",
            "setting": {"WX_MINIPROGRAM_SECRET": "test-secret-value"},
        },
    ),
)
def test_wechat_release_form_rejects_appsecret_in_private_project_config(
    tmp_path,
    private_values,
):
    form = _write_wechat_release_form(tmp_path)
    private_config = tmp_path / release_validator.WECHAT_PROJECT_PRIVATE_CONFIG_PATH
    private_config.write_text(json.dumps(private_values) + "\n", encoding="utf-8")
    private_config.chmod(0o600)

    result = validate_wechat_release_form(form, require_ready=True)

    error_summary = "\n".join(result["errors"])
    assert result["ok"] is False
    assert any("不得包含 AppSecret" in error for error in result["errors"])
    assert "test-secret-value" not in error_summary
    assert str(private_config) not in error_summary


def test_wechat_release_form_requires_private_project_config_to_be_ignored(tmp_path):
    _prepare_release_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".env.*\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "--quiet", "-m", "remove private config ignore")
    form = _write_wechat_release_form(tmp_path)

    result = validate_wechat_release_form(form, require_ready=True)

    error_summary = "\n".join(result["errors"])
    assert result["ok"] is False
    assert any("必须被 Git 忽略" in error for error in result["errors"])
    assert "wx12345678" not in error_summary
    assert str(tmp_path) not in error_summary


def test_wechat_release_form_binds_privacy_version_to_frozen_config(tmp_path):
    _prepare_release_repo(tmp_path)
    (tmp_path / "miniprogram" / "config.js").write_text(
        "const defaults = {\n"
        "  PRIVACY_CONSENT_VERSION: '2026-07-19',\n"
        "};\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "miniprogram/config.js")
    _git(tmp_path, "commit", "--quiet", "-m", "different privacy version")
    form = _write_wechat_release_form(tmp_path)

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any(
        "WX_MINIPROGRAM_PRIVACY_VERSION" in error for error in result["errors"]
    )
    assert "2026-07-19" not in "\n".join(result["errors"])


def test_wechat_release_form_validates_request_domain_value(tmp_path):
    for name, value in (
        ("empty", ""),
        ("non-https", "http://yilaoweather.org"),
        ("mismatch", "https://api.example.com"),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        form = _write_wechat_release_form(
            case_dir,
            WECHAT_REQUEST_DOMAIN=value,
        )

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any("WECHAT_REQUEST_DOMAIN" in error for error in result["errors"])
        if value:
            assert value not in "\n".join(result["errors"])


def test_wechat_release_form_requires_unique_runtime_request_domain(tmp_path):
    for name, runtime_text in (
        (
            "empty-runtime",
            "module.exports = {\n  API_BASE_URL: '',\n};\n",
        ),
        (
            "multiple-runtime",
            "module.exports = {\n"
            "  API_BASE_URL: 'https://yilaoweather.org',\n"
            "  API_BASE_URL: 'https://yilaoweather.org',\n"
            "};\n",
        ),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        _prepare_release_repo(case_dir)
        runtime_config = case_dir / "miniprogram" / "config.runtime.js"
        runtime_config.write_text(runtime_text, encoding="utf-8")
        _git(case_dir, "add", "miniprogram/config.runtime.js")
        _git(case_dir, "commit", "--quiet", "-m", name)
        form = _write_wechat_release_form(case_dir)

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any("WECHAT_REQUEST_DOMAIN" in error for error in result["errors"])


def test_wechat_release_form_rejects_dirty_tracked_file_without_leaking_path(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    (tmp_path / "release-marker.txt").write_text("changed\n", encoding="utf-8")

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("Git 工作树保持干净" in error for error in result["errors"])
    assert "release-marker.txt" not in "\n".join(result["errors"])


def test_wechat_release_form_rejects_staged_tracked_change(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    (tmp_path / "release-marker.txt").write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "release-marker.txt")

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("Git 工作树保持干净" in error for error in result["errors"])


def test_wechat_release_form_rejects_nonignored_untracked_file(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    (tmp_path / "private-review-draft.txt").write_text(
        "do not disclose\n",
        encoding="utf-8",
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("Git 工作树保持干净" in error for error in result["errors"])
    assert "private-review-draft.txt" not in "\n".join(result["errors"])


def test_wechat_release_form_rejects_target_from_previous_commit(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    (tmp_path / "later-commit.txt").write_text("later\n", encoding="utf-8")
    _git(tmp_path, "add", "later-commit.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "later commit")

    result = validate_wechat_release_form(form, require_ready=True)

    assert result["ok"] is False
    assert any("WECHAT_TARGET_COMMIT_SHA" in error for error in result["errors"])
    assert not any("工作树保持干净" in error for error in result["errors"])


def test_wechat_release_form_rejects_each_well_formed_fake_artifact_hash(tmp_path):
    fake_digest = "0" * 64
    for key in _RELEASE_ARTIFACTS:
        case_dir = tmp_path / key.lower()
        case_dir.mkdir()
        form = _write_wechat_release_form(case_dir, **{key: fake_digest})

        result = validate_wechat_release_form(form, require_ready=True)

        assert result["ok"] is False
        assert any(key in error for error in result["errors"])
        assert fake_digest not in "\n".join(result["errors"])


def test_wechat_release_form_allows_gitignored_private_files(tmp_path):
    form = _write_wechat_release_form(tmp_path)
    (tmp_path / ".env.local").write_text(
        "PRIVATE_VALUE=never-print-this\n",
        encoding="utf-8",
    )

    result = validate_wechat_release_form(form, require_ready=True)

    assert _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert result["ok"] is True
    assert result["form_ready"] is True


def test_wechat_preview_skips_git_and_content_freeze_until_form_ready(tmp_path):
    _prepare_release_repo(tmp_path)
    artifact = tmp_path / _RELEASE_ARTIFACTS["WECHAT_PRIVACY_DOC_SHA256"]
    artifact.write_text("发布候选版\n", encoding="utf-8")
    _git(tmp_path, "add", str(artifact.relative_to(tmp_path)))
    _git(tmp_path, "commit", "--quiet", "-m", "preview candidate")
    form = _write_wechat_release_form(
        tmp_path,
        WECHAT_FORM_READY="0",
        WECHAT_CATEGORY_CONFIRMED="0",
        WECHAT_CATEGORY_CONFIRMED_AT="2000-01-01T00:00:00Z",
        WECHAT_TARGET_COMMIT_SHA="a" * 40,
        WECHAT_PRIVACY_DOC_SHA256="b" * 64,
        WECHAT_AGREEMENT_DOC_SHA256="c" * 64,
        WECHAT_LISTING_COPY_SHA256="d" * 64,
        WECHAT_PRIVACY_PAGE_SHA256="e" * 64,
        WECHAT_AGREEMENT_PAGE_SHA256="f" * 64,
    )
    (tmp_path / "preview-only.txt").write_text("preview\n", encoding="utf-8")

    result = validate_wechat_release_form(form, require_ready=False)

    assert result["ok"] is True
    assert result["form_ready"] is False
    assert result["warnings"]
