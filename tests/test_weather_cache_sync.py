# -*- coding: utf-8 -*-
"""受管天气周期的锁、租约与来源新鲜度回归测试。"""

import json
import hashlib
from datetime import timedelta

import pytest


class _LeaseRedis:
    def __init__(self, accepted=True, values=None):
        self.accepted = accepted
        self.calls = []
        self.values = dict(values or {})

    def set(self, key, value, **kwargs):
        self.calls.append((key, value, kwargs))
        if self.accepted:
            self.values[key] = value
        return self.accepted

    def get(self, key):
        return self.values.get(key)


def test_production_cycle_rejects_busy_redis_lease_before_fetcher(app, monkeypatch):
    from services.pipelines import sync_weather_cache as pipeline

    fake_redis = _LeaseRedis(accepted=False)
    app.config.update(
        DEBUG=False,
        TESTING=False,
        QWEATHER_AUTH_MODE="api_key",
        QWEATHER_KEY="test-only-key",
        QWEATHER_API_BASE="https://unit-test.qweatherapi.com/v7",
        REDIS_URL="redis://127.0.0.1:6379/0",
    )
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "get_qweather_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        pipeline,
        "WeatherService",
        lambda: pytest.fail("lease 被占用时不得创建联网 fetcher"),
    )

    result = pipeline.sync_weather_cache(update_daily=False)

    assert result["status"] == "busy"
    assert result["reason"] == "redis_lease_busy"
    assert len(fake_redis.calls) == 1
    assert fake_redis.calls[0][2] == {"nx": True, "ex": 1800}


def test_same_host_lock_is_nonblocking():
    from services.pipelines import sync_weather_cache as pipeline

    assert pipeline._HOST_LOCK.acquire(blocking=False) is True
    try:
        with pytest.raises(pipeline.WeatherSyncBusy, match="host_thread_lock_busy"):
            with pipeline._host_cycle_lock():
                pass
    finally:
        pipeline._HOST_LOCK.release()


def test_formal_smoke_ticket_is_receipt_bound_and_consumed_once(
    app,
    tmp_path,
    monkeypatch,
):
    from services.pipelines import sync_weather_cache as pipeline

    token = "formal-test-token"
    binding = "a" * 64
    lease_token = "c" * 64
    ticket = tmp_path / "formal-smoke.ticket"
    ticket.write_text(
        "binding=" + binding + "\n"
        "token_sha256=" + hashlib.sha256(token.encode()).hexdigest() + "\n"
        "lease_token_sha256="
        + hashlib.sha256(lease_token.encode()).hexdigest()
        + "\n",
        encoding="ascii",
    )
    ticket.chmod(0o640)
    fake_redis = _LeaseRedis(
        accepted=True,
        values={pipeline.SYNC_LEASE_KEY: lease_token},
    )
    monkeypatch.setattr(pipeline, "get_qweather_redis_client", lambda: fake_redis)
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_TOKEN", token)
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_BINDING", binding)
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_TICKET", str(ticket))
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN", lease_token)

    with app.app_context():
        assert pipeline._acquire_distributed_cycle_lease() is True
        with pytest.raises(pipeline.WeatherSyncBusy, match="formal_smoke_ticket_invalid"):
            pipeline._acquire_distributed_cycle_lease()

    assert ticket.exists() is False
    assert len(fake_redis.calls) == 1
    assert fake_redis.calls[0][0].startswith(
        pipeline.FORMAL_SMOKE_USED_PREFIX + ":" + binding
    )
    assert fake_redis.calls[0][2] == {"nx": True, "ex": 7 * 86400}


