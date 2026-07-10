# -*- coding: utf-8 -*-
"""Forecast service regression tests."""

import math
from datetime import date

import pytest


def test_normalize_forecast_entry_preserves_zero_p50():
    from services.forecast_service import ForecastService

    service = ForecastService()
    normalized = service._normalize_forecast_entry({
        'temperature_ensemble_p50': 0,
        'temperature_ensemble_mean': 5,
        'temperature': 8,
    })

    assert normalized['temperature_p50'] == 0.0
    assert normalized['temp'] == 0.0


def test_composite_exposure_returns_score_stages_and_input_trace():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    result = service._composite_exposure_risk(
        temperature=36,
        temp_min=None,
        humidity=None,
        pm25=None,
        aqi=100,
        temp_min_fallback=26,
    )

    assert result['synergy_bonus'] == 12.0
    assert result['pre_clip_score'] == 63.4
    assert result['final_score'] == 63.4
    assert result['score'] == result['final_score']
    assert result['pm25_source'] == 'aqi_proxy'
    assert result['inputs']['pm25'] == {
        'used_value': 65.0,
        'imputed': True,
        'source': 'aqi_proxy',
        'detail_source': 'day_aqi_input',
        'aqi_used': 100.0,
        'aqi_imputed': False,
    }
    assert result['inputs']['humidity']['used_value'] == 60.0
    assert result['inputs']['humidity']['imputed'] is True
    assert result['inputs']['temp_min'] == {
        'used_value': 26.0,
        'imputed': True,
        'source': 'temperature_uncertainty_lower',
    }

    reused_observation_result = service._composite_exposure_risk(
        temperature=32,
        temp_min=24,
        humidity=80,
        pm25=42,
        aqi=120,
        pm25_origin='current_weather_context',
    )
    assert reused_observation_result['pm25_source'] == 'current_observation_reuse'
    assert reused_observation_result['inputs']['pm25']['used_value'] == 42.0
    assert reused_observation_result['inputs']['pm25']['imputed'] is True
    assert reused_observation_result['inputs']['pm25']['source'] == 'current_observation_reuse'
    assert reused_observation_result['inputs']['pm25']['detail_source'] == 'current_weather_context'

    current_aqi_result = service._composite_exposure_risk(
        temperature=32,
        temp_min=24,
        humidity=80,
        pm25=None,
        aqi=80,
        aqi_origin='current_weather_context',
    )
    assert current_aqi_result['pm25_source'] == 'current_observation_aqi_proxy'
    assert current_aqi_result['inputs']['pm25']['detail_source'] == 'current_weather_context'
    assert current_aqi_result['inputs']['pm25']['aqi_used'] == 80.0

    default_aqi_result = service._composite_exposure_risk(
        temperature=32,
        temp_min=24,
        humidity=80,
    )
    assert default_aqi_result['pm25_source'] == 'default_aqi_50'
    assert default_aqi_result['inputs']['pm25']['source'] == 'default_aqi_50'
    assert default_aqi_result['inputs']['pm25']['aqi_used'] == 50.0
    assert default_aqi_result['inputs']['pm25']['aqi_imputed'] is True


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('temperature_max', None),
        ('temperature_min', float('nan')),
        ('temperature_mean', float('inf')),
        ('humidity', 'invalid'),
    ],
)
def test_qweather_forecast_contract_rejects_missing_or_nonfinite_fields(field, value):
    from core.weather import _valid_qweather_only_forecast

    day = {
        'date': '2026-07-10',
        'temperature_max': 32,
        'temperature_min': 24,
        'temperature_mean': 28,
        'humidity': 70,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    day[field] = value

    assert _valid_qweather_only_forecast([day], days=1) is False


def test_qweather_normalizer_does_not_insert_temperature_or_humidity_defaults():
    from services.weather_service import WeatherService

    service = WeatherService.__new__(WeatherService)
    normalized = service._normalize_qweather_daily_entry({
        'fxDate': '2026-07-10',
        'tempMax': 'nan',
        'tempMin': None,
        'humidity': 'inf',
    })

    assert normalized['temperature_max'] is None
    assert normalized['temperature_min'] is None
    assert normalized['temperature_mean'] is None
    assert normalized['humidity'] is None


def test_predict_daily_visits_exposes_raw_probability_inputs(monkeypatch):
    from services.forecast_service import ForecastService

    class FakeDlnmService:
        seasonal_baseline = {}

        def calculate_rr(self, temperature, lag_temps=None):
            return 2.0, {'temperature': temperature, 'lag_temps': lag_temps}

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: FakeDlnmService(),
    )

    service = ForecastService.__new__(ForecastService)
    service.visit_mean = 50.0
    service.visit_threshold_p90 = 30.0
    service.max_observed_daily_visits = 20.0

    result = service.predict_daily_visits(temperature=35, lag_temps=[35], dow=6)

    expected_std = math.sqrt(70.0 + 70.0 ** 2 / 2.0)
    assert result['raw_point_estimate'] == 70.0
    assert result['point_estimate'] == 40.0
    assert result['rr'] == 2.0
    assert result['baseline'] == 50.0
    assert result['dow_factor'] == 0.7
    assert result['visit_threshold_p90'] == 30.0
    assert result['std_estimate'] == pytest.approx(expected_std, abs=1e-4)
    assert result['probability_method'] == 'normal_approximation'
    assert result['guardrail_cap'] == 40.0
    assert result['guardrail_applied'] is True


