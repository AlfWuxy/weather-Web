"""工作缺口、未知项与可核实替代方向（只读报告，schema 1.0）。

定位（R15-A08 无人手与无窗口）：
把既有 ``yilao_agri.plan()`` 的结果翻译成“还差什么、还缺什么、下一步可核实什么”，
而不是给出“到底能不能做完 / 是否安全”的结论。

硬约束（不得弱化，也不改动任何既有模块）：
- 只读：不排程、不估算、不生成处方、不修改输入计划。
- ``gap_is_proven_unavoidable`` 恒为 False：余量只是本次有界搜索与所输入约束的结果。
- 缺人手只报“缺可用人手”，不写成“已证无解”；缺窗口只报“无通过窗口”。
- 不输出系统崩溃 / 个人健康结论 / 现场已验证一类的话；只报告缺口、未知与待核实方向。
- 缺数值一律 None，不编数。
"""

from __future__ import annotations

import json

SCHEMA_VERSION = "1.0"
REPORT_KIND = "work_gap_report"

# 只允许“带证据标签的待核实方向”，避免把建议写成已完成。
PROPOSED_NOT_DONE = "PROPOSED_NOT_DONE"

# 拒绝码 -> 缺口类型。未列出的码保守归入 UNKNOWN_INPUT（不猜原因）。
GAP_KIND_BY_CODE = {
    "NO_WORKER": "NO_MANPOWER",
    "WORKER_STATE_NOT_CLEAR": "NO_MANPOWER",
    "MISSING_WORKER_RATE": "NO_MANPOWER_RATE",
    "WORKER_UNAVAILABLE": "NO_WINDOW",
    "WORKER_CONFLICT_OR_REST": "NO_WINDOW",
    "DAILY_ACTIVE_LIMIT": "NO_WINDOW",
    "DEADLINE_OR_HORIZON": "NO_WINDOW",
    "TASK_PARALLELISM": "NO_WINDOW",
    "MISSING_RESOURCE_KIND": "NO_TOOL",
    "RESOURCE_UNAVAILABLE": "NO_TOOL",
    "RESOURCE_CAPACITY": "NO_TOOL",
    "DEPENDENCY_INCOMPLETE": "DEPENDENCY_NOT_DONE",
    "INTEGER_UNIT_SPLIT": "SPLIT_NOT_ALLOWED",
    "WORKER_LIMITS_UNCONFIRMED": "UNKNOWN_INPUT",
    "RATE_SOURCE_UNKNOWN": "UNKNOWN_INPUT",
    "WHOLE_SESSION_RATE": "UNKNOWN_INPUT",
}

GAP_KIND_LABELS = {
    "NO_MANPOWER": "缺可用人手",
    "NO_MANPOWER_RATE": "缺该人的作业速率依据",
    "NO_TOOL": "缺工具或必要资源",
    "NO_WINDOW": "在输入约束下本时段无通过窗口",
    "DEPENDENCY_NOT_DONE": "前置任务尚未完成",
    "SPLIT_NOT_ALLOWED": "该计量单位不能拆成小数",
    "UNKNOWN_INPUT": "输入未核实 / 未知",
}

# 缺口类型 -> 可核实替代方向（每条都必须先取得证据才算数）。
DIRECTION_CATALOG = {
    "VERIFY_SPLIT_PARTS": {
        "text": "核实可分工部分、每趟/每段最小作业量是否成立",
        "requires_evidence": ["任务拆分依据（非模型假定）", "该单位是否允许小数"],
        "evidence_label": "FIELD_OBSERVATION",
    },
    "VERIFY_HELPER_LIMITS_AND_TIME": {
        "text": "核实本人或替代人手的限制、可用时间与作业速率依据",
        "requires_evidence": ["该人自己的活动/休息/负重限制来源", "该人的可用时段", "该人的速率依据"],
        "evidence_label": "PROFESSIONAL_REVIEW",
    },
    "VERIFY_ALTERNATIVE_TOOL_AND_RATE": {
        "text": "核实替代工具及其对应作业速率",
        "requires_evidence": ["替代工具的可得性声明", "该工具的速率依据"],
        "evidence_label": "FIELD_OBSERVATION",
    },
    "VERIFY_RESOURCE_AVAILABILITY": {
        "text": "核实该任务所需工具/资源在本时段是否真的可用",
        "requires_evidence": ["工具/资源的可用区间声明", "资源同时占用容量"],
        "evidence_label": "FIELD_OBSERVATION",
    },
    "VERIFY_AGRONOMIC_DEFERRAL": {
        "text": "经农艺确认后调整期限或本次范围",
        "requires_evidence": ["改期是否被农艺允许（含条件）", "改期不影响后续环节的依据"],
        "evidence_label": "PROFESSIONAL_REVIEW",
    },
    "VERIFY_INPUT_FIELDS": {
        "text": "补齐并核实该任务缺失的输入字段",
        "requires_evidence": ["缺失字段的真实取值与来源"],
        "evidence_label": "PRIMARY_SOURCE",
    },
    "VERIFY_DEPENDENCY_COMPLETION": {
        "text": "核实前置任务是否已完成并可交接",
        "requires_evidence": ["前置任务的完成记录/交接声明"],
        "evidence_label": "FIELD_OBSERVATION",
    },
    "RECORD_FIELD_OBSERVATION": {
        "text": "按自然作业记录该字段，供下次排程使用",
        "requires_evidence": ["现场记录（保留未做与中断）"],
        "evidence_label": "FIELD_OBSERVATION",
    },
}

