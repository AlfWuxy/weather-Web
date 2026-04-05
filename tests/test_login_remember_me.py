# -*- coding: utf-8 -*-
"""Regression tests for login 'remember me' functionality."""


def _extract_set_cookie(resp):
    try:
        cookies = resp.headers.getlist('Set-Cookie')
    except Exception:
        cookies = resp.headers.get_all('Set-Cookie', [])
    return "\n".join(cookies or [])


def test_login_sets_remember_cookie_when_checked(client, db_session):
    from core.db_models import User

    user = User(username='remember_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    resp = client.post(
        '/login',
        data={
            'username': 'remember_user',
            'password': 'testpass',
            'csrf_token': csrf,
            'remember': '1',
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    set_cookie = _extract_set_cookie(resp)
    assert 'remember_token=' in set_cookie


def test_login_does_not_set_remember_cookie_by_default(client, db_session):
    from core.db_models import User

    user = User(username='no_remember_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    resp = client.post(
        '/login',
        data={
            'username': 'no_remember_user',
            'password': 'testpass',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    set_cookie = _extract_set_cookie(resp)
    assert 'remember_token=' not in set_cookie
