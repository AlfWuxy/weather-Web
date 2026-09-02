# -*- coding: utf-8 -*-
"""API 不得在缺年龄时用 40/50 岁顶上。"""


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
