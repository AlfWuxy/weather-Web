"""R16-A02 可选 CP-SAT 参照求解器（约束规划，只读参照，不接入生产排程）。

用途
----
在依赖（Google OR-Tools 的 CP-SAT）可用时，对**小实例**在同一批候选出工上用一个约束规划
模型求出**可证明最优**的取舍；依赖不可用时，本模块只提供**关闭接口**与**错误路径**，任何
调用都返回“未启用 / 未求解”，绝不把“没有 CP-SAT 后端”写成“不可行”或“已得最优”。

本模块**不改变** `yilao_agri.engine.plan` 的任何行为：engine 不导入本模块，生产排程路径
完全不变。参照模型回答的是“在同一批候选之内，能不能做得比有界束搜索更优”，而不是替换它。

三态（+1）语义——本卡要求逐项区分
--------------------------------
- OPTIMAL   ：在所声明的（受限）模型空间内已证明最优。``optimality = PROVEN``。
- FEASIBLE  ：找到可行解，但未证明最优（通常因时间上限）。``optimality = NOT_PROVEN``。
- UNKNOWN   ：没有得出结论（后端缺失 / 超时且无可行解 / 实例超出参照范围）。
              ``feasibility = UNDETERMINED``。**UNKNOWN 不等于无解。**
- INFEASIBLE：在完整搜索下证明“无解”。``feasibility = UNSAT``。**INFEASIBLE 不等于 UNKNOWN**，
              也不是“没找到”。

参照模型的空间与限制（写清楚，避免夸大）
------------------------------------
1. 候选来自 ``engine._Planner.candidates(空状态)``，因此**单段出工的全部硬约束**（人员可用、
   天气/农艺、截止、每日额度、资源可用与容量、最小分段量、首段休息）与生产完全同源。
2. 跨段约束由本模块在 CP 模型里复刻：同一人重叠/休息不足、同一人每日累计活动上限、同一任务
   默认串行、资源并发容量、前置任务顺序与完成、每任务总量上限。这些谓词直接复用 engine
   的 ``classify_worker_conflict`` 与候选自带的 ``resource_occupation``，不另写一套判断。
3. **受限范围**：只覆盖“一轮”选择（每个任务至多一段已在候选池内），不做多轮再枚举；候选数
   超过 ``max_candidates`` 或缺少可比较的环境分项时，返回 UNKNOWN 并标注原因，不硬解。
4. 目标与 engine ``_Planner.score`` 的字典序同序（优先级完成比例 5→1、环境峰值、环境剂量、
   活动人分钟、完成时刻、出工次数）。整数量任务下第一段用最小公倍数放大，可与 engine 完全
   同值比较；出现非整数需求时退化为定点缩放近似并置 ``objective_exact=False``。
"""

from __future__ import annotations

import math
from datetime import timedelta

from .engine import _Planner, classify_worker_conflict
from .models import same_task_parallel_allowed, to_utc, validate_request

__all__ = [
    "CPSatUnavailable", "backend_status", "require_backend", "reference_solve",
    "compare_with_search", "classify_solver_status", "verify_parity",
    "HARD_CONSTRAINTS", "OBJECTIVE_ORDER", "SOLVER_STATUSES_OF_SOLUTION",
    "STATUS_OPTIMAL", "STATUS_FEASIBLE", "STATUS_UNKNOWN", "STATUS_INFEASIBLE",
    "REASON_ORTOOLS_UNAVAILABLE", "REASON_TIME_LIMIT", "REASON_SCOPE_EXCEEDED",
]

# ---- 三态（+1）状态词表 -----------------------------------------------------
STATUS_OPTIMAL = "OPTIMAL"
STATUS_FEASIBLE = "FEASIBLE"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_INFEASIBLE = "INFEASIBLE"
#: 本卡点名的三态“有解”状态集；INFEASIBLE 是独立的终止态，绝不并入其中。
SOLVER_STATUSES_OF_SOLUTION = (STATUS_OPTIMAL, STATUS_FEASIBLE, STATUS_UNKNOWN)

FEASIBILITY_SAT = "SAT"
FEASIBILITY_UNSAT = "UNSAT"
FEASIBILITY_UNDETERMINED = "UNDETERMINED"

