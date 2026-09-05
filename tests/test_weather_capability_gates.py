# -*- coding: utf-8 -*-
"""天气来源能力门与持久化边界回归测试。"""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest


QWEATHER_COMPLETE = {
    'temperature': 33.0,
    'temperature_max': 37.0,
    'temperature_min': 26.0,
    'humidity': 68.0,
    'pressure': 1006.0,
    'weather_condition': '晴',
    'wind_speed': 3.2,
    'pm25': 18.0,
    'aqi': 42,
    'air_quality_available': True,
    'observed_at': datetime.now(timezone.utc).isoformat(),
    'air_observed_at': datetime.now(timezone.utc).isoformat(),
    'is_mock': False,
    'data_source': 'QWeather',
}

OPENMETEO_COMPLETE = {
    **QWEATHER_COMPLETE,
    'pm25': None,
    'aqi': None,
    'air_quality_available': False,
    'data_source': 'Open-Meteo',
}


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return deepcopy(self._payload)


def _openmeteo_current_payload():
    return {
        'current': {
            'time': datetime.now().astimezone().isoformat(timespec='minutes'),
            'temperature_2m': 33.4,
            'relative_humidity_2m': 66,
            'surface_pressure': 1008,
            'weather_code': 1,
            'wind_speed_10m': 4.2,
        },
        'daily': {
            'time': [datetime.now().astimezone().date().isoformat()],
            'temperature_2m_max': [37.1],
            'temperature_2m_min': [26.2],
        },
    }


def test_capability_gates_split_display_heat_air_and_qweather():
    from core.weather import (
        is_air_quality_available,
        is_heat_action_weather_ready,
        is_live_observational_weather,
        is_qweather_online_weather,
        is_qweather_production_ready,
    )

    assert is_live_observational_weather(QWEATHER_COMPLETE) is True
    assert is_heat_action_weather_ready(QWEATHER_COMPLETE) is True
    assert is_air_quality_available(QWEATHER_COMPLETE) is True
    assert is_qweather_online_weather(QWEATHER_COMPLETE) is True
    assert is_qweather_production_ready(QWEATHER_COMPLETE) is True

    assert is_live_observational_weather(OPENMETEO_COMPLETE) is True
    assert is_heat_action_weather_ready(OPENMETEO_COMPLETE) is True
    assert is_air_quality_available(OPENMETEO_COMPLETE) is False
    assert is_qweather_online_weather(OPENMETEO_COMPLETE) is False
    assert is_qweather_production_ready(OPENMETEO_COMPLETE) is False


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('temperature', float('nan')),
        ('temperature', float('inf')),
        ('temperature', 75),
        ('humidity', -1),
        ('humidity', 101),
        ('temperature_max', 70),
        ('temperature_min', -100),
    ),
)
def test_capability_gates_reject_non_finite_and_out_of_range_values(field, value):
    from core.weather import is_heat_action_weather_ready, is_live_observational_weather

    payload = dict(OPENMETEO_COMPLETE)
    payload[field] = value

    assert is_heat_action_weather_ready(payload) is False
    if field == 'temperature':
        assert is_live_observational_weather(payload) is False


def test_capability_gates_reject_mock_demo_and_reversed_temperature_range():
    from core.weather import is_heat_action_weather_ready, is_live_observational_weather

    for flag in ('is_mock', 'is_demo'):
        payload = dict(OPENMETEO_COMPLETE)
        payload[flag] = True
        assert is_live_observational_weather(payload) is False
        assert is_heat_action_weather_ready(payload) is False

    reversed_range = dict(OPENMETEO_COMPLETE, temperature_max=20, temperature_min=30)
    assert is_heat_action_weather_ready(reversed_range) is False


def test_all_basic_heat_paths_forward_current_weather_to_hot_day_history_gate():
    """允许 Open-Meteo 的入口必须把来源交给连续高温历史门。"""
    import ast
    import inspect
    import textwrap

    from services import public_service
    from services.user import caregiver_service, community_service

    functions = (
        public_service._build_action_context,
        public_service.render_public_risk_page,
        caregiver_service._load_heat_risk,
        caregiver_service._build_pair_management_context,
        community_service._load_heat_risk,
    )
    for function in functions:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, 'id', None) == 'get_consecutive_hot_days'
        ]
        assert calls, f'{function.__name__} 应调用连续高温历史门'
        assert all(
            any(keyword.arg == 'weather_data' for keyword in call.keywords)
            for call in calls
        ), f'{function.__name__} 必须传入当前天气来源'


def test_air_quality_gate_rejects_missing_nonfinite_and_estimated_values():
    from core.weather import is_air_quality_available

    for field, value in (
        ('aqi', None),
        ('pm25', None),
        ('aqi', float('nan')),
        ('pm25', float('inf')),
        ('aqi', 501),
        ('pm25', -1),
    ):
        payload = dict(QWEATHER_COMPLETE)
        payload[field] = value
        assert is_air_quality_available(payload) is False

    estimated = dict(QWEATHER_COMPLETE, aqi_estimated=True)
    assert is_air_quality_available(estimated) is False


