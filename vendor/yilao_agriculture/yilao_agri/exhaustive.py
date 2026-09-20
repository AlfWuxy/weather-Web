"""极小实例枚举器：有界搜索的评价参照，不替换生产搜索（R16-A01）。

定位（原要求 U16：优化对照、独立审计与不能证明无解的区别）
------------------------------------------------------------------
本模块在**同一份输入、同一套硬约束、同一个目标函数**下，做一次**完备可达状态枚举**，
给出该候选空间内真正的最优解，用来把三件常被混为一谈的事分开：

1. 有界搜索“没找到安排” ≠ 已证明无解；
2. 有界搜索“找到了安排” ≠ 已接近最优；
3. 参照枚举**走完全部可达状态**仍无解 = 在候选空间内证明无解（范围有限，不是全局无解）。

与 engine 的关系（务必区分）
------------------------------------------------------------------
- 复用 ``engine._Planner`` 的 ``candidates()/score()/_build()`` 与环境缓存：
  参照与搜索**共用**候选生成、硬约束检查与字典序目标，只差“搜索是否截断”。
- 因此本参照能检出的缺口，是 **beam 截断 / 状态预算 / 深度上限** 这一类
  “搜索不完备”，而不是候选生成本身漏解（``_build`` 的 ``choices[:2]``、
  ``candidates()`` 的 top-2 排序截断对两者同样生效），也不是硬约束实现错误
  （后者由 R15-A10 ``independent_verify`` 负责）。
- 完备性只相对于 engine 的候选生成器，**不是**对全部可想象计划的全局最优证明；
  也不做任何人体安全或农艺结论。现场样本 n_real 仍为 0。
- 本模块必须显式声明：当状态上限被触发时 ``enumeration_complete=False``，
  此时只报“未走完”，**不得**给出无解结论。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from .engine import _Planner, EPS, plan
from .models import validate_request

# R03-A06 已经写下的小实例纳入门槛（任务≤3、人员≤2、地块≤2、资源≤6、窗口≤240 分钟）。
TINY_INSTANCE_LIMITS = {
    "max_tasks": 3,
    "max_workers": 2,
    "max_plots": 2,
    "max_resources": 6,
    "max_window_minutes": 240,
}
DEFAULT_MAX_STATES = 200000

# 组件顺序与 engine._Planner.score() 完全同序：优先级 5..1 完成比例、峰值、积分、活动分钟、结束时刻、出工次数。
_COMPONENT_NAMES = ("p5", "p4", "p3", "p2", "p1", "peak", "dose", "active", "finish", "sessions")
# 完成比例越大越好；其余分量越小越好（与 engine.score() 的字典序一致）。
_HIGHER_IS_BETTER = frozenset(("p5", "p4", "p3", "p2", "p1"))


class _ExhaustivePlanner(_Planner):
    """在 engine 的候选空间上做无 beam、无预算、无深度上限的完备枚举。"""

    def __init__(self, request, max_states):
        super().__init__(request)
        self.max_states = int(max_states)
        self.states_enumerated = 0
        self.state_limit_reached = False
        # 关闭 engine 的候选检查上限：该上限属于“预算不足”，会让我们无法给出无解结论。
        self.check_limit = float("inf")

    def run(self):
        empty = {"sessions": [], "done": {}}
        best = empty
        frontier = {self.signature(empty): empty}
        seen = set(frontier)
        complete = True
        while frontier:
            nxt = {}
            for state in frontier.values():
                for session in self.candidates(state):
                    done = dict(state["done"])
                    done[session["task_id"]] = done.get(session["task_id"], 0) + session["quantity"]
                    successor = {"sessions": state["sessions"] + [session], "done": done}
                    signature = self.signature(successor)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    nxt[signature] = successor
                    if self.score(successor) < self.score(best):
                        best = successor
                    if len(seen) >= self.max_states:
                        self.state_limit_reached = True
                        break
                if self.state_limit_reached:
                    break
            if self.state_limit_reached:
                complete = False
                break
            if not nxt:
                break
            frontier = nxt
        self.states_enumerated = len(seen)
        result = self.result(best)
        result["search"].update({
            "algorithm": "exhaustive_reachable_state_enumeration",
            "beam_width": None,
            "budget_exhausted": False,
            "depth_limit_reached": False,
            "candidate_check_limit_reached": False,
            "states_explored": self.states_enumerated,
            # 仅在真正走完整个可达状态图时才宣称在本候选空间内最优。
            "optimality_proven": bool(complete),
        })
        result["reference"] = {
            "kind": "EXHAUSTIVE_ENUM",
            "candidate_space": "engine_candidates",
            "enumeration_complete": bool(complete),
            "states_enumerated": self.states_enumerated,
            "state_limit": self.max_states,
            "state_limit_reached": self.state_limit_reached,
            "optimal_within_candidate_space": bool(complete),
            "note": ("枚举走完全部可达状态；无解结论只在枚举完整时给出，且仅限该候选空间。"
                     if complete else
                     "达到状态上限，枚举不完整：不得据此声称无解，也不得声称最优。"),
        }
        return result


def tiny_instance_eligible(request):
    """按 R03-A06 的规模门槛判断是否属于小实例；返回 (bool, 原因列表)。"""
    request = validate_request(request)
    problems = []
    if len(request["tasks"]) > TINY_INSTANCE_LIMITS["max_tasks"]:
        problems.append(f"tasks={len(request['tasks'])} > {TINY_INSTANCE_LIMITS['max_tasks']}")
    if len(request["workers"]) > TINY_INSTANCE_LIMITS["max_workers"]:
        problems.append(f"workers={len(request['workers'])} > {TINY_INSTANCE_LIMITS['max_workers']}")
    if len(request["plots"]) > TINY_INSTANCE_LIMITS["max_plots"]:
        problems.append(f"plots={len(request['plots'])} > {TINY_INSTANCE_LIMITS['max_plots']}")
    if len(request["resources"]) > TINY_INSTANCE_LIMITS["max_resources"]:
        problems.append(f"resources={len(request['resources'])} > {TINY_INSTANCE_LIMITS['max_resources']}")
    window = _minutes(request["horizon_start"], request["horizon_end"])
    if window > TINY_INSTANCE_LIMITS["max_window_minutes"]:
        problems.append(f"window_minutes={window:.0f} > {TINY_INSTANCE_LIMITS['max_window_minutes']}")
    return (not problems), problems


def _minutes(start, end):
    a = datetime.fromisoformat(start.replace("Z", "+00:00")) if isinstance(start, str) else start
    b = datetime.fromisoformat(end.replace("Z", "+00:00")) if isinstance(end, str) else end
    return (b - a).total_seconds() / 60.0


def exhaustive_plan(raw, max_states=DEFAULT_MAX_STATES):
    """在 engine 候选空间上做完备枚举，返回与 ``engine.plan`` 同构的结果。"""
    request = validate_request(raw)
    return deepcopy(_ExhaustivePlanner(request, max_states).run())


def extract_components(result, priority_by_task):
    """从结果字典抽取与 score() 同序的字典序分量（搜索与参照通用）。"""
    ratios = {p: [] for p in range(1, 6)}
    for row in result["task_results"]:
        ratios[priority_by_task[row["task_id"]]].append(row["completion_ratio"])
    progress = []
    for priority in range(5, 0, -1):
        members = ratios[priority]
        progress.append(sum(members) / len(members) if members else 0.0)
    metric = result["ranking"]["environment_metric"]
    values, integral = [], []
    for session in result["sessions"]:
        metrics = session["environment"]["metrics"]
        values.append(metrics.get(f"max_{metric}_c"))
        integral.append(metrics.get(f"{metric}_positive_degree_minutes"))
    peak = max(0.0, max(values)) if values and all(v is not None for v in values) else (float("inf") if values else 0.0)
    dose = sum(integral) if all(v is not None for v in integral) else float("inf")
    finish = max((datetime.fromisoformat(s["end"]).timestamp() for s in result["sessions"]),
                 default=datetime.fromisoformat(result["as_of"]).timestamp())
    active = sum(s["active_minutes"] for s in result["sessions"])
    return dict(zip(_COMPONENT_NAMES, progress + [peak, dose, active, finish, len(result["sessions"])]))


def _close(a, b, tol=1e-9):
    if a is None or b is None:
        return a == b
    return abs(a - b) <= tol


def compare_with_search(raw, max_states=DEFAULT_MAX_STATES, reference_step_minutes=None):
    """同输入对比有界搜索与完备枚举，产出 U16 需要的可行性状态与字典序差距。

    可行性状态沿用 R03-A06 词汇，但“候选空间内无解”另用
    ``NO_PLAN_CERTIFIED_WITHIN_CANDIDATE_SPACE`` 表示，并始终附
    ``global_infeasibility_certified=false``：``no_plan_found``、预算不足、状态上限
    一律不升格为无解。
    按 R03-A06 协议，参照可与搜索共用除 ``step_minutes/beam_width/max_search_states``
    以外的全部输入；``reference_step_minutes`` 用于让参照用更细网格去找搜索因粗网格漏掉的解。
    """
    request = validate_request(raw)
    priority_by_task = {t["id"]: t["priority"] for t in request["tasks"]}

    search = plan(raw)
    reference_raw = raw
    if reference_step_minutes is not None and int(reference_step_minutes) != int(request["step_minutes"]):
        reference_raw = deepcopy(request)
        reference_raw["step_minutes"] = int(reference_step_minutes)
    reference = exhaustive_plan(reference_raw, max_states)
    sc = extract_components(search, priority_by_task)
    rc = extract_components(reference, priority_by_task)

    gaps = {}
    first_diff = None
    ordered = list(_COMPONENT_NAMES)
    for index, name in enumerate(ordered):
        sv, rv = sc[name], rc[name]
        if first_diff is None and not _close(sv, rv):
            first_diff = name
        if first_diff is not None and name != first_diff:
            # 首个差异之后的分量记 null，避免“没出工所以更凉”之类的伪优势。
            gaps[name] = None
            continue
        if name in ("p5", "p4", "p3", "p2", "p1"):
            gaps[name] = rv - sv          # 完成比例：参照 − 搜索（越大越好）
        else:
            gaps[name] = sv - rv          # 其余：搜索 − 参照（越小越好）
    gaps["first_differing_component"] = first_diff
    if first_diff in ("finish", None):
        gaps["finish_gap_minutes"] = (sc["finish"] - rc["finish"]) / 60.0
    else:
        gaps["finish_gap_minutes"] = None

    search_sessions = search["sessions"]
    ref_sessions = reference["sessions"]
    enum_complete = reference["reference"]["enumeration_complete"]
    sh = search["search"]
    if ref_sessions and not search_sessions:
        feasibility = "OPEN_KNOWN_MISS"          # 搜索漏解：这正是 U16 要能看见的情况
    elif search_sessions:
        feasibility = "FEASIBLE_FOUND"
    elif enum_complete:
        feasibility = "NO_PLAN_CERTIFIED_WITHIN_CANDIDATE_SPACE"
    elif sh["budget_exhausted"] or sh["candidate_check_limit_reached"] or sh["depth_limit_reached"]:
        feasibility = "OPEN_BUDGET"
    else:
        feasibility = "OPEN_NO_PLAN"

    return {
        "tiny_instance": dict(zip(("eligible", "problems"), tiny_instance_eligible(raw))),
        "objective_component_order": ordered,
        "search_score_components": sc,
        "reference_score_components": rc,
        "gap": gaps,
        "suboptimal_witnessed": bool(_lex_better(rc, sc, ordered)),
        # 完备枚举是搜索的超集，因此搜索不可能严格优于参照；此恒等式用于自检。
        "search_strictly_better_than_reference": bool(_lex_better(sc, rc, ordered)),
        "reference_at_least_as_good_as_search": not _lex_better(sc, rc, ordered),
        "feasibility_state": feasibility,
        "search_feasibility": _result_feasibility(sh, search_sessions),
        "reference_feasibility": _result_feasibility(
            reference["search"], ref_sessions,
            certified_infeasible=bool(not ref_sessions and enum_complete)),
        "enumeration": reference["reference"],
        "search_algorithm": sh["algorithm"],
        "reference_algorithm": reference["search"]["algorithm"],
        "search_step_minutes": int(request["step_minutes"]),
        "reference_step_minutes": int(reference["search"]["step_minutes"]),
        "finer_reference_grid": bool(int(reference["search"]["step_minutes"]) < int(request["step_minutes"])),
        "gap_is_proven_unavoidable": False,       # 与 engine 一致：本层不提供不可行证明
        # 本枚举复用 engine 候选空间，不构成 R03-A06 意义上的全局 INFEASIBLE_CERTIFIED。
        "global_infeasibility_certified": False,
        "certificate_scope": "engine_candidate_space_only",
        "note": ("字典序差距：完成比例越大越好（gap=参照−搜索），其余越小越好（gap=搜索−参照）；"
                 "首个差异之后的分量记 null。对照仅限 engine 候选空间。"),
    }


def _lex_better(a, b, ordered):
    """a 是否在字典序上严格优于 b；方向随分量而定。"""
    for name in ordered:
        av, bv = a[name], b[name]
        if _close(av, bv):
            continue
        if name in _HIGHER_IS_BETTER:
            return av > bv
        return av < bv
    return False


def _result_feasibility(search_block, sessions, certified_infeasible=False):
    if sessions:
        return "FEASIBLE_FOUND"
    if certified_infeasible:
        return "NO_PLAN_CERTIFIED_WITHIN_CANDIDATE_SPACE"
    if (search_block.get("budget_exhausted") or search_block.get("candidate_check_limit_reached")
            or search_block.get("depth_limit_reached")):
        return "OPEN_BUDGET"
    return "OPEN_NO_PLAN"
