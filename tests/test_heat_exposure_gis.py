# -*- coding: utf-8 -*-
"""都昌县热暴露 GIS 页面、数据口径与隐私边界回归测试。"""

import json
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
    assert "科研展示 v1.1" in html
    assert "综合风险分" not in html
    assert "自动决策" not in html


def test_heat_exposure_gis_uses_shared_metric_info_contract(authenticated_client):
    html = authenticated_client.get("/heat-exposure-gis").get_data(as_text=True)

    expected_keys = {
        "gis_native_grid",
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
    assert metadata["schema_version"] == "1.1.0"
    assert all(layer["metric_key"].startswith("gis_") for layer in metadata["layers"].values())
    assert all(layer["details_anchor"].startswith("gis-") for layer in metadata["layers"].values())


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
    assert "ui.tableToggle.disabled = true" in script
    assert "GeoJSON 网格数与元数据不一致" in script
