# -*- coding: utf-8 -*-
"""已删除的匿名高德代理必须持续保持不可访问。"""

import pytest
import requests


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/_AMapService/v3/place/text?keywords=test&key=client-key"),
        ("GET", "/_AMapService/v3/weather/weatherInfo?city=360428"),
        ("GET", "/_AMapService"),
    ),
)
def test_removed_amap_proxy_is_always_404_without_upstream_request(
    client,
    monkeypatch,
    method,
    path,
):
    upstream_calls = []

    def forbidden_request(*args, **kwargs):
        upstream_calls.append((args, kwargs))
        raise AssertionError("已删除端点不应向高德发送请求")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_request)

    response = client.open(path, method=method)

    assert response.status_code == 404
    assert upstream_calls == []


def test_amap_proxy_route_is_absent(app):
    assert all(
        not rule.rule.startswith("/_AMapService")
        for rule in app.url_map.iter_rules()
    )
