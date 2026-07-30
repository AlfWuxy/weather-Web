# -*- coding: utf-8 -*-
"""公开网页搜索发现与爬虫隐私边界测试。"""

import json
import re
import xml.etree.ElementTree as ET


INDEXABLE_PATHS = {
    "https://yilaoweather.org/",
    "https://yilaoweather.org/risk",
    "https://yilaoweather.org/cooling",
    "https://yilaoweather.org/duchang-heat-vulnerability-map",
    "https://yilaoweather.org/transparency",
    "https://yilaoweather.org/about/trust-network",
}


def _meta_content(body, name):
    match = re.search(
        rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)"',
        body,
        flags=re.IGNORECASE,
    )
    assert match is not None
    return match.group(1)


def _canonical(body):
    match = re.search(
        r'<link\s+rel="canonical"\s+href="([^"]+)"',
        body,
        flags=re.IGNORECASE,
    )
    assert match is not None
    return match.group(1)


def test_robots_points_to_sitemap_and_keeps_private_routes_blocked(client):
    response = client.get("/robots.txt")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sitemap: https://yilaoweather.org/sitemap.xml" in body
    assert "Allow: /llms.txt" in body
    for path in (
        "/admin",
        "/api/",
        "/mp/api/",
        "/community-risk",
        "/account-link",
        "/healthz",
        "/e/",
        "/t/",
    ):
        assert f"Disallow: {path}" in body
    for public_path in (
        "/risk",
        "/cooling",
        "/duchang-heat-vulnerability-map",
        "/transparency",
    ):
        assert f"Disallow: {public_path}" not in body


def test_sitemap_contains_only_fixed_anonymous_pages_and_ignores_host(client):
    response = client.get(
        "/sitemap.xml",
        headers={"Host": "attacker.example"},
    )
    root = ET.fromstring(response.get_data(as_text=True))
    locations = {
        node.text
        for node in root.findall(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}url/"
            "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
        )
    }

    assert response.status_code == 200
    assert response.mimetype == "application/xml"
    assert locations == INDEXABLE_PATHS
    assert all("?" not in location for location in locations)
    assert all("attacker.example" not in location for location in locations)


def test_home_has_search_metadata_canonical_and_valid_json_ld(client):
    response = client.get("/", headers={"Host": "attacker.example"})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert _canonical(body) == "https://yilaoweather.org/"
    assert _meta_content(body, "robots") == "index, follow"
    description = _meta_content(body, "description")
    assert "都昌县" in description
    assert "高温" in description
    assert "attacker.example" not in body

    schemas = [
        json.loads(text)
        for text in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            body,
            flags=re.DOTALL,
        )
    ]
    assert {schema["@type"] for schema in schemas} == {
        "WebSite",
        "WebApplication",
    }
    assert all(schema["@context"] == "https://schema.org" for schema in schemas)
    assert all(schema["url"] == "https://yilaoweather.org/" for schema in schemas)
    assert (
        'href="/duchang-heat-vulnerability-map"'
        in body
    )
    assert "查看热暴露与老年人口地图" in body


def test_public_pages_link_back_to_heat_vulnerability_map(client):
    for path in ("/", "/risk", "/transparency"):
        body = client.get(path).get_data(as_text=True)
        assert 'href="/duchang-heat-vulnerability-map"' in body


def test_llms_txt_lists_only_public_discovery_pages_and_privacy_boundary(client):
    response = client.get(
        "/llms.txt",
        headers={"Host": "attacker.example"},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert response.headers["X-Robots-Tag"] == "index, follow"
    assert response.headers["Cache-Control"] == "public, max-age=3600"
    assert "实验性 AI 发现摘要" in body
    assert "不代表正式或通用的网络标准" in body
    for path in (
        "/",
        "/risk",
        "/cooling",
        "/duchang-heat-vulnerability-map",
        "/transparency",
        "/about/trust-network",
        "/sitemap.xml",
        "/robots.txt",
    ):
        assert f"https://yilaoweather.org{path}" in body
    for private_term in (
        "登录后页面",
        "管理后台",
        "家庭与照护关系",
        "社区私密工作区",
        "手机号",
        "微信身份",
        "绑定码",
        "用户精确位置",
    ):
        assert private_term in body
    assert "attacker.example" not in body
    assert "https://yilaoweather.org/admin" not in body
    assert "https://yilaoweather.org/community-risk" not in body


def test_public_content_pages_have_unique_descriptions_and_canonical_urls(
    app,
    client,
    db_session,
    monkeypatch,
):
    from blueprints import public
    from flask import render_template

    monkeypatch.setattr(
        public,
        "render_cooling_resources_page",
        lambda **_kwargs: render_template(
            "cooling.html",
            resources_by_community={},
            communities=[],
            resource_types=[],
            map_points=[],
            total=0,
            outdoor_temp=None,
        ),
    )
    monkeypatch.setattr(
        public,
        "render_public_risk_page",
        lambda _location: render_template(
            "risk.html",
            location="都昌县",
            weather=None,
            heat_result=None,
            risk_label=None,
            actions=[],
            risk_reasons=[],
        ),
    )

    descriptions = set()
    for path in (
        "/risk?location=测试",
        "/cooling?type=医院",
        "/duchang-heat-vulnerability-map?layer=age65",
        "/transparency",
        "/about/trust-network",
    ):
        response = client.get(path)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert _meta_content(body, "robots") == "index, follow"
        assert "?" not in _canonical(body)
        descriptions.add(_meta_content(body, "description"))
    assert len(descriptions) == 5


def test_login_and_private_pages_are_noindex(client, db_session):
    login = client.get("/login")

    assert login.status_code == 200
    assert _meta_content(login.get_data(as_text=True), "robots").startswith(
        "noindex"
    )
    assert login.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    api = client.get("/mp/api/v1/bootstrap")
    assert api.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