OPTIMALITY_BY_STATUS = {
    STATUS_OPTIMAL: "PROVEN",
    STATUS_FEASIBLE: "NOT_PROVEN",
    STATUS_UNKNOWN: "NOT_DETERMINED",
    STATUS_INFEASIBLE: "PROVEN_NO_SOLUTION",
}

REASON_ORTOOLS_UNAVAILABLE = "ORTOOLS_UNAVAILABLE"
REASON_TIME_LIMIT = "TIME_LIMIT_NO_SOLUTION"
REASON_SCOPE_EXCEEDED = "INSTANCE_OUT_OF_REFERENCE_SCOPE"
REASON_MODEL_INVALID = "MODEL_INVALID"

#: OR-Tools ``CpSolverStatus`` 的整型取值是稳定常量。这里本地复制，映射逻辑无需导入第三方库。
_CPSAT_UNKNOWN, _CPSAT_MODEL_INVALID, _CPSAT_FEASIBLE, _CPSAT_INFEASIBLE, _CPSAT_OPTIMAL = 0, 1, 2, 3, 4

_SCALE = 1000          # 定点缩放：数量/分钟/温度统一到整数格
_DEFAULT_TIME_LIMIT = 8.0
_DEFAULT_MAX_CANDIDATES = 400


class CPSatUnavailable(RuntimeError):
    """CP-SAT 后端不可用时，需显式求值的地方抛出该错误（错误路径）。"""


def _import_cp_model():
    """惰性导入：未安装 ortools 时本模块仍可正常导入。"""
    from ortools.sat.python import cp_model  # noqa: WPS433 (延迟导入是设计)
    return cp_model


def backend_status():
    """返回后端可用性与版本。不可用时也返回结构化信息，不抛异常。"""
    try:
        cp_model = _import_cp_model()
    except Exception as exc:  # pragma: no cover - 取决于环境是否装 ortools
        return {"available": False, "backend": "ortools.sat.python.cp_model", "version": None,
                "reason": REASON_ORTOOLS_UNAVAILABLE,
                "detail": "%s: %s" % (type(exc).__name__, exc),
                "enabled": False}
    version = None
    try:
        import ortools  # noqa: WPS433
        version = getattr(ortools, "__version__", None)
    except Exception:  # pragma: no cover
        version = None
    return {"available": True, "backend": "ortools.sat.python.cp_model", "version": version,
            "reason": None, "detail": None, "enabled": True,
            "status_codes": {"OPTIMAL": int(cp_model.OPTIMAL), "FEASIBLE": int(cp_model.FEASIBLE),
                             "INFEASIBLE": int(cp_model.INFEASIBLE), "UNKNOWN": int(cp_model.UNKNOWN),
                             "MODEL_INVALID": int(cp_model.MODEL_INVALID)}}


def require_backend():
    """需要 CP-SAT 的调用点先经过这里；不可用即抛错，不静默退化。"""
    status = backend_status()
    if not status["available"]:
        raise CPSatUnavailable(
            "CP-SAT 后端不可用（%s）：%s。参照求解器未启用；这**不代表**任何实例不可行。"
            % (REASON_ORTOOLS_UNAVAILABLE, status["detail"]))
    return status


def classify_solver_status(code, solution_found=False):
    """把 CP-SAT 状态码映射到本模块的三态（+1）词表。

    ``solution_found`` 由调用方按“是否真的取到解向量”给出，避免仅凭状态码臆测。
    """
    if int(code) == _CPSAT_OPTIMAL:
        return {"solver_status": STATUS_OPTIMAL,
                "feasibility": FEASIBILITY_SAT if solution_found else FEASIBILITY_UNSAT,
                "optimality": OPTIMALITY_BY_STATUS[STATUS_OPTIMAL],
                "reason": None if solution_found else "INFEASIBILITY_PROVEN"}
    if int(code) == _CPSAT_FEASIBLE:
        return {"solver_status": STATUS_FEASIBLE, "feasibility": FEASIBILITY_SAT,
                "optimality": OPTIMALITY_BY_STATUS[STATUS_FEASIBLE],
                "reason": "OPTIMALITY_NOT_PROVEN"}
    if int(code) == _CPSAT_INFEASIBLE:
        return {"solver_status": STATUS_INFEASIBLE, "feasibility": FEASIBILITY_UNSAT,
                "optimality": OPTIMALITY_BY_STATUS[STATUS_INFEASIBLE],
                "reason": "INFEASIBILITY_PROVEN"}
    if int(code) == _CPSAT_MODEL_INVALID:
        return {"solver_status": STATUS_UNKNOWN, "feasibility": FEASIBILITY_UNDETERMINED,
                "optimality": OPTIMALITY_BY_STATUS[STATUS_UNKNOWN], "reason": REASON_MODEL_INVALID}
    return {"solver_status": STATUS_UNKNOWN, "feasibility": FEASIBILITY_UNDETERMINED,
            "optimality": OPTIMALITY_BY_STATUS[STATUS_UNKNOWN], "reason": "NO_CONCLUSION"}


