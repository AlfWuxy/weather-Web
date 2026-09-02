# -*- coding: utf-8 -*-
import json

import pytest

from core.db_models import Community, HealthRiskAssessment, MedicalRecord, User
from core.time_utils import utcnow
from utils.parsers import safe_json_loads


def _qweather_payload():
    return {
        'temperature': 31,
        'temperature_max': 34,
        'temperature_min': 26,
        'humidity': 68,
        'pressure': 1006,
        'weather_condition': '多云',
        'wind_speed': 2.5,
        'pm25': 38,
        'aqi': 62,
        'data_source': 'QWeather',
        'is_mock': False,
        'is_demo': False,
    }


def _seed_health_assessment_user(db_session):
    db_session.add(Community(
        name='测试社区',
        population=1200,
        elderly_ratio=0.34,
        chronic_disease_ratio=0.16,
        vulnerability_index=58.0,
        risk_level='中'
    ))
    user = User.query.filter_by(username='testuser').first()
    user.age = 72
    user.gender = '男'
    user.community = '测试社区'
    user.has_chronic_disease = True
    user.chronic_diseases = json.dumps(['高血压', '慢性支气管炎'], ensure_ascii=False)
    db_session.add(MedicalRecord(
        patient_name='测试病例',
        visit_time=utcnow(),
        community='测试社区'
    ))
    db_session.commit()


def test_health_assessment_page_has_screening_controls(authenticated_client, db_session):
    _seed_health_assessment_user(db_session)

    response = authenticated_client.get('/health-assessment')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert '健康风险评估' in html
    assert '即时状态筛查' in html
    assert 'name="outdoor_exposure"' in html
    assert 'name="symptom_level"' in html
    assert 'name="hydration"' in html
    assert 'name="medication_adherence"' in html
    assert 'name="sleep_quality"' in html
    assert 'type="radio" name="outdoor_exposure"' in html
    assert 'class="btn btn-outline-secondary assess-choice"' in html
    assert 'function syncGroup(name)' in html
    assert "style.background = 'var(--yl-orange-500)'" not in html


