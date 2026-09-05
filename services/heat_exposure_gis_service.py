# -*- coding: utf-8 -*-
"""都昌县 1 km 热暴露 GIS 页面与公开 GeoJSON 构建器。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MODIS_RADIUS_M = 6_371_007.181
MODIS_X_ORIGIN_M = 11_119_505.197665
MODIS_Y_ORIGIN_M = 3_335_851.5593
MODIS_PIXEL_WIDTH_M = 926.6254331391661
MODIS_PIXEL_HEIGHT_M = -926.6254331391666
DEFAULT_CELL_ID = "h28v06-r0081-c0156"
PUBLIC_GEOJSON_FILENAME = "data/gis/duchang_heat_exposure_cells.geojson"


LAYER_DEFINITIONS = {
    "age65_share_pct": {
        "label": "65 岁及以上人口比例",
        "short_label": "65+ 人口比例",
        "metric_key": "gis_age65_share",
        "details_anchor": "gis-age65-share",
        "unit": "%",
        "digits": 1,
        "definition": "ASPECT 模型化 65 岁及以上人口占模型化总人口的比例，仅在正人口支持网格显示。",
        "source": "ASPECT 2020",
        "palette": ["#edf7f4", "#c9e7df", "#8bcbbf", "#45a59a", "#177a77", "#0b4f5c"],
    },
    "q3_lst_c_mean": {
        "label": "晴空地表温度均值",
        "short_label": "地表温度",
        "metric_key": "gis_lst_mean",
        "details_anchor": "gis-lst-mean",
        "unit": "°C",
        "digits": 1,
        "definition": "2020 至 2024 年夏季 Aqua 白天、Q3 质量口径下的晴空地表温度均值。它不等同于气温或体感温度。",
        "source": "NASA MYD11A1.061",
        "palette": ["#fff7e4", "#fbdca2", "#f4b36c", "#e37a4d", "#b94739", "#762936"],
    },
    "q3_coverage_pct": {
        "label": "Q3 观测覆盖率",
        "short_label": "观测覆盖",
        "metric_key": "gis_q3_coverage",
        "details_anchor": "gis-q3-coverage",
        "unit": "%",
        "digits": 1,
        "definition": "Q3 质量合格观测天数占 448 个本地已冻结场景的比例，用于识别云遮与质量筛选造成的数据稀疏。",
        "source": "独立复核程序 v3",
        "palette": ["#eff5fb", "#d5e5f2", "#a9cee4", "#73afd0", "#3c89b5", "#1c5d8b"],
    },
    "tree_cover_pct": {
        "label": "树木覆盖比例",
        "short_label": "树木覆盖",
        "metric_key": "gis_tree_cover",
        "details_anchor": "gis-tree-cover",
        "unit": "%",
        "digits": 1,
        "definition": "ESA WorldCover 2020 树木覆盖类别对原生 MODIS 网格的源像元覆盖权重比例。",
        "source": "ESA WorldCover 2020 v100",
        "palette": ["#f2f6e9", "#dce9c5", "#bad497", "#88b966", "#55953f", "#2f6e2d"],
    },
    "built_up_pct": {
        "label": "建成区覆盖比例",
        "short_label": "建成区",
        "metric_key": "gis_built_up",
        "details_anchor": "gis-built-up",
        "unit": "%",
        "digits": 1,
        "definition": "ESA WorldCover 2020 建成区类别对原生 MODIS 网格的源像元覆盖权重比例。",
        "source": "ESA WorldCover 2020 v100",
        "palette": ["#f6f2f0", "#eadbd5", "#d6b9ae", "#bb8c7f", "#965e55", "#6f3d3b"],
    },
    "permanent_water_pct": {
        "label": "近似永久水域比例",
        "short_label": "永久水域",
        "metric_key": "gis_permanent_water",
        "details_anchor": "gis-permanent-water",
        "unit": "%",
        "digits": 1,
        "definition": "ESA WorldCover 2020 永久水体类别的源像元覆盖权重比例，属于近似覆盖比例，不是严格大地测量面积。",
        "source": "ESA WorldCover 2020 v100",
        "palette": ["#f0f7fa", "#d6ebf1", "#a9d7e3", "#72bdd0", "#3d99b5", "#216d91"],
    },
    "mean_elevation_m": {
        "label": "平均表面高程",
        "short_label": "表面高程",
        "metric_key": "gis_mean_elevation",
        "details_anchor": "gis-mean-elevation",
        "unit": "m",
        "digits": 0,
        "definition": "Copernicus DEM GLO-30 数字表面模型聚合到原生 MODIS 网格后的平均表面高程。",
        "source": "Copernicus DEM GLO-30",
        "palette": ["#f4f2e9", "#e5ddc4", "#cec29b", "#aa9d70", "#7d724d", "#51492f"],
    },
}


def _sha256(path: Path) -> str:
    """计算输入文件 SHA-256，形成可追溯指纹。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _project_to_wgs84(x_m: float, y_m: float) -> list[float]:
    """把 MODIS 正弦投影坐标转换为 WGS84 经纬度。"""
    latitude_rad = y_m / MODIS_RADIUS_M
    longitude_rad = x_m / (MODIS_RADIUS_M * math.cos(latitude_rad))
    return [round(math.degrees(longitude_rad), 9), round(math.degrees(latitude_rad), 9)]


