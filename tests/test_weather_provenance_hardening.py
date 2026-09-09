# -*- coding: utf-8 -*-
"""天气观测时效、canonical 身份与生产门回归测试。"""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
import requests

from core.time_utils import today_local, utcnow


def _production_weather(**overrides):
    payload = {
        'temperature': 36.0,
        'temperature_max': 39.0,
        'temperature_min': 27.0,
        'humidity': 70.0,
        'pressure': 1002.0,
        'weather_condition': '晴',
        'wind_speed': 2.0,
        'pm25': 20.0,
        'aqi': 45.0,
        'air_quality_available': True,
        'data_source': 'QWeather',
        'observed_at': utcnow().isoformat(),
        'air_observed_at': utcnow().isoformat(),
        'quality_version': 1,
        'is_mock': False,
    }
    payload.update(overrides)
    return payload


def _qweather_days(days=7):
    start = today_local()
    return [
        {
            'date': (start + timedelta(days=index)).isoformat(),
            'forecast_date': (start + timedelta(days=index)).isoformat(),
            'temperature_max': 34.0 + index,
            'temperature_min': 25.0 + index / 2,
            'temperature_mean': 29.5 + index * 0.75,
            'condition': '多云',
            'condition_night': '多云',
            'humidity': 65.0,
            'wind_speed': 3.0,
            'precip_probability': 10.0,
            'data_source': 'QWeather',
            'is_mock': False,
        }
        for index in range(days)
    ]


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.text = 'test response'

    def json(self):
        return deepcopy(self.payload)


def test_observation_freshness_rejects_missing_invalid_stale_and_future(app):
    from core.weather import is_weather_observation_fresh

    now = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    with app.app_context():
        app.config['WEATHER_OBSERVATION_MAX_AGE_MINUTES'] = 120
        app.config['WEATHER_OBSERVATION_FUTURE_TOLERANCE_MINUTES'] = 15

        assert is_weather_observation_fresh({}, now=now) is False
        assert is_weather_observation_fresh({'observed_at': 'damaged'}, now=now) is False
        assert is_weather_observation_fresh({'observed_at': '2026-08-27'}, now=now) is False
        assert is_weather_observation_fresh(
            {'observed_at': (now - timedelta(minutes=121)).isoformat()},
            now=now,
        ) is False
        assert is_weather_observation_fresh(
            {'observed_at': (now + timedelta(minutes=16)).isoformat()},
            now=now,
        ) is False
        assert is_weather_observation_fresh(
            {'observed_at': (now - timedelta(minutes=120)).isoformat()},
            now=now,
        ) is True
        assert is_weather_observation_fresh(
            {'observed_at': (now + timedelta(minutes=15)).isoformat()},
            now=now,
        ) is True


def test_freshness_config_override_is_applied(app):
    from core.weather import is_weather_observation_fresh, normalize_weather_observed_at

    now = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    with app.app_context():
        app.config['WEATHER_OBSERVATION_MAX_AGE_MINUTES'] = 10
        assert is_weather_observation_fresh(
            {'observed_at': (now - timedelta(minutes=11)).isoformat()},
            now=now,
        ) is False
        assert normalize_weather_observed_at(
            datetime(2026, 8, 27, 8, 0)
        ) == '2026-08-27T08:00:00+00:00'
        assert normalize_weather_observed_at(
            '2026-08-27T16:00:00'
        ) == '2026-08-27T08:00:00+00:00'


def test_air_quality_freshness_uses_independent_observation_time(
    app,
    monkeypatch,
):
    from core import weather as weather_module

    now = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(weather_module, 'utcnow', lambda: now)
    with app.app_context():
        app.config['WEATHER_OBSERVATION_MAX_AGE_MINUTES'] = 240
        app.config['AIR_QUALITY_OBSERVATION_MAX_AGE_MINUTES'] = 120
        fresh = _production_weather(
            observed_at=(now - timedelta(minutes=180)).isoformat(),
            air_observed_at=(now - timedelta(minutes=120)).isoformat(),
        )
        assert weather_module.is_air_quality_available(fresh) is True

        missing = dict(fresh)
        missing.pop('air_observed_at')
        assert weather_module.is_air_quality_available(missing) is False

        stale = dict(fresh)
        stale['air_observed_at'] = (now - timedelta(minutes=121)).isoformat()
        assert weather_module.is_air_quality_available(stale) is False


def test_legacy_zero_aqi_without_explicit_availability_is_untrusted():
    from core.weather import is_air_quality_available

    legacy = _production_weather(aqi=0, pm25=0)
    legacy.pop('air_quality_available')
    assert is_air_quality_available(legacy) is False
    assert is_air_quality_available(
        _production_weather(aqi=0, pm25=0, air_quality_available=True)
    ) is True


