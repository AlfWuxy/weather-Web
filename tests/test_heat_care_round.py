# -*- coding: utf-8 -*-
"""高温照护协作回合：家属自处理、请医生接手、跨天等待、档案去重与能力声明。"""
from datetime import timedelta

from core.db_models import FamilyMember, HelpRequest, Pair, User
from core.extensions import db
from core.time_utils import utcnow
from core.usage import create_api_token
from services.care_enrollment import enroll_weather_care, find_recent_duplicate_member
from services.family_access import consume_invite, create_invite
from services.help_request_service import (
    HelpRequestError,
    RESOLUTION_DEFINITIONS,
    apply_pair_help_stage,
    resolution_success,
)


def _user(username, role='user'):
    user = User(username=username, role=role)
    user.set_password('pass12344')
    db.session.add(user)
    db.session.commit()
    return user


def _enroll(user, name, relation, location_query):
    member = FamilyMember(
        user_id=user.id,
        name=name,
        relation=relation,
        created_at=utcnow(),
    )
    db.session.add(member)
    db.session.flush()
    pair, created = enroll_weather_care(
        user,
        member,
        location_query=location_query,
        commit=True,
    )
    return member, pair, created


def _csrf(client, token='heat-care-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def _login(client, user, csrf_token='heat-care-csrf'):
    from flask import g

    with client.session_transaction() as sess:
        sess['_user_id'] = user.get_id()
        sess['_fresh'] = True
        sess['_csrf_token'] = csrf_token
    # db_session 保持同一 app_context；清掉 Flask-Login 缓存才能切换账号。
    if hasattr(g, '_login_user'):
        delattr(g, '_login_user')
    return csrf_token


def _auth(user_id):
    return {'Authorization': f'Bearer {create_api_token(user_id, name="heat-care")}'}


def _api_headers(csrf_token):
    return {'X-CSRF-Token': csrf_token}


def _data(response):
    body = response.get_json() or {}
    return body.get('data') or {}


def test_family_self_complete_assisted_is_only_success(app, client, db_session):
    caregiver = _user('heat_self_care')
    _member, pair, _created = _enroll(caregiver, '母亲', '母亲', '都昌')
    pair_id = pair.id
    csrf = _login(client, caregiver)

    created = client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair_id, 'category': 'cannot_complete'},
        headers=_api_headers(csrf),
    )
    assert created.status_code == 200, created.get_data(as_text=True)
    body = _data(created)
    assert body['status'] == 'pending_ack'
    assert body['status_label'] == '等待接手'
    help_id = body['id']
    version = body['version']

    acked = client.post(
        f'/api/v1/help-requests/{help_id}/ack',
        json={'expected_version': version},
        headers=_api_headers(csrf),
    )
    assert acked.status_code == 200, acked.get_data(as_text=True)
    ack_body = _data(acked)
    assert ack_body['status'] == 'acknowledged'

    started = client.post(
        f'/api/v1/help-requests/{help_id}/start',
        json={'expected_version': ack_body['version']},
        headers=_api_headers(csrf),
    )
    assert started.status_code == 200, started.get_data(as_text=True)
    start_body = _data(started)
    assert start_body['status'] == 'in_progress'

    resolved = client.post(
        f'/api/v1/help-requests/{help_id}/resolve',
        json={'expected_version': start_body['version'], 'resolution_code': 'assisted'},
        headers=_api_headers(csrf),
    )
    assert resolved.status_code == 200, resolved.get_data(as_text=True)
    resolved_body = _data(resolved)
    assert resolved_body['status'] == 'resolved'
    assert resolved_body['resolution_code'] == 'assisted'
    assert resolved_body['outcome_success'] is True

    assert resolution_success('assisted') is True
    assert all(
        spec['success'] is False
        for code, spec in RESOLUTION_DEFINITIONS.items()
        if code != 'assisted'
    )
    assert resolution_success('transferred_confirmed') is False
    assert resolution_success('unreachable') is False


