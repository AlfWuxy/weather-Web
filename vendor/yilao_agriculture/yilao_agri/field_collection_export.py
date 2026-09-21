"""把已加载的社区资料投影为待复核材料；不采集、认证、冻结或写回任何资料。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
import hmac
import json
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .community_catalog import get_community_catalog

SCHEMA = "field-collection-draft-1"
UNITS = {"mu", "sqm", "kg", "plant", "trip", "m", "m3"}
STATUSES = {"completed", "partial", "interrupted", "not_done", "unknown"}
REVIEW_FIELDS = (
    "local_date", "timezone", "setup_minutes", "travel_minutes", "other_minutes",
    "tool", "posture", "field_condition", "interruption_reason", "observer_id", "observation_ref",
    "consent_status", "consent_ref", "review_status", "reviewer_id", "review_ref", "split",
    "data_origin", "prediction_id", "prediction_sha256",
)


class FieldCollectionExportError(ValueError):
    """错误只含固定说明，不回显原始身份或资料。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def projection_sha256(package):
    """仅验证导出投影的一致性；不是源记录、正式冻结或真实性证明。"""
    return sha256(_json({k: v for k, v in package.items() if k != "projection_sha256"}).encode()).hexdigest()


def _id(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 150


def _index(rows, key, limit):
    if not isinstance(rows, list) or len(rows) > limit or any(not isinstance(row, dict) for row in rows):
        raise FieldCollectionExportError("记录集合格式或数量不符合社区资料契约")
    ids = [row.get(key) for row in rows]
    if any(not _id(value) for value in ids) or len(ids) != len(set(ids)):
        raise FieldCollectionExportError("记录编号缺失或重复，不能生成不明确的代号关联")
    return dict(zip(ids, rows))


def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.isoformat() if moment.utcoffset() is not None else None
    except ValueError:
        return None


def _zone(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        return ZoneInfo(value).key
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _integrity(row, *, prediction=False):
    excluded = {"sha256", "trust_status", "imported_at"} if prediction else {"sha256"}
    try:
        digest = sha256(_json({k: v for k, v in row.items() if k not in excluded}).encode()).hexdigest()
        return row.get("sha256") == digest
    except (ValueError, TypeError, RecursionError):
        return False


def _context(row, alias, crops, tasks):
    snapshot = row.get("context_snapshot")
    valid = (isinstance(snapshot, dict) and snapshot.get("schema_version") == "community-rate-context-1"
             and snapshot.get("captured_by") == "server" and _integrity(snapshot))
    context = snapshot.get("context") if valid else None
    valid = (valid and isinstance(context, dict) and context.get("worker_id") == row.get("worker_id")
             and context.get("task_id") == row.get("task_id"))
    context = context if valid else {}
    code = _choice(context.get("task_code"), tasks)
    plot = context.get("plot") if isinstance(context.get("plot"), dict) else {}
    work = context.get("work") if isinstance(context.get("work"), dict) else {}
    candidates = {"plot_id": alias("PL", plot.get("id")), "crop_id": _choice(context.get("crop_id"), crops),
                  "task_code": code, "method": _choice(context.get("method"), tasks[code]["methods"] if code else ()),
                  "load_kg_per_trip": _number(work.get("load_per_trip_kg")),
                  "distance_m": _number(work.get("distance_m"))}
    return bool(valid), candidates


def _history(events):
    """断链、分叉、环或换绑时保留全部行，但不选择有效版本或计算当前行数。"""
    successors, superseded, valid = {}, set(), True
    for ident, row in events.items():
        previous = row.get("supersedes_event_id")
        if previous is None:
            continue
        original = events.get(previous) if isinstance(previous, str) else None
        if (original is None or previous == ident or previous in successors
                or any(row.get(k) != original.get(k) for k in ("task_id", "worker_id", "prediction_id"))):
            valid = False
        if original is not None:
            successors[previous] = ident
            superseded.add(previous)
    for ident in events:
        cursor, seen = ident, set()
        while cursor in successors:
            if cursor in seen:
                valid = False
                break
            seen.add(cursor)
            cursor = successors[cursor]
    return valid, superseded


def _lineage(row, events):
    """更正不能抹去旧版本的导入、合成或撤回声明；断链也不越界猜测。"""
    lineage, seen = [row], set()
    previous = row.get("supersedes_event_id")
    while isinstance(previous, str) and previous in events and previous not in seen:
        seen.add(previous)
        row = events[previous]
        lineage.append(row)
        previous = row.get("supersedes_event_id")
    return lineage


def export_field_collection(state: dict, *, export_secret: bytes) -> dict:
    """同一账户持续使用同一独立密钥；调用方负责账户授权、保密和持久化。

    原始身份只参与分域 HMAC，不进入结果。这里不接受客户端账户号、路径或时钟，
    不读取运行库、不写文件、不改变输入，不把本设备保存同意升级为现场观察同意。
    """
    if not isinstance(export_secret, bytes) or len(export_secret) < 32:
        raise FieldCollectionExportError("export_secret 必须为调用方提供的至少32字节账户独立密钥")
    if (not isinstance(state, dict) or state.get("schema_version") != "community-1"
            or _choice(state.get("mode"), {"real", "demonstration"}) is None):
        raise FieldCollectionExportError("需要已加载的社区真实区或演示区资料")
    mode = state["mode"]
    events = _index(state.get("feedback"), "event_id", 5000)
    journal = _index(state.get("prediction_journal", []), "prediction_id", 1000)
    catalog = get_community_catalog()
    crops = {row["id"] for row in catalog["crops"]}
    tasks = {row["code"]: row for row in catalog["tasks"]}

    def alias(kind, value):
        if not _id(value):
            return None
        message = _json([SCHEMA, mode, kind, value]).encode()
        return kind + "-" + hmac.new(export_secret, message, sha256).hexdigest()[:32].upper()

    history_ok, superseded = _history(events)
    active_links = Counter(row.get("prediction_id") for ident, row in events.items()
                           if history_ok and ident not in superseded and isinstance(row.get("prediction_id"), str))
    records = []
    for ident, row in events.items():
        reasons = ["SELF_REPORT_NOT_FIELD_EVIDENCE", "FIELD_CONSENT_AND_INDEPENDENT_REVIEW_NOT_COLLECTED",
                   "FORMAL_SPLIT_NOT_ASSIGNED", "HISTORICAL_TIMEZONE_NOT_RECORDED"]
        context_ok, candidates = _context(row, alias, crops, tasks)
        if not context_ok:
            reasons.append("HISTORICAL_CONTEXT_MISSING_OR_INVALID")
        if row.get("context_matches_task") is not True:
            reasons.append("ACTUAL_CONTEXT_NOT_CONFIRMED")
        times = {out: _time(row.get(source)) for out, source in
                 (("start_at", "started_at"), ("end_at", "ended_at"), ("recorded_at", "recorded_at"))}
        elapsed = None
        if all(times.values()):
            start, end, recorded = [datetime.fromisoformat(times[k]) for k in ("start_at", "end_at", "recorded_at")]
            if start < end <= recorded:
                elapsed = (end - start).total_seconds() / 60
                if start.date() != end.date() or start.utcoffset() != end.utcoffset():
                    reasons.append("LOCAL_DAY_REQUIRES_REVIEW_NO_AUTOMATIC_SPLIT")
            else:
                reasons.append("ACTUAL_TIME_ORDER_INVALID")
        else:
            reasons.append("ACTUAL_TIME_MISSING_OR_INVALID")
        quantity = row.get("completed_quantity")
        quantity = quantity if isinstance(quantity, dict) else {}
        reported = {**times, "elapsed_minutes": elapsed, "quantity": _number(quantity.get("value")),
                    "unit": _choice(quantity.get("unit"), UNITS), "net_minutes": _number(row.get("net_minutes")),
                    "rest_minutes": _number(row.get("rest_minutes")), "status": _choice(row.get("status"), STATUSES)}
        if reported["status"] != "completed":
            reasons.append("FORMAL_COMPLETED_OUTCOME_NOT_ESTABLISHED")
        code = candidates["task_code"]
        if code and reported["unit"] is not None and reported["unit"] not in tasks[code]["valid_units"]:
            reasons.append("QUANTITY_UNIT_NOT_SUPPORTED_FOR_TASK")
        amount, net, status = reported["quantity"], reported["net_minutes"], reported["status"]
        if (status is None or (status == "unknown" and (amount is not None or net is not None))
                or (status == "not_done" and (amount != 0 or net != 0))
                or (status in {"completed", "partial", "interrupted"}
                    and (amount is None or net is None or amount <= 0 or net <= 0))
                or (reported["unit"] in {"trip", "plant"} and amount is not None and amount % 1 != 0)):
            reasons.append("REPORTED_QUANTITY_OR_STATUS_INCOMPATIBLE")
        if elapsed is not None and reported["net_minutes"] is not None and reported["rest_minutes"] is not None:
            if reported["net_minutes"] + reported["rest_minutes"] > elapsed + 1e-7:
                reasons.append("ACTUAL_MINUTES_EXCEED_ELAPSED")
        lineage = _lineage(row, events)
        source = {"mode": mode, "evidence_type": _choice(row.get("evidence_type"),
                  {"SELF_REPORT", "IMPORTED_SELF_REPORT", "SYNTHETIC"}) or "unknown",
                  "imported": any("imported_at" in prior or prior.get("evidence_type") == "IMPORTED_SELF_REPORT" for prior in lineage),
                  "synthetic_declared_or_inherited": any(prior.get("evidence_type") == "SYNTHETIC" for prior in lineage),
                  "local_storage_consent": row.get("consent") if type(row.get("consent")) is bool else None,
                  "withdrawal_declared": any(prior.get("withdrawn") is True or bool(prior.get("withdrawn_at"))
                  or prior.get("verification_status") == "withdrawn" for prior in lineage), "assurance": "unverified_declaration"}
        if source["imported"]:
            reasons.append("IMPORTED_UNVERIFIED")
        if mode == "demonstration" or source["synthetic_declared_or_inherited"]:
            reasons.append("DEMONSTRATION_OR_SYNTHETIC")
        if source["withdrawal_declared"] or source["local_storage_consent"] is not True:
            reasons.append("WITHDRAWAL_OR_LOCAL_CONSENT_MISSING")
        pred_id = row.get("prediction_id")
        if pred_id is not None:
            reasons.append("COMMUNITY_PREDICTION_IS_NOT_FORMAL_SNAPSHOT")
            if not isinstance(pred_id, str) or pred_id not in journal:
                reasons.append("PREDICTION_REFERENCE_MISSING")
            elif any(row.get(k) != journal[pred_id].get(k) for k in ("worker_id", "task_id")):
                reasons.append("PREDICTION_REFERENCE_MISMATCH")
            else:
                frozen = _time(journal[pred_id].get("frozen_at"))
                if frozen and times["start_at"] and datetime.fromisoformat(times["start_at"]) <= datetime.fromisoformat(frozen):
                    reasons.append("ACTUAL_NOT_AFTER_COMMUNITY_FREEZE")
            if isinstance(pred_id, str) and active_links[pred_id] > 1:
                reasons.append("MULTIPLE_CURRENT_PREDICTION_ASSOCIATIONS")
        if not history_ok:
            reasons.append("CORRECTION_HISTORY_INVALID")
        if not alias("P", row.get("worker_id")) or not alias("T", row.get("task_id")):
            reasons.append("PERSON_OR_TASK_REFERENCE_MISSING")
        missing = ["review_fields." + k for k in REVIEW_FIELDS]
        missing += ["reported." + k for k, value in reported.items() if value is None]
        missing += ["historical_context_candidates." + k for k, value in candidates.items() if value is None]
        records.append({"record_id": alias("R", ident), "person_id": alias("P", row.get("worker_id")),
                        "task_id": alias("T", row.get("task_id")), "reported": reported,
                        "historical_context_candidates": candidates, "context_integrity_ok": context_ok,
                        "context_matches_task_declared": row.get("context_matches_task") if type(row.get("context_matches_task")) is bool else None,
                        "source": source, "source_prediction_id": alias("PR", pred_id),
                        "supersedes_record_id": alias("R", row.get("supersedes_event_id")),
                        "is_current": ident not in superseded if history_ok else None,
                        "review_fields": dict.fromkeys(REVIEW_FIELDS), "missing_fields": missing,
                        "reason_codes": sorted(set(reasons))})

    projections = []
    for ident, row in journal.items():
        reasons = ["PROJECTION_NOT_FORMAL_FROZEN_SNAPSHOT", "FORMAL_PROTOCOL_AND_BASIS_REFS_NOT_BOUND"]
        context_ok, candidates = _context(row, alias, crops, tasks)
        estimate = row.get("estimate") if isinstance(row.get("estimate"), dict) else {}
        target = row.get("target_quantity") if isinstance(row.get("target_quantity"), dict) else {}
        policy = _choice(row.get("point_policy"), {"none", "midpoint", "slow_bound"})
        estimate = {"low_minutes": _number(estimate.get("low_minutes")), "high_minutes": _number(estimate.get("high_minutes")),
                    "point_minutes": _number(estimate.get("point_minutes")), "scope": _choice(estimate.get("scope"), {"net_work"})}
        if policy == "none" or estimate["point_minutes"] is None:
            reasons.append("FORMAL_POINT_PREDICTION_NOT_FROZEN")
        low, high, point = [estimate[k] for k in ("low_minutes", "high_minutes", "point_minutes")]
        if low is None or high is None or low <= 0 or high < low or estimate["scope"] is None:
            reasons.append("FROZEN_ESTIMATE_INVALID")
        else:
            expected = None if policy == "none" else high if policy == "slow_bound" else (low + high) / 2 if policy == "midpoint" else "invalid"
            if expected == "invalid" or point != expected:
                reasons.append("FROZEN_POINT_POLICY_INVALID")
        integrity = _integrity(row, prediction=True)
        if not integrity:
            reasons.append("SOURCE_PREDICTION_HASH_MISMATCH")
        trust = _choice(row.get("trust_status"), {"local_frozen", "imported_unverified"}) or "unknown"
        imported = "imported_at" in row or trust == "imported_unverified"
        if imported:
            reasons.append("IMPORTED_UNVERIFIED")
        if not context_ok:
            reasons.append("HISTORICAL_CONTEXT_MISSING_OR_INVALID")
        if row.get("mode") != mode:
            reasons.append("SOURCE_MODE_MISMATCH")
        if mode == "demonstration" or row.get("mode") == "demonstration":
            reasons.append("DEMONSTRATION_OR_SYNTHETIC")
        if active_links[ident] > 1:
            reasons.append("MULTIPLE_CURRENT_PREDICTION_ASSOCIATIONS")
        projected = {"prediction_projection_id": alias("PR", ident), "person_id": alias("P", row.get("worker_id")),
                     "task_id": alias("T", row.get("task_id")), "frozen_at_declared": _time(row.get("frozen_at")),
                     "expected_start_at": _time(row.get("expected_start_at")), "timezone": _zone(row.get("timezone")),
                     "target_quantity": {"value": _number(target.get("value")), "unit": _choice(target.get("unit"), UNITS)},
                     "estimate": estimate, "point_policy": policy, "historical_context_candidates": candidates,
                     "source": {"declared_mode": _choice(row.get("mode"), {"real", "demonstration"}),
                                "declared_trust_status": trust, "imported": imported,
                                "assurance": "unverified_declaration"},
                     "source_integrity_ok": integrity, "context_integrity_ok": context_ok,
                     "linked_record_ids": [alias("R", event_id) for event_id, event in events.items() if event.get("prediction_id") == ident],
                     "reason_codes": sorted(set(reasons))}
        projected["missing_fields"] = [k for k in ("frozen_at_declared", "expected_start_at", "timezone", "point_policy") if projected[k] is None]
        projected["missing_fields"] += ["estimate." + k for k, value in estimate.items() if value is None]
        projected["missing_fields"] += ["target_quantity." + k for k, value in projected["target_quantity"].items() if value is None]
        projected["missing_fields"] += ["historical_context_candidates." + k for k, value in candidates.items() if value is None]
        code = candidates["task_code"]
        if code and projected["target_quantity"]["unit"] is not None and projected["target_quantity"]["unit"] not in tasks[code]["valid_units"]:
            projected["reason_codes"].append("QUANTITY_UNIT_NOT_SUPPORTED_FOR_TASK")
        if not projected["frozen_at_declared"] or not projected["expected_start_at"] or datetime.fromisoformat(projected["frozen_at_declared"]) >= datetime.fromisoformat(projected["expected_start_at"]):
            projected["reason_codes"].append("DECLARED_FREEZE_TIME_INVALID")
        projections.append(projected)
    result = {"schema_version": SCHEMA, "mode": mode,
              "identity_namespace": hmac.new(export_secret, _json([SCHEMA, mode, "identity_namespace"]).encode(), sha256).hexdigest()[:32],
              "claims": {"n_real": 0, "field_verified": False, "formal_validation_ready": False},
              "counts": {"archived_record_versions": len(records),
                         "current_record_versions": sum(r["is_current"] is True for r in records) if history_ok else None,
                         "prediction_projections": len(projections)},
              "records": records, "prediction_projections": projections,
              "warnings": ["去直接标识不等于完全匿名；日期、时刻和作业组合仍可能识别人。",
                           "仅整理未核验自述及来源声明；没有现场观察同意、独立复核、实测样本或正式冻结。",
                           "原始记录与身份映射留在本机；本包不能还原或验证原始对象的完整正文。",
                           "投影哈希只核对本包内容；不证明来源、事前时序或真实性。",
                           "同一账户须持续使用同一独立密钥；不同账户不可共用密钥。跨账户同人不能自动识别。",
                           "密钥轮换或恢复备份时缺少原密钥会改变代号范围；不同identity_namespace不能自动合并去重。"]}
    result["projection_sha256"] = projection_sha256(result)
    return result
