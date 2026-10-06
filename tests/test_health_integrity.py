# -*- coding: utf-8 -*-
"""未知资料、插补和退出生产必须在公开服务边界保守处理。"""
from services.miniprogram_auth import current_privacy_version
from types import SimpleNamespace
import pytest

from core.health_profiles import compute_member_risk, compute_profile_completion, member_weather_triggered
from services.health_risk_service import HealthRiskService
from services.chronic_risk_service import ChronicRiskService
from services.ml_prediction_service import MLPredictionService


@pytest.fixture(autouse=True)
def health_app_context(app):
    with app.app_context():
        yield


@pytest.mark.parametrize('field,value', [('age', None), ('age', float('nan')), ('chronic_diseases', None)])
def test_missing_profile_never_gets_low_risk(field, value):
    profile = {'age': 65, 'chronic_diseases': []}
    profile[field] = value
    result = ChronicRiskService().predict_individual_risk(profile, {'temperature': 25, 'humidity': 60, 'aqi': 45})
    assert result['overall_risk']['score'] is None
    assert result['overall_risk']['level'] == '风险未知'
    assert result['followup_priority'] == 'medium'
    assert result['input_states'][field]['status'] == 'unknown'


@pytest.mark.parametrize('field,value', [('temperature', None), ('aqi', None), ('humidity', float('inf'))])
def test_unknown_weather_cannot_be_normal(field, value):
    weather = {'temperature': 25, 'humidity': 60, 'aqi': 45, field: value}
    result = ChronicRiskService().predict_individual_risk({'age': 65, 'chronic_diseases': []}, weather)
    assert result['risk_score'] is None
    assert result['input_states'][field]['used_value'] is None


def test_imputed_weather_requires_verification_instead_of_lower_risk():
    weather = {'temperature': 25, 'humidity': 60, 'aqi': 45,
               'input_states': {'temperature': {'status': 'imputed', 'method': 'historical_mean'}}}
    result = ChronicRiskService().predict_individual_risk({'age': 65, 'chronic_diseases': []}, weather)
    assert result['risk_score'] is None
    assert result['input_states']['temperature']['method'] == 'historical_mean'


def test_missing_screening_is_not_healthy_default():
    result = HealthRiskService().assess_personal_weather_health_risk(
        {'age': 65, 'chronic_diseases': []}, {'temperature': 25, 'humidity': 60, 'aqi': 45})
    assert result['risk_score'] is None
    assert result['input_states']['symptom_level']['status'] == 'unknown'


def test_family_missing_profile_is_unknown_and_followup_medium():
    member = SimpleNamespace(age=30, relation='家人', gender='男', chronic_diseases=None)
    result = compute_member_risk(member, None)
    assert result['level'] == 'unknown'
    assert result['score'] is None
    assert result['followup_priority'] == 'medium'
    assert result['completeness']['total'] == 10
    assert result['completeness']['percent'] == 30


def test_family_explicit_empty_diseases_is_answered():
    member = SimpleNamespace(age=30, relation='家人', gender='男', chronic_diseases='[]', health_sensitive_consented_at=True, health_sensitive_consent_version=current_privacy_version())
    profile = SimpleNamespace(allergies='无', medications='无', metrics=None, risk_tags=None,
                              weather_thresholds=None, contact_prefs=None)
    result = compute_member_risk(member, profile)
    assert result['completeness']['percent'] == 60
    assert result['level'] == 'low'


def test_family_known_high_signal_retains_high_followup_when_incomplete():
    member = SimpleNamespace(age=85, relation=None, gender=None, chronic_diseases='["a", "b", "c", "d"]', health_sensitive_consented_at=True, health_sensitive_consent_version=current_privacy_version())
    result = compute_member_risk(member, None)
    assert result['level'] == 'unknown'
    assert result['followup_priority'] == 'high'
    assert result['observed_score'] >= 70


def test_missing_temperature_does_not_trigger_false_cold_alert():
    profile = SimpleNamespace(weather_thresholds='{"low_temp": 5}')
    weather = SimpleNamespace(temperature=None, humidity=None, aqi=None)
    assert member_weather_triggered(profile, weather) == []


