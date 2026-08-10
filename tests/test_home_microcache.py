# -*- coding: utf-8 -*-
"""匿名首页 Cloudflare 微缓存边界测试。"""


def test_fresh_anonymous_home_is_edge_cacheable_without_session_cookie(client):
    response = client.get('/')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['Cloudflare-CDN-Cache-Control'] == (
        'public, max-age=60, stale-while-revalidate=30'
    )
    assert response.headers.getlist('Set-Cookie') == []
    assert 'Cookie' not in response.headers.get('Vary', '')
    assert '<meta name="csrf-token" content="">' in response.get_data(as_text=True)


def test_home_with_existing_session_cookie_is_private(client):
    login_response = client.get('/login')
    assert login_response.headers.getlist('Set-Cookie')

    response = client.get('/')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Cloudflare-CDN-Cache-Control'] == 'no-store'
    assert '<meta name="csrf-token" content="">' not in response.get_data(as_text=True)


def test_home_with_query_string_is_private_and_creates_csrf_session(client):
    response = client.get('/?utm_source=cache-boundary-test')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Cloudflare-CDN-Cache-Control'] == 'no-store'
    assert response.headers.getlist('Set-Cookie')


def test_home_with_remember_cookie_is_private(client):
    client.set_cookie('remember_token', 'invalid-but-present')

    response = client.get('/')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Cloudflare-CDN-Cache-Control'] == 'no-store'


def test_authenticated_home_is_private(authenticated_client):
    response = authenticated_client.get('/')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Cloudflare-CDN-Cache-Control'] == 'no-store'
    assert 'test-csrf-token' in response.get_data(as_text=True)
