"""最优差距与选择条件（R16-A09，独立评价层）。

目标（R16-A09）：在小实例上比较“生产有界 beam 搜索”与“同一候选空间内的完全枚举参照”
的字典序目标差距，同时报告两者的运行代价，并给出“何时才应启用更复杂/更昂贵求解”的选择条件。

口径沿用 R03-A06（搜索质量层）与 R09 主控算法选型，不改动任何既有约束：

- 字典序目标与 ``engine`` 内部 ``score()`` 同序：
  优先级 5..1 的平均完成比例 → 非负环境峰值 → 逐段非负环境值积分 → 活动人分钟 → 结束时刻 → 出工次数。
- 参照通过放宽 ``beam_width`` / ``max_search_states`` 得到；只有枚举完整结束时才算“已认证”。
- 首个差异分量之后的分量记 ``null``，避免“没出工所以更凉”的假优势。
- ``no_plan_found`` ≠ 无解；余量 ≠ 田间做不完；预算用尽 ≠ ``INFEASIBLE``。

本模块只读既有引擎（``_Planner``），不修改任何既有模块，也不改变生产搜索参数；
它只负责“比较、报告、给选择条件”，不替换生产搜索。

证据状态：全部为 SYNTHETIC / IMPLEMENTATION_TEST。现场 n=0，不声称现场准确率、
健康安全、全国通用或全局连续时间最优。
"""

from __future__ import annotations

import math
import time
from datetime import datetime

from .models import InputError, validate_request

SCHEMA_VERSION = "1.0"
EVALUATOR_KIND = "optimality_gap_evaluator"

# 评价器专用放宽上限。生产入口仍受 ``validate_request`` 上限（beam_width<=64、
# max_search_states<=20000）约束，本模块刻意放宽只是为了得到“同候选空间的完全枚举参照”，
# 不改变任何硬约束，也不改生产默认值。
REFERENCE_BEAM_WIDTH = 200000
REFERENCE_MAX_STATES = 200000

# 与 engine.score() 相同的分量顺序。完成比例分量在前，其余“越小越好”的分量在后。
PRIORITIES = (5, 4, 3, 2, 1)
COMPLETION_COMPONENTS = len(PRIORITIES)
EPS = 1e-9

# 任何情况下都不得把本层的差距或“未找到安排”写成“已证明无解”。
GAP_IS_PROVEN_UNAVOIDABLE = False

# 可行性状态（对齐 R03-A06）。
FEASIBILITY_STATES = (
    "FEASIBLE_FOUND",
    "OPEN_NO_PLAN",
    "OPEN_KNOWN_MISS",
    "OPEN_BUDGET",
    "CERTIFICATE_CONTRADICTION",
    "INFEASIBLE_CERTIFIED",
    "MODEL_INVALID",
)

# 选择条件结果。
RECOMMEND_KEEP = "KEEP_PRODUCTION_BEAM"
RECOMMEND_ESCALATE = "ESCALATE_FINER_OR_EXACT"
RECOMMEND_INSUFFICIENT = "INSUFFICIENT_CERTIFICATION"


def _run(raw_request, *, beam_width, max_search_states, step_minutes=None):
    """在同候选空间内跑一次排程；返回 (plan, wallclock_seconds)。

    ``validate_request`` 先做结构校验，再刻意放宽搜索预算。放宽的只是预算参数，
    不触碰天气、限制、截止、资源等硬约束。
    """
    request = validate_request(raw_request)
    request["beam_width"] = int(beam_width)
    request["max_search_states"] = int(max_search_states)
    if step_minutes is not None:
        request["step_minutes"] = int(step_minutes)
    planner = _import_planner()(request)
    started = time.perf_counter()
    plan = planner.run()
    elapsed = time.perf_counter() - started
    return plan, elapsed


def _import_planner():
    # 延迟导入，避免与既有导入顺序互相影响；仅只读复用，不修改。
    from .engine import _Planner

    return _Planner