def test_openmeteo_current_weather_uses_none_for_air_quality(app, monkeypatch):
    from services import weather_service as weather_module

    with app.app_context():
        service = weather_module.WeatherService()
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response(_openmeteo_current_payload()),
        )

        result = service._get_openmeteo_weather('都昌')

    assert result['data_source'] == 'Open-Meteo'
    assert result['aqi'] is None
    assert result['pm25'] is None
    assert result['air_quality_available'] is False
    assert result['temperature'] == 33.4
    assert result['humidity'] == 66.0


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('temperature_2m', None),
        ('temperature_2m', float('nan')),
        ('relative_humidity_2m', float('inf')),
        ('relative_humidity_2m', 101),
        ('surface_pressure', 500),
        ('wind_speed_10m', -1),
        ('weather_code', None),
        ('weather_code', 4),
    ),
)
def test_openmeteo_current_weather_rejects_missing_invalid_fields(app, monkeypatch, field, value):
    from services import weather_service as weather_module

    payload = _openmeteo_current_payload()
    payload['current'][field] = value
    with app.app_context():
        service = weather_module.WeatherService()
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response(payload),
        )

        assert service._get_openmeteo_weather('都昌') is None


@pytest.mark.parametrize(
    'payload',
    (
        {**OPENMETEO_COMPLETE, 'is_mock': True, 'data_source': 'Mock'},
        OPENMETEO_COMPLETE,
        {key: value for key, value in QWEATHER_COMPLETE.items() if key != 'pressure'},
        {key: value for key, value in QWEATHER_COMPLETE.items() if key != 'data_source'},
    ),
    ids=('mock', 'openmeteo', 'incomplete-qweather', 'missing-provenance'),
)
def test_sync_weather_cache_never_upserts_untrusted_daily_weather(
    app,
    db_session,
    monkeypatch,
    payload,
):
    from core.db_models import WeatherCache, WeatherData
    from services.pipelines import sync_weather_cache as pipeline

    class FakeWeatherService:
        def get_current_weather(self, _location):
            return dict(payload)

    monkeypatch.setattr(pipeline, 'app', app)
    monkeypatch.setattr(pipeline, 'WeatherService', FakeWeatherService)

    result = pipeline.sync_weather_cache(locations=['都昌县'], update_daily=True)

    assert result['updated'] == 1
    assert result['daily_updated'] == 0
    assert result['daily_skipped'] == 1
    assert WeatherData.query.count() == 0
    cache = WeatherCache.query.filter_by(location='都昌县').one()
    assert json.loads(cache.payload).get('data_source') == payload.get('data_source')


def test_sync_weather_cache_keeps_complete_qweather_daily_path(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import WeatherData
    from services.pipelines import sync_weather_cache as pipeline

    class FakeWeatherService:
        def get_current_weather(self, _location):
            return dict(QWEATHER_COMPLETE)

    monkeypatch.setattr(pipeline, 'app', app)
    monkeypatch.setattr(pipeline, 'WeatherService', FakeWeatherService)

    result = pipeline.sync_weather_cache(locations=['都昌县'], update_daily=True)

    assert result['updated'] == 1
    assert result['daily_updated'] == 1
    assert result['daily_skipped'] == 0
    record = WeatherData.query.one()
    assert record.temperature_max == 37.0
    assert record.aqi == 42


def test_dashboard_openmeteo_only_displays_and_runs_basic_heat_action(
    authenticated_client,
    db_session,
    monkeypatch,
):
    from core.db_models import WeatherAlert, WeatherData
    from core.time_utils import today_local

    captured = {}
    heat_calls = []
    openmeteo_days = [
        {
            'date': (today_local() + timedelta(days=index)).isoformat(),
            'temperature_max': 34 + index,
            'temperature_min': 25 + index / 2,
            'condition': '晴',
            'precip_probability': 20 + index,
            'data_source': 'Open-Meteo',
            'is_mock': False,
        }
        for index in range(7)
    ]

    def fake_render(template_name, **context):
        captured['template'] = template_name
        captured.update(context)
        return 'dashboard-ok'

    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda _location: (dict(OPENMETEO_COMPLETE), False),
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda _location, days=7: ([], False, {'source': 'QWeather'}),
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_openmeteo_forecast_with_cache',
        lambda _location, days=7: (openmeteo_days[:days], False, {'source': 'Open-Meteo'}),
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_forecast_service',
        lambda: pytest.fail('首页 Open-Meteo weather-only 路径不得调用 ForecastService'),
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_consecutive_hot_days',
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.HeatActionService.calculate_heat_risk',
        lambda _self, weather_data, **_kwargs: heat_calls.append(weather_data) or {
            'risk_level': 'medium',
        },
    )
    monkeypatch.setattr('services.user.dashboard_service.render_template', fake_render)
    monkeypatch.setattr(
        'services.weather_service.WeatherService.identify_extreme_weather',
        lambda *_args, **_kwargs: pytest.fail('Open-Meteo 不得进入官方极端天气识别链'),
    )
    monkeypatch.setattr(
        'services.weather_service.WeatherService.generate_weather_alert',
        lambda *_args, **_kwargs: pytest.fail('Open-Meteo 不得生成官方 WeatherAlert'),
    )

    response = authenticated_client.get('/dashboard')

    assert response.status_code == 200
    assert heat_calls and heat_calls[0]['data_source'] == 'Open-Meteo'
    assert captured['template'] == 'user_dashboard.html'
    assert captured['weather_available'] is True
    assert captured['display_weather_available'] is True
    assert captured['heat_action_weather_ready'] is True
    assert captured['air_quality_available'] is False
    assert captured['qweather_production_ready'] is False
    assert captured['weather_source_label'] == 'Open-Meteo'
    assert len(captured['forecast_days']) == 7
    assert all(day['risk_available'] is False for day in captured['forecast_days'])
    assert WeatherData.query.count() == 0
    assert WeatherAlert.query.count() == 0
