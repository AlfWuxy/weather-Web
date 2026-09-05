# -*- coding: utf-8 -*-
"""都昌县热暴露 GIS 页面、数据口径与隐私边界回归测试。"""

import hashlib
import json
import math
from pathlib import Path

import pytest

from services.heat_exposure_gis_service import _validated_hard_failure_count


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEOJSON_PATH = PROJECT_ROOT / "static/data/gis/duchang_heat_exposure_cells.geojson"


def _load_geojson():
    return json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))


def test_heat_exposure_gis_requires_login(client):
    response = client.get("/heat-exposure-gis", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_heat_exposure_gis_page_has_academic_contract(authenticated_client):
    response = authenticated_client.get("/heat-exposure-gis")
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert "都昌县 1 km 网格级热暴露 GIS" in html
    assert "独立复核程序通过" in html
    assert "模型化人口与 Aqua 白天晴空地表温度" in html
    assert "结果不代表个人健康风险、2 米气温、室内温度或因果效应" in html
    assert "2,593" in html
    assert "250,270" in html
    assert "SHA-256" in html
    assert "MYD11A1.061" in html
    assert "ASPECT" in html
    assert "WorldCover" in html
    assert "Copernicus DEM" in html
    assert "geoBoundaries" in html
    assert "/static/data/gis/duchang_heat_exposure_cells.geojson" in html
    assert "/static/data/gis/duchang_heat_exposure_cells.geojson?v=" in html
    assert 'download="duchang_heat_exposure_cells.geojson"' in html
    assert "/static/js/heat-exposure-gis.js" in html
    assert "/static/vendor/leaflet/dist/leaflet.css" in html
    assert "/static/vendor/leaflet/dist/leaflet.js" in html
    assert "unpkg.com" not in html
    assert "网页读取冻结上游产物，不会在线重新估计这些图层" in html
    assert "程序复核不代表外部机构认证" in html
    assert "生成方法与关键限制" in html
    assert "原始编码乘 0.02 后减 273.15" in html
    assert "科研展示 v1.2" in html
    assert 'role="group" aria-label="网格显示几何"' in html
    assert 'data-geometry-mode="rectified" aria-pressed="true"' in html
    assert 'data-geometry-mode="native" aria-pressed="false"' in html
    assert 'id="gisCellGeometryMode"' in html
    assert "下载 GeoJSON 始终保留原生四角" in html
    assert "综合风险分" not in html
    assert "自动决策" not in html


def test_heat_exposure_gis_uses_shared_metric_info_contract(authenticated_client):
    html = authenticated_client.get("/heat-exposure-gis").get_data(as_text=True)

    expected_keys = {
        "gis_native_grid",
        "gis_display_geometry",
        "gis_age65_share",
        "gis_lst_mean",
        "gis_q3_coverage",
        "gis_tree_cover",
        "gis_built_up",
        "gis_permanent_water",
        "gis_mean_elevation",
        "gis_validation",
    }
    for key in expected_keys:
        assert f'data-metric-info="{key}"' in html
    assert 'data-transparency-url="/transparency"' in html
    assert 'id="gisMobileLayerInfo"' in html
    assert 'id="gisPrimaryInfo"' in html


def test_heat_exposure_gis_can_be_disabled_without_affecting_login(app, authenticated_client):
    app.config["FEATURE_HEAT_EXPOSURE_GIS"] = False

    response = authenticated_client.get("/heat-exposure-gis")
    navigation = authenticated_client.get("/").get_data(as_text=True)

    assert response.status_code == 404
    assert 'href="/heat-exposure-gis"' not in navigation


def test_heat_exposure_geojson_counts_and_validation_status():
    collection = _load_geojson()
    metadata = collection["metadata"]
    cells = [
        feature for feature in collection["features"]
        if feature["properties"]["feature_type"] == "modis_cell"
    ]
    boundaries = [
        feature for feature in collection["features"]
        if feature["properties"]["feature_type"] == "study_boundary"
    ]

    assert collection["type"] == "FeatureCollection"
    assert len(cells) == 2593
    assert len(boundaries) == 1
    assert metadata["spatial_definition"]["positive_population_support_cells"] == 1721
    assert metadata["spatial_definition"]["zero_population_support_cells"] == 872
    assert metadata["study_period"]["calendar_dates"] == 460
    assert metadata["study_period"]["local_frozen_scenes"] == 448
    assert metadata["quality_summary"]["q3_valid_cell_days"] == 250270
    assert metadata["quality_summary"]["independent_validation"] == "pass"
    assert metadata["quality_summary"]["hard_failures"] == 0


def test_heat_exposure_geojson_selected_cell_matches_frozen_inputs():
    collection = _load_geojson()
    selected = next(
        feature for feature in collection["features"]
        if feature.get("id") == "h28v06-r0081-c0156"
    )
    properties = selected["properties"]

    assert properties["modis_row_0based"] == 81
    assert properties["modis_col_0based"] == 156
    assert properties["center_lon_wgs84"] == 116.188990665
    assert properties["center_lat_wgs84"] == 29.320833333
    assert properties["age65_share_pct"] == 15.1493
    assert properties["q3_lst_c_mean"] == 32.3715
    assert properties["q3_dates"] == 82
    assert properties["local_available_dates"] == 448
    assert properties["tree_cover_pct"] == 89.7364
    assert properties["built_up_pct"] == 0.0
    assert properties["permanent_water_pct"] == 6.4984
    assert properties["mean_elevation_m"] == 66.5247
    assert selected["geometry"]["type"] == "Polygon"
    assert len(selected["geometry"]["coordinates"][0]) == 5
    assert selected["geometry"]["coordinates"][0] == [
        [116.188957768, 29.325],
        [116.198515925, 29.325],
        [116.189024174, 29.316666667],
        [116.179466798, 29.316666667],
        [116.188957768, 29.325],
    ]


def test_heat_exposure_geojson_preserves_all_native_feature_geometries():
    collection = _load_geojson()
    geometry_payload = json.dumps(
        [
            {"id": feature.get("id"), "geometry": feature["geometry"]}
            for feature in collection["features"]
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert hashlib.sha256(geometry_payload).hexdigest() == "25edd26ff9c825496e59dbde6116521a6cc0795408e2dcf4807e159fc7562eef"


def test_heat_exposure_geojson_hides_unresolved_counts_and_local_paths():
    collection = _load_geojson()
    serialized = json.dumps(collection, ensure_ascii=False)
    cell_keys = {
        key
        for feature in collection["features"]
        if feature["properties"]["feature_type"] == "modis_cell"
        for key in feature["properties"]
    }

    assert "/Users/" not in serialized
    assert "patient_name" not in serialized
    assert "household" not in serialized.lower()
    assert "duchang_total_population_2020_estimate" not in cell_keys
    assert "duchang_population_65plus_2020_estimate" not in cell_keys
    assert "pop_ge10" not in cell_keys
    assert "positive_population_support" in cell_keys
    assert "q3_lst_c_median" not in cell_keys
    assert "wetland_pct" not in cell_keys
    assert "native_land_lt50" not in cell_keys

    zero_support = next(
        feature for feature in collection["features"]
        if feature["properties"].get("positive_population_support") is False
    )
    assert zero_support["properties"]["age65_share_pct"] is None


def test_heat_exposure_geojson_has_traceable_layer_metadata():
    metadata = _load_geojson()["metadata"]
    assert len(metadata["input_fingerprints"]) == 4
    assert all(len(item["sha256"]) == 64 for item in metadata["input_fingerprints"])
    assert set(metadata["layers"]) == {
        "age65_share_pct",
        "q3_lst_c_mean",
        "q3_coverage_pct",
        "tree_cover_pct",
        "built_up_pct",
        "permanent_water_pct",
        "mean_elevation_m",
    }
    assert metadata["layers"]["age65_share_pct"]["valid_cells"] == 1721
    assert metadata["layers"]["age65_share_pct"]["missing_cells"] == 872
    assert all(len(layer["breaks"]) == 7 for layer in metadata["layers"].values())
    assert all(len(layer["palette"]) == 6 for layer in metadata["layers"].values())
    assert metadata["schema_version"] == "1.2.0"
    assert all(layer["metric_key"].startswith("gis_") for layer in metadata["layers"].values())
    assert all(layer["details_anchor"].startswith("gis-") for layer in metadata["layers"].values())


def test_heat_exposure_geojson_declares_dual_display_geometry():
    metadata = _load_geojson()["metadata"]
    spatial = metadata["spatial_definition"]
    display = spatial["display_geometry"]

    assert spatial["native_sphere_radius_m"] == 6371007.181
    assert spatial["native_nominal_resolution_m"] == 926.6254331391661
    assert display["default_mode"] == "rectified"
    assert display["available_modes"] == ["rectified", "native"]
    assert display["native_geometry_preserved"] is True
    assert display["native_geometry_field"] == "feature.geometry"
    assert display["rectified_geometry_analysis_use"] is False
    assert display["rectified_formula"] == (
        "delta_lat_deg=(p/(2R))*180/pi; "
        "delta_lon_deg=delta_lat_deg/cos(latitude_center_rad)"
    )


def test_rectified_display_formula_preserves_selected_cell_center_and_scale():
    collection = _load_geojson()
    spatial = collection["metadata"]["spatial_definition"]
    selected = next(feature for feature in collection["features"] if feature.get("id") == "h28v06-r0081-c0156")
    properties = selected["properties"]
    radius = spatial["native_sphere_radius_m"]
    resolution = spatial["native_nominal_resolution_m"]
    center_lon = properties["center_lon_wgs84"]
    center_lat = properties["center_lat_wgs84"]

    half_lat = resolution / (2 * radius) * 180 / math.pi
    half_lon = half_lat / math.cos(math.radians(center_lat))
    west, east = center_lon - half_lon, center_lon + half_lon
    south, north = center_lat - half_lat, center_lat + half_lat

    assert math.isclose((west + east) / 2, center_lon, abs_tol=1e-12)
    assert math.isclose((south + north) / 2, center_lat, abs_tol=1e-12)
    assert math.isclose(north - south, resolution / radius * 180 / math.pi, abs_tol=1e-12)
    assert math.isclose((east - west) * math.cos(math.radians(center_lat)), north - south, abs_tol=1e-12)
    assert math.isclose(west, 116.184211782, abs_tol=1e-9)
    assert math.isclose(east, 116.193769548, abs_tol=1e-9)


def test_rectified_display_max_corner_shift_is_recomputed_for_all_cells():
    collection = _load_geojson()
    spatial = collection["metadata"]["spatial_definition"]
    audit = spatial["display_geometry"]["rectified_corner_shift_audit"]
    radius = spatial["native_sphere_radius_m"]
    resolution = spatial["native_nominal_resolution_m"]
    corner_labels = ("NW", "NE", "SE", "SW")
    maximum = (-1.0, None, None)
    audited_cells = 0

    for feature in collection["features"]:
        if feature["properties"].get("feature_type") != "modis_cell":
            continue
        audited_cells += 1
        properties = feature["properties"]
        center_lon = properties["center_lon_wgs84"]
        center_lat = properties["center_lat_wgs84"]
        half_lat = resolution / (2 * radius) * 180 / math.pi
        half_lon = half_lat / math.cos(math.radians(center_lat))
        rectified_corners = (
            (center_lon - half_lon, center_lat + half_lat),
            (center_lon + half_lon, center_lat + half_lat),
            (center_lon + half_lon, center_lat - half_lat),
            (center_lon - half_lon, center_lat - half_lat),
        )
        native_corners = feature["geometry"]["coordinates"][0][:4]

        for label, native, rectified in zip(corner_labels, native_corners, rectified_corners):
            lon_a, lat_a = map(math.radians, native)
            lon_b, lat_b = map(math.radians, rectified)
            delta_lon = lon_b - lon_a
            delta_lat = lat_b - lat_a
            haversine = (
                math.sin(delta_lat / 2) ** 2
                + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
            )
            distance_m = 2 * radius * math.asin(min(1, math.sqrt(haversine)))
            if distance_m > maximum[0]:
                maximum = (distance_m, feature["id"], label)

    assert audited_cells == 2593
    assert math.isclose(maximum[0], 465.805852, abs_tol=1e-6)
    assert maximum[1:] == ("h28v06-r0044-c0152", "NE")
    assert audit == {
        "max_corner_shift_m": 465.805852,
        "max_corner_shift_cell_id": "h28v06-r0044-c0152",
        "max_corner_shift_corner": "NE",
        "distance_method": "MODIS 球体上的同名角大圆表面距离",
        "audited_cells": 2593,
    }


@pytest.mark.parametrize(
    "validation",
    [
        {"status": "pass", "validation_pass": True},
        {"status": "pass", "validation_pass": True, "hard_failure_count": 1},
        {"status": "pass", "validation_pass": True, "hard_failure_count": "0"},
        {"status": "fail", "validation_pass": True, "hard_failure_count": 0},
        {"status": "pass", "validation_pass": False, "hard_failure_count": 0},
    ],
)
def test_heat_exposure_publish_gate_fails_closed(validation):
    with pytest.raises(ValueError, match="停止生成 GIS 数据"):
        _validated_hard_failure_count(validation)


def test_heat_exposure_publish_gate_accepts_zero_hard_failures():
    validation = {"status": "pass", "validation_pass": True, "hard_failure_count": 0}
    assert _validated_hard_failure_count(validation) == 0


def test_logged_in_navigation_places_heat_exposure_gis_inside_more(authenticated_client):
    html = authenticated_client.get("/heat-exposure-gis").get_data(as_text=True)
    desktop_primary = html.split('class="app-desktop-nav', 1)[1].split('class="app-more-menu"', 1)[0]
    mega_menu = html.split('id="appMegaMenu"', 1)[1].split('data-bs-toggle="offcanvas"', 1)[0]
    drawer = html.split('id="appNavDrawer"', 1)[1]

    assert 'href="/heat-exposure-gis"' not in desktop_primary
    assert 'href="/heat-exposure-gis"' in mega_menu
    assert 'href="/heat-exposure-gis"' in drawer
    assert html.count('href="/heat-exposure-gis"') == 2
    assert 'data-nav-key="heat-exposure-gis"' in html
    assert 'app-more-trigger active' in html
    assert 'aria-current="page"' in html


def test_heat_exposure_gis_static_runtime_is_local(client):
    assert client.get('/static/vendor/leaflet/dist/leaflet.css').status_code == 200
    assert client.get('/static/vendor/leaflet/dist/leaflet.js').status_code == 200
    assert client.get('/static/vendor/leaflet/dist/images/layers.png').status_code == 200


def test_heat_exposure_gis_script_keeps_controls_accessible_and_fails_closed():
    script = (PROJECT_ROOT / 'static/js/heat-exposure-gis.js').read_text(encoding='utf-8')

    assert "item.setAttribute('role', 'listitem')" in script
    assert "button.setAttribute('role', 'listitem')" not in script
    assert "createMetricInfoButton" in script
    assert "closeMetricPopovers" in script
    assert "closeMetricPopovers(true)" in script
    assert "instance.dispose()" in script
    assert "instance.hide()" in script
    assert "window.initMetricInfo" in script
    assert "duplicatePointAlreadyShown" in script
    assert "geometryModes = new Set(['rectified', 'native'])" in script
    assert "featureForDisplay(selected)" in script
    assert "features: cellsForDisplay()" in script
    assert "app.dataset.activeGeometry = state.geometryMode" in script
    assert "url.searchParams.set('geometry', state.geometryMode)" in script
    assert "state.map.invalidateSize({pan: false})" in script
    assert "button.disabled = true" in script
    assert "ui.tableToggle.disabled = true" in script
    assert "GeoJSON 网格数与元数据不一致" in script
