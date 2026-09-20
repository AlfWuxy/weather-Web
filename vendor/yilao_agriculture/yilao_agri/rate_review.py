"""描述性速率复盘：逐例报告误差与来源；不收紧保守范围，不产生校准概率区间。

本模块只做**描述性**汇总：次数、个案、来源归属、逐例误差符号与幅度、
慢端低估。它**不含**标准误、置信/预测区间、显著性检验或区间覆盖等推断统计。
请求这类推断统计会走关闭的错误路径（`InputError`），不返回任何伪区间。

口径来自已冻结的耗时误差协议（`benchmark/evaluation_plan.json`，R03-A02）：
- 点预测使用排程较慢端 ``yhat = max(1, ceil(q_obs * rate_high - 1e-8))`` 分钟；
- 符号误差 ``e = yhat - y``（`e < 0` 表示预测偏短、实际更慢）；
- ``MAE = mean(|e|)``，``bias = mean(e)``；
- 慢端低估 = 单位速率 ``rho = y / q`` 位于或高于类型 7 的 75% 分位子集上 ``mean(max(0, y - yhat))``，
  分析集 ``n >= 4`` 且慢端 ``n >= 2`` 才给值，否则为 ``null``。

只报告**已核实口径**：仅当任务速率来源非占位词且口径为 ``net_work`` 时，
才把逐例误差与 MAE/bias 当作对已核实参考范围的描述；否则误差一律 ``null`` 并写明原因。
复盘**不会**修改任务速率，也不会因少量快记录收紧保守慢端。
"""

from __future__ import annotations

import math

from .models import InputError, _number, is_placeholder_source
from .workload import canonical_quantity, estimate_task, review_observations

EPS = 1e-8
SLOW_TAIL_P = 0.75
MIN_TAIL_ANALYSIS_N = 4
MIN_TAIL_N = 2

# 复盘只接受净作业口径；整次出工经验先拆清再入复盘。
VERIFIED_RATE_SCOPE = "net_work"

# 固定可用的**描述性**统计键；其余（尤其推断统计）一律拒绝。
DESCRIPTIVE_STATISTIC_KEYS = frozenset({
    "count", "cases", "min", "max", "mean", "bias_sign", "sources",
    "mae", "bias", "slow_tail_underestimate",
})

# 明确识别并拒绝的推断统计 / 校准概率口径键（说明用，不改变拒绝行为）。
INFERENTIAL_STATISTIC_KEYS = frozenset({
    "confidence_interval", "confidence_interval_95", "ci", "ci95",
    "standard_error", "stderr", "std_err", "standard_deviation",
    "p_value", "significance", "hypothesis_test", "t_test", "z_test",
    "calibrated_probability", "calibrated_interval", "probability_interval",
    "prediction_interval", "interval_coverage", "nominal_coverage",
    "quantile_regression", "bootstrap_ci", "credible_interval", "coverage_probability",
})

_INFERENCE_NOTE = ("仅描述性口径：不含标准误、置信/预测区间、显著性检验或区间覆盖；"
                   "不称已校准概率区间。")


def quantile_type7(values, p):
    """Hyndman–Fan (1996) 类型 7 分位：h=(n-1)p+1，相邻次序统计量线性插值。"""
    xs = sorted(float(v) for v in values)
    n = len(xs)
    if n == 0:
        return None
    if n == 1 or p <= 0:
        return xs[0]
    if p >= 1:
        return xs[-1]
    h = (n - 1) * p + 1.0
    lo = max(0, min(n - 1, int(math.floor(h)) - 1))
    hi = max(0, min(n - 1, int(math.ceil(h)) - 1))
    if lo == hi:
        return xs[lo]
    gamma = h - math.floor(h)
    return (1.0 - gamma) * xs[lo] + gamma * xs[hi]


def _mean(values):
    return sum(values) / len(values) if values else None


def _normalize_statistic_key(key):
    if not isinstance(key, str) or not key.strip():
        raise InputError("requested_statistics: 每项必须为非空字符串")
    return key.strip().casefold()


