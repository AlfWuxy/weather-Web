# -*- coding: utf-8 -*-
"""注册与限流响应的回归契约。"""

from urllib.parse import parse_qs, urlparse

from core.db_models import User


def _csrf(client, token):
    with client.session_transaction() as flask_session:
        flask_session['_csrf_token'] = token
    return token


def _registration_data(username, *, password='LongPassword1!', confirm=None):
    return {
        'username': username,
        'email': f'{username}@example.com',
        'phone': '13800138000',
        'password': password,
        'confirm_password': password if confirm is None else confirm,
        'age': '67',
        'gender': '女性',
        'community': '测试社区',
    }


def _post_register(client, csrf, data, *, remote_addr):
    payload = dict(data)
    payload['csrf_token'] = csrf
    return client.post(
        '/register',
        data=payload,
        environ_overrides={'REMOTE_ADDR': remote_addr},
        follow_redirects=False,
    )


def test_register_requires_matching_confirmation_and_preserves_safe_fields(
    app,
    client,
    db_session,
):
    csrf = _csrf(client, 'register-confirm-csrf')
    submitted = _registration_data(
        'confirm_mismatch',
        confirm='DifferentPass1!',
    )

    response = _post_register(
        client,
        csrf,
        submitted,
        remote_addr='198.51.100.21',
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers['Location'].endswith('/register')
    assert User.query.filter_by(username='confirm_mismatch').first() is None
    with client.session_transaction() as flask_session:
        preserved = flask_session['registration_form_data']
        flashes = list(flask_session.get('_flashes') or [])
    assert preserved == {
        'username': 'confirm_mismatch',
        'email': 'confirm_mismatch@example.com',
        'phone': '13800138000',
        'age': '67',
        'gender': '女性',
        'community': '测试社区',
    }
    assert 'password' not in preserved
    assert any('两次输入的密码不一致' in message for _, message in flashes)

    rendered = client.get('/register')
    body = rendered.get_data(as_text=True)
    assert 'value="confirm_mismatch"' in body
    assert 'value="confirm_mismatch@example.com"' in body
    assert 'value="13800138000"' in body
    assert 'value="LongPassword1!"' not in body
    assert 'value="DifferentPass1!"' not in body


def test_register_missing_confirmation_is_rejected(app, client, db_session):
    csrf = _csrf(client, 'register-missing-confirm-csrf')
    submitted = _registration_data('confirm_missing')
    submitted.pop('confirm_password')

    response = _post_register(
        client,
        csrf,
        submitted,
        remote_addr='198.51.100.22',
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers['Location'].endswith('/register')
    assert User.query.filter_by(username='confirm_missing').first() is None


def test_web_registration_creates_caregiver_and_guides_login_to_pairs(
    app,
    client,
    db_session,
):
    app.config['WECHAT_FORMAL_RUNTIME'] = False
    app.config['WEB_PRIVATE_FEATURES_ENABLED'] = True
    csrf = _csrf(client, 'register-caregiver-csrf')

    response = _post_register(
        client,
        csrf,
        _registration_data('new_caregiver_role'),
        remote_addr='198.51.100.33',
    )

    assert response.status_code in (301, 302, 303)
    location = urlparse(response.headers['Location'])
    assert location.path.endswith('/login')
    assert parse_qs(location.query).get('next') == ['/pairs']
    created = User.query.filter_by(username='new_caregiver_role').one()
    assert created.role == 'caregiver'


def test_empty_community_table_uses_free_text_location_with_suggestions(
    app,
    client,
    db_session,
):
    from core.db_models import Community

    Community.query.delete()
    db_session.commit()

    body = client.get('/register').get_data(as_text=True)

    assert 'name="community" list="locationSuggestions"' in body
    assert '例如：都昌县 / 牛家垄周村' in body
    assert '这里的年龄和性别属于照护者账号' in body
    assert '老人的年龄、慢病和称呼' in body
    for location in app.config['COMMUNITY_COORDS_GCJ']:
        assert f'value="{location}"' in body


def test_empty_registration_reports_all_required_fields_in_chinese(
    app,
    client,
    db_session,
):
    csrf = _csrf(client, 'register-empty-csrf')

    response = _post_register(
        client,
        csrf,
        {},
        remote_addr='198.51.100.27',
    )

    assert response.status_code in (301, 302, 303)
    with client.session_transaction() as flask_session:
        flashes = list(flask_session.get('_flashes') or [])
    message = flashes[-1][1]
    assert '用户名不能为空' in message
    assert '密码不能为空' in message
    assert '请再次输入密码' in message
    assert client.get('/register').status_code == 200


def test_register_processed_quota_counts_duplicate_before_new_account(
    app,
    client,
    db_session,
):
    app.config['RATE_LIMIT_REGISTER'] = '1 per hour'
    app.config['RATE_LIMIT_REGISTER_ATTEMPTS'] = '10 per hour'
    csrf = _csrf(client, 'register-marker-csrf')
    remote_addr = '198.51.100.23'

    occupied = User(username='already_taken')
    occupied.set_password('ExistingPassword1!')
    db_session.add(occupied)
    db_session.commit()

    # 语法合法的占用结果也必须扣配额，避免配额状态泄露账号是否存在。
    duplicate = _post_register(
        client,
        csrf,
        _registration_data('already_taken'),
        remote_addr=remote_addr,
    )
    limited = _post_register(
        client,
        csrf,
        _registration_data('new_account_one'),
        remote_addr=remote_addr,
    )

    assert duplicate.status_code in (301, 302, 303)
    assert limited.status_code == 429
    assert User.query.filter_by(username='new_account_one').first() is None


def test_registration_processed_reservation_is_atomic_for_same_ip(app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from core.security import reserve_registration_processed_quota
    from werkzeug.exceptions import TooManyRequests

    app.config['RATE_LIMIT_REGISTER'] = '3 per hour'
    barrier = Barrier(12)

    def reserve_once():
        with app.test_request_context(
            '/register',
            environ_base={'REMOTE_ADDR': '198.51.100.28'},
        ):
            barrier.wait()
            try:
                reserve_registration_processed_quota()
            except TooManyRequests:
                return False
            return True

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(lambda _index: reserve_once(), range(12)))

    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 9


def test_exhausted_processed_quota_blocks_before_identity_lookup(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.security import reserve_registration_processed_quota
    from services import public_service

    app.config['RATE_LIMIT_REGISTER'] = '1 per minute'
    app.config['RATE_LIMIT_REGISTER_ATTEMPTS'] = '10 per hour'
    remote_addr = '198.51.100.29'
    with app.test_request_context(
        '/register',
        environ_base={'REMOTE_ADDR': remote_addr},
    ):
        reserve_registration_processed_quota()

    class ForbiddenIdentityLookup:
        def __init__(self, *args, **kwargs):
            raise AssertionError('配额耗尽后不得构造用户或查询身份占用')

    monkeypatch.setattr(public_service, 'User', ForbiddenIdentityLookup)
    csrf = _csrf(client, 'register-oracle-csrf')
    response = _post_register(
        client,
        csrf,
        _registration_data('oracle_blocked'),
        remote_addr=remote_addr,
    )

    assert response.status_code == 429
    # 成功配额是一分钟，不能误用尝试上限的一小时窗口。
    assert 1 <= int(response.headers['Retry-After']) <= 61


def test_processed_registration_paths_clear_prefill_and_match_response(
    app,
    db_session,
    monkeypatch,
):
    from services import public_service
    from sqlalchemy.exc import IntegrityError

    app.config['RATE_LIMIT_REGISTER'] = '10 per hour'
    app.config['RATE_LIMIT_REGISTER_ATTEMPTS'] = '20 per hour'
    occupied = User(username='processed_duplicate')
    occupied.set_password('ExistingPassword1!')
    db_session.add(occupied)
    db_session.commit()

    def submit(username, remote_addr, *, force_integrity_error=False):
        test_client = app.test_client()
        csrf = f'processed-{username}-csrf'
        with test_client.session_transaction() as flask_session:
            flask_session['_csrf_token'] = csrf
            flask_session['registration_form_data'] = {'username': 'stale'}

        if force_integrity_error:
            def fail_commit():
                raise IntegrityError('forced insert', {}, Exception('forced'))

            with monkeypatch.context() as patcher:
                patcher.setattr(public_service.db.session, 'commit', fail_commit)
                response = _post_register(
                    test_client,
                    csrf,
                    _registration_data(username),
                    remote_addr=remote_addr,
                )
        else:
            response = _post_register(
                test_client,
                csrf,
                _registration_data(username),
                remote_addr=remote_addr,
            )

        with test_client.session_transaction() as flask_session:
            assert 'registration_form_data' not in flask_session
            flashes = list(flask_session.get('_flashes') or [])
        return response.status_code, response.headers['Location'], flashes

    created_result = submit('processed_created', '198.51.100.30')
    duplicate_result = submit('processed_duplicate', '198.51.100.31')
    integrity_result = submit(
        'processed_integrity',
        '198.51.100.32',
        force_integrity_error=True,
    )

    assert created_result == duplicate_result == integrity_result


def test_register_attempt_quota_stops_unlimited_invalid_retries(
    app,
    client,
    db_session,
):
    app.config['RATE_LIMIT_REGISTER'] = '10 per hour'
    app.config['RATE_LIMIT_REGISTER_ATTEMPTS'] = '2 per hour'
    csrf = _csrf(client, 'register-attempt-csrf')
    remote_addr = '198.51.100.24'

    responses = [
        _post_register(
            client,
            csrf,
            _registration_data(
                f'invalid_attempt_{index}',
                confirm='DifferentPass1!',
            ),
            remote_addr=remote_addr,
        )
        for index in range(3)
    ]

    assert [response.status_code for response in responses[:2]] == [302, 302]
    assert responses[2].status_code == 429


def test_registration_limit_uses_trusted_forwarded_client_ip(app):
    from core.security import registration_rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = ('127.0.0.1/32',)
    with app.test_request_context(
        '/register',
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
        headers={'X-Forwarded-For': '203.0.113.41'},
    ):
        assert registration_rate_limit_key() == 'ip:203.0.113.41'


def test_api_rate_limit_keeps_json_contract_and_real_retry_after(app, db_session):
    app.config['RATE_LIMIT_MP_READ'] = '1 per hour'
    api_client = app.test_client()

    first = api_client.get(
        '/mp/api/v1/me',
        environ_overrides={'REMOTE_ADDR': '198.51.100.25'},
    )
    limited = api_client.get(
        '/mp/api/v1/me',
        environ_overrides={'REMOTE_ADDR': '198.51.100.25'},
    )

    assert first.status_code == 401
    assert limited.status_code == 429
    assert limited.content_type.startswith('application/json')
    payload = limited.get_json()
    assert payload['success'] is False
    assert payload['error'] == 'rate_limit_exceeded'
    retry_after = int(limited.headers['Retry-After'])
    assert 1 <= retry_after <= 3601
    assert abs(payload['data']['retry_after_seconds'] - retry_after) <= 1
    assert limited.headers['Cache-Control'] == 'no-store'


def test_browser_rate_limit_renders_chinese_countdown(app, client, db_session):
    app.config['RATE_LIMIT_LOGIN'] = '1 per 5 minutes'
    csrf = _csrf(client, 'login-rate-csrf')
    request_data = {
        'username': 'missing-user',
        'password': 'WrongPassword1!',
        'csrf_token': csrf,
    }

    first = client.post(
        '/login',
        data=request_data,
        environ_overrides={'REMOTE_ADDR': '198.51.100.26'},
    )
    limited = client.post(
        '/login',
        data=request_data,
        environ_overrides={'REMOTE_ADDR': '198.51.100.26'},
    )

    assert first.status_code == 200
    assert limited.status_code == 429
    body = limited.get_data(as_text=True)
    assert '操作太频繁' in body
    assert 'data-rate-limit-countdown' in body
    assert 1 <= int(limited.headers['Retry-After']) <= 301
    assert limited.headers['Cache-Control'] == 'no-store'


def test_profile_password_requires_matching_confirmation(app, client, db_session):
    user = User(username='profile_confirm_user')
    user.set_password('OldPassword1!')
    db_session.add(user)
    db_session.commit()
    csrf = _csrf(client, 'profile-confirm-csrf')
    client.post(
        '/login',
        data={
            'username': user.username,
            'password': 'OldPassword1!',
            'csrf_token': csrf,
        },
    )

    rejected = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPassword1!',
            'new_password': 'NewPassword2!',
            'confirm_password': 'DifferentPass3!',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert rejected.status_code in (301, 302, 303)
    db_session.refresh(user)
    assert user.check_password('OldPassword1!')
    assert not user.check_password('NewPassword2!')

    accepted = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPassword1!',
            'new_password': 'NewPassword2!',
            'confirm_password': 'NewPassword2!',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert accepted.status_code in (301, 302, 303)
    db_session.refresh(user)
    assert user.check_password('NewPassword2!')


def test_password_forms_publish_consistent_twelve_character_constraints(
    app,
    client,
    db_session,
):
    register_body = client.get('/register').get_data(as_text=True)
    assert 'name="password" minlength="12" maxlength="100"' in register_body
    assert (
        'name="confirm_password" minlength="12" maxlength="100"'
        in register_body
    )

    admin = User(username='password_constraint_admin', role='admin')
    admin.set_password('AdminPassword1!')
    db_session.add(admin)
    db_session.commit()
    csrf = _csrf(client, 'password-constraint-csrf')
    client.post(
        '/login',
        data={
            'username': admin.username,
            'password': 'AdminPassword1!',
            'csrf_token': csrf,
        },
    )

    profile_body = client.get('/profile').get_data(as_text=True)
    assert 'name="new_password" minlength="12" maxlength="100"' in profile_body
    assert (
        'name="confirm_password" minlength="12" maxlength="100"'
        in profile_body
    )
    add_body = client.get('/admin/user/add').get_data(as_text=True)
    edit_body = client.get(
        f'/admin/user/{admin.id}/edit',
    ).get_data(as_text=True)
    assert 'name="password" minlength="12" maxlength="100"' in add_body
    assert 'name="password" minlength="12" maxlength="100"' in edit_body
