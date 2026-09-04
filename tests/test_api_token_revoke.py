# -*- coding: utf-8 -*-
"""个人设置里的 API Token 必须可列出、可撤销，且不能动别人的。"""


def _login_as(client, user_id: int, csrf_token='token-csrf'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token
    return csrf_token


def _create_user(db_session, username, role='user'):
    from core.db_models import User

    user = User(username=username, role=role)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def test_profile_lists_active_tokens_without_hashes(client, db_session):
    from core.usage import create_api_token

    user = _create_user(db_session, 'token_owner')
    create_api_token(user.id, name='我的手机')
    _login_as(client, user.id)

    response = client.get('/profile')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '我的手机' in body
    assert '撤销' in body
    assert '一次性绑定凭证' not in body
    assert '明文只显示一次' in body
    assert '长期使用' in body
    assert 'token_hash' not in body
    assert 'sha256' not in body.lower()


def test_profile_can_revoke_own_token(client, db_session):
    from core.db_models import ApiToken
    from core.usage import create_api_token, verify_api_token

    user = _create_user(db_session, 'token_revoker')
    plain = create_api_token(user.id, name='旧手机')
    token_row = ApiToken.query.filter_by(user_id=user.id).one()
    csrf = _login_as(client, user.id)

    response = client.post(
        '/profile',
        data={
            'form_id': 'revoke_api_token',
            'token_id': str(token_row.id),
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '已撤销' in body
    assert verify_api_token(plain) is None
    assert '旧手机' not in body or '已撤销' in body


def test_cannot_revoke_another_users_token(client, db_session):
    from core.db_models import ApiToken
    from core.usage import create_api_token, verify_api_token

    owner = _create_user(db_session, 'token_owner_b')
    attacker = _create_user(db_session, 'token_attacker')
    plain = create_api_token(owner.id, name='家人平板')
    token_row = ApiToken.query.filter_by(user_id=owner.id).one()
    csrf = _login_as(client, attacker.id)

    response = client.post(
        '/profile',
        data={
            'form_id': 'revoke_api_token',
            'token_id': str(token_row.id),
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert verify_api_token(plain) is not None
    assert db_session.get(ApiToken, token_row.id).revoked_at is None
