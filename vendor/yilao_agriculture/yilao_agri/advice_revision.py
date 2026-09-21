"""天气变化重排的建议版本台账（schema 1.0）。

定位（R17-A04｜天气变化重排）：
- **新预报 -> 新天气修订 -> 重排 -> 新的建议版本**；旧版本原样保留，用 ``supersedes`` /
  ``superseded_by`` 链接，绝不原地改写已生成的建议记录。
- **不能悄悄改写已展示建议**：已 ``displayed`` 的建议一旦被新的天气修订改变，必须产出新版本
  并置 ``requires_redisplay=True``；调用方需显式决定是否再次展示，本模块不自动重发。
- **同一天气修订不得产生不同建议**（默认 ``require_new_forecast=True``）：天气未变而建议变了，
  说明是别的输入（人员/资源/约束）在变，属其他卡片职责，本模块直接拒绝
  （``ADVICE_SUPERSEDE_WITHOUT_NEW_FORECAST``），避免把改写伪装成天气更新。
- **迟到预报不得顶替更新的建议**：新修订发布时间不晚于当前建议所依据的修订时间时，拒绝
  supersede（``ADVICE_STALE_FORECAST``）；旧修订仍由归档保留、可单独回放。
- **只按已核实的预报成员评估**：占位/空来源（``is_placeholder_source``）直接拒绝
  （``ADVICE_FORECAST_SOURCE_UNVERIFIED``）；非演示模式拒绝合成天气。

硬约束（不得弱化）：
- 天气修订沿用 :class:`forecast_archive.ForecastArchive` 的追加式身份与哈希校验，
  同一产品身份出现不同内容即拒（``ARCHIVE_PRODUCT_REWRITE``），不覆盖历史证据；
- 重排仍调用 ``engine.plan`` 并逐次执行 ``audit.verify_plan``；复查不通过即拒绝产出建议；
- 每个成员/情境下的硬约束判定与主流程完全一致，不新增放宽；
- 只读：不修改传入请求与计划，全部深拷贝返回；
- 缺真实预报源时关闭路径（``fetch_new_forecast`` 恒返回 ``ADVICE_FORECAST_FETCH_DISABLED``），
  不编造成员、不编造发布时间。

证据标签：SYNTHETIC / IMPLEMENTATION_TEST，``n_real=0``；不得写成现场效果或健康结论，
也不得把建议版本变化写成个人健康安全或天气概率。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any

from .audit import verify_plan
from .engine import plan
from .forecast_archive import ForecastArchive, replay_into_request
from .models import is_placeholder_source, parse_time

SCHEMA_VERSION = "1.0"
LEDGER_KIND = "advice_revision_ledger"
EVIDENCE_TYPE = "SYNTHETIC"
EVIDENCE_LABEL = "IMPLEMENTATION_TEST"
DISABLED_FETCH_CODE = "ADVICE_FORECAST_FETCH_DISABLED"

_ALLOWED_KINDS = ("forecast", "measured", "synthetic")
_REAL_MODES = ("shadow", "assistance")

_MEANING = (
    "把「已展示建议」绑定到具体的天气修订，并在新预报到达时产出新的建议版本。"
    "changed 只表示建议内容（状态、各任务排入量、出工段集合）是否不同；不表示天气发生概率、"
    "不表示个人健康安全，也不是可行性证明。"
)

_FORBIDDEN_READINGS = [
    "把 changed / 版本号写成天气变化概率或降雨把握",
    "把 requires_redisplay 写成建议已验证有效或健康有害",
    "把空计划版本写成已证明不可行",
    "把 SYNTHETIC / IMPLEMENTATION_TEST 版本台账写成现场实测或已接通天气通",
    "用建议版本数量冒充参与者数量或作业效果",
]


class AdviceError(ValueError):
    """建议版本台账失败；``code`` 为稳定的机器可读原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

