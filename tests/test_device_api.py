# -*- coding: utf-8 -*-
"""网站侧终端合同：blueprints/device_api.py + services/device_link_service.py。"""
from datetime import timedelta

from services.device_link_service import DEVICE_EVENT_NAMES, DEVICE_ONLINE_SECONDS, serialize_device

CSRF = 'device-api-csrf'
PERSONAL_DATA_KEYS = {'name', 'medications', 'phone', 'disease', 'chronic_diseases'}
ALLOWED_EVENT_NAMES = frozenset({
    'heartbeat',
    'button_pressed',
    'tts_command_sent',
    'sos_hold',
    'content_pulled',
})


def _create_user(db_session, username, role='user'):
    from core.db_models import User

    user = User(username=username, role=role)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def _enroll_owner_pair(db_session, username='device_owner'):
    from core.db_models import FamilyMember, FamilyMemberProfile
    from services.care_enrollment import enroll_weather_care

    user = _create_user(db_session, username)
    member = FamilyMember(
        user_id=user.id,
        name='张三敏',
        relation='母亲',
        age=78,
        gender='女性',
        chronic_diseases='["高血压"]',
    )
    db_session.add(member)
    db_session.flush()
    pair, _created = enroll_weather_care(user, member, location_query='都昌', commit=True)
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).one()
    profile.medications = '阿司匹林'
    db_session.commit()
    return user, member, pair


def _login(client, user, csrf_token=CSRF):
    with client.session_transaction() as sess:
        sess['_user_id'] = user.get_id()
        sess['_fresh'] = True
        sess['_csrf_token'] = csrf_token
        sess.pop('guest_id', None)
        sess.pop('guest_profile', None)
    return csrf_token


def _login_guest(client, app, csrf_token=CSRF):
    from flask import session as flask_session
    from flask_login import login_user

    from core.constants import GUEST_ID_PREFIX
    from core.guest import GuestUser

    guest_id = f'{GUEST_ID_PREFIX}device_api_guest'
    profile = {
        'username': '游客',
        'age': None,
        'gender': '未知',
        'community': '朝阳社区',
        'has_chronic_disease': False,
        'chronic_diseases': None,
    }
    guest = GuestUser(guest_id, profile)
    with client.session_transaction() as sess:
        sess['guest_id'] = guest_id
        sess['guest_profile'] = profile
        sess['_csrf_token'] = csrf_token
    with app.test_request_context('/'):
        login_user(guest)
        login_keys = {
            key: flask_session[key]
            for key in ('_user_id', '_fresh', '_id')
            if key in flask_session
        }
    with client.session_transaction() as sess:
        sess.update(login_keys)
        sess.setdefault('_user_id', guest_id)
        sess.setdefault('_fresh', True)
        sess['guest_id'] = guest_id
        sess['guest_profile'] = profile
        sess['_csrf_token'] = csrf_token
    return csrf_token, guest


def _post_json(client, path, payload, *, csrf=CSRF, headers=None):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = csrf
    hdrs = {'X-CSRF-Token': csrf}
    if headers:
        hdrs.update(headers)
    return client.post(path, json=payload, headers=hdrs)


def _authorize_device(client, pair_id, *, csrf=CSRF, label='kitchen-box'):
    return _post_json(
        client,
        '/api/v1/devices',
        {'pair_id': pair_id, 'label': label},
        csrf=csrf,
    )


def _json_keys(value):
    keys = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys.update(_json_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_json_keys(item))
    return keys


def _seed_advice(db_session):
    from core.time_utils import utcnow
    from services.advice_content_service import create_draft, publish_advice

    doctor = _create_user(db_session, 'device_doctor', role='doctor')
    live = create_draft(
        doctor,
        kind='template',
        title='高温喝水提醒',
        body='每隔一段时间喝一口温水。',
        source='平台规则',
        scenario='heat',
    )
    publish_advice(doctor, live.public_id)

    draft = create_draft(
        doctor,
        kind='template',
        title='草稿不应下发',
        body='这是未发布草稿。',
        source='平台规则',
        scenario='heat',
    )

    expired = create_draft(
        doctor,
        kind='template',
        title='过期模板',
        body='这是过期模板。',
        source='平台规则',
        scenario='heat',
    )
    publish_advice(doctor, expired.public_id)
    expired.valid_until = utcnow() - timedelta(days=1)

    future = create_draft(
        doctor,
        kind='template',
        title='未到有效期',
        body='这是未开始模板。',
        source='平台规则',
        scenario='heat',
    )
    publish_advice(doctor, future.public_id)
    future.valid_from = utcnow() + timedelta(days=7)

    science = create_draft(
        doctor,
        kind='science',
        title='科普文章',
        body='这是科普而不是短提醒模板。',
        source='平台规则',
        scenario='heat',
    )
    publish_advice(doctor, science.public_id)
    db_session.commit()
    return {
        'live': live.public_id,
        'draft': draft.public_id,
        'expired': expired.public_id,
        'future': future.public_id,
        'science': science.public_id,
    }


