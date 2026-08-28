# -*- coding: utf-8 -*-
"""工具页与用户端导航回归测试。"""

import pytest


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


def _trusted_qweather_current(
    temperature=30,
    humidity=68,
    *,
    aqi=None,
    pm25=None,
):
    from core.time_utils import utcnow

    observed_at = utcnow().isoformat()
    air_available = aqi is not None and pm25 is not None
    return {
        'temperature': temperature,
        'temperature_max': temperature + 4,
        'temperature_min': temperature - 5,
        'humidity': humidity,
        'pressure': 1005,
        'wind_speed': 2.5,
        'weather_condition': '多云',
        'observed_at': observed_at,
        'quality_version': 1,
        'aqi': aqi,
        'pm25': pm25,
        'air_observed_at': observed_at if air_available else None,
        'air_quality_available': air_available,
        'data_source': 'QWeather',
        'is_mock': False,
    }


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
        lambda _location: (
            _trusted_qweather_current(27, aqi=42, pm25=18),
            False,
        ),
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
    monkeypatch.setattr(
        'blueprints.tools.get_openmeteo_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'cache_miss'}),
        raising=False,
    )

    response = client.get('/forecast-7day?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '7 天天气正在更新' in body
    assert '34° / 26°' not in body
    assert '35° / 27°' not in body


def test_forecast_page_reads_openmeteo_cache_without_health_model(
    client,
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from core.time_utils import today_local, utcnow

    user = _create_user(db_session, username='forecast_openmeteo_user')
    _login_as(client, user.id)
    start = today_local()
    openmeteo_days = [
        {
            'date': (start + timedelta(days=index)).isoformat(),
            'forecast_date': (start + timedelta(days=index)).isoformat(),
            'temperature_max': 31 + index,
            'temperature_min': 22 + index,
            'temperature_mean': 26.5 + index,
            'condition': '多云',
            'precip_probability': 20,
            'humidity': None,
            'wind_speed': None,
            'data_source': 'Open-Meteo',
            'is_mock': False,
        }
        for index in range(7)
    ]
    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'cache_miss'}),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_openmeteo_forecast_with_cache',
        lambda location, days=7: (
            openmeteo_days,
            True,
            {'source': 'Open-Meteo', 'fetched_at': utcnow().isoformat()},
        ),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda *_args, **_kwargs: pytest.fail('Open-Meteo 基础预报不得进入健康模型'),
        raising=False,
    )
    monkeypatch.setattr(
        'blueprints.tools.get_forecast_service',
        lambda: pytest.fail('Open-Meteo 基础预报不得创建健康预测服务'),
    )

    response = client.get('/forecast-7day?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '来源：Open-Meteo' in body
    assert '31° / 22°' in body
    assert '健康关注分待计算' in body
    assert '7 天天气正在更新' not in body


def test_forecast_page_stale_qweather_only_renders_weather_cards(
    client,
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from core.time_utils import today_local

    user = _create_user(db_session, username='forecast_stale_qweather_user')
    _login_as(client, user.id)
    start = today_local()
    qweather_days = [
        {
            'date': (start + timedelta(days=index)).isoformat(),
            'temperature_max': 30 + index,
            'temperature_min': 20 + index,
            'temperature_mean': 25 + index,
            'condition': '多云',
            'humidity': 70,
            'wind_speed': 2.0,
            'data_source': 'QWeather',
            'is_mock': False,
        }
        for index in range(7)
    ]
    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days=7: (qweather_days, True, {'source': 'QWeather', 'stale': True}),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda *_args, **_kwargs: pytest.fail('陈旧预报不得读取健康模型输入'),
    )
    monkeypatch.setattr(
        'blueprints.tools.get_forecast_service',
        lambda: pytest.fail('陈旧预报不得创建健康预测服务'),
    )

    response = client.get('/forecast-7day?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '来源：和风天气' in body
    assert '30° / 20°' in body
    assert '健康关注分待计算' in body


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
        lambda location: (
            _trusted_qweather_current(24, aqi=42, pm25=18),
            False,
        ),
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


def test_forecast_api_rejects_stale_qweather_forecast(client, db_session, monkeypatch):
    user = _create_user(db_session, username='forecast_api_stale_user')
    _login_as(client, user.id)
    monkeypatch.setattr(
        'services.api_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([{'temperature_mean': 25}] * 7, True, {'stale': True}),
    )
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda *_args, **_kwargs: pytest.fail('陈旧预报必须在读取健康上下文前拒绝'),
    )
    with client.session_transaction() as session:
        session['_csrf_token'] = 'forecast-stale-csrf'

    response = client.post(
        '/api/forecast/7day',
        json={'city': '都昌'},
        headers={'X-CSRF-Token': 'forecast-stale-csrf'},
    )

    assert response.status_code == 503
    assert response.get_json()['error'] == 'forecast_stale'


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
        lambda location: (
            _trusted_qweather_current(24, aqi=35, pm25=12),
            False,
        ),
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
            ], {
                'high_risk_days': None,
                'model_warning_status': 'disabled_uncalibrated',
                'recommendations': [],
            }

    class FakeCommunityService:
        def generate_community_risk_map(self, current_weather):
            return {'summary': {'total': 0}, 'rankings': []}

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda location: (
            _trusted_qweather_current(24, aqi=38, pm25=14),
            False,
        ),
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
    assert payload['alert']['available'] is False
    assert payload['alert']['level'] == 'unavailable'
    assert payload['alert']['text'] == 'disabled_unvalidated'
    assert payload['alert']['status'] == 'disabled_unvalidated'
    assert '官方天气预警' in payload['alert']['message']
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
    assert '健康关注线索' in body
    assert 'AI 疾病预测' not in body
    assert 'AI 提问' not in body
    assert 'data-nav-key="ml-prediction"' not in body
    assert 'data-nav-key="ai-qa"' not in body
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
        lambda _location: (_trusted_qweather_current(31, 68), False),
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
        lambda _location: (_trusted_qweather_current(30, 66), False),
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
        lambda _location: (_trusted_qweather_current(32, 70), False),
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


