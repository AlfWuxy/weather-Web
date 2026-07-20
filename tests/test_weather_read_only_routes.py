# -*- coding: utf-8 -*-
"""普通天气 HTTP 路由只能读取缓存。"""

import json
from datetime import timedelta

import pytest

from core.time_utils import utcnow


class _ForbiddenRouteFetcher:
    def __init__(self):
        self.current_calls = 0
        self.forecast_calls = 0
        self.qweather_forecast_calls = 0
        self.nowcast_calls = 0

    def get_current_weather(self, _location):
        self.current_calls += 1
        raise AssertionError("current 路由不得调用 fetcher")

    def get_weather_forecast(self, _location, days=7):
        del days
        self.forecast_calls += 1
        raise AssertionError("forecast 路由不得调用通用 fetcher")

    def get_qweather_daily_forecast(self, _location, days=7):
        del days
        self.qweather_forecast_calls += 1
        raise AssertionError("forecast 路由不得调用和风 fetcher")

    def get_short_term_nowcast(self, _location, hours=24):
        del hours
        self.nowcast_calls += 1
        raise AssertionError("nowcast 路由不得调用 fetcher")


def test_current_and_forecast_http_routes_do_not_call_fetcher(
    app,
    authenticated_client,
    db_session,
):
    from core.weather import register_weather_fetcher

    del db_session
    fetcher = _ForbiddenRouteFetcher()
    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        register_weather_fetcher(fetcher)

    current_response = authenticated_client.get('/api/v1/weather/current?location=都昌')
    forecast_response = authenticated_client.post(
        '/api/v1/forecast/7day',
        json={'city': '都昌'},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert current_response.status_code == 200
    assert current_response.get_json()['success'] is False
    assert forecast_response.status_code == 503
    assert forecast_response.get_json()['success'] is False
    assert fetcher.current_calls == 0
    assert fetcher.forecast_calls == 0
    assert fetcher.qweather_forecast_calls == 0
    assert fetcher.nowcast_calls == 0


def test_nowcast_http_route_reads_scheduled_cache_only(app, client, db_session):
    from core.db_models import ForecastCache
    from core.weather import register_weather_fetcher

    fetcher = _ForbiddenRouteFetcher()
    timeline = [
        {
            'time': f'2026-07-18T{hour:02d}:00',
            'precipitation_probability': float(hour * 10),
            'precipitation_mm': 0.0,
            'temperature': float(30 + hour),
            'condition': '多云',
            'risk_level': '低',
        }
        for hour in range(1, 4)
    ]
    with app.app_context():
        app.config['DEMO_MODE'] = False
        register_weather_fetcher(fetcher)
        db_session.add(ForecastCache(
            location='nowcast:都昌县',
            days=24,
            fetched_at=utcnow(),
            payload=json.dumps({
                'available': True,
                'source': 'Open-Meteo',
                'timeline': timeline,
            }, ensure_ascii=False),
            is_mock=False,
        ))
        db_session.commit()

    response = client.get('/api/weather/nowcast?hours=2')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['data']['available'] is True
    assert payload['data']['from_cache'] is True
    assert payload['data']['stale'] is False
    assert payload['data']['timeline'] == timeline[:2]
    assert fetcher.current_calls == 0
    assert fetcher.forecast_calls == 0
    assert fetcher.qweather_forecast_calls == 0
    assert fetcher.nowcast_calls == 0


def test_stale_current_and_nowcast_are_unavailable(app, client, db_session):
    from core.db_models import ForecastCache, WeatherCache
    from core.weather import register_weather_fetcher

    fetcher = _ForbiddenRouteFetcher()
    stale_at = utcnow() - timedelta(hours=2)
    with app.app_context():
        app.config.update(DEMO_MODE=False, WEATHER_CACHE_TTL_MINUTES=30)
        app.extensions['redis_client'] = None
        register_weather_fetcher(fetcher)
        db_session.add(WeatherCache(
            location='都昌县',
            fetched_at=stale_at,
            payload=json.dumps({
                'temperature': 36.0,
                'temperature_max': 39.0,
                'temperature_min': 29.0,
                'humidity': 62.0,
                'data_source': 'QWeather',
                'is_mock': False,
            }, ensure_ascii=False),
            is_mock=False,
        ))
        db_session.add(ForecastCache(
            location='nowcast:都昌县',
            days=24,
            fetched_at=stale_at,
            payload=json.dumps({
                'available': True,
                'source': 'Open-Meteo',
                'timeline': [{'time': '2026-07-18T01:00'}],
            }, ensure_ascii=False),
            is_mock=False,
        ))
        db_session.commit()

    current_response = client.get('/api/v1/weather/current')
    nowcast_response = client.get('/api/weather/nowcast')

    assert current_response.status_code == 200
    assert current_response.get_json()['success'] is False
    nowcast = nowcast_response.get_json()['data']
    assert nowcast_response.status_code == 200
    assert nowcast['available'] is False
    assert nowcast['timeline'] == []
    assert nowcast['reason'] == 'cache_stale'
    assert nowcast['stale'] is True
    assert nowcast['cache_age_seconds'] >= 7200
    assert fetcher.current_calls == 0
    assert fetcher.forecast_calls == 0
    assert fetcher.qweather_forecast_calls == 0
    assert fetcher.nowcast_calls == 0


@pytest.mark.parametrize(
    ('extra_microseconds', 'expected_stale'),
    ((0, False), (1, True)),
)
def test_nowcast_uses_exact_1800_second_boundary(
    app,
    client,
    db_session,
    monkeypatch,
    extra_microseconds,
    expected_stale,
):
    from core.db_models import ForecastCache

    fixed_now = utcnow()
    monkeypatch.setattr('services.api_service.utcnow', lambda: fixed_now)
    with app.app_context():
        db_session.add(ForecastCache(
            location='nowcast:都昌县',
            days=24,
            fetched_at=fixed_now - timedelta(
                seconds=1800,
                microseconds=extra_microseconds,
            ),
            payload=json.dumps({
                'available': True,
                'source': 'Open-Meteo',
                'timeline': [{'time': '2026-07-18T01:00'}],
            }, ensure_ascii=False),
            is_mock=False,
        ))
        db_session.commit()

    response = client.get('/api/weather/nowcast')
    data = response.get_json()['data']

    assert response.status_code == 200
    assert data['stale'] is expected_stale
    assert data['available'] is (not expected_stale)
    assert bool(data['timeline']) is (not expected_stale)
