# -*- coding: utf-8 -*-
"""7 天健康预测不得用气候态滞后或假门诊基线编造就诊负担。"""
from datetime import timedelta

import pandas as pd

from core.time_utils import today_local


def _service_with_history(days=14, *, fallback_thresholds=False):
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    start = today_local()
    service.weather_history = pd.DataFrame({
        'date': pd.to_datetime([start - timedelta(days=offset) for offset in range(days)]),
        'tmean': [28.0] * days,
    })
    service.qm_params = {'mean': 28.0}
    service.visit_thresholds_fallback = fallback_thresholds
    service.visit_threshold_p90 = 20.0
    service.visit_mean = 12.0
    service.visit_std = 4.0
    service.max_observed_daily_visits = 40
    return service


def test_generate_7day_forecast_skips_health_when_lags_are_climatology():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    service.weather_history = pd.DataFrame()
    service.qm_params = {'mean': 17.3}
    service.visit_thresholds_fallback = False
    service.visit_threshold_p90 = 20.0
    service.visit_mean = 12.0
    service.visit_std = 4.0
    service.max_observed_daily_visits = 40

    forecasts, summary = service.generate_7day_forecast(
        [28, 29, 30, 31, 30, 29, 28],
        start_date=today_local(),
    )

    assert forecasts == []
    assert summary['health_forecast_available'] is False
    assert '观测' in summary['health_forecast_reason']


def test_generate_7day_forecast_skips_health_when_visit_baseline_is_fallback():
    service = _service_with_history(fallback_thresholds=True)
    forecasts, summary = service.generate_7day_forecast(
        [28, 29, 30, 31, 30, 29, 28],
        start_date=today_local(),
    )

    assert forecasts == []
    assert summary['health_forecast_available'] is False
    assert '门诊' in summary['health_forecast_reason']


def test_generate_7day_forecast_skips_health_when_a_day_has_no_temperature():
    service = _service_with_history(fallback_thresholds=False)
    service.qm_params = {}
    start = today_local()
    week = []
    for idx in range(7):
        day = start + timedelta(days=idx)
        week.append({
            'date': day.strftime('%Y-%m-%d'),
            'temperature_mean': None if idx == 2 else 32.0,
            'temperature_min': 24.0,
            'humidity': 80.0,
        })

    forecasts, summary = service.generate_7day_forecast(week, start_date=start)

    assert forecasts == []
    assert summary['health_forecast_available'] is False
    assert '气温' in summary['health_forecast_reason']


def test_composite_exposure_does_not_invent_20_when_temperature_missing():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    try:
        result = service._composite_exposure_risk(
            temperature=None,
            temp_min=24,
            humidity=80,
        )
    except ValueError as exc:
        assert '气温' in str(exc)
        return
    used = ((result.get('inputs') or {}).get('temperature') or {}).get('used_value')
    assert used != 20.0
    assert (result.get('inputs') or {}).get('temperature', {}).get('imputed') is not True


def test_composite_exposure_does_not_invent_hot_night_from_daytime_minus_4():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    missing_night = service._composite_exposure_risk(
        temperature=36,
        temp_min=None,
        humidity=80,
    )
    known_hot_night = service._composite_exposure_risk(
        temperature=36,
        temp_min=32,
        humidity=80,
    )

    night = (missing_night.get('inputs') or {}).get('temp_min') or {}
    assert night.get('used_value') != 32.0
    assert night.get('source') != 'temperature_minus_4'
    assert missing_night.get('hot_night_in_score') is False
    assert missing_night['score'] < known_hot_night['score']


def test_live_forecast_service_does_not_emit_visit_burden_from_stale_csv():
    from services.forecast_service import ForecastService

    service = ForecastService()
    forecasts, summary = service.generate_7day_forecast(
        [32, 33, 31, 30, 29, 28, 27],
        start_date=today_local(),
    )

    assert forecasts == [] or summary.get('health_forecast_available') is False
    assert not any(
        (row.get('probability_high_visits') or 0) > 0
        for row in (forecasts or [])
    )
    assert '橙色预警' not in str(forecasts)


def _login_tool_user(client, db_session, username):
    from core.db_models import User

    user = User(username=username, role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
        session['_csrf_token'] = 'test-csrf-token'
    return user


def _qweather_week():
    from core.time_utils import today_local as _today

    start = _today()
    days = []
    for idx in range(7):
        day = start + timedelta(days=idx)
        days.append({
            'date': day.strftime('%Y-%m-%d'),
            'temperature_max': 24 + idx,
            'temperature_min': 14 + idx,
            'temperature_mean': 19 + idx,
            'condition': '多云',
            'humidity': 70,
            'data_source': 'QWeather',
            'is_mock': False,
        })
    days[1]['temperature_max'] = 26
    days[1]['temperature_min'] = 18
    return days


def test_forecast_page_keeps_weather_and_hides_visit_burden_when_health_unavailable(
    client, db_session, monkeypatch
):
    _login_tool_user(client, db_session, 'forecast_health_page_user')
    qweather_days = _qweather_week()

    class FakeForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            return [], {
                'health_forecast_available': False,
                'health_forecast_reason': '近几日气温观测不足，就诊负担预测暂不显示。',
                'recommendations': [],
            }

    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days=7: (qweather_days, False, {'source': 'QWeather'}),
        raising=False,
    )
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: ({
            'temperature': 27,
            'pm25': 18,
            'aqi': 42,
            'data_source': 'QWeather',
            'is_mock': False,
        }, False),
        raising=False,
    )
    monkeypatch.setattr(
        'blueprints.tools.get_forecast_service',
        lambda: FakeForecastService(),
        raising=False,
    )

    body = client.get('/forecast-7day?location=都昌').get_data(as_text=True)

    assert '近几日气温观测不足' in body
    assert '就诊负担升高' not in body
    assert '26° / 18°' in body
    assert '7 天天气正在更新' not in body
    assert '早晚测血压' not in body
    assert '补水 1.5L+' not in body


