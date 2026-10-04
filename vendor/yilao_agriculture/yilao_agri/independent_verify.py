"""R15-A10 独立全局计划复查器（只读）。

设计要点：**刻意不复用** engine / audit / environment / workload / models 的任何判断函数。
时间解析、单位归一（mu→sqm）、区间覆盖、出工合并、量子分段、当地日界切分、
可行性的必要条件（下界证书）都在本文件内独立实现。这样它与 ``audit.verify_plan``
构成两条互不相同的判断路径，可用于交叉核对，而不是同一函数自证。

本模块只读，不改写请求或结果；**不证明最优性、不证明现场真实性、不构成健康许可**，
也不把"有界搜索未找到"当成"已证明无解"（原要求 U16）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, time as _time_of_day
from math import isfinite
from zoneinfo import ZoneInfo

EPS = 1e-6
SQM_PER_MU = 2000.0 / 3.0
ACTIVE_PHASE_KINDS = frozenset({"setup", "outbound", "work", "cleanup", "return", "buffer"})
REST_KINDS = frozenset({"rest"})
INTEGER_UNITS = frozenset({"trip", "plant"})
UNITS = frozenset({"mu", "sqm", "kg", "trip", "plant", "m", "m3"})
# 与 models.SOURCE_PLACEHOLDERS 语义一致的独立副本（不复用其常量）。
PLACEHOLDER_SOURCES = frozenset({
    "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未确认", "未知", "待定", "待核实", "待补充", "待填写",
})


# ---------- 独立基础工具 ----------

def _parse(value):
    if not isinstance(value, str):
        raise ValueError("时间必须为字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("时间缺少时区")
    return moment.astimezone(timezone.utc)


def _minutes(start, end):
    return (end - start).total_seconds() / 60.0


def _num(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("必须为数值")
    if not isfinite(value):
        raise ValueError("必须为有限值")
    if positive and value <= 0:
        raise ValueError("必须为正数")
    return float(value)


def _canonical(quantity):
    if not isinstance(quantity, dict):
        raise ValueError("quantity 必须为对象")
    unit = quantity.get("unit")
    if unit not in UNITS:
        raise ValueError("不支持的单位: %r" % (unit,))
    value = _num(quantity.get("value"), positive=False)
    return value * (SQM_PER_MU if unit == "mu" else 1.0), ("sqm" if unit == "mu" else unit)


def _is_placeholder(text):
    if not isinstance(text, str):
        return True
    return " ".join(text.split()).casefold() in PLACEHOLDER_SOURCES


def _task_rate(task, worker_id):
    """独立读取该劳动者净速率；返回 None 表示不可精细排程。"""
    rates = task.get("rates")
    if not isinstance(rates, dict) or worker_id not in rates:
        return None
    rate = rates[worker_id]
    if not isinstance(rate, dict):
        return None
    unit = rate.get("unit")
    factor = SQM_PER_MU if unit == "mu" else 1.0
    try:
        low = _num(rate.get("low"), positive=True) / factor
        high = _num(rate.get("high"), positive=True) / factor
    except ValueError:
        return None
    scope = rate.get("scope")
    source_ok = not _is_placeholder(rate.get("source"))
    return {"low_rate": low, "high_rate": high, "unit": "sqm" if unit == "mu" else unit,
            "scope": scope, "schedulable": scope == "net_work" and source_ok,
            "source_placeholder": not source_ok}


def _intervals(items):
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            out.append((_parse(item["start"]), _parse(item["end"])))
        except (KeyError, ValueError):
            continue
    return out


def _covered(intervals, start, end):
    """首尾相接允许合并，缺口不能跨过。"""
    cursor = start
    for lo, hi in sorted(intervals):
        if hi <= cursor:
            continue
        if lo > cursor:
            return False
        cursor = max(cursor, hi)
        if cursor >= end:
            return True
    return False


def _local_spans(start, end, tzname):
    zone = ZoneInfo(tzname)
    cursor, out = start, []
    guard = 0
    while cursor < end:
        guard += 1
        if guard > 64:
            raise ValueError("当地日界切分异常")
        day = cursor.astimezone(zone).date()
        nxt = datetime.combine(day + timedelta(days=1), _time_of_day.min, tzinfo=zone)
        nxt = nxt.astimezone(timezone.utc)
        if nxt <= cursor:
            raise ValueError("当地日界未前进")
        segment_end = min(nxt, end)
        out.append((day.isoformat(), _minutes(cursor, segment_end)))
        cursor = segment_end
    return out


def _quantum(task, unit, high_rate, step_minutes):
    remaining = task.get("remaining_quantity") if isinstance(task.get("remaining_quantity"), dict) else {}
    step = task.get("quantity_step")
    if step is not None:
        try:
            base = _num(step, positive=True)
        except ValueError:
            return None
        return base * (SQM_PER_MU if remaining.get("unit") == "mu" else 1.0)
    if unit in INTEGER_UNITS:
        return 1.0
    if not high_rate:
        return None
    return float(step_minutes) / float(high_rate)


def _chunk_problem(quantity, remaining_here, min_chunk, high_rate, unit, quantum):
    """独立复算分段合法性；返回问题描述或 None。"""
    if quantity <= EPS or quantity > remaining_here + EPS:
        return "分段数量越界（%g / 剩余 %g）" % (quantity, remaining_here)
    if unit in INTEGER_UNITS and abs(quantity - round(quantity)) > EPS:
        return "整数单位出现小数分段"
    is_tail = quantity >= remaining_here - EPS
    if unit in INTEGER_UNITS and quantum:
        steps = quantity / quantum
        if abs(steps - round(steps)) > 1e-6:
            return "未对齐分步"
    if is_tail:
        return None
    if high_rate is None or min_chunk is None or not quantum:
        return "缺少速率/最小段/量子信息，无法确认非尾段"
    if quantity * high_rate < min_chunk - EPS:
        return "非尾段短于 min_chunk_minutes"
    steps = quantity / quantum
    if abs(steps - round(steps)) > 1e-6:
        return "未对齐 quantity_step 量子"
    return None


# ---------- 主复查 ----------

def verify_plan_independent(raw, result):
    """独立复查一个计划是否满足请求中的硬约束。

    返回 ``{"valid": bool, "errors": [{"code","detail"}], "counts": {...},
    "objective": {...}}``；errors 去重排序。
    """
    errors = []
    checks_run = 0

    def add(code, detail):
        errors.append({"code": code, "detail": detail})

    if not isinstance(result, dict):
        return {"valid": False, "errors": [{"code": "RESULT_NOT_OBJECT", "detail": "结果必须为对象"}],
                "counts": {"sessions": 0, "tasks": 0}, "objective": None}
    if not isinstance(raw, dict):
        return {"valid": False, "errors": [{"code": "REQUEST_NOT_OBJECT", "detail": "请求必须为对象"}],
                "counts": {"sessions": 0, "tasks": 0}, "objective": None}

    tasks = {t["id"]: t for t in raw.get("tasks", []) if isinstance(t, dict) and "id" in t}
    workers = {w["id"]: w for w in raw.get("workers", []) if isinstance(w, dict) and "id" in w}
    resources = {r["id"]: r for r in raw.get("resources", []) if isinstance(r, dict) and "id" in r}
    try:
        tzname = raw["timezone"]
        ZoneInfo(tzname)
        horizon_start = _parse(raw["horizon_start"])
        horizon_end = _parse(raw["horizon_end"])
        step_minutes = float(raw.get("step_minutes", 15))
    except Exception:
        return {"valid": False, "errors": [{"code": "REQUEST_UNREADABLE", "detail": "请求的时区/范围无法解析"}],
                "counts": {"sessions": 0, "tasks": len(tasks)}, "objective": None}

    # 输出身份与输入绑定（防替换/改标）。
    digest = result.get("request_sha256")
    checks_run += 1
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        add("REQUEST_SHA256_INVALID", "request_sha256 缺失或非 64 位十六进制")
    else:
        checks_run += 1
        if result.get("plan_id") != "plan-" + digest[:16]:
            add("PLAN_ID_MISBIND", "plan_id 与 request_sha256 前缀不一致")
    checks_run += 1
    if result.get("mode") != raw.get("mode"):
        add("MODE_MISBIND", "输出 mode=%r 与请求 mode=%r 不一致" % (result.get("mode"), raw.get("mode")))

    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        add("SESSIONS_NOT_ARRAY", "sessions 必须为数组")
        sessions = []
    task_results = result.get("task_results")
    if not isinstance(task_results, list):
        add("TASK_RESULTS_NOT_ARRAY", "task_results 必须为数组")
        task_results = []

    daily = {}
    amounts = {}
    worker_spans = {}
    resource_events = {}
    session_records = []

    for index, session in enumerate(sessions):
        label = "sessions[%d]" % index
        checks_run += 1
        if not isinstance(session, dict):
            add("SESSION_NOT_OBJECT", label + " 必须为对象")
            continue
        try:
            tid = session["task_id"]
            wid = session["worker_id"]
            start = _parse(session["start"])
            end = _parse(session["end"])
        except (KeyError, ValueError):
            add("SESSION_UNREADABLE", label + " 缺少 task_id/worker_id 或时间不可解析")
            continue
        if tid not in tasks:
            add("SESSION_UNKNOWN_TASK", label + " 引用请求外任务 %r" % (tid,))
            continue
        if wid not in workers:
            add("SESSION_UNKNOWN_WORKER", label + " 引用请求外劳动者 %r" % (wid,))
            continue
        task, worker = tasks[tid], workers[wid]
        try:
            requested, requested_unit = _canonical(task.get("remaining_quantity"))
        except ValueError:
            add("TASK_QUANTITY_INVALID", label + " 任务剩余量不可解析")
            requested, requested_unit = 0.0, None
        quantity, unit = None, None
        try:
            quantity, unit = _canonical({"value": session.get("quantity"), "unit": session.get("unit")})
        except ValueError:
            pass
        if unit != requested_unit:
            add("UNIT_MISMATCH", label + " 单位 %r 与请求规范单位 %r 不一致" % (session.get("unit"), requested_unit))
        if quantity is None or quantity <= 0:
            add("QUANTITY_INVALID", label + " 数量无效")

        # 范围与农时窗口。
        checks_run += 1
        if not (horizon_start <= start < end <= horizon_end):
            add("OUT_OF_HORIZON", label + " 超出规划范围")
        try:
            earliest, deadline = _parse(task["earliest_start"]), _parse(task["deadline"])
            if start < earliest or end > deadline:
                add("OUT_OF_WINDOW", label + " 超出农时窗口")
        except (KeyError, ValueError):
            add("TASK_WINDOW_UNREADABLE", label + " 任务窗口不可解析")

        # 可用性覆盖。
        checks_run += 1
        if not _covered(_intervals(worker.get("availability")), start, end):
            add("WORKER_UNAVAILABLE", label + " 劳动者不可用")
        # 独立按声明时间戳复核，不调用 models 的资料判定函数。
        profile = worker.get("worker_profile")
        if raw.get("schema_version") == "1.1" or profile is not None:
            try:
                if not isinstance(profile, dict) or profile.get("status") != "valid" or _is_placeholder(profile.get("source")):
                    raise ValueError("资料未确认")
                for observed, until in (("observed_at", "valid_until"), ("state_observed_at", "state_valid_until")):
                    if _parse(profile[observed]) > start or _parse(profile[until]) < end:
                        raise ValueError("有效期未覆盖本段")
            except (ValueError, KeyError, TypeError):
                add("WORKER_PROFILE_INVALID", label + " 人员资料或当前状态未覆盖整段出工")

        # 阶段：连续、无缺口/重叠/越界、已知类型、休息与持续活动。
        phases = session.get("phases")
        if not isinstance(phases, list) or not phases:
            add("PHASES_MISSING", label + " 缺少 phases")
            continue
        cursor = start
        continuous = 0.0
        phase_minutes = {}
        ok_phases = True
        for phase in phases:
            checks_run += 1
            if not isinstance(phase, dict):
                add("PHASE_NOT_OBJECT", label + " 阶段必须为对象")
                ok_phases = False
                continue
            try:
                lo, hi = _parse(phase["start"]), _parse(phase["end"])
            except (KeyError, ValueError):
                add("PHASE_UNREADABLE", label + " 阶段时间不可解析")
                ok_phases = False
                continue
            if lo != cursor or hi <= lo or hi > end:
                add("PHASE_GAP_OR_OVERLAP", label + " 阶段有缺口、重叠或越界")
            cursor = hi
            kind = phase.get("kind")
            minutes = _minutes(lo, hi)
            phase_minutes[kind] = phase_minutes.get(kind, 0.0) + minutes
            if kind == "rest":
                if minutes + EPS < float(worker["limits"]["min_rest_minutes"]):
                    add("REST_TOO_SHORT", label + " 休息不足")
                else:
                    continuous = 0.0
            elif kind == "wait":
                pass  # 等待占用时间，但既不算劳动也不重置连续活动。
            elif kind in ACTIVE_PHASE_KINDS:
                continuous += minutes
                if continuous > float(worker["limits"]["max_continuous_active_minutes"]) + EPS:
                    add("CONTINUOUS_LIMIT", label + " 超过持续活动限制")
                for day, span in _local_spans(lo, hi, tzname):
                    daily[(wid, day)] = daily.get((wid, day), 0.0) + span
            else:
                add("UNKNOWN_PHASE_KIND", label + " 未知阶段 %r" % (kind,))
        if cursor != end:
            add("PHASE_NOT_COVERING", label + " 阶段未覆盖出工结束")

        # 内部汇总自洽（独立复算）。
        clock = sum(phase_minutes.values())
        active = clock - phase_minutes.get("rest", 0.0) - phase_minutes.get("wait", 0.0)
        duty = task.get("wait_duty") or {}
        expected_wait = duty.get("minutes", 0) if duty.get("status") == "known" else 0
        if duty and duty.get("status") != "known":
            add("WAIT_STATUS_UNKNOWN", label + " 未知等待不能生成安排")
        if abs(phase_minutes.get("wait", 0) - expected_wait) > EPS:
            add("WAIT_MINUTES_MISMATCH", label + " 等待时长与输入不一致")
        for field, value in (("active_minutes", active), ("rest_minutes", phase_minutes.get("rest", 0.0)),
                             ("clock_minutes", clock)):
            if not isinstance(session.get(field), (int, float)) or abs(session.get(field) - value) > EPS:
                add("SESSION_TOTAL_MISMATCH", label + " " + field + " 与阶段复算不一致")
        # 每次出工附加时间必须完整计入（不弱化既有硬约束）。
        session_spec = task.get("session") or {}
        for kind in ("setup", "outbound", "return", "cleanup", "buffer"):
            need = float(session_spec.get(kind + "_minutes", 0) or 0)
            if phase_minutes.get(kind, 0.0) + EPS < need:
                add("MISSING_ADDON", label + " 漏计 " + kind)
        # 面积/体力口径：净作业时间不得低于保守下界。
        rate = _task_rate(task, wid)
        if rate and rate["schedulable"] and quantity:
            if phase_minutes.get("work", 0.0) + EPS < quantity * rate["high_rate"]:
                add("INSUFFICIENT_NET_WORK", label + " 净作业时间不足以完成所报数量")

        # 资源。
        selected = session.get("resources")
        if not isinstance(selected, list):
            add("RESOURCES_NOT_ARRAY", label + " resources 必须为数组")
            selected = []
        if len(set(selected)) != len(selected):
            add("RESOURCE_DUPLICATE", label + " 资源重复")
        if not set(task.get("required_resources", [])) <= set(selected):
            add("RESOURCE_MISSING_REQUIRED", label + " 缺少任务必需资源")
        for rid in selected:
            if rid not in resources:
                add("RESOURCE_UNKNOWN", label + " 引用请求外资源 %r" % (rid,))
                continue
            if not _covered(_intervals(resources[rid].get("availability")), start, end):
                add("RESOURCE_UNAVAILABLE", label + " 资源 %r 不可用" % (rid,))

        amounts[tid] = amounts.get(tid, 0.0) + (quantity or 0.0)
        worker_spans.setdefault(wid, []).append((start, end, label, tid))
        for rid in selected:
            if rid in resources:
                resource_events.setdefault(rid, []).append((start, 1, label))
                resource_events.setdefault(rid, []).append((end, -1, label))
        session_records.append({"label": label, "task_id": tid, "worker_id": wid,
                                "start": start, "end": end, "quantity": quantity, "unit": unit,
                                "resources": selected, "phases": phases, "session": session})

    # 必要资源类型（policy 级）。
    required_kinds = set((raw.get("policy") or {}).get("required_resource_kinds", []) or [])
    for record in session_records:
        kinds = {resources[r]["kind"] for r in record["resources"] if r in resources}
        if not required_kinds <= kinds:
            add("REQUIRED_KIND_MISSING", record["label"] + " 缺少必要资源类型 %s" % sorted(required_kinds - kinds))

    # 同人出工冲突与间隔休息。
    for wid, spans in worker_spans.items():
        rest = timedelta(minutes=float(workers[wid]["limits"]["min_rest_minutes"]))
        ordered = sorted(spans)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a_start, a_end = ordered[i][0], ordered[i][1]
                b_start, b_end = ordered[j][0], ordered[j][1]
                if not (a_end + rest <= b_start or b_end + rest <= a_start):
                    add("WORKER_CONFLICT", "%s: %s / %s 出工冲突或间隔休息不足"
                        % (wid, ordered[i][2], ordered[j][2]))

    # 同任务并行（默认串行）。
    by_task = {}
    for record in session_records:
        by_task.setdefault(record["task_id"], []).append(record)
    for tid, records in by_task.items():
        spec = tasks[tid].get("parallel_within_task")
        allowed = (isinstance(spec, dict) and spec.get("allowed") is True
                   and spec.get("review_status") in ({"confirmed", "illustrative"}
                                                     if raw.get("mode") == "demonstration" else {"confirmed"})
                   and not _is_placeholder(spec.get("source")))
        ordered = sorted(records, key=lambda r: r["start"])
        for i in range(len(ordered) - 1):
            if ordered[i]["end"] > ordered[i + 1]["start"] and not allowed:
                add("SAME_TASK_PARALLEL", tid + ": 同任务不支持并行分段")

    # 分段量子与最小段（独立复算）。
    for tid, records in by_task.items():
        task = tasks[tid]
        remaining, _ = _canonical(task["remaining_quantity"])
        unit = _canonical(task["remaining_quantity"])[1]
        for record in records:
            rate = _task_rate(task, record["worker_id"])
            high_rate = rate["high_rate"] if rate else None
            quantum = _quantum(task, unit, high_rate, step_minutes)
            problem = _chunk_problem(record["quantity"], remaining, task.get("min_chunk_minutes"),
                                     high_rate, unit, quantum)
            if problem:
                add("CHUNK_INVALID", record["label"] + " " + problem)
            remaining -= record["quantity"]
        if not task.get("divisible", True) and len(records) > 1:
            add("INDIVISIBLE_SPLIT", tid + ": 不可分段任务被拆分")

    # 数量不超过剩余量。
    for tid, task in tasks.items():
        requested = _canonical(task["remaining_quantity"])[0]
        if amounts.get(tid, 0.0) > requested + EPS:
            add("OVER_SCHEDULED", tid + ": 安排量超过剩余量")

    # 依赖：前置必须全部完成且返回结束早于后继开始。
    for tid, task in tasks.items():
        deps = task.get("depends_on", []) or []
        for record in by_task.get(tid, []):
            for dep in deps:
                if dep not in tasks:
                    add("DEPENDENCY_UNKNOWN", tid + ": 前置任务 %r 不在请求中" % (dep,))
                    continue
                need = _canonical(tasks[dep]["remaining_quantity"])[0]
                if amounts.get(dep, 0.0) + EPS < need:
                    add("DEPENDENCY_INCOMPLETE", tid + ": 前置 " + dep + " 未全部完成")
                for other in by_task.get(dep, []):
                    if other["end"] > record["start"]:
                        add("DEPENDENCY_ORDER", tid + ": 开始早于前置 " + dep + " 返回结束")

    # 初始休息：未确认已休息时首段出工必须先休息。
    first_by_worker = {}
    for record in session_records:
        wid = record["worker_id"]
        if wid not in first_by_worker or record["start"] < first_by_worker[wid]["start"]:
            first_by_worker[wid] = record
    for wid, record in first_by_worker.items():
        if workers[wid].get("initial_rest_confirmed", False):
            continue
        phases = record["phases"]
        first_kind = phases[0].get("kind") if phases and isinstance(phases[0], dict) else None
        if first_kind != "rest":
            add("INITIAL_REST_MISSING", wid + ": 初始休息未确认且未安排")

    # 每日累计活动。
    for (wid, day), minutes in daily.items():
        used = float((workers[wid].get("used_active_minutes_by_date") or {}).get(day, 0) or 0)
        limit = float(workers[wid]["limits"]["max_active_minutes_per_day"])
        if minutes + used > limit + EPS:
            add("DAILY_LIMIT", "%s/%s 超过当日累计活动限制" % (wid, day))

    # 资源容量。
    for rid, events in resource_events.items():
        capacity = float(resources[rid].get("capacity", 1))
        count = 0
        for _, change, _label in sorted(events, key=lambda e: (e[0], -e[1])):
            count += change
            if count > capacity + EPS:
                add("RESOURCE_CAPACITY", rid + ": 共享容量超限")
                break

    # task_results 绑定：幽灵行、逐项一致、完成语义。
    results_by_task = {}
    for index, entry in enumerate(task_results):
        checks_run += 1
        if not isinstance(entry, dict):
            add("TASK_RESULT_NOT_OBJECT", "task_results[%d] 必须为对象" % index)
            continue
        tid = entry.get("task_id")
        if tid not in tasks:
            add("GHOST_TASK_RESULT", "task_results[%d] 幽灵行，task_id=%r 不在请求中" % (index, tid))
            continue
        results_by_task.setdefault(tid, []).append(entry)

    for tid, task in tasks.items():
        requested, requested_unit = _canonical(task["remaining_quantity"])
        scheduled = amounts.get(tid, 0.0)
        entries = results_by_task.get(tid, [])
        if len(entries) != 1:
            add("TASK_RESULT_COUNT", tid + ": 任务结果条目数 %d 不等于 1" % len(entries))
            continue
        entry = entries[0]
        for field, expected in (("scheduled_quantity", scheduled), ("remaining_quantity", requested - scheduled),
                                ("requested_quantity", requested)):
            if field in entry and (not isinstance(entry[field], (int, float))
                                   or abs(entry[field] - expected) > EPS):
                add("TASK_RESULT_MISMATCH", tid + ": " + field + " 与时间轴复算不一致")
        if "unit" in entry and entry["unit"] != requested_unit:
            add("TASK_RESULT_UNIT_MISMATCH", tid + ": 单位与请求不一致")
        if "completion_ratio" in entry:
            expected_ratio = scheduled / requested if requested > EPS else 1.0
            if not isinstance(entry["completion_ratio"], (int, float)) \
                    or abs(entry["completion_ratio"] - expected_ratio) > EPS:
                add("TASK_RESULT_RATIO_MISMATCH", tid + ": 完成度与时间轴不一致")
        declared_complete = entry.get("status") == "complete"
        if not declared_complete and isinstance(entry.get("remaining_quantity"), (int, float)) \
                and abs(entry["remaining_quantity"]) <= EPS:
            declared_complete = True
        if declared_complete and scheduled + EPS < requested:
            add("FALSE_COMPLETE", tid + ": 声明完成但未排满")

    if result.get("status") == "complete":
        for tid, task in tasks.items():
            if _canonical(task["remaining_quantity"])[0] - amounts.get(tid, 0.0) > EPS:
                add("FALSE_OVERALL_COMPLETE", tid + ": 总体声明完成但该任务未排满")

    objective = objective_vector(result, recompute=True, recomputed_active=sum(s.get("active_minutes", 0) for s in sessions if isinstance(s, dict)))
    # 汇总与独立复算对照（优化对照 / 独立审计）。
    summary = result.get("summary")
    if isinstance(summary, dict):
        recomputed = {
            "active_person_minutes": sum(float(r["session"].get("active_minutes", 0) or 0) for r in session_records),
            "rest_person_minutes": sum(float(r["session"].get("rest_minutes", 0) or 0) for r in session_records),
            "occupied_person_minutes": sum(float(r["session"].get("clock_minutes", 0) or 0) for r in session_records),
            "task_count": len(tasks),
            "completed_task_count": sum(1 for tid in tasks
                                        if amounts.get(tid, 0.0) + EPS >= _canonical(tasks[tid]["remaining_quantity"])[0]),
        }
        if session_records:
            recomputed["elapsed_plan_minutes"] = _minutes(min(r["start"] for r in session_records),
                                                          max(r["end"] for r in session_records))
        for field, value in recomputed.items():
            if field in summary and (not isinstance(summary[field], (int, float))
                                     or abs(summary[field] - value) > EPS):
                add("SUMMARY_MISMATCH", "summary.%s 与独立复算不一致（声明 %r / 复算 %r）"
                    % (field, summary[field], value))

    unique = sorted({(e["code"], e["detail"]) for e in errors})
    return {"valid": not unique,
            "errors": [{"code": c, "detail": d} for c, d in unique],
            "counts": {"sessions": len(session_records), "tasks": len(tasks), "checks": checks_run},
            "objective": objective}


def objective_vector(result, recompute=False, recomputed_active=None):
    """独立复算的排序目标分量，用于不同计划之间的对照（不读声明的分数）。"""
    if not isinstance(result, dict):
        return None
    sessions = [s for s in result.get("sessions", []) if isinstance(s, dict)]
    active = sum(float(s.get("active_minutes", 0) or 0) for s in sessions)
    ends, starts = [], []
    for s in sessions:
        try:
            starts.append(_parse(s["start"]))
            ends.append(_parse(s["end"]))
        except (KeyError, ValueError):
            continue
    return {"active_person_minutes": active,
            "session_count": len(sessions),
            "finish_utc": max(ends).isoformat() if ends else None,
            "elapsed_plan_minutes": _minutes(min(starts), max(ends)) if starts and ends else 0.0,
            "recomputed_active_person_minutes": recomputed_active}


# ---------- 可行性归因（原要求 U16） ----------

def _min_low_rate(task):
    rates = task.get("rates")
    if not isinstance(rates, dict):
        return None
    best = None
    for wid, rate in rates.items():
        info = _task_rate(task, wid)
        if info and info["schedulable"]:
            best = info["low_rate"] if best is None else min(best, info["low_rate"])
    return best


def infeasibility_certificates(raw):
    """只返回**可核对的下界证书**：满足即证明该输入下无法满足全部任务。

    证书必须是任何合法安排都绕不过的必要条件；未通过者一律不列入，避免把
    "有界搜索未找到"误当"已证无解"。
    """
    certs = []
    if not isinstance(raw, dict):
        return [{"code": "REQUEST_NOT_OBJECT", "detail": "请求不可读"}]
    try:
        horizon_start = _parse(raw["horizon_start"])
        horizon_end = _parse(raw["horizon_end"])
        tzname = raw.get("timezone")
    except (KeyError, ValueError):
        return []
    tasks = {t["id"]: t for t in raw.get("tasks", []) if isinstance(t, dict) and "id" in t}
    workers = {w["id"]: w for w in raw.get("workers", []) if isinstance(w, dict) and "id" in w}

    total_available = 0.0
    for wid, worker in workers.items():
        for lo, hi in _intervals(worker.get("availability")):
            lo2, hi2 = max(lo, horizon_start), min(hi, horizon_end)
            if hi2 > lo2:
                total_available += _minutes(lo2, hi2)

    total_lower_bound = 0.0
    for tid, task in tasks.items():
        remaining, unit = _canonical(task["remaining_quantity"])
        if remaining <= EPS:
            continue
        try:
            earliest, deadline = _parse(task["earliest_start"]), _parse(task["deadline"])
        except (KeyError, ValueError):
            continue
        window_lo, window_hi = max(earliest, horizon_start), min(deadline, horizon_end)
        if window_hi <= window_lo:
            certs.append({"code": "WINDOW_DISJOINT",
                          "detail": "%s: 农时窗口与规划范围无交集，剩余 %g %s 无法安排" % (tid, remaining, unit)})
            continue
        low_rate = _min_low_rate(task)
        if low_rate is None:
            certs.append({"code": "NO_SCHEDULABLE_RATE",
                          "detail": "%s: 无任何可精细排程的净速率（缺速率或来源待核实），无法生成安排" % tid})
            continue
        total_lower_bound += remaining * low_rate
        # 逐任务窗口内可用人分钟下界。
        window_available = 0.0
        for wid, worker in workers.items():
            for lo, hi in _intervals(worker.get("availability")):
                lo2, hi2 = max(lo, window_lo), min(hi, window_hi)
                if hi2 > lo2:
                    window_available += _minutes(lo2, hi2)
        if remaining * low_rate > window_available + EPS:
            certs.append({"code": "WINDOW_CAPACITY",
                          "detail": "%s: 窗口内可用人分钟 %g 少于净工作下界 %g 分钟"
                                    % (tid, window_available, remaining * low_rate)})
    if total_lower_bound > total_available + EPS and not certs:
        certs.append({"code": "TOTAL_CAPACITY",
                      "detail": "全部任务净工作下界 %g 分钟超过全部可用人分钟 %g"
                                % (total_lower_bound, total_available)})
    return certs


def classify_outcome(raw, result):
    """把结果归入四类，明确区分"已证无解"与"未证明无解"（原要求 U16）。"""
    report = verify_plan_independent(raw, result)
    certs = infeasibility_certificates(raw)
    if not report["valid"]:
        outcome = "PLAN_INVALID"
    elif certs:
        outcome = "INFEASIBLE_PROVEN"
    elif result.get("status") == "complete":
        outcome = "PLAN_VALID"
    else:
        outcome = "NO_PLAN_FOUND_NOT_PROVEN"
    return {"outcome": outcome, "valid": report["valid"],
            "certificates": certs, "errors": report["errors"],
            "note": {"PLAN_INVALID": "计划违反请求硬约束，必须拒绝。",
                     "INFEASIBLE_PROVEN": "存在可核对下界证书，任一合法安排都无法满足全部任务。",
                     "PLAN_VALID": "计划满足本复查覆盖的硬约束；不证明最优、不证明现场真实或健康安全。",
                     "NO_PLAN_FOUND_NOT_PROVEN": "有界搜索未找到安排，**不是**已证明无解；"
                                                 "不得据此声称任何方案都无法完成。"}[outcome]}


def compare_with_audit(raw, result, audit_report):
    """交叉核对本复查器与 audit.verify_plan 的判断，供独立审计留痕。"""
    mine = verify_plan_independent(raw, result)
    if not isinstance(audit_report, dict):
        return {"agreement": None, "note": "audit 报告不可读"}
    audit_valid = bool(audit_report.get("valid"))
    mine_valid = bool(mine["valid"])
    return {"agreement": audit_valid == mine_valid,
            "independent_valid": mine_valid, "audit_valid": audit_valid,
            "independent_error_count": len(mine["errors"]),
            "audit_error_count": len(audit_report.get("errors", []) or []),
            "only_independent": [e for e in mine["errors"]
                                 if not any(e["code"].split("_")[0] in str(a) for a in audit_report.get("errors", []) or [])],
            "note": "两条独立路径结论一致时仍不等于证明；不一致即为可审查线索。"}
