# -*- coding: utf-8 -*-
"""预报失效、备用源、跨日缓存及村点落格回归。"""

import json
from datetime import timedelta

import pytest

from services import heat_risk_workbench_forecast as forecast_service
from services.heat_risk_workbench_service import (
    ACTION_CARDS, _geometry_covers, classify_daily_hazard, locate_cell,
    rank_villages, village_points,
)


def _forecast(source="QWeather", start=None):
    from core.time_utils import today_local

    start = start or today_local()
    return [{"forecast_date": (start + timedelta(days=i)).isoformat(),
             "temperature_max": 36, "temperature_min": 24, "humidity": 60,
             "temperature_mean": 30, "data_source": source, "is_mock": False}
            for i in range(7)]


@pytest.fixture
def production_forecasts(app, monkeypatch):
    from core import weather

    app.config["DEMO_MODE"] = False
    monkeypatch.setattr(weather, "get_qweather_forecast_with_cache", lambda *a, **kw: ([], False, {}))
    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", lambda location: [])
    return app


def test_all_real_sources_fail_reports_unknown(production_forecasts, authenticated_client):
    payload = authenticated_client.get("/heat-exposure-gis/daily.json").get_json()
    assert payload["forecast_status"] == "unavailable"
    assert payload["forecast_source"] == "预报暂不可用"
    assert payload["forecast_notice"] == "风险暂不可判定；地图仅供静态参考"
    assert payload["days"] == payload["priority"] == []
    assert len(payload["villages"]) == 16
    assert "cooling_resources" in payload


def test_real_fallback_is_labeled_and_cached(production_forecasts, authenticated_client, monkeypatch):
    calls = []

    def fetch(location):
        calls.append(location)
        return _forecast("Open-Meteo")

    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", fetch)
    for _ in range(2):
        payload = authenticated_client.get("/heat-exposure-gis/daily.json").get_json()
        assert payload["forecast_status"] == "fallback"
        assert payload["forecast_source"] == "Open-Meteo（备用预报）"
        assert len(payload["days"]) == 7
        assert payload["days"][0]["level"] == 2
    assert len(calls) == 1


def test_primary_valid_prevents_fallback(production_forecasts, authenticated_client, monkeypatch):
    from core import weather

    monkeypatch.setattr(weather, "get_qweather_forecast_with_cache", lambda *a, **kw: (_forecast(), True, {}))
    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", lambda location: pytest.fail("有效主源不应调用备用源"))
    payload = authenticated_client.get("/heat-exposure-gis/daily.json").get_json()
    assert payload["forecast_status"] == "ok"
    assert payload["forecast_source"] == "和风天气 7 天预报"


@pytest.mark.parametrize("meta", [{"stale": True}, {"expires_at": "2020-01-01T00:00:00Z"}])
def test_expired_primary_uses_fallback(production_forecasts, authenticated_client, monkeypatch, meta):
    from core import weather

    monkeypatch.setattr(weather, "get_qweather_forecast_with_cache", lambda *a, **kw: (_forecast(), True, meta))
    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", lambda location: _forecast("Open-Meteo"))
    assert authenticated_client.get("/heat-exposure-gis/daily.json").get_json()["forecast_status"] == "fallback"


@pytest.mark.parametrize("field,value", [
    ("temperature_max", None), ("temperature_min", None), ("temperature_max", float("nan")),
    ("temperature_min", float("inf")), ("temperature_max", 20), ("temperature_max", True),
    ("forecast_date", "2020-01-01"), ("is_mock", True), ("is_demo", True),
    ("data_source", "Mock"), ("data_source", None),
])
def test_invalid_primary_and_fallback_are_rejected(app, field, value):
    with app.app_context():
        for source in ("QWeather", "Open-Meteo"):
            entries = _forecast(source)
            entries[3][field] = value
            assert forecast_service.validated_forecast(entries, source) == []


def test_date_conflicts_and_gaps_are_rejected(app):
    with app.app_context():
        entries = _forecast()
        entries[0]["date"] = entries[1]["forecast_date"]
        assert forecast_service.validated_forecast(entries, "QWeather") == []
        assert forecast_service.validated_forecast(_forecast()[:6], "QWeather") == []


def test_fresh_cache_from_yesterday_is_rejected(production_forecasts, db_session, monkeypatch):
    from core.db_models import ForecastCache
    from core.time_utils import today_local, utcnow

    db_session.add(ForecastCache(location="heat-workbench-openmeteo:都昌县", days=7,
                                fetched_at=utcnow(), is_mock=False,
                                payload=json.dumps(_forecast("Open-Meteo", today_local() - timedelta(days=1)))))
    db_session.commit()
    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", lambda location: _forecast("Open-Meteo"))
    actual = forecast_service.get_openmeteo_forecast_with_cache("都昌县")
    assert actual[0]["forecast_date"] == today_local().isoformat()


@pytest.mark.parametrize("failure", ["exception", "empty", "invalid"])
def test_failed_fallback_cooldown_and_recovery_without_redis(production_forecasts, db_session, monkeypatch, failure):
    from core import weather
    from core.time_utils import utcnow

    instant = utcnow()
    monkeypatch.setattr(forecast_service, "utcnow", lambda: instant)
    monkeypatch.setattr(weather, "_get_redis_client", lambda: None)
    calls = []

    def fetch(location):
        calls.append(location)
        if failure == "exception":
            raise TimeoutError("模拟备用源超时")
        if failure == "empty":
            return []
        entries = _forecast("Open-Meteo")
        entries[0]["temperature_max"] = None
        return entries

    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", fetch)
    for _ in range(2):
        entries, status, _, _ = forecast_service.get_workbench_forecast("都昌县")
        assert entries == [] and status == "unavailable"
    assert len(calls) == 1

    instant += timedelta(seconds=61)
    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", lambda location: _forecast("Open-Meteo"))
    entries, status, _, _ = forecast_service.get_workbench_forecast("都昌县")
    assert len(entries) == 7 and status == "fallback"


