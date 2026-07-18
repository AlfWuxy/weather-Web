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


def test_profile_rejects_wxpusher_enable_when_channel_missing_without_partial_update(
    app,
    authenticated_client,
    db_session,
):
    app.config['WXPUSHER_APP_TOKEN'] = ''
    current = User.query.filter_by(username='testuser').first()
    current.age = 50
    current.gender = '女性'
    current.community = '原社区'
    current.email = 'before@example.com'
    current.wxpusher_uid = 'UID_KEEP'
    current.push_enabled = False
    db_session.commit()
    form = _profile_form('after@example.com')
    form.update({
        'wxpusher_uid': 'UID_CHANGED',
        'push_enabled': 'on',
        'wxpusher_consent': '1',
    })

    response = authenticated_client.post('/profile', data=form, follow_redirects=True)

    assert response.status_code == 200
    assert '第三方推送服务暂不可用，本次更改未保存' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.age == 50
    assert current.gender == '女性'
    assert current.community == '原社区'
    assert current.email == 'before@example.com'
    assert current.wxpusher_uid == 'UID_KEEP'
    assert current.push_enabled is False


def test_profile_requires_independent_consent_for_enable_without_partial_update(
    app,
    authenticated_client,
    db_session,
):
    app.config['WXPUSHER_APP_TOKEN'] = 'AT_test-wxpusher-token'
    current = User.query.filter_by(username='testuser').first()
    current.age = 50
    current.email = 'before@example.com'
    current.wxpusher_uid = 'UID_KEEP'
    current.push_enabled = False
    db_session.commit()
    form = _profile_form('after@example.com')
    form.update({'wxpusher_uid': 'UID_CHANGED', 'push_enabled': 'on'})

    response = authenticated_client.post('/profile', data=form, follow_redirects=True)

    assert response.status_code == 200
    assert '请先确认本次开启涉及的第三方传输范围' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.age == 50
    assert current.email == 'before@example.com'
    assert current.wxpusher_uid == 'UID_KEEP'
    assert current.push_enabled is False


def test_profile_enables_wxpusher_with_current_consent(
    app,
    authenticated_client,
    db_session,
):
    app.config['WXPUSHER_APP_TOKEN'] = 'AT_test-wxpusher-token'
    current = User.query.filter_by(username='testuser').first()
    form = _profile_form('after@example.com')
    form.update({
        'wxpusher_uid': 'UID_ENABLED',
        'push_enabled': 'on',
        'wxpusher_consent': '1',
    })

    response = authenticated_client.post('/profile', data=form, follow_redirects=True)

    assert response.status_code == 200
    assert '个人信息更新成功' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.email == 'after@example.com'
    assert current.wxpusher_uid == 'UID_ENABLED'
    assert current.push_enabled is True


def test_profile_keeps_existing_wxpusher_enabled_without_reusing_consent(
    app,
    authenticated_client,
    db_session,
):
    app.config['WXPUSHER_APP_TOKEN'] = 'AT_test-wxpusher-token'
    current = User.query.filter_by(username='testuser').first()
    current.wxpusher_uid = 'UID_KEEP'
    current.push_enabled = True
    db_session.commit()
    form = _profile_form('after@example.com')
    form.update({'wxpusher_uid': 'UID_KEEP', 'push_enabled': 'on'})

    response = authenticated_client.post('/profile', data=form, follow_redirects=True)

    assert response.status_code == 200
    assert '个人信息更新成功' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.email == 'after@example.com'
    assert current.push_enabled is True


def test_profile_allows_disable_when_wxpusher_channel_missing(
    app,
    authenticated_client,
    db_session,
):
    app.config['WXPUSHER_APP_TOKEN'] = ''
    current = User.query.filter_by(username='testuser').first()
    current.wxpusher_uid = 'UID_KEEP'
    current.push_enabled = True
    db_session.commit()
    form = _profile_form('after@example.com')
    form['wxpusher_uid'] = 'UID_KEEP'

    response = authenticated_client.post('/profile', data=form, follow_redirects=True)

    assert response.status_code == 200
    assert '个人信息更新成功' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.email == 'after@example.com'
    assert current.wxpusher_uid == 'UID_KEEP'
    assert current.push_enabled is False


def test_profile_allows_clearing_uid_when_wxpusher_channel_missing(
    app,
    authenticated_client,
    db_session,
):
    app.config['WXPUSHER_APP_TOKEN'] = ''
    current = User.query.filter_by(username='testuser').first()
    current.wxpusher_uid = 'UID_REMOVE'
    current.push_enabled = True
    db_session.commit()
    form = _profile_form('after@example.com')
    form['wxpusher_uid'] = ''

    response = authenticated_client.post('/profile', data=form, follow_redirects=True)

    assert response.status_code == 200
    assert '个人信息更新成功' in response.get_data(as_text=True)
    db_session.refresh(current)
    assert current.wxpusher_uid is None
    assert current.push_enabled is False


