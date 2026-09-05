# -*- coding: utf-8 -*-
"""游客高成本 API 门禁：guest 调 AI/ML 应得 403 guest_not_allowed。

依赖 I01：`@reject_guest` 挂在 `POST /api/v1/ai/ask` 与 `POST /api/v1/ml/predict`。
"""


def _login_guest_user(client, app, csrf_token='guest-api-403-csrf'):
    """用 login_user(GuestUser(...)) 建立游客会话，并写入 CSRF。"""
    from flask import session as flask_session
    from flask_login import login_user

    from core.constants import GUEST_ID_PREFIX
    from core.guest import GuestUser

    guest_id = f'{GUEST_ID_PREFIX}i04_api_403'
    profile = {
        'username': '游客',
        'age': None,
        'gender': '未知',
        'community': '朝阳社区',
        'has_chronic_disease': False,
        'chronic_diseases': None,
    }
    guest = GuestUser(guest_id, profile)

    # 先植 guest 会话数据，user_loader 重建时 build_guest_user 会读这些键
    with client.session_transaction() as sess:
        sess['guest_id'] = guest_id
        sess['guest_profile'] = profile
        sess['_csrf_token'] = csrf_token

    # 在请求上下文中调用 login_user，再把 flask-login 会话键拷到 test client
    with app.test_request_context('/'):
        login_user(guest)
        login_keys = {
            key: flask_session[key]
            for key in ('_user_id', '_fresh', '_id')
            if key in flask_session
        }

    with client.session_transaction() as sess:
        sess.update(login_keys)
        # 兜底：确保 flask-login 识别为已登录 guest
        sess.setdefault('_user_id', guest_id)
        sess.setdefault('_fresh', True)
        sess['guest_id'] = guest_id
        sess['guest_profile'] = profile
        sess['_csrf_token'] = csrf_token

    return csrf_token, guest


def test_guest_post_ai_ask_returns_403_guest_not_allowed(client, app, db_session):
    """游客 session 调 POST /api/v1/ai/ask → 403 + guest_not_allowed。"""
    csrf_token, _guest = _login_guest_user(client, app)

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


def test_guest_post_ml_predict_returns_403_guest_not_allowed(client, app, db_session):
    """游客 session 调 POST /api/v1/ml/predict → 403 + guest_not_allowed。"""
    csrf_token, _guest = _login_guest_user(client, app)

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
