# -*- coding: utf-8 -*-
"""高德前端与服务端配置隔离测试。"""

import logging

from flask import Flask

from core.config import configure_app


def _configure_test_app(monkeypatch):
    monkeypatch.setenv('DEBUG', 'true')
    monkeypatch.setenv('WECHAT_FORMAL_RUNTIME', '0')
    monkeypatch.setenv('ACCOUNT_LINK_CODE_PEPPER', '')
    app = Flask('amap-config-contract')
    configure_app(app, logging.getLogger('amap-config-contract'))
    return app


def test_split_amap_keys_are_loaded_by_purpose(monkeypatch):
    monkeypatch.setenv('AMAP_KEY', 'legacy-key-that-must-not-win-123')
    monkeypatch.setenv('AMAP_JS_API_KEY', 'browser-js-key-123456789012345678')
    monkeypatch.setenv(
        'AMAP_WEB_SERVICE_KEY',
        'server-web-key-123456789012345678',
    )
    monkeypatch.setenv(
        'AMAP_SECURITY_JS_CODE',
        'browser-security-code-1234567890123',
    )

    app = _configure_test_app(monkeypatch)

    assert app.config['AMAP_JS_API_KEY'] == (
        'browser-js-key-123456789012345678'
    )
    assert app.config['AMAP_WEB_SERVICE_KEY'] == (
        'server-web-key-123456789012345678'
    )
    assert app.config['AMAP_KEY'] == ''
    assert app.config['AMAP_LEGACY_KEY_CONFIGURED'] is True


def test_legacy_amap_key_is_detected_but_never_used_as_fallback(monkeypatch):
    monkeypatch.setenv('AMAP_KEY', 'legacy-compatible-key-123456789012')
    monkeypatch.delenv('AMAP_JS_API_KEY', raising=False)
    monkeypatch.delenv('AMAP_WEB_SERVICE_KEY', raising=False)

    app = _configure_test_app(monkeypatch)

    assert app.config['AMAP_KEY'] == ''
    assert app.config['AMAP_LEGACY_KEY_CONFIGURED'] is True
    assert app.config['AMAP_JS_API_KEY'] == ''
    assert app.config['AMAP_WEB_SERVICE_KEY'] == ''


def test_coordinate_verification_ttl_is_bounded(monkeypatch):
    monkeypatch.setenv('COOLING_COORDINATE_VERIFICATION_TTL_DAYS', '9999')

    app = _configure_test_app(monkeypatch)

    assert app.config['COOLING_COORDINATE_VERIFICATION_TTL_DAYS'] == 730


def test_heat_gis_receives_only_browser_amap_credentials(
    app,
    authenticated_client,
):
    app.config['AMAP_JS_API_KEY'] = 'j' * 32
    app.config['AMAP_SECURITY_JS_CODE'] = 's' * 32
    app.config['AMAP_WEB_SERVICE_KEY'] = 'server-web-key-must-stay-private'
    app.config['AMAP_KEY'] = 'legacy-key-must-stay-unused'

    response = authenticated_client.get('/heat-exposure-gis')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f"key={'j' * 32}" in body
    assert 'securityJsCode: "' + ('s' * 32) + '"' in body
    assert 'server-web-key-must-stay-private' not in body
    assert 'legacy-key-must-stay-unused' not in body


def test_location_resolver_uses_server_key_instead_of_browser_key(
    app,
    db_session,
    monkeypatch,
):
    from services.location_resolver import resolve_location

    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                'status': '1',
                'geocodes': [{
                    'location': '116.20,29.27',
                    'formatted_address': '测试地址',
                }],
            }

    def fake_get(url, params=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        captured['timeout'] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        'services.location_resolver.requests.get',
        fake_get,
    )
    with app.app_context():
        app.config['CITY_LOCATION_MAP'] = {}
        app.config['AMAP_JS_API_KEY'] = 'browser-key-must-not-be-used'
        app.config['AMAP_WEB_SERVICE_KEY'] = 'server-key-used-for-geocode'
        app.config['AMAP_KEY'] = 'legacy-key-must-not-win'
        result = resolve_location('测试用未知地址')

    assert result['provider'] == 'amap'
    assert captured['params']['key'] == 'server-key-used-for-geocode'
    assert captured['timeout'] == 10


def test_location_resolver_ignores_legacy_key(
    app,
    db_session,
    monkeypatch,
):
    from services.location_resolver import resolve_location

    called = False

    def fake_get(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError('废弃 Key 不应触发高德请求')

    monkeypatch.setattr(
        'services.location_resolver.requests.get',
        fake_get,
    )
    with app.app_context():
        app.config['CITY_LOCATION_MAP'] = {}
        app.config['AMAP_WEB_SERVICE_KEY'] = ''
        app.config['AMAP_KEY'] = 'legacy-key-must-never-be-used'
        result = resolve_location('另一个测试未知地址')

    assert result['provider'] == 'fallback'
    assert called is False