def test_request_doctor_support_returns_pending_ack(app, client, db_session):
    caregiver = _user('heat_ask_doctor')
    stranger = _user('heat_stranger_read')
    _member, pair, _created = _enroll(caregiver, '父亲', '父亲', '都昌')
    pair_id = pair.id
    csrf = _login(client, caregiver)

    created = client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair_id, 'category': 'need_checkin'},
        headers=_api_headers(csrf),
    )
    body = _data(created)
    help_id = body['id']

    acked = client.post(
        f'/api/v1/help-requests/{help_id}/ack',
        json={'expected_version': body['version']},
        headers=_api_headers(csrf),
    )
    ack_body = _data(acked)

    support = client.post(
        f'/api/v1/help-requests/{help_id}/request-support',
        json={'expected_version': ack_body['version'], 'support_role': 'doctor'},
        headers=_api_headers(csrf),
    )
    assert support.status_code == 200, support.get_data(as_text=True)
    support_body = _data(support)
    assert support_body['status'] == 'pending_ack'
    assert support_body['assignee_user_id'] is None
    assert support_body['requested_support_role'] == 'doctor'
    assert '等待医生' in (support_body.get('status_label') or '')

    detail = client.get(f'/api/v1/help-requests/{help_id}')
    assert detail.status_code == 200
    assert _data(detail)['id'] == help_id

    _login(client, stranger)
    hidden = client.get(f'/api/v1/help-requests/{help_id}')
    assert hidden.status_code == 404


def test_doctor_scope_and_transferred_confirmed_not_success(app, client, db_session):
    family = _user('heat_family_owner')
    other = _user('heat_other_family')
    doctor = _user('heat_doctor', role='doctor')
    member, pair, _created = _enroll(family, '王桂英', '母亲', '都昌')
    _other_member, other_pair, _ = _enroll(other, '周溪伯', '父亲', '周溪')
    family_pair_id = pair.id
    other_pair_id = other_pair.id
    member_name = member.name

    family_csrf = _login(client, family)
    family_help = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': family_pair_id, 'category': 'cannot_complete'},
        headers=_api_headers(family_csrf),
    ))
    family_acked = _data(client.post(
        f'/api/v1/help-requests/{family_help["id"]}/ack',
        json={'expected_version': family_help['version']},
        headers=_api_headers(family_csrf),
    ))
    family_support = _data(client.post(
        f'/api/v1/help-requests/{family_help["id"]}/request-support',
        json={'expected_version': family_acked['version'], 'support_role': 'doctor'},
        headers=_api_headers(family_csrf),
    ))
    doctor_ticket_id = family_support['id']

    other_csrf = _login(client, other)
    other_created = client.post(
        '/api/v1/help-requests',
        json={'pair_id': other_pair_id, 'category': 'need_cooling'},
        headers=_api_headers(other_csrf),
    )
    assert other_created.status_code == 200, other_created.get_data(as_text=True)
    other_help = _data(other_created)
    family_only_id = other_help['id']
    assert other_help['requested_support_role'] in (None, '')

    doctor_csrf = _login(client, doctor)
    listed = client.get('/api/v1/help-requests?status=open')
    assert listed.status_code == 200
    ids = [item['id'] for item in _data(listed).get('items') or []]
    assert doctor_ticket_id in ids
    assert family_only_id not in ids
    assert all(item.get('requested_support_role') == 'doctor' for item in _data(listed).get('items') or [])

    cannot_ack_family = client.post(
        f'/api/v1/help-requests/{family_only_id}/ack',
        json={'expected_version': other_help['version']},
        headers=_api_headers(doctor_csrf),
    )
    assert cannot_ack_family.status_code == 404

    health = client.get('/family-members', follow_redirects=True)
    assert health.status_code in {200, 302, 403, 404}
    assert member_name not in health.get_data(as_text=True)

    acked = client.post(
        f'/api/v1/help-requests/{doctor_ticket_id}/ack',
        json={'expected_version': family_support['version']},
        headers=_api_headers(doctor_csrf),
    )
    assert acked.status_code == 200, acked.get_data(as_text=True)
    ack_body = _data(acked)
    assert ack_body['status'] == 'acknowledged'
    assert ack_body['assignee_user_id'] == doctor.id

    resolved = client.post(
        f'/api/v1/help-requests/{doctor_ticket_id}/resolve',
        json={
            'expected_version': ack_body['version'],
            'resolution_code': 'transferred_confirmed',
        },
        headers=_api_headers(doctor_csrf),
    )
    assert resolved.status_code == 200, resolved.get_data(as_text=True)
    resolved_body = _data(resolved)
    assert resolved_body['resolution_code'] == 'transferred_confirmed'
    assert resolved_body['outcome_success'] is False
    assert resolved_body['status'] == 'resolved'


