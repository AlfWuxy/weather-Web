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