def test_failure_cooldown_redis_and_midnight(production_forecasts, db_session, monkeypatch):
    from core import weather
    from core.db_models import ForecastCache
    from core.time_utils import today_local

    calls, stored = [], {}

    class Redis:
        def get(self, key):
            return stored.get(key)

        def setex(self, key, ttl, payload):
            assert ttl == 60
            stored[key] = payload

    monkeypatch.setattr(weather, "_get_redis_client", lambda: Redis())
    monkeypatch.setattr(forecast_service, "_fetch_openmeteo", lambda location: calls.append(location) or [])
    assert forecast_service.get_openmeteo_forecast_with_cache("都昌县") == []
    db_session.query(ForecastCache).delete()
    db_session.commit()
    assert forecast_service.get_openmeteo_forecast_with_cache("都昌县") == []
    assert len(calls) == 1

    # Redis TTL 尚未到期，但日期已变，失败标记也不得阻止新一天重试。
    for key in stored:
        payload = json.loads(stored[key])
        payload["unavailable_on"] = (today_local() - timedelta(days=1)).isoformat()
        stored[key] = json.dumps(payload)
    assert forecast_service.get_openmeteo_forecast_with_cache("都昌县") == []
    assert len(calls) == 2


def test_primary_cache_from_yesterday_rejected(production_forecasts, authenticated_client, monkeypatch):
    from core import weather
    from core.time_utils import today_local

    monkeypatch.setattr(weather, "get_qweather_forecast_with_cache", lambda *a, **kw: (
        _forecast(start=today_local() - timedelta(days=1)), True, {}))
    payload = authenticated_client.get("/heat-exposure-gis/daily.json").get_json()
    assert payload["forecast_status"] == "unavailable"


def test_openmeteo_parser_does_not_replace_missing_temperature(app, monkeypatch):
    from core import weather
    from services.weather_service import WeatherService

    daily = {"time": [entry["forecast_date"] for entry in _forecast()],
             "temperature_2m_max": [36] * 7, "temperature_2m_min": [24] * 7}
    daily["temperature_2m_max"][0] = None

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"daily": daily}

    with app.app_context():
        monkeypatch.setattr(weather, "get_weather_fetcher", lambda: WeatherService())
        monkeypatch.setattr(forecast_service.requests, "get", lambda *a, **kw: Response())
        parsed = forecast_service._fetch_openmeteo("都昌县")
        assert parsed[0]["temperature_max"] is None
        assert forecast_service.validated_forecast(parsed, "Open-Meteo") == []


def test_observed_heatwave_does_not_require_today_record(production_forecasts, authenticated_client, db_session, monkeypatch):
    from core import weather
    from core.db_models import WeatherData
    from core.time_utils import today_local

    production_forecasts.config["HEAT_WORKBENCH_LOCATION"] = " 都昌 "
    for offset in (1, 2):
        db_session.add(WeatherData(location="都昌县", date=today_local() - timedelta(days=offset), temperature_max=36))
    db_session.commit()
    monkeypatch.setattr(weather, "get_qweather_forecast_with_cache", lambda *a, **kw: (_forecast(), False, {}))
    payload = authenticated_client.get("/heat-exposure-gis/daily.json").get_json()
    assert payload["days"][0]["hot_day_run"] == 3
    assert payload["days"][0]["level"] == 3


@pytest.mark.parametrize("high,low", [(None, 29), (36, None), (float("nan"), 24), (36, float("inf")), (20, 25)])
def test_missing_temperature_never_means_low_risk(high, low):
    day = classify_daily_hazard([{"temperature_max": high, "temperature_min": low}], 26.5)[0]
    assert day["level"] is None and day["label"] == "风险暂不可判定"
    assert rank_villages([{"name": "村", "static_level": 4}], day) == []


def test_four_villages_use_actual_grid_and_village_township(app):
    villages = {row["name"]: row for row in village_points(app.config["COMMUNITY_COORDS_GCJ"])}
    expected = {
        "牛家垄周村": ("h28v06-r0079-c0156", 26), "新屋汪家": ("h28v06-r0078-c0154", 23.4),
        "段家颈村": ("h28v06-r0079-c0155", 43), "上下付村": ("h28v06-r0080-c0156", 44),
    }
    for name, (cell_id, score) in expected.items():
        assert villages[name]["cell_id"] == cell_id
        assert villages[name]["static_score"] == score
    assert villages["牛家垄周村"]["township"] == "北山乡"
    assert locate_cell(0, 0) is None


def test_geometry_boundary_and_holes_are_deterministic():
    geometry = {"type": "Polygon", "coordinates": [
        [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]],
        [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]],
    ]}
    assert _geometry_covers(0, 1, geometry)
    assert not _geometry_covers(1, 1, geometry)
    assert not _geometry_covers(3, 1, geometry)


def test_hydration_and_emergency_advice_covers_restrictions():
    advice = json.dumps(ACTION_CARDS, ensure_ascii=False)
    assert "1.5–2 升" not in advice
    for level in (0, 1, 2):
        assert "限水" in json.dumps(ACTION_CARDS[level], ensure_ascii=False)
    assert "有汗也可能发生" in advice
    assert "停止出汗" not in advice
