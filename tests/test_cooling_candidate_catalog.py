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
    legacy = json.loads(catalog.AMAP_CANDIDATES_PATH.read_text(encoding="utf-8"))
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(catalog, "GIS_INVENTORY_PATH", inventory_path)
    monkeypatch.setattr(catalog, "GIS_RESOURCES_PATH", resource_path)
    monkeypatch.setattr(catalog, "AMAP_CANDIDATES_PATH", legacy_path)
    monkeypatch.setattr(public, "PUBLIC_COOLING_CANDIDATE_PATH", legacy_path)
    monkeypatch.setattr(admin, "COOLING_CANDIDATE_PATH", legacy_path)
    monkeypatch.setattr("services.public_service.get_weather_with_cache", lambda _location: ({}, False))
    return records, inventory_path, resource_path


def _map_points(html):
    match = re.search(r'<script id="coolingMapData" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert match
    return json.loads(match.group(1))


def test_all_ten_historical_candidates_join_seven_public_pois(candidate_catalog):
    records, _, _ = candidate_catalog
    payload = catalog.load_cooling_candidate_catalog()
    official = [item for item in payload["items"] if item["catalog_origin"] == "official_inventory"]
    public = catalog.public_candidate_previews(payload)
    assert len(official) == 10
    assert len(public) == 17
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


def test_public_candidates_never_create_resources_or_map_points(candidate_catalog, client, db_session):
    from core.db_models import CoolingResource

    records, _, _ = candidate_catalog
    response = client.get("/cooling")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert all(row["name"] in html for row in records)
    assert html.count('data-cooling-candidate="pending"') == 17
    assert CoolingResource.query.count() == 0
    assert _map_points(html) == []
    assert 'href="https://example.gov.cn/report"' in html
    assert "历史报道提到电风扇，未提及空调" in html
    assert "当前开放：</strong>未知" in html
    assert "official-cooling-" not in html


def test_candidates_stay_visible_beside_verified_formal_resources(candidate_catalog, client, db_session):
    from core.db_models import CoolingResource
    from core.time_utils import utcnow

    db_session.add(CoolingResource(community_code="都昌", name="正式核验资源", is_active=True,
                                  latitude=29.27, longitude=116.20, coordinate_system="GCJ-02",
                                  coordinate_source="人工现场核验", coordinate_verified_at=utcnow()))
    db_session.commit()
    html = client.get("/cooling").get_data(as_text=True)
    assert html.count('data-cooling-candidate="pending"') == 17
    assert len(_map_points(html)) == 1
    assert _map_points(html)[0]["name"] == "正式核验资源"
    assert CoolingResource.query.count() == 1


def test_admin_unmapped_prefill_has_no_zero_coordinates_or_assumed_facilities(candidate_catalog, admin_client):
    html = admin_client.get("/admin/cooling/add?candidate=official-cooling-1").get_data(as_text=True)
    assert 'name="name" value="历史候选场所1"' in html
    for element_id in ("coordinateLatitude", "coordinateLongitude", "coordinateSource"):
        tag = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', html).group(0)
        assert 'value=""' in tag
    assert '<option value="GCJ-02" selected' not in html
    assert 'name="open_hours" value=""' in html
    for element_id in ("hasAc", "isAccessible", "coordinateVerified", "isActive"):
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


def test_location_verification_does_not_invent_hours_or_activate_resources(candidate_catalog, client, db_session):
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
    assert _map_points(html) == []
    assert CoolingResource.query.count() == 0


def test_new_inventory_record_wins_over_same_legacy_poi(candidate_catalog):
    records, path, _ = candidate_catalog
    legacy = catalog._legacy_items(catalog._read(catalog.AMAP_CANDIDATES_PATH))
    old = next(item for item in legacy if item.get("public_role") == "cooling_candidate")
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
