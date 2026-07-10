# -*- coding: utf-8 -*-
"""首页与避暑页数据动效回归测试。"""
import json
from datetime import timedelta

from core.time_utils import today_local


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def test_dashboard_renders_temperature_and_registered_metric_widgets(client, db_session, monkeypatch):
    from core.db_models import FamilyMember, FamilyMemberProfile, User

    user = User(username='motion_user', role='user', community='都昌')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    member = FamilyMember(user_id=user.id, name='父亲', relation='父亲', age=72)
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        metrics=json.dumps({
            'blood_pressure': '138/82',
            'heart_rate': 78,
            'blood_sugar': 6.2,
        }, ensure_ascii=False)
    ))
    db_session.commit()
    _login_as(client, user.id)
    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda location: ({
            'temperature': 27.5,
            'temperature_max': 30,
            'temperature_min': 22,
            'humidity': 64,
            'pressure': 1008,
            'weather_condition': '多云',
            'wind_speed': 2.5,
            'aqi': 42,
            'is_mock': False,
            'data_source': 'QWeather',
        }, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-fx="thermo-bar"' in body
    assert '已登记健康指标' in body
    assert 'data-fx="sparkline"' in body
    assert '父亲 · 当前登记' in body
    assert '橙线 = 当前登记值定位' in body


def test_dashboard_forecast_uses_qweather_cards(client, db_session, monkeypatch):
    from core.db_models import User

    user = User(username='dashboard_qweather_user', role='user', community='都昌')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user.id)
    start = today_local()

    qweather_days = []
    for idx in range(7):
        day = start + timedelta(days=idx)
        qweather_days.append({
            'date': day.strftime('%Y-%m-%d'),
            'temperature_max': 23 + idx,
            'temperature_min': 13 + idx,
            'temperature_mean': 18 + idx,
            'condition': '多云',
            'humidity': 64,
            'data_source': 'QWeather',
            'is_mock': False,
        })
    qweather_days[1]['temperature_max'] = 26
    qweather_days[1]['temperature_min'] = 18

    captured = {}

    def fake_qweather(location, days=7):
        captured['location'] = location
        captured['days'] = days
        return qweather_days, False, {'source': 'QWeather'}

    class FakeForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            captured['start_date'] = start_date
            captured['context'] = context
            forecasts = []
            for idx, _entry in enumerate(forecast_temps):
                day = start + timedelta(days=idx)
                forecasts.append({
                    'date': day.strftime('%Y-%m-%d'),
                    'probability_high_visits': 10 + idx,
                    'composite_exposure': {'score': 18 + idx, 'level': '低'},
                })
            return forecasts, {'recommendations': []}

    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        fake_qweather,
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda _location: ({
            'temperature': 27,
            'temperature_max': 30,
            'temperature_min': 22,
            'humidity': 64,
            'pm25': 18,
            'aqi': 42,
            'data_source': 'QWeather',
            'is_mock': False,
        }, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_forecast_service',
        lambda: FakeForecastService(),
        raising=False,
    )

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert captured['location'] == '都昌'
    assert captured['days'] == 7
    assert captured['start_date'] == start
    assert captured['context'] == {'pm25': 18.0, 'aqi': 42.0}
    assert '26° / 18°' in body
    assert '演示风险' not in body
    assert '34°/26°' not in body


def test_dashboard_forecast_failure_does_not_render_demo_heat(client, db_session, monkeypatch):
    from core.db_models import User

    user = User(username='dashboard_qweather_fail_user', role='user', community='都昌')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user.id)

    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '暂无实时 7 日预报' in body
    assert '演示风险' not in body
    assert '34°/26°' not in body


def test_dashboard_forecast_generation_failure_marks_risk_unknown(client, db_session, monkeypatch):
    from core.db_models import User

    user = User(username='dashboard_forecast_unknown_user', role='user', community='都昌')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user.id)
    start = today_local()

    qweather_days = []
    for idx in range(7):
        day = start + timedelta(days=idx)
        qweather_days.append({
            'date': day.strftime('%Y-%m-%d'),
                'temperature_max': 24,
                'temperature_min': 16,
                'temperature_mean': 20,
                'humidity': 70,
                'condition': '阴',
            'data_source': 'QWeather',
            'is_mock': False,
        })

    class FailingForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            raise RuntimeError('forecast unavailable')

    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: (qweather_days, False, {'source': 'QWeather'}),
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_forecast_service',
        lambda: FailingForecastService(),
        raising=False,
    )

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '待计算' in body
    assert '风险 低风险 · 阴 · 24°/16°' not in body


def test_dashboard_current_risk_does_not_use_mock_weather(client, db_session, monkeypatch):
    from core.db_models import User, WeatherData

    user = User(username='dashboard_mock_weather_user', role='user', community='都昌')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user.id)

    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda location: ({
            'temperature': 36.5,
            'temperature_max': 39,
            'temperature_min': 27,
            'humidity': 80,
            'pressure': 1000,
            'weather_condition': '晴',
            'wind_speed': 2,
            'aqi': 88,
            'is_mock': True,
            'data_source': 'fallback',
        }, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '实时天气暂不可用' in body
    assert '待实时天气' in body
    assert '等待实时天气' in body
    assert '36.5' not in body
    assert '高风险' not in body
    assert WeatherData.query.filter_by(date=today_local(), location='都昌').count() == 0


def test_dashboard_missing_critical_qweather_fields_stays_unavailable_and_does_not_persist(
    client,
    db_session,
    monkeypatch,
):
    """来源虽为 QWeather，关键字段缺失时仍不能显示或写入默认观测。"""
    from core.db_models import User, WeatherData

    user = User(username='dashboard_missing_weather_fields', role='user', community='都昌')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user.id)

    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda location: ({
            'temperature': 36.5,
            'temperature_max': 39,
            'temperature_min': None,
            'humidity': 80,
            'pressure': 1000,
            'weather_condition': '晴',
            'wind_speed': 2,
            'aqi': 88,
            'is_mock': False,
            'data_source': 'QWeather',
        }, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    response = client.get('/dashboard')
    elder_response = client.get('/elder-mode')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '实时天气暂不可用' in body
    assert '关键输入不完整' in body
    assert '36.5' not in body
    assert '39°' not in body
    assert WeatherData.query.filter_by(date=today_local(), location='都昌').count() == 0

    assert elder_response.status_code == 200
    elder_body = elder_response.get_data(as_text=True)
    assert '等待真实天气' in elder_body
    assert '默认温度或风险结论' in elder_body
    assert '36.5' not in elder_body


def test_cooling_page_uses_real_weather_for_thermometer(client, db_session, monkeypatch):
    def fake_weather(location):
        return ({
            'temperature': 27.5,
            'weather_condition': '多云',
            'is_mock': False,
            'data_source': 'QWeather',
        }, False)

    monkeypatch.setattr('services.public_service.get_weather_with_cache', fake_weather)

    response = client.get('/cooling?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-fx="thermometer"' in body
    assert 'data-temp="27.5"' in body
    assert '36.5' not in body


def test_cooling_page_hides_thermometer_for_mock_weather(client, db_session, monkeypatch):
    def fake_weather(location):
        return ({
            'temperature': 36.5,
            'weather_condition': '晴',
            'is_mock': True,
            'data_source': 'fallback',
        }, False)

    monkeypatch.setattr('services.public_service.get_weather_with_cache', fake_weather)

    response = client.get('/cooling?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-fx="thermometer"' not in body
    assert 'data-temp="36.5"' not in body
