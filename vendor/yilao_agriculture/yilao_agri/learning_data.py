"""学习数据集的构建与版本管理：只纳入已核实同口径观测，人时地区分组，原始记录可追溯。

本模块只做“纳入判定 / 分组 / 版本 / 追溯”，不下人体安全结论，不自动收紧保守范围，
不产生概率区间，也不把测试当现场效果。证据标签：实现检查（IMPLEMENTATION_TEST）。

- 纳入判定（include）：逐条给稳定原因码。只有同时满足
  「同口径」（同任务、同人、同方法、net_work 口径、单位兼容、落在目标时间范围内）
  与「已核实」（来源非占位、``review_status=confirmed``）的观测才进入训练表。
- 人时地区分组（group）：训练行按 ``(worker_id, region, period)`` 分组；默认不跨人合并，
  对应 U19 的个体速率学习。
- 版本管理（version）：数据集带单调递增 ``revision`` 与内容哈希；``LearningDatasetStore``
  只追加，同一 revision 不得改写。
- 追溯（trace）：数据集冻结原始记录快照；每条原始记录带内容哈希；每行训练行引用
  ``record_id`` 与 ``record_sha256``；``verify_dataset`` 重算哈希并从原始快照重建训练表。

硬约束：

1. 只纳入已核实同口径观测。未纳入记录保留在纳入清单并附原因码，不静默丢弃。
2. 慢端与中断不得忽略（U19）：``partial`` / ``interrupted`` 记录只要通过口径与核实检查
   就必须纳入并计入 ``slow_tail`` 台账；任何“按速率剔除/剔除慢端”的入口直接被拒绝。
3. 追溯：训练行只能引用已纳入记录；原始快照与逐条哈希必须自洽，事后改动可被检出。
4. 不自动收紧既有保守速率，不输出“已校准概率区间”或个体健康结论。
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from hashlib import sha256
from typing import Any

from .models import InputError, is_placeholder_source, parse_time, to_utc
from .workload import canonical_quantity

LEARNING_DATASET_SCHEMA_VERSION = "1.0"

# 记录允许字段；出现未知字段即拒绝（沿用本项目“不静默忽略”的口径）。
_RECORD_FIELDS = frozenset({
    "record_id", "task_id", "worker_id", "method", "scope", "status", "region",
    "period", "observed_at", "quantity", "minutes", "source", "review_status", "note",
})
_TARGET_FIELDS = frozenset({"task_id", "worker_id", "method", "unit", "scope", "window"})

# 只有这些状态可进入训练表；中断与慢记录不是剔除理由。
INCLUDED_STATUSES = frozenset({"completed", "partial", "interrupted"})
# 慢端：中断与部分完成记录必须保留，只用于描述性慢端。
SLOW_TAIL_STATUSES = frozenset({"partial", "interrupted"})
NET_SCOPE = "net_work"
CONFIRMED = "confirmed"

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

__all__ = [
    "LEARNING_DATASET_SCHEMA_VERSION",
    "INCLUDED_STATUSES",
    "SLOW_TAIL_STATUSES",
    "LearningDataError",
    "LearningDatasetStore",
    "build_learning_dataset",
    "check_inclusion",
    "dataset_sha256",
    "merge_rows_across_people",
    "record_sha256",
    "summarize",
    "trace_record",
    "verify_dataset",
]


class LearningDataError(ValueError):
    """学习数据集错误；``code`` 为稳定的机器可读原因。"""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        text = f"{code}: {message}" if path is None else f"{path}: {code} {message}"
        super().__init__(text)


# --------------------------------------------------------------------------- #
# 规范化与哈希
# --------------------------------------------------------------------------- #
def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise LearningDataError(
            "DATASET_PAYLOAD_NOT_JSON", "内容必须是可规范化的 JSON（不含 NaN/Infinity）"
        ) from exc


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def record_sha256(record: dict) -> str:
    """单条原始记录的内容哈希；用于训练表→原始记录的追溯。"""
    return _digest(record)


def dataset_sha256(dataset: dict) -> str:
    """数据集内容哈希（不含 ``dataset_sha256`` 自身）。"""
    if not isinstance(dataset, dict):
        raise LearningDataError("DATASET_INVALID", "dataset 必须为对象")
    return _digest({key: value for key, value in dataset.items() if key != "dataset_sha256"})


def _text(value: Any, path: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningDataError(code, "必须为非空字符串", path=path)
    return value


def _instant(value: Any, path: str):
    try:
        return parse_time(value)
    except InputError as exc:
        raise LearningDataError("TIME_INVALID", str(exc), path=path) from exc


def _normalize_target(target: Any) -> dict:
    if not isinstance(target, dict):
        raise LearningDataError("TARGET_INVALID", "target 必须为对象")
    if "canonical_unit" in target:
        # 已是归一化目标（含内部字段 canonical_unit）：按归一化目标复核后原样返回，
        # 供 verify_dataset 从原始快照重建时使用。
        for key in ("task_id", "worker_id", "method", "unit", "canonical_unit"):
            _text(target.get(key), f"target.{key}", "TARGET_FIELD_INVALID")
        if target.get("scope", NET_SCOPE) != NET_SCOPE:
            raise LearningDataError(
                "TARGET_SCOPE_UNSUPPORTED", "学习数据集只接受 net_work 口径的目标",
                path="target.scope",
            )
        window_norm = target.get("window")
        normalized_window = None
        if window_norm is not None:
            if not isinstance(window_norm, dict) or set(window_norm) - {"start", "end"}:
                raise LearningDataError(
                    "TARGET_WINDOW_INVALID", "window 须为 {start,end}", path="target.window"
                )
            start = _instant(window_norm.get("start"), "target.window.start")
            end = _instant(window_norm.get("end"), "target.window.end")
            if end <= start:
                raise LearningDataError(
                    "TARGET_WINDOW_INVALID", "window.end 必须晚于 window.start", path="target.window"
                )
            normalized_window = {"start": start.isoformat(), "end": end.isoformat()}
        return {
            "task_id": target["task_id"],
            "worker_id": target["worker_id"],
            "method": target["method"],
            "scope": NET_SCOPE,
            "unit": target["unit"],
            "canonical_unit": target["canonical_unit"],
            "window": normalized_window,
        }
    for key in target:
        if key not in _TARGET_FIELDS:
            raise LearningDataError("TARGET_FIELD_UNKNOWN", f"未知字段 {key}", path="target")
    for key in ("task_id", "worker_id", "method", "unit"):
        _text(target.get(key), f"target.{key}", "TARGET_FIELD_INVALID")
    scope = target.get("scope", NET_SCOPE)
    if scope != NET_SCOPE:
        raise LearningDataError(
            "TARGET_SCOPE_UNSUPPORTED", "学习数据集只接受 net_work 口径的目标", path="target.scope"
        )
    try:
        _, canonical_unit = canonical_quantity({"value": 1, "unit": target["unit"]})
    except InputError as exc:
        raise LearningDataError("TARGET_UNIT_UNKNOWN", str(exc), path="target.unit") from exc
    window = target.get("window")
    normalized_window = None
    if window is not None:
        if not isinstance(window, dict) or set(window) - {"start", "end"}:
            raise LearningDataError(
                "TARGET_WINDOW_INVALID", "window 须为 {start,end}", path="target.window"
            )
        start = _instant(window.get("start"), "target.window.start")
        end = _instant(window.get("end"), "target.window.end")
        if end <= start:
            raise LearningDataError(
                "TARGET_WINDOW_INVALID", "window.end 必须晚于 window.start", path="target.window"
            )
        normalized_window = {"start": start.isoformat(), "end": end.isoformat()}
    return {
        "task_id": target["task_id"],
        "worker_id": target["worker_id"],
        "method": target["method"],
        "scope": NET_SCOPE,
        "unit": target["unit"],
        "canonical_unit": canonical_unit,
        "window": normalized_window,
    }


# --------------------------------------------------------------------------- #
# 纳入判定
# --------------------------------------------------------------------------- #
def check_inclusion(record: dict, target: dict) -> dict:
    """对单条记录判定“是否纳入训练表”，并给出稳定原因码。

    ``target`` 可传已归一化目标（含 ``canonical_unit``）或原始目标对象。
    返回的清单行保留原因码与内容哈希；未纳入记录不会被丢弃。
    """
    normalized = target if isinstance(target, dict) and "canonical_unit" in target else _normalize_target(target)
    if not isinstance(record, dict):
        raise LearningDataError("RECORD_INVALID", "记录必须为对象")
    for key in record:
        if key not in _RECORD_FIELDS:
            raise LearningDataError("RECORD_FIELD_UNKNOWN", f"未知字段 {key}", path="record")
    record_id = _text(record.get("record_id"), "record.record_id", "RECORD_ID_UNKNOWN")

    reasons: list[str] = []

    # —— 同口径 ——
    if record.get("task_id") != normalized["task_id"]:
        reasons.append("DIFFERENT_TASK")
    if record.get("worker_id") != normalized["worker_id"]:
        reasons.append("DIFFERENT_WORKER")
    if record.get("method") != normalized["method"]:
        reasons.append("DIFFERENT_METHOD")
    if record.get("scope", NET_SCOPE) != NET_SCOPE:
        reasons.append("NOT_NET_WORK")

    quantity_value = None
    canonical_unit = None
    try:
        quantity_value, canonical_unit = canonical_quantity(record.get("quantity"))
    except InputError:
        reasons.append("QUANTITY_UNKNOWN")
    if canonical_unit is not None and canonical_unit != normalized["canonical_unit"]:
        reasons.append("UNIT_INCOMPATIBLE")

    observed = None
    try:
        observed = _instant(record.get("observed_at"), "record.observed_at")
    except LearningDataError:
        reasons.append("OBSERVED_AT_UNKNOWN")

    window = normalized.get("window")
    if window is not None:
        if observed is None:
            # 时刻未知就不能声称落在目标时间范围内。
            reasons.append("OUTSIDE_TARGET_WINDOW")
        else:
            start, end = parse_time(window["start"]), parse_time(window["end"])
            if not (to_utc(start) <= to_utc(observed) < to_utc(end)):
                reasons.append("OUTSIDE_TARGET_WINDOW")

    # —— 已核实 ——
    source = record.get("source")
    if not isinstance(source, str) or not source.strip() or is_placeholder_source(source):
        reasons.append("SOURCE_NOT_VERIFIABLE")
    if record.get("review_status") != CONFIRMED:
        reasons.append("NOT_VERIFIED")

    status = record.get("status")
    if status not in INCLUDED_STATUSES:
        reasons.append("STATUS_NOT_RATE_ELIGIBLE")

    minutes = record.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or float(minutes) <= 0:
        reasons.append("NO_MINUTES")

    if quantity_value is None or quantity_value <= 0:
        reasons.append("NO_COMPLETED_QUANTITY")

    region = record.get("region")
    if not isinstance(region, str) or not region.strip() or is_placeholder_source(region):
        reasons.append("REGION_UNKNOWN")

    period = record.get("period")
    if not isinstance(period, str) or not _PERIOD_RE.match(period):
        reasons.append("PERIOD_UNKNOWN")

    reasons = sorted(set(reasons))
    included = not reasons
    return {
        "record_id": record_id,
        "record_sha256": record_sha256(record),
        "included": included,
        "reason_codes": reasons,
        "status": status,
        "review_status": record.get("review_status"),
        "source": source if isinstance(source, str) else None,
        "slow_tail": bool(included and status in SLOW_TAIL_STATUSES),
        "unit": canonical_unit,
        "observed_rate_minutes_per_unit": (
            float(minutes) / quantity_value if included and quantity_value else None
        ),
        "group_key": (
            {"worker_id": record["worker_id"], "region": region, "period": period} if included else None
        ),
    }


# --------------------------------------------------------------------------- #
# 训练表与分组
# --------------------------------------------------------------------------- #
def _training_rows(included: list[dict], target: dict) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for row in included:
        group = row["group_key"]
        key = (group["worker_id"], group["region"], group["period"])
        buckets.setdefault(key, []).append(row)
    rows: list[dict] = []
    for key in sorted(buckets):
        members = sorted(buckets[key], key=lambda item: item["record_id"])
        rates = [(m["record_id"], m["observed_rate_minutes_per_unit"]) for m in members]
        slow_id, slow_rate = max(rates, key=lambda pair: (pair[1], pair[0]))
        fast_id, fast_rate = min(rates, key=lambda pair: (pair[1], pair[0]))
        slow_tail = sorted(m["record_id"] for m in members if m["slow_tail"])
        rows.append({
            "group_key": {"worker_id": key[0], "region": key[1], "period": key[2]},
            "worker_id": key[0],
            "region": key[1],
            "period": key[2],
            "task_id": target["task_id"],
            "method": target["method"],
            "unit": members[0]["unit"],
            "count": len(members),
            "record_ids": [m["record_id"] for m in members],
            "record_sha256s": {m["record_id"]: m["record_sha256"] for m in members},
            "rate_fast_minutes_per_unit": fast_rate,
            "rate_slow_minutes_per_unit": slow_rate,
            "fast_end_record_id": fast_id,
            "slow_end_record_id": slow_id,
            "slow_tail_count": len(slow_tail),
            "slow_tail_record_ids": slow_tail,
            "interval_kind": "descriptive_observed_range_not_probability_interval",
            "pooled_across_workers": False,
        })
    return rows


def _group_ledger(included: list[dict]) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for row in included:
        group = row["group_key"]
        key = (group["worker_id"], group["region"], group["period"])
        buckets.setdefault(key, []).append(row)
    ledger: list[dict] = []
    for key in sorted(buckets):
        members = sorted(buckets[key], key=lambda item: item["record_id"])
        ledger.append({
            "group_key": {"worker_id": key[0], "region": key[1], "period": key[2]},
            "worker_id": key[0],
            "region": key[1],
            "period": key[2],
            "count": len(members),
            "record_ids": [m["record_id"] for m in members],
            "slow_tail_count": sum(1 for m in members if m["slow_tail"]),
        })
    return ledger


def build_learning_dataset(
    records: list[dict],
    *,
    target: dict,
    revision: int,
    generated_at: Any,
    version_label: str | None = None,
    exclude_slow_tail: bool = False,
) -> dict:
    """从原始观测记录构建一个学习数据集版本（不写入任何存储）。

    ``exclude_slow_tail=True`` 会被直接拒绝：U19 要求不清除慢端与中断记录。
    返回结构含原始快照、纳入清单、人时地区分组、训练行与内容哈希。
    """
    if exclude_slow_tail:
        raise LearningDataError(
            "SLOW_TAIL_EXCLUSION_FORBIDDEN",
            "慢端与中断记录不得剔除；个体速率学习必须保留 slow_tail（U19）",
        )
    normalized_target = _normalize_target(target)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise LearningDataError("REVISION_INVALID", "revision 必须为 >=1 的整数", path="revision")
    generated = _instant(generated_at, "generated_at")
    if version_label is not None:
        _text(version_label, "version_label", "VERSION_LABEL_INVALID")
    if not isinstance(records, list):
        raise LearningDataError("RECORDS_INVALID", "records 必须为数组")

    snapshot = deepcopy(records)
    seen: dict[str, int] = {}
    for index, record in enumerate(snapshot):
        if not isinstance(record, dict):
            raise LearningDataError("RECORD_INVALID", "记录必须为对象", path=f"records[{index}]")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise LearningDataError(
                "RECORD_ID_UNKNOWN", "record_id 必须为非空字符串", path=f"records[{index}]"
            )
        if record_id in seen:
            raise LearningDataError(
                "RECORD_ID_DUPLICATE", f"record_id 重复：{record_id}", path=f"records[{index}]"
            )
        seen[record_id] = index
    snapshot.sort(key=lambda item: item["record_id"])

    manifest = [check_inclusion(record, normalized_target) for record in snapshot]
    included = [row for row in manifest if row["included"]]
    groups = _group_ledger(included)
    training_rows = _training_rows(included, normalized_target)
    counters = {
        "records_total": len(manifest),
        "included": len(included),
        "excluded": len(manifest) - len(included),
        "slow_tail_included": sum(1 for row in included if row["slow_tail"]),
        "groups": len(groups),
    }
    dataset: dict[str, Any] = {
        "learning_dataset_schema_version": LEARNING_DATASET_SCHEMA_VERSION,
        "revision": revision,
        "version_label": version_label,
        "generated_at": generated.isoformat(),
        "target": normalized_target,
        "raw_records": snapshot,
        "manifest": manifest,
        "groups": groups,
        "training_rows": training_rows,
        "counters": counters,
        "interval_kind": "descriptive_observed_range_not_probability_interval",
        "notes": [
            "只纳入已核实同口径观测：同任务、同人、同方法、net_work 口径、单位兼容、"
            "落在目标时间范围内，且来源非占位、review_status=confirmed。",
            "未纳入记录保留在纳入清单中并附原因码，不做静默丢弃。",
            "慢端与中断（partial/interrupted）记录只要通过口径与核实检查就必须纳入，"
            "并计入 slow_tail 台账。",
            "训练行按 (worker_id, region, period) 分组，默认不跨人合并，用于个体速率学习。",
            "观测范围只是描述性区间，不是概率区间，也不自动替换或收紧既有保守速率。",
        ],
    }
    dataset["dataset_sha256"] = dataset_sha256(dataset)
    return dataset


# --------------------------------------------------------------------------- #
# 追溯与校验
# --------------------------------------------------------------------------- #
def trace_record(dataset: dict, record_id: str) -> dict:
    """返回某条原始记录在纳入清单与训练行中的落点（只读，供审查）。"""
    raw = {item["record_id"]: item for item in dataset.get("raw_records") or []}
    record = raw.get(record_id)
    if record is None:
        raise LearningDataError("RECORD_UNKNOWN", f"未知 record_id：{record_id}")
    manifest_row = next(
        (row for row in dataset.get("manifest") or [] if row.get("record_id") == record_id), None
    )
    training_row = None
    for row in dataset.get("training_rows") or []:
        if record_id in (row.get("record_ids") or []):
            training_row = row
            break
    return {
        "record_id": record_id,
        "record": deepcopy(record),
        "record_sha256": record_sha256(record),
        "manifest_row": deepcopy(manifest_row),
        "training_row": deepcopy(training_row),
    }


def verify_dataset(dataset: dict) -> dict:
    """重算哈希并从原始快照重建训练表；检出任何事后改动或追溯断链。"""
    if not isinstance(dataset, dict):
        return {"valid": False, "errors": ["dataset 必须为对象"], "revision": None,
                "dataset_sha256": None}
    errors: list[str] = []
    recorded = dataset.get("dataset_sha256")
    if not isinstance(recorded, str):
        errors.append("dataset_sha256 缺失")
    else:
        try:
            expected = dataset_sha256(dataset)
        except LearningDataError as exc:
            expected = None
            errors.append(exc.message)
        if expected is not None and recorded != expected:
            errors.append("dataset_sha256 与当前内容不一致：数据集已被改动")

    raw = dataset.get("raw_records")
    target = dataset.get("target")
    if not isinstance(raw, list):
        errors.append("raw_records 必须为数组")
    if not isinstance(target, dict):
        errors.append("target 必须为对象")
    if errors:
        return {"valid": False, "errors": errors, "revision": dataset.get("revision"),
                "dataset_sha256": recorded}

    try:
        rebuilt = build_learning_dataset(
            raw,
            target=target,
            revision=dataset.get("revision"),
            generated_at=dataset.get("generated_at"),
            version_label=dataset.get("version_label"),
        )
    except (LearningDataError, InputError) as exc:
        return {"valid": False, "errors": [f"无法从原始记录重建：{exc}"],
                "revision": dataset.get("revision"), "dataset_sha256": recorded}

    for field in ("manifest", "training_rows", "groups", "counters"):
        if dataset.get(field) != rebuilt.get(field):
            errors.append(f"{field} 与由原始记录重建的结果不一致")

    raw_by_id = {item["record_id"]: item for item in raw}
    manifest_rows = dataset.get("manifest") or []
    manifest_ids = sorted(row.get("record_id") for row in manifest_rows)
    if manifest_ids != sorted(raw_by_id):
        errors.append("纳入清单未与原始记录一一对应")
    for row in manifest_rows:
        record = raw_by_id.get(row.get("record_id"))
        if record is None:
            errors.append(f"纳入清单引用了不存在的原始记录 {row.get('record_id')}")
        elif row.get("record_sha256") != record_sha256(record):
            errors.append(f"{row.get('record_id')} 的 record_sha256 与原始记录不一致")

    included_ids = {row["record_id"] for row in manifest_rows if row.get("included")}
    for row in dataset.get("training_rows") or []:
        for record_id in row.get("record_ids") or []:
            if record_id not in included_ids:
                errors.append(f"训练行引用了未纳入的记录 {record_id}")
        for record_id in row.get("slow_tail_record_ids") or []:
            if record_id not in included_ids:
                errors.append(f"slow_tail 引用了未纳入的记录 {record_id}")

    slow_included = {
        row["record_id"] for row in manifest_rows if row.get("included") and row.get("slow_tail")
    }
    referenced: set[str] = set()
    for row in dataset.get("training_rows") or []:
        referenced.update(row.get("slow_tail_record_ids") or [])
    missing = slow_included - referenced
    if missing:
        errors.append(f"慢端记录未进入训练表：{sorted(missing)}")

    return {"valid": not errors, "errors": errors, "revision": dataset.get("revision"),
            "dataset_sha256": recorded}


# --------------------------------------------------------------------------- #
# 跨人池化（描述性，不替换个体行）
# --------------------------------------------------------------------------- #
def merge_rows_across_people(rows: list[dict], *, note: str | None = None) -> list[dict]:
    """把同 (task,method,unit,region,period) 的多个个体训练行汇成描述性池化行。

    仅用于说明；池化行显式标注 ``pooled_across_workers=True`` 与参与劳动者，
    不产生个体安全结论，也不回写训练表。
    """
    if not isinstance(rows, list):
        raise LearningDataError("ROWS_INVALID", "rows 必须为数组")
    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LearningDataError("ROWS_INVALID", "训练行必须为对象")
        key = (row.get("task_id"), row.get("method"), row.get("unit"), row.get("region"),
               row.get("period"))
        buckets.setdefault(key, []).append(row)
    pooled: list[dict] = []
    for key in sorted(buckets, key=lambda item: tuple("" if part is None else str(part) for part in item)):
        members = buckets[key]
        rates = [(m["slow_end_record_id"], m["rate_slow_minutes_per_unit"]) for m in members]
        fasts = [(m["fast_end_record_id"], m["rate_fast_minutes_per_unit"]) for m in members]
        slow_id, slow_rate = max(rates, key=lambda pair: (pair[1], pair[0]))
        fast_id, fast_rate = min(fasts, key=lambda pair: (pair[1], pair[0]))
        pooled.append({
            "task_id": key[0],
            "method": key[1],
            "unit": key[2],
            "region": key[3],
            "period": key[4],
            "worker_ids": sorted({m["worker_id"] for m in members}),
            "source_row_count": len(members),
            "record_ids": sorted(rid for m in members for rid in m.get("record_ids") or []),
            "rate_fast_minutes_per_unit": fast_rate,
            "rate_slow_minutes_per_unit": slow_rate,
            "fast_end_record_id": fast_id,
            "slow_end_record_id": slow_id,
            "pooled_across_workers": True,
            "interval_kind": "descriptive_observed_range_not_probability_interval",
            "note": note or "跨人池化仅用于说明，不替换个体行，不产生个体安全结论。",
        })
    return pooled


def summarize(dataset: dict) -> str:
    """中文摘要，供人工审查；不替代数据集本体。"""
    target = dataset.get("target") or {}
    counters = dataset.get("counters") or {}
    lines = [
        "# 学习数据集摘要",
        "",
        f"- schema：{dataset.get('learning_dataset_schema_version')}；revision：{dataset.get('revision')}"
        f"；内容哈希：{str(dataset.get('dataset_sha256'))[:16]}…",
        f"- 目标：任务 {target.get('task_id')}／人 {target.get('worker_id')}／方法 {target.get('method')}"
        f"／单位 {target.get('unit')}",
        f"- 记录 {counters.get('records_total')}：纳入 {counters.get('included')}，"
        f"排除 {counters.get('excluded')}，慢端/中断纳入 {counters.get('slow_tail_included')}，"
        f"分组 {counters.get('groups')}",
        "",
        "## 训练行（人时地区分组）",
        "",
        "| 人 | 地区 | 期间 | 条数 | 慢端条数 | 快端(分钟/单位) | 慢端(分钟/单位) |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in dataset.get("training_rows") or []:
        lines.append(
            f"| {row['worker_id']} | {row['region']} | {row['period']} | {row['count']} | "
            f"{row['slow_tail_count']} | {row['rate_fast_minutes_per_unit']:.4g} | "
            f"{row['rate_slow_minutes_per_unit']:.4g} |"
        )
    lines += ["", "观测范围只是描述性区间，不是概率区间，也不自动收紧既有保守速率。"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 版本管理
# --------------------------------------------------------------------------- #
class LearningDatasetStore:
    """只追加地保存学习数据集版本；同一 revision 不得改写，乱序写入被拒绝。"""

    def __init__(self) -> None:
        self._by_revision: dict[int, dict] = {}
        self._max_revision: int | None = None

    def add(self, dataset: dict) -> dict:
        check = verify_dataset(dataset)
        if not check["valid"]:
            raise LearningDataError("DATASET_INVALID", "；".join(check["errors"]))
        revision = dataset["revision"]
        existing = self._by_revision.get(revision)
        digest = dataset["dataset_sha256"]
        if existing is not None:
            if existing["dataset_sha256"] != digest:
                raise LearningDataError(
                    "DATASET_VERSION_REWRITE",
                    f"revision {revision} 已存在且内容不同，不得改写既有版本",
                )
            return deepcopy(existing)  # 同一内容重复写入：幂等
        if self._max_revision is not None and revision <= self._max_revision:
            raise LearningDataError(
                "DATASET_REVISION_OUT_OF_ORDER",
                f"revision {revision} 不晚于已存最大版本 {self._max_revision}",
            )
        self._by_revision[revision] = deepcopy(dataset)
        self._max_revision = revision
        return deepcopy(dataset)

    def latest(self) -> dict | None:
        if self._max_revision is None:
            return None
        return deepcopy(self._by_revision[self._max_revision])

    def revision(self, revision: int) -> dict | None:
        found = self._by_revision.get(revision)
        return deepcopy(found) if found is not None else None

    def revisions(self) -> tuple[int, ...]:
        return tuple(sorted(self._by_revision))

    def history(self) -> tuple[dict, ...]:
        return tuple(
            {
                "revision": revision,
                "version_label": self._by_revision[revision].get("version_label"),
                "dataset_sha256": self._by_revision[revision].get("dataset_sha256"),
                "counters": dict(self._by_revision[revision].get("counters") or {}),
            }
            for revision in sorted(self._by_revision)
        )

    def verify_all(self) -> dict:
        issues = []
        for revision in sorted(self._by_revision):
            check = verify_dataset(self._by_revision[revision])
            if not check["valid"]:
                issues.append({"revision": revision, "errors": check["errors"]})
        return {"valid": not issues, "revision_count": len(self._by_revision), "issues": issues}

    def to_json(self) -> str:
        return json.dumps(
            {
                "learning_dataset_schema_version": LEARNING_DATASET_SCHEMA_VERSION,
                "versions": [self._by_revision[r] for r in sorted(self._by_revision)],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> "LearningDatasetStore":
        data = json.loads(text)
        store = cls()
        for dataset in data.get("versions", []):
            check = verify_dataset(dataset)
            if not check["valid"]:
                raise LearningDataError("DATASET_LOAD_TAMPERED", "；".join(check["errors"]))
            store._restore(dataset)
        return store

    def _restore(self, dataset: dict) -> None:
        revision = dataset["revision"]
        self._by_revision[revision] = dataset
        if self._max_revision is None or revision > self._max_revision:
            self._max_revision = revision