def test_profile_wxpusher_controls_follow_runtime_capability(
    app,
    authenticated_client,
):
    app.config['WXPUSHER_APP_TOKEN'] = ''

    unavailable = authenticated_client.get('/profile')

    unavailable_html = unavailable.get_data(as_text=True)
    assert '管理员尚未配置通道' in unavailable_html
    uid_id = unavailable_html.index('id="wxpusher_uid"')
    uid_input_start = unavailable_html.index('<input', uid_id - 100)
    uid_input_end = unavailable_html.index('>', uid_input_start)
    uid_input = unavailable_html[uid_input_start:uid_input_end]
    assert 'readonly' not in uid_input
    assert 'disabled' not in uid_input
    push_enabled_id = unavailable_html.index('id="push_enabled"')
    push_input_start = unavailable_html.index('<input', push_enabled_id - 100)
    push_input_end = unavailable_html.index('>', push_input_start)
    assert 'disabled' in unavailable_html[push_input_start:push_input_end]
    assert 'name="wxpusher_consent"' not in unavailable_html

    app.config['WXPUSHER_APP_TOKEN'] = 'AT_test-wxpusher-token'
    available = authenticated_client.get('/profile')

    available_html = available.get_data(as_text=True)
    assert 'name="wxpusher_consent"' in available_html
    assert '都昌县级预警标题与正文及 7 天内有效的点击链接' in available_html
    assert '打开页面本身不会记录' in available_html
    assert '确认时间和自动确认标记满 30 天后' in available_html
    assert '不会发送家人姓名、健康筛查、健康日记、用药记录或家庭地址' in available_html


def test_web_family_member_delete_detaches_pair_and_removes_all_member_records(
    authenticated_client,
    db_session,
    monkeypatch,
):
    """Web 删除成员必须在同一事务解除外键并清理全部成员级敏感记录。"""
    from core.db_models import (
        FamilyMember,
        FamilyMemberProfile,
        HealthDiary,
        MedicationReminder,
        Notification,
        Pair,
        UsageEvent,
    )
    from core.security import hash_short_code
    from core.time_utils import today_local, utcnow

    owner = User.query.filter_by(username='testuser').one()
    member = FamilyMember(user_id=owner.id, name='待删除家人', relation='家人')
    db_session.add(member)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        member_id=member.id,
        community_code='都昌县',
        location_query='都昌县',
        elder_code='web-delete-member-elder',
        short_code='74444444',
        short_code_hash=hash_short_code('74444444'),
        status='active',
        created_at=utcnow(),
    )
    db_session.add_all([
        pair,
        FamilyMemberProfile(member_id=member.id, privacy_level='family'),
        HealthDiary(
            user_id=owner.id,
            member_id=member.id,
            entry_date=today_local(),
            symptoms='待删除日记',
            severity='mild',
        ),
        MedicationReminder(user_id=owner.id, member_id=member.id, medicine_name='待删除用药'),
        HealthRiskAssessment(
            user_id=owner.id,
            member_id=member.id,
            assessment_date=utcnow(),
            risk_level='低',
        ),
        Notification(
            user_id=owner.id,
            member_id=member.id,
            title='待删除通知',
            message='仅测试',
        ),
        UsageEvent(
            user_id=owner.id,
            member_id=member.id,
            event_type='member_delete_test',
            source='web',
        ),
    ])
    db_session.commit()
    member_id = member.id
    pair_id = pair.id
    refreshed_communities = []
    monkeypatch.setattr(
        'blueprints.health.refresh_latest_community_daily_best_effort',
        lambda community_codes, **_kwargs: refreshed_communities.append(set(community_codes)) or True,
    )

    response = authenticated_client.post(
        f'/family-members/{member_id}/delete',
        data={'csrf_token': 'test-csrf-token'},
        follow_redirects=False,
    )

    assert response.status_code in (301, 302)
    db_session.expire_all()
    assert db_session.get(FamilyMember, member_id) is None
    assert FamilyMemberProfile.query.filter_by(member_id=member_id).count() == 0
    assert HealthDiary.query.filter_by(member_id=member_id).count() == 0
    assert MedicationReminder.query.filter_by(member_id=member_id).count() == 0
    assert HealthRiskAssessment.query.filter_by(member_id=member_id).count() == 0
    assert Notification.query.filter_by(member_id=member_id).count() == 0
    assert UsageEvent.query.filter_by(member_id=member_id).count() == 0
    retained_pair = db_session.get(Pair, pair_id)
    assert retained_pair.status == 'inactive'
    assert retained_pair.member_id is None
    assert refreshed_communities == [{'都昌县'}]
