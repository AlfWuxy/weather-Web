"""工具/帮手被替代或不可用时的排程降级与替代路径检查（只读报告，schema 1.0）。

定位（R16-A08 工具帮手替代情境）：
当一次出工原本依赖的**帮手**或**工具/资源**被替代或变得不可用时，
本模块回答三件事，并给出下一步可核实什么：

1. 替代**需要**什么（速率依据、个体限制、在场状态、可用时段、工具同类与容量）；
   逐项对照输入记录核实，缺什么就报缺什么，**不替替代者编造速率或生成凭空帮手**。
2. 把“不可用”如实作用到输入的**副本**上重新排程，比较排程**降级**（完成比例、余量、
   活动人分钟、出工次数），只报告本次有界搜索与所输入约束下的差异。
3. 给出**替代路径**目录（全部为待核实 PROPOSED_NOT_DONE，带所需证据与证据标签）。

硬约束（不得弱化，也不改动任何既有模块）：
- 只读：`build_substitution_review` 不修改传入对象；作用只发生在内部 ``deepcopy`` 上。
- 不生成凭空帮手：只把“不可用”标记到副本；替代者必须在输入中已存在，否则记
  ``SUBSTITUTE_NOT_IN_INPUT`` 并保持该替代者缺位，绝不新建 worker/resource 记录。
- 不借用他人速率：替代者自身的速率依据缺失时如实报缺（由 ``estimate_task`` 判定）。
- ``substitution_verified_in_field`` 与 ``degradation_proven_for_all_arrangements`` 恒为 False。
- 不输出“已证无解 / 已保证安全 / 现场已验证”一类越权结论；缺数值一律 None。
"""

from __future__ import annotations

import json
from copy import deepcopy

from .models import is_placeholder_source, parse_time, validate_request
from .workload import estimate_task

SCHEMA_VERSION = "1.0"
REVIEW_KIND = "substitution_review"
SYSTEM_STATUS = "SUBSTITUTION_REVIEW_COMPLETED"

# 只允许“带证据标签的待核实方向”，避免把建议写成已完成。
PROPOSED_NOT_DONE = "PROPOSED_NOT_DONE"

SUBSTITUTION_KINDS = ("worker", "resource")

# 替代所需核实的类别（与具体 id 无关的静态清单）。
REQUIREMENT_KINDS = {
    "worker": (
        ("RATE", "该替代者对该任务是否已有可用速率依据（net_work，且来源非占位）"),
        ("LIMITS", "该替代者自己的活动/休息/负重限制是否有可采用来源"),
        ("STATE", "该替代者当前是否处于可出工状态"),
        ("AVAILABILITY", "该替代者的可用时段是否与任务窗口和规划范围相交"),
    ),
    "resource": (
        ("KIND", "替代工具/资源的 kind 是否与原需求一致"),
        ("AVAILABILITY", "替代工具/资源在需要时段是否真的可用"),
        ("CAPACITY", "替代工具/资源的可同时占用容量是否足够"),
    ),
}

# 本模块特有的越权写法（在 work_gaps 既有模式之外补充）。
EXTRA_FORBIDDEN_CLAIM_PATTERNS = (
    "替代已核实",
    "已确认可替代",
    "替代方案已确认",
    "帮手已到位",
    "已找到可用帮手",
    "替代后已证明可行",
)


def find_forbidden_claims(text) -> list:
    """返回命中的越权结论片段；空列表表示通过。复用 work_gaps 的模式并补充替代语境。"""
    from .work_gaps import find_forbidden_claims as _base

    hits = list(_base(text))
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    hits.extend(pattern for pattern in EXTRA_FORBIDDEN_CLAIM_PATTERNS
                if pattern in text and pattern not in hits)
    return hits


def _accepted_review_states(mode) -> set:
    """与 engine._build 对限制来源的取舍保持一致。"""
    return {"confirmed", "illustrative"} if mode == "demonstration" else {"confirmed"}


