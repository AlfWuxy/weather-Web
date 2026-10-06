"""复核核心温度缺失与上游评分来源的语义边界。"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from services.forecast_service import ForecastService
from services.weather_service import WeatherService


@pytest.fixture
def forecast():
    return ForecastService.__new__(ForecastService)


@pytest.mark.parametrize('temperature', [None, float('nan'), float('inf'), 'bad'])
@pytest.mark.parametrize('other', [dict(humidity=70), dict(humidity=95, pm25=200, temp_min=29)])
def test_temperature_unknown_prevents_overall_exposure_classification(forecast, temperature, other):
    result = forecast._composite_exposure_risk(temperature, **{'temp_min': None, **other})
    assert result['score'] is result['pre_clip_score'] is result['final_score'] is None
    assert result['level'] == '未知'
    assert result['status'] == 'unknown'
    assert result['missing_required_inputs'] == ['temperature']
    assert result['components']['heat'] is None
    assert result['components']['humidity'] is not None
    assert result['inputs']['temperature']['status'] == 'unknown'
    assert 'heat' in result['unknown_components']


@pytest.mark.parametrize('temperature', [0, -10, 35])
def test_finite_temperature_keeps_valid_zero_and_negative_values(forecast, temperature):
    result = forecast._composite_exposure_risk(temperature, None, 70)
    assert result['score'] is not None
    assert result['missing_required_inputs'] == []
    assert result['inputs']['temperature']['used_value'] == temperature


@pytest.mark.parametrize('score', [0, 75, 100])
@pytest.mark.parametrize('spread,count', [(None, 1), (float('nan'), 3), (-1, 3), (0, 1), (0, 3)])
def test_valid_external_score_is_used_and_marked_unvalidated(forecast, score, spread, count):
    result = forecast._calculate_predictability(4, spread, count, score,
        external_source='provider_reference', external_method='provider_index')
    assert result['score'] == score
    assert result['branch'] == 'external'
    assert result['source'] == 'provider_reference'
    assert result['method'] == 'provider_index'
    assert result['validated'] is False
    assert result['calibration_status'] == 'unvalidated'
    assert result['inputs']['external_score_status'] == 'valid'
    assert result['inputs']['lead_penalty'] is None
    assert '非实测准确率' in result['note']


@pytest.mark.parametrize('external', [None, float('nan'), float('inf'), -1, 101, True, 'bad'])
@pytest.mark.parametrize('spread,count,branch,expected', [(None, 2, 'lead_only', 60), (0, 1, 'lead_only', 60), (0, 2, 'derived', 99), (1.5, 5, 'derived', 76)])
def test_missing_or_invalid_external_scores_have_explicit_fallback(forecast, external, spread, count, branch, expected):
    result = forecast._calculate_predictability(1, spread, count, external)
    assert result['branch'] == branch
    assert result['score'] == expected
    assert result['inputs']['external_score_status'] == ('missing' if external is None else 'invalid')
    if external is not None:
        assert result['inputs']['external_score_rejection']


def test_local_weather_upstream_is_not_mislabelled_external(forecast):
    weather = WeatherService()
    entry = weather._merge_multimodel_forecast([
        {'date': '2026-10-06', 'temperature_mean': 30, 'temperature_max': 35, 'temperature_min': 25}
    ], [])[0]
    normalized = forecast._normalize_forecast_entry(entry)
    result = forecast._calculate_predictability(1, normalized['model_spread'], normalized['model_count'],
        normalized['predictability_score'], external_source=normalized['predictability_source'],
        external_method=normalized['predictability_method'])
    assert result['branch'] == 'lead_only'
    assert result['score'] == 60
    assert result['source'] == 'local_forecast_service'


def test_external_metadata_survives_full_seven_day_prediction(forecast, monkeypatch):
    from services import dlnm_risk_service
    forecast.weather_history = None
    forecast.qm_params = {}
    forecast.visit_mean = forecast.visit_threshold_p90 = None
    forecast.max_observed_daily_visits = None
    monkeypatch.setattr(dlnm_risk_service, 'get_dlnm_service', lambda: SimpleNamespace(identify_extreme_weather_events=lambda *_a: []))
    days, summary = forecast.generate_7day_forecast([
        {'date': (date(2026, 10, 6) + timedelta(days=i)).isoformat(), 'temperature': 30,
         'predictability_score': 0, 'predictability_source': 'provider_reference', 'predictability_method': 'index'}
        for i in range(7)
    ], start_date=date(2026, 10, 6))
    assert summary['predictability']['average_score'] == 0
    assert all(day['predictability']['branch'] == 'external' for day in days)
    assert all(day['predictability']['source'] == 'provider_reference' for day in days)


def _nowcast(monkeypatch, hourly, units=None, timezone='Asia/Shanghai'):
    from services import weather_service as module
    calls = []
    payload = {'hourly': hourly, 'hourly_units': units or {}, 'timezone': timezone}
    def get(*_a, **kwargs):
        calls.append(kwargs['params'])
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(module.requests, 'get', get)
    monkeypatch.setattr(module, '_record_external_api_timing', lambda *_a: None)
    result = WeatherService().get_short_term_nowcast(hours=len(hourly['time']))
    return result, calls[0]


def test_hourly_rainfall_amount_is_separate_from_snow_and_probability(monkeypatch):
    result, params = _nowcast(monkeypatch, {
        'time': ['2026-10-06T12:00', '2026-10-06T13:00'],
        'rain': [1, 0], 'showers': [2, 0], 'precipitation': [9, 5],
        'precipitation_probability': [None, 0],
    })
    assert {'rain', 'showers'} <= set(params['hourly'].split(','))
    assert params['precipitation_unit'] == 'mm'
    assert result['timezone'] == 'Asia/Shanghai'
    assert result['rainfall_interval_minutes'] == result['precipitation_interval_minutes'] == 60
    assert result['rainfall_time_reference'] == result['precipitation_time_reference'] == 'interval_end'
    assert result['timeline'][0]['rainfall_mm'] == 3
    assert result['timeline'][1]['rainfall_mm'] == 0
    assert result['timeline'][0]['precipitation_probability'] is None
    assert result['timeline'][0]['time'] == '2026-10-06T12:00+08:00'
    assert result['timeline'][0]['interval_start'] == '2026-10-06T11:00+08:00'
    assert result['rainfall_source'] == 'forecast_model'
    assert result['probability_complete'] is False


@pytest.mark.parametrize('rain,showers', [(None, 3), (2, None), (float('nan'), 2), (-1, 2)])
def test_partial_rainfall_never_inferred_from_probability(monkeypatch, rain, showers):
    result, _ = _nowcast(monkeypatch, {'time': ['2026-10-06T12:00'],
        'rain': [rain], 'showers': [showers], 'precipitation_probability': [90]})
    assert result['timeline'][0]['rainfall_mm'] is None
    assert result['timeline'][0]['precipitation_probability'] == 90


@pytest.mark.parametrize('times', [['2026-10-06T12:00', '2026-10-06T14:00'],
                                  ['2026-10-06T12:30'], ['damaged']])
def test_invalid_hourly_window_is_unavailable(monkeypatch, times):
    result, _ = _nowcast(monkeypatch, {'time': times})
    assert result['available'] is False
    assert result['timeline'] == []


def test_incompatible_amount_unit_is_unknown(monkeypatch):
    result, _ = _nowcast(monkeypatch, {'time': ['2026-10-06T12:00'],
        'rain': [1], 'showers': [2]}, units={'rain': 'inch', 'showers': 'mm'})
    assert result['timeline'][0]['rainfall_mm'] is None


def test_unexpected_timezone_is_not_mislabeled(monkeypatch):
    result, _ = _nowcast(monkeypatch, {'time': ['2026-10-06T12:00']}, timezone='GMT')
    assert result['available'] is False
    assert result['reason'] == 'unexpected_timezone'
