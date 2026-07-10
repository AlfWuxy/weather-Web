# -*- coding: utf-8 -*-
"""家庭成员页面回归测试。"""

import json


def _login_as(client, user_id: int, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
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
    _login_as(client, user.id)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda location: ({'temperature': 36, 'humidity': 72, 'aqi': 88, 'data_source': 'QWeather', 'is_mock': False}, None),
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
    _login_as(client, user.id)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda _location: ({'temperature': 37, 'humidity': 70, 'data_source': 'Demo', 'is_mock': True}, False),
    )

    response = client.get('/family-members')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '等待真实天气' in body
    assert '模拟值不会触发通知' in body
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
