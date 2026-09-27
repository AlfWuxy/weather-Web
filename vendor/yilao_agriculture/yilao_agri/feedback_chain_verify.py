"""R17-A10 反馈链独立复查器：观测 -> 筛选 -> 学习 -> 重排。

定位：本模块是一份**独立**的反馈链语义规格与不变量检查器，用于在合成夹具上
端到端核对 R17 反馈链（A01 事件存储、A02 剩余量与重复、A03 历史负荷、
A04/A05 重排、A06 同口径筛选、A07 描述性复盘、A08 学习数据集、A09 版本回退）。
它不代替任何实现，也不依赖同轮 9 个兄弟任务的产物。

独立性：不引入 engine、audit、environment、workload、models 等判定模块，只依据
事件清单与被检查的状态做核对。与 R15-A10 的 independent_verify 同一约定。

能得出的结论：在给定合成输入与这些规则下，派生状态是否违反已声明的反馈链
不变量。不能得出真实人群/真实作业/健康效果结论；n_real 恒为 0。

硬约束（本文件编码并据以判违反）：
  C1  事件按 event_id 幂等，并对完全相同的记录内容去重；重复上传不重复扣量。
  C2  派生结果与上传先后（recorded_at）无关，只由 occurred_at 决定。
  C3  无反馈保持 unknown：无实际发生事件的条目不得被记为已执行/已完成。
  C4  计划分钟永远不算劳动；劳动仅来自 executed/interrupted 的实际 minutes。
  C5  参与人数 = 纳入记录中发生实际作业的**去重个人**数；不数记录条数。
  C6  n_real=0 时不得给出概率/效果声明，也不自动收紧保守范围。
  C7  重排生成新版本并保留旧版；被替换且已展示的旧版必须有暴露标记，不得静默改写。
  C8  同口径筛选：纳入仅在 (人,任务,方法,时间范围) 内的记录；越界与中断记录保留可追溯。
"""

from __future__ import annotations

from datetime import datetime

# ---------------------------------------------------------------------------
# 事件状态与常量
# ---------------------------------------------------------------------------

STATUS_PLANNED = "planned"
STATUS_SHOWN = "shown"
STATUS_UNDERSTOOD = "understood"
STATUS_EXECUTED = "executed"
STATUS_INTERRUPTED = "interrupted"
STATUS_SKIPPED = "skipped"
STATUS_PLAN_PUBLISHED = "plan_published"
STATUS_REORDER_TRIGGER = "reorder_trigger"

# 观测类事件（进入筛选与学习）；控制类事件（版本链）另行处理。
OBSERVATION_STATUSES = frozenset({
    STATUS_PLANNED, STATUS_SHOWN, STATUS_UNDERSTOOD,
    STATUS_EXECUTED, STATUS_INTERRUPTED, STATUS_SKIPPED,
})
# 只有这两类会更新实际完成量与劳动分钟。
ACTUAL_STATUSES = frozenset({STATUS_EXECUTED, STATUS_INTERRUPTED})
CONTROL_STATUSES = frozenset({STATUS_PLAN_PUBLISHED, STATUS_REORDER_TRIGGER})

DEFAULT_SLOW_THRESHOLD_MINUTES = 240.0

# 注入的已知错误（变异体）。每个对应一条应当被独立检出的不变量。
MUTATIONS = frozenset({
    "DEDUP_OFF",             # 关掉 event_id 幂等 -> 重复计数
    "CONTENT_DEDUP_OFF",     # 关掉内容去重 -> 同一记录换 id 再传也重复计
    "PLAN_AS_LABOR",         # 计划分钟计入劳动
    "UNKNOWN_AS_DONE",       # 无反馈记为已执行
    "COUNT_RECORDS_AS_PEOPLE",  # 用记录条数当参与人数
    "SILENT_OVERWRITE",      # 静默丢弃旧版本、清除暴露标记
    "SCOPE_LEAK",            # 越界记录进入纳入集
    "DROP_INTERRUPTED",      # 中断记录被丢弃而非保留
    "CLAIM_INTERVAL",        # n_real=0 仍给出概率/效果声明
})


