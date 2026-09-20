"""多预报成员 / 区间天气情境下的排程稳健检查（schema 1.0）。

定位（R16-A07｜稳健天气情境）：
- 把同一地块的一组**明确标注**的天气情境（成员 member）或**区间带**（interval）作为情境集合；
- 对每个成员，把该地块天气替换成该成员，再检查「已承诺出工」是否仍满足硬约束，或重新求解；
- 只输出「情境通过计数」与逐段原因码，不输出可行性结论。

硬约束（不得弱化）：
- **无概率来源时只称情境**：``probability_claim`` 恒为 ``None``；成员带概率/权重字段却没有
  已确认概率来源时直接拒绝（``MEMBER_PROBABILITY_SOURCE_UNKNOWN``）。
- 即使声明了概率来源，本版仍不做概率排程（可靠度图未完成，能力 HOLD），只保留计数；
  份额、PoP、成员占比一律不得当作都昌校准概率。
- **研究/合成不等于实测**：输出带 ``evidence_type`` 与 ``n_real=0`` 标签；不得写成现场效果。
- **只读**：不修改输入请求与已承诺计划（全部深拷贝）；缺真实成员数据时关闭路径
  （``fetch_ensemble_members`` 返回 ``MEMBER_FETCH_DISABLED``），不编成员。
- 检查仍复用 ``engine.plan`` / ``audit.verify_plan`` / ``environment.assess_interval``，
  不新增放宽：每个成员的硬约束判定与主流程一致。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .audit import verify_plan
from .engine import plan
from .environment import assess_interval
from .models import InputError, is_placeholder_source, parse_time, request_digest, validate_request

SCHEMA_VERSION = "1.0"
MEMBER_SET_KIND = "weather_member_set"
EVIDENCE_TYPE = "SYNTHETIC"
EVIDENCE_LABEL = "IMPLEMENTATION_TEST"
DISABLED_CODE = "MEMBER_FETCH_DISABLED"

MEMBER_KINDS = ("forecast", "measured", "synthetic")
ENVIRONMENTS = ("outdoor", "greenhouse")

# 逐个成员允许的元数据键；记录另按天气记录键校验。
_MEMBER_META_KEYS = frozenset({
    "member_id", "source", "kind", "environment", "issued_at",
    "wbgt_method", "records", "probability", "note",
})
_SET_KEYS = frozenset({
    "kind", "plot_id", "members", "intervals", "template", "probability_source", "note",
})
_TEMPLATE_KEYS = frozenset({"source", "kind", "environment", "issued_at", "wbgt_method", "note"})

# 区间带里可给 [lo, hi] 的数值字段。逆势方位：全部取较大值更保守（更热 / 风更大 / 雨更多）。
_INTERVAL_FIELDS = ("temperature_c", "relative_humidity_pct", "wind_m_s",
                    "shortwave_w_m2", "precipitation_mm")

# 禁止出现在结果对象里的概率字段：键名命中概率词根且值为非空即视为概率声称。
# 这些键名是「显式载体」，允许存在但必须保持 null / HOLD，不视为概率声称。
_PROBABILITY_CARRIER_KEYS = frozenset({
    "probability_claim", "probability_scheduling_status", "probability_source",
})
_PROBABILITY_TOKENS = ("probability", "probabilit", "chance", "likelihood",
                       "confidence", "概率", "把握", "置信")

# 成员显式声明的概率字段名（精确匹配）；用于「无来源即拒绝」。
_FORBIDDEN_PROBABILITY_KEYS = frozenset({
    "probability", "probability_pct", "prob", "chance", "likelihood", "confidence",
    "pop", "weight", "weights", "share", "member_share",
})

_MEANING_COMMITTED = (
    "对已承诺出工逐段检查个别成员/区间情境下是否仍满足硬约束。"
    "allowed_count / blocked_count 是成员内的会话计数，不是发生概率、出工把握或健康安全。"
)
_MEANING_REPLAN = (
    "按各成员情境重新求解（丢弃原时间轴）。空计划只表示该成员下本次搜索未找到安排，"
    "不是不可行证明；成员通过数不得写成概率。"
)

_FORBIDDEN_READINGS = [
    "把 robust_committed_all_allowed / 成员通过数写成计划仍可行的概率或把握",
    "把成员份额、PoP 或 ECMWF 原始占比当作都昌校准概率",
    "把排程成立写成个人健康安全",
    "把 SYNTHETIC / IMPLEMENTATION_TEST 情境检查写成现场实测效果",
    "把某成员下的空计划写成已证明不可行",
]


class MemberSetError(ValueError):
    """成员集校验失败；``code`` 为稳定的机器可读原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise MemberSetError(code, message)