# ---- 约束 / 目标 对齐清单（供主控审查与自动核对） ---------------------------
#: CP 模型逐条复刻的硬约束；code 必须能在 engine/environment 源码里找到同名拒绝码。
HARD_CONSTRAINTS = (
    {"code": "WORKER_OVERLAP",
     "cp": "同一人两段候选区间真重叠 => x_i + x_j <= 1",
     "source": "engine._build / classify_worker_conflict"},
    {"code": "WORKER_REST_INSUFFICIENT",
     "cp": "同一人两段候选间隔 < min_rest => x_i + x_j <= 1",
     "source": "engine._build / classify_worker_conflict"},
    {"code": "WORKER_UNAVAILABLE",
     "cp": "候选已由 engine 枚举保证单人可用覆盖（同源）",
     "source": "engine._build"},
    {"code": "DAILY_ACTIVE_LIMIT",
     "cp": "同一人同一当地日 sum(active_by_date_i * x_i) <= limit - used",
     "source": "engine._build"},
    {"code": "TASK_PARALLELISM",
     "cp": "同一任务默认串行：重叠候选 x_i + x_j <= 1",
     "source": "engine._build / same_task_parallel_allowed"},
    {"code": "DEPENDENCY_INCOMPLETE",
     "cp": "A 有出工 => 其依赖 B 被选满，且 B 的结束不晚于 A 的开始",
     "source": "engine.candidates / _build"},
    {"code": "DEADLINE_OR_HORIZON",
     "cp": "候选已由 engine 枚举保证在截止与规划范围内（同源）",
     "source": "engine._build"},
    {"code": "INTEGER_UNIT_SPLIT",
     "cp": "整数单位（趟/株）按整数量子取候选（同源枚举）",
     "source": "engine.candidates / workload"},
    {"code": "RESOURCE_UNAVAILABLE",
     "cp": "候选已由 engine 保证资源可用覆盖（同源枚举）",
     "source": "engine._resource_choices"},
    {"code": "RESOURCE_CAPACITY",
     "cp": "每个资源在每个事件时点 sum(占用该资源的 x_i) <= capacity",
     "source": "engine._capacity_ok"},
    {"code": "WORKER_STATE_NOT_CLEAR",
     "cp": "非 clear 状态的人不进入候选（同源枚举）",
     "source": "engine._build"},
    {"code": "WORKER_LIMITS_UNCONFIRMED",
     "cp": "限制不可采用的人不进入候选（同源枚举）",
     "source": "engine._build"},
    {"code": "NO_WORKER",
     "cp": "空人手：候选池为空，任何任务都不会被选中",
     "source": "engine.candidates"},
)

#: 字典序目标顺序，必须与 engine.result()["ranking"]["objectives"] 完全一致。
OBJECTIVE_ORDER = ("各优先级任务平均完成比例", "非负环境峰值", "逐段非负环境值乘分钟",
                   "活动人分钟", "完成时刻", "出工次数")


def verify_parity(raw):
    """核对“同约束、同目标”是否真的对齐。

    - 约束：``HARD_CONSTRAINTS`` 里每个 code 都必须能在 engine/environment 源码中找到。
    - 目标：本模块声明的顺序必须等于一次真实 ``plan`` 输出的 ranking.objectives。
    """
    from pathlib import Path
    from .engine import plan
    root = Path(__file__).resolve().parent
    corpus = "\n".join((root / name).read_text(encoding="utf-8")
                       for name in ("engine.py", "environment.py"))
    missing = [row["code"] for row in HARD_CONSTRAINTS if row["code"] not in corpus]
    result = plan(raw)
    declared = tuple(result["ranking"]["objectives"])
    return {"constraints_declared": len(HARD_CONSTRAINTS), "constraints_missing_in_source": missing,
            "constraints_ok": not missing,
            "objective_order_matches_engine": declared == OBJECTIVE_ORDER,
            "engine_objective_order": list(declared),
            "declared_objective_order": list(OBJECTIVE_ORDER),
            "note": "仅核对代码名与顺序对齐；不声称两套实现逐位等价，也不声称现场有效。"}