def test_unacked_doctor_ticket_stays_open_across_day(app, client, db_session):
    caregiver = _user('heat_wait_doctor')
    _member, pair, _created = _enroll(caregiver, '外婆', '外婆', '都昌')
    pair_id = pair.id
    caregiver_id = caregiver.id
    mp_headers = _auth(caregiver.id)
    csrf = _login(client, caregiver)

    created = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair_id, 'category': 'cannot_complete'},
        headers=_api_headers(csrf),
    ))
    acked = _data(client.post(
        f'/api/v1/help-requests/{created["id"]}/ack',
        json={'expected_version': created['version']},
        headers=_api_headers(csrf),
    ))
    support = _data(client.post(
        f'/api/v1/help-requests/{created["id"]}/request-support',
        json={'expected_version': acked['version'], 'support_role': 'doctor'},
        headers=_api_headers(csrf),
    ))
    help_id = support['id']
    assert support['status'] == 'pending_ack'

    row = HelpRequest.query.filter_by(public_id=help_id).one()
    past = utcnow() - timedelta(hours=26)
    row.created_at = past
    row.updated_at = past
    db.session.commit()

    pending = client.get('/mp/api/v1/pending', headers=mp_headers)
    assert pending.status_code == 200
    pending_ids = [item['id'] for item in (pending.get_json() or {}).get('data', {}).get('help_requests') or []]
    assert help_id in pending_ids

    opened = client.get('/api/v1/help-requests?status=open')
    assert opened.status_code == 200
    open_ids = [item['id'] for item in _data(opened).get('items') or []]
    assert help_id in open_ids
    assert HelpRequest.query.filter_by(public_id=help_id).one().status == 'pending_ack'

    resolve_resp = client.post(
        f'/api/v1/help-requests/{help_id}/resolve',
        json={'expected_version': support['version'], 'resolution_code': 'assisted'},
        headers=_api_headers(csrf),
    )
    assert resolve_resp.status_code == 409, resolve_resp.get_data(as_text=True)
    assert (resolve_resp.get_json() or {}).get('error') == 'invalid_transition'
    assert HelpRequest.query.filter_by(public_id=help_id).one().status == 'pending_ack'

    doctor = _user('heat_wait_md', role='doctor')
    doctor_csrf = _login(client, doctor)
    doctor_resolve = client.post(
        f'/api/v1/help-requests/{help_id}/resolve',
        json={'expected_version': support['version'], 'resolution_code': 'assisted'},
        headers=_api_headers(doctor_csrf),
    )
    assert doctor_resolve.status_code == 409, doctor_resolve.get_data(as_text=True)
    assert (doctor_resolve.get_json() or {}).get('error') == 'invalid_transition'
    assert HelpRequest.query.filter_by(public_id=help_id).one().status == 'pending_ack'

    stranger = _user('heat_wait_stranger')
    stranger_csrf = _login(client, stranger)
    stranger_resolve = client.post(
        f'/api/v1/help-requests/{help_id}/resolve',
        json={'expected_version': support['version'], 'resolution_code': 'assisted'},
        headers=_api_headers(stranger_csrf),
    )
    assert stranger_resolve.status_code == 404
    assert HelpRequest.query.filter_by(public_id=help_id).one().status == 'pending_ack'

    closed = client.post(
        f'/mp/api/v1/pairs/{pair_id}/events',
        json={'stage': 'closed'},
        headers=mp_headers,
    )
    assert closed.status_code == 409, closed.get_data(as_text=True)
    assert (closed.get_json() or {}).get('error') == 'invalid_transition'
    assert HelpRequest.query.filter_by(public_id=help_id).one().status == 'pending_ack'

    owner = db.session.get(User, caregiver_id)
    live_pair = db.session.get(Pair, pair_id)
    try:
        apply_pair_help_stage(owner, live_pair, 'closed', commit=True)
        assert False, 'pending_ack must not close via pair stage'
    except HelpRequestError as exc:
        assert exc.code == 'invalid_transition'
        assert exc.status_code == 409
    assert HelpRequest.query.filter_by(public_id=help_id).one().status == 'pending_ack'


def test_stranger_volunteer_and_admin_cannot_act_or_publish(app, client, db_session):
    caregiver = _user('heat_acl_owner')
    stranger = _user('heat_acl_stranger')
    volunteer = _user('heat_acl_volunteer')
    admin = _user('heat_acl_admin', role='admin')
    _member, pair, _created = _enroll(caregiver, '舅舅', '舅舅', '都昌')
    csrf = _login(client, caregiver)
    created = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair.id, 'category': 'other'},
        headers=_api_headers(csrf),
    ))
    help_id = created['id']
    version = created['version']

    for actor in (stranger, volunteer):
        actor_csrf = _login(client, actor)
        headers = _api_headers(actor_csrf)
        assert client.get(f'/api/v1/help-requests/{help_id}').status_code == 404
        assert client.post(
            f'/api/v1/help-requests/{help_id}/ack',
            json={'expected_version': version},
            headers=headers,
        ).status_code == 404
        assert client.post(
            f'/api/v1/help-requests/{help_id}/resolve',
            json={'expected_version': version, 'resolution_code': 'assisted'},
            headers=headers,
        ).status_code == 404

    admin_csrf = _login(client, admin)
    inbox = client.get('/doctor/content')
    assert inbox.status_code == 404
    publish = client.post(
        '/doctor/content/new',
        data={
            'title': '高温饮水',
            'body': '少量多次饮水。',
            'source': '内部',
            'kind': 'template',
            'csrf_token': admin_csrf,
        },
    )
    assert publish.status_code == 404


