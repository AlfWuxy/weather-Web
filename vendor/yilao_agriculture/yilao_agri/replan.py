"""身体状态或帮手（人手）变化后的重排检查：旧计划及时失效 + 按新输入重新筛选（只读，schema 1.0）。

定位（R17-A05 身体或帮手变化重排）：
给出一份**已经算好的旧计划**与一份**更新后的输入**（例如本人体力/限制变化、当日状态异常，
或一名帮手被撤回/不再可用），本模块回答两件事，并且只做被要求的两件事：

1. **旧计划是否必须及时失效**：把旧计划里的每一段出工，逐段对照新输入里的硬约束重新核实
   （人员在不在、状态是否 clear、限制是否仍可采用、可用时段是否仍覆盖、负重/禁用标签、
   任务速率、所需资源与规划范围、以及该段是否在 ``now`` 之前就已开始）。任何一条不满足，
   该段即判 INVALIDATED，旧计划不得原样沿用。
2. **按新输入重新筛选**：在**新输入的副本**上重新排程，给出排程方向变化（完成比例、余量、
   活动人分钟、出工次数）与逐任务差异，并列出下一步可核实的方向。

硬约束（不得弱化，也不改动任何既有模块）：
- 只读：``build_replan_check`` 不修改传入的旧计划或新请求；一切作用只发生在内部 ``deepcopy`` 上。
- 旧计划失效是**加法**：本模块只新增“必须重排 / 不得沿用”的判定，绝不改动 engine / audit 的既有结果。
- 不生成凭空帮手：重新排程只在**新输入已存在的** worker/resource 上进行；不得新增记录。
- 缺数值一律 None：不编造完成比例、余量或任何健康/可行性结论。
- ``old_plan_reusable`` 只有在输入未变且全部分段仍满足新输入硬约束时才可为 True。
- ``rescreen_proves_infeasible`` / ``field_verified`` / ``health_or_safety_verified`` 恒为 False。
- 不输出“已证无解 / 已保证安全 / 现场已验证 / 排得进去即个人安全”一类越权结论。
"""

from __future__ import annotations

import json
from copy import deepcopy

from .models import InputError, parse_time, request_digest, validate_request, worker_profile_status
from .workload import estimate_task

SCHEMA_VERSION = "1.0"
REVIEW_KIND = "replan_check"
SYSTEM_STATUS = "REPLAN_CHECK_COMPLETED"

# 只允许“带证据标签的待核实方向”，避免把建议写成已完成。
PROPOSED_NOT_DONE = "PROPOSED_NOT_DONE"

# 旧计划的绑定状态。
PRIOR_CURRENT = "CURRENT_BINDING"
PRIOR_SUPERSEDED = "SUPERSEDED_BY_NEW_INPUT"

# 单段出工的重筛结论。
STILL_VALID = "STILL_VALID"
INVALIDATED = "INVALIDATED"
NOT_FOUND_IN_PRIOR = "NO_PRIOR_SESSION"

# 失效原因码 -> 变化轴。轴用于把“身体状态变化 / 人手撤回 / 资源 / 任务 / 时间”分开汇报，
# 程序不判断谁是被照护者、谁是帮手，只按输入字段如实归类。
AXIS_BY_CODE = {
    "WORKER_WITHDRAWN": "manpower_withdrawal",
    "WORKER_STATE_NOT_CLEAR": "personal_state",
    "WORKER_LIMITS_UNCONFIRMED": "personal_state",
    "WORKER_LOAD_EXCEEDED": "personal_load",
    "WORKER_LOAD_LIMIT_UNKNOWN": "personal_load",
    "WORKER_TAG_FORBIDDEN": "personal_tag",
    "WORKER_UNAVAILABLE": "personal_availability",
    "WORKER_PROFILE_INVALID": "personal_state",
    "MISSING_WORKER_RATE": "rate",
    "RATE_UNUSABLE": "rate",
    "TASK_WITHDRAWN": "task",
    "MISSING_RESOURCE_KIND": "resource",
    "RESOURCE_UNAVAILABLE": "resource",
    "OUTSIDE_PLANNING_HORIZON": "horizon",
    "SESSION_STARTED_OR_PAST": "temporal_stale",
}