def _overlaps(intervals, inner_start, inner_end) -> bool:
    for item in intervals or []:
        try:
            start, end = parse_time(item["start"]), parse_time(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < inner_end and end > inner_start:
            return True
    return False


def _worker_requirements(request, worker_id, task_ids, mode) -> list:
    """核实替代人手对该批任务所需的条件；全部只读取输入记录。"""
    workers = {w["id"]: w for w in request["workers"]}
    worker = workers.get(worker_id)
    if worker is None:
        return [{
            "id": "SUBSTITUTE_IN_INPUT", "met": False,
            "detail": f"输入中没有 worker {worker_id!r}；不凭任务描述生成替代人手。",
            "error_code": "SUBSTITUTE_NOT_IN_INPUT",
            "missing_evidence": ["该替代人的真实在场记录（不得由程序凭空生成）"],
        }]

    tasks = {t["id"]: t for t in request["tasks"]}
    accepted = _accepted_review_states(mode)
    limits = worker.get("limits") or {}
    requirements = [{
        "id": "SUBSTITUTE_IN_INPUT", "met": True,
        "detail": f"输入中已存在 worker {worker_id!r}（未新建记录）。",
        "error_code": None, "missing_evidence": [],
    }]

    for task_id in task_ids:
        task = tasks.get(task_id)
        if task is None:
            continue
        try:
            estimate = estimate_task(task, worker_id)
        except Exception as exc:  # 单位不兼容等 → 视为缺该人速率依据
            requirements.append({
                "id": f"RATE:{task_id}", "met": False,
                "detail": f"该替代者对 {task_id} 的速率无法采用：{exc}",
                "error_code": "SUBSTITUTE_RATE_UNUSABLE",
                "missing_evidence": ["该替代人相同做法的净用时依据"],
            })
            continue
        if estimate["schedulable"]:
            requirements.append({
                "id": f"RATE:{task_id}", "met": True,
                "detail": f"该替代者对 {task_id} 已有 net_work 速率依据。",
                "error_code": None, "missing_evidence": [],
            })
        else:
            code = (estimate["reasons"][0]["code"] if estimate["reasons"] else "SUBSTITUTE_RATE_MISSING")
            requirements.append({
                "id": f"RATE:{task_id}", "met": False,
                "detail": f"该替代者对 {task_id} 没有可采用的速率依据：{code}；不借用他人速率。",
                "error_code": "SUBSTITUTE_RATE_MISSING",
                "missing_evidence": ["该替代人相同做法的净用时依据（不得用原承担者速率代替）"],
            })

    limits_ok = (limits.get("review_status") in accepted
                 and (limits.get("max_continuous_active_minutes") or 0) > 0
                 and (limits.get("min_rest_minutes") or 0) > 0
                 and (limits.get("max_active_minutes_per_day") or 0) > 0)
    requirements.append({
        "id": "LIMITS", "met": limits_ok,
        "detail": ("该替代者自己的活动/休息限制可采用。" if limits_ok
                   else "该替代者缺本模式可采用的活动与休息限制。"),
        "error_code": None if limits_ok else "SUBSTITUTE_LIMITS_UNCONFIRMED",
        "missing_evidence": [] if limits_ok else ["该替代人自己的活动/休息/负重限制来源与审核状态"],
    })
    state_ok = worker.get("state") == "clear"
    requirements.append({
        "id": "STATE", "met": state_ok,
        "detail": ("该替代者当前状态为 clear。" if state_ok
                   else f"该替代者当前状态为 {worker.get('state')!r}，不能据此排入。"),
        "error_code": None if state_ok else "SUBSTITUTE_UNAVAILABLE",
        "missing_evidence": [] if state_ok else ["该替代人当时的在场/可用状态记录"],
    })
    try:
        horizon_start, horizon_end = parse_time(request["horizon_start"]), parse_time(request["horizon_end"])
        span_ok = bool(worker.get("availability")) and _overlaps(
            worker.get("availability"), horizon_start, horizon_end)
    except (KeyError, TypeError, ValueError):
        span_ok = False
    requirements.append({
        "id": "AVAILABILITY", "met": span_ok,
        "detail": ("该替代者的可用时段与规划范围相交。" if span_ok
                   else "该替代者的可用时段为空或不与规划范围相交。"),
        "error_code": None if span_ok else "SUBSTITUTE_UNAVAILABLE",
        "missing_evidence": [] if span_ok else ["该替代人的可用时段记录（不得由程序推定）"],
    })
    return requirements


def _resource_requirements(request, resource_id, task_ids, mode, expected_kinds=()) -> list:
    resources = {r["id"]: r for r in request["resources"]}
    resource = resources.get(resource_id)
    if resource is None:
        return [{
            "id": "SUBSTITUTE_IN_INPUT", "met": False,
            "detail": f"输入中没有 resource {resource_id!r}；不凭任务描述生成替代工具。",
            "error_code": "SUBSTITUTE_NOT_IN_INPUT",
            "missing_evidence": ["该替代工具/资源的真实记录（不得由程序凭空生成）"],
        }]

    declared_kind = resource.get("kind")
    requirements = [{
        "id": "SUBSTITUTE_IN_INPUT", "met": True,
        "detail": f"输入中已存在 resource {resource_id!r}（未新建记录）。",
        "error_code": None, "missing_evidence": [],
    }]
    if not expected_kinds:
        kind_ok, kind_detail, kind_code = False, (
            "原资源不在输入中，无法判定 kind 是否同类；不据此声称可替代。"), "SUBSTITUTE_ORIGIN_UNKNOWN"
    elif declared_kind in set(expected_kinds):
        kind_ok, kind_detail, kind_code = True, (
            f"替代资源 kind={declared_kind!r} 与原需求同类。"), None
    else:
        kind_ok, kind_detail, kind_code = False, (
            f"替代资源 kind={declared_kind!r} 与原需求 {sorted(expected_kinds)} 不同类。"), \
            "SUBSTITUTE_RESOURCE_KIND_MISMATCH"
    requirements.append({
        "id": "KIND", "met": kind_ok, "detail": kind_detail,
        "error_code": kind_code,
        "missing_evidence": [] if kind_ok else ["与原需求同类的替代工具/资源的 kind 声明与来源"],
    })
    try:
        horizon_start, horizon_end = parse_time(request["horizon_start"]), parse_time(request["horizon_end"])
        span_ok = bool(resource.get("availability")) and _overlaps(
            resource.get("availability"), horizon_start, horizon_end)
    except (KeyError, TypeError, ValueError):
        span_ok = False
    requirements.append({
        "id": "AVAILABILITY", "met": span_ok,
        "detail": ("替代资源在规划范围内有可用时段。" if span_ok
                   else "替代资源在规划范围内没有可用时段。"),
        "error_code": None if span_ok else "SUBSTITUTE_TOOL_UNAVAILABLE",
        "missing_evidence": [] if span_ok else ["该替代工具/资源的可用区间声明"],
    })
    capacity_ok = int(resource.get("capacity") or 0) >= 1
    requirements.append({
        "id": "CAPACITY", "met": capacity_ok,
        "detail": (f"替代资源可同时占用容量={resource.get('capacity')}。" if capacity_ok
                   else "替代资源的可同时占用容量不足 1。"),
        "error_code": None if capacity_ok else "SUBSTITUTE_TOOL_UNAVAILABLE",
        "missing_evidence": [] if capacity_ok else ["该替代工具/资源的容量声明"],
    })
    return requirements


def _affected_task_ids(request, kind, unavailable_ids) -> list:
    """不可用对象直接牵动的任务：人手看 rates，资源看 required_resources。"""
    affected = []
    if kind == "worker":
        wanted = set(unavailable_ids)
        for task in request["tasks"]:
            if wanted & set((task.get("rates") or {}).keys()):
                affected.append(task["id"])
    else:
        kinds = {r["id"]: (r.get("kind") or "") for r in request["resources"]}
        wanted_ids = set(unavailable_ids)
        wanted_kinds = {kinds.get(rid) for rid in wanted_ids if kinds.get(rid)}
        for task in request["tasks"]:
            required = set(task.get("required_resources") or [])
            if required & wanted_ids:
                affected.append(task["id"])
                continue
            if wanted_kinds and {kinds.get(rid) for rid in required} & wanted_kinds:
                affected.append(task["id"])
    return affected


def _normalize_operations(substitution) -> list:
    if not isinstance(substitution, dict):
        raise ValueError("substitution: 必须为对象，含 operations 或 kind/unavailable")
    raw = substitution.get("operations")
    if raw is None:
        raw = [{"kind": substitution.get("kind"),
                "unavailable": substitution.get("unavailable"),
                "substitute": substitution.get("substitute")}]
    if not isinstance(raw, list) or not raw:
        raise ValueError("substitution.operations: 必须为至少一项的数组")
    operations = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"substitution.operations[{index}]: 必须为对象")
        kind = item.get("kind")
        if kind not in SUBSTITUTION_KINDS:
            raise ValueError(f"substitution.operations[{index}].kind: 只能是 {'/'.join(SUBSTITUTION_KINDS)}")
        unavailable = item.get("unavailable")
        if not isinstance(unavailable, list) or not unavailable or not all(
                isinstance(v, str) and v for v in unavailable):
            raise ValueError(f"substitution.operations[{index}].unavailable: 必须为非空 id 数组")
        substitute = item.get("substitute")
        if substitute is not None and not (isinstance(substitute, str) and substitute):
            raise ValueError(f"substitution.operations[{index}].substitute: 只能为字符串 id 或省略")
        operations.append({"kind": kind, "unavailable": list(unavailable), "substitute": substitute})
    return operations


