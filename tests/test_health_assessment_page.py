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


def _seed_test_community(db_session):
    db_session.add(Community(
        name='测试社区',
        population=1200,
        elderly_ratio=0.34,
        chronic_disease_ratio=0.16,
        vulnerability_index=58.0,
        risk_level='中'
    ))
    db_session.add(MedicalRecord(
        patient_name='测试病例',
        visit_time=utcnow(),
        community='测试社区'
    ))
    db_session.commit()


def _assign_assessment_profile(user, community='测试社区'):
    user.age = 72
    user.gender = '男'
    user.community = community
    user.has_chronic_disease = True
    user.chronic_diseases = json.dumps(['高血压', '慢性支气管炎'], ensure_ascii=False)


def _seed_health_assessment_user(db_session):
    _seed_test_community(db_session)
    user = User.query.filter_by(username='testuser').first()
    _assign_assessment_profile(user)
    db_session.commit()


_ASSESSMENT_FORM = {
    'outdoor_exposure': 'high',
    'symptom_level': 'moderate',
    'hydration': 'poor',
    'medication_adherence': 'partial',
    'sleep_quality': 'poor',
    'csrf_token': 'test-csrf-token',
}


def _patch_real_weather(monkeypatch):
    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        lambda _location: (_qweather_payload(), False)
    )


class _MedicalRecordQuerySpy:
    """记录 MedicalRecord.query 是否被取出，并继续走原来的查询描述符。"""

    def __init__(self, descriptor):
        self._descriptor = descriptor
        self.calls = 0

    def __get__(self, instance, owner):
        self.calls += 1
        return self._descriptor.__get__(instance, owner)


def _spy_medical_record_query(monkeypatch):
    descriptor = None
    for cls in MedicalRecord.__mro__:
        if 'query' in cls.__dict__:
            descriptor = cls.__dict__['query']
            break
    assert descriptor is not None
    spy = _MedicalRecordQuerySpy(descriptor)
    monkeypatch.setattr(MedicalRecord, 'query', spy)
    return spy


def _post_health_assessment(http_client):
    return http_client.post(
        '/health-assessment',
        data=_ASSESSMENT_FORM,
        follow_redirects=True,
    )


def _latest_community_context():
    assessment = HealthRiskAssessment.query.order_by(HealthRiskAssessment.id.desc()).first()
    assert assessment is not None
    explain_payload = safe_json_loads(assessment.explain, {})
    return explain_payload['academic_profile']['community_context']


def _assert_seeded_community(community_context):
    assert community_context['community'] == '测试社区'
    assert community_context['source'] == 'community_table'
    assert community_context['vulnerability_source'] == 'community_table'
    assert community_context['vulnerability_index'] == 58.0
    assert community_context['population'] == 1200
    assert community_context['elderly_ratio'] == pytest.approx(0.34)
    assert community_context['chronic_disease_ratio'] == pytest.approx(0.16)
    assert community_context['vulnerability_level'] == '中'


def _assert_restricted_community(community_context):
    assert community_context['community'] == '测试社区'
    assert community_context['source'] == 'unauthorized_proxy'
    assert community_context['source_label'] == '无权读取精确社区健康统计'
    assert community_context['vulnerability_source'] == 'default_proxy'
    assert community_context['vulnerability_index'] == 45.0
    assert community_context['population'] is None
    assert community_context['elderly_ratio'] is None
    assert community_context['chronic_disease_ratio'] is None
    assert community_context['vulnerability_level'] == '中'
    assert 58.0 not in community_context.values()
    assert 1200 not in community_context.values()
    assert any('无权读取' in item and '精确健康统计' in item for item in community_context['warnings'])


def _assert_burden_hidden(community_context, html):
    _assert_restricted_community(community_context)
    assert community_context['cases_30d'] is None
    assert community_context['burden_per_1000'] is None
    assert community_context['burden_available'] is False
    assert community_context['burden_source'] == 'unavailable_not_authorized'
    assert community_context['burden_score'] == 30.0
    assert community_context['imputed'] is True
    assert community_context['imputed_fields'] == ['population', 'vulnerability_index', 'burden_score']
    assert any('无权读取' in item for item in community_context['warnings'])
    assert '所在社区的参考情况' in html
    assert '45.0' in html
    assert '58.0' not in html
    assert '一般参考' in html
    assert '1 条' not in html
    assert '0.833 条 / 千人' not in html
    assert '资料不足' in html


