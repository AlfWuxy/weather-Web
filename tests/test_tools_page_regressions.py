# -*- coding: utf-8 -*-
"""工具页与用户端导航回归测试。"""


def _login_as(client, user_id: int, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username='tooluser', role='user'):
    from core.db_models import User

    user = User(username=username, role=role)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def test_forecast_page_loads_chartjs(client, db_session):
    user = _create_user(db_session, username='forecast_user')
    _login_as(client, user.id)

    response = client.get('/forecast-7day')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="forecastChart"' in body
    assert '/static/vendor/chartjs/chart.umd.min.js' in body


def test_authenticated_nav_uses_desktop_mega_menu(client, db_session):
    user = _create_user(db_session, username='nav_user')
    _login_as(client, user.id)

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="appMegaMenu"' in body
    assert 'data-nav-more-trigger="desktop"' in body
    assert 'AI 疾病预测' in body
    assert 'AI 提问' in body
    assert '健康评估' in body
    assert '家庭成员' in body


def test_ml_prediction_post_renders_result_and_preserves_form(client, db_session, monkeypatch):
    user = _create_user(db_session, username='ml_user')
    _login_as(client, user.id)

    class FakeMLService:
        def predict_disease_risk(self, user_info, weather_info=None):
            return {
                'success': True,
                'predictions': [
                    {'disease': '高血压', 'probability': 0.812, 'percentage': '81.2%'},
                    {'disease': '支气管炎', 'probability': 0.421, 'percentage': '42.1%'},
                ],
                'risk_factors': [
                    '高温天气增加心血管负担',
                    '湿度偏高可能放大呼吸系统不适',
                ],
            }

    monkeypatch.setattr('blueprints.tools.get_ml_service', lambda: FakeMLService())

    response = client.post(
        '/ml-prediction',
        data={
            'location': '都昌',
            'age': '72',
            'chronic': ['高血压', '糖尿病'],
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '未来 3 天风险预测' in body
    assert '高血压' in body
    assert 'Method Not Allowed' not in body
    assert 'value="72"' in body
    assert 'value="都昌"' in body
    assert '<option value="高血压" selected>' in body
    assert '<option value="糖尿病" selected>' in body


def test_ml_prediction_selected_member_chronic_aliases_refill_selected(client, db_session, monkeypatch):
    import json
    from core.db_models import FamilyMember

    user = _create_user(db_session, username='ml_member_user')
    member = FamilyMember(
        user_id=user.id,
        name='母亲',
        relation='母亲',
        age=74,
        gender='女',
        chronic_diseases=json.dumps(['慢性阻塞性肺病', '脑卒中史', '关节炎'], ensure_ascii=False),
    )
    db_session.add(member)
    db_session.commit()
    _login_as(client, user.id)

    class FakeMLService:
        def predict_disease_risk(self, user_info, weather_info=None):
            return {
                'success': True,
                'predictions': [{'disease': '支气管炎', 'probability': 0.52}],
                'risk_factors': ['高温天气增加呼吸负担'],
            }

    monkeypatch.setattr('blueprints.tools.get_ml_service', lambda: FakeMLService())

    response = client.post(
        '/ml-prediction',
        data={
            'member_id': str(member.id),
            'location': '都昌',
            'age': '',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'value="74"' in body
    assert '<option value="慢阻肺" selected>' in body
    assert '<option value="脑卒中" selected>' in body
    assert '<option value="关节炎" selected>' in body


def test_chronic_risk_post_no_longer_returns_405(client, db_session, monkeypatch):
    user = _create_user(db_session, username='chronic_user')
    _login_as(client, user.id)
    captured = {}

    class FakeChronicService:
        def predict_individual_risk(self, user_info, weather_data, target_diseases=None):
            captured['user_info'] = user_info
            return {
                'overall_risk': {'score': 66, 'level': '中风险'},
                'disease_risks': {
                    'cardiovascular': {'risk_score': 66, 'risk_level': '中风险', 'vital_adjustment': 8},
                    'respiratory': {'risk_score': 34, 'risk_level': '低风险'},
                },
                'recommendations': [{'advice': '按时服药'}, {'advice': '本周内复诊'}],
                'vital_adjustment': {
                    'score_adjustment': 8,
                    'factors': ['近7天最高收缩压142mmHg，血压略高'],
                    'recommendations': ['建议连续记录血压']
                },
            }

    monkeypatch.setattr('blueprints.tools.get_chronic_service', lambda: FakeChronicService())

    response = client.post(
        '/chronic-risk',
        data={
            'disease': 'hypertension',
            'sbp': '142',
            'fbg': '7.8',
            'adherence': 'loose',
            'symptoms': '头晕',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Method Not Allowed' not in body
    assert '综合风险评分' in body
    assert '按时服药' in body
    assert '血压/血糖修正' in body
    assert '近7天最高收缩压142mmHg' in body
    assert captured['user_info']['vitals'] == {'sbp': 142.0, 'fbg': 7.8}


def test_chronic_risk_service_uses_submitted_vitals():
    from services.chronic_risk_service import ChronicRiskService

    service = ChronicRiskService()
    weather = {'temperature': 24, 'humidity': 60, 'aqi': 45}
    base = service.predict_individual_risk(
        {'age': 45, 'gender': '男', 'chronic_diseases': [], 'vitals': {'sbp': 120, 'fbg': 5.2}},
        weather,
        target_diseases=['general'],
    )
    high = service.predict_individual_risk(
        {'age': 45, 'gender': '男', 'chronic_diseases': [], 'vitals': {'sbp': 178, 'fbg': 9.2}},
        weather,
        target_diseases=['general'],
    )

    assert high['overall_risk']['score'] > base['overall_risk']['score']
    assert high['vital_adjustment']['score_adjustment'] > base['vital_adjustment']['score_adjustment']