def _apply_substitution(request, operations) -> tuple:
    """把替代情境作用到副本，绝不新增记录。

    - 不可用：人手置 state=stop；资源清空可用区间。
    - 替代：只对**已存在**且**同类**的资源把 required_resources 改指替代 id；
      人手本就在 worker 列表中，无需新增；替代者不在输入中时保持缺位。
    """
    workers = {w["id"]: w for w in request["workers"]}
    resources = {r["id"]: r for r in request["resources"]}
    applied = []
    for op in operations:
        for rid in op["unavailable"]:
            if op["kind"] == "worker":
                if rid in workers:
                    workers[rid]["state"] = "stop"
                    applied.append({"role": "unavailable", "kind": "worker",
                                    "id": rid, "action": "state=stop"})
                else:
                    applied.append({"role": "unavailable", "kind": "worker",
                                    "id": rid, "action": "absent_in_input"})
            else:
                if rid in resources:
                    resources[rid]["availability"] = []
                    applied.append({"role": "unavailable", "kind": "resource",
                                    "id": rid, "action": "availability=[]"})
                else:
                    applied.append({"role": "unavailable", "kind": "resource",
                                    "id": rid, "action": "absent_in_input"})
        substitute = op["substitute"]
        if substitute is None:
            continue
        if op["kind"] == "worker":
            action = ("already_present_no_change" if substitute in workers
                      else "not_in_input_not_created")
            applied.append({"role": "substitute", "kind": "worker",
                            "id": substitute, "action": action})
            continue
        if substitute not in resources:
            applied.append({"role": "substitute", "kind": "resource",
                            "id": substitute, "action": "not_in_input_not_created"})
            continue
        original_kinds = {resources[rid].get("kind") for rid in op["unavailable"] if rid in resources}
        if resources[substitute].get("kind") in original_kinds:
            rewired = 0
            for task in request["tasks"]:
                required = task.get("required_resources") or []
                updated = [substitute if rid in op["unavailable"] else rid for rid in required]
                if updated != required:
                    task["required_resources"] = updated
                    rewired += 1
            applied.append({"role": "substitute", "kind": "resource", "id": substitute,
                            "action": f"rewired_required_resources_tasks={rewired}"})
        else:
            applied.append({"role": "substitute", "kind": "resource", "id": substitute,
                            "action": "kind_mismatch_not_wired"})
    return request, applied


