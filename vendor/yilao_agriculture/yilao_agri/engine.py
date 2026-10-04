"""保守耗时、硬约束优先的确定性有界排程。"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
from itertools import product
import math
import time

from . import __version__
from .environment import assess_interval
from .models import (
    CANONICALIZATION_VERSION, INTEGER_UNITS, classify_deadline_basis, iter_local_date_spans,
    request_digest, resolve_timezone, resource_occupation, same_task_parallel_allowed, to_utc,
    utc_minutes, validate_request, worker_profile_status,
)
from .rejection_explain import SCOPE_TASK_STRUCTURAL, RejectionLedger
from .workload import (
    canonical_quantity, chunk_is_allowed, discrete_quantity_ok, estimate_split_addons,
    estimate_task, estimate_transport_trips, min_partial_quanta, quantity_quantum,
    session_overhead_pieces, session_trip_plan,
)

EPS = 1e-8
# 除 rest 外全部计入连续活动；未知 kind 也按活动处理，避免漏计。
ACTIVE_PHASE_KINDS = frozenset({"setup", "outbound", "work", "cleanup", "return", "buffer"})


def needs_initial_rest(worker, start, existing_starts):
    """未确认已休息时，该人时间轴上最早出工必须先休息。

    用已有段的开始时刻判断，而不是结束时刻：先排入的晚段不能让更早插入的段跳过首段休息。
    """
    if worker.get("initial_rest_confirmed", False):
        return False
    return not any(peer < start for peer in existing_starts)


def _time(value):
    return to_utc(value)


def _minutes(start, end):
    return utc_minutes(start, end)


def _covers(intervals, start, end):
    """首尾相接的可用区间允许合并，缺口不能跨过。"""
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


def classify_worker_conflict(previous_start, previous_end, start, end, min_rest):
    """把同一人的两段出工分开：真正重叠 / 同边界接续或间隔不足 / 无冲突。

    区间为半开 ``[start, end)``：一段恰在另一段结束时开始（``previous_end == start``）不算重叠，
    但两段之间仍必须满足 ``min_rest``，因此它归入 ``rest_short`` 而不是 ``overlap``。
    参数已是 UTC ``datetime``；返回 ``"overlap"``、``"rest_short"`` 或 ``None``。
    """
    if start < previous_end and previous_start < end:
        return "overlap"
    if end + min_rest <= previous_start or previous_end + min_rest <= start:
        return None
    return "rest_short"


class PlanInterrupted(Exception):
    """计算被取消或超时。

    只携带计数器进度（``progress``），绝不携带、返回或落盘任何半成品出工方案。
    调用方可用同一输入重算即等价续算（见 :func:`resume_plan`）。
    """

    def __init__(self, reason, progress):
        super().__init__("计算已中断：%s" % reason)
        self.reason = reason
        self.progress = progress


class CancelToken:
    """协作式取消令牌：外部布尔回调与可选墙钟上限，任一满足即取消。

    传入 ``clock`` 便于测试注入；默认 ``time.monotonic``。令牌不产生任何计划，
    只在 :class:`_Planner` 的搜索循环里被轮询；未调用 :meth:`bind` 时墙钟上限不生效。
    """

    def __init__(self, is_cancelled=None, max_wall_seconds=None, clock=None):
        if is_cancelled is not None and not callable(is_cancelled):
            raise TypeError("cancel 必须是可调用对象或 None")
        if max_wall_seconds is not None:
            if isinstance(max_wall_seconds, bool) or not isinstance(max_wall_seconds, (int, float)) \
                    or not math.isfinite(max_wall_seconds) or max_wall_seconds <= 0:
                raise ValueError("max_wall_seconds 必须为正数或 None")
        self._is_cancelled = is_cancelled
        self._max_wall_seconds = max_wall_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._started = None

    def bind(self):
        """记录起始时刻；仅墙钟上限依赖它。"""
        self._started = self._clock()

    def reason(self):
        """返回 ``None``、``"cancelled"`` 或 ``"timeout"``；外部回调优先。"""
        if self._is_cancelled is not None and self._is_cancelled():
            return "cancelled"
        if self._max_wall_seconds is not None and self._started is not None:
            if self._clock() - self._started >= self._max_wall_seconds:
                return "timeout"
        return None


class _Planner:
    def __init__(self, request, cancel=None):
        self.r = request
        self.cancel = cancel
        self.zone = resolve_timezone(request["timezone"])
        self.start = _time(request["horizon_start"])
        self.end = _time(request["horizon_end"])
        self.step = request["step_minutes"]
        self.tasks = {t["id"]: t for t in request["tasks"]}
        self.workers = {w["id"]: w for w in request["workers"]}
        self.resources = {r["id"]: r for r in request["resources"]}
        # 资源占用口径：默认整段保留（保守）；仅声明依据且可采用的休息处可按 rest 阶段细分。
        self.occupation = {rid: resource_occupation(resource, request["mode"])
                           for rid, resource in self.resources.items()}
        self.availability = {
            (kind, item["id"]): [(_time(v["start"]), _time(v["end"]))
                                  for v in item["availability"]]
            for kind, items in (("worker", request["workers"]),
                                ("resource", request["resources"]))
            for item in items
        }
        self.estimates = {(tid, wid): estimate_task(t, wid)
                          for tid, t in self.tasks.items() for wid in self.workers}
        self.quantities = {tid: canonical_quantity(t["remaining_quantity"])[0] for tid, t in self.tasks.items()}
        self.units = {tid: canonical_quantity(t["remaining_quantity"])[1] for tid, t in self.tasks.items()}
        self.rejections = defaultdict(Counter)
        self.ledger = RejectionLedger()
        self.messages = {}
        self.env_cache = {}
        self.session_cache = {}
        self.attempts = 0
        self.check_limit = min(200000, max(2000, request["max_search_states"] * 40))
        self.check_limit_reached = False
        self.states_explored = 0
        self.budget_exhausted = False
        self.depth_limit_reached = False
        # R16-A03：网格之外的连续边界起点计数（仅用于透明报告，不改变硬约束）。
        self.boundary_start_candidates = 0
        # R16-A06 公平预算：候选检查预算按 (任务, 人) 对等分，避免先枚举到的大任务或
        # 排序靠前的人独占全部检查预算。各份份额之和 <= check_limit，全局检查上限仍是
        # 硬上限；公平分配只改变“谁先被搜”，不放松任何约束判定，也不把截断记作约束失败。
        self.pairs_all = [(tid, wid) for tid in sorted(self.tasks) for wid in sorted(self.workers)]
        self.pairs_explorable = [pair for pair in self.pairs_all
                                 if self.quantities.get(pair[0], 0.0) > EPS
                                 and self.estimates[pair]["schedulable"]]
        n_share = len(self.pairs_explorable) or 1
        self.pair_fair_cap = max(1, -(-self.check_limit // n_share))
        self.pair_attempts = defaultdict(int)
        self.pair_capped = set()
        self.pair_seen = set()
        self.pair_complete = set()
        weather = [request["weather"][p] for p in
                   sorted({t["plot_id"] for t in self.tasks.values()})
                   if p in request["weather"]]
        records = [v for series in weather for v in series["records"]]
        methods = {s.get("wbgt_method") for s in weather}
        self.metric = "wbgt" if (records and len(methods) == 1 and None not in methods
                                   and "" not in methods
                                   and all(v.get("wbgt_c") is not None for v in records)) else "temperature"

    def reject(self, tid, code, message, worker_id=None, window_start=None):
        self.rejections[tid][code] += 1
        self.messages[tid, code] = message
        self.ledger.record(tid, code, message, worker_id=worker_id,
                           window_start=self.stamp(window_start) if window_start is not None else None)

    def stamp(self, instant):
        return instant.astimezone(self.zone).isoformat()

    def _dates(self, start, end):
        """按指定地点的民用日期切分，UTC 时间轴避免夏令时算术错误。"""
        yield from iter_local_date_spans(start, end, self.zone)

    def _phases(self, task, worker, start, quantity, high_rate, initial_rest=False,
                include_once=False):
        """除休息外全部保守计为活动，延误余量也不被当成恢复。

        连续活动分钟跨准备/往返/作业/收尾/延误累计，不在当地午夜清零。
        """
        key = (task["id"], worker["id"], start, round(quantity, 10), initial_rest, include_once)
        if key in self.session_cache:
            return self.session_cache[key]
        maximum = worker["limits"]["max_continuous_active_minutes"]
        rest = worker["limits"]["min_rest_minutes"]
        cursor, continuous = start, 0.0
        phases, active_by_date = [], defaultdict(float)
        overhead = dict(session_overhead_pieces(task, include_once))
        work_total = max(1, math.ceil(quantity * high_rate - EPS)) if quantity > EPS else 0
        unit = canonical_quantity(task["remaining_quantity"])[1]
        pieces = [("setup", overhead["setup"], None),
                  ("outbound", overhead["outbound"], None)]
        if unit == "trip" and quantity > EPS:
            trips = int(round(quantity))
            load = task.get("load_per_trip_kg")
            trip_plan = session_trip_plan(
                trips, work_total, load_per_trip_kg=load if load else None)
            if trip_plan["slices"]:
                for row in trip_plan["slices"]:
                    extra = {field: row[field] for field in
                             ("trip_index", "trip_count", "trip_role", "trip_load_kg") if field in row}
                    pieces.append(("work", row["minutes"], extra))
            else:
                pieces.append(("work", work_total, {"trip_count": trips}))
        else:
            pieces.append(("work", work_total, None))
        duty = task.get("wait_duty") or {}
        if duty.get("status") == "known" and duty["minutes"] > 0:
            pieces.append(("wait", duty["minutes"], None))
        pieces.extend([("cleanup", overhead["cleanup"], None),
                       ("return", overhead["return"], None),
                       ("buffer", overhead["buffer"], None)])

        def append(kind, duration, extra=None):
            nonlocal cursor
            end = cursor + timedelta(minutes=duration)
            phase = {"kind": kind, "start": self.stamp(cursor), "end": self.stamp(end)}
            if extra:
                phase.update(extra)
            phases.append(phase)
            if kind not in {"rest", "wait"}:
                # 非休息阶段都算活动，包括准备与延误；跨日只切每日合计。
                for day, minutes in self._dates(cursor, end):
                    active_by_date[day] += minutes
            cursor = end

        if initial_rest:
            append("rest", rest)
        for kind, duration, extra in pieces:
            if kind == "wait":
                # 等待占用人和资源、不抵扣劳动、不重置连续活动计数。
                append(kind, duration, extra)
                continue
            remaining = float(duration)
            while remaining > EPS:
                if continuous >= maximum - EPS:
                    append("rest", rest)
                    continuous = 0.0
                block = min(remaining, maximum - continuous)
                append(kind, block, extra)
                continuous += block
                remaining -= block
        value = (phases, cursor, dict(active_by_date))
        self.session_cache[key] = value
        return value

    def _resource_windows(self, rid, start, end, rest_windows):
        """该资源在一次出工内实际需要保留容量的时间窗。

        subdivided 时只取已排定的 rest 阶段窗（无 rest 即不占用）；否则整段出工。
        """
        if self.occupation[rid]["subdivided"]:
            return rest_windows
        return [(start, end)]

    def _covers_windows(self, intervals, windows):
        return all(_covers(intervals, lo, hi) for lo, hi in windows)

    def _capacity_ok(self, rid, windows, sessions):
        """按各次出工的实际占用窗做容量并发峰值检查（半开区间，同刻先减后加）。"""
        capacity = self.resources[rid]["capacity"]
        events = []
        for lo, hi in windows:
            if hi > lo:
                events.append((lo, 1))
                events.append((hi, -1))
        for previous in sessions:
            if rid not in previous["resources"]:
                continue
            occupation = previous.get("resource_occupation")
            if isinstance(occupation, dict) and rid in occupation:
                previous_windows = [(_time(lo), _time(hi)) for lo, hi in occupation[rid]]
            else:
                # 旧记录或未声明占用窗时按整段回退，属保守。
                previous_windows = [(_time(previous["start"]), _time(previous["end"]))]
            for lo, hi in previous_windows:
                if hi > lo:
                    events.append((lo, 1))
                    events.append((hi, -1))
        count = 0
        for _, delta in sorted(events):
            count += delta
            if count > capacity:
                return False
        return True

    def _resource_choices(self, task, start, end, rest_windows, sessions):
        fixed = set(task["required_resources"])
        kinds = set(self.r["policy"]["required_resource_kinds"])
        for rid in fixed:
            windows = self._resource_windows(rid, start, end, rest_windows)
            if not self._covers_windows(self.availability["resource", rid], windows):
                self.reject(task["id"], "RESOURCE_UNAVAILABLE", f"资源 {rid} 未覆盖整段出工时间。",
                            window_start=start)
                return []
            if not self._capacity_ok(rid, windows, sessions):
                self.reject(task["id"], "RESOURCE_CAPACITY", f"资源 {rid} 的同时占用容量不足。",
                            window_start=start)
                return []
        missing = sorted(kinds - {self.resources[r]["kind"] for r in fixed})
        choices = []
        for kind in missing:
            candidates = [rid for rid, resource in sorted(self.resources.items())
                          if resource["kind"] == kind
                          and self._covers_windows(self.availability["resource", rid],
                                                   self._resource_windows(rid, start, end, rest_windows))
                          and self._capacity_ok(rid, self._resource_windows(rid, start, end, rest_windows),
                                                sessions)]
            if not candidates:
                self.reject(task["id"], "MISSING_RESOURCE_KIND", f"没有覆盖本时段的 {kind} 资源。",
                            window_start=start)
                return []
            choices.append(candidates)
        result = []
        for index, extra in enumerate(product(*choices)):
            if index >= 16:
                break
            selected = sorted(fixed | set(extra))
            if all(self._capacity_ok(rid, self._resource_windows(rid, start, end, rest_windows), sessions)
                   for rid in selected):
                result.append(selected)
        if not result:
            self.reject(task["id"], "RESOURCE_UNAVAILABLE", f"资源 {rid} 未覆盖整段出工时间。",
                            window_start=start)
        return result

    def _build(self, task, worker, start, quantity, state):
        tid, wid = task["id"], worker["id"]
        pair = (tid, wid)
        if self.attempts >= self.check_limit:
            self.check_limit_reached = True
            return []
        if self.pair_attempts[pair] >= self.pair_fair_cap:
            # 公平份额用尽：只停止对这一 (任务, 人) 继续搜索，不记作约束失败、不写拒绝原因。
            self.pair_capped.add(pair)
            return []
        self.pair_attempts[pair] += 1
        self.attempts += 1
        rate = self.estimates[tid, wid]
        profile_status = worker_profile_status(worker, start)
        if profile_status not in {"valid", "legacy_unspecified"}:
            self.reject(tid, "WORKER_PROFILE_" + profile_status.upper(),
                        f"{wid} 人员资料为 {profile_status}，不能安排本人作业。", worker_id=wid)
            return []
        if task.get("wait_duty", {}).get("status") == "unknown":
            self.reject(tid, "WAIT_STATUS_UNKNOWN", "等待时长与来源未确认，不能省略等待后生成安排。", worker_id=wid)
            return []
        if not discrete_quantity_ok(quantity, rate["unit"]):
            self.reject(tid, "INTEGER_UNIT_SPLIT", "趟数或株数不能拆成小数。")
            return []
        if rate["unit"] in INTEGER_UNITS:
            quantity = float(round(quantity))
        # 搜索允许把新段插到已选晚段之前；尾量必须按最终时间位置判断，不能按加入顺序判断。
        # 同时起点的排序与结果输出一致，避免先选下午尾量后在早晨生成不对齐量子的“伪尾量”。
        remaining_at_start = self.quantities[tid] - sum(
            s["quantity"] for s in state["sessions"] if s["task_id"] == tid
            and (_time(s["start"]), s["worker_id"]) < (start, wid))
        quantum = quantity_quantum(task, rate["unit"], rate["high_rate"], self.step)
        if not chunk_is_allowed(quantity, remaining_at_start, task["min_chunk_minutes"],
                                rate["high_rate"], rate["unit"], quantum):
            self.reject(tid, "QUANTITY_STEP_AT_TIME", "该时间位置不是尾量，分段须满足最小劳动时长并对齐数量步长。",
                        worker_id=wid, window_start=start)
            return []
        limits = worker["limits"]
        accepted = {"confirmed", "illustrative"} if self.r["mode"] == "demonstration" else {"confirmed"}
        if worker["state"] != "clear":
            self.reject(tid, "WORKER_STATE_NOT_CLEAR", f"{wid} 当前状态为 {worker['state']}，不生成本人作业段。",
                        worker_id=wid)
            return []
        if (limits["review_status"] not in accepted or limits["max_continuous_active_minutes"] <= 0
                or limits["min_rest_minutes"] <= 0 or limits["max_active_minutes_per_day"] <= 0):
            self.reject(tid, "WORKER_LIMITS_UNCONFIRMED", f"{wid} 缺少本模式可采用的活动与休息限制。",
                        worker_id=wid)
            return []
        # 分段时重复附加按本次出工再计；每任务一次项只给该任务尚未出工的第一段。
        prior_task_sessions = [s for s in state["sessions"] if s["task_id"] == tid]
        # 本次搜索只向该任务首段之后扩展，避免把已计准备的晚段变成非首段。
        # 不事后移动时间或只改标记；每个候选仍重新检查全部硬约束。
        has_once_minutes = any(float(v) > 0 for v in (task.get("once") or {}).values())
        if has_once_minutes and prior_task_sessions and start < min(_time(s["start"]) for s in prior_task_sessions):
            return []
        include_once = not prior_task_sessions
        overhead = sum(minutes for _, minutes in session_overhead_pieces(task, include_once))
        # 先检查不含休息的时长下界，防止巨大输入创建数百万个动作段。
        lower_bound = max(1, math.ceil(quantity * rate["high_rate"] - EPS)) + overhead
        if lower_bound > _minutes(start, min(self.end, _time(task["deadline"]))) + EPS:
            self.reject(tid, "DEADLINE_OR_HORIZON", "即使尚未计入休息，保守耗时也超出截止或规划范围。",
                        worker_id=wid, window_start=start)
            return []
        peer_starts = [_time(s["start"]) for s in state["sessions"] if s["worker_id"] == wid]
        initial_rest = needs_initial_rest(worker, start, peer_starts)
        phases, end, active_by_date = self._phases(
            task, worker, start, quantity, rate["high_rate"], initial_rest, include_once)
        if worker_profile_status(worker, start, end) not in {"valid", "legacy_unspecified"}:
            self.reject(tid, "WORKER_PROFILE_EXPIRED", "人员资料或当前状态有效期未覆盖整段出工。", worker_id=wid)
            return []
        # 已排定的休息阶段窗：细分口径下，休息处等资源只在这些窗内保留容量。
        rest_windows = [(_time(phase["start"]), _time(phase["end"]))
                        for phase in phases if phase["kind"] == "rest"]
        if end > min(self.end, _time(task["deadline"])):
            self.reject(tid, "DEADLINE_OR_HORIZON", "即使尚未计入休息，保守耗时也超出截止或规划范围。",
                        worker_id=wid, window_start=start)
            return []
        if not _covers(self.availability["worker", wid], start, end):
            self.reject(tid, "WORKER_UNAVAILABLE", f"{wid} 的可用时间未覆盖整段出工。",
                        worker_id=wid, window_start=start)
            return []
        rest = timedelta(minutes=worker["limits"]["min_rest_minutes"])
        for previous in state["sessions"]:
            lo, hi = _time(previous["start"]), _time(previous["end"])
            if previous["worker_id"] == wid:
                # 同一人的争用：真正重叠与同边界接续分开报告；两者都不得排入。
                conflict = classify_worker_conflict(lo, hi, start, end, rest)
                if conflict == "overlap":
                    self.reject(tid, "WORKER_OVERLAP",
                                f"{wid} 两段出工时间真正重叠：同一人不能同时出现在两处作业。",
                                worker_id=wid, window_start=start)
                    return []
                if conflict == "rest_short":
                    self.reject(tid, "WORKER_REST_INSUFFICIENT",
                                f"{wid} 两次出工间休息不足：需 {worker['limits']['min_rest_minutes']} 分钟。",
                                worker_id=wid, window_start=start)
                    return []
            if previous["task_id"] == tid and lo < end and hi > start:
                if not same_task_parallel_allowed(task, self.r["mode"]):
                    self.reject(tid, "TASK_PARALLELISM",
                                "同一任务分段默认串行；缺少可采用的并行依据时，不假设多人同时作业的线性效率。")
                    return []
        for day, minutes in active_by_date.items():
            # 已发生负荷按当地日键计入；schema 1.0 缺日仍为 0。禁止把建议 sessions 写回该字段。
            prior = worker["used_active_minutes_by_date"].get(day, 0)
            prior += sum(s["active_minutes_by_date"].get(day, 0) for s in state["sessions"] if s["worker_id"] == wid)
            if prior + minutes > worker["limits"]["max_active_minutes_per_day"] + EPS:
                self.reject(tid, "DAILY_ACTIVE_LIMIT", f"{wid} 在 {day} 的累计活动时间超出输入限制。",
                            worker_id=wid, window_start=start)
                return []
        key = (tid, wid, start, end)
        if key not in self.env_cache:
            self.env_cache[key] = assess_interval(self.r, task, worker, start, end)
        environment = self.env_cache[key]
        if not environment["allowed"]:
            for reason in environment["reasons"]:
                self.reject(tid, reason["code"], reason["message"],
                            worker_id=wid, window_start=start)
            return []
        choices = self._resource_choices(task, start, end, rest_windows, state["sessions"])
        active = sum(active_by_date.values())
        base = {"task_id": tid, "worker_id": wid, "plot_id": task["plot_id"],
                "start": self.stamp(start), "end": self.stamp(end),
                "quantity": quantity, "unit": rate["unit"],
                "active_minutes": active,
                "rest_minutes": sum(_minutes(_time(p["start"]), _time(p["end"])) for p in phases if p["kind"] == "rest"),
                "wait_minutes": sum(_minutes(_time(p["start"]), _time(p["end"])) for p in phases if p["kind"] == "wait"),
                "clock_minutes": _minutes(start, end), "active_minutes_by_date": active_by_date,
                "phases": phases, "environment": environment,
                "initial_rest_reserved": initial_rest,
                "rate_source": rate["source"], "rate_interval_minutes_per_unit": [rate["low_rate"], rate["high_rate"]],
                "once_addon_applied": include_once}
        if rate["unit"] == "trip":
            trips = int(round(quantity))
            load = task.get("load_per_trip_kg")
            if load:
                info = estimate_transport_trips(0.0 if trips == 0 else float(load) * trips, load)
            else:
                info = session_trip_plan(trips, 0)
            base["transport"] = {
                "trips": trips,
                "full_trips": info["full_trips"],
                "tail_trips": info["tail_trips"],
                "last_trip_kg": info.get("last_trip_kg"),
                "load_per_trip_kg": None if not load else float(load),
                "session_overhead_once": True,
                "loading_rule": "出工附加只计一次；净用时按趟，不把装卸再乘准备/收尾",
            }
        return [dict(base, resources=selected,
                     resource_occupation=self._occupation_map(selected, rest_windows, start, end))
                for selected in choices[:2]]

    def _occupation_map(self, selected, rest_windows, start, end):
        """逐资源给出本次出工需要保留容量的时间窗，供复查与后续出工复用。"""
        payload = {}
        for rid in selected:
            windows = rest_windows if self.occupation[rid]["subdivided"] else [(start, end)]
            payload[rid] = [[self.stamp(lo), self.stamp(hi)] for lo, hi in windows]
        return payload

    def _candidate_order(self, candidate):
        metrics = candidate["environment"]["metrics"]
        peak = metrics.get(f"max_{self.metric}_c")
        integral = metrics.get(f"{self.metric}_positive_degree_minutes")
        return (-candidate["quantity"], max(0, peak) if peak is not None else math.inf,
                integral if integral is not None else math.inf,
                candidate["active_minutes"], candidate["end"], tuple(candidate["resources"]))

    def candidates(self, state):
        selected = []
        ordered = sorted(self.tasks.values(), key=lambda t: (-t["priority"], _time(t["deadline"]), t["id"]))
        for task in ordered:
            if self.check_limit_reached:
                break
            tid = task["id"]
            remaining = self.quantities[tid] - state["done"].get(tid, 0)
            if remaining <= EPS:
                continue
            if not self.workers:
                # 空人手保留余量与缺口原因，不把缺人写成已证无解，也不混入依赖未完成。
                self.reject(tid, "NO_WORKER", "尚未提供可用于排程的劳动者。")
                continue
            if self.units[tid] in INTEGER_UNITS:
                if not discrete_quantity_ok(remaining, self.units[tid]):
                    self.reject(tid, "INTEGER_UNIT_SPLIT", "趟数或株数不能拆成小数。")
                    continue
                remaining = float(round(remaining))
                if remaining <= EPS:
                    continue
            if any(state["done"].get(dep, 0) < self.quantities[dep] - EPS for dep in task["depends_on"]):
                self.reject(tid, "DEPENDENCY_INCOMPLETE", "前置任务尚未全部完成。")
                continue
            earliest = max(self.start, _time(task["earliest_start"]))
            for s in state["sessions"]:
                if s["task_id"] in task["depends_on"]:
                    earliest = max(earliest, _time(s["end"]))
            first_step = max(0, math.ceil(_minutes(self.start, earliest) / self.step - EPS))
            for wid, worker in sorted(self.workers.items()):
                if self.check_limit_reached:
                    break
                rate = self.estimates[tid, wid]
                if not rate["schedulable"]:
                    for reason in rate["reasons"]:
                        self.reject(tid, reason["code"], reason["message"],
                            worker_id=wid)
                    continue
                self.pair_seen.add((tid, wid))
                pair = []
                quantum = quantity_quantum(task, rate["unit"], rate["high_rate"], self.step)
                min_q = min_partial_quanta(task["min_chunk_minutes"], rate["high_rate"], quantum)
                truncated = False
                tick_cap = math.ceil(_minutes(self.start, self.end) / self.step)
                for start in self._start_candidates(task, worker, earliest, first_step, tick_cap, state):
                    if self.check_limit_reached or (tid, wid) in self.pair_capped:
                        truncated = True
                        break
                    if start >= _time(task["deadline"]):
                        break
                    # 全量（含短于最小段的尾量）优先；否则在不低于最小段的量子上二分。
                    options = self._build(task, worker, start, remaining, state)
                    best_quantity = remaining if options else 0.0
                    if not options and task["divisible"]:
                        low, high = min_q, math.floor((remaining - EPS) / quantum)
                        while low <= high:
                            if self.check_limit_reached:
                                break
                            middle = (low + high) // 2
                            quantity = middle * quantum
                            trial = self._build(task, worker, start, quantity, state)
                            if trial:
                                best_quantity, options = quantity, trial
                                low = middle + 1
                            else:
                                high = middle - 1
                    if not options:
                        continue
                    if not chunk_is_allowed(best_quantity, remaining, task["min_chunk_minutes"],
                                            rate["high_rate"], rate["unit"], quantum):
                        continue
                    pair.extend(options)
                    if task["divisible"]:
                        smaller = math.floor(best_quantity / 2 / quantum) * quantum
                        if chunk_is_allowed(smaller, remaining, task["min_chunk_minutes"],
                                            rate["high_rate"], rate["unit"], quantum):
                            pair.extend(self._build(task, worker, start, smaller, state))
                if not truncated:
                    self.pair_complete.add((tid, wid))
                # 保留大段、较凉时段及较早结束等不同取舍，不只截取最早起点。
                if pair:
                    rankings = [sorted(pair, key=self._candidate_order),
                                sorted(pair, key=lambda c: (c["end"],) + self._candidate_order(c)),
                                sorted(pair, key=lambda c: self._candidate_order(c)[1:3] + self._candidate_order(c))]
                    seen = set()
                    for ranking in rankings:
                        for c in ranking[:2]:
                            key = (c["start"], c["end"], round(c["quantity"], 8), tuple(c["resources"]))
                            if key not in seen:
                                selected.append(c)
                                seen.add(key)
        return selected

    def _start_candidates(self, task, worker, earliest, first_step, tick_cap, state):
        """把网格起点与连续可行区间的边界起点合并成升序去重序列（R16-A03）。

        网格起点（horizon_start + k*step）保持原有采样密度；边界起点补上连续可行
        区间的下界，避免粗步长把整段可行窗口夹在两个网格点之间而漏解：
        任务最早可开工时刻、本人可用窗起点、所需资源可用窗起点，以及本段前一次
        出工结束后必须满足最小休息的时刻。

        边界起点与网格点走同一条 `_build` 路径做连续时间检查（最小休息、可用性
        覆盖、同人冲突、资源容量、日累计负荷、环境），更精细不构成绕过检查的
        捷径，也不放松任何硬约束。
        """
        upper = min(self.end, _time(task["deadline"]))
        grid = {self.start + timedelta(minutes=tick * self.step)
                for tick in range(first_step, tick_cap)}
        boundaries = set()
        # 边界起点必须 ≥ earliest（已含前置完成与任务最早可开工时刻），否则会绕过
        # 依赖/最早开工约束；也不能晚于 min(horizon_end, deadline)。
        if earliest < upper:
            boundaries.add(earliest)
        for window_start, _window_end in self.availability["worker", worker["id"]]:
            if earliest <= window_start < upper:
                boundaries.add(window_start)
        for rid in task["required_resources"]:
            for window_start, _window_end in self.availability.get(("resource", rid), []):
                if earliest <= window_start < upper:
                    boundaries.add(window_start)
        rest = timedelta(minutes=worker["limits"]["min_rest_minutes"])
        for session in state["sessions"]:
            if session["worker_id"] == worker["id"]:
                boundary = _time(session["end"]) + rest
                if earliest <= boundary < upper:
                    boundaries.add(boundary)
        boundaries -= grid
        self.boundary_start_candidates += len(boundaries)
        return sorted(grid | boundaries)

    def score(self, state):
        progress = []
        for priority in range(5, 0, -1):
            members = [t for t in self.tasks.values() if t["priority"] == priority]
            ratios = [min(1, state["done"].get(t["id"], 0) / self.quantities[t["id"]])
                      if self.quantities[t["id"]] > EPS else 1 for t in members]
            progress.append(-round(sum(ratios) / len(ratios), 9) if ratios else 0)
        values = [s["environment"]["metrics"].get(f"max_{self.metric}_c") for s in state["sessions"]]
        integral = [s["environment"]["metrics"].get(f"{self.metric}_positive_degree_minutes") for s in state["sessions"]]
        peak = max(0, max(values)) if values and all(v is not None for v in values) else (math.inf if values else 0)
        dose = sum(integral) if all(v is not None for v in integral) else math.inf
        finish = max((_time(s["end"]).timestamp() for s in state["sessions"]), default=self.start.timestamp())
        return tuple(progress) + (peak, dose, sum(s["active_minutes"] for s in state["sessions"]),
                                  finish, len(state["sessions"]), self.signature(state))

    @staticmethod
    def signature(state):
        return tuple(sorted((s["task_id"], s["worker_id"], s["start"], s["end"],
                             round(s["quantity"], 8), tuple(s["resources"])) for s in state["sessions"]))

    def progress(self):
        """中断时暴露的进度快照：只有计数器与请求摘要，绝不含任何出工方案。"""
        return {"scope": "search_progress_only",
                "no_plan_emitted": True,
                "states_explored": self.states_explored,
                "candidate_checks": self.attempts,
                "search_budget_exhausted": self.budget_exhausted,
                "depth_limit_reached": self.depth_limit_reached,
                "request_sha256": request_digest(validated=self.r),
                "search": {"max_search_states": self.r["max_search_states"],
                           "beam_width": self.r["beam_width"],
                           "step_minutes": self.step}}

    def _check_cancel(self):
        if self.cancel is None:
            return
        reason = self.cancel.reason()
        if reason is not None:
            raise PlanInterrupted(reason, self.progress())

    def run(self):
        empty = {"sessions": [], "done": {}}
        best, beam = empty, [empty]
        maximum = self.r["max_search_states"]
        depth_limit = 32
        for depth in range(depth_limit):
            self._check_cancel()
            expanded = {}
            for state in beam:
                self._check_cancel()
                if self.states_explored >= maximum:
                    self.budget_exhausted = True
                    break
                if self.check_limit_reached:
                    break
                for session in self.candidates(state):
                    self._check_cancel()
                    if self.states_explored >= maximum:
                        self.budget_exhausted = True
                        break
                    self.states_explored += 1
                    done = dict(state["done"])
                    done[session["task_id"]] = done.get(session["task_id"], 0) + session["quantity"]
                    successor = {"sessions": state["sessions"] + [session], "done": done}
                    signature = self.signature(successor)
                    expanded[signature] = successor
                    if self.score(successor) < self.score(best):
                        best = successor
            if not expanded or self.budget_exhausted or self.check_limit_reached:
                break
            beam = sorted(expanded.values(), key=self.score)[:self.r["beam_width"]]
            if all(best["done"].get(t, 0) >= q - EPS for t, q in self.quantities.items()):
                break
        else:
            self.depth_limit_reached = True
        return self.result(best)

    def _fair_allocation(self):
        """候选检查预算在 (任务, 人) 之间的分配与实际探索范围（只读汇报，不改结果）。

        ``unexplored_pairs`` 只列预算真的被截断的组合：
        - fair_share_cap_reached：达到对等份额上限；
        - not_reached_within_budget / global_budget_truncated：全局检查或状态预算耗尽。
        若两个预算都未耗尽，则不把“未探索”归给预算（它属约束/结构原因，已有拒绝码覆盖）。
        """
        per_worker = defaultdict(int)
        for (_tid, wid), count in self.pair_attempts.items():
            per_worker[wid] += count
        budget_hit = self.check_limit_reached or self.budget_exhausted
        unexplored = []
        for pair in self.pairs_explorable:
            tid, wid = pair
            if pair in self.pair_capped:
                reason = "fair_share_cap_reached"
            elif pair not in self.pair_seen:
                if not budget_hit:
                    continue
                reason = "not_reached_within_budget"
            elif pair not in self.pair_complete:
                reason = "global_budget_truncated"
            else:
                continue
            unexplored.append({"task_id": tid, "worker_id": wid, "reason": reason,
                               "candidate_checks": self.pair_attempts.get(pair, 0),
                               "fair_share_cap": self.pair_fair_cap})
        return {
            "policy": "equal_share_per_task_worker_pair",
            "candidate_check_budget": self.check_limit,
            "explorable_pair_count": len(self.pairs_explorable),
            "pair_count_all": len(self.pairs_all),
            "fair_share_cap_per_pair": self.pair_fair_cap,
            "pairs_capped_at_fair_share": len(self.pair_capped),
            "per_worker_candidate_checks": {wid: per_worker.get(wid, 0) for wid in sorted(self.workers)},
            "state_budget_exhausted": self.budget_exhausted,
            "candidate_check_limit_reached": self.check_limit_reached,
            "unexplored_pairs": unexplored,
            "exploration_complete": not unexplored,
        }

    def result(self, best):
        # 没有一次性分钟时允许较早插入，只将首段展示标记对齐最终时间顺序。
        best = deepcopy(best)
        for tid, task in self.tasks.items():
            if not any(float(v) > 0 for v in (task.get("once") or {}).values()):
                rows = sorted((s for s in best["sessions"] if s["task_id"] == tid), key=lambda s: (s["start"], s["worker_id"]))
                for index, session in enumerate(rows):
                    session["once_addon_applied"] = index == 0
        results = []
        personal_deadline_seen = False
        fair = self._fair_allocation()
        truncated_by_task = defaultdict(set)
        for row in fair["unexplored_pairs"]:
            truncated_by_task[row["task_id"]].add(row["worker_id"])
        for tid, task in self.tasks.items():
            requested = self.quantities[tid]
            scheduled = min(requested, best["done"].get(tid, 0))
            remaining = max(0, requested - scheduled)
            task_sessions = [s for s in best["sessions"] if s["task_id"] == tid]
            # R15-A07：失败原因逐条关联到具体人/时段/字段；只有与具体人、时段无关的
            # 结构性原因才给出 global_cause，局部候选失败不升级为全局原因。
            if remaining > EPS:
                attribution = self.ledger.attribution(tid)
                scope_map = self.ledger.scope_by_code(tid)
                field_map: dict = {}
                for row in (attribution["task_structural"] + attribution["worker_level"]
                            + attribution["candidate_local"]):
                    field_map.setdefault(row["code"], row["field"])
                reasons = [{"code": code, "message": self.messages[tid, code],
                            "candidate_rejection_count": count,
                            "scope": scope_map.get(code, "candidate_local"),
                            "field": field_map.get(code),
                            "is_global_cause": scope_map.get(code) == SCOPE_TASK_STRUCTURAL}
                           for code, count in sorted(self.rejections[tid].items())]
            else:
                attribution = None
                reasons = []
            help_estimates = []
            for wid in self.workers:
                estimate = self.estimates[tid, wid]
                if remaining > EPS and estimate["schedulable"]:
                    addon = estimate_split_addons(task, 1)
                    once_remaining = 0.0 if task_sessions else addon["once_minutes"]
                    help_estimates.append({"worker_id": wid,
                                           "net_minutes_interval": [remaining * estimate["low_rate"], remaining * estimate["high_rate"]],
                                           "repeating_addon_minutes_per_outing": addon["repeating_minutes_per_session"],
                                           "once_addon_minutes_if_no_outing_yet": once_remaining,
                                           "source": estimate["source"],
                                           "meaning": "仅为余量的净用时估计；还需独立核实此人限制、时段、资源及每次出工附加时间。"})
            finish = max((_time(s["end"]) for s in task_sessions), default=None)
            basis = classify_deadline_basis(task)
            personal_deadline_seen = personal_deadline_seen or basis["personal_preference"]
            alternatives = (["核实可分工部分及帮手自己的限制与时间", "核实替代工具和对应作业速率",
                             "经农艺确认后调整期限或本次范围"] if remaining > EPS else [])
            if remaining > EPS and basis["delay_allowed"]:
                conditions = basis["delay_conditions"]
                alternatives.append("可改期条件（仅为输入声明，需核实）：" + "；".join(conditions)
                                    if conditions else "输入声明允许改期，但未列条件，应补齐后再决定")
            results.append({"task_id": tid, "status": "complete" if remaining <= EPS else "not_fully_scheduled",
                            "requested_quantity": requested, "scheduled_quantity": scheduled,
                            "remaining_quantity": remaining, "unit": self.units[tid],
                            "completion_ratio": scheduled / requested if requested > EPS else 1.0,
                            "finish": self.stamp(finish) if finish else None,
                            "deadline": task["deadline"], "deadline_source": task["deadline_source"],
                            "deadline_reason": basis["label"], "deadline_basis": basis,
                            "deadline_slack_minutes": _minutes(finish, _time(task["deadline"])) if finish and remaining <= EPS else None,
                            "gap_is_proven_unavoidable": False,
                            "same_task_overlap": ("allowed_with_evidence"
                                                   if same_task_parallel_allowed(task, self.r["mode"])
                                                   else "serial_default"),
                            "reasons": reasons, "failure_attribution": attribution, "remaining_work_estimates": help_estimates,
                            "search_exploration": {
                                "workers_explored": sorted(wid for wid in self.workers
                                                           if (tid, wid) in self.pair_seen),
                                "workers_not_fully_explored": sorted(truncated_by_task.get(tid, ())),
                                "exploration_truncated": bool(truncated_by_task.get(tid)),
                            },
                            "alternatives": alternatives})
        complete = all(r["remaining_quantity"] <= EPS for r in results)
        sessions = sorted(best["sessions"], key=lambda s: (s["start"], s["worker_id"], s["task_id"]))
        digest = request_digest(validated=self.r)
        total_active = sum(s["active_minutes"] for s in sessions)
        warnings = ["排程成立仅表示满足所输入的约束，不构成个人健康许可。",
                    "未安排余量是本次有界搜索的结果，不是已经证明任何方案都无法完成。",
                    "天气、本人状态、人手或任务进度变化后，应以新版本输入重新排程。",
                    "休息和必要资源的可用性为输入声明，软件没有验证现场实际条件。"]
        if self.r["mode"] == "demonstration":
            warnings.insert(0, "本结果为合成参数演示，不得直接作为真实作业建议。")
        if self.metric == "temperature":
            warnings.append("WBGT 缺失或来源方法不一致，排序只比较温度维度，未评估完整人体热负担。")
        if personal_deadline_seen:
            warnings.append("存在以个人习惯为理由的截止：程序只把它当个人时间偏好，不视作农艺硬期限，也不据此越过身体或天气限制。")
        if any(spec["subdivided"] for spec in self.occupation.values()):
            warnings.append("部分资源占用按声明阶段细分：仅在其声明的阶段窗内保留容量，假定计划外不使用该资源；未声明时一律整段保留（保守）。细分只改变资源占用口径，不降低人员活动、休息、天气与容量硬限。")
        if not fair["exploration_complete"]:
            warnings.append(
                "本次有界搜索把候选检查预算按(任务,人)对等分（每对上限 "
                f"{self.pair_fair_cap}）；有 {len(fair['unexplored_pairs'])} 个(任务,人)组合未充分探索"
                "（见 search.fair_allocation.unexplored_pairs）。这些组合未排入的部分只是本次搜索未穷尽，不表示在任何安排下都做不成。")
        return {"schema_version": "1.0", "request_schema_version": self.r["schema_version"], "algorithm_version": __version__,
                "canonicalization_version": CANONICALIZATION_VERSION,
                "plan_id": "plan-" + digest[:16], "request_sha256": digest,
                "mode": self.r["mode"], "as_of": self.r["now"], "timezone": self.r["timezone"],
                "status": "complete" if complete else ("partial" if sessions else "no_plan_found"),
                "sessions": sessions, "task_results": results,
                "workload_estimates": [{"task_id": tid, "worker_id": wid, **value}
                                       for (tid, wid), value in self.estimates.items()],
                "summary": {"active_person_minutes": total_active,
                            "rest_person_minutes": sum(s["rest_minutes"] for s in sessions),
                            "occupied_person_minutes": sum(s["clock_minutes"] for s in sessions),
                            "elapsed_plan_minutes": _minutes(min(_time(s["start"]) for s in sessions), max(_time(s["end"]) for s in sessions)) if sessions else 0,
                            "completed_task_count": sum(r["status"] == "complete" for r in results),
                            "task_count": len(results),
                            "resource_occupation_subdivided": any(spec["subdivided"]
                                                                  for spec in self.occupation.values())},
                "ranking": {"method": "lexicographic", "priority_order": [5, 4, 3, 2, 1],
                            "objectives": ["各优先级任务平均完成比例", "非负环境峰值", "逐段非负环境值乘分钟", "活动人分钟", "完成时刻", "出工次数"],
                            "environment_metric": self.metric,
                            "interpretation": "只作暖热维度比较；零截断是避免负值奖励长时间暴露的算术下界，不是人体安全阈值。寒冷限制须另行输入。排序不是医学风险分数。"},
                "search": {"algorithm": "bounded_beam_with_conservative_session_enumeration",
                           "timepoint_enumeration": "grid_plus_continuous_feasible_boundaries",
                           "boundary_timepoint_candidates": self.boundary_start_candidates,
                           "states_explored": self.states_explored, "candidate_checks": self.attempts,
                           "candidate_check_limit": self.check_limit, "candidate_check_limit_reached": self.check_limit_reached,
                           "max_search_states": self.r["max_search_states"], "beam_width": self.r["beam_width"],
                           "step_minutes": self.step, "budget_exhausted": self.budget_exhausted,
                           "depth_limit": 32, "depth_limit_reached": self.depth_limit_reached,
                           "resource_combination_limit": 16, "optimality_proven": False,
                           "fair_allocation": fair},
                "warnings": warnings,
                "provenance": {"policy_source": self.r["policy"]["source"],
                               "policy_review_status": self.r["policy"]["review_status"],
                               "weather_sources": {pid: {k: v.get(k) for k in ("source", "issued_at", "kind", "wbgt_method")}
                                                   for pid, v in self.r["weather"].items()},
                               "worker_limit_sources": {wid: w["limits"]["source"] for wid, w in self.workers.items()},
                               "resource_occupation_modes": {rid: {"mode": spec["mode"], "stages": spec["stages"],
                                                                  "subdivided": spec["subdivided"], "source": spec["source"],
                                                                  "review_status": spec["review_status"]}
                                                              for rid, spec in self.occupation.items()},
                               "agronomy_sources": {tid: t["agronomy"]["source"] for tid, t in self.tasks.items()}}}


def plan(raw, *, cancel=None, max_wall_seconds=None, clock=None):
    """计算完整结果，不修改输入，也不触发通知或持久化。

    ``cancel`` 为可调用对象，返回真值时协作式中断；``max_wall_seconds`` 为墙钟上限。
    任一触发都抛出 :class:`PlanInterrupted`，只携带计数器进度，
    绝不返回或落盘半成品计划；调用方以同一输入重算即等价续算。
    """
    request = validate_request(raw)
    token = None
    if cancel is not None or max_wall_seconds is not None:
        token = CancelToken(is_cancelled=cancel, max_wall_seconds=max_wall_seconds, clock=clock)
        token.bind()
    return deepcopy(_Planner(request, cancel=token).run())


def resume_plan(raw, prior_plan=None, *, prior_plan_id=None, cancel=None,
                max_wall_seconds=None, clock=None):
    """中断后的确定性续算：以同一输入重算，并核对与先前结果一致。

    排程是纯函数——不写盘、不改输入、无全局状态；因此中断后无需保留任何半成品，
    直接重算即等价于续算。若给定先前 ``plan_id``（或 ``prior_plan`` 的 ``plan_id``）
    而重算的 ``plan_id`` 不同，说明输入或算法版本已变，抛 ``RuntimeError``。
    返回 ``(plan, report)``；``report`` 记录一致性而不弱化任何硬约束。
    """
    result = plan(raw, cancel=cancel, max_wall_seconds=max_wall_seconds, clock=clock)
    expect = prior_plan_id
    if expect is None and isinstance(prior_plan, dict):
        expect = prior_plan.get("plan_id")
    got = result["plan_id"]
    consistent = expect is None or expect == got
    report = {"resumed": True, "deterministic_recompute": True,
              "prior_plan_id": expect, "plan_id": got,
              "request_sha256": result["request_sha256"],
              "consistent_with_prior": consistent,
              "search": result["search"]}
    if not consistent:
        raise RuntimeError("续算结果与先前计划不一致：输入或算法版本可能已变（prior=%s, got=%s）"
                           % (expect, got))
    return result, report
