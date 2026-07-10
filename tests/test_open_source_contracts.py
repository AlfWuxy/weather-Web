import json
from pathlib import Path
from unittest.mock import MagicMock


def _response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def test_openmeteo_current_weather_contract_marks_source_and_uncertainty(app, monkeypatch):
    with app.app_context():
        from services.weather_service import WeatherService
        import services.weather_service as weather_module

        service = WeatherService()
        monkeypatch.setattr(service, '_get_location', lambda city: '116.20,29.27')
        monkeypatch.setattr(service, '_parse_lon_lat', lambda location: (116.20, 29.27))

        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append((url, params or {}, timeout))
            return _response({
                'current': {
                    'temperature_2m': 33.4,
                    'relative_humidity_2m': 66,
                    'surface_pressure': 1008,
                    'weather_code': 1,
                    'wind_speed_10m': 4.2,
                },
                'daily': {
                    'temperature_2m_max': [37.1],
                    'temperature_2m_min': [26.2],
                },
            })

        monkeypatch.setattr(weather_module.requests, 'get', fake_get)

        result = service._get_openmeteo_weather('都昌')

    assert result['data_source'] == 'Open-Meteo'
    assert result['is_mock'] is False
    assert result['aqi'] == 0
    assert result['pm25'] == 0
    assert result['aqi_estimated'] is True
    assert result['temperature_range_source'] == 'daily'
    assert result['temperature_range_confidence'] == 'high'
    assert calls[0][1]['timezone'] == 'Asia/Shanghai'
    assert 'temperature_2m,relative_humidity_2m' in calls[0][1]['current']


def test_openmeteo_daily_forecast_contract_uses_no_sdk_or_live_network(app, monkeypatch):
    with app.app_context():
        from services.weather_service import WeatherService
        import services.weather_service as weather_module

        service = WeatherService()
        monkeypatch.setattr(service, '_get_location', lambda city: '116.20,29.27')
        monkeypatch.setattr(service, '_parse_lon_lat', lambda location: (116.20, 29.27))

        def fake_get(url, params=None, timeout=None):
            assert url == 'https://api.open-meteo.com/v1/forecast'
            assert params['timezone'] == 'Asia/Shanghai'
            assert 'temperature_2m_max' in params['daily']
            return _response({
                'daily': {
                    'time': ['2026-06-01', '2026-06-02'],
                    'temperature_2m_max': [36.0, 35.0],
                    'temperature_2m_min': [26.0, 25.0],
                    'precipitation_probability_max': [30, 45],
                    'weather_code': [1, 61],
                },
            })

        monkeypatch.setattr(weather_module.requests, 'get', fake_get)

        result = service._get_openmeteo_forecast('都昌', days=2)

    assert [row['data_source'] for row in result] == ['Open-Meteo', 'Open-Meteo']
    assert result[0]['date'] == '2026-06-01'
    assert result[0]['temperature_mean'] == 31.0
    assert result[0]['is_mock'] is False
    assert result[1]['condition'] == '小雨'


def test_multimodel_forecast_contract_preserves_provider_names(app):
    with app.app_context():
        from services.weather_service import WeatherService

        service = WeatherService()
        merged = service._merge_multimodel_forecast(
            [{
                'date': '2026-06-01',
                'temperature_max': 37,
                'temperature_min': 27,
                'temperature_mean': 32,
                'condition': '晴',
                'data_source': 'QWeather',
            }],
            [{
                'date': '2026-06-01',
                'temperature_max': 35,
                'temperature_min': 25,
                'temperature_mean': 30,
                'condition': '多云',
                'data_source': 'Open-Meteo',
            }],
            days=1,
        )

    assert len(merged) == 1
    row = merged[0]
    assert row['model_count'] == 2
    assert row['model_names'] == ['QWeather', 'Open-Meteo']
    assert row['data_source'] == 'QWeather+Open-Meteo'
    assert row['temperature_ensemble_p10'] <= row['temperature_ensemble_p50'] <= row['temperature_ensemble_p90']


def test_qweather_production_guard_rejects_non_finite_temperature():
    from core.weather import is_qweather_online_weather

    assert is_qweather_online_weather({
        'temperature': 31,
        'data_source': 'QWeather',
        'is_mock': False,
    }) is True
    assert is_qweather_online_weather({
        'temperature': 'NaN',
        'data_source': 'QWeather',
        'is_mock': False,
    }) is False
    assert is_qweather_online_weather({
        'temperature': float('inf'),
        'data_source': 'QWeather',
        'is_mock': False,
    }) is False
    assert is_qweather_online_weather({
        'temperature': 31,
        'is_mock': False,
    }) is False


def test_dlnm_profile_contract_is_frozen_json_artifact():
    profile_path = Path('data/models/final_single_model_ar1_profile.json')
    assert profile_path.exists()

    profile = json.loads(profile_path.read_text(encoding='utf-8'))

    assert isinstance(profile.get('curve'), list)
    assert len(profile['curve']) >= 10
    assert {'temp', 'rr'}.issubset(profile['curve'][0])
    assert isinstance(profile.get('mmt'), (int, float))
    assert isinstance(profile.get('max_lag'), int)
    assert isinstance(profile.get('max_lag_cold'), int)
    assert profile.get('source')


def test_transparency_page_names_openmeteo_attribution(client):
    response = client.get('/transparency')

    assert response.status_code == 200
    assert 'Open-Meteo' in response.get_data(as_text=True)