def _task_ratios(plan_result) -> dict:
    return {tr["task_id"]: float(tr.get("completion_ratio") or 0.0)
            for tr in (plan_result.get("task_results") or [])}


def _degradation(base_plan, sub_plan) -> dict:
    base, sub = _task_ratios(base_plan), _task_ratios(sub_plan)
    eps = 1e-9
    total_base, total_sub = sum(base.values()), sum(sub.values())
    if total_sub < total_base - eps:
        direction = "degraded"
    elif total_sub > total_base + eps:
        direction = "improved"
    else:
        direction = "unchanged"
    per_task = []
    for tid in sorted(set(base) | set(sub)):
        before, after = base.get(tid, 0.0), sub.get(tid, 0.0)
        delta = after - before
        per_task.append({
            "task_id": tid,
            "completion_ratio_before": before,
            "completion_ratio_after": after,
            "change": "degraded" if delta < -eps else ("improved" if delta > eps else "unchanged"),
        })
    base_summary = base_plan.get("summary") or {}
    sub_summary = sub_plan.get("summary") or {}
    return {
        "direction": direction,
        "total_completion_ratio_before": total_base,
        "total_completion_ratio_after": total_sub,
        "newly_incomplete_tasks": [t["task_id"] for t in per_task
                                   if t["change"] == "degraded" and t["completion_ratio_after"] < 1.0],
        "recovered_tasks": [t["task_id"] for t in per_task if t["change"] == "improved"],
        "active_person_minutes_before": base_summary.get("active_person_minutes"),
        "active_person_minutes_after": sub_summary.get("active_person_minutes"),
        "session_count_before": len(base_plan.get("sessions") or []),
        "session_count_after": len(sub_plan.get("sessions") or []),
        "per_task": per_task,
        "degradation_proven_for_all_arrangements": False,
        "note": "该差异只是本次输入与有界搜索的结果；不等于任何替代安排在现实中必然如此。",
    }


