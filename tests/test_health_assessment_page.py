# -*- coding: utf-8 -*-
import json

from core.db_models import Community, HealthRiskAssessment, User
from utils.parsers import safe_json_loads


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


def test_health_assessment_post_persists_academic_payload(authenticated_client, db_session):
    _seed_health_assessment_user(db_session)

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
    assert len(academic['model_paths']) == 3
    assert 'component_scores' in academic
    assert 'community_context' in academic
    assert 'methodology' in academic
    assert len(academic['methodology']) >= 4

    disease_risks = safe_json_loads(assessment.disease_risks, {})
    assert isinstance(disease_risks, dict)