def test_cooling_page_empty_database_renders_safe_candidate_preview(
    app,
    client,
    db_session,
    monkeypatch,
):
    import json
    import re

    from core.db_models import CoolingResource

    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )
    app.config['AMAP_JS_API_KEY'] = 'j' * 32
    assert CoolingResource.query.count() == 0

    response = client.get('/cooling?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert CoolingResource.query.count() == 0
    assert '暂无已发布且完成核验的避暑资源' in body
    assert '待核验场所预览' in body
    assert '都昌县图书馆' in body
    assert '都昌志愿服务联合会' in body
    assert '待人工核验' in body
    assert '不代表当天开放、具备空调、允许公众纳凉或已获本项目推荐' in body
    assert '都昌县人民医院' not in body
    assert '左里中心卫生院' not in body
    assert '万达广场' not in body
    assert '人民公园纳凉亭' not in body
    assert 'B03180SL06' not in body
    assert '116.187665' not in body
    assert '29.249263' not in body
    assert 'data-publication-status="candidate-only"' in body
    assert 'data-verification-status="pending-human-verification"' in body
    assert body.count('data-cooling-candidate="pending"') == 7
    assert '筛选将在人工核验发布后开放，待核验预览不参与筛选' in body
    assert re.search(r'id="coolingCommunity"[^>]*\sdisabled(?:\s|>)', body)
    assert re.search(r'id="coolingResourceType"[^>]*\sdisabled(?:\s|>)', body)
    assert re.search(r'id="coolingFilterSubmit"[^>]*\sdisabled(?:\s|>)', body)
    assert 'data-cooling-map-focus' not in body
    assert 'id="coolingLocateButton"' in body
    assert 'id="coolingLocateButton"\n                    disabled' in body
    assert 'geolocation=()' in response.headers['Permissions-Policy']
    match = re.search(
        r'<script id="coolingMapData" type="application/json">(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert match is not None
    assert json.loads(match.group(1)) == []


def test_cooling_page_renders_real_resources_only(client, db_session, monkeypatch):
    import re

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
    assert '待核验场所预览' not in body
    assert '筛选将在人工核验发布后开放，待核验预览不参与筛选' not in body
    assert not re.search(r'id="coolingCommunity"[^>]*\sdisabled(?:\s|>)', body)
    assert not re.search(r'id="coolingResourceType"[^>]*\sdisabled(?:\s|>)', body)
    assert not re.search(r'id="coolingFilterSubmit"[^>]*\sdisabled(?:\s|>)', body)


def test_cooling_location_button_meets_minimum_touch_target():
    import re
    from pathlib import Path

    stylesheet = (
        Path(__file__).resolve().parents[1]
        / 'static'
        / 'css'
        / 'cooling-map.css'
    ).read_text(encoding='utf-8')
    rule = re.search(
        r'\.cooling-locate-button\s*\{(?P<body>.*?)\}',
        stylesheet,
        re.DOTALL,
    )

    assert rule is not None
    assert re.search(r'min-height:\s*44px', rule.group('body'))
    assert re.search(r'min-width:\s*44px', rule.group('body'))


def test_public_cooling_candidates_reject_unapproved_category(
    monkeypatch,
):
    from blueprints import public as public_blueprint

    payload = {
        'publication_status': 'candidate_only',
        'coordinate_system': 'GCJ-02',
        'items': [{
            'name': '异常医院候选',
            'category': 'hospital',
            'public_role': 'cooling_candidate',
            'address': '测试地址',
            'opening_hours_hint': '全天',
            'verification_status': 'pending_human_verification',
            'is_active': False,
        }],
    }
    monkeypatch.setattr(
        public_blueprint,
        '_read_versioned_public_json',
        lambda _path: payload,
    )

    assert public_blueprint._public_cooling_candidates() == []


def test_cooling_map_only_serializes_current_verified_gcj02_points(
    client,
    app,
    db_session,
    monkeypatch,
):
    import json
    import re
    from datetime import timedelta

    from core.db_models import CoolingResource
    from core.time_utils import utcnow

    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: (
            {
                'temperature': 27.5,
                'is_mock': False,
                'data_source': 'QWeather',
            },
            False,
        ),
    )
    app.config['AMAP_JS_API_KEY'] = 'j' * 32
    app.config['AMAP_SECURITY_JS_CODE'] = 's' * 32
    app.config['AMAP_WEB_SERVICE_KEY'] = 'server-web-key-that-must-stay-private'
    app.config['COOLING_COORDINATE_VERIFICATION_TTL_DAYS'] = 365
    db_session.add_all([
        CoolingResource(
            community_code='都昌',
            name='有效核验点',
            resource_type='社区服务中心',
            address_hint='核验路 1 号',
            latitude=29.27,
            longitude=116.20,
            coordinate_system='GCJ-02',
            coordinate_source='管理员现场使用微信地图人工核对',
            coordinate_verified_at=utcnow() - timedelta(days=1),
            is_active=True,
        ),
        CoolingResource(
            community_code='都昌',
            name='过期核验点',
            resource_type='社区服务中心',
            address_hint='过期路 2 号',
            latitude=29.28,
            longitude=116.21,
            coordinate_system='GCJ-02',
            coordinate_source='一年前的人工核验记录',
            coordinate_verified_at=utcnow() - timedelta(days=366),
            is_active=True,
        ),
        CoolingResource(
            community_code='都昌',
            name='来源缺失点',
            resource_type='社区服务中心',
            latitude=29.29,
            longitude=116.22,
            coordinate_system='GCJ-02',
            coordinate_verified_at=utcnow(),
            is_active=True,
        ),
    ])
    db_session.commit()

    response = client.get('/cooling?location=都昌')
    body = response.get_data(as_text=True)
    match = re.search(
        r'<script id="coolingMapData" type="application/json">(.*?)</script>',
        body,
        re.DOTALL,
    )

    assert response.status_code == 200
    assert match is not None
    points = json.loads(match.group(1))
    assert [point['name'] for point in points] == ['有效核验点']
    assert points[0]['coordinate_system'] == 'GCJ-02'
    assert '过期核验点' in body
    assert '来源缺失点' in body
    assert 'server-web-key-that-must-stay-private' not in body
    assert 'geolocation=(self)' in response.headers['Permissions-Policy']
    assert 'data-cooling-map-focus' in body


def test_cooling_location_is_click_only_and_cleared_on_page_exit():
    import re
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1]
        / 'static'
        / 'js'
        / 'cooling-map.js'
    ).read_text(encoding='utf-8')

    assert "locateButton.addEventListener('click', requestOneTimeLocation)" in script
    assert 'navigator.geolocation.getCurrentPosition(' in script
    assert 'wgs84ToGcj02' in script
    assert 'AMap.convertFrom' not in script
    assert "window.addEventListener('pagehide', clearEphemeralLocation)" in script
    assert "window.addEventListener('beforeunload', clearEphemeralLocation)" in script
    assert 'window.fetch' not in script
    assert 'localStorage' not in script
    assert 'sessionStorage' not in script
    assert 'userMarker' not in script
    assert 'new window.AMap.Marker({' in script
    assert 'position: [point.lng, point.lat]' in script
    assert script.count('map.setFitView(') == 1
    assert 'map.setFitView(Array.from(markerById.values())' in script
    assert 'map.setCenter(' not in script
    assert 'map.setZoomAndCenter(15, marker.getPosition())' in script
    assert '不上传至本项目服务器或保存' in script

    location_handler = re.search(
        r'function applyConvertedLocation\(location\) \{(.*?)\n    \}\n\n'
        r'    function convertBrowserLocation',
        script,
        re.DOTALL,
    )
    assert location_handler is not None
    handler_source = location_handler.group(1)
    assert 'const userPoint' in handler_source
    assert 'nearestPoint(userPoint)' in handler_source
    assert 'openPoint(nearest.point)' in handler_source
    assert 'window.AMap' not in handler_source
    assert 'map.' not in handler_source


def test_cooling_page_explains_local_only_location_boundary(
    client,
    app,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: (
            {
                'temperature': 27.5,
                'is_mock': False,
                'data_source': 'QWeather',
            },
            False,
        ),
    )
    app.config['AMAP_JS_API_KEY'] = 'j' * 32

    response = client.get('/cooling')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '地图只接收已核验的公开资源点' in body
    assert '精确位置仅在本页内存计算距离' in body
    assert '不上传至本项目服务器或保存' in body
    assert '也不会用于地图打点' in body


def test_non_cooling_pages_keep_geolocation_disabled(client):
    response = client.get('/')

    assert response.status_code == 200
    assert 'geolocation=()' in response.headers['Permissions-Policy']
    assert 'geolocation=(self)' not in response.headers['Permissions-Policy']


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
