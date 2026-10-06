"""社区风险排名敏感性：只接受社区级汇总，结果不输出名称或个人字段。"""
import argparse
import json
import math
import random
from pathlib import Path

from scipy.stats import kendalltau


WEIGHTS = {"weather": 0.45, "svi": 0.35, "burden": 0.20}


def hazard(rr, baseline, efold, vi=1.0):
    return round(100.0 * (1.0 - math.exp(-max(rr - 1, 0) * baseline * vi / efold)), 1)


def comparison(reference, candidate, top_k=10):
    """tau-b 保留同分；前十按原输入顺序稳定解同分，并公开边界同分。"""
    n = len(reference)
    k = min(top_k, n)
    if not n:
        return {"kendall_tau_b": None, "top_k": 0, "top_k_overlap": None}
    before = sorted(range(n), key=lambda i: (-reference[i], i))
    after = sorted(range(n), key=lambda i: (-candidate[i], i))
    tau = float(kendalltau(reference, candidate, variant="b").statistic) if n > 1 else float("nan")
    return {
        "kendall_tau_b": tau if math.isfinite(tau) else None,
        "top_k": k,
        "top_k_overlap": len(set(before[:k]) & set(after[:k])) / k,
        "top_k_boundary_tie": k < n and (reference[before[k - 1]] == reference[before[k]]
                                        or candidate[after[k - 1]] == candidate[after[k]]),
    }


def analyze(payload):
    if payload.get("source_kind") not in {"observed_aggregate", "synthetic"}:
        raise ValueError("source_kind 必须明确为 observed_aggregate 或 synthetic")
    legacy_efold = float(payload.get("legacy_count_efold", 0))
    if not math.isfinite(legacy_efold) or legacy_efold <= 0:
        raise ValueError("须提供旧数量公式的 legacy_count_efold 以便同口径复算")
    rows = payload.get("rankings", [])
    eligible = []
    for row in rows:
        formula = row.get("hazard_formula") or {}
        values = [formula.get(k) for k in ("weather_rr", "baseline_visits", "efold", "population", "baseline_period_days")]
        values += [row.get("vulnerability_index"), row.get("svi_percentile")]
        if formula.get("population_scope") != "all_residents" or any(v is None for v in values):
            continue
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError("汇总输入含非有限值")
        rr, baseline, efold, population, period_days, vi, svi = map(float, values)
        if rr <= 0 or baseline < 0 or efold <= 0 or population <= 0 or period_days <= 0 or vi <= 0 or not 0 <= svi <= 100:
            raise ValueError("汇总输入范围无效")
        burden = row.get("burden_percentile")
        if burden is not None and (not math.isfinite(float(burden)) or not 0 <= float(burden) <= 100):
            raise ValueError("历史负担分位无效")
        uncertainty = row.get("uncertainty_index")
        if uncertainty is not None and (not math.isfinite(float(uncertainty)) or not 0 <= float(uncertainty) <= 100):
            raise ValueError("不确定性无效")
        burden = float(burden) if burden is not None else None
        uncertainty = float(uncertainty) if uncertainty is not None else None
        eligible.append((hazard(rr, baseline / period_days / population * 1000, efold),
                         hazard(rr, baseline, legacy_efold, vi), hazard(rr, baseline, legacy_efold),
                         svi, burden, uncertainty))

    def scores(weights, legacy=False, count_only=False):
        output = []
        for new_hazard, old_hazard, count_hazard, svi, burden, uncertainty in eligible:
            active = {k: v for k, v in weights.items() if k != "burden" or burden is not None}
            denominator = sum(active.values())
            parts = {"weather": old_hazard if legacy else count_hazard if count_only else new_hazard, "svi": svi, "burden": burden}
            score = sum(parts[k] * w / denominator for k, w in active.items())
            if legacy and burden is not None and uncertainty is not None and uncertainty >= 70:
                score *= 0.93
            output.append(round(score, 1))
        return output

    baseline = scores(WEIGHTS)
    perturbations = []
    for key in WEIGHTS:
        for multiplier in (0.8, 1.2):
            perturbed = {**WEIGHTS, key: WEIGHTS[key] * multiplier}
            perturbations.append({"weight": key, "multiplier": multiplier,
                                  **comparison(baseline, scores(perturbed))})
    return {
        "schema_version": "2.0",
        "ranking_rate_unit": "人次/千居民/日",
        "legacy_count_efold": legacy_efold,
        "source_kind": payload["source_kind"],
        "sample_size": len(eligible),
        "excluded_missing_rows": len(rows) - len(eligible),
        "meaning": "合成数据方法检查，不是实测验证" if payload["source_kind"] == "synthetic" else "同一汇总样本的排名敏感性，不是预测有效性验证",
        "old_vs_new": comparison(scores(WEIGHTS, legacy=True), baseline),
        "count_vs_rate": comparison(scores(WEIGHTS, count_only=True), baseline),
        "weight_perturbations": perturbations,
        "limitations": ["old_vs_new 对比原始数量+VI+折减；count_vs_rate 单独对比上一版无VI数量公式与人均率公式", "每次只扰动一项权重，随后按可用分量重新归一", "tau-b 在全部同分或样本不足时返回 null", "前十按输入顺序稳定解同分，需结合边界同分标记解释"],
    }


def synthetic_payload():
    """固定种子生成方法回归样本，不冒充真实社区。"""
    rng = random.Random(20261005)
    return {"source_kind": "synthetic", "legacy_count_efold": 10, "rankings": [
        {"hazard_formula": {"weather_rr": 2.2, "baseline_visits": rng.uniform(1, 15), "efold": 10,
                            "population": rng.uniform(100, 1500), "baseline_period_days": 1,
                            "population_scope": "all_residents"},
         "vulnerability_index": rng.uniform(0.5, 2.8), "svi_percentile": rng.uniform(0, 100),
         "burden_percentile": rng.uniform(0, 100), "uncertainty_index": rng.uniform(0, 100)}
        for _ in range(40)
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="带 source_kind 和 rankings 的去标识社区汇总 JSON")
    group.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = synthetic_payload() if args.synthetic else json.loads(args.input.read_text())
    args.output.write_text(json.dumps(analyze(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