def _iso(value: Any, label: str) -> str:
    """规范化为带时区的 ISO 字符串；拒绝无时区或非法时间。"""
    try:
        moment = parse_time(value)
    except Exception as exc:  # InputError 等
        raise AdviceError("ADVICE_TIME_INVALID", f"{label} 无效或缺少时区：{value!r}。") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise AdviceError("ADVICE_TIME_INVALID", f"{label} 缺少时区。")
    return moment.isoformat()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AdviceError("ADVICE_PAYLOAD_NOT_JSON",
                          "建议内容必须是可规范化的 JSON（不含 NaN/Infinity）。") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        return round(float(value), 8)
    except (OverflowError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 建议内容归一化与差异
# --------------------------------------------------------------------------- #

def advice_content(plan_result: Any) -> dict:
    """抽出与「已展示建议」有关的决策字段；排除 plan_id/request_sha256 等外发标识。

    只保留状态、各任务排入/剩余量与完成比例、出工段集合。这样同一预报内容在不同外发时刻
    重排会得到同一内容哈希，不会因为 plan_id 变化而误判建议已改写。
    """
    if not isinstance(plan_result, dict):
        raise AdviceError("ADVICE_PLAN_INVALID", "计划必须是对象。")
    tasks = []
    for row in plan_result.get("task_results") or []:
        if not isinstance(row, dict):
            continue
        tasks.append({
            "task_id": row.get("task_id"),
            "status": row.get("status"),
            "scheduled_quantity": _number(row.get("scheduled_quantity")),
            "remaining_quantity": _number(row.get("remaining_quantity")),
            "completion_ratio": _number(row.get("completion_ratio")),
            "unit": row.get("unit"),
        })
    tasks.sort(key=lambda item: (str(item["task_id"])))
    sessions = []
    for session in plan_result.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        sessions.append({
            "worker_id": session.get("worker_id"),
            "task_id": session.get("task_id"),
            "start": session.get("start"),
            "end": session.get("end"),
            "quantity": _number(session.get("quantity")),
            "unit": session.get("unit"),
        })
    sessions.sort(key=lambda item: (str(item["start"]), str(item["worker_id"]), str(item["task_id"])))
    return {"status": plan_result.get("status"), "tasks": tasks, "sessions": sessions}


def content_digest(content: Any) -> str:
    return _digest(content)


def advice_id_for(plot_id: str, content: dict) -> str:
    """内容寻址：同一地块 + 同一建议内容 => 同一 advice_id（与天气修订发布时间无关）。"""
    return "adv-" + _digest({"plot_id": plot_id, "advice": content})[:16]


def diff_content(old: dict, new: dict) -> dict:
    """比较两份归一化建议内容，给出变化种类；不判断好坏、不判断可行性。"""
    kinds: list[str] = []
    if old.get("status") != new.get("status"):
        kinds.append("status_changed")

    def _session_keys(content):
        return {(s["worker_id"], s["task_id"], s["start"], s["end"], s["quantity"], s["unit"])
                for s in content.get("sessions") or []}

    added = _session_keys(new) - _session_keys(old)
    removed = _session_keys(old) - _session_keys(new)
    if added and removed:
        kinds.append("sessions_rescheduled")
    elif added:
        kinds.append("sessions_added")
    elif removed:
        kinds.append("sessions_removed")

    old_tasks = {str(t["task_id"]): t for t in old.get("tasks") or []}
    new_tasks = {str(t["task_id"]): t for t in new.get("tasks") or []}
    for task_id in sorted(set(old_tasks) | set(new_tasks)):
        before, after = old_tasks.get(task_id), new_tasks.get(task_id)
        if before is None or after is None:
            kinds.append("task_set_changed")
            continue
        if (before["scheduled_quantity"], before["completion_ratio"]) != \
           (after["scheduled_quantity"], after["completion_ratio"]):
            kinds.append("task_quantity_changed")
        if before["status"] == "complete" and after["status"] != "complete":
            kinds.append("task_newly_blocked")
        if before["status"] != "complete" and after["status"] == "complete":
            kinds.append("task_newly_complete")
    if not kinds and old != new:
        kinds.append("other_change")
    return {
        "changed": bool(kinds),
        "change_kinds": sorted(set(kinds)),
        "old_digest": _digest(old),
        "new_digest": _digest(new),
    }


def diff_advice(old_plan: Any, new_plan: Any) -> dict:
    return diff_content(advice_content(old_plan), advice_content(new_plan))


# --------------------------------------------------------------------------- #
# 来源核实（只按已核实的预报成员评估）
# --------------------------------------------------------------------------- #

def verify_weather_source(source: Any, *, kind: Any = None, mode: Any = None) -> bool:
    """占位/空来源一律拒绝；非演示模式拒绝合成天气。只核对声明，不认证数据质量。"""
    if is_placeholder_source(source) or not isinstance(source, str) or not source.strip():
        raise AdviceError("ADVICE_FORECAST_SOURCE_UNVERIFIED",
                          f"预报来源为占位或空值（{source!r}），不能据以生成建议版本。")
    if kind is not None and kind not in _ALLOWED_KINDS:
        raise AdviceError("ADVICE_FORECAST_KIND_UNVERIFIED",
                          f"天气类型未知（{kind!r}），不能据以生成建议版本。")
    if kind == "synthetic" and mode in _REAL_MODES:
        raise AdviceError("ADVICE_FORECAST_KIND_UNVERIFIED",
                          "非演示模式不能使用合成天气生成建议版本。")
    return True


# --------------------------------------------------------------------------- #
# 建议版本台账
# --------------------------------------------------------------------------- #

class AdviceLedger:
    """按地块追加式保存建议版本；已写入的版本记录不被后续更新改动。

    - ``_entries``：地块 -> 版本记录（不可变，含 ``advice_sha256``）；
    - ``_superseded_by`` / ``_displayed``：可变状态放在记录之外，保证建议内容哈希稳定。
    """

    def __init__(self) -> None:
        self._entries: dict[str, list[dict]] = {}
        self._by_id: dict[str, dict] = {}
        self._superseded_by: dict[str, str] = {}
        self._displayed: dict[str, str] = {}

    # -- 渲染（把外部状态叠加到不可变记录上） -------------------------------- #
    def _render(self, entry: dict) -> dict:
        rendered = deepcopy(entry)
        rendered["superseded_by"] = self._superseded_by.get(entry["advice_id"])
        rendered["displayed"] = entry["advice_id"] in self._displayed
        rendered["displayed_at"] = self._displayed.get(entry["advice_id"])
        return rendered

    # -- 写入 --------------------------------------------------------------- #
    def append(
        self,
        plot_id: str,
        *,
        weather_revision_id: str,
        weather_issued_at: Any,
        weather_source: Any,
        decision_at: Any,
        plan_result: Any,
        change_kinds: Any = None,
        require_new_forecast: bool = True,
    ) -> dict:
        if not isinstance(plot_id, str) or not plot_id.strip():
            raise AdviceError("ADVICE_PLOT_UNKNOWN", "plot_id 必须为非空字符串。")
        if not isinstance(weather_revision_id, str) or not weather_revision_id.strip():
            raise AdviceError("ADVICE_REVISION_UNKNOWN", "weather_revision_id 必须为非空字符串。")
        verify_weather_source(weather_source)
        decision = _iso(decision_at, "decision_at")
        issued = _iso(weather_issued_at, "weather_issued_at")
        content = advice_content(plan_result)
        digest = content_digest(content)
        advice_id = advice_id_for(plot_id, content)

        history = self._entries.get(plot_id, [])
        previous = history[-1] if history else None

        if previous is not None:
            if decision < previous["decision_at"]:
                raise AdviceError("ADVICE_DECISION_OUT_OF_ORDER",
                                  "决策时点早于已记录建议；不得回填或重排历史建议。")
            same_revision = previous["weather_revision_id"] == weather_revision_id
            if same_revision and previous["plan_digest"] != digest and require_new_forecast:
                raise AdviceError(
                    "ADVICE_SUPERSEDE_WITHOUT_NEW_FORECAST",
                    "天气修订未变却要替换建议内容；请改用新预报形成新修订，"
                    "或显式声明这是非天气来源（人员/资源/约束）的变更。",
                )
            if not same_revision and issued <= previous["weather_issued_at"]:
                raise AdviceError(
                    "ADVICE_STALE_FORECAST",
                    "新修订发布时间不晚于当前建议所依据的修订，不得顶替更新的已展示建议。",
                )
            if previous["plan_digest"] == digest:
                # 新预报但建议内容未变：不产生新版本，保留当前建议（不虚增版本）。
                return self._render(previous)

        record = {
            "advice_schema_version": SCHEMA_VERSION,
            "ledger_kind": LEDGER_KIND,
            "advice_seq": (previous["advice_seq"] if previous else 0) + 1,
            "advice_id": advice_id,
            "plot_id": plot_id,
            "weather_revision_id": weather_revision_id,
            "weather_issued_at": issued,
            "weather_source": weather_source,
            "decision_at": decision,
            "plan_id": plan_result.get("plan_id") if isinstance(plan_result, dict) else None,
            "status": content["status"],
            "plan_digest": digest,
            "content": deepcopy(content),
            "supersedes": previous["advice_id"] if previous else None,
            "change_kinds": sorted(set(change_kinds or [])),
            "evidence_type": EVIDENCE_TYPE,
            "evidence_label": EVIDENCE_LABEL,
            "n_real": 0,
            "probability_claim": None,
            "probability_scheduling_status": "HOLD",
        }
        record["advice_sha256"] = _record_sha(record)
        self._entries.setdefault(plot_id, []).append(record)
        self._by_id[advice_id] = record
        if previous is not None:
            self._superseded_by[previous["advice_id"]] = advice_id
        return self._render(record)

    # -- 标记首次展示 -------------------------------------------------------- #
    def mark_displayed(self, advice_id: str, displayed_at: Any) -> dict:
        entry = self._by_id.get(advice_id)
        if entry is None:
            raise AdviceError("ADVICE_ID_UNKNOWN", f"未知 advice_id：{advice_id!r}。")
        if advice_id in self._superseded_by:
            raise AdviceError(
                "ADVICE_SUPERSEDED_NOT_DISPLAYABLE",
                "该版本已被更晚的建议取代；不得把它当作当前建议展示，请展示当前版本。",
            )
        moment = _iso(displayed_at, "displayed_at")
        # 首次展示为准：重复标记不覆盖第一次暴露时刻。
        self._displayed.setdefault(advice_id, moment)
        return self._render(entry)

    # -- 读取 --------------------------------------------------------------- #
    def current(self, plot_id: str) -> dict | None:
        history = self._entries.get(plot_id)
        if not history:
            return None
        return self._render(history[-1])

    def history(self, plot_id: str) -> tuple[dict, ...]:
        return tuple(self._render(entry) for entry in self._entries.get(plot_id, ()))

    def entry(self, advice_id: str) -> dict | None:
        found = self._by_id.get(advice_id)
        return self._render(found) if found is not None else None

    def advice_count(self) -> int:
        return len(self._by_id)

    # -- 校验 / 持久化 ------------------------------------------------------- #
    def verify_all(self) -> dict:
        issues: list[dict] = []
        for plot_id, entries in self._entries.items():
            for index, entry in enumerate(entries):
                if entry.get("advice_sha256") != _record_sha(entry):
                    issues.append({"plot_id": plot_id, "advice_id": entry.get("advice_id"),
                                   "error": "advice_sha256 与条目不一致：条目已被改动"})
                    continue
                expected = advice_id_for(plot_id, entry.get("content") or {})
                if entry.get("advice_id") != expected:
                    issues.append({"plot_id": plot_id, "advice_id": entry.get("advice_id"),
                                   "error": "advice_id 与内容不一致"})
                expected_supersedes = entries[index - 1]["advice_id"] if index else None
                if entry.get("supersedes") != expected_supersedes:
                    issues.append({"plot_id": plot_id, "advice_id": entry.get("advice_id"),
                                   "error": "supersedes 链与序列不一致"})
                if entry.get("plot_id") != plot_id:
                    issues.append({"plot_id": plot_id, "advice_id": entry.get("advice_id"),
                                   "error": "plot_id 与所在序列不一致"})
        for old, new in self._superseded_by.items():
            if old not in self._by_id or new not in self._by_id:
                issues.append({"advice_id": old, "error": "superseded_by 指向不存在的版本"})
        for advice_id in self._displayed:
            if advice_id not in self._by_id:
                issues.append({"advice_id": advice_id, "error": "displayed 指向不存在的版本"})
        return {"valid": not issues, "advice_count": len(self._by_id), "issues": issues}

    def to_json(self) -> str:
        rows = [entry for entries in self._entries.values() for entry in entries]
        return json.dumps({
            "ledger_schema_version": SCHEMA_VERSION,
            "ledger_kind": LEDGER_KIND,
            "entries": rows,
            "displayed": dict(self._displayed),
            "superseded_by": dict(self._superseded_by),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "AdviceLedger":
        data = json.loads(text)
        ledger = cls()
        for entry in data.get("entries", []):
            if entry.get("advice_sha256") != _record_sha(entry):
                raise AdviceError("ADVICE_LOAD_TAMPERED", "载入的建议条目哈希不一致。")
            ledger._entries.setdefault(entry["plot_id"], []).append(entry)
            ledger._by_id[entry["advice_id"]] = entry
        ledger._displayed = dict(data.get("displayed") or {})
        ledger._superseded_by = dict(data.get("superseded_by") or {})
        check = ledger.verify_all()
        if not check["valid"]:
            raise AdviceError("ADVICE_LOAD_TAMPERED", "载入的台账未通过一致性校验。")
        return ledger


def _record_sha(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "advice_sha256"}
    return _digest(body)


# --------------------------------------------------------------------------- #
# 重排入口
# --------------------------------------------------------------------------- #

def replan_advice(
    request: Any,
    weather: Any,
    *,
    plot_id: str,
    archive: ForecastArchive,
    ledger: AdviceLedger,
    decision_at: Any,
    retrieved_at: Any = None,
    replan=None,
    verify=None,
) -> dict:
    """按一份新预报重排并登记建议版本；旧版本保留，已展示建议不会被悄悄改写。

    步骤：核实来源 -> 归档新天气修订（追加式、拒绝改写）-> 回放进请求副本 ->
    ``engine.plan`` -> ``audit.verify_plan`` -> 与当前建议比较 -> 登记新版本（如有变化）。
    任一步失败即抛出，不产生半成品建议。
    """
    replan = replan or plan
    verify = verify or verify_plan
    if not isinstance(archive, ForecastArchive):
        raise AdviceError("ADVICE_ARCHIVE_INVALID", "archive 必须是 ForecastArchive。")
    if not isinstance(ledger, AdviceLedger):
        raise AdviceError("ADVICE_LEDGER_INVALID", "ledger 必须是 AdviceLedger。")
    if not isinstance(weather, dict):
        raise AdviceError("ADVICE_WEATHER_INVALID", "新天气必须是对象。")
    mode = request.get("mode") if isinstance(request, dict) else None
    verify_weather_source(weather.get("source"), kind=weather.get("kind"), mode=mode)

    entry = archive.append(plot_id, weather, decision_at=decision_at, retrieved_at=retrieved_at)
    cloned = replay_into_request(entry, request)
    result = replan(cloned)
    check = verify(cloned, result)
    if not check.get("valid"):
        raise AdviceError("ADVICE_PLAN_INVALID",
                          "重排结果未通过独立复查：" + "；".join(check.get("errors", [])))

    previous = ledger.current(plot_id)
    change = (diff_content(previous["content"], advice_content(result))
              if previous is not None else
              {"changed": True, "change_kinds": ["initial_advice"],
               "old_digest": None, "new_digest": content_digest(advice_content(result))})
    new_record = ledger.append(
        plot_id,
        weather_revision_id=entry["revision_id"],
        weather_issued_at=entry["product"]["issued_at"],
        weather_source=weather.get("source"),
        decision_at=decision_at,
        plan_result=result,
        change_kinds=change["change_kinds"],
    )
    changed = previous is None or previous["advice_id"] != new_record["advice_id"]
    superseded = None
    if previous is not None and changed:
        superseded = ledger.entry(previous["advice_id"])
    return {
        "plot_id": plot_id,
        "changed": changed,
        "requires_redisplay": bool(previous is not None and previous["displayed"] and changed),
        "advice": new_record,
        "superseded_advice": superseded,
        "diff": change,
        "weather_revision": {
            "revision_id": entry["revision_id"],
            "issued_at": entry["product"]["issued_at"],
            "late_arrival": entry["late_arrival"],
            "source": entry["product"]["source"],
            "kind": entry["product"]["kind"],
        },
        "plan": result,
        "verification": check,
        "evidence_type": EVIDENCE_TYPE,
        "evidence_label": EVIDENCE_LABEL,
        "n_real": 0,
        "probability_claim": None,
        "probability_scheduling_status": "HOLD",
        "meaning": _MEANING,
        "forbidden_readings": list(_FORBIDDEN_READINGS),
    }


def fetch_new_forecast(*_args, **_kwargs) -> dict:
    """本版不连接任何真实预报源；缺真实成员时返回关闭接口，不编造成员。"""
    return {
        "ok": False,
        "code": DISABLED_FETCH_CODE,
        "message": "本版不连接任何真实预报源；请由调用方提供已核实来源与发布时间的天气修订。",
        "evidence_type": EVIDENCE_TYPE,
        "evidence_label": EVIDENCE_LABEL,
        "n_real": 0,
        "probability_claim": None,
    }


__all__ = [
    "AdviceError", "AdviceLedger", "advice_content", "advice_id_for", "content_digest",
    "diff_advice", "diff_content", "fetch_new_forecast", "replan_advice",
    "verify_weather_source",
]
