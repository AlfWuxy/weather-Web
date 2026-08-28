# -*- coding: utf-8 -*-
"""家庭照护角色边界与统一入口回归测试。"""

import pytest


def _create_user(db_session, username, role):
    from core.db_models import User

    user = User(username=username, role=role)
    user.set_password('LongCarePassword1!')
    db_session.add(user)
    db_session.commit()
    return user


def _login_as(client, user, csrf_token='csrf-care-role'):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def test_care_roles_constant_is_positive_allowlist():
    from services.user._common import CARE_ROLES

    assert CARE_ROLES == frozenset({'user', 'caregiver', 'admin'})
    assert 'community' not in CARE_ROLES
    assert 'guest' not in CARE_ROLES


@pytest.mark.parametrize('role', ['user', 'caregiver', 'admin'])
def test_care_roles_open_pairs_and_legacy_entry_redirects(
    app,
    client,
    db_session,
    role,
):
    app.config['WECHAT_FORMAL_RUNTIME'] = False
    user = _create_user(db_session, f'care-entry-{role}', role)
    _login_as(client, user)

    pairs_response = client.get('/pairs', follow_redirects=False)
    legacy_response = client.get('/caregiver', follow_redirects=False)

    assert pairs_response.status_code == 200
    assert legacy_response.status_code in (301, 302, 303)
    assert legacy_response.headers['Location'].endswith('/pairs')


def test_community_role_cannot_read_or_create_family_care_records(
    app,
    client,
    db_session,
):
    from core.db_models import Pair

    app.config['WECHAT_FORMAL_RUNTIME'] = False
    user = _create_user(db_session, 'community-care-denied', 'community')
    _login_as(client, user)

    responses = [
        client.get('/pairs', follow_redirects=False),
        client.get('/caregiver', follow_redirects=False),
        client.post(
            '/pairs',
            data={
                'location_query': '牛家垄周村',
                'csrf_token': 'csrf-care-role',
            },
            follow_redirects=False,
        ),
        client.post(
            '/caregiver/pair/create',
            data={
                'location_query': '牛家垄周村',
                'csrf_token': 'csrf-care-role',
            },
            follow_redirects=False,
        ),
    ]

    assert all(response.status_code in (301, 302, 303) for response in responses)
    assert Pair.query.count() == 0


def test_empty_care_workspace_hides_push_setup_warning_and_lists_locations(
    app,
    client,
    db_session,
):
    app.config['WECHAT_FORMAL_RUNTIME'] = False
    app.config['FEATURE_WXPUSHER'] = True
    app.config['WXPUSHER_APP_TOKEN'] = ''
    user = _create_user(db_session, 'empty-care-workspace', 'caregiver')
    _login_as(client, user)

    response = client.get('/pairs')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '提醒不会自动发到微信' not in body
    assert '照护工作台 · 先为家人添加一个关注地点' in body
    for location in app.config['COMMUNITY_COORDS_GCJ']:
        assert f'value="{location}"' in body
