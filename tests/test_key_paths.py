# -*- coding: utf-8 -*-
import json


def _dummy_weather_payload():
    return {
        'temperature': 30,
        'temperature_max': 33,
        'temperature_min': 25,
        'humidity': 60,
        'pressure': 1008,
        'weather_condition': '晴',
        'wind_speed': 1.8,
        'pm25': 20,
        'aqi': 40,
        'is_mock': True,
        'data_source': 'Mock'
    }


class DummyWeatherFetcher:
    def __init__(self):
        self.calls = 0
        self.forecast_calls = 0
        self.qweather_forecast_calls = 0

    def get_current_weather(self, location):
        self.calls += 1
        return _dummy_weather_payload()

    def get_weather_forecast(self, location, days=7):
        self.forecast_calls += 1
        return [
            {
                'forecast_date': f'day-{idx + 1}',
                'temperature_max': 30,
                'temperature_min': 20,
                'is_mock': True
            }
            for idx in range(days)
        ]

    def get_qweather_daily_forecast(self, location, days=7):
        from datetime import timedelta

        from core.time_utils import today_local

        self.qweather_forecast_calls += 1
        start = today_local()
        return {
            'success': True,
            'daily': [
                {
                    'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
                    'temperature_max': 26,
                    'temperature_min': 18,
                    'temperature_mean': 22,
                    'humidity': 70,
                    'condition': '阴',
                    'data_source': 'QWeather',
                    'is_mock': False,
                }
                for idx in range(days)
            ],
            'meta': {
                'source': 'QWeather',
                'update_time': '2026-04-26T19:43+08:00',
            },
        }

    def get_short_term_nowcast(self, city="北京", hours=6):
        return {
            'available': True,
            'source': 'test',
            'timeline': [
                {
                    'time': '2026-02-13T10:00',
                    'precipitation_probability': 20.0,
                    'precipitation_mm': 0.0,
                    'temperature': 20.0,
                    'condition': '多云',
                    'risk_level': '低'
                }
            ][:hours]
        }


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl_seconds, value):
        self.store[key] = value


def test_weather_cache_db_roundtrip(app, db_session):
    from core.weather import get_weather_with_cache, register_weather_fetcher

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None

        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data_1, from_cache_1 = get_weather_with_cache('北京', cache_only=False)
        # 该桩是 mock，仅诊断模式允许复用；普通只读链路只接受真实和风缓存。
        data_2, from_cache_2 = get_weather_with_cache('北京', cache_only=False)

        assert data_1
        assert data_2
        assert fetcher.calls == 1
        assert from_cache_1 is False
        assert from_cache_2 is True


def test_forecast_cache_prefers_redis(app, db_session):
    from core.weather import get_forecast_with_cache, register_weather_fetcher

    with app.app_context():
        app.config['DEMO_MODE'] = False
        fake_redis = FakeRedis()
        app.extensions['redis_client'] = fake_redis

        cached_forecast = [{
            'forecast_date': 'cached',
            'temperature_max': 28,
            'temperature_min': 18,
            'is_mock': True
        }]
        # 产品只使用都昌县天气；旧城市参数也必须命中同一份县级缓存。
        redis_key = 'weather:forecast:都昌县:3'
        fake_redis.setex(redis_key, 600, json.dumps(cached_forecast, ensure_ascii=False))

        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data, from_cache = get_forecast_with_cache('北京', days=3)

        assert from_cache is True
        assert data == cached_forecast
        assert fetcher.forecast_calls == 0


def test_generic_stale_mock_forecast_is_rejected_in_read_only_mode(app, db_session):
    from datetime import timedelta

    from core.db_models import ForecastCache
    from core.extensions import db
    from core.time_utils import utcnow
    from core.weather import get_forecast_with_cache, register_weather_fetcher

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        db.session.add(ForecastCache(
            location='都昌县',
            days=3,
            fetched_at=utcnow() - timedelta(minutes=30),
            payload=json.dumps([{'date': 'mock-day', 'is_mock': True}], ensure_ascii=False),
            is_mock=True,
        ))
        db.session.commit()
        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data, from_cache = get_forecast_with_cache('都昌', days=3, ttl_minutes=10)

        assert data == []
        assert from_cache is False
        assert fetcher.forecast_calls == 0