def _device_headers(token, csrf=CSRF):
    return {'Authorization': f'Bearer {token}', 'X-CSRF-Token': csrf}


def test_owner_authorize_device_returns_simulated_token_once(client, db_session):
    user, member, pair = _enroll_owner_pair(db_session)
    csrf = _login(client, user)

    response = _authorize_device(client, pair.id, csrf=csrf)
    assert response.status_code == 200
    body = response.get_json()
    assert body['success'] is True
    assert body['verification_mode'] == 'simulated'
    data = body['data']
    token = data['device_token']
    assert isinstance(token, str) and token
    assert data['verification_mode'] == 'simulated'
    assert data['label'] == 'kitchen-box'
    assert data['pair_id'] == pair.id
    assert 'name' not in data
    assert 'disease' not in data
    assert PERSONAL_DATA_KEYS.isdisjoint(_json_keys(body))
    assert member.name not in response.get_data(as_text=True)

    revoke = _post_json(
        client,
        f"/api/v1/devices/{data['id']}/revoke",
        {},
        csrf=csrf,
    )
    assert revoke.status_code == 200
    assert 'device_token' not in revoke.get_json()['data']


def test_stranger_cannot_authorize_same_pair(client, db_session):
    _owner, _member, pair = _enroll_owner_pair(db_session)
    stranger = _create_user(db_session, 'device_stranger')
    csrf = _login(client, stranger)

    response = _authorize_device(client, pair.id, csrf=csrf)
    assert response.status_code == 404
    body = response.get_json()
    assert body['success'] is False
    assert body['error'] == 'not_found'


def test_device_content_requires_token_and_omits_personal_data(client, db_session):
    user, member, pair = _enroll_owner_pair(db_session)
    ids = _seed_advice(db_session)
    csrf = _login(client, user)
    authorized = _authorize_device(client, pair.id, csrf=csrf)
    token = authorized.get_json()['data']['device_token']

    missing = client.get('/device/api/v1/content')
    assert missing.status_code == 401
    assert missing.get_json()['error'] == 'unauthorized'

    ok = client.get('/device/api/v1/content', headers={'Authorization': f'Bearer {token}'})
    assert ok.status_code == 200
    payload = ok.get_json()
    data = payload['data']
    notes = ''.join(data['notes'])
    assert '拉取内容不等于老人听到' in notes
    template_ids = {item['content_id'] for item in data['templates']}
    assert ids['live'] in template_ids
    assert ids['draft'] not in template_ids
    assert ids['expired'] not in template_ids
    assert ids['future'] not in template_ids
    assert ids['science'] not in template_ids
    assert PERSONAL_DATA_KEYS.isdisjoint(_json_keys(payload))
    text = ok.get_data(as_text=True)
    assert member.name not in text
    assert '阿司匹林' not in text
    assert 'device_token' not in data


def test_button_pressed_dedupes_and_rejects_heard_or_done_names(client, db_session):
    user, _member, pair = _enroll_owner_pair(db_session)
    csrf = _login(client, user)
    token = _authorize_device(client, pair.id, csrf=csrf).get_json()['data']['device_token']
    headers = _device_headers(token, csrf)

    first = _post_json(
        client,
        '/device/api/v1/events',
        {'event_name': 'button_pressed', 'client_event_id': 'btn-1'},
        csrf=csrf,
        headers=headers,
    )
    assert first.status_code == 200
    first_data = first.get_json()['data']
    assert first_data['created'] is True
    assert first_data['not_action_completed'] is True

    repeat = _post_json(
        client,
        '/device/api/v1/events',
        {'event_name': 'button_pressed', 'client_event_id': 'btn-1'},
        csrf=csrf,
        headers=headers,
    )
    assert repeat.status_code == 200
    assert repeat.get_json()['data']['created'] is False

    for bad_name in ('heard_alert', 'action_done'):
        bad = _post_json(
            client,
            '/device/api/v1/events',
            {'event_name': bad_name, 'client_event_id': f'bad-{bad_name}'},
            csrf=csrf,
            headers=headers,
        )
        assert bad.status_code == 400
        assert bad.get_json()['error'] == 'invalid_event_name'


