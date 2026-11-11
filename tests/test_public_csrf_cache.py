# -*- coding: utf-8 -*-
"""匿名页写出 CSRF 会话 cookie 时，响应不得进入共享缓存。"""


def _assert_not_stored(response):
    cache = response.headers.get('Cache-Control', '')
    assert 'no-store' in cache
    assert response.headers.get('Pragma') == 'no-cache'


def test_anonymous_login_and_register_are_not_stored(client, db_session):
    for path in ('/login', '/register'):
        response = client.get(path)
        assert response.status_code == 200
        _assert_not_stored(response)


def test_anonymous_care_action_page_is_not_stored(client, db_session):
    response = client.get('/action')
    assert response.status_code == 200
    _assert_not_stored(response)


def test_anonymous_page_that_sets_csrf_cookie_is_not_stored(client, db_session):
    response = client.get('/')
    assert response.status_code == 200
    cookie_name = client.application.config.get('SESSION_COOKIE_NAME', 'session')
    cookies = response.headers.getlist('Set-Cookie')
    assert any(item.split('=', 1)[0] == cookie_name for item in cookies)
    _assert_not_stored(response)


def test_static_assets_stay_cacheable_after_csrf_cookie(client, db_session):
    login = client.get('/login')
    assert login.status_code == 200
    _assert_not_stored(login)

    static = client.get('/static/css/apple-polish.css')
    assert static.status_code == 200
    cache = static.headers.get('Cache-Control', '')
    assert 'no-store' not in cache
    assert 'max-age=' in cache
    age = int(cache.split('max-age=', 1)[1].split(',', 1)[0].strip())
    assert age > 0


def test_robots_without_session_is_not_forced_no_store(client):
    response = client.get('/robots.txt')
    assert response.status_code == 200
    assert response.headers.getlist('Set-Cookie') == []
    assert 'no-store' not in response.headers.get('Cache-Control', '')


def test_permanent_csrf_session_refresh_is_not_stored(client, db_session):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'already-set-token'
        sess.permanent = True

    response = client.get('/robots.txt')
    assert response.status_code == 200
    _assert_not_stored(response)
