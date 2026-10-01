# -*- coding: utf-8 -*-
"""P1：HTML 429 中文页与 API JSON 按路径分流。"""
from flask_limiter.wrappers import LimitGroup

from core.extensions import limiter
from core.hooks import _wants_rate_limit_json

HTML_ESCAPE_TEXT = '先看今日公开风险'
ELDER_MODE_HREF = '/guest?next=/elder-mode'
RISK_HREF = '/risk'


def _reset_limiter():
    limiter.reset()


def _tighten_default_limit(limit_str='1 per minute'):
    original = list(limiter.limit_manager._default_limits)
    limiter.limit_manager.set_default_limits([
        LimitGroup(limit_provider=limit_str, key_function=limiter._key_func),
    ])
    _reset_limiter()
    return original


def _restore_default_limits(original):
    limiter.limit_manager.set_default_limits(original)
    _reset_limiter()


def test_rate_limit_json_split_is_path_only():
    assert _wants_rate_limit_json('/api/v1/weather/current') is True
    assert _wants_rate_limit_json('/mp/api/v1/me') is True
    assert _wants_rate_limit_json('/_AMapService/v3/place/text') is True
    assert _wants_rate_limit_json('/login') is False
    assert _wants_rate_limit_json('/risk') is False
    assert _wants_rate_limit_json('/guest') is False


def test_html_get_429_is_chinese_page_with_escape_hatches(app, client):
    original = _tighten_default_limit()
    try:
        first = client.get('/login', follow_redirects=False)
        blocked = client.get(
            '/login',
            headers={'Accept': 'application/json'},
            follow_redirects=False,
        )

        assert first.status_code == 200
        assert blocked.status_code == 429
        assert blocked.mimetype == 'text/html'
        body = blocked.get_data(as_text=True)
        assert HTML_ESCAPE_TEXT in body
        assert f'href="{RISK_HREF}"' in body
        assert f'href="{ELDER_MODE_HREF}"' in body
        assert 'href="/elder"' not in body
        assert 'href="/elder?' not in body
        assert '回到今天' not in body
        assert 'location.reload' not in body
        assert blocked.headers.get('Retry-After', '').isdigit()
        assert int(blocked.headers['Retry-After']) >= 0
        assert blocked.get_json(silent=True) is None
    finally:
        _restore_default_limits(original)


def test_html_post_429_does_not_auto_reload(app, client, db_session):
    app.config['RATE_LIMIT_LOGIN'] = '1 per minute'
    _reset_limiter()
    csrf = 'html-429-csrf'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf
    payload = {
        'username': 'missing-user',
        'password': 'wrong-password',
        'csrf_token': csrf,
    }

    try:
        first = client.post('/login', data=payload, follow_redirects=False)
        blocked = client.post('/login', data=payload, follow_redirects=False)

        assert first.status_code == 200
        assert blocked.status_code == 429
        body = blocked.get_data(as_text=True)
        assert HTML_ESCAPE_TEXT in body
        assert f'href="{ELDER_MODE_HREF}"' in body
        assert 'location.reload' not in body
        assert 'window.location' not in body
        assert '请返回上一页后重新提交' in body
        assert blocked.headers.get('Retry-After', '').isdigit()
    finally:
        _reset_limiter()


def test_api_429_is_json_without_html_escape_copy(app, client):
    app.config['RATE_LIMIT_WEATHER'] = '1 per minute'
    _reset_limiter()
    same_ip = {'REMOTE_ADDR': '203.0.113.40'}

    try:
        first = client.get(
            '/api/v1/weather/current',
            environ_overrides=same_ip,
            follow_redirects=False,
        )
        blocked = client.get(
            '/api/v1/weather/current',
            headers={'Accept': 'text/html'},
            environ_overrides=same_ip,
            follow_redirects=False,
        )

        assert first.status_code in (200, 400, 503)
        assert blocked.status_code == 429
        assert blocked.is_json
        body = blocked.get_json()
        assert body['success'] is False
        assert body['error'] == 'rate_limited'
        assert body['message'] == '请求过于频繁，请稍后再试'
        assert isinstance(body['retry_after'], int)
        assert body['retry_after'] >= 0
        raw = blocked.get_data(as_text=True)
        assert HTML_ESCAPE_TEXT not in raw
        assert '进入老人模式' not in raw
        assert blocked.headers.get('Retry-After') == str(body['retry_after'])
    finally:
        _reset_limiter()


def test_amap_proxy_429_is_json(app, client):
    app.config['RATE_LIMIT_AMAP_PROXY'] = '1 per minute'
    _reset_limiter()
    path = '/_AMapService/v3/weather/weatherInfo'
    same_ip = {'REMOTE_ADDR': '203.0.113.41'}

    try:
        first = client.get(path, environ_overrides=same_ip, follow_redirects=False)
        blocked = client.get(path, environ_overrides=same_ip, follow_redirects=False)

        assert first.status_code != 429
        assert blocked.status_code == 429
        assert blocked.is_json
        body = blocked.get_json()
        assert body['success'] is False
        assert body['error'] == 'rate_limited'
        assert HTML_ESCAPE_TEXT not in blocked.get_data(as_text=True)
    finally:
        _reset_limiter()
