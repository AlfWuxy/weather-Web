# -*- coding: utf-8 -*-
"""地点核验与纳凉设施状态分别保存，导入坐标不改变既有身份或行政边界。"""

import json

import pytest

from services import heat_risk_poi_service as poi
from services import heat_risk_workbench_service as workbench


EXPECTED_GCJ02 = {
    "B0JKZ7QJFT": [
        116.53776,
        29.380525
    ],
    "B0J05RX596": [
        116.261786,
        29.275105
    ],
    "B0KGRZGICR": [
        116.223279,
        29.282777
    ],
    "B0JKMM40AL": [
        116.401876,
        29.477282
    ],
    "B0JR3LFQTI": [
        116.210075,
        29.275325
    ],
    "B0J3R71OYY": [
        116.196444,
        29.268134
    ],
    "B0JKG7EDM2": [
        116.194614,
        29.268543
    ],
    "B0J1J7YSDT": [
        116.19814,
        29.26432
    ],
    "B0KG7H9V9G": [
        116.200175,
        29.270275
    ],
    "B0J1JUR9L3": [
        116.209815,
        29.275565
    ],
    "B0JK9UDWR1": [
        116.208889,
        29.276595
    ],
    "B0JK1CJZ7O": [
        116.193955,
        29.276255
    ],
    "B0J3R71OZC": [
        116.201522,
        29.271245
    ],
    "B0KGRZFVJR": [
        116.197263,
        29.279578
    ],
    "B0KGRZCRWB": [
        116.219472,
        29.280458
    ],
    "B0KGRZCHCP": [
        116.188776,
        29.265089
    ],
    "B0J2BRSA93": [
        116.205541,
        29.272613
    ],
    "B0J1JCM7T9": [
        116.196747,
        29.268326
    ],
    "B0J1JCYY0R": [
        116.199525,
        29.271075
    ],
    "B0J05RXXXW": [
        116.438345,
        29.42166
    ],
    "B0HAUZPGP9": [
        116.406636,
        29.477729
    ],
    "B0JD45KUB8": [
        116.519499,
        29.400977
    ],
    "B03180SMPJ": [
        116.540303,
        29.378939
    ],
    "B0JDY51J1S": [
        116.169177,
        29.293802
    ],
    "B0MDN6DWSM": [
        116.215581,
        29.295329
    ],
    "B0FFK8C8TF": [
        116.209286,
        29.280046
    ],
    "B0JDZ5JAVZ": [
        116.292752,
        29.242788
    ],
    "B0FFHSTED9": [
        116.439555,
        29.38575
    ],
    "B031802CXQ": [
        116.4224,
        29.366276
    ],
    "B0HDPMJLVX": [
        116.14652,
        29.44752
    ],
    "B0JDYHYVCA": [
        116.131204,
        29.398015
    ],
    "B0KG5SDIJN": [
        116.136458,
        29.397874
    ],
    "B0JDBUGOLH": [
        116.288573,
        29.271521
    ],
    "B0JDBH8480": [
        116.514198,
        29.5229
    ],
    "B0JDBUHBHB": [
        116.526299,
        29.475859
    ],
    "B0JDKZ6H1X": [
        116.286015,
        29.472182
    ],
    "B0JDGZYYPI": [
        116.294921,
        29.447975
    ],
    "B0JDYSZ24F": [
        116.280305,
        29.428002
    ],
    "B0JDB7JDDC": [
        116.481229,
        29.329733
    ],
    "B0JDBZGRSC": [
        116.532251,
        29.278763
    ],
    "B0JDMM1ZSN": [
        116.545432,
        29.222639
    ],
    "B0K16DQGTT": [
        116.427549,
        29.246222
    ],
    "B0JDBH9M6L": [
        116.443707,
        29.200544
    ],
    "B0JDYSZ28E": [
        116.453839,
        29.205248
    ],
    "B0JDBSJ4D6": [
        116.219471,
        29.280447
    ],
    "B03180SKW0": [
        116.195532,
        29.267872
    ],
    "B0JABMV4AM": [
        116.197371,
        29.279447
    ],
    "B0LGLH6T95": [
        116.219975,
        29.271875
    ],
    "B0K1GUQA52": [
        116.208966,
        29.276314
    ],
    "B0JDMR4DKE": [
        116.359229,
        29.314843
    ]
}
REUSED_LEGACY_IDS = {"B0HAUZPGP9", "B0KG5SDIJN", "B03180SKW0", "B0JABMV4AM", "B0K1GUQA52"}


def _data():
    inventory = json.loads(poi.RESOURCE_INVENTORY_PATH.read_text(encoding="utf-8"))
    collection = json.loads(poi.RESOURCES_PATH.read_text(encoding="utf-8"))
    records = [row for row in inventory["records"] if row.get("location_verification_status") == "verified"]
    features = [f for f in collection["features"] if f["properties"].get("location_verification_status") == "verified"]
    return inventory, collection, records, features