def _time(value: Any, label: str):
    try:
        return parse_time(value)
    except (InputError, TypeError, ValueError, OverflowError) as exc:
        raise MemberSetError("MEMBER_TIME_INVALID", f"{label} 无效或缺少时区：{value!r}。") from exc


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _normalize_probability_source(value: Any) -> dict | None:
    """概率来源：缺省即无；给出时必须可辨识且已确认，否则拒绝。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        _fail("MEMBER_PROBABILITY_SOURCE_UNKNOWN", "probability_source 必须为对象或省略。")
    extra = set(value) - {"source", "review_status", "note"}
    if extra:
        _fail("MEMBER_PROBABILITY_SOURCE_UNKNOWN", f"probability_source 未知字段：{sorted(extra)[0]}。")
    source = value.get("source")
    if not isinstance(source, str) or not source.strip() or is_placeholder_source(source):
        _fail("MEMBER_PROBABILITY_SOURCE_UNKNOWN", "probability_source.source 缺失或为占位，不能支撑概率。")
    if value.get("review_status") != "confirmed":
        _fail("MEMBER_PROBABILITY_SOURCE_UNKNOWN",
              "probability_source.review_status 必须为 confirmed（本环境尚无可靠度图）。")
    return {"source": source, "review_status": "confirmed"}


def _member_probability(raw: dict, prob_source: dict | None, path: str) -> dict | None:
    """成员概率字段：无来源即拒绝；有来源也仅登记并保持 HOLD，绝不进入排程。"""
    declared = {key: raw[key] for key in _FORBIDDEN_PROBABILITY_KEYS
                if key in raw and raw[key] is not None}
    if not declared:
        return None
    if prob_source is None:
        _fail("MEMBER_PROBABILITY_SOURCE_UNKNOWN",
              f"{path} 声明了概率/权重字段 {sorted(declared)}，但未提供已确认概率来源；只允许称情境。")
    return {"declared_keys": sorted(declared), "probability_claim": None,
            "status": "HOLD", "note": "本版不做概率排程（可靠度图未完成）；仅登记，不参与排序。"}


def _validate_records(records: Any, path: str) -> list:
    if not isinstance(records, list) or not records:
        _fail("MEMBER_RECORDS_INVALID", f"{path} 必须为非空数组。")
    out = []
    previous_end = None
    for index, record in enumerate(records):
        where = f"{path}[{index}]"
        if not isinstance(record, dict):
            _fail("MEMBER_RECORDS_INVALID", f"{where} 必须为对象。")
        start = _time(record.get("start"), f"{where}.start")
        end = _time(record.get("end"), f"{where}.end")
        if end <= start:
            _fail("MEMBER_RECORD_INTERVAL_INVALID", f"{where} 区间必须具有正时长。")
        if previous_end is not None and start < previous_end:
            _fail("MEMBER_RECORD_INTERVAL_INVALID", f"{where} 与前一条记录重叠；记录必须不重叠。")
        previous_end = end
        out.append(deepcopy(record))
    return out


def _validate_member_meta(raw: dict, path: str) -> dict:
    extra = set(raw) - _MEMBER_META_KEYS
    if extra:
        _fail("MEMBER_FIELD_UNKNOWN", f"{path} 未知字段：{sorted(extra)[0]}。")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip() or is_placeholder_source(source):
        _fail("MEMBER_SOURCE_UNKNOWN", f"{path}.source 缺失或为占位，不能标识情境来源。")
    kind = raw.get("kind")
    if kind not in MEMBER_KINDS:
        _fail("MEMBER_KIND_UNKNOWN", f"{path}.kind 必须为 {MEMBER_KINDS} 之一。")
    environment = raw.get("environment")
    if environment not in ENVIRONMENTS:
        _fail("MEMBER_ENVIRONMENT_UNKNOWN", f"{path}.environment 必须为 {ENVIRONMENTS} 之一。")
    issued = _time(raw.get("issued_at"), f"{path}.issued_at")
    wbgt_method = raw.get("wbgt_method")
    if wbgt_method is not None and (not isinstance(wbgt_method, str) or not wbgt_method.strip()):
        _fail("MEMBER_WBGT_METHOD_UNKNOWN", f"{path}.wbgt_method 必须为非空字符串或省略。")
    return {"source": source, "kind": kind, "environment": environment,
            "issued_at": issued.isoformat(), "wbgt_method": wbgt_method}


def _validate_members(members: Any, prob_source: dict | None) -> list:
    if not isinstance(members, list) or not members:
        _fail("MEMBER_SET_EMPTY", "members 必须为非空数组。")
    seen: set = set()
    out = []
    for index, member in enumerate(members):
        path = f"members[{index}]"
        if not isinstance(member, dict):
            _fail("MEMBER_FIELD_UNKNOWN", f"{path} 必须为对象。")
        member_id = member.get("member_id")
        if not isinstance(member_id, str) or not member_id.strip():
            _fail("MEMBER_ID_MISSING", f"{path}.member_id 必须为非空字符串。")
        if member_id in seen:
            _fail("MEMBER_ID_DUPLICATE", f"{path}.member_id 重复：{member_id}。")
        seen.add(member_id)
        meta = _validate_member_meta(member, path)
        records = _validate_records(member.get("records"), f"{path}.records")
        probability = _member_probability(member, prob_source, path)
        entry = {"member_id": member_id, **meta, "records": records}
        if probability is not None:
            entry["probability"] = probability
        out.append(entry)
    return out


def _interval_bounds(value: Any, label: str) -> tuple:
    """把区间字段解析为 (lo, hi)；标量视为 lo==hi。"""
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            _fail("MEMBER_INTERVAL_INVALID", f"{label} 区间必须为 [lo, hi]。")
        lo, hi = _number(value[0]), _number(value[1])
        if lo is None or hi is None:
            _fail("MEMBER_INTERVAL_INVALID", f"{label} 区间端点必须为有限数值。")
        if lo > hi:
            _fail("MEMBER_INTERVAL_INVALID", f"{label} 区间下界 {lo} 大于上界 {hi}。")
        return lo, hi
    single = _number(value)
    if single is None:
        _fail("MEMBER_INTERVAL_INVALID", f"{label} 必须为有限数值或 [lo, hi]。")
    return single, single


def intervals_to_members(intervals: Any, template: Any, prob_source: dict | None) -> list:
    """把区间带（逐小时 [lo, hi]）展开为「下界成员」与「上界成员」两个情境。

    上界成员对每个字段取较大值（更热 / 风更大 / 雨更多），作为保守方位；下界成员相反。
    两个成员都是**明确情境**，不是分位数、不是概率。
    """
    if not isinstance(template, dict):
        _fail("MEMBER_TEMPLATE_MISSING", "区间带必须提供 template（source/kind/environment/issued_at）。")
    extra = set(template) - _TEMPLATE_KEYS
    if extra:
        _fail("MEMBER_FIELD_UNKNOWN", f"template 未知字段：{sorted(extra)[0]}。")
    if "records" in template:
        _fail("MEMBER_FIELD_UNKNOWN", "template 不得包含 records；记录放在 intervals 内。")
    meta = _validate_member_meta(template, "template")
    if not isinstance(intervals, list) or not intervals:
        _fail("MEMBER_SET_EMPTY", "intervals 必须为非空数组。")
    lower_records, upper_records = [], []
    previous_end = None
    for index, record in enumerate(intervals):
        where = f"intervals[{index}]"
        if not isinstance(record, dict):
            _fail("MEMBER_RECORDS_INVALID", f"{where} 必须为对象。")
        allowed = {"start", "end", "daylight", "hazards", "wbgt_c", "hazard_item_ids", *_INTERVAL_FIELDS}
        extra = set(record) - allowed
        if extra:
            _fail("MEMBER_FIELD_UNKNOWN", f"{where} 未知字段：{sorted(extra)[0]}。")
        start = _time(record.get("start"), f"{where}.start")
        end = _time(record.get("end"), f"{where}.end")
        if end <= start:
            _fail("MEMBER_RECORD_INTERVAL_INVALID", f"{where} 区间必须具有正时长。")
        if previous_end is not None and start < previous_end:
            _fail("MEMBER_RECORD_INTERVAL_INVALID", f"{where} 与前一条记录重叠。")
        previous_end = end
        daylight = record.get("daylight")
        if not isinstance(daylight, bool):
            _fail("MEMBER_DAYLIGHT_UNKNOWN", f"{where}.daylight 必须为布尔。")
        lo_row = {"start": start.isoformat(), "end": end.isoformat(), "daylight": daylight}
        hi_row = {"start": start.isoformat(), "end": end.isoformat(), "daylight": daylight}
        for field in _INTERVAL_FIELDS:
            if field not in record:
                continue
            lo, hi = _interval_bounds(record[field], f"{where}.{field}")
            lo_row[field] = lo
            hi_row[field] = hi
        for field in ("hazards", "wbgt_c", "hazard_item_ids"):
            if field in record:
                lo_row[field] = deepcopy(record[field])
                hi_row[field] = deepcopy(record[field])
        lower_records.append(lo_row)
        upper_records.append(hi_row)
    lower = {"member_id": "interval__lower", **meta, "records": lower_records,
             "corner": "lower", "meaning": "区间下界情境；不代表最有利概率。"}
    upper = {"member_id": "interval__upper", **meta, "records": upper_records,
             "corner": "upper", "meaning": "区间上界（保守方位）情境；不代表最差概率。"}
    return [lower, upper]


def validate_member_set(raw: Any) -> dict:
    """校验并规范化成员集；不改原对象，错误带稳定 code。"""
    if not isinstance(raw, dict):
        _fail("MEMBER_SET_NOT_OBJECT", "成员集必须是对象。")
    extra = set(raw) - _SET_KEYS
    if extra:
        _fail("MEMBER_SET_UNKNOWN_FIELD", f"未知字段：{sorted(extra)[0]}。")
    if raw.get("kind") != MEMBER_SET_KIND:
        _fail("MEMBER_SET_KIND_UNKNOWN", f"kind 必须为 {MEMBER_SET_KIND}。")
    plot_id = raw.get("plot_id")
    if not isinstance(plot_id, str) or not plot_id.strip():
        _fail("MEMBER_SET_PLOT_UNKNOWN", "plot_id 必须为非空字符串。")
    has_members = raw.get("members") is not None
    has_intervals = raw.get("intervals") is not None
    if has_members and has_intervals:
        _fail("MEMBER_SET_FORM_AMBIGUOUS", "members 与 intervals 不能同时给出。")
    if not has_members and not has_intervals:
        _fail("MEMBER_SET_EMPTY", "必须给出 members 或 intervals。")
    prob_source = _normalize_probability_source(raw.get("probability_source"))
    if has_members:
        members = _validate_members(raw.get("members"), prob_source)
    else:
        members = intervals_to_members(raw.get("intervals"), raw.get("template"), prob_source)
    return {
        "kind": MEMBER_SET_KIND,
        "plot_id": plot_id,
        "probability_source": prob_source,
        "probability_scheduling_status": "HOLD",
        "members": members,
        "n_members": len(members),
        "evidence_type": EVIDENCE_TYPE,
        "evidence_label": EVIDENCE_LABEL,
        "n_real": 0,
        "note": raw.get("note"),
    }


def _normalized(member_set: Any) -> dict:
    if isinstance(member_set, dict) and member_set.get("kind") == MEMBER_SET_KIND \
            and "n_members" in member_set:
        return member_set
    return validate_member_set(member_set)


def member_weather(member: dict) -> dict:
    """把成员还原成一个 weather[plot] 信封（可直接喂给 engine / environment）。"""
    block = {"source": member["source"], "issued_at": member["issued_at"],
             "kind": member["kind"], "environment": member["environment"],
             "records": deepcopy(member["records"])}
    if member.get("wbgt_method"):
        block["wbgt_method"] = member["wbgt_method"]
    return block


def apply_member(request: dict, member_set: dict, member: dict) -> dict:
    """深拷贝请求，只把该地块天气替换为该成员；不改原请求。"""
    plot_id = member_set["plot_id"]
    patched = deepcopy(request)
    weather = dict(patched.get("weather") or {})
    weather[plot_id] = member_weather(member)
    patched["weather"] = weather
    return patched


def assert_scenario_only(payload: Any) -> list:
    """返回结果对象里出现概率声称字段的路径；空列表表示只称情境。

    键名含概率/把握词根且值非空即报警；``probability_claim`` /
    ``probability_scheduling_status`` / ``probability_source`` 是显式载体，允许存在但须为 null 或 HOLD。
    """
    problems: list = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                lowered = key.lower()
                if (key not in _PROBABILITY_CARRIER_KEYS
                        and any(token in lowered for token in _PROBABILITY_TOKENS)
                        and value not in (None, False)):
                    problems.append(here)
                walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(payload, "")
    return problems


def _member_brief(member: dict) -> dict:
    brief = {"member_id": member.get("member_id"), "kind": member.get("kind"),
             "source": member.get("source"), "issued_at": member.get("issued_at")}
    if member.get("corner"):
        brief["corner"] = member["corner"]
    return brief


def check_committed_plan(request: dict, committed_plan: dict, member_set: dict) -> dict:
    """保留已承诺出工的时间轴，逐成员检查其是否仍满足硬约束。

    复用 ``assess_interval``（逐段）与 ``verify_plan``（全时间轴结构）；两者都按主流程口径，
    不新增放宽。计数不是概率。
    """
    member_set = _normalized(member_set)
    sessions = [s for s in (committed_plan.get("sessions") or []) if isinstance(s, dict)]
    rows = []
    for member in member_set["members"]:
        patched = apply_member(request, member_set, member)
        row = _member_brief(member)
        row.update({"session_count": len(sessions), "allowed_count": 0,
                    "blocked_count": len(sessions), "blocked_sessions": []})
        try:
            validated = validate_request(patched)
            tasks = {t["id"]: t for t in validated["tasks"]}
            workers = {w["id"]: w for w in validated["workers"]}
            allowed, blocked = 0, []
            for session in sessions:
                assessment = assess_interval(validated, tasks[session["task_id"]],
                                             workers[session["worker_id"]],
                                             session["start"], session["end"])
                if assessment["allowed"]:
                    allowed += 1
                else:
                    blocked.append({"task_id": session.get("task_id"),
                                    "worker_id": session.get("worker_id"),
                                    "start": session.get("start"), "end": session.get("end"),
                                    "reason_codes": sorted({r["code"] for r in assessment["reasons"]})})
            # 把已承诺计划重新绑定到该成员的规范化输入摘要后复查时间轴；
            # 这样 verify_plan 只报告结构/天气差异，不把「换了天气」本身当成篡改。
            rebound = deepcopy(committed_plan)
            rebound["request_sha256"] = request_digest(patched, validated=validated)
            verification = verify_plan(patched, rebound)
            row.update({
                "allowed_count": allowed,
                "blocked_count": len(sessions) - allowed,
                "blocked_sessions": blocked,
                "verify_plan_valid": bool(verification.get("valid")),
                "verify_plan_error_count": len(verification.get("errors") or []),
            })
            row["all_allowed"] = bool(sessions) and allowed == len(sessions)
        except InputError as exc:
            row["member_weather_invalid"] = str(exc)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            row["member_weather_invalid"] = str(exc)
        rows.append(row)
    allowed_members = sum(1 for row in rows if row.get("all_allowed"))
    payload = {
        "task_id": "R16-A07",
        "schema_version": SCHEMA_VERSION,
        "check_mode": "committed_plan",
        "evidence_type": EVIDENCE_TYPE,
        "evidence_label": EVIDENCE_LABEL,
        "n_real": 0,
        "meaning": _MEANING_COMMITTED,
        "plot_id": member_set["plot_id"],
        "probability_claim": None,
        "probability_scheduling_status": member_set["probability_scheduling_status"],
        "member_count": len(rows),
        "committed_session_count": len(sessions),
        "has_committed_sessions": bool(sessions),
        "robust_committed_all_allowed": bool(sessions) and allowed_members == len(rows),
        "robust_committed_all_allowed_count": allowed_members,
        "members": rows,
        "forbidden_readings": list(_FORBIDDEN_READINGS),
    }
    problems = assert_scenario_only(payload)
    if problems:
        raise RuntimeError("稳健检查结果出现概率声称字段: " + ", ".join(problems))
    return payload


def replan_over_members(request: dict, member_set: dict) -> dict:
    """对每个成员重建求解（丢弃原时间轴）；空计划只报告为「本次未找到安排」。"""
    member_set = _normalized(member_set)
    rows = []
    for member in member_set["members"]:
        patched = apply_member(request, member_set, member)
        row = _member_brief(member)
        try:
            validated = validate_request(patched)
        except InputError as exc:
            row.update({"plan_status": "MEMBER_WEATHER_INVALID",
                        "error": str(exc), "session_count": None,
                        "task_remaining": None})
            rows.append(row)
            continue
        result = plan(validated)
        row.update({
            "plan_status": result["status"],
            "session_count": len(result["sessions"]),
            "task_remaining": [{"task_id": t["task_id"],
                                "scheduled_quantity": t["scheduled_quantity"],
                                "remaining_quantity": t["remaining_quantity"],
                                "unit": t["unit"]} for t in result["task_results"]],
        })
        rows.append(row)
    complete_members = sum(1 for row in rows if row.get("plan_status") == "complete")
    payload = {
        "task_id": "R16-A07",
        "schema_version": SCHEMA_VERSION,
        "check_mode": "memberwise_replan",
        "evidence_type": EVIDENCE_TYPE,
        "evidence_label": EVIDENCE_LABEL,
        "n_real": 0,
        "meaning": _MEANING_REPLAN,
        "plot_id": member_set["plot_id"],
        "probability_claim": None,
        "probability_scheduling_status": member_set["probability_scheduling_status"],
        "member_count": len(rows),
        "complete_member_count": complete_members,
        "all_members_complete": bool(rows) and complete_members == len(rows),
        "empty_plan_accepted": True,
        "empty_plan_note": "空计划是该成员下本次有界搜索的结果，不是不可行证明，也不降低任何硬约束。",
        "members": rows,
        "forbidden_readings": list(_FORBIDDEN_READINGS),
    }
    problems = assert_scenario_only(payload)
    if problems:
        raise RuntimeError("成员重解结果出现概率声称字段: " + ", ".join(problems))
    return payload


def robust_committed_check(request: dict, committed_plan: dict, member_set: dict) -> dict:
    """便捷入口：对已承诺计划做逐成员稳健检查（committed_plan 模式）。"""
    return check_committed_plan(request, committed_plan, member_set)


def member_set_evidence(member_set: dict) -> dict:
    """成员集证据状态：本环境没有可核实的真实成员批次，恒为 NEEDS_EVIDENCE。"""
    member_set = _normalized(member_set)
    kinds = {}
    for member in member_set["members"]:
        kinds[member["kind"]] = kinds.get(member["kind"], 0) + 1
    return {
        "status": "NEEDS_EVIDENCE",
        "reason": "未取得可核实的真实成员/预报批次；全部检查为合成情境，n_real=0。",
        "evidence_type": EVIDENCE_TYPE,
        "evidence_label": EVIDENCE_LABEL,
        "n_real": 0,
        "member_kind_counts": kinds,
        "note": "合成情境通过只说明在该明确输入下满足所输入约束，不是实测效果或个人健康安全。",
    }


def fetch_ensemble_members(*_args, **_kwargs) -> dict:
    """关闭网络抓取。本模块只接受调用方提供的成员集，不外发、不购买、不带 key。"""
    return {
        "ok": False,
        "members": [],
        "errors": [{
            "code": DISABLED_CODE,
            "path": "fetch",
            "message": "本模块只解析调用方提供的成员集；不外发 HTTP、不购买付费档、不带 API key。",
        }],
        "provenance": {"network": "disabled", "purchase_status": "not_purchased"},
    }


def summarize(payload: dict) -> str:
    """一行人类可读摘要；不含概率、可行性或健康结论。"""
    mode = payload.get("check_mode")
    if mode == "committed_plan":
        return ("已承诺出工 {n} 段；{k} 个情境中 {a} 个仍全部满足硬约束"
                "（计数，不是概率）。").format(
            n=payload["committed_session_count"], k=payload["member_count"],
            a=payload["robust_committed_all_allowed_count"])
    if mode == "memberwise_replan":
        return ("逐情境重解 {k} 个情境；{c} 个情境本次完成全部任务"
                "（计数，不是概率；空计划不是不可行证明）。").format(
            k=payload["member_count"], c=payload["complete_member_count"])
    return "未知检查模式。"
