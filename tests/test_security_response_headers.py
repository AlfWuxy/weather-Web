# -*- coding: utf-8 -*-
"""HTML / API 响应的 nosniff 与 X-Frame-Options。"""


def _assert_csp_unchanged(response, policy):
    assert response.headers['Content-Security-Policy'] == policy
    assert response.headers.getlist('Content-Security-Policy') == [policy]


def test_login_html_gets_nosniff_and_sameorigin(client, db_session):
    response = client.get('/login')
    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'SAMEORIGIN'


def test_map_parent_page_stays_sameorigin(admin_client):
    admin_client.application.config['AMAP_JS_API_KEY'] = 'a' * 32
    response = admin_client.get('/community-risk')
    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    body = response.get_data(as_text=True)
    assert 'amap-sandbox.html' in body
    assert 'sandbox="allow-scripts"' in body
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'SAMEORIGIN'


def test_html_and_api_keep_existing_csp(app, client):
    html_policy = "default-src 'self'; frame-ancestors 'self'"
    json_policy = "default-src 'none'"
    problem_policy = "default-src 'none'; frame-ancestors 'none'"

    def html_page():
        response = app.response_class(
            '<!doctype html><title>headers</title>',
            content_type='text/html; charset=utf-8',
        )
        response.headers['Content-Security-Policy'] = html_policy
        return response

    def json_page():
        response = app.response_class(
            '{"ok":true}',
            content_type='application/json; charset=utf-8',
        )
        response.headers['Content-Security-Policy'] = json_policy
        return response

    def problem_page():
        response = app.response_class(
            '{"error":"nope"}',
            content_type='application/problem+json',
        )
        response.headers['Content-Security-Policy'] = problem_policy
        return response

    app.add_url_rule('/__security_headers_html', 'security_headers_html', html_page)
    app.add_url_rule('/__security_headers_json', 'security_headers_json', json_page)
    app.add_url_rule('/__security_headers_problem', 'security_headers_problem', problem_page)

    html = client.get('/__security_headers_html')
    assert html.status_code == 200
    assert html.headers['X-Content-Type-Options'] == 'nosniff'
    assert html.headers['X-Frame-Options'] == 'SAMEORIGIN'
    _assert_csp_unchanged(html, html_policy)

    api = client.get('/__security_headers_json')
    assert api.status_code == 200
    assert api.mimetype == 'application/json'
    assert api.headers['X-Content-Type-Options'] == 'nosniff'
    assert api.headers['X-Frame-Options'] == 'DENY'
    _assert_csp_unchanged(api, json_policy)

    problem = client.get('/__security_headers_problem')
    assert problem.headers['X-Content-Type-Options'] == 'nosniff'
    assert problem.headers['X-Frame-Options'] == 'DENY'
    _assert_csp_unchanged(problem, problem_policy)


def test_amap_sandbox_stays_frameable_by_same_site(client):
    response = client.get('/static/amap-sandbox.html')
    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers.get('X-Frame-Options') is None
    assert 'Content-Security-Policy' not in response.headers


def test_static_stylesheet_does_not_get_document_frame_headers(client, db_session):
    login = client.get('/login')
    assert login.status_code == 200

    css = client.get('/static/css/apple-polish.css')
    assert css.status_code == 200
    assert css.mimetype == 'text/css'
    assert css.headers.get('X-Frame-Options') is None
    assert css.headers.get('X-Content-Type-Options') is None
    cache = css.headers.get('Cache-Control', '')
    assert 'no-store' not in cache
    assert 'max-age=' in cache