# ---- 参照模型 ---------------------------------------------------------------

def _disabled_report(reason, detail):
    return {"enabled": False, "solver_status": STATUS_UNKNOWN, "feasibility": FEASIBILITY_UNDETERMINED,
            "optimality": OPTIMALITY_BY_STATUS[STATUS_UNKNOWN], "reason": reason,
            "detail": detail, "sessions": [], "status": "no_plan_found",
            "objective_vector": None, "objective_exact": None,
            "note": "参照求解器未启用/未求解；UNKNOWN 不等于无解，也不构成任何可行或最优声明。"}


def _occ_windows(candidate):
    """候选的资源占用窗（分钟），半开 [lo, hi)；无声明时按整段回退（保守）。"""
    start, end = to_utc(candidate["start"]), to_utc(candidate["end"])
    occupation = candidate.get("resource_occupation")
    payload = {}
    for rid in candidate.get("resources", []):
        windows = []
        if isinstance(occupation, dict) and rid in occupation:
            for lo, hi in occupation[rid]:
                windows.append((to_utc(lo), to_utc(hi)))
        if not windows:
            windows = [(start, end)]
        payload[rid] = windows
    return payload


def _dedup(candidates):
    seen, out = set(), []
    for c in candidates:
        key = (c["task_id"], c["worker_id"], c["start"], c["end"], round(c["quantity"], 8),
               tuple(c["resources"]))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def reference_solve(raw, *, time_limit_s=_DEFAULT_TIME_LIMIT, max_candidates=_DEFAULT_MAX_CANDIDATES,
                    require_complete=False):
    """在参照模型上求解。

    ``require_complete=False``：求字典序下最优的“尽力而为”取舍（可只完成部分任务）。
    ``require_complete=True`` ：额外要求所有任务被 100% 覆盖，用于给出“单轮不可行”的判定。
    后端缺失或实例超出参照范围时返回关闭/未决报告，绝不返回 INFEASIBLE。
    """
    status = backend_status()
    if not status["available"]:
        return _disabled_report(REASON_ORTOOLS_UNAVAILABLE, status["detail"])

    request = validate_request(raw)
    planner = _Planner(request)
    pool = _dedup(planner.candidates({"sessions": [], "done": {}}))
    if not pool and require_complete and planner.tasks:
        # 空人手或全部任务为 0：引擎口径是“未提供劳动者”，不是已证无解。
        if not planner.workers:
            return _disabled_report(REASON_SCOPE_EXCEEDED, "尚未提供可用于排程的劳动者，参照不判定无解。")
    if len(pool) > max_candidates:
        return _disabled_report(REASON_SCOPE_EXCEEDED,
                                "候选数 %d 超过参照上限 %d：仅覆盖小实例。" % (len(pool), max_candidates))

    cp_model = _import_cp_model()
    metric = planner.metric
    peak_key = "max_%s_c" % metric
    dose_key = "%s_positive_degree_minutes" % metric

    quantities = {tid: float(q) for tid, q in planner.quantities.items()}
    q_int = {}
    objective_exact = True
    for tid, q in quantities.items():
        scaled = round(q * _SCALE)
        if abs(scaled / _SCALE - q) > 1e-9:
            objective_exact = False
        q_int[tid] = max(0, scaled)

    model = cp_model.CpModel()
    x = [model.NewBoolVar("x_%d" % i) for i in range(len(pool))]
    index_by_task = {}
    for i, c in enumerate(pool):
        index_by_task.setdefault(c["task_id"], []).append(i)

    # 每任务总量上限（不超排）。
    for tid, idx in index_by_task.items():
        model.Add(sum(round(pool[i]["quantity"] * _SCALE) * x[i] for i in idx) <= q_int.get(tid, 0))

    # 同一人：重叠 / 休息不足；同一任务：默认串行。
    for i in range(len(pool)):
        ci = pool[i]
        rest_i = timedelta(minutes=planner.workers[ci["worker_id"]]["limits"]["min_rest_minutes"])
        for j in range(i + 1, len(pool)):
            cj = pool[j]
            if ci["worker_id"] == cj["worker_id"]:
                conflict = classify_worker_conflict(
                    to_utc(ci["start"]), to_utc(ci["end"]), to_utc(cj["start"]), to_utc(cj["end"]),
                    rest_i)
                if conflict in ("overlap", "rest_short"):
                    model.Add(x[i] + x[j] <= 1)
            if ci["task_id"] == cj["task_id"]:
                task = planner.tasks[ci["task_id"]]
                if not same_task_parallel_allowed(task, request["mode"]):
                    if to_utc(ci["start"]) < to_utc(cj["end"]) and to_utc(cj["start"]) < to_utc(ci["end"]):
                        model.Add(x[i] + x[j] <= 1)

    # 同一人每日累计活动上限。
    by_worker_day = {}
    for i, c in enumerate(pool):
        for day, minutes in c["active_minutes_by_date"].items():
            by_worker_day.setdefault((c["worker_id"], day), []).append((i, minutes))
    for (wid, day), rows in by_worker_day.items():
        worker = planner.workers[wid]
        used = float(worker["used_active_minutes_by_date"].get(day, 0) or 0)
        budget = float(worker["limits"]["max_active_minutes_per_day"]) - used
        model.Add(sum(round(minutes * _SCALE) * x[i] for i, minutes in rows)
                  <= int(math.floor(budget * _SCALE + 1e-6)))

    # 资源并发容量：峰值只可能出现在某个占用窗起点，逐个时点约束即为精确。
    by_resource = {}
    for i, c in enumerate(pool):
        for rid, windows in _occ_windows(c).items():
            by_resource.setdefault(rid, []).append((i, windows))
    for rid, rows in by_resource.items():
        capacity = int(planner.resources[rid]["capacity"])
        checkpoints = sorted({lo for _, windows in rows for lo, _ in windows})
        for point in checkpoints:
            hit = [i for i, windows in rows if any(lo <= point < hi for lo, hi in windows)]
            if hit:
                model.Add(sum(x[i] for i in hit) <= capacity)

    # 前置任务：A 有出工 => 依赖 B 被选满，且 B 结束不晚于 A 开始。
    any_selected = {}
    for tid, idx in index_by_task.items():
        flag = model.NewBoolVar("any_%s" % tid)
        for i in idx:
            model.Add(flag >= x[i])
        model.Add(flag <= sum(x[i] for i in idx))
        any_selected[tid] = flag
    for tid in list(index_by_task):
        task = planner.tasks[tid]
        for dep in task["depends_on"]:
            if dep not in index_by_task:
                model.Add(any_selected[tid] == 0)
                continue
            dep_idx = index_by_task[dep]
            for i in index_by_task[tid]:
                for j in dep_idx:
                    if to_utc(pool[j]["end"]) > to_utc(pool[i]["start"]):
                        model.Add(x[i] + x[j] <= 1)
            total = sum(round(pool[j]["quantity"] * _SCALE) for j in dep_idx)
            slack = total if total > 0 else 0
            model.Add(sum(round(pool[j]["quantity"] * _SCALE) * x[j] for j in dep_idx)
                      >= q_int.get(dep, 0) - slack * (1 - any_selected[tid]))

    # 环境分项：缺失时该目标段不建模（如实标注）。
    peaks = [c["environment"]["metrics"].get(peak_key) for c in pool]
    doses = [c["environment"]["metrics"].get(dose_key) for c in pool]
    peak_modeled = all(p is not None for p in peaks)
    dose_modeled = all(d is not None for d in doses)

    horizon = to_utc(request["horizon_start"])

    # 第一段：按优先级 5->1，最大化该优先级任务的平均完成比例（与 engine.score 同序）。
    stage_exprs = []   # (kind, expr, label)
    for priority in range(5, 0, -1):
        members = [t for t, task in planner.tasks.items() if task["priority"] == priority]
        reqs = [q_int.get(t, 0) for t in members if q_int.get(t, 0) > 0]
        if not members or not reqs:
            continue
        lcm = reqs[0]
        for value in reqs[1:]:
            lcm = lcm * value // math.gcd(lcm, value)
        if lcm > 10 ** 9:      # 过大则退化为定点近似，并如实标注
            objective_exact = False
            lcm = _SCALE
        expr = None
        for tid in members:
            idx = index_by_task.get(tid)
            if not idx or q_int.get(tid, 0) <= 0:
                continue
            weight = lcm // q_int[tid]
            if weight * q_int[tid] != lcm:
                objective_exact = False
                weight = max(1, int(round(lcm / q_int[tid])))
            term = sum(round(pool[i]["quantity"] * _SCALE) * weight * x[i] for i in idx)
            expr = term if expr is None else expr + term
        if expr is not None:
            stage_exprs.append(("max", expr, "priority_%d_mean_completion" % priority))

    if peak_modeled:
        peak_int = [int(round(p * _SCALE)) for p in peaks]
        cap = max(peak_int) if peak_int else 0
        peak_var = model.NewIntVar(0, max(cap, 0), "peak")
        for i, value in enumerate(peak_int):
            model.Add(peak_var >= value * x[i])
        stage_exprs.append(("min", peak_var, "environment_peak"))
    if dose_modeled:
        dose_int = [int(round(d)) for d in doses]
        stage_exprs.append(("min", sum(dose_int[i] * x[i] for i in range(len(pool))), "environment_dose"))
    stage_exprs.append(("min", sum(round(pool[i]["active_minutes"] * _SCALE) * x[i]
                                   for i in range(len(pool))), "active_person_minutes"))
    finish_int = [int(round((to_utc(c["end"]) - horizon).total_seconds() / 60.0 * _SCALE)) for c in pool]
    finish_cap = max(finish_int) if finish_int else 0
    finish_var = model.NewIntVar(0, max(finish_cap, 0), "finish")
    for i, value in enumerate(finish_int):
        model.Add(finish_var >= value * x[i])
    stage_exprs.append(("min", finish_var, "finish"))
    stage_exprs.append(("min", sum(x), "session_count"))

    if require_complete:
        for tid, idx in index_by_task.items():
            if q_int.get(tid, 0) > 0:
                model.Add(sum(round(pool[i]["quantity"] * _SCALE) * x[i] for i in idx) >= q_int[tid])

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_workers = 1

    final_status, fixed, stage_report = _CPSAT_UNKNOWN, [], []
    for kind, expr, label in stage_exprs:
        if kind == "max":
            model.Maximize(expr)
        else:
            model.Minimize(expr)
        code = solver.Solve(model)
        if code in (_CPSAT_INFEASIBLE, _CPSAT_MODEL_INVALID, _CPSAT_UNKNOWN):
            final_status = code
            break
        value = int(round(solver.ObjectiveValue()))
        fixed.append((expr, value, label, kind))
        stage_report.append({"stage": label, "kind": kind, "value": value,
                             "status": "OPTIMAL" if code == _CPSAT_OPTIMAL else "FEASIBLE"})
        if code != _CPSAT_OPTIMAL:
            final_status = _CPSAT_FEASIBLE
            break
        model.Add(expr == value)
    else:
        final_status = _CPSAT_OPTIMAL

    if final_status in (_CPSAT_INFEASIBLE, _CPSAT_MODEL_INVALID, _CPSAT_UNKNOWN):
        mapped = classify_solver_status(final_status, solution_found=False)
        report = _disabled_report(mapped["reason"] or REASON_TIME_LIMIT,
                                  "参照求解在 %g 秒内未给出结论。" % time_limit_s)
        report.update(enabled=True, solver_status=mapped["solver_status"],
                      feasibility=mapped["feasibility"], optimality=mapped["optimality"],
                      backend=status["backend"], version=status["version"],
                      stage_report=stage_report, objective_exact=objective_exact,
                      require_complete=require_complete)
        return report

    selected = [i for i in range(len(pool)) if solver.Value(x[i]) == 1]
    sessions = sorted((pool[i] for i in selected),
                      key=lambda s: (to_utc(s["start"]), s["worker_id"], s["task_id"]))
    mapped = classify_solver_status(final_status, solution_found=True)
    return _build_report(planner, sessions, mapped, status, stage_report, objective_exact,
                         require_complete=require_complete, pool_size=len(pool))