def test_health_assessment_post_persists_academic_payload(
    authenticated_client,
    db_session,
    monkeypatch
):
    _seed_health_assessment_user(db_session)
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_qweather_payload(), False)
    )

    response = authenticated_client.post(
        '/health-assessment',
        data={
            'outdoor_exposure': 'high',
            'symptom_level': 'moderate',
            'hydration': 'poor',
            'medication_adherence': 'partial',
            'sleep_quality': 'poor',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '最新评估结果' in html
    assert '评估依据' in html
    assert '风险分布' in html

    assessment = HealthRiskAssessment.query.order_by(HealthRiskAssessment.id.desc()).first()
    assert assessment is not None
    assert assessment.risk_score is not None
    assert assessment.risk_level in ['低风险', '中风险', '高风险']

    explain_payload = safe_json_loads(assessment.explain, {})
    assert 'academic_profile' in explain_payload

    academic = explain_payload['academic_profile']
    assert 'risk_interval' in academic
    assert 'risk_probabilities' in academic
    assert 'cap_semantics' in academic
    assert 'impact_likelihood' in academic
    assert 'model_paths' in academic
    assert len(academic['model_paths']) == 4
    assert 'fusion_breakdown' in academic
    assert abs(
        sum(path['contribution'] for path in academic['model_paths'])
        - academic['fusion_breakdown']['final_score']
    ) <= 0.2
    assert 'impact_score' in academic['impact_likelihood']
    assert 'likelihood_score' in academic['impact_likelihood']
    assert 'component_scores' in academic
    assert 'community_context' in academic
    community_context = academic['community_context']
    assert community_context['community'] == '测试社区'
    assert community_context['source'] == 'community_table'
    assert community_context['vulnerability_source'] == 'community_table'
    assert community_context['vulnerability_index'] == 58.0
    assert community_context['cases_30d'] == 1
    assert community_context['burden_available'] is True
    assert community_context['burden_per_1000'] == pytest.approx(0.833, abs=0.001)
    assert community_context['imputed'] is False
    assert '所在社区的参考情况' in html
    assert '社区资料' in html
    assert '社区脆弱性' in html
    assert '1 条' in html
    assert '0.833 条 / 千人' in html
    assert '路径 C' not in html
    assert '代理值' not in html
    assert 'methodology' in academic
    assert len(academic['methodology']) >= 4
    assert academic['weather']['weather_condition'] == '多云'
    assert '多云' in html

    disease_risks = safe_json_loads(assessment.disease_risks, {})
    assert isinstance(disease_risks, dict)


@pytest.mark.parametrize(
    'weather_data',
    [
        {
            'temperature': 37,
            'humidity': 70,
            'data_source': 'Demo',
            'is_demo': True,
        },
        {
            'temperature': 30,
            'humidity': 70,
            'data_source': 'Mock',
            'is_mock': True,
        },
        {
            'temperature': 30,
            'humidity': 70,
            'data_source': 'LocalFallback',
        },
        {
            'temperature': None,
            'humidity': 70,
            'data_source': 'QWeather',
        },
        {
            'temperature': 30,
            'humidity': None,
            'data_source': 'QWeather',
            'is_mock': False,
        },
    ],
    ids=['demo', 'mock', 'non-qweather', 'missing-temperature', 'missing-humidity']
)
def test_health_assessment_post_waits_for_real_weather_without_side_effects(
    authenticated_client,
    db_session,
    app,
    monkeypatch,
    weather_data
):
    _seed_health_assessment_user(db_session)
    app.config['FEATURE_NOTIFICATIONS'] = True
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (weather_data, False)
    )

    def unexpected_assessment(*_args, **_kwargs):
        raise AssertionError('无效天气不应进入评估服务')

    def unexpected_notification(*_args, **_kwargs):
        raise AssertionError('无效天气不应发送通知')

    monkeypatch.setattr(
        'services.health_risk_service.HealthRiskService.assess_personal_weather_health_risk',
        unexpected_assessment
    )
    monkeypatch.setattr(
        'services.user.profile_service.create_notification',
        unexpected_notification
    )

    response = authenticated_client.post(
        '/health-assessment',
        data={
            'outdoor_exposure': 'medium',
            'symptom_level': 'none',
            'hydration': 'normal',
            'medication_adherence': 'good',
            'sleep_quality': 'good',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '天气正在更新，本次评估暂未完成' in html
    assert '未保存记录' not in html
    assert HealthRiskAssessment.query.count() == 0


def test_personal_susceptibility_accepts_full_gender_labels():
    from services.health_risk_service import HealthRiskService

    service = HealthRiskService()
    male_short = service._calc_personal_susceptibility_score(
        {'age': 60, 'gender': '男', 'chronic_diseases': []}
    )
    male_full = service._calc_personal_susceptibility_score(
        {'age': 60, 'gender': '男性', 'chronic_diseases': []}
    )
    female_short = service._calc_personal_susceptibility_score(
        {'age': 75, 'gender': '女', 'chronic_diseases': []}
    )
    female_full = service._calc_personal_susceptibility_score(
        {'age': 75, 'gender': '女性', 'chronic_diseases': []}
    )
    unknown = service._calc_personal_susceptibility_score(
        {'age': 60, 'gender': '未知', 'chronic_diseases': []}
    )

    assert male_short == male_full
    assert female_short == female_full
    assert male_full > unknown


def test_community_context_marks_default_vi_and_unavailable_burden(db_session):
    from services.health_risk_service import HealthRiskService

    db_session.add(Community(
        name='人口缺失社区',
        population=None,
        elderly_ratio=0.25,
        chronic_disease_ratio=0.12,
        vulnerability_index=None,
        risk_level=None
    ))
    db_session.commit()

    service = HealthRiskService()
    missing_community = service._build_community_context('')
    assert missing_community['source'] == 'user_profile_missing'
    assert missing_community['vulnerability_source'] == 'default_proxy'
    assert missing_community['burden_available'] is False
    assert missing_community['cases_30d'] is None
    assert missing_community['burden_per_1000'] is None
    assert missing_community['imputed_fields'] == ['vulnerability_index', 'burden_score']
    assert all('模型使用' not in item for item in missing_community['warnings'])
    assert all('不计入' in item for item in missing_community['warnings'])

    missing_population = service._build_community_context('人口缺失社区')
    assert missing_population['source'] == 'community_table'
    assert missing_population['vulnerability_source'] == 'default_proxy'
    assert missing_population['vulnerability_index'] == 45.0
    assert missing_population['population_available'] is False
    assert missing_population['population_source'] == 'missing'
    assert missing_population['cases_30d'] == 0
    assert missing_population['burden_available'] is False
    assert missing_population['burden_per_1000'] is None
    assert missing_population['burden_source'] == 'unavailable_missing_population'
    assert set(missing_population['imputed_fields']) == {
        'population',
        'vulnerability_index',
        'burden_score'
    }
    assert any('每千人负担无法计算' in item for item in missing_population['warnings'])


def test_legacy_assessment_without_matrix_does_not_render_false_low_bucket(
    authenticated_client,
    db_session
):
    _seed_health_assessment_user(db_session)
    user = User.query.filter_by(username='testuser').first()
    db_session.add(HealthRiskAssessment(
        user_id=user.id,
        assessment_date=utcnow(),
        weather_condition=json.dumps({'temperature': 30}, ensure_ascii=False),
        risk_score=52,
        risk_level='中风险',
        disease_risks='{}',
        recommendations='[]',
        explain=json.dumps({'academic_profile': {}}, ensure_ascii=False),
    ))
    db_session.commit()

    response = authenticated_client.get('/health-assessment')

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '这条历史记录没有完整的评估依据' in html
    assert '影响程度：<strong>--</strong>' in html
    assert '发生可能性：<strong>--</strong>' in html
    assert '综合位置 -- / 16' in html
    assert '影响程度：<strong>low</strong>' not in html


def test_legacy_proxy_assessment_does_not_keep_community_path_label(
    authenticated_client,
    db_session,
):
    _seed_health_assessment_user(db_session)
    user = User.query.filter_by(username='testuser').first()
    user.community = ''
    db_session.add(HealthRiskAssessment(
        user_id=user.id,
        assessment_date=utcnow(),
        weather_condition=json.dumps({'temperature': 30}, ensure_ascii=False),
        risk_score=52.1,
        risk_level='中风险',
        disease_risks='{}',
        recommendations='[]',
        explain=json.dumps({
            'academic_profile': {
                'model_paths': [
                    {'name': 'DLNM个体模型', 'score': 52.7, 'weight': 0.382, 'contribution': 20.14},
                    {'name': '规则暴露模型', 'score': 11.8, 'weight': 0.255, 'contribution': 3.0},
                    {'name': '社区脆弱性模型', 'score': 65.6, 'weight': 0.212, 'contribution': 13.93},
                    {'name': '慢病专项模型', 'score': 100.0, 'weight': 0.15, 'contribution': 15.0},
                ],
                'fusion_breakdown': {
                    'path_fused_score': 43.6,
                    'chronic_overall_score': 100.0,
                    'final_score': 52.1,
                    'contribution_total': 52.07,
                    'community_in_score': False,
                },
                'community_context': {
                    'community': '未设置',
                    'vulnerability_index': 45.0,
                    'vulnerability_level': '中',
                    'vulnerability_source': 'default_proxy',
                    'source': 'user_profile_missing',
                    'warnings': ['个人资料未设置社区，社区 VI 仅作 45 分中性参考，不计入个人风险分。'],
                    'burden_available': False,
                },
            }
        }, ensure_ascii=False),
    ))
    db_session.commit()

    html = authenticated_client.get('/health-assessment').get_data(as_text=True)
    assert '个体与气温' in html
    assert '社区脆弱性模型' not in html
    assert '没有计入' in html


def test_health_assessment_restores_saved_screening_radios(
    authenticated_client,
    db_session,
    monkeypatch,
):
    _seed_health_assessment_user(db_session)
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_qweather_payload(), False)
    )

    post = authenticated_client.post(
        '/health-assessment',
        data={
            'outdoor_exposure': 'high',
            'symptom_level': 'moderate',
            'hydration': 'poor',
            'medication_adherence': 'partial',
            'sleep_quality': 'poor',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True,
    )
    assert post.status_code == 200

    html = post.get_data(as_text=True)
    assert 'name="outdoor_exposure" value="high"' in html
    assert 'name="outdoor_exposure" value="high" class="d-none assess-opt" required checked' in html
    assert 'name="symptom_level" value="moderate" class="d-none assess-opt" required checked' in html
    assert 'name="hydration" value="poor" class="d-none assess-opt" required checked' in html
    assert 'name="medication_adherence" value="partial" class="d-none assess-opt" required checked' in html
    assert 'name="sleep_quality" value="poor" class="d-none assess-opt" required checked' in html


def _assessment_weather(**overrides):
    payload = {
        'temperature': 31,
        'humidity': 68,
        'aqi': 62,
        'pm25': 38,
        'weather_condition': '多云',
        'data_source': 'QWeather',
        'is_mock': False,
        'is_demo': False,
    }
    payload.update(overrides)
    return payload


def _assessment_profile(**overrides):
    payload = {
        'age': 72,
        'gender': '男',
        'community': '',
        'has_chronic_disease': True,
        'chronic_diseases': ['高血压'],
    }
    payload.update(overrides)
    return payload


def _assessment_screening():
    return {
        'outdoor_exposure': 'medium',
        'symptom_level': 'none',
        'hydration': 'normal',
        'medication_adherence': 'good',
        'sleep_quality': 'good',
    }


def test_imputed_community_proxy_is_not_fused_into_personal_score(db_session):
    from services.health_risk_service import HealthRiskService

    db_session.add(Community(
        name='低脆弱社区',
        population=1000,
        elderly_ratio=0.1,
        chronic_disease_ratio=0.05,
        vulnerability_index=10.0,
        risk_level='低',
    ))
    db_session.add(Community(
        name='高脆弱社区',
        population=1000,
        elderly_ratio=0.4,
        chronic_disease_ratio=0.2,
        vulnerability_index=90.0,
        risk_level='高',
    ))
    db_session.commit()

    service = HealthRiskService()
    weather = _assessment_weather()
    screening = _assessment_screening()
    missing = service.assess_personal_weather_health_risk(
        _assessment_profile(community=''),
        weather,
        screening,
    )
    low = service.assess_personal_weather_health_risk(
        _assessment_profile(community='低脆弱社区'),
        weather,
        screening,
    )
    high = service.assess_personal_weather_health_risk(
        _assessment_profile(community='高脆弱社区'),
        weather,
        screening,
    )

    assert missing['community_context']['imputed'] is True
    assert missing['community_in_score'] is False
    assert low['community_in_score'] is True
    assert high['community_in_score'] is True
    assert high['risk_score'] > low['risk_score']


def test_missing_aqi_is_not_scored_as_default_50(db_session):
    from services.health_risk_service import HealthRiskService

    service = HealthRiskService()
    profile = _assessment_profile()
    screening = _assessment_screening()
    with_aqi = service.assess_personal_weather_health_risk(
        profile,
        _assessment_weather(aqi=180),
        screening,
    )
    without_aqi = service.assess_personal_weather_health_risk(
        profile,
        _assessment_weather(aqi=None),
        screening,
    )

    assert with_aqi['aqi_in_score'] is True
    assert without_aqi['aqi_in_score'] is False
    assert without_aqi['component_scores']['空气质量风险'] == 0
    assert with_aqi['risk_score'] > without_aqi['risk_score']


def test_health_assessment_page_says_proxy_community_is_not_in_score(
    authenticated_client,
    db_session,
    monkeypatch,
):
    user = User.query.filter_by(username='testuser').first()
    user.community = ''
    user.age = 72
    db_session.commit()
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_assessment_weather(), False),
    )

    response = authenticated_client.post(
        '/health-assessment',
        data={
            'outdoor_exposure': 'medium',
            'symptom_level': 'none',
            'hydration': 'normal',
            'medication_adherence': 'good',
            'sleep_quality': 'good',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '没有计入' in body
    assert '当前风险得分使用了社区参考值' not in body
    assert '已计入个人风险分' not in body
    assert '社区脆弱性模型' not in body


def _screening_post(**overrides):
    payload = {
        'outdoor_exposure': 'medium',
        'symptom_level': 'none',
        'hydration': 'normal',
        'medication_adherence': 'good',
        'sleep_quality': 'good',
        'csrf_token': 'test-csrf-token',
    }
    payload.update(overrides)
    return payload


def test_health_assessment_post_without_age_does_not_invent_a_default(
    authenticated_client,
    db_session,
    monkeypatch,
):
    user = User.query.filter_by(username='testuser').first()
    user.age = None
    db_session.commit()
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_assessment_weather(), False),
    )

    response = authenticated_client.post(
        '/health-assessment',
        data=_screening_post(),
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '请先在个人设置填写年龄' in html
    assert HealthRiskAssessment.query.count() == 0


def test_health_assessment_get_asks_for_age_when_missing(authenticated_client, db_session):
    user = User.query.filter_by(username='testuser').first()
    user.age = None
    db_session.commit()

    html = authenticated_client.get('/health-assessment').get_data(as_text=True)
    assert '请先在个人设置填写年龄' in html
    assert 'href="/profile"' in html


def test_health_assessment_post_without_screening_does_not_use_optimistic_defaults(
    authenticated_client,
    db_session,
    monkeypatch,
):
    user = User.query.filter_by(username='testuser').first()
    user.age = 72
    db_session.commit()
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_assessment_weather(), False),
    )

    response = authenticated_client.post(
        '/health-assessment',
        data={'csrf_token': 'test-csrf-token'},
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '请完成全部 5 项筛查' in html
    assert HealthRiskAssessment.query.count() == 0


def test_proxy_community_is_not_named_or_reasoned_as_scored_community(db_session):
    from services.health_risk_service import HealthRiskService

    result = HealthRiskService().assess_personal_weather_health_risk(
        _assessment_profile(community=''),
        _assessment_weather(),
        _assessment_screening(),
    )

    assert result['community_in_score'] is False
    path_names = [path['name'] for path in result['model_paths']]
    assert '社区脆弱性模型' not in path_names
    assert any('个体' in name and '气温' in name for name in path_names)
    reasons = (result.get('explain') or {}).get('reasons') or []
    assert all('社区脆弱性' not in item for item in reasons)
    assert result['community_context']['vulnerability_index'] == 45.0


def test_real_community_path_is_labeled_and_can_appear_in_reasons(db_session):
    from services.health_risk_service import HealthRiskService

    db_session.add(Community(
        name='高脆弱社区',
        population=1000,
        elderly_ratio=0.4,
        chronic_disease_ratio=0.2,
        vulnerability_index=90.0,
        risk_level='高',
    ))
    db_session.commit()

    result = HealthRiskService().assess_personal_weather_health_risk(
        _assessment_profile(community='高脆弱社区'),
        _assessment_weather(),
        _assessment_screening(),
    )

    assert result['community_in_score'] is True
    assert any(path['name'] == '社区脆弱性模型' for path in result['model_paths'])
    reasons = (result.get('explain') or {}).get('reasons') or []
    assert any('社区脆弱性' in item for item in reasons)


def test_assessment_without_age_does_not_invent_45(db_session):
    from services.health_risk_service import HealthRiskService

    with pytest.raises(ValueError, match='年龄'):
        HealthRiskService().assess_personal_weather_health_risk(
            _assessment_profile(age=None),
            _assessment_weather(),
            _assessment_screening(),
        )


def test_assessment_without_temperature_does_not_invent_20(db_session):
    from services.health_risk_service import HealthRiskService

    with pytest.raises(ValueError, match='气温'):
        HealthRiskService().assess_personal_weather_health_risk(
            _assessment_profile(),
            _assessment_weather(temperature=None),
            _assessment_screening(),
        )


def test_missing_chronic_score_is_not_fused_as_30(monkeypatch, db_session):
    from services.health_risk_service import HealthRiskService

    class _MissingChronic:
        def predict_individual_risk(self, *_args, **_kwargs):
            return {'overall_risk': {}, 'recommendations': []}

    monkeypatch.setattr(
        'services.chronic_risk_service.get_chronic_service',
        lambda: _MissingChronic(),
    )

    result = HealthRiskService().assess_personal_weather_health_risk(
        _assessment_profile(),
        _assessment_weather(),
        _assessment_screening(),
    )

    path = result['fusion_breakdown']['path_fused_score']
    final = result['fusion_breakdown']['final_score']
    invented = round(0.85 * path + 0.15 * 30.0, 1)

    assert result.get('chronic_in_score') is False
    assert result['fusion_breakdown'].get('chronic_in_score') is False
    assert final != invented
    assert abs(final - round(path, 1)) <= 0.1


def test_chronic_score_is_fused_when_present(monkeypatch, db_session):
    from services.health_risk_service import HealthRiskService

    class _HighChronic:
        def predict_individual_risk(self, *_args, **_kwargs):
            return {'overall_risk': {'score': 90.0}, 'recommendations': []}

    monkeypatch.setattr(
        'services.chronic_risk_service.get_chronic_service',
        lambda: _HighChronic(),
    )

    result = HealthRiskService().assess_personal_weather_health_risk(
        _assessment_profile(),
        _assessment_weather(),
        _assessment_screening(),
    )

    path = result['fusion_breakdown']['path_fused_score']
    final = result['fusion_breakdown']['final_score']
    expected = round(0.85 * path + 0.15 * 90.0, 1)

    assert result.get('chronic_in_score') is True
    assert abs(final - expected) <= 0.1


def test_missing_dlnm_rr_is_not_fused_as_zero_or_one(monkeypatch, db_session):
    from services.health_risk_service import HealthRiskService

    class UnavailableDLNM:
        def calculate_rr(self, *_args, **_kwargs):
            return None, {
                'calculation_branch': 'untrained_unavailable',
                'final_rr': None,
            }

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: UnavailableDLNM(),
    )

    result = HealthRiskService().assess_personal_weather_health_risk(
        _assessment_profile(),
        _assessment_weather(),
        _assessment_screening(),
    )

    path_names = [path['name'] for path in result['model_paths']]
    assert 'DLNM个体模型' not in path_names
    assert result.get('dlnm_in_score') is False
    assert result['fusion_breakdown'].get('dlnm_in_score') is False
    assert result['component_scores']['温度风险'] is None
    assert result['risk_score'] is not None
    assert result['chronic_in_score'] is False


def test_missing_humidity_is_not_scored_or_shown_as_60(db_session):
    from services.health_risk_service import HealthRiskService

    result = HealthRiskService().assess_personal_weather_health_risk(
        _assessment_profile(),
        _assessment_weather(humidity=None),
        _assessment_screening(),
    )

    assert result['weather'].get('humidity') is None
    assert result['component_scores']['湿度风险'] == 0
    assert result['fusion_breakdown'].get('humidity_in_score') is False


def test_health_assessment_page_says_real_community_is_in_score(
    authenticated_client,
    db_session,
    monkeypatch,
):
    _seed_health_assessment_user(db_session)
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_assessment_weather(), False),
    )

    response = authenticated_client.post(
        '/health-assessment',
        data=_screening_post(),
        follow_redirects=True,
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '已计入个人风险分' in body
    assert '当前风险得分没有计入这些社区代理值' not in body
    assert '社区脆弱性模型' in body
