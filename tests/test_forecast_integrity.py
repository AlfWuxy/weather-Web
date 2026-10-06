"""验证缺数据不能变成高置信度或虚构的空气质量。"""
import pytest

from services.forecast_service import ForecastService
from services.weather_service import WeatherService
from services.missing_policy import input_state, risk_floor, weighted_known_risk


@pytest.fixture
def forecast():
    return ForecastService.__new__(ForecastService)


@pytest.mark.parametrize('spread,count', [(None, 1), (None, 5), (0, 1), (float('nan'), 4), (-1, 4)])
def test_missing_multimodel_information_caps_local_score(forecast, spread, count):
    result = forecast._calculate_predictability(1, spread, count)
    assert result['score'] == 60
    assert result['branch'] == 'lead_only'
    assert result['inputs']['model_spread'] is None
    assert '无多模型信息' in result['note']
    assert forecast._calculate_predictability(7, spread, count)['score'] == 42


def test_model_count_does_not_reward_score(forecast):
    assert forecast._calculate_predictability(2, 1.5, 2)['score'] == forecast._calculate_predictability(2, 1.5, 20)['score']


@pytest.mark.parametrize('aqi', [None, 0, 100, 500, float('nan')])
def test_aqi_without_verified_standard_is_not_pm25(forecast, aqi):
    result = forecast._composite_exposure_risk(38, 27, 85, aqi=aqi)
    assert result['pm25_proxy'] is None
    assert result['components']['pm25'] is None
    assert result['effective_weights']['pm25'] == 0
    assert sum(result['effective_weights'].values()) == pytest.approx(1)
    assert result['status'] == 'partial'


@pytest.mark.parametrize('standard,pollutant,hours', [
    ('cn-mee', 'pm2p5', 24), ('HJ633-2026', 'pm2p5', 24),
    ('HJ633-2012', 'o3', 24), ('HJ633-2012', 'pm2p5', 1),
])
def test_incompatible_aqi_definition_stays_unknown(forecast, standard, pollutant, hours):
    result = forecast._composite_exposure_risk(35, 24, 80, aqi=100,
        aqi_standard=standard, primary_pollutant=pollutant, aqi_averaging_hours=hours)
    assert result['pm25_proxy'] is None


def test_explicit_historical_iaqi_inverse_and_no_risk_dilution(forecast):
    kwargs = dict(aqi_standard='HJ633-2012', primary_pollutant='pm2p5', aqi_averaging_hours=24)
    result = forecast._composite_exposure_risk(38, 27, 85, aqi=100, **kwargs)
    assert result['pm25_proxy'] == 75
    assert result['inputs']['pm25']['status'] == 'imputed'
    assert result['inputs']['pm25']['method']
    baseline = forecast._composite_exposure_risk(38, 27, 85)['score']
    low_pm = forecast._composite_exposure_risk(38, 27, 85, aqi=50, **kwargs)
    assert low_pm['score'] >= baseline
    direct = forecast._composite_exposure_risk(38, 27, 85, pm25=120, aqi=50, **kwargs)
    assert direct['pm25_proxy'] == 120
    assert direct['inputs']['pm25']['status'] == 'observed'


def test_unknown_weather_is_not_low_risk(forecast):
    result = forecast._composite_exposure_risk(None, None, None)
    assert result['score'] is None
    assert result['level'] == '未知'
    assert result['status'] == 'unknown'
    with pytest.raises(ValueError, match='temperature'):
        forecast._normalize_forecast_entry({'temperature': None})


def test_missing_policy_preserves_zero_and_requires_imputation_method():
    assert input_state(0)['status'] == 'observed'
    assert input_state(float('nan'))['value'] is None
    with pytest.raises(ValueError, match='method'):
        input_state(0, status='imputed')
    assert input_state(0, method='median')['status'] == 'imputed'
    assert risk_floor(10, 50, status='imputed') == 50
    assert risk_floor(10, None, status='imputed') is None
    assert weighted_known_risk({'heat': None}, {'heat': 1})['score'] is None


def test_upstream_single_model_spread_is_unknown():
    service = WeatherService()
    merged = service._merge_multimodel_forecast([
        {'date': '2026-10-05', 'temperature_mean': 30, 'temperature_max': 34, 'temperature_min': 26}
    ], [])
    assert merged[0]['temperature_ensemble_std'] is None
    assert merged[0]['temperature_ensemble_p10'] is None
    assert merged[0]['predictability_score'] == 60
    assert merged[0]['predictability_branch'] == 'lead_only'