def certify(plan, *, beam_width, max_search_states):
    """判断一次放宽后的搜索是否为“完全枚举参照”。

    认证成立需同时满足：没有触发状态预算、没有触达候选检查上限、没有触达深度上限、
    且 ``beam_width`` 不小于最终探索状态数（``|expanded| <= states_explored``，故不截断）。
    """
    search = plan["search"]
    reasons = []
    if search.get("budget_exhausted"):
        reasons.append("STATES_BUDGET")
    if search.get("candidate_check_limit_reached"):
        reasons.append("CHECK_LIMIT")
    if search.get("depth_limit_reached"):
        reasons.append("DEPTH_LIMIT")
    if int(beam_width) < int(search.get("states_explored", 0)):
        reasons.append("BEAM_TRUNCATED")
    return {
        "certified": not reasons,
        "reasons": reasons,
        "beam_width": int(beam_width),
        "max_search_states": int(max_search_states),
        "states_explored": int(search.get("states_explored", 0)),
        "candidate_checks": int(search.get("candidate_checks", 0)),
        "certificate": ("EXHAUSTIVE_WITHIN_ENGINE_CANDIDATE_SPACE" if not reasons else None),
        "scope_note": ("认证只覆盖引擎离散候选空间与所输入约束，不证明连续时间最优，"
                       "也不证明田间可行或健康安全。"),
    }


def _priority_map(raw_request):
    request = validate_request(raw_request)
    return {task["id"]: int(task["priority"]) for task in request["tasks"]}


def lexicographic_vector(plan, priorities=None):
    """从 plan 输出抽出与 ``engine.score()`` 同序的字典序分量。

    ``priorities`` 为 task_id -> priority 映射；缺省时按 task_results 顺序猜不出优先级，
    故调用方应显式传入。返回 ``components``（有序列表）与具名字段。
    """
    if priorities is None:
        priorities = {res["task_id"]: None for res in plan["task_results"]}
    ratio_by_task = {res["task_id"]: float(res["completion_ratio"]) for res in plan["task_results"]}
    completion = []
    for priority in PRIORITIES:
        members = [tid for tid, pri in priorities.items() if pri == priority]
        if members:
            value = sum(min(1.0, ratio_by_task.get(tid, 0.0)) for tid in members) / len(members)
        else:
            value = 0.0
        completion.append(round(value, 9))
    metric = plan["ranking"]["environment_metric"]
    peaks = [s["environment"]["metrics"].get(f"max_{metric}_c") for s in plan["sessions"]]
    integrals = [s["environment"]["metrics"].get(f"{metric}_positive_degree_minutes")
                 for s in plan["sessions"]]
    peak = max(0.0, max(peaks)) if peaks and all(v is not None for v in peaks) else (math.inf if peaks else 0.0)
    dose = sum(integrals) if integrals and all(v is not None for v in integrals) else (math.inf if integrals else 0.0)
    active = sum(s["active_minutes"] for s in plan["sessions"])
    ends = [_minutes(s["end"]) for s in plan["sessions"]]
    finish = max(ends) if ends else 0.0
    components = completion + [round(peak, 9), round(dose, 9), round(active, 9),
                               round(finish, 6), len(plan["sessions"])]
    names = [f"priority_{p}_mean_completion" for p in PRIORITIES] + [
        "environment_peak", "environment_positive_degree_minutes",
        "active_person_minutes", "finish_unix_minutes", "session_count"]
    return {"components": components, "names": names, "environment_metric": metric,
            "completion_count": COMPLETION_COMPONENTS}


def _minutes(stamp):
    return datetime.fromisoformat(stamp).timestamp() / 60.0


def optimality_gap(production_plan, reference_plan, priorities=None):
    """比较生产输出与已认证参照；给出字典序差距与首个差异分量。

    完成比例分量：gap = 参照 − 生产（越大越差）。
    其余分量：gap = 生产 − 参照（越小越好）。
    首个差异分量之后的分量记 null。
    """
    prod = lexicographic_vector(production_plan, priorities)
    ref = lexicographic_vector(reference_plan, priorities)
    if prod["environment_metric"] != ref["environment_metric"]:
        return {"comparable": False, "reason": "ENVIRONMENT_METRIC_MISMATCH",
                "first_differing_component": None, "visible_gain": False, "gaps": {}}
    gaps = []
    first = None
    for index, (x, y) in enumerate(zip(prod["components"], ref["components"])):
        if x is None or y is None:
            gaps.append(None)
            continue
        gap = (y - x) if index < COMPLETION_COMPONENTS else (x - y)
        if math.isinf(gap) or math.isnan(gap):
            gap = None
        elif abs(gap) <= EPS:
            gap = 0.0
        if first is None and gap is not None and abs(gap) > EPS:
            first = index
        gaps.append(gap)
    trimmed = [gaps[i] if (first is None or i <= first) else None for i in range(len(gaps))]
    finish_gap = None
    if first is None or first >= COMPLETION_COMPONENTS:
        pg, rg = prod["components"][-2], ref["components"][-2]
        if pg is not None and rg is not None and not math.isinf(pg) and not math.isinf(rg):
            finish_gap = pg - rg
    return {
        "comparable": True,
        "first_differing_component": first,
        "first_differing_name": None if first is None else prod["names"][first],
        "visible_gain": first is not None,
        "gaps": {name: trimmed[i] for i, name in enumerate(prod["names"])},
        "finish_gap_minutes": finish_gap,
        "production_vector": prod["components"],
        "reference_vector": ref["components"],
        "interpretation": ("差距只说明本次有界搜索相对同候选空间完全枚举参照的字典序位置；"
                           "不是现场准确率、不是健康结论、也不是‘已证明无解’。"),
    }