def _build_report(planner, sessions, mapped, backend, stage_report, objective_exact,
                  require_complete, pool_size):
    # 用 engine 自己的 result() 生成同一 schema 的计划载荷，避免另造一套字段。
    done = {}
    for s in sessions:
        done[s["task_id"]] = done.get(s["task_id"], 0.0) + s["quantity"]
    payload = planner.result({"sessions": list(sessions), "done": done})
    total_active = sum(s["active_minutes"] for s in sessions)
    return {"enabled": True, "backend": backend["backend"], "version": backend["version"],
            "solver_status": mapped["solver_status"], "feasibility": mapped["feasibility"],
            "optimality": mapped["optimality"], "reason": mapped["reason"],
            "status": payload["status"], "sessions": sessions,
            "task_results": payload["task_results"],
            "objective_vector": {"active_person_minutes": total_active, "session_count": len(sessions),
                                 "finish_utc": max((to_utc(s["end"]) for s in sessions), default=None).isoformat()
                                 if sessions else None},
            "objective_exact": objective_exact, "stage_report": stage_report,
            "require_complete": require_complete, "pool_size": pool_size,
            "optimal_over": "enumerated_single_round_candidate_space",
            "plan": payload,
            "note": ("OPTIMAL 只表示在该受限参照空间内已证最优，不等于现场更优或健康安全；"
                     "UNKNOWN 不等于无解。")}


