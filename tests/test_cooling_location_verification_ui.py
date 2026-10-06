# -*- coding: utf-8 -*-
"""地点核验、开放设施与正式发布必须在真实页面中分别呈现。"""

import json
import re
from html import unescape

import pytest


LOCATION_NOTE = (
    "高德名称与坐标已核对；项目负责人确认地点真实、可前往。"
    "未现场核验，开放时间及空调等设施信息未提供。"
)


@pytest.fixture
def location_candidates(monkeypatch):
    from blueprints import admin, public

    rows = []
    for index in range(51):
        verified = index < 50
        row = {
            "source_id": f"location-ui-{index:02d}",
            "name": f"公共场所{index:02d}" if verified else "旧待核验场所",
            "address": f"测试公共地址{index:02d}",
            "public_role": "cooling_candidate",
            "category": "community_service",
            "role_label": "公共纳凉场所",
            "category_label": "社区服务场所",
            "source_label": "测试公开资料",
            "source_url": "https://example.org/cooling-locations",
            "source_date": "2026-09-27",
            "verification_status": "pending_human_verification",
            "verification_note": "资料留档，开放时间与设施信息尚未提供。",
            "current_opening_status": "unknown",
            "opening_hours_hint": "",
            "audience_hint": "",
            "facilities_hint": "",
            "has_ac": None,
            "is_accessible": None,
            "is_active": False,
            "latitude": 29.27,
            "longitude": 116.20,
            "coordinate_system": "GCJ-02",
            "location_verification_status": "verified" if verified else "pending",
            "location_verification_method": "amap_poi_and_user_confirmation" if verified else "",
            "location_verified_at": "2026-09-27" if verified else "",
            "public_access_confirmation": "user_confirmed" if verified else "",
            "location_verification_note": LOCATION_NOTE if verified else "尚未确认地点。",
        }
        rows.append(row)

    def public_rows():
        # 公开候选入口仍不携带坐标、来源 ID 或管理预填字段。
        private_fields = {"source_id", "latitude", "longitude", "coordinate_system"}
        return [{key: value for key, value in row.items() if key not in private_fields}
                for row in rows]

    monkeypatch.setattr(public, "_public_cooling_candidates", public_rows)
    monkeypatch.setattr(admin, "_load_cooling_candidates", lambda: {
        "items": rows,
        "generated_at": "2026-09-27",
        "coordinate_system": "GCJ-02",
        "notice": "地点核验不代表已掌握开放时间及设施。",
    })
    monkeypatch.setattr("services.public_service.get_weather_with_cache", lambda _location: ({}, False))
    return rows


def _elements(html, tag, attribute, value):
    return re.findall(
        rf'<{tag}\b[^>]*\b{attribute}="{value}"[^>]*>(.*?)</{tag}>',
        html,
        re.DOTALL,
    )


def _visible_text(markup):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", markup)))


def _assert_location_details(markup, grouped_facilities=False):
    text = _visible_text(markup)
    compact = text.replace("：", "").replace(":", "")
    facilities = ("空调、无障碍未知",) if grouped_facilities else ("空调未知", "无障碍未知")
    for expected in ("地点已核验", "开放时间未提供", *facilities,
                     "高德名称与坐标已核对", "项目负责人确认地点真实、可前往", "未现场核验", "2026-09-27"):
        assert expected in compact
    assert "待人工核验" not in text