def test_forecast_page_still_explains_missing_weather(client, db_session, monkeypatch):
    _login_tool_user(client, db_session, 'forecast_weather_missing_user')
    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    body = client.get('/forecast-7day?location=都昌').get_data(as_text=True)

    assert '7 天天气正在更新' in body
    assert '就诊负担升高' not in body


def test_composite_exposure_does_not_score_non_forecast_pm25():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    kwargs = dict(temperature=32, temp_min=24, humidity=80)

    reused = service._composite_exposure_risk(
        pm25=80,
        pm25_origin='current_weather_context',
        **kwargs,
    )
    current_aqi = service._composite_exposure_risk(
        pm25=None,
        aqi=180,
        aqi_origin='current_weather_context',
        **kwargs,
    )
    defaulted = service._composite_exposure_risk(**kwargs)
    forecast_pm = service._composite_exposure_risk(pm25=80, **kwargs)
    day_aqi = service._composite_exposure_risk(pm25=None, aqi=180, **kwargs)

    assert reused['pm25_source'] == 'current_observation_reuse'
    assert reused['pm25_in_score'] is False
    assert reused['score_basis'] == 'heat_humidity_hot_night'
    assert reused['score'] < forecast_pm['score']
    assert reused['components']['pm25'] == forecast_pm['components']['pm25']

    assert current_aqi['pm25_source'] == 'current_observation_aqi_proxy'
    assert current_aqi['pm25_in_score'] is False
    assert current_aqi['score'] < day_aqi['score']

    assert defaulted['pm25_source'] == 'default_aqi_50'
    assert defaulted['pm25_in_score'] is False
    assert defaulted['score'] == reused['score']
    assert defaulted['inputs']['pm25']['aqi_used'] is None
    assert defaulted['inputs']['pm25']['used_value'] is None
    assert defaulted['pm25_proxy'] is None

    assert forecast_pm['pm25_in_score'] is True
    assert forecast_pm['score_basis'] == 'composite'
    assert day_aqi['pm25_in_score'] is True


def test_generate_7day_forecast_does_not_score_reused_current_pm25():
    service = _service_with_history(fallback_thresholds=False)
    service.qm_params = {}
    start = today_local()
    week = []
    for idx in range(7):
        day = start + __import__('datetime').timedelta(days=idx)
        week.append({
            'date': day.strftime('%Y-%m-%d'),
            'temperature_mean': 32.0,
            'temperature_min': 24.0,
            'humidity': 80.0,
        })

    forecasts, summary = service.generate_7day_forecast(
        week,
        start_date=start,
        context={'pm25': 80.0, 'aqi': 160.0},
    )

    assert forecasts
    assert summary.get('health_forecast_available') is not False
    sources = {(row.get('composite_exposure') or {}).get('pm25_source') for row in forecasts}
    assert sources == {'current_observation_reuse'}
    assert all(
        (row.get('composite_exposure') or {}).get('pm25_in_score') is False
        for row in forecasts
    )
    scores = [(row.get('composite_exposure') or {}).get('score') for row in forecasts]
    with_forecast_pm = service._composite_exposure_risk(
        temperature=32,
        temp_min=24,
        humidity=80,
        pm25=80,
    )['score']
    assert all(score is not None and score < with_forecast_pm for score in scores)


def test_forecast_cards_label_heat_risk_when_pm_is_reused():
    from datetime import date as _date

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
        'composite_exposure': {
            'score': 27.0,
            'final_score': 27.0,
            'pm25_source': 'current_observation_reuse',
            'pm25_in_score': False,
            'score_basis': 'heat_humidity_hot_night',
            'components': {'heat': 24, 'pm25': 81, 'humidity': 24, 'hot_night': 72},
            'inputs': {
                'pm25': {
                    'used_value': 80.0,
                    'imputed': True,
                    'source': 'current_observation_reuse',
                    'included_in_score': False,
                },
            },
        },
    }]

    card = build_forecast_cards(qweather_days, health_forecasts, _date(2026, 7, 10))[0]

    assert card['risk_available'] is True
    assert card['pm25_in_score'] is False
    assert card['risk_label'] == '热风险低'
    assert '高风险' not in card['risk_label']


def test_heat_risk_does_not_reuse_daytime_temp_as_night_min():
    from services.heat_action_service import HeatActionService

    service = HeatActionService()
    missing_night = service.calculate_heat_risk({'temperature': 36, 'humidity': 70})
    known_hot_night = service.calculate_heat_risk({
        'temperature': 36,
        'humidity': 70,
        'temperature_min': 32,
    })

    assert missing_night['night_min'] != 36
    assert missing_night['night_min'] is None
    assert missing_night['risk_score'] < known_hot_night['risk_score']