def test_formal_smoke_keeps_ticket_when_preclaimed_lease_does_not_match(
    app,
    tmp_path,
    monkeypatch,
):
    from services.pipelines import sync_weather_cache as pipeline

    token = "formal-test-token"
    binding = "b" * 64
    lease_token = "d" * 64
    ticket = tmp_path / "formal-smoke.ticket"
    ticket.write_text(
        "binding=" + binding + "\n"
        "token_sha256=" + hashlib.sha256(token.encode()).hexdigest() + "\n"
        "lease_token_sha256="
        + hashlib.sha256(lease_token.encode()).hexdigest()
        + "\n",
        encoding="ascii",
    )
    ticket.chmod(0o640)
    fake_redis = _LeaseRedis(
        accepted=True,
        values={pipeline.SYNC_LEASE_KEY: "e" * 64},
    )
    monkeypatch.setattr(pipeline, "get_qweather_redis_client", lambda: fake_redis)
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_TOKEN", token)
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_BINDING", binding)
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_TICKET", str(ticket))
    monkeypatch.setenv("CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN", lease_token)

    with app.app_context():
        with pytest.raises(pipeline.WeatherSyncBusy, match="formal_smoke_lease_invalid"):
            pipeline._acquire_distributed_cycle_lease()

    assert ticket.is_file()
    assert fake_redis.calls == []


def test_formal_smoke_lease_reservation_fails_before_receipt_when_busy(
    app,
    monkeypatch,
):
    from services.pipelines import sync_weather_cache as pipeline

    fake_redis = _LeaseRedis(accepted=False)
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "get_qweather_redis_client", lambda: fake_redis)

    with pytest.raises(pipeline.WeatherSyncBusy, match="redis_lease_busy"):
        pipeline.reserve_formal_cycle_lease("f" * 64)

    assert len(fake_redis.calls) == 1
    assert fake_redis.calls[0][0] == pipeline.SYNC_LEASE_KEY
    assert fake_redis.calls[0][2] == {"nx": True, "ex": 1800}


def test_current_weather_can_disable_untracked_fallback(monkeypatch):
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = "test-key"
    service.api_base_url = "https://qweather.invalid"
    monkeypatch.setattr(service, "_get_location", lambda _city: "116.20,29.27")
    monkeypatch.setattr(service, "_qweather_headers", lambda: {})
    monkeypatch.setattr(weather_module, "reserve_qweather_request", lambda _endpoint: False)
    monkeypatch.setattr(
        service,
        "_get_fallback_weather",
        lambda *_args: pytest.fail("正式烟测关闭备用源后不得访问 Open-Meteo"),
    )
    monkeypatch.setattr(
        weather_module.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("预算阻断后不得访问 QWeather"),
    )

    assert service.get_current_weather("都昌县", allow_fallback=False) == {}


@pytest.mark.parametrize(
    "source_status",
    [
        {
            "weather": {
                "available": True,
                "provider": "QWeather",
                "is_mock": False,
            },
            "forecast": {
                "available": True,
                "providers": ["QWeather"],
            },
            "warnings": {
                "available": False,
                "status": "network_error",
            },
        },
        {
            "weather": {
                "available": True,
                "provider": "QWeather",
                "is_mock": False,
            },
            "forecast": {
                "available": False,
                "providers": [],
            },
            "warnings": {
                "available": True,
                "status": "ok",
            },
        },
        {
            "weather": {
                "available": True,
                "provider": "Open-Meteo",
                "is_mock": False,
            },
            "forecast": {
                "available": True,
                "providers": ["QWeather"],
            },
            "warnings": {
                "available": True,
                "status": "ok",
            },
        },
    ],
)
def test_dispatch_rejects_degraded_weather_cycle(source_status):
    from services.pipelines import sync_weather_cache as pipeline

    assert pipeline._trusted_complete_snapshot(
        {"source_status": source_status}
    ) is False


def test_dispatch_accepts_complete_qweather_cycle():
    from services.pipelines import sync_weather_cache as pipeline

    assert pipeline._trusted_complete_snapshot(
        {
            "source_status": {
                "weather": {
                    "available": True,
                    "provider": "QWeather",
                    "is_mock": False,
                },
                "forecast": {
                    "available": True,
                    "providers": ["QWeather"],
                },
                "warnings": {
                    "available": True,
                    "status": "ok",
                },
            }
        }
    ) is True


