# -*- coding: utf-8 -*-
"""热风险医生工作台：回滚开关、逐日分级、风险分数据与坐标换算回归测试。"""

import json
import math
from pathlib import Path

import pytest

from services.heat_risk_workbench_service import (
    build_workbench_data,
    classify_daily_hazard,
    combine_daily_level,
    gcj02_to_wgs84,
    gis_ui_mode,
    haversine_km,
    rank_villages,
    wgs84_to_gcj02,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_PATH = PROJECT_ROOT / "static/data/gis/duchang_heat_risk_workbench.json"
CELLS_PATH = PROJECT_ROOT / "static/data/gis/duchang_heat_exposure_cells.geojson"
TOWNSHIPS_PATH = PROJECT_ROOT / "data/gis/duchang_townships_osm.geojson"
CLIMATE_PATH = PROJECT_ROOT / "data/raw/逐日数据.csv"


def _workbench():
    return json.loads(WORKBENCH_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 页面与回滚开关
# ---------------------------------------------------------------------------

def test_workbench_is_default_view(authenticated_client):
    response = authenticated_client.get("/heat-exposure-gis")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'id="heatRiskWorkbench"' in html
    assert "风险暂不可判定" in html
    assert "巡访优先清单" in html
    assert "/static/data/gis/duchang_heat_risk_workbench.json?v=" in html
    assert "/static/data/gis/duchang_heat_exposure_cells.geojson?v=" in html
    assert 'data-daily-url="/heat-exposure-gis/daily.json"' in html
    assert 'data-tianditu-key=""' in html
    assert "/heat-exposure-gis?ui=legacy" in html
    assert "/static/js/heat-risk-workbench.js?v=" in html
    assert "/static/css/heat-risk-workbench.css?v=" in html
    assert "/static/vendor/leaflet/dist/leaflet.js" in html
    assert "unpkg.com" not in html
    for key in ("gis_risk_score", "gis_daily_level", "gis_hotspot", "gis_rank_stability", "gis_bivariate", "gis_facility_access"):
        assert f'data-metric-info="{key}"' in html


def test_legacy_view_available_by_query(authenticated_client):
    html = authenticated_client.get("/heat-exposure-gis?ui=legacy").get_data(as_text=True)
    assert 'id="heatExposureGisApp"' in html
    assert 'id="heatRiskWorkbench"' not in html


def test_legacy_view_available_by_config(app, authenticated_client):
    app.config["HEAT_EXPOSURE_GIS_UI"] = "legacy"
    html = authenticated_client.get("/heat-exposure-gis").get_data(as_text=True)
    assert 'id="heatExposureGisApp"' in html

    html = authenticated_client.get("/heat-exposure-gis?ui=workbench").get_data(as_text=True)
    assert 'id="heatRiskWorkbench"' in html


@pytest.mark.parametrize(
    ("args", "config", "expected"),
    [
        ({}, {}, "workbench"),
        ({}, {"HEAT_EXPOSURE_GIS_UI": "legacy"}, "legacy"),
        ({}, {"HEAT_EXPOSURE_GIS_UI": "LEGACY"}, "legacy"),
        ({}, {"HEAT_EXPOSURE_GIS_UI": "unknown"}, "workbench"),
        ({"ui": "legacy"}, {}, "legacy"),
        ({"ui": "workbench"}, {"HEAT_EXPOSURE_GIS_UI": "legacy"}, "workbench"),
        ({"ui": "bogus"}, {"HEAT_EXPOSURE_GIS_UI": "legacy"}, "legacy"),
    ],
)
def test_gis_ui_mode_switch(args, config, expected):
    assert gis_ui_mode(args, config) == expected


def test_tianditu_key_is_passed_to_page(app, authenticated_client):
    app.config["TIANDITU_TK"] = "browser-key-123"
    html = authenticated_client.get("/heat-exposure-gis").get_data(as_text=True)
    assert 'data-tianditu-key="browser-key-123"' in html


def test_daily_api_requires_login(client):
    response = client.get("/heat-exposure-gis/daily.json", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_daily_api_respects_feature_flag(app, authenticated_client):
    app.config["FEATURE_HEAT_EXPOSURE_GIS"] = False
    assert authenticated_client.get("/heat-exposure-gis/daily.json").status_code == 404


def test_daily_api_payload_in_demo_mode(authenticated_client):
    response = authenticated_client.get("/heat-exposure-gis/daily.json")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["forecast_source"] == "演示数据"
    assert payload["forecast_status"] == "demo"
    assert payload["episode_thresholds"] == {"tmax3_c": 34.2, "tmin3_c": 26.3}
    assert len(payload["days"]) == 7
    # 演示天气：最高 39/38/37 °C、最低 29/28/29 °C → 3 日均温双触发，基础 3 级上调为 4 级。
    assert payload["days"][0]["base_level"] == 3
    assert payload["days"][0]["level"] == 4
    assert payload["days"][0]["episode"] is True
    assert len(payload["villages"]) >= 16
    legacy = [v for v in payload["villages"] if v["id"].startswith("legacy-village:")]
    assert len(legacy) == 16 and all(v["cell_id"] for v in legacy)
    assert len(payload["poi_coverage"]) == 24
    assert len(payload["priority"]) == 7
    assert len(payload["priority"][0]["villages"]) == 5
    assert set(payload["action_cards"]) == {"0", "1", "2", "3", "4"}


# ---------------------------------------------------------------------------
# 逐日分级与矩阵
# ---------------------------------------------------------------------------

def _days(*pairs):
    return [{"forecast_date": f"2026-07-{i + 1:02d}", "temperature_max": hi, "temperature_min": lo} for i, (hi, lo) in enumerate(pairs)]


@pytest.mark.parametrize(
    ("tmax", "expected"),
    [(None, None), (32.9, 0), (33.0, 1), (34.9, 1), (35.0, 2), (36.9, 2), (37.0, 3), (39.9, 3), (40.0, 4)],
)
def test_base_hazard_thresholds(tmax, expected):
    day = classify_daily_hazard([{"temperature_max": tmax, "temperature_min": 20}], 34.2, 26.3)[0]
    assert day["base_level"] == expected
    assert day["level"] == expected


def test_episode_requires_both_three_day_means():
    hot_nights = classify_daily_hazard(_days((35, 27), (35, 27), (35, 27)), 34.2, 26.3)
    assert [d["level"] for d in hot_nights] == [3, 3, 3]
    assert all(d["episode"] and d["escalated"] for d in hot_nights)
    assert hot_nights[0]["tmax3"] == 35.0 and hot_nights[0]["tmin3"] == 27.0
    assert "持续高温：07-01 至 07-03" in hot_nights[0]["reasons"][-1]
    # 白天够热但夜间能降温：不触发。
    cool_nights = classify_daily_hazard(_days((36, 25), (36, 25), (36, 25)), 34.2, 26.3)
    assert [d["level"] for d in cool_nights] == [2, 2, 2]
    assert not any(d["episode"] for d in cool_nights)
    # 夜间闷热但白天不够热：不触发。
    mild_days = classify_daily_hazard(_days((33, 28), (34, 28), (34, 28)), 34.2, 26.3)
    assert not any(d["episode"] for d in mild_days)


def test_episode_uses_mean_not_every_day():
    # 单日 32 °C 不达标，但 3 日平均 34.3 °C 达阈值；32 °C 当天基础 0 级不上调。
    days = classify_daily_hazard(_days((36, 27), (35, 27), (32, 27)), 34.2, 26.3)
    assert all(d["episode"] for d in days)
    assert [d["level"] for d in days] == [3, 3, 0]


def test_episode_window_uses_last_three_days_at_forecast_end():
    days = classify_daily_hazard(_days((30, 22), (30, 22), (36, 27), (36, 27), (36, 27)), 34.2, 26.3)
    assert [d["episode"] for d in days] == [False, False, True, True, True]
    assert days[4]["tmax3"] == 36.0
    assert classify_daily_hazard(_days((36, 27), (36, 27)), 34.2, 26.3)[0]["episode"] is False


def test_invalid_day_in_window_blocks_episode():
    days = classify_daily_hazard(_days((36, 27), (None, 27), (36, 27), (36, 27), (36, 27)), 34.2, 26.3)
    assert days[0]["episode"] is False and days[0]["tmax3"] is None
    assert days[1]["level"] is None
    assert days[2]["episode"] is True


def test_escalation_caps_at_level_four():
    days = classify_daily_hazard(_days((41, 30), (41, 30), (41, 30)), 34.2, 26.3)
    assert [d["level"] for d in days] == [4, 4, 4]


@pytest.mark.parametrize(
    ("hazard", "static", "expected"),
    [
        (None, 4, None), (0, 4, 0), (0, None, 0),
        (1, 0, 1), (1, 2, 1), (1, 3, 2),
        (2, 1, 1), (2, 2, 2), (2, 4, 3),
        (3, 0, 2), (3, 3, 4),
        (4, 0, 3), (4, 4, 4), (4, None, 4),
    ],
)
def test_daily_matrix(hazard, static, expected):
    assert combine_daily_level(hazard, static) == expected


def test_js_matrix_mirrors_python_rule():
    script = (PROJECT_ROOT / "static/js/heat-risk-workbench.js").read_text(encoding="utf-8")
    assert "if (hazard === 0) return 0;" in script
    assert "if (staticLevel <= 1) adjust = -1;" in script
    assert "else if (staticLevel >= 3) adjust = 1;" in script
    assert "return Math.max(1, Math.min(4, hazard + adjust));" in script


def test_rank_villages_orders_by_daily_then_static():
    villages = [
        {"name": "甲", "static_level": 1, "static_score": 30, "population": 100, "elderly_ratio": 0.5},
        {"name": "乙", "static_level": 3, "static_score": 65, "population": 50, "elderly_ratio": 0.4},
        {"name": "丙", "static_level": 3, "static_score": 65, "population": 200, "elderly_ratio": 0.4},
    ]
    ranked = rank_villages(villages, {"level": 2, "reasons": []}, limit=0)
    assert [v["name"] for v in ranked] == ["丙", "乙", "甲"]
    assert [v["daily_level"] for v in ranked] == [3, 3, 1]
    assert ranked[0]["elderly_estimate"] == 80


# ---------------------------------------------------------------------------
# 坐标换算
# ---------------------------------------------------------------------------

def test_gcj_round_trip_is_sub_meter():
    for lon, lat in [(116.2, 29.33), (116.55, 29.2), (116.05, 29.6)]:
        g_lon, g_lat = wgs84_to_gcj02(lon, lat)
        offset_m = haversine_km(lon, lat, g_lon, g_lat) * 1000
        assert 100 < offset_m < 800
        w_lon, w_lat = gcj02_to_wgs84(g_lon, g_lat)
        assert haversine_km(lon, lat, w_lon, w_lat) * 1000 < 0.5


def test_js_gcj_formula_matches_python_constants():
    script = (PROJECT_ROOT / "static/js/heat-risk-workbench.js").read_text(encoding="utf-8")
    assert "const GCJ_A = 6378245.0;" in script
    assert "const GCJ_EE = 0.00669342162296594323;" in script
    assert "320.0 * Math.sin(y * PI / 30.0)" in script
    assert "300.0 * Math.sin(x / 30.0 * PI)" in script


# ---------------------------------------------------------------------------
# 风险分数据
# ---------------------------------------------------------------------------

def test_workbench_cells_align_with_published_geojson():
    workbench = _workbench()
    geojson = json.loads(CELLS_PATH.read_text(encoding="utf-8"))
    cell_ids = [f["properties"]["cell_id"] for f in geojson["features"] if f["properties"]["feature_type"] == "modis_cell"]
    assert workbench["cells"]["cell_id"] == cell_ids
    assert all(len(values) == len(cell_ids) for values in workbench["cells"].values())


def test_workbench_counts_and_masks():
    workbench = _workbench()
    meta, cells = workbench["metadata"], workbench["cells"]
    counts = meta["counts"]

    assert meta["schema_version"] == "2.1.0"
    assert counts["cells"] == 2593
    assert counts["water_masked_cells"] == 626
    assert counts["land_cells"] == 1967
    assert counts["scored_cells"] == sum(cells["scored"]) == 1606
    assert sum(counts["level_counts"]) == counts["scored_cells"]
    for i in range(counts["cells"]):
        if cells["scored"][i]:
            assert cells["land"][i] == 1
            assert 0 <= cells["score"][i] <= 100
        else:
            assert cells["score"][i] is None and cells["level"][i] is None


def test_score_is_geometric_mean_of_components():
    cells = _workbench()["cells"]
    checked = 0
    for i, scored in enumerate(cells["scored"]):
        if not scored:
            continue
        expected = 100 * (cells["hazard_pct"][i] / 100 * cells["exposure_pct"][i] / 100 * cells["vulnerability_pct"][i] / 100) ** (1 / 3)
        # 分量按 0.01 个百分点发布，重算误差应小于 0.2 分。
        assert math.isclose(cells["score"][i], expected, abs_tol=0.2)
        level = sum(cells["score"][i] >= b for b in (20, 40, 60, 80))
        assert cells["level"][i] == level
        checked += 1
    assert checked == 1606


def test_stability_interval_brackets_rank():
    cells = _workbench()["cells"]
    for i, scored in enumerate(cells["scored"]):
        if scored:
            assert cells["top_pct_p05"][i] <= cells["top_pct_p95"][i]
            assert 0 < cells["top_pct"][i] <= 100


def test_hotspots_have_significant_z():
    workbench = _workbench()
    cells = workbench["cells"]
    hot = [cells["gi_z"][i] for i, b in enumerate(cells["gi_bin"]) if b > 0]
    cold = [cells["gi_z"][i] for i, b in enumerate(cells["gi_bin"]) if b < 0]
    assert len(hot) == workbench["metadata"]["counts"]["hotspot_cells"] > 0
    assert min(hot) > 1.96
    assert max(cold) < -1.96


def test_townships_and_facilities_cover_county():
    workbench = _workbench()
    townships = workbench["townships"]["features"]
    assert len(townships) == 24
    assert {t["properties"]["name_zh"] for t in townships} >= {"都昌镇", "北山乡", "周溪镇", "蔡岭镇"}
    assert sum(t["properties"]["cells"] for t in townships) == 2593
    facilities = workbench["facilities"]
    assert len(facilities) == 24
    assert any(f["precision"] == "exact" for f in facilities)
    assert all(116.0 < f["lon"] < 116.7 and 29.0 < f["lat"] < 29.7 for f in facilities)


def test_daily_threshold_comes_from_local_climate():
    daily = _workbench()["metadata"]["daily"]
    assert daily["episode_tmax3_c"] == 34.2
    assert daily["episode_tmin3_c"] == 26.3
    assert daily["episode_windows_in_sample"] == 27
    assert daily["climate_sample"]["days"] == 1036


def test_committed_workbench_matches_builder(tmp_path):
    rebuilt = build_workbench_data(CELLS_PATH, TOWNSHIPS_PATH, CLIMATE_PATH, tmp_path / "wb.json")
    committed = _workbench()
    assert rebuilt["cells"] == committed["cells"]
    assert rebuilt["facilities"] == committed["facilities"]
    assert rebuilt["townships"] == committed["townships"]
    for key in ("counts", "score_method", "bivariate", "hotspot", "stability", "daily", "input_fingerprints"):
        assert rebuilt["metadata"][key] == committed["metadata"][key]


def test_village_points_convert_site_coordinates(app):
    from services.heat_risk_workbench_service import village_points

    with app.app_context():
        villages = village_points(app.config["COMMUNITY_COORDS_GCJ"])
    assert len(villages) == 16
    by_name = {v["name"]: v for v in villages}
    assert by_name["岭背徐村"]["township"] == "北山乡"
    assert by_name["庙北吴村"]["township"] == "都昌镇"
    gcj = app.config["COMMUNITY_COORDS_GCJ"]["岭背徐村"]
    shift_m = haversine_km(gcj[0], gcj[1], by_name["岭背徐村"]["lon_wgs84"], by_name["岭背徐村"]["lat_wgs84"]) * 1000
    assert 300 < shift_m < 700


def test_transparency_lists_new_metrics(client):
    body = client.get("/transparency").get_data(as_text=True)
    for anchor in ("gis-risk-score", "gis-daily-level", "gis-hotspot", "gis-rank-stability"):
        assert f'id="{anchor}"' in body


def test_map_labels_escape_admin_entered_text():
    script = (PROJECT_ROOT / "static/js/heat-risk-workbench.js").read_text(encoding="utf-8")
    assert "function esc(value)" in script
    assert "esc(point.name)" in script
    assert "esc(entry.point.name)" in script
    assert "esc(referenceName(point))" in script
    assert "${point.name}<" not in script
    assert "${village.name}<" not in script


def test_tied_scores_share_rank():
    # 并列分值必须得到相同的“前 x%”，排名不能依赖排序算法对并列值的处理顺序。
    cells = _workbench()["cells"]
    by_score = {}
    for i, scored in enumerate(cells["scored"]):
        if scored:
            by_score.setdefault(cells["score"][i], set()).add(cells["top_pct"][i])
    assert any(len(v) == 1 for v in by_score.values())
    assert all(len(v) == 1 for v in by_score.values())


# ---------------------------------------------------------------------------
# 夜间地表温度
# ---------------------------------------------------------------------------

def test_night_lst_source_covers_every_cell():
    night = json.loads((PROJECT_ROOT / "data/gis/duchang_night_lst_myd11a1.json").read_text(encoding="utf-8"))
    assert night["metadata"]["granules"] == len(night["granules"]) == 448
    assert "2 K" in night["metadata"]["q3_rule"]
    assert night["cells"]["cell_id"] == _workbench()["cells"]["cell_id"]
    assert all(v is not None and 15 < v < 35 for v in night["cells"]["q3_night_lst_c_mean"])
    assert min(night["cells"]["q3_night_dates"]) > 0


def test_hazard_blends_day_and_night_ranks():
    workbench = _workbench()
    cells, meta = workbench["cells"], workbench["metadata"]
    assert meta["night_layer"]["unit"] == "°C"
    assert len(meta["night_layer"]["breaks"]) == len(meta["night_layer"]["palette"]) + 1
    assert meta["bivariate"]["hazard_breaks_pct"]
    checked = 0
    for i, scored in enumerate(cells["scored"]):
        if not scored:
            continue
        assert 0 < cells["hazard_day_pct"][i] <= 100
        assert 0 < cells["hazard_night_pct"][i] <= 100
        assert cells["night_lst_c"][i] is not None and cells["night_dates"][i] > 0
        checked += 1
    assert checked == 1606
    # 夜间热岛与白天地表温度几乎不相关，夜间分量必须实际改变危险排序。
    differs = sum(
        1 for i, scored in enumerate(cells["scored"])
        if scored and abs(cells["hazard_day_pct"][i] - cells["hazard_pct"][i]) > 10
    )
    assert differs > 300


def test_frontend_shows_night_layer_and_episode_flag(client):
    script = (PROJECT_ROOT / "static/js/heat-risk-workbench.js").read_text(encoding="utf-8")
    assert "night_lst_c" in script
    assert "持续高温 +1" in script
    assert "hot_night" not in script and "hot_day_run" not in script