def test_duplicate_enroll_and_profiles_not_merged(app, client, db_session):
    caregiver = _user('heat_enroll_owner')
    member, pair, created = _enroll(caregiver, '李大爷', '父亲', '都昌')
    assert created is True
    reused, created_again = enroll_weather_care(
        caregiver,
        member,
        location_query='都昌',
        commit=True,
    )
    assert created_again is False
    assert reused.id == pair.id

    duplicate = find_recent_duplicate_member(caregiver.id, '李大爷', '父亲', '都昌', window_seconds=120)
    assert duplicate is not None
    dup_member, dup_pair = duplicate
    assert dup_member.id == member.id
    assert dup_pair.id == pair.id

    other_member, other_pair, other_created = _enroll(caregiver, '李大爷', '父亲', '周溪')
    assert other_created is True
    assert other_member.id != member.id
    assert other_pair.id != pair.id
    assert FamilyMember.query.filter_by(user_id=caregiver.id, name='李大爷').count() == 2
    loc_a = find_recent_duplicate_member(caregiver.id, '李大爷', '父亲', '都昌')
    loc_b = find_recent_duplicate_member(caregiver.id, '李大爷', '父亲', '周溪')
    assert loc_a[0].id == member.id
    assert loc_b[0].id == other_member.id

    web_user = _user('heat_web_no_join')
    csrf = _login(client, web_user)
    response = client.post(
        '/family-members',
        data={
            'name': '独立档案',
            'relation': '母亲',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    created_member = FamilyMember.query.filter_by(user_id=web_user.id, name='独立档案').one()
    active_pairs = Pair.query.filter_by(member_id=created_member.id, status='active').count()
    assert active_pairs == 0


def test_reached_elder_alias_serializes_as_assisted(app, client, db_session):
    caregiver = _user('heat_alias_owner')
    _member, pair, _created = _enroll(caregiver, '奶奶', '奶奶', '都昌')
    csrf = _login(client, caregiver)
    created = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair.id, 'category': 'cannot_complete'},
        headers=_api_headers(csrf),
    ))
    acked = _data(client.post(
        f'/api/v1/help-requests/{created["id"]}/ack',
        json={'expected_version': created['version']},
        headers=_api_headers(csrf),
    ))
    resolved = client.post(
        f'/api/v1/help-requests/{created["id"]}/resolve',
        json={'expected_version': acked['version'], 'resolution_code': 'reached_elder'},
        headers=_api_headers(csrf),
    )
    assert resolved.status_code == 200, resolved.get_data(as_text=True)
    body = _data(resolved)
    assert body['resolution_code'] == 'assisted'
    assert body['outcome_success'] is True
    row = HelpRequest.query.filter_by(public_id=created['id']).one()
    assert row.resolution_code == 'assisted'


def test_capabilities_not_emergency_channel(app, client, db_session):
    response = client.get('/api/v1/capabilities')
    assert response.status_code == 200
    data = _data(response)
    features = data.get('features') or {}
    assert features.get('not_emergency_channel') is True
    disclaimer = data.get('disclaimer') or ''
    assert '不是实时急救通道' in disclaimer


def test_web_resolve_without_resolution_code_is_400(app, client, db_session):
    caregiver = _user('heat_resolve_code')
    _member, pair, _created = _enroll(caregiver, '叔叔', '叔叔', '都昌')
    csrf = _login(client, caregiver)
    created = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair.id, 'category': 'cannot_complete'},
        headers=_api_headers(csrf),
    ))
    acked = _data(client.post(
        f'/api/v1/help-requests/{created["id"]}/ack',
        json={'expected_version': created['version']},
        headers=_api_headers(csrf),
    ))
    missing = client.post(
        f'/api/v1/help-requests/{created["id"]}/resolve',
        json={'expected_version': acked['version']},
        headers=_api_headers(csrf),
    )
    assert missing.status_code == 400
    row = HelpRequest.query.filter_by(public_id=created['id']).one()
    assert row.status == 'acknowledged'