def test_forecast_failure_cannot_generate_random_data(monkeypatch):
    service = WeatherService()
    monkeypatch.setattr(service, '_qweather_is_configured', lambda: False)
    monkeypatch.setattr(service, '_get_openmeteo_forecast', lambda *_a, **_k: [])
    monkeypatch.setattr(service, '_get_mock_forecast', lambda *_a: pytest.fail('random fallback called'))
    assert service.get_weather_forecast() == []


@pytest.mark.parametrize('unit,accepted', [('μg/m3', True), ('mg/m3', False), (None, False)])
def test_air_pollutant_requires_compatible_units(monkeypatch, unit, accepted):
    from types import SimpleNamespace
    from services import weather_service as module
    service = WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://unit-test.qweatherapi.com/v7'
    payload = {'pubTime': '2026-10-06T08:00:00+08:00', 'indexes': [{'code': 'cn-mee', 'aqi': 80}], 'pollutants': [
        {'code': 'pm2p5', 'concentration': {'value': 120, 'unit': unit}}
    ]}
    monkeypatch.setattr(module, 'reserve_qweather_request', lambda *_a: True)
    monkeypatch.setattr(module, '_record_external_api_timing', lambda *_a: None)
    monkeypatch.setattr(module.requests, 'get', lambda *_a, **_k: SimpleNamespace(status_code=200, json=lambda: payload))
    result = service._get_qweather_air_quality('116.20,29.27')
    assert ('pm25' in result) is accepted


