# -*- coding: utf-8 -*-
"""Open-Meteo 7 日 weather-only 兜底回归测试。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return deepcopy(self._payload)


def _openmeteo_daily_payload(start_date, days=7):
    return {
        'daily': {
            'time': [(start_date + timedelta(days=index)).isoformat() for index in range(days)],
            'temperature_2m_max': [34.0 + index for index in range(days)],
            'temperature_2m_min': [25.0 + index / 2 for index in range(days)],
            'precipitation_probability_max': [20 + index * 5 for index in range(days)],
            'weather_code': [1, 2, 3, 61, 63, 80, 95][:days],
        },
    }


def _openmeteo_entries(start_date, days=7):
    return [
        {
            'date': (start_date + timedelta(days=index)).isoformat(),
            'forecast_date': (start_date + timedelta(days=index)).isoformat(),
            'temperature_max': 34.0 + index,
            'temperature_min': 25.0 + index / 2,
            'temperature_mean': 29.5 + index * 0.75,
            'condition': '晴' if index < 3 else '小雨',
            'condition_night': '晴' if index < 3 else '小雨',
            'humidity': None,
            'precip_probability': 20 + index * 5,
            'data_source': 'Open-Meteo',
            'is_mock': False,
        }
        for index in range(days)
    ]


@pytest.mark.parametrize(
    'mutate',
    (
        lambda payload: payload['daily']['temperature_2m_max'].__setitem__(0, float('nan')),
        lambda payload: payload['daily']['temperature_2m_min'].__setitem__(0, float('inf')),
        lambda payload: payload['daily']['temperature_2m_max'].__setitem__(0, 20),
        lambda payload: payload['daily']['precipitation_probability_max'].__setitem__(0, 101),
        lambda payload: payload['daily']['time'].__setitem__(0, 'invalid-date'),
        lambda payload: payload['daily'].__setitem__('weather_code', [1]),
    ),
    ids=('nan', 'inf', 'reversed-range', 'invalid-pop', 'invalid-date', 'missing-day'),
)
def test_openmeteo_daily_forecast_rejects_invalid_or_missing_values(app, monkeypatch, mutate):
    from core.time_utils import today_local
    from services import weather_service as weather_module

    payload = _openmeteo_daily_payload(today_local(), days=2)
    mutate(payload)
    with app.app_context():
        service = weather_module.WeatherService()
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response(payload),
        )

        assert service._get_openmeteo_forecast('都昌', days=2) == []


def test_openmeteo_daily_forecast_returns_only_valid_weather_fields(app, monkeypatch):
    from core.time_utils import today_local
    from services import weather_service as weather_module

    payload = _openmeteo_daily_payload(today_local(), days=2)
    with app.app_context():
        service = weather_module.WeatherService()
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response(payload),
        )

        result = service.get_openmeteo_daily_forecast('任意村', days=2)

    assert result['success'] is True
    assert result['meta']['source'] == 'Open-Meteo'
    assert len(result['daily']) == 2
    assert result['daily'][0]['precip_probability'] == 20.0
    assert result['daily'][0]['humidity'] is None
    assert result['daily'][0]['data_source'] == 'Open-Meteo'


def test_openmeteo_forecast_cache_is_provider_specific_and_duchang_canonical(
    app,
    db_session,
):
    from core.db_models import ForecastCache
    from core.time_utils import today_local
    from core.weather import get_openmeteo_forecast_with_cache, register_weather_fetcher

    calls = []
    entries = _openmeteo_entries(today_local())

    class FakeFetcher:
        def get_openmeteo_daily_forecast(self, location, days=7):
            calls.append((location, days))
            return {
                'success': True,
                'daily': entries[:days],
                'meta': {'source': 'Open-Meteo'},
            }

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        register_weather_fetcher(FakeFetcher())

        first, first_cached, meta = get_openmeteo_forecast_with_cache('某个村', days=7)
        second, second_cached, _ = get_openmeteo_forecast_with_cache('另一个村', days=7)

        assert first == entries
        assert second == entries
        assert first_cached is False
        assert second_cached is True
        assert meta['source'] == 'Open-Meteo'
        assert calls == [('都昌县', 7)]
        cache = ForecastCache.query.filter_by(
            location='openmeteo-only:都昌县',
            days=7,
        ).one()
        assert cache.is_mock is False


def test_forecast_page_uses_openmeteo_weather_only_without_forecast_service(
    authenticated_client,
    monkeypatch,
):
    from core.time_utils import today_local

    entries = _openmeteo_entries(today_local())
    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda _location, days=7: ([], False, {'source': 'QWeather', 'error': 'unavailable'}),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_openmeteo_forecast_with_cache',
        lambda _location, days=7: (entries[:days], False, {'source': 'Open-Meteo'}),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_forecast_service',
        lambda: pytest.fail('Open-Meteo weather-only 路径不得调用 ForecastService'),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda *_args, **_kwargs: pytest.fail('Open-Meteo weather-only 路径不得读取 AQI 实况上下文'),
    )

    response = authenticated_client.get('/forecast-7day')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '来源：Open-Meteo' in body
    assert '7 天天气预报' in body
    assert '未生成健康风险' in body
    assert '查看“7 天复合暴露评分（0–100）”的计算说明' not in body
    assert '降水概率' in body
    assert '默认AQI 50代理' not in body
    assert '本周高风险日' not in body


def test_forecast_page_keeps_complete_qweather_health_scoring(
    authenticated_client,
    monkeypatch,
):
    from core.time_utils import today_local

    start_date = today_local()
    qweather_days = [
        {
            'date': (start_date + timedelta(days=index)).isoformat(),
            'temperature_max': 34.0,
            'temperature_min': 25.0,
            'temperature_mean': 29.5,
                'humidity': 65.0,
                'condition': '晴',
                'wind_speed': 3.0,
            'precip_probability': 10.0,
            'data_source': 'QWeather',
            'is_mock': False,
        }
        for index in range(7)
    ]
    calls = []

    class FakeForecastService:
        def generate_7day_forecast(self, weather_days, start_date=None, context=None):
            calls.append((weather_days, start_date, context))
            return [
                {
                    'date': item['date'],
                    'composite_exposure': {
                        'final_score': 72,
                        'components': {},
                        'inputs': {},
                    },
                }
                for item in weather_days
            ], {'recommendations': []}

    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda _location, days=7: (qweather_days[:days], False, {'source': 'QWeather'}),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_openmeteo_forecast_with_cache',
        lambda *_args, **_kwargs: pytest.fail('完整 QWeather 不应调用 Open-Meteo fallback'),
    )
    monkeypatch.setattr('blueprints.tools.get_forecast_service', lambda: FakeForecastService())
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: ({
            'temperature': 31,
            'temperature_max': 34,
            'temperature_min': 25,
            'humidity': 65,
            'pressure': 1008,
            'weather_condition': '晴',
            'wind_speed': 3,
            'data_source': 'QWeather',
            'is_mock': False,
            'pm25': 20,
            'aqi': 40,
            'air_quality_available': True,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'air_observed_at': datetime.now(timezone.utc).isoformat(),
        }, False),
    )

    response = authenticated_client.get('/forecast-7day')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert calls
    assert '来源：和风天气' in body
    assert '风险 72' in body
    assert '本周高风险日' in body
