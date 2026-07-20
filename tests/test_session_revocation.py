# -*- coding: utf-8 -*-
"""密码修改后的全端会话撤销回归测试。"""

from datetime import timedelta


def _login(client, username, password, *, remember=False, csrf_token='session-revoke-csrf'):
    with client.session_transaction() as flask_session:
        flask_session['_csrf_token'] = csrf_token
    response = client.post(
        '/login',
        data={
            'username': username,
            'password': password,
            'csrf_token': csrf_token,
            'remember': '1' if remember else '',
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302, 303)
    return csrf_token, response


def _cookie_headers(response):
    return '\n'.join(response.headers.getlist('Set-Cookie'))


def test_real_user_legacy_numeric_cookie_is_rejected(app, client):
    """升级前的纯数字 Web 会话不能绕过认证版本。"""
    from core.db_models import User
    from core.extensions import db

    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User(username='legacy-cookie-user', role='user')
        user.set_password('LegacyPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)

    with client.session_transaction() as flask_session:
        flask_session['_user_id'] = str(user_id)
        flask_session['_fresh'] = True

    response = client.get('/profile', follow_redirects=False)

    assert response.status_code in (301, 302)
    assert '/login' in response.headers['Location']


def test_password_change_revokes_all_other_sessions_and_refreshes_current_browser(
    app,
    client,
):
    """改密后只保留已再次验证旧密码的当前 Web 浏览器。"""
    from core.db_models import (
        ApiToken,
        MiniProgramIdentity,
        MiniProgramSession,
        User,
    )
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User(username='session-revoke-owner', role='user')
        user.set_password('OldPassword1!')
        db.session.add(user)
        db.session.flush()
        now = utcnow()
        api_token = ApiToken(
            user_id=user.id,
            name='改密前绑定凭证',
            token_hash='b' * 64,
            created_at=now,
            expires_at=now + timedelta(days=30),
            scopes='miniapp:read',
            privacy_consent_version='privacy-v1',
        )
        identity = MiniProgramIdentity(
            user_id=user.id,
            openid_hash='c' * 64,
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now,
            acquisition_source='direct',
            created_at=now,
            last_login_at=now,
        )
        db.session.add_all([api_token, identity])
        db.session.flush()
        mini_session = MiniProgramSession(
            identity_id=identity.id,
            user_id=user.id,
            token_hash='d' * 64,
            privacy_consent_version='privacy-v1',
            expires_at=now + timedelta(days=30),
            created_at=now,
            last_used_at=now,
        )
        db.session.add(mini_session)
        db.session.commit()
        username = user.username
        user_id = int(user.id)
        api_token_id = int(api_token.id)
        mini_session_id = int(mini_session.id)

    stale_session_client = app.test_client()
    stale_remember_client = app.test_client()
    current_csrf, initial_login = _login(
        client,
        username,
        'OldPassword1!',
        remember=True,
    )
    assert 'remember_token=' in _cookie_headers(initial_login)
    _login(stale_session_client, username, 'OldPassword1!')
    _login(
        stale_remember_client,
        username,
        'OldPassword1!',
        remember=True,
    )

    response = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPassword1!',
            'new_password': 'NewPassword2!',
            'csrf_token': current_csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert 'remember_token=' in _cookie_headers(response)
    with client.session_transaction() as flask_session:
        assert flask_session['_user_id'] == f'{user_id}:2'

    with app.app_context():
        refreshed_user = db.session.get(User, user_id)
        refreshed_api_token = db.session.get(ApiToken, api_token_id)
        refreshed_mini_session = db.session.get(MiniProgramSession, mini_session_id)
        assert refreshed_user.auth_version == 2
        assert refreshed_user.check_password('NewPassword2!')
        assert refreshed_api_token.revoked_at is not None
        assert refreshed_mini_session.revoked_at is not None

    assert client.get('/profile', follow_redirects=False).status_code == 200

    stale_response = stale_session_client.get('/profile', follow_redirects=False)
    assert stale_response.status_code in (301, 302)
    assert '/login' in stale_response.headers['Location']

    # 删除旧浏览器的 Flask 会话，只留下它原先的 remember cookie 继续尝试恢复。
    with stale_remember_client.session_transaction() as flask_session:
        flask_session.pop('_user_id', None)
        flask_session.pop('_fresh', None)
        flask_session.pop('_id', None)
    remembered_response = stale_remember_client.get('/profile', follow_redirects=False)
    assert remembered_response.status_code in (301, 302)
    assert '/login' in remembered_response.headers['Location']
