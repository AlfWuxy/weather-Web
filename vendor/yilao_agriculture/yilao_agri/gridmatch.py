"""网格/站点与田块的空间核对：坐标、距离与环境匹配。

设计原则：
- 容差只能来自已确认策略 ``policy.grid_matching.max_distance_km``。缺容差、
  容差来源未知或容差未确认时本模块报错关闭通路，不发明默认容差。
- 坐标缺失、非有限或越界时返回未核对/报错，不用 0 或田块坐标冒充网格点。
- 距离是球面大圆直线距离，不是道路里程，也不代表个人实际暴露路径。
- 未声明网格来源时输出「未核对」警告，不把它当成已匹配。
"""

from __future__ import annotations

import math
from typing import Any

from .models import GRID_ENVIRONMENTS, GRID_KINDS

# IUGG 平均地球半径（km）。仅用于距离描述，不是定位精度或传感器精度声明。
EARTH_RADIUS_KM = 6371.0088
DEGREE_KM = math.pi * EARTH_RADIUS_KM / 180.0

_UNKNOWN = {
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未知", "待定", "待核实", "待补充", "待填写",
}


def _number(value: Any) -> float | None:
    # bool 在 Python 中属于 int，但不能冒充坐标或容差数值。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _UNKNOWN
    return True


def valid_coordinate_pair(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    """返回有效的 (纬度, 经度)；缺失、非有限或越界一律 None。"""
    lat, lon = _number(latitude), _number(longitude)
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def great_circle_km(latitude_1: Any, longitude_1: Any,
                    latitude_2: Any, longitude_2: Any) -> float | None:
    """半正矢大圆距离（km）。任一坐标未知/越界返回 None，不返回 0 冒充重合。"""
    first = valid_coordinate_pair(latitude_1, longitude_1)
    second = valid_coordinate_pair(latitude_2, longitude_2)
    if first is None or second is None:
        return None
    lat1, lon1 = first
    lat2, lon2 = second
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    haver = (math.sin(d_phi / 2.0) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2)
    haver = min(1.0, max(0.0, haver))
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(haver))


def _degree_per_km(latitude: float) -> tuple[float, float] | None:
    """返回该纬度上 (纬度方向 1km 的度数, 经度方向 1km 的度数)。极点附近返回 None。"""
    cos_lat = math.cos(math.radians(latitude))
    if abs(cos_lat) < 1e-9:
        return None
    return 1.0 / DEGREE_KM, 1.0 / (DEGREE_KM * cos_lat)


def cell_half_extent_degrees(resolution_km: Any, latitude: Any) -> tuple[float, float] | None:
    """网格单元在纬度/经度方向的半宽（度）。无效分辨率或退化纬度返回 None。"""
    resolution = _number(resolution_km)
    lat = _number(latitude)
    if resolution is None or resolution <= 0 or lat is None or not -90.0 <= lat <= 90.0:
        return None
    per_km = _degree_per_km(lat)
    if per_km is None:
        return None
    return resolution / 2.0 * per_km[0], resolution / 2.0 * per_km[1]


