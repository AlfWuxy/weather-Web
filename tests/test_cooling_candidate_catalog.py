# -*- coding: utf-8 -*-
"""历史候选可阅读，但不能绕过正式资源与坐标核验。"""

import json
import re

import pytest

from services import cooling_candidate_catalog as catalog


@pytest.fixture
def candidate_catalog(tmp_path, monkeypatch):
    from blueprints import admin, public

    records = [{
        "id": f"official-cooling-{index}", "name": f"历史候选场所{index}",
        "kind": "cooling_candidate", "public_preview": True,
        "category": "community_service", "public_role": "cooling_candidate",
        "address": f"历史地址{index}", "source_label": "政府公开报道",
        "source_url": "https://example.gov.cn/report", "source_date": "2022-07-01",
        "opening_hours_hint": "2022年报道：12:00—16:00",
        "audience_hint": "当时接待社区学生，老人接待条件待核验",
        "facilities_hint": "历史报道提到电风扇，未提及空调",
        "verification_note": "仅历史证据，当前开放未知",
        "current_opening_status": "unknown",
    } for index in range(10)]
    inventory_path, resource_path, legacy_path = (
        tmp_path / name for name in ("inventory.json", "resources.json", "legacy.json")
    )
    inventory_path.write_text(json.dumps({"records": records}), encoding="utf-8")
    resource_path.write_text(json.dumps({"features": [{
        "id": "mapped-candidate", "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [116.20, 29.27]},
        "properties": {"kind": "cooling_candidate", "inventory_ids": [records[0]["id"]],
                       "source_id": "B0TEST", "coordinate_source_url": "https://www.amap.com/place/B0TEST",
                       "coordinate_source": "高德公开点位，原坐标已转换 WGS84"},
    }]}), encoding="utf-8")
    # main 无旧候选文件，缺文件仍应保留官方十项。
    monkeypatch.setattr(catalog, "GIS_INVENTORY_PATH", inventory_path)
    monkeypatch.setattr(catalog, "GIS_RESOURCES_PATH", resource_path)
    monkeypatch.setattr(catalog, "AMAP_CANDIDATES_PATH", legacy_path)
    monkeypatch.setattr(public, "PUBLIC_COOLING_CANDIDATE_PATH", legacy_path)
    monkeypatch.setattr(admin, "COOLING_CANDIDATE_PATH", legacy_path)
    monkeypatch.setattr("services.public_service.get_weather_with_cache", lambda _location: ({}, False))
    return records, inventory_path, resource_path


@pytest.fixture
def rendered_context(monkeypatch):
    from services import public_service

    contexts = []
    render = public_service.render_template

    def capture(template, **context):
        if template == "cooling.html":
            contexts.append(context)
        return render(template, **context)

    monkeypatch.setattr(public_service, "render_template", capture)
    return contexts


def test_all_ten_historical_candidates_work_without_legacy_json(candidate_catalog):
    records, _, _ = candidate_catalog
    payload = catalog.load_cooling_candidate_catalog()
    official = [item for item in payload["items"] if item["catalog_origin"] == "official_inventory"]
    public = catalog.public_candidate_previews(payload)
    assert len(official) == 10
    assert len(public) == 10
    assert {item["name"] for item in official} == {row["name"] for row in records}
    assert all(item["is_active"] is False and item["has_ac"] is None for item in official)
    assert all(item["current_opening_status"] == "unknown" for item in public)
    assert all(not ({"longitude", "latitude", "source_id", "source_query"} & item.keys()) for item in public)


def test_only_linked_wgs84_point_is_converted_for_admin(candidate_catalog):
    from services.heat_risk_workbench_service import wgs84_to_gcj02

    payload = catalog.load_cooling_candidate_catalog()
    items = {item["source_id"]: item for item in payload["items"]}
    mapped, unmapped = items["official-cooling-0"], items["official-cooling-1"]
    expected = wgs84_to_gcj02(116.20, 29.27)
    assert mapped["longitude"] == round(expected[0], 6)
    assert mapped["latitude"] == round(expected[1], 6)
    assert mapped["coordinate_system"] == "GCJ-02"
    assert mapped["source_url"] == "https://example.gov.cn/report"
    assert "POI B0TEST" in mapped["coordinate_source"]
    assert "https://www.amap.com/place/B0TEST" in mapped["coordinate_source"]
    assert "example.gov.cn" not in mapped["coordinate_source"]
    assert len(mapped["coordinate_source"]) <= 500
    assert unmapped["longitude"] is None and unmapped["latitude"] is None
    assert unmapped["coordinate_system"] is None and unmapped["coordinate_source"] == ""