# 本模块特有的越权写法（在 work_gaps / substitution 既有模式之外补充）。
EXTRA_FORBIDDEN_CLAIM_PATTERNS = (
    "旧计划仍然安全",
    "可直接沿用旧计划",
    "重排后已证明可行",
    "已保证本人可完成",
    "失效检查等于健康评估",
)


def find_forbidden_claims(text) -> list:
    """返回命中的越权结论片段；空列表表示通过。复用既有模式并补充重排语境。"""
    from .work_gaps import find_forbidden_claims as _base

    hits = list(_base(text))
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    hits.extend(pattern for pattern in EXTRA_FORBIDDEN_CLAIM_PATTERNS
                if pattern in text and pattern not in hits)
    return hits


def _accepted_review_states(mode) -> set:
    """与 engine / substitution 对限制来源的取舍保持一致。"""
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


def _covers(intervals, start, end) -> bool:
    """存在单个区间完整覆盖 [start, end)。"""
    for item in intervals or []:
        try:
            lo, hi = parse_time(item["start"]), parse_time(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= start and hi >= end:
            return True
    return False


def _rate_reason(task, worker_id):
    """核实该人对该任务是否仍有可采用的速率依据；返回 (ok, code, message)。"""
    if worker_id not in (task.get("rates") or {}):
        return False, "MISSING_WORKER_RATE", "新输入中缺少该人该方法的时间依据。"
    try:
        estimate = estimate_task(task, worker_id)
    except Exception as exc:  # 单位不兼容等 → 视为该人速率不可用
        return False, "RATE_UNUSABLE", f"该人对该任务的速率无法采用：{exc}"
    if estimate["schedulable"]:
        return True, None, None
    code = estimate["reasons"][0]["code"] if estimate["reasons"] else "RATE_UNUSABLE"
    return False, "RATE_UNUSABLE", f"该人对该任务的速率不可采用（{code}）。"


def _session_reasons(session, request, now, mode, tasks, workers, resources) -> list:
    """逐段对照新输入硬约束；返回原因列表（空表示该段仍满足全部可核实硬约束）。"""
    reasons = []
    worker_id = session.get("worker_id")
    task_id = session.get("task_id")

    try:
        start, end = parse_time(session["start"]), parse_time(session["end"])
    except (KeyError, TypeError, ValueError):
        return [{"code": "SESSION_TIME_INVALID", "axis": "temporal_stale",
                 "message": "旧计划该段缺少可解析的开始/结束时间。"}]

    if now is not None and start < now:
        reasons.append({"code": "SESSION_STARTED_OR_PAST", "axis": "temporal_stale",
                        "message": "该段在本次检查时刻之前已经开始，旧计划不再可原样执行。"})

    try:
        horizon_start, horizon_end = parse_time(request["horizon_start"]), parse_time(request["horizon_end"])
        if start < horizon_start or end > horizon_end:
            reasons.append({"code": "OUTSIDE_PLANNING_HORIZON", "axis": "horizon",
                            "message": "该段落在新输入的规划范围之外。"})
    except (KeyError, TypeError, ValueError):
        reasons.append({"code": "OUTSIDE_PLANNING_HORIZON", "axis": "horizon",
                        "message": "新输入缺少有效的规划范围，无法确认该段仍在范围内。"})

    worker = workers.get(worker_id)
    if worker is None:
        reasons.append({"code": "WORKER_WITHDRAWN", "axis": "manpower_withdrawal",
                        "message": f"该人 {worker_id!r} 已不在新输入中（人手撤回）。"})
    else:
        if worker_profile_status(worker, start, end) not in {"valid", "legacy_unspecified"}:
            reasons.append({"code": "WORKER_PROFILE_INVALID", "axis": "personal_state",
                            "message": "人员资料或当前状态有效期未覆盖旧出工段。"})
        if worker.get("state") != "clear":
            reasons.append({"code": "WORKER_STATE_NOT_CLEAR", "axis": "personal_state",
                            "message": f"该人状态为 {worker.get('state')!r}，不能再据此保留出工段。"})
        limits = worker.get("limits") or {}
        if limits.get("review_status") not in _accepted_review_states(mode):
            reasons.append({"code": "WORKER_LIMITS_UNCONFIRMED", "axis": "personal_state",
                            "message": "该人缺本模式可采用的活动/休息限制。"})
        if not _covers(worker.get("availability"), start, end):
            reasons.append({"code": "WORKER_UNAVAILABLE", "axis": "personal_availability",
                            "message": "该人新的可用时段未完整覆盖该出工段。"})
        task = tasks.get(task_id)
        if task is not None:
            forbidden = set(limits.get("forbidden_tags") or [])
            hit = sorted(set(task.get("tags") or []) & forbidden)
            if hit:
                reasons.append({"code": "WORKER_TAG_FORBIDDEN", "axis": "personal_tag",
                                "message": "该任务标签被更新的个人禁止标签命中：" + ", ".join(hit) + "。"})
            load = task.get("load_per_trip_kg")
            if load:
                if "max_load_kg" not in limits:
                    reasons.append({"code": "WORKER_LOAD_LIMIT_UNKNOWN", "axis": "personal_load",
                                    "message": "任务有实际负重，但该人新的负重上限缺失。"})
                elif float(limits.get("max_load_kg") or 0) < float(load):
                    reasons.append({"code": "WORKER_LOAD_EXCEEDED", "axis": "personal_load",
                                    "message": "任务单趟负重超过该人新的负重上限。"})

    task = tasks.get(task_id)
    if task is None:
        reasons.append({"code": "TASK_WITHDRAWN", "axis": "task",
                        "message": f"该任务 {task_id!r} 已不在新输入中。"})
    else:
        if worker is not None:
            ok, code, message = _rate_reason(task, worker_id)
            if not ok:
                reasons.append({"code": code, "axis": "rate", "message": message})
        kinds = {r["id"]: (r.get("kind") or "") for r in request["resources"]}
        required = sorted(set(task.get("required_resources") or []) | set(session.get("resources") or []))
        required_kinds = [kinds.get(rid) for rid in (session.get("resources") or []) if kinds.get(rid)]
        policy_kinds = set((request.get("policy") or {}).get("required_resource_kinds") or [])
        for kind in sorted(policy_kinds - set(required_kinds)):
            reasons.append({"code": "MISSING_RESOURCE_KIND", "axis": "resource",
                            "message": f"策略要求的资源类别 {kind!r} 在该任务中缺失。"})
        for rid in required:
            resource = resources.get(rid)
            if resource is None:
                reasons.append({"code": "RESOURCE_UNAVAILABLE", "axis": "resource",
                                "message": f"所需资源 {rid!r} 已不在新输入中。"})
                continue
            if not _covers(resource.get("availability"), start, end):
                reasons.append({"code": "RESOURCE_UNAVAILABLE", "axis": "resource",
                                "message": f"资源 {rid!r} 的可用时段未完整覆盖该出工段。"})
    return reasons


def _prior_sessions(prior_plan) -> list:
    if not isinstance(prior_plan, dict):
        raise TypeError("prior_plan 必须是计划对象")
    if not prior_plan.get("plan_id") or not prior_plan.get("request_sha256"):
        raise ValueError("prior_plan 缺少 plan_id 或 request_sha256，无法绑定到输入")
    return list(prior_plan.get("sessions") or [])


def _validate_with_withdrawal_repair(new_request, prior_sessions) -> tuple:
    """校验新输入；若仅因**人手撤回**留下悬空速率而失败（旧计划用过该人、新输入已移除该人），
    则在副本上移除这些悬空速率后重试，并如实报告清理了什么。

    - 只清理“旧计划确实用过、新输入确实已移除”的劳动者速率（人手撤回）。
    - 资源引用不做自动清理：工具需求不是可从输入删掉的表面引用，悬空资源引用按原错误抛出。
    - 指向从未在旧计划出现过的劳动者的悬空速率仍按原错误抛出（不掩盖真实输入错误）。
    - 返回 ``(normalized, reference_repair)``；未做清理时 reference_repair 为 None。
    """
    try:
        return validate_request(deepcopy(new_request)), None
    except InputError as original:
        prior_worker_ids = {s.get("worker_id") for s in prior_sessions}
        new_worker_ids = {w.get("id") for w in new_request.get("workers") or []}
        withdrawn_workers = prior_worker_ids - new_worker_ids
        if not withdrawn_workers:
            raise
        patched = deepcopy(new_request)
        dropped_rates = []
        for task in patched.get("tasks") or []:
            for wid in sorted(set((task.get("rates") or {}).keys()) & withdrawn_workers):
                del task["rates"][wid]
                dropped_rates.append({"task_id": task.get("id"), "worker_id": wid})
        try:
            normalized = validate_request(deepcopy(patched))
        except InputError:
            raise original  # 清理后仍不一致 → 按原输入错误处理，不掩盖
        return normalized, {
            "repair": "WITHDRAWN_WORKER_RATE_CLEANUP",
            "withdrawn_worker_ids": sorted(withdrawn_workers),
            "dropped_task_rates": dropped_rates,
            "note": "只在副本上移除指向已撤回劳动者的速率；不新增任何 worker/resource，也不改资源引用。",
        }


def _change_summary(prior_request, new_request, prior_digest, mode) -> dict:
    """字段级变化摘要（可选）：仅在给出旧请求时提供，绝不据此生成新的 worker/resource。"""
    try:
        prior_norm = validate_request(deepcopy(prior_request))
    except Exception as exc:
        return {"available": False, "reason": "PRIOR_REQUEST_INVALID", "detail": str(exc)}
    mismatch = request_digest(validated=prior_norm) != prior_digest

    def by_id(items):
        return {item["id"]: item for item in items}

    pw, nw = by_id(prior_norm["workers"]), by_id(new_request["workers"])
    pr, nr = by_id(prior_norm["resources"]), by_id(new_request["resources"])
    worker_changes = []
    for wid in sorted(set(pw) | set(nw)):
        if wid not in nw:
            worker_changes.append({"worker_id": wid, "kind": "removed"})
            continue
        if wid not in pw:
            worker_changes.append({"worker_id": wid, "kind": "added"})
            continue
        before, after = pw[wid], nw[wid]
        fields = {}
        if before.get("state") != after.get("state"):
            fields["state"] = {"from": before.get("state"), "to": after.get("state")}
        bl, al = before.get("limits") or {}, after.get("limits") or {}
        for key in ("max_active_minutes_per_day", "max_continuous_active_minutes",
                    "min_rest_minutes", "max_load_kg", "forbidden_tags", "review_status"):
            if bl.get(key) != al.get(key):
                fields.setdefault("limits", {})[key] = {"from": bl.get(key), "to": al.get(key)}
        if (before.get("availability") or []) != (after.get("availability") or []):
            fields["availability"] = {"changed": True}
        for key in ("role", "worker_profile"):
            if before.get(key) != after.get(key):
                fields[key] = {"from": before.get(key), "to": after.get(key)}
        if fields:
            worker_changes.append({"worker_id": wid, "kind": "changed", "fields": fields})
    resource_changes = []
    for rid in sorted(set(pr) | set(nr)):
        if rid not in nr:
            resource_changes.append({"resource_id": rid, "kind": "removed"})
        elif rid not in pr:
            resource_changes.append({"resource_id": rid, "kind": "added"})
        elif (pr[rid].get("availability") or []) != (nr[rid].get("availability") or []):
            resource_changes.append({"resource_id": rid, "kind": "availability_changed"})
    return {
        "available": True,
        "prior_request_matches_plan_binding": not mismatch,
        "worker_changes": worker_changes,
        "resource_changes": resource_changes,
        "worker_roles": {wid: w.get("role", "unknown") for wid, w in nw.items()},
        "note": "角色只读取显式声明，缺省 unknown；不按年龄、亲属或病名猜测，不据差异生成新记录。",
    }


def _rescreen_delta(prior_plan, new_plan) -> dict:
    """旧计划与新排程的逐任务差异；只报告本次有界搜索与所输入约束下的结果。"""
    before = {tr["task_id"]: float(tr.get("completion_ratio") or 0.0)
              for tr in (prior_plan.get("task_results") or [])}
    after = {tr["task_id"]: float(tr.get("completion_ratio") or 0.0)
             for tr in (new_plan.get("task_results") or [])}
    eps = 1e-9
    total_before, total_after = sum(before.values()), sum(after.values())
    if total_after < total_before - eps:
        direction = "degraded"
    elif total_after > total_before + eps:
        direction = "improved"
    else:
        direction = "unchanged"
    per_task = []
    for tid in sorted(set(before) | set(after)):
        b, a = before.get(tid, 0.0), after.get(tid, 0.0)
        per_task.append({
            "task_id": tid, "completion_ratio_before": b, "completion_ratio_after": a,
            "change": "degraded" if a < b - eps else ("improved" if a > b + eps else "unchanged"),
        })
    new_summary = new_plan.get("summary") or {}
    return {
        "direction": direction,
        "total_completion_ratio_before": total_before,
        "total_completion_ratio_after": total_after,
        "newly_incomplete_tasks": [t["task_id"] for t in per_task
                                   if t["change"] == "degraded" and t["completion_ratio_after"] < 1.0],
        "recovered_tasks": [t["task_id"] for t in per_task if t["change"] == "improved"],
        "active_person_minutes_before": (prior_plan.get("summary") or {}).get("active_person_minutes"),
        "active_person_minutes_after": new_summary.get("active_person_minutes"),
        "session_count_before": len(prior_plan.get("sessions") or []),
        "session_count_after": len(new_plan.get("sessions") or []),
        "per_task": per_task,
        "rescreen_proves_infeasible": False,
        "note": "差异只是本次新输入与有界搜索的结果；不等于任何安排在任何情况下必然如此。",
    }


def _next_step_directions(new_plan) -> list:
    """从新排程结果汇总待核实方向（复用 work_gaps 的方向目录，全部 PROPOSED_NOT_DONE）。"""
    from .work_gaps import build_gap_report

    report = build_gap_report(new_plan)
    grouped: dict = {}
    for task in report["tasks"]:
        for alt in task["alternatives"]:
            entry = grouped.setdefault(alt["code"], {
                "code": alt["code"], "text": alt["text"],
                "requires_evidence": list(alt["requires_evidence"]),
                "evidence_label": alt["evidence_label"], "status": alt["status"], "task_ids": [],
            })
            if task["task_id"] not in entry["task_ids"]:
                entry["task_ids"].append(task["task_id"])
    return [grouped[code] for code in sorted(grouped)]


def build_replan_check(prior_plan, new_request, *, prior_request=None) -> dict:
    """旧计划失效检查 + 按新输入重排。只读；不修改 prior_plan、new_request 或 prior_request。"""
    sessions = _prior_sessions(prior_plan)
    normalized, reference_repair = _validate_with_withdrawal_repair(new_request, sessions)
    mode = normalized["mode"]
    new_digest = request_digest(validated=normalized)
    prior_digest = prior_plan["request_sha256"]
    input_changed = new_digest != prior_digest

    try:
        now = parse_time(normalized["now"]) if normalized.get("now") else None
    except (KeyError, TypeError, ValueError):
        now = None

    tasks = {t["id"]: t for t in normalized["tasks"]}
    workers = {w["id"]: w for w in normalized["workers"]}
    resources = {r["id"]: r for r in normalized["resources"]}

    checked = []
    for index, session in enumerate(sessions):
        reasons = _session_reasons(session, normalized, now, mode, tasks, workers, resources)
        checked.append({
            "index": index,
            "task_id": session.get("task_id"),
            "worker_id": session.get("worker_id"),
            "worker_role": workers.get(session.get("worker_id"), {}).get("role", "unknown"),
            "start": session.get("start"),
            "end": session.get("end"),
            "status": INVALIDATED if reasons else STILL_VALID,
            "reasons": reasons,
            "axes": sorted({r["axis"] for r in reasons}),
        })

    invalidated = [c for c in checked if c["status"] == INVALIDATED]
    carried_forward = [c for c in checked if c["status"] == STILL_VALID]
    missing_prior = not sessions

    old_plan_status = PRIOR_SUPERSEDED if input_changed else PRIOR_CURRENT
    # 旧计划可复用 = 输入未变 且 没有任何一段被新输入硬约束否掉。
    old_plan_reusable = (not input_changed) and not invalidated

    from .cli import run_checked

    new_plan = run_checked(deepcopy(normalized))

    original_workers = set(workers)
    original_resources = set(resources)
    introduced_workers = sorted({w["id"] for w in normalized["workers"]} - original_workers)
    introduced_resources = sorted({r["id"] for r in normalized["resources"]} - original_resources)
    fabrication_check = {
        "no_phantom_helper": not introduced_workers and not introduced_resources,
        "introduced_worker_ids": introduced_workers,
        "introduced_resource_ids": introduced_resources,
        "note": "重排只在新输入已存在的 worker/resource 上进行，不新增记录。",
    }
    if not fabrication_check["no_phantom_helper"]:
        raise AssertionError("重排检查不得向输入副本新增 worker/resource 记录")

    change = _change_summary(prior_request, normalized, prior_digest, mode) if prior_request else {
        "available": False, "reason": "PRIOR_REQUEST_NOT_PROVIDED",
        "note": "未提供旧输入，无法列举字段级变化；仅按新输入逐段重筛旧计划。",
    }

    review = {
        "schema_version": SCHEMA_VERSION,
        "review_kind": REVIEW_KIND,
        "mode": mode,
        "system_status": SYSTEM_STATUS,
        "system_status_note": "本检查正常完成；这不是系统故障报告，也不是个人健康结论。",
        "old_plan_id": prior_plan.get("plan_id"),
        "old_plan_digest": prior_digest,
        "new_input_digest": new_digest,
        "input_changed": input_changed,
        "old_plan_status": old_plan_status,
        "old_plan_reusable": old_plan_reusable,
        "input_change_summary": change,
        "reference_repair": reference_repair,
        "prior_session_count": len(sessions),
        "session_count_missing_prior": missing_prior,
        "invalidated_session_count": len(invalidated),
        "carried_forward_session_count": len(carried_forward),
        "invalidated_axes": sorted({axis for c in invalidated for axis in c["axes"]}),
        "sessions": checked,
        "rescreened_plan_id": new_plan.get("plan_id"),
        "rescreened_plan_status": new_plan.get("status"),
        "rescreen_delta": _rescreen_delta(prior_plan, new_plan),
        "fabrication_check": fabrication_check,
        "next_step_directions": _next_step_directions(new_plan),
        "rescreen_proves_infeasible": False,
        "field_verified": False,
        "health_or_safety_verified": False,
        "evidence_labels": ["SYNTHETIC", "IMPLEMENTATION_TEST"],
        "notes": [
            "本检查只判定旧计划是否仍满足新输入的硬约束，并给出本次重排差异；不认证任何安排的现场可行性。",
            "身体状态变化与帮手撤回落在人员字段上；只展示声明的 role，缺省 unknown，不猜测角色。",
            "旧计划一旦被否（人员撤回/状态异常/限制变化/时段或资源不再覆盖），不得原样沿用，必须按新输入重排。",
            "重排方向只是本次有界搜索的结果；未安排的余量不是“必然无法完成”的证明。",
            "所有待核实方向均为 PROPOSED_NOT_DONE，取得相应证据前不得当作已完成。",
        ],
    }
    violations = assert_replan_invariants(review)
    if violations:
        raise AssertionError("重排检查违反硬约束: " + "; ".join(violations))
    return review


def assert_replan_invariants(review) -> list:
    """返回违反项列表；空列表表示满足全部硬约束。"""
    violations = []
    if review.get("system_status") != SYSTEM_STATUS:
        violations.append(f"system_status 只能是 {SYSTEM_STATUS}")
    for key in ("rescreen_proves_infeasible", "field_verified", "health_or_safety_verified"):
        if review.get(key) is not False:
            violations.append(f"{key} 必须为 False")
    sessions = review.get("sessions") or []
    invalidated = [c for c in sessions if c.get("status") == INVALIDATED]
    if invalidated and review.get("old_plan_reusable") is not False:
        violations.append("存在被否出工段时 old_plan_reusable 必须为 False")
    if review.get("input_changed") and review.get("old_plan_reusable") is not False:
        violations.append("输入已变化时 old_plan_reusable 必须为 False")
    if review.get("input_changed") and review.get("old_plan_status") != PRIOR_SUPERSEDED:
        violations.append(f"输入已变化时 old_plan_status 必须是 {PRIOR_SUPERSEDED}")
    if not review.get("input_changed") and review.get("old_plan_status") != PRIOR_CURRENT:
        violations.append(f"输入未变化时 old_plan_status 必须是 {PRIOR_CURRENT}")
    for session in sessions:
        if session.get("status") not in (STILL_VALID, INVALIDATED):
            violations.append(f"出工段状态非法: {session.get('status')}")
        if session.get("status") == STILL_VALID and session.get("reasons"):
            violations.append("仍有效的出工段不应带失效原因")
        for reason in session.get("reasons", []):
            if reason.get("code") not in AXIS_BY_CODE:
                violations.append(f"未知失效原因码: {reason.get('code')}")
            elif reason.get("axis") != AXIS_BY_CODE[reason["code"]]:
                violations.append(f"原因码 {reason['code']} 的变化轴不一致")
    check = review.get("fabrication_check") or {}
    if check.get("no_phantom_helper") is not True:
        violations.append("no_phantom_helper 必须为 True")
    if check.get("introduced_worker_ids") or check.get("introduced_resource_ids"):
        violations.append("不得向输入引入新的 worker/resource id")
    for direction in review.get("next_step_directions", []):
        if direction.get("status") != PROPOSED_NOT_DONE:
            violations.append(f"{direction.get('code')}: 待核实方向必须是 {PROPOSED_NOT_DONE}")
    claims = find_forbidden_claims(json.dumps(review, ensure_ascii=False))
    if claims:
        violations.append("越权结论片段: " + ", ".join(claims))
    return violations


def summarize_replan_check(review) -> str:
    """一行人类可读摘要；不含健康、可行性或最优性结论。"""
    delta = review["rescreen_delta"]
    if review["old_plan_reusable"]:
        verdict = "旧计划仍与新输入一致"
    else:
        verdict = "旧计划须失效并重排"
    return ("重排检查：输入{changed}；旧计划 {sessions} 段中 {bad} 段被否；{verdict}。"
            "重排方向 {direction}（完成比例 {before:.3f}→{after:.3f}）；"
            "待核实方向 {paths} 条。").format(
        changed="已变化" if review["input_changed"] else "未变化",
        sessions=review["prior_session_count"], bad=review["invalidated_session_count"],
        verdict=verdict, direction=delta["direction"],
        before=delta["total_completion_ratio_before"],
        after=delta["total_completion_ratio_after"],
        paths=len(review["next_step_directions"]))
