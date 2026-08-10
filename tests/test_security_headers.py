# -*- coding: utf-8 -*-
"""统一浏览器安全头与 token Referer 防泄漏测试。"""


def test_global_security_headers_are_present(client):
    response = client.get('/')

    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert response.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert 'camera=()' in response.headers['Permissions-Policy']


def test_sensitive_token_paths_force_no_referrer(client, db_session):
    for path in ('/e/not-a-real-token', '/t/not-a-real-token'):
        response = client.get(path)
        assert response.headers['Referrer-Policy'] == 'no-referrer'
