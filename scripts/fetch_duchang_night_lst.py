# -*- coding: utf-8 -*-
"""从 Microsoft Planetary Computer 读取 Aqua MYD11A1 夜间地表温度，生成都昌网格夏季均值。

数据源：Planetary Computer 的 modis-11A1-061 集合（NASA MYD11A1 v061 的 COG 镜像），
匿名访问，只需按 Planetary Computer 规则申请短期 SAS 令牌，无需 NASA Earthdata 账号。

口径与现有白天图层一致：
- 研究期：2020 至 2024 年每年 6 月 1 日至 8 月 31 日；
- 质量筛选（Q3）：QC 的 mandatory QA 位（bit 0-1）为 00 或 01，LST 误差位（bit 6-7）为 00 或 01
  （误差 ≤ 2 K），且原始编码有效。该口径用 --check-day 对白天数据复现已发布图层，
  2,593 格的合格天数与均值逐格完全一致；
- 换算：LST_C = Raw × 0.02 − 273.15；
- 空间单位：MODIS h28v06 原生网格，按 cell_id 中的 0-based 行列直接取像元。

依赖（只在离线构建时需要，不进入网站运行环境）：
    pip install rasterio requests

用法：
    python scripts/fetch_duchang_night_lst.py \
        --output data/gis/duchang_night_lst_myd11a1.json \
        --check-day   # 可选：同口径重算白天均值并与已发布图层比对
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
COLLECTION = "modis-11A1-061"
PROBE_POINT = [116.25, 29.3]  # 都昌县内一点，用于检索 h28v06 瓦片
TILE = (28, 6)
MODIS_PIXEL_M = 926.6254331391661
MODIS_X_ORIGIN_M = 11_119_505.197665
MODIS_Y_ORIGIN_M = 3_335_851.5593
VALID_MIN_RAW = 7500  # MOD11 用户指南的有效范围下限（150 K）

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CELLS = PROJECT_ROOT / "static/data/gis/duchang_heat_exposure_cells.geojson"


class SasSigner:
    """缓存 Planetary Computer SAS 令牌，过期前自动刷新。"""

    def __init__(self, session: requests.Session):
        self.session = session
        self.tokens: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def sign(self, href: str) -> str:
        account = href.split("//", 1)[1].split(".", 1)[0]
        container = href.split(".net/", 1)[1].split("/", 1)[0]
        key = f"{account}/{container}"
        with self.lock:
            token, expires = self.tokens.get(key, ("", 0.0))
            if time.time() > expires - 300:
                response = self.session.get(SAS_TOKEN_URL.format(account=account, container=container), timeout=30)
                response.raise_for_status()
                payload = response.json()
                token = payload["token"]
                expiry = datetime.fromisoformat(payload["msft:expiry"].replace("Z", "+00:00"))
                self.tokens[key] = (token, expiry.timestamp())
        return f"{href}?{token}"


def search_items(session: requests.Session, start: str, end: str) -> list[dict]:
    """检索研究期内 Aqua（MYD11A1）h28v06 的全部日产品。"""
    body = {
        "collections": [COLLECTION],
        "intersects": {"type": "Point", "coordinates": PROBE_POINT},
        "datetime": f"{start}/{end}",
        "limit": 100,
    }
    items, url, payload = [], STAC_SEARCH, body
    while url:
        response = session.post(url, json=payload, timeout=60)
        response.raise_for_status()
        page = response.json()
        for item in page["features"]:
            props = item["properties"]
            # 部分年份条目缺少 platform 字段，按产品编号前缀识别 Aqua（MYD）。
            if item["id"].startswith("MYD11A1.") and (props.get("modis:horizontal-tile"), props.get("modis:vertical-tile")) == TILE:
                items.append(item)
        next_link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not next_link:
            break
        url = next_link["href"]
        payload = next_link.get("body", payload)
    return items


def item_date(item: dict) -> str:
    props = item["properties"]
    return (props.get("datetime") or props.get("start_datetime") or "")[:10]


def summer_ranges(first_year: int, last_year: int) -> list[tuple[str, str]]:
    return [(f"{year}-06-01T00:00:00Z", f"{year}-08-31T23:59:59Z") for year in range(first_year, last_year + 1)]


def read_window(href: str, window) -> tuple[np.ndarray, tuple]:
    import rasterio

    with rasterio.open(href) as dataset:
        return dataset.read(1, window=window), tuple(dataset.transform)[:6]


def q3_lst(raw: np.ndarray, qc: np.ndarray) -> np.ndarray:
    """Q3：mandatory QA 为 00/01、LST 误差 ≤ 2 K 且原始编码有效；返回摄氏度，不合格为 NaN。"""
    raw = raw.astype(np.float64)
    good = ((qc & 0b11) <= 1) & (((qc >> 6) & 0b11) <= 1) & (raw >= VALID_MIN_RAW)
    return np.where(good, raw * 0.02 - 273.15, np.nan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cells", type=Path, default=DEFAULT_CELLS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-year", type=int, default=2020)
    parser.add_argument("--last-year", type=int, default=2024)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--check-day", action="store_true", help="同口径重算白天均值并与已发布图层比对")
    args = parser.parse_args()

    from rasterio.windows import Window

    collection = json.loads(args.cells.read_text(encoding="utf-8"))
    cells = [f["properties"] for f in collection["features"] if f["properties"]["feature_type"] == "modis_cell"]
    rows = np.array([c["modis_row_0based"] for c in cells])
    cols = np.array([c["modis_col_0based"] for c in cells])
    row0, col0 = int(rows.min()), int(cols.min())
    window = Window(col0, row0, int(cols.max()) - col0 + 1, int(rows.max()) - row0 + 1)
    r_idx, c_idx = rows - row0, cols - col0

    session = requests.Session()
    signer = SasSigner(session)
    items = []
    for start, end in summer_ranges(args.first_year, args.last_year):
        items.extend(search_items(session, start, end))
    items.sort(key=item_date)
    print(f"检索到 Aqua h28v06 日产品 {len(items)} 个", file=sys.stderr)

    expected_transform = (MODIS_PIXEL_M, 0.0, MODIS_X_ORIGIN_M, 0.0, -MODIS_PIXEL_M, MODIS_Y_ORIGIN_M)

    def process(item):
        assets = item["assets"]
        bands = {"night": ("LST_Night_1km", "QC_Night")}
        if args.check_day:
            bands["day"] = ("LST_Day_1km", "QC_Day")
        out = {}
        for key, (lst_name, qc_name) in bands.items():
            for attempt in range(4):
                try:
                    raw, transform = read_window(signer.sign(assets[lst_name]["href"]), window)
                    qc, _ = read_window(signer.sign(assets[qc_name]["href"]), window)
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            if not all(math.isclose(a, b, rel_tol=0, abs_tol=1e-3) for a, b in zip(transform, expected_transform)):
                raise ValueError(f"{item['id']} 网格仿射参数与 MODIS 原生网格不一致：{transform}")
            out[key] = q3_lst(raw, qc)[r_idx, c_idx]
        return item["id"], item_date(item), out

    sums = {"night": np.zeros(len(cells)), "day": np.zeros(len(cells))}
    counts = {"night": np.zeros(len(cells), dtype=int), "day": np.zeros(len(cells), dtype=int)}
    granules = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, item) for item in items]
        for done, future in enumerate(as_completed(futures), 1):
            granule_id, date, values = future.result()
            granules.append({"id": granule_id, "date": date})
            for key, arr in values.items():
                ok = ~np.isnan(arr)
                sums[key][ok] += arr[ok]
                counts[key][ok] += 1
            if done % 50 == 0:
                print(f"  已处理 {done}/{len(items)}", file=sys.stderr)
    granules.sort(key=lambda g: g["date"])

    def mean(key):
        with np.errstate(invalid="ignore", divide="ignore"):
            values = sums[key] / counts[key]
        return [None if counts[key][i] == 0 else round(float(values[i]), 4) for i in range(len(cells))]

    night_mean = mean("night")
    payload = {
        "metadata": {
            "title": "都昌县 MYD11A1 夜间地表温度夏季均值",
            "product": "NASA Aqua MODIS MYD11A1 v061, LST_Night_1km + QC_Night",
            "doi": "10.5067/MODIS/MYD11A1.061",
            "access": "Microsoft Planetary Computer, collection modis-11A1-061 (COG mirror of LP DAAC)",
            "study_period": f"{args.first_year}-{args.last_year}, 每年 6 月 1 日至 8 月 31 日",
            "q3_rule": "QC_Night mandatory QA (bit 0-1) 为 00 或 01，LST 误差 (bit 6-7) ≤ 2 K，且原始编码 ≥ 7500",
            "day_reproduction": "同一脚本 --check-day 按相同口径重算白天，与已发布白天图层逐格一致",
            "conversion": "LST_C = Raw × 0.02 − 273.15",
            "overpass": "Aqua 夜间过境约当地时间 01:30",
            "granules": len(granules),
            "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "script": "scripts/fetch_duchang_night_lst.py",
        },
        "cells": {
            "cell_id": [c["cell_id"] for c in cells],
            "q3_night_lst_c_mean": night_mean,
            "q3_night_dates": counts["night"].tolist(),
        },
        "granules": granules,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    valid = [v for v in night_mean if v is not None]
    print(json.dumps({
        "output": str(args.output),
        "granules": len(granules),
        "cells_with_night_value": len(valid),
        "night_mean_range_c": [min(valid), max(valid)] if valid else None,
        "median_q3_night_dates": int(np.median(counts["night"])),
    }, ensure_ascii=False))

    if args.check_day:
        day_mean = mean("day")
        published = [c.get("q3_lst_c_mean") for c in cells]
        pairs = [(a, b) for a, b in zip(day_mean, published) if a is not None and b is not None]
        diffs = np.array([a - b for a, b in pairs])
        corr = float(np.corrcoef([a for a, _ in pairs], [b for _, b in pairs])[0, 1])
        published_dates = np.array([c.get("q3_dates") for c in cells])
        print(json.dumps({
            "day_q3_dates_exact_match_share": round(float((counts["day"] == published_dates).mean()), 4),
            "day_q3_dates_mean_diff": round(float((counts["day"] - published_dates).mean()), 3),
            "day_check_cells": len(pairs),
            "day_vs_published_corr": round(corr, 5),
            "day_vs_published_mean_abs_diff_c": round(float(np.abs(diffs).mean()), 4),
            "day_vs_published_max_abs_diff_c": round(float(np.abs(diffs).max()), 4),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
