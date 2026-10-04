"""历史负荷台账：只由「实际发生且可核实」的事件更新每日活动负荷；查询按当地民用日。

口径边界（本模块刻意不做的事）：
- 不从建议 sessions / 计划拷贝（copied_from_plan）更新；计划分钟不是已劳动。
- 不扣减 remaining_quantity（那是剩余量台账，另属 R17-A02 范围）。
- 不做事件状态机（计划/已看到/已理解/已执行，另属 R17-A01 范围）；只消费已标为
  实际发生的事件，并沿用 workload.accumulate_occurred_load 作为唯一校验与切分来源。
- 不生成健康许可、不判断个人是否"安全"；未知日期保持未知，不写成 0。

台账结构（schema load_history/1.0）::

    {
      "schema_version": "load_history/1.0",
      "timezone": "Asia/Shanghai",              # 台账固定民用时区，更新时不得改变
      "used_active_minutes_by_date": {wid: {day: minutes}},
      "unknown_dates": {wid: [day, ...]},
      "applied_event_ids": [event_id, ...],     # 防重复计入（同 id 不二次加负荷）
      "events": [ {event_id, worker_id, status, active_minutes_by_date}, ... ],
    }

查询只返回有核实记录的日；无记录的日不返回（既不返回 0，也不返回 unknown）。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json

from .models import InputError, _number, resolve_timezone, is_placeholder_source, to_utc
from .workload import accumulate_occurred_load

SCHEMA_VERSION = "load_history/1.0"
MAX_DAY_MINUTES = 1440
EPS = 1e-8

_HISTORY_KEYS = frozenset({
    "schema_version", "timezone", "used_active_minutes_by_date", "unknown_dates",
    "applied_event_ids", "events",
})


def _valid_day(value, path) -> str:
    try:
        valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        valid = False
    if not valid:
        raise InputError(f"{path}: 日期键必须为 YYYY-MM-DD")
    return value


def empty_load_history(timezone) -> dict:
    """新建空台账。时区在创建时确定，之后更新不得改变（改时区会移动日界）。"""
    resolve_timezone(timezone)
    return {
        "schema_version": SCHEMA_VERSION,
        "timezone": timezone if isinstance(timezone, str) else str(timezone),
        "used_active_minutes_by_date": {},
        "unknown_dates": {},
        "applied_event_ids": [],
        "events": [],
    }


def _validated_history(history) -> dict:
    """校验台账结构并返回深拷贝；输入对象不被修改。"""
    if not isinstance(history, dict):
        raise InputError("history: 必须为对象")
    extra = sorted(set(history) - _HISTORY_KEYS)
    if extra:
        raise InputError(f"history.{extra[0]}: 未知字段")
    missing = sorted(_HISTORY_KEYS - set(history))
    if missing:
        raise InputError(f"history.{missing[0]}: 缺少字段")
    if history["schema_version"] != SCHEMA_VERSION:
        raise InputError("history.schema_version: 不支持的台账版本")
    resolve_timezone(history["timezone"])
    used = history["used_active_minutes_by_date"]
    if not isinstance(used, dict):
        raise InputError("history.used_active_minutes_by_date: 必须为对象")
    for wid, days in used.items():
        if not isinstance(wid, str) or not wid.strip():
            raise InputError("history.used_active_minutes_by_date: 劳动者键必须为非空字符串")
        if not isinstance(days, dict):
            raise InputError(f"history.used_active_minutes_by_date.{wid}: 必须为对象")
        for day, minutes in days.items():
            _valid_day(day, f"history.used_active_minutes_by_date.{wid}")
            _number(minutes, f"history.used_active_minutes_by_date.{wid}.{day}", 0, MAX_DAY_MINUTES)
    unknown = history["unknown_dates"]
    if not isinstance(unknown, dict):
        raise InputError("history.unknown_dates: 必须为对象")
    for wid, days in unknown.items():
        if not isinstance(wid, str) or not wid.strip():
            raise InputError("history.unknown_dates: 劳动者键必须为非空字符串")
        if not isinstance(days, list):
            raise InputError(f"history.unknown_dates.{wid}: 必须为数组")
        for day in days:
            _valid_day(day, f"history.unknown_dates.{wid}")
            if day in used.get(wid, {}):
                raise InputError(f"history: {wid} {day} 不得同时存在已核实分钟与未知标记")
    applied = history["applied_event_ids"]
    if not isinstance(applied, list):
        raise InputError("history.applied_event_ids: 必须为数组")
    for event_id in applied:
        if not isinstance(event_id, str) or not event_id.strip():
            raise InputError("history.applied_event_ids: 必须为非空字符串")
    if len(applied) != len(set(applied)):
        raise InputError("history.applied_event_ids: 不得重复")
    events = history["events"]
    if not isinstance(events, list):
        raise InputError("history.events: 必须为数组")
    for index, row in enumerate(events):
        if not isinstance(row, dict):
            raise InputError(f"history.events[{index}]: 必须为对象")
    return deepcopy(history)


def _event_key(event):
    """取事件稳定标识；无则返回 None（不可做防重保护，但仍按已核实与否处理）。"""
    for field in ("event_id", "id"):
        value = event.get(field)
        if isinstance(value, str) and not is_placeholder_source(value):
            return value.strip()
    return None


def _event_fingerprint(event):
    """按实际负荷事实去重；来源备注变化不应重复增加同一次劳动。"""
    payload = {key: event.get(key) for key in ("kind", "worker_id", "task_id", "status",
        "active_minutes_occurred", "active_minutes", "clock_source", "quantity_derivation", "phases")}
    for key, fallback in (("actual_start", "start"), ("actual_end", "end")):
        value = event.get(key, event.get(fallback))
        try:
            payload[key] = to_utc(value).isoformat()
        except (ValueError, TypeError):
            payload[key] = value
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def update_load_history(history, events, worker_id=None) -> dict:
    """用实际发生事件增量更新历史负荷，只写入已核实（included）的分钟。

    未核实事件（计划拷贝、状态未知、缺时钟来源等）记录拒绝原因但不写入负荷。
    同一 event_id 重复提交不二次计入。任何校验失败都不修改入参台账（先深拷贝）。

    返回 {"history": 新台账, "applied": [...], "rejected": [...], "replayed": [...],
          "warnings": [...], "resolved_unknown_dates": [...], 计数, "note"}。
    """
    state = _validated_history(history)
    timezone = state["timezone"]
    if not isinstance(events, list):
        raise InputError("events: 必须为数组")

    applied_ids = set(state["applied_event_ids"])
    fingerprints = {row.get("event_id"): row.get("event_fingerprint") for row in state["events"]}
    seen_fingerprints = {value for value in fingerprints.values() if value}
    seen_batch = {}
    pending = []
    replayed = []
    warnings = []
    identity_rejected = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise InputError(f"events[{index}]: 必须为对象")
        key = _event_key(event)
        if key is None:
            warnings.append({"index": index, "code": "MISSING_EVENT_ID",
                             "message": "事件缺少稳定标识，无法防重复计入"})
            identity_rejected.append({"index": index, "event_id": None, "reasons": ["missing_event_id"]})
            continue
        fingerprint = _event_fingerprint(event)
        if key in fingerprints and fingerprints[key] and fingerprints[key] != fingerprint:
            raise InputError(f"events[{index}]: EVENT_ID_CONFLICT 同一事件标识与已入账事实冲突")
        if key in seen_batch and seen_batch[key] != fingerprint:
            raise InputError(f"events[{index}]: EVENT_ID_CONFLICT 同批同一事件标识内容冲突")
        if key in applied_ids or key in seen_batch:
            replayed.append({"event_id": key, "reason": "ALREADY_APPLIED"})
            continue
        if fingerprint in seen_fingerprints:
            replayed.append({"event_id": key, "reason": "CONTENT_DUPLICATE"})
            continue
        seen_batch[key] = fingerprint
        seen_fingerprints.add(fingerprint)
        pending.append((index, key, event))

    rows = {"events": [], "unknown_dates": {}}
    if pending:
        rows = accumulate_occurred_load([event for _, _, event in pending], timezone,
                                        worker_id=worker_id)

    used = {wid: dict(days) for wid, days in state["used_active_minutes_by_date"].items()}
    unknown = {wid: set(days) for wid, days in state["unknown_dates"].items()}
    applied_event_ids = list(state["applied_event_ids"])
    stored_events = list(state["events"])
    applied_rows = []
    rejected_rows = identity_rejected
    resolved_unknown = []

    for (index, key, _event), row in zip(pending, rows["events"]):
        if not row["included"]:
            rejected_rows.append({"index": index, "event_id": key,
                                  "reasons": list(row["exclusion_reasons"])})
            continue
        wid = row["worker_id"]
        for day, minutes in row["active_minutes_by_date"].items():
            bucket = used.setdefault(wid, {})
            total = bucket.get(day, 0.0) + float(minutes)
            if total > MAX_DAY_MINUTES + EPS:
                raise InputError(
                    f"used_active_minutes_by_date.{wid}.{day}: 累计 {total:g} 分钟超过每日 "
                    f"{MAX_DAY_MINUTES} 分钟上限，拒绝更新，历史保持不变")
            bucket[day] = total
            if day in unknown.get(wid, set()):
                unknown[wid].discard(day)
                resolved_unknown.append({"worker_id": wid, "date": day})
        if key is not None:
            applied_event_ids.append(key)
        stored_events.append({
            "event_id": key,
            "event_fingerprint": _event_fingerprint(_event),
            "worker_id": wid,
            "status": row["status"],
            "active_minutes_by_date": dict(row["active_minutes_by_date"]),
        })
        applied_rows.append({"index": index, "event_id": key, "worker_id": wid,
                             "active_minutes_by_date": dict(row["active_minutes_by_date"])})

    for wid, days in rows["unknown_dates"].items():
        for day in days:
            if day in used.get(wid, {}):
                raise InputError(
                    f"occurred_load.{wid}.{day}: 该日已有已核实分钟，不能再标记为未知，"
                    f"拒绝更新，历史保持不变")
            unknown.setdefault(wid, set()).add(day)

    state = {
        "schema_version": SCHEMA_VERSION,
        "timezone": timezone,
        "used_active_minutes_by_date": used,
        "unknown_dates": {wid: sorted(days) for wid, days in unknown.items() if days},
        "applied_event_ids": applied_event_ids,
        "events": stored_events,
    }
    return {
        "history": state,
        "applied": applied_rows,
        "rejected": rejected_rows,
        "replayed": replayed,
        "warnings": warnings,
        "resolved_unknown_dates": resolved_unknown,
        "included_count": len(applied_rows),
        "excluded_count": len(rejected_rows),
        "replayed_count": len(replayed),
        "note": "只把已核实（included）的实际发生分钟写入历史负荷；计划拷贝与未知状态不写入，"
                "也不写成 0。同 event_id 不二次计入。不扣减 remaining，不是健康许可。",
    }


def query_load_history(history, worker_id, start_date=None, end_date=None) -> dict:
    """查询某劳动者在 [start_date, end_date]（含端点）内的已核实每日负荷。

    只返回有记录的日；无记录的日不返回 0，也不返回 unknown（unknown 单列）。
    """
    state = _validated_history(history)
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise InputError("worker_id: 必须为非空字符串")
    start = _valid_day(start_date, "start_date") if start_date is not None else None
    end = _valid_day(end_date, "end_date") if end_date is not None else None
    if start and end and start > end:
        raise InputError("start_date/end_date: 起始日不得晚于结束日")

    def _in_range(day):
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    days = {day: float(minutes)
            for day, minutes in sorted(state["used_active_minutes_by_date"].get(worker_id, {}).items())
            if _in_range(day)}
    unknown_days = sorted(day for day in state["unknown_dates"].get(worker_id, []) if _in_range(day))
    provenance = []
    for row in state["events"]:
        if row.get("worker_id") != worker_id:
            continue
        for day, minutes in sorted((row.get("active_minutes_by_date") or {}).items()):
            if _in_range(day):
                provenance.append({"event_id": row.get("event_id"), "status": row.get("status"),
                                   "date": day, "minutes": float(minutes)})
    return {
        "worker_id": worker_id,
        "timezone": state["timezone"],
        "start_date": start,
        "end_date": end,
        "days": days,
        "days_count": len(days),
        "total_minutes": sum(days.values()),
        "unknown_dates": unknown_days,
        "provenance": provenance,
        "verified_only": True,
        "note": "仅含已核实实际发生分钟；无记录的日不返回（不是 0）。unknown_dates 单列，"
                "不得当作 0 或已劳动。不是健康许可。",
    }