def test_predictability_reports_external_and_derived_branches():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    external = service._calculate_predictability(
        lead_day=4,
        model_spread=2.0,
        model_count=3,
        external_score=87,
    )
    derived = service._calculate_predictability(
        lead_day=3,
        model_spread=1.5,
        model_count=3,
    )

    assert external['branch'] == 'external'
    assert external['score'] == 87.0
    assert external['inputs']['external_score'] == 87.0
    assert external['inputs']['lead_penalty'] is None
    assert external['inputs']['model_bonus'] is None

    assert derived['branch'] == 'derived'
    assert derived['raw_score'] == 74.0
    assert derived['score'] == 74.0
    assert derived['inputs']['lead_penalty'] == 6.0
    assert derived['inputs']['model_bonus'] == 4.0


def test_forecast_cards_do_not_substitute_visit_probability_for_composite_score():
    from services.forecast_cards import build_forecast_cards

    qweather_days = [{
        'date': '2026-07-10',
        'temperature_max': 31,
        'temperature_min': 24,
        'temperature_mean': 27.5,
        'humidity': 70,
        'condition': '多云',
        'data_source': 'QWeather',
        'is_mock': False,
    }]
    health_forecasts = [{
        'date': '2026-07-10',
        'probability_high_visits': 82.0,
        'composite_exposure': {},
    }]

    card = build_forecast_cards(qweather_days, health_forecasts, date(2026, 7, 10))[0]

    assert card['probability_high_visits'] == 82.0
    assert card['risk_available'] is False
    assert card['risk_score'] is None
    assert card['risk_level'] == 'unknown'


def test_forecast_cards_pass_transparency_inputs():
    from services.forecast_cards import build_forecast_cards

    qweather_days = [{
        'date': '2026-07-10',
        'temperature_max': 33,
        'temperature_min': 25,
        'temperature_mean': 29,
        'humidity': 72,
        'condition': '晴',
        'data_source': 'QWeather',
        'is_mock': False,
    }]
    health_forecasts = [{
        'date': '2026-07-10',
        'probability_high_visits': 63.2,
        'composite_exposure': {
            'score': 71.2,
            'pre_clip_score': 104.7,
            'final_score': 100.0,
            'synergy_bonus': 18.0,
            'pm25_source': 'aqi_proxy',
            'components': {'heat': 80, 'pm25': 55, 'humidity': 44, 'hot_night': 100},
            'inputs': {
                'temperature': {'used_value': 34.0, 'imputed': False, 'source': 'corrected_forecast'},
                'temp_min': {'used_value': 25.0, 'imputed': True, 'source': 'temperature_uncertainty_lower'},
                'humidity': {'used_value': 60.0, 'imputed': True, 'source': 'default_60'},
                'pm25': {
                    'used_value': 52.0,
                    'imputed': True,
                    'source': 'aqi_proxy',
                    'detail_source': 'day_aqi_input',
                    'aqi_used': 80.0,
                },
            },
        },
        'visits': {
            'point_estimate': 40.0,
            'raw_point_estimate': 47.25,
            'rr': 1.35,
            'baseline': 35.0,
            'dow_factor': 1.0,
            'visit_threshold_p90': 42.0,
            'std_estimate': 18.4,
            'probability_method': 'normal_approximation',
            'guardrail_cap': 40.0,
            'guardrail_applied': True,
        },
        'predictability': {
            'score': 76.0,
            'label': '高',
            'branch': 'derived',
            'raw_score': 76.0,
            'inputs': {
                'external_score': None,
                'lead_day': 2,
                'model_spread': 1.2,
                'model_count': 2,
                'lead_penalty': 3.0,
                'model_bonus': 2.0,
            },
        },
    }]

    card = build_forecast_cards(qweather_days, health_forecasts, date(2026, 7, 10))[0]

    assert card['risk_score'] == 100
    assert card['composite_pre_clip_score'] == 104.7
    assert card['composite_synergy_bonus'] == 18.0
    assert card['pm25_source'] == 'aqi_proxy'
    assert card['pm25_used'] == 52.0
    assert card['humidity_imputed'] is True
    assert card['temp_min_imputed'] is True
    assert card['visit_raw_point_estimate'] == 47.25
    assert card['visit_threshold_p90'] == 42.0
    assert card['visit_dow_factor'] == 1.0
    assert card['predictability_branch'] == 'derived'
    assert card['predictability_lead_penalty'] == 3.0
    assert card['predictability_model_bonus'] == 2.0


