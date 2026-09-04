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
        lambda location: ({
            'temperature': 36,
            'temperature_max': 36,
            'temperature_min': 26,
            'humidity': 72,
            'aqi': 88,
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
    assert '天气正在更新' in body
    assert '家庭成员的阈值提醒稍后恢复' in body
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


def test_family_member_delete_unlinks_pairs_and_related_rows(client, db_session):
    from core.db_models import (
        FamilyMember,
        FamilyMemberProfile,
        Notification,
        Pair,
        UsageEvent,
    )
    from core.security import hash_short_code
    from core.time_utils import utcnow

    user = _create_user(db_session, username='family_delete_user')
    member = FamilyMember(user_id=user.id, name='舅舅', relation='舅舅', age=70, gender='男性')
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(member_id=member.id, alert_enabled=True))
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        member_id=member.id,
        elder_code='delete-elder-1',
        short_code='41414141',
        short_code_hash=hash_short_code('41414141'),
        status='active',
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.add(Notification(
        user_id=user.id,
        member_id=member.id,
        title='测试通知',
        message='删除前应一并清理',
    ))
    db_session.add(UsageEvent(
        user_id=user.id,
        member_id=member.id,
        event_type='elder_profile_created',
        source='web',
    ))
    db_session.commit()
    member_id = member.id
    pair_id = pair.id
    _login_as(client, user.id)

    response = client.post(
        f'/family-members/{member_id}/delete',
        data={'csrf_token': 'test-csrf-token'},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '家庭成员已删除' in response.get_data(as_text=True)
    assert db_session.get(FamilyMember, member_id) is None
    remaining_pair = db_session.get(Pair, pair_id)
    assert remaining_pair is not None
    assert remaining_pair.member_id is None
    assert remaining_pair.status == 'inactive'
    assert Notification.query.filter_by(member_id=member_id).count() == 0
    leftover_events = UsageEvent.query.filter_by(user_id=user.id).all()
    assert leftover_events
    assert all(event.member_id is None for event in leftover_events)


def test_family_member_detail_survives_missing_humidity(client, db_session, monkeypatch):
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_missing_humidity_user')
    member = FamilyMember(user_id=user.id, name='周奶奶', relation='母亲', age=81, gender='女性')
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        weather_thresholds=json.dumps({'high_temp': 32, 'high_humidity': 80}, ensure_ascii=False),
        alert_enabled=True,
    ))
    db_session.commit()
    _login_as(client, user.id)

    monkeypatch.setattr('blueprints.health.ensure_user_location_valid', lambda: '都昌')
    monkeypatch.setattr(
        'blueprints.health.get_weather_with_cache',
        lambda _location: ({
            'temperature': 36,
            'temperature_max': 36,
            'temperature_min': 26,
            'humidity': None,
            'aqi': 88,
            'data_source': 'QWeather',
            'is_mock': False,
        }, None),
    )

    response = client.get(f'/family-members/{member.id}')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '周奶奶' in body
    assert '天气更新中' in body
    assert '湿度 None' not in body
    assert '触发：高温' not in body


def test_family_member_form_rejects_age_zero(client, db_session):
    from core.db_models import FamilyMember

    user = _create_user(db_session, username='family_age_zero_user')
    _login_as(client, user.id)

    response = client.post(
        '/family-members/new',
        data={
            'name': '邻居',
            'relation': '邻居',
            'age': '0',
            'gender': '女性',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '年龄需在1-150之间' in response.get_data(as_text=True)
    assert FamilyMember.query.filter_by(user_id=user.id).count() == 0


def test_family_member_edit_form_age_bounds_match_api():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / 'templates' / 'family_member_edit.html').read_text(
        encoding='utf-8'
    )
    assert 'min="1"' in html
    assert 'max="150"' in html
    assert 'min="0"' not in html
    assert 'max="120"' not in html


def test_compute_member_risk_without_age_is_unknown():
    from types import SimpleNamespace

    from core.health_profiles import compute_member_risk

    unknown = compute_member_risk(SimpleNamespace(age=None, chronic_diseases=None), None)
    assert unknown['level'] == 'unknown'
    assert unknown['label'] == '风险未知'
    assert unknown['score'] is None

    aged = compute_member_risk(SimpleNamespace(age=76, chronic_diseases=None), None)
    assert aged['level'] == 'low'
    assert aged['score'] == 25
    assert '老年' in aged['reasons']


def test_family_members_page_does_not_invent_low_risk_when_age_missing(client, db_session):
    from core.db_models import FamilyMember

    user = _create_user(db_session, username='family_unknown_risk_user')
    member = FamilyMember(user_id=user.id, name='邻居阿婆', relation='邻居', age=None, gender='女性')
    db_session.add(member)
    db_session.commit()
    _login_as(client, user.id)

    body = client.get('/family-members').get_data(as_text=True)
    assert '邻居阿婆' in body
    assert '风险未知' in body
    assert "risk-label or '低风险'" not in body
    card = body[body.index('邻居阿婆'): body.index('邻居阿婆') + 800]
    assert '低风险' not in card
    assert 'None岁' not in body
    assert '年龄未填' in body
    detail = client.get(f'/family-members/{member.id}').get_data(as_text=True)
    assert 'None岁' not in detail
    assert '年龄未填' in detail


def test_family_member_pages_expose_delete_and_toggle_forms(client, db_session):
    """删除和开关提醒路由已存在，页面必须给出可提交的表单。"""
    from core.db_models import FamilyMember, FamilyMemberProfile

    user = _create_user(db_session, username='family_action_ui_user')
    member = FamilyMember(user_id=user.id, name='姨妈', relation='姨妈', age=68, gender='女性')
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(member_id=member.id, alert_enabled=True))
    db_session.commit()
    _login_as(client, user.id)

    list_body = client.get('/family-members').get_data(as_text=True)
    assert f'action="/family-members/{member.id}/delete"' in list_body

    detail_body = client.get(f'/family-members/{member.id}').get_data(as_text=True)
    assert f'action="/family-members/{member.id}/delete"' in detail_body
    assert f'action="/family-members/{member.id}/toggle-alert"' in detail_body
