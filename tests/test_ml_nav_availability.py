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


def test_dashboard_hides_ml_prediction_for_normal_user_even_when_model_loaded(
    authenticated_client,
    monkeypatch,
):
    class _Ready:
        model_loaded = True

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: _Ready(),
    )

    html = authenticated_client.get('/dashboard').get_data(as_text=True)
    assert 'AI 疾病预测' not in html
    assert 'href="/ml-prediction"' not in html


def test_admin_dashboard_shows_ml_prediction_when_model_loaded(admin_client, monkeypatch):
    class _Ready:
        model_loaded = True

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: _Ready(),
    )

    html = admin_client.get('/dashboard').get_data(as_text=True)
    assert 'AI 疾病预测' in html
    assert 'href="/ml-prediction"' in html


def test_ml_prediction_page_has_no_generate_form_when_model_missing(authenticated_client, monkeypatch):
    class _Missing:
        model_loaded = False

        def predict_disease_risk(self, *_args, **_kwargs):
            raise AssertionError('模型未加载时不应计算')

    monkeypatch.setattr(
        'blueprints.tools.get_ml_service',
        lambda: _Missing(),
    )

    html = authenticated_client.get('/ml-prediction').get_data(as_text=True)
    assert '生成类别线索' not in html
    assert '模型未部署' in html
