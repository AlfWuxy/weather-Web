# -*- coding: utf-8 -*-
"""API 不得在缺年龄或缺测天气时用默认岁数/20°C/AQI 50 顶上。"""

from core.db_models import User


def test_ml_predict_api_without_age_does_not_invent_40(authenticated_client, monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError('缺年龄不应进入 ML 预测')

    monkeypatch.setattr(
        'services.ml_prediction_service.MLPredictionService.predict_disease_risk',
        unexpected,
    )

    response = authenticated_client.post(
        '/api/v1/ml/predict',
        json={'temperature': 31, 'humidity': 68},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    body = response.get_json()
    assert response.status_code == 400
    assert body['success'] is False
    assert '请提供年龄' in (body.get('error_detail') or body.get('error') or '')


def test_chronic_individual_api_without_age_does_not_invent_50(
    authenticated_client,
    monkeypatch,
):
    def unexpected(*_args, **_kwargs):
        raise AssertionError('缺年龄不应进入慢病 API')

    monkeypatch.setattr(
        'services.chronic_risk_service.ChronicRiskService.predict_individual_risk',
        unexpected,
    )
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda _city: ({
            'temperature': 31,
            'humidity': 68,
            'data_source': 'QWeather',
            'is_mock': False,
        }, False),
    )

    response = authenticated_client.post(
        '/api/v1/chronic/individual',
        json={'weather': {
            'temperature': 31,
            'humidity': 68,
            'data_source': 'QWeather',
            'is_mock': False,
        }},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    body = response.get_json()
    assert response.status_code == 400
    assert body['success'] is False
    assert '请提供年龄' in (body.get('error_detail') or body.get('error') or '')


def test_ml_predict_api_without_weather_does_not_invent_20_70_50(
    authenticated_client,
    db_session,
    monkeypatch,
):
    user = User.query.filter_by(username='testuser').first()
    user.age = 72
    db_session.commit()
    captured = {}

    def unexpected(_self, user_info, weather_info=None):
        captured['weather'] = weather_info
        raise AssertionError('缺天气不应进入 ML 预测')

    monkeypatch.setattr(
        'services.ml_prediction_service.MLPredictionService.predict_disease_risk',
        unexpected,
    )
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda _city: ({'temperature': 20, 'humidity': 70, 'aqi': 50, 'is_mock': True}, False),
    )

    response = authenticated_client.post(
        '/api/v1/ml/predict',
        json={'age': 72},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    assert response.status_code == 503
    assert captured == {}
    body = response.get_json()
    assert body['success'] is False


def test_ml_predict_api_does_not_fill_missing_humidity_or_aqi(
    authenticated_client,
    db_session,
    monkeypatch,
):
    user = User.query.filter_by(username='testuser').first()
    user.age = 72
    db_session.commit()
    captured = {}

    class FakeML:
        def predict_disease_risk(self, user_info, weather_info=None):
            captured['weather'] = weather_info
            return {'success': True, 'predictions': []}

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: FakeML(),
    )

    response = authenticated_client.post(
        '/api/v1/ml/predict',
        json={'age': 72, 'temperature': 31},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    assert response.status_code == 200
    weather = captured['weather']
    assert weather['temperature'] == 31
    assert weather.get('humidity') is None
    assert weather.get('aqi') is None
    assert weather.get('wind_speed') is None
    assert weather.get('humidity') != 70
    assert weather.get('aqi') != 50
    assert weather.get('wind_speed') != 2.5


def test_ml_community_predict_api_without_weather_does_not_invent_20_70_50(
    authenticated_client,
    monkeypatch,
):
    captured = {}

    def unexpected(_self, community_info, weather_info=None):
        captured['weather'] = weather_info
        raise AssertionError('缺天气不应进入社区 ML 预测')

    monkeypatch.setattr(
        'services.ml_prediction_service.MLPredictionService.predict_community_risk',
        unexpected,
    )
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda _city: ({'temperature': 20, 'humidity': 70, 'aqi': 50, 'is_mock': True}, False),
    )

    response = authenticated_client.post(
        '/api/v1/ml/predict-community',
        json={'name': '测试社区', 'elderly_ratio': 0.3, 'population': 800},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    assert response.status_code == 503
    assert captured == {}
    body = response.get_json()
    assert body['success'] is False


def test_ml_community_predict_api_does_not_fill_missing_humidity_or_aqi(
    authenticated_client,
    monkeypatch,
):
    captured = {}

    class FakeML:
        def predict_community_risk(self, community_info, weather_info=None):
            captured['weather'] = weather_info
            return {'success': True, 'community_risk': {}}

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: FakeML(),
    )

    response = authenticated_client.post(
        '/api/v1/ml/predict-community',
        json={'name': '测试社区', 'temperature': 31, 'elderly_ratio': 0.3, 'population': 800},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    assert response.status_code == 200
    weather = captured['weather']
    assert weather['temperature'] == 31
    assert weather.get('humidity') is None
    assert weather.get('aqi') is None
    assert weather.get('wind_speed') is None
