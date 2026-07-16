# -*- coding: utf-8 -*-
"""和风天气单地点缓存与月度预算保护测试。"""

import logging

from flask import Flask


def _reset_local_budget(budget):
    budget._LOCAL_TOTALS.clear()
    budget._LOCAL_ENDPOINTS.clear()
    budget._BLOCKED_LOGGED.clear()


class _FakeBudgetRedis:
    """只实现预算保护测试需要的 Redis 方法。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.hashes = {}

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    def decr(self, key):
        self.values[key] = int(self.values.get(key, 0)) - 1
        return self.values[key]

    def get(self, key):
        return self.values.get(key)

    def expire(self, key, ttl):
        del key, ttl
        return True

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = int(bucket.get(field, 0)) + int(amount)
        return bucket[field]

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def test_monthly_budget_blocks_after_safety_limit(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["REDIS_URL"] = ""
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 2

        assert budget.reserve_qweather_request("weather_now") is True
        assert budget.reserve_qweather_request("air_now") is True
        assert budget.reserve_qweather_request("weather_7d") is False

        snapshot = budget.get_qweather_budget_snapshot()
        assert snapshot["used"] == 2
        assert snapshot["remaining"] == 0
        assert snapshot["endpoints"] == {"weather_now": 1, "air_now": 1}


def test_budget_fails_closed_when_configured_redis_is_unavailable(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["REDIS_URL"] = "redis://127.0.0.1:6379/0"
        app.config["QWEATHER_BUDGET_FAIL_CLOSED"] = True

        assert budget.reserve_qweather_request("weather_now") is False


def test_zero_budget_disables_qweather(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["REDIS_URL"] = ""
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 0

        assert budget.reserve_qweather_request("weather_now") is False


def test_legacy_provider_total_does_not_block_app_counter(app, monkeypatch):
    from services import qweather_budget as budget

    month = budget._month_key()
    legacy_key = f"qweather:budget:{month}:total"
    app_total_key, _ = budget._redis_budget_keys(month)
    fake_redis = _FakeBudgetRedis({legacy_key: 60000})
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: fake_redis)

    with app.app_context():
        app.config["REDIS_URL"] = "redis://127.0.0.1:6379/0"
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 40000

        assert budget.reserve_qweather_request("weather_7d_forecast") is True
        snapshot = budget.get_qweather_budget_snapshot()

    assert fake_redis.get(legacy_key) == 60000
    assert fake_redis.get(app_total_key) == 1
    assert snapshot["used"] == 1
    assert snapshot["remaining"] == 39999
    assert snapshot["endpoints"] == {"weather_7d_forecast": 1}


def test_cache_ttl_has_ten_minute_floor(monkeypatch):
    from core import config

    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("WEATHER_CACHE_TTL_MINUTES", "1")
    monkeypatch.setenv("FORECAST_CACHE_TTL_MINUTES", "2")
    monkeypatch.setenv("QWEATHER_WARNING_CACHE_TTL_MINUTES", "3")

    app = Flask(__name__)
    config.configure_app(app, logging.getLogger(__name__))

    assert app.config["WEATHER_CACHE_TTL_MINUTES"] == 10
    assert app.config["FORECAST_CACHE_TTL_MINUTES"] == 10
    assert app.config["QWEATHER_WARNING_CACHE_TTL_MINUTES"] == 10


def test_weather_cache_uses_one_duchang_key(app):
    from core.weather import _weather_cache_location

    with app.app_context():
        app.config["QWEATHER_CANONICAL_LOCATION"] = "116.20,29.27"

        assert _weather_cache_location("牛家垄周村") == "都昌县"
        assert _weather_cache_location("北京") == "都昌县"
        assert _weather_cache_location("都昌") == "都昌县"


def test_weather_sync_defaults_to_duchang_only(monkeypatch):
    from services.pipelines import sync_weather_cache

    monkeypatch.delenv("WEATHER_SYNC_LOCATIONS", raising=False)

    assert sync_weather_cache._resolve_locations(None) == ["都昌县"]


def test_weather_sync_allows_explicit_location_override(monkeypatch):
    from services.pipelines import sync_weather_cache

    monkeypatch.setenv("WEATHER_SYNC_LOCATIONS", "都昌县,九江")

    assert sync_weather_cache._resolve_locations(None) == ["都昌县", "九江"]
