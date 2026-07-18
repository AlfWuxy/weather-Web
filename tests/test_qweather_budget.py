# -*- coding: utf-8 -*-
"""和风天气单地点缓存与月度预算保护测试。"""

import logging
import importlib
import time

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
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = ""
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
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = ""
        app.config["QWEATHER_BUDGET_FAIL_CLOSED"] = True

        assert budget.reserve_qweather_request("weather_now") is False


def test_configured_redis_never_falls_back_even_if_legacy_flag_is_disabled(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["REDIS_URL"] = "redis://127.0.0.1:6379/0"
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = ""
        app.config["QWEATHER_BUDGET_FAIL_CLOSED"] = False

        assert budget.reserve_qweather_request("weather_now") is False

    assert dict(budget._LOCAL_TOTALS) == {}


def test_zero_budget_disables_qweather(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["REDIS_URL"] = ""
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = ""
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
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = ""
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 40000

        assert budget.reserve_qweather_request("weather_7d_forecast") is True
        snapshot = budget.get_qweather_budget_snapshot()

    assert fake_redis.get(legacy_key) == 60000
    assert fake_redis.get(app_total_key) == 1
    assert snapshot["used"] == 1
    assert snapshot["remaining"] == 39999
    assert snapshot["endpoints"] == {"weather_7d_forecast": 1}


def test_request_context_fails_closed_before_any_budget_counter(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    fake_redis = _FakeBudgetRedis()
    redis_calls = []
    gate_calls = []
    monkeypatch.setattr(
        budget,
        "get_qweather_redis_client",
        lambda: redis_calls.append(True) or fake_redis,
    )
    monkeypatch.setattr(
        budget,
        "_network_gate_allows_request",
        lambda: gate_calls.append(True) or True,
    )

    with app.test_request_context("/api/v1/weather/current"):
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 40000
        assert budget.reserve_qweather_request("weather_now") is False

    assert gate_calls == []
    assert redis_calls == []
    assert fake_redis.values == {}
    assert fake_redis.hashes == {}
    assert dict(budget._LOCAL_TOTALS) == {}
    assert dict(budget._LOCAL_ENDPOINTS) == {}


def test_future_network_gate_blocks_before_any_budget_counter(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    fake_redis = _FakeBudgetRedis()
    redis_calls = []

    def fake_get_redis_client():
        redis_calls.append(True)
        return fake_redis

    monkeypatch.setattr(budget, "get_qweather_redis_client", fake_get_redis_client)
    with app.app_context():
        app.config["REDIS_URL"] = "redis://127.0.0.1:6379/0"
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = str(int(time.time()) + 3600)
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 40000

        assert budget.reserve_qweather_request("weather_now") is False

    assert redis_calls == []
    assert fake_redis.values == {}
    assert fake_redis.hashes == {}
    assert dict(budget._LOCAL_TOTALS) == {}
    assert dict(budget._LOCAL_ENDPOINTS) == {}


def test_invalid_network_gate_fails_closed_without_logging_raw_value(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    raw_value = "private-invalid-gate-value"
    error_messages = []
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    monkeypatch.setattr(
        budget.logger,
        "error",
        lambda message, *args: error_messages.append(message % args if args else message),
    )
    with app.app_context():
        app.config["REDIS_URL"] = ""
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = raw_value
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 40000
        app.config["QWEATHER_BUDGET_FAIL_CLOSED"] = False

        assert budget.reserve_qweather_request("weather_now") is False

    assert dict(budget._LOCAL_TOTALS) == {}
    assert raw_value not in "\n".join(error_messages)
    assert any("网络闸门配置无效" in message for message in error_messages)


def test_expired_network_gate_allows_budget_reservation(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["REDIS_URL"] = ""
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = str(int(time.time()) - 1)
        app.config["QWEATHER_MONTHLY_REQUEST_LIMIT"] = 2

        assert budget.reserve_qweather_request("weather_now") is True
        snapshot = budget.get_qweather_budget_snapshot()

    assert snapshot["used"] == 1
    assert snapshot["endpoints"] == {"weather_now": 1}


def test_formal_runtime_without_redis_fails_closed_across_module_reload(monkeypatch):
    """模块重载模拟下一个定时进程，正式环境不得因本地计数归零而放行。"""
    from services import qweather_budget as imported_budget

    app = Flask(__name__)
    app.config.update(
        DEBUG=False,
        TESTING=False,
        REDIS_URL="",
        WEATHER_CACHE_REDIS_URL="",
        QWEATHER_NETWORK_NOT_BEFORE_EPOCH="",
        QWEATHER_MONTHLY_REQUEST_LIMIT=2,
        QWEATHER_REQUIRE_PERSISTENT_BUDGET=False,
    )

    budget = importlib.reload(imported_budget)
    with app.app_context():
        assert budget.reserve_qweather_request("weather_now") is False
        assert budget.get_qweather_budget_snapshot()["backend"] == "unavailable"
    assert dict(budget._LOCAL_TOTALS) == {}

    budget = importlib.reload(budget)
    with app.app_context():
        assert budget.reserve_qweather_request("weather_now") is False
        assert budget.get_qweather_budget_snapshot()["remaining"] == 0
    assert dict(budget._LOCAL_TOTALS) == {}


def test_debug_runtime_can_explicitly_require_persistent_budget(app, monkeypatch):
    from services import qweather_budget as budget

    _reset_local_budget(budget)
    monkeypatch.setattr(budget, "get_qweather_redis_client", lambda: None)
    with app.app_context():
        app.config["DEBUG"] = True
        app.config["TESTING"] = True
        app.config["REDIS_URL"] = ""
        app.config["WEATHER_CACHE_REDIS_URL"] = ""
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = ""
        app.config["QWEATHER_REQUIRE_PERSISTENT_BUDGET"] = True

        assert budget.reserve_qweather_request("weather_now") is False
        assert budget.get_qweather_budget_snapshot()["backend"] == "unavailable"

    assert dict(budget._LOCAL_TOTALS) == {}


def test_network_gate_opens_at_exact_not_before_second(app):
    from services import qweather_budget as budget

    with app.app_context():
        app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] = "1800"

        assert budget._network_gate_allows_request(now_epoch=1799) is False
        assert budget._network_gate_allows_request(now_epoch=1800) is True
        assert budget._network_gate_allows_request(now_epoch=1801) is True


def test_cache_ttl_has_ten_minute_floor(monkeypatch):
    from core import config

    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("WEATHER_CACHE_TTL_MINUTES", "1")
    monkeypatch.setenv("FORECAST_CACHE_TTL_MINUTES", "2")
    monkeypatch.setenv("QWEATHER_WARNING_CACHE_TTL_MINUTES", "3")
    monkeypatch.setenv("QWEATHER_NETWORK_NOT_BEFORE_EPOCH", "1234567890")

    app = Flask(__name__)
    config.configure_app(app, logging.getLogger(__name__))

    assert app.config["WEATHER_CACHE_TTL_MINUTES"] == 10
    assert app.config["FORECAST_CACHE_TTL_MINUTES"] == 10
    assert app.config["QWEATHER_WARNING_CACHE_TTL_MINUTES"] == 10
    assert app.config["QWEATHER_NETWORK_NOT_BEFORE_EPOCH"] == "1234567890"


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
