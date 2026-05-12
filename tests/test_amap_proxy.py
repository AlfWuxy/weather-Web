# -*- coding: utf-8 -*-
import json


def test_amap_proxy_appends_server_side_jscode(client, app, monkeypatch):
    class FakeResp:
        status_code = 200
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        content = json.dumps({'status': '1'}).encode('utf-8')

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        captured['timeout'] = timeout
        return FakeResp()

    monkeypatch.setattr('blueprints.public.requests.get', fake_get)

    with app.app_context():
        app.config['AMAP_SECURITY_JS_CODE'] = 'server-side-security-code-123456'

    response = client.get('/_AMapService/v3/place/text?key=frontend-visible-key&keywords=test&jscode=bad-value')
    assert response.status_code == 200
    assert response.get_json()['status'] == '1'
    assert captured['url'] == 'https://restapi.amap.com/v3/place/text'
    assert ('key', 'frontend-visible-key') in captured['params']
    assert ('keywords', 'test') in captured['params']
    assert ('jscode', 'server-side-security-code-123456') in captured['params']
    assert ('jscode', 'bad-value') not in captured['params']


def test_amap_proxy_rejects_invalid_path(client, app):
    with app.app_context():
        app.config['AMAP_SECURITY_JS_CODE'] = 'server-side-security-code-123456'

    response = client.get('/_AMapService/../../etc/passwd')
    assert response.status_code == 404


def test_amap_proxy_rejects_unlisted_path(client, app):
    with app.app_context():
        app.config['AMAP_SECURITY_JS_CODE'] = 'server-side-security-code-123456'

    response = client.get('/_AMapService/v3/weather/weatherInfo?city=110000')
    assert response.status_code == 404


def test_amap_proxy_rejects_oversized_response(client, app, monkeypatch):
    class FakeResp:
        status_code = 200
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        content = b'{' + (b'"x":' + b'"a"' * (256 * 1024)) + b'}'

    monkeypatch.setattr('blueprints.public.requests.get', lambda *args, **kwargs: FakeResp())

    with app.app_context():
        app.config['AMAP_SECURITY_JS_CODE'] = 'server-side-security-code-123456'

    response = client.get('/_AMapService/v3/place/text?key=frontend-visible-key&keywords=test')
    assert response.status_code == 502
