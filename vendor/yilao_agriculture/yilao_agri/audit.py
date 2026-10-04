"""用最终时间轴复查硬约束；不参与搜索，不证明最优性或健康效果。"""

from collections import defaultdict
from datetime import timedelta
from math import isfinite

from .engine import ACTIVE_PHASE_KINDS, classify_worker_conflict, needs_initial_rest
from .environment import assess_interval
from .models import (
    iter_local_date_spans, request_digest, resolve_timezone, resource_occupation,
    same_task_parallel_allowed, to_utc, validate_request, worker_profile_status,
)
from .workload import (
    MAX_SESSION_COUNT, canonical_quantity, chunk_is_allowed, estimate_task,
    prep_ledger_findings, quantity_quantum, session_overhead_pieces,
)


def _phase_minutes(phase):
    """单个阶段的时长（分钟）。"""
    return (to_utc(phase["end"]) - to_utc(phase["start"])).total_seconds() / 60


def _parse_windows(raw):
    """把输出的占用窗列表解析为 (lo, hi) 列表；结构不符返回 None。"""
    if not isinstance(raw, (list, tuple)):
        return None
    parsed = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        try:
            lo, hi = to_utc(pair[0]), to_utc(pair[1])
        except (TypeError, ValueError, OverflowError):
            return None
        if hi <= lo:
            return None
        parsed.append((lo, hi))
    return parsed


