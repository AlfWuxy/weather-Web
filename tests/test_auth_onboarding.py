# -*- coding: utf-8 -*-
"""账号首登、表单重试和导航三态回归。"""

import pytest


def _set_csrf(client, token):
    with client.session_transaction() as flask_session:
        flask_session['_csrf_token'] = token


def _create_user(db_session, username='auth-onboarding-user', role='user'):
    from core.db_models import User

    user = User(username=username, role=role, community='朝阳社区')
    user.set_password('ExistingPassword1!')
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username, csrf='auth-onboarding-login-csrf'):
    _set_csrf(client, csrf)
    return client.post(
        '/login',
        data={
            'username': username,
            'password': 'ExistingPassword1!',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )


def _register_payload(username, csrf, *, community=''):
    return {
        'username': username,
        'email': f'{username}@example.com',
        'password': 'RegistrationPassword1!',
        'confirm_password': 'RegistrationPassword1!',
        'community': community,
        'csrf_token': csrf,
    }


def test_register_aggregates_errors_and_preserves_only_safe_fields(
    client,
    db_session,
):
    _set_csrf(client, 'register-errors-csrf')
    response = client.post(
        '/register',
        data={
            'username': 'x',
            'email': 'remember-me-invalid-email',
            'password': 'short',
            'confirm_password': 'secret-different',
            'age': '999',
            'gender': '任意值',
            'community': '朝阳社区',
            'csrf_token': 'register-errors-csrf',
        },
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 422
    assert '用户名长度需在3-25字符之间' in body
    assert '密码长度至少12位' in body
    assert '两次输入的密码不一致' in body
    assert '邮箱格式不正确' in body
    assert '年龄需在1-150之间' in body
    assert '性别选择不正确' in body
    assert 'value="x"' in body
    assert 'remember-me-invalid-email' in body
    assert 'value="short"' not in body
    assert 'secret-different' not in body


def test_register_clears_guest_identity_and_starts_non_persistent_session(
    app,
    client,
    db_session,
):
    app.config['WECHAT_FORMAL_RUNTIME'] = False
    app.config['WEB_PRIVATE_FEATURES_ENABLED'] = True
    client.get('/guest', follow_redirects=False)
    _set_csrf(client, 'register-success-csrf')

    response = client.post(
        '/register',
        data={
            'username': 'new_care_account',
            'email': 'new-care@example.com',
            'password': 'NewCarePassword1!',
            'confirm_password': 'NewCarePassword1!',
            'csrf_token': 'register-success-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers['Location'].endswith('/pairs?welcome=1')
    assert 'remember_token=' not in '\n'.join(
        response.headers.getlist('Set-Cookie')
    )
    with client.session_transaction() as flask_session:
        assert flask_session.get('_user_id')
        assert 'guest_id' not in flask_session
        assert 'guest_profile' not in flask_session

    welcome = client.get('/pairs?welcome=1')
    body = welcome.get_data(as_text=True)
    assert welcome.status_code == 200
    assert '账号已准备好，按这 3 步开始照护' in body
    assert '添加需要关注的家人' in body


@pytest.mark.parametrize(
    'invalid_location',
    ('北京', '116.20,29.27', '101240201'),
)
def test_register_rejects_external_coordinate_and_qweather_locations_inline(
    client,
    db_session,
    invalid_location,
):
    from core.db_models import User

    csrf = 'register-invalid-location-csrf'
    _set_csrf(client, csrf)
    response = client.post(
        '/register',
        data=_register_payload(
            f'location_case_{len(invalid_location)}_{ord(invalid_location[0])}',
            csrf,
            community=invalid_location,
        ),
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 422
    assert '目前仅支持都昌县及已配置的乡镇、社区' in body
    assert f'value="{invalid_location}" selected' in body
    assert User.query.count() == 0


@pytest.mark.parametrize(
    ('username', 'submitted_location', 'expected_location'),
    (
        ('register_blank_location', '', None),
        ('register_county_community', '县内测试社区', '县内测试社区'),
    ),
)
def test_register_accepts_blank_or_configured_county_community(
    client,
    db_session,
    username,
    submitted_location,
    expected_location,
):
    from core.db_models import Community, User

    if submitted_location:
        db_session.add(Community(name=submitted_location, location='都昌县'))
        db_session.commit()
    csrf = f'{username}-csrf'
    _set_csrf(client, csrf)
    response = client.post(
        '/register',
        data=_register_payload(
            username,
            csrf,
            community=submitted_location,
        ),
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    user = User.query.filter_by(username=username).one()
    assert user.community == expected_location
    assert user.last_login is not None


def test_register_commits_once_before_replacing_guest_identity(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import User
    from core.extensions import db

    app.config['WECHAT_FORMAL_RUNTIME'] = False
    app.config['WEB_PRIVATE_FEATURES_ENABLED'] = True
    client.get('/guest', follow_redirects=False)
    csrf = 'register-single-commit-csrf'
    _set_csrf(client, csrf)
    real_commit = db.session.commit
    commit_calls = 0

    def commit_once():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls > 1:
            raise AssertionError('注册成功路径不得执行第二次提交')
        return real_commit()

    monkeypatch.setattr(db.session, 'commit', commit_once)
    response = client.post(
        '/register',
        data=_register_payload('single_commit_register', csrf),
        follow_redirects=False,
    )
    monkeypatch.setattr(db.session, 'commit', real_commit)

    assert response.status_code in (302, 303)
    assert commit_calls == 1
    user = User.query.filter_by(username='single_commit_register').one()
    assert user.last_login is not None
    with client.session_transaction() as flask_session:
        assert flask_session.get('_user_id') == user.get_id()
        assert 'guest_id' not in flask_session
        assert 'guest_profile' not in flask_session


def test_register_transaction_failure_keeps_guest_identity_and_rolls_back(
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import User
    from core.extensions import db
    from sqlalchemy.exc import OperationalError

    client.get('/guest', follow_redirects=False)
    with client.session_transaction() as flask_session:
        guest_user_id = flask_session.get('_user_id')
        guest_id = flask_session.get('guest_id')
    csrf = 'register-transaction-failure-csrf'
    _set_csrf(client, csrf)
    real_commit = db.session.commit

    def fail_commit():
        raise OperationalError('INSERT users', {}, RuntimeError('database unavailable'))

    monkeypatch.setattr(db.session, 'commit', fail_commit)
    response = client.post(
        '/register',
        data=_register_payload('failed_reg_txn', csrf),
    )
    monkeypatch.setattr(db.session, 'commit', real_commit)

    assert response.status_code == 503
    assert '注册暂时无法完成，请稍后重试' in response.get_data(as_text=True)
    assert User.query.filter_by(username='failed_reg_txn').first() is None
    with client.session_transaction() as flask_session:
        assert flask_session.get('_user_id') == guest_user_id
        assert flask_session.get('guest_id') == guest_id


def test_register_miniprogram_only_runtime_keeps_account_link_landing(
    app,
    client,
    db_session,
):
    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WEB_PRIVATE_FEATURES_ENABLED'] = False
    _set_csrf(client, 'register-mini-only-csrf')

    response = client.post(
        '/register',
        data={
            'username': 'mini_only_new_user',
            'password': 'MiniOnlyPassword1!',
            'confirm_password': 'MiniOnlyPassword1!',
            'csrf_token': 'register-mini-only-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers['Location'].endswith('/account-link')


def test_login_retry_preserves_identifier_and_remember_without_password(
    client,
    db_session,
):
    _set_csrf(client, 'login-retry-csrf')
    response = client.post(
        '/login',
        data={
            'username': 'retry-name',
            'password': 'NeverRenderThisPassword1!',
            'remember': '1',
            'csrf_token': 'login-retry-csrf',
        },
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'value="retry-name"' in body
    assert 'id="remember" name="remember" value="1" checked' in body
    assert 'NeverRenderThisPassword1!' not in body
    assert '用户名或密码错误' in body


def test_register_and_login_copy_only_promises_username_login(client, db_session):
    register_page = client.get('/register').get_data(as_text=True)
    login_page = client.get('/login').get_data(as_text=True)

    assert 'name="phone"' not in register_page
    assert '>用户名<' in login_page
    assert '用户名或已验证手机号' not in login_page


def test_navigation_distinguishes_anonymous_guest_and_real_user(
    client,
    db_session,
):
    anonymous = client.get('/login').get_data(as_text=True)
    assert '登录正式账号' not in anonymous
    assert '结束游客体验' not in anonymous
    assert '>退出<' not in anonymous

    client.get('/guest', follow_redirects=False)
    guest = client.get('/login').get_data(as_text=True)
    assert '登录正式账号' in guest
    assert '注册账号' in guest
    assert '结束游客体验' in guest

    logout_csrf = 'guest-finish-csrf'
    _set_csrf(client, logout_csrf)
    client.post(
        '/logout',
        data={'csrf_token': logout_csrf},
        follow_redirects=False,
    )
    _create_user(db_session, username='real-nav-user')
    _login(client, 'real-nav-user')
    real = client.get('/login').get_data(as_text=True)
    assert '>退出<' in real
    assert '结束游客体验' not in real


def test_account_link_says_phone_is_optional(client, db_session):
    _create_user(db_session, username='link-copy-user')
    _login(client, 'link-copy-user')

    body = client.get('/account-link').get_data(as_text=True)
    assert '不需要填写手机号' in body
    assert '不影响小程序绑定' in body


def test_ordinary_profile_hides_legacy_token_and_password_needs_confirmation(
    client,
    db_session,
):
    user = _create_user(db_session, username='profile-confirm-user')
    _login(client, 'profile-confirm-user')
    profile_page = client.get('/profile').get_data(as_text=True)
    assert '旧版 API Token 兼容入口' not in profile_page

    _set_csrf(client, 'password-confirm-csrf')
    response = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'ExistingPassword1!',
            'new_password': 'ChangedPassword1!',
            'confirm_password': 'DifferentPassword1!',
            'csrf_token': 'password-confirm-csrf',
        },
        follow_redirects=True,
    )
    assert '两次输入的新密码不一致' in response.get_data(as_text=True)
    db_session.refresh(user)
    assert user.check_password('ExistingPassword1!')
    assert not user.check_password('ChangedPassword1!')


def test_profile_rejects_external_location_and_prompts_for_legacy_value(
    client,
    db_session,
):
    user = _create_user(db_session, username='profile-location-user')
    user.community = '北京'
    db_session.commit()
    _login(client, 'profile-location-user')

    page = client.get('/profile')
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert '历史地点“北京”已无法继续使用' in body
    assert '北京（不再支持，请重选）' in body

    _set_csrf(client, 'profile-invalid-location-csrf')
    response = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'age': '66',
            'gender': '男性',
            'community': '101240201',
            'email': '',
            'csrf_token': 'profile-invalid-location-csrf',
        },
        follow_redirects=True,
    )
    assert '目前仅支持都昌县及已配置的乡镇、社区' in response.get_data(as_text=True)
    db_session.refresh(user)
    assert user.community == '北京'


def test_location_write_uses_same_county_resolver_for_user_and_guest(
    client,
    db_session,
):
    from core.db_models import Community

    db_session.add(Community(name='县内定位社区', location='都昌县'))
    db_session.commit()
    user = _create_user(db_session, username='location-write-user')
    _login(client, 'location-write-user')

    _set_csrf(client, 'location-user-invalid-csrf')
    invalid = client.post(
        '/location',
        data={'location': '116.20,29.27', 'csrf_token': 'location-user-invalid-csrf'},
        follow_redirects=True,
    )
    assert '目前仅支持都昌县及已配置的乡镇、社区' in invalid.get_data(as_text=True)
    db_session.refresh(user)
    assert user.community == '朝阳社区'

    _set_csrf(client, 'location-user-valid-csrf')
    valid = client.post(
        '/location',
        data={'location': '县内定位社区', 'csrf_token': 'location-user-valid-csrf'},
        follow_redirects=False,
    )
    assert valid.status_code in (302, 303)
    db_session.refresh(user)
    assert user.community == '县内定位社区'

    _set_csrf(client, 'location-user-logout-csrf')
    client.post(
        '/logout',
        data={'csrf_token': 'location-user-logout-csrf'},
        follow_redirects=False,
    )
    client.get('/guest', follow_redirects=False)
    _set_csrf(client, 'location-guest-valid-csrf')
    guest_valid = client.post(
        '/location',
        data={'location': '县内定位社区', 'csrf_token': 'location-guest-valid-csrf'},
        follow_redirects=False,
    )
    assert guest_valid.status_code in (302, 303)
    with client.session_transaction() as flask_session:
        assert flask_session['guest_profile']['community'] == '县内定位社区'

    _set_csrf(client, 'location-guest-invalid-csrf')
    guest_invalid = client.post(
        '/location',
        data={'location': '北京', 'csrf_token': 'location-guest-invalid-csrf'},
        follow_redirects=False,
    )
    assert guest_invalid.status_code in (302, 303)
    with client.session_transaction() as flask_session:
        assert flask_session['guest_profile']['community'] == '县内定位社区'