def assess_grid_match(plot: dict, grid: Any, policy: Any, demonstration: bool) -> dict:
    """核对 weather.<plot>.grid 与 plot 的坐标/距离/环境是否一致。

    返回 ``{applicable, matched, reasons, metrics, warnings}``。
    ``applicable=False`` 表示未声明网格来源，只给「未核对」警告，不改判其他约束。
    只读输入，不修改传参对象。
    """
    reasons: list[dict[str, str]] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "grid_plot_distance_km": None,
        "grid_resolution_km": None,
        "grid_cell_id": None,
    }

    def reject(code: str, message: str) -> None:
        reason = {"code": code, "message": message}
        if reason not in reasons:
            reasons.append(reason)

    def warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    def finish(applicable: bool) -> dict:
        return {
            "applicable": applicable,
            "matched": None if not applicable else not reasons,
            "reasons": reasons,
            "metrics": metrics,
            "warnings": warnings,
        }

    if grid is None:
        warn("未声明网格/站点与田块的空间对应（weather.<plot_id>.grid 缺省）；"
             "该段天气的网格代表性未核对，不得声称代表该田块。")
        return finish(False)
    if not isinstance(grid, dict):
        reject("GRID_DECLARATION_INVALID", "grid 必须为对象。")
        return finish(True)

    kind = grid.get("kind")
    if kind not in GRID_KINDS:
        reject("GRID_KIND_UNKNOWN",
               "grid.kind 须为 point / grid_cell / station / region；未知不能当作已核对。")
    elif kind == "region":
        reject("GRID_REGION_NOT_PLOT_LEVEL",
               "region 级网格只声明区域，不能当作该田块的代表点。")
    if not _known(grid.get("source")):
        reject("GRID_SOURCE_UNKNOWN", "grid 缺少可辨识来源，不能核对网格代表性。")

    plot_pair = valid_coordinate_pair(plot.get("latitude"), plot.get("longitude"))
    if plot_pair is None:
        reject("PLOT_COORDINATE_UNKNOWN", "田块坐标缺失、非有限或越界，不能核对网格代表性。")
    grid_pair = valid_coordinate_pair(grid.get("latitude"), grid.get("longitude"))
    if grid_pair is None:
        reject("GRID_COORDINATE_UNKNOWN", "网格代表点坐标缺失、非有限或越界，不能核对网格代表性。")

    environment = grid.get("environment")
    if environment not in GRID_ENVIRONMENTS:
        reject("GRID_ENVIRONMENT_UNKNOWN", "grid 未确认露天或棚内环境，不能匹配田块。")
    elif environment != plot.get("environment"):
        reject("GRID_ENVIRONMENT_MISMATCH", "网格声明的露天/棚内环境与田块不符。")

    # 容差：只认已确认策略；缺项按未知关闭，不设默认值。
    tolerance = None
    matching = policy.get("grid_matching") if isinstance(policy, dict) else None
    if not isinstance(matching, dict):
        reject("GRID_TOLERANCE_UNKNOWN",
               "缺少已确认策略 policy.grid_matching.max_distance_km；不得使用自定默认容差。")
    else:
        if not _known(matching.get("source")):
            reject("GRID_TOLERANCE_SOURCE_UNKNOWN", "空间匹配容差策略缺少来源说明。")
        value = _number(matching.get("max_distance_km"))
        status = matching.get("review_status")
        if value is None or value < 0:
            reject("GRID_TOLERANCE_UNKNOWN",
                   "policy.grid_matching.max_distance_km 必须是有限非负数。")
        elif status == "illustrative" and demonstration:
            warn("空间匹配容差来自演示参数，不是已核实的实际规则。")
            tolerance = value
        elif status != "confirmed":
            reject("GRID_TOLERANCE_UNCONFIRMED", "容差策略未确认，不能用于当前模式的网格核对。")
        else:
            tolerance = value

    if plot_pair is not None and grid_pair is not None:
        distance = great_circle_km(plot_pair[0], plot_pair[1], grid_pair[0], grid_pair[1])
        metrics["grid_plot_distance_km"] = distance
        if distance is None:
            reject("GRID_DISTANCE_UNKNOWN", "无法计算田块与网格代表点之间的距离。")
        elif tolerance is not None and distance > tolerance:
            reject("GRID_PLOT_DISTANCE_EXCEEDED",
                   f"田块到网格代表点 {distance:.3f} km，超过已确认容差 {tolerance} km。")

    if kind == "grid_cell":
        if not _known(grid.get("cell_id")):
            reject("GRID_CELL_ID_MISSING", "grid_cell 必须提供可辨识 cell_id。")
        else:
            metrics["grid_cell_id"] = grid["cell_id"]
        resolution = _number(grid.get("resolution_km"))
        if resolution is None or resolution <= 0:
            reject("GRID_RESOLUTION_UNKNOWN", "grid_cell 必须提供正的 resolution_km。")
        else:
            metrics["grid_resolution_km"] = resolution
            if plot_pair is not None and grid_pair is not None and tolerance is not None:
                half = cell_half_extent_degrees(resolution, grid_pair[0])
                per_km = _degree_per_km(grid_pair[0])
                if half is None or per_km is None:
                    reject("GRID_RESOLUTION_UNKNOWN",
                           "该纬度上无法按 resolution_km 近似单元边界，不能核对包含关系。")
                else:
                    allowance_lat = tolerance * per_km[0]
                    allowance_lon = tolerance * per_km[1]
                    if (abs(plot_pair[0] - grid_pair[0]) > half[0] + allowance_lat
                            or abs(plot_pair[1] - grid_pair[1]) > half[1] + allowance_lon):
                        reject("GRID_PLOT_OUTSIDE_CELL",
                               "田块落在所声明网格单元（含已确认容差）之外。")

    if metrics["grid_plot_distance_km"] is not None:
        warn("网格距离为大圆直线距离，不含道路、地形与绕行；不代表个人实际暴露路径。")
    return finish(True)
