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


def _create_user(app, username, password='SessionPassword1!'):
    from core.db_models import User
    from core.extensions import db

    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User(username=username, role='user')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return int(user.id)


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


def test_stale_web_cookie_does_not_intercept_valid_bearer_api(app, client):
    """过期 Web 身份不能把独立 Bearer API 提前重定向到登录页。"""
    from core.db_models import User
    from core.extensions import db
    from core.usage import create_api_token

    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User(username='stale-cookie-api-user', role='user')
        user.set_password('BearerPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
        plain_token = create_api_token(
            user_id,
            name='stale-web-cookie-regression',
            scopes=['miniprogram:read'],
        )

    with client.session_transaction() as flask_session:
        flask_session['_user_id'] = f'{user_id}:999'
        flask_session['_fresh'] = True

    response = client.get(
        '/mp/api/v1/me',
        headers={'Authorization': f'Bearer {plain_token}'},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json()['data']['username'] == 'stale-cookie-api-user'


def test_logout_get_and_head_only_show_confirmation(app, client):
    """GET/HEAD 只能展示确认页，不能撤销任何已有会话。"""
    from core.db_models import User
    from core.extensions import db

    user_id = _create_user(app, 'logout-confirm-user')
    other_client = app.test_client()
    csrf_token, login_response = _login(
        client,
        'logout-confirm-user',
        'SessionPassword1!',
        remember=True,
    )
    _login(other_client, 'logout-confirm-user', 'SessionPassword1!')
    assert 'remember_token=' in _cookie_headers(login_response)

    get_response = client.get('/logout', follow_redirects=False)
    head_response = client.head('/logout', follow_redirects=False)

    assert get_response.status_code == 200
    assert '确认退出登录' in get_response.get_data(as_text=True)
    assert 'method="POST"' in get_response.get_data(as_text=True)
    assert head_response.status_code == 200
    assert head_response.get_data() == b''
    with app.app_context():
        assert db.session.get(User, user_id).auth_version == 1
    with client.session_transaction() as flask_session:
        assert flask_session['_user_id'] == f'{user_id}:1'
        assert flask_session['_csrf_token'] == csrf_token
    assert other_client.get('/profile', follow_redirects=False).status_code == 200


def test_logout_post_requires_csrf_before_revoking_sessions(app, client):
    """缺失 CSRF 的退出请求不能递增认证版本。"""
    from core.db_models import User
    from core.extensions import db

    user_id = _create_user(app, 'logout-csrf-user')
    _login(client, 'logout-csrf-user', 'SessionPassword1!')

    response = client.post('/logout', follow_redirects=False)

    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(User, user_id).auth_version == 1
    assert client.get('/profile', follow_redirects=False).status_code == 200


def test_logout_post_revokes_other_session_and_remember_cookie(app, client):
    """主动退出撤销 Web 身份，同时保持已验证的小程序会话。"""
    from core.db_models import MiniProgramIdentity, MiniProgramSession, User
    from core.extensions import db
    from core.time_utils import utcnow
    from services.miniprogram_auth import issue_miniprogram_session

    user_id = _create_user(app, 'logout-revoke-user')
    app.config.update(
        WX_MINIPROGRAM_SESSION_SECRET='s' * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION='privacy-v1',
    )
    with app.app_context():
        user = db.session.get(User, user_id)
        identity = MiniProgramIdentity(
            user_id=user_id,
            openid_hash='logout-preserved-mini-openid-hash',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=utcnow(),
            binding_auth_version=1,
        )
        db.session.add(identity)
        db.session.flush()
        mini_payload = issue_miniprogram_session(identity, user)
        db.session.commit()
        identity_id = int(identity.id)
        mini_session_id = int(MiniProgramSession.query.one().id)
        mini_headers = {
            'Authorization': f"Bearer {mini_payload['session_token']}"
        }

    stale_session_client = app.test_client()
    stale_remember_client = app.test_client()
    mini_client = app.test_client()
    csrf_token, _login_response = _login(
        client,
        'logout-revoke-user',
        'SessionPassword1!',
        remember=True,
    )
    _login(stale_session_client, 'logout-revoke-user', 'SessionPassword1!')
    _login(
        stale_remember_client,
        'logout-revoke-user',
        'SessionPassword1!',
        remember=True,
    )
    assert mini_client.get(
        '/mp/api/v1/me',
        headers=mini_headers,
    ).status_code == 200

    response = client.post(
        '/logout',
        data={'csrf_token': csrf_token},
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers['Location'].endswith('/')
    assert 'remember_token=;' in _cookie_headers(response)
    with app.app_context():
        assert db.session.get(User, user_id).auth_version == 2
        assert (
            db.session.get(MiniProgramIdentity, identity_id).binding_auth_version
            == 2
        )
        assert db.session.get(MiniProgramSession, mini_session_id).revoked_at is None

    mini_response = mini_client.get(
        '/mp/api/v1/me',
        headers=mini_headers,
    )
    assert mini_response.status_code == 200
    assert mini_response.get_json()['data']['display_name'] == 'logout-revoke-user'

    stale_session_response = stale_session_client.get(
        '/profile',
        follow_redirects=False,
    )
    assert stale_session_response.status_code in (301, 302)
    assert '/login' in stale_session_response.headers['Location']

    # 删除短期会话，只保留退出前签发的 remember cookie 进行重放。
    with stale_remember_client.session_transaction() as flask_session:
        flask_session.pop('_user_id', None)
        flask_session.pop('_fresh', None)
        flask_session.pop('_id', None)
    remembered_response = stale_remember_client.get(
        '/profile',
        follow_redirects=False,
    )
    assert remembered_response.status_code in (301, 302)
    assert '/login' in remembered_response.headers['Location']


def test_logout_does_not_reactivate_identity_invalidated_by_password_change(
    app,
    client,
):
    """退出只能迁移当前有效绑定，不能恢复改密后遗留的旧微信身份。"""
    from core.db_models import MiniProgramIdentity, User
    from core.extensions import db
    from core.time_utils import utcnow

    user_id = _create_user(app, 'logout-stale-mini-user')
    with app.app_context():
        user = db.session.get(User, user_id)
        user.auth_version = 2
        identity = MiniProgramIdentity(
            user_id=user_id,
            openid_hash='logout-stale-mini-openid-hash',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=utcnow(),
            binding_auth_version=1,
        )
        db.session.add(identity)
        db.session.commit()
        identity_id = int(identity.id)

    csrf_token, _response = _login(
        client,
        'logout-stale-mini-user',
        'SessionPassword1!',
    )
    response = client.post(
        '/logout',
        data={'csrf_token': csrf_token},
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    with app.app_context():
        assert db.session.get(User, user_id).auth_version == 3
        assert (
            db.session.get(MiniProgramIdentity, identity_id).binding_auth_version
            == 1
        )


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
            'confirm_password': 'NewPassword2!',
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
