"""实际作业事件的存储与查询（R17-A01）。

目标（唯一）：
- 区分计划(planned) / 已看到(seen) / 已理解(understood) / 已执行(executed)；
  未反馈保持未知(unknown)。
- 只记已核实的事件；未核实/未反馈不写入存储，查询按未知返回，不写 0。

本模块承接 R10-A08 独立提案 `yilao.plan_feedback`（0.2.0）中
`work_event` / `occurred_load` 的硬约束，只做存储与查询，不改排程计算：

1. `sessions` / `scheduled_*` 永远是建议，不是已发生。
2. 未反馈记 unknown，不能记完成；JSON 的 null 不是缺字段，也不是 0。
3. 已发生负荷只来自已核实作业事件合计或显式零证明，禁止从建议的
   `scheduled_sessions[].active_minutes_by_date` 抄入。
4. 作业事件的钟表与完成量必须独立于计划：`clock_source=copied_from_plan`
   与 `quantity_derivation=copied_from_schedule` 一律拒绝（PLAN_AS_ACTUAL）。
5. 建议一旦暴露，分析组不得退回 `shadow_unexposed`。

契约状态：未冻结。本模块是可由主控冻结的实现提案，`contract_status`
恒为 `IMPLEMENTATION_PROPOSAL`。不声称临床效果、全国通用或现场有效。

命令行（自包含，不改 yilao_agri/cli.py）：

    python3 -m yilao_agri.work_events record --store S.json --events E.json
    python3 -m yilao_agri.work_events mark --store S.json --suggestion sug-1 --stage seen --at 2026-09-12T06:00:00+08:00
    python3 -m yilao_agri.work_events query --store S.json --action occurred --worker elder --date 2026-09-12
    python3 -m yilao_agri.work_events query --store S.json --action summary
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .models import is_placeholder_source

CONTRACT_ID = "yilao.plan_feedback"
CONTRACT_VERSION = "0.2.0"
CONTRACT_STATUS = "IMPLEMENTATION_PROPOSAL"
SCHEMA_VERSION = "1.0"

SQM_PER_MU = 2000.0 / 3.0
EPS = 0.05

UNITS = frozenset({"mu", "sqm", "kg", "trip", "plant", "m", "m3"})
INTEGER_UNITS = frozenset({"trip", "plant"})

EVENT_STATUSES = frozenset({"completed", "partial", "not_done", "interrupted", "unknown"})

EVIDENCE_TYPES = frozenset({
    "SYNTHETIC",
    "IMPLEMENTATION_TEST",
    "FIELD_OBSERVATION",
    "SECONDARY_SUMMARY",
    "PRIMARY_SOURCE",
    "PROFESSIONAL_REVIEW",
})

# 允许的钟表来源；copied_from_plan 显式拒绝。
CLOCK_SOURCES = frozenset({
    "observed_clock",
    "recorder_stated",
    "clock_instrument",
    "recorder_estimate",
})
FORBIDDEN_CLOCK_SOURCES = frozenset({"copied_from_plan"})

# 允许的完成量推导；copied_from_schedule 显式拒绝。
QUANTITY_DERIVATIONS = frozenset({
    "independent_observation",
    "recorder_count",
    "measured",
    "weighed",
    "explicit_zero",
})
FORBIDDEN_QUANTITY_DERIVATIONS = frozenset({"copied_from_schedule"})

# 生命周期阶段。unknown 表示无任何反馈。
STAGE_UNKNOWN = "unknown"
STAGE_PLANNED = "planned"
STAGE_SEEN = "seen"
STAGE_UNDERSTOOD = "understood"
STAGE_EXECUTED = "executed"
STAGES = (STAGE_UNKNOWN, STAGE_PLANNED, STAGE_SEEN, STAGE_UNDERSTOOD, STAGE_EXECUTED)

# 严格链条：planned -> seen -> understood -> executed。不允许跳级或回退。
_STAGE_EDGE = {
    STAGE_PLANNED: STAGE_SEEN,
    STAGE_SEEN: STAGE_UNDERSTOOD,
}
# executed 只能由已核实作业事件推进（可来自 seen 或 understood）。
_EXECUTED_FROM = frozenset({STAGE_SEEN, STAGE_UNDERSTOOD})

VERIFICATION_STATES = frozenset({"verified", "unverified", "unknown"})

INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

HEALTH_KEYS = ("health_claim", "treat_as_health_outcome", "reduces_disease", "proves_safety",
               "health_or_safety_verified", "clinically_validated", "clinical_effect_verified")
FIELD_CLAIM_KEYS = ("field_verified", "is_field_evidence")


class WorkEventError(ValueError):
    """作业事件结构或语义非法。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class UnverifiedEventError(WorkEventError):
    """未核实事件被拒绝写入存储（只记已核实的事件）。"""


