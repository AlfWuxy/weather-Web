"""工作量、搬运与同口径复盘；不生成个人健康许可或虚假概率区间。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_CEILING, localcontext
import math

from .models import (
    INTEGER_UNITS, InputError, UNITS, _infer_quantity_family, _number, is_placeholder_source,
    iter_local_date_spans, local_civil_date, parse_time, resolve_timezone, utc_minutes,
)


SQM_PER_MU = 2000.0 / 3.0
EPS = 1e-8


def canonical_quantity(quantity) -> tuple[float, str]:
    """面积统一为平方米，其余计量维度不得相互猜测换算。"""
    if not isinstance(quantity, dict):
        raise InputError("quantity: 必须为含 value/unit 的对象")
    unit = quantity.get("unit")
    if not isinstance(unit, str) or unit not in UNITS:
        raise InputError("quantity.unit: 不支持的工作量单位")
    value = _number(quantity.get("value"), "quantity.value", 0,
                    integer=unit in {"trip", "plant"})
    canonical_value = float(value) * (SQM_PER_MU if unit == "mu" else 1.0)
    _number(canonical_value, "quantity.value", 0)
    return canonical_value, "sqm" if unit == "mu" else unit


def quantity_quantum(task, canonical_unit, high_rate, step_minutes) -> float:
    """分段量子。亩步长换成平方米；趟/株默认 1，避免按时间网格切出小数趟。"""
    if not isinstance(task, dict):
        raise InputError("task: 必须为对象")
    remaining = task.get("remaining_quantity") if isinstance(task.get("remaining_quantity"), dict) else {}
    remaining_unit = remaining.get("unit")
    step = task.get("quantity_step")
    if step is not None:
        quantum = _number(step, "quantity_step", positive=True,
                          integer=remaining_unit in INTEGER_UNITS)
        quantum = float(quantum) * (SQM_PER_MU if remaining_unit == "mu" else 1.0)
        _number(quantum, "quantity_step.canonical", positive=True)
        return quantum
    if canonical_unit in INTEGER_UNITS:
        return 1.0
    high_rate = _number(high_rate, "rate.high", positive=True)
    step_minutes = _number(step_minutes, "step_minutes", positive=True)
    quantum = float(step_minutes) / float(high_rate)
    _number(quantum, "quantity_quantum", positive=True)
    return quantum


def discrete_quantity_ok(value, unit, epsilon=1e-8) -> bool:
    """趟、株必须是整数；其他单位允许小数。"""
    if unit not in INTEGER_UNITS:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    return bool(finite) and value >= -epsilon and abs(value - round(value)) <= epsilon


def min_partial_quanta(min_chunk_minutes, high_rate, quantum, epsilon=1e-8) -> int:
    """非尾量至少需要多少个量子，才能达到 min_chunk_minutes。"""
    minutes = float(quantum) * float(high_rate)
    if minutes <= 0:
        return 1
    return max(1, math.ceil((float(min_chunk_minutes) - epsilon) / minutes))


def chunk_is_allowed(quantity, remaining, min_chunk_minutes, high_rate, unit, quantum,
                     epsilon=1e-8) -> bool:
    """尾量可短于最小段；非尾量须达到 min_chunk 并对齐量子。趟/株不得为小数。"""
    if isinstance(quantity, bool) or isinstance(remaining, bool):
        return False
    if not isinstance(quantity, (int, float)) or not isinstance(remaining, (int, float)):
        return False
    try:
        if not math.isfinite(quantity) or not math.isfinite(remaining):
            return False
    except (OverflowError, TypeError):
        return False
    if quantity <= epsilon or quantity > remaining + epsilon:
        return False
    if not discrete_quantity_ok(quantity, unit, epsilon):
        return False
    is_tail = quantity >= remaining - epsilon
    if unit in INTEGER_UNITS and quantum:
        steps = quantity / float(quantum)
        if abs(steps - round(steps)) > 1e-6:
            return False
    if is_tail:
        return True
    if high_rate is None or min_chunk_minutes is None or not quantum:
        return False
    try:
        if not math.isfinite(high_rate) or not math.isfinite(min_chunk_minutes) or not math.isfinite(quantum):
            return False
    except (OverflowError, TypeError):
        return False
    if float(quantum) <= 0 or quantity * float(high_rate) < float(min_chunk_minutes) - epsilon:
        return False
    steps = quantity / float(quantum)
    return abs(steps - round(steps)) <= 1e-6


def _rate(rate):
    if not isinstance(rate, dict):
        raise InputError("rate: 必须为对象")
    factor, unit = canonical_quantity({"value": 1, "unit": rate.get("unit")})
    low = _number(rate.get("low"), "rate.low", positive=True)
    high = _number(rate.get("high"), "rate.high", positive=True)
    if low > high:
        raise InputError("rate: low 不得大于 high")
    scope = rate.get("scope")
    if scope not in {"net_work", "whole_session"}:
        raise InputError("rate.scope: 必须为 net_work 或 whole_session")
    if not isinstance(rate.get("source"), str) or not rate["source"].strip():
        raise InputError("rate.source: 必须为非空字符串")
    low, high = float(low) / factor, float(high) / factor
    _number(low, "rate.low", positive=True)
    _number(high, "rate.high", positive=True)
    return low, high, unit, scope


# session 五项每次出工都要再计；once 仅 setup/cleanup，整任务一次。
REPEAT_ADDON_KEYS = ("setup_minutes", "outbound_minutes", "return_minutes",
                     "cleanup_minutes", "buffer_minutes")
ONCE_ADDON_KEYS = ("setup_minutes", "cleanup_minutes")
MAX_SESSION_COUNT = 32


def _addon_block(block, keys, path):
    """附加分钟块：缺省 0，未知键拒绝，不把往返或等待塞进一次项。"""
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise InputError(f"{path}: 必须为对象")
    extra = [key for key in block if key not in keys]
    if extra:
        raise InputError(f"{path}.{extra[0]}: 未知字段")
    parts = {}
    total = 0.0
    for key in keys:
        minutes = float(_number(block.get(key, 0), f"{path}.{key}", 0, 10080))
        parts[key] = minutes
        total += minutes
    _number(total, f"{path}.total", 0, 10080 * len(keys))
    return total, parts


def repeating_addon_minutes(session) -> dict:
    """每次出工重复的准备/往返/收尾/预留；不是净劳动，也不是整任务一次项。"""
    total, parts = _addon_block(session, REPEAT_ADDON_KEYS, "session")
    return {"minutes": total, "parts": parts, "applies": "each_outing"}


def once_addon_minutes(once) -> dict:
    """整任务一次的准备/收尾。往返、预留、等待不得写入。"""
    total, parts = _addon_block(once, ONCE_ADDON_KEYS, "once")
    return {"minutes": total, "parts": parts, "applies": "once_per_task"}


def estimate_split_addons(task, session_count) -> dict:
    """按出工次数复算附加时间：重复项×次数，一次项在有出工时只加一次。"""
    if not isinstance(task, dict):
        raise InputError("task: 必须为对象")
    count = int(_number(session_count, "session_count", 0, MAX_SESSION_COUNT, integer=True))
    repeating = repeating_addon_minutes(task.get("session"))
    once = once_addon_minutes(task.get("once"))
    repeating_total = repeating["minutes"] * count
    once_total = once["minutes"] if count else 0.0
    total = repeating_total + once_total
    _number(repeating_total, "repeating_minutes_total", 0)
    _number(once_total, "once_minutes_total", 0)
    _number(total, "addon_minutes_total", 0)
    return {
        "session_count": count,
        "repeating_minutes_per_session": repeating["minutes"],
        "repeating_minutes_total": repeating_total,
        "once_minutes": once_total,
        "addon_minutes_total": total,
        "repeating_parts": repeating["parts"],
        "once_parts": once["parts"],
        "note": "净劳动另计。分段只倍增每次出工重复项；每任务一次项不随分段倍增。不是健康许可。",
    }


def session_overhead_pieces(task, include_once=False):
    """单次出工非净阶段分钟。include_once 仅给该任务尚未出工的第一段。"""
    addons = estimate_split_addons(task, 1)
    setup = addons["repeating_parts"]["setup_minutes"]
    cleanup = addons["repeating_parts"]["cleanup_minutes"]
    if include_once:
        setup += addons["once_parts"]["setup_minutes"]
        cleanup += addons["once_parts"]["cleanup_minutes"]
    return (("setup", setup), ("outbound", addons["repeating_parts"]["outbound_minutes"]),
            ("cleanup", cleanup), ("return", addons["repeating_parts"]["return_minutes"]),
            ("buffer", addons["repeating_parts"]["buffer_minutes"]))


# 准备/收尾阶段与任务声明键的对应。
# 重复项（setup/outbound/return/cleanup/buffer）每次出工都重新计入；一次项（once 的 setup/cleanup）整任务只一份。
REPEAT_PREP_KINDS = (("setup", "setup_minutes"), ("outbound", "outbound_minutes"),
                     ("return", "return_minutes"), ("cleanup", "cleanup_minutes"),
                     ("buffer", "buffer_minutes"))
ONCE_PREP_KINDS = (("setup", "setup_minutes"), ("cleanup", "cleanup_minutes"))
_ONCE_PREP_NAMES = frozenset(name for name, _ in ONCE_PREP_KINDS)


def prep_ledger(task, session_count) -> dict:
    """准备成本台账：重复项 × 出工次数 + 每任务一次项（仅有出工时）。

    单一来源，供排程下界、独立复查（audit）与独立复算（recalc）共用。
    只登记准备/收尾与延误余量，不含净劳动与休息，也不产生健康结论。
    """
    if not isinstance(task, dict):
        raise InputError("task: 必须为对象")
    addons = estimate_split_addons(task, session_count)
    count = addons["session_count"]
    per_kind = {}
    for kind, key in REPEAT_PREP_KINDS:
        minutes = addons["repeating_parts"][key] * count
        if count and kind in _ONCE_PREP_NAMES:
            minutes += addons["once_parts"][key]
        per_kind[kind] = minutes
    _number(sum(per_kind.values()), "prep_ledger.total", 0)
    return {"session_count": count, "per_kind_expected": per_kind,
            "repeating_minutes_total": addons["repeating_minutes_total"],
            "once_minutes": addons["once_minutes"], "total": addons["addon_minutes_total"],
            "note": "重复项随出工次数倍增；每任务一次项只一份。净劳动与休息另计，不是健康许可。"}


def prep_ledger_findings(task, per_kind_actual, session_count, epsilon=1e-6) -> list:
    """把时间轴实测的准备分钟与台账比对，逐阶段返回漏计/重计差异。

    漏计「重复项」= 分段没有重新计入每次出工的准备；
    漏计「一次项」= 凭省略整任务一次准备获得虚假容量；
    超出台账 = 一次项被按段重复计入或阶段被重复申报。
    """
    ledger = prep_ledger(task, session_count)
    count = ledger["session_count"]
    addons = estimate_split_addons(task, count)
    actual = per_kind_actual if isinstance(per_kind_actual, dict) else {}
    findings = []
    for kind, key in REPEAT_PREP_KINDS:
        got = float(_number(actual.get(kind, 0), f"actual.{kind}", 0))
        repeat_only = addons["repeating_parts"][key] * count
        expected = ledger["per_kind_expected"][kind]
        if got + epsilon < repeat_only:
            code = "PREP_NOT_RECOUNTED"
        elif got + epsilon < expected:
            code = "ONCE_PREP_MISSING"
        elif got > expected + epsilon:
            code = "PREP_OVERCOUNTED"
        else:
            continue
        findings.append({"code": code, "kind": kind, "actual_minutes": got,
                         "repeating_minutes": repeat_only, "expected_minutes": expected,
                         "message": f"{kind} 实计 {got:g} 分钟 / 台账 {expected:g} 分钟"})
    return findings


def estimate_clock_with_splits(task, worker_id, session_count, quantity=None) -> dict:
    """净劳动加按出工次数复算的附加时间。整次出工口径不得再叠附加。"""
    net = estimate_task(task, worker_id)
    addons = estimate_split_addons(task, session_count)
    result = {"quantity": net["quantity"], "unit": net["unit"], "session_count": addons["session_count"],
              "net_low_minutes": net["low_minutes"], "net_high_minutes": net["high_minutes"],
              "repeating_minutes_total": addons["repeating_minutes_total"],
              "once_minutes": addons["once_minutes"],
              "clock_low_minutes": None, "clock_high_minutes": None,
              "schedulable": net["schedulable"], "reasons": list(net["reasons"]),
              "addon_stacked": False, "source": net["source"]}
    if quantity is not None:
        amount = float(_number(quantity, "quantity", 0))
        if net["low_rate"] is None or net["high_rate"] is None:
            return result
        low, high = amount * net["low_rate"], amount * net["high_rate"]
        _number(low, "estimate.net_low_minutes", 0, positive=amount > 0)
        _number(high, "estimate.net_high_minutes", 0, positive=amount > 0)
        result.update(quantity=amount, net_low_minutes=low, net_high_minutes=high)
    if result["net_low_minutes"] is None:
        return result
    if any(reason["code"] == "WHOLE_SESSION_RATE" for reason in result["reasons"]):
        result.update(clock_low_minutes=result["net_low_minutes"],
                      clock_high_minutes=result["net_high_minutes"], addon_stacked=False)
        result["reasons"].append({
            "code": "WHOLE_SESSION_NO_STACK",
            "message": "整次出工口径已含部分附加时间，分段时不得再把准备往返叠加上去",
        })
        result["schedulable"] = False
        return result
    result.update(
        clock_low_minutes=result["net_low_minutes"] + addons["addon_minutes_total"],
        clock_high_minutes=result["net_high_minutes"] + addons["addon_minutes_total"],
        addon_stacked=True,
    )
    return result


def estimate_task(task, worker_id) -> dict:
    """仅计算该人的剩余净工作量，附加时间由排程器按出工次数计入。"""
    if not isinstance(task, dict):
        raise InputError("task: 必须为对象")
    quantity, unit = canonical_quantity(task.get("remaining_quantity"))
    family = _infer_quantity_family(task)
    if family == "remaining_trips" and unit != "trip":
        if unit == "kg":
            code, detail = "UNIT_WEIGHT_NOT_TRIP", "即使有 load_per_trip_kg 也不把 kg 排成趟"
        elif unit in {"mu", "sqm"}:
            code, detail = "TRANSPORT_AREA_AS_REMAINING", "不把亩或平方米静默当成趟数"
        else:
            code, detail = "FORBIDDEN_UNIT_FOR_FAMILY", f"单位 {unit} 不属于 remaining_trips"
        raise InputError(f"task.remaining_quantity.unit: {code} 搬运 remaining 必须是 trip；{detail}")
    result = {"quantity": quantity, "unit": unit, "low_minutes": None, "high_minutes": None,
              "low_rate": None, "high_rate": None, "schedulable": False, "reasons": [], "source": ""}
    rates = task.get("rates", {})
    if not isinstance(rates, dict):
        raise InputError("task.rates: 必须为对象")
    if worker_id not in rates:
        result["reasons"].append({"code": "MISSING_WORKER_RATE", "message": "缺少该劳动者相同方法的用时依据"})
        return result
    rate = rates[worker_id]
    low, high, rate_unit, scope = _rate(rate)
    if rate_unit != unit:
        raise InputError("task.rates.unit: 速率与剩余工作量单位不兼容")
    low_minutes, high_minutes = quantity * low, quantity * high
    _number(low_minutes, "estimate.low_minutes", 0, positive=quantity > 0)
    _number(high_minutes, "estimate.high_minutes", 0, positive=quantity > 0)
    result.update(low_minutes=low_minutes, high_minutes=high_minutes, low_rate=low,
                  high_rate=high, source=rate["source"], schedulable=scope == "net_work")
    if is_placeholder_source(rate["source"]):
        result["schedulable"] = False
        result["reasons"].append({"code": "RATE_SOURCE_UNKNOWN", "message": "速率来源是待确认占位值，不能据此生成确定安排"})
    if scope == "whole_session":
        result["reasons"].append({"code": "WHOLE_SESSION_RATE", "message": "整次出工经验已含部分附加时间；先拆清口径再做精细排程"})
    if unit == "trip":
        result["transport"] = _trip_context_from_remaining(quantity, task.get("load_per_trip_kg"))
    return result


def estimate_transport_trips(total_kg, load_per_trip_kg, minutes_per_trip=None) -> dict:
    """载量必须由调用者提供有依据的限制；函数不优化或批准单趟负重。"""
    _number(total_kg, "total_kg", 0)
    _number(load_per_trip_kg, "load_per_trip_kg", positive=True)
    ratio = total_kg / load_per_trip_kg
    _number(ratio, "transport.trip_ratio", 0)
    # 十进制输入避免 0.14 / 0.01 的二进制误差多算一趟。
    total_decimal, load_decimal = Decimal(str(total_kg)), Decimal(str(load_per_trip_kg))
    with localcontext() as context:
        context.prec = max(40, len(total_decimal.as_tuple().digits) + len(load_decimal.as_tuple().digits)
                           + abs(total_decimal.adjusted() - load_decimal.adjusted()) + 4)
        trips = int((total_decimal / load_decimal).to_integral_value(rounding=ROUND_CEILING))
        last_trip = float(total_decimal - (trips - 1) * load_decimal) if trips else 0.0
    load_value = float(load_per_trip_kg)
    if trips == 0:
        full_trips, tail_trips = 0, 0
    elif last_trip + 1e-12 < load_value:
        full_trips, tail_trips = trips - 1, 1
    else:
        full_trips, tail_trips = trips, 0
    result = {"total_kg": float(total_kg), "load_per_trip_kg": load_value,
              "trips": trips, "full_trips": full_trips, "tail_trips": tail_trips,
              "last_trip_kg": last_trip,
              "remaining_quantity": {"value": trips, "unit": "trip"},
              "low_minutes": None, "high_minutes": None,
              "tail_uses_full_trip_minutes": True,
              "session_overhead_multiplier": 1,
              "note": "仅按给定载量计算整趟与尾趟，不代表负重安全；净用时按趟，出工准备/往返/收尾只计一次，装卸不得再乘趟数"}
    if minutes_per_trip is not None:
        if not isinstance(minutes_per_trip, dict):
            raise InputError("minutes_per_trip: 必须为含 low/high/source 的对象")
        rate = {**minutes_per_trip, "unit": "trip", "scope": "net_work"}
        low, high, _, _ = _rate(rate)
        for key, value in (("low_minutes", trips * low), ("high_minutes", trips * high)):
            _number(value, f"transport.{key}", 0)
            result[key] = value
        result["source"] = rate["source"]
    return result


def _trip_context_from_remaining(trip_count, load_per_trip_kg) -> dict:
    """remaining 已是趟时的载量上下文；无原始总重则不发明不足一载的尾趟重量。"""
    trips = int(_number(trip_count, "trip_count", minimum=0, integer=True))
    if load_per_trip_kg is None:
        return {"trips": trips, "full_trips": trips, "tail_trips": 0, "last_trip_kg": None,
                "load_per_trip_kg": None, "mass_basis": "remaining_trips_only",
                "remaining_quantity": {"value": trips, "unit": "trip"},
                "note": "缺少载量上下文时只按已给趟数；不发明重量"}
    load = _number(load_per_trip_kg, "load_per_trip_kg", positive=True)
    info = estimate_transport_trips(0.0 if trips == 0 else float(load) * trips, load)
    info["mass_basis"] = "full_load_times_remaining_trips"
    return info


def session_trip_plan(trip_count, work_minutes, load_per_trip_kg=None, total_kg=None) -> dict:
    """一次出工内按整趟/尾趟切开净作业分钟；出工附加由调用方只加一次。"""
    trips = int(_number(trip_count, "trip_count", minimum=0, integer=True))
    work = _number(work_minutes, "work_minutes", 0)
    work_int = int(round(float(work)))
    if total_kg is not None and load_per_trip_kg is not None:
        breakdown = estimate_transport_trips(total_kg, load_per_trip_kg)
        if breakdown["trips"] != trips:
            raise InputError("trip_count: 与给定总重和载量算出的趟数不一致，禁止把 kg 当作 remaining 排完")
    elif load_per_trip_kg is not None:
        breakdown = _trip_context_from_remaining(trips, load_per_trip_kg)
    else:
        breakdown = _trip_context_from_remaining(trips, None)
    slices = []
    if trips > 0 and work_int > 0:
        base, extra = divmod(work_int, trips)
        for index in range(trips):
            minutes = base + (1 if index < extra else 0)
            if minutes <= 0:
                continue
            role = "tail" if breakdown.get("tail_trips") and index == trips - 1 else "full"
            slice_row = {"kind": "work", "minutes": minutes, "trip_index": index + 1,
                         "trip_count": trips, "trip_role": role}
            if role == "tail" and breakdown.get("last_trip_kg") is not None:
                slice_row["trip_load_kg"] = breakdown["last_trip_kg"]
            elif breakdown.get("load_per_trip_kg") is not None:
                slice_row["trip_load_kg"] = breakdown["load_per_trip_kg"]
            slices.append(slice_row)
    return {**breakdown, "session_trips": trips, "work_minutes": float(work), "slices": slices,
            "session_overhead_multiplier": 1,
            "loading_rule": "净用时按趟；setup/outbound/cleanup/return/buffer 每次出工只计一次，不按趟重复装卸"}


OBSERVATION_CALIBER_FIELDS = frozenset({"time_range", "timezone", "basis", "source"})


def _caliber_civil_date(value, path):
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{path}: 必须为 YYYY-MM-DD 民用日期")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise InputError(f"{path}: 必须为 YYYY-MM-DD 民用日期")
    if parsed.isoformat() != text:
        raise InputError(f"{path}: 必须为 YYYY-MM-DD 民用日期")
    return parsed


def _review_time_window(caliber):
    """校验并解析「同口径」时间范围；缺省 None 表示不做时间范围筛选。

    口径窗口必须显式给出依据与非占位来源，且结构正确；否则拒绝（fail-closed），
    不静默回退成“跨时间合并有效”。
    """
    if caliber is None:
        return None
    if not isinstance(caliber, dict):
        raise InputError("caliber: 必须为对象，或省略表示不做时间范围筛选")
    unknown = sorted(set(caliber) - OBSERVATION_CALIBER_FIELDS)
    if unknown:
        raise InputError(f"caliber: 未知字段 {unknown}")
    window = caliber.get("time_range")
    if not isinstance(window, dict):
        raise InputError("caliber.time_range: 必须为 {start,end} 对象")
    unknown = sorted(set(window) - {"start", "end"})
    if unknown:
        raise InputError(f"caliber.time_range: 未知字段 {unknown}")
    start = _caliber_civil_date(window.get("start"), "caliber.time_range.start")
    end = _caliber_civil_date(window.get("end"), "caliber.time_range.end")
    if start > end:
        raise InputError("caliber.time_range: start 不得晚于 end")
    timezone = resolve_timezone(caliber.get("timezone"), "caliber.timezone")
    basis = caliber.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        raise InputError("caliber.basis: 必须说明认定同一口径的依据")
    source = caliber.get("source")
    if not isinstance(source, str) or not source.strip() or is_placeholder_source(source):
        raise InputError("caliber.source: 必须说明口径来源，不得为占位词")
    return {"start": start, "end": end, "timezone": timezone, "basis": basis, "source": source}


def _observation_civil_date(observation, window, path):
    """观测的当地民用日期；无 observed_at 返回 None（不猜、不补零）。"""
    value = observation.get("observed_at")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{path}.observed_at: 必须为 YYYY-MM-DD 或带时区 ISO 瞬时")
    text = value.strip()
    if "T" not in text and " " not in text:
        return _caliber_civil_date(text, f"{path}.observed_at")
    try:
        moment = parse_time(text)
    except InputError:
        raise InputError(f"{path}.observed_at: 带时钟的观测必须包含时区偏移")
    return local_civil_date(moment, window["timezone"])


def _window_report(window):
    if window is None:
        return None
    return {"start": window["start"].isoformat(), "end": window["end"].isoformat(),
            "timezone": str(window["timezone"]), "basis": window["basis"], "source": window["source"]}


def review_observations(task, worker_id, observations, *, caliber=None) -> dict:
    """观测须含完成量、净分钟、方法、人员、口径与来源；不自动替换原速率。

    每条支持 id、task_id、worker_id、method、scope、quantity、minutes、source、
    status（completed/partial/interrupted/not_done/unknown）与可选 observed_at。
    仅同一任务、同人同法同口径（且落在同一口径时间范围内）的有完成量记录参与描述性更新。
    可选 caliber 给出同口径时间范围；超出范围或缺观测日期的记录被排除并注明原因，
    不静默合并。中断与慢记录只要在同一口径内仍保留并只扩大描述性区间。
    """
    estimate = estimate_task(task, worker_id)
    window = _review_time_window(caliber)
    if not isinstance(observations, list):
        raise InputError("observations: 必须为数组")
    records = []
    rates = []
    total_quantity = total_minutes = 0.0
    for i, observation in enumerate(observations):
        path = f"observations[{i}]"
        if not isinstance(observation, dict):
            raise InputError(f"{path}: 必须为对象")
        quantity, unit = canonical_quantity(observation.get("quantity"))
        minutes = _number(observation.get("minutes"), f"{path}.minutes", 0)
        source = observation.get("source")
        if not isinstance(source, str) or not source.strip() or is_placeholder_source(source):
            raise InputError(f"{path}.source: 必须说明观测或回忆来源，不得为占位词")
        status = observation.get("status", "unknown")
        if status not in {"completed", "partial", "interrupted", "not_done", "unknown"}:
            raise InputError(f"{path}.status: 无效状态")
        reasons = []
        if observation.get("task_id") != task.get("id") or not task.get("id"):
            reasons.append("different_task")
        if observation.get("worker_id") != worker_id:
            reasons.append("different_worker")
        if observation.get("method") != task.get("method"):
            reasons.append("different_method")
        if observation.get("scope") != "net_work":
            reasons.append("not_net_work")
        if unit != estimate["unit"]:
            reasons.append("different_unit")
        if quantity == 0 or status == "not_done":
            reasons.append("no_completed_quantity")
        if status == "unknown":
            reasons.append("unknown_status")
        if quantity > 0 and minutes == 0:
            reasons.append("zero_minutes_with_output")
        observed_date = None
        if window is not None:
            observed_date = _observation_civil_date(observation, window, path)
            if observed_date is None:
                reasons.append("caliber_time_missing")
            elif observed_date < window["start"] or observed_date > window["end"]:
                reasons.append("outside_time_range")
        usable = not reasons
        observed_rate = minutes / quantity if usable else None
        if usable:
            _number(observed_rate, f"{path}.observed_rate", positive=True)
            rates.append(observed_rate)
            total_quantity += quantity
            total_minutes += minutes
            _number(total_quantity, "observations.total_quantity", positive=True)
            _number(total_minutes, "observations.total_minutes", positive=True)
        records.append({"id": observation.get("id", str(i)), "status": status, "source": source,
                        "observed_date": observed_date.isoformat() if observed_date else None,
                        "included": usable, "exclusion_reasons": reasons, "observed_rate": observed_rate})
    observed_low, observed_high = (min(rates), max(rates)) if rates else (None, None)
    # 少量快速记录不收紧既有慢情形；慢记录只扩大描述性区间。
    planning_low, planning_high = estimate["low_rate"], estimate["high_rate"]
    if rates and estimate["schedulable"]:
        planning_low = min(planning_low, observed_low)
        planning_high = max(planning_high, observed_high)
    return {"unit": estimate["unit"], "observations": records, "included_count": len(rates),
            "excluded_count": len(records) - len(rates),
            "observed_rate_min": observed_low, "observed_rate_max": observed_high,
            "quantity_weighted_rate": total_minutes / total_quantity if total_quantity else None,
            "suggested_rate_low": planning_low, "suggested_rate_high": planning_high,
            "interval_kind": "descriptive_not_probability_interval",
            "time_range_applied": window is not None,
            "time_range": _window_report(window),
            "excluded_outside_time_range": sum(
                1 for row in records if "outside_time_range" in row["exclusion_reasons"]),
            "excluded_missing_time": sum(
                1 for row in records if "caliber_time_missing" in row["exclusion_reasons"]),
            "note": "按实际完成量复盘；中断与慢记录保留。时间范围缺省不筛选，给出 caliber 时"
                    "仅纳入同一口径时间范围内的记录。范围没有统计覆盖保证，输出不会修改任务或批准劳动。"}


PLAN_COPY_MARKERS = frozenset({
    "copied_from_plan", "copied_from_plan_sessions", "copied_from_schedule",
})
OCCURRED_STATUSES = frozenset({
    "completed", "partial", "interrupted", "not_done", "unknown",
})
ACTIVE_PHASE_KINDS = frozenset({
    "setup", "outbound", "work", "cleanup", "return", "buffer",
})


def _explicit_active_minutes(event):
    if "active_minutes_occurred" in event:
        return event.get("active_minutes_occurred"), True
    if "active_minutes" in event:
        return event.get("active_minutes"), True
    return None, False


def accumulate_occurred_load(events, timezone, worker_id=None) -> dict:
    """把已发生作业事件按当地民用日合计活动分钟。

    禁止从建议 sessions 抄入；未知不得当 0；不扣减 remaining。
    返回的 used_active_minutes_by_date 可写入 schema 1.0 同名字段。
    """
    resolve_timezone(timezone)
    if not isinstance(events, list):
        raise InputError("events: 必须为数组")
    used = {}
    unknown = {}
    records = []

    def add_used(wid, day, minutes):
        worker_map = used.setdefault(wid, {})
        total = worker_map.get(day, 0.0) + minutes
        _number(total, f"used_active_minutes_by_date.{wid}.{day}", 0, 1440)
        worker_map[day] = total

    def add_unknown(wid, day):
        unknown.setdefault(wid, set()).add(day)

    for index, event in enumerate(events):
        path = f"events[{index}]"
        if not isinstance(event, dict):
            raise InputError(f"{path}: 必须为对象")
        remaining_after = event.get("remaining_after")
        reasons = []
        kind = event.get("kind", "work_event")
        if kind != "work_event":
            reasons.append("not_work_event")
        for field in ("clock_source", "clock_derivation", "load_derivation", "quantity_derivation"):
            marker = event.get(field)
            if isinstance(marker, str) and marker.strip().casefold() in PLAN_COPY_MARKERS:
                reasons.append("copied_from_plan")
                break
        if not event.get("clock_source"):
            reasons.append("missing_clock_source")
        wid = event.get("worker_id")
        if not isinstance(wid, str) or not wid.strip():
            reasons.append("missing_worker")
            wid = None
        elif worker_id is not None and wid != worker_id:
            reasons.append("different_worker")
        status = event.get("status", "unknown")
        if status not in OCCURRED_STATUSES:
            raise InputError(f"{path}.status: 无效状态")
        start = event.get("actual_start", event.get("start"))
        end = event.get("actual_end", event.get("end"))
        active_by_date = {}
        if start is None or end is None:
            reasons.append("missing_time")
        elif "copied_from_plan" not in reasons and "not_work_event" not in reasons:
            clock = utc_minutes(start, end)
            _number(clock, f"{path}.clock_minutes", 0)
            phases = event.get("phases")
            explicit, explicit_present = _explicit_active_minutes(event)
            if status == "unknown" or (explicit_present and explicit is None):
                reasons.append("unknown_load")
                if wid:
                    for day, _span in iter_local_date_spans(start, end, timezone):
                        add_unknown(wid, day)
            elif status == "not_done":
                if explicit not in (None, 0, 0.0):
                    reasons.append("not_done_with_minutes")
                else:
                    for day, _span in iter_local_date_spans(start, end, timezone):
                        active_by_date[day] = 0.0
            elif isinstance(phases, list) and phases:
                for phase_index, phase in enumerate(phases):
                    phase_path = f"{path}.phases[{phase_index}]"
                    if not isinstance(phase, dict):
                        raise InputError(f"{phase_path}: 必须为对象")
                    phase_kind = phase.get("kind")
                    if phase_kind in {"rest", "wait"}:
                        continue
                    if phase_kind not in ACTIVE_PHASE_KINDS:
                        raise InputError(f"{phase_path}.kind: 未知阶段")
                    for day, minutes in iter_local_date_spans(
                            phase.get("start"), phase.get("end"), timezone):
                        active_by_date[day] = active_by_date.get(day, 0.0) + minutes
            else:
                spans = list(iter_local_date_spans(start, end, timezone))
                if explicit_present:
                    minutes = _number(explicit, f"{path}.active_minutes", 0, 1440 * max(1, len(spans)))
                    if len(spans) != 1:
                        raise InputError(f"{path}: 跨日已发生负荷缺少可切分阶段，不能把总分钟摊到各日")
                    if minutes - EPS > clock:
                        raise InputError(f"{path}.active_minutes: 不得超过钟表时长")
                    active_by_date[spans[0][0]] = float(minutes)
                else:
                    for day, minutes in spans:
                        active_by_date[day] = minutes

        usable = not reasons
        if usable and wid:
            for day, minutes in active_by_date.items():
                if day in unknown.get(wid, set()):
                    raise InputError(f"{path}: 同一当地日不能既有已记录负荷又标记未知")
                add_used(wid, day, minutes)
        records.append({
            "id": event.get("event_id", event.get("id", str(index))),
            "worker_id": wid,
            "status": status,
            "included": usable,
            "exclusion_reasons": reasons,
            "active_minutes_by_date": dict(active_by_date) if usable else {},
            "remaining_after_input": remaining_after,
        })

    for wid, days in unknown.items():
        for day in days:
            if day in used.get(wid, {}):
                raise InputError(f"occurred_load.{wid}.{day}: 未知不得与已记录分钟并存，也不得当 0")

    return {
        "timezone": timezone if isinstance(timezone, str) else str(timezone),
        "used_active_minutes_by_date": {wid: dict(days) for wid, days in used.items()},
        "unknown_dates": {wid: sorted(days) for wid, days in unknown.items()},
        "events": records,
        "included_count": sum(1 for row in records if row["included"]),
        "excluded_count": sum(1 for row in records if not row["included"]),
        "remaining_quantity_unchanged": True,
        "note": "已发生活动分钟按当地民用日切分，供 used_active_minutes_by_date 使用。"
                "不是建议 sessions，不扣 remaining，不是健康许可。未知日期不写入 0。",
    }
