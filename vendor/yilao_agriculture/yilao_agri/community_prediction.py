"""事前冻结本人输入估时；只做单条自述对照，不训练、不改写预测、不认证健康。"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

from . import __version__
from .community_learning import (_context, _context_reasons, _find, _hash, _semantic,
                                 _source, capture_rate_context)
from .models import InputError, parse_time
from .workload import canonical_quantity

PREVIEW_SCHEMA = "community-prediction-preview-1"
PREDICTION_SCHEMA = "community-prediction-1"
REVIEW_SCHEMA = "community-prediction-review-1"
POINT_POLICIES = {"none", "slow_bound", "midpoint"}
CLAIMS = {"n_real": 0, "field_verified": False, "calibrated": False, "health_clearance": False}
EPS = 1e-7


def _instant(value):
    moment = value if isinstance(value, datetime) else parse_time(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InputError("需要含时区的时间")
    return moment.astimezone(timezone.utc)


def _now(value):
    return _instant(value) if value is not None else datetime.now(timezone.utc)


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def prediction_sha256(prediction):
    """传输信任标记不属于原预测内容；导入降级时保留原内容和原哈希。"""
    if not isinstance(prediction, dict):
        raise InputError("prediction: 需要对象")
    return _hash({k: v for k, v in prediction.items() if k not in {"sha256", "trust_status", "imported_at"}})


def _archive(events):
    """保留完整更正历史；冲突不选择一条胜出，也不将断链当作有效历史。"""
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise InputError("feedback: 需要完整记录对象数组")
    _hash(events)
    by_id, superseded, successors, errors = {}, set(), {}, []
    for event in events:
        ident = event.get("event_id")
        if not _source(ident):
            errors.append("FEEDBACK_ID_INVALID")
        elif ident in by_id:
            errors.append("FEEDBACK_ID_CONFLICT")
        else:
            by_id[ident] = event
    for ident, event in by_id.items():
        prior = event.get("supersedes_event_id")
        if prior is None:
            continue
        if not isinstance(prior, str) or prior not in by_id or prior == ident or prior in successors:
            errors.append("FEEDBACK_CORRECTION_INVALID")
            continue
        original = by_id[prior]
        if (original.get("task_id"), original.get("worker_id")) != (event.get("task_id"), event.get("worker_id")):
            errors.append("FEEDBACK_CORRECTION_INVALID")
            continue
        successors[prior] = ident
        superseded.add(prior)
    for ident in by_id:
        seen, cursor = set(), ident
        while cursor in successors:
            if cursor in seen:
                errors.append("FEEDBACK_CORRECTION_INVALID")
                break
            seen.add(cursor)
            cursor = successors[cursor]
    current = [e for e in events if not isinstance(e.get("event_id"), str) or e["event_id"] not in superseded]
    return current, superseded, sorted(set(errors))


def _remaining(task, events):
    total, unit = canonical_quantity(task.get("remaining_quantity"))
    current, _, errors = _archive(events)
    done, physical = 0.0, []
    for event in current:
        if event.get("task_id") != task.get("id"):
            continue
        if event.get("status") not in {"completed", "partial", "interrupted", "not_done"}:
            errors.append("REMAINING_QUANTITY_UNKNOWN")
            continue
        try:
            value, actual_unit = canonical_quantity(event.get("completed_quantity"))
            if actual_unit != unit or (event["status"] == "not_done" and value != 0):
                raise InputError("已发生完成量与剩余量口径冲突")
            if event["status"] != "not_done":
                start, end = _instant(event.get("started_at")), _instant(event.get("ended_at"))
                if start >= end:
                    raise InputError("实际起止不正确")
                for wid, lo, hi in physical:
                    if wid == event.get("worker_id") and max(start, lo) < min(end, hi):
                        errors.append("REMAINING_PHYSICAL_EVENT_CONFLICT")
                physical.append((event.get("worker_id"), start, end))
            done += value
        except (InputError, ValueError, TypeError):
            errors.append("REMAINING_QUANTITY_UNKNOWN")
    if done > total + EPS:
        errors.append("COMPLETED_EXCEEDS_REMAINING_BASE")
    return max(0, total - done), unit, sorted(set(errors))


def preview_prediction(state, task_id, worker_id, quantity=None):
    """预览只取已声明的净劳动速率，不要求身体/天气/资源达到排程 ready。"""
    task = _find(state, task_id, worker_id)
    context = _context(state, task, worker_id)
    context_reasons = _context_reasons(context)
    rate = deepcopy((task.get("rates") or {}).get(worker_id))
    reasons = []
    remaining = target = conversion = estimate = None
    try:
        left, canonical_unit, issues = _remaining(task, state.get("feedback", []))
        reasons.extend(issues)
        original_unit = task["remaining_quantity"]["unit"]
        unit_factor, _ = canonical_quantity({"value": 1, "unit": original_unit})
        remaining = {"value": left / unit_factor, "unit": original_unit}
        requested = deepcopy(quantity if quantity is not None else remaining)
        amount, requested_unit = canonical_quantity(requested)
        if requested_unit != canonical_unit or not _positive(amount):
            raise InputError("预测工作量须为同口径正数")
        if amount > left + EPS:
            reasons.append("TARGET_EXCEEDS_REMAINING")
        target = {"value": amount / unit_factor, "unit": original_unit}
        if requested["unit"] != original_unit:
            conversion = {"from_unit": requested["unit"], "to_unit": original_unit,
                          "factor": target["value"] / requested["value"], "basis": "1 mu = 2000/3 sqm"}
    except (InputError, ValueError, TypeError, KeyError):
        reasons.append("TARGET_QUANTITY_INVALID")
    if not isinstance(rate, dict) or not _positive(rate.get("low")) or not _positive(rate.get("high")) or rate["high"] < rate["low"]:
        reasons.append("RATE_RANGE_UNKNOWN")
    elif rate.get("scope") != "net_work":
        reasons.append("RATE_NOT_NET_WORK")
    elif not _source(rate.get("source")):
        reasons.append("RATE_SOURCE_UNKNOWN")
    elif target is not None:
        try:
            rate_factor, rate_unit = canonical_quantity({"value": 1, "unit": rate.get("unit")})
            amount, target_unit = canonical_quantity(target)
            if rate_unit != target_unit:
                raise InputError("速率与工作量单位不兼容")
            low, high = amount / rate_factor * rate["low"], amount / rate_factor * rate["high"]
            if not _positive(low) or not _positive(high):
                raise InputError("估时溢出")
            estimate = {"low_minutes": low, "high_minutes": high, "scope": "net_work"}
        except (InputError, ValueError, TypeError):
            reasons.append("RATE_UNIT_INCOMPATIBLE")
    messages = {
        "TARGET_EXCEEDS_REMAINING": "本次目标不能超过已记录的剩余工作量。",
        "TARGET_QUANTITY_INVALID": "本次目标需要明确正数量和兼容单位。",
        "RATE_RANGE_UNKNOWN": "先填写该人有来源的净劳动估时区间。",
        "RATE_NOT_NET_WORK": "只能冻结净劳动估时；整次出工时间须先拆清。",
        "RATE_SOURCE_UNKNOWN": "速率来源未知或照抄安排，不能冻结为明确估时。",
        "RATE_UNIT_INCOMPATIBLE": "速率与工作量单位不兼容或数值溢出。",
    }
    reasons = sorted(set(reasons))
    result = {"schema_version": PREVIEW_SCHEMA, "ready": not reasons and estimate is not None,
              "task_id": task_id, "worker_id": worker_id, "target_quantity": target, "remaining_quantity": remaining,
              "quantity_conversion": conversion, "rate_snapshot": rate, "estimate": estimate,
              "point_policy_options": ["none", "slow_bound", "midpoint"], "context": context,
              "context_reason_codes": context_reasons,
              "reasons": [{"code": code, "message": messages.get(code, "实际记录或更正链尚有冲突，先核对剩余量。")} for code in reasons],
              "source_revision": state.get("revision"), "mode": state.get("mode"),
              "timezone": (state.get("settings") or {}).get("timezone", "Asia/Shanghai"),
              "algorithm_version": __version__, "claims": deepcopy(CLAIMS),
              "note": "估时留存，不是出工安排；未知情境可先保存，但以后未必可与实际比较。"}
    result["preview_sha256"] = _hash(result)
    return result


def freeze_prediction(state, data, *, now=None):
    """返回一个新冻结对象；由服务端事务追加到 journal，禁止客户端提供冻结时刻。"""
    if not isinstance(data, dict) or data.get("confirmed") is not True:
        raise InputError("confirmed: 需要本人明确确认事前留存这次估时")
    allowed = {"revision", "task_id", "worker_id", "quantity", "expected_start_at", "point_policy", "confirmed", "preview_sha256", "mode"}
    if set(data) - allowed:
        raise InputError("冻结输入包含服务端专用或未知字段")
    if type(data.get("revision")) is not int or data["revision"] != state.get("revision"):
        raise InputError("revision: 资料已变化，请重新预览")
    if "mode" in data and data["mode"] != state.get("mode"):
        raise InputError("mode: 预览与资料模式不一致")
    moment = _now(now)
    expected = _instant(data.get("expected_start_at"))
    if expected <= moment:
        raise InputError("expected_start_at: 只能为未来尚未开始的目标冻结估时")
    policy = data.get("point_policy", "none")
    if not isinstance(policy, str) or policy not in POINT_POLICIES:
        raise InputError("point_policy: 只能为 none / slow_bound / midpoint")
    preview = preview_prediction(state, data.get("task_id"), data.get("worker_id"), data.get("quantity"))
    if not preview["ready"]:
        raise InputError("无法冻结：" + "；".join(r["message"] for r in preview["reasons"]))
    if data.get("preview_sha256") != preview["preview_sha256"]:
        raise InputError("preview_sha256: 估时依据已变化，请重新预览并确认")
    try:
        zone = ZoneInfo(preview["timezone"])
    except (ValueError, TypeError, KeyError) as exc:
        raise InputError("timezone: 时区无效") from exc
    frozen_at = moment.astimezone(zone).isoformat()
    estimate = deepcopy(preview["estimate"])
    estimate["point_minutes"] = (estimate["high_minutes"] if policy == "slow_bound" else
                                  (estimate["low_minutes"] + estimate["high_minutes"]) / 2 if policy == "midpoint" else None)
    snapshot = capture_rate_context(state, preview["task_id"], preview["worker_id"], captured_at=frozen_at)
    prediction = {"schema_version": PREDICTION_SCHEMA, "frozen_at": frozen_at,
                  "expected_start_at": expected.astimezone(zone).isoformat(),
                  "mode": state.get("mode"), "timezone": preview["timezone"],
                  "task_id": preview["task_id"], "worker_id": preview["worker_id"],
                  "target_quantity": deepcopy(preview["target_quantity"]),
                  "remaining_quantity_at_freeze": deepcopy(preview["remaining_quantity"]),
                  "rate_snapshot": deepcopy(preview["rate_snapshot"]), "estimate": estimate,
                  "point_policy": policy, "context_snapshot": snapshot, "source_revision": state["revision"],
                  "algorithm_version": __version__, "trust_status": "local_frozen",
                  "preview_sha256": preview["preview_sha256"], "confirmed": True,
                  "claims": deepcopy(CLAIMS), "note": "事前输入估时留存，不是出工安排或安全许可。"}
    prediction["prediction_id"] = "prediction-" + prediction_sha256(prediction)[:24]
    prediction["sha256"] = prediction_sha256(prediction)
    journal = state.get("prediction_journal", [])
    if not isinstance(journal, list):
        raise InputError("prediction_journal: 需要数组")
    if any(isinstance(p, dict) and p.get("prediction_id") == prediction["prediction_id"] for p in journal):
        raise InputError("该冻结对象已存在，不重复创建或覆盖")
    return prediction


def _snapshot_context(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "community-rate-context-1" or snapshot.get("captured_by") != "server":
        return None, ["HISTORICAL_CONTEXT_MISSING_OR_INVALID"]
    if snapshot.get("sha256") != _hash({k: v for k, v in snapshot.items() if k != "sha256"}):
        return None, ["HISTORICAL_CONTEXT_HASH_MISMATCH"]
    context = snapshot.get("context")
    return context, _context_reasons(context)


def _compare(prediction, actual, moment):
    reasons = []
    if "imported_at" in actual or actual.get("evidence_type") == "IMPORTED_SELF_REPORT":
        reasons.append("ACTUAL_IMPORTED_UNVERIFIED")
    if (actual.get("consent") is not True or actual.get("withdrawn") is True or actual.get("withdrawn_at")
            or actual.get("verification_status") == "withdrawn"):
        reasons.append("ACTUAL_WITHDRAWN_OR_NO_CONSENT")
    if actual.get("status") == "interrupted":
        reasons.append("ACTUAL_INTERRUPTED")
    elif actual.get("status") not in {"completed", "partial"}:
        reasons.append("ACTUAL_STATUS_UNKNOWN_OR_NOT_DONE")
    if actual.get("context_matches_task") is not True:
        reasons.append("ACTUAL_CONTEXT_NOT_CONFIRMED")
    if actual.get("task_id") != prediction.get("task_id") or actual.get("worker_id") != prediction.get("worker_id"):
        reasons.append("ACTUAL_TASK_OR_WORKER_MISMATCH")
    for key in ("quantity_source", "clock_source"):
        if not _source(actual.get(key)):
            reasons.append("ACTUAL_SOURCE_UNKNOWN_OR_COPIED")
    if actual.get("scope", "net_work") != "net_work" or actual.get("time_scope", "net_work") != "net_work":
        reasons.append("ACTUAL_NOT_NET_WORK")
    original_context, original_issues = _snapshot_context(prediction.get("context_snapshot"))
    actual_context, actual_issues = _snapshot_context(actual.get("context_snapshot"))
    reasons.extend(original_issues + actual_issues)
    if not original_issues and not actual_issues:
        for context, owner in ((original_context, prediction), (actual_context, actual)):
            if context.get("task_id") != owner.get("task_id") or context.get("worker_id") != owner.get("worker_id"):
                reasons.append("CONTEXT_REFERENCE_MISMATCH")
        if _semantic(original_context) != _semantic(actual_context):
            reasons.append("ACTUAL_CONTEXT_MISMATCH")
    conversion = None
    try:
        target, unit = canonical_quantity(prediction.get("target_quantity"))
        amount, actual_unit = canonical_quantity(actual.get("completed_quantity"))
        if unit != actual_unit or not _positive(amount) or not math.isclose(target, amount, abs_tol=EPS, rel_tol=1e-10):
            raise InputError("本次完成量不等于冻结目标")
        if actual["completed_quantity"]["unit"] != prediction["target_quantity"]["unit"]:
            conversion = {"from_unit": actual["completed_quantity"]["unit"], "to_unit": prediction["target_quantity"]["unit"],
                          "factor": prediction["target_quantity"]["value"] / actual["completed_quantity"]["value"], "basis": "1 mu = 2000/3 sqm"}
    except (InputError, ValueError, TypeError, KeyError):
        reasons.append("ACTUAL_QUANTITY_MISMATCH_OR_UNKNOWN")
    try:
        frozen, expected = _instant(prediction["frozen_at"]), _instant(prediction["expected_start_at"])
        start, end, recorded = (_instant(actual.get(k)) for k in ("started_at", "ended_at", "recorded_at"))
        zone = ZoneInfo(prediction["timezone"])
        if start <= frozen:
            reasons.append("ACTUAL_NOT_AFTER_FREEZE")
        if start.astimezone(zone).date() != expected.astimezone(zone).date():
            reasons.append("ACTUAL_DATE_MISMATCH")
        net, rest = actual.get("net_minutes"), actual.get("rest_minutes")
        if not start < end <= recorded <= moment or not _positive(net) or type(rest) not in (int, float) or not math.isfinite(rest) or rest < 0 or net + rest > (end-start).total_seconds()/60 + EPS:
            reasons.append("ACTUAL_TIME_INVALID_OR_UNKNOWN")
        if isinstance(actual.get("context_snapshot"), dict) and _instant(actual["context_snapshot"].get("captured_at")) > recorded:
            reasons.append("ACTUAL_SNAPSHOT_AFTER_RECORDING")
    except (InputError, ValueError, TypeError, KeyError):
        reasons.append("ACTUAL_TIME_INVALID_OR_UNKNOWN")
    if actual.get("evidence_type") == "SYNTHETIC" and prediction.get("mode") == "real":
        reasons.append("SYNTHETIC_REAL_MODE_MISMATCH")
    if reasons:
        return None, sorted(set(reasons))
    estimate = prediction.get("estimate")
    if not isinstance(estimate, dict) or not _positive(estimate.get("low_minutes")) or not _positive(estimate.get("high_minutes")):
        return None, ["FROZEN_ESTIMATE_OR_TIME_INVALID"]
    point = estimate.get("point_minutes")
    if point is not None and not _positive(point):
        return None, ["FROZEN_ESTIMATE_OR_TIME_INVALID"]
    policy = prediction.get("point_policy")
    if not isinstance(policy, str) or policy not in POINT_POLICIES:
        return None, ["FROZEN_ESTIMATE_OR_TIME_INVALID"]
    return {"evidence_basis": "self_report", "actual_event_id": actual["event_id"],
            "actual_net_minutes": actual["net_minutes"], "actual_quantity": deepcopy(actual["completed_quantity"]),
            "unit_conversion": conversion, "predicted_low_minutes": estimate["low_minutes"],
            "predicted_high_minutes": estimate["high_minutes"], "point_policy": policy,
            "predicted_point_minutes": point,
            "actual_minus_point_minutes": actual["net_minutes"] - point if point is not None else None,
            "within_input_range": estimate["low_minutes"] - EPS <= actual["net_minutes"] <= estimate["high_minutes"] + EPS,
            "meaning": "单条自述净劳动时间与事前输入范围对照，不是总体准确率；差值为实际减点估计。"}, []


def review_predictions(state, *, now=None):
    """只读冻结原文与显式关联记录；不从当前任务/速率重建旧预测。"""
    journal = state.get("prediction_journal", []) if isinstance(state, dict) else None
    if not isinstance(journal, list) or any(not isinstance(p, dict) for p in journal):
        raise InputError("prediction_journal: 需要冻结对象数组")
    events = state.get("feedback", [])
    current, superseded, archive_errors = _archive(events)
    _hash(journal)
    moment = _now(now)
    ids = [p.get("prediction_id") for p in journal if isinstance(p.get("prediction_id"), str)]
    counts = Counter(ids)
    result = []
    for prediction in journal:
        ident = prediction.get("prediction_id")
        reasons = []
        integrity = prediction.get("sha256") == prediction_sha256(prediction)
        if not integrity:
            reasons.append("PREDICTION_HASH_MISMATCH")
        if prediction.get("schema_version") != PREDICTION_SCHEMA or not _source(ident):
            reasons.append("PREDICTION_SCHEMA_INVALID")
        if isinstance(ident, str) and counts[ident] != 1:
            reasons.append("PREDICTION_ID_CONFLICT")
        if prediction.get("trust_status") == "imported_unverified" or "imported_at" in prediction:
            reasons.append("IMPORTED_UNVERIFIED")
        elif prediction.get("trust_status") != "local_frozen":
            reasons.append("LOCAL_FREEZE_NOT_ESTABLISHED")
        if prediction.get("mode") != state.get("mode"):
            reasons.append("PREDICTION_MODE_MISMATCH")
        try:
            if not _instant(prediction["frozen_at"]) < _instant(prediction["expected_start_at"]) or _instant(prediction["frozen_at"]) > moment:
                raise InputError("冻结时刻不符合事前要求")
            estimate = prediction["estimate"]
            policy = prediction["point_policy"]
            if not isinstance(estimate, dict) or not _positive(estimate.get("low_minutes")) or not _positive(estimate.get("high_minutes")) or estimate["low_minutes"] > estimate["high_minutes"] or estimate.get("scope") != "net_work":
                raise InputError("冻结区间无效")
            expected_point = (None if policy == "none" else estimate["high_minutes"] if policy == "slow_bound" else
                              (estimate["low_minutes"] + estimate["high_minutes"])/2 if policy == "midpoint" else "invalid")
            if (expected_point == "invalid" or estimate.get("point_minutes") != expected_point
                    or (expected_point is not None and not _positive(estimate.get("point_minutes")))):
                raise InputError("冻结点政策与点值不一致")
        except (InputError, ValueError, TypeError, KeyError):
            reasons.append("FROZEN_ESTIMATE_OR_TIME_INVALID")
        linked = [e for e in events if isinstance(ident, str) and e.get("prediction_id") == ident]
        active = [e for e in current if isinstance(ident, str) and e.get("prediction_id") == ident]
        links = [{"event_id": e.get("event_id"), "status": e.get("status"),
                  "is_current": not isinstance(e.get("event_id"), str) or e["event_id"] not in superseded,
                  "reason_codes": ["SUPERSEDED"] if isinstance(e.get("event_id"), str) and e["event_id"] in superseded else [],
                  "record_sha256": _hash(e)} for e in linked]
        comparison = None
        if archive_errors:
            reasons.extend(archive_errors)
        if len(active) > 1:
            reasons.append("MULTIPLE_CURRENT_ASSOCIATIONS")
            for link in links:
                if link["is_current"]:
                    link["reason_codes"].append("MULTIPLE_CURRENT_ASSOCIATIONS")
        elif len(active) == 1:
            tentative, actual_reasons = _compare(prediction, active[0], moment)
            reasons.extend(actual_reasons)
            for link in links:
                if link["is_current"]:
                    link["reason_codes"].extend(actual_reasons)
            if not reasons:
                comparison = tentative
        status = ("association_conflict" if len(active) > 1 else "comparable_self_report" if comparison is not None
                  else "not_comparable" if reasons or active else "awaiting_actual")
        result.append({"prediction_id": ident, "status": status, "prediction_sha256": prediction.get("sha256"),
                       "frozen": deepcopy(prediction), "prediction_integrity_ok": integrity,
                       "reason_codes": sorted(set(reasons)), "current_event_ids": [e.get("event_id") for e in active],
                       "linked_records": links, "comparison": comparison})
    return {"schema_version": REVIEW_SCHEMA, "predictions": result,
            "unlinked_record_ids": [e.get("event_id") for e in current if not e.get("prediction_id")],
            "orphan_prediction_links": [{"event_id": e.get("event_id"), "prediction_id": e.get("prediction_id")}
                                        for e in current if e.get("prediction_id") and e.get("prediction_id") not in ids],
            "claims": deepcopy(CLAIMS),
            "warnings": ["原预测只保留冻结时的输入区间与明确选择的点估计政策，不用今日速率重算。",
                         "差值与是否落入范围只描述一条自述记录，不是总体准确率、校准或健康认证。",
                         "导入信任标记须由服务端降级；内容哈希不能证明它曾在本机事前冻结。"]}