class LifecycleError(WorkEventError):
    """建议生命周期非法（跳级、回退或缺少前置暴露）。"""


class DuplicateEventError(WorkEventError):
    """event_id 重复；存储为追加式，不允许覆盖。"""


def _fail(code: str, message: str) -> None:
    raise WorkEventError(code, message)


def _parse_instant(value, path: str):
    if not isinstance(value, str) or not INSTANT_RE.match(value):
        _fail("MISSING_TIMEZONE", f"{path}: 时刻须为带偏移的 ISO-8601，如 2026-09-12T06:20:00+08:00")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("MISSING_TIMEZONE", f"{path}: 无法解析时刻 {value!r}")
    if stamp.tzinfo is None:
        _fail("MISSING_TIMEZONE", f"{path}: 缺少时区偏移")
    return stamp


def _finite_number(value, path: str, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("NAN_OR_INF", f"{path}: 必须是有限数字，禁止 null 冒充 0")
    if not math.isfinite(float(value)):
        _fail("NAN_OR_INF", f"{path}: 禁止 NaN/Infinity")
    if minimum is not None and float(value) < minimum:
        _fail("STATUS_QUANTITY_CONFLICT", f"{path}: 不得小于 {minimum}")
    return float(value)


def _text(value, path: str, *, allow_null=False):
    if value is None:
        if allow_null:
            return None
        _fail("MISSING_FIELD", f"{path}: 缺字段")
    if not isinstance(value, str) or not value.strip():
        _fail("MISSING_FIELD", f"{path}: 须为非空字符串")
    return value


def canonical_amount(quantity, path: str, *, allow_null=False):
    """把 {value, unit} 规范化为 (value_in_canonical_unit, canonical_unit)。

    mu -> sqm 换算；trip/plant 必须为整数。返回 None 表示未知（不是 0）。
    """
    if quantity is None:
        if allow_null:
            return None
        _fail("UNIT_MISMATCH", f"{path}: 缺数量")
    if not isinstance(quantity, dict) or "value" not in quantity or "unit" not in quantity:
        _fail("UNIT_MISMATCH", f"{path}: 必须含 value/unit")
    unit = quantity["unit"]
    if unit not in UNITS:
        _fail("UNIT_MISMATCH", f"{path}: 不支持的单位 {unit!r}")
    value = _finite_number(quantity["value"], f"{path}.value", minimum=0.0)
    if unit in INTEGER_UNITS and value != int(value):
        _fail("UNIT_MISMATCH", f"{path}: {unit} 必须为整数")
    if unit == "mu":
        return value * SQM_PER_MU, "sqm"
    return value, unit


def _phase_minutes(phases, kind: str, path: str) -> float:
    total = 0.0
    for i, phase in enumerate(phases):
        if not isinstance(phase, dict):
            _fail("PHASE_KIND", f"{path}[{i}]: 阶段须为对象")
        if phase.get("kind") != kind:
            continue
        start = _parse_instant(phase.get("start"), f"{path}[{i}].start")
        end = _parse_instant(phase.get("end"), f"{path}[{i}].end")
        if end <= start:
            _fail("CLOCK_INCONSISTENT", f"{path}[{i}]: 结束须晚于开始")
        total += (end - start).total_seconds() / 60.0
    return total


def validate_work_event(raw: dict) -> dict:
    """校验并规范化一条实际作业事件。只接受已核实事件。

    非法即抛 WorkEventError（子类），错误码含义见 R10-A08 error_codes.json。
    """
    if not isinstance(raw, dict):
        _fail("MISSING_KIND", "事件须为对象")
    if raw.get("kind", "work_event") != "work_event":
        _fail("UNKNOWN_KIND", f"kind 须为 work_event，得到 {raw.get('kind')!r}")

    for key in ("event_id", "worker_id", "task_id", "timezone", "status", "evidence_type"):
        _text(raw.get(key), key)
    # 幂等键由采集端一次生成并保存；重试不能另造 ID，也不能使用占位词。
    if is_placeholder_source(raw["event_id"]):
        _fail("MISSING_EVENT_ID", "event_id 必须是稳定的非占位标识")
    if not isinstance(raw.get("source"), str) or is_placeholder_source(raw["source"]):
        _fail("MISSING_SOURCE", "source 必须指向非占位的实际记录；核实声明不能代替来源")

    evidence_type = raw["evidence_type"]
    if evidence_type not in EVIDENCE_TYPES:
        _fail("EVIDENCE_TYPE", f"evidence_type 非法：{evidence_type!r}")

    for key in HEALTH_KEYS:
        if raw.get(key):
            _fail("HEALTH_CLAIM", f"不得把作业事件当健康安全或疾病结论：{key}")
    if evidence_type in {"SYNTHETIC", "IMPLEMENTATION_TEST"}:
        for key in FIELD_CLAIM_KEYS:
            if raw.get(key):
                _fail("SYNTHETIC_AS_FIELD", f"合成或实现测试不得标为现场证据：{key}")

    # 只记已核实的事件：verification.state 必须为 verified。
    verification = raw.get("verification")
    if not isinstance(verification, dict):
        raise UnverifiedEventError("UNVERIFIED_EVENT", "缺 verification 块；未核实不得写入")
    state = verification.get("state")
    if state not in VERIFICATION_STATES:
        raise UnverifiedEventError("UNVERIFIED_EVENT", f"verification.state 非法：{state!r}")
    if state != "verified":
        raise UnverifiedEventError(
            "UNVERIFIED_EVENT",
            f"verification.state={state!r}：只记已核实的事件，未反馈保持未知",
        )
    _text(verification.get("method"), "verification.method")
    if verification.get("method") == "field_observation" and evidence_type != "FIELD_OBSERVATION":
        _fail("SYNTHETIC_AS_FIELD", "verification.method 声称为现场观察，但 evidence_type 不是 FIELD_OBSERVATION")
    _text(verification.get("verified_by_role"), "verification.verified_by_role")
    _parse_instant(verification.get("verified_at"), "verification.verified_at")

    timezone = _text(raw.get("timezone"), "timezone")
    try:
        ZoneInfo(timezone)
    except Exception:
        _fail("MISSING_TIMEZONE", f"未知时区：{timezone!r}")

    clock_source = raw.get("clock_source")
    if clock_source in FORBIDDEN_CLOCK_SOURCES:
        _fail("COPIED_FROM_PLAN_CLOCK", "钟表不得抄自计划：clock_source=copied_from_plan")
    if clock_source not in CLOCK_SOURCES:
        _fail("PLAN_AS_ACTUAL", f"clock_source 非法或来自计划：{clock_source!r}")

    start = _parse_instant(raw.get("actual_start"), "actual_start")
    end = _parse_instant(raw.get("actual_end"), "actual_end")
    if end <= start:
        _fail("CLOCK_INCONSISTENT", "actual_end 须晚于 actual_start")
    span_minutes = (end - start).total_seconds() / 60.0
    clock_minutes = _finite_number(raw.get("clock_minutes_occurred"), "clock_minutes_occurred", minimum=0.0)
    if abs(clock_minutes - span_minutes) > EPS:
        _fail("CLOCK_INCONSISTENT", f"clock_minutes_occurred={clock_minutes} 与起止差 {span_minutes} 不一致")

    phases = raw.get("phases", [])
    if not isinstance(phases, list):
        _fail("PHASE_KIND", "phases 须为数组")
    phases_separable = raw.get("phases_separable")
    if not isinstance(phases_separable, bool):
        _fail("PHASES_SPLIT_FROM_TOTAL", "phases_separable 须显式给出 true/false")

    net = raw.get("net_work_minutes")
    active = raw.get("active_minutes_occurred")
    if active is not None:
        _finite_number(active, "active_minutes_occurred", minimum=0.0)
    if phases_separable:
        if not phases:
            _fail("PHASES_SPLIT_FROM_TOTAL", "phases_separable=true 但 phases 为空")
        expected_net = _phase_minutes(phases, "work", "phases")
        if net is None:
            _fail("PHASES_SPLIT_FROM_TOTAL", "可分辨阶段时须给出 net_work_minutes")
        net_value = _finite_number(net, "net_work_minutes", minimum=0.0)
        if abs(net_value - expected_net) > EPS:
            _fail("PHASES_SPLIT_FROM_TOTAL", f"net_work_minutes={net_value} 与 work 阶段合计 {expected_net} 不一致")
        cursor, expected_active = start, 0.0
        for index, phase in enumerate(phases):
            lo = _parse_instant(phase.get("start"), f"phases[{index}].start")
            hi = _parse_instant(phase.get("end"), f"phases[{index}].end")
            if lo != cursor or hi <= lo or hi > end:
                _fail("CLOCK_INCONSISTENT", "阶段必须连续、不重叠且完全落在实际起止内")
            if phase.get("kind") not in {"setup", "outbound", "work", "cleanup", "return", "buffer", "rest", "wait"}:
                _fail("PHASE_KIND", "未知阶段不能估算成劳动或休息")
            if phase["kind"] not in {"rest", "wait"}:
                expected_active += (hi - lo).total_seconds() / 60
            cursor = hi
        if cursor != end:
            _fail("CLOCK_INCONSISTENT", "阶段未覆盖实际结束时间")
        if active is not None and abs(active - expected_active) > EPS:
            _fail("CLOCK_INCONSISTENT", "active_minutes_occurred 与实际活动阶段合计不一致")
    else:
        if net is not None:
            _fail("PHASES_SPLIT_FROM_TOTAL", "不能分辨阶段时 net_work_minutes 必须为 null，禁止按总耗时拆分")
        if phases:
            _fail("PHASES_SPLIT_FROM_TOTAL", "phases_separable=false 却给出 phases")

    status = raw["status"]
    if status not in EVENT_STATUSES:
        _fail("STATUS_QUANTITY_CONFLICT", f"status 非法：{status!r}")

    quantity_derivation = raw.get("quantity_derivation", "independent_observation")
    if quantity_derivation in FORBIDDEN_QUANTITY_DERIVATIONS:
        _fail("SCHEDULED_AS_COMPLETED", "完成量不得抄自安排：quantity_derivation=copied_from_schedule")
    if quantity_derivation not in QUANTITY_DERIVATIONS:
        _fail("SCHEDULED_AS_COMPLETED", f"quantity_derivation 非法：{quantity_derivation!r}")

    quantity = raw.get("completed_quantity")
    explicit_zero = bool(raw.get("explicit_zero", False))
    if status == "completed":
        if quantity is None:
            _fail("STATUS_QUANTITY_CONFLICT", "completed 须给出正完成量")
        if canonical_amount(quantity, "completed_quantity")[0] <= 0.0:
            _fail("STATUS_QUANTITY_CONFLICT", "completed 完成量必须为正；零不是完成")
    elif status == "partial":
        if quantity is None or canonical_amount(quantity, "completed_quantity")[0] <= 0.0:
            _fail("STATUS_QUANTITY_CONFLICT", "partial 须给出正完成量")
    elif status == "not_done":
        if quantity is None or canonical_amount(quantity, "completed_quantity")[0] != 0.0:
            _fail("STATUS_QUANTITY_CONFLICT", "not_done 完成量必须为 0")
        if not explicit_zero:
            _fail("MISSING_AS_ZERO", "not_done 须显式 explicit_zero=true 作为零证明")
    elif status == "interrupted":
        interrupts = raw.get("interrupt_events")
        if not isinstance(interrupts, list) or not interrupts:
            _fail("INTERRUPT_EVENTS_MISSING", "interrupted 必须保留中断事件")
    elif status == "unknown":
        if quantity is not None:
            _fail("STATUS_QUANTITY_CONFLICT", "unknown 完成量必须为 null，不得写 0")
        explicit_zero = False

    normalized = deepcopy(raw)
    normalized["kind"] = "work_event"
    normalized["contract_id"] = CONTRACT_ID
    normalized["contract_version"] = CONTRACT_VERSION
    normalized["phases_separable"] = phases_separable
    normalized["explicit_zero"] = explicit_zero
    normalized["completed_quantity"] = deepcopy(quantity)
    normalized["quantity_derivation"] = quantity_derivation
    normalized["clock_source"] = clock_source
    normalized.setdefault("linked_suggestion_id", None)
    normalized.setdefault("plot_id", None)
    normalized.setdefault("remaining_after", None)
    normalized.setdefault("interrupt_events", [])
    normalized.setdefault("treat_as_health_outcome", False)
    normalized["health_or_safety_verified"] = False
    return normalized


class WorkEventStore:
    """追加式作业事件存储 + 查询。

    - 只接受通过 validate_work_event 的已核实事件；未核实抛 UnverifiedEventError。
    - 重复 event_id 抛 DuplicateEventError（不覆盖）。
    - 查询：无已核实事件时返回 unknown(None)，绝不返回 0。
    """

    def __init__(self):
        self._events: dict[str, dict] = {}
        self._order: list[str] = []
        self._suggestions: dict[str, dict] = {}

    # ---- 写入：只记已核实的事件 ----------------------------------------
    def record(self, raw: dict) -> dict:
        event = validate_work_event(raw)
        event_id = event["event_id"]
        if event_id in self._events:
            raise DuplicateEventError("DUPLICATE_EVENT", f"event_id 重复：{event_id}")
        linked = event.get("linked_suggestion_id")
        if linked is not None:
            info = self._suggestions.get(linked)
            if info is None:
                raise LifecycleError("UNKNOWN_SUGGESTION_REF", f"未登记的建议：{linked}")
            if info["stage"] not in _EXECUTED_FROM:
                raise LifecycleError(
                    "EXECUTION_WITHOUT_EXPOSURE",
                    f"建议 {linked} 处于 {info['stage']}，未出现已看到/已理解，不能记已执行",
                )
            if event["status"] in {"completed", "partial", "interrupted"} and event["completed_quantity"] is not None and canonical_amount(event["completed_quantity"], "completed_quantity")[0] > 0:
                info["stage"] = STAGE_EXECUTED
                info["executed_at"] = event["actual_end"]
                info["executed_event_id"] = event_id
        self._events[event_id] = event
        self._order.append(event_id)
        return deepcopy(event)

    def record_all(self, raws) -> list[dict]:
        out = []
        for raw in raws:
            out.append(self.record(raw))
        return out

    # ---- 生命周期：区分计划/已看到/已理解/已执行 -----------------------
    def register_suggestion(self, suggestion_id: str, *, plan_id=None,
                            request_sha256=None, at=None) -> dict:
        _text(suggestion_id, "suggestion_id")
        if suggestion_id in self._suggestions:
            raise LifecycleError("DUPLICATE_SUGGESTION", f"建议已登记：{suggestion_id}")
        if request_sha256 is not None and not HASH_RE.match(str(request_sha256)):
            _fail("BAD_HASH", "request_sha256 须为 64 位 hex")
        info = {
            "suggestion_id": suggestion_id,
            "plan_id": plan_id,
            "request_sha256": request_sha256,
            "stage": STAGE_PLANNED,
            "stage_at": at,
            "exposed_at": None,
            "exposed_to_roles": [],
            "executed_at": None,
            "executed_event_id": None,
        }
        self._suggestions[suggestion_id] = info
        return dict(info)

    def _advance(self, suggestion_id: str, target: str, *, at=None, roles=None) -> dict:
        info = self._suggestions.get(suggestion_id)
        if info is None:
            raise LifecycleError("UNKNOWN_SUGGESTION_REF", f"未登记的建议：{suggestion_id}")
        current = info["stage"]
        if target == STAGE_EXECUTED:
            raise LifecycleError("EXECUTED_NEEDS_EVENT", "已执行只能由已核实作业事件推进，不能直接标记")
        expected = _STAGE_EDGE.get(current)
        if expected != target:
            raise LifecycleError(
                "STAGE_ORDER",
                f"建议 {suggestion_id} 当前 {current}，不能直接到 {target}（须 {expected}）",
            )
        info["stage"] = target
        info["stage_at"] = at
        if target in {STAGE_SEEN, STAGE_UNDERSTOOD}:
            if info["exposed_at"] is None:
                info["exposed_at"] = at
            if roles:
                for role in roles:
                    if role not in info["exposed_to_roles"]:
                        info["exposed_to_roles"].append(role)
        return dict(info)

    def mark_seen(self, suggestion_id: str, *, at=None, roles=None) -> dict:
        return self._advance(suggestion_id, STAGE_SEEN, at=at, roles=roles)

    def mark_understood(self, suggestion_id: str, *, at=None, roles=None) -> dict:
        return self._advance(suggestion_id, STAGE_UNDERSTOOD, at=at, roles=roles)

    def stage(self, suggestion_id: str) -> str:
        info = self._suggestions.get(suggestion_id)
        return info["stage"] if info else STAGE_UNKNOWN

    def lifecycle(self, suggestion_id: str) -> dict:
        info = self._suggestions.get(suggestion_id)
        if info is None:
            return {"suggestion_id": suggestion_id, "stage": STAGE_UNKNOWN,
                    "note": "无反馈或未登记：保持未知，不得当已看到/已理解/已执行"}
        return dict(info)

    def analysis_group(self, suggestion_id: str) -> str:
        """影子组：未暴露保持 shadow_unexposed；一旦暴露不得退回。"""
        stage = self.stage(suggestion_id)
        return "shadow_unexposed" if stage in {STAGE_UNKNOWN, STAGE_PLANNED} else "exposed"

    # ---- 查询 ----------------------------------------------------------
    def events(self, *, worker_id=None, task_id=None, plot_id=None, status=None) -> list[dict]:
        out = []
        for event_id in self._order:
            event = self._events[event_id]
            if worker_id is not None and event["worker_id"] != worker_id:
                continue
            if task_id is not None and event["task_id"] != task_id:
                continue
            if plot_id is not None and event.get("plot_id") != plot_id:
                continue
            if status is not None and event["status"] != status:
                continue
            out.append(deepcopy(event))
        return sorted(out, key=lambda e: (e["actual_start"], e["event_id"]))

    def _civil_date(self, event: dict) -> date:
        stamp = _parse_instant(event["actual_start"], "actual_start")
        return stamp.astimezone(ZoneInfo(event["timezone"])).date()

    def occurred_load(self, worker_id: str, civil_date: str) -> dict:
        """按民用日合计已发生主动分钟。无事件=unknown；有 null 分钟=unknown；显式零=recorded_zero。"""
        if not DATE_RE.match(str(civil_date)):
            _fail("BAD_DATE", f"civil_date 须为 YYYY-MM-DD：{civil_date!r}")
        group = [e for e in self.events(worker_id=worker_id)
                 if self._civil_date(e).isoformat() == civil_date]
        base = {
            "kind": "occurred_load",
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "worker_id": worker_id,
            "civil_date": civil_date,
            "unit": "minutes",
            "maps_to_engine_field": "workers[].used_active_minutes_by_date",
            "must_not_copy_from": "suggestion.scheduled_sessions[].active_minutes_by_date",
            "event_ids": [e["event_id"] for e in group],
        }
        if not group:
            base.update({"status": "unknown", "active_minutes": None,
                         "load_derivation": "unknown",
                         "replan_gate": "HOLD_unknown_load",
                         "note": "无已核实作业事件：保持 unknown，不得当 0。"})
            return base
        if any(e.get("active_minutes_occurred") is None for e in group):
            base.update({"status": "unknown", "active_minutes": None,
                         "load_derivation": "unknown",
                         "replan_gate": "HOLD_unknown_load",
                         "note": "存在未给出分钟的已核实事件：合计保持 unknown，不得当 0。"})
            return base
        total = sum(float(e["active_minutes_occurred"]) for e in group)
        if total > 0.0:
            base.update({"status": "recorded_positive", "active_minutes": total,
                         "load_derivation": "sum_of_events", "replan_gate": "allow_replan"})
        else:
            base.update({"status": "recorded_zero", "active_minutes": 0.0,
                         "load_derivation": "explicit_zero", "replan_gate": "allow_replan"})
        return base

    def occurred_active_minutes(self, worker_id: str, civil_date: str):
        """返回合计主动分钟；未知返回 None（不是 0）。"""
        return self.occurred_load(worker_id, civil_date)["active_minutes"]

    def completed_total(self, *, worker_id=None, task_id=None) -> dict:
        """合计已核实完成量。任一事件完成量未知则整体 unknown；单位混用报错。"""
        group = self.events(worker_id=worker_id, task_id=task_id)
        group = [e for e in group if e["status"] in {"completed", "partial", "interrupted"}]
        if not group:
            return {"status": "unknown", "value": None, "unit": None,
                    "note": "无已核实的完成量记录：保持 unknown，不得当 0。"}
        if any(e.get("completed_quantity") is None for e in group):
            return {"status": "unknown", "value": None, "unit": None,
                    "note": "存在完成量未知的已核实事件：合计保持 unknown，不得按 0 累加。"}
        units = {canonical_amount(e["completed_quantity"], "completed_quantity")[1] for e in group}
        if len(units) != 1:
            _fail("UNIT_MISMATCH", f"完成量单位混用，不能相加：{sorted(units)}")
        unit = units.pop()
        total = sum(canonical_amount(e["completed_quantity"], "completed_quantity")[0] for e in group)
        return {"status": "recorded_positive" if total > 0 else "recorded_zero",
                "value": total, "unit": unit}

    def summary(self) -> dict:
        return {
            "kind": "work_event_store",
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "contract_status": CONTRACT_STATUS,
            "event_count": len(self._order),
            "suggestion_count": len(self._suggestions),
            "stages": {sid: info["stage"] for sid, info in sorted(self._suggestions.items())},
            "note": "仅含已核实事件；未反馈保持 unknown。不是现场效果或健康结论。",
        }

    # ---- 序列化（追加式；null 与 0 分开保存） ---------------------------
    def to_dict(self) -> dict:
        return {
            "kind": "work_event_store",
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "contract_status": CONTRACT_STATUS,
            "schema_version": SCHEMA_VERSION,
            "events": [self._events[eid] for eid in sorted(self._events)],
            "suggestions": [self._suggestions[sid] for sid in sorted(self._suggestions)],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkEventStore":
        if not isinstance(payload, dict) or payload.get("kind") != "work_event_store":
            _fail("UNKNOWN_KIND", "存储文件须为 work_event_store 对象")
        store = cls()
        for info in payload.get("suggestions", []):
            _text(info.get("suggestion_id"), "suggestions[].suggestion_id")
            store._suggestions[info["suggestion_id"]] = dict(info)
        for event in payload.get("events", []):
            eid = event.get("event_id")
            if eid in store._events:
                raise DuplicateEventError("DUPLICATE_EVENT", f"event_id 重复：{eid}")
            store._events[eid] = event
            store._order.append(eid)
        return store

    def save(self, path) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        Path(path).write_text(text, encoding="utf-8")
        return text

    @classmethod
    def load(cls, path) -> "WorkEventStore":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---- 自包含命令行（不改共享 cli.py） -----------------------------------
def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="yilao_agri.work_events",
                                     description="实际作业事件存储与查询（只记已核实的事件）")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="把已核实事件写入存储；未核实拒绝")
    rec.add_argument("--store", required=True)
    rec.add_argument("--events", required=True, help="事件 JSON（对象或数组）")
    rec.add_argument("--suggestion", action="append", default=[],
                     help="先登记建议（planned），可重复")

    mk = sub.add_parser("mark", help="推进建议生命周期 seen/understood；executed 只能由已核实事件推进")
    mk.add_argument("--store", required=True)
    mk.add_argument("--suggestion", required=True)
    mk.add_argument("--stage", required=True, choices=["seen", "understood"])
    mk.add_argument("--at")
    mk.add_argument("--role", action="append", default=[])

    q = sub.add_parser("query", help="查询存储；未知返回 null，不是 0")
    q.add_argument("--store", required=True)
    q.add_argument("--action", required=True,
                   choices=["summary", "events", "occurred", "completed", "lifecycle"])
    q.add_argument("--worker")
    q.add_argument("--task")
    q.add_argument("--plot")
    q.add_argument("--status")
    q.add_argument("--date")
    q.add_argument("--suggestion")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            store = WorkEventStore.load(args.store) if Path(args.store).exists() else WorkEventStore()
            for sid in args.suggestion:
                if sid not in store._suggestions:
                    store.register_suggestion(sid)
            payload = json.loads(Path(args.events).read_text(encoding="utf-8"))
            raws = payload if isinstance(payload, list) else [payload]
            recorded = store.record_all(raws)
            store.save(args.store)
            _print({"recorded": [e["event_id"] for e in recorded], "store": args.store})
            return 0
        if args.command == "mark":
            store = WorkEventStore.load(args.store) if Path(args.store).exists() else WorkEventStore()
            sid = args.suggestion
            if sid not in store._suggestions:
                store.register_suggestion(sid, at=args.at)
            roles = args.role or None
            if args.stage == "seen":
                info = store.mark_seen(sid, at=args.at, roles=roles)
            else:
                info = store.mark_understood(sid, at=args.at, roles=roles)
            store.save(args.store)
            _print(info)
            return 0
        store = WorkEventStore.load(args.store)
        if args.action == "summary":
            result = store.summary()
        elif args.action == "events":
            result = {"events": store.events(worker_id=args.worker, task_id=args.task,
                                             plot_id=args.plot, status=args.status)}
        elif args.action == "occurred":
            if not args.worker or not args.date:
                _fail("MISSING_FIELD", "occurred 需要 --worker 与 --date")
            result = store.occurred_load(args.worker, args.date)
        elif args.action == "completed":
            result = store.completed_total(worker_id=args.worker, task_id=args.task)
        else:
            if not args.suggestion:
                _fail("MISSING_FIELD", "lifecycle 需要 --suggestion")
            result = store.lifecycle(args.suggestion)
        _print(result)
        return 0
    except WorkEventError as exc:
        _print({"error": exc.code, "message": exc.message})
        return 2
    except (OSError, ValueError) as exc:
        _print({"error": "ERROR", "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
