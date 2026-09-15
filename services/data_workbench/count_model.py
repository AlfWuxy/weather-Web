"""机构就诊计数模型：固定特征、可复现拟合与不执行代码的 JSON 契约。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import digamma, gammaln
from scipy.stats import nbinom

SCHEMA = "pilot.count_model.v1"
FEATURE_VERSION = "pilot.calendar_weather.v1"
OUTCOME = "daily_encounters_60plus"
FAMILIES = {"nb2_calendar": 10, "nb2_thermal": 16}
COMPLETE = {"complete", "confirmed_zero"}
MAX_ZIP_BYTES = 30 * 1024 * 1024


class ModelValidationError(ValueError):
    """不能安全使用或复现模型包。"""


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _require(condition, message):
    if not condition:
        raise ModelValidationError(message)


def _date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "日期必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ModelValidationError("日期无效") from exc


def _number(value, low=-1e9, high=1e9):
    _require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
             "数值必须有限且在允许范围内")
    return float(value)


def _keys(value, expected):
    _require(isinstance(value, dict) and set(value) == set(expected), "JSON 字段不符合固定模型契约")


def read_snapshot(source):
    """不解压到磁盘；限制体积、成员、路径并逐一校验内容哈希。"""
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    _require(len(raw) <= MAX_ZIP_BYTES, "训练包过大")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            expected = {"manifest.json", "daily.csv", "coverage.csv", "dictionary.json"}
            _require(set(names) == expected and len(names) == len(expected), "训练包成员不符合固定契约")
            _require(sum(x.file_size for x in archive.infolist()) <= MAX_ZIP_BYTES,
                     "解压后的训练包过大")
            manifest = json.loads(archive.read("manifest.json"))
            _require(isinstance(manifest, dict), "训练清单必须是 JSON 对象")
            _require(manifest.get("schema_version") == "pilot.dataset.v1", "数据版本不支持")
            _require(manifest.get("outcome") == OUTCOME and manifest.get("age_min") == 60,
                     "结局必须为 60 岁及以上日就诊人次")
            _require(manifest.get("feature_version") == FEATURE_VERSION, "数据特征版本不支持")
            _require(manifest.get("weather_product") == "era5", "训练仅支持固定 ERA5 产品")
            _require(set(manifest.get("files", {})) == expected - {"manifest.json"}, "训练包缺少文件哈希")
            for name, expected_hash in manifest["files"].items():
                if isinstance(expected_hash, dict):
                    expected_hash = expected_hash.get("sha256")
                _require(hashlib.sha256(archive.read(name)).hexdigest() == expected_hash,
                         "训练包文件哈希不匹配")
            rows = list(csv.DictReader(io.StringIO(archive.read("daily.csv").decode("utf-8-sig"))))
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, KeyError) as exc:
        raise ModelValidationError("训练包损坏") from exc
    _require(1 <= len(rows) <= 10000, "训练数据天数无效")
    required = {"date", "cases_60plus", "coverage_status", "tmean", "rh_mean", "precipitation", "weather_source", "weather_product"}
    _require(required.issubset(rows[0]), "日聚合数据列不完整")
    parsed = []
    previous = None
    for row in rows:
        day = _date(row["date"])
        _require(previous is None or day == previous + timedelta(days=1), "日历必须连续且无重复")
        previous = day
        item = dict(row)
        _require(item["coverage_status"] in COMPLETE | {"partial", "closed", "unreported"}, "报送状态无效")
        for key, low, high in (("cases_60plus", 0, 1000000), ("tmean", -90, 65),
                               ("rh_mean", 0, 100), ("precipitation", 0, 3000)):
            try:
                item[key] = None if row[key] == "" else _number(float(row[key]), low, high)
            except (ValueError, TypeError) as exc:
                raise ModelValidationError("日聚合数值无效") from exc
        if item["cases_60plus"] is not None:
            _require(item["cases_60plus"].is_integer(), "就诊人次必须为整数")
            _require(item["coverage_status"] in COMPLETE, "非完整报送不能作为已知就诊量")
        if item["tmean"] is not None:
            _require(item["weather_product"] == manifest["weather_product"], "日天气与固定产品不一致")
        parsed.append(item)
    _require(manifest.get("date_start") == rows[0]["date"] and manifest.get("date_end") == rows[-1]["date"],
             "清单日期与实际日历不一致")
    _require(manifest.get("cutoff_date") == rows[-1]["date"], "截止日期与日历不一致")
    for key in ("dataset_id", "institution_id", "region_code"):
        _require(isinstance(manifest.get(key), str) and 0 < len(manifest[key]) <= 100, "数据身份信息缺失")
    return manifest, parsed, hashlib.sha256(raw).hexdigest()


def split_periods(start, end):
    first, last = _date(start), _date(end)
    holdout_start = last - timedelta(days=89)
    development_start = holdout_start - timedelta(days=90)
    _require(first < development_start, "至少需要 181 天日历才能划分训练与两段评估")
    return {"train": {"start": first.isoformat(), "end": (development_start - timedelta(days=1)).isoformat()},
            "development": {"start": development_start.isoformat(), "end": (holdout_start - timedelta(days=1)).isoformat()},
            "holdout": {"start": holdout_start.isoformat(), "end": last.isoformat()}}


def feature_row(model, row):
    """温度数组严格按当日、前 1 日至前 7 日排列，病例不进入特征。"""
    day = _date(row["date"])
    params = model["feature_params"]
    days = (day - _date(params["reference_date"])).days
    annual = 2 * math.pi * (day - date(2000, 1, 1)).days / 365.2425
    features = [1.0] + [float(day.weekday() == x) for x in range(1, 7)]
    features += [math.sin(annual), math.cos(annual), days / 365.2425]
    if model["family"] == "nb2_thermal":
        values = row.get("temperature_history")
        _require(isinstance(values, list) and len(values) == 8, "温度候选需要当日及前 7 日温度")
        values = [_number(x, -90, 65) for x in values]
        for temperature in (values[0], sum(values[1:4]) / 3, sum(values[4:8]) / 4):
            features.extend([max(0.0, params["temperature_p10"] - temperature) / 10,
                             max(0.0, temperature - params["temperature_p90"]) / 10])
    return np.array(features, dtype=float)


def validate_parameters(model):
    _keys(model, {"family", "feature_version", "coefficients", "alpha", "feature_params"})
    _require(isinstance(model["family"], str) and model["family"] in FAMILIES
             and model["feature_version"] == FEATURE_VERSION, "模型类型或特征版本不支持")
    beta = model["coefficients"]
    _require(isinstance(beta, list) and len(beta) == FAMILIES[model["family"]], "模型参数维度不符")
    for value in beta:
        _number(value, -30, 30)
    _number(model["alpha"], 0.000001, 1000)
    params = model["feature_params"]
    _keys(params, {"reference_date", "temperature_p10", "temperature_p90"})
    _date(params["reference_date"])
    _number(params["temperature_p10"], -90, 65)
    _number(params["temperature_p90"], -90, 65)
    _require(params["temperature_p10"] <= params["temperature_p90"], "温度分位数次序错误")


def predict_daily(model, rows):
    validate_parameters(model)
    results = []
    for row in rows:
        eta = float(feature_row(model, row) @ np.array(model["coefficients"]))
        _require(-30 <= eta <= 20, "预测超出可支持数值范围")
        mu = math.exp(eta)
        size = 1 / model["alpha"]
        lo, hi = nbinom.ppf([0.025, 0.975], size, size / (size + mu))
        results.append({"date": row["date"], "mean": mu,
                        "lower_95": float(lo), "upper_95": float(hi)})
    return results


def _fit(family, params, rows):
    model = {"family": family, "feature_version": FEATURE_VERSION,
             "feature_params": params, "coefficients": [], "alpha": 1.0}
    matrix = np.vstack([feature_row(model, row) for row in rows])
    observed = np.array([row["cases_60plus"] for row in rows])
    _require(len(rows) >= max(30, matrix.shape[1] * 2), "有效训练日不足")
    _require(float(observed.sum()) > 0, "训练期全部为零，不能拟合计数模型")
    initial = np.zeros(matrix.shape[1] + 1)
    initial[0] = math.log(float(observed.mean()))
    initial[-1] = math.log(max(0.01, (float(observed.var()) - float(observed.mean())) / float(observed.mean()) ** 2))
    initial[-1] = float(np.clip(initial[-1], -13, 6))

    def objective(theta):
        eta = matrix @ theta[:-1]
        clipped = np.clip(eta, -30, 20)
        mu = np.exp(clipped)
        alpha = math.exp(theta[-1])
        size = 1 / alpha
        likelihood = (gammaln(observed + size) - gammaln(size) - gammaln(observed + 1)
                      + size * math.log(size) + observed * clipped - (observed + size) * np.log(size + mu))
        beta_gradient = matrix.T @ (((observed - mu) / (1 + alpha * mu)) * ((eta > -30) & (eta < 20)))
        size_gradient = (digamma(observed + size) - digamma(size) + math.log(size) + 1
                         - np.log(size + mu) - (size + observed) / (size + mu))
        gradient = np.concatenate([beta_gradient, [-size * float(size_gradient.sum())]])
        return -float(likelihood.mean()), -gradient / len(rows)

    fitted = minimize(objective, initial, jac=True, method="L-BFGS-B",
                      bounds=[(-15, 15)] * matrix.shape[1] + [(math.log(0.000001), math.log(1000))],
                      options={"maxiter": 2000, "ftol": 1e-11, "gtol": 1e-7})
    _require(fitted.success and np.isfinite(fitted.fun), "负二项拟合未收敛")
    model["coefficients"] = [float(x) for x in fitted.x[:-1]]
    model["alpha"] = math.exp(float(fitted.x[-1]))
    validate_parameters(model)
    return model


def _usable_rows(rows):
    by_day = {row["date"]: row for row in rows}
    usable = []
    for row in rows:
        if row["coverage_status"] not in COMPLETE or row["cases_60plus"] is None:
            continue
        day = _date(row["date"])
        history = [by_day.get((day - timedelta(days=i)).isoformat(), {}).get("tmean") for i in range(8)]
        if any(x is None for x in history):
            continue
        usable.append({**row, "temperature_history": history})
    return usable


def metrics_for(model, rows):
    predictions = predict_daily(model, rows)
    observed = np.array([row["cases_60plus"] for row in rows])
    mu = np.array([row["mean"] for row in predictions])
    _require(len(rows) > 0, "评估窗口无完整数据")
    size = 1 / model["alpha"]
    logp = nbinom.logpmf(observed, size, size / (size + mu))
    by_day = {row["date"]: (row["cases_60plus"], pred["mean"]) for row, pred in zip(rows, predictions)}
    seven_errors = []
    # 同一 split 内完整连续 7 日才计算累计误差，绝不跨边界拼接标签。
    for row in rows:
        first = _date(row["date"])
        values = [by_day.get((first + timedelta(days=i)).isoformat()) for i in range(7)]
        if all(value is not None for value in values):
            seven_errors.append(abs(sum(value[0] for value in values) - sum(value[1] for value in values)))
    return {"n_days": len(rows), "mae": float(np.abs(observed - mu).mean()),
            "rmse": float(np.sqrt(np.square(observed - mu).mean())), "mean_nll": float(-logp.mean()),
            "daily_interval_coverage": float(np.mean([p["lower_95"] <= y <= p["upper_95"] for y, p in zip(observed, predictions)])),
            "seven_day_windows": len(seven_errors),
            "seven_day_mae": float(np.mean(seven_errors)) if seven_errors else None}


def train_snapshot(source, name=None):
    manifest, rows, snapshot_hash = read_snapshot(source)
    periods = split_periods(rows[0]["date"], rows[-1]["date"])
    usable = _usable_rows(rows)
    splits = {key: [row for row in usable if period["start"] <= row["date"] <= period["end"]]
              for key, period in periods.items()}
    _require(len(splits["development"]) >= 14 and len(splits["holdout"]) >= 14, "开发与保留评估各需至少 14 个完整日")
    temperatures = [row["tmean"] for row in splits["train"]]
    _require(temperatures, "训练期无有效天气")
    p10, p90 = np.quantile(temperatures, [0.1, 0.9])
    params = {"reference_date": periods["train"]["start"], "temperature_p10": float(p10), "temperature_p90": float(p90)}
    fitted = {family: _fit(family, params, splits["train"]) for family in FAMILIES}
    development = {family: metrics_for(model, splits["development"]) for family, model in fitted.items()}
    # 仅开发集选择；平分时采用参数较少的日历基线。保留集不能参与选择或重新拟合。
    selected = min(FAMILIES, key=lambda family: (development[family]["mean_nll"], FAMILIES[family]))
    holdout = {family: metrics_for(model, splits["holdout"]) for family, model in fitted.items()}
    train_days = (_date(periods["train"]["end"]) - _date(periods["train"]["start"])).days + 1
    exploratory = train_days < 365 or len(splits["train"]) < 365
    golden_rows = [{"date": row["date"], "temperature_history": row["temperature_history"]}
                   for row in splits["holdout"][:7]]
    selected_model = fitted[selected]
    artifact = {
        "schema_version": SCHEMA, "name": name or "机构计数模型候选",
        "institution_id": manifest["institution_id"], "region_code": manifest["region_code"],
        "dataset_id": manifest["dataset_id"], "dataset_sha256": snapshot_hash,
        "outcome": OUTCOME, "age_min": 60, "feature_version": FEATURE_VERSION,
        "periods": periods, "training_days": len(splits["train"]), "exploratory": exploratory,
        "evaluation_kind": "retrospective_observed_weather",
        "reporting_time_evidence": "historical_availability_unverified",
        "delay_replay_performed": False,
        "selection_rule": "development_mean_nll_then_simpler_v1",
        "weather_product": manifest["weather_product"],
        "model": selected_model, "baseline_model": fitted["nb2_calendar"],
        "thermal_model": fitted["nb2_thermal"],
        "metrics": {"development": development, "holdout": holdout},
        "golden": [{"input": row, "mean": pred["mean"]}
                   for row, pred in zip(golden_rows, predict_daily(selected_model, golden_rows))],
        "code_version": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    validate_bundle(artifact, manifest=manifest, dataset_sha256=snapshot_hash, snapshot_rows=rows)
    return artifact


def validate_bundle(payload, manifest=None, dataset_sha256=None, snapshot_rows=None):
    """所有接受字段均固定；服务端进一步绑定冻结快照并重算计数、指标和金样本。"""
    _keys(payload, {"schema_version", "name", "institution_id", "region_code", "dataset_id", "dataset_sha256",
                    "outcome", "age_min", "feature_version", "periods", "training_days", "exploratory",
                    "evaluation_kind", "selection_rule", "weather_product", "model", "baseline_model", "thermal_model",
                    "metrics", "golden", "code_version", "reporting_time_evidence", "delay_replay_performed"})
    _require(len(canonical_bytes(payload)) <= 2 * 1024 * 1024, "模型包过大")
    _require(payload["schema_version"] == SCHEMA and payload["feature_version"] == FEATURE_VERSION, "模型版本不支持")
    _require(payload["outcome"] == OUTCOME and type(payload["age_min"]) is int and payload["age_min"] == 60, "不能将风险比用作就诊计数模型")
    for key in ("institution_id", "region_code", "dataset_id", "name", "weather_product"):
        _require(isinstance(payload[key], str) and 0 < len(payload[key]) <= 120, "模型身份字段无效")
    for key in ("dataset_sha256", "code_version"):
        _require(isinstance(payload[key], str) and re.fullmatch(r"[a-f0-9]{64}", payload[key]), "模型哈希无效")
    _require(payload["evaluation_kind"] == "retrospective_observed_weather", "历史实况评估不能标为前瞻评估")
    _require(payload["reporting_time_evidence"] == "historical_availability_unverified"
             and payload["delay_replay_performed"] is False, "不能声称已验证历史报送延迟")
    _require(payload["weather_product"] == "era5", "模型训练天气产品不支持")
    _require(payload["selection_rule"] == "development_mean_nll_then_simpler_v1", "模型选择方法不支持")
    _require(type(payload["training_days"]) is int and 30 <= payload["training_days"] <= 10000, "训练天数无效")
    _require(type(payload["exploratory"]) is bool, "探索状态无效")
    _keys(payload["periods"], {"train", "development", "holdout"})
    for period in payload["periods"].values():
        _keys(period, {"start", "end"})
        _require(_date(period["start"]) <= _date(period["end"]), "评估日期顺序无效")
    expected_periods = split_periods(payload["periods"]["train"]["start"], payload["periods"]["holdout"]["end"])
    _require(payload["periods"] == expected_periods, "评估必须冻结最新两个 90 天窗口")
    span = (_date(expected_periods["train"]["end"]) - _date(expected_periods["train"]["start"])).days + 1
    _require(payload["exploratory"] == (span < 365 or payload["training_days"] < 365), "探索状态与训练范围矛盾")
    validate_parameters(payload["model"])
    validate_parameters(payload["baseline_model"])
    validate_parameters(payload["thermal_model"])
    _require(payload["baseline_model"]["family"] == "nb2_calendar", "对照必须为日历计数基线")
    _require(payload["thermal_model"]["family"] == "nb2_thermal", "温度候选类型不符")
    for model in (payload["model"], payload["baseline_model"], payload["thermal_model"]):
        _require(model["feature_params"]["reference_date"] == expected_periods["train"]["start"], "特征时间基准与训练期不符")
    _keys(payload["metrics"], {"development", "holdout"})
    for metrics in payload["metrics"].values():
        _keys(metrics, FAMILIES)
        supports = []
        for metric in metrics.values():
            _keys(metric, {"n_days", "mae", "rmse", "mean_nll", "daily_interval_coverage", "seven_day_windows", "seven_day_mae"})
            _require(type(metric["n_days"]) is int and 14 <= metric["n_days"] <= 90, "评估有效日无效")
            supports.append(metric["n_days"])
            for key in ("mae", "rmse", "mean_nll"):
                _number(metric[key], 0, 1e9)
            _number(metric["daily_interval_coverage"], 0, 1)
            _require(type(metric["seven_day_windows"]) is int and 0 <= metric["seven_day_windows"] <= 84, "七日标签跨越评估边界")
            _require((metric["seven_day_mae"] is None) == (metric["seven_day_windows"] == 0), "七日指标缺失状态矛盾")
            if metric["seven_day_mae"] is not None:
                _number(metric["seven_day_mae"], 0, 1e9)
        _require(len(set(supports)) == 1, "模型对照必须使用相同评估日")
    selected = min(FAMILIES, key=lambda family: (payload["metrics"]["development"][family]["mean_nll"], FAMILIES[family]))
    _require(payload["model"]["family"] == selected, "候选模型与开发集选择不一致")
    _require(payload["model"] == payload["baseline_model" if selected == "nb2_calendar" else "thermal_model"], "所选参数与对照参数不一致")
    golden = payload["golden"]
    _require(isinstance(golden, list) and len(golden) == 7, "需要七个预测一致性样例")
    for sample in golden:
        _keys(sample, {"input", "mean"})
        _keys(sample["input"], {"date", "temperature_history"})
        _date(sample["input"]["date"])
        _require(isinstance(sample["input"]["temperature_history"], list) and len(sample["input"]["temperature_history"]) == 8, "金样本温度历史无效")
        for temperature in sample["input"]["temperature_history"]:
            _number(temperature, -90, 65)
        _number(sample["mean"], 0, math.exp(20))
        actual = predict_daily(payload["model"], [sample["input"]])[0]["mean"]
        _require(math.isclose(actual, sample["mean"], rel_tol=1e-9, abs_tol=1e-9), "本地与服务器预测不一致")
    if manifest is not None:
        for key in ("institution_id", "region_code", "dataset_id", "age_min", "outcome", "feature_version", "weather_product"):
            _require(payload[key] == manifest.get(key), "模型与机构快照身份或天气产品不匹配")
        _require(expected_periods == split_periods(manifest["date_start"], manifest["cutoff_date"]), "模型日期与冻结快照不匹配")
    if dataset_sha256 is not None:
        _require(payload["dataset_sha256"] == dataset_sha256, "模型与数据快照哈希不匹配")
    if snapshot_rows is not None:
        usable = _usable_rows(snapshot_rows)
        split_rows = {key: [row for row in usable if period["start"] <= row["date"] <= period["end"]]
                      for key, period in expected_periods.items()}
        _require(payload["training_days"] == len(split_rows["train"]), "训练日数与数据不匹配")
        p10, p90 = np.quantile([row["tmean"] for row in split_rows["train"]], [0.1, 0.9])
        for model in (payload["baseline_model"], payload["thermal_model"]):
            _require(math.isclose(model["feature_params"]["temperature_p10"], p10, abs_tol=1e-9)
                     and math.isclose(model["feature_params"]["temperature_p90"], p90, abs_tol=1e-9), "温度分位数必须仅由训练期确定")
            for split in ("development", "holdout"):
                computed = metrics_for(model, split_rows[split])
                provided = payload["metrics"][split][model["family"]]
                for key, value in computed.items():
                    _require(provided[key] is None if value is None else provided[key] is not None and math.isclose(value, provided[key], rel_tol=1e-8, abs_tol=1e-8), "评估指标与冻结数据重算不一致")
        expected_golden = [{"date": row["date"], "temperature_history": row["temperature_history"]} for row in split_rows["holdout"][:7]]
        _require([sample["input"] for sample in golden] == expected_golden, "金样本未绑定冻结数据")
    return {"valid": True, "golden_passed": True, "snapshot_verified": snapshot_rows is not None,
            "eligible_for_activation": not payload["exploratory"],
            "evaluation_kind": payload["evaluation_kind"]}