def feasibility_state(plan, reference=None, *, reference_found_sessions=None,
                      search_error=False):
    """按 R03-A06 映射可行性状态；预算/限额用尽一律不得写成 INFEASIBLE。"""
    if search_error:
        return "MODEL_INVALID"
    sessions = plan.get("sessions") or []
    search = plan.get("search", {})
    limited = bool(search.get("budget_exhausted")
                   or search.get("candidate_check_limit_reached")
                   or search.get("depth_limit_reached"))
    if plan.get("status") == "no_plan_found" and not sessions:
        if limited:
            return "OPEN_BUDGET"
        if reference is not None and reference.get("certified") and reference_found_sessions:
            return "OPEN_KNOWN_MISS"
        if reference is not None and reference.get("certified") and not reference_found_sessions:
            # 完全枚举参照也没找到任何会话：仍是“开放/无安排”，不得升级为不可行。
            return "OPEN_NO_PLAN"
        return "OPEN_NO_PLAN"
    return "FEASIBLE_FOUND"


def budget_misread_as_infeasible(state):
    """工程不变量：预算/深度/检查用尽不得映射为 INFEASIBLE_CERTIFIED。"""
    return state == "INFEASIBLE_CERTIFIED"


def compare_instance(raw_request, *, production=None, reference=None):
    """在同一实例上比较“生产配置”与“认证参照”，返回一条可审查记录。"""
    production = dict(production or {})
    reference = dict(reference or {})
    try:
        validated = validate_request(raw_request)
    except InputError as exc:
        return {"status": "MODEL_INVALID", "error": str(exc), "visible_gain": False,
                "certification": {"certified": False, "reasons": ["MODEL_INVALID"]}}
    priorities = {task["id"]: int(task["priority"]) for task in validated["tasks"]}

    prod_beam = int(production.get("beam_width", validated["beam_width"]))
    prod_states = int(production.get("max_search_states", validated["max_search_states"]))
    ref_beam = int(reference.get("beam_width", REFERENCE_BEAM_WIDTH))
    ref_states = int(reference.get("max_search_states", REFERENCE_MAX_STATES))
    ref_step = reference.get("step_minutes")

    prod_plan, prod_seconds = _run(raw_request, beam_width=prod_beam,
                                   max_search_states=prod_states)
    ref_plan, ref_seconds = _run(raw_request, beam_width=ref_beam,
                                 max_search_states=ref_states, step_minutes=ref_step)

    certification = certify(ref_plan, beam_width=ref_beam, max_search_states=ref_states)
    gap = optimality_gap(prod_plan, ref_plan, priorities)
    ref_found = bool(ref_plan.get("sessions"))
    prod_state = feasibility_state(prod_plan, certification,
                                   reference_found_sessions=ref_found)
    ref_state = feasibility_state(ref_plan, certification,
                                  reference_found_sessions=ref_found)

    horizon_minutes = _minutes(validated["horizon_end"]) - _minutes(validated["horizon_start"])
    return {
        "status": "OK",
        "instance": {
            "n_tasks": len(validated["tasks"]),
            "n_workers": len(validated["workers"]),
            "n_plots": len(validated["plots"]),
            "n_resources": len(validated["resources"]),
            "horizon_minutes": round(horizon_minutes, 3),
            "step_minutes": validated["step_minutes"],
        },
        "production": {
            "beam_width": prod_beam, "max_search_states": prod_states,
            "status": prod_plan["status"], "sessions": len(prod_plan["sessions"]),
            "states_explored": prod_plan["search"]["states_explored"],
            "wallclock_seconds": round(prod_seconds, 6),
        },
        "reference": {
            "beam_width": ref_beam, "max_search_states": ref_states,
            "status": ref_plan["status"], "sessions": len(ref_plan["sessions"]),
            "states_explored": ref_plan["search"]["states_explored"],
            "candidate_checks": ref_plan["search"]["candidate_checks"],
            "wallclock_seconds": round(ref_seconds, 6),
            "certification": certification,
        },
        "gap": gap,
        "visible_gain": bool(gap.get("visible_gain")),
        "feasibility": {"production": prod_state, "reference": ref_state},
        "runtime_ratio": (round(ref_seconds / prod_seconds, 4) if prod_seconds > 0 else None),
        "gap_is_proven_unavoidable": GAP_IS_PROVEN_UNAVOIDABLE,
        "evidence_label": "SYNTHETIC / IMPLEMENTATION_TEST",
    }