@pytest.mark.parametrize('timestamp_field', ('pubTime', 'updateTime'))
def test_qweather_air_observation_time_is_parsed(
    app,
    monkeypatch,
    timestamp_field,
):
    from services import weather_service as weather_module

    observed_at = utcnow().replace(microsecond=0).isoformat()
    weather_payload = {
        'code': '200',
        'now': {
            'obsTime': observed_at,
            'temp': '30',
            'humidity': '65',
            'pressure': '1005',
            'text': '晴',
            'windSpeed': '2',
        },
    }
    air_payload = {
        'code': '200',
        'now': {
            'pm2p5': '18',
            'aqi': '42',
            'category': '优',
        },
    }
    if timestamp_field == 'pubTime':
        air_payload['now']['pubTime'] = observed_at
    else:
        air_payload['updateTime'] = observed_at
    responses = iter([_Response(weather_payload), _Response(air_payload)])

    with app.app_context():
        service = weather_module.WeatherService()
        service.qweather_key = 'test-key'
        service.api_base_url = 'https://qweather.invalid'
        monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)
        monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
        monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: next(responses))
        monkeypatch.setattr(
            service,
            '_resolve_qweather_current_temperature_range',
            lambda _location: (34.0, 24.0, 'daily', 'high'),
        )

        result = service.get_current_weather('都昌')

    assert result['air_quality_available'] is True
    assert result['air_observed_at'] == observed_at
    assert result['pm25'] == 18.0
    assert result['aqi'] == 42


@pytest.mark.parametrize(
    'air_payload',
    [
        {'code': '200', 'now': {'pm2p5': '18', 'aqi': '42'}},
        {
            'code': '200',
            'updateTime': 'damaged',
            'now': {'pm2p5': '18', 'aqi': '42', 'pubTime': 'damaged'},
        },
    ],
    ids=['missing-time', 'invalid-time'],
)
def test_qweather_air_without_valid_observation_time_is_unavailable(
    app,
    monkeypatch,
    air_payload,
):
    from services import weather_service as weather_module

    observed_at = utcnow().replace(microsecond=0).isoformat()
    responses = iter([
        _Response({
            'code': '200',
            'now': {
                'obsTime': observed_at,
                'temp': '30',
                'humidity': '65',
                'pressure': '1005',
                'text': '晴',
                'windSpeed': '2',
            },
        }),
        _Response(air_payload),
    ])

    with app.app_context():
        service = weather_module.WeatherService()
        service.qweather_key = 'test-key'
        service.api_base_url = 'https://qweather.invalid'
        monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)
        monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
        monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: next(responses))
        monkeypatch.setattr(
            service,
            '_resolve_qweather_current_temperature_range',
            lambda _location: (34.0, 24.0, 'daily', 'high'),
        )

        result = service.get_current_weather('都昌')

    assert result['air_quality_available'] is False
    assert result['air_observed_at'] is None
    assert result['pm25'] is None
    assert result['aqi'] is None


def test_stale_current_cache_is_rejected_and_refetched_canonically(
    app,
    db_session,
):
    from core.db_models import WeatherCache
    from core.weather import get_weather_with_cache, register_weather_fetcher

    stale_payload = _production_weather()
    stale_payload.pop('observed_at')
    db_session.add(WeatherCache(
        location='都昌县',
        fetched_at=utcnow(),
        payload=json.dumps(stale_payload, ensure_ascii=False),
        is_mock=False,
    ))
    db_session.commit()
    calls = []

    class _Fetcher:
        def get_current_weather(self, location):
            calls.append(location)
            return _production_weather(location='北京')

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'
        app.extensions['redis_client'] = None
        register_weather_fetcher(_Fetcher())
        weather, from_cache = get_weather_with_cache('北京')

    assert from_cache is False
    assert calls == ['都昌县']
    assert weather['location'] == '都昌县'
    assert weather['weather_location'] == '都昌县'
    assert weather['audience_location'] == '北京'
    stored = json.loads(WeatherCache.query.filter_by(location='都昌县').one().payload)
    assert stored['location'] == '都昌县'
    assert 'audience_location' not in stored


def test_sync_aliases_dedupe_to_one_canonical_location(app, monkeypatch):
    from services.pipelines import sync_weather_cache as pipeline

    monkeypatch.setattr(pipeline, 'app', app)
    with app.app_context():
        app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'
        assert list(pipeline._dedupe_locations([
            '都昌',
            '都昌县',
            '牛家垄周村',
            '北京',
            '116.20,29.27',
        ])) == ['都昌县']