def verify_plan(raw, result):
    request = validate_request(raw)
    tasks = {t["id"]: t for t in request["tasks"]}
    workers = {w["id"]: w for w in request["workers"]}
    resources = {r["id"]: r for r in request["resources"]}
    zone = resolve_timezone(request["timezone"])
    errors, spans = [], []
    daily, amounts = defaultdict(float), defaultdict(float)
    capacity_events = defaultdict(list)
    epsilon = 1e-6

    # 模式/标签绑定：输出声明的 mode 必须与请求一致，防止把演示/影子结果改标为可执行建议。
    if not isinstance(result, dict):
        return {"valid": False, "errors": ["result 必须为对象"]}
    if "mode" not in result:
        errors.append("mode 缺失：输出未声明 mode，无法与请求绑定")
    elif result["mode"] != request["mode"]:
        errors.append(f"mode 不一致：输出为 {result['mode']!r}，请求为 {request['mode']!r}")

    def at(value):
        return to_utc(value)

    def covered(windows, start, end):
        points = sorted({start, end} | {at(w[k]) for w in windows for k in ("start", "end")
                                       if start < at(w[k]) < end})
        return all(any(at(w["start"]) <= left and at(w["end"]) >= right for w in windows)
                   for left, right in zip(points, points[1:]))

    def occupation_windows(resource, session, start, end, label):
        """按声明的占用口径给出本次出工需保留容量的窗，并核对输出声明。

        细分只能来自可采用的声明；输出 resource_occupation 必须与之逐窗一致，
        否则报错。缺失声明时不默认细分。复查只重算口径，不认证现场实际占用。
        """
        spec = resource_occupation(resource, request["mode"])
        rest_windows = []
        for phase in session.get("phases") or []:
            if isinstance(phase, dict) and phase.get("kind") == "rest":
                try:
                    rest_windows.append((at(phase["start"]), at(phase["end"])))
                except (KeyError, TypeError, ValueError, OverflowError):
                    return []
        expected = rest_windows if spec["subdivided"] else [(start, end)]
        occupation = session.get("resource_occupation")
        declared = occupation.get(resource["id"]) if isinstance(occupation, dict) else None
        # 细分口径必须由声明支撑，且输出占用窗要与声明的阶段逐窗一致。
        # 整段保留的资源以 [(start, end)] 为准（更保守），输出中的 occ 不改变复查口径。
        if spec["subdivided"]:
            if declared is None:
                errors.append(label + ": 细分资源占用未在会话中声明")
            else:
                parsed = _parse_windows(declared)
                if parsed is None:
                    errors.append(label + ": 资源占用声明结构无效")
                elif parsed != expected:
                    errors.append(label + ": 资源占用声明与声明的占用阶段不一致")
        return expected

    expected_digest = request_digest(raw, validated=request)
    if not isinstance(result, dict):
        return {"valid": False, "errors": ["result 必须为对象"],
                "meaning": "复查具体输出的数量与时间轴约束；不认证现场真实性、搜索最优性或健康效果。"}
    if result.get("request_sha256") != expected_digest:
        errors.append("request_sha256: 与规范化输入不一致")
    sessions = result.get("sessions", [])
    if not isinstance(sessions, list):
        errors.append("sessions 必须为数组")
        return {"valid": False, "errors": sorted(set(errors)),
                "meaning": "复查具体输出的数量与时间轴约束；不认证现场真实性、搜索最优性或健康效果。"}
    for index, session in enumerate(sessions):
        label = f"sessions[{index}]"
        try:
            tid, wid = session["task_id"], session["worker_id"]
            task, worker = tasks[tid], workers[wid]
            start, end = at(session["start"]), at(session["end"])
            quantity = session["quantity"]
            if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not isfinite(quantity) or quantity <= 0:
                raise ValueError("数量须为有限正数")
            if not (at(request["horizon_start"]) <= start < end <= at(request["horizon_end"])):
                errors.append(label + ": 超出规划范围")
            if start < at(task["earliest_start"]) or end > at(task["deadline"]):
                errors.append(label + ": 超出农时窗口")
            estimate = estimate_task(task, wid)
            if not estimate["schedulable"] or session["unit"] != estimate["unit"]:
                errors.append(label + ": 无可排程的同口径速率")
            if not covered(worker["availability"], start, end):
                errors.append(label + ": 劳动者不可用")
            if worker_profile_status(worker, start, end) not in {"valid", "legacy_unspecified"}:
                errors.append(label + ": WORKER_PROFILE_INVALID 人员资料或当前状态未覆盖整段出工")
            cursor, continuous = start, 0.0
            phase_minutes = defaultdict(float)
            # 出工内活动阶段必须按「准备→去程→作业→收尾→返回→预留」的顺序出现；
            # 休息可插任意处（rank 不变），多个作业段并列（rank 相同）允许。
            # 这是「准备至返回全时段」的结构不变量：颠倒顺序不得通过复查。
            active_rank = {"setup": 0, "outbound": 1, "work": 2,
                           "cleanup": 3, "return": 4, "buffer": 5}
            last_active_rank = -1
            for phase in session["phases"]:
                lo, hi = at(phase["start"]), at(phase["end"])
                minutes = (hi - lo).total_seconds() / 60
                if lo != cursor or hi <= lo or hi > end:
                    errors.append(label + ": 阶段有缺口、重叠或越界")
                cursor = hi
                kind = phase["kind"]
                phase_minutes[kind] += minutes
                if kind == "rest":
                    if minutes + epsilon < worker["limits"]["min_rest_minutes"]:
                        errors.append(label + ": 休息不足")
                    else:
                        continuous = 0
                elif kind == "wait":
                    # 等待不能清零连续活动，不能记作恢复或净劳动。
                    pass
                elif kind in ACTIVE_PHASE_KINDS:
                    # 连续计数含全部活动阶段，跨午夜不清零。
                    continuous += minutes
                    if continuous > worker["limits"]["max_continuous_active_minutes"] + epsilon:
                        errors.append(label + ": 超过持续活动限制")
                    rank = active_rank[kind]
                    if rank < last_active_rank:
                        errors.append(label + ": 活动阶段顺序颠倒（准备→去程→作业→收尾→返回→预留）")
                    last_active_rank = max(last_active_rank, rank)
                    for day, span_minutes in iter_local_date_spans(lo, hi, zone):
                        daily[wid, day] += span_minutes
                else:
                    errors.append(label + ": 未知阶段")
            # R15-A04：阶段顺序必须与引擎一致。返程(return)在作业(work)之后、
            # 收尾(cleanup)与缓冲(buffer)之前；准备(setup)在往返(outbound)之前。
            # rest 只能插在活动阶段之间；work 可被 rest 或 trip 切片拆成多段。
            phase_ranks = {"setup": 0, "outbound": 1, "work": 2, "cleanup": 3, "return": 4, "buffer": 5}
            seen_active = [p.get("kind") for p in session["phases"] if p.get("kind") not in {"rest", "wait"}]
            ranks = [phase_ranks.get(k, -1) for k in seen_active]
            if any(r < 0 for r in ranks):
                errors.append(label + ": 活动阶段顺序包含未知阶段")
            elif ranks != sorted(ranks):
                errors.append(label + ": 活动阶段顺序不正确（应为 setup→outbound→work→cleanup→return→buffer，"
                                         "rest 只能插在活动阶段之间）：实际 " + "→".join(seen_active))
            # R15-A09：声明与时间轴绑定：标记已预留初始休息时，该段首段必须是休息。
            # 防止保留 initial_rest_reserved 标记却把「恢复依赖」从时间轴上抹掉。
            if session.get("initial_rest_reserved"):
                head = session["phases"][0] if session.get("phases") else None
                if not head or head.get("kind") != "rest":
                    errors.append(label + ": 声明已预留初始休息，但首段不是休息")
            if cursor != end:
                errors.append(label + ": 阶段未覆盖出工结束")
            if estimate["schedulable"] and phase_minutes["work"] + epsilon < quantity * estimate["high_rate"]:
                errors.append(label + ": 净作业时间不足")
            for kind in ("setup", "outbound", "return", "cleanup", "buffer"):
                if phase_minutes[kind] + epsilon < task["session"][kind + "_minutes"]:
                    errors.append(label + ": 漏计 " + kind)
            clock = sum(phase_minutes.values())
            active = clock - phase_minutes["rest"] - phase_minutes["wait"]
            duty = task.get("wait_duty") or {}
            if duty and duty.get("status") != "known":
                errors.append(label + ": WAIT_STATUS_UNKNOWN 未知等待不能安排")
            expected_wait = duty.get("minutes", 0) if duty.get("status") == "known" else 0
            if abs(phase_minutes["wait"] - expected_wait) > epsilon:
                errors.append(label + ": WAIT_MINUTES_MISMATCH 等待阶段与声明不一致")
            if "wait_minutes" in session and abs(session["wait_minutes"] - phase_minutes["wait"]) > epsilon:
                errors.append(label + ": wait_minutes 汇总不一致")
            for field, value in (("active_minutes", active), ("rest_minutes", phase_minutes["rest"]), ("clock_minutes", clock)):
                if abs(session[field] - value) > epsilon:
                    errors.append(label + ": " + field + " 汇总不一致")
            if not assess_interval(request, task, worker, start, end)["allowed"]:
                errors.append(label + ": 环境或个人/农艺条件未通过")
            selected = session["resources"]
            if len(set(selected)) != len(selected) or not set(task["required_resources"]) <= set(selected):
                errors.append(label + ": 资源重复或遗漏")
            if not set(request["policy"]["required_resource_kinds"]) <= {resources[r]["kind"] for r in selected}:
                errors.append(label + ": 缺少必要资源类型")
            for rid in selected:
                windows = occupation_windows(resources[rid], session, start, end, label)
                for lo, hi in windows:
                    if hi > lo:
                        capacity_events[rid].append((lo, 1))
                        capacity_events[rid].append((hi, -1))
                if not all(covered(resources[rid]["availability"], lo, hi) for lo, hi in windows):
                    errors.append(label + ": 资源不可用")
            if estimate["unit"] in {"trip", "plant"} and abs(quantity - round(quantity)) > epsilon:
                errors.append(label + ": 离散单位被拆成小数")
            amounts[tid] += quantity
            spans.append((tid, wid, start, end, selected, session))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            errors.append(label + ": 无效结构或数值: " + str(exc))
    leftover = {tid: canonical_quantity(task["remaining_quantity"])[0] for tid, task in tasks.items()}
    timed = []
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        try:
            timed.append((at(session["start"]), index, session))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    timed.sort()
    for _, index, session in timed:
        tid = session.get("task_id")
        if tid not in tasks:
            continue
        task = tasks[tid]
        quantity = session.get("quantity")
        remaining_here = leftover.get(tid, 0.0)
        wid = session.get("worker_id")
        if wid in workers and isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
            try:
                estimate = estimate_task(task, wid)
            except (KeyError, TypeError, ValueError, OverflowError):
                estimate = None
            if estimate and estimate["schedulable"] and estimate["high_rate"]:
                quantum = quantity_quantum(task, estimate["unit"], estimate["high_rate"],
                                           request["step_minutes"])
                unit = session.get("unit") or estimate["unit"]
                if not chunk_is_allowed(quantity, remaining_here, task["min_chunk_minutes"],
                                        estimate["high_rate"], unit, quantum):
                    label = f"sessions[{index}]"
                    if unit in {"trip", "plant"} and abs(quantity - round(quantity)) > epsilon:
                        pass
                    elif quantity + epsilon < remaining_here and quantity * estimate["high_rate"] < task["min_chunk_minutes"] - epsilon:
                        errors.append(label + ": 非尾量分段短于 min_chunk_minutes")
                    else:
                        errors.append(label + ": 分段数量未对齐 quantity_step")
            leftover[tid] = remaining_here - quantity
    # 准备成本台账：每个任务的全部准备分钟必须等于「出工段数×每次出工重复项 + 每任务一次项」。
    # 不依赖引擎自报的 once_addon_applied；分段漏计重复项、漏计或重复计入一次项、无限切片都判失败。
    prep_totals, segment_counts = defaultdict(lambda: defaultdict(float)), defaultdict(int)
    for session in sessions:
        if not isinstance(session, dict):
            continue
        tid = session.get("task_id")
        phases = session.get("phases")
        if tid not in tasks or not isinstance(phases, list):
            continue
        segment_counts[tid] += 1
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            try:
                span = (at(phase["end"]) - at(phase["start"])).total_seconds() / 60
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if span > 0:
                prep_totals[tid][phase.get("kind")] += span
    for tid in tasks:
        count = segment_counts.get(tid, 0)
        if count > MAX_SESSION_COUNT:
            errors.append(f"{tid}: 同一任务出工段数 {count} 超过上限 {MAX_SESSION_COUNT}，无限切片不被接受")
            continue
        try:
            for item in prep_ledger_findings(tasks[tid], prep_totals.get(tid, {}), count):
                errors.append(f"{tid}: {item['code']} {item['message']}")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            errors.append(f"{tid}: 准备成本台账无法复核: {exc}")
    for (wid, day), minutes in daily.items():
        worker = workers[wid]
        if minutes + worker["used_active_minutes_by_date"].get(day, 0) > worker["limits"]["max_active_minutes_per_day"] + epsilon:
            errors.append(f"{wid}/{day}: 超过累计活动限制")
    first_by_worker = {}
    for tid, wid, start, end, selected, session in spans:
        previous = first_by_worker.get(wid)
        if previous is None or start < previous[0] or (start == previous[0] and tid < previous[1]):
            first_by_worker[wid] = (start, tid, session)
    for wid, (start, _, session) in first_by_worker.items():
        # 按时间轴最早出工检查；后插入的更早段会成为新的 first。
        if not needs_initial_rest(workers[wid], start, []):
            continue
        phase = session["phases"][0] if session.get("phases") else None
        if not phase or phase["kind"] != "rest":
            errors.append(wid + ": 初始休息未确认且未安排")
    for index, (tid, wid, start, end, selected, session) in enumerate(spans):
        for other_tid, other_wid, lo, hi, _, _ in spans[index + 1:]:
            if wid == other_wid:
                rest = timedelta(minutes=workers[wid]["limits"]["min_rest_minutes"])
                # 真正重叠与同边界接续分开；两者都是硬约束拒绝，只是原因不同。
                conflict = classify_worker_conflict(lo, hi, start, end, rest)
                if conflict == "overlap":
                    errors.append(wid + ": 同一人两段出工时间真正重叠")
                elif conflict == "rest_short":
                    errors.append(wid + ": 同边界接续或间隔不足最小休息")
            if tid == other_tid and start < hi and lo < end:
                if not same_task_parallel_allowed(tasks[tid], request["mode"]):
                    errors.append(tid + ": 同任务不支持并行分段")
        for dep in tasks[tid]["depends_on"]:
            needed = canonical_quantity(tasks[dep]["remaining_quantity"])[0]
            if amounts[dep] + epsilon < needed or any(v[3] > start for v in spans if v[0] == dep):
                errors.append(tid + ": 前置任务未全部完成返回")
    for rid, resource in resources.items():
        count = 0
        for _, change in sorted(capacity_events[rid]):
            count += change
            if count > resource["capacity"]:
                errors.append(rid + ": 共享容量超限")
                break
    # 幽灵行：task_results 中出现请求任务集之外的 task_id（凭空注入的“已完成”行）必须拒绝。
    raw_task_results = result.get("task_results", [])
    if not isinstance(raw_task_results, list):
        errors.append("task_results 必须为数组")
        raw_task_results = []
    task_results = []
    for index, entry in enumerate(raw_task_results):
        if not isinstance(entry, dict):
            errors.append(f"task_results[{index}]: 条目必须为对象")
            continue
        if entry.get("task_id") not in tasks:
            errors.append(
                f"task_results[{index}]: 幽灵行，task_id={entry.get('task_id')!r} 不在请求任务集中")
        task_results.append(entry)

    for tid, task in tasks.items():
        requested, requested_unit = canonical_quantity(task["remaining_quantity"])
        if amounts[tid] > requested + epsilon:
            errors.append(tid + ": 安排量超过剩余量")
        if not task["divisible"] and len([s for s in spans if s[0] == tid]) > 1:
            errors.append(tid + ": 不可分段任务被拆分")
        if not task["divisible"] and epsilon < amounts[tid] < requested - epsilon:
            errors.append(tid + ": 不可分段任务被拆成部分量")
        matching = [v for v in task_results if v.get("task_id") == tid]
        if len(matching) != 1 or abs(matching[0].get("scheduled_quantity", -1) - amounts[tid]) > epsilon:
            errors.append(tid + ": 任务结果与时间轴数量不一致")
            continue
        entry = matching[0]
        # 展示字段绑定：若结果携带请求量/单位/完成度，必须与请求剩余和时间轴一致，
        # 防止把面向读者的数字下调、改换单位或伪装完成（不放宽既有硬约束）。
        if "requested_quantity" in entry:
            declared_requested = entry.get("requested_quantity")
            if isinstance(declared_requested, bool) or not isinstance(declared_requested, (int, float)) \
                    or not isfinite(declared_requested):
                errors.append(tid + ": 请求量字段无效")
            elif abs(declared_requested - requested) > epsilon:
                errors.append(tid + f": 请求量与请求不一致（输出 {declared_requested:g} / 请求 {requested:g}）")
        if "unit" in entry:
            if entry.get("unit") != requested_unit:
                errors.append(tid + f": 单位与请求不一致（输出 {entry.get('unit')!r} / 请求 {requested_unit!r}）")
        if "completion_ratio" in entry:
            declared_ratio = entry.get("completion_ratio")
            expected_ratio = (amounts[tid] / requested) if requested > epsilon else 1.0
            if isinstance(declared_ratio, bool) or not isinstance(declared_ratio, (int, float)) \
                    or not isfinite(declared_ratio):
                errors.append(tid + ": 完成度字段无效")
            elif abs(declared_ratio - expected_ratio) > epsilon:
                errors.append(tid + f": 完成度与时间轴不一致（输出 {declared_ratio:g} / 应为 {expected_ratio:g}）")
        # 未安排量字段必须等于 请求剩余 - 时间轴已安排量，防止把余量改小或改成 0。
        declared_remaining = entry.get("remaining_quantity")
        if "remaining_quantity" in entry:
            if isinstance(declared_remaining, bool) or not isinstance(declared_remaining, (int, float)) \
                    or not isfinite(declared_remaining):
                errors.append(tid + ": 未安排量字段无效")
            elif abs(declared_remaining - (requested - amounts[tid])) > epsilon:
                errors.append(tid + ": 未安排量与时间轴不一致")
        # 完成语义：声明 complete（或未安排量=0）时，时间轴已安排量必须达到请求剩余。
        declared_complete = entry.get("status") == "complete"
        if not declared_complete and isinstance(declared_remaining, (int, float)) \
                and not isinstance(declared_remaining, bool) and abs(declared_remaining) <= epsilon:
            declared_complete = True
        if declared_complete and amounts[tid] + epsilon < requested:
            errors.append(tid + f": 声明完成但未排满（已安排 {amounts[tid]:g} / 请求 {requested:g}）")
    # R15-A04：once_addon 一致性。每任务一次项（once setup/cleanup）只能给最早出工段；
    # 多段时恰好一段 once_addon_applied=True，且必须是时间最早的那段。
    once_setup = {}
    once_cleanup = {}
    for tid, task in tasks.items():
        once_block = task.get("once") or {}
        once_setup[tid] = float(once_block.get("setup_minutes", 0))
        once_cleanup[tid] = float(once_block.get("cleanup_minutes", 0))
    task_sessions_by_start = defaultdict(list)
    for tid, wid, start, end, selected, session in spans:
        task_sessions_by_start[tid].append((start, session))
    for tid, rows in task_sessions_by_start.items():
        rows.sort(key=lambda pair: (pair[0], not bool(pair[1].get("once_addon_applied"))))
        flags = [bool(row[1].get("once_addon_applied")) for row in rows]
        if rows and once_setup[tid] <= 0 and once_cleanup[tid] <= 0:
            # 任务没有 once 附加项时，引擎仍标 True（语义为首段），此处不强制。
            pass
        if sum(flags) != 1:
            errors.append(tid + f": once_addon_applied 应恰好一段为 True（实际 {sum(flags)}/{len(flags)}）")
            continue
        if not flags[0]:
            # 同起点并列时，引擎按 worker_id 排序选第一段；只要求并列段中至少一段为 True。
            tied = [row for row in rows if row[0] == rows[0][0]]
            if not any(bool(row[1].get("once_addon_applied")) for row in tied):
                errors.append(tid + ": once_addon_applied 不在时间最早的出工段上")
            continue
        # 首段的 setup/cleanup 阶段分钟必须包含 once 附加。
        first = rows[0][1]
        setup_total = sum(_phase_minutes(p) for p in first["phases"] if p["kind"] == "setup")
        cleanup_total = sum(_phase_minutes(p) for p in first["phases"] if p["kind"] == "cleanup")
        rep_setup = float(tasks[tid]["session"]["setup_minutes"])
        rep_cleanup = float(tasks[tid]["session"]["cleanup_minutes"])
        if setup_total + epsilon < rep_setup + once_setup[tid]:
            errors.append(tid + ": 首段 setup 未包含 once 附加分钟")
        if cleanup_total + epsilon < rep_cleanup + once_cleanup[tid]:
            errors.append(tid + ": 首段 cleanup 未包含 once 附加分钟")
        # 非首段不得包含 once 附加。
        for later_start, later_session in rows[1:]:
            later_setup = sum(_phase_minutes(p) for p in later_session["phases"] if p["kind"] == "setup")
            later_cleanup = sum(_phase_minutes(p) for p in later_session["phases"] if p["kind"] == "cleanup")
            if later_setup + epsilon < rep_setup:
                errors.append(tid + ": 非首段 setup 低于重复附加下界")
            if later_cleanup + epsilon < rep_cleanup:
                errors.append(tid + ": 非首段 cleanup 低于重复附加下界")
    if result.get("status") == "complete":
        for task in request["tasks"]:
            tid = task["id"]
            if canonical_quantity(task["remaining_quantity"])[0] - amounts[tid] > epsilon:
                errors.append(tid + ": 总体状态声明完成但该任务未排满")
    return {"valid": not errors, "errors": sorted(set(errors)),
            "meaning": "复查具体输出的数量与时间轴约束；不认证现场真实性、搜索最优性或健康效果。"}