def test_fifty_location_records_reuse_old_ids_without_duplicate_pois():
    inventory, collection, records, features = _data()
    assert len(records) == len(features) == 50
    assert len(inventory["records"]) == len({row["id"] for row in inventory["records"]}) == 423
    assert len(collection["features"]) == len({f["id"] for f in collection["features"]}) == 84
    assert {f["properties"]["source_id"] for f in features} == set(EXPECTED_GCJ02)
    record_ids = {row["id"] for row in records}
    assert REUSED_LEGACY_IDS <= record_ids
    assert not {"amap-cooling-" + item for item in REUSED_LEGACY_IDS} & record_ids
    assert sum(row["source_category"] == "驿站" for row in records) == 20
    assert sum(row["source_category"] == "文化或社区设施" for row in records) == 30
    for feature in features:
        props = feature["properties"]
        expected_id = props["source_id"] if props["source_id"] in REUSED_LEGACY_IDS else "amap-cooling-" + props["source_id"]
        assert props["inventory_ids"] == [expected_id]
        row = next(row for row in records if row["id"] == expected_id)
        assert row["mapped_resource_ids"] == [feature["id"]]
        assert row["public_preview"] is True and row["public_role"] == "cooling_candidate"
        assert "geometry" not in row and "longitude" not in row and "latitude" not in row


def test_location_confirmation_never_fills_unknown_cooling_facilities_or_hours():
    _, _, records, features = _data()
    for row in [*records, *(feature["properties"] for feature in features)]:
        assert row["location_verification_status"] == "verified"
        assert row["location_verification_method"] == "amap_poi_and_user_confirmation"
        assert row["location_verified_at"] == "2026-09-27"
        assert row["public_access_confirmation"] == "user_confirmed"
        assert "项目负责人确认" in row["location_verification_note"] and "未现场核验" in row["location_verification_note"]
        assert row["kind"] == "cooling_candidate" and row["is_active"] is False
        assert row["opening_hours_hint"] == ""
        assert all(row[key] is None for key in ("has_ac", "is_accessible", "open_hours", "verified_at", "valid_until"))
        assert row["source_url"].startswith("https://www.amap.com/place/")
        assert row["coordinate_source_url"] == row["source_url"]
        assert row["source_date"] == "2026-09-27"
        assert "official_source_url" not in row


def test_fifty_coordinate_sources_remain_gcj02_and_geometry_is_wgs84():
    _, _, _, features = _data()
    for feature in features:
        props = feature["properties"]
        expected = EXPECTED_GCJ02[props["source_id"]]
        coords = feature["geometry"]["coordinates"]
        assert props["source_coordinates"] == expected and coords != expected
        assert props["source_coordinate_system"] == "GCJ-02" and props["coordinate_system"] == "WGS84"
        assert coords == pytest.approx(workbench.gcj02_to_wgs84(*expected), abs=5.1e-9)
        assert props["coordinate_evidence"]["poi_id"] == props["source_id"]
        assert props["coordinate_evidence"]["adcode"] == "360428"
        assert props["coordinate_evidence"]["queried_at"].startswith("2026-09-27T")
        assert props["coordinate_precision"] == "approximate"
        assert "不是实地测量精度" in props["coordinate_conversion"]["note"]
        assert poi._match_point(*coords, poi._spatial_signature()) is not None


def test_cailing_county_point_survives_township_geometry_gap_without_moving():
    payload = poi.build_poi_payload([])
    item = next(point for point in payload["cooling_candidates"] if point["id"] == "amap-B0HAUZPGP9")
    assert [item["lon_wgs84"], item["lat_wgs84"]] == [116.40146866, 29.48015564]
    assert item["source_township"] == "蔡岭镇" and item["township"] is None
    assert item["township_geometry_status"] == "unmatched"
    assert "乡界缝隙" in item["township_warning"]
    assert item["location_verification_status"] == "verified"
    assert payload["poi_metadata"]["rejected_points"] == 0
    assert len(payload["cooling_candidates"]) == 59
    assert sum(row["cooling_candidate_count"] for row in payload["poi_coverage"]) == 58
    assert not payload["cooling_resources"]
    assert payload["poi_metadata"]["inventory_linked_count"] == 79
    assert len(payload["unmapped_resources"]) == 344


def test_nearby_records_keep_relationships_without_claiming_independent_capacity():
    _, collection, _, features = _data()
    by_id = {feature["id"]: feature["properties"] for feature in collection["features"]}
    pairs = [
        ("B0JABMV4AM", "B0KGRZFVJR"), ("B0K1GUQA52", "B0JK9UDWR1"),
        ("B03180SKW0", "B0J3R71OYY"), ("B0JDBSJ4D6", "B0KGRZCRWB"),
        ("B0J3R71OYY", "B0J1JCM7T9"), ("B0JR3LFQTI", "B0J1JUR9L3"),
    ]
    for a, b in pairs:
        for current, other in ((a, b), (b, a)):
            row = by_id["amap-" + current]
            assert "amap-" + other in row["related_ids"]
            assert row["independent_facility_confirmed"] is False
            assert any("不能相加为独立纳凉容量" in note for note in row["notes"])
    for feature in features:
        assert set(feature["properties"]["related_ids"]) <= set(by_id)
    assert "amap-B0J057S1T0" in by_id["amap-B0KGRZGICR"]["related_ids"]
    assert "amap-B0KG7HA2PR" in by_id["amap-B0J3R71OZC"]["related_ids"]
