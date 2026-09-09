# -*- coding: utf-8 -*-
"""照护角色正向门禁与工作台上下文回归测试。"""

import pytest


def _create_user(db_session, username, role, uid=None, push_enabled=False):
    from core.db_models import User

    user = User(
        username=username,
        role=role,
        wxpusher_uid=uid,
        push_enabled=push_enabled,
    )
    user.set_password('StrongPass123')
    db_session.add(user)
    db_session.commit()
    return user


def _login_as(client, user, csrf_token='csrf-care-role'):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_pair(db_session, caregiver_id, suffix='01'):
    from core.db_models import Pair

    pair = Pair(
        caregiver_id=caregiver_id,
        community_code='牛家垄周村',
        location_query='牛家垄周村',
        elder_code=f'elder-{suffix}',
        short_code=f'100000{suffix}',
        status='active',
    )
    db_session.add(pair)
    db_session.commit()
    return pair


def test_care_roles_constant_is_explicit_positive_set():
    from services.user._common import CARE_ROLES

    assert CARE_ROLES == frozenset({'user', 'caregiver', 'admin'})
    assert 'community' not in CARE_ROLES
    assert 'guest' not in CARE_ROLES


@pytest.mark.parametrize('role', ['user', 'caregiver', 'admin'])
def test_care_roles_can_open_pairs_and_legacy_caregiver_redirects(client, db_session, role):
    user = _create_user(db_session, f'care_entry_{role}', role)
    _login_as(client, user)

    pairs_response = client.get('/pairs', follow_redirects=False)
    legacy_response = client.get('/caregiver', follow_redirects=False)

    assert pairs_response.status_code == 200
    assert legacy_response.status_code == 302
    assert legacy_response.headers['Location'].endswith('/pairs')


def test_community_role_cannot_open_or_create_pairs(client, db_session):
    from core.db_models import Pair

    community_user = _create_user(db_session, 'community_care_denied', 'community')
    _login_as(client, community_user)

    get_response = client.get('/pairs', follow_redirects=False)
    legacy_get_response = client.get('/caregiver', follow_redirects=False)
    post_response = client.post(
        '/pairs',
        data={'location_query': '徐家湾', 'csrf_token': 'csrf-care-role'},
        follow_redirects=False,
    )
    legacy_create_response = client.post(
        '/caregiver/pair/create',
        data={'location_query': '徐家湾', 'csrf_token': 'csrf-care-role'},
        follow_redirects=False,
    )

    assert get_response.status_code == 302
    assert legacy_get_response.status_code == 302
    assert legacy_get_response.headers['Location'].endswith('/dashboard')
    assert post_response.status_code == 302
    assert legacy_create_response.status_code == 302
    assert Pair.query.count() == 0


def test_community_role_cannot_mutate_care_records_through_legacy_paths(client, db_session):
    from core.db_models import DailyStatus, PairActionToken
    from core.time_utils import today_local

    caregiver = _create_user(db_session, 'care_owner', 'caregiver')
    pair = _create_pair(db_session, caregiver.id, suffix='02')
    status = DailyStatus(
        pair_id=pair.id,
        status_date=today_local(),
        community_code=pair.community_code,
        relay_stage='none',
    )
    db_session.add(status)
    db_session.commit()

    community_user = _create_user(db_session, 'community_write_denied', 'community')
    _login_as(client, community_user)
    write_requests = [
        ('/pairs/{}/escalate'.format(pair.id), {}),
        ('/pairs/{}/backup'.format(pair.id), {}),
        ('/caregiver/relay/escalate', {'pair_id': str(pair.id)}),
        ('/caregiver/relay/backup', {'pair_id': str(pair.id)}),
        ('/caregiver/pair/{}/action-log'.format(pair.id), {'caregiver_actions': 'remind'}),
    ]

    for path, data in write_requests:
        response = client.post(
            path,
            data={**data, 'csrf_token': 'csrf-care-role'},
            follow_redirects=False,
        )
        assert response.status_code == 302

    token_response = client.get(
        '/caregiver/wechat_template',
        query_string={'short_code': pair.short_code, 'community_code': pair.community_code},
        follow_redirects=False,
    )
    detail_response = client.get(f'/caregiver/pair/{pair.id}', follow_redirects=False)
    assert token_response.status_code == 302
    assert detail_response.status_code == 302

    db_session.refresh(status)
    assert status.relay_stage == 'none'
    assert status.caregiver_actions is None
    assert status.caregiver_note is None
    assert PairActionToken.query.count() == 0


def test_wechat_template_never_rebuilds_action_link_from_request_host(app, db_session):
    from flask_login import login_user

    from services.user import caregiver_service

    caregiver = _create_user(db_session, 'trusted_link_owner', 'caregiver')
    pair = _create_pair(db_session, caregiver.id, suffix='04')
    app.config['PUBLIC_BASE_URL'] = 'https://trusted.example'

    with app.test_request_context(
        '/caregiver/wechat_template',
        query_string={'short_code': pair.short_code},
        base_url='https://evil.example',
    ):
        login_user(caregiver)
        generated_body = caregiver_service.caregiver_wechat_template()

    assert 'https://trusted.example/e/' in generated_body
    assert 'evil.example' not in generated_body

    with app.test_request_context(
        '/caregiver/wechat_template',
        query_string={'short_code': pair.short_code, 'token': 'provided-token'},
        base_url='https://evil.example',
    ):
        login_user(caregiver)
        existing_token_body = caregiver_service.caregiver_wechat_template()

    assert '/e/provided-token' in existing_token_body
    assert 'evil.example' not in existing_token_body

    with app.test_request_context(
        '/caregiver/wechat_template',
        query_string={'short_code': '99999999'},
        base_url='https://evil.example',
    ):
        login_user(caregiver)
        no_token_body = caregiver_service.caregiver_wechat_template()

    assert '/elder?short_code=99999999' in no_token_body
    assert 'evil.example' not in no_token_body


@pytest.mark.parametrize(
    ('pair_count', 'channel_token', 'uid', 'enabled', 'expected_state'),
    [
        (0, '', None, False, 'none'),
        (1, '', None, False, 'channel_unavailable'),
        (1, 'backend-token', None, False, 'user_setup_required'),
        (1, 'backend-token', 'UID_READY', False, 'user_setup_required'),
        (1, 'backend-token', 'UID_READY', True, 'ready'),
    ],
)
def test_pair_management_context_exposes_push_matrix_and_location_suggestions(
    app,
    db_session,
    monkeypatch,
    pair_count,
    channel_token,
    uid,
    enabled,
    expected_state,
):
    from flask_login import login_user
    from services.user import caregiver_service

    user = _create_user(
        db_session,
        f'push_state_{expected_state}_{pair_count}_{int(enabled)}',
        'caregiver',
        uid=uid,
        push_enabled=enabled,
    )
    if pair_count:
        _create_pair(db_session, user.id, suffix='03')

    monkeypatch.setattr(
        caregiver_service,
        'resolve_location',
        lambda label: {'location_code': '', 'display_name': label},
    )
    monkeypatch.setattr(
        caregiver_service,
        '_build_pair_action_link',
        lambda pair: f'/e/test-token?short_code={pair.short_code}',
    )
    app.config['WXPUSHER_APP_TOKEN'] = channel_token

    with app.test_request_context('/pairs'):
        login_user(user)
        context = caregiver_service._build_pair_management_context()

    assert context['push_notice_state'] == expected_state
    assert context['push_channel_ready'] is bool(channel_token)
    assert context['location_suggestions'] == list(app.config['COMMUNITY_COORDS_GCJ'].keys())
    assert len(context['location_suggestions']) == 16
