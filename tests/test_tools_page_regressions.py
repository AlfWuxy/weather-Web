# -*- coding: utf-8 -*-
"""工具页与用户端导航回归测试。"""


def _login_as(client, user_id: int, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = f'{user_id}:1'
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


def test_forecast_page_uses_qweather_only_data(client, db_session, monkeypatch):
    from datetime import timedelta

    from core.time_utils import today_local

    user = _create_user(db_session, username='forecast_qweather_user')
    _login_as(client, user.id)
    start = today_local()

    qweather_days = []
    for idx in range(7):
        day = start + timedelta(days=idx)
        qweather_days.append({
            'date': day.strftime('%Y-%m-%d'),
            'temperature_max': 24 + idx,
            'temperature_min': 14 + idx,
            'temperature_mean': 19 + idx,
            'condition': '阴' if idx == 1 else '多云',
            'condition_night': '中雨' if idx == 1 else '多云',
            'humidity': 72,
            'wind_speed': 3.2,
            'data_source': 'QWeather',
            'is_mock': False,
        })
    qweather_days[1]['temperature_max'] = 26
    qweather_days[1]['temperature_min'] = 18

    captured = {}

    def fake_qweather(location, days=7):
        captured['location'] = location
        captured['days'] = days
        return qweather_days, False, {
            'source': 'QWeather',
            'update_time': '2026-04-26T19:43+08:00',
        }

    class FakeForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            captured['start_date'] = start_date
            captured['context'] = context
            forecasts = []
            for idx, entry in enumerate(forecast_temps):
                day = start + timedelta(days=idx)
                forecasts.append({
                    'date': day.strftime('%Y-%m-%d'),
                    'probability_high_visits': 12 + idx,
                    'composite_exposure': {'score': 20 + idx, 'level': '低'},
                })
            return forecasts, {'recommendations': []}

    monkeypatch.setattr('blueprints.tools.get_qweather_forecast_with_cache', fake_qweather, raising=False)
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
    monkeypatch.setattr('blueprints.tools.get_forecast_service', lambda: FakeForecastService(), raising=False)

    response = client.get('/forecast-7day?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert captured['location'] == '都昌'
    assert captured['days'] == 7
    assert captured['start_date'] == start
    assert captured['context'] == {'pm25': 18.0, 'aqi': 42.0}
    assert '26° / 18°' in body
    assert '来源：和风天气' in body
    assert '2026-04-26 19:43' in body
    assert '34° / 26°' not in body
    assert 'value="都昌"' in body


def test_forecast_page_qweather_failure_does_not_render_demo_heat(client, db_session, monkeypatch):
    user = _create_user(db_session, username='forecast_qweather_fail_user')
    _login_as(client, user.id)

    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    response = client.get('/forecast-7day?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '7 天天气正在更新' in body
    assert '34° / 26°' not in body
    assert '35° / 27°' not in body


def test_forecast_api_default_uses_qweather_only_data(client, db_session, monkeypatch):
    from datetime import timedelta

    from core.time_utils import today_local

    user = _create_user(db_session, username='forecast_api_qweather_user')
    _login_as(client, user.id)
    start = today_local()

    qweather_days = []
    for idx in range(7):
        day = start + timedelta(days=idx)
        qweather_days.append({
            'date': day.strftime('%Y-%m-%d'),
            'temperature_max': 24 + idx,
            'temperature_min': 14 + idx,
            'temperature_mean': 19 + idx,
            'condition': '多云',
            'humidity': 70,
            'aqi': 42,
            'data_source': 'QWeather',
            'is_mock': False,
        })

    captured = {}

    def fake_qweather(location, days=7):
        captured['location'] = location
        captured['days'] = days
        return qweather_days, False, {'source': 'QWeather'}

    class FakeForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            captured['forecast_temps'] = forecast_temps
            captured['start_date'] = start_date
            captured['context'] = context
            return [
                {
                    'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
                    'composite_exposure': {'score': 22 + idx, 'level': '低'},
                }
                for idx in range(7)
            ], {'recommendations': [], 'high_risk_days': 0}

    monkeypatch.setattr('services.api_service.get_qweather_forecast_with_cache', fake_qweather, raising=False)
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda location: ({'temperature': 24, 'aqi': 42, 'pm25': 18, 'data_source': 'QWeather', 'is_mock': False}, False),
        raising=False,
    )
    monkeypatch.setattr('services.forecast_service.get_forecast_service', lambda: FakeForecastService(), raising=False)

    with client.session_transaction() as session:
        session['_csrf_token'] = 'forecast-csrf'

    response = client.post(
        '/api/forecast/7day',
        json={'city': '都昌'},
        headers={'X-CSRF-Token': 'forecast-csrf'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['data_source'] == 'QWeather'
    assert captured['location'] == '都昌'
    assert captured['days'] == 7
    assert captured['forecast_temps'] == qweather_days
    assert captured['start_date'] == start
    assert captured['context'] == {'aqi': 42, 'pm25': 18}


def test_comprehensive_alert_rejects_mock_current_weather(client, db_session, monkeypatch):
    user = _create_user(db_session, username='alert_mock_weather_user')
    _login_as(client, user.id)

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda location: ({'temperature': 36, 'is_mock': True}, False),
        raising=False,
    )

    with client.session_transaction() as session:
        session['_csrf_token'] = 'alert-csrf'

    response = client.post(
        '/api/alert/comprehensive',
        json={'city': '都昌'},
        headers={'X-CSRF-Token': 'alert-csrf'},
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload['error'] == 'weather_unavailable'


def test_comprehensive_alert_rejects_incomplete_qweather_forecast(client, db_session, monkeypatch):
    user = _create_user(db_session, username='alert_incomplete_forecast_user')
    _login_as(client, user.id)

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda location: ({'temperature': 24, 'aqi': 35, 'pm25': 12, 'data_source': 'QWeather', 'is_mock': False}, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.api_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )

    with client.session_transaction() as session:
        session['_csrf_token'] = 'alert-csrf'

    response = client.post(
        '/api/alert/comprehensive',
        json={'city': '都昌'},
        headers={'X-CSRF-Token': 'alert-csrf'},
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload['error'] == 'forecast_data_incomplete'


def test_comprehensive_alert_uses_qweather_forecast_with_today_start(client, db_session, monkeypatch):
    from datetime import timedelta

    from core.time_utils import today_local

    user = _create_user(db_session, username='alert_qweather_user')
    _login_as(client, user.id)
    start = today_local()
    qweather_days = [
        {
            'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
            'temperature_max': 25 + idx,
            'temperature_min': 15 + idx,
            'temperature_mean': 20 + idx,
            'condition': '多云',
            'humidity': 66,
            'aqi': 38,
            'data_source': 'QWeather',
            'is_mock': False,
        }
        for idx in range(7)
    ]
    captured = {}

    class FakeDlnmService:
        def calculate_rr(self, temperature):
            return 1.0, {}

        def identify_extreme_weather_events(self, temperature):
            return []

    class FakeForecastService:
        def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
            captured['forecast_temps'] = forecast_temps
            captured['start_date'] = start_date
            captured['context'] = context
            return [
                {
                    'date': (start + timedelta(days=idx)).strftime('%Y-%m-%d'),
                    'composite_exposure': {'score': 20 + idx, 'level': '低'},
                }
                for idx in range(7)
            ], {'high_risk_days': 0, 'recommendations': []}

    class FakeCommunityService:
        def generate_community_risk_map(self, current_weather):
            return {'summary': {'total': 0}, 'rankings': []}

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda location: ({'temperature': 24, 'aqi': 38, 'pm25': 14, 'data_source': 'QWeather', 'is_mock': False}, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.api_service.get_qweather_forecast_with_cache',
        lambda location, days=7: (qweather_days, False, {'source': 'QWeather'}),
        raising=False,
    )
    monkeypatch.setattr('services.dlnm_risk_service.get_dlnm_service', lambda: FakeDlnmService(), raising=False)
    monkeypatch.setattr('services.forecast_service.get_forecast_service', lambda: FakeForecastService(), raising=False)
    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: FakeCommunityService(), raising=False)

    with client.session_transaction() as session:
        session['_csrf_token'] = 'alert-csrf'

    response = client.post(
        '/api/alert/comprehensive',
        json={'city': '都昌'},
        headers={'X-CSRF-Token': 'alert-csrf'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['alert']['text'] == '蓝色预警'
    assert captured['forecast_temps'] == qweather_days
    assert captured['start_date'] == start
    assert captured['context'] == {'aqi': 38, 'pm25': 14}


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
    captured = {}

    class FakeMLService:
        def predict_disease_risk(self, user_info, weather_info=None):
            captured['user_info'] = user_info
            return {
                'success': True,
                'predictions': [
                    {'disease': '高血压', 'probability': 0.812, 'original_probability': 0.70, 'weather_multiplier': 1.16},
                    {'disease': '支气管炎', 'probability': 0.421, 'original_probability': 0.40, 'weather_multiplier': 1.0525},
                ],
                'risk_factors': [
                    '高温天气增加心血管负担',
                    '湿度偏高可能放大呼吸系统不适',
                ],
            }

    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: ({'temperature': 31, 'humidity': 68, 'data_source': 'QWeather', 'is_mock': False}, False),
    )
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
    assert '本次天气调整关注分' in body
    assert '关注排序第 1' in body
    assert '81.2/100' in body
    assert 'data-metric-context=' in body
    assert '70.0%' in body
    assert '高血压' in body
    assert 'Method Not Allowed' not in body
    assert 'value="72"' in body
    assert 'value="都昌"' in body
    assert 'name="chronic"' not in body
    assert '慢病档案不会参与这项类别排序' in body
    assert captured['user_info'] == {'age': 72, 'gender': '男'}


def test_ml_prediction_selected_member_uses_age_and_gender_only(client, db_session, monkeypatch):
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
    captured = {}

    class FakeMLService:
        def predict_disease_risk(self, user_info, weather_info=None):
            captured['user_info'] = user_info
            return {
                'success': True,
                'predictions': [{'disease': '支气管炎', 'probability': 0.52}],
                'risk_factors': ['高温天气增加呼吸负担'],
            }

    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: ({'temperature': 30, 'humidity': 66, 'data_source': 'QWeather', 'is_mock': False}, False),
    )
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
    assert f'<option value="{member.id}" selected>' in body
    assert 'name="chronic"' not in body
    assert captured['user_info'] == {'age': 74, 'gender': '女'}


def test_chronic_risk_post_no_longer_returns_405(client, db_session, monkeypatch):
    user = _create_user(db_session, username='chronic_user')
    _login_as(client, user.id)
    captured = {}

    class FakeChronicService:
        def predict_individual_risk(self, user_info, weather_data, target_diseases=None):
            captured['user_info'] = user_info
            return {
                'overall_risk': {'score': 87.3, 'level': '高风险'},
                'disease_risks': {
                    'cardiovascular': {
                        'risk_score': 87.3,
                        'risk_level': '高风险',
                        'raw_dlnm_rr': 1.2,
                        'dlnm_disease_modifier': 1.1,
                        'dlnm_age_modifier': 1.3,
                        'dlnm_adjusted_rr': 1.716,
                        'dlnm_rr_cap': 3.5,
                        'chronic_age_amplifier': 1.1,
                        'comorbidity_amplifier': 1.4,
                        'personal_rr': 2.643,
                        'vital_adjustment': 8,
                    },
                    'respiratory': {
                        'risk_score': 34,
                        'risk_level': '低风险',
                        'raw_dlnm_rr': 1.05,
                        'dlnm_disease_modifier': 1.0,
                        'dlnm_age_modifier': 1.0,
                        'dlnm_adjusted_rr': 1.05,
                        'dlnm_rr_cap': 3.5,
                        'chronic_age_amplifier': 1.0,
                        'comorbidity_amplifier': 1.0,
                        'personal_rr': 1.05,
                    },
                },
                'recommendations': [{'advice': '按时服药'}, {'advice': '本周内复诊'}],
                'vital_adjustment': {
                    'score_adjustment': 8,
                    'factors': ['近7天最高收缩压142mmHg，血压略高'],
                    'recommendations': ['建议连续记录血压']
                },
            }

    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: ({'temperature': 32, 'humidity': 70, 'data_source': 'QWeather', 'is_mock': False}, False),
    )
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
    assert 'Raw DLNM RR 1.2' in body
    assert 'DLNM病种修正 ×1.1' in body
    assert 'DLNM年龄修正 ×1.3' in body
    assert '慢病层年龄修正 ×1.1' in body
    assert '共病修正 ×1.4' in body
    assert 'Personal RR 2.643' in body
    assert 'DLNM内层：1.2 × 1.1 × 1.3，上限 3.5 未触发，得 1.716' in body
    assert '生命体征修正 +8.0' in body
    assert '近7天最高收缩压142mmHg' in body
    assert captured['user_info']['vitals'] == {'sbp': 142.0, 'fbg': 7.8}


def test_ml_and_chronic_pages_reject_mock_weather(client, db_session, monkeypatch):
    user = _create_user(db_session, username='tool_mock_weather_user')
    _login_as(client, user.id)

    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: ({'temperature': 37, 'humidity': 70, 'data_source': 'Demo', 'is_mock': True}, False),
    )

    class UnexpectedService:
        def __getattr__(self, _name):
            raise AssertionError('模拟天气不应进入风险服务')

    monkeypatch.setattr('blueprints.tools.get_ml_service', lambda: UnexpectedService())
    monkeypatch.setattr('blueprints.tools.get_chronic_service', lambda: UnexpectedService())

    ml_response = client.post(
        '/ml-prediction',
        data={'location': '都昌', 'age': '72', 'csrf_token': 'test-csrf-token'},
        follow_redirects=True,
    )
    chronic_response = client.post(
        '/chronic-risk',
        data={'disease': 'hypertension', 'csrf_token': 'test-csrf-token'},
        follow_redirects=True,
    )

    assert ml_response.status_code == 200
    assert chronic_response.status_code == 200
    assert '健康关注线索暂时无法生成' in ml_response.get_data(as_text=True)
    assert '天气正在更新，本次提醒暂未生成' in chronic_response.get_data(as_text=True)
    assert '模拟值不会进入' not in ml_response.get_data(as_text=True)
    assert '模拟值不会进入' not in chronic_response.get_data(as_text=True)
    assert '本次天气调整关注分' not in ml_response.get_data(as_text=True)
    assert '综合风险评分' not in chronic_response.get_data(as_text=True)


def test_chronic_risk_get_shows_empty_state_without_synthetic_result(client, db_session):
    user = _create_user(db_session, username='chronic_empty_user')
    _login_as(client, user.id)

    response = client.get('/chronic-risk')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '填写信息后生成评估' in body
    assert '可查看风险提示与行动建议' in body
    assert '示例评分或示例医疗建议' not in body
    assert '>58<' not in body
    assert '本周内到社区医生处复诊' not in body
    assert '综合当前数据,控制偏向偏松' not in body
    assert '血压波动' not in body


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


def test_cooling_page_empty_database_does_not_render_default_resources(client, db_session, monkeypatch):
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )

    response = client.get('/cooling?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '暂无录入的避暑资源' in body
    assert '都昌县图书馆' not in body
    assert '万达广场' not in body
    assert '人民公园纳凉亭' not in body


def test_cooling_page_renders_real_resources_only(client, db_session, monkeypatch):
    from core.db_models import CoolingResource

    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )
    db_session.add(CoolingResource(
        community_code='都昌',
        name='真实图书馆',
        resource_type='图书馆',
        address_hint='真实路 1 号',
        open_hours='09:00-18:00',
        has_ac=True,
        is_accessible=True,
        contact_hint='服务台登记',
        notes='仅展示真实录入信息',
        is_active=True,
    ))
    db_session.commit()

    response = client.get('/cooling?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '真实图书馆' in body
    assert '真实路 1 号' in body
    assert '09:00-18:00' in body
    assert '服务台登记' in body
    assert '仅展示真实录入信息' in body
    assert '距你' not in body
    assert '都昌县图书馆' not in body
    assert '万达广场' not in body


def test_cooling_resource_type_filter_accepts_legacy_type_alias(client, db_session, monkeypatch):
    from core.db_models import CoolingResource

    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )
    db_session.add_all([
        CoolingResource(
            community_code='都昌',
            name='真实图书馆',
            resource_type='图书馆',
            address_hint='真实路 1 号',
            is_active=True,
        ),
        CoolingResource(
            community_code='都昌',
            name='真实商场',
            resource_type='商场',
            address_hint='商业路 2 号',
            is_active=True,
        ),
    ])
    db_session.commit()

    response = client.get('/cooling?type=图书馆')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '真实图书馆' in body
    assert '真实商场' not in body
    assert 'name="resource_type"' in body
    assert '<option value="图书馆" selected>' in body


def test_cooling_resource_type_takes_precedence_over_legacy_type(client, db_session, monkeypatch):
    from core.db_models import CoolingResource

    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )
    db_session.add_all([
        CoolingResource(
            community_code='都昌',
            name='真实图书馆',
            resource_type='图书馆',
            address_hint='真实路 1 号',
            is_active=True,
        ),
        CoolingResource(
            community_code='都昌',
            name='真实商场',
            resource_type='商场',
            address_hint='商业路 2 号',
            is_active=True,
        ),
    ])
    db_session.commit()

    response = client.get('/cooling?resource_type=商场&type=图书馆')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '真实商场' in body
    assert '真实图书馆' not in body
    assert '<option value="商场" selected>' in body


def test_cooling_community_filter_supports_new_and_legacy_query_names(client, db_session, monkeypatch):
    from core.db_models import CoolingResource

    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )
    db_session.add_all([
        CoolingResource(
            community_code='甲村',
            name='甲村纳凉点',
            resource_type='活动中心',
            is_active=True,
        ),
        CoolingResource(
            community_code='乙村',
            name='乙村纳凉点',
            resource_type='活动中心',
            is_active=True,
        ),
    ])
    db_session.commit()

    response = client.get('/cooling?community=甲村')
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '甲村纳凉点' in body
    assert '乙村纳凉点' not in body
    assert 'name="community"' in body
    assert 'value="甲村"' in body

    legacy_response = client.get('/cooling?location=乙村')
    legacy_body = legacy_response.get_data(as_text=True)
    assert legacy_response.status_code == 200
    assert '乙村纳凉点' in legacy_body
    assert '甲村纳凉点' not in legacy_body
    assert 'name="community"' in legacy_body
    assert 'value="乙村"' in legacy_body