DIRECTIONS_BY_GAP_KIND = {
    "NO_MANPOWER": ["VERIFY_HELPER_LIMITS_AND_TIME", "VERIFY_SPLIT_PARTS"],
    "NO_MANPOWER_RATE": ["VERIFY_HELPER_LIMITS_AND_TIME", "RECORD_FIELD_OBSERVATION"],
    "NO_TOOL": ["VERIFY_RESOURCE_AVAILABILITY", "VERIFY_ALTERNATIVE_TOOL_AND_RATE"],
    "NO_WINDOW": ["VERIFY_AGRONOMIC_DEFERRAL", "VERIFY_INPUT_FIELDS"],
    "DEPENDENCY_NOT_DONE": ["VERIFY_DEPENDENCY_COMPLETION"],
    "SPLIT_NOT_ALLOWED": ["VERIFY_SPLIT_PARTS"],
    "UNKNOWN_INPUT": ["VERIFY_INPUT_FIELDS", "RECORD_FIELD_OBSERVATION"],
}

FIELD_HINT_BY_CODE = {
    "WORKER_LIMITS_UNCONFIRMED": "workers[].limits.review_status 与 max_*_minutes",
    "WORKER_STATE_NOT_CLEAR": "workers[].state",
    "RATE_SOURCE_UNKNOWN": "tasks[].rates[worker].source",
    "WHOLE_SESSION_RATE": "tasks[].rates[worker].scope",
    "MISSING_WORKER_RATE": "tasks[].rates[worker]（缺该人速率）",
    "DEADLINE_SOURCE_UNKNOWN": "tasks[].deadline_source",
    "WORKER_LOAD_LIMIT_UNKNOWN": "workers[].limits.max_load_kg",
    "TASK_LOAD_UNKNOWN": "tasks[].load_per_trip_kg",
}

# 一旦出现即视为越权结论：本模块必须永不产出。
FORBIDDEN_CLAIM_PATTERNS = (
    "已证无解",
    "证明不可能",
    "不可能完成",
    "系统崩溃",
    "程序崩溃",
    "系统异常终止",
    "已保证安全",
    "个人健康结论成立",
    "现场已验证",
    "现场效果已确认",
)

GAP_KIND_ORDER = (
    "NO_MANPOWER", "NO_MANPOWER_RATE", "NO_TOOL", "NO_WINDOW",
    "DEPENDENCY_NOT_DONE", "SPLIT_NOT_ALLOWED", "UNKNOWN_INPUT",
)


def gap_kind_for_code(code) -> str:
    """把拒绝码归类到缺口类型；未知码保守归入 UNKNOWN_INPUT。"""
    if not isinstance(code, str) or not code:
        return "UNKNOWN_INPUT"
    if code in GAP_KIND_BY_CODE:
        return GAP_KIND_BY_CODE[code]
    if code.endswith("SOURCE_UNKNOWN") or "UNCONFIRMED" in code or code.endswith("_INVALID"):
        return "UNKNOWN_INPUT"
    if "UNKNOWN" in code:
        return "UNKNOWN_INPUT"
    if code.startswith("WEATHER") or code in {
        "OUTSIDE_PLANNING_HORIZON", "INTERVAL_EMPTY", "INTERVAL_TIME_INVALID", "HORIZON_INVALID",
    }:
        return "NO_WINDOW"
    return "UNKNOWN_INPUT"