def test_admin_and_volunteer_cannot_resolve_family_acked_ticket(app, client, db_session):
    caregiver = _user('heat_acl_resolve_owner')
    volunteer = _user('heat_acl_resolve_vol')
    admin = _user('heat_acl_resolve_admin', role='admin')
    _member, pair, _created = _enroll(caregiver, '姨妈', '姨妈', '都昌')
    csrf = _login(client, caregiver)
    invite, code = create_invite(caregiver, pair, 'volunteer')
    db.session.commit()
    consume_invite(volunteer, code)
    db.session.commit()

    created = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': pair.id, 'category': 'cannot_complete'},
        headers=_api_headers(csrf),
    ))
    acked = _data(client.post(
        f'/api/v1/help-requests/{created["id"]}/ack',
        json={'expected_version': created['version']},
        headers=_api_headers(csrf),
    ))
    assert acked['status'] == 'acknowledged'
    assert acked['assignee_user_id'] == caregiver.id

    for actor in (volunteer, admin):
        actor_csrf = _login(client, actor)
        closed = client.post(
            f'/api/v1/help-requests/{created["id"]}/resolve',
            json={'expected_version': acked['version'], 'resolution_code': 'assisted'},
            headers=_api_headers(actor_csrf),
        )
        assert closed.status_code == 404, actor.username
    assert HelpRequest.query.filter_by(public_id=created['id']).one().status == 'acknowledged'


def test_doctor_with_own_family_does_not_see_foreign_nondoctor_history(app, client, db_session):
    doctor = _user('heat_doc_own_family', role='doctor')
    other = _user('heat_doc_other_family')
    _own_member, own_pair, _ = _enroll(doctor, '自己的母亲', '母亲', '都昌')
    _other_member, other_pair, _ = _enroll(other, '周溪伯', '父亲', '周溪')

    other_csrf = _login(client, other)
    first = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': other_pair.id, 'category': 'cannot_complete'},
        headers=_api_headers(other_csrf),
    ))
    first_acked = _data(client.post(
        f'/api/v1/help-requests/{first["id"]}/ack',
        json={'expected_version': first['version']},
        headers=_api_headers(other_csrf),
    ))
    first_resolved = client.post(
        f'/api/v1/help-requests/{first["id"]}/resolve',
        json={'expected_version': first_acked['version'], 'resolution_code': 'assisted'},
        headers=_api_headers(other_csrf),
    )
    assert first_resolved.status_code == 200

    second = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': other_pair.id, 'category': 'need_checkin'},
        headers=_api_headers(other_csrf),
    ))
    second_acked = _data(client.post(
        f'/api/v1/help-requests/{second["id"]}/ack',
        json={'expected_version': second['version']},
        headers=_api_headers(other_csrf),
    ))
    support = _data(client.post(
        f'/api/v1/help-requests/{second["id"]}/request-support',
        json={'expected_version': second_acked['version'], 'support_role': 'doctor'},
        headers=_api_headers(other_csrf),
    ))

    doctor_csrf = _login(client, doctor)
    own_help = _data(client.post(
        '/api/v1/help-requests',
        json={'pair_id': own_pair.id, 'category': 'other'},
        headers=_api_headers(doctor_csrf),
    ))
    listed = client.get('/api/v1/help-requests?status=all')
    assert listed.status_code == 200
    ids = [item['id'] for item in _data(listed).get('items') or []]
    assert support['id'] in ids
    assert own_help['id'] in ids
    assert first['id'] not in ids


def test_web_pair_copy_is_heat_only(app, client, db_session, monkeypatch):
    from core.time_utils import utcnow
    from services.user import caregiver_service

    caregiver = _user('heat_copy_only')
    _enroll(caregiver, '伯母', '伯母', '都昌')
    _login(client, caregiver)
    observed = utcnow().isoformat()
    cool_weather = {
        'temperature': 26.0,
        'temperature_max': 28.0,
        'temperature_min': 22.0,
        'humidity': 60.0,
        'pressure': 1008.0,
        'weather_condition': '多云',
        'wind_speed': 2.0,
        'aqi': 40,
        'pm25': 18,
        'air_quality_available': True,
        'data_source': 'QWeather',
        'observed_at': observed,
        'air_observed_at': observed,
        'quality_version': 1,
        'is_mock': False,
    }
    monkeypatch.setattr(
        caregiver_service,
        'resolve_location',
        lambda _label: {'location_code': '101240201', 'display_name': '都昌', 'provider': 'map'},
    )
    monkeypatch.setattr(
        caregiver_service,
        'get_weather_with_cache',
        lambda _location: (cool_weather, False),
    )

    response = client.get('/pairs')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '复制行动链接说明' in body
    assert '复制提醒话术' not in body
    assert '未触发高温' in body
    assert '日常提醒' not in body
    assert '今天就记一件事' not in body
