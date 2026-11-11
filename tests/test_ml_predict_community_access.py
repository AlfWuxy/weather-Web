# -*- coding: utf-8 -*-
"""社区风险预测不得把其他社区的老龄/慢病统计侧写进建议文案。"""
import pytest

from core.db_models import Community, User


LEAK_MARKERS = (
    '老年人群体',
    '加强对独居老人的健康巡访',
    '社区卫生站做好应急药品储备',
    '0.67',
    '0.22',
)


class _RecordingPredictor:
    def __init__(self):
        self.calls = []

    def predict_community_risk(self, community_info, weather_info):
        self.calls.append(dict(community_info))
        ratio = community_info.get('elderly_ratio') or 0
        groups = ['老年人群体'] if ratio > 0.3 else []
        recommendations = (
            ['加强对独居老人的健康巡访', '社区卫生站做好应急药品储备']
            if ratio > 0.3 else ['保持常规健康管理工作']
        )
        return {
            'success': True,
            'community': community_info.get('name'),
            'high_risk_groups': groups,
            'recommendations': recommendations,
            'risk_score': 55,
        }


def _login_as(client, user, csrf_token='predict-community-csrf'):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token
    return csrf_token


def _user(db_session, username, role, authorized_community=None):
    user = User(
        username=username,
        role=role,
        authorized_community=authorized_community,
    )
    user.set_password('Predict-community-1')
    db_session.add(user)
    db_session.commit()
    return user


def _post(client, path, community_id, csrf_token=None):
    headers = {'X-CSRF-Token': csrf_token} if csrf_token else {}
    return client.post(
        path,
        json={'community_id': community_id, 'temperature': 20, 'aqi': 40},
        headers=headers,
    )


def _assert_no_stored_stat_leak(response):
    body = response.get_data(as_text=True)
    for marker in LEAK_MARKERS:
        assert marker not in body


@pytest.fixture
def high_risk_community(db_session):
    community = Community(
        name='甲村',
        population=132,
        elderly_ratio=0.67,
        chronic_disease_ratio=0.22,
    )
    db_session.add(community)
    db_session.commit()
    return community


@pytest.fixture
def predictor(monkeypatch):
    service = _RecordingPredictor()
    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: service,
    )
    return service


@pytest.mark.parametrize('path', [
    '/api/v1/ml/predict-community',
    '/api/ml/predict-community',
])
def test_anonymous_predict_community_stays_login_gated(client, high_risk_community, predictor, path):
    csrf = 'anonymous-predict-community-csrf'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    response = _post(client, path, high_risk_community.id, csrf)

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
    _assert_no_stored_stat_leak(response)
    assert predictor.calls == []


@pytest.mark.parametrize('path', [
    '/api/v1/ml/predict-community',
    '/api/ml/predict-community',
])
def test_regular_user_cannot_read_other_community_stats(
    client, db_session, high_risk_community, predictor, path
):
    slug = path.strip('/').replace('/', '-')
    user = _user(db_session, f'regular-{slug}', 'user', authorized_community='甲村')
    csrf = _login_as(client, user)

    response = _post(client, path, high_risk_community.id, csrf)

    assert response.status_code == 403
    assert response.get_json()['success'] is False
    _assert_no_stored_stat_leak(response)
    assert predictor.calls == []


def test_community_officer_cannot_read_unassigned_community(
    client, db_session, high_risk_community, predictor
):
    user = _user(db_session, 'officer-other', 'community', authorized_community='乙村')
    csrf = _login_as(client, user)

    response = _post(client, '/api/v1/ml/predict-community', high_risk_community.id, csrf)

    assert response.status_code == 403
    _assert_no_stored_stat_leak(response)
    assert predictor.calls == []


@pytest.mark.parametrize('role, username, authorized', [
    ('community', 'officer-own', '甲村'),
    ('admin', 'predict-admin', None),
])
def test_authorized_user_can_predict_assigned_community(
    client, db_session, high_risk_community, predictor, role, username, authorized
):
    user = _user(db_session, username, role, authorized_community=authorized)
    csrf = _login_as(client, user)

    response = _post(client, '/api/v1/ml/predict-community', high_risk_community.id, csrf)

    payload = response.get_json()
    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['community'] == '甲村'
    assert payload['high_risk_groups'] == ['老年人群体']
    assert '加强对独居老人的健康巡访' in payload['recommendations']
    assert len(predictor.calls) == 1
    seen = predictor.calls[0]
    assert seen['name'] == '甲村'
    assert seen['elderly_ratio'] == pytest.approx(0.67)
    assert seen['chronic_disease_ratio'] == pytest.approx(0.22)
    assert seen['population'] == 132
