"""R17-A02：实际剩余量查询与重复事件去重（只读纯函数）。

定位
----
把「已上传的实际作业事件」折算成本任务当前的**实际剩余量**，并保证同一
事件被重复上传时不会重复扣减。本模块只做数量核对与去重，不排程、不估算
速率、不认证事件真实性，也不修改任何既有模块或输入对象。

硬约束（不得弱化）
------------------
- 只读：不修改入参、不写文件、不调用 engine/audit/recalc 的排程内部逻辑。
- 重复上传不重复扣量：
  * 同一 ``event_id`` 的多次上传（内容一致）只计一次；
  * 不同 ``event_id`` 但同一「任务 / 人 / 起止 / 完成量」的内容重复同样只计一次；
  * 同一 ``event_id`` 内容不一致、或同一时间窗完成量不一致时判为**冲突**，
    整组不参与扣减并上报，绝不静默择一。
- 不猜单位、不静默换算：事件完成量单位必须与任务剩余量同一规范口径
  （面积仅允许 mu↔sqm 的精确换算，且只做一次）。
- 缺凭证不计数：无稳定 ``event_id``、无来源、状态未知、来自计划复制的事件
  一律不参与扣减并逐条上报原因。
- 保守方向恒为「宁可少扣，绝不多扣」：被排除的事件只会让剩余量偏保守（偏高），
  永远不会因为重复上传而把剩余量多扣一截。
- 余量不为负：完成量超过剩余量时 ``remaining_after`` 截断为 0，并单独上报超额。

输入事件契约（与 A01「实际作业事件存储」对齐，轮末由主控合并）
------------------------------------------------------------
单个事件（dict）关键字段::

    {
      "kind": "work_event",                 # 非 work_event 不计入
      "event_id": "evt-...",                # 幂等键；缺失/占位则不计数
      "task_id": "t1",                      # 必须等于被查任务的 id
      "worker_id": "w1",                    # 可选；传 worker_id 时按人过滤
      "actual_start"/"actual_end": ISO8601, # 含时区；回退 start/end
      "status": "completed|partial|interrupted|not_done|unknown",
      "quantity": {"value": 200, "unit": "sqm"},  # 本次完成量；缺省按 0 计
      "source": "field-note-2026-09-14",    # 有完成量时必须给非占位来源
      "clock_source": "observed_clock"      # 出现 copied_from_plan 类标记则不计
    }

``remaining_after``（事件自报余量）**不参与**扣减：自报值可能被重复上传放大，
本模块只按可去重的完成量求和，避免「上传方每次重报整段余量」造成重复扣减。
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .models import InputError, is_placeholder_source, to_utc
from .workload import PLAN_COPY_MARKERS, canonical_quantity

SCHEMA_VERSION = "1.0"
REPORT_KIND = "remaining_query_report"

# 计入完成量的状态；not_done 计入但贡献 0；unknown 一律不计入。
COUNTING_STATUSES = frozenset({"completed", "partial", "interrupted"})
ZERO_STATUSES = frozenset({"not_done"})
UNKNOWN_STATUSES = frozenset({"unknown"})
ALL_STATUSES = COUNTING_STATUSES | ZERO_STATUSES | UNKNOWN_STATUSES

# 事件字段里若出现这些标记，说明是从计划抄来的，不是已发生的实际作业。
_PLAN_MARKERS = PLAN_COPY_MARKERS
_PLAN_MARKER_FIELDS = ("clock_source", "clock_derivation", "load_derivation", "quantity_derivation")

# 去重后因重复而排除的码 / 因冲突而排除的码（供上层分类统计）。
DUPLICATE_CODES = ("ID_DUPLICATE", "CONTENT_DUPLICATE")
CONFLICT_CODES = ("EVENT_ID_CONFLICT", "QUANTITY_CONFLICT")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _usable_id(value):
    """稳定标识：非空、非占位词；否则返回 None（无法去重）。"""
    text = _text(value)
    if not text or is_placeholder_source(text):
        return None
    return text


def _event_id_of(event):
    if not isinstance(event, dict):
        return None
    return _usable_id(event.get("event_id", event.get("id")))


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _canon(event):
    """事件完成量的规范口径 (value, unit)；无完成量返回 (None, None)。"""
    quantity = event.get("completed_quantity", event.get("quantity"))
    # 两种接口字段均在时，必须表达相同完成量，禁止择一掩盖冲突。
    if "completed_quantity" in event and "quantity" in event:
        try:
            if canonical_quantity(event["completed_quantity"]) != canonical_quantity(event["quantity"]):
                return "invalid", None
        except (InputError, ValueError, TypeError):
            return "invalid", None
    if quantity is None:
        return None, None
    try:
        value, unit = canonical_quantity(quantity)
    except (InputError, ValueError):
        return "invalid", None
    return value, unit


def _quantity_key(event):
    value, unit = _canon(event)
    if value is None:
        return None
    if value == "invalid":
        return ("invalid", json.dumps(event.get("quantity"), ensure_ascii=False,
                                       sort_keys=True, default=str))
    return (repr(round(float(value), 9)), unit)


def _instant_key(event, actual, fallback):
    value = event.get(actual, event.get(fallback))
    try:
        return to_utc(value).isoformat()
    except (InputError, ValueError, TypeError):
        return value


def _payload_key(event):
    """同一 event_id 下用于判「内容是否一致」的规范载荷（不含 source 等元数据）。"""
    value, unit = _canon(event)
    payload = {
        "task_id": event.get("task_id"),
        "worker_id": event.get("worker_id"),
        "start": _instant_key(event, "actual_start", "start"),
        "end": _instant_key(event, "actual_end", "end"),
        "status": event.get("status", "unknown"),
        "kind": event.get("kind", "work_event"),
        "quantity": [repr(value) if value is not None else None, unit],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _content_key(event):
    """跨 event_id 判「是否同一物理事件」的键：任务 / 人 / 起止 时间窗。"""
    return json.dumps(
        {
            "task_id": event.get("task_id"),
            "worker_id": event.get("worker_id"),
            "start": _instant_key(event, "actual_start", "start"),
            "end": _instant_key(event, "actual_end", "end"),
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )


def _copied_from_plan(event):
    for field in _PLAN_MARKER_FIELDS:
        marker = event.get(field)
        if isinstance(marker, str) and marker.strip().casefold() in _PLAN_MARKERS:
            return True
    return False


def _domain_check(event, path, task_id, worker_id, unit):
    """对去重后的幸存事件做逐条域校验；返回 (code, message) 或 None。"""
    if event.get("kind", "work_event") != "work_event":
        return "NOT_WORK_EVENT", "非 work_event，不计入剩余量"
    if _copied_from_plan(event):
        return "COPIED_FROM_PLAN", "事件标记为从计划复制，不是已发生的实际作业"
    if event.get("task_id") != task_id:
        return "TASK_MISMATCH", "event.task_id 与所查任务不一致"
    if worker_id is not None and event.get("worker_id") != worker_id:
        return "WORKER_MISMATCH", "event.worker_id 与筛选人员不一致"
    try:
        start = to_utc(event.get("actual_start", event.get("start")))
        end = to_utc(event.get("actual_end", event.get("end")))
        if end <= start:
            return "INVALID_TIME", "实际起止必须满足开始早于结束"
    except (InputError, ValueError, TypeError):
        return "INVALID_TIME", "实际起止缺失或无时区，无法确认和去重"
    verification = event.get("verification")
    if verification is not None and (not isinstance(verification, dict) or verification.get("state") != "verified"):
        return "UNVERIFIED_EVENT", "事件明确未核实，不参与扣减"
    status = event.get("status", "unknown")
    if status not in ALL_STATUSES:
        return "INVALID_STATUS", f"未知状态 {status!r}"
    if status in UNKNOWN_STATUSES:
        return "UNKNOWN_STATUS", "状态未知，不做任何扣减"
    value, event_unit = _canon(event)
    has_quantity = event.get("completed_quantity", event.get("quantity")) is not None
    if status in COUNTING_STATUSES and not has_quantity:
        return "MISSING_QUANTITY", "完成量未知，不把缺字段或 null 当作 0"
    if has_quantity and value == "invalid":
        return "INVALID_QUANTITY", "完成量不是合法的 {value,unit}"
    if has_quantity and event_unit != unit:
        return "UNIT_MISMATCH", f"完成量单位 {event_unit} 与任务剩余量口径 {unit} 不一致，不静默换算"
    if status in ZERO_STATUSES and has_quantity and value is not None and float(value) > 0:
        return "NOT_DONE_WITH_QUANTITY", "未做/中断为 not_done 却带正完成量"
    if has_quantity and status in COUNTING_STATUSES and not _usable_id(event.get("source")):
        return "MISSING_SOURCE", "带完成量的事件必须给出非占位来源，避免编造完成数"
    return None


def query_remaining(task, events, *, worker_id=None):
    """查询任务在给定实际作业事件下的更新剩余量（含重复事件去重）。

    只读：不修改 ``task`` 与 ``events``。返回结构化报告；对结构性错误抛 InputError，
    对单条事件的缺陷按保守方式排除并逐条上报，不因一条坏记录丢掉整批。
    """
    if not isinstance(task, dict):
        raise InputError("task: 必须为对象")
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise InputError("task.id: 必须为非空字符串")
    try:
        before_value, unit = canonical_quantity(task.get("remaining_quantity"))
    except InputError as exc:
        raise InputError(f"task.remaining_quantity: {exc}")
    before = _decimal(before_value)
    if before is None or before < 0:
        raise InputError("task.remaining_quantity: 必须为非负有限数")
    if not isinstance(events, list):
        raise InputError("events: 必须为数组")

    # 逐条登记，后续阶段只填 excluded，不在早期丢弃，保证 总数=计入+排除。
    records = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            records.append({"index": index, "event": None, "event_id": None,
                            "excluded": ("EVENT_NOT_OBJECT", "事件必须为对象")})
            continue
        records.append({"index": index, "event": event,
                        "event_id": _event_id_of(event), "excluded": None})

    # 阶段一：按幂等键 event_id 去重与冲突检测。
    by_id = {}
    for record in records:
        if record["excluded"] is not None:
            continue
        if record["event_id"] is None:
            record["excluded"] = ("MISSING_EVENT_ID",
                                  "缺少稳定 event_id，无法去重，不参与扣减")
            continue
        by_id.setdefault(record["event_id"], []).append(record)
    for event_id, group in by_id.items():
        if len({_payload_key(record["event"]) for record in group}) > 1:
            for record in group:
                record["excluded"] = (
                    "EVENT_ID_CONFLICT",
                    f"event_id {event_id} 的多次上传内容不一致，不静默择一，整组不参与扣减")
            continue
        group.sort(key=lambda record: record["index"])
        for record in group[1:]:
            record["excluded"] = ("ID_DUPLICATE",
                                  f"event_id {event_id} 重复上传，只计一次")

    # 阶段二：跨 event_id 的内容重复（换 id 重传同一物理事件）与数量冲突。
    by_content = {}
    for record in records:
        if record["excluded"] is None:
            by_content.setdefault(_content_key(record["event"]), []).append(record)
    for group in by_content.values():
        if len(group) == 1:
            continue
        if len({_quantity_key(record["event"]) for record in group}) > 1:
            for record in group:
                record["excluded"] = (
                    "QUANTITY_CONFLICT",
                    "同一(任务,人,起止)窗口出现不同完成量，指代不明，整组不参与扣减")
            continue
        group.sort(key=lambda record: (record["event_id"] or "", record["index"]))
        for record in group[1:]:
            record["excluded"] = (
                "CONTENT_DUPLICATE",
                "不同 event_id 但同一(任务,人,起止,完成量)，视为重复上传，只计一次")

    # 阶段三：对幸存事件做逐条域校验。
    for record in records:
        if record["excluded"] is not None:
            continue
        code = _domain_check(record["event"], f"events[{record['index']}]",
                             task_id, worker_id, unit)
        if code:
            record["excluded"] = code

    # 阶段四：汇总完成量与更新剩余量。全程 Decimal，避免浮点误差。
    completed = Decimal(0)
    included, excluded = [], []
    for record in records:
        if record["excluded"] is not None:
            code, message = record["excluded"]
            excluded.append({"index": record["index"], "event_id": record["event_id"],
                             "code": code, "message": message})
            continue
        event = record["event"]
        status = event.get("status", "unknown")
        value, _unit = _canon(event)
        contribution = Decimal(0)
        if value is not None and status in COUNTING_STATUSES:
            contribution = _decimal(value) or Decimal(0)
        completed += contribution
        included.append({
            "index": record["index"], "event_id": record["event_id"], "status": status,
            "contribution": float(contribution), "unit": unit,
            "source": event.get("source"),
        })

    remaining_after = before - completed
    over_completed = Decimal(0)
    if remaining_after < 0:
        over_completed = -remaining_after
        remaining_after = Decimal(0)

    duplicates = [row for row in excluded if row["code"] in DUPLICATE_CODES]
    conflicts = [row for row in excluded if row["code"] in CONFLICT_CODES]
    if not records:
        status = "NO_EVENTS"
    elif not included:
        status = "NO_USABLE_EVENTS"
    else:
        status = "UPDATED"

    report = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "implementation": "yilao_agri.remaining",
        "task_id": task_id,
        "worker_filter": worker_id,
        "unit": unit,
        "status": status,
        "remaining_before": float(before),
        "completed_included": float(completed),
        "remaining_after": float(remaining_after),
        "over_completed": float(over_completed),
        "is_complete": (before == 0) or (remaining_after <= 0 and completed > 0),
        "events_total": len(records),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "deduplicated_count": len(duplicates),
        "conflict_count": len(conflicts),
        "has_duplicates": bool(duplicates),
        "has_conflicts": bool(conflicts),
        "included_events": included,
        "excluded_events": excluded,
        "input_mutated": False,
        "meaning": "按可去重的实际完成量更新剩余量；不排程、不估算速率、不认证事件真实性，"
                   "也不构成个人健康或现场效果结论。",
        "note": "同一事件重复上传只计一次；冲突记录整组不扣减（宁可少扣，绝不多扣）。"
                "remaining_after 由 remaining_before 减可去重完成量得到，不采信事件自报余量。",
    }
    violations = assert_report_invariants(report)
    if violations:
        raise AssertionError("剩余量报告违反硬约束: " + "; ".join(violations))
    return report


# 语义别名：本模块的主要用途就是「查询剩余量与重复事件」。
query = query_remaining


def assert_report_invariants(report) -> list:
    """返回违反项列表；空表示报告满足全部硬约束（供测试与独立复核调用）。"""
    violations = []
    before = report.get("remaining_before")
    completed = report.get("completed_included")
    after = report.get("remaining_after")
    over = report.get("over_completed")
    for name, value in (("remaining_before", before), ("completed_included", completed),
                        ("remaining_after", after), ("over_completed", over)):
        if not isinstance(value, (int, float)) or value is None or value < 0:
            violations.append(f"{name} 必须为非负有限数")
    if isinstance(after, (int, float)) and after < 0:
        violations.append("remaining_after 不得为负")
    if isinstance(over, (int, float)) and over < 0:
        violations.append("over_completed 不得为负")
    if all(isinstance(x, (int, float)) for x in (before, completed, after, over)):
        if over == 0 and abs(before - completed - after) > 1e-6:
            violations.append("未超额时须满足 remaining_before - completed = remaining_after")
        if over > 0 and after != 0:
            violations.append("超额时 remaining_after 必须截断为 0")
    if report.get("events_total") != report.get("included_count", 0) + report.get("excluded_count", 0):
        violations.append("事件总数必须等于计入数加排除数")
    included_ids = {row["index"] for row in report.get("included_events", [])}
    excluded_ids = {row["index"] for row in report.get("excluded_events", [])}
    if included_ids & excluded_ids:
        violations.append("同一条事件不能既计入又排除")
    keys = [(row.get("event_id"), row.get("index")) for row in report.get("included_events", [])]
    ids = [row.get("event_id") for row in report.get("included_events", [])]
    if len(ids) != len(set(ids)):
        violations.append("计入集合中 event_id 必须唯一（去重后不得重复）")
    if report.get("input_mutated") is not False:
        violations.append("input_mutated 必须为 False")
    if len(keys) != report.get("included_count", 0):
        violations.append("included_count 与计入明细不一致")
    return violations


def summary_line(report) -> str:
    """一行人类可读摘要，便于日志与界面标题；不含健康或可行性结论。"""
    return ("任务 {task}：剩余 {before:g} → {after:g} {unit}（计入 {inc} 条 / 共 {total} 条，"
            "去重 {dup} 条，冲突 {conf} 条{over}）。").format(
        task=report["task_id"], before=report["remaining_before"], after=report["remaining_after"],
        unit=report["unit"], inc=report["included_count"], total=report["events_total"],
        dup=report["deduplicated_count"], conf=report["conflict_count"],
        over=f"，超额 {report['over_completed']:g}" if report["over_completed"] else "",
    )