def _cell_polygon(row: int, column: int) -> list[list[list[float]]]:
    """生成完整的原生 MODIS 网格四边形，不沿县界裁切。"""
    x_left = MODIS_X_ORIGIN_M + column * MODIS_PIXEL_WIDTH_M
    x_right = x_left + MODIS_PIXEL_WIDTH_M
    y_top = MODIS_Y_ORIGIN_M + row * MODIS_PIXEL_HEIGHT_M
    y_bottom = y_top + MODIS_PIXEL_HEIGHT_M
    ring = [
        _project_to_wgs84(x_left, y_top),
        _project_to_wgs84(x_right, y_top),
        _project_to_wgs84(x_right, y_bottom),
        _project_to_wgs84(x_left, y_bottom),
    ]
    ring.append(ring[0])
    return [ring]


def _great_circle_distance_m(point_a: list[float], point_b: list[float]) -> float:
    """按 MODIS 球体计算两个经纬度点之间的表面距离。"""
    lon_a, lat_a = map(math.radians, point_a)
    lon_b, lat_b = map(math.radians, point_b)
    delta_lon = lon_b - lon_a
    delta_lat = lat_b - lat_a
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * MODIS_RADIUS_M * math.asin(min(1, math.sqrt(haversine)))


def _rectified_ring(center_lon: float, center_lat: float) -> list[list[float]]:
    """生成中心保持、经纬轴对齐的局部等边近似显示格四角。"""
    half_lat_deg = MODIS_PIXEL_WIDTH_M / (2 * MODIS_RADIUS_M) * 180 / math.pi
    half_lon_deg = half_lat_deg / math.cos(math.radians(center_lat))
    return [
        [center_lon - half_lon_deg, center_lat + half_lat_deg],
        [center_lon + half_lon_deg, center_lat + half_lat_deg],
        [center_lon + half_lon_deg, center_lat - half_lat_deg],
        [center_lon - half_lon_deg, center_lat - half_lat_deg],
    ]


