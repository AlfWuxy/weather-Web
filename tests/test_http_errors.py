# -*- coding: utf-8 -*-
"""中文错误页、限流响应和可信代理客户端键回归。"""
import importlib

from flask import abort
from flask_login import current_user
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError


def _set_csrf(client, token):
    with client.session_transaction() as flask_session:
        flask_session['_csrf_token'] = token


def _register_payload(index, csrf):
    return {
        'username': f'error_rate_{index}',
        'password': 'RegistrationPassword1!',
        'confirm_password': 'RegistrationPassword1!',
        'csrf_token': csrf,
    }


def test_html_rate_limit_is_branded_and_has_retry_contract(
    app,
    client,
    db_session,
):
    app.config['RATE_LIMIT_REGISTER'] = '2 per hour'
    csrf = 'html-rate-limit-csrf'
    _set_csrf(client, csrf)
    responses = [
        client.post(
            '/register',
            data=_register_payload(index, csrf),
            environ_base={'REMOTE_ADDR': '198.51.100.70'},
            follow_redirects=False,
        )
        for index in range(3)
    ]

    response = responses[-1]
    body = response.get_data(as_text=True)
    assert [item.status_code for item in responses[:2]] == [302, 302]
    assert response.status_code == 429
    assert '操作太频繁' in body
    assert '5 per 5 minute' not in body
    assert 'data-retry-seconds=' in body
    assert int(response.headers['Retry-After']) >= 1
    assert response.headers['Cache-Control'] == 'no-store, private, max-age=0'
    assert response.headers['X-Request-ID']
    assert response.headers['X-Request-ID'] in body


def test_json_rate_limit_keeps_structured_api_contract(
    app,
    client,
    db_session,
):
    app.config['RATE_LIMIT_REGISTER'] = '1 per hour'
    csrf = 'json-rate-limit-csrf'
    _set_csrf(client, csrf)
    first = client.post(
        '/register',
        data=_register_payload(20, csrf),
        headers={'Accept': 'application/json'},
        environ_base={'REMOTE_ADDR': '198.51.100.71'},
        follow_redirects=False,
    )
    limited = client.post(
        '/register',
        data=_register_payload(21, csrf),
        headers={'Accept': 'application/json'},
        environ_base={'REMOTE_ADDR': '198.51.100.71'},
        follow_redirects=False,
    )

    payload = limited.get_json()
    assert first.status_code == 302
    assert limited.status_code == 429
    assert payload['success'] is False
    assert payload['error'] == 'rate_limit_exceeded'
    assert payload['retry_after_seconds'] == int(limited.headers['Retry-After'])
    assert payload['request_id'] == limited.headers['X-Request-ID']


def test_successful_logins_do_not_consume_failed_login_limit(
    app,
    client,
    db_session,
):
    from core.db_models import User

    app.config['RATE_LIMIT_LOGIN'] = '2 per 5 minutes'
    user = User(username='deduct-login-user', role='user')
    user.set_password('CorrectLoginPassword1!')
    db_session.add(user)
    db_session.commit()
    csrf = 'deduct-login-csrf'
    _set_csrf(client, csrf)
    remote = {'REMOTE_ADDR': '198.51.100.72'}

    for _index in range(3):
        success = client.post(
            '/login',
            data={
                'username': user.username,
                'password': 'CorrectLoginPassword1!',
                'csrf_token': csrf,
            },
            environ_base=remote,
            follow_redirects=False,
        )
        assert success.status_code == 302
        client.post(
            '/logout',
            data={'csrf_token': csrf},
            environ_base=remote,
            follow_redirects=False,
        )

    failures = [
        client.post(
            '/login',
            data={
                'username': user.username,
                'password': 'WrongLoginPassword1!',
                'csrf_token': csrf,
            },
            environ_base=remote,
            follow_redirects=False,
        )
        for _index in range(3)
    ]
    assert [item.status_code for item in failures] == [200, 200, 429]