def _assert_burden_visible(community_context, html):
    _assert_seeded_community(community_context)
    assert community_context['cases_30d'] == 1
    assert community_context['burden_available'] is True
    assert community_context['burden_per_1000'] == pytest.approx(0.833, abs=0.001)
    assert community_context['burden_source'] == 'medical_records_30d_per_1000'
    assert community_context['imputed'] is False
    assert '58.0' in html
    assert '社区资料' in html
    assert '1 条' in html
    assert '0.833 条 / 千人' in html
    assert '资料不足' not in html


def _login(client, username):
    with client.session_transaction() as session:
        session['_csrf_token'] = 'test-csrf-token'
    response = client.post('/login', data={
        'username': username,
        'password': 'testpass',
        'csrf_token': 'test-csrf-token',
    }, follow_redirects=False)
    assert response.status_code == 302


def _isolated_client(app):
    from flask.testing import FlaskClient

    class _RequestIsolatedClient(FlaskClient):
        def open(self, *args, **kwargs):
            # db_session 持有外层应用上下文时，g 会把上一请求的登录用户留到下一次。
            # 游客资料版本必须在每个请求里重新加载。
            with self.application.app_context():
                return super().open(*args, **kwargs)

    return _RequestIsolatedClient(app, app.response_class)


def _enter_guest_for_community(client, community='测试社区'):
    from services.guest_experience import get_experience, mutate_experience

    response = client.get('/guest', follow_redirects=False)
    assert response.status_code == 302
    with client.session_transaction() as session:
        guest_id = session['_user_id']
        session['_csrf_token'] = 'test-csrf-token'
    state = get_experience(guest_id, create=False)
    updated = mutate_experience(
        guest_id,
        'profile',
        {
            'community': community,
            'age': 72,
            'gender': '男',
            'has_chronic_disease': True,
            'chronic_diseases': ['高血压'],
        },
        state['version'],
    )
    assert updated['profile']['community'] == community


def _guest_community_context(client):
    from services.guest_experience import get_experience

    with client.session_transaction() as session:
        guest_id = session['_user_id']
    state = get_experience(guest_id, create=False)
    assert state is not None and state.get('assessment')
    explain_payload = safe_json_loads(state['assessment'].get('explain'), {})
    return explain_payload['academic_profile']['community_context']


def _add_community_user(db_session, username, authorized_community):
    user = User(
        username=username,
        role='community',
        authorized_community=authorized_community,
    )
    user.set_password('testpass')
    _assign_assessment_profile(user, community='测试社区')
    db_session.add(user)
    db_session.commit()
    return user


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
    query_spy = _spy_medical_record_query(monkeypatch)

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
    _assert_burden_hidden(community_context, html)
    assert query_spy.calls == 0
    assert '社区脆弱性' in html
    assert '路径 C' not in html
    assert '代理值' not in html
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
    assert '天气正在更新，本次评估暂未完成' in html
    assert '未保存记录' not in html
    assert HealthRiskAssessment.query.count() == 0


