# -*- coding: utf-8 -*-
"""微信登录、健康同意、公开聚合、家庭邀请 HTTP 与旧接口兼容。"""
from datetime import timedelta

from core.db_models import User
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import utcnow
from core.usage import create_api_token
from services.family_access import create_invite
from services.miniprogram_auth import current_privacy_version, login_with_wechat_code


def _user(username):
    user = User(username=username, role='user')
    user.set_password('pass12344')
    db.session.add(user)
    db.session.commit()
    return user


def _pair(user, code='80220001'):
    from core.db_models import Pair

    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code=f'elder-{user.username}',
        short_code=code,
        short_code_hash=hash_short_code(code),
        status='active',
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db.session.add(pair)
    db.session.commit()
    return pair


def _auth(user_id):
    return {'Authorization': f'Bearer {create_api_token(user_id, name="mp-care")}'}


def test_wechat_login_privacy_428_then_success(app, client, db_session, monkeypatch):
    monkeypatch.setattr(
        'services.miniprogram_auth.exchange_wechat_code',
        lambda code: 'openid-test-abc',
    )
    required = None
    with app.app_context():
        required = current_privacy_version()

    denied = client.post(
        '/mp/api/v1/auth/wechat',
        json={'code': 'wx-code', 'privacy_consent_version': 'old-version'},
    )
    assert denied.status_code == 428
    body = denied.get_json()
    assert body['error'] == 'privacy_consent_required'
    assert body['required_privacy_consent_version'] == required
    assert body['data']['required_privacy_consent_version'] == required

    ok = client.post(
        '/mp/api/v1/auth/wechat',
        json={'code': 'wx-code', 'privacy_consent_version': required},
    )
    assert ok.status_code == 200
    data = ok.get_json()['data']
    token = data['session_token']
    assert token
    me = client.get('/mp/api/v1/me', headers={'Authorization': f'Bearer {token}'})
    assert me.status_code == 200

    logout = client.post('/mp/api/v1/auth/logout', headers={'Authorization': f'Bearer {token}'})
    assert logout.status_code == 200
    after = client.get('/mp/api/v1/me', headers={'Authorization': f'Bearer {token}'})
    assert after.status_code == 401