def test_hot_day_history_uses_only_finite_canonical_quality_v1_rows(app, db_session):
    from core.db_models import WeatherData
    from core.weather import get_consecutive_hot_days

    target = today_local()
    db_session.add_all([
        WeatherData(
            date=target - timedelta(days=1),
            location='都昌县',
            temperature_max=38,
            data_source='QWeather',
            quality_version=1,
        ),
        WeatherData(
            date=target - timedelta(days=2),
            location='都昌县',
            temperature_max=float('inf'),
            data_source='QWeather',
            quality_version=1,
        ),
        WeatherData(
            date=target - timedelta(days=2),
            location='北京',
            temperature_max=39,
            data_source='QWeather',
            quality_version=1,
        ),
        WeatherData(
            date=target - timedelta(days=3),
            location='都昌县',
            temperature_max=39,
            data_source='QWeather',
            quality_version=0,
        ),
    ])
    db_session.commit()

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'
        assert get_consecutive_hot_days(
            '北京',
            target_date=target,
            today_max=39,
            weather_data=_production_weather(),
        ) == 2
        assert get_consecutive_hot_days(
            '都昌',
            target_date=target,
            today_max=39,
            weather_data=_production_weather(
                data_source='Open-Meteo',
                air_quality_available=False,
            ),
        ) == 1


def test_qweather_hourly_extremes_only_use_local_today(app, monkeypatch):
    from services import weather_service as weather_module

    today = today_local()
    tomorrow = today + timedelta(days=1)
    payload = {
        'code': '200',
        'hourly': [
            {'fxTime': f'{today.isoformat()}T0{hour}:00+08:00', 'temp': 28 + hour}
            for hour in range(3)
        ] + [
            {'fxTime': f'{tomorrow.isoformat()}T{hour:02d}:00+08:00', 'temp': 45}
            for hour in range(20)
        ],
    }
    with app.app_context():
        service = weather_module.WeatherService()
        service.qweather_key = 'test-key'
        service.api_base_url = 'https://qweather.invalid'
        monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)
        monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
        monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: _Response(payload))

        assert service._get_qweather_hourly_extremes('116.20,29.27') == (None, None, 'none')
        payload['hourly'].append({
            'fxTime': f'{today.isoformat()}T03:00+08:00',
            'temp': 31,
        })
        assert service._get_qweather_hourly_extremes('116.20,29.27') == (31.0, 28.0, 'low')


def test_openmeteo_hourly_extremes_only_use_local_today(app, monkeypatch):
    from services import weather_service as weather_module

    today = today_local()
    tomorrow = today + timedelta(days=1)
    payload = {
        'hourly': {
            'time': [
                f'{today.isoformat()}T0{hour}:00'
                for hour in range(3)
            ] + [
                f'{tomorrow.isoformat()}T{hour:02d}:00'
                for hour in range(20)
            ],
            'temperature_2m': [28, 29, 30] + [45] * 20,
        },
    }
    with app.app_context():
        service = weather_module.WeatherService()
        monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
        monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: _Response(payload))

        assert service._get_openmeteo_hourly_extremes(116.20, 29.27) == (None, None, 'none')
        payload['hourly']['time'].append(f'{today.isoformat()}T03:00')
        payload['hourly']['temperature_2m'].append(31)
        assert service._get_openmeteo_hourly_extremes(116.20, 29.27) == (31.0, 28.0, 'low')


def test_qweather_daily_zero_must_be_local_today(app, monkeypatch):
    from services import weather_service as weather_module

    tomorrow = today_local() + timedelta(days=1)
    with app.app_context():
        service = weather_module.WeatherService()
        service.qweather_key = 'test-key'
        service.api_base_url = 'https://qweather.invalid'
        monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)
        monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response({
                'code': '200',
                'daily': [{
                    'fxDate': tomorrow.isoformat(),
                    'tempMax': '39',
                    'tempMin': '27',
                }],
            }),
        )

        assert service._get_qweather_today_extremes('116.20,29.27') == (None, None)


def test_current_providers_require_upstream_observation_time(app, monkeypatch):
    from services import weather_service as weather_module

    with app.app_context():
        service = weather_module.WeatherService()
        service.qweather_key = 'test-key'
        service.api_base_url = 'https://qweather.invalid'
        monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)
        monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response({
                'code': '200',
                'now': {'temp': '30', 'humidity': '65'},
            }),
        )
        monkeypatch.setattr(
            service,
            '_get_fallback_weather',
            lambda *_args, **_kwargs: {'data_source': 'fallback'},
        )
        assert service.get_current_weather('都昌') == {'data_source': 'fallback'}

        openmeteo = {
            'current': {
                'temperature_2m': 30,
                'relative_humidity_2m': 65,
                'surface_pressure': 1005,
                'weather_code': 1,
                'wind_speed_10m': 2,
            },
            'daily': {
                'time': [today_local().isoformat()],
                'temperature_2m_max': [34],
                'temperature_2m_min': [24],
            },
        }
        monkeypatch.setattr(
            weather_module.requests,
            'get',
            lambda *_args, **_kwargs: _Response(openmeteo),
        )
        assert service._get_openmeteo_weather('都昌') is None


