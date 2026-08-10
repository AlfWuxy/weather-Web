# -*- coding: utf-8 -*-
"""都昌县 1 km 热暴露 GIS 页面与公开 GeoJSON 构建器。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

MODIS_RADIUS_M = 6_371_007.181
MODIS_X_ORIGIN_M = 11_119_505.197665
MODIS_Y_ORIGIN_M = 3_335_851.5593
MODIS_PIXEL_WIDTH_M = 926.6254331391661
MODIS_PIXEL_HEIGHT_M = -926.6254331391666
DEFAULT_CELL_ID = "h28v06-r0081-c0156"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_GEOJSON_PATH = (
    PROJECT_ROOT / "data" / "gis" / "duchang_heat_exposure_cells.geojson"
)
PUBLIC_GEOJSON_SHA256 = (
    "c72b6d1a9ac9ba2c53bdf3ffdc1aadb14775c15ced2e156674e64be3a8a40237"
)
PUBLIC_METADATA_KEYS = frozenset({
    "age65_share_pct", "audited_cells", "available_modes", "breaks",
    "built_up_pct", "calendar_dates", "classification",
    "county_center_cells", "dataset", "default_mode", "definition",
    "details_anchor", "digits", "display_crs", "display_geometry",
    "distance_method", "doi", "end", "generated_at_utc", "geometry_rule",
    "hard_failures", "independent_validation", "input_fingerprints",
    "interpretation_ceiling", "label", "layers", "limitation",
    "local_frozen_scenes", "logical_name", "max",
    "max_corner_shift_cell_id", "max_corner_shift_corner",
    "max_corner_shift_m", "mean_elevation_m", "median", "metric_key",
    "min", "missing_cells", "native_geometry_field",
    "native_geometry_preserved", "native_grid_crs",
    "native_nominal_resolution_m", "native_sphere_radius_m",
    "official_catalog_scenes", "palette", "permanent_water_pct",
    "positive_population_support_cells", "product", "q3_coverage_pct",
    "q3_definition", "q3_lst_c_mean", "q3_valid_cell_days",
    "quality_summary", "rectified_corner_shift_audit", "rectified_formula",
    "rectified_geometry_analysis_use", "rectified_method",
    "schema_version", "season", "selection_rule", "sha256", "short_label",
    "source", "source_versions", "spatial_definition", "start",
    "study_period", "title", "tree_cover_pct", "unit", "valid_cells",
    "version", "zero_population_support_cells",
})
PUBLIC_BOUNDARY_PROPERTY_KEYS = frozenset({
    "boundary_level",
    "boundary_notice",
    "feature_type",
    "name_en",
    "name_zh",
    "shape_id",
})
PUBLIC_CELL_PROPERTY_KEYS = frozenset({
    "age65_share_pct",
    "built_up_pct",
    "cell_id",
    "center_lat_wgs84",
    "center_lon_wgs84",
    "feature_type",
    "local_available_dates",
    "mean_elevation_m",
    "modis_col_0based",
    "modis_row_0based",
    "modis_tile",
    "permanent_water_pct",
    "positive_population_support",
    "q3_coverage_pct",
    "q3_dates",
    "q3_lst_c_mean",
    "tree_cover_pct",
})
_PUBLIC_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|\s)/(?:Users|home|root|private|etc|var)/",
    re.IGNORECASE,
)


def _validate_public_metadata_value(value: Any, depth: int = 0) -> None:
    """公开元数据只接受审核过的键、短标量和有界列表。"""
    if depth > 8:
        raise ValueError("public_gis_metadata_too_deep")
    if isinstance(value, dict):
        if len(value) > 80 or not set(value).issubset(PUBLIC_METADATA_KEYS):
            raise ValueError("public_gis_metadata_key_not_allowed")
        for nested in value.values():
            _validate_public_metadata_value(nested, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 80:
            raise ValueError("public_gis_metadata_list_too_long")
        for nested in value:
            _validate_public_metadata_value(nested, depth + 1)
        return
    if isinstance(value, str):
        if (
            len(value) > 1200
            or "\x00" in value
            or _PUBLIC_ABSOLUTE_PATH_RE.search(value)
            or "BEGIN PRIVATE KEY" in value
        ):
            raise ValueError("public_gis_metadata_value_not_allowed")
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("public_gis_metadata_number_invalid")
        return
    raise ValueError("public_gis_metadata_type_not_allowed")


def _validate_public_polygon(geometry: Any) -> None:
    """公开几何仅接受都昌县附近、闭合且有界的 WGS84 Polygon。"""
    if not isinstance(geometry, dict) or set(geometry) != {"type", "coordinates"}:
        raise ValueError("public_gis_geometry_shape_invalid")
    if geometry.get("type") != "Polygon":
        raise ValueError("public_gis_geometry_type_invalid")
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or len(rings) != 1:
        raise ValueError("public_gis_geometry_rings_invalid")
    ring = rings[0]
    if not isinstance(ring, list) or not 4 <= len(ring) <= 10_000:
        raise ValueError("public_gis_geometry_points_invalid")
    normalized = []
    for point in ring:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("public_gis_geometry_point_invalid")
        longitude, latitude = point
        if (
            isinstance(longitude, bool)
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or not isinstance(latitude, (int, float))
            or not math.isfinite(float(longitude))
            or not math.isfinite(float(latitude))
            or not 115.0 <= float(longitude) <= 118.0
            or not 28.0 <= float(latitude) <= 31.0
        ):
            raise ValueError("public_gis_geometry_coordinate_invalid")
        normalized.append((float(longitude), float(latitude)))
    if normalized[0] != normalized[-1]:
        raise ValueError("public_gis_geometry_not_closed")


def _validate_public_feature(feature: Any, seen_ids: set[str]) -> str:
    """验证单个公开边界或网格要素的字段白名单。"""
    if (
        not isinstance(feature, dict)
        or set(feature) != {"type", "id", "properties", "geometry"}
        or feature.get("type") != "Feature"
        or not isinstance(feature.get("id"), str)
        or not 1 <= len(feature["id"]) <= 80
        or feature["id"] in seen_ids
        or not isinstance(feature.get("properties"), dict)
    ):
        raise ValueError("public_gis_feature_invalid")
    seen_ids.add(feature["id"])
    properties = feature["properties"]
    feature_type = properties.get("feature_type")
    if feature_type == "study_boundary":
        if set(properties) != PUBLIC_BOUNDARY_PROPERTY_KEYS:
            raise ValueError("public_gis_boundary_properties_invalid")
        if feature["id"] != "duchang-research-boundary":
            raise ValueError("public_gis_boundary_id_invalid")
        shape_id = properties.get("shape_id")
        if not isinstance(shape_id, str) or not 1 <= len(shape_id) <= 120:
            raise ValueError("public_gis_boundary_shape_id_invalid")
    elif feature_type == "modis_cell":
        if set(properties) != PUBLIC_CELL_PROPERTY_KEYS:
            raise ValueError("public_gis_cell_properties_invalid")
        if feature["id"] != properties.get("cell_id"):
            raise ValueError("public_gis_cell_id_invalid")
        if not re.fullmatch(r"h\d{2}v\d{2}-r\d{4}-c\d{4}", feature["id"]):
            raise ValueError("public_gis_cell_id_format_invalid")
        longitude = properties.get("center_lon_wgs84")
        latitude = properties.get("center_lat_wgs84")
        if (
            isinstance(longitude, bool)
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or not isinstance(latitude, (int, float))
            or not 115.0 <= float(longitude) <= 118.0
            or not 28.0 <= float(latitude) <= 31.0
        ):
            raise ValueError("public_gis_cell_center_invalid")
        for key in {
            "age65_share_pct",
            "built_up_pct",
            "permanent_water_pct",
            "q3_coverage_pct",
            "tree_cover_pct",
        }:
            value = properties.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 100
            ):
                raise ValueError("public_gis_cell_percent_invalid")
    else:
        raise ValueError("public_gis_feature_type_invalid")
    _validate_public_polygon(feature["geometry"])
    return feature_type


@lru_cache(maxsize=4)
def _load_validated_public_geojson_cached(
    path_value: str,
    mtime_ns: int,
    size_bytes: int,
) -> dict[str, Any]:
    """按文件版本缓存发布校验；摘要参数用于阻止陈旧缓存复用。"""
    del mtime_ns, size_bytes
    raw = Path(path_value).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PUBLIC_GEOJSON_SHA256:
        raise ValueError("public_gis_digest_mismatch")
    collection = json.loads(raw)
    if (
        not isinstance(collection, dict)
        or set(collection) != {"type", "name", "metadata", "features"}
        or collection.get("type") != "FeatureCollection"
        or collection.get("name") != "duchang_heat_exposure_cells"
        or not isinstance(collection.get("metadata"), dict)
        or not isinstance(collection.get("features"), list)
    ):
        raise ValueError("public_gis_collection_invalid")
    metadata = collection["metadata"]
    if set(metadata) != {
        "generated_at_utc",
        "input_fingerprints",
        "interpretation_ceiling",
        "layers",
        "quality_summary",
        "schema_version",
        "source_versions",
        "spatial_definition",
        "study_period",
        "title",
    }:
        raise ValueError("public_gis_metadata_contract_invalid")
    _validate_public_metadata_value(metadata)
    quality = metadata.get("quality_summary") or {}
    spatial = metadata.get("spatial_definition") or {}
    expected_cells = spatial.get("county_center_cells")
    if (
        quality.get("independent_validation") != "pass"
        or quality.get("hard_failures") != 0
        or not isinstance(expected_cells, int)
        or expected_cells <= 0
        or len(collection["features"]) != expected_cells + 1
    ):
        raise ValueError("public_gis_release_status_invalid")
    seen_ids: set[str] = set()
    feature_types = [
        _validate_public_feature(feature, seen_ids)
        for feature in collection["features"]
    ]
    if (
        feature_types.count("study_boundary") != 1
        or feature_types.count("modis_cell") != expected_cells
    ):
        raise ValueError("public_gis_feature_count_invalid")
    return collection


def load_validated_public_geojson(path: Path) -> dict[str, Any]:
    """验证冻结摘要、发布状态及完整字段/值边界后返回公开产物。"""
    stat_result = path.stat()
    return _load_validated_public_geojson_cached(
        str(path),
        stat_result.st_mtime_ns,
        stat_result.st_size,
    )


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

    load_validated_public_geojson(PUBLIC_GEOJSON_PATH)
    return render_template(
        "heat_exposure_gis.html",
        gis_data_url=url_for(
            "public.public_heat_geojson",
            v=PUBLIC_GEOJSON_SHA256[:16],
        ),
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