def test_public_candidates_never_create_resources_or_map_points(candidate_catalog, client, db_session, rendered_context):
    from core.db_models import CoolingResource

    records, _, _ = candidate_catalog
    response = client.get("/cooling")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert all(row["name"] in html for row in records)
    assert html.count('data-cooling-candidate="pending"') == 10
    assert CoolingResource.query.count() == 0
    assert rendered_context[-1]["map_points"] == []
    assert rendered_context[-1]["total"] == 0
    assert 'href="https://example.gov.cn/report"' in html
    assert "历史报道提到电风扇，未提及空调" in html
    assert "当前开放：</strong>未知" in html
    assert "official-cooling-" not in html


def test_candidates_stay_visible_beside_verified_formal_resources(candidate_catalog, client, db_session, rendered_context):
    from core.db_models import CoolingResource

    db_session.add(CoolingResource(community_code="都昌", name="正式核验资源", is_active=True,
                                  latitude=29.27, longitude=116.20))
    db_session.commit()
    html = client.get("/cooling").get_data(as_text=True)
    assert html.count('data-cooling-candidate="pending"') == 10
    assert len(rendered_context[-1]["map_points"]) == 1
    assert rendered_context[-1]["map_points"][0]["name"] == "正式核验资源"
    assert rendered_context[-1]["total"] == 1
    assert CoolingResource.query.count() == 1


@pytest.mark.parametrize("candidate_index", [0, 1])
def test_admin_prefill_is_text_only_without_assumed_facilities(candidate_catalog, admin_client, candidate_index):
    html = admin_client.get(f"/admin/cooling/add?candidate=official-cooling-{candidate_index}").get_data(as_text=True)
    assert f'name="name" value="历史候选场所{candidate_index}"' in html
    for field in ("latitude", "longitude"):
        tag = re.search(rf'<input[^>]*name="{field}"[^>]*>', html).group(0)
        assert 'value=""' in tag
    assert '<option value="GCJ-02" selected' not in html
    assert 'name="open_hours" value=""' in html
    for element_id in ("hasAc", "isAccessible", "isActive"):
        assert "checked" not in re.search(rf'<input[^>]*id="{element_id}"[^>]*>', html).group(0)
    assert "历史报道提到电风扇，未提及空调" in html
    assert "2022年报道：12:00—16:00" in html
    admin_html = admin_client.get("/admin/cooling/candidates").get_data(as_text=True)
    assert all(row["name"] in admin_html for row in candidate_catalog[0])


def test_candidate_html_escapes_names_and_rejects_script_urls(candidate_catalog, client, admin_client):
    records, inventory_path, _ = candidate_catalog
    records[0]["name"] = '<img src=x onerror="alert(1)">'
    records[0]["source_label"] = '<script>alert(2)</script>'
    records[0]["source_url"] = "javascript:alert(3)"
    inventory_path.write_text(json.dumps({"records": records}), encoding="utf-8")
    for response in (client.get("/cooling"), admin_client.get("/admin/cooling/candidates"),
                     admin_client.get("/admin/cooling/add?candidate=official-cooling-0")):
        html = response.get_data(as_text=True)
        assert "&lt;img src=x" in html
        assert "<img src=x" not in html
        assert "<script>alert(2)</script>" not in html
        assert "javascript:alert(3)" not in html


def test_inventory_requires_explicit_preview_and_public_category(candidate_catalog):
    records, inventory_path, _ = candidate_catalog
    records[0].pop("public_preview")
    records[1]["category"] = "hospital"
    records[2]["public_role"] = "medical_support"
    records[3]["is_active"] = True
    inventory_path.write_text(json.dumps({"records": records}), encoding="utf-8")
    official = [item for item in catalog.load_cooling_candidate_catalog()["items"]
                if item["catalog_origin"] == "official_inventory"]
    assert len(official) == 6


@pytest.mark.parametrize("field", ["items", "features", "records"])
def test_malformed_collection_does_not_break_candidate_pages(monkeypatch, field):
    payload = {"publication_status": "candidate_only", "coordinate_system": "GCJ-02", field: 42}
    monkeypatch.setattr(catalog, "_read", lambda _path: payload)
    assert catalog.load_cooling_candidate_catalog()["items"] == []
    assert catalog.public_candidate_previews(payload) == []


def test_missing_legacy_file_preserves_official_candidates_without_warning(candidate_catalog, tmp_path, caplog):
    payload = catalog.load_cooling_candidate_catalog(amap_path=tmp_path / "missing.json")
    assert len(payload["items"]) == 10
    assert "候选资料暂不可读" not in caplog.text


