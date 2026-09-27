# -*- coding: utf-8 -*-
"""点位目录的坐标、来源、去重、缓存与未知值边界。"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from services import heat_risk_poi_service as poi
from services import heat_risk_workbench_service as workbench


def _legacy():
    from config import COMMUNITY_COORDS_GCJ

    return workbench.village_points(COMMUNITY_COORDS_GCJ)


def _feature(point_id, name, point, kind="settlement", **properties):
    return {"type": "Feature", "id": point_id,
            "geometry": {"type": "Point", "coordinates": [point["lon_wgs84"], point["lat_wgs84"]]},
            "properties": {"id": point_id, "name": name, "kind": kind, "township_name": point.get("township"),
                           "source_id": "fixture", "source_url": "https://example.org/points/" + point_id,
                           "coordinate_precision": "mapped_point", "verification_status": "publicly_listed",
                           "settlement_level": "unknown", **properties}}


@pytest.fixture
def catalogs(tmp_path, monkeypatch):
    def write(settlements=(), resources=(), inventory=()):
        for attribute, features in (("SETTLEMENTS_PATH", settlements), ("RESOURCES_PATH", resources)):
            path = tmp_path / (attribute + ".json")
            path.write_text(json.dumps({"type": "FeatureCollection", "schema_version": "1.0",
                                       "sources": [{"id": "fixture", "name": "测试公开来源", "url": "https://example.org", "license": "test"}],
                                       "features": list(features)}), encoding="utf-8")
            monkeypatch.setattr(poi, attribute, path)
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps({"records": list(inventory)}), encoding="utf-8")
        monkeypatch.setattr(poi, "RESOURCE_INVENTORY_PATH", path)
        poi._static_catalog.cache_clear()
    write()
    return write


def test_imported_wgs84_is_not_converted_again(catalogs, monkeypatch):
    legacy = _legacy()
    feature = _feature("settlement:1", "新收录聚落", legacy[0])
    catalogs([feature])
    monkeypatch.setattr(workbench, "gcj02_to_wgs84", lambda *a: pytest.fail("WGS84 不应重复纠偏"))
    payload = poi.build_poi_payload(legacy)
    actual = next(v for v in payload["villages"] if v["id"] == "settlement:1")
    assert [actual["lon_wgs84"], actual["lat_wgs84"]] == feature["geometry"]["coordinates"]
    assert actual["township"] == "北山乡"


def test_only_same_name_same_township_nearby_merges(catalogs):
    legacy = _legacy()
    profile = SimpleNamespace(name=legacy[0]["name"], population=123, elderly_ratio=0.2)
    from config import COMMUNITY_COORDS_GCJ

    legacy = workbench.village_points(COMMUNITY_COORDS_GCJ, [profile])
    other_town = next(v for v in legacy if v["township"] != legacy[0]["township"])
    catalogs([
        _feature("settlement:near", legacy[0]["name"], legacy[0], potential_duplicate=True, potential_duplicate_ids=["settlement:far"]),
        _feature("settlement:other", legacy[0]["name"], other_town),
        _feature("settlement:far", legacy[0]["name"], legacy[3]),
    ])
    villages = poi.build_poi_payload(legacy)["villages"]
    original = next(v for v in villages if v["id"] == legacy[0]["id"])
    assert original["population"] == 123 and original["elderly_ratio"] == 0.2
    assert original["potential_duplicate"] and original["potential_duplicate_ids"] == ["settlement:far"]
    assert original["cell_id"] == legacy[0]["cell_id"]
    assert len(villages) == len(legacy) + 2
    assert all(v["population"] is None and v["elderly_ratio"] is None
               for v in villages if v["id"].startswith("settlement:"))


def test_imported_possible_duplicates_remain_separate(catalogs):
    point = _legacy()[0]
    catalogs([
        _feature("possible:1", "同名聚落", point, potential_duplicate=True, potential_duplicate_ids=["possible:2"]),
        _feature("possible:2", "同名聚落", point, potential_duplicate=True, potential_duplicate_ids=["possible:1"]),
    ])
    payload = poi.build_poi_payload([])
    assert len(payload["villages"]) == 2
    assert payload["poi_metadata"]["counts"]["potential_duplicates"] == 2


def test_source_merge_preserves_coordinates_dates_and_license():
    rich_source = {"id": "osm-node-1", "url": "https://www.openstreetmap.org/node/1",
                   "coordinates": [116.2, 29.33], "source_updated_at": "2026-09-01", "license": "ODbL"}
    result = poi._merge_sources({"sources": [rich_source]},
                                {"source_url": rich_source["url"], "source_id": "1", "source_label": "OSM"})
    assert len(result["sources"]) == 1
    for key, value in rich_source.items():
        assert result["sources"][0][key] == value


def test_candidates_never_become_open_cooling_resources(catalogs):
    from core.time_utils import utcnow

    point = _legacy()[0]
    catalogs(resources=[_feature("candidate:1", "公共活动场所", point, "cooling_candidate", has_ac=None,
                                 cooling_status="candidate", verified_at=utcnow().isoformat(),
                                 valid_until=(utcnow() + timedelta(days=30)).date().isoformat())])
    payload = poi.build_poi_payload([])
    assert payload["cooling_resources"] == []
    assert payload["cooling_candidates"][0]["cooling_status"] == "candidate"
    assert payload["cooling_candidates"][0]["has_ac"] is None


def test_bad_crs_outside_county_and_missing_source_are_rejected(catalogs):
    point = _legacy()[0]
    catalogs([
        _feature("bad:crs", "错误坐标系", point, coordinate_system="GCJ-02"),
        _feature("bad:outside", "县外点", {"lon_wgs84": 0, "lat_wgs84": 0}),
        _feature("bad:source", "无来源点", point, source_url=None),
    ])
    payload = poi.build_poi_payload([])
    assert payload["villages"] == []
    assert payload["poi_metadata"]["rejected_points"] == 3


def test_conflicting_township_has_explicit_warning(catalogs):
    catalogs([_feature("conflict:1", "乡镇存疑聚落", _legacy()[0], township_name="蔡岭镇")])
    payload = poi.build_poi_payload([])
    village = payload["villages"][0]
    assert village["source_township"] == "蔡岭镇"
    assert village["township"] == "北山乡"
    assert "归属待核验" in village["township_warning"]


def test_unmapped_county_records_and_review_status_are_preserved(catalogs):
    catalogs(inventory=[
        {"id": "county:1", "kind": "medical", "name": "县级机构", "township_name": None,
         "source_url": "https://example.org/registry", "status": "unmapped", "verification_status": "unmapped"},
        {"id": "review:1", "kind": "medical", "name": "拟注销机构", "township_name": "北山乡",
         "source_url": "https://example.org/registry", "status": "unmapped", "verification_status": "needs_review"},
    ])
    payload = poi.build_poi_payload([])
    assert len(payload["unmapped_resources"]) == 2
    town = next(row for row in payload["poi_coverage"] if row["township"] == "北山乡")
    assert town["unmapped_medical_count"] == 0 and town["needs_review_count"] == 1
    assert payload["poi_metadata"]["counts"]["unmapped_unassigned_township"] == 1


def test_located_official_record_is_removed_from_runtime_unmapped_only(catalogs):
    point = _legacy()[0]
    inventory = [{"id": "official:1", "kind": "medical", "name": "医院官方名称", "township_name": None,
                  "source_url": "https://example.org/registry", "status": "unmapped", "verification_status": "unmapped"},
                 {"id": "review:1", "kind": "medical", "name": "拟注销机构", "township_name": "北山乡",
                  "source_url": "https://example.org/review", "status": "unmapped", "verification_status": "needs_review"}]
    catalogs(resources=[_feature("mapped:1", "医院地图名称", point, "medical", official_record_ids=["official:1"])], inventory=inventory)
    payload = poi.build_poi_payload([])
    assert [row["id"] for row in payload["unmapped_resources"]] == ["review:1"]
    assert payload["poi_metadata"]["inventory_linked_count"] == 1
    assert len(json.loads(poi.RESOURCE_INVENTORY_PATH.read_text())["records"]) == 2


def test_exact_medical_dedup_and_24_township_counts(catalogs):
    point = {"lon_wgs84": 116.3935165, "lat_wgs84": 29.4802228, "township": "蔡岭镇"}
    catalogs(resources=[_feature("fixture:medical", "都昌县蔡岭中心卫生院", point, "medical")],
             inventory=[{"id": "unmapped:1", "name": "未定位机构", "kind": "medical", "township_name": "北山乡",
                         "source_url": "https://example.org/registry", "status": "unmapped", "address": "某街道"}])
    payload = poi.build_poi_payload([])
    assert len(payload["medical_pois"]) == 1
    assert len(payload["poi_coverage"]) == 24
    assert all(row["completeness"] == "unknown" for row in payload["poi_coverage"])
    by_town = {row["township"]: row for row in payload["poi_coverage"]}
    assert by_town["蔡岭镇"]["medical_count"] == 1
    assert by_town["北山乡"]["unmapped_medical_count"] == 1
    assert by_town["北山乡"]["medical_count"] == 0
    assert len(payload["unmapped_resources"]) == 1


def test_cached_static_catalog_does_not_repeat_polygon_matching(catalogs, monkeypatch):
    point = _legacy()[0]
    catalogs([_feature("cached:1", "缓存聚落", point)])
    first = poi.build_poi_payload([])
    monkeypatch.setattr(workbench, "_geometry_covers", lambda *a: pytest.fail("静态目录不应每请求重新落界"))
    second = poi.build_poi_payload([])
    assert first == second


def _cooling_row(point, **overrides):
    from core.time_utils import utcnow

    fields = dict(id=1, name="人工核验避暑点", longitude=point["lon_wgs84"], latitude=point["lat_wgs84"],
                  coordinate_system="WGS84", coordinate_source="https://example.org/inspection", coordinate_verified_at=utcnow(),
                  is_active=True, resource_type="community_center", open_hours=None, has_ac=True, is_accessible=None)
    return SimpleNamespace(**{**fields, **overrides})


def test_cooling_rows_respect_crs_and_valid_verification():
    from core.time_utils import utcnow

    point = _legacy()[0]
    gcj = workbench.wgs84_to_gcj02(point["lon_wgs84"], point["lat_wgs84"])
    rows = [_cooling_row(point), _cooling_row(point, id=2, coordinate_system="GCJ-02", longitude=gcj[0], latitude=gcj[1]),
            _cooling_row(point, id=3, coordinate_system=None), _cooling_row(point, id=4, coordinate_source=None),
            _cooling_row(point, id=5, coordinate_verified_at=utcnow() - timedelta(days=366))]
    actual = workbench._cooling_points(rows)
    assert len(actual) == 2
    assert actual[0]["lon_wgs84"] == point["lon_wgs84"]
    assert abs(actual[1]["lon_wgs84"] - point["lon_wgs84"]) < 1e-6
    assert all(row["cooling_status"] == "verified" for row in actual)


def test_missing_grid_is_not_ranked_as_low_risk():
    village = {"id": "unknown:1", "name": "无网格聚落", "static_level": None, "static_score": None}
    assert workbench.rank_villages([village], {"level": 2}) == []
    assert "网格数据缺失" in poi.priority_exclusion_reason(village)


def test_suspected_duplicate_villages_remain_on_map_but_do_not_take_priority_slots(catalogs):
    point = _legacy()[0]
    catalogs([
        _feature("xihuangxi:1", "西璜溪", point, potential_duplicate=True, potential_duplicate_ids=["xihuangxi:2"]),
        _feature("xihuangxi:2", "西璜溪", point, potential_duplicate=True, potential_duplicate_ids=["xihuangxi:1"]),
        *[_feature(f"eligible:{i}", f"已区分聚落{i}", point) for i in range(5)],
    ])
    payload = poi.build_poi_payload([])
    assert len(payload["villages"]) == 7
    duplicates = [v for v in payload["villages"] if v["name"] == "西璜溪"]
    assert len(duplicates) == 2
    assert all(not v["priority_eligible"] and "身份或坐标待核验" in v["priority_exclusion_reason"] for v in duplicates)
    for hazard in range(5):
        ranked = workbench.rank_villages(payload["villages"], {"level": hazard})
        assert len(ranked) == 5
        assert all(v["id"].startswith("eligible:") for v in ranked)
    assert payload["poi_metadata"]["counts"]["priority_excluded"] == 2


def test_expanded_daily_uses_one_county_forecast(authenticated_client, monkeypatch):
    from services import heat_risk_workbench_forecast as forecast
    from core.time_utils import today_local

    calls = []

    def get_forecast(location):
        calls.append(location)
        return ([{"forecast_date": (today_local() + timedelta(days=i)).isoformat(),
                  "temperature_max": 36, "temperature_min": 24} for i in range(7)], "demo", "演示数据", "演示")

    monkeypatch.setattr(forecast, "get_workbench_forecast", get_forecast)
    payload = authenticated_client.get("/heat-exposure-gis/daily.json").get_json()
    assert len(calls) == 1
    assert len(payload["poi_coverage"]) == 24
    ids = {v["id"] for v in payload["villages"]}
    assert len(ids) == len(payload["villages"])
    assert all(set(day["village_ids"]) <= ids for day in payload["priority"])
    assert all(len(day["villages"]) <= 5 for day in payload["priority"])


def _public_cooling_extension():
    inventory = json.loads(poi.RESOURCE_INVENTORY_PATH.read_text(encoding="utf-8"))
    resources = json.loads(poi.RESOURCES_PATH.read_text(encoding="utf-8"))
    preview = [row for row in inventory["records"] if row.get("public_preview") and row["id"].startswith("official-cooling-")]
    mapped = [feature for feature in resources["features"] if feature["properties"].get("public_preview") and any(str(record_id).startswith("official-cooling-") for record_id in feature["properties"].get("inventory_ids", []))]
    return inventory, resources, preview, mapped


def test_public_cooling_extension_has_ten_candidates_and_no_invented_coordinates():
    inventory, resources, preview, mapped = _public_cooling_extension()
    assert len(inventory["records"]) == 423
    assert len({row["id"] for row in inventory["records"]}) == 423
    assert len(preview) == 10 and len(mapped) == 7
    assert len({feature["id"] for feature in resources["features"]}) == len(resources["features"])
    mapped_inventory_ids = {item for feature in mapped for item in feature["properties"]["inventory_ids"]}
    assert mapped_inventory_ids <= {row["id"] for row in preview}
    missing = {row["id"] for row in preview} - mapped_inventory_ids
    assert missing == {"official-cooling-liushan-culture-center", "official-cooling-yangfeng-culture-station",
                       "official-cooling-dagang-culture-station"}
    # 全部原始名录保持无坐标；乡镇和村中心不能冒充具体场所位置。
    assert all(not any(key in row for key in ("coordinates", "longitude", "latitude", "geometry")) for row in preview)
    for row in preview:
        assert row["kind"] == row["public_role"] == "cooling_candidate"
        assert row["current_opening_status"] == "unknown"
        assert all(row.get(key) is None for key in ("has_ac", "is_accessible", "verified_at", "valid_until", "open_hours"))
        assert all(row.get(key) for key in ("source_label", "source_url", "source_date", "audience_hint",
                                          "facilities_hint", "opening_hours_hint", "verification_note"))
        if row["id"] in missing:
            assert row["coordinate_status"] == "unmapped" and not row.get("mapped_resource_ids")
            assert row["coordinate_search"]["provider"] == "高德地图"


def test_public_cooling_amap_coordinates_are_converted_and_inside_county():
    _, _, _, mapped = _public_cooling_extension()
    expected = {
        "B0KG7HA2PR": [116.201836, 29.270071], "B0J057S1T0": [116.221689, 29.281885],
        "B0J0T6UGOA": [116.189982, 29.263534], "B0KG7H9VA3": [116.190058, 29.263556],
        "B0J1UG0JK1": [116.206436, 29.269909], "B0L3FD628U": [116.205125, 29.268475],
        "B0JAJMNSE0": [116.334872, 29.457898],
    }
    assert {feature["properties"]["source_id"] for feature in mapped} == set(expected)
    for feature in mapped:
        props = feature["properties"]
        raw = expected[props["source_id"]]
        point = feature["geometry"]["coordinates"]
        assert feature["geometry"]["type"] == "Point"
        assert props["source_coordinate_system"] == "GCJ-02" and props["coordinate_system"] == "WGS84"
        assert props["source_coordinates"] == raw and point != raw
        assert point == pytest.approx(workbench.gcj02_to_wgs84(*raw), abs=5.1e-8)
        converted = workbench.wgs84_to_gcj02(*point)
        error_m = workbench.haversine_km(*raw, *converted) * 1000
        assert error_m < 0.02
        assert props["coordinate_conversion"]["mathematical_roundtrip_error_m"] == pytest.approx(error_m, abs=1e-6)
        assert "不代表" in props["coordinate_conversion"]["note"]
        match = poi._match_point(*point, poi._spatial_signature())
        assert match and match["township"] == props["township_name"]
        assert props["coordinate_evidence"]["region"] == "360428"
        assert props["coordinate_evidence"]["queried_at"].startswith("2026-09-27T")
        assert props["coordinate_source_url"] == f"https://www.amap.com/place/{props['source_id']}"
        assert props["official_source_url"] != props["coordinate_source_url"]


def test_public_cooling_history_does_not_become_verified_capacity():
    _, _, preview, mapped = _public_cooling_extension()
    data = poi.build_poi_payload(_legacy())
    counts = data["poi_metadata"]["counts"]
    assert (counts["settlements"], counts["medical"], counts["cooling_candidates"], counts["cooling_verified"]) == (502, 25, 59, 0)
    assert counts["unmapped_resources"] == 344
    assert data["poi_metadata"]["inventory_linked_count"] == 79
    assert data["poi_metadata"]["rejected_points"] == 0
    assert not data["cooling_resources"]
    runtime_ids = {row["id"] for row in data["cooling_candidates"]}
    assert {feature["id"] for feature in mapped} <= runtime_ids
    missing = {row["id"] for row in preview if row["coordinate_status"] == "unmapped"}
    assert missing <= {row["id"] for row in data["unmapped_resources"]}
    for row in data["cooling_candidates"]:
        assert row["kind"] == "cooling_candidate" and row["cooling_status"] == "candidate"
        assert row["current_opening_status"] == "unknown"
        assert all(row.get(key) is None for key in ("has_ac", "is_accessible", "verified_at", "valid_until"))


def test_cooling_colocated_xijie_records_retain_cross_references():
    _, _, _, mapped = _public_cooling_extension()
    features = {feature["id"]: feature for feature in mapped}
    library_id, station_id = "amap-B0J0T6UGOA", "amap-B0KG7H9VA3"
    for this_id, other_id in ((library_id, station_id), (station_id, library_id)):
        props = features[this_id]["properties"]
        assert props["related_ids"] == [other_id]
        assert "不能相加为两处独立容量" in props["verification_note"]
    distance = workbench.haversine_km(*features[library_id]["geometry"]["coordinates"],
                                     *features[station_id]["geometry"]["coordinates"]) * 1000
    assert 5 < distance < 15
    renmin = features["amap-B0J057S1T0"]["properties"]
    assert "183米" in renmin["verification_note"] and "不将两者当作别名" in renmin["verification_note"]
    assert "都昌县红色蒲公英驿站(人民广场站)" not in renmin["aliases"]


def test_cooling_evidence_preserves_alias_difference_and_fan_only_history():
    _, _, preview, mapped = _public_cooling_extension()
    by_id = {row["id"]: row for row in preview}
    lanhai = next(f["properties"] for f in mapped if f["id"] == "amap-B0L3FD628U")
    assert lanhai["amap_name"] == "墨韵抬光城市书房"
    assert "墨韵拾光" in lanhai["name"]
    assert any(source.get("source_date") == "2026-01-14" for source in lanhai["sources"])
    liushan = by_id["official-cooling-liushan-culture-center"]
    assert liushan["source_date"] == "2023-07-11"
    assert "风扇纳凉，不能标为空调场所" in liushan["facilities_hint"]
    for station in ("yangfeng", "dagang", "xubu"):
        row = by_id[f"official-cooling-{station}-culture-station"]
        assert row["official_status"] == "public_culture_facility_report"
        assert "不能据此认定" in row["facilities_hint"]
        assert row["source_date"] == "2026-05-19"


def test_amap_inventory_sources_keep_platform_attribution():
    data = poi.build_poi_payload(_legacy())
    amap_sources = [source for source in data["poi_metadata"]["sources"]
                    if source.get("url", "").startswith("https://www.amap.com/place/")]
    assert len(amap_sources) >= 50
    assert all("高德" in source["name"] and "官方列名" not in source["name"] for source in amap_sources)