def test_forecast_page_embeds_recalculation_context(authenticated_client, monkeypatch):
    import json
    from datetime import timedelta
    from html.parser import HTMLParser

    from core.time_utils import today_local

    start = today_local()
    qweather_days = [
        {
            'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
            'temperature_max': 32 + idx,
            'temperature_min': 24 + idx,
            'temperature_mean': 28 + idx,
            'condition': '晴',
            'humidity': 75,
        }
        for idx in range(7)
    ]

    class FakeForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            forecasts = []
            for idx in range(7):
                pm25_source = 'current_observation_reuse' if idx == 0 else 'aqi_proxy'
                pm25_input = (
                    {
                        'used_value': 42.0,
                        'imputed': True,
                        'source': 'current_observation_reuse',
                        'detail_source': 'current_weather_context',
                        'aqi_used': None,
                    }
                    if idx == 0
                    else {
                        'used_value': 52.0,
                        'imputed': True,
                        'source': 'aqi_proxy',
                        'aqi_used': 80.0,
                    }
                )
                forecasts.append({
                    'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
                    'probability_high_visits': 61.5,
                    'composite_exposure': {
                        'score': 72.0,
                        'pre_clip_score': 72.0,
                        'final_score': 72.0,
                        'synergy_bonus': 12.0,
                        'pm25_source': pm25_source,
                        'components': {'heat': 60, 'pm25': 50, 'humidity': 20, 'hot_night': 72},
                        'inputs': {
                            'temp_min': {'used_value': 24.0, 'imputed': True, 'source': 'temperature_uncertainty_lower'},
                            'humidity': {'used_value': 60.0, 'imputed': True, 'source': 'default_60'},
                            'pm25': pm25_input,
                        },
                    },
                    'visits': {
                        'point_estimate': 35.0,
                        'raw_point_estimate': 39.5,
                        'rr': 1.25,
                        'baseline': 31.6,
                        'dow_factor': 1.0,
                        'visit_threshold_p90': 38.0,
                        'std_estimate': 16.2,
                        'probability_method': 'normal_approximation',
                        'guardrail_applied': False,
                    },
                    'predictability': {
                        'score': 88.0,
                        'label': '高',
                        'branch': 'external',
                        'raw_score': 88.0,
                        'inputs': {
                            'external_score': 88.0,
                            'lead_day': idx + 1,
                            'model_spread': 0.8,
                            'model_count': 3,
                            'lead_penalty': None,
                            'model_bonus': None,
                        },
                    },
                })
            return forecasts, {'recommendations': []}

    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days=7: (qweather_days, False, {'source': 'QWeather'}),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_forecast_service',
        lambda: FakeForecastService(),
    )

    response = authenticated_client.get('/forecast-7day')
    body = response.get_data(as_text=True)

    class MetricContextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.contexts = []

        def handle_starttag(self, tag, attrs):
            if tag != 'button':
                return
            values = dict(attrs)
            metric_key = values.get('data-metric-info')
            raw_context = values.get('data-metric-context')
            if metric_key and raw_context:
                self.contexts.append((metric_key, json.loads(raw_context)))

    parser = MetricContextParser()
    parser.feed(body)
    contexts_by_metric = {}
    for metric_key, context_values in parser.contexts:
        contexts_by_metric.setdefault(metric_key, []).append(context_values)

    assert response.status_code == 200
    exposure_context = contexts_by_metric['forecast_exposure_score'][0]
    visit_context = contexts_by_metric['forecast_visit_probability'][0]
    predictability_context = contexts_by_metric['forecast_predictability'][0]
    assert exposure_context['限幅前评分'] == 72.0
    assert exposure_context['协同加分'] == 12.0
    assert exposure_context['PM2.5来源'] == '当前实况复用（非未来预报）'
    assert contexts_by_metric['forecast_exposure_score'][1]['PM2.5来源'] == '未来日AQI×0.65代理'
    assert visit_context['概率所用原始门诊量'] == 39.5
    assert visit_context['历史P90阈值'] == 38.0
    assert predictability_context['计算分支'] == '上游外部分数'
    assert predictability_context['外部分数输入'] == 88.0