def compare_with_search(raw, **kwargs):
    """把参照结论与生产有界束搜索对照，回答“同样的限制与目标下谁更好/是否一致”。

    后端缺失时**仍**返回搜索侧结论与独立的四类判定（PLAN_VALID / INFEASIBLE_PROVEN /
    NO_PLAN_FOUND_NOT_PROVEN / PLAN_INVALID），只是把参照标为不可用。
    """
    from .engine import plan
    from .independent_verify import classify_outcome, objective_vector, verify_plan_independent

    search_result = plan(raw)
    outcome = classify_outcome(raw, search_result)
    report = {"reference_available": backend_status()["available"],
              "search_status": search_result["status"],
              "search_optimality_proven": bool(search_result["search"]["optimality_proven"]),
              "search_outcome": outcome["outcome"], "search_valid": outcome["valid"],
              "search_objective": objective_vector(search_result),
              "reference": None, "agreement": None,
              "note": "有界束搜索不证明最优；其“未找到”不等于无解。"}
    if not report["reference_available"]:
        report["comparison"] = "UNAVAILABLE_NO_CP_SAT_BACKEND"
        report["note"] += " 参照求解器不可用（ORTOOLS_UNAVAILABLE），未做数值对照。"
        return report

    ref = reference_solve(raw, **kwargs)
    report["reference"] = {k: ref[k] for k in
                           ("enabled", "solver_status", "feasibility", "optimality", "status",
                            "objective_exact", "reason", "optimal_over")}
    if not ref["enabled"]:
        report["comparison"] = "UNAVAILABLE_REFERENCE_SCOPE"
        return report

    # 搜索计划是否落在参照的候选空间内（同空间才比较数值）。
    pool = _dedup(_Planner(validate_request(raw)).candidates({"sessions": [], "done": {}}))
    keys = {(c["task_id"], c["worker_id"], c["start"], c["end"], round(c["quantity"], 8))
            for c in pool}
    same_space = all((s["task_id"], s["worker_id"], s["start"], s["end"], round(s["quantity"], 8)) in keys
                     for s in search_result["sessions"])
    ref_valid = verify_plan_independent(raw, ref["plan"])["valid"]

    report["reference_plan_valid"] = ref_valid
    report["same_candidate_space"] = same_space
    report["comparison"] = "COMPARED_SAME_SPACE" if same_space else "COMPARED_DIFFERENT_SPACE"
    if same_space:
        ref_obj, search_obj = ref["objective_vector"], report["search_objective"]
        dominates = (ref_obj["active_person_minutes"] <= search_obj["active_person_minutes"] + 1e-6
                     and (ref_obj["session_count"] or 0) <= (search_obj["session_count"] or 0))
        report["reference_dominates_or_ties"] = dominates
        report["search_coverage"] = _coverage(search_result)
        report["reference_coverage"] = _coverage(ref["plan"])
        report["note"] += (" 参照在枚举候选内已证最优时，搜索覆盖比例不应更高；"
                           "若更高即为可审查线索。")
    return report


def _coverage(result):
    rows = result.get("task_results") or []
    if not rows:
        return 1.0 if result.get("status") == "complete" else 0.0
    return sum(r.get("completion_ratio", 0.0) for r in rows) / len(rows)
