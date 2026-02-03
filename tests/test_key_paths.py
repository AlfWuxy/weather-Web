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

        data_1, from_cache_1 = get_weather_with_cache('北京')
        data_2, from_cache_2 = get_weather_with_cache('北京')

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
        redis_key = 'weather:forecast:北京:3'
        fake_redis.setex(redis_key, 600, json.dumps(cached_forecast, ensure_ascii=False))

        fetcher = DummyWeatherFetcher()
        register_weather_fetcher(fetcher)

        data, from_cache = get_forecast_with_cache('北京', days=3)

        assert from_cache is True
        assert data == cached_forecast
        assert fetcher.forecast_calls == 0


def test_short_code_action_resolve_pair(app, db_session):
    from services import public_service
    from core.db_models import PairLink, User
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

        refreshed = PairLink.query.get(link.id)
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