def reject_inferential_statistics(requested):
    """关闭的错误路径：请求任何非描述性/推断统计键即抛出 ``InputError``。

    返回被接受的描述性键列表；只要出现一个非描述性键，整次调用失败，
    不产生任何部分结果或伪区间。
    """
    if requested is None:
        return []
    if not isinstance(requested, (list, tuple)):
        raise InputError("requested_statistics: 必须为数组")
    accepted, refused = [], []
    for item in requested:
        key = _normalize_statistic_key(item)
        if key in DESCRIPTIVE_STATISTIC_KEYS:
            accepted.append(key)
        else:
            refused.append(key)
    if refused:
        inferred = sorted(set(refused) & INFERENTIAL_STATISTIC_KEYS)
        detail = ""
        if inferred:
            detail = "；其中属于推断统计/校准概率口径: " + ", ".join(inferred)
        raise InputError(
            "RATE_REVIEW_INFERENTIAL_REFUSED: 本复盘只提供描述性口径，"
            "不提供校准概率区间或推断统计；拒绝键: " + ", ".join(sorted(set(refused))) + detail)
    return accepted


def _planning_point_minutes(quantity, rate_high):
    """排程慢端在观测完成量上的点预测（分钟）；与耗时误差协议一致。"""
    value = float(quantity) * float(rate_high)
    return float(max(1.0, math.ceil(value - 1e-8)))


def _direction(observed_rate, rate_low, rate_high, epsilon=EPS):
    if observed_rate < rate_low - epsilon:
        return "faster_than_planning_range"
    if observed_rate > rate_high + epsilon:
        return "slower_than_planning_range"
    return "within_planning_range"


def _source_summary(records):
    """按来源分组的描述性计数与已纳入记录的单位速率极值。"""
    order, buckets = [], {}
    for row in records:
        source = row["source"]
        key = source if isinstance(source, str) else str(source)
        if key not in buckets:
            buckets[key] = {"source": source, "record_count": 0, "included_count": 0, "rates": []}
            order.append(key)
        bucket = buckets[key]
        bucket["record_count"] += 1
        if row["included"] and row["observed_rate"] is not None:
            bucket["included_count"] += 1
            bucket["rates"].append(row["observed_rate"])
    summary = []
    for key in order:
        bucket = buckets[key]
        rates = bucket.pop("rates")
        bucket["observed_rate_min"] = min(rates) if rates else None
        bucket["observed_rate_max"] = max(rates) if rates else None
        summary.append(bucket)
    return summary


