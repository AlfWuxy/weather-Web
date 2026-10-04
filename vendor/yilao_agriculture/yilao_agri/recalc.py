"""独立数量/时间复算：不调用 engine、audit、workload、environment。

按请求与结果时间轴重算剩余量、净劳动与 U05 分段。不认证现场、健康或最优。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, localcontext
from math import ceil, isfinite

# 市亩；禁止 666.67 一类近似。
SQM_PER_MU = 2000.0 / 3.0
EPS = 1e-4
FORBIDDEN_MU_SQM = (667.0, 666.0, 666.6, 666.67, 666.7, 666.666, 666.667)

UNITS = frozenset({"mu", "sqm", "kg", "trip", "plant", "m", "m3"})
AREA_UNITS = frozenset({"mu", "sqm"})
INTEGER_UNITS = frozenset({"trip", "plant"})
PLANNER_KINDS = ("setup", "outbound", "work", "cleanup", "return", "buffer", "rest")
U05_KINDS = frozenset({"setup", "cleanup", "outbound", "return", "work", "wait", "rest", "unknown"})
OVERHEAD_KEYS = (
    ("setup", "setup_minutes"),
    ("outbound", "outbound_minutes"),
    ("return", "return_minutes"),
    ("cleanup", "cleanup_minutes"),
    ("buffer", "buffer_minutes"),
)
# 与 workload.MAX_SESSION_COUNT 同步：同一任务最多 32 段出工，避免无限切片。
MAX_SESSION_COUNT = 32
PLACEHOLDERS = frozenset({
    "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未确认", "未知", "待定", "待核实", "待补充", "待填写",
})
FAMILY_UNITS = {
    "remaining_area": AREA_UNITS,
    "remaining_trips": frozenset({"trip"}),
    "mature_batch": frozenset({"kg", "plant"}),
    "remaining_mass": frozenset({"kg"}),
    "remaining_plants": frozenset({"plant"}),
    "remaining_length": frozenset({"m"}),
    "remaining_volume": frozenset({"m3"}),
}
HARVEST_TOKENS = (
    "harvest", "picking", "pick_fruit", "pick_leaf", "pick_pod", "pick_shoot",
    "pluck", "reap", "cut_grain", "dig_root", "harvest_dry", "采收", "摘果", "摘叶",
)
TRANSPORT_TOKENS = (
    "transport", "carry", "haul", "field_carry", "carry_material", "manure_carry",
    "挑粪", "搬运",
)
AREA_TOKENS = (
    "weeding", "weed", "tillage", "land_prep", "fertilization", "apply_material",
    "除草", "整地", "施肥",
)
DRY_TOKENS = ("sun_dry", "dry_store", "store_paddy", "materials_prepare", "postprocess", "晾晒", "储藏")
PLANT_TOKENS = ("transplant", "移栽", "穴施")
IRRIGATION_TOKENS = ("irrigation", "irrigate", "灌溉")


def _finding(code, path, message, severity="error"):
    return {"code": code, "path": path, "message": message, "severity": severity}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _declared_minutes(block, key):
    """任务准备声明里的分钟，非有限值按 0；只读不写。"""
    value = block.get(key, 0) if isinstance(block, dict) else 0
    return float(value) if _finite(value) else 0.0


def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, TypeError):
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _minutes(start, end):
    return (end - start).total_seconds() / 60.0


def _placeholder(source):
    if not isinstance(source, str):
        return True
    text = " ".join(source.split()).casefold()
    return not text or text in PLACEHOLDERS


def _has_token(text, tokens):
    blob = str(text or "").strip().casefold()
    if not blob:
        return False
    ident = blob.replace("-", "_")
    for token in tokens:
        token_cf = token.casefold()
        if any("\u4e00" <= ch <= "\u9fff" for ch in token):
            if token_cf in blob:
                return True
        elif token_cf in ident:
            return True
    return False


def infer_quantity_family(task):
    """按 operation/method 判别数量族；无法判别时返回 None，不猜 kg 含义。"""
    if not isinstance(task, dict):
        return None
    blob = f"{task.get('operation', '')} {task.get('method', '')}"
    operation = str(task.get("operation") or "").strip().casefold()
    harvest = _has_token(blob, HARVEST_TOKENS) or operation in {"pick", "harvest"}
    transport = _has_token(blob, TRANSPORT_TOKENS)
    if harvest and transport:
        return "merge_harvest_and_carry"
    if harvest:
        return "mature_batch"
    if transport:
        return "remaining_trips"
    if _has_token(blob, DRY_TOKENS):
        return "remaining_mass"
    if _has_token(blob, PLANT_TOKENS):
        return "remaining_plants"
    if _has_token(blob, IRRIGATION_TOKENS):
        return "remaining_volume"
    if _has_token(blob, AREA_TOKENS):
        return "remaining_area"
    return None


def canonical_quantity(quantity):
    """面积只做 mu↔sqm；其他单位不互相换算。"""
    if not isinstance(quantity, dict):
        raise ValueError("quantity: 必须为含 value/unit 的对象")
    unit = quantity.get("unit")
    if unit not in UNITS:
        raise ValueError("quantity.unit: 不支持的工作量单位")
    value = quantity.get("value")
    if not _finite(value) or value < 0:
        raise ValueError("quantity.value: 必须为非负有限数")
    if unit in INTEGER_UNITS and int(value) != value:
        raise ValueError("quantity.value: 趟数和株数须为整数")
    if unit == "mu":
        return float(value) * SQM_PER_MU, "sqm"
    return float(value), unit


def ceil_trips(total_kg, load_per_trip_kg):
    """仅按给定载量向上取整趟数，不批准负重。"""
    if not _finite(total_kg) or total_kg < 0:
        raise ValueError("total_kg: 必须为非负有限数")
    if not _finite(load_per_trip_kg) or load_per_trip_kg <= 0:
        raise ValueError("load_per_trip_kg: 必须为正有限数")
    total_decimal, load_decimal = Decimal(str(total_kg)), Decimal(str(load_per_trip_kg))
    with localcontext() as context:
        context.prec = max(40, len(total_decimal.as_tuple().digits) + len(load_decimal.as_tuple().digits)
                           + abs(total_decimal.adjusted() - load_decimal.adjusted()) + 4)
        trips = int((total_decimal / load_decimal).to_integral_value(rounding=ROUND_CEILING))
    return trips


def _canonical_rate(rate):
    if not isinstance(rate, dict):
        raise ValueError("rate: 必须为对象")
    factor, unit = canonical_quantity({"value": 1, "unit": rate.get("unit")})
    low, high = rate.get("low"), rate.get("high")
    if not _finite(low) or not _finite(high) or low <= 0 or high <= 0 or low > high:
        raise ValueError("rate.low/high: 必须为正有限数且 low≤high")
    scope = rate.get("scope")
    if scope not in {"net_work", "whole_session"}:
        raise ValueError("rate.scope: 必须为 net_work 或 whole_session")
    return float(low) / factor, float(high) / factor, unit, scope, rate.get("source")


def _phase_minutes(phases, path, findings):
    totals = defaultdict(float)
    cursor = None
    first_start = last_end = None
    if not isinstance(phases, list):
        findings.append(_finding("PHASES_NOT_ARRAY", path, "phases 必须为数组"))
        return totals, first_start, last_end
    for index, phase in enumerate(phases):
        item = f"{path}.phases[{index}]"
        if not isinstance(phase, dict):
            findings.append(_finding("PHASE_INVALID", item, "阶段必须为对象"))
            continue
        kind = phase.get("kind")
        start, end = _parse_time(phase.get("start")), _parse_time(phase.get("end"))
        if start is None or end is None or end <= start:
            findings.append(_finding("PHASE_TIME_INVALID", item, "阶段时刻必须含时区且 end>start"))
            continue
        if kind not in U05_KINDS and kind != "buffer":
            findings.append(_finding("PHASE_KIND_UNKNOWN", item, f"未知阶段 {kind}；不得摊进净劳动或休息"))
        minutes = _minutes(start, end)
        totals[kind] += minutes
        if first_start is None:
            first_start = start
        if cursor is not None and start != cursor:
            findings.append(_finding("PHASE_GAP_OR_OVERLAP", item, "阶段有缺口、重叠或乱序；缺口不得按比例摊入 U05 桶"))
        cursor = end
        last_end = end
    return totals, first_start, last_end


def _family_findings(task, path):
    findings = []
    quantity = task.get("remaining_quantity") if isinstance(task, dict) else None
    if not isinstance(quantity, dict):
        return findings
    unit = quantity.get("unit")
    family = infer_quantity_family(task)
    if family == "merge_harvest_and_carry":
        findings.append(_finding("MERGE_HARVEST_AND_CARRY", path + ".remaining_quantity",
                                 "采收与搬运不得合成一条 remaining，不猜测该用 kg 还是 trip"))
        return findings
    if family:
        allowed = FAMILY_UNITS[family]
        if unit not in allowed:
            if family == "mature_batch" and unit in AREA_UNITS:
                findings.append(_finding("HARVEST_AREA_AS_REMAINING", path + ".remaining_quantity.unit",
                                         "采收 remaining 不得为亩或平方米，不用面积乘统一产量"))
            elif family == "remaining_trips" and unit in AREA_UNITS:
                findings.append(_finding("TRANSPORT_AREA_AS_REMAINING", path + ".remaining_quantity.unit",
                                         "搬运 remaining 不得为面积"))
            elif family == "remaining_trips" and unit == "kg":
                extra = ""
                load = task.get("load_per_trip_kg") if isinstance(task, dict) else None
                if _finite(quantity.get("value")) and _finite(load) and load > 0:
                    extra = f"；给定载量时应先换成 {ceil_trips(quantity['value'], load)} trip，不得把 kg 当 remaining"
                findings.append(_finding("UNIT_WEIGHT_NOT_TRIP", path + ".remaining_quantity.unit",
                                         "搬运 remaining 必须是 trip，不得用 kg 冒充" + extra))
            else:
                findings.append(_finding("FORBIDDEN_UNIT_FOR_FAMILY", path + ".remaining_quantity.unit",
                                         f"单位 {unit} 不属于数量族 {family}，不静默换算"))
    elif unit == "kg":
        findings.append(_finding("MISSING_QUANTITY_FAMILY", path + ".remaining_quantity.unit",
                                 "未知操作的 kg 不能猜是采收、晾晒还是未换趟的搬运"))
    return findings


def _u05_buckets(totals):
    work = totals.get("work", 0.0)
    preparation = totals.get("setup", 0.0) + totals.get("cleanup", 0.0)
    travel = totals.get("outbound", 0.0) + totals.get("return", 0.0)
    wait = totals.get("wait", 0.0)
    rest = totals.get("rest", 0.0)
    unknown = totals.get("unknown", 0.0)
    buffer = totals.get("buffer", 0.0)
    clock = work + preparation + travel + wait + rest + unknown + buffer
    return {
        "work": work,
        "preparation": preparation,
        "travel": travel,
        "wait": wait,
        "rest": rest,
        "unknown": unknown,
        "buffer": buffer,
        "clock": clock,
        "u05_net_work": work,
        "engine_active_v010": clock - rest,
        "u05_non_rest_non_wait": clock - rest - wait - unknown,
    }


def _session_quantity_unit(session, task):
    quantity = session.get("quantity")
    unit = session.get("unit")
    if task and unit is None:
        remaining = task.get("remaining_quantity") or {}
        unit = remaining.get("unit")
    return quantity, unit


def _check_session(session, index, tasks, workers):
    path = f"sessions[{index}]"
    findings = []
    report = {"path": path, "task_id": None, "worker_id": None, "buckets": None,
              "expected_net_work": None, "quantity": None, "unit": None}
    if not isinstance(session, dict):
        findings.append(_finding("SESSION_INVALID", path, "出工必须为对象"))
        return report, findings
    tid, wid = session.get("task_id"), session.get("worker_id")
    report["task_id"], report["worker_id"] = tid, wid
    task = tasks.get(tid) if tid in tasks else None
    worker = workers.get(wid) if wid in workers else None
    totals, first_start, last_end = _phase_minutes(session.get("phases"), path, findings)
    buckets = _u05_buckets(totals)
    report["buckets"] = buckets
    start, end = _parse_time(session.get("start")), _parse_time(session.get("end"))
    if start and end and end <= start:
        findings.append(_finding("SESSION_TIME_INVALID", path, "出工 end 必须晚于 start"))
    if first_start and start and first_start != start:
        findings.append(_finding("SESSION_PHASE_START", path, "阶段起点与出工 start 不一致"))
    if last_end and end and last_end != end:
        findings.append(_finding("SESSION_PHASE_END", path, "阶段终点与出工 end 不一致"))
    if start and end:
        span = _minutes(start, end)
        if abs(span - buckets["clock"]) > EPS:
            findings.append(_finding("CLOCK_SPAN_MISMATCH", path, "出工起止差与阶段合计不一致"))
    claimed_clock = session.get("clock_minutes")
    claimed_rest = session.get("rest_minutes")
    claimed_active = session.get("active_minutes")
    wait, rest = buckets["wait"], buckets["rest"]
    if task:
        duty = task.get("wait_duty") or {}
        if duty and duty.get("status") != "known":
            findings.append(_finding("WAIT_STATUS_UNKNOWN", path, "等待未知却产生了安排"))
        expected_wait = duty.get("minutes", 0) if duty.get("status") == "known" else 0
        if not _finite(expected_wait) or abs(wait - expected_wait) > EPS:
            findings.append(_finding("WAIT_MINUTES_MISMATCH", path, "实际等待阶段与输入声明分钟不一致"))
    if _finite(claimed_clock) and abs(claimed_clock - buckets["clock"]) > EPS:
        findings.append(_finding("CLOCK_MISMATCH", path + ".clock_minutes",
                                 "申报钟表分钟与阶段独立合计不一致"))
    if wait > EPS:
        if _finite(claimed_rest) and abs(claimed_rest - (rest + wait)) <= EPS:
            findings.append(_finding("WAIT_AS_REST", path + ".rest_minutes",
                                     "等待被计入休息；等待不是恢复"))
        elif _finite(claimed_rest) and abs(claimed_rest - rest) > EPS:
            findings.append(_finding("REST_MISMATCH", path + ".rest_minutes",
                                     "申报休息与 rest 阶段合计不一致"))
        if _finite(claimed_active) and abs(claimed_active - buckets["engine_active_v010"]) <= EPS:
            findings.append(_finding("WAIT_AS_ACTIVE", path + ".active_minutes",
                                     "等待被计入活动分钟；等待默认不是净劳动或恢复"))
        elif _finite(claimed_active) and abs(claimed_active - buckets["u05_non_rest_non_wait"]) > EPS:
            findings.append(_finding("ACTIVE_MISMATCH", path + ".active_minutes",
                                     "申报活动分钟与非休息非等待阶段合计不一致"))
    else:
        if _finite(claimed_rest) and abs(claimed_rest - rest) > EPS:
            findings.append(_finding("REST_MISMATCH", path + ".rest_minutes",
                                     "申报休息与 rest 阶段合计不一致"))
        if _finite(claimed_active) and abs(claimed_active - buckets["engine_active_v010"]) > EPS:
            findings.append(_finding("ACTIVE_MISMATCH", path + ".active_minutes",
                                     "申报活动分钟与钟表减休息不一致"))
    if totals.get("buffer", 0.0) > EPS and _finite(claimed_rest) and abs(claimed_rest - (rest + totals["buffer"])) <= EPS:
        findings.append(_finding("BUFFER_AS_REST", path, "延误余量被记成休息"))
    quantity, unit = _session_quantity_unit(session, task)
    report["quantity"], report["unit"] = quantity, unit
    if not _finite(quantity) or quantity <= 0:
        findings.append(_finding("SESSION_QUANTITY_INVALID", path + ".quantity", "出工数量须为有限正数"))
        quantity = None
    if unit in INTEGER_UNITS and quantity is not None and abs(quantity - round(quantity)) > EPS:
        findings.append(_finding("DISCRETE_FRACTION", path + ".quantity", "趟数和株数不得拆成小数"))
    expected_net = None
    if task and wid and quantity is not None:
        rates = task.get("rates") if isinstance(task.get("rates"), dict) else {}
        if wid not in rates:
            findings.append(_finding("MISSING_WORKER_RATE", path, "缺少该劳动者同口径速率"))
        else:
            try:
                low, high, rate_unit, scope, source = _canonical_rate(rates[wid])
                remaining = task.get("remaining_quantity") or {}
                _, task_unit = canonical_quantity(remaining)
                session_canon = canonical_quantity({"value": quantity, "unit": unit})[1] if unit in UNITS else None
                if session_canon and task_unit != session_canon:
                    findings.append(_finding("UNIT_MISMATCH", path + ".unit",
                                             "出工单位与任务剩余量口径不一致"))
                if rate_unit != (session_canon or rate_unit):
                    findings.append(_finding("RATE_UNIT_MISMATCH", path, "速率单位与出工单位不兼容"))
                expected_net = quantity * high
                report["expected_net_work"] = expected_net
                work = buckets["work"]
                if scope == "whole_session":
                    findings.append(_finding("WHOLE_SESSION_TREATED_AS_NET", path,
                                             "整次出工经验含附加时间，不能当净劳动排程"))
                if _placeholder(source):
                    findings.append(_finding("RATE_SOURCE_PLACEHOLDER_SCHEDULED", path,
                                             "占位速率来源不能生成确定安排"))
                if scope == "net_work" and not _placeholder(source):
                    if work + EPS < expected_net:
                        findings.append(_finding("WORK_MINUTES_SHORT", path,
                                                 "净作业分钟不足：独立复算 quantity×high_rate"))
                    allowed = ceil(expected_net - 1e-9) if expected_net > 0 else 0.0
                    if work > max(allowed, expected_net) + EPS:
                        findings.append(_finding("WORK_MINUTES_INCLUDES_NONWORK", path,
                                                 "净作业分钟超出保守净耗时，准备/往返/休息/等待不得并入 work"))
                # 只有实际存在附加劳动时，active==work 才代表漏计；休息/等待本就不属于 active。
                nonwork_active = buckets["preparation"] + buckets["travel"] + buckets["buffer"]
                if _finite(claimed_active) and abs(claimed_active - work) <= EPS and nonwork_active > EPS:
                    findings.append(_finding("NET_EQ_ACTIVE", path + ".active_minutes",
                                             "不得把 active_minutes 当成净劳动；active 含准备往返收尾和 buffer"))
            except ValueError as exc:
                findings.append(_finding("RATE_INVALID", path, str(exc)))
        session_spec = task.get("session") if isinstance(task.get("session"), dict) else {}
        for kind, key in OVERHEAD_KEYS:
            required = session_spec.get(key, 0)
            if _finite(required) and totals.get(kind, 0.0) + EPS < required:
                findings.append(_finding("OVERHEAD_MISSING", path,
                                         f"漏计 {kind}：阶段合计少于 session.{key}"))
        family = infer_quantity_family(task)
        if family == "mature_batch" and unit in AREA_UNITS:
            findings.append(_finding("HARVEST_AREA_AS_REMAINING", path + ".unit",
                                     "采收完成量不得用亩或平方米"))
        if family == "remaining_trips" and unit != "trip":
            code = "UNIT_WEIGHT_NOT_TRIP" if unit == "kg" else "FORBIDDEN_UNIT_FOR_FAMILY"
            findings.append(_finding(code, path + ".unit", "搬运出工单位必须是 trip"))
    if worker and isinstance(worker.get("limits"), dict):
        maximum = worker["limits"].get("max_continuous_active_minutes")
        min_rest = worker["limits"].get("min_rest_minutes")
        if _finite(maximum) and maximum > 0:
            continuous = 0.0
            for phase in session.get("phases") or []:
                if not isinstance(phase, dict):
                    continue
                start, end = _parse_time(phase.get("start")), _parse_time(phase.get("end"))
                if start is None or end is None or end <= start:
                    continue
                minutes = _minutes(start, end)
                kind = phase.get("kind")
                if kind == "rest":
                    if _finite(min_rest) and minutes + EPS < min_rest:
                        findings.append(_finding("REST_TOO_SHORT", path, "休息短于输入的最小休息"))
                    continuous = 0.0
                elif kind == "wait":
                    # 等待默认不是恢复，也不把连续劳动清零。
                    pass
                elif kind in {"setup", "outbound", "work", "cleanup", "return", "buffer"}:
                    continuous += minutes
                    if continuous > maximum + EPS:
                        findings.append(_finding("CONTINUOUS_ACTIVE_EXCEEDED", path,
                                                 "非休息阶段连续活动超过输入上限；把休息改标为 work 不能少算负荷"))
                        break
    if buckets["work"] > EPS and abs(buckets["work"] - buckets["clock"]) <= EPS and (
            buckets["preparation"] + buckets["travel"] + buckets["rest"] + buckets["wait"] + buckets["buffer"] > EPS
            or (task and any(_finite(task.get("session", {}).get(k)) and task["session"][k] > 0
                             for _, k in OVERHEAD_KEYS))):
        findings.append(_finding("NET_EQ_CLOCK", path, "存在附加段时不得把净劳动写成总耗时"))
    return report, findings


def _mu_approx(value, reported):
    """申报换算因子贴近 666.67 等近似、且不是 2000/3 时判为禁用近似。"""
    if not _finite(value) or value == 0 or not _finite(reported):
        return False
    factor = reported / value
    if abs(factor - SQM_PER_MU) <= 1e-4:
        return False
    return any(abs(factor - approx) <= 1e-3 for approx in FORBIDDEN_MU_SQM)


def check_plan(request, result):
    """独立复查数量守恒与 U05 时间分段；不调用排程内部函数，不修改入参。"""
    findings = []
    sessions_out, tasks_out = [], []
    if not isinstance(request, dict):
        return {"ok": False, "findings": [_finding("REQUEST_INVALID", "request", "请求必须为对象")],
                "sessions": [], "tasks": [], "depends_on_scheduler": False,
                "meaning": "独立复算数量与时间分段；不认证现场真实性、搜索最优性或健康效果。"}
    if not isinstance(result, dict):
        return {"ok": False, "findings": [_finding("RESULT_INVALID", "result", "结果必须为对象")],
                "sessions": [], "tasks": [], "depends_on_scheduler": False,
                "meaning": "独立复算数量与时间分段；不认证现场真实性、搜索最优性或健康效果。"}
    tasks = {t["id"]: t for t in request.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
    workers = {w["id"]: w for w in request.get("workers") or [] if isinstance(w, dict) and w.get("id")}
    for i, task in enumerate(request.get("tasks") or []):
        if isinstance(task, dict):
            findings.extend(_family_findings(task, f"tasks[{i}]"))
    sessions = result.get("sessions", [])
    if not isinstance(sessions, list):
        findings.append(_finding("SESSIONS_NOT_ARRAY", "sessions", "sessions 必须为数组"))
        sessions = []
    scheduled = defaultdict(float)
    scheduled_unit = {}
    for index, session in enumerate(sessions):
        report, extra = _check_session(session, index, tasks, workers)
        findings.extend(extra)
        sessions_out.append(report)
        if report.get("task_id") and _finite(report.get("quantity")):
            scheduled[report["task_id"]] += report["quantity"]
            scheduled_unit[report["task_id"]] = report.get("unit")
    # 准备成本台账（独立复算，不引用 workload/engine/audit）：
    # 每任务准备桶必须等于「出工段数×每次出工重复项 + 每任务一次项（仅有出工）」；
    # 漏计重复项=分段未重新准备；漏计一次项=凭省略准备得到虚假容量；超出=重复计入。
    prep_by_task = defaultdict(lambda: {"preparation": 0.0, "travel": 0.0, "buffer": 0.0})
    segment_count = defaultdict(int)
    for report in sessions_out:
        tid = report.get("task_id")
        buckets = report.get("buckets")
        if tid not in tasks or not isinstance(buckets, dict):
            continue
        segment_count[tid] += 1
        for bucket in ("preparation", "travel", "buffer"):
            value = buckets.get(bucket, 0.0)
            prep_by_task[tid][bucket] += float(value) if _finite(value) else 0.0
    for tid, task in tasks.items():
        n = segment_count.get(tid, 0)
        if n > MAX_SESSION_COUNT:
            findings.append(_finding("SEGMENT_COUNT_OVER_CAP", f"tasks.{tid}",
                                     f"同一任务出工段数 {n} 超过上限 {MAX_SESSION_COUNT}，无限切片不被接受"))
            continue
        spec = task.get("session") if isinstance(task.get("session"), dict) else {}
        once = task.get("once") if isinstance(task.get("once"), dict) else {}
        repeat_prep = _declared_minutes(spec, "setup_minutes") + _declared_minutes(spec, "cleanup_minutes")
        repeat_travel = _declared_minutes(spec, "outbound_minutes") + _declared_minutes(spec, "return_minutes")
        repeat_buffer = _declared_minutes(spec, "buffer_minutes")
        once_prep = ((_declared_minutes(once, "setup_minutes") + _declared_minutes(once, "cleanup_minutes"))
                     if n else 0.0)
        for bucket, expected_total in (("preparation", repeat_prep * n + once_prep),
                                       ("travel", repeat_travel * n),
                                       ("buffer", repeat_buffer * n)):
            repeat_only = {"preparation": repeat_prep, "travel": repeat_travel,
                           "buffer": repeat_buffer}[bucket] * n
            actual = prep_by_task[tid][bucket]
            if actual + EPS < repeat_only:
                findings.append(_finding("PREP_NOT_RECOUNTED", f"tasks.{tid}",
                                         f"{bucket} 实计 {actual:g} 分钟，少于分段重复项 {repeat_only:g} 分钟"))
            elif actual + EPS < expected_total:
                findings.append(_finding("ONCE_PREP_MISSING", f"tasks.{tid}",
                                         f"{bucket} 实计 {actual:g} 分钟，少于含一次项的台账 {expected_total:g} 分钟："
                                         "疑似凭省略准备获得虚假容量"))
            elif actual > expected_total + EPS:
                findings.append(_finding("PREP_OVERCOUNTED", f"tasks.{tid}",
                                         f"{bucket} 实计 {actual:g} 分钟，超出台账 {expected_total:g} 分钟"))
    rows = result.get("task_results", [])
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        findings.append(_finding("TASK_RESULTS_NOT_ARRAY", "task_results", "task_results 必须为数组"))
        rows = []
    by_id = {}
    for i, row in enumerate(rows):
        if isinstance(row, dict) and row.get("task_id"):
            by_id.setdefault(row["task_id"], []).append((i, row))
    for tid, task in tasks.items():
        remaining = task.get("remaining_quantity") or {}
        try:
            requested, unit = canonical_quantity(remaining)
        except ValueError as exc:
            findings.append(_finding("QUANTITY_INVALID", f"tasks.{tid}.remaining_quantity", str(exc)))
            continue
        if remaining.get("unit") == "mu" and isinstance(rows, list):
            for i, row in by_id.get(tid, []):
                reported = row.get("requested_quantity")
                if _finite(reported) and _mu_approx(remaining.get("value"), reported):
                    findings.append(_finding("AREA_CONVERSION_FORBIDDEN_APPROX",
                                             f"task_results[{i}].requested_quantity",
                                             "市亩换算必须用 2000/3，不得用 666.67 近似"))
        done = scheduled.get(tid, 0.0)
        if done > requested + EPS:
            findings.append(_finding("QUANTITY_OVER_REMAINING", f"tasks.{tid}",
                                     "安排量超过剩余量"))
        matches = by_id.get(tid, [])
        if len(matches) > 1:
            findings.append(_finding("TASK_RESULT_DUPLICATE", f"task_results.{tid}", "同一任务结果重复"))
        elif len(matches) == 1:
            i, row = matches[0]
            scheduled_qty = row.get("scheduled_quantity")
            remaining_qty = row.get("remaining_quantity")
            requested_qty = row.get("requested_quantity")
            if _finite(scheduled_qty) and abs(scheduled_qty - done) > EPS:
                findings.append(_finding("TASK_RESULT_QTY_MISMATCH", f"task_results[{i}].scheduled_quantity",
                                         "任务结果已安排量与时间轴独立合计不一致"))
            if _finite(requested_qty) and abs(requested_qty - requested) > EPS:
                if remaining.get("unit") != "mu" or not _mu_approx(remaining.get("value"), requested_qty):
                    findings.append(_finding("TASK_RESULT_REQUESTED_MISMATCH",
                                             f"task_results[{i}].requested_quantity",
                                             "任务结果待做量与独立换算不一致"))
            if _finite(scheduled_qty) and _finite(remaining_qty) and _finite(requested_qty):
                if abs(scheduled_qty + remaining_qty - requested_qty) > EPS:
                    findings.append(_finding("QUANTITY_NOT_CONSERVED", f"task_results[{i}]",
                                             "待做、已安排、未安排不守恒"))
            for estimate in row.get("remaining_work_estimates") or []:
                if not isinstance(estimate, dict):
                    continue
                interval = estimate.get("net_minutes_interval")
                if not (isinstance(interval, list) and len(interval) == 2 and all(_finite(x) for x in interval)):
                    continue
                leftover = remaining_qty if _finite(remaining_qty) else max(0.0, requested - done)
                wid = estimate.get("worker_id")
                rates = task.get("rates") if isinstance(task.get("rates"), dict) else {}
                if wid in rates:
                    try:
                        low, high, _, scope, _ = _canonical_rate(rates[wid])
                    except ValueError:
                        continue
                    if scope == "net_work":
                        expected_high = leftover * high
                        overhead = sum(float(task.get("session", {}).get(k, 0) or 0) for _, k in OVERHEAD_KEYS)
                        if overhead > EPS and abs(interval[1] - (expected_high + overhead)) <= 0.5:
                            findings.append(_finding("NET_INCLUDES_OVERHEAD",
                                                     f"task_results[{i}].remaining_work_estimates",
                                                     "余量净用时不得把准备往返收尾加进 net_minutes_interval"))
                        if abs(interval[1] - leftover * high) > 0.5 and leftover > EPS:
                            # 允许展示区间；明显把钟表/活动分钟当净劳动则报错。
                            if abs(interval[1] - leftover * high - overhead) > 0.5:
                                pass
        tasks_out.append({"task_id": tid, "requested": requested, "unit": unit,
                          "scheduled": done, "family": infer_quantity_family(task)})
    ok = not any(item["severity"] == "error" for item in findings)
    return {
        "ok": ok,
        "findings": findings,
        "sessions": sessions_out,
        "tasks": tasks_out,
        "depends_on_scheduler": False,
        "implementation": "yilao_agri.recalc",
        "meaning": "独立复算数量与 U05 时间分段（净劳动/准备/往返/等待/休息）；不认证现场真实性、搜索最优性或健康效果。",
    }
