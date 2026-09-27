# -*- coding: utf-8 -*-
"""家庭成员页面回归测试。"""

import json

import pytest


def _fresh_qweather(**overrides):
    from core.time_utils import utcnow

    payload = {
        'temperature': 36,
        'temperature_max': 38,
        'temperature_min': 27,
        'humidity': 72,
        'pressure': 1005,
        'wind_speed': 3.2,
        'weather_condition': '晴',
        'aqi': 88,
        'pm25': 35,
        'air_quality_available': True,
        'observed_at': utcnow().isoformat(),
        'air_observed_at': utcnow().isoformat(),
        'data_source': 'QWeather',
        'is_mock': False,
    }
    payload.update(overrides)
    return payload


def _login_as(client, user, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
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
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_temp': 32}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda location: (_fresh_qweather(), None),
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
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_temp': 32}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda _location: ({'temperature': 37, 'humidity': 70, 'data_source': 'Demo', 'is_mock': True}, False),
    )

    response = client.get('/family-members')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '天气正在更新' in body
    assert '家庭成员的阈值提醒稍后恢复' in body
    assert '模拟值不会触发通知' not in body
    assert '今日确认达到阈值' in body
    assert '触发：高温' not in body


def test_family_member_new_page_supports_post_create(client, db_session):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_create_user')
    _login_as(client, user)

    response = client.post(
        '/family-members/new',
        data={
            'name': '父亲',
            'relation': '父亲',
            'age': '73',
            'gender': '男性',
            'chronic_diseases': ['糖尿病'],
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
    _login_as(client, user)

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
    db_session.add(member)
    db_session.commit()
    _login_as(client, user)

    response = client.get(f'/family-members/{member.id}/edit')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '编辑家庭成员' in body
    marker = 'value="高血压"'
    assert marker in body
    snippet_start = body.index(marker)
    snippet = body[max(0, snippet_start - 80): snippet_start + 120]
    assert 'checked' in snippet


@pytest.mark.parametrize(
    'path',
    (
        '/family-members',
        '/family-members/new',
        '/health-diary',
        '/medication-reminders',
    ),
)
def test_community_role_is_rejected_from_family_health_domain(
    client,
    db_session,
    path,
):
    user = _create_user(
        db_session,
        username=f'community_family_denied_{path.replace("/", "_")}',
        role='community',
    )
    _login_as(client, user)

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/entry')


def test_guest_is_rejected_from_family_health_domain(client):
    assert client.get('/guest').status_code == 302

    response = client.get('/family-members', follow_redirects=False)

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/entry')


@pytest.mark.parametrize(
    'weather_overrides',
    (
        {'pm25': None, 'air_quality_available': False},
        {'aqi_estimated': True},
    ),
    ids=('missing-pm25', 'estimated-aqi'),
)
def test_family_detail_does_not_trigger_or_clear_unavailable_aqi(
    client,
    db_session,
    monkeypatch,
    weather_overrides,
):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username=f'family_aqi_guard_{weather_overrides!s}')
    member = FamilyMember(
        user_id=user.id,
        name='外公',
        relation='外公',
        age=79,
        gender='男性',
    )
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_aqi': 50}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌县')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda _location: (_fresh_qweather(aqi=180, **weather_overrides), False),
    )

    response = client.get(f'/family-members/{member.id}')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'AQI≥50' not in body
    assert 'AQI 暂不可判断' in body
    assert '/ AQI --' in body


def test_family_detail_triggers_aqi_only_when_full_gate_is_ready(
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_aqi_ready')
    member = FamilyMember(user_id=user.id, name='奶奶', relation='奶奶', age=80, gender='女性')
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_aqi': 50}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌县')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda _location: (_fresh_qweather(aqi=180, pm25=70), False),
    )

    response = client.get(f'/family-members/{member.id}')

    assert response.status_code == 200
    assert 'AQI≥50' in response.get_data(as_text=True)
