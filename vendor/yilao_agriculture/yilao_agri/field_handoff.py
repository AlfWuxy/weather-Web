"""把尚未开工的网页估时交接为现在新建的正式冻结；不继承旧冻结效力。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .community_catalog import validate_task_quantity, get_community_catalog
from .field_collection_export import projection_sha256
from .field_validation import freeze_predictions, FieldValidationError

REVIEW_SCHEMA = "field-handoff-review-1"
HANDOFF_SCHEMA = "field-handoff-provenance-1"
MAX_BYTES = 4_000_000
INSTRUCTIONS = [
    "这是当前时刻新建的正式冻结，不继承或认证网页原冻结时间。",
    "投影哈希和 source_integrity_ok 只是一致性及来源声明，不是来源真实性认证。",
    "user_range 表示协调者此次明确采用已提供区间作为输入估计，不认证缺失的原速率来源，也不声称实测训练。",
    "reviewed 确认核对身份代号、原目标与点政策，并只填写非识别性工具、姿势、现场条件及依据编号。",
    "not_started_confirmed 必须在执行冻结时再次核对；较旧导出无法证明此后没有开始作业或出现结果。",
    "同意、实际观察、独立复核须另行收集；当前交接 n_real=0，不能跨身份命名空间自动合并。",
]
DRAFT_KEYS = {"schema_version", "mode", "identity_namespace", "claims", "counts", "records", "prediction_projections", "warnings", "projection_sha256"}
PROJECTION_KEYS = {"prediction_projection_id", "person_id", "task_id", "frozen_at_declared", "expected_start_at", "timezone",
    "target_quantity", "estimate", "point_policy", "historical_context_candidates", "source", "source_integrity_ok",
    "context_integrity_ok", "linked_record_ids", "reason_codes", "missing_fields"}
CONTEXT_KEYS = {"plot_id", "crop_id", "task_code", "method", "load_kg_per_trip", "distance_m"}
RECORD_KEYS = {"record_id", "person_id", "task_id", "reported", "historical_context_candidates", "context_integrity_ok",
    "context_matches_task_declared", "source", "source_prediction_id", "supersedes_record_id", "is_current", "review_fields", "missing_fields", "reason_codes"}
REVIEW_KEYS = {"schema_version", "identity_namespace", "projection_sha256", "selected_prediction_ids", "protocol", "prediction_reviews", "instructions"}
PER_REVIEW_KEYS = {"reviewed", "not_started_confirmed", "rate_basis", "basis_refs", "tool", "posture", "field_condition"}
ALLOWED_REASONS = {"PROJECTION_NOT_FORMAL_FROZEN_SNAPSHOT", "FORMAL_PROTOCOL_AND_BASIS_REFS_NOT_BOUND"}
PROTOCOL_KEYS = {"min_records", "min_people", "min_days", "min_plots", "rationale"}
REF = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,100}$")


class FieldHandoffError(ValueError):
    """仅输出固定错误说明，不回显来源内容或人员信息。"""


def _fail(code, message):
    raise FieldHandoffError(f"{code}：{message}")


def _canonical(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(result.encode("utf-8")) > MAX_BYTES:
            _fail("HANDOFF_TOO_LARGE", "资料超过交接大小上限")
        return result
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail("HANDOFF_JSON_INVALID", "资料须为有界且不含非有限数的 JSON")


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        _fail("HANDOFF_FIELDS_INVALID", "字段缺失或含本版本不接受的字段")


def _id(value, prefix):
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + r"-[A-F0-9]{32}", value) is None:
        _fail("HANDOFF_ID_INVALID", "须保留导出的稳定代号，不接受原身份或另行映射")
    return value


def _time(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError
        return moment.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        _fail("HANDOFF_TIME_INVALID", "时刻须明确时区且格式有效")


def _number(value, *, optional=False, zero=False):
    if optional and value is None:
        return None
    if type(value) not in (int, float) or value < 0 or (not zero and value == 0):
        _fail("HANDOFF_NUMBER_INVALID", "数量和分钟须为正数，已记录负重与距离不能为负数")
    # 所有输入先经过 allow_nan=False 的规范化；不把 bool 当作数字。
    return value


def _strings(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail("HANDOFF_FIELDS_INVALID", "原因与缺项须为字符串数组")


def _draft(draft):
    _canonical(draft)
    _object(draft, DRAFT_KEYS)
    if draft["schema_version"] != "field-collection-draft-1" or draft["mode"] != "real":
        _fail("HANDOFF_MODE_INVALID", "仅接受真实区的待复核导出，演示不能进入本交接")
    if not isinstance(draft["identity_namespace"], str) or re.fullmatch(r"[0-9a-f]{32}", draft["identity_namespace"]) is None:
        _fail("HANDOFF_NAMESPACE_INVALID", "缺少有效身份命名空间")
    if not isinstance(draft["projection_sha256"], str) or draft["projection_sha256"] != projection_sha256(draft):
        _fail("HANDOFF_PROJECTION_CHANGED", "导出内容与投影摘要不符")
    # 零实测声明只检查一致性，不作为跳过其他检查的豁免。
    if _canonical(draft["claims"]) != _canonical({"n_real": 0, "field_verified": False, "formal_validation_ready": False}):
        _fail("HANDOFF_CLAIMS_INVALID", "待复核包不得自称现场已核验")
    _strings(draft["warnings"])
    rows, records = draft["prediction_projections"], draft["records"]
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 1000
            or not isinstance(records, list) or len(records) > 5000):
        _fail("HANDOFF_FIELDS_INVALID", "预测或记录数组数量无效")
    indexed = {}
    for row in rows:
        _object(row, PROJECTION_KEYS)
        ident = _id(row["prediction_projection_id"], "PR")
        if ident in indexed:
            _fail("HANDOFF_SELECTION_INVALID", "预测代号重复")
        indexed[ident] = row
    ids = set()
    for row in records:
        _object(row, RECORD_KEYS)
        ident = _id(row["record_id"], "R")
        if ident in ids or row["is_current"] not in (True, False):
            _fail("HANDOFF_HISTORY_INVALID", "记录重复或更正链未确定")
        ids.add(ident)
        if row["source_prediction_id"] is not None:
            _id(row["source_prediction_id"], "PR")
    _object(draft["counts"], {"archived_record_versions", "current_record_versions", "prediction_projections"})
    counts = draft["counts"]
    expected = {"archived_record_versions": len(records), "current_record_versions": sum(r["is_current"] is True for r in records), "prediction_projections": len(rows)}
    if any(type(counts[k]) is not int or counts[k] != v for k, v in expected.items()):
        _fail("HANDOFF_HISTORY_INVALID", "记录和预测计数与投影不一致")
    return indexed


def _selected(draft, ids, now):
    indexed = _draft(draft)
    if (not isinstance(ids, list) or not ids or len(ids) > 1000 or any(not isinstance(v, str) for v in ids)
            or len(ids) != len(set(ids)) or not set(ids) <= set(indexed)):
        _fail("HANDOFF_SELECTION_INVALID", "须显式选择包内互不重复的预测代号")
    chosen = []
    for ident in ids:
        row = indexed[ident]
        _object(row["source"], {"declared_mode", "declared_trust_status", "imported", "assurance"})
        if (row["source"] != {"declared_mode": "real", "declared_trust_status": "local_frozen", "imported": False, "assurance": "unverified_declaration"}
                or type(row["source"]["imported"]) is not bool or row["source_integrity_ok"] is not True or row["context_integrity_ok"] is not True):
            _fail("HANDOFF_SOURCE_INELIGIBLE", "导入、未核对或来源不兼容的预测不能交接")
        _strings(row["reason_codes"])
        _strings(row["missing_fields"])
        if set(row["reason_codes"]) - ALLOWED_REASONS:
            _fail("HANDOFF_SOURCE_INELIGIBLE", "预测投影仍有不兼容原因")
        if not isinstance(row["linked_record_ids"], list) or row["linked_record_ids"]:
            _fail("HANDOFF_OUTCOME_EXISTS", "已有结果关联，不得重新冻结为未来留出")
        # 只查关联，不读取结果数值作拟合；旧更正版本也算曾经见过结果。
        if any(record["source_prediction_id"] == ident for record in draft["records"]):
            _fail("HANDOFF_OUTCOME_EXISTS", "已有结果关联，不得重新冻结为未来留出")
        frozen, expected = _time(row["frozen_at_declared"]), _time(row["expected_start_at"])
        if not frozen <= now < expected or frozen >= expected:
            _fail("HANDOFF_NOT_PROSPECTIVE", "原声明时间不能在未来，预计作业必须尚未开始")
        _id(row["person_id"], "P")
        _id(row["task_id"], "T")
        context = row["historical_context_candidates"]
        _object(context, CONTEXT_KEYS)
        _id(context["plot_id"], "PL")
        if any(not isinstance(context[k], str) or not context[k] for k in ("crop_id", "task_code", "method")):
            _fail("HANDOFF_CONTEXT_MISSING", "原作物、任务或做法缺失，不能用当前资料补写")
        if context["crop_id"] not in {crop["id"] for crop in get_community_catalog()["crops"]}:
            _fail("HANDOFF_CONTEXT_MISSING", "作物尚未对应明确目录编号")
        for key in ("load_kg_per_trip", "distance_m"):
            _number(context[key], optional=True, zero=True)
        _object(row["target_quantity"], {"value", "unit"})
        _number(row["target_quantity"]["value"])
        estimate = row["estimate"]
        _object(estimate, {"low_minutes", "point_minutes", "high_minutes", "scope"})
        if (estimate["scope"] != "net_work" or not isinstance(row["point_policy"], str)
                or row["point_policy"] not in {"midpoint", "slow_bound"}):
            _fail("HANDOFF_POINT_MISSING", "须已有明确净劳动点政策；不能事后选择点值")
        low, point, high = [_number(estimate[k]) for k in ("low_minutes", "point_minutes", "high_minutes")]
        expected_point = high if row["point_policy"] == "slow_bound" else (low + high) / 2
        if not low <= point <= high or point != expected_point:
            _fail("HANDOFF_POINT_MISSING", "点值与原点政策或区间不一致")
        allowed_missing = {"historical_context_candidates.load_kg_per_trip", "historical_context_candidates.distance_m"}
        if set(row["missing_fields"]) - allowed_missing:
            _fail("HANDOFF_CONTEXT_MISSING", "预测仍缺少必要原始内容")
        try:
            ZoneInfo(row["timezone"])
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            _fail("HANDOFF_TIME_INVALID", "预测时区无效")
        chosen.append(row)
    return chosen


def make_review_template(draft: dict, selected_prediction_ids: list[str]) -> dict:
    """只生成空白复核表，不执行正式冻结，不读取结果作拟合。"""
    _selected(draft, selected_prediction_ids, datetime.now(timezone.utc))
    return {"schema_version": REVIEW_SCHEMA, "identity_namespace": draft["identity_namespace"],
        "projection_sha256": draft["projection_sha256"], "selected_prediction_ids": list(selected_prediction_ids),
        "protocol": {"min_records": None, "min_people": None, "min_days": None, "min_plots": None, "rationale": ""},
        "prediction_reviews": {ident: {"reviewed": False, "not_started_confirmed": False, "rate_basis": "",
            "basis_refs": [], "tool": "", "posture": "", "field_condition": ""} for ident in selected_prediction_ids},
        "instructions": list(INSTRUCTIONS)}


def _review_text(value, *, context=False):
    if not isinstance(value, str) or not value.strip() or len(value) > 300 or any(ord(c) < 32 for c in value):
        _fail("HANDOFF_REVIEW_INCOMPLETE", "须明确填写非识别性复核内容及协议理由")
    if context and (re.search(r"https?://|@|\d{7,}", value, re.IGNORECASE) or "/" in value or "\\" in value):
        _fail("HANDOFF_REVIEW_INCOMPLETE", "工具、姿势和现场条件不得填写联系信息、网址或文件路径")
    return value.strip()


def freeze_field_handoff(draft: dict, review: dict) -> dict:
    """正式冻结发生在这次调用，不接受外传时钟、旧时间或已有结果。"""
    _object(draft, DRAFT_KEYS)
    _canonical(review)
    _object(review, REVIEW_KEYS)
    if (review["schema_version"] != REVIEW_SCHEMA or review["identity_namespace"] != draft.get("identity_namespace")
            or review["projection_sha256"] != draft.get("projection_sha256") or review["instructions"] != INSTRUCTIONS):
        _fail("HANDOFF_REVIEW_BINDING", "复核表与导出命名空间、摘要或交接说明不一致")
    began = datetime.now(timezone.utc)
    chosen = _selected(draft, review["selected_prediction_ids"], began)
    details = review["prediction_reviews"]
    if not isinstance(details, dict) or set(details) != set(review["selected_prediction_ids"]):
        _fail("HANDOFF_SELECTION_INVALID", "复核条目必须与选择的预测代号集合完全一致")
    protocol = review["protocol"]
    _object(protocol, PROTOCOL_KEYS)
    if any(type(protocol[k]) is not int or protocol[k] <= 0 for k in PROTOCOL_KEYS - {"rationale"}):
        _fail("HANDOFF_PROTOCOL_INCOMPLETE", "样本要求须在结果出现前明确为正整数，不预填临床有效门槛")
    _review_text(protocol["rationale"])
    predictions, provenance = [], []
    for row in chosen:
        ident = row["prediction_projection_id"]
        checked = details[ident]
        _object(checked, PER_REVIEW_KEYS)
        if checked["reviewed"] is not True or checked["not_started_confirmed"] is not True or checked["rate_basis"] != "user_range":
            _fail("HANDOFF_REVIEW_INCOMPLETE", "须明确复核、确认尚未开工，并采用 user_range")
        refs = checked["basis_refs"]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 50 or any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in refs)
                or len(refs) != len(set(refs))):
            _fail("HANDOFF_BASIS_MISSING", "须明确提供互不重复的非识别性估计依据编号")
        context = row["historical_context_candidates"]
        prediction = {k: deepcopy(context[k]) for k in CONTEXT_KEYS}
        prediction.update(prediction_id=ident, person_id=row["person_id"], timezone=row["timezone"],
            local_date=_time(row["expected_start_at"]).astimezone(ZoneInfo(row["timezone"])).date().isoformat(),
            quantity=row["target_quantity"]["value"], unit=row["target_quantity"]["unit"],
            **deepcopy(row["estimate"]), rate_basis="user_range", basis_refs=list(refs), model_version="community-handoff-v1")
        prediction.update({key: _review_text(checked[key], context=True) for key in ("tool", "posture", "field_condition")})
        try:
            quantity = validate_task_quantity(prediction["task_code"], prediction["quantity"], prediction["unit"], prediction)
        except (ValueError, TypeError):
            _fail("HANDOFF_CONTEXT_MISSING", "任务做法、单位或条件不兼容")
        if quantity["missing_context"]:
            _fail("HANDOFF_CONTEXT_MISSING", "任务所需负重或路线等原始条件缺失")
        predictions.append(prediction)
        provenance.append({"prediction_id": ident, "source_prediction_projection_id": ident,
            "source_task_id": row["task_id"], "person_id": row["person_id"], "plot_id": context["plot_id"],
            "source_frozen_at_declared": row["frozen_at_declared"], "expected_start_at": row["expected_start_at"],
            "source_point_policy": row["point_policy"], "source_assurance": "unverified_declaration",
            "source_integrity_ok_declared": True, "context_integrity_ok_declared": True})
    try:
        snapshot = freeze_predictions(predictions, [], deepcopy(protocol))
    except (FieldValidationError, ValueError, TypeError):
        _fail("HANDOFF_FREEZE_REJECTED", "正式预测契约未通过；未生成可用交接快照")
    finished = datetime.now(timezone.utc)
    created, frozen = _time(snapshot["created_at"]), _time(snapshot["frozen_at"])
    if not began <= created <= frozen <= finished or any(finished >= _time(row["expected_start_at"]) for row in chosen):
        _fail("HANDOFF_NOT_PROSPECTIVE", "冻结完成前已到预计开工时刻或本机时钟回退，请勿回填")
    snapshot["handoff_provenance"] = {"schema_version": HANDOFF_SCHEMA,
        "identity_namespace": draft["identity_namespace"], "projection_sha256": draft["projection_sha256"],
        "review_sha256": sha256(_canonical(review).encode()).hexdigest(), "source_mode": "real",
        "handoff_completed_at": finished.isoformat(), "source_predictions": provenance,
        "meaning": "本次新正式冻结，不继承或认证网页旧冻结效力；仅采用协调者明确确认的输入区间。",
        "namespace_rule": "仅单一命名空间；不同账户、密钥或代号版本不能自动合并去重。"}
    snapshot["claims"] = {"n_real": 0, "field_verified": False, "source_authenticated": False,
                          "original_freeze_time_authenticated": False}
    snapshot["sha256"] = sha256(_canonical({k: v for k, v in snapshot.items() if k != "sha256"}).encode()).hexdigest()
    return snapshot


def _read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("HANDOFF_JSON_INVALID", "JSON 含重复字段")
            result[key] = value
        return result
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            _fail("HANDOFF_TOO_LARGE", "资料超过交接大小上限")
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: _fail("HANDOFF_JSON_INVALID", "JSON 含非有限数"))
        _canonical(result)
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail("HANDOFF_JSON_INVALID", "无法读取有界 UTF-8 JSON")


def main(argv=None):
    parser = argparse.ArgumentParser(description="网页预测的事前正式交接；不继承旧冻结时刻")
    sub = parser.add_subparsers(dest="command", required=True)
    template = sub.add_parser("template", help="为显式选择的预测生成空白复核表")
    template.add_argument("--prediction-id", action="append", required=True)
    freeze = sub.add_parser("freeze", help="复核且尚未开工时创建新的正式冻结")
    freeze.add_argument("--review", required=True)
    for command in (template, freeze):
        command.add_argument("--draft", required=True)
        command.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        draft = _read_json(args.draft)
        result = (make_review_template(draft, args.prediction_id) if args.command == "template"
                  else freeze_field_handoff(draft, _read_json(args.review)))
        if args.command == "freeze" and any(datetime.now(timezone.utc) >= _time(row["expected_start_at"])
                for row in result["handoff_provenance"]["source_predictions"]):
            _fail("HANDOFF_NOT_PROSPECTIVE", "写出前已到预计开工时刻，未输出冻结文件")
        # 与正式验证工具保持新建语义；不覆盖旧冻结或复核表。
        with Path(args.output).open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"status": "written", "schema_version": result["schema_version"], "sha256": result.get("sha256"), "n_real": 0}, ensure_ascii=False))
        return 0
    except FieldHandoffError as error:
        print(json.dumps({"error": str(error), "n_real": 0}, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"error": "HANDOFF_IO_OR_INPUT_INVALID：输入或文件操作失败，未覆盖已有文件", "n_real": 0}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