def test_wmo_code_rejects_fractional_boolean_and_nonfinite(app):
    from services.weather_service import WeatherService

    with app.app_context():
        service = WeatherService()
        assert service._parse_wmo_code(1) == 1
        assert service._parse_wmo_code(1.5) is None
        assert service._parse_wmo_code(True) is None
        assert service._parse_wmo_code(float('nan')) is None


@pytest.mark.parametrize('failure_kind', ('budget', 'http_403', 'timeout'))
def test_qweather_forecast_failures_use_negative_cache(
    app,
    db_session,
    failure_kind,
):
    from core import weather as weather_module

    calls = []

    class _Fetcher:
        def get_qweather_daily_forecast(self, _location, days=7):
            calls.append(days)
            if failure_kind == 'timeout':
                raise requests.Timeout('test timeout')
            error = (
                'qweather_budget_exhausted'
                if failure_kind == 'budget'
                else 'http_403'
            )
            return {
                'success': False,
                'daily': [],
                'meta': {'source': None, 'error': error},
            }

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'
        app.config['QWEATHER_FORECAST_NEGATIVE_CACHE_SECONDS'] = 120
        app.extensions['redis_client'] = None
        weather_module._QWEATHER_FORECAST_NEGATIVE_CACHE.clear()
        weather_module.register_weather_fetcher(_Fetcher())

        first, first_cached, first_meta = weather_module.get_qweather_forecast_with_cache(
            '牛家垄周村',
            days=7,
        )
        second, second_cached, second_meta = weather_module.get_qweather_forecast_with_cache(
            '北京',
            days=7,
        )

    assert first == second == []
    assert first_cached is False
    assert second_cached is True
    assert calls == [7]
    assert first_meta['source'] is None
    assert second_meta['source'] is None
    assert first_meta['negative_cache'] is True
    assert second_meta['negative_cache'] is True
    assert first_meta['retry_after_seconds'] == 120


def test_qweather_forecast_damaged_meta_is_negative_cached(app, db_session):
    from core import weather as weather_module

    calls = []

    class _Fetcher:
        def get_qweather_daily_forecast(self, _location, days=7):
            calls.append(days)
            return {
                'success': True,
                'daily': _qweather_days(days),
                'meta': ['damaged'],
            }

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'
        app.extensions['redis_client'] = None
        weather_module._QWEATHER_FORECAST_NEGATIVE_CACHE.clear()
        weather_module.register_weather_fetcher(_Fetcher())
        first = weather_module.get_qweather_forecast_with_cache('都昌', days=7)
        second = weather_module.get_qweather_forecast_with_cache('都昌县', days=7)

    assert first[0] == second[0] == []
    assert first[2]['error'] == 'invalid_meta'
    assert first[2]['source'] is None
    assert second[1] is True
    assert calls == [7]


def test_forecast_cards_reject_nan_score_and_tolerate_damaged_nested_meta():
    from services.forecast_cards import build_forecast_cards

    days = _qweather_days(1)
    health = [{
        'date': days[0]['date'],
        'composite_exposure': {
            'final_score': float('nan'),
            'components': ['damaged'],
            'inputs': 'damaged',
        },
        'visits': 'damaged',
        'predictability': ['damaged'],
    }]

    cards = build_forecast_cards(days, health, today_local())

    assert len(cards) == 1
    assert cards[0]['risk_available'] is False
    assert cards[0]['risk_score'] is None
    assert cards[0]['risk_label'] == '待计算'


def test_dashboard_passes_latest_three_family_members_for_current_user_only(
    authenticated_client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember, User

    owner = User.query.filter_by(username='testuser').one()
    outsider = User(username='dashboard-family-outsider', role='user')
    outsider.set_password('testpass')
    db_session.add(outsider)
    db_session.flush()
    base_time = utcnow()
    db_session.add_all([
        FamilyMember(
            user_id=owner.id,
            name=f'家人{index}',
            created_at=base_time + timedelta(seconds=index),
        )
        for index in range(4)
    ] + [
        FamilyMember(
            user_id=outsider.id,
            name='外部账号家人',
            created_at=base_time + timedelta(seconds=10),
        ),
    ])
    db_session.commit()
    captured = {}

    def _capture_template(template_name, **context):
        captured['template'] = template_name
        captured.update(context)
        return 'dashboard-ok'

    monkeypatch.setattr(
        'services.user.dashboard_service.render_template',
        _capture_template,
    )

    response = authenticated_client.get('/dashboard')

    assert response.status_code == 200
    assert captured['template'] == 'user_dashboard.html'
    assert [member.name for member in captured['family_members']] == [
        '家人3',
        '家人2',
        '家人1',
    ]