def test_client_rate_limit_key_obeys_trusted_proxy_boundary(app):
    from core.security import client_rate_limit_key

    app.config['TRUSTED_PROXY_CIDRS'] = '127.0.0.1/32,::1/128'
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '8.8.8.8'},
        environ_base={'REMOTE_ADDR': '203.0.113.10'},
    ):
        untrusted_a = client_rate_limit_key()
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '9.9.9.9'},
        environ_base={'REMOTE_ADDR': '203.0.113.10'},
    ):
        untrusted_b = client_rate_limit_key()
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '8.8.8.8'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
    ):
        trusted_a = client_rate_limit_key()
    with app.test_request_context(
        '/',
        headers={'X-Forwarded-For': '9.9.9.9'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
    ):
        trusted_b = client_rate_limit_key()

    assert untrusted_a == untrusted_b
    assert trusted_a != trusted_b
    assert all(
        key.startswith('client:')
        and '8.8.8.8' not in key
        and '203.0.113.10' not in key
        for key in (untrusted_a, trusted_a, trusted_b)
    )


def test_client_rate_limit_key_is_stable_across_worker_reload(app):
    import core.security as security

    app.config['PAIR_TOKEN_PEPPER'] = 'pair-pepper-stable-across-workers'
    app.config['SECRET_KEY'] = 'different-session-secret'
    request_kwargs = {
        'headers': {'X-Forwarded-For': '8.8.4.4'},
        'environ_base': {'REMOTE_ADDR': '127.0.0.1'},
    }

    with app.test_request_context('/', **request_kwargs):
        first_worker = security.client_rate_limit_key()

    importlib.reload(security)
    app.config['SECRET_KEY'] = 'rotated-but-lower-priority-session-secret'
    with app.test_request_context('/', **request_kwargs):
        restarted_worker = security.client_rate_limit_key()

    assert restarted_worker == first_worker
    assert first_worker.startswith('client:')
    assert '8.8.4.4' not in first_worker

    app.config['PAIR_TOKEN_PEPPER'] = 'rotated-pair-pepper'
    with app.test_request_context('/', **request_kwargs):
        rotated_pepper = security.client_rate_limit_key()
    assert rotated_pepper != first_worker


def test_html_and_api_not_found_are_content_negotiated(client, db_session):
    html_response = client.get('/settings')
    api_response = client.get('/api/does-not-exist')

    html_body = html_response.get_data(as_text=True)
    assert html_response.status_code == 404
    assert '页面没有找到' in html_body
    assert 'Not Found' not in html_body
    assert api_response.status_code == 404
    assert api_response.get_json()['error'] == 'not_found'
    assert api_response.get_json()['request_id']


def test_forbidden_and_internal_errors_use_station_pages(app, client, db_session):
    app.config['PROPAGATE_EXCEPTIONS'] = False

    @app.get('/_test/forbidden')
    def _test_forbidden():
        abort(403)

    @app.get('/_test/server-error')
    def _test_server_error():
        raise RuntimeError('raw-private-error-text')

    forbidden = client.get('/_test/forbidden')
    failed = client.get('/_test/server-error')
    assert forbidden.status_code == 403
    assert '没有访问权限' in forbidden.get_data(as_text=True)
    assert failed.status_code == 500
    assert '页面暂时不可用' in failed.get_data(as_text=True)
    assert 'raw-private-error-text' not in failed.get_data(as_text=True)


def test_authenticated_database_failure_500_never_queries_database_again(
    app,
    authenticated_client,
):
    from core.extensions import db

    app.config['PROPAGATE_EXCEPTIONS'] = False
    app.config['FEATURE_STRUCTURED_LOGS'] = True
    state = {'database_offline': False, 'query_count': 0}

    with app.app_context():
        engine = db.engine

    def reject_queries_after_failure(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ):
        if not state['database_offline']:
            return
        state['query_count'] += 1
        raise OperationalError(
            statement,
            parameters,
            RuntimeError('database unavailable'),
        )

    def _test_authenticated_db_failure():
        assert current_user.is_authenticated
        loaded_user = current_user._get_current_object()
        db.session.expire(loaded_user)
        state['database_offline'] = True
        db.session.execute(text('SELECT 1'))
        raise AssertionError('数据库故障必须在这里终止请求')

    endpoint = 'user.user_dashboard'
    original_view = app.view_functions[endpoint]
    app.view_functions[endpoint] = _test_authenticated_db_failure
    event.listen(engine, 'before_cursor_execute', reject_queries_after_failure)
    try:
        response = authenticated_client.get('/dashboard')
    finally:
        app.view_functions[endpoint] = original_view
        event.remove(
            engine,
            'before_cursor_execute',
            reject_queries_after_failure,
        )

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert state['query_count'] == 1
    assert '页面暂时不可用' in body
    assert response.headers['X-Request-ID']
    assert response.headers['X-Request-ID'] in body
    assert response.headers['Cache-Control'] == 'no-store, private, max-age=0'
    assert 'database unavailable' not in body


def test_community_permission_message_names_required_role(
    client,
    db_session,
):
    from core.db_models import User

    user = User(username='ordinary-community-visitor', role='user')
    user.set_password('CommunityVisitor1!')
    db_session.add(user)
    db_session.commit()
    csrf = 'community-role-csrf'
    _set_csrf(client, csrf)
    client.post(
        '/login',
        data={
            'username': user.username,
            'password': 'CommunityVisitor1!',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    response = client.get('/community', follow_redirects=False)
    assert response.status_code == 302
    with client.session_transaction() as flask_session:
        messages = [message for _kind, message in flask_session.get('_flashes', [])]
    assert '该页面仅限社区工作人员或管理员使用，请切换对应账号。' in messages


def test_base_template_has_no_full_screen_page_loader(client, db_session):
    body = client.get('/login').get_data(as_text=True)
    assert 'id="pageLoader"' not in body
    assert 'function hideLoader' not in body
    assert '处理中…' in body