def _rectified_corner_shift_summary(features: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """逐格比较近似显示格与原生同名四角，记录最大表面位移。"""
    feature_list = list(features)
    corner_labels = ("NW", "NE", "SE", "SW")
    maximum = {"distance_m": -1.0, "cell_id": None, "corner": None}
    for feature in feature_list:
        properties = feature["properties"]
        rectified_corners = _rectified_ring(
            properties["center_lon_wgs84"],
            properties["center_lat_wgs84"],
        )
        native_corners = feature["geometry"]["coordinates"][0][:4]
        for label, native_corner, rectified_corner in zip(
            corner_labels,
            native_corners,
            rectified_corners,
        ):
            distance_m = _great_circle_distance_m(native_corner, rectified_corner)
            if distance_m > maximum["distance_m"]:
                maximum = {
                    "distance_m": distance_m,
                    "cell_id": feature["id"],
                    "corner": label,
                }
    return {
        "max_corner_shift_m": round(maximum["distance_m"], 6),
        "max_corner_shift_cell_id": maximum["cell_id"],
        "max_corner_shift_corner": maximum["corner"],
        "distance_method": "MODIS 球体上的同名角大圆表面距离",
        "audited_cells": len(feature_list),
    }


def _float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _quantile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("无法对空序列计算分位数")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _layer_statistics(features: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    feature_list = list(features)
    statistics: dict[str, dict[str, Any]] = {}
    for field, definition in LAYER_DEFINITIONS.items():
        values = sorted(
            float(feature["properties"][field])
            for feature in feature_list
            if feature["properties"].get(field) is not None
        )
        digits = int(definition["digits"])
        breaks = [
            _round(_quantile(values, probability), digits + 1)
            for probability in (0, 1 / 6, 2 / 6, .5, 4 / 6, 5 / 6, 1)
        ]
        statistics[field] = {
            **definition,
            "valid_cells": len(values),
            "missing_cells": len(feature_list) - len(values),
            "min": _round(values[0], digits + 1),
            "median": _round(_quantile(values, .5), digits + 1),
            "max": _round(values[-1], digits + 1),
            "breaks": breaks,
            "classification": "全县有效网格六分位数分级",
        }
    return statistics


def _validated_hard_failure_count(validation: dict[str, Any]) -> int:
    """验证冻结复核报告，任一必需字段缺失或异常时停止发布。"""
    hard_failure_count = validation.get("hard_failure_count")
    passed = (
        validation.get("status") == "pass"
        and validation.get("validation_pass") is True
        and type(hard_failure_count) is int
        and hard_failure_count == 0
    )
    if not passed:
        raise ValueError("独立复核报告未通过，停止生成 GIS 数据")
    return hard_failure_count


def build_public_geojson(
    universe_path: Path,
    observation_path: Path,
    boundary_path: Path,
    validation_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """从已冻结审计产物生成不含个人信息的公开 GIS 数据。"""
    universe_rows = _read_csv(universe_path)
    observation_rows = {row["cell_id"]: row for row in _read_csv(observation_path)}
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    boundary_collection = json.loads(boundary_path.read_text(encoding="utf-8"))

    if len(universe_rows) != 2593:
        raise ValueError(f"县域网格数应为 2593，当前为 {len(universe_rows)}")
    if set(observation_rows) != {row["cell_id"] for row in universe_rows}:
        raise ValueError("网格宇宙与观测摘要的 cell_id 不完全一致")
    hard_failure_count = _validated_hard_failure_count(validation)
    if len(boundary_collection.get("features", [])) != 1:
        raise ValueError("县界文件应仅含一个要素")

    cell_features: list[dict[str, Any]] = []
    for universe in universe_rows:
        observation = observation_rows[universe["cell_id"]]
        row = int(universe["modis_row_0based"])
        column = int(universe["modis_col_0based"])
        positive_population = universe["positive_population"] == "1"
        center_lon = float(universe["center_longitude_wgs84"])
        center_lat = float(universe["center_latitude_wgs84"])
        polygon = _cell_polygon(row, column)

        # 中心点是空间筛选依据，生成后再次校验投影转换。
        corner_lons = [point[0] for point in polygon[0][:-1]]
        corner_lats = [point[1] for point in polygon[0][:-1]]
        projected_center = _project_to_wgs84(
            MODIS_X_ORIGIN_M + (column + .5) * MODIS_PIXEL_WIDTH_M,
            MODIS_Y_ORIGIN_M + (row + .5) * MODIS_PIXEL_HEIGHT_M,
        )
        if abs(projected_center[0] - center_lon) > 1e-6 or abs(projected_center[1] - center_lat) > 1e-6:
            raise ValueError(f"{universe['cell_id']} 中心点投影校验失败")
        if not (min(corner_lons) <= center_lon <= max(corner_lons) and min(corner_lats) <= center_lat <= max(corner_lats)):
            raise ValueError(f"{universe['cell_id']} 中心点不在网格包围盒内")

        age_share = _float(universe["duchang_population_65plus_share"]) if positive_population else None
        cell_features.append({
            "type": "Feature",
            "id": universe["cell_id"],
            "geometry": {"type": "Polygon", "coordinates": polygon},
            "properties": {
                "feature_type": "modis_cell",
                "cell_id": universe["cell_id"],
                "modis_tile": universe["modis_tile"],
                "modis_row_0based": row,
                "modis_col_0based": column,
                "center_lon_wgs84": round(center_lon, 9),
                "center_lat_wgs84": round(center_lat, 9),
                "positive_population_support": positive_population,
                "age65_share_pct": _round(age_share * 100 if age_share is not None else None, 4),
                "q3_lst_c_mean": _round(_float(observation["q3_lst_c_mean"]), 4),
                "q3_dates": int(observation["q3_dates"]),
                "local_available_dates": int(observation["local_available_dates"]),
                "q3_coverage_pct": _round(float(observation["q3_fraction_of_local_dates"]) * 100, 4),
                "tree_cover_pct": _round(float(universe["tree_cover_fraction"]) * 100, 4),
                "built_up_pct": _round(float(universe["built_up_fraction"]) * 100, 4),
                "permanent_water_pct": _round(float(universe["permanent_water_fraction_area_weighted"]) * 100, 4),
                "mean_elevation_m": _round(_float(universe["mean_elevation_m"]), 4),
            },
        })

    layer_statistics = _layer_statistics(cell_features)
    positive_cells = sum(feature["properties"]["positive_population_support"] for feature in cell_features)
    total_q3_cell_days = sum(feature["properties"]["q3_dates"] for feature in cell_features)
    rectified_corner_shift = _rectified_corner_shift_summary(cell_features)

    source_boundary = boundary_collection["features"][0]
    boundary_feature = {
        "type": "Feature",
        "id": "duchang-research-boundary",
        "geometry": source_boundary["geometry"],
        "properties": {
            "feature_type": "study_boundary",
            "name_zh": "都昌县研究边界",
            "name_en": source_boundary.get("properties", {}).get("shapeName", "Duchang County"),
            "shape_id": source_boundary.get("properties", {}).get("shapeID"),
            "boundary_level": source_boundary.get("properties", {}).get("shapeType", "ADM3"),
            "boundary_notice": "geoBoundaries 研究边界，仅用于空间筛选与学术展示，不作为法定行政边界凭证。",
        },
    }

    metadata = {
        "title": "都昌县 1 km 网格级热暴露 GIS",
        "schema_version": "1.2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_period": {
            "start": "2020-06-01",
            "end": "2024-08-31",
            "season": "每年 6 月 1 日至 8 月 31 日",
            "calendar_dates": 460,
            "official_catalog_scenes": 449,
            "local_frozen_scenes": 448,
        },
        "spatial_definition": {
            "display_crs": "EPSG:4326 (WGS84)",
            "native_grid_crs": "MODIS Sinusoidal, sphere radius 6371007.181 m",
            "native_sphere_radius_m": MODIS_RADIUS_M,
            "native_nominal_resolution_m": MODIS_PIXEL_WIDTH_M,
            "selection_rule": "MODIS h28v06 原生网格中心点严格位于都昌县研究边界内",
            "geometry_rule": "GeoJSON 保留完整原生网格且不沿县界裁切；网页可切换制图显示几何",
            "display_geometry": {
                "default_mode": "rectified",
                "available_modes": ["rectified", "native"],
                "rectified_method": "以原生网格中心为锚点，生成中心保持、经纬轴对齐的局部等边近似显示格",
                "rectified_formula": "delta_lat_deg=(p/(2R))*180/pi; delta_lon_deg=delta_lat_deg/cos(latitude_center_rad)",
                "rectified_corner_shift_audit": rectified_corner_shift,
                "native_geometry_preserved": True,
                "native_geometry_field": "feature.geometry",
                "rectified_geometry_analysis_use": False,
                "limitation": "近似显示格仅用于页面制图、点击和比较；点落格、边界相交与精确空间分析应使用原生 GeoJSON 几何",
            },
            "county_center_cells": len(cell_features),
            "positive_population_support_cells": positive_cells,
            "zero_population_support_cells": len(cell_features) - positive_cells,
        },
        "quality_summary": {
            "independent_validation": "pass",
            "hard_failures": hard_failure_count,
            "q3_valid_cell_days": total_q3_cell_days,
            "q3_definition": "Q3 包含 mandatory QA 00 或 01 且 LST 原始编码有效的 Aqua 白天晴空观测",
        },
        "interpretation_ceiling": [
            "地表温度不等同于 2 米气温、室内温度或人体体感温度。",
            "65 岁及以上人口比例来自模型化栅格，不是个人记录或逐户普查微数据。",
            "各图层用于描述空间暴露与背景条件，不能单独解释个人健康风险或因果效应。",
            "云遮和质量筛选使有效观测在空间上不均匀，应与观测覆盖图层联合阅读。",
        ],
        "source_versions": [
            {"dataset": "NASA Aqua MODIS Land Surface Temperature", "product": "MYD11A1.061", "doi": "10.5067/MODIS/MYD11A1.061"},
            {"dataset": "ASPECT age-structured population", "version": "2020, 100 m", "doi": "10.1038/s41597-025-05401-1"},
            {"dataset": "ESA WorldCover", "version": "2020 v100, 10 m", "doi": "10.5281/zenodo.5571936"},
            {"dataset": "Copernicus DEM", "version": "GLO-30 DSM", "doi": "10.5270/ESA-c5d3d65"},
            {"dataset": "geoBoundaries", "version": "ADM3 research boundary", "doi": "10.1371/journal.pone.0231866"},
        ],
        "input_fingerprints": [
            {"logical_name": "cell_universe.csv", "sha256": _sha256(universe_path)},
            {"logical_name": "cell_observation_summary.csv", "sha256": _sha256(observation_path)},
            {"logical_name": "duchang_boundary.geojson", "sha256": _sha256(boundary_path)},
            {"logical_name": "independent_validation_report.json", "sha256": _sha256(validation_path)},
        ],
        "layers": layer_statistics,
    }

    collection = {
        "type": "FeatureCollection",
        "name": "duchang_heat_exposure_cells",
        "metadata": metadata,
        "features": [boundary_feature, *cell_features],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return collection


def render_heat_exposure_gis():
    """渲染登录后的学术 GIS 原型。"""
    # 构建器无需 Flask 环境，页面依赖在渲染时再加载。
    from flask import current_app, render_template, url_for

    static_path = Path(current_app.static_folder) / PUBLIC_GEOJSON_FILENAME
    try:
        static_version = int(static_path.stat().st_mtime)
    except OSError:
        static_version = None
    url_values = {"filename": PUBLIC_GEOJSON_FILENAME}
    if static_version is not None:
        url_values["v"] = static_version
    return render_template(
        "heat_exposure_gis.html",
        gis_data_url=url_for("static", **url_values),
        default_cell_id=DEFAULT_CELL_ID,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建都昌县热暴露 GIS GeoJSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="生成公开 GeoJSON")
    build.add_argument("--universe", required=True, type=Path)
    build.add_argument("--observations", required=True, type=Path)
    build.add_argument("--boundary", required=True, type=Path)
    build.add_argument("--validation", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "build":
        collection = build_public_geojson(
            args.universe,
            args.observations,
            args.boundary,
            args.validation,
            args.output,
        )
        metadata = collection["metadata"]
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "cell_count": metadata["spatial_definition"]["county_center_cells"],
                    "validation": metadata["quality_summary"]["independent_validation"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
