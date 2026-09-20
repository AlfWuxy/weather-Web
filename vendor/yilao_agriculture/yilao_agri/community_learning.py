"""本人估时的描述性回顾：保存历史情境，不自动采用，不认证现场或健康。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math

from .learning_data import record_sha256, LearningDataError
from .models import InputError, is_placeholder_source, parse_time
from .workload import canonical_quantity

CONTEXT_SCHEMA = "community-rate-context-1"
REVIEW_SCHEMA = "community-rate-review-1"
UNITS = {"sqm", "mu", "kg", "plant", "trip", "m", "m3"}
EPS = 1e-7


def _hash(value):
    try:
        return record_sha256(value)
    except LearningDataError as exc:
        raise InputError("个人估时回顾只接受有限、可保存为 JSON 的资料") from exc


def _source(value):
    return (isinstance(value, str) and not is_placeholder_source(value)
            and value.strip().casefold() not in {"copied_from_plan", "copied_from_schedule"})


def _finite(value, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def _instant(value):
    return parse_time(value).astimezone(timezone.utc)


def _find(state, task_id, worker_id):
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
        raise InputError("state.tasks: 缺少可读取的农活资料")
    tasks = [t for t in state["tasks"] if isinstance(t, dict) and t.get("id") == task_id]
    people = [state.get("profile"), *(state.get("helpers") or [])]
    workers = [w for w in people if isinstance(w, dict) and w.get("id") == worker_id]
    if len(tasks) != 1 or len(workers) != 1:
        raise InputError("task_id/worker_id: 需要唯一且仍保留的农活与人员")
    return tasks[0]


def _context(state, task, worker_id):
    if not isinstance(state.get("plots", []), list) or not isinstance(state.get("resources", []), list):
        raise InputError("state.plots/resources: 需要数组")
    for key in ("remaining_quantity", "agronomy"):
        if task.get(key) is not None and not isinstance(task[key], dict):
            raise InputError("task." + key + ": 需要对象或未知值")
    plot = next((p for p in state.get("plots", []) if isinstance(p, dict) and p.get("id") == task.get("plot_id")), {})
    resources = {r.get("id"): r for r in state.get("resources", []) if isinstance(r, dict)}
    required = task.get("required_resources")
    resource_rows = []
    if isinstance(required, list) and all(isinstance(rid, str) for rid in required):
        for rid in sorted(set(required)):
            if rid not in resources:
                resource_rows.append({"id": rid, "missing": True})
            else:
                # 时间可用性是排程条件；不因新的一天就否定同一工具的历史净劳动记录。
                resource_rows.append({k: deepcopy(v) for k, v in resources[rid].items()
                                      if k not in {"availability", "confirmed"}})
    else:
        resource_rows = None
    return {
        "worker_id": worker_id, "task_id": task.get("id"),
        **{k: deepcopy(task.get(k)) for k in ("task_code", "operation", "method", "crop_id", "stage")},
        "unit": (task.get("remaining_quantity") or {}).get("unit"), "scope": "net_work",
        "plot": {k: deepcopy(plot.get(k)) for k in ("id", "region_id", "environment", "latitude", "longitude", "conditions")},
        "work": {**{k: deepcopy(task.get(k)) for k in ("tags", "load_per_trip_kg", "distance_m", "session", "once", "wait_duty", "operation_scope")},
                 "agronomy_conditions": deepcopy((task.get("agronomy") or {}).get("conditions"))},
        "resources": resource_rows,
    }


def capture_rate_context(state, task_id, worker_id, *, captured_at=None):
    """仅由反馈保存服务调用；不替使用者确认过去的实际做法与当前任务相同。

    未知字段也保留为未知，允许继续保存作业记录；review 会明确拒绝其估时候选。
    不可在回顾时为旧记录补造快照；更正默认保留旧快照。
    """
    task = _find(state, task_id, worker_id)
    at = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    _instant(at)
    snapshot = {"schema_version": CONTEXT_SCHEMA, "captured_by": "server", "captured_at": at,
                "state_revision": state.get("revision"), "context": _context(state, task, worker_id)}
    snapshot["sha256"] = _hash(snapshot)
    return snapshot


def _known_values(value):
    if value is None:
        return False
    if isinstance(value, str):
        return _source(value)
    if isinstance(value, dict):
        return bool(value) and all(_known_values(v) for v in value.values())
    if isinstance(value, list):
        return all(_known_values(v) for v in value)
    return isinstance(value, bool) or _finite(value, -math.inf)


def _context_reasons(context):
    if not isinstance(context, dict):
        return ["CONTEXT_INVALID"]
    reasons = []
    for key in ("worker_id", "task_id", "task_code", "operation", "method", "crop_id", "stage"):
        if not _source(context.get(key)):
            reasons.append("CONTEXT_UNKNOWN_" + key.upper())
    if not isinstance(context.get("unit"), str) or context["unit"] not in UNITS:
        reasons.append("CONTEXT_UNKNOWN_UNIT")
    if context.get("scope") != "net_work":
        reasons.append("NOT_NET_WORK")
    plot = context.get("plot")
    if not isinstance(plot, dict) or not all(_source(plot.get(k)) for k in ("id", "region_id", "environment")):
        reasons.append("PLOT_CONTEXT_UNKNOWN")
    elif (not _known_values(plot.get("conditions"))
          or not _finite(plot.get("latitude"), -90) or plot["latitude"] > 90
          or not _finite(plot.get("longitude"), -180) or plot["longitude"] > 180):
        reasons.append("PLOT_CONDITIONS_UNKNOWN")
    work = context.get("work")
    if not isinstance(work, dict):
        reasons.append("WORK_CONTEXT_UNKNOWN")
    else:
        if not isinstance(work.get("tags"), list) or any(not _source(t) for t in work["tags"]):
            reasons.append("ACTIVITY_CONTEXT_UNKNOWN")
        if not _finite(work.get("load_per_trip_kg")):
            reasons.append("LOAD_CONTEXT_UNKNOWN")
        if not _known_values(work.get("agronomy_conditions")):
            reasons.append("AGRONOMY_CONTEXT_UNKNOWN")
        if str(context.get("operation", "")).startswith("haul_"):
            if not _finite(work.get("distance_m")) or not _finite(work.get("load_per_trip_kg"), EPS):
                reasons.append("TRANSPORT_CONTEXT_UNKNOWN")
        if work.get("wait_duty") is not None:
            duty = work["wait_duty"]
            if not isinstance(duty, dict) or duty.get("status") != "known":
                reasons.append("WAIT_CONTEXT_UNKNOWN")
    resources = context.get("resources")
    if not isinstance(resources, list) or any(not isinstance(r, dict) or r.get("missing") or not _known_values(r)
                                              or not _source(r.get("id")) or not _source(r.get("kind")) for r in resources):
        reasons.append("RESOURCE_CONTEXT_UNKNOWN")
    return reasons


def _semantic(context):
    """只比较实际语义；task_id、记录时刻和修订号仅用于追溯。"""
    out = deepcopy(context)
    out.pop("task_id", None)
    if out.get("unit") in {"mu", "sqm"}:
        out["unit"] = "sqm"
    if isinstance(out.get("work"), dict) and isinstance(out["work"].get("tags"), list):
        out["work"]["tags"] = sorted(set(out["work"]["tags"]))
    if isinstance(out.get("resources"), list):
        out["resources"] = sorted(out["resources"], key=lambda r: str(r.get("id")))
    return out


def _quantity_in_unit(quantity, unit):
    if not isinstance(quantity, dict) or not _finite(quantity.get("value"), EPS):
        raise InputError("完成量必须为有限正数")
    if quantity.get("unit") in {"plant", "trip"} and quantity["value"] != int(quantity["value"]):
        raise InputError("株和趟必须为整数")
    value, actual_unit = canonical_quantity(quantity)
    factor, target_unit = canonical_quantity({"value": 1, "unit": unit})
    if actual_unit != target_unit:
        raise InputError("不兼容的单位不能换算")
    amount = value / factor
    if not _finite(amount, EPS):
        raise InputError("完成量换算溢出或不是正数")
    conversion = None
    if quantity["unit"] != unit:
        conversion = {"from_unit": quantity["unit"], "to_unit": unit,
                      "factor": amount / quantity["value"], "basis": "1 mu = 2000/3 sqm"}
    return amount, conversion


def review_personal_rates(state, task_id, worker_id, *, now=None):
    """只读地产生描述性候选。完整反馈档案必须保留更正链，不能只传当前事件。"""
    task = _find(state, task_id, worker_id)
    events = state.get("feedback")
    if not isinstance(events, list):
        raise InputError("state.feedback: 需要完整反馈档案")
    _hash(events)
    moment = _instant(now) if now is not None else datetime.now(timezone.utc)
    target = _context(state, task, worker_id)
    target_reasons = _context_reasons(target)
    target_semantic = _semantic(target) if not target_reasons else None
    current_rate = deepcopy((task.get("rates") or {}).get(worker_id))
    rows, by_id, conflicts = [], {}, set()

    # 同 ID 冲突不取“最后一条”；每条原记录都有稳定哈希及排除原因。
    for index, event in enumerate(events):
        event = event if isinstance(event, dict) else {"invalid_record": event}
        ident = event.get("event_id")
        row = {"event_id": ident, "task_id": event.get("task_id"), "status": event.get("status"),
               "included": False, "reason_codes": [], "observed_rate_minutes_per_unit": None,
               "quantity_in_target_unit": None, "unit_conversion": None,
               "record_sha256": _hash(event), "context_sha256": None, "index": index}
        rows.append(row)
        if not _source(ident):
            row["reason_codes"].append("EVENT_ID_UNKNOWN")
        elif ident in by_id:
            previous = rows[by_id[ident]]
            if previous["record_sha256"] == row["record_sha256"]:
                row["reason_codes"].append("DUPLICATE_EVENT_ID")
            else:
                conflicts.add(ident)
        else:
            by_id[ident] = index
        if event.get("worker_id") != worker_id:
            row["reason_codes"].append("DIFFERENT_WORKER")
    for row in rows:
        if isinstance(row["event_id"], str) and row["event_id"] in conflicts:
            row["reason_codes"].append("EVENT_ID_CONFLICT")

    superseded, successors, invalid_chain = set(), {}, False
    for ident, index in by_id.items():
        event = events[index]
        prior = event.get("supersedes_event_id")
        if prior is None:
            continue
        if not isinstance(prior, str) or prior not in by_id or prior == ident:
            rows[index]["reason_codes"].append("CORRECTION_CHAIN_INVALID")
            invalid_chain = True
            continue
        original = events[by_id[prior]]
        if (event.get("worker_id"), event.get("task_id")) != (original.get("worker_id"), original.get("task_id")) or prior in successors:
            rows[index]["reason_codes"].append("CORRECTION_CHAIN_INVALID")
            invalid_chain = True
        successors[prior] = ident
        superseded.add(prior)
    for ident in by_id:
        seen, cursor = set(), ident
        while cursor in successors:
            if cursor in seen:
                invalid_chain = True
                rows[by_id[ident]]["reason_codes"].append("CORRECTION_CHAIN_INVALID")
                break
            seen.add(cursor)
            cursor = successors[cursor]
    for row in rows:
        if isinstance(row["event_id"], str) and row["event_id"] in superseded:
            row["reason_codes"].append("SUPERSEDED")

    intervals, comparable_interrupted = [], []
    for index, event in enumerate(events):
        row = rows[index]
        reasons = row["reason_codes"]
        if not isinstance(event, dict) or reasons:
            continue
        if event.get("consent") is not True:
            reasons.append("CONSENT_WITHDRAWN_OR_MISSING")
        if event.get("withdrawn") is True or event.get("withdrawn_at") or event.get("verification_status") == "withdrawn":
            reasons.append("WITHDRAWN")
        if event.get("status") not in {"completed", "partial", "interrupted"}:
            reasons.append("STATUS_NOT_RATE_ELIGIBLE")
        if event.get("context_matches_task") is not True:
            reasons.append("ACTUAL_CONTEXT_NOT_CONFIRMED")
        snapshot = event.get("context_snapshot")
        historic = None
        if not isinstance(snapshot, dict):
            reasons.append("HISTORICAL_CONTEXT_MISSING")
        elif (snapshot.get("schema_version") != CONTEXT_SCHEMA or snapshot.get("captured_by") != "server"
              or type(snapshot.get("state_revision")) is not int or snapshot["state_revision"] < 0
              or snapshot.get("sha256") != _hash({k: v for k, v in snapshot.items() if k != "sha256"})):
            reasons.append("HISTORICAL_CONTEXT_INVALID")
        else:
            row["context_sha256"] = snapshot["sha256"]
            historic = snapshot.get("context")
            reasons.extend(_context_reasons(historic))
            if isinstance(historic, dict):
                if historic.get("task_id") != event.get("task_id") or historic.get("worker_id") != event.get("worker_id"):
                    reasons.append("HISTORICAL_CONTEXT_REFERENCE_MISMATCH")
                if target_reasons:
                    reasons.append("TARGET_CONTEXT_UNKNOWN")
                elif not reasons and _semantic(historic) != target_semantic:
                    reasons.append("CONTEXT_MISMATCH")
        for key in ("quantity_source", "clock_source"):
            if not _source(event.get(key)):
                reasons.append("SOURCE_UNKNOWN_OR_COPIED")
        if event.get("scope", "net_work") != "net_work" or event.get("time_scope", "net_work") != "net_work":
            reasons.append("NOT_NET_WORK")
        if state.get("mode") == "real" and event.get("evidence_type") == "SYNTHETIC":
            reasons.append("SYNTHETIC_REAL_MODE_MISMATCH")
        try:
            start, end, recorded = (_instant(event.get(k)) for k in ("started_at", "ended_at", "recorded_at"))
            if not start < end <= recorded <= moment:
                raise ValueError("先有作业，后有记录；不得记录未来")
            if isinstance(snapshot, dict) and not _instant(snapshot.get("captured_at")) <= recorded:
                raise ValueError("快照不能晚于记录保存")
            net, rest = event.get("net_minutes"), event.get("rest_minutes")
            if not _finite(net, EPS) or not _finite(rest) or net + rest > (end - start).total_seconds() / 60 + EPS:
                raise ValueError("净劳动和休息必须与实际起止一致")
        except (InputError, ValueError, TypeError):
            reasons.append("ACTUAL_TIME_INVALID_OR_UNKNOWN")
            continue
        try:
            amount, conversion = _quantity_in_unit(event.get("completed_quantity"), target.get("unit"))
            if isinstance(historic, dict):
                _quantity_in_unit(event.get("completed_quantity"), historic.get("unit"))
        except (InputError, ValueError, TypeError):
            reasons.append("QUANTITY_OR_UNIT_INCOMPATIBLE")
            continue
        rate = net / amount
        if not _finite(rate, EPS):
            reasons.append("RATE_INVALID")
            continue
        if reasons:
            continue
        # 以绝对时刻识别同人的物理重复，不因 event_id/task_id/时区写法不同重复计数。
        fingerprint = (start, end, amount, net, rest, _hash(_semantic(historic)))
        duplicate = False
        for previous in intervals:
            if fingerprint == previous["fingerprint"]:
                reasons.append("DUPLICATE_PHYSICAL_EVENT")
                duplicate = True
                break
            if max(start, previous["start"]) < min(end, previous["end"]):
                reasons.append("PHYSICAL_EVENT_OVERLAP")
                rows[previous["index"]]["reason_codes"].append("PHYSICAL_EVENT_OVERLAP")
        if duplicate:
            continue
        intervals.append({"start": start, "end": end, "fingerprint": fingerprint, "index": index})
        row.update(observed_rate_minutes_per_unit=rate, quantity_in_target_unit=amount, unit_conversion=conversion)
        if event["status"] == "interrupted":
            reasons.append("INTERRUPTED_SEPARATE_REVIEW")
            comparable_interrupted.append(event["event_id"])

    # 不可比的另一项农活也不能在同一时刻占用本人；不能先按做法筛掉，再漏查物理冲突。
    for current in intervals:
        for index, event in enumerate(events):
            if (not isinstance(event, dict) or event.get("worker_id") != worker_id or index == current["index"]
                    or event.get("status") not in {"completed", "partial", "interrupted"}
                    or not isinstance(event.get("event_id"), str) or event["event_id"] in superseded
                    or "DUPLICATE_EVENT_ID" in rows[index]["reason_codes"]
                    or "DUPLICATE_PHYSICAL_EVENT" in rows[index]["reason_codes"]):
                continue
            try:
                start, end = _instant(event.get("started_at")), _instant(event.get("ended_at"))
            except (InputError, ValueError, TypeError):
                continue
            if max(start, current["start"]) < min(end, current["end"]):
                rows[index]["reason_codes"].append("PHYSICAL_EVENT_OVERLAP")
                rows[current["index"]]["reason_codes"].append("PHYSICAL_EVENT_OVERLAP")

    integrity_blocked = bool(conflicts or invalid_chain or any("PHYSICAL_EVENT_OVERLAP" in r["reason_codes"] for r in rows))
    for row in rows:
        if integrity_blocked and not row["reason_codes"]:
            row["reason_codes"].append("ARCHIVE_INTEGRITY_REVIEW_REQUIRED")
        row["reason_codes"] = sorted(set(row["reason_codes"]))
        row["included"] = not row["reason_codes"] and row["observed_rate_minutes_per_unit"] is not None
        if row["included"]:
            row["reason_codes"] = ["COMPARABLE_SELF_REPORT"]
    included = [r for r in rows if r["included"]]
    excluded = [r for r in rows if not r["included"]]
    basis = {"schema_version": REVIEW_SCHEMA, "target_context": target, "current_rate": current_rate,
             "input_record_refs": [{"index": r["index"], "event_id": r["event_id"], "sha256": r["record_sha256"]} for r in rows],
             "state_revision": state.get("revision"), "mode": state.get("mode")}
    basis_hash = _hash(basis)
    candidate = None
    if included:
        values = [r["observed_rate_minutes_per_unit"] for r in included]
        candidate = {"low": min(values), "high": max(values), "unit": target["unit"], "scope": "net_work",
                     "count": len(included), "record_ids": [r["event_id"] for r in included],
                     "interval_kind": "self_report_observed_range_not_probability_interval"}
    conditions = [
        {"code": "CONFIRM_CONTEXT_AND_RECORDS", "message": "逐条核对本人、作物阶段、地块条件、做法、工具负重、数量及净劳动口径。"},
        {"code": "EXPLICIT_RATE_ADOPTION", "message": "候选不自动采用；明确确认后另存一个速率版本及依据哈希。"},
        {"code": "REFRESH_BASIS_BEFORE_ADOPTION", "message": "采用前重算依据哈希；记录更正、撤回或情境变化后旧候选不可直接采用。"},
        {"code": "NO_AUTOMATIC_TIGHTENING", "message": "少量较快记录不能自动收紧原慢端；个人限制和已用速率保持不变。"},
        {"code": "CURRENT_CONDITIONS_STILL_REQUIRED", "message": "估时回顾不证明当前身体、天气、农艺和工具可用；排程仍需独立核对。"},
    ]
    if excluded:
        conditions.append({"code": "REVIEW_EXCLUSIONS", "message": "查看全部排除记录，不能把未知、中断或缺快照的慢记录当作不存在。"})
    if comparable_interrupted:
        conditions.append({"code": "RESOLVE_INTERRUPTED_RECORDS", "message": "存在中断记录，须先核对中断及未完成部分；常规区间可能遗漏慢端，暂不提供采用建议。"})
    if integrity_blocked:
        conditions.append({"code": "RESOLVE_ARCHIVE_CONFLICT", "message": "更正链、编号或物理时间存在冲突，请先解决记录完整性问题。"})
    if target_reasons:
        conditions.append({"code": "COMPLETE_TARGET_CONTEXT", "message": "当前农活情境仍有未知字段，不能推断历史可比性。"})
    test_data = any(events[r["index"]].get("evidence_type") in {"SYNTHETIC", "IMPLEMENTATION_TEST"} for r in included)
    blocked = bool(not candidate or integrity_blocked or comparable_interrupted or target_reasons or state.get("mode") != "real" or test_data)
    if test_data or state.get("mode") != "real":
        conditions.append({"code": "TEST_DATA_NOT_ADOPTABLE", "message": "演示或实现测试只能检查代码，不能作为本人真实速率采用依据。"})
    proposal = None
    if candidate and not blocked:
        low, high = candidate["low"], candidate["high"]
        # 复用现有保守范围规则：可扩宽描述边界，不能凭少量快记录收紧慢端。
        if isinstance(current_rate, dict):
            try:
                factor, _ = _quantity_in_unit({"value": 1, "unit": current_rate.get("unit")}, target["unit"])
                if (current_rate.get("scope") != "net_work" or not _source(current_rate.get("source"))
                        or not _finite(current_rate.get("low"), EPS) or not _finite(current_rate.get("high"), EPS)
                        or current_rate["high"] < current_rate["low"]):
                    raise ValueError("既有速率口径待核对")
                low = min(low, current_rate["low"] / factor)
                high = max(high, current_rate["high"] / factor)
            except (InputError, ValueError, TypeError):
                blocked = True
                conditions.append({"code": "CURRENT_RATE_REVIEW_REQUIRED", "message": "既有速率的来源、单位或口径尚未明确，不能直接覆盖。"})
        if not blocked:
            proposal = {"low": low, "high": high, "unit": target["unit"], "scope": "net_work",
                        "source": "self_report_review", "basis_sha256": basis_hash}
    return {"schema_version": REVIEW_SCHEMA, "task_id": task_id, "worker_id": worker_id,
            "status": "review_blocked" if integrity_blocked or target_reasons or comparable_interrupted else "descriptive_candidate" if candidate else "no_comparable_records",
            "candidate": candidate, "current_rate": current_rate, "conservative_proposal": proposal,
            "records": rows, "included_record_ids": [r["event_id"] for r in included],
            "excluded_record_ids": [r["event_id"] for r in excluded], "interrupted_record_ids": comparable_interrupted,
            "target_reason_codes": target_reasons, "basis_snapshot": basis, "basis_sha256": basis_hash,
            "adoption": {"automatic": False, "requires_explicit_confirmation": True, "conditions": conditions, "blocked": blocked},
            "claims": {"n_real": 0, "field_verified": False, "calibrated": False, "health_clearance": False},
            "warnings": ["这是输入者观察或回忆记录的描述范围；confirmed、来源文字和快照哈希均不认证真实性。",
                         "无任意样本数门槛：一条也可描述，但最小值等于最大值不代表未来耗时固定。",
                         "不提供校准准确率、置信区间、预测区间或疾病系数；不自动改变个人限额、休息或速率。"]}