def select_strategy(records):
    """由若干 ``compare_instance`` 记录给出选择条件。

    规则：只有在小实例上、参照已认证、且确实观测到可见增益时才建议启用更复杂求解；
    未认证一律不声称增益；无可见增益则维持生产 beam（不因“更复杂”而启用）。
    """
    certified = [r for r in records if r.get("status") == "OK"
                 and r["reference"]["certification"].get("certified")]
    gains = [r for r in certified if r.get("visible_gain")]
    ratios = [r["runtime_ratio"] for r in certified if r.get("runtime_ratio") is not None]
    ratios.sort()
    median_ratio = None
    if ratios:
        mid = len(ratios) // 2
        median_ratio = ratios[mid] if len(ratios) % 2 else round((ratios[mid - 1] + ratios[mid]) / 2, 4)

    if not certified:
        recommendation = RECOMMEND_INSUFFICIENT
        enabled = False
        reason = ("没有任何已认证的小实例参照，无法判断增益；不启用更复杂求解，"
                  "也不据此声称无解。")
    elif gains:
        recommendation = RECOMMEND_ESCALATE
        enabled = True
        reason = ("在 %d/%d 个已认证小实例上观测到可见增益（窄搜索相对完全枚举参照存在字典序差距），"
                  "故对这些实例类别建议启用更精细或精确求解。" % (len(gains), len(certified)))
    else:
        recommendation = RECOMMEND_KEEP
        enabled = False
        reason = ("在 %d 个已认证小实例上生产 beam 与完全枚举参照字典序一致，未观测到可见增益；"
                  "更复杂求解当前不启用。" % len(certified))

    return {
        "recommendation": recommendation,
        "complex_solver_enabled": enabled,
        "certified_instances": len(certified),
        "instances_with_visible_gain": len(gains),
        "median_reference_runtime_ratio": median_ratio,
        "reasons": [reason],
        "escalation_conditions": [
            "实例规模落在认证预算内（本次：任务≤3、人员≤2、地块≤2、窗口≤4 小时、步长≥30 分钟）",
            "参照完全枚举通过认证（无预算/检查/深度截断、beam 未截断）",
            "同一实例上生产 beam 相对参照出现可见字典序差距",
        ],
        "keep_conditions": [
            "参照未认证：不比较、不声称增益、不声称无解",
            "生产 beam 与认证参照一致：不因“更复杂”而启用昂贵求解",
        ],
        "runtime_note": ("参照完全枚举的墙钟代价约为生产 beam 的中位数 %s 倍；"
                         "只有在该代价下确实换来可见增益时才值得升级。"
                         % ("null" if median_ratio is None else median_ratio)),
        "gap_is_proven_unavoidable": GAP_IS_PROVEN_UNAVOIDABLE,
        "claims_infeasible": any(r.get("feasibility", {}).get("production") == "INFEASIBLE_CERTIFIED"
                                 for r in records),
    }


__all__ = [
    "SCHEMA_VERSION", "EVALUATOR_KIND", "REFERENCE_BEAM_WIDTH", "REFERENCE_MAX_STATES",
    "GAP_IS_PROVEN_UNAVOIDABLE", "FEASIBILITY_STATES",
    "RECOMMEND_KEEP", "RECOMMEND_ESCALATE", "RECOMMEND_INSUFFICIENT",
    "certify", "lexicographic_vector", "optimality_gap", "feasibility_state",
    "budget_misread_as_infeasible", "compare_instance", "select_strategy",
]
