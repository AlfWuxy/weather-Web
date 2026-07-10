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
    assert '融合模型路径' in html
    assert '风险矩阵定位' in html

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
    assert '社区上下文' in html
    assert '社区表实时记录' in html
    assert '社区表 VI' in html
    assert '1 条' in html
    assert '0.833 条 / 千人' in html
    assert 'methodology' in academic
    assert len(academic['methodology']) >= 4

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
    ],
    ids=['demo', 'mock', 'non-qweather', 'missing-temperature']
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
    assert '当前等待真实和风天气及有效温度' in html
    assert '本次未生成评估、未保存记录、未发送通知' in html
    assert HealthRiskAssessment.query.count() == 0


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
    assert '历史记录未包含矩阵拆解，当前不可回算' in html
    assert 'Impact：<strong>--</strong>' in html
    assert 'Likelihood：<strong>--</strong>' in html
    assert '矩阵得分 -- / 16' in html
    assert 'Impact：<strong>low</strong>' not in html