def test_allowed_event_names_only(client, db_session):
    user, _member, pair = _enroll_owner_pair(db_session)
    csrf = _login(client, user)
    token = _authorize_device(client, pair.id, csrf=csrf).get_json()['data']['device_token']
    headers = _device_headers(token, csrf)

    assert DEVICE_EVENT_NAMES == ALLOWED_EVENT_NAMES
    for name in sorted(ALLOWED_EVENT_NAMES):
        response = _post_json(
            client,
            '/device/api/v1/events',
            {'event_name': name, 'client_event_id': f'ok-{name}'},
            csrf=csrf,
            headers=headers,
        )
        assert response.status_code == 200, name
        assert response.get_json()['data']['event_name'] == name

    rejected = _post_json(
        client,
        '/device/api/v1/events',
        {'event_name': 'action_completed', 'client_event_id': 'not-allowed'},
        csrf=csrf,
        headers=headers,
    )
    assert rejected.status_code == 400


def test_heartbeat_updates_last_heartbeat_and_serialize_online(client, db_session):
    from core.db_models import CareDevice
    from core.time_utils import utcnow

    user, _member, pair = _enroll_owner_pair(db_session)
    csrf = _login(client, user)
    created = _authorize_device(client, pair.id, csrf=csrf).get_json()['data']
    public_id = created['id']
    token = created['device_token']
    assert created['online'] is False
    assert created['last_heartbeat_at'] is None

    response = _post_json(
        client,
        '/device/api/v1/events',
        {'event_name': 'heartbeat', 'client_event_id': 'hb-1'},
        csrf=csrf,
        headers=_device_headers(token, csrf),
    )
    assert response.status_code == 200

    db_session.expire_all()
    device = CareDevice.query.filter_by(public_id=public_id).one()
    assert device.last_heartbeat_at is not None
    payload = serialize_device(device)
    assert payload['online'] is True
    assert payload['last_heartbeat_at'] is not None

    device.last_heartbeat_at = utcnow() - timedelta(seconds=DEVICE_ONLINE_SECONDS + 5)
    device.last_seen_at = device.last_heartbeat_at
    stale = serialize_device(device)
    assert stale['online'] is False


def test_revoke_then_content_is_unauthorized(client, db_session):
    user, _member, pair = _enroll_owner_pair(db_session)
    csrf = _login(client, user)
    created = _authorize_device(client, pair.id, csrf=csrf).get_json()['data']
    token = created['device_token']
    public_id = created['id']

    before = client.get('/device/api/v1/content', headers={'Authorization': f'Bearer {token}'})
    assert before.status_code == 200

    revoked = _post_json(client, f'/api/v1/devices/{public_id}/revoke', {}, csrf=csrf)
    assert revoked.status_code == 200
    assert revoked.get_json()['data']['revoked'] is True
    assert 'device_token' not in revoked.get_json()['data']

    after = client.get('/device/api/v1/content', headers={'Authorization': f'Bearer {token}'})
    assert after.status_code == 401


def test_device_occurred_at_differs_from_server_received_at(client, db_session):
    user, _member, pair = _enroll_owner_pair(db_session)
    csrf = _login(client, user)
    token = _authorize_device(client, pair.id, csrf=csrf).get_json()['data']['device_token']

    response = _post_json(
        client,
        '/device/api/v1/events',
        {
            'event_name': 'sos_hold',
            'client_event_id': 'sos-clock',
            'device_occurred_at': '2020-01-01T00:00:00+00:00',
        },
        csrf=csrf,
        headers=_device_headers(token, csrf),
    )
    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['device_occurred_at']
    assert data['server_received_at']
    assert data['device_occurred_at'] != data['server_received_at']


def test_guest_cannot_authorize_device(client, app, db_session):
    _user, _member, pair = _enroll_owner_pair(db_session)
    csrf, _guest = _login_guest(client, app)

    response = _post_json(
        client,
        '/api/v1/devices',
        {'pair_id': pair.id, 'label': 'guest-box'},
        csrf=csrf,
    )
    assert response.status_code == 403
    body = response.get_json()
    assert body['success'] is False
    assert body['error'] == 'guest_not_allowed'