def test_force_refresh_does_not_reuse_29_minute_forecast(
    app,
    db_session,
    monkeypatch,
):
    from core import weather
    from core.db_models import ForecastCache
    from core.time_utils import today_local, utcnow

    fixed_now = utcnow()
    start = today_local()

    def forecast(temperature):
        return [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "forecast_date": (start + timedelta(days=index)).isoformat(),
                "temperature_max": temperature,
                "temperature_min": 20,
                "temperature_mean": (temperature + 20) / 2,
                "humidity": 60,
                "data_source": "QWeather",
                "is_mock": False,
            }
            for index in range(7)
        ]

    old_forecast = forecast(30)
    new_forecast = forecast(39)
    db_session.add(
        ForecastCache(
            location="qweather-only:都昌县",
            days=7,
            fetched_at=fixed_now - timedelta(minutes=29),
            payload=json.dumps(
                {"daily": old_forecast, "meta": {"source": "QWeather"}},
                ensure_ascii=False,
            ),
            is_mock=False,
        )
    )
    db_session.commit()

    class Fetcher:
        calls = 0

        def get_qweather_daily_forecast(self, _location, days=7):
            assert days == 7
            self.calls += 1
            return {
                "success": True,
                "daily": new_forecast,
                "meta": {"source": "QWeather"},
            }

    fetcher = Fetcher()
    monkeypatch.setattr(weather, "utcnow", lambda: fixed_now)
    with app.app_context():
        app.config.update(DEMO_MODE=False, FORECAST_CACHE_TTL_MINUTES=30)
        data, from_cache, meta = weather.get_qweather_forecast_with_cache(
            "都昌县",
            days=7,
            cache_only=False,
            fetcher=fetcher,
            force_refresh=True,
        )

    assert fetcher.calls == 1
    assert from_cache is False
    assert data[0]["temperature_max"] == 39
    assert meta["fetched_at"] == fixed_now.isoformat()
    assert meta["expires_at"] == (fixed_now + timedelta(minutes=30)).isoformat()


def test_snapshot_expiry_uses_earliest_required_source(app, db_session):
    from core.time_utils import utcnow
    from services.miniprogram_service import persist_snapshot, snapshot_payload

    cycle_time = utcnow().replace(microsecond=0)
    forecast_time = cycle_time - timedelta(minutes=29)
    forecast = [{
        "date": cycle_time.date().isoformat(),
        "temperature_max": 38,
        "temperature_min": 27,
        "temperature_mean": 32.5,
        "humidity": 70,
        "data_source": "QWeather",
        "is_mock": False,
    }]
    current = {
        "temperature": 35,
        "humidity": 70,
        "data_source": "QWeather",
        "is_mock": False,
    }
    with app.app_context():
        app.config["QWEATHER_BUDGET_FAIL_CLOSED"] = False
        record = persist_snapshot(
            current,
            forecast,
            [],
            fetched_at=cycle_time,
            forecast_meta={"source": "QWeather"},
            warning_status={"available": True, "status": "ok"},
            source_timing={
                "current": {
                    "fetched_at": cycle_time,
                    "expires_at": cycle_time + timedelta(minutes=30),
                },
                "forecast": {
                    "fetched_at": forecast_time,
                    "expires_at": forecast_time + timedelta(minutes=30),
                },
                "warnings": {
                    "fetched_at": cycle_time,
                    "expires_at": cycle_time + timedelta(minutes=30),
                },
            },
        )
        payload = snapshot_payload(record, now=cycle_time)

    assert payload["fetched_at"] == forecast_time.isoformat()
    assert payload["expires_at"] == (forecast_time + timedelta(minutes=30)).isoformat()
    assert payload["source_status"]["forecast"]["fetched_at"] == forecast_time.isoformat()
    assert payload["source_status"]["budget_guard"] == "enabled"
