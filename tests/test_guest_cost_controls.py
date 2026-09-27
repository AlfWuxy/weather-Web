# -*- coding: utf-8 -*-
"""游客费用/滥用门禁回归：rate_limit_key + 高成本 API reject_guest。

覆盖：
1. GuestUser 限流键按 IP（同 remote_addr 轮换 guest id 键不变）
2. 正式 User 限流键以 user: 开头
3. guest POST /api/v1/ml/predict → 403 guest_not_allowed
4. guest POST /api/v1/ai/ask → 403 guest_not_allowed
5. 未登录 POST 上述接口 → 非 guest 403（401/302 登录）
"""


def _login_guest_via_session(client, app, guest_id='guest:i10_cost', csrf_token='i10-guest-csrf'):
    """建立游客 Flask-Login 会话，并写入 CSRF（session + 返回 token）。"""
    from flask import session as flask_session
    from flask_login import login_user

    from core.guest import GuestUser

    profile = {
        'username': '游客',
        'age': None,
        'gender': '未知',
        'community': '朝阳社区',
        'has_chronic_disease': False,
        'chronic_diseases': None,
    }
    guest = GuestUser(guest_id, profile)

    # user_loader 会按 guest_id 重建 GuestUser，需先落 session 数据
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


def test_guest_rate_limit_key_stable_across_guest_ids(app):
    """同 remote_addr、两次不同 guest id：rate_limit_key 相同且 startswith 'ip:'。"""
    from flask_login import current_user, login_user

    from core.guest import GuestUser
    from core.security import rate_limit_key

    same_ip = {'REMOTE_ADDR': '203.0.113.50'}
    profile = {'username': '游客'}

    with app.app_context():
        with app.test_request_context('/', environ_base=same_ip):
            guest_a = GuestUser('guest:i10-token-a', profile)
            assert guest_a.is_guest is True
            login_user(guest_a)
            assert current_user.is_authenticated is True
            key_a = rate_limit_key()

        with app.test_request_context('/', environ_base=same_ip):
            guest_b = GuestUser('guest:i10-token-b', profile)
            assert guest_b.is_guest is True
            login_user(guest_b)
            assert current_user.id == 'guest:i10-token-b'
            key_b = rate_limit_key()

    assert key_a.startswith('ip:')
    assert key_b.startswith('ip:')
    assert key_a == key_b
    assert key_a == 'ip:203.0.113.50'
    # 轮换 guest id 不得进入限流键
    assert 'guest:i10-token-a' not in key_a
    assert 'guest:i10-token-b' not in key_b


def test_real_user_rate_limit_key_starts_with_user(app, db_session):
    """正式 User 登录后 rate_limit_key 以 'user:' 开头。"""
    from flask_login import current_user, login_user

    from core.db_models import User
    from core.security import rate_limit_key

    user = User(username='i10_real_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    with app.app_context():
        with app.test_request_context(
            '/',
            environ_base={'REMOTE_ADDR': '203.0.113.60'},
        ):
            login_user(user)
            assert current_user.is_authenticated is True
            assert getattr(current_user, 'is_guest', False) is False
            key = rate_limit_key()

    assert key.startswith('user:')
    assert key == f'user:{user.id}'
    assert not key.startswith('ip:')


def test_guest_post_ml_predict_forbidden(client, app, db_session):
    """guest login_user 后 POST /api/v1/ml/predict（带 CSRF）→ 403 guest_not_allowed。"""
    csrf_token, _guest = _login_guest_via_session(
        client, app, guest_id='guest:i10_ml', csrf_token='i10-ml-csrf'
    )

    response = client.post(
        '/api/v1/ml/predict',
        json={
            'age': 70,
            'gender': '男',
            'temperature': 28,
            'humidity': 60,
        },
        headers={'X-CSRF-Token': csrf_token},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload is not None
    assert payload.get('success') is False
    assert payload.get('error') == 'guest_not_allowed'


def test_guest_post_ai_ask_forbidden(client, app, db_session):
    """guest POST /api/v1/ai/ask → 403 guest_not_allowed。"""
    csrf_token, _guest = _login_guest_via_session(
        client, app, guest_id='guest:i10_ai', csrf_token='i10-ai-csrf'
    )

    response = client.post(
        '/api/v1/ai/ask',
        json={'question': '今天适合出门吗？', 'model': 'dummy'},
        headers={'X-CSRF-Token': csrf_token},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload is not None
    assert payload.get('success') is False
    assert payload.get('error') == 'guest_not_allowed'


def test_anonymous_post_costly_apis_not_guest_403(client, app, db_session):
    """未登录 POST 高成本接口：不得返回 guest_not_allowed，应为 401/302 登录。"""
    csrf_token = 'i10-anon-csrf'
    with client.session_transaction() as sess:
        sess['_csrf_token'] = csrf_token

    endpoints = (
        (
            '/api/v1/ml/predict',
            {
                'age': 70,
                'gender': '男',
                'temperature': 28,
                'humidity': 60,
            },
        ),
        (
            '/api/v1/ai/ask',
            {'question': '今天适合出门吗？', 'model': 'dummy'},
        ),
    )

    for path, body in endpoints:
        response = client.post(
            path,
            json=body,
            headers={'X-CSRF-Token': csrf_token},
        )
        # 未登录由 @login_required 处理，不得误报游客门禁
        payload = response.get_json(silent=True) or {}
        assert payload.get('error') != 'guest_not_allowed', (
            f'{path}: 未登录不应返回 guest_not_allowed，实际 body={payload}'
        )
        # 期望登录拦截：302 跳转 /login，或 401；部分环境下可能先 CSRF/其它码
        assert response.status_code in (302, 401), (
            f'{path}: 未登录期望 401/302，实际 {response.status_code} body={payload}'
        )
        if response.status_code == 302:
            location = response.headers.get('Location') or ''
            assert 'login' in location.lower() or location.startswith('/'), (
                f'{path}: 302 Location 应指向登录相关路径，实际 {location}'
            )