def _alternative_paths(sub_plan) -> list:
    """从降级后的排程结果汇总待核实替代路径（复用 work_gaps 的方向目录）。"""
    from .work_gaps import build_gap_report

    report = build_gap_report(sub_plan)
    grouped: dict = {}
    for task in report["tasks"]:
        for alt in task["alternatives"]:
            entry = grouped.setdefault(alt["code"], {
                "code": alt["code"], "text": alt["text"],
                "requires_evidence": list(alt["requires_evidence"]),
                "evidence_label": alt["evidence_label"],
                "status": alt["status"],
                "task_ids": [],
            })
            if task["task_id"] not in entry["task_ids"]:
                entry["task_ids"].append(task["task_id"])
    return [grouped[code] for code in sorted(grouped)]


def build_substitution_review(request, substitution) -> dict:
    """核实替代条件、量化排程降级、列出替代路径。只读，不修改输入。"""
    if not isinstance(request, dict):
        raise TypeError("request 必须是请求对象")
    if request.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的 request.schema_version")
    operations = _normalize_operations(substitution)
    normalized = validate_request(deepcopy(request))
    mode = normalized["mode"]

    original_workers = {w["id"] for w in normalized["workers"]}
    original_resources = {r["id"] for r in normalized["resources"]}

    reviewed = []
    for op in operations:
        task_ids = _affected_task_ids(normalized, op["kind"], op["unavailable"])
        substitute = op["substitute"]
        entry = {
            "kind": op["kind"],
            "unavailable_ids": list(op["unavailable"]),
            "affected_task_ids": task_ids,
            "proposed_substitute": substitute,
            "substitute_in_input": None,
            "requirements": [],
            "required_evidence_checklist": [
                {"requirement": label, "meaning": meaning}
                for label, meaning in REQUIREMENT_KINDS[op["kind"]]
            ],
            "status": PROPOSED_NOT_DONE,
        }
        if substitute is None:
            entry["requirements"] = [{
                "id": "SUBSTITUTE_PROVIDED", "met": False,
                "detail": "未提供替代者 id；程序不替本人选定帮手或工具。",
                "error_code": "SUBSTITUTE_NOT_PROVIDED",
                "missing_evidence": ["由本人/家属确认的替代人或替代工具 id"],
            }]
        else:
            entry["substitute_in_input"] = (
                substitute in original_workers if op["kind"] == "worker"
                else substitute in original_resources)
            if op["kind"] == "worker":
                entry["requirements"] = _worker_requirements(normalized, substitute, task_ids, mode)
            else:
                by_id = {r["id"]: r for r in normalized["resources"]}
                expected_kinds = {by_id[rid].get("kind") for rid in op["unavailable"] if rid in by_id}
                entry["requirements"] = _resource_requirements(
                    normalized, substitute, task_ids, mode, expected_kinds)
        entry["unmet_requirements"] = [r["id"] for r in entry["requirements"] if not r["met"]]
        reviewed.append(entry)

    substituted = deepcopy(normalized)
    substituted, applied = _apply_substitution(substituted, operations)

    # 不生成凭空帮手：副本里不得出现原输入没有的 worker/resource id。
    introduced_workers = sorted({w["id"] for w in substituted["workers"]} - original_workers)
    introduced_resources = sorted({r["id"] for r in substituted["resources"]} - original_resources)
    fabrication_check = {
        "no_phantom_helper": not introduced_workers and not introduced_resources,
        "introduced_worker_ids": introduced_workers,
        "introduced_resource_ids": introduced_resources,
        "note": "替代者必须已在输入中；本模块只标记不可用，不新增 worker/resource 记录。",
    }
    if not fabrication_check["no_phantom_helper"]:
        raise AssertionError("替代审查不得向输入副本新增 worker/resource 记录")

    from .engine import plan

    base_plan = plan(deepcopy(normalized))
    sub_plan = plan(substituted)
    review = {
        "schema_version": SCHEMA_VERSION,
        "review_kind": REVIEW_KIND,
        "mode": mode,
        "system_status": SYSTEM_STATUS,
        "system_status_note": "本审查正常完成；这不是系统故障报告，也不是个人健康结论。",
        "operations": reviewed,
        "applied_unavailability": applied,
        "fabrication_check": fabrication_check,
        "baseline_plan_status": base_plan.get("status"),
        "substituted_plan_status": sub_plan.get("status"),
        "degradation": _degradation(base_plan, sub_plan),
        "alternative_paths": _alternative_paths(sub_plan),
        "substitution_verified_in_field": False,
        "degradation_proven_for_all_arrangements": False,
        "evidence_labels": ["SYNTHETIC", "IMPLEMENTATION_TEST"],
        "notes": [
            "本审查只核实替代所需的条件与本次排程差异，不认证任何替代方案在现实中可行。",
            "替代者必须在输入中已存在；程序不凭任务描述生成帮手或工具，也不借用他人速率。",
            "所有替代路径均为待核实（PROPOSED_NOT_DONE），取得相应证据前不得当作已完成。",
            "降级差异是本次有界搜索的结果；天气、本人状态、人手或进度变化后应以新输入重排。",
        ],
    }
    violations = assert_review_invariants(review)
    if violations:
        raise AssertionError("替代审查违反硬约束: " + "; ".join(violations))
    return review


