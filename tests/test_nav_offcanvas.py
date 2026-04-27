# -*- coding: utf-8 -*-
"""Regression tests for the offcanvas navigation + local vendor assets."""


def test_nav_offcanvas_present(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'data-bs-toggle="offcanvas"' in body
    assert 'id="appNavDrawer"' in body
    assert '/static/vendor/bootstrap/bootstrap.bundle.min.js' in body


def test_base_loads_light_motion_assets(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'data-motion="m1 m2 m4 m5"' in body
    assert '/static/css/yilao-motion.css' in body
    assert '/static/css/yilao-data-fx.css' in body
    assert '/static/css/yilao-data-fx-extra.css' in body
    assert '/static/js/yilao-motion.js' in body
    assert '/static/js/yilao-data-fx.js' in body
    assert '/static/js/yilao-data-fx-extra.js' in body
    assert client.get('/static/css/yilao-motion.css').status_code == 200
    assert client.get('/static/css/yilao-data-fx.css').status_code == 200
    assert client.get('/static/css/yilao-data-fx-extra.css').status_code == 200
    assert client.get('/static/js/yilao-motion.js').status_code == 200
    assert client.get('/static/js/yilao-data-fx.js').status_code == 200
    assert client.get('/static/js/yilao-data-fx-extra.js').status_code == 200
