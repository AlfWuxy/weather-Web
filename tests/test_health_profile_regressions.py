# -*- coding: utf-8 -*-
import pytest
from sqlalchemy.exc import IntegrityError

from core.db_models import HealthRiskAssessment, User
from core.extensions import db


SCREENING_DATA = {
    'outdoor_exposure': 'medium',
    'symptom_level': 'none',
    'hydration': 'normal',
    'medication_adherence': 'good',
    'sleep_quality': 'good',
    'csrf_token': 'test-csrf-token',
}


@pytest.mark.parametrize(
    ('gender', 'age', 'expected_score'),
    [
        ('男', 55, 37.55),
        ('男性', 55, 37.55),
        ('女', 70, 52.7),
        ('女性', 70, 52.7),
    ],
)
def test_personal_susceptibility_accepts_current_and_legacy_gender_values(
    gender,
    age,
    expected_score,
):
    from services.health_risk_service import HealthRiskService

    profile = {
        'age': age,
        'gender': gender,
        'chronic_diseases': [],
    }

    score = HealthRiskService()._calc_personal_susceptibility_score(profile)

    assert score == pytest.approx(expected_score)


def test_health_assessment_marks_every_screening_group_required(authenticated_client):
    response = authenticated_client.get('/health-assessment')

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for field_name in SCREENING_DATA:
        if field_name == 'csrf_token':
            continue
        assert f'name="{field_name}"' in html
        assert f'name="{field_name}" value=' in html
    assert html.count('class="visually-hidden assess-opt" required') == 16
    assert 'class="d-none assess-opt" required' not in html


@pytest.mark.parametrize(
    ('field_name', 'field_value'),
    [
        ('sleep_quality', None),
        ('hydration', 'invalid-value'),
    ],
)
def test_health_assessment_rejects_missing_or_invalid_screening_without_side_effects(
    authenticated_client,
    monkeypatch,
    field_name,
    field_value,
):
    data = dict(SCREENING_DATA)
    if field_value is None:
        data.pop(field_name)
    else:
        data[field_name] = field_value

    def unexpected_weather_call(*_args, **_kwargs):
        raise AssertionError('无效筛查不应请求天气')

    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        unexpected_weather_call,
    )

    response = authenticated_client.post(
        '/health-assessment',
        data=data,
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '请完整选择全部 5 项健康筛查后再提交' in response.get_data(as_text=True)
    assert HealthRiskAssessment.query.count() == 0


def _profile_form(email):
    return {
        'form_id': 'basic',
        'age': '66',
        'gender': '男性',
        'community': '新社区',
        'email': email,
        'csrf_token': 'test-csrf-token',
    }


def test_profile_rejects_email_used_by_another_account_before_mutation(
    authenticated_client,
    db_session,
):
    current = User.query.filter_by(username='testuser').first()
    current.age = 50
    current.gender = '女性'
    current.community = '原社区'
    current.email = 'current@example.com'
    other = User(username='other-user', email='occupied@example.com')
    other.set_password('testpass')
    db_session.add(other)
    db_session.commit()

    response = authenticated_client.post(
        '/profile',
        data=_profile_form('OCCUPIED@example.com'),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '该邮箱已被其他账号使用' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.email == 'current@example.com'
    assert current.age == 50
    assert current.gender == '女性'
    assert current.community == '原社区'


def test_profile_allows_clearing_optional_email(authenticated_client, db_session):
    current = User.query.filter_by(username='testuser').first()
    current.email = 'before@example.com'
    db_session.commit()

    response = authenticated_client.post(
        '/profile',
        data=_profile_form(''),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '个人信息更新成功' in response.get_data(as_text=True)
    assert User.query.filter_by(username='testuser').first().email is None


def test_profile_rolls_back_concurrent_email_unique_conflict(
    authenticated_client,
    db_session,
    monkeypatch,
):
    current = User.query.filter_by(username='testuser').first()
    current.email = 'before@example.com'
    db_session.commit()
    rollback_called = False
    real_rollback = db.session.rollback

    def fail_commit():
        raise IntegrityError('UPDATE users', {}, Exception('unique conflict'))

    def track_rollback():
        nonlocal rollback_called
        rollback_called = True
        return real_rollback()

    monkeypatch.setattr(db.session, 'commit', fail_commit)
    monkeypatch.setattr(db.session, 'rollback', track_rollback)

    response = authenticated_client.post(
        '/profile',
        data=_profile_form('new@example.com'),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '该邮箱已被其他账号使用' in response.get_data(as_text=True)
    assert rollback_called is True
    assert User.query.filter_by(username='testuser').first().email == 'before@example.com'