def test_community_context_marks_default_vi_and_unavailable_burden(db_session, app, monkeypatch):
    from flask_login import login_user

    from services.health_risk_service import HealthRiskService

    db_session.add(Community(
        name='人口缺失社区',
        population=None,
        elderly_ratio=0.25,
        chronic_disease_ratio=0.12,
        vulnerability_index=None,
        risk_level=None
    ))
    admin = User(username='contextadmin', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.commit()

    query_spy = _spy_medical_record_query(monkeypatch)
    service = HealthRiskService()
    missing_community = service._build_community_context('')
    assert missing_community['source'] == 'user_profile_missing'
    assert missing_community['vulnerability_source'] == 'default_proxy'
    assert missing_community['burden_available'] is False
    assert missing_community['cases_30d'] is None
    assert missing_community['burden_per_1000'] is None
    assert missing_community['burden_source'] == 'unavailable_no_community'
    assert missing_community['imputed_fields'] == ['vulnerability_index', 'burden_score']
    assert query_spy.calls == 0

    missing_population = service._build_community_context('人口缺失社区')
    assert missing_population['source'] == 'community_table'
    assert missing_population['vulnerability_source'] == 'default_proxy'
    assert missing_population['vulnerability_index'] == 45.0
    assert missing_population['population_available'] is False
    assert missing_population['population_source'] == 'missing'
    assert missing_population['cases_30d'] is None
    assert missing_population['burden_available'] is False
    assert missing_population['burden_per_1000'] is None
    assert missing_population['burden_source'] == 'unavailable_not_authorized'
    assert missing_population['burden_score'] == 30.0
    assert set(missing_population['imputed_fields']) == {
        'population',
        'vulnerability_index',
        'burden_score'
    }
    assert any('无权读取' in item for item in missing_population['warnings'])
    assert query_spy.calls == 0

    with app.test_request_context():
        assert login_user(admin) is True
        authorized_population = service._build_community_context('人口缺失社区')

    assert query_spy.calls == 1
    assert authorized_population['source'] == 'community_table'
    assert authorized_population['vulnerability_source'] == 'default_proxy'
    assert authorized_population['vulnerability_index'] == 45.0
    assert authorized_population['population_available'] is False
    assert authorized_population['population_source'] == 'missing'
    assert authorized_population['cases_30d'] == 0
    assert authorized_population['burden_available'] is False
    assert authorized_population['burden_per_1000'] is None
    assert authorized_population['burden_source'] == 'unavailable_missing_population'
    assert authorized_population['burden_score'] == 30.0
    assert set(authorized_population['imputed_fields']) == {
        'population',
        'vulnerability_index',
        'burden_score'
    }
    assert any('每千人负担无法计算' in item for item in authorized_population['warnings'])
    assert not any('无权读取' in item for item in authorized_population['warnings'])


def test_guest_health_assessment_hides_recent_visit_counts(app, db_session, monkeypatch):
    client = _isolated_client(app)
    _seed_test_community(db_session)
    _enter_guest_for_community(client)
    _patch_real_weather(monkeypatch)
    query_spy = _spy_medical_record_query(monkeypatch)

    response = _post_health_assessment(client)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '最新评估结果' in html
    _assert_burden_hidden(_guest_community_context(client), html)
    assert query_spy.calls == 0
    assert HealthRiskAssessment.query.count() == 0


def test_admin_health_assessment_shows_recent_visit_counts(admin_client, db_session, monkeypatch):
    _seed_test_community(db_session)
    admin = User.query.filter_by(username='adminuser').first()
    assert admin.role == 'admin'
    _assign_assessment_profile(admin)
    db_session.commit()
    _patch_real_weather(monkeypatch)
    query_spy = _spy_medical_record_query(monkeypatch)

    response = _post_health_assessment(admin_client)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '最新评估结果' in html
    _assert_burden_visible(_latest_community_context(), html)
    assert query_spy.calls == 1


def test_authorized_community_account_sees_recent_visit_counts(client, db_session, monkeypatch):
    _seed_test_community(db_session)
    user = _add_community_user(db_session, 'community-owner', '测试社区')
    _login(client, user.username)
    _patch_real_weather(monkeypatch)
    query_spy = _spy_medical_record_query(monkeypatch)

    response = _post_health_assessment(client)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '最新评估结果' in html
    community_context = _latest_community_context()
    _assert_burden_visible(community_context, html)
    assert query_spy.calls == 1


def test_other_community_account_hides_test_community_visit_counts(client, db_session, monkeypatch):
    _seed_test_community(db_session)
    user = _add_community_user(db_session, 'community-other', '其他社区')
    assert user.authorized_community == '其他社区'
    assert user.community == '测试社区'
    _login(client, user.username)
    _patch_real_weather(monkeypatch)
    query_spy = _spy_medical_record_query(monkeypatch)

    response = _post_health_assessment(client)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '最新评估结果' in html
    _assert_burden_hidden(_latest_community_context(), html)
    assert query_spy.calls == 0


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
