# -*- coding: utf-8 -*-
"""个人健康评估的天气来源与空气质量门禁回归。"""
import json
from datetime import timedelta

import pytest


def _login_as(client, user):
    with client.session_transaction() as session:
        session.clear()
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'profile-weather-csrf'


def _weather_payload(*, source='QWeather', air_observed_at=None):
    from core.time_utils import utcnow

    return {
        'temperature': 34.0,
        'temperature_max': 36.0,
        'temperature_min': 27.0,
        'humidity': 68.0,
        'pressure': 1002.0,
        'wind_speed': 2.0,
        'weather_condition': '晴',
        'data_source': source,
        'observed_at': utcnow().isoformat(),
        'quality_version': 1,
        'is_mock': False,
        'is_demo': False,
        'aqi': 88,
        'pm25': 31,
        'air_quality_available': True,
        'air_observed_at': air_observed_at,
        'aqi_estimated': False,
        'air_quality_estimated': False,
    }


def _assessment_form():
    return {
        'csrf_token': 'profile-weather-csrf',
        'outdoor_exposure': 'low',
        'symptom_level': 'none',
        'hydration': 'good',
        'medication_adherence': 'good',
        'sleep_quality': 'good',
    }


def test_personal_assessment_masks_stale_air_before_health_model(
    app,
    client,
    db_session,
    monkeypatch,
):
    """天气可用而空气过期时，模型与历史记录都不能看到旧 AQI。"""
    from core.db_models import HealthRiskAssessment, User
    from core.time_utils import utcnow

    user = User(
        username='profile-stale-air',
        role='user',
        age=70,
        gender='女性',
        community='都昌县',
    )
    user.set_password('LongWeatherPass1!')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user)

    weather = _weather_payload(
        air_observed_at=(utcnow() - timedelta(hours=3)).isoformat(),
    )
    captured = {}

    class FakeHealthRiskService:
        def assess_personal_weather_health_risk(
            self,
            user_profile,
            weather_data,
            screening=None,
        ):
            captured['weather'] = dict(weather_data)
            return {
                'risk_score': 20,
                'risk_level': '低风险',
                'recommendations': [],
                'disease_risks': {},
            }

    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (dict(weather), False),
    )
    monkeypatch.setattr(
        'services.health_risk_service.HealthRiskService',
        FakeHealthRiskService,
    )

    response = client.post(
        '/health-assessment',
        data=_assessment_form(),
        follow_redirects=False,
    )

    assert response.status_code in (301, 302)
    assert captured['weather']['aqi'] is None
    assert captured['weather']['pm25'] is None
    assert captured['weather']['air_observed_at'] is None
    assert captured['weather']['air_quality_available'] is False

    record = HealthRiskAssessment.query.filter_by(user_id=user.id).one()
    saved_weather = json.loads(record.weather_condition)
    assert 'aqi' not in saved_weather


def test_personal_assessment_rejects_openmeteo_health_model(
    client,
    db_session,
    monkeypatch,
):
    """Open-Meteo 只提供基础天气展示，不能进入疾病健康评估。"""
    from core.db_models import HealthRiskAssessment, User

    user = User(
        username='profile-openmeteo',
        role='caregiver',
        age=66,
        gender='男性',
        community='都昌县',
    )
    user.set_password('LongWeatherPass2!')
    db_session.add(user)
    db_session.commit()
    _login_as(client, user)

    weather = _weather_payload(source='Open-Meteo', air_observed_at=None)
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (dict(weather), False),
    )

    response = client.post(
        '/health-assessment',
        data=_assessment_form(),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '天气正在更新，本次评估暂未完成' in response.get_data(as_text=True)
    assert HealthRiskAssessment.query.filter_by(user_id=user.id).count() == 0


@pytest.mark.parametrize(
    'path',
    (
        '/api/v1/chronic/individual',
        '/api/v1/chronic/population',
    ),
)
def test_chronic_api_rejects_client_supplied_weather(
    authenticated_client,
    monkeypatch,
    path,
):
    """正式慢病 API 禁止客户端自报 QWeather provenance。"""
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda *_args, **_kwargs: pytest.fail('拒绝请求不应读取天气缓存'),
    )

    response = authenticated_client.post(
        path,
        json={
            'age': 70,
            'weather': _weather_payload(),
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload['success'] is False
    assert payload['error'] == 'client_weather_not_allowed'
