# -*- coding: utf-8 -*-
"""Regression tests for the offcanvas navigation + local vendor assets."""


def test_nav_offcanvas_present(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'data-bs-toggle="offcanvas"' in body
    assert 'id="appNavDrawer"' in body
    assert '/static/vendor/bootstrap/bootstrap.bundle.min.js' in body

