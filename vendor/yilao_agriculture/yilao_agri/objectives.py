"""R16-A05 多目标结果展示与比较。

把若干**可行**排程方案的目标分量并排展示，保留少量取舍不同者，供本人与家属
自行判断。本模块只读 ``engine.plan`` 的既有输出与对应请求：不修改搜索算法、
不放松任何硬约束、不合成加权总分，也不输出医学风险分数。缺暖热环境证据时走
关闭路径（分量置 null 并标 NEEDS_EVIDENCE），不编数。

分量按三族分组：**完成量**（各优先级平均完成比例、整体完成比例、未完成任务数）、
**公平性**（人-人活动分钟极差）、**风险**（非负暖热峰值与积分、活动人分钟、完成时刻、
出工次数）。分量只分列量纲。``direction`` 仅用于"谁支配谁"的偏序比较：

- 完成类：越大越好（maximize）。
- 暖热峰值、非负暖热积分、活动人分钟、完成时刻、出工次数：越小越好（minimize）。

零截断只避免负值奖励长时间暴露，是算术下界，不是人体安全阈值。寒冷限制须另行
输入。排序既不是医学风险分数，也不能替代人做取舍决定。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from .audit import verify_plan
from .engine import plan as _plan
from .models import InputError, parse_time, validate_request

EPS = 1e-9
MAX_KEEP_DEFAULT = 3
MAX_KEEP_LIMIT = 5

#: 目标分量表。``family`` 标出所属族（完成量 / 公平性 / 风险），
#: ``weight`` 恒为 None：本模块不提供权重，也不把偏好写成医学系数。
COMPONENTS = (
    {"id": "completion_by_priority", "label": "各优先级任务平均完成比例（5→1）",
     "family": "完成量", "unit": "ratio", "direction": "maximize"},
    {"id": "completion_overall", "label": "全部任务平均完成比例",
     "family": "完成量", "unit": "ratio", "direction": "maximize"},
    {"id": "unscheduled_task_count", "label": "未完成任务数",
     "family": "完成量", "unit": "count", "direction": "minimize"},
    {"id": "worker_load_spread", "label": "人员活动分钟极差（人-人差距）",
     "family": "公平性", "unit": "min", "direction": "minimize"},
    {"id": "environment_peak", "label": "非负环境峰值",
     "family": "风险", "unit": "degC", "direction": "minimize"},
    {"id": "environment_positive_degree_minutes", "label": "逐段非负环境值乘分钟",
     "family": "风险", "unit": "degC*min", "direction": "minimize"},
    {"id": "active_person_minutes", "label": "活动人分钟",
     "family": "风险", "unit": "min", "direction": "minimize"},
    {"id": "finish", "label": "完成时刻", "family": "风险", "unit": "iso8601",
     "direction": "minimize"},
    {"id": "session_count", "label": "出工次数", "family": "风险", "unit": "count",
     "direction": "minimize"},
)
COMPONENT_IDS = tuple(c["id"] for c in COMPONENTS)
_DIRECTION = {c["id"]: c["direction"] for c in COMPONENTS}
#: 展示取舍时的读取顺序：先看完成，再看公平，最后看风险。
_DISPLAY_ORDER = ("completion_by_priority", "completion_overall", "unscheduled_task_count",
                  "worker_load_spread", "environment_peak",
                  "environment_positive_degree_minutes", "active_person_minutes",
                  "finish", "session_count")

_METRIC_KEYS = ("temperature", "wbgt")


class ScalarizationRejected(ValueError):
    """拒绝把多目标合成加权总分，避免伪装成医学风险分数。"""

    code = "WEIGHTED_SCALARIZATION_REJECTED"


def _metric_of(plan):
    """取排序所用的暖热维度；返回 (metric, declared)。"""
    ranking = plan.get("ranking") or {}
    metric = ranking.get("environment_metric")
    if metric in _METRIC_KEYS:
        return metric, True
    sessions = [s for s in (plan.get("sessions") or []) if isinstance(s, dict)]
    if sessions and all((s.get("environment") or {}).get("metrics", {}).get("max_wbgt_c") is not None
                        for s in sessions):
        return "wbgt", False
    return "temperature", False


def _numeric_finish(sessions):
    ends = []
    for s in sessions:
        try:
            ends.append(parse_time(s["end"]))
        except (KeyError, TypeError, ValueError):
            continue
    return (max(ends) if ends else None), (None if not ends else max(ends).timestamp())


def objective_vector(plan, request=None):
    """从一份既有计划输出抽取目标分量。

    返回 ``status``、``missing``、``notes``、``components``（对外可读值）与
    ``numeric``（内部偏序比较用，JSON 安全）。缺暖热证据时对应分量置 null 并
    标 NEEDS_EVIDENCE，不臆测暴露。
    """
    entry = {"status": "OK", "missing": [], "notes": [],
             "components": {cid: None for cid in COMPONENT_IDS},
             "numeric": {cid: None for cid in COMPONENT_IDS}}
    if not isinstance(plan, dict):
        entry["status"] = "NEEDS_EVIDENCE"
        entry["missing"] = list(COMPONENT_IDS)
        entry["notes"].append("计划输出不可读。")
        return entry

    sessions = [s for s in (plan.get("sessions") or []) if isinstance(s, dict)]
    tasks = [t for t in (plan.get("task_results") or []) if isinstance(t, dict)]

    ratios = [float(t.get("completion_ratio") or 0.0) for t in tasks]
    completion_overall = (sum(ratios) / len(ratios)) if ratios else None
    entry["components"]["completion_overall"] = completion_overall
    entry["numeric"]["completion_overall"] = completion_overall
    if completion_overall is None:
        entry["missing"].append("completion_overall")

    priorities = {}
    if isinstance(request, dict):
        for t in request.get("tasks") or []:
            if isinstance(t, dict) and "id" in t:
                priorities[t["id"]] = t.get("priority")
    if priorities:
        by_priority, tuple_values = {}, []
        for level in (5, 4, 3, 2, 1):
            members = [float(t.get("completion_ratio") or 0.0) for t in tasks
                       if priorities.get(t.get("task_id")) == level]
            if members:
                ratio = round(sum(members) / len(members), 9)
                by_priority[str(level)] = ratio
                tuple_values.append(ratio)
        entry["components"]["completion_by_priority"] = by_priority or None
        entry["numeric"]["completion_by_priority"] = tuple(tuple_values) if tuple_values else None
        if not tuple_values:
            entry["missing"].append("completion_by_priority")
    else:
        entry["notes"].append("未提供请求，无法按优先级分组；只展示整体完成比例。")

    metric, declared = _metric_of(plan)
    entry["notes"].append("暖热维度=%s（%s）。" % (metric, "输出声明" if declared else "按记录推断"))
    if sessions:
        peaks = [(s.get("environment") or {}).get("metrics", {}).get(f"max_{metric}_c")
                 for s in sessions]
        doses = [(s.get("environment") or {}).get("metrics", {}).get(f"{metric}_positive_degree_minutes")
                 for s in sessions]
        if all(v is not None for v in peaks):
            entry["components"]["environment_peak"] = max(0.0, max(peaks))
            entry["numeric"]["environment_peak"] = max(0.0, max(peaks))
        else:
            entry["missing"].append("environment_peak")
        if all(v is not None for v in doses):
            entry["components"]["environment_positive_degree_minutes"] = sum(doses)
            entry["numeric"]["environment_positive_degree_minutes"] = sum(doses)
        else:
            entry["missing"].append("environment_positive_degree_minutes")
    else:
        # 无出工：暴露未评估，取 0 与 engine 打分口径一致，避免奖励空计划。
        entry["components"]["environment_peak"] = 0.0
        entry["numeric"]["environment_peak"] = 0.0
        entry["components"]["environment_positive_degree_minutes"] = 0.0
        entry["numeric"]["environment_positive_degree_minutes"] = 0.0
        entry["notes"].append("本次无出工，暖热暴露未评估，记 0。")

    finish, finish_ts = _numeric_finish(sessions)
    entry["components"]["finish"] = finish.isoformat() if finish else None
    entry["numeric"]["finish"] = finish_ts
    if finish is None:
        entry["notes"].append("无出工，无完成时刻。")

    summary = plan.get("summary") or {}
    active = summary.get("active_person_minutes")
    if not isinstance(active, (int, float)):
        active = sum(float(s.get("active_minutes") or 0.0) for s in sessions)
    entry["components"]["active_person_minutes"] = float(active)
    entry["numeric"]["active_person_minutes"] = float(active)

    entry["components"]["session_count"] = len(sessions)
    entry["numeric"]["session_count"] = len(sessions)

    # 公平性：各人活动分钟的极差（人-人劳动量差距），单人或无出工记 0。
    per_worker = {}
    for session in sessions:
        worker_id = session.get("worker_id") or "?"
        per_worker[worker_id] = per_worker.get(worker_id, 0.0) + float(session.get("active_minutes") or 0.0)
    spread = (max(per_worker.values()) - min(per_worker.values())) if len(per_worker) > 1 else 0.0
    entry["components"]["worker_load_spread"] = round(spread, 9)
    entry["numeric"]["worker_load_spread"] = round(spread, 9)

    # 完成量：未完成任务数。
    unscheduled = sum(1 for t in tasks if float(t.get("completion_ratio") or 0.0) < 1 - EPS)
    entry["components"]["unscheduled_task_count"] = unscheduled
    entry["numeric"]["unscheduled_task_count"] = unscheduled

    if entry["missing"]:
        entry["status"] = "NEEDS_EVIDENCE"
    return entry


def _compare_scalar(a, b, direction):
    if a is None or b is None:
        return None
    if direction == "maximize":
        return (a >= b - EPS, a > b + EPS)
    return (a <= b + EPS, a < b - EPS)


def _compare_component(a, b, direction):
    if isinstance(a, (tuple, list)) or isinstance(b, (tuple, list)):
        if not (isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)) and len(a) == len(b)):
            return None
        ge, any_strict = True, False
        for x, y in zip(a, b):
            result = _compare_scalar(x, y, direction)
            if result is None:
                return None
            ge = ge and result[0]
            any_strict = any_strict or result[1]
        # 严格更优必须同时满足"不劣于全部"且"至少一项更优"。
        return (ge, ge and any_strict)
    return _compare_scalar(a, b, direction)


def dominates(a_numeric, b_numeric):
    """a 是否**严格支配** b：不劣于全部可比分量，且至少一个更优。

    任一分量在一侧为 None 时该分量不可比，跳过；无可比分量则记为不支配，
    避免把缺证据当成"更优"。
    """
    comparable, strict = False, False
    for cid in COMPONENT_IDS:
        result = _compare_component(a_numeric.get(cid), b_numeric.get(cid), _DIRECTION[cid])
        if result is None:
            continue
        comparable = True
        if not result[0]:
            return False
        if result[1]:
            strict = True
    return comparable and strict


def non_dominated(entries):
    """返回未被任何其他项支配的 id 列表（保持输入顺序，确定性）。"""
    kept = []
    for i, item in enumerate(entries):
        dominated = False
        for j, other in enumerate(entries):
            if i == j:
                continue
            if dominates(other["numeric"], item["numeric"]):
                dominated = True
                break
        if not dominated:
            kept.append(item["id"])
    return kept


def reject_weighted_aggregation(weights=None):
    """关闭加权综合接口：指定非空权重即报错，不返回任何综合分。"""
    if weights:
        raise ScalarizationRejected(
            "多目标只分列量纲、不做加权综合：权重会把工程取舍伪装成医学风险系数。"
            "请分别查看各分量，由本人与家属自行判断。")
    return {"scalarization": "NOT_PERFORMED", "weights": None}


def _best_on_component(entries, cid):
    direction = _DIRECTION[cid]
    best, best_value = None, None
    for item in entries:
        value = item["_numeric"].get(cid)
        if value is None:
            continue
        if best is None:
            best, best_value = item, value
            continue
        result = _compare_component(value, best_value, direction)
        if result and result[1]:
            best, best_value = item, value
    return best


def _cap_frontier(frontier, max_keep):
    """从非受支配集合里保留少量取舍不同者：按展示顺序取各分量最优者。"""
    if len(frontier) <= max_keep:
        return list(frontier), []
    chosen, seen = [], set()
    for cid in _DISPLAY_ORDER:
        candidate = _best_on_component(frontier, cid)
        if candidate is not None and candidate["id"] not in seen:
            chosen.append(candidate)
            seen.add(candidate["id"])
        if len(chosen) >= max_keep:
            break
    for item in frontier:
        if len(chosen) >= max_keep:
            break
        if item["id"] not in seen:
            chosen.append(item)
            seen.add(item["id"])
    beyond = [item["id"] for item in frontier if item["id"] not in seen]
    return chosen, beyond


def _trade_off_notes(entry, retained):
    better, worse = [], []
    for cid in _DISPLAY_ORDER:
        value = entry["_numeric"].get(cid)
        if value is None:
            continue
        others = [o["_numeric"].get(cid) for o in retained if o["id"] != entry["id"]]
        others = [v for v in others if v is not None]
        if not others:
            continue
        direction = _DIRECTION[cid]
        # 只在**严格**优于/劣于全部其他保留方案时记名，平手不计。
        is_better = all((_compare_component(value, v, direction) or (False, False))[1] for v in others)
        is_worse = all((_compare_component(v, value, direction) or (False, False))[1] for v in others)
        if is_better and not is_worse:
            better.append(cid)
        elif is_worse and not is_better:
            worse.append(cid)
    return better, worse


def compare_plans(entries, max_keep=MAX_KEEP_DEFAULT, verify=True):
    """并排比较若干可行方案，保留少量取舍不同者，不给综合分。

    ``entries`` 为 ``[{"id", "label"?, "declared_change"?, "request"?, "plan"}]``。
    仅通过全局复查（``audit.verify_plan``）且证据完整的方案参与比较；不放松硬约束。
    """
    if not isinstance(entries, (list, tuple)):
        raise InputError("entries 必须是列表")
    try:
        keep = int(max_keep)
    except (TypeError, ValueError):
        raise InputError("max_keep 必须是整数")
    keep = max(1, min(MAX_KEEP_LIMIT, keep))

    records = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        request, plan = item.get("request"), item.get("plan")
        vector = objective_vector(plan, request)
        record = {"id": item.get("id"), "label": item.get("label"),
                  "declared_change": item.get("declared_change"),
                  "status": vector["status"], "missing": vector["missing"],
                  "notes": vector["notes"], "components": vector["components"],
                  "_numeric": vector["numeric"]}
        if verify and isinstance(request, dict) and isinstance(plan, dict):
            try:
                report = verify_plan(request, plan)
                record["verified"] = bool(report.get("valid"))
                record["verification_errors"] = list(report.get("errors") or [])
            except Exception as exc:  # 复查本身失败 → 不进入比较
                record["verified"] = False
                record["verification_errors"] = ["verify_plan 失败: %s" % exc]
        else:
            record["verified"] = None
        records.append(record)

    eligible = [r for r in records if r["verified"] is not False and r["status"] == "OK"]
    excluded = [{"id": r["id"], "reason": "未通过全局复查", "errors": r.get("verification_errors") or []}
                for r in records if r["verified"] is False]
    needs_evidence = [{"id": r["id"], "missing": r["missing"]} for r in records
                      if r["status"] == "NEEDS_EVIDENCE"]

    frontier_ids = non_dominated([{"id": r["id"], "numeric": r["_numeric"]} for r in eligible])
    frontier = [r for r in eligible if r["id"] in set(frontier_ids)]
    kept, beyond = _cap_frontier(frontier, keep)

    dropped = []
    for r in eligible:
        if r["id"] in {k["id"] for k in kept}:
            continue
        if r["id"] not in set(frontier_ids):
            dominators = [o["id"] for o in eligible
                          if o["id"] != r["id"] and dominates(o["_numeric"], r["_numeric"])]
            dropped.append({"id": r["id"], "reason": "被支配", "dominated_by": dominators})
        else:
            dropped.append({"id": r["id"], "reason": "超出展示上限", "dominated_by": []})

    retained = []
    for item in kept:
        better, worse = _trade_off_notes(item, kept)
        retained.append({"id": item["id"], "label": item["label"],
                         "declared_change": item["declared_change"],
                         "components": item["components"], "missing": item["missing"],
                         "better_on": better, "worse_on": worse})

    for r in records:
        r.pop("_numeric", None)

    warnings = [
        "只并排展示各分量，不合成加权总分，也不输出医学风险分数。",
        "展示不替代本人与家属的取舍判断；方案选择权仍归本人。",
        "被保留方案来自不同的声明输入（如最晚可用起点），差异属输入变化，不是健康效果差异。",
        "只有通过全局复查的可行方案进入比较；本展示不放宽任何既有硬约束。",
        "暖热为零截断口径，不是人体安全阈值；寒冷限制须另行输入。",
    ]
    return {"schema_version": "r16a05-1", "method": "multi_objective_display_non_dominated",
            "objective_components": [dict(c, weight=None) for c in COMPONENTS],
            "scalarization": "NOT_PERFORMED", "weights": None,
            "plans": records, "retained": retained,
            "dropped": dropped, "excluded": excluded, "needs_evidence": needs_evidence,
            "beyond_display_cap": beyond,
            "counts": {"input": len(records), "eligible": len(eligible),
                       "retained": len(retained), "dropped": len(dropped),
                       "excluded": len(excluded), "needs_evidence": len(needs_evidence)},
            "max_keep": keep, "warnings": warnings}


def variant_requests(raw, start_shifts_minutes=(0, 60, 120, 180, 240, 300)):
    """生成声明式输入变体：只平移可用起点，不放松任何个人限值或天气限制。"""
    if not isinstance(raw, dict):
        raise InputError("raw 必须是对象")
    horizon_start, horizon_end, now = raw.get("horizon_start"), raw.get("horizon_end"), raw.get("now")
    try:
        base_start = parse_time(horizon_start)
        base_end = parse_time(horizon_end)
        now_time = parse_time(now) if now else None
    except (TypeError, ValueError):
        raise InputError("horizon_start/horizon_end 必须含时区")

    variants, seen = [], set()
    for shift in start_shifts_minutes:
        try:
            shift = int(shift)
        except (TypeError, ValueError):
            continue
        candidate = base_start + timedelta(minutes=shift)
        if now_time is not None and candidate < now_time:
            candidate = now_time
        if candidate >= base_end:
            continue
        request = copy.deepcopy(raw)
        request["horizon_start"] = candidate.isoformat()
        try:
            validate_request(request)
        except InputError:
            continue
        if request["horizon_start"] in seen:
            continue
        seen.add(request["horizon_start"])
        variants.append({"id": "shift-%d" % shift,
                         "label": "可用起点 %s" % candidate.isoformat(),
                         "declared_change": "horizon_start 平移 %d 分钟（声明的可用起点变化）" % shift,
                         "request": request})
    return variants


def plan_set(raw, start_shifts_minutes=(0, 60, 120, 180, 240, 300),
             max_keep=MAX_KEEP_DEFAULT, verify=True):
    """对声明的输入变体逐一求解，再并排比较，保留少量取舍不同者。"""
    entries = []
    for variant in variant_requests(raw, start_shifts_minutes):
        plan_out = _plan(variant["request"])
        entries.append({"id": variant["id"], "label": variant["label"],
                        "declared_change": variant["declared_change"],
                        "request": variant["request"], "plan": plan_out})
    report = compare_plans(entries, max_keep=max_keep, verify=verify)
    report["variants"] = [{"id": v["id"], "label": v["label"],
                           "declared_change": v["declared_change"]} for v in
                          variant_requests(raw, start_shifts_minutes)]
    return report


def render_table(report):
    """把比较报告渲染为中文表格；只展示，不给结论。"""
    lines = ["# 多目标方案比较（并排展示，不给综合分）", "",
             "| 方案 | 状态 | 复查 | 完成(按优先级) | 整体完成 | 未完成数 | 人员活动极差(公平) | 非负环境峰值 | 非负暖热积分 | 活动人分钟 | 完成时刻 | 出工次数 |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for record in report.get("plans", []):
        components = record["components"]
        by_priority = components.get("completion_by_priority")
        by_priority_text = "；".join("%s:%s" % (k, v) for k, v in sorted(by_priority.items(), reverse=True)) \
            if isinstance(by_priority, dict) else "—"
        verified = record.get("verified")
        verified_text = "—" if verified is None else ("通过" if verified else "未通过")
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            record["id"], record["status"], verified_text, by_priority_text,
            components.get("completion_overall"), components.get("unscheduled_task_count"),
            components.get("worker_load_spread"), components.get("environment_peak"),
            components.get("environment_positive_degree_minutes"),
            components.get("active_person_minutes"), components.get("finish") or "—",
            components.get("session_count")))
    lines += ["", "保留（取舍不同，未排序）：" + "、".join(r["id"] for r in report.get("retained", [])),
              "被支配已剔除：" + ("、".join(d["id"] for d in report.get("dropped", [])
                                            if d["reason"] == "被支配") or "无"),
              "未通过复查故不参与比较：" + ("、".join(e["id"] for e in report.get("excluded", [])) or "无"),
              "加权综合：" + report.get("scalarization", "NOT_PERFORMED"),
              "", "说明："]
    lines += ["- " + w for w in report.get("warnings", [])]
    return "\n".join(lines)