def descriptive_rate_retrospective(task, worker_id, observations, requested_statistics=None) -> dict:
    """逐例列出误差与来源的描述性复盘；不收紧保守范围、不产生概率区间。

    仅把同一任务、同人同做法、净作业口径、有完成量的记录纳入误差描述。
    请求 ``requested_statistics`` 时，任何非描述性（推断统计）键都会走关闭路径。
    """
    if requested_statistics is not None:
        reject_inferential_statistics(requested_statistics)
    base = review_observations(task, worker_id, observations)
    estimate = estimate_task(task, worker_id)
    unit = estimate["unit"]
    rate_low, rate_high = estimate.get("low_rate"), estimate.get("high_rate")
    source = estimate.get("source") if isinstance(estimate.get("source"), str) else ""
    caliber_verified = bool(rate_low is not None and rate_high is not None
                            and estimate["schedulable"] and not is_placeholder_source(source))
    reason_codes = {reason.get("code") for reason in estimate["reasons"]}
    unverified_reason = None
    if not caliber_verified:
        if rate_low is None or rate_high is None:
            unverified_reason = "no_worker_rate"
        elif "WHOLE_SESSION_RATE" in reason_codes:
            unverified_reason = "planning_scope_not_net_work"
        elif "RATE_SOURCE_UNKNOWN" in reason_codes:
            unverified_reason = "rate_source_not_verified"
        else:
            unverified_reason = "rate_not_schedulable"

    rows, error_pairs = [], []
    for index, row in enumerate(base["observations"]):
        entry = dict(row)
        entry.update({
            "error_minutes": None, "absolute_error_minutes": None, "predicted_minutes": None,
            "observed_minutes": None, "direction": None,
            "error_basis": None if caliber_verified else unverified_reason,
            "rate_caliber_verified": caliber_verified,
        })
        observation = observations[index]
        quantity, _q_unit = canonical_quantity(observation.get("quantity"))
        entry["observed_quantity"] = quantity
        if row["included"] and caliber_verified:
            y = float(_number(observation.get("minutes"), f"observations[{index}].minutes", 0))
            yhat = _planning_point_minutes(quantity, rate_high)
            error = yhat - y
            _number(error, f"observations[{index}].error_minutes", None)
            entry.update({
                "predicted_minutes": yhat, "observed_minutes": y,
                "error_minutes": error, "absolute_error_minutes": abs(error),
                "direction": _direction(row["observed_rate"], rate_low, rate_high),
                "error_basis": "verified_net_work_high_rate",
            })
            error_pairs.append({"rate": row["observed_rate"], "y": y, "yhat": yhat})
        rows.append(entry)

    mae = bias = None
    if error_pairs:
        errors = [item["yhat"] - item["y"] for item in error_pairs]
        mae = _mean([abs(err) for err in errors])
        bias = _mean(errors)

    tail = {"result": None, "observations": len(error_pairs), "n_tail": 0, "rate_quantile": None,
            "quantile_p": SLOW_TAIL_P, "quantile_definition": "Hyndman-Fan type 7",
            "null_reason": None}
    if not error_pairs:
        tail["null_reason"] = "no_pairs"
    elif len(error_pairs) < MIN_TAIL_ANALYSIS_N:
        tail["null_reason"] = f"analysis_n<{MIN_TAIL_ANALYSIS_N}"
    else:
        q_hat = quantile_type7([item["rate"] for item in error_pairs], SLOW_TAIL_P)
        tail["rate_quantile"] = q_hat
        slow = [item for item in error_pairs if item["rate"] + EPS >= q_hat]
        tail["n_tail"] = len(slow)
        if len(slow) < MIN_TAIL_N:
            tail["null_reason"] = f"tail_n<{MIN_TAIL_N}"
        else:
            tail["result"] = _mean([max(0.0, item["y"] - item["yhat"]) for item in slow])
            tail["null_reason"] = None

    suggested_low = base["suggested_rate_low"]
    suggested_high = base["suggested_rate_high"]
    tightened = False
    if caliber_verified:
        tightened = ((suggested_high is not None and suggested_high < rate_high - EPS)
                     or (suggested_low is not None and suggested_low > rate_low + EPS))

    zero_completion = sum(1 for row in base["observations"]
                          if "no_completed_quantity" in row["exclusion_reasons"])

    if "WHOLE_SESSION_RATE" in reason_codes:
        planning_scope = "whole_session"
    elif estimate["schedulable"]:
        planning_scope = "net_work"
    else:
        planning_scope = "unverified"

    return {
        "unit": unit,
        "worker_id": worker_id,
        "rate_definition": {
            "scope": planning_scope,
            "source": source,
            "source_verified": not is_placeholder_source(source),
            "planning_low_minutes_per_unit": rate_low,
            "planning_high_minutes_per_unit": rate_high,
            "verified": caliber_verified,
            "unverified_reason": unverified_reason,
        },
        "observations": rows,
        "included_count": base["included_count"],
        "excluded_count": base["excluded_count"],
        "zero_completion_count": zero_completion,
        "sources": _source_summary(base["observations"]),
        "error_summary": {
            "observations": len(error_pairs),
            "mae_minutes": mae,
            "bias_minutes": bias,
            "bias_sign_convention": "e = yhat - y；负值表示预测偏短（实际更慢）",
            "slow_tail_underestimate": tail,
            "no_inferential_statistics": True,
            "note": _INFERENCE_NOTE,
        },
        "observed_rate_min": base["observed_rate_min"],
        "observed_rate_max": base["observed_rate_max"],
        "quantity_weighted_rate": base["quantity_weighted_rate"],
        "conservative_range": {
            "suggested_rate_low": suggested_low,
            "suggested_rate_high": suggested_high,
            "planning_low_minutes_per_unit": rate_low,
            "planning_high_minutes_per_unit": rate_high,
            "tightened": tightened,
            "planning_range_unchanged": not tightened,
            "note": "少量快记录不会收紧保守慢端；复盘不修改任务速率。",
        },
        "interval_kind": "descriptive_not_probability_interval",
        "calibrated_probability_interval": None,
        "coverage_claim": None,
        "inferential_statistics": None,
        "remaining_quantity_unchanged": True,
        "not_health_clearance": True,
        "note": ("逐例描述误差与来源；不改任务速率、不收紧保守范围、"
                 "不产生校准概率区间，也不是健康许可。"),
    }