def assert_review_invariants(review) -> list:
    """返回违反项列表；空列表表示满足全部硬约束。"""
    violations = []
    if review.get("system_status") != SYSTEM_STATUS:
        violations.append(f"system_status 只能是 {SYSTEM_STATUS}")
    for key in ("substitution_verified_in_field", "degradation_proven_for_all_arrangements"):
        if review.get(key) is not False:
            violations.append(f"{key} 必须为 False")
    if review.get("degradation", {}).get("degradation_proven_for_all_arrangements") is not False:
        violations.append("degradation.degradation_proven_for_all_arrangements 必须为 False")
    check = review.get("fabrication_check") or {}
    if check.get("no_phantom_helper") is not True:
        violations.append("no_phantom_helper 必须为 True")
    if check.get("introduced_worker_ids") or check.get("introduced_resource_ids"):
        violations.append("不得向输入引入新的 worker/resource id")
    for op in review.get("operations", []):
        if op.get("status") != PROPOSED_NOT_DONE:
            violations.append(f"{op.get('kind')}: 操作状态必须是 {PROPOSED_NOT_DONE}")
        if op.get("proposed_substitute") and op.get("substitute_in_input") is False:
            for req in op.get("requirements", []):
                if req["id"] == "SUBSTITUTE_IN_INPUT" and req.get("met"):
                    violations.append("替代者不在输入中时不得标记为已核实")
    for path in review.get("alternative_paths", []):
        if path.get("status") != PROPOSED_NOT_DONE:
            violations.append(f"{path.get('code')}: 替代路径必须是 {PROPOSED_NOT_DONE}")
    if find_forbidden_claims(json.dumps(review, ensure_ascii=False)):
        violations.append("越权结论片段: " + ", ".join(find_forbidden_claims(json.dumps(review, ensure_ascii=False))))
    return violations


def summarize_review(review) -> str:
    """一行人类可读摘要；不含健康或可行性结论。"""
    degradation = review["degradation"]
    unmet = [req for op in review["operations"] for req in op["unmet_requirements"]]
    return ("替代审查：{ops} 项操作，{unmet} 项替代条件未核实；"
            "排程方向 {direction}（完成比例 {before:.3f}→{after:.3f}）；"
            "待核实替代路径 {paths} 条。").format(
        ops=len(review["operations"]), unmet=len(unmet),
        direction=degradation["direction"],
        before=degradation["total_completion_ratio_before"],
        after=degradation["total_completion_ratio_after"],
        paths=len(review["alternative_paths"]))