def _parse(value):
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _fingerprint(event):
    """内容指纹：与事件 id 无关，用于识别同一物理记录被重复上传。"""
    return (
        event.get("person_id"), event.get("task_id"), event.get("method"),
        str(event.get("occurred_at")), event.get("status"),
        _num(event.get("completed_quantity")), _num(event.get("actual_minutes")),
    )


def _num(value):
    if value is None:
        return None
    return round(float(value), 9)


def _in_scope(event, scope):
    """scope 为 None 或字段为 None 时视为通配；时间范围半开 [start, end)。"""
    if not scope:
        return True
    for field in ("person_id", "task_id", "method"):
        want = scope.get(field)
        if want is not None and event.get(field) != want:
            return False
    occ = _parse(event.get("occurred_at"))
    start = _parse(scope.get("time_start"))
    end = _parse(scope.get("time_end"))
    if start is not None and (occ is None or occ < start):
        return False
    if end is not None and (occ is None or occ >= end):
        return False
    return True


# ---------------------------------------------------------------------------
# 参考派生（规格实现）。mutations 用于注入已知错误以验证检出能力。
# ---------------------------------------------------------------------------

def fold(events, *, query_scope=None, mutations=frozenset(),
         slow_threshold_minutes=DEFAULT_SLOW_THRESHOLD_MINUTES):
    """把事件清单折叠为反馈链状态。mutations 非空时故意偏离规格。"""
    mut = frozenset(mutations)
    unknown_mut = mut - MUTATIONS
    if unknown_mut:
        raise ValueError("未知注入项：%s" % sorted(unknown_mut))

    observation = [e for e in events if e.get("status") in OBSERVATION_STATUSES]
    control = [e for e in events if e.get("status") in CONTROL_STATUSES]

    # C1 幂等 + 内容去重（先按输入顺序，首次出现者胜出）。
    seen_ids, ignored_ids = set(), []
    seen_fp, ignored_fp = set(), []
    unique = []
    for event in observation:
        event_id = event.get("event_id")
        if "DEDUP_OFF" not in mut and event_id in seen_ids:
            ignored_ids.append(event_id)
            continue
        seen_ids.add(event_id)
        fp = _fingerprint(event)
        if ("DEDUP_OFF" not in mut and "CONTENT_DEDUP_OFF" not in mut
                and fp in seen_fp):
            ignored_fp.append(event_id)
            continue
        seen_fp.add(fp)
        unique.append(event)

    # C2 只按 occurred_at 决定顺序，与上传时间无关。
    unique.sort(key=lambda e: (str(e.get("occurred_at")), str(e.get("event_id"))))

    items = {}
    labor_minutes = 0.0
    planned_minutes_total = 0.0
    completed_by_task, planned_minutes_by_task = {}, {}
    actual_person_ids = set()

    for event in unique:
        key = (event.get("person_id"), event.get("task_id"), event.get("method"))
        item = items.setdefault(key, {"status": None, "actual": False})
        status = event.get("status")
        if status in (STATUS_PLANNED, STATUS_SHOWN, STATUS_UNDERSTOOD):
            item["status"] = status
            minutes = float(event.get("planned_minutes") or 0.0)
            planned_minutes_total += minutes
            planned_minutes_by_task[event.get("task_id")] = (
                planned_minutes_by_task.get(event.get("task_id"), 0.0) + minutes)
        elif status in ACTUAL_STATUSES:
            item["status"] = status
            item["actual"] = True
            quantity = float(event.get("completed_quantity") or 0.0)
            completed_by_task[event.get("task_id")] = (
                completed_by_task.get(event.get("task_id"), 0.0) + quantity)
            labor_minutes += float(event.get("actual_minutes") or 0.0)
            actual_person_ids.add(event.get("person_id"))
        else:  # skipped
            item["status"] = status

    if "PLAN_AS_LABOR" in mut:
        labor_minutes += planned_minutes_total
    if "UNKNOWN_AS_DONE" in mut:
        for key, item in items.items():
            if not item["actual"]:
                item["status"] = STATUS_EXECUTED
                item["actual"] = True
                actual_person_ids.add(key[0])

    # 计划量来自 plan_published 控制事件的 items。
    task_planned_quantity = {}
    versions_input = []
    for event in sorted(control, key=lambda e: (str(e.get("occurred_at")), str(e.get("event_id")))):
        if event.get("status") == STATUS_PLAN_PUBLISHED:
            versions_input.append(event)
            for entry in event.get("items") or []:
                task_planned_quantity[entry["task_id"]] = float(entry.get("planned_quantity") or 0.0)

    remaining_by_task = {}
    for task_id, planned in task_planned_quantity.items():
        completed = completed_by_task.get(task_id, 0.0)
        remaining_by_task[task_id] = planned - completed

    # C3 无反馈保持 unknown。
    unknown_items = sorted(
        "%s|%s|%s" % (k[0], k[1], k[2]) for k, v in items.items() if not v["actual"])
    executed_items = sorted(
        "%s|%s|%s" % (k[0], k[1], k[2]) for k, v in items.items() if v["actual"])

    # C8 同口径筛选：越界/中断记录保留可追溯。
    observed_records, excluded_records, slow_flags, interrupted_retained = [], [], [], []
    for event in unique:
        record_id = event.get("event_id")
        if not _in_scope(event, query_scope):
            excluded_records.append(record_id)
            continue
        observed_records.append(record_id)
        minutes = event.get("actual_minutes")
        if minutes is not None and float(minutes) > slow_threshold_minutes:
            slow_flags.append(record_id)
        if event.get("status") == STATUS_INTERRUPTED:
            interrupted_retained.append(record_id)
    if "SCOPE_LEAK" in mut:
        observed_records = observed_records + excluded_records
        observed_records = sorted(set(observed_records), key=lambda r: str(r))
    if "DROP_INTERRUPTED" in mut:
        drop = set(interrupted_retained)
        observed_records = [r for r in observed_records if r not in drop]
        interrupted_retained = []

    admitted_ids = set(observed_records)
    admitted_actual_persons = {
        event.get("person_id") for event in unique
        if event.get("status") in ACTUAL_STATUSES and event.get("event_id") in admitted_ids
    }
    if "COUNT_RECORDS_AS_PEOPLE" in mut:
        participants = sorted({str(e.get("person_id")) for e in unique}, key=str)
        participant_count = len(unique)
    else:
        participants = sorted(admitted_actual_persons, key=str)
        participant_count = len(admitted_actual_persons)

    # C7 版本链：保留全部已发布版本，被替换且已展示者须有暴露标记。
    versions, exposed = [], []
    superseded_by = {}
    for i, event in enumerate(versions_input):
        versions.append({
            "version_no": event.get("version_no"),
            "shown": bool(event.get("shown")),
            "supersedes": event.get("supersedes"),
        })
    for version in versions:
        prev = version.get("supersedes")
        if prev is not None:
            superseded_by[prev] = version.get("version_no")
    for version in versions:
        if version["version_no"] in superseded_by and version["shown"]:
            exposed.append(version["version_no"])
    if "SILENT_OVERWRITE" in mut:
        versions = versions[-1:] if versions else []
        exposed = []

    state = {
        "events_seen": len(events),
        "processed_event_ids": [e.get("event_id") for e in unique],
        "duplicates_ignored": ignored_ids,
        "content_duplicates_ignored": ignored_fp,
        "labor_minutes": round(labor_minutes, 9),
        "planned_minutes_total": round(planned_minutes_total, 9),
        "completed_by_task": {k: round(v, 9) for k, v in completed_by_task.items()},
        "remaining_by_task": {k: round(v, 9) for k, v in remaining_by_task.items()},
        "unknown_items": unknown_items,
        "executed_items": executed_items,
        "participants": [str(p) for p in participants],
        "participant_count": participant_count,
        "observed_records": sorted(observed_records, key=str),
        "excluded_records": sorted(excluded_records, key=str),
        "interrupted_retained": interrupted_retained,
        "slow_flags": slow_flags,
        "versions": versions,
        "exposed_versions": exposed,
        "probability_claim": None,
        "effect_claim": None,
        "n_real": 0,
    }
    if "CLAIM_INTERVAL" in mut:
        state["probability_claim"] = {"interval": [10, 20], "level": 0.9}
        state["effect_claim"] = "降低热相关不适（合成声称，违反 C6）"
    return state