def test_rf_production_does_not_load_or_infer(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('生产不得加载或推理 RF')
    monkeypatch.setattr(MLPredictionService, '_load_model', forbidden)
    service = MLPredictionService()
    for result in (service.predict_disease_risk({}), service.predict_community_risk({}, {})):
        assert result['success'] is False
        assert result['status'] == 'research_only'
    assert service.get_model_status()['accuracy'] is None


def test_population_missing_weather_propagates_unknown():
    result = ChronicRiskService().predict_population_risk({}, {})
    assert result['overall_summary']['highest_rr'] is None
    assert result['stratified_risks'] == {}
    assert result['status'] == 'unknown'


def test_personal_unknown_is_saved_without_fake_risk_score(authenticated_client, db_session, monkeypatch):
    import json
    from core.db_models import User, HealthRiskAssessment
    user = User.query.filter_by(username='testuser').first()
    from core.time_utils import utcnow
    user.health_sensitive_consented_at = utcnow()
    user.health_sensitive_consent_version = current_privacy_version()
    user.age = None
    user.chronic_diseases = None
    db_session.commit()
    monkeypatch.setattr('services.user.profile_service._personal_weather_available', lambda data: True)
    monkeypatch.setattr('services.user.profile_service.normalize_health_model_weather', lambda data: data)
    monkeypatch.setattr('services.user.profile_service.get_weather_with_cache',
                        lambda location: ({'temperature': 25, 'humidity': 60, 'aqi': 45}, False))
    response = authenticated_client.post('/health-assessment', data={
        'outdoor_exposure': 'low', 'symptom_level': 'none', 'hydration': 'good',
        'medication_adherence': 'good', 'sleep_quality': 'good', 'csrf_token': 'test-csrf-token'
    }, follow_redirects=True)
    assert response.status_code == 200
    assessment = HealthRiskAssessment.query.order_by(HealthRiskAssessment.id.desc()).first()
    assert assessment is not None
    assert assessment.risk_score is None
    assert assessment.risk_level == '风险未知'
    assert '资料完整度' in response.get_data(as_text=True)
    payload = json.loads(assessment.explain)['academic_profile']
    assert payload['requires_followup'] is True
    assert payload['data_quality']['input_states']['age']['status'] == 'unknown'


@pytest.mark.parametrize('flag', ['is_mock', 'is_demo'])
def test_mock_weather_is_unknown_even_with_numbers(flag):
    result = ChronicRiskService().predict_individual_risk(
        {'age': 65, 'chronic_diseases': []}, {'temperature': 25, 'humidity': 60, 'aqi': 45, flag: True})
    assert result['risk_score'] is None
    assert result['input_states']['temperature']['reason'] == 'non_observational_weather'


def test_imputation_without_method_remains_unknown():
    result = ChronicRiskService().predict_individual_risk({'age': 65, 'chronic_diseases': []},
        {'temperature': 25, 'humidity': 60, 'aqi': 45, 'imputed_fields': ['aqi']})
    assert result['risk_score'] is None
    assert result['input_states']['aqi']['method'] is None
    assert result['input_states']['aqi']['reason'] == 'imputation_method_missing'


def test_no_consent_health_posts_do_not_read_or_store_health(authenticated_client, monkeypatch):
    from core.db_models import HealthRiskAssessment
    def forbidden(*args, **kwargs):
        raise AssertionError('未同意不应调用天气或评分')
    monkeypatch.setattr('services.user.profile_service.get_weather_with_cache', forbidden)
    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', forbidden)
    response = authenticated_client.post('/health-assessment', data={
        'outdoor_exposure': 'low', 'symptom_level': 'none', 'hydration': 'good',
        'medication_adherence': 'good', 'sleep_quality': 'good', 'csrf_token': 'test-csrf-token'})
    assert response.status_code == 302
    assert response.location.endswith('/account/security')
    response = authenticated_client.post('/chronic-risk', data={'disease': 'hypertension', 'csrf_token': 'test-csrf-token'})
    assert response.status_code == 302
    assert response.location.endswith('/account/security')
    assert HealthRiskAssessment.query.count() == 0


def test_unconsented_legacy_health_data_does_not_change_member_score():
    member = SimpleNamespace(age=30, relation='家人', gender='男', chronic_diseases='["隐私病名"]')
    profile = SimpleNamespace(metrics='{"blood_sugar": 18}', risk_tags='["高危"]', weather_thresholds='{"high_temp": 20}')
    result = compute_member_risk(member, profile)
    assert result['level'] == 'unknown'
    assert result['observed_score'] == 15
    assert '慢性病' not in result['reasons']
    assert result['completeness']['percent'] == 30
    assert result['completeness']['health_consent'] is False


def test_family_unknown_counts_sorting_and_legacy_health_redaction(authenticated_client, db_session, monkeypatch):
    import blueprints.health as health
    from core.db_models import User, FamilyMember, FamilyMemberProfile, HealthDiary
    from core.time_utils import utcnow
    user = User.query.filter_by(username='testuser').first()
    unknown = FamilyMember(user_id=user.id, name='待核实成员', relation='家人', age=30, gender='男', chronic_diseases='["不得展示的遗留病名"]')
    low = FamilyMember(user_id=user.id, name='完整低关注', relation='家人', age=30, gender='男', chronic_diseases='[]', health_sensitive_consented_at=utcnow(), health_sensitive_consent_version=current_privacy_version())
    high = FamilyMember(user_id=user.id, name='高关注待补资料', age=85, chronic_diseases='["a", "b", "c", "d"]', health_sensitive_consented_at=utcnow(), health_sensitive_consent_version=current_privacy_version())
    db_session.add_all([unknown, low, high])
    db_session.flush()
    db_session.add(FamilyMemberProfile(member_id=low.id, allergies='无', medications='无'))
    db_session.add(HealthDiary(user_id=user.id, member_id=unknown.id, symptoms='不得展示的遗留症状'))
    db_session.commit()
    monkeypatch.setattr(health, 'get_weather_with_cache', lambda location: ({}, False))
    captured = {}
    original_render = health.render_template
    def capture(name, **context):
        captured.update(context)
        return original_render(name, **context)
    monkeypatch.setattr(health, 'render_template', capture)
    response = authenticated_client.get('/family-members')
    assert response.status_code == 200
    assert captured['risk_counts'] == {'low': 1, 'medium': 0, 'high': 0, 'unknown': 2}
    assert [row['id'] for row in captured['members']] == [high.id, unknown.id, low.id]
    assert captured['members'][1]['last_diary'] is None
    assert '风险未知' in response.text
    assert '不得展示的遗留病名' not in response.text
    response = authenticated_client.get(f'/family-members/{unknown.id}')
    assert response.status_code == 200
    assert captured['diary_entries'] == []
    assert '不得展示的遗留症状' not in response.text


def test_forecast_page_does_not_require_current_air_quality(authenticated_client, monkeypatch):
    from datetime import timedelta
    from core.time_utils import today_local
    import blueprints.tools as module
    days = [{'date': (today_local() + timedelta(days=i)).isoformat(), 'temperature_max': 35,
             'temperature_min': 25, 'temperature_mean': 30, 'humidity': 65,
             'data_source': 'QWeather', 'is_mock': False} for i in range(7)]
    called = []
    class Forecast:
        def generate_7day_forecast(self, values, start_date=None, context=None):
            called.append(context)
            return [], {}
    def forbidden(*args, **kwargs):
        raise AssertionError('逐日风险不应依赖今日空气实况')
    monkeypatch.setattr(module, 'get_qweather_forecast_with_cache', lambda *args, **kwargs: (days, False, {}))
    monkeypatch.setattr(module, 'get_forecast_service', lambda: Forecast())
    monkeypatch.setattr(module, 'get_weather_with_cache', forbidden)
    response = authenticated_client.get('/forecast-7day')
    assert response.status_code == 200
    assert called == [{}]


def test_withdrawn_user_cannot_recreate_assessment(authenticated_client, db_session, monkeypatch):
    from core.db_models import User, HealthRiskAssessment
    from services.account_service import grant_health_consent, withdraw_health
    user = User.query.filter_by(username='testuser').first()
    grant_health_consent(user, user)
    withdraw_health(user)
    db_session.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError('撤回后不得评估')
    monkeypatch.setattr(HealthRiskService, 'assess_personal_weather_health_risk', forbidden)
    response = authenticated_client.post('/health-assessment', data={
        'outdoor_exposure': 'high', 'symptom_level': 'severe', 'hydration': 'poor',
        'medication_adherence': 'poor', 'sleep_quality': 'poor', 'csrf_token': 'test-csrf-token'})
    assert response.status_code == 302
    assert HealthRiskAssessment.query.count() == 0
    assert response.location.endswith('/account/security')


def test_no_consent_pages_do_not_collect_or_show_legacy_health(authenticated_client, db_session):
    from core.db_models import User, HealthRiskAssessment
    user = User.query.filter_by(username='testuser').first()
    db_session.add(HealthRiskAssessment(user_id=user.id, risk_score=91, risk_level='旧健康推断不应显示', recommendations='[]'))
    db_session.commit()
    response = authenticated_client.get('/health-assessment')
    assert 'name="symptom_level"' not in response.text
    assert '旧健康推断不应显示' not in response.text
    assert '前往同意设置' in response.text
    response = authenticated_client.get('/chronic-risk')
    assert 'name="sbp"' not in response.text
    assert '前往同意设置' in response.text


def test_assessment_rechecks_withdrawal_inside_owner_lock(authenticated_client, db_session, monkeypatch):
    from contextlib import contextmanager
    from core.db_models import User, HealthRiskAssessment
    from services.account_service import grant_health_consent, withdraw_health
    from services.user import profile_service
    from services.user.owner_write_guard import owner_write_guard
    user = User.query.filter_by(username='testuser').one()
    grant_health_consent(user, user)
    db_session.commit()
    monkeypatch.setattr(profile_service, 'get_weather_with_cache', lambda _: ({}, False))
    monkeypatch.setattr(profile_service, '_personal_weather_available', lambda _: True)
    monkeypatch.setattr(profile_service, 'normalize_health_model_weather', lambda _: {})
    monkeypatch.setattr(HealthRiskService, 'assess_personal_weather_health_risk', lambda *args, **kwargs: {
        'risk_score': 80, 'risk_level': '高风险', 'recommendations': [], 'disease_risks': {}})

    @contextmanager
    def withdrawn_before_lock(uid):
        withdraw_health(db_session.get(User, uid))
        db_session.commit()
        with owner_write_guard(uid) as locked:
            yield locked

    monkeypatch.setattr(profile_service, 'owner_write_guard', withdrawn_before_lock)
    response = authenticated_client.post('/health-assessment', data={
        'outdoor_exposure': 'low', 'symptom_level': 'none', 'hydration': 'good',
        'medication_adherence': 'good', 'sleep_quality': 'good', 'csrf_token': 'test-csrf-token'})
    assert response.status_code == 302
    assert response.location.endswith('/account/security')
    assert HealthRiskAssessment.query.count() == 0


def test_mp_member_consent_is_independent_for_reads_and_writes(client, db_session):
    from core.db_models import User, FamilyMember, HealthDiary
    from core.time_utils import utcnow
    from core.usage import create_api_token
    from services.account_service import grant_health_consent
    user = User(username='mp-member-consent', role='user')
    user.set_password('LongPassword123!')
    db_session.add(user)
    db_session.flush()
    grant_health_consent(user, user)
    member = FamilyMember(user_id=user.id, name='旧未授权成员', age=70)
    db_session.add(member)
    db_session.flush()
    db_session.add(HealthDiary(user_id=user.id, member_id=member.id, symptoms='不能读取的旧健康字段'))
    db_session.commit()
    token = create_api_token(user.id, scopes=['miniprogram:read', 'miniprogram:write', 'miniprogram:sensitive'])
    headers = {'Authorization': 'Bearer ' + token}
    for route in ['/health/diary', '/medications', '/health/assessment']:
        response = client.get('/mp/api/v1' + route, query_string={'member_id': member.id}, headers=headers)
        assert response.status_code == 428
        assert response.json['error'] == 'member_health_consent_required'
        response = client.post('/mp/api/v1' + route, json={'member_id': member.id}, headers=headers)
        assert response.status_code == 428
    all_diaries = client.get('/mp/api/v1/health/diary', headers=headers)
    assert all_diaries.json['data']['items'] == []
    assert HealthDiary.query.count() == 1