def test_weather_helpers_are_read_only_by_default(app, db_session):
    from core.weather import (
        get_forecast_with_cache,
        get_qweather_forecast_with_cache,
        get_weather_with_cache,
        register_weather_fetcher,
    )

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        current, current_from_cache = get_weather_with_cache('都昌')
        forecast, forecast_from_cache = get_forecast_with_cache('都昌', days=7)
        qweather, qweather_from_cache, meta = get_qweather_forecast_with_cache('都昌', days=7)

        assert current == {}
        assert current_from_cache is False
        assert forecast == []
        assert forecast_from_cache is False
        assert qweather == []
        assert qweather_from_cache is False
        assert meta['error'] == 'cache_miss'
        assert fetcher.calls == 0
        assert fetcher.forecast_calls == 0
        assert fetcher.qweather_forecast_calls == 0


def test_qweather_only_forecast_ignores_legacy_mock_cache(app, db_session):
    from core.db_models import ForecastCache
    from core.extensions import db
    from core.time_utils import utcnow
    from core.weather import get_qweather_forecast_with_cache, register_weather_fetcher

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        db.session.add(ForecastCache(
            location='都昌',
            days=7,
            fetched_at=utcnow(),
            payload=json.dumps([{
                'date': 'legacy',
                'temperature_max': 34,
                'temperature_min': 26,
                'is_mock': True,
                'data_source': 'Mock',
            }], ensure_ascii=False),
            is_mock=True,
        ))
        db.session.commit()

        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data, from_cache, meta = get_qweather_forecast_with_cache(
            '都昌',
            days=7,
            cache_only=False,
        )

        assert from_cache is False
        assert fetcher.qweather_forecast_calls == 1
        assert data[0]['data_source'] == 'QWeather'
        assert meta['source'] == 'QWeather'

        cached, cached_from_cache, cached_meta = get_qweather_forecast_with_cache('都昌', days=7)
        assert cached == data
        assert cached_from_cache is True
        assert cached_meta['source'] == 'QWeather'
        assert fetcher.qweather_forecast_calls == 1


def test_qweather_only_forecast_ignores_stale_date_cache(app, db_session):
    from datetime import timedelta

    from core.db_models import ForecastCache
    from core.extensions import db
    from core.time_utils import today_local, utcnow
    from core.weather import get_qweather_forecast_with_cache, register_weather_fetcher

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        stale_start = today_local() - timedelta(days=1)
        stale_daily = [
            {
                'date': (stale_start + timedelta(days=idx)).strftime('%Y-%m-%d'),
                'temperature_max': 31,
                'temperature_min': 24,
                'temperature_mean': 27.5,
                'humidity': 70,
                'data_source': 'QWeather',
                'is_mock': False,
            }
            for idx in range(7)
        ]
        db.session.add(ForecastCache(
            location='qweather-only:都昌县',
            days=7,
            fetched_at=utcnow(),
            payload=json.dumps({'daily': stale_daily, 'meta': {'source': 'QWeather'}}, ensure_ascii=False),
            is_mock=False,
        ))
        db.session.commit()

        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data, from_cache, meta = get_qweather_forecast_with_cache(
            '都昌',
            days=7,
            cache_only=False,
        )

        assert from_cache is False
        assert fetcher.qweather_forecast_calls == 1
        assert data[0]['date'] == today_local().strftime('%Y-%m-%d')
        assert meta['source'] == 'QWeather'


def test_qweather_only_forecast_rejects_mock_flagged_record(app, db_session):
    from datetime import timedelta

    from core.db_models import ForecastCache
    from core.extensions import db
    from core.time_utils import today_local, utcnow
    from core.weather import get_qweather_forecast_with_cache, register_weather_fetcher

    start = today_local()
    daily = [
        {
            'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
            'temperature_max': 30,
            'temperature_min': 22,
            'temperature_mean': 26,
            'humidity': 70,
            'data_source': 'QWeather',
            'is_mock': False,
        }
        for idx in range(7)
    ]
    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        db.session.add(ForecastCache(
            location='qweather-only:都昌县',
            days=7,
            fetched_at=utcnow(),
            payload=json.dumps({'daily': daily, 'meta': {'source': 'QWeather'}}, ensure_ascii=False),
            is_mock=True,
        ))
        db.session.commit()
        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data, from_cache, meta = get_qweather_forecast_with_cache('都昌', days=7)

        assert data == []
        assert from_cache is False
        assert meta['error'] == 'cache_miss'
        assert fetcher.qweather_forecast_calls == 0


