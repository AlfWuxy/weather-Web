# -*- coding: utf-8 -*-
"""Walk-forward backtest for ForecastService (offline; no external APIs).

Outputs: tmp/backtest_report.json
"""

import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from services.forecast_service import ForecastService


def _load_daily_visits():
    base_dir = Path(__file__).resolve().parents[1]
    env_path = os.getenv("MEDICAL_DATA_PATH")
    data_path = Path(env_path) if env_path else (base_dir / "data" / "research" / "数据.xlsx")
    if not data_path.exists():
        raise FileNotFoundError(f"medical data not found: {data_path}")

    df = pd.read_excel(data_path, header=None, usecols=[5])
    df.columns = ["visit_time"]
    df["visit_time"] = pd.to_datetime(df["visit_time"])
    df["date"] = df["visit_time"].dt.date
    daily = df.groupby("date").size().sort_index()
    return daily


def backtest(max_days=None):
    svc = ForecastService()
    daily_visits = _load_daily_visits()

    if svc.weather_history is None or svc.weather_history.empty:
        raise RuntimeError("weather history is empty; cannot backtest")

    weather = svc.weather_history.copy()
    weather["date_only"] = weather["date"].dt.date
    weather = weather.dropna(subset=["tmean"])

    # Join on date
    merged = pd.DataFrame({"date": weather["date_only"], "tmean": weather["tmean"]})
    merged = merged.drop_duplicates(subset=["date"]).set_index("date").sort_index()
    merged["actual"] = daily_visits
    merged = merged.dropna(subset=["actual"]).sort_index()

    if max_days:
        merged = merged.tail(int(max_days))

    records = []
    for date, row in merged.iterrows():
        tmean = float(row["tmean"])
        actual = float(row["actual"])
        lag, _ = svc.get_lag_temperature_profile(pd.to_datetime(date))
        pred = svc.predict_daily_visits(
            temperature=tmean,
            lag_temps=lag,
            month=pd.to_datetime(date).month,
            dow=pd.to_datetime(date).weekday(),
        )
        records.append(
            {
                "date": str(date),
                "tmean": tmean,
                "actual": actual,
                "pred": pred.get("point_estimate"),
                "lower": pred.get("lower_bound"),
                "upper": pred.get("upper_bound"),
            }
        )

    if not records:
        raise RuntimeError("no overlapping dates between weather and visits")

    y = np.array([r["actual"] for r in records], dtype=float)
    yhat = np.array([r["pred"] for r in records], dtype=float)
    mae = float(np.mean(np.abs(y - yhat)))
    rmse = float(np.sqrt(np.mean((y - yhat) ** 2)))

    cover = []
    for r in records:
        lo = r["lower"]
        hi = r["upper"]
        if lo is None or hi is None:
            continue
        cover.append(1.0 if (float(lo) <= float(r["actual"]) <= float(hi)) else 0.0)
    coverage = float(np.mean(cover)) if cover else None

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_days": len(records),
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "interval_coverage": (round(coverage, 4) if coverage is not None else None),
        "guardrail": {
            "max_observed_daily_visits": getattr(svc, "max_observed_daily_visits", None),
            "cap_multiple": 2.0,
        },
        "sample": records[-14:],  # last 14 days for quick inspection
    }

    out_path = Path(__file__).resolve().parents[1] / "tmp" / "backtest_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, report


def main():
    max_days = os.getenv("BACKTEST_MAX_DAYS")
    max_days = int(max_days) if max_days and str(max_days).strip() else None
    path, report = backtest(max_days=max_days)
    print(f"Backtest saved: {path}")
    print(f"MAE={report['mae']} RMSE={report['rmse']} n={report['n_days']}")


if __name__ == "__main__":
    main()

