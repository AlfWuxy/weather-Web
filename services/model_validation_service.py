"""只从可核验的时间外记录生成公开成绩单，不读取原始病历。"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


SCHEMA_VERSION = 2
DEFAULT_REPORT = Path(__file__).resolve().parents[1] / "tmp" / "backtest_report.json"


def unavailable_report(reason="尚无满足来源与时间边界要求的验证数据"):
    return {
        "schema_version": SCHEMA_VERSION, "status": "unavailable", "reason": reason,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_type": "not_validated", "outcome": None,
        "test_period": {"start": None, "end": None}, "n": 0,
        "prospective_n": 0, "metrics": None, "baseline": None,
        "limitations": ["训练集拟合成绩不代表时间外预测能力", "缺测日不能自动补成零门诊"],
    }


def _metrics(rows, key):
    y = np.asarray([r["actual"] for r in rows], dtype=float)
    pred = np.asarray([r[key] for r in rows], dtype=float)
    result = {"n": len(rows), "mae": round(float(np.mean(abs(y - pred))), 4),
              "rmse": round(float(np.sqrt(np.mean((y - pred) ** 2))), 4)}
    for level in (80, 95):
        pairs = [(r["actual"], r.get(f"{key}_lower_{level}"), r.get(f"{key}_upper_{level}")) for r in rows]
        pairs = [(a, lo, hi) for a, lo, hi in pairs if lo is not None and hi is not None]
        result[f"coverage_{level}"] = round(sum(lo <= a <= hi for a, lo, hi in pairs) / len(pairs), 4) if pairs else None
        result[f"coverage_{level}_n"] = len(pairs)
    return result


def _daily_input(frame, outcome):
    required = {"date", outcome, "coverage_confirmed"}
    if not required.issubset(frame.columns):
        raise ValueError("日聚合输入必须含 date、指定结局及 coverage_confirmed；不能假设缺失为零")
    data = frame[list(required)].copy()
    data["date"] = pd.to_datetime(data["date"], errors="raise").dt.normalize()
    if data["date"].isna().any() or data["date"].dt.tz is not None:
        raise ValueError("日聚合日期须为明确的本地日期")
    if data["date"].duplicated().any():
        raise ValueError("日聚合存在重复日期")
    data[outcome] = pd.to_numeric(data[outcome], errors="coerce")
    valid = data["coverage_confirmed"].astype(str).str.lower().isin(["true", "1", "yes"])
    data.loc[~valid, outcome] = np.nan
    values = data[outcome].dropna()
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("结局必须为有限非负数")
    return data.set_index("date")[outcome].sort_index()


def _target_day(value):
    """目标日契约只接受日期，不接受可能跨日的隐含时区时间。"""
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("target_date 必须为 YYYY-MM-DD 本地日期")
    return pd.Timestamp(datetime.strptime(value, "%Y-%m-%d").date())


def rolling_origin_report(frame, *, outcome="cases_60plus", predictions=None, season=7, min_train=28):
    """逐日起点仅使用过去已确认日；模型预测须另供归档及其时间证据。"""
    if season < 1 or min_train < season * 2:
        raise ValueError("训练窗口至少覆盖两个季节周期")
    daily = _daily_input(frame, outcome)
    archive = {}
    availability = None
    if "available_at" in frame.columns:
        for value in frame["available_at"].dropna():
            if pd.Timestamp(value).tzinfo is None:
                raise ValueError("结局可用时间必须包含时区")
        availability = pd.Series(pd.to_datetime(frame["available_at"], utc=True, errors="raise").array,
                                 index=pd.to_datetime(frame["date"]).dt.normalize())
    if predictions is not None:
        if "available_at" not in frame.columns:
            raise ValueError("归档对比必须提供日结局 available_at，避免基线偷看后来录入的数据")
        required = {"target_date", "issued_at", "training_end", "inputs_available_at", "prediction", "outcome", "model_version"}
        if not required.issubset(predictions.columns):
            raise ValueError("预测归档缺少发行时间、训练截止、输入可用时间、结局或模型版本")
        for item in predictions.to_dict("records"):
            target = _target_day(item["target_date"])
            issued = pd.Timestamp(item["issued_at"])
            train_end = pd.Timestamp(item["training_end"])
            available = pd.Timestamp(item["inputs_available_at"])
            # 时间戳统一要求显式时区，目标日以预报发行时区解释。
            if any(x.tzinfo is None for x in (issued, train_end, available)):
                raise ValueError("预测证据时间必须含时区")
            issued, train_end, available = (x.tz_convert("Asia/Shanghai") for x in (issued, train_end, available))
            item["issued_at"] = issued.isoformat()
            target_cutoff = target.tz_localize("Asia/Shanghai")
            if any(pd.isna(x) for x in (issued, train_end, available)) or train_end >= issued or available > issued or issued >= target_cutoff:
                raise ValueError("预测记录含未来训练/输入，或不是事前发行")
            if item["outcome"] != outcome or pd.isna(item["model_version"]) or not str(item["model_version"]).strip():
                raise ValueError("预测结局或模型版本不匹配")
            if target in archive:
                raise ValueError("同一目标日只能评估一个固定提前期的预测")
            item["prediction"] = float(item["prediction"])
            if not math.isfinite(item["prediction"]) or item["prediction"] < 0:
                raise ValueError("预测必须为有限非负数")
            for level in (80, 95):
                lo, hi = item.get(f"lower_{level}"), item.get(f"upper_{level}")
                if pd.isna(lo) and pd.isna(hi):
                    item[f"prediction_lower_{level}"] = item[f"prediction_upper_{level}"] = None
                    continue
                if lo is None or hi is None or not all(math.isfinite(float(v)) for v in (lo, hi)) or float(lo) > float(hi):
                    raise ValueError("预测区间必须完整、有限且有序")
                item[f"prediction_lower_{level}"] = float(lo)
                item[f"prediction_upper_{level}"] = float(hi)
            archive[target] = item

        horizons = {(pd.Timestamp(r["target_date"]).date() - pd.Timestamp(r["issued_at"]).date()).days for r in archive.values()}
        if len(horizons) > 1:
            raise ValueError("不同提前天数须分别生成成绩单")

    rows = []
    for target, actual in daily.items():
        if pd.isna(actual):
            continue
        if predictions is not None:
            if target not in archive:
                continue
            issued = pd.Timestamp(archive[target]["issued_at"])
            origin = issued.tz_localize(None).normalize()
            history = daily.loc[(daily.index < origin) & (availability.reindex(daily.index) <= issued)].dropna()
        else:
            origin = target
            history = daily.loc[daily.index < origin].dropna()
            if availability is not None:
                cutoff = target.tz_localize("Asia/Shanghai")
                history = history.loc[availability.reindex(history.index) <= cutoff]
        reference = target - pd.Timedelta(days=season)
        while reference >= origin:
            reference -= pd.Timedelta(days=season)
        if len(history) < min_train or reference not in history.index:
            continue
        # 区间只用该起点之前、相隔确切日数的朴素预测残差校准。
        residuals = [float(value - history.loc[day - pd.Timedelta(days=season)])
                     for day, value in history.items() if day - pd.Timedelta(days=season) in history.index]
        if len(residuals) < season:
            continue
        row = {"date": str(target.date()), "actual": float(actual), "baseline": float(history.loc[reference])}
        for level in (80, 95):
            radius = float(np.quantile(np.abs(residuals), level / 100, method="higher"))
            row[f"baseline_lower_{level}"] = max(0, row["baseline"] - radius)
            row[f"baseline_upper_{level}"] = row["baseline"] + radius
        if predictions is not None:
            if target not in archive:
                continue
            row.update({k: v for k, v in archive[target].items() if k.startswith("prediction")})
        rows.append(row)
    if not rows:
        return unavailable_report("没有足够已确认覆盖的日聚合与事前预测；尚未完成时间外验证")
    baseline = _metrics(rows, "baseline")
    model = _metrics(rows, "prediction") if predictions is not None else None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "evaluated" if model else "baseline_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_type": "archived_out_of_time" if model else "rolling_origin_baseline",
        "availability_verified": availability is not None,
        "model_versions": sorted({str(r["model_version"]) for r in archive.values()}),
        "lead_days": sorted(horizons) if predictions is not None else [1],
        "outcome": outcome, "test_period": {"start": rows[0]["date"], "end": rows[-1]["date"]},
        "n": len(rows), "prospective_n": 0, "metrics": model,
        "baseline": {"name": f"季节朴素（{season}日）", **baseline},
        "mae_improvement_vs_baseline": round(1 - model["mae"] / baseline["mae"], 4) if model and baseline["mae"] else None,
        "limitations": ["历史回测不等于真实前瞻验证", "基线区间使用起点前绝对残差经验分位数，覆盖率须实测", "仅在结局与来源审核一致时可比较生产模型"] +
                        (["缺少结局可用时间，基线仅为假设前日已知的回顾性练习"] if availability is None else []),
    }


def _get_strict_validation_scorecard(path=None):
    """旧版全样本拟合报告不能作为新版公开成绩单。"""
    try:
        report = json.loads(Path(path or DEFAULT_REPORT).read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION or report.get("status") not in {"unavailable", "evaluated", "baseline_only"}:
            return unavailable_report("现有报告缺少时间外与基线证据，待重新生成")
        if not {"test_period", "metrics", "baseline", "n", "limitations", "evaluation_type"}.issubset(report):
            return unavailable_report("成绩单字段不完整，待重新生成")
        _validate_report(report)
        return report
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable_report()


def _validate_report(report):
    """验证公开输入的范围和组合，避免损坏报告变成虚假成绩或页面错误。"""
    def number(value, minimum=0, maximum=None):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            raise ValueError("指标超出有效范围")
    number(report["n"])
    if not isinstance(report["n"], int) or not isinstance(report["limitations"], list) or not all(isinstance(x, str) for x in report["limitations"]):
        raise ValueError("成绩单样本或局限字段无效")
    if report.get("prospective_n") != 0:
        raise ValueError("回测管线不认证前瞻样本")
    kind = {"unavailable": "not_validated", "baseline_only": "rolling_origin_baseline", "evaluated": "archived_out_of_time"}
    if report["evaluation_type"] != kind[report["status"]]:
        raise ValueError("验证类型与状态不一致")
    if report["status"] == "unavailable":
        if report["n"] != 0 or report["metrics"] is not None or report["baseline"] is not None:
            raise ValueError("未验证报告不得含成绩")
        return
    if report["n"] < 1 or not isinstance(report["test_period"], dict):
        raise ValueError("验证期缺失")
    if not isinstance(report.get("model_versions"), list) or not all(isinstance(x, str) and x for x in report["model_versions"]):
        raise ValueError("模型版本字段无效")
    if not isinstance(report.get("lead_days"), list) or len(report["lead_days"]) != 1 or not isinstance(report["lead_days"][0], int) or report["lead_days"][0] < 1:
        raise ValueError("提前期字段无效")
    if report["status"] == "evaluated" and (not report["model_versions"] or report.get("availability_verified") is not True):
        raise ValueError("预测来源或可用时间证据缺失")
    start, end = (pd.Timestamp(report["test_period"][key]) for key in ("start", "end"))
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError("验证期无效")
    if report["status"] == "baseline_only" and report["metrics"] is not None:
        raise ValueError("基线报告不得混入模型成绩")
    for item in [report["baseline"]] + ([report["metrics"]] if report["status"] == "evaluated" else []):
        if not isinstance(item, dict) or item.get("n") != report["n"]:
            raise ValueError("对比样本不一致")
        for key in ("mae", "rmse"):
            number(item[key])
        for level in (80, 95):
            count, value = item[f"coverage_{level}_n"], item[f"coverage_{level}"]
            number(count, maximum=report["n"])
            if not isinstance(count, int) or (count == 0) != (value is None):
                raise ValueError("区间覆盖样本不一致")
            if value is not None:
                number(value, maximum=1)
    if not isinstance(report["baseline"].get("name"), str):
        raise ValueError("基线名称缺失")


EXPLORATORY_REPORT = Path(__file__).resolve().parents[1] / "static" / "data" / "retrospective_exploratory.json"
EXPLORATORY_VERSION = "record-count-poisson-ridge-v1"


def _snapshot_input(frame, outcome):
    """只解释快照中的记录计数，不把零记录认证为真实零门诊。"""
    if outcome != "cases_60plus" or not {"date", outcome, "cases_all"}.issubset(frame.columns):
        raise ValueError("探索模式要求 date、cases_60plus、cases_all；不能重标年龄口径")
    data = frame[["date", outcome, "cases_all"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="raise")
    if data["date"].isna().any() or data["date"].dt.tz is not None or (data["date"] != data["date"].dt.normalize()).any():
        raise ValueError("快照必须使用无时间分量的本地日历日期")
    if data["date"].duplicated().any():
        raise ValueError("日快照存在重复日期；不自动合并")
    data = data.set_index("date").sort_index()
    for key in (outcome, "cases_all"):
        data[key] = pd.to_numeric(data[key], errors="raise")
        if data[key].isna().any() or not np.isfinite(data[key]).all() or (data[key] < 0).any() or (data[key] % 1 != 0).any():
            raise ValueError("记录计数必须是有限非负整数；缺失不能补零")
    if (data[outcome] > data["cases_all"]).any():
        raise ValueError("60岁及以上记录不得超过全部记录")
    if len(data) and len(pd.date_range(data.index.min(), data.index.max())) != len(data):
        raise ValueError("快照日期不连续；不自动补零或压缩日历")
    return data


def _snapshot_features(data, outcome):
    """每一行只使用该目标日以前的计数，星期是事前已知日历。"""
    y = data[outcome].astype(float)
    features = pd.DataFrame(index=data.index)
    for day in range(1, 7):
        features[f"weekday_{day}"] = (data.index.dayofweek == day).astype(float)
    features["log_lag7"] = np.log1p(y.shift(7))
    features["log_mean7"] = np.log1p(y.shift(1).rolling(7, min_periods=7).mean())
    features["log_mean28"] = np.log1p(y.shift(1).rolling(28, min_periods=28).mean())
    return features


def _fit_snapshot_candidate(train_x, train_y, target_x):
    """正则强度预先固定；缩放和拟合都只见本起点以前的训练行。"""
    from sklearn.linear_model import PoissonRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.exceptions import ConvergenceWarning
    import warnings
    if not np.any(train_y):
        return 0.0
    scaler = StandardScaler()
    normalized = scaler.fit_transform(train_x)
    model = PoissonRegressor(alpha=1.0, max_iter=300, tol=1e-8)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(normalized, train_y)
    value = float(model.predict(scaler.transform(target_x))[0])
    if not math.isfinite(value) or value < 0:
        raise ValueError("候选模型未产生有效预测；不可静默删除失败目标日")
    return value


def _retrospective_rows(data, outcome, min_train=84, calibration_days=28):
    """先预测和取过去残差区间，再读取目标结局以更新下一起点。"""
    if min_train < 28 or calibration_days < 20:
        raise ValueError("至少28个训练样本与20个历史滚动残差用于探索")
    x = _snapshot_features(data, outcome)
    eligible = x.dropna().index
    residuals = {"prediction": [], "baseline": []}
    rows = []
    fitted_origins = 0
    for target in eligible:
        history_dates = eligible[eligible < target]
        if len(history_dates) < min_train:
            continue
        reference = target - pd.Timedelta(days=7)
        prediction = _fit_snapshot_candidate(x.loc[history_dates], data.loc[history_dates, outcome], x.loc[[target]])
        baseline = float(data.loc[reference, outcome])
        row = {"date": str(target.date()), "prediction": prediction, "baseline": baseline,
               "training_end": str(history_dates[-1].date()), "training_n": len(history_dates),
               "calibration_n": len(residuals["prediction"])}
        for key in ("prediction", "baseline"):
            if len(residuals[key]) >= calibration_days:
                for level in (80, 95):
                    radius = float(np.quantile(np.abs(residuals[key]), level / 100, method="higher"))
                    row[f"{key}_lower_{level}"] = max(0.0, row[key] - radius)
                    row[f"{key}_upper_{level}"] = row[key] + radius
        # 目标结局不参与本次模型、缩放器、区间与超参数选择。
        actual = float(data.loc[target, outcome])
        row.update(actual=actual, has_any_record=bool(data.loc[target, "cases_all"] > 0))
        if len(residuals["prediction"]) >= calibration_days:
            rows.append(row)
        for key in residuals:
            residuals[key].append(actual - row[key])
        fitted_origins += 1
    return rows, fitted_origins


def _paired_snapshot_metrics(rows):
    if not rows:
        return {"n": 0, "test_period": {"start": None, "end": None}, "metrics": None, "baseline": None,
                "mae_improvement_vs_baseline": None}
    model, baseline = _metrics(rows, "prediction"), _metrics(rows, "baseline")
    for key, metrics in (("prediction", model), ("baseline", baseline)):
        for level in (80, 95):
            metrics[f"mean_interval_width_{level}"] = round(float(np.mean([
                row[f"{key}_upper_{level}"] - row[f"{key}_lower_{level}"] for row in rows])), 4)
    return {"n": len(rows), "test_period": {"start": rows[0]["date"], "end": rows[-1]["date"]},
            "metrics": model, "baseline": {"name": "每周季节朴素（目标日前7日记录数）", **baseline},
            "mae_improvement_vs_baseline": round(1-model["mae"]/baseline["mae"], 4) if baseline["mae"] else None}


def retrospective_exploratory_report(frame, *, outcome="cases_60plus", min_train=84, calibration_days=28, provenance=None):
    """独立的记录快照回顾性实验，不放宽真实时间外或前瞻验证门槛。"""
    import sklearn
    data = _snapshot_input(frame, outcome)
    rows, fitted_origins = _retrospective_rows(data, outcome, min_train, calibration_days)
    if not rows:
        raise ValueError("快照不足以完成训练、起点外残差校准和测试")
    paired = _paired_snapshot_metrics(rows)
    sensitivity = _paired_snapshot_metrics([row for row in rows if row["has_any_record"]])
    return {
        "schema_version": 1, "status": "exploratory_evaluated", "evaluation_type": "retrospective_exploratory",
        "generated_at": datetime.now(timezone.utc).isoformat(), "production_ready": False,
        "production_model_validated": False, "prospective_n": 0, "availability_verified": False,
        "coverage_confirmed": False, "weather_use": "none", "forecast_archive_used": False,
        "outcome": outcome, "outcome_label": "旧快照60岁及以上记录计数（未裁决零值与重复记录）",
        "headline": "这是记录计数回顾性比较，尚未验证高温健康模型",
        "candidate_name": "历史计数候选：正则化Poisson均值模型（非DLNM）", "model_version": EXPLORATORY_VERSION,
        "design": {"horizon_days_assumed": 1, "min_train_rows": min_train, "feature_warmup_days": 28,
                   "interval_calibration_origins": calibration_days, "training_window": "expanding",
                   "alpha": 1.0, "features": ["星期", "log1p(前7日计数)", "log1p(过去7日均值)", "log1p(过去28日均值)"],
                   "parameter_selection": "fixed_before_run_no_full_sample_tuning", "interval_method": "past_prequential_absolute_residual_quantile_higher",
                   "outcome_availability_assumption": "假设前一日记录已在日终可用；没有历史录入时间证据"},
        "input_summary": {"n": len(data), "start": str(data.index.min().date()), "end": str(data.index.max().date()),
                          "zero_outcome_days": int((data[outcome] == 0).sum()),
                          "no_record_days": int((data["cases_all"] == 0).sum()),
                          "coverage_unverified_days": len(data), "summer_seasons": len(set(data.index.year[data.index.month.isin([6,7,8])]))},
        "exclusions": {"feature_warmup_days": min(28, len(data)), "initial_training_rows": min_train,
                       "calibration_only_origins": min(calibration_days, fitted_origins),
                       "total_not_scored": len(data)-len(rows), "scored_no_record_days": sum(not row["has_any_record"] for row in rows)},
        "audit": {"refitted_origins": fitted_origins, "first_training_end": rows[0]["training_end"],
                  "last_training_end": rows[-1]["training_end"], "first_training_n": rows[0]["training_n"],
                  "last_training_n": rows[-1]["training_n"], "first_calibration_n": rows[0]["calibration_n"],
                  "last_calibration_n": rows[-1]["calibration_n"], "target_not_used_before_prediction": True},
        **paired,
        "sensitivity_exclude_no_record_targets": {
            "label": "敏感性：仅评分全部年龄有记录的目标日", "role": "secondary_not_model_selection",
            "excluded_target_days": paired["n"]-sensitivity["n"],
            "description": "仅改变评分目标集合；模型、训练数据、预测和区间均不变。按目标记录筛选存在选择偏倚，不代表真实覆盖或更可靠真值。", **sensitivity},
        "provenance": provenance or {}, "runtime": {"numpy": np.__version__, "pandas": pd.__version__, "scikit_learn": sklearn.__version__},
        "limitations": [
            "逐日覆盖、零值含义与结局录入时间均未核验；零只表示旧快照没有相应记录，不能证明没有就诊。",
            "源快照未去重，标签调查发现重复记录及报告结构变化；此处不擅自制定修复规则。",
            "训练与测试仅覆盖一个夏季；不同季节和不同医院的泛化能力未知。",
            "每个起点仅用历史计数重拟合，但过去日期不等于当时已可获取；未核验录入延迟。",
            "本候选不使用天气，不检验温度效应、生产DLNM或个人健康风险；不是实况天气hindcast，更不是历史预报归档。",
            "Poisson仅用来估计条件均值；区间来自历史滚动残差经验分位数，不假设计数方差等于均值，也不保证未来覆盖。",
            "本次是事后固定快照上的开发性比较，不是未触碰的最终检验集或真实前瞻试验。",
        ],
    }


def get_exploratory_scorecard(path=None):
    """只公开完整的聚合探索报告；任何不完整或冒充生产验证的文件拒绝显示。"""
    try:
        report = json.loads(Path(path or EXPLORATORY_REPORT).read_text(encoding="utf-8"))
        _validate_exploratory_report(report)
        return report
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _validate_exploratory_report(report):
    if not isinstance(report, dict) or report.get("schema_version") != 1 or report.get("status") != "exploratory_evaluated" or report.get("evaluation_type") != "retrospective_exploratory":
        raise ValueError("探索报告版本或类型不正确")
    for field in ("production_ready", "production_model_validated", "availability_verified", "coverage_confirmed", "forecast_archive_used"):
        if report.get(field) is not False:
            raise ValueError("探索报告不能冒充已核验验证")
    if report.get("weather_use") != "none" or report.get("model_version") != EXPLORATORY_VERSION or report.get("outcome") != "cases_60plus" or report.get("prospective_n") != 0:
        raise ValueError("模型、结局或天气口径不符")
    if not isinstance(report.get("limitations"), list) or len(report["limitations"]) < 5 or not all(isinstance(x, str) for x in report["limitations"]):
        raise ValueError("探索局限不完整")
    def finite(value):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    for item in (report, report["sensitivity_exclude_no_record_targets"]):
        n = item["n"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ValueError("探索样本数无效")
        if n == 0:
            if item["metrics"] is not None or item["baseline"] is not None:
                raise ValueError("零样本不能有指标")
            continue
        start, end = (pd.Timestamp(item["test_period"][key]) for key in ("start", "end"))
        if pd.isna(start) or pd.isna(end) or start > end:
            raise ValueError("探索测试期无效")
        for metrics in (item["metrics"], item["baseline"]):
            if metrics["n"] != n or not all(finite(metrics[key]) for key in ("mae", "rmse")):
                raise ValueError("探索指标或配对样本数不一致")
            for level in (80,95):
                coverage = metrics[f"coverage_{level}"]
                if metrics[f"coverage_{level}_n"] != n or not finite(coverage) or coverage > 1 or not finite(metrics[f"mean_interval_width_{level}"]):
                    raise ValueError("探索覆盖率或区间宽度无效")
            if metrics["coverage_80"] > metrics["coverage_95"] or metrics["mean_interval_width_80"] > metrics["mean_interval_width_95"]:
                raise ValueError("区间包含关系不一致")
    if report["n"] <= 0 or report["sensitivity_exclude_no_record_targets"]["n"] + report["sensitivity_exclude_no_record_targets"]["excluded_target_days"] != report["n"]:
        raise ValueError("敏感性样本数不一致")
    if not isinstance(report.get("provenance"), dict) or not report["provenance"].get("daily_sha256") or len(report["provenance"]["daily_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in report["provenance"]["daily_sha256"]):
        raise ValueError("聚合来源哈希缺失")
    for field in ("design", "input_summary", "exclusions", "audit", "runtime"):
        if not isinstance(report.get(field), dict):
            raise ValueError("探索方法元数据不完整")
    for field in ("headline", "outcome_label", "candidate_name", "generated_at"):
        if not isinstance(report.get(field), str) or not report[field].strip():
            raise ValueError("探索报告展示信息缺失")
    metadata_counts = {
        "design": ("horizon_days_assumed", "min_train_rows", "feature_warmup_days", "interval_calibration_origins"),
        "input_summary": ("n", "zero_outcome_days", "no_record_days", "coverage_unverified_days", "summer_seasons"),
        "exclusions": ("feature_warmup_days", "initial_training_rows", "calibration_only_origins", "total_not_scored", "scored_no_record_days"),
        "audit": ("refitted_origins", "first_training_n", "last_training_n", "first_calibration_n", "last_calibration_n"),
    }
    for section, fields in metadata_counts.items():
        for field in fields:
            value = report[section][field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("探索方法计数字段无效")
    summary, exclusions, design = (report[key] for key in ("input_summary", "exclusions", "design"))
    if summary["n"] != report["n"] + exclusions["total_not_scored"] or exclusions["total_not_scored"] != sum(exclusions[k] for k in ("feature_warmup_days", "initial_training_rows", "calibration_only_origins")):
        raise ValueError("评分与排除数量不一致")
    if design["horizon_days_assumed"] != 1 or design["min_train_rows"] < 28 or design["interval_calibration_origins"] < 20 or design.get("training_window") != "expanding" or design.get("alpha") != 1.0:
        raise ValueError("候选方法配置不匹配")
    for key in ("start", "end"):
        _target_day(summary[key])
    if report["audit"].get("target_not_used_before_prediction") is not True:
        raise ValueError("起点边界声明缺失")
    for key in ("numpy", "pandas", "scikit_learn"):
        if not isinstance(report["runtime"].get(key), str):
            raise ValueError("运行库版本缺失")
    sensitivity = report["sensitivity_exclude_no_record_targets"]
    if sensitivity.get("role") != "secondary_not_model_selection" or not isinstance(sensitivity.get("description"), str):
        raise ValueError("敏感性结果定位不明确")



def get_validation_scorecard(path=None):
    """严格事前归档与探索性快照结果并列，不能互相替代。"""
    report = _get_strict_validation_scorecard(path)
    report["exploratory"] = get_exploratory_scorecard() if path is None else None
    return report
