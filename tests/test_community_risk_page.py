# -*- coding: utf-8 -*-


def test_community_risk_page_without_amap_key_shows_fallback_message(authenticated_client):
    response = authenticated_client.get('/community-risk')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert '未配置地图 Key' in html
    assert '地图已隐藏，右侧分析结果仍可正常查看。' in html
    assert 'id="community-list"' in html


def test_community_risk_page_uses_proxy_mode_without_exposing_security_code(authenticated_client):
    app = authenticated_client.application
    app.config['AMAP_JS_API_KEY'] = 'a' * 32
    app.config['AMAP_WEB_SERVICE_KEY'] = 'b' * 32
    app.config['AMAP_SECURITY_JS_CODE'] = 'c' * 32

    response = authenticated_client.get('/community-risk')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert 'serviceHost' in html
    assert '/_AMapService' in html
    assert 'securityJsCode' not in html
    assert ('c' * 32) not in html
    assert ('b' * 32) not in html
    assert ('a' * 32) in html