def test_wechat_delete_me_requires_confirm_and_revokes(app, client, db_session, monkeypatch):
    monkeypatch.setattr(
        'services.miniprogram_auth.exchange_wechat_code',
        lambda code: 'openid-delete-me',
    )
    with app.app_context():
        required = current_privacy_version()
    login = client.post(
        '/mp/api/v1/auth/wechat',
        json={'code': 'wx-code-2', 'privacy_consent_version': required},
    )
    token = login.get_json()['data']['session_token']
    missing = client.delete('/mp/api/v1/me', json={}, headers={'Authorization': f'Bearer {token}'})
    assert missing.status_code == 400
    assert missing.get_json()['error'] == 'delete_confirmation_required'
    deleted = client.delete(
        '/mp/api/v1/me',
        json={'confirm': True},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert deleted.status_code == 200
    assert deleted.get_json()['data']['deleted'] is True
    after = client.get('/mp/api/v1/me', headers={'Authorization': f'Bearer {token}'})
    assert after.status_code == 401


def test_health_consent_gate_on_diary_not_on_pending(app, client, db_session):
    with app.app_context():
        user = _user('care_consent')
        pair = _pair(user, code='80221111')
        headers = _auth(user.id)
        pair_id = pair.id
    pending = client.get('/mp/api/v1/pending', headers=headers)
    assert pending.status_code == 200
    blocked = client.get(f'/mp/api/v1/health/diary?pair_id={pair_id}', headers=headers)
    assert blocked.status_code == 428
    assert blocked.get_json()['error'] == 'health_sensitive_consent_required'
    with app.app_context():
        required = current_privacy_version()
    granted = client.post(
        '/mp/api/v1/health-consent',
        json={'consent': True, 'health_consent_version': required},
        headers=headers,
    )
    assert granted.status_code == 200
    diary = client.get(f'/mp/api/v1/health/diary?pair_id={pair_id}', headers=headers)
    assert diary.status_code == 200


def test_assessment_hold_does_not_return_probability(app, client, db_session):
    with app.app_context():
        user = _user('care_assess')
        pair = _pair(user, code='80222222')
        headers = _auth(user.id)
        pair_id = pair.id
        required = current_privacy_version()
    client.post(
        '/mp/api/v1/health-consent',
        json={'consent': True, 'health_consent_version': required},
        headers=headers,
    )
    created = client.post(
        '/mp/api/v1/health/assessment',
        json={
            'pair_id': pair_id,
            'outdoor_exposure': 'low',
            'symptom_level': 'none',
            'hydration': 'good',
            'medication_adherence': 'good',
            'sleep_quality': 'good',
        },
        headers=headers,
    )
    assert created.status_code == 201
    assessment = created.get_json()['data']['assessment']
    assert assessment['risk_score'] is None
    assert assessment['risk_level'] == '已记录'


def test_public_community_bundle_and_verified_cooling_only_coords(app, client, db_session):
    from core.db_models import CoolingResource

    with app.app_context():
        unverified = CoolingResource(
            community_code='都昌',
            name='未核验点',
            latitude=29.27,
            longitude=116.20,
            is_active=True,
            verify_status='unverified',
        )
        verified = CoolingResource(
            community_code='都昌',
            name='已核验点',
            latitude=29.27,
            longitude=116.20,
            is_active=True,
            last_verified_at=utcnow(),
            verify_status='verified',
        )
        db.session.add_all([unverified, verified])
        db.session.commit()
    bundle = client.get('/mp/api/v1/public/community')
    assert bundle.status_code == 200
    data = bundle.get_json()['data']
    assert data['not_personal_risk'] is True
    by_name = {item['name']: item for item in data['cooling']}
    assert by_name['未核验点']['latitude'] is None
    assert by_name['已核验点']['latitude'] == 29.27


def test_family_invite_http_preview_does_not_consume(app, client, db_session):
    with app.app_context():
        owner = _user('invite_http_owner')
        joiner = _user('invite_http_joiner')
        pair = _pair(owner, code='80223333')
        invite, plain = create_invite(owner, pair, 'caregiver', ttl_hours=2, max_uses=1)
        db.session.commit()
        owner_headers = _auth(owner.id)
        joiner_headers = _auth(joiner.id)
        code = plain
    preview = client.get(f'/mp/api/v1/family-invites/{code}', headers=joiner_headers)
    assert preview.status_code == 200
    assert preview.get_json()['data']['consumes'] is False
    second = client.get(f'/mp/api/v1/family-invites/{code}', headers=joiner_headers)
    assert second.status_code == 200
    accept = client.post(f'/mp/api/v1/family-invites/{code}/accept', headers=joiner_headers)
    assert accept.status_code == 200
    replay = client.post(f'/mp/api/v1/family-invites/{code}/accept', headers=joiner_headers)
    assert replay.status_code == 409


def test_legacy_action_help_returns_id_and_status(app, client, db_session):
    with app.app_context():
        user = _user('legacy_help')
        pair = _pair(user, code='80224444')
        headers = _auth(user.id)
        pair_id = pair.id
    created = client.post(f'/mp/api/v1/actions/{pair_id}/help', json={'note': '需要帮忙'}, headers=headers)
    assert created.status_code == 200
    data = created.get_json()['data']
    assert data['help_flag'] is True
    assert data['id']
    assert data['status'] == 'pending_ack'
    pending = client.get('/mp/api/v1/pending', headers=headers)
    assert pending.status_code == 200
    items = pending.get_json()['data']['help_requests']
    assert any(item['id'] == data['id'] for item in items)


def test_web_invite_page_get_does_not_consume(app, authenticated_client, db_session):
    with app.app_context():
        owner = _user('web_invite_owner')
        pair = _pair(owner, code='80225555')
        _invite, plain = create_invite(owner, pair, 'caregiver', ttl_hours=2, max_uses=1)
        db.session.commit()
        code = plain
    page = authenticated_client.get(f'/caregiver/invite?code={code}')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert '确认加入家庭' in html
    assert '不会消耗' in html or '不会消耗次数' in html