# ---------------------------------------------------------------------------
# 独立不变量检查器
# ---------------------------------------------------------------------------

def _expected_processed_ids(events):
    seen_ids, seen_fp, expected = set(), set(), []
    for event in [e for e in events if e.get("status") in OBSERVATION_STATUSES]:
        event_id = event.get("event_id")
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        fp = _fingerprint(event)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        expected.append(event_id)
    expected.sort(key=lambda e: (str(_occ(events, e)), str(e)))
    return expected


def _occ(events, event_id):
    for event in events:
        if event.get("event_id") == event_id:
            return event.get("occurred_at")
    return None


def verify_chain(events, state, *, query_scope=None,
                 slow_threshold_minutes=DEFAULT_SLOW_THRESHOLD_MINUTES):
    """独立核对派生状态是否违反 C1..C8。返回 {valid, violations, counts}。"""
    violations = []

    def fail(code, detail):
        violations.append({"code": code, "detail": detail})

    observation = [e for e in events if e.get("status") in OBSERVATION_STATUSES]

    # C1/C2 去重与顺序无关。
    expected_ids = _expected_processed_ids(events)
    if state.get("processed_event_ids") != expected_ids:
        fail("V_DEDUP", "处理后事件集与幂等去重结果不一致（重复计数）：%r != %r"
             % (state.get("processed_event_ids"), expected_ids))

    unique = _dedup_for_check(observation)

    # C4 劳动只来自实际事件。
    expected_labor = round(sum(float(e.get("actual_minutes") or 0.0)
                               for e in unique if e.get("status") in ACTUAL_STATUSES), 9)
    if round(float(state.get("labor_minutes") or 0.0), 9) != expected_labor:
        fail("V_LABOR", "劳动分钟含非实际来源：%s != %s"
             % (state.get("labor_minutes"), expected_labor))

    # C4 计划分钟须单独保留，不得并入劳动。
    expected_planned = round(sum(float(e.get("planned_minutes") or 0.0)
                                 for e in unique if e.get("status") in
                                 (STATUS_PLANNED, STATUS_SHOWN, STATUS_UNDERSTOOD)), 9)
    if round(float(state.get("planned_minutes_total") or 0.0), 9) != expected_planned:
        fail("V_LABOR", "计划分钟未如实单列：%s != %s"
             % (state.get("planned_minutes_total"), expected_planned))

    # C3 无反馈保持 unknown：不得把无实际事件的条目记为已执行。
    expected_actual_keys = {
        (e.get("person_id"), e.get("task_id"), e.get("method"))
        for e in unique if e.get("status") in ACTUAL_STATUSES
    }
    expected_actual_str = {"%s|%s|%s" % k for k in expected_actual_keys}
    executed_items = set(state.get("executed_items") or [])
    if not executed_items <= expected_actual_str:
        fail("V_UNKNOWN", "无反馈条目被记为已执行：%r"
             % sorted(executed_items - expected_actual_str))
    expected_unknown = sorted({
        "%s|%s|%s" % (e.get("person_id"), e.get("task_id"), e.get("method"))
        for e in unique
    } - expected_actual_str)
    if sorted(state.get("unknown_items") or []) != expected_unknown:
        fail("V_UNKNOWN", "unknown 集合不符：%r != %r"
             % (state.get("unknown_items"), expected_unknown))

    # C5 参与人数 = 纳入记录中实际作业的去重个人数。
    if state.get("observed_records") is not None:
        admitted = set(state.get("observed_records"))
    else:
        admitted = {e.get("event_id") for e in unique}
    expected_persons = {
        e.get("person_id") for e in unique
        if e.get("status") in ACTUAL_STATUSES and e.get("event_id") in admitted
    }
    if int(state.get("participant_count") or 0) != len(expected_persons):
        fail("V_PARTICIPANTS", "参与人数被虚增/错算：%s != %s（去重个人）"
             % (state.get("participant_count"), len(expected_persons)))
    if sorted(str(p) for p in (state.get("participants") or [])) != sorted(map(str, expected_persons)):
        fail("V_PARTICIPANTS", "参与人清单不符：%r" % (state.get("participants"),))

    # C6 n_real=0 时不得给出概率/效果声明。
    if int(state.get("n_real") or 0) == 0:
        if state.get("probability_claim") is not None:
            fail("V_CLAIM", "n_real=0 却给出概率区间声明：%r" % (state.get("probability_claim"),))
        if state.get("effect_claim") is not None:
            fail("V_CLAIM", "n_real=0 却给出效果声明：%r" % (state.get("effect_claim"),))

    # C7 版本链：全部已发布版本保留；被替换且已展示者须暴露标记。
    published = [e for e in events if e.get("status") == STATUS_PLAN_PUBLISHED]
    published_nos = {e.get("version_no") for e in published}
    present_nos = {v.get("version_no") for v in (state.get("versions") or [])}
    if not published_nos <= present_nos:
        fail("V_VERSION", "已发布版本被静默丢弃：缺失 %r" % sorted(published_nos - present_nos))
    superseded = {}
    for event in published:
        if event.get("supersedes") is not None:
            superseded[event.get("supersedes")] = event.get("version_no")
    expected_exposed = {
        event.get("version_no") for event in published
        if event.get("version_no") in superseded and bool(event.get("shown"))
    }
    if set(state.get("exposed_versions") or []) != expected_exposed:
        fail("V_VERSION", "已展示旧版的暴露标记不符：%r != %r"
             % (state.get("exposed_versions"), sorted(expected_exposed)))

    # C8 同口径筛选：纳入仅 in-scope；越界与中断记录保留。
    expected_observed, expected_excluded = [], []
    for event in unique:
        if _in_scope(event, query_scope):
            expected_observed.append(event.get("event_id"))
        else:
            expected_excluded.append(event.get("event_id"))
    if sorted(state.get("observed_records") or [], key=str) != sorted(expected_observed, key=str):
        fail("V_SCOPE", "纳入集与同口径筛选不符：%r" % (state.get("observed_records"),))
    if sorted(state.get("excluded_records") or [], key=str) != sorted(expected_excluded, key=str):
        fail("V_SCOPE", "越界记录未被保留/归档：%r" % (state.get("excluded_records"),))
    expected_interrupted = [e.get("event_id") for e in unique
                            if e.get("status") == STATUS_INTERRUPTED
                            and _in_scope(e, query_scope)]
    if sorted(state.get("interrupted_retained") or [], key=str) != sorted(expected_interrupted, key=str):
        fail("V_RETAIN", "中断记录未被保留：%r" % (state.get("interrupted_retained"),))

    counts = {
        "violations": len(violations),
        "observed": len(state.get("observed_records") or []),
        "excluded": len(state.get("excluded_records") or []),
        "participants": int(state.get("participant_count") or 0),
        "labor_minutes": state.get("labor_minutes"),
    }
    return {"valid": not violations, "violations": violations, "counts": counts}


def _dedup_for_check(observation):
    seen_ids, seen_fp, unique = set(), set(), []
    for event in observation:
        event_id = event.get("event_id")
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        fp = _fingerprint(event)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        unique.append(event)
    return unique


def run_chain(events, *, query_scope=None, mutations=frozenset(),
              slow_threshold_minutes=DEFAULT_SLOW_THRESHOLD_MINUTES):
    """端到端：派生 + 独立核对，返回合并报告。"""
    state = fold(events, query_scope=query_scope, mutations=mutations,
                 slow_threshold_minutes=slow_threshold_minutes)
    report = verify_chain(events, state, query_scope=query_scope,
                          slow_threshold_minutes=slow_threshold_minutes)
    report["state"] = state
    return report
