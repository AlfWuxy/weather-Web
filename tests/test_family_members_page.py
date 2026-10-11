# -*- coding: utf-8 -*-
"""家庭成员页面回归测试。"""

import json

import pytest

from core.time_utils import utcnow


def _login_as(client, user_id: int, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = f'{user_id}:1'
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username='family_user', role='user'):
    from core.db_models import User

    user = User(username=username, role=role)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def test_family_members_page_uses_new_route_and_renders_member_alerts(client, db_session, monkeypatch):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_list_user')
    member = FamilyMember(
        user_id=user.id,
        name='母亲',
        relation='母亲',
        age=76,
        gender='女性',
        chronic_diseases=json.dumps(['高血压'], ensure_ascii=False),
    )
    from services.miniprogram_auth import current_privacy_version
    member.health_sensitive_consented_at = utcnow()
    member.health_sensitive_consent_version = current_privacy_version()
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_temp': 32}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user.id)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda location: ({
            'temperature': 36,
            'temperature_max': 38,
            'temperature_min': 28,
            'humidity': 72,
            'pressure': 1002,
            'weather_condition': '晴',
            'wind_speed': 2.0,
            'aqi': 88,
            'pm25': 42,
            'observed_at': utcnow().isoformat(),
            'air_observed_at': utcnow().isoformat(),
            'air_quality_available': True,
            'quality_version': 1,
            'data_source': 'QWeather',
            'is_mock': False,
        }, None),
    )

    response = client.get('/family-members')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '/family-members/new' in body
    assert '母亲' in body
    assert '高血压' in body
    assert '都昌' in body
    assert '高温≥32' in body


def test_family_members_page_does_not_trigger_alerts_from_mock_weather(client, db_session, monkeypatch):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_mock_weather_user')
    member = FamilyMember(user_id=user.id, name='父亲', relation='父亲', age=75, gender='男性')
    from services.miniprogram_auth import current_privacy_version
    member.health_sensitive_consented_at = utcnow()
    member.health_sensitive_consent_version = current_privacy_version()
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_temp': 32}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user.id)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda _location: ({'temperature': 37, 'humidity': 70, 'data_source': 'Demo', 'is_mock': True}, False),
    )

    response = client.get('/family-members')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '当前天气数据不可用' in body
    assert '暂无法核对家人的天气提醒条件' in body
    assert '模拟值不会触发通知' not in body
    assert '今日触发' in body
    assert '触发：高温' not in body


def test_family_member_new_page_supports_post_create(client, db_session):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_create_user')
    _login_as(client, user.id)

    response = client.post(
        '/family-members/new',
        data={
            'name': '父亲',
            'relation': '父亲',
            'age': '73',
            'gender': '男性',
            'chronic_diseases': ['糖尿病'],
            'member_health_consent': 'on',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    member = FamilyMember.query.filter_by(user_id=user.id, name='父亲').first()
    assert member is not None
    assert FamilyMemberProfile.query.filter_by(member_id=member.id).first() is not None
    assert '家庭成员已添加' in response.get_data(as_text=True)


def test_family_member_edit_zero_redirects_to_new_page(client, db_session):
    user = _create_user(db_session, username='family_redirect_user')
    _login_as(client, user.id)

    response = client.get('/family-members/0/edit', follow_redirects=False)

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/family-members/new')


def test_family_member_edit_prefills_existing_chronic_diseases(client, db_session):
    from core.db_models import FamilyMember

    user = _create_user(db_session, username='family_edit_user')
    member = FamilyMember(
        user_id=user.id,
        name='外婆',
        relation='外婆',
        age=82,
        gender='女性',
        chronic_diseases=json.dumps(['高血压'], ensure_ascii=False),
    )
    from services.miniprogram_auth import current_privacy_version
    member.health_sensitive_consented_at = utcnow()
    member.health_sensitive_consent_version = current_privacy_version()
    db_session.add(member)
    db_session.commit()
    _login_as(client, user.id)

    response = client.get(f'/family-members/{member.id}/edit')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '编辑家庭成员' in body
    marker = 'value="高血压"'
    assert marker in body
    snippet_start = body.index(marker)
    snippet = body[max(0, snippet_start - 80): snippet_start + 120]
    assert 'checked' in snippet


def test_missing_family_profile_has_specific_fields_and_no_created_followup(client, db_session, monkeypatch):
    from core.db_models import FamilyMember
    from services.account_service import grant_health_consent
    user = _create_user(db_session, username='family_missing_copy')
    member = FamilyMember(user_id=user.id, name='长辈', relation='父亲', age=None, gender='男性')
    grant_health_consent(member, user)
    db_session.add(member)
    db_session.commit()
    _login_as(client, user.id)
    monkeypatch.setattr('blueprints.health.get_weather_with_cache', lambda _: (None, False))
    for path in ('/family-members', f'/family-members/{member.id}'):
        response = client.get(path)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert '风险未知' in body
        assert '待补充：年龄、慢病情况' in body
        assert f'/family-members/{member.id}/edit' in body
        assert '补充资料' in body
        assert '已进入' not in body
        assert '已列入回访' not in body
        assert '天气更新中' not in body


def test_member_detail_does_not_show_unconsented_legacy_chronic_data(client, db_session, monkeypatch):
    from core.db_models import FamilyMember
    user = _create_user(db_session, username='family_unconsented_copy')
    member = FamilyMember(user_id=user.id, name='长辈', chronic_diseases=json.dumps(['旧档案隐私测试疾病']))
    db_session.add(member)
    db_session.commit()
    _login_as(client, user.id)
    monkeypatch.setattr('blueprints.health.get_weather_with_cache', lambda _: (None, False))
    response = client.get(f'/family-members/{member.id}')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '旧档案隐私测试疾病' not in body
    assert '健康信息未授权' in body
    assert '健康资料尚未授权使用' in body


@pytest.mark.parametrize('consented,enabled,expected', [
    (False, True, '健康信息未授权，未核对个人提醒条件'),
    (True, False, '提醒已暂停'),
    (True, True, '未触发'),
])
def test_member_detail_distinguishes_unchecked_alerts_from_no_trigger(client, db_session, monkeypatch, consented, enabled, expected):
    from core.db_models import FamilyMember, FamilyMemberProfile
    from services.account_service import grant_health_consent
    user = _create_user(db_session, username='family_alert_status')
    member = FamilyMember(user_id=user.id, name='长辈', age=72, chronic_diseases='[]')
    if consented:
        grant_health_consent(member, user)
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(member_id=member.id, alert_enabled=enabled,
        weather_thresholds=json.dumps({'high_temp': 35})))
    db_session.commit()
    _login_as(client, user.id)
    monkeypatch.setattr('blueprints.health.get_weather_with_cache', lambda _: ({
        'temperature': 25, 'humidity': 60, 'aqi': 0, 'data_source': 'QWeather',
        'is_mock': False, 'observed_at': utcnow().isoformat(),
    }, False))
    response = client.get(f'/family-members/{member.id}')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert expected in body
    assert 'AQI 0' in body
    if not consented or not enabled:
        assert '>未触发<' not in body
        assert '>触发：' not in body
