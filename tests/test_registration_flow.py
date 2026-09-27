# -*- coding: utf-8 -*-
"""注册、会话切换与改密闭环回归测试。"""

import pytest


PAIR_SESSION_KEYS = ('pair_token', 'pair_session_id', 'pair_session_code')


def _set_csrf(client, token):
    with client.session_transaction() as session:
        session['_csrf_token'] = token


def _login_as(client, user, csrf_token='csrf-profile-flow'):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username, role='caregiver', password='OldPass123'):
    from core.db_models import User

    user = User(username=username, role=role)
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def test_registration_password_mismatch_rerenders_and_keeps_only_safe_fields(client, db_session):
    from core.db_models import User

    csrf = 'csrf-register-mismatch'
    _set_csrf(client, csrf)
    response = client.post(
        '/register',
        data={
            'username': '照护账号01',
            'email': 'care@example.com',
            'password': 'StrongPassA1',
            'confirm_password': 'StrongPassB2',
            'age': '42',
            'gender': '女性',
            'community': '江西省九江市都昌县自定义村',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '两次输入的密码不一致' in body
    assert 'value="照护账号01"' in body
    assert 'value="care@example.com"' in body
    assert 'value="42"' in body
    assert 'value="江西省九江市都昌县自定义村"' in body
    assert 'StrongPassA1' not in body
    assert 'StrongPassB2' not in body
    assert User.query.count() == 0


def test_registration_uses_configured_location_datalist_without_seeding_communities(app, client, db_session):
    from core.db_models import Community

    response = client.get('/register')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    suggestions = list(app.config['COMMUNITY_COORDS_GCJ'].keys())
    assert len(suggestions) == 16
    assert 'list="locationSuggestions"' in body
    assert '慢病等健康情况均为选填' in body
    for location in suggestions:
        assert f'value="{location}"' in body
    assert Community.query.count() == 0


def test_registration_rejects_case_insensitive_duplicate_email(client, db_session):
    from core.db_models import User

    existing = _create_user(db_session, 'existing_email_owner')
    existing.email = 'Care.Owner@Example.com'
    db_session.commit()
    csrf = 'csrf-register-email-case'
    _set_csrf(client, csrf)

    response = client.post(
        '/register',
        data={
            'username': 'new_email_case',
            'email': 'care.owner@example.com',
            'password': 'StrongPass123',
            'confirm_password': 'StrongPass123',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert '邮箱已被注册' in response.get_data(as_text=True)
    assert User.query.filter_by(username='new_email_case').first() is None


def test_registration_creates_logged_in_caregiver_and_clears_guest_state(client, db_session):
    from core.constants import DEFAULT_CITY_LABEL
    from core.db_models import Community, User

    assert client.get('/guest').status_code == 302
    csrf = 'csrf-register-success'
    with client.session_transaction() as session:
        assert session['guest_profile']['community'] == DEFAULT_CITY_LABEL == '都昌县'
        session['_csrf_token'] = csrf
        session['guest_assessment'] = {'risk_level': '低风险'}
        session['pair_token'] = 'stale-register-token'
        session['pair_session_id'] = 41
        session['pair_session_code'] = '41000001'

    response = client.post(
        '/register',
        data={
            'username': 'new_caregiver',
            'email': 'new_caregiver@example.com',
            'password': 'StrongPass123',
            'confirm_password': 'StrongPass123',
            'age': '36',
            'gender': '男性',
            'community': '九江市都昌县自定义村',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/pairs')
    user = User.query.filter_by(username='new_caregiver').one()
    assert user.role == 'caregiver'
    assert user.last_login is not None
    assert user.check_password('StrongPass123') is True
    assert user.community == '九江市都昌县自定义村'
    assert Community.query.count() == 0
    with client.session_transaction() as session:
        assert session.get('_user_id') == user.get_id()
        assert 'guest_id' not in session
        assert 'guest_profile' not in session
        assert 'guest_assessment' not in session
        for key in PAIR_SESSION_KEYS:
            assert key not in session


@pytest.mark.parametrize(
    ('role', 'expected_path'),
    [
        ('user', '/pairs'),
        ('caregiver', '/pairs'),
        ('community', '/community'),
        ('admin', '/admin'),
    ],
)
def test_logged_in_user_cannot_open_registration_to_switch_accounts(
    client,
    db_session,
    role,
    expected_path,
):
    user = _create_user(db_session, f'already_logged_in_{role}', role=role)
    _login_as(client, user)

    response = client.get('/register', follow_redirects=False)

    assert response.status_code == 302
    assert response.headers['Location'].endswith(expected_path)


def test_registration_handles_unique_constraint_race_with_200_rerender(client, db_session, monkeypatch):
    from flask_sqlalchemy.query import Query
    from core.db_models import User

    _create_user(db_session, 'race_username')
    # 模拟预检查后另一请求已提交同名账号，让数据库唯一约束成为最终防线。
    monkeypatch.setattr(Query, 'first', lambda _query: None)
    csrf = 'csrf-register-race'
    _set_csrf(client, csrf)

    response = client.post(
        '/register',
        data={
            'username': 'race_username',
            'email': 'race_new@example.com',
            'password': 'StrongPass123',
            'confirm_password': 'StrongPass123',
            'community': '岭背徐村',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert '用户名或邮箱已被注册' in response.get_data(as_text=True)
    assert User.query.filter_by(username='race_username').count() == 1


def test_password_change_requires_matching_confirmation(client, db_session):
    user = _create_user(db_session, 'password_mismatch')
    _login_as(client, user)

    response = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPass123',
            'new_password': 'NewStrongPass1',
            'confirm_password': 'NewStrongPass2',
            'csrf_token': 'csrf-profile-flow',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    db_session.refresh(user)
    assert user.check_password('OldPass123') is True
    assert user.check_password('NewStrongPass1') is False


def test_password_change_validates_new_password_server_side(client, db_session):
    user = _create_user(db_session, 'password_too_short')
    _login_as(client, user)

    response = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPass123',
            'new_password': 'short',
            'confirm_password': 'short',
            'csrf_token': 'csrf-profile-flow',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    db_session.refresh(user)
    assert user.check_password('OldPass123') is True


def test_successful_password_change_logs_out_and_prompts_relogin(client, db_session):
    user = _create_user(db_session, 'password_success')
    _login_as(client, user)
    with client.session_transaction() as session:
        session['pair_token'] = 'stale-password-token'
        session['pair_session_id'] = 81
        session['pair_session_code'] = '81000001'

    response = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPass123',
            'new_password': 'NewStrongPass1',
            'confirm_password': 'NewStrongPass1',
            'csrf_token': 'csrf-profile-flow',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')
    db_session.refresh(user)
    assert user.check_password('NewStrongPass1') is True
    with client.session_transaction() as session:
        assert '_user_id' not in session
        for key in PAIR_SESSION_KEYS:
            assert key not in session
    login_page = client.get('/login')
    assert '密码已更新，请使用新密码重新登录' in login_page.get_data(as_text=True)


def test_profile_rejects_tampered_invalid_username(client, db_session):
    user = _create_user(db_session, 'stable_username')
    user.email = 'before@example.com'
    db_session.commit()
    _login_as(client, user)

    response = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'username': 'x',
            'email': 'after@example.com',
            'csrf_token': 'csrf-profile-flow',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    db_session.refresh(user)
    assert user.username == 'stable_username'
    assert user.email == 'before@example.com'


def test_login_and_profile_copy_keep_push_channel_optional(client, db_session):
    user = _create_user(db_session, 'optional_push_copy')

    login_body = client.get('/login').get_data(as_text=True)
    assert '选填微信接收码' in login_body
    assert '未配置消息通道时' in login_body

    csrf = 'csrf-optional-push-copy'
    _set_csrf(client, csrf)
    login_response = client.post(
        '/login',
        data={
            'username': user.username,
            'password': 'OldPass123',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 302
    profile_response = client.get('/profile')
    assert profile_response.status_code == 200
    profile_body = profile_response.get_data(as_text=True)
    assert '微信提醒接收码（选填）' in profile_body
    assert '通道未配置时不会自动推送' in profile_body
    assert 'name="confirm_password"' in profile_body
