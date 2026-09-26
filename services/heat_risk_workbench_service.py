# -*- coding: utf-8 -*-
"""都昌县热风险医生工作台：综合风险分构建、每日危险等级与页面渲染。

构建阶段（离线，结果提交到仓库）：
    读取已发布的 1 km 热暴露 GeoJSON、OSM 乡镇边界和都昌逐日气象，
    按 IPCC AR6“危险性 × 暴露 × 脆弱性”框架生成网格综合风险分，
    并计算 Getis-Ord Gi* 热点、权重扰动排名稳定性、乡镇汇总和医疗点可达距离。

运行阶段（请求时）：
    读取 7 天预报，按中国气象局高温口径与本地热夜阈值给出逐日危险等级，
    再与网格静态风险组合成逐日风险，用于村级巡访优先排序。

旧版 GIS 页面与其 GeoJSON 保持不变，可通过 HEAT_EXPOSURE_GIS_UI=legacy 回滚。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CELLS_GEOJSON_FILENAME = "data/gis/duchang_heat_exposure_cells.geojson"
WORKBENCH_FILENAME = "data/gis/duchang_heat_risk_workbench.json"
TOWNSHIPS_SOURCE_PATH = PROJECT_ROOT / "data/gis/duchang_townships_osm.geojson"
CLIMATE_SOURCE_PATH = PROJECT_ROOT / "data/raw/逐日数据.csv"

SCHEMA_VERSION = "2.0.0"
WATER_MASK_PCT = 50.0
LEVEL_BREAKS = (20.0, 40.0, 60.0, 80.0)
STABILITY_PERTURBATION = 0.20
STABILITY_DRAWS = 500
STABILITY_SEED = 20260926
STABLE_RANK_SPAN_PCT = 20.0
GI_FDR_Q = 0.05
GI_FDR_Q_STRONG = 0.01
EARTH_RADIUS_KM = 6371.0088

# 与 NWS HeatRisk 同构的五级配色：绿、黄、橙、红、品红。
RISK_LEVELS = [
    {"level": 0, "label": "较低", "color": "#cfe8c8"},
    {"level": 1, "label": "轻度", "color": "#f6e27a"},
    {"level": 2, "label": "中度", "color": "#f5a34b"},
    {"level": 3, "label": "高", "color": "#d9412b"},
    {"level": 4, "label": "极高", "color": "#9c1d6b"},
]

# 3×3 双变量配色（Stevens 方案）：字母为地表温度三分位，数字为 65+ 比例三分位。
BIVARIATE_PALETTE = {
    "a1": "#e8e8e8", "b1": "#e4acac", "c1": "#c85a5a",
    "a2": "#b0d5df", "b2": "#ad9ea5", "c2": "#985356",
    "a3": "#64acbe", "b3": "#627f8c", "c3": "#574249",
}

# 中国气象局高温预警口径：35 °C 为高温日，37 °C 橙色，40 °C 红色。
HAZARD_LEVELS = [
    {"level": 0, "label": "无高温", "rule": "最高气温 < 33 °C"},
    {"level": 1, "label": "偏热关注", "rule": "最高气温 33–34.9 °C"},
    {"level": 2, "label": "高温", "rule": "最高气温 ≥ 35 °C（黄色预警量级）"},
    {"level": 3, "label": "严重高温", "rule": "最高气温 ≥ 37 °C（橙色预警量级）"},
    {"level": 4, "label": "极端高温", "rule": "最高气温 ≥ 40 °C（红色预警量级）"},
]
HEATWAVE_MIN_DAYS = 3
HOT_DAY_C = 35.0

ACTION_CARDS = {
    0: {
        "title": "常规随访",
        "doctor": ["按常规随访计划执行", "核对高龄独居老人联系方式是否有效"],
        "caregiver": ["保持日常饮水", "留意天气预报变化"],
    },
    1: {
        "title": "提醒关注",
        "doctor": ["电话提醒高龄、慢病老人午间避暑", "确认降压药、利尿剂使用者知道补水要点"],
        "caregiver": ["11–15 时减少户外劳作", "每天主动饮水 1.5–2 升，不等口渴"],
    },
    2: {
        "title": "重点电话随访",
        "doctor": [
            "当天电话联系名单内全部 75 岁以上独居老人",
            "对心脑血管、肾病、糖尿病患者提示用药与补水",
            "确认最近避暑点开放时间",
        ],
        "caregiver": ["每天至少两次查看老人（中午、傍晚）", "室温超过 32 °C 时陪同去阴凉处或避暑点"],
    },
    3: {
        "title": "当日上门巡访",
        "doctor": [
            "优先上门巡访清单前列村庄的高龄、失能、独居老人",
            "携带口服补液盐与电子体温计",
            "发现意识模糊、体温 ≥ 39 °C、停止出汗时立即按中暑急救处理并转诊",
        ],
        "caregiver": ["中午和夜间都要查看老人", "夜间室温高时安排去有空调的亲友家或避暑点"],
    },
    4: {
        "title": "应急响应",
        "doctor": [
            "启动村医与村委联动，逐户确认高风险老人状态",
            "提前联系乡镇卫生院预留中暑转诊通道",
            "停止老人一切午间户外活动与劳作",
        ],
        "caregiver": ["全天有人陪护高龄与失能老人", "出现头晕、呕吐、意识改变立即拨打 120"],
    },
}

SOURCE_NOTES = [
    {"topic": "风险框架", "citation": "IPCC AR6 WGII Chapter 1", "url": "https://www.ipcc.ch/report/ar6/wg2/chapter/chapter-1/"},
    {"topic": "网格热风险方法先例", "citation": "Morabito et al., PLOS ONE 2015", "url": "https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0127277"},
    {"topic": "赣北 1 km 热风险", "citation": "Zheng et al., IJERPH 2020", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC7559026/"},
    {"topic": "逐日分级与行动卡", "citation": "NWS HeatRisk", "url": "https://www.wpc.ncep.noaa.gov/heatrisk/"},
    {"topic": "影响 × 可能性矩阵", "citation": "UKHSA Heat-Health Alerting", "url": "https://www.gov.uk/guidance/heat-health-alerting-system"},
    {"topic": "热夜对 65 岁以上人群的影响", "citation": "Wu et al. 2025", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12514547/"},
    {"topic": "热浪与老年死亡", "citation": "Pan et al. 2023", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10063284/"},
    {"topic": "热点分析", "citation": "Getis-Ord Gi*（Esri 方法说明）", "url": "https://doc.esri.com/en/arcgis-pro/latest/tool-reference/spatial-statistics/h-how-hot-spot-analysis-getis-ord-gi-spatial-stati.html"},
    {"topic": "指数排名敏感性", "citation": "Tate 2012, Natural Hazards", "url": "https://link.springer.com/article/10.1007/s11069-012-0152-2"},
    {"topic": "乡镇边界与驻地", "citation": "OpenStreetMap（ODbL）", "url": "https://www.openstreetmap.org/copyright"},
]


# ---------------------------------------------------------------------------
# 坐标：WGS84 与 GCJ-02 互转（站内高德坐标为 GCJ-02）
# ---------------------------------------------------------------------------

_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _gcj_delta(lon: float, lat: float) -> tuple[float, float]:
    x, y = lon - 105.0, lat - 35.0
    d_lat = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    d_lat += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    d_lat += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    d_lat += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    d_lon = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    d_lon += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    d_lon += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    d_lon += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    rad_lat = lat / 180.0 * math.pi
    magic = 1 - _GCJ_EE * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (_GCJ_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return d_lon, d_lat


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 → GCJ-02（国测局加偏公式）。"""
    d_lon, d_lat = _gcj_delta(lon, lat)
    return lon + d_lon, lat + d_lat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → WGS84，迭代反解，残差约 0.01 m。"""
    w_lon, w_lat = lon, lat
    for _ in range(12):
        g_lon, g_lat = wgs84_to_gcj02(w_lon, w_lat)
        w_lon -= g_lon - lon
        w_lat -= g_lat - lat
    return w_lon, w_lat


def haversine_km(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    lon_a, lat_a, lon_b, lat_b = map(math.radians, (lon_a, lat_a, lon_b, lat_b))
    h = math.sin((lat_b - lat_a) / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin((lon_b - lon_a) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


# ---------------------------------------------------------------------------
# 构建：综合风险分
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _percentile_rank(values):
    """平均秩百分位，取值 (0, 1]，并列值共享同一秩。"""
    import numpy as np
    from scipy.stats import rankdata

    values = np.asarray(values, dtype=float)
    return rankdata(values, method="average") / len(values)


def _points_in_ring(lons, lats, ring):
    """射线法判断点是否落在单个环内（numpy 向量化）。"""
    import numpy as np

    inside = np.zeros(len(lons), dtype=bool)
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    count = len(ring)
    j = count - 1
    for i in range(count):
        xi, yi, xj, yj = xs[i], ys[i], xs[j], ys[j]
        crosses = (yi > lats) != (yj > lats)
        if yj != yi:
            x_cross = (xj - xi) * (lats - yi) / (yj - yi) + xi
            inside ^= crosses & (lons < x_cross)
        j = i
    return inside


def _points_in_geometry(lons, lats, geometry):
    import numpy as np

    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    inside = np.zeros(len(lons), dtype=bool)
    for polygon in polygons:
        shell = _points_in_ring(lons, lats, polygon[0])
        for hole in polygon[1:]:
            shell &= ~_points_in_ring(lons, lats, hole)
        inside |= shell
    return inside


def _build_facilities(townships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """医疗点：OSM 精确点优先，其余按“一乡一卫生院”放在乡镇驻地近似位置。"""
    exact = {
        "蔡岭镇": {
            "name": "都昌县蔡岭中心卫生院",
            "lon": 116.3935165,
            "lat": 29.4802228,
            "source": "OSM way 1485918607 amenity=hospital",
            "precision": "exact",
        },
    }
    facilities = []
    for township in townships:
        properties = township["properties"]
        name = properties["name_zh"]
        if name in exact:
            item = exact[name]
            facilities.append({**item, "township": name, "kind": "township_health_center"})
            continue
        is_county_seat = name == "都昌镇"
        facilities.append({
            "name": "都昌县城医疗机构（县城驻地近似）" if is_county_seat else f"{name}卫生院（驻地近似）",
            "township": name,
            "kind": "county_medical_center" if is_county_seat else "township_health_center",
            "lon": properties["seat_lon_wgs84"],
            "lat": properties["seat_lat_wgs84"],
            "source": f"OSM node {properties['seat_osm_node']} admin_centre",
            "precision": "township_seat",
        })
    return facilities


def _gi_star(values, rows, cols):
    """Getis-Ord Gi*：Queen 8 邻域并包含自身，二值权重，解析 z 值。"""
    import numpy as np

    values = np.asarray(values, dtype=float)
    n = len(values)
    index = {(int(r), int(c)): i for i, (r, c) in enumerate(zip(rows, cols))}
    mean = values.mean()
    std = math.sqrt(max((values ** 2).mean() - mean ** 2, 1e-12))
    z_scores = np.zeros(n)
    for i, (r, c) in enumerate(zip(rows, cols)):
        members = [
            index[(int(r) + dr, int(c) + dc)]
            for dr in (-1, 0, 1)
            for dc in (-1, 0, 1)
            if (int(r) + dr, int(c) + dc) in index
        ]
        weight = len(members)
        local_sum = values[members].sum()
        denominator = std * math.sqrt((n * weight - weight ** 2) / (n - 1))
        z_scores[i] = 0.0 if denominator == 0 else (local_sum - mean * weight) / denominator
    return z_scores


def _benjamini_hochberg(p_values, q):
    """返回 BH 校正后显著的布尔数组。"""
    import numpy as np

    p_values = np.asarray(p_values, dtype=float)
    n = len(p_values)
    order = np.argsort(p_values)
    thresholds = q * (np.arange(1, n + 1) / n)
    passed = p_values[order] <= thresholds
    significant = np.zeros(n, dtype=bool)
    if passed.any():
        cutoff = np.max(np.nonzero(passed)[0])
        significant[order[: cutoff + 1]] = True
    return significant


def _read_climate_thresholds(path: Path) -> dict[str, Any]:
    """从都昌逐日气象计算热夜阈值（全年日最低气温第 95 百分位）。"""
    import numpy as np

    tmin_column = "2米最低气温 (多源融合)(°C)"
    tmax_column = "2米最高气温 (多源融合)(°C)"
    tmins, tmaxs, dates = [], [], []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                tmins.append(float(row[tmin_column]))
                tmaxs.append(float(row[tmax_column]))
                dates.append(row["日期"])
            except (KeyError, TypeError, ValueError):
                continue
    tmins_arr = np.asarray(tmins)
    tmaxs_arr = np.asarray(tmaxs)
    return {
        "hot_night_tmin_c": round(float(np.percentile(tmins_arr, 95)), 1),
        "hot_night_rule": "日最低气温 ≥ 都昌全年日最低气温第 95 百分位",
        "hot_day_tmax_c": HOT_DAY_C,
        "heatwave_min_days": HEATWAVE_MIN_DAYS,
        "climate_sample": {
            "source": "data/raw/逐日数据.csv（多源融合再分析，都昌县城点位）",
            "start": min(dates),
            "end": max(dates),
            "days": len(dates),
            "days_tmax_ge_35": int((tmaxs_arr >= 35).sum()),
            "days_tmax_ge_37": int((tmaxs_arr >= 37).sum()),
        },
    }


def build_workbench_data(
    cells_path: Path,
    townships_path: Path,
    climate_path: Path,
    output_path: Path,
    draws: int = STABILITY_DRAWS,
    seed: int = STABILITY_SEED,
) -> dict[str, Any]:
    """从已发布的网格 GeoJSON 生成医生工作台所需的风险数据。"""
    import numpy as np
    from scipy.stats import norm, rankdata

    collection = json.loads(cells_path.read_text(encoding="utf-8"))
    township_collection = json.loads(townships_path.read_text(encoding="utf-8"))
    townships = township_collection["features"]
    cells = [f for f in collection["features"] if f["properties"]["feature_type"] == "modis_cell"]
    props = [f["properties"] for f in cells]
    n = len(cells)

    def column(key):
        return np.array([np.nan if p.get(key) is None else float(p[key]) for p in props])

    lons, lats = column("center_lon_wgs84"), column("center_lat_wgs84")
    rows = np.array([p["modis_row_0based"] for p in props])
    cols = np.array([p["modis_col_0based"] for p in props])
    lst, age = column("q3_lst_c_mean"), column("age65_share_pct")
    tree, built, water = column("tree_cover_pct"), column("built_up_pct"), column("permanent_water_pct")

    land = water < WATER_MASK_PCT
    scored = land & ~np.isnan(age) & ~np.isnan(lst)

    # 乡镇归属：中心点落在 OSM 多边形内；落在多边形缝隙时取最近驻地。
    township_index = np.full(n, -1)
    for t_idx, township in enumerate(townships):
        hit = _points_in_geometry(lons, lats, township["geometry"]) & (township_index < 0)
        township_index[hit] = t_idx
    seat_lons = np.array([t["properties"]["seat_lon_wgs84"] for t in townships])
    seat_lats = np.array([t["properties"]["seat_lat_wgs84"] for t in townships])
    unassigned = np.nonzero(township_index < 0)[0]
    for i in unassigned:
        distances = [haversine_km(lons[i], lats[i], sl, sa) for sl, sa in zip(seat_lons, seat_lats)]
        township_index[i] = int(np.argmin(distances))
    township_method = np.where(np.isin(np.arange(n), unassigned), "nearest_seat", "polygon")

    facilities = _build_facilities(townships)
    facility_km = np.zeros(n)
    facility_idx = np.zeros(n, dtype=int)
    for i in range(n):
        distances = [haversine_km(lons[i], lats[i], f["lon"], f["lat"]) for f in facilities]
        facility_idx[i] = int(np.argmin(distances))
        facility_km[i] = distances[facility_idx[i]]

    # 各分量在“有人居住的陆地网格”内取百分位。
    s_idx = np.nonzero(scored)[0]
    hazard = _percentile_rank(lst[s_idx])
    exposure = _percentile_rank(age[s_idx])
    shade_deficit = _percentile_rank(100.0 - tree[s_idx])
    built_share = _percentile_rank(built[s_idx])
    access = _percentile_rank(facility_km[s_idx])
    vulnerability = _percentile_rank((shade_deficit + built_share + access) / 3.0)
    components = np.vstack([hazard, exposure, vulnerability])
    score = np.round(100.0 * np.prod(components ** (1.0 / 3.0), axis=0), 1)
    # 等级按发布的一位小数分值划分，避免页面显示分值与等级不一致。
    level = np.digitize(score, LEVEL_BREAKS)

    # 双变量三分位。
    lst_breaks = np.quantile(lst[s_idx], [1 / 3, 2 / 3])
    age_breaks = np.quantile(age[s_idx], [1 / 3, 2 / 3])
    lst_class = np.digitize(lst[s_idx], lst_breaks)
    age_class = np.digitize(age[s_idx], age_breaks)
    bivariate = [f"{'abc'[a]}{b + 1}" for a, b in zip(lst_class, age_class)]

    # Gi* 热点，BH FDR 校正。
    gi_z = _gi_star(score, rows[s_idx], cols[s_idx])
    gi_p = 2 * norm.sf(np.abs(gi_z))
    sig_05 = _benjamini_hochberg(gi_p, GI_FDR_Q)
    sig_01 = _benjamini_hochberg(gi_p, GI_FDR_Q_STRONG)
    gi_bin = np.where(sig_01, 2, np.where(sig_05, 1, 0)) * np.sign(gi_z).astype(int)

    # 权重 ±20% 扰动下的排名区间（“前 x%”）。
    rng = np.random.default_rng(seed)
    m = len(s_idx)
    top_share_draws = np.zeros((draws, m))
    log_components = np.log(components)
    for d in range(draws):
        weights = (1 / 3) * (1 + rng.uniform(-STABILITY_PERTURBATION, STABILITY_PERTURBATION, 3))
        weights /= weights.sum()
        draw_score = weights @ log_components
        # 并列分值共享最优名次；argsort 对并列值的顺序依赖 CPU 与 numpy 实现，会导致结果不可复现。
        top_share_draws[d] = rankdata(-draw_score, method="min") / m * 100.0
    top_share = rankdata(-score, method="min") / m * 100.0
    top_p05 = np.percentile(top_share_draws, 5, axis=0)
    top_p95 = np.percentile(top_share_draws, 95, axis=0)

    def blank(fill=None):
        return [fill] * n

    fields = {
        "cell_id": [p["cell_id"] for p in props],
        "township": township_index.tolist(),
        "township_method": township_method.tolist(),
        "land": land.astype(int).tolist(),
        "scored": scored.astype(int).tolist(),
        "facility": facility_idx.tolist(),
        "facility_km": np.round(facility_km, 2).tolist(),
        "hazard_pct": blank(), "exposure_pct": blank(), "vulnerability_pct": blank(),
        "shade_deficit_pct": blank(), "built_pct": blank(), "access_pct": blank(),
        "score": blank(), "level": blank(), "bivariate": blank(),
        "gi_z": blank(), "gi_bin": blank(0),
        "top_pct": blank(), "top_pct_p05": blank(), "top_pct_p95": blank(),
    }
    for k, i in enumerate(s_idx):
        fields["hazard_pct"][i] = round(float(hazard[k]) * 100, 2)
        fields["exposure_pct"][i] = round(float(exposure[k]) * 100, 2)
        fields["vulnerability_pct"][i] = round(float(vulnerability[k]) * 100, 2)
        fields["shade_deficit_pct"][i] = round(float(shade_deficit[k]) * 100, 2)
        fields["built_pct"][i] = round(float(built_share[k]) * 100, 2)
        fields["access_pct"][i] = round(float(access[k]) * 100, 2)
        fields["score"][i] = round(float(score[k]), 1)
        fields["level"][i] = int(level[k])
        fields["bivariate"][i] = bivariate[k]
        fields["gi_z"][i] = round(float(gi_z[k]), 2)
        fields["gi_bin"][i] = int(gi_bin[k])
        fields["top_pct"][i] = round(float(top_share[k]), 1)
        fields["top_pct_p05"][i] = round(float(top_p05[k]), 1)
        fields["top_pct_p95"][i] = round(float(top_p95[k]), 1)

    township_features = []
    for t_idx, township in enumerate(townships):
        members = np.nonzero(township_index == t_idx)[0]
        scored_members = [i for i in members if scored[i]]
        scores = [fields["score"][i] for i in scored_members]
        township_features.append({
            "type": "Feature",
            "id": township["id"],
            "geometry": township["geometry"],
            "properties": {
                **township["properties"],
                "index": t_idx,
                "cells": int(len(members)),
                "land_cells": int(land[members].sum()),
                "scored_cells": len(scored_members),
                "mean_score": round(float(np.mean(scores)), 1) if scores else None,
                "p90_score": round(float(np.percentile(scores, 90)), 1) if scores else None,
                "high_cells": int(sum(1 for i in scored_members if fields["level"][i] >= 3)),
                "hotspot_cells": int(sum(1 for i in scored_members if fields["gi_bin"][i] > 0)),
            },
        })

    level_counts = np.bincount(level, minlength=5).tolist()
    metadata = {
        "title": "都昌县热风险医生工作台数据",
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_geojson": CELLS_GEOJSON_FILENAME,
        "counts": {
            "cells": n,
            "water_masked_cells": int((~land).sum()),
            "land_cells": int(land.sum()),
            "scored_cells": int(scored.sum()),
            "level_counts": level_counts,
            "hotspot_cells": int((gi_bin > 0).sum()),
            "coldspot_cells": int((gi_bin < 0).sum()),
            "township_gap_cells": int(len(unassigned)),
        },
        "water_mask": {
            "rule": f"近似永久水域比例 ≥ {WATER_MASK_PCT:.0f}% 的网格视为湖面，不参与评分与分级",
            "threshold_pct": WATER_MASK_PCT,
        },
        "score_method": {
            "framework": "IPCC AR6：风险 = 危险性 × 暴露 × 脆弱性",
            "population": "有人居住的陆地网格（正人口支持且水域 < 50%）",
            "normalization": "各分量在评分网格内取平均秩百分位 (0, 1]",
            "hazard": "Aqua 白天晴空地表温度均值（2020–2024 夏季）",
            "exposure": "ASPECT 65 岁及以上人口比例",
            "vulnerability": "树荫缺口（100 − 树木覆盖）、建成区比例、到最近医疗点距离三者百分位均值，再取百分位",
            "combination": "三项等权几何平均 × 100",
            "weights": {"hazard": 1 / 3, "exposure": 1 / 3, "vulnerability": 1 / 3},
            "level_breaks": list(LEVEL_BREAKS),
            "levels": RISK_LEVELS,
            "limitation": "65+ 为比例而非人数；地表温度不等于气温；尚未用本地就诊结局验证。",
        },
        "bivariate": {
            "x": "晴空地表温度三分位（a 低 → c 高）",
            "y": "65+ 人口比例三分位（1 低 → 3 高）",
            "lst_breaks_c": [round(float(v), 2) for v in lst_breaks],
            "age65_breaks_pct": [round(float(v), 2) for v in age_breaks],
            "palette": BIVARIATE_PALETTE,
        },
        "hotspot": {
            "method": "Getis-Ord Gi*，Queen 8 邻域含自身，二值权重，解析 z 值",
            "variable": "综合风险分",
            "multiple_testing": f"Benjamini–Hochberg FDR，q = {GI_FDR_Q}（强显著 q = {GI_FDR_Q_STRONG}）",
            "gi_bin": "2 强显著热点，1 显著热点，0 不显著，负值为冷点",
        },
        "stability": {
            "method": f"三项权重各自随机扰动 ±{STABILITY_PERTURBATION:.0%} 后归一化，重算 {draws} 次排名",
            "seed": seed,
            "draws": draws,
            "reported": "全县排名“前 x%”的第 5 与第 95 百分位；并列分值共享最优名次",
            "stable_span_pct": STABLE_RANK_SPAN_PCT,
        },
        "facilities": {
            "rule": "一乡一卫生院：OSM 精确点优先，其余放在 OSM 乡镇驻地点，仅作近似可达距离",
            "distance": "网格中心到最近医疗点的球面直线距离（km）",
        },
        "townships": {
            "source": township_collection.get("metadata", {}),
            "assignment": "网格中心点落入 OSM 乡镇多边形；落在多边形缝隙时取最近乡镇驻地",
        },
        "daily": {
            **_read_climate_thresholds(climate_path),
            "hazard_levels": HAZARD_LEVELS,
            "escalation": "当日最低气温达到热夜阈值，或已连续 ≥ 3 天最高气温 ≥ 35 °C 时，危险等级上调一级（最高 4 级）",
            "matrix": "危险等级为 0 时当日风险为 0；否则按静态风险等级调整：静态 0–1 级 −1，2 级不变，3–4 级 +1，结果限定在 1–4 级",
        },
        "action_cards": ACTION_CARDS,
        "sources": SOURCE_NOTES,
        "input_fingerprints": [
            {"logical_name": Path(CELLS_GEOJSON_FILENAME).name, "sha256": _sha256(cells_path)},
            {"logical_name": townships_path.name, "sha256": _sha256(townships_path)},
            {"logical_name": climate_path.name, "sha256": _sha256(climate_path)},
        ],
    }

    payload = {
        "metadata": metadata,
        "cells": fields,
        "townships": {"type": "FeatureCollection", "features": township_features},
        "facilities": facilities,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# 运行：逐日危险等级与村级优先排序
# ---------------------------------------------------------------------------

def _to_float(value):
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def base_hazard_level(tmax: float | None) -> int:
    if tmax is None:
        return 0
    if tmax >= 40:
        return 4
    if tmax >= 37:
        return 3
    if tmax >= 35:
        return 2
    if tmax >= 33:
        return 1
    return 0


def classify_daily_hazard(
    forecast: Iterable[dict[str, Any]],
    hot_night_tmin_c: float,
    prior_hot_days: int = 0,
) -> list[dict[str, Any]]:
    """把预报逐日转换为 0–4 级危险等级。

    prior_hot_days 为预报首日之前已连续出现的高温日数，用于跨越观测与预报识别热浪。
    """
    results = []
    run = max(int(prior_hot_days or 0), 0)
    for entry in forecast:
        tmax = _to_float(entry.get("temperature_max"))
        tmin = _to_float(entry.get("temperature_min"))
        run = run + 1 if tmax is not None and tmax >= HOT_DAY_C else 0
        base = base_hazard_level(tmax)
        reasons = []
        if tmax is not None and base > 0:
            reasons.append(f"最高气温 {tmax:.0f} °C")
        hot_night = tmin is not None and tmin >= hot_night_tmin_c
        heatwave = run >= HEATWAVE_MIN_DAYS
        level = base
        if base >= 1 and (hot_night or heatwave):
            level = min(base + 1, 4)
        if hot_night and base >= 1:
            reasons.append(f"热夜：最低气温 {tmin:.0f} °C ≥ {hot_night_tmin_c:.1f} °C")
        if heatwave:
            reasons.append(f"连续第 {run} 个高温日")
        results.append({
            "date": str(entry.get("forecast_date") or entry.get("date") or ""),
            "temperature_max": tmax,
            "temperature_min": tmin,
            "humidity": _to_float(entry.get("humidity")),
            "base_level": base,
            "level": level,
            "label": HAZARD_LEVELS[level]["label"],
            "hot_night": bool(hot_night),
            "hot_day_run": run,
            "escalated": level > base,
            "reasons": reasons,
        })
    return results


def combine_daily_level(hazard_level: int, static_level: int | None) -> int:
    """逐日风险矩阵：危险等级 × 静态风险等级。"""
    if not hazard_level:
        return 0
    if static_level is None:
        adjust = 0
    elif static_level <= 1:
        adjust = -1
    elif static_level == 2:
        adjust = 0
    else:
        adjust = 1
    return max(1, min(4, hazard_level + adjust))


def static_asset_path(filename: str) -> Path:
    return PROJECT_ROOT / "static" / filename


@lru_cache(maxsize=4)
def _load_workbench(mtime: float) -> dict[str, Any]:
    return json.loads(static_asset_path(WORKBENCH_FILENAME).read_text(encoding="utf-8"))


def load_workbench() -> dict[str, Any]:
    path = static_asset_path(WORKBENCH_FILENAME)
    return _load_workbench(path.stat().st_mtime)


@lru_cache(maxsize=4)
def _cell_centers(mtime: float) -> dict[str, Any]:
    collection = json.loads(static_asset_path(CELLS_GEOJSON_FILENAME).read_text(encoding="utf-8"))
    centers = {}
    for feature in collection["features"]:
        props = feature["properties"]
        if props.get("feature_type") == "modis_cell":
            centers[props["cell_id"]] = (props["center_lon_wgs84"], props["center_lat_wgs84"])
    return centers


def cell_centers() -> dict[str, Any]:
    path = static_asset_path(CELLS_GEOJSON_FILENAME)
    return _cell_centers(path.stat().st_mtime)


def locate_cell(lon: float, lat: float, max_km: float = 0.75) -> str | None:
    """返回中心点距离最近且在 max_km 内的网格。"""
    best_id, best_km = None, None
    for cell_id, (c_lon, c_lat) in cell_centers().items():
        if abs(c_lon - lon) > 0.02 or abs(c_lat - lat) > 0.02:
            continue
        km = haversine_km(lon, lat, c_lon, c_lat)
        if best_km is None or km < best_km:
            best_id, best_km = cell_id, km
    return best_id if best_km is not None and best_km <= max_km else None


def village_points(coords_gcj: dict[str, Any], community_rows: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """把站内高德坐标（GCJ-02）村点换算为 WGS84，并挂接网格风险与村档案。"""
    workbench = load_workbench()
    fields = workbench["cells"]
    index = {cell_id: i for i, cell_id in enumerate(fields["cell_id"])}
    townships = workbench["townships"]["features"]
    profiles = {getattr(row, "name", None): row for row in community_rows}
    villages = []
    for name, coords in (coords_gcj or {}).items():
        if not coords or len(coords) != 2:
            continue
        lon, lat = gcj02_to_wgs84(float(coords[0]), float(coords[1]))
        cell_id = locate_cell(lon, lat)
        i = index.get(cell_id) if cell_id else None
        profile = profiles.get(name)
        villages.append({
            "name": name,
            "lon_wgs84": round(lon, 6),
            "lat_wgs84": round(lat, 6),
            "coordinate_source": "站内高德坐标（GCJ-02）换算",
            "cell_id": cell_id,
            "township": townships[fields["township"][i]]["properties"]["name_zh"] if i is not None else None,
            "static_score": fields["score"][i] if i is not None else None,
            "static_level": fields["level"][i] if i is not None else None,
            "hotspot": fields["gi_bin"][i] if i is not None else 0,
            "facility_km": fields["facility_km"][i] if i is not None else None,
            "population": getattr(profile, "population", None) if profile else None,
            "elderly_ratio": getattr(profile, "elderly_ratio", None) if profile else None,
        })
    return villages


def rank_villages(villages: list[dict[str, Any]], day: dict[str, Any] | None, limit: int = 5) -> list[dict[str, Any]]:
    """按当日风险、静态风险分、老人数排序，生成巡访优先清单。"""
    hazard = day["level"] if day else 0
    ranked = []
    for village in villages:
        daily = combine_daily_level(hazard, village.get("static_level"))
        elderly = None
        if village.get("population") and village.get("elderly_ratio") is not None:
            elderly = round(village["population"] * village["elderly_ratio"])
        reasons = []
        if village.get("static_score") is not None:
            reasons.append(f"静态风险分 {village['static_score']:.0f}")
        if village.get("hotspot", 0) > 0:
            reasons.append("位于统计显著热点")
        if elderly:
            reasons.append(f"约 {elderly} 位老人")
        if village.get("facility_km") is not None and village["facility_km"] >= 3:
            reasons.append(f"距最近医疗点 {village['facility_km']:.1f} km")
        ranked.append({**village, "daily_level": daily, "elderly_estimate": elderly, "reasons": reasons})
    ranked.sort(key=lambda v: (
        -v["daily_level"],
        -(v.get("static_score") or 0),
        -(v.get("elderly_estimate") or 0),
        v["name"],
    ))
    return ranked[:limit] if limit else ranked


def build_daily_payload(forecast: list[dict[str, Any]], prior_hot_days: int, villages, cooling, source: str):
    workbench = load_workbench()
    threshold = workbench["metadata"]["daily"]["hot_night_tmin_c"]
    days = classify_daily_hazard(forecast, threshold, prior_hot_days)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "forecast_source": source,
        "hot_night_tmin_c": threshold,
        "days": days,
        "villages": villages,
        "priority": [
            {"date": day["date"], "villages": rank_villages(villages, day)}
            for day in days
        ],
        "cooling_resources": cooling,
        "action_cards": workbench["metadata"]["action_cards"],
    }


def _cooling_points(rows) -> list[dict[str, Any]]:
    points = []
    for row in rows:
        if row.latitude is None or row.longitude is None:
            continue
        lon, lat = gcj02_to_wgs84(float(row.longitude), float(row.latitude))
        points.append({
            "name": row.name,
            "type": row.resource_type,
            "lon_wgs84": round(lon, 6),
            "lat_wgs84": round(lat, 6),
            "open_hours": row.open_hours,
            "has_ac": bool(row.has_ac),
            "is_accessible": bool(row.is_accessible),
            "coordinate_source": "后台录入坐标，按站内高德坐标（GCJ-02）换算",
        })
    return points


def daily_payload_for_request():
    """供路由调用：读取预报、村档案与避暑点，返回逐日工作台数据。"""
    from flask import current_app

    from core.db_models import Community, CoolingResource
    from core.time_utils import today_local
    from core.weather import get_consecutive_hot_days, get_forecast_with_cache, is_demo_mode

    location = current_app.config.get("HEAT_WORKBENCH_LOCATION") or "都昌"
    forecast, _ = get_forecast_with_cache(location, days=7)
    forecast = list(forecast or [])
    prior = 0
    try:
        if not is_demo_mode():
            yesterday_run = get_consecutive_hot_days(location, target_date=today_local())
            prior = max(int(yesterday_run) - 1, 0) if yesterday_run else 0
    except Exception:
        prior = 0
    try:
        communities = Community.query.all()
    except Exception:
        communities = []
    try:
        cooling_rows = CoolingResource.query.filter_by(is_active=True).all()
    except Exception:
        cooling_rows = []
    villages = village_points(current_app.config.get("COMMUNITY_COORDS_GCJ") or {}, communities)
    source = "演示数据" if is_demo_mode() else "站内 7 天预报"
    if forecast and forecast[0].get("is_mock"):
        source = "演示数据"
    return build_daily_payload(forecast, prior, villages, _cooling_points(cooling_rows), source)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

def _versioned_static(filename: str) -> str:
    from flask import url_for

    try:
        version = int(static_asset_path(filename).stat().st_mtime)
    except OSError:
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=version)


def gis_ui_mode(request_args, config) -> str:
    """回滚开关：HEAT_EXPOSURE_GIS_UI=legacy 或 ?ui=legacy 显示旧版页面。"""
    requested = (request_args.get("ui") or "").strip().lower()
    if requested in {"legacy", "workbench"}:
        return requested
    configured = str(config.get("HEAT_EXPOSURE_GIS_UI") or "workbench").strip().lower()
    return configured if configured in {"legacy", "workbench"} else "workbench"


def render_heat_risk_workbench():
    from flask import current_app, render_template, url_for

    from services.heat_exposure_gis_service import DEFAULT_CELL_ID

    return render_template(
        "heat_risk_workbench.html",
        gis_data_url=_versioned_static(CELLS_GEOJSON_FILENAME),
        workbench_data_url=_versioned_static(WORKBENCH_FILENAME),
        daily_url=url_for("user.heat_exposure_gis_daily"),
        default_cell_id=DEFAULT_CELL_ID,
        tianditu_key=current_app.config.get("TIANDITU_TK") or "",
        legacy_url=url_for("user.heat_exposure_gis", ui="legacy"),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建都昌县热风险医生工作台数据")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="生成工作台 JSON")
    build.add_argument("--cells", type=Path, default=static_asset_path(CELLS_GEOJSON_FILENAME))
    build.add_argument("--townships", type=Path, default=TOWNSHIPS_SOURCE_PATH)
    build.add_argument("--climate", type=Path, default=CLIMATE_SOURCE_PATH)
    build.add_argument("--output", type=Path, default=static_asset_path(WORKBENCH_FILENAME))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "build":
        payload = build_workbench_data(args.cells, args.townships, args.climate, args.output)
        print(json.dumps({"output": str(args.output), **payload["metadata"]["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