@pytest.mark.parametrize("field", ["public_role", "category"])
def test_unhashable_classification_is_rejected_without_breaking_preview(candidate_catalog, field):
    records, inventory_path, _ = candidate_catalog
    records[0][field] = []
    inventory_path.write_text(json.dumps({"records": records}), encoding="utf-8")
    payload = catalog.load_cooling_candidate_catalog()
    assert len([item for item in payload["items"] if item["catalog_origin"] == "official_inventory"]) == 9
    malformed = {**records[0], "verification_status": "pending_human_verification", "is_active": False}
    assert catalog.public_candidate_previews({"items": [malformed]}) == []


def test_committed_official_ten_keep_seven_mapped_three_unmapped():
    payload = catalog.load_cooling_candidate_catalog()
    official = [item for item in payload["items"] if item["catalog_origin"] == "official_inventory" and item["source_id"].startswith("official-cooling-")]
    assert len(official) == 10
    assert sum(item["latitude"] is not None for item in official) == 7
    assert sum(item["latitude"] is None and item["longitude"] is None for item in official) == 3
    assert all(item["source_url"] and item["prefill_open_hours"] == "" for item in official)
    assert all(item["has_ac"] is None and item["is_active"] is False for item in official)


def test_optional_legacy_catalog_can_add_seven_previews(candidate_catalog):
    # 合成旧目录验证兼容性，不要求 main 携带生产历史文件。
    legacy = {"publication_status": "candidate_only", "coordinate_system": "GCJ-02", "items": [
        {"source_id": f"legacy-{index}", "name": f"旧候选{index}", "category": "public_culture",
         "public_role": "cooling_candidate", "verification_status": "pending_human_verification",
         "is_active": False, "longitude": 116.20, "latitude": 29.27}
        for index in range(7)
    ]}
    catalog.AMAP_CANDIDATES_PATH.write_text(json.dumps(legacy), encoding="utf-8")
    assert len(catalog.public_candidate_previews(catalog.load_cooling_candidate_catalog())) == 17


def test_candidate_admin_requires_authentication(candidate_catalog, client, db_session):
    for url in ("/admin/cooling/candidates", "/admin/cooling/add?candidate=official-cooling-0",
                "/admin/cooling/999999/edit"):
        response = client.get(url)
        assert response.status_code == 302
        assert "/login" in response.location


def test_candidate_admin_rejects_non_admin(candidate_catalog, authenticated_client):
    for url in ("/admin/cooling/candidates", "/admin/cooling/add?candidate=official-cooling-0",
                "/admin/cooling/999999/edit"):
        response = authenticated_client.get(url)
        assert response.status_code == 302
        assert "/dashboard" in response.location


def test_candidate_admin_preview_is_read_only(candidate_catalog, admin_client):
    from core.db_models import CoolingResource

    assert CoolingResource.query.count() == 0
    assert admin_client.get("/admin/cooling/candidates").status_code == 200
    assert admin_client.get("/admin/cooling/add?candidate=official-cooling-0").status_code == 200
    invalid = admin_client.get("/admin/cooling/add?candidate=missing")
    assert invalid.status_code == 302 and invalid.location.endswith("/admin/cooling/candidates")
    assert CoolingResource.query.count() == 0


def test_candidate_save_without_activation_stays_inactive(candidate_catalog, admin_client):
    from core.db_models import CoolingResource

    with admin_client.session_transaction() as session:
        token = session["_csrf_token"]
    response = admin_client.post("/admin/cooling/add", data={
        "csrf_token": token, "community_code": "都昌", "name": "仅保存待核验资料",
        "resource_type": "待核验公共避暑资源", "address_hint": "资料地址",
    })
    assert response.status_code == 302
    resource = CoolingResource.query.filter_by(name="仅保存待核验资料").one()
    assert resource.is_active is False
    assert resource.has_ac is False and resource.is_accessible is False
    assert resource.latitude is None and resource.longitude is None
    assert "仅保存待核验资料" not in admin_client.get("/cooling").get_data(as_text=True)


def test_committed_main_catalog_has_sixty_public_previews_and_no_legacy_file():
    assert not catalog.AMAP_CANDIDATES_PATH.exists()
    payload = catalog.load_cooling_candidate_catalog()
    previews = catalog.public_candidate_previews(payload)
    assert len(previews) == 60
    assert sum(item["location_verification_status"] == "verified" for item in previews) == 50
    assert {"B0HAUZPGP9", "B0KG5SDIJN", "B03180SKW0", "B0JABMV4AM", "B0K1GUQA52"} <= {
        item["source_id"] for item in payload["items"]}