def _direction(code) -> dict:
    spec = DIRECTION_CATALOG[code]
    return {
        "code": code,
        "text": spec["text"],
        "requires_evidence": list(spec["requires_evidence"]),
        "evidence_label": spec["evidence_label"],
        "status": PROPOSED_NOT_DONE,
    }


def _ordered(values) -> list:
    seen, out = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _sorted_kinds(kinds) -> list:
    unique = set(kinds)
    ordered = [k for k in GAP_KIND_ORDER if k in unique]
    ordered += sorted(unique - set(ordered))
    return ordered


def directions_for_kinds(kinds) -> list:
    codes = []
    for kind in _sorted_kinds(kinds):
        codes.extend(DIRECTIONS_BY_GAP_KIND.get(kind, []))
    return [_direction(code) for code in _ordered(codes)]


def find_forbidden_claims(text) -> list:
    """返回文本中命中的越权结论片段；空列表表示通过。"""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return [pattern for pattern in FORBIDDEN_CLAIM_PATTERNS if pattern in text]


def _task_gap_entry(tr) -> dict:
    remaining = tr.get("remaining_quantity") or 0.0
    codes = [reason.get("code") for reason in (tr.get("reasons") or [])]
    kinds = _sorted_kinds(gap_kind_for_code(code) for code in codes)
    gaps = []
    for reason in (tr.get("reasons") or []):
        code = reason.get("code")
        gaps.append({
            "code": code,
            "kind": gap_kind_for_code(code),
            "kind_label": GAP_KIND_LABELS[gap_kind_for_code(code)],
            "message": reason.get("message"),
            "candidate_rejection_count": reason.get("candidate_rejection_count"),
            "field_hint": FIELD_HINT_BY_CODE.get(code),
        })
    unknowns = [g for g in gaps if g["kind"] == "UNKNOWN_INPUT"]
    alternatives = directions_for_kinds(kinds)
    if not alternatives and remaining > 0:
        # 有未完成余量但没有显式拒绝码：仍给保守的补齐方向，不猜原因。
        alternatives = [_direction("VERIFY_INPUT_FIELDS")]
    complete = remaining <= 0
    return {
        "task_id": tr.get("task_id"),
        "status": tr.get("status"),
        "complete": complete,
        "requested_quantity": tr.get("requested_quantity"),
        "scheduled_quantity": tr.get("scheduled_quantity"),
        "remaining_quantity": tr.get("remaining_quantity"),
        "unit": tr.get("unit"),
        "gap_kinds": [] if complete else kinds,
        "gap_is_proven_unavoidable": False,
        "gaps": [] if complete else gaps,
        "unknowns": [] if complete else unknowns,
        "unverified_estimates": tr.get("remaining_work_estimates") or [],
        "alternatives": [] if complete else alternatives,
        "engine_alternatives": tr.get("alternatives") or [],
        "deadline_basis": tr.get("deadline_basis"),
    }


def _window_gap_disclosure(report) -> dict:
    if not any(t["status"] == "not_fully_scheduled" for t in report["tasks"]):
        return None
    return {
        "has_window_gap": True,
        "note": "“无通过窗口”是本次有界搜索与所输入约束的结果，不等于证明任何安排都做不成。",
        "search_optimality_proven": report["search"].get("optimality_proven"),
        "search_budget_exhausted": report["search"].get("budget_exhausted"),
        "search_limit_reached": report["search"].get("candidate_check_limit_reached"),
    }


def _budget_disclosure(report) -> dict:
    """候选检查预算的分配与实际未充分探索范围；无事那么返回 None。"""
    search = report.get("search") or {}
    fair = search.get("fair_allocation")
    if not isinstance(fair, dict):
        return None
    unexplored = fair.get("unexplored_pairs") or []
    if not unexplored and not search.get("budget_exhausted") and not search.get("candidate_check_limit_reached"):
        return None
    return {
        "fair_allocation_policy": fair.get("policy"),
        "fair_share_cap_per_pair": fair.get("fair_share_cap_per_pair"),
        "candidate_check_budget": fair.get("candidate_check_budget"),
        "unexplored_pair_count": len(unexplored),
        "unexplored_pairs": unexplored,
        "candidate_check_limit_reached": search.get("candidate_check_limit_reached"),
        "state_budget_exhausted": search.get("budget_exhausted"),
        "note": "预算用尽只说明本次有界搜索未穷尽这些(任务,人)组合，不等于任何安排都做不成；未列出的组合可能只是受约束限制。",
    }


