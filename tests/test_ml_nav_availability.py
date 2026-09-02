# -*- coding: utf-8 -*-


def test_dashboard_hides_ml_prediction_when_model_missing(authenticated_client, monkeypatch):
    class _Missing:
        model_loaded = False

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: _Missing(),
    )

    html = authenticated_client.get('/dashboard').get_data(as_text=True)
    assert 'AI 疾病预测' not in html
    assert 'href="/ml-prediction"' not in html
    assert 'data-nav-key="ml-prediction"' not in html


def test_dashboard_shows_ml_prediction_when_model_loaded(authenticated_client, monkeypatch):
    class _Ready:
        model_loaded = True

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: _Ready(),
    )

    html = authenticated_client.get('/dashboard').get_data(as_text=True)
    assert 'AI 疾病预测' in html
    assert 'href="/ml-prediction"' in html