def _map_points(html):
    match = re.search(
        r'<script id="coolingMapData" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match
    return json.loads(match.group(1))


def test_public_page_separates_fifty_verified_locations_and_one_pending(location_candidates, client, db_session):
    from core.db_models import CoolingResource

    response = client.get("/cooling")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    verified = _elements(html, "article", "data-cooling-candidate", "location-verified")
    pending = _elements(html, "article", "data-cooling-candidate", "pending")
    assert len(verified) == 50
    assert len(pending) == 1
    for row, card in zip(location_candidates[:50], verified):
        assert row["name"] in _visible_text(card)
        _assert_location_details(card)
    assert "旧待核验场所" in _visible_text(pending[0])
    assert "待人工核验" in _visible_text(pending[0])
    assert "地点已核验" not in _visible_text(pending[0])
    assert "全部待人工核验" not in html
    assert CoolingResource.query.count() == 0
    assert _map_points(html) == []
    assert "location-ui-" not in html


def test_verified_locations_do_not_join_existing_formal_map_points(location_candidates, client, db_session):
    from core.db_models import CoolingResource
    from core.time_utils import utcnow

    resource = CoolingResource(
        community_code="都昌", name="现有正式避暑资源", is_active=True,
        latitude=29.27, longitude=116.20, coordinate_system="GCJ-02",
        coordinate_source="管理员现场核验", coordinate_verified_at=utcnow(),
        last_verified_at=utcnow(), verify_method="onsite",
    )
    db_session.add(resource)
    db_session.commit()

    response = client.get("/cooling")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    points = _map_points(html)
    assert len(points) == 1
    assert points[0]["name"] == "现有正式避暑资源"
    assert len(_elements(html, "article", "data-cooling-candidate", "location-verified")) == 50
    assert len(_elements(html, "article", "data-cooling-candidate", "pending")) == 1
    assert CoolingResource.query.count() == 1


def test_admin_table_keeps_all_location_names_statuses_and_unknown_facilities(location_candidates, admin_client):
    from core.db_models import CoolingResource

    response = admin_client.get("/admin/cooling/candidates")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    verified = _elements(html, "tr", "data-location-status", "verified")
    pending = _elements(html, "tr", "data-location-status", "pending")
    assert len(verified) == 50
    assert len(pending) == 1
    for row, table_row in zip(location_candidates[:50], verified):
        assert row["name"] in _visible_text(table_row)
        assert row["source_id"] in _visible_text(table_row)
        _assert_location_details(table_row)
    assert "旧待核验场所" in _visible_text(pending[0])
    assert "待人工核验" in _visible_text(pending[0])
    assert "地点已核验" not in _visible_text(pending[0])
    assert "全部待人工核验" not in html
    assert CoolingResource.query.count() == 0


@pytest.mark.parametrize("client_fixture,label,other_label", [
    ("client", "登录后在县域地图查看", "在县域地图查看这些地点"),
    ("admin_client", "在县域地图查看这些地点", "登录后在县域地图查看"),
])
def test_cooling_map_link_uses_direct_route_and_login_specific_label(
    location_candidates, db_session, request, client_fixture, label, other_label,
):
    page_client = request.getfixturevalue(client_fixture)
    response = page_client.get("/cooling")
    assert response.status_code == 200
    links = _elements(response.get_data(as_text=True), "a", "href", "/heat-exposure-gis")
    labels = [_visible_text(link) for link in links]
    assert label in labels
    assert other_label not in labels


def test_public_heat_preview_keeps_fifty_verified_and_one_pending(location_candidates, client, db_session):
    from core.db_models import CoolingResource

    response = client.get("/duchang-heat-vulnerability-map")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    verified = _elements(html, "article", "data-location-status", "verified")
    pending = _elements(html, "article", "data-location-status", "pending")
    assert len(verified) == 50
    assert len(pending) == 1
    for row, card in zip(location_candidates[:50], verified):
        assert row["name"] in _visible_text(card)
        _assert_location_details(card, grouped_facilities=True)
    assert "旧待核验场所" in _visible_text(pending[0])
    assert "待人工核验" in _visible_text(pending[0])
    assert "地点已核验" not in _visible_text(pending[0])
    assert "全部待人工核验" not in html
    assert "location-ui-" not in html
    assert CoolingResource.query.count() == 0


@pytest.mark.parametrize("url", ["/cooling", "/admin/cooling/candidates", "/duchang-heat-vulnerability-map"])
def test_location_verification_names_and_notes_are_escaped(location_candidates, admin_client, url):
    location_candidates[0]["name"] = '<img src=x onerror="alert(1)">'
    location_candidates[0]["location_verification_note"] = '<script>alert("location-note")</script>'

    response = admin_client.get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "&lt;img src=x" in html
    assert "&lt;script&gt;alert(" in html
    assert "location-note" in html
    assert "<img src=x" not in html
    assert '<script>alert("location-note")</script>' not in html