@pytest.mark.parametrize("initial_active", [False, True])
def test_edit_without_activation_keeps_or_makes_resource_inactive(candidate_catalog, admin_client, db_session, initial_active):
    from core.db_models import CoolingResource

    resource = CoolingResource(community_code="都昌", name="编辑时未启用", is_active=initial_active)
    db_session.add(resource)
    db_session.commit()
    with admin_client.session_transaction() as session:
        token = session["_csrf_token"]
    response = admin_client.post(f"/admin/cooling/{resource.id}/edit", data={
        "csrf_token": token, "community_code": "都昌", "name": "编辑时未启用", "notes": "只改备注",
    })
    assert response.status_code == 302
    db_session.refresh(resource)
    assert resource.is_active is False
    assert "编辑时未启用" not in admin_client.get("/cooling").get_data(as_text=True)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_edit_missing_resource_returns_404(admin_client, method):
    from core.db_models import CoolingResource

    assert CoolingResource.query.count() == 0
    with admin_client.session_transaction() as session:
        token = session["_csrf_token"]
    response = admin_client.open("/admin/cooling/999999/edit", method=method,
                                 data={"csrf_token": token})
    assert response.status_code == 404
    assert CoolingResource.query.count() == 0


def test_location_verification_does_not_invent_hours_or_activate_resources(candidate_catalog, client, db_session, rendered_context):
    from core.db_models import CoolingResource

    records, path, _ = candidate_catalog
    records[0].update(location_verification_status="verified",
                      location_verification_method="amap_poi_and_user_confirmation",
                      location_verified_at="2026-09-27", public_access_confirmation="user_confirmed",
                      location_verification_note="高德位置已核对；运营方确认可前往，未现场核验。",
                      opening_hours_hint="")
    path.write_text(json.dumps({"records": records}), encoding="utf-8")
    payload = catalog.load_cooling_candidate_catalog()
    point = next(item for item in payload["items"] if item["source_id"] == records[0]["id"])
    assert point["location_verification_status"] == "verified"
    assert point["current_opening_status"] == "unknown"
    assert point["prefill_open_hours"] == "" and point["has_ac"] is None
    assert point["is_active"] is False
    public = catalog.public_candidate_previews(payload)
    assert public[0]["location_verification_status"] == "verified"
    assert public[0]["public_access_confirmation"] == "user_confirmed"
    assert public[1]["location_verification_status"] == "pending"
    html = client.get("/cooling").get_data(as_text=True)
    assert "地点已核验" in html
    assert rendered_context[-1]["map_points"] == []
    assert rendered_context[-1]["total"] == 0
    assert CoolingResource.query.count() == 0


def test_new_inventory_record_wins_over_same_legacy_poi(candidate_catalog):
    records, path, _ = candidate_catalog
    # main 不附带生产旧候选文件，构造一个旧 POI 验证同 ID 覆盖次序。
    old = {"source_id": "B0HAUZPGP9", "name": "旧文化站资料", "category": "public_culture",
           "public_role": "cooling_candidate", "verification_status": "pending_human_verification",
           "is_active": False, "longitude": 116.406636, "latitude": 29.477729}
    catalog.AMAP_CANDIDATES_PATH.write_text(json.dumps({
        "publication_status": "candidate_only", "coordinate_system": "GCJ-02", "items": [old]
    }), encoding="utf-8")
    records[0].update(id=old["source_id"], name="本轮更新地点",
                      location_verification_status="verified",
                      location_verification_method="amap_poi_and_user_confirmation")
    path.write_text(json.dumps({"records": records}), encoding="utf-8")
    matches = [item for item in catalog.load_cooling_candidate_catalog()["items"] if item["source_id"] == old["source_id"]]
    assert len(matches) == 1
    assert matches[0]["name"] == "本轮更新地点"
    assert matches[0]["location_verification_status"] == "verified"


def test_public_preview_includes_all_62_entries_without_sixty_item_cutoff(candidate_catalog):
    records, path, _ = candidate_catalog
    expanded = [{**records[0], "id": f"expansion-{i}", "name": f"完整目录地点{i}"} for i in range(62)]
    path.write_text(json.dumps({"records": expanded}), encoding="utf-8")
    previews = catalog.public_candidate_previews(catalog.load_cooling_candidate_catalog())
    assert {row["name"] for row in expanded} <= {row["name"] for row in previews}


def test_unrecognized_location_verification_method_does_not_claim_verified():
    assert catalog._location_verification({"location_verification_status": "verified", "location_verification_method": "unknown"})["location_verification_status"] == "pending"