def test_future_days_do_not_reuse_current_pollution(forecast, monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from services import dlnm_risk_service
    forecast.qm_params = {}
    forecast.visit_mean = 10
    monkeypatch.setattr(forecast, 'get_lag_temperature_profile', lambda *_a, **_k: ([30] * 8, ['test'] * 8))
    monkeypatch.setattr(forecast, 'predict_daily_visits', lambda *_a, **_k: {
        'probability_exceed_p90': .2, 'point_estimate': 10, 'p10': 8, 'p50': 10, 'p90': 12,
    })
    monkeypatch.setattr(dlnm_risk_service, 'get_dlnm_service', lambda: SimpleNamespace(identify_extreme_weather_events=lambda *_a: []))
    result = forecast.generate_7day_forecast([30] * 7, start_date=date(2026, 10, 5), context={'pm25': 10, 'aqi': 50})
    for day in result[0]:
        assert day['composite_exposure']['pm25_proxy'] is None
        assert day['composite_exposure']['inputs']['pm25']['status'] == 'unknown'
        assert day['predictability']['branch'] == 'lead_only'


@pytest.mark.parametrize('lead', [1, 4, 7])
def test_uncalibrated_postprocessing_never_lowers_forecast(forecast, lead):
    forecast.qm_params = {'temp_values': [10, 20, 30], 'min': 10, 'max': 30}
    corrected, uncertainty = forecast.quantile_mapping(40, lead_day=lead)
    assert corrected == 40
    assert uncertainty['bias_correction'] == 0
    assert uncertainty['model_spread'] is None
    assert uncertainty['calibration_status'] == 'unvalidated'
    assert uncertainty['interval_method'] == 'heuristic_scenario'


def test_production_fallback_carries_no_fabricated_numbers():
    from core.weather import get_fallback_weather_data, is_live_observational_weather, is_heat_action_weather_ready
    data = get_fallback_weather_data()
    assert data['available'] is False
    assert data['is_mock'] is False
    for key in ('temperature', 'temperature_max', 'humidity', 'pm25', 'aqi'):
        assert data[key] is None
        assert data['input_states'][key]['status'] == 'unknown'
    assert not is_live_observational_weather(data)
    assert not is_heat_action_weather_ready(data)


def test_cache_failure_preserves_unavailable_payload(app, monkeypatch):
    from types import SimpleNamespace
    from core import weather
    with app.app_context():
        monkeypatch.setattr(weather, 'is_demo_mode', lambda: False)
        monkeypatch.setattr(weather, '_get_redis_client', lambda: None)
        monkeypatch.setattr(weather, 'get_weather_fetcher', lambda: SimpleNamespace(get_current_weather=lambda *_a: None))
        data, cached = weather.get_weather_with_cache('都昌', cache_only=False)
        assert data['temperature'] is None
        assert data['aqi'] is None
        assert data['status'] == 'unknown'


def test_missing_lag_and_visit_thresholds_never_become_default_forecasts(forecast, monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from services import dlnm_risk_service
    forecast.weather_history = None
    forecast.qm_params = {'mean': 15}
    forecast.visit_mean = None
    forecast.visit_threshold_p90 = None
    forecast.max_observed_daily_visits = None
    lags, sources = forecast.get_lag_temperature_profile(date(2026, 10, 5))
    assert lags == [None] * 8
    assert sources == ['unknown'] * 8
    assert forecast.predict_daily_visits(35, lags)['probability_exceed_p90'] is None
    monkeypatch.setattr(dlnm_risk_service, 'get_dlnm_service', lambda: SimpleNamespace(identify_extreme_weather_events=lambda *_a: []))
    days, summary = forecast.generate_7day_forecast([35] * 7, start_date=date(2026, 10, 5))
    assert all(day['risk_level'] is None for day in days)
    assert all(day['composite_exposure']['score'] is not None for day in days)
    assert summary['unknown_health_days'] == 7
    assert summary['visit_projection_status'] == 'unknown'
    assert summary['probability_products']['available'] is False
    assert summary['probability_products']['status'] == 'disabled_uncalibrated'
    assert summary['overall_risk'] == 'unavailable'
    assert summary['total_expected_visits'] is None
    assert summary['scenario_totals']['baseline_total'] is None
    assert summary['impact_likelihood_matrix']['available'] is False
    assert not any('预计正常' in row['advice'] for row in summary['recommendations'])


@pytest.mark.parametrize('field,value', [('precipitation_probability', None), ('precipitation_probability', float('nan')), ('precipitation', None)])
def test_nowcast_missing_rain_is_not_dry(monkeypatch, field, value):
    from types import SimpleNamespace
    from services import weather_service as module
    service = WeatherService()
    hourly = {'time': ['2026-10-05T12:00'], 'precipitation_probability': [0],
              'precipitation': [0], 'temperature_2m': [30], 'weather_code': [0]}
    hourly[field][0] = value
    monkeypatch.setattr(module, '_record_external_api_timing', lambda *_a: None)
    monkeypatch.setattr(module.requests, 'get', lambda *_a, **_k: SimpleNamespace(status_code=200, json=lambda: {'hourly': hourly}))
    result = service.get_short_term_nowcast(hours=1)
    assert result['available'] is True
    expected_field = 'precipitation_probability' if field == 'precipitation_probability' else 'precipitation_mm'
    assert result['timeline'][0][expected_field] is None


def test_forecast_page_renders_unknown_visit_predictions(authenticated_client, monkeypatch, forecast):
    from datetime import timedelta
    from types import SimpleNamespace
    from core.time_utils import today_local
    from services import dlnm_risk_service
    start = today_local()
    forecast.weather_history = None
    forecast.qm_params = {}
    forecast.visit_mean = None
    forecast.visit_threshold_p90 = None
    forecast.max_observed_daily_visits = None
    entries = [{'date': (start + timedelta(days=i)).isoformat(), 'temperature_max': 36,
                'temperature_min': 25, 'temperature_mean': 30.5, 'humidity': 70,
                'wind_speed': 2, 'condition': '晴', 'data_source': 'QWeather', 'is_mock': False}
               for i in range(7)]
    monkeypatch.setattr('blueprints.tools.get_qweather_forecast_with_cache', lambda *_a, **_k: (entries, False, {'source': 'QWeather'}))
    monkeypatch.setattr('blueprints.tools.get_forecast_service', lambda: forecast)
    from core.time_utils import utcnow
    current = {'temperature': 30, 'temperature_max': 36, 'temperature_min': 25,
               'humidity': 70, 'pressure': 1005, 'wind_speed': 2, 'weather_condition': '晴',
               'data_source': 'QWeather', 'is_mock': False, 'observed_at': utcnow().isoformat(),
               'quality_version': 1, 'pm25': 20, 'aqi': 45, 'air_quality_available': True,
               'air_observed_at': utcnow().isoformat()}
    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', lambda *_a: (current, False))
    monkeypatch.setattr(dlnm_risk_service, 'get_dlnm_service', lambda: SimpleNamespace(identify_extreme_weather_events=lambda *_a: []))
    response = authenticated_client.get('/forecast-7day')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '就诊负荷未知：预测资料不足' in body
    assert '空气质量未知，未计入评分' in body
    assert '无多模型信息' in body
    assert '预计正常' not in body