def build_gap_report(plan, *, now=None) -> dict:
    """把 plan() 结果翻译成缺口报告。只读，不改变输入。"""
    if not isinstance(plan, dict):
        raise TypeError("plan 必须是 yilao_agri.plan() 返回的对象")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的 plan.schema_version")
    tasks = [_task_gap_entry(tr) for tr in (plan.get("task_results") or [])]
    all_kinds = _ordered(k for t in tasks for k in t["gap_kinds"])
    unknowns = [u for t in tasks for u in t["unknowns"]]
    report = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "plan_id": plan.get("plan_id"),
        "request_sha256": plan.get("request_sha256"),
        "mode": plan.get("mode"),
        "as_of": plan.get("as_of"),
        "now": plan.get("as_of") if now is None else now,
        "system_status": "GAPS_REPORTED_SEARCH_COMPLETED",
        "system_status_note": "本次搜索正常完成并返回结果；这不是系统故障报告，也不是健康结论。",
        "plan_status": plan.get("status"),
        "gap_is_proven_unavoidable": False,
        "gap_kinds_present": all_kinds,
        "search": dict(plan.get("search") or {}),
        "tasks": tasks,
        "unknowns": unknowns,
        "window_gap": None,
        "summary": {},
        "notes": [
            "本报告只转述既有排程结果的缺口、未知与待核实方向，不重新排程、不估算、不生成处方。",
            "缺人手只表示“本次输入里没有可用人手”，不等于已证明这件事没人能做。",
            "所有替代方向均为待核实（PROPOSED_NOT_DONE），取得相应证据前不得当作已完成方案。",
            "余量是本次有界搜索的结果；天气、本人状态、人手或进度变化后应以新输入重新排程。",
        ],
    }
    report["summary"] = {
        "task_count": len(tasks),
        "complete_task_count": sum(t["complete"] for t in tasks),
        "incomplete_task_count": sum(not t["complete"] for t in tasks),
        "gap_kind_counts": {k: sum(k in t["gap_kinds"] for t in tasks) for k in all_kinds},
        "unknown_count": len(unknowns),
    }
    report["window_gap"] = _window_gap_disclosure(report)
    report["budget_disclosure"] = _budget_disclosure(report)
    violations = assert_report_invariants(report)
    if violations:
        raise AssertionError("缺口报告违反硬约束: " + "; ".join(violations))
    return report


def assert_report_invariants(report) -> list:
    """返回违反项列表；空列表表示报告满足全部硬约束。"""
    violations = []
    if report.get("gap_is_proven_unavoidable") is not False:
        violations.append("gap_is_proven_unavoidable 必须为 False")
    for task in report.get("tasks", []):
        if task.get("gap_is_proven_unavoidable") is not False:
            violations.append(f"{task.get('task_id')}: gap_is_proven_unavoidable 必须为 False")
        for alt in task.get("alternatives", []):
            if alt.get("status") != PROPOSED_NOT_DONE:
                violations.append(f"{task.get('task_id')}: 替代方向必须是 {PROPOSED_NOT_DONE}")
    blobs = [report.get("system_status_note", ""), " ".join(report.get("notes", []))]
    for task in report.get("tasks", []):
        blobs.extend(g.get("message") or "" for g in task.get("gaps", []))
        blobs.extend(a.get("text", "") for a in task.get("alternatives", []))
    for blob in blobs:
        hits = find_forbidden_claims(blob)
        if hits:
            violations.append("越权结论片段: " + ", ".join(hits))
    if report.get("system_status") not in {"GAPS_REPORTED_SEARCH_COMPLETED"}:
        violations.append("system_status 只能是 GAPS_REPORTED_SEARCH_COMPLETED")
    return violations


def summary_line(report) -> str:
    """一行人类可读摘要，便于日志与界面标题；不含健康或可行性结论。"""
    summary = report["summary"]
    kinds = "、".join(GAP_KIND_LABELS[k] for k in report["gap_kinds_present"]) or "无已分类缺口"
    return ("共 {task_count} 项任务，其中 {incomplete_task_count} 项未排满；"
            "缺口类型：{kinds}；待核实方向 {unknown_count} 条未知项。").format(
        task_count=summary["task_count"],
        incomplete_task_count=summary["incomplete_task_count"],
        kinds=kinds, unknown_count=summary["unknown_count"])