def test_qweather_only_forecast_rejects_partial_fetch_result(app, db_session):
    from core.db_models import ForecastCache
    from core.weather import get_qweather_forecast_with_cache

    class PartialForecastFetcher(DummyWeatherFetcher):
        def get_qweather_daily_forecast(self, location, days=7):
            result = super().get_qweather_daily_forecast(location, days=days)
            result['daily'] = result['daily'][:3]
            return result

    with app.app_context():
        app.config['DEMO_MODE'] = False
        app.extensions['redis_client'] = None
        fetcher = PartialForecastFetcher()

        data, from_cache, meta = get_qweather_forecast_with_cache(
            '都昌',
            days=7,
            cache_only=False,
            fetcher=fetcher,
        )

        assert data == []
        assert from_cache is False
        assert meta['error'] == 'qweather_data_incomplete'
        assert fetcher.qweather_forecast_calls == 1
        assert ForecastCache.query.filter_by(
            location='qweather-only:都昌县',
            days=7,
        ).count() == 0


def test_cycle_forecast_persists_and_reuses_forecast_cache(app, db_session, monkeypatch):
    from core.db_models import ForecastCache
    from services.miniprogram_service import refresh_snapshot_from_cycle

    with app.app_context():
        app.config.update(
            DEMO_MODE=False,
            FORECAST_CACHE_TTL_MINUTES=20,
            QWEATHER_AUTH_MODE='api_key',
            QWEATHER_KEY='test-only-key',
            QWEATHER_API_BASE='https://qweather.invalid/v7',
        )
        app.extensions['redis_client'] = None
        monkeypatch.setattr(
            'services.warning_service.get_qweather_warnings_result',
            lambda _location: {'available': True, 'status': 'ok', 'warnings': []},
        )
        fetcher = DummyWeatherFetcher()
        current = dict(_dummy_weather_payload(), is_mock=False, data_source='QWeather')

        first = refresh_snapshot_from_cycle(current, weather_service=fetcher)
        second = refresh_snapshot_from_cycle(current, weather_service=fetcher)

        record = ForecastCache.query.filter_by(location='qweather-only:都昌县', days=7).one()
        first_forecast = json.loads(first.forecast_json)
        second_forecast = json.loads(second.forecast_json)
        assert first_forecast == second_forecast
        assert fetcher.qweather_forecast_calls == 1
        assert record.is_mock is False


def test_short_code_action_resolve_pair(app, db_session):
    from services import public_service
    from core.db_models import PairLink, User
    from core.extensions import db
    from core.security import hash_pair_token, hash_short_code

    with app.app_context():
        app.config['PAIR_TOKEN_PEPPER'] = 'test-pepper'

        caregiver = User(username='caregiver')
        caregiver.set_password('password123')
        db_session.add(caregiver)
        db_session.commit()

        short_code = '12345678'
        token = 'token-123'
        link = PairLink(
            caregiver_id=caregiver.id,
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            token_hash=hash_pair_token(token),
            community_code='test',
            status='active'
        )
        db_session.add(link)
        db_session.commit()

        with app.test_request_context('/'):
            pair, error = public_service._resolve_pair(short_code, token)
        assert error is None
        assert pair is not None
        assert pair.short_code == short_code

        refreshed = db.session.get(PairLink, link.id)
        assert refreshed.status == 'redeemed'
        assert refreshed.redeemed_at is not None


def test_api_current_weather_structure(client):
    response = client.get('/api/weather/current')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    data = payload.get('data') or {}
    for key in ('temperature', 'humidity', 'data_source', 'from_cache'):
        assert key in data


def test_api_nowcast_structure(client):
    response = client.get('/api/weather/nowcast')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    data = payload.get('data') or {}
    assert 'available' in data
    assert 'timeline' in data


def test_api_forecast_structure(authenticated_client):
    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'csrf-token'

    response = authenticated_client.post(
        '/api/forecast/7day',
        json={'forecast_temps': [15, 16, 17, 18, 19, 20, 21]},
        headers={'X-CSRF-Token': 'csrf-token'}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert 'forecasts' in payload
    assert 'summary' in payload
    forecasts = payload.get('forecasts') or []
    assert len(forecasts) >= 1
    first = forecasts[0]
    assert 'composite_exposure' in first
    assert 'cap_semantics' in first
    assert 'scenarios' in first
    assert 'p10' in (first.get('visits') or {})
    assert 'p50' in (first.get('visits') or {})
    assert 'p90' in (first.get('visits') or {})

    summary = payload.get('summary') or {}
    assert 'role_action_cards' in summary
    assert 'scenario_totals' in summary
    assert 'probability_products' in summary
