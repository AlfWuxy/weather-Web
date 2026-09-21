"""可执行的农活实测校验、预测冻结和独立留出评估；不作临床有效性声明。"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .community_catalog import get_community_catalog, validate_task_quantity
from .models import InputError

SCHEMA_VERSION = "field-validation-1"
CSV_FIELDS = (
    "record_id", "person_id", "plot_id", "local_date", "timezone", "start_at", "end_at", "recorded_at",
    "crop_id", "task_code", "method", "quantity", "unit", "net_minutes", "elapsed_minutes",
    "setup_minutes", "travel_minutes", "rest_minutes", "other_minutes", "status", "split",
    "consent_status", "consent_ref", "data_origin", "observation_ref", "observer_id", "review_status",
    "reviewer_id", "review_ref", "prediction_id", "prediction_sha256", "interruption_reason",
    "load_kg_per_trip", "distance_m", "tool", "posture", "field_condition",
)
STATUSES = {"completed", "partial", "interrupted", "not_done", "unknown"}
ORIGINS = {"actual_field", "synthetic", "demonstration", "unverified_import"}
PERFORMED = {"completed", "partial", "interrupted"}
ID_RE = re.compile(r"^[A-Z][A-Z0-9]{0,7}-[A-Z0-9]{4,32}$")
REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,100}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
PREDICTION_FIELDS = {
    "prediction_id", "person_id", "plot_id", "local_date", "timezone", "crop_id", "task_code",
    "method", "quantity", "unit", "scope", "low_minutes", "point_minutes", "high_minutes",
    "rate_basis", "basis_refs", "model_version", "tool", "posture", "field_condition",
    "load_kg_per_trip", "distance_m",
}
GROUP_FIELDS = ("person_id", "local_date", "plot_id")
PROTOCOL_FIELDS = {"min_records", "min_people", "min_days", "min_plots", "rationale"}


class FieldValidationError(InputError):
    """校验失败时保持关闭，错误不回显可能的身份原文。"""


def _assessment_now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FieldValidationError("内容必须是有限、可序列化的 JSON") from exc
    return sha256(raw.encode("utf-8")).hexdigest()


def record_sha256(record: dict) -> str:
    """规范化实测行的指纹，供脱敏现场日志及复核日志绑定。"""
    return _digest(validate_record(record))


def _text(value: Any, field: str, *, blank: bool = False) -> str:
    if not isinstance(value, str) or (not blank and not value.strip()):
        raise FieldValidationError(f"{field}: 必须为{'可空' if blank else '非空'}字符串")
    if len(value) > 300 or any(ord(ch) < 32 for ch in value):
        raise FieldValidationError(f"{field}: 不允许控制字符或超过300字符")
    return value.strip()


def _id(value: Any, field: str) -> str:
    value = _text(value, field)
    if not ID_RE.fullmatch(value):
        raise FieldValidationError(f"{field}: 使用形如 P-A1B2 的随机匿名编号，不填写姓名电话")
    return value


def _ref(value: Any, field: str, *, blank: bool = False) -> str:
    value = _text(value, field, blank=blank)
    if value == "" and blank:
        return value
    if not REF_RE.fullmatch(value):
        raise FieldValidationError(f"{field}: 仅允许非识别性凭据编号，不接受个人信息或路径")
    return value


def _number(value: Any, field: str, *, zero: bool = False, blank: bool = False) -> float | None:
    if blank and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FieldValidationError(f"{field}: 必须是有限数字")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise FieldValidationError(f"{field}: 必须是有限数字") from exc
    if not math.isfinite(number) or number < 0 or (not zero and number == 0):
        raise FieldValidationError(f"{field}: 必须是有限{'非负' if zero else '正'}数")
    return number


def _timestamp(value: Any, field: str) -> datetime:
    text = _text(value, field)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FieldValidationError(f"{field}: 必须是 ISO 时间") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise FieldValidationError(f"{field}: 时间必须含时区")
    return result.astimezone(timezone.utc)


def _date_and_zone(row: dict) -> tuple[date, ZoneInfo]:
    try:
        day = date.fromisoformat(_text(row.get("local_date"), "local_date"))
        zone = ZoneInfo(_text(row.get("timezone"), "timezone"))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise FieldValidationError("local_date/timezone: 必须为日历日期和有效 IANA 时区") from exc
    return day, zone


def _choice(value: Any, choices: set, field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise FieldValidationError(f"{field}: 不在允许的状态集合内")
    return value


def validate_record(raw: dict) -> dict:
    """严格读入一条；未知字段拒绝，净劳动与经过时间逐项对账。"""
    if not isinstance(raw, dict) or set(raw) != set(CSV_FIELDS):
        raise FieldValidationError("记录必须恰好包含 CSV_FIELDS 中的字段，不接受额外身份或健康字段")
    row = deepcopy(raw)
    for field in ("record_id", "person_id", "plot_id", "observer_id"):
        row[field] = _id(row[field], field)
    row["reviewer_id"] = _id(row["reviewer_id"], "reviewer_id") if row["reviewer_id"] else ""
    for field in ("consent_ref", "observation_ref", "review_ref", "prediction_id"):
        row[field] = _ref(row[field], field, blank=True)
    row["status"] = _choice(row["status"], STATUSES, "status")
    row["split"] = _choice(row["split"], {"training", "holdout"}, "split")
    row["consent_status"] = _choice(row["consent_status"], {"granted", "pending", "declined", "withdrawn"}, "consent_status")
    row["data_origin"] = _choice(row["data_origin"], ORIGINS, "data_origin")
    row["review_status"] = _choice(row["review_status"], {"pending", "confirmed", "rejected"}, "review_status")
    if row["consent_status"] == "granted" and not row["consent_ref"]:
        raise FieldValidationError("consent_ref: 已同意记录必须保留同意凭据编号")
    if row["review_status"] == "confirmed" and (not row["reviewer_id"] or not row["review_ref"]):
        raise FieldValidationError("review_status: 已复核必须有复核者及凭据")
    if row["data_origin"] == "actual_field" and not row["observation_ref"]:
        raise FieldValidationError("observation_ref: 声称实测的记录必须有脱敏现场日志引用")
    day, zone = _date_and_zone(row)
    start, end, recorded = (_timestamp(row[k], k) for k in ("start_at", "end_at", "recorded_at"))
    if end <= start or recorded < end:
        raise FieldValidationError("时间顺序须为 start_at < end_at <= recorded_at")
    if start.astimezone(zone).date() != day or end.astimezone(zone).date() != day:
        raise FieldValidationError("local_date: 起止时间须属于该当地日；跨日记录按日拆分")
    row["elapsed_minutes"] = _number(row["elapsed_minutes"], "elapsed_minutes")
    if abs((end - start).total_seconds() / 60 - row["elapsed_minutes"]) > 1 / 60:
        raise FieldValidationError("elapsed_minutes: 必须与起止时刻相符（允许1秒舍入差）")
    unknown = row["status"] == "unknown"
    no_work = row["status"] == "not_done"
    for field in ("quantity", "net_minutes"):
        if unknown and row[field] not in (None, ""):
            raise FieldValidationError(f"{field}: unknown 时保持空值，不推断为0或完成量")
        row[field] = _number(row[field], field, zero=no_work, blank=unknown)
        if no_work and row[field] != 0:
            raise FieldValidationError(f"{field}: not_done 必须为明确的0；不知道用unknown")
    for field in ("setup_minutes", "travel_minutes", "rest_minutes", "other_minutes"):
        row[field] = _number(row[field], field, zero=True)
    if not unknown:
        accounted = sum(row[k] for k in ("net_minutes", "setup_minutes", "travel_minutes", "rest_minutes", "other_minutes"))
        if abs(accounted - row["elapsed_minutes"]) > 1 / 60:
            raise FieldValidationError("净劳动+准备+往返+休息+其他 必须等于经过时间，不能把整次出工当净劳动")
    for field in ("tool", "posture", "field_condition", "interruption_reason"):
        row[field] = _text(row[field], field, blank=field == "interruption_reason")
    if row["status"] in {"partial", "interrupted", "not_done"} and not row["interruption_reason"]:
        raise FieldValidationError("interruption_reason: 未全部完成须记录原因，慢记录不删除")
    for field in ("load_kg_per_trip", "distance_m"):
        # 非搬运任务可明确记录零；搬运所需正值仍由任务目录逐项核对。
        row[field] = _number(row[field], field, zero=True, blank=True)
    crop_ids = {c["id"] for c in get_community_catalog()["crops"]}
    if row["crop_id"] not in crop_ids:
        raise FieldValidationError("crop_id: 必须是目录编号，未识别作物先留待确认")
    context = {k: row[k] for k in ("method", "tool", "posture", "field_condition", "load_kg_per_trip", "distance_m")}
    try:
        checked = validate_task_quantity(row["task_code"], row["quantity"] or 1, row["unit"], context)
    except InputError as exc:
        raise FieldValidationError(str(exc)) from exc
    if checked["missing_context"]:
        raise FieldValidationError("任务上下文不完整: " + ", ".join(checked["missing_context"]))
    frozen_hash = row["prediction_sha256"]
    if not isinstance(frozen_hash, str) or (frozen_hash and not HEX_RE.fullmatch(frozen_hash)):
        raise FieldValidationError("prediction_sha256: 必须为空或64位小写内容摘要")
    if bool(row["prediction_id"]) != bool(frozen_hash):
        raise FieldValidationError("预测编号与冻结摘要必须同时填写或同时留空")
    if row["split"] == "training" and row["prediction_id"]:
        raise FieldValidationError("training: 不得标成冻结预测的留出结果")
    return row


def validate_records(records: list[dict]) -> list[dict]:
    if not isinstance(records, list):
        raise FieldValidationError("records: 必须为数组")
    rows = [validate_record(r) for r in records]
    ids = [r["record_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise FieldValidationError("record_id: 同一记录不允许重复计数")
    events = [tuple(r[k] for k in ("person_id", "plot_id", "start_at", "end_at", "task_code", "method")) for r in rows]
    if len(events) != len(set(events)):
        raise FieldValidationError("DUPLICATE_EVENT: 相同作业不能仅换编号重复计数")
    # 三个维度分别查重；仅按(人,日,田块)组合分组仍会泄漏。
    training = [r for r in rows if r["split"] == "training"]
    holdout = [r for r in rows if r["split"] == "holdout"]
    _no_leakage(training, holdout)
    return rows


def _no_leakage(training: list[dict], holdout: list[dict]) -> None:
    for field in GROUP_FIELDS:
        if {r[field] for r in training} & {r[field] for r in holdout}:
            raise FieldValidationError(f"SPLIT_LEAKAGE: training 与 holdout 共享 {field}")


def validate_field_csv(path_or_text: str | Path) -> dict:
    """读取CSV路径或CSV文本；任一行失败时不放行部分数据。"""
    if isinstance(path_or_text, Path):
        text = path_or_text.read_text(encoding="utf-8-sig")
    elif isinstance(path_or_text, str) and ("\n" in path_or_text or "," in path_or_text):
        text = path_or_text.lstrip("\ufeff")
    elif isinstance(path_or_text, str):
        text = Path(path_or_text).read_text(encoding="utf-8-sig")
    else:
        raise FieldValidationError("CSV输入必须为路径或文本")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)) or set(reader.fieldnames) != set(CSV_FIELDS):
        return {"valid": False, "records": [], "errors": [{"line": 1, "message": "CSV表头必须完整且无重复/额外字段"}]}
    rows, errors = [], []
    try:
        for line, raw in enumerate(reader, 2):
            try:
                rows.append(validate_record(raw))
            except (FieldValidationError, TypeError) as exc:
                errors.append({"line": line, "message": str(exc)})
    except csv.Error:
        errors.append({"line": reader.line_num, "message": "CSV语法错误"})
    if not errors:
        try:
            rows = validate_records(rows)
        except FieldValidationError as exc:
            errors.append({"line": None, "message": str(exc)})
    return {"valid": not errors, "records": rows if not errors else [], "errors": errors,
            "record_count": len(rows) if not errors else 0,
            "n_real": 0, "real_count_reason": "CSV自报状态不能证明实测；需核验同意、现场及独立复核凭据"}


def _artifact(ref: str, kind: str, manifest: dict | None, root: Path | str | None) -> dict | None:
    if not manifest or root is None or ref not in manifest:
        return None
    item = manifest[ref]
    if not isinstance(item, dict) or set(item) != {"relative_path", "sha256", "kind"} or item["kind"] != kind:
        return None
    try:
        base = Path(root).resolve(strict=True)
        relative = Path(item["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            return None
        path = (base / relative).resolve(strict=True)
        if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != item["sha256"]:
            return None
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def classify_evidence(record: dict, evidence_manifest: dict | None = None,
                      evidence_root: Path | str | None = None) -> dict:
    """内容凭据可追溯性判定；程序不能认证现实发生或替代人工复核。"""
    row = validate_record(record)
    reasons = []
    if row["data_origin"] != "actual_field":
        return {"classification": row["data_origin"], "counts_as_real": False, "reasons": ["not_declared_field_observation"]}
    if _timestamp(row["recorded_at"], "recorded_at") > _assessment_now():
        reasons.append("future_observation_not_yet_collectable")
    if row["consent_status"] != "granted":
        reasons.append("consent_not_granted")
    if row["review_status"] != "confirmed" or row["reviewer_id"] in {"", row["observer_id"]}:
        reasons.append("independent_review_missing")
    consent = _artifact(row["consent_ref"], "consent_log", evidence_manifest, evidence_root)
    observation = _artifact(row["observation_ref"], "field_log", evidence_manifest, evidence_root)
    review = _artifact(row["review_ref"], "review_log", evidence_manifest, evidence_root)
    expected = _digest(row)
    try:
        if (not consent or consent.get("person_id") != row["person_id"]
                or consent.get("status") != "granted" or consent.get("scope") != "farm_work_observation"
                or consent.get("withdrawn_at") is not None
                or _timestamp(consent.get("obtained_at"), "consent.obtained_at") > _timestamp(row["start_at"], "start_at")):
            reasons.append("consent_artifact_missing_or_mismatched")
        if (not observation or observation.get("record_id") != row["record_id"]
                or observation.get("record_sha256") != expected or observation.get("data_origin") != "actual_field"):
            reasons.append("field_artifact_missing_or_mismatched")
        if (not review or review.get("record_id") != row["record_id"] or review.get("record_sha256") != expected
                or review.get("reviewer_id") != row["reviewer_id"] or review.get("status") != "confirmed"
                or _timestamp(review.get("reviewed_at"), "review.reviewed_at") < _timestamp(row["recorded_at"], "recorded_at")
                or _timestamp(review.get("reviewed_at"), "review.reviewed_at") > _assessment_now()):
            reasons.append("review_artifact_missing_or_mismatched")
    except FieldValidationError:
        reasons.append("artifact_timestamp_invalid")
    return {"classification": "reviewed_field_with_local_artifacts" if not reasons else "unverified_field_claim",
            "counts_as_real": not reasons, "reasons": reasons}


def _protocol(protocol: dict | None) -> dict | None:
    if protocol is None:
        return None
    if not isinstance(protocol, dict) or set(protocol) != PROTOCOL_FIELDS:
        raise FieldValidationError("protocol: 须预先给出 min_records/min_people/min_days/min_plots/rationale")
    for key in PROTOCOL_FIELDS - {"rationale"}:
        if isinstance(protocol[key], bool) or not isinstance(protocol[key], int) or protocol[key] <= 0:
            raise FieldValidationError(f"protocol.{key}: 须为预先选定的正整数，非临床效力阈值")
    _text(protocol["rationale"], "protocol.rationale")
    return deepcopy(protocol)


def _prediction(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) != PREDICTION_FIELDS:
        raise FieldValidationError("prediction: 字段不符合冻结预测契约")
    row = deepcopy(raw)
    for field in ("person_id", "plot_id"):
        row[field] = _id(row[field], field)
    row["prediction_id"] = _ref(row["prediction_id"], "prediction_id")
    _date_and_zone(row)
    if row["crop_id"] not in {c["id"] for c in get_community_catalog()["crops"]}:
        raise FieldValidationError("prediction.crop_id: 不在目录")
    row["quantity"] = _number(row["quantity"], "quantity")
    for field in ("tool", "posture", "field_condition"):
        row[field] = _text(row[field], field)
    for field in ("load_kg_per_trip", "distance_m"):
        # 保留非搬运的显式零，不将它悄悄改为未记录。
        row[field] = _number(row[field], field, zero=True, blank=True)
    context = {k: row[k] for k in ("method", "tool", "posture", "field_condition", "load_kg_per_trip", "distance_m")}
    checked = validate_task_quantity(row["task_code"], row["quantity"], row["unit"], context)
    if checked["missing_context"]:
        raise FieldValidationError("prediction: 必须先记录预定的工具、姿势、现场条件和适用的负重路线")
    row["scope"] = _choice(row["scope"], {"net_work", "elapsed"}, "scope")
    row["rate_basis"] = _choice(row["rate_basis"], {"user_range", "field_records"}, "rate_basis")
    for key in ("low_minutes", "point_minutes", "high_minutes"):
        row[key] = _number(row[key], key)
    if not row["low_minutes"] <= row["point_minutes"] <= row["high_minutes"]:
        raise FieldValidationError("prediction: 须满足 low <= point <= high，不替用户选择点预测")
    if not isinstance(row["basis_refs"], list) or not row["basis_refs"]:
        raise FieldValidationError("basis_refs: 必须预先保留用户估计凭据或训练记录编号")
    for ref in row["basis_refs"]:
        _ref(ref, "basis_refs")
    row["model_version"] = _ref(row["model_version"], "model_version")
    return row


def freeze_predictions(predictions: list[dict], training_records: list[dict],
                       protocol: dict | None, frozen_at: str | None = None, *,
                       evidence_manifest: dict | None = None,
                       evidence_root: Path | str | None = None) -> dict:
    """在结果采集前绑定预测、训练指纹、分组和评价标准；返回可存档快照。"""
    if not isinstance(predictions, list) or not predictions:
        raise FieldValidationError("predictions: 至少需要一条预测")
    preds = [_prediction(p) for p in predictions]
    if len({p["prediction_id"] for p in preds}) != len(preds):
        raise FieldValidationError("prediction_id: 不得重复")
    records = validate_records(training_records)
    if any(r["split"] != "training" for r in records):
        raise FieldValidationError("冻结阶段只能读取 training，不读取 holdout 结果")
    created = datetime.now(timezone.utc)
    declared = _timestamp(frozen_at, "frozen_at") if frozen_at is not None else created
    # 显式日期只能使冻结点更晚；调用者不能靠填旧日期绕过本机创建时间。
    effective = max(created, declared)
    classifications = {}
    for record in records:
        if _timestamp(record["recorded_at"], "recorded_at") > effective:
            raise FieldValidationError("training: 冻结时尚未采集的记录不可用于训练")
        if record["consent_status"] != "granted" or record["status"] not in PERFORMED:
            raise FieldValidationError("training: 必须有同意与实际完成量；中断/部分完成不得按速度剔除")
        classifications[record["record_id"]] = classify_evidence(record, evidence_manifest, evidence_root)
        if record["data_origin"] == "actual_field" and not classifications[record["record_id"]]["counts_as_real"]:
            raise FieldValidationError("training: 未核验的实测声明不能变成训练标签")
    _no_leakage(records, preds)
    training_index = {record["record_id"]: record for record in records}
    for pred in preds:
        target_day, target_zone = _date_and_zone(pred)
        if target_day < effective.astimezone(target_zone).date():
            raise FieldValidationError("prediction.local_date: 不得冻结已经过去的作业日作为未来留出")
        if pred["rate_basis"] == "field_records":
            if any(ref not in classifications or not classifications[ref]["counts_as_real"] for ref in pred["basis_refs"]):
                raise FieldValidationError("field_records: 预测依据必须绑定已核验的训练记录")
            for ref in pred["basis_refs"]:
                source = training_index[ref]
                if any(source[key] != pred[key] for key in ("crop_id", "task_code", "method", "unit", "tool", "posture", "field_condition")):
                    raise FieldValidationError("field_records: 不得把不同作物、任务、做法或条件的训练行当同口径速率")
    payload = {"schema_version": SCHEMA_VERSION, "created_at": created.isoformat(),
               "frozen_at": effective.isoformat(), "timestamp_assurance": "local_clock_not_external_attestation",
               "predictions": preds, "protocol": _protocol(protocol),
               "training_records": [{"record_id": r["record_id"], "sha256": _digest(r),
                                      **{k: r[k] for k in GROUP_FIELDS},
                                      "evidence_class": classifications[r["record_id"]]["classification"]} for r in records],
               "interpretation": "冻结与本地指纹证明内容一致性；现实采集真实性仍需可审查人工凭据"}
    return {**payload, "sha256": _digest(payload)}


def _verify_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or "sha256" not in snapshot:
        raise FieldValidationError("snapshot: 缺少冻结摘要")
    payload = {k: v for k, v in snapshot.items() if k != "sha256"}
    if snapshot["sha256"] != _digest(payload) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise FieldValidationError("SNAPSHOT_TAMPERED: 冻结预测内容与摘要不一致")
    if _timestamp(snapshot["frozen_at"], "frozen_at") < _timestamp(snapshot["created_at"], "created_at"):
        raise FieldValidationError("snapshot: 冻结时间不能早于快照创建")
    _protocol(snapshot["protocol"])
    preds = [_prediction(p) for p in snapshot["predictions"]]
    if len({p["prediction_id"] for p in preds}) != len(preds):
        raise FieldValidationError("snapshot: 重复预测编号")


def _counts(rows: list[dict]) -> dict:
    return {"records": len(rows), "people": len({r["person_id"] for r in rows}),
            "days": len({r["local_date"] for r in rows}), "plots": len({r["plot_id"] for r in rows}),
            "person_day_plot_groups": len({tuple(r[k] for k in GROUP_FIELDS) for r in rows})}


def _metrics(rows: list[dict], protocol: dict | None) -> dict:
    counts = _counts(rows)
    reasons = []
    if not rows:
        reasons.append("no_eligible_pairs")
    if protocol is None:
        reasons.append("sample_requirement_not_frozen")
    else:
        for count, key in (("records", "min_records"), ("people", "min_people"), ("days", "min_days"), ("plots", "min_plots")):
            if counts[count] < protocol[key]:
                reasons.append("insufficient_" + count)
    return {"counts": counts, "mae_minutes": None if reasons else sum(r["absolute_error_minutes"] for r in rows) / len(rows),
            "interval_coverage": None if reasons else sum(r["inside_range"] for r in rows) / len(rows),
            "null_reasons": reasons, "interval_kind": "empirical_coverage_of_frozen_input_range",
            "clinical_validity": None}


def evaluate_holdout(snapshot: dict, records: list[dict], evidence_manifest: dict | None = None,
                     evidence_root: Path | str | None = None) -> dict:
    """评估冻结的同口径完成预测；不足样本及非实测均不生成准确率。"""
    _verify_snapshot(snapshot)
    rows = validate_records(records)
    if any(r["split"] != "holdout" for r in rows):
        raise FieldValidationError("evaluate: 只能传入独立 holdout 记录")
    _no_leakage(snapshot["training_records"], rows)
    preds = {p["prediction_id"]: p for p in snapshot["predictions"]}
    # 每个现场行绑定一个预先选定的时间口径，不复制现场行来增加分母。
    matched = set()
    assessed, pairs, real_rows = [], [], []
    for row in rows:
        classification = classify_evidence(row, evidence_manifest, evidence_root)
        if classification["counts_as_real"]:
            real_rows.append(row)
        reasons = list(classification["reasons"])
        pred = preds.get(row["prediction_id"])
        if not pred:
            reasons.append("frozen_prediction_missing")
        else:
            if row["prediction_id"] in matched:
                raise FieldValidationError("同一冻结预测不得匹配多条结果以重复计数")
            matched.add(row["prediction_id"])
            if row["prediction_sha256"] != snapshot["sha256"]:
                raise FieldValidationError("结果引用的预测摘要不匹配")
            if _timestamp(row["start_at"], "start_at") <= _timestamp(snapshot["frozen_at"], "frozen_at"):
                raise FieldValidationError("PREDICTION_AFTER_OUTCOME: 必须先冻结再开始采集留出结果")
            for key in ("person_id", "plot_id", "local_date", "timezone", "crop_id", "task_code", "method", "unit",
                        "tool", "posture", "field_condition", "load_kg_per_trip", "distance_m"):
                if pred[key] != row[key]:
                    raise FieldValidationError(f"预测与实测 {key} 不同，不能进行同口径误差比较")
            if row["status"] != "completed":
                reasons.append("incomplete_or_unknown_outcome_retained")
            if row["quantity"] != pred["quantity"]:
                reasons.append("completed_quantity_differs_from_frozen_target")
        eligible = bool(pred) and not reasons and classification["counts_as_real"]
        observed = row["net_minutes"] if pred and pred["scope"] == "net_work" else row["elapsed_minutes"] if pred else None
        item = {"record_id": row["record_id"], **{k: row[k] for k in GROUP_FIELDS},
                "prediction_id": row["prediction_id"], "status": row["status"],
                "classification": classification["classification"], "counts_as_real": classification["counts_as_real"],
                "eligible": eligible, "reasons": reasons, "scope": pred["scope"] if pred else None,
                "observed_minutes": observed, "absolute_error_minutes": abs(pred["point_minutes"] - observed) if eligible else None,
                "inside_range": pred["low_minutes"] <= observed <= pred["high_minutes"] if eligible else None}
        assessed.append(item)
        if eligible:
            pairs.append({**item, "stratum": "|".join(pred[k] for k in ("crop_id", "task_code", "method", "unit", "scope"))})
    strata = sorted({r["stratum"] for r in pairs})
    grouped = {s: _metrics([r for r in pairs if r["stratum"] == s], snapshot["protocol"]) for s in strata}
    summary = _metrics(pairs, snapshot["protocol"])
    if len(strata) > 1:
        summary.update({"mae_minutes": None, "interval_coverage": None,
                        "null_reasons": summary["null_reasons"] + ["heterogeneous_strata_report_separately"]})
    return {"schema_version": SCHEMA_VERSION, "snapshot_sha256": snapshot["sha256"],
            "n_records": len(rows), "n_real": len(real_rows), "real_groups": _counts(real_rows),
            "evidence_classes": dict(Counter(r["classification"] for r in assessed)),
            "status_counts": dict(Counter(r["status"] for r in rows)),
            "slow_or_incomplete_count": sum(r["status"] in {"partial", "interrupted"} for r in rows),
            "unobserved_prediction_count": len(set(preds) - matched),
            "rows": assessed, "metrics": summary, "metrics_by_stratum": grouped,
            "claim": "实测计数须有本地同意/现场/独立复核凭据；指标仅描述已冻结的时间估计，不证明健康效果或全国适用",
            "clinical_effect": None, "personal_hourly_risk": None}


def _read_records(path: str) -> list[dict]:
    checked = validate_field_csv(Path(path))
    if not checked["valid"]:
        raise FieldValidationError(json.dumps(checked["errors"], ensure_ascii=False))
    return checked["records"]


def _json_file(path: str | None) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="农活实测校验、冻结预测和留出评估")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("csv")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--predictions", required=True)
    freeze.add_argument("--training", required=True)
    freeze.add_argument("--protocol")
    freeze.add_argument("--output", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--snapshot", required=True)
    evaluate.add_argument("--records", required=True)
    evaluate.add_argument("--output")
    for command in (freeze, evaluate):
        command.add_argument("--manifest")
        command.add_argument("--evidence-root")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_field_csv(Path(args.csv))
            # CLI不将全部记录及人员分组复制进终端；API调用方可读取records。
            result = {k: v for k, v in result.items() if k != "records"}
        elif args.command == "freeze":
            result = freeze_predictions(_json_file(args.predictions), _read_records(args.training), _json_file(args.protocol),
                                        evidence_manifest=_json_file(args.manifest), evidence_root=args.evidence_root)
        else:
            result = evaluate_holdout(_json_file(args.snapshot), _read_records(args.records),
                                      _json_file(args.manifest), args.evidence_root)
        output = getattr(args, "output", None)
        if output:
            # 只新建，不覆写已冻结的预测或已有评价产物。
            with Path(output).open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            print(json.dumps({"written": str(output), "sha256": result.get("sha256", result.get("snapshot_sha256"))}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result.get("valid", True) else 2
    except (InputError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
