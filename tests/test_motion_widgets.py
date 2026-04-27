# -*- coding: utf-8 -*-
"""首页与避暑页数据动效回归测试。"""
import json


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def test_dashboard_renders_temperature_and_registered_metric_widgets(client, db_session):
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

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-fx="thermo-bar"' in body
    assert '已登记健康指标' in body
    assert 'data-fx="sparkline"' in body
    assert '父亲 · 当前登记' in body
    assert '橙线 = 当前登记值定位' in body


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
