"""把带时点的本人自述累计量与后续作业记录合并；未知绝不当零。"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("需要带时区的时间")
    return value.astimezone(timezone.utc)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1440


def is_untimed_not_done(event):
    """仅识别明确未开始且三个零值齐全的记录，不把未知或往返等待当作零。"""
    if (not isinstance(event, dict) or event.get("status") != "not_done"
            or any(event.get(key) is not None for key in ("started_at", "ended_at", "actual_start", "actual_end"))):
        return False
    quantity = event.get("completed_quantity")
    if not isinstance(quantity, dict):
        return False
    return all(type(value) in (int, float) and value == 0
               for value in (quantity.get("value"), event.get("net_minutes"), event.get("rest_minutes")))


def assess_day_load(worker, events, day, now):
    """返回当天自述台账合计；不写入输入、不生成健康许可。

    ``load_baseline_at_by_date[day]`` 应由服务端在该日累计分钟被修改时生成。
    截止基数时点以前的作业只核对、不重加；以后的非休息时间才追加。
    events 可为完整更正历史或已由 current_events 筛出的当前记录。
    """
    result = {"known": False, "used_active_minutes": None, "reasons": [],
              "day": day, "baseline_minutes": None, "baseline_at": None,
              "before_baseline_recorded_minutes": 0.0, "after_baseline_added_minutes": 0.0,
              "included_event_ids": [], "deduplicated_event_ids": [],
              "basis": "self_report_and_recorded_non_rest_time",
              "meaning": "仅合并输入的活动台账；未记录事实与健康安全不由软件认证。"}

    def fail(code, message):
        if code not in {r["code"] for r in result["reasons"]}:
            result["reasons"].append({"code": code, "message": message})

    if not isinstance(worker, dict) or not isinstance(events, list):
        fail("LOAD_INPUT_INVALID", "人员或作业记录格式不正确")
        return result
    try:
        civil_day = date.fromisoformat(day)
        if civil_day.isoformat() != day:
            raise ValueError("日期无效")
        zone = ZoneInfo(worker.get("timezone", "Asia/Shanghai"))
        moment = _instant(now)
        lo = datetime.combine(civil_day, time.min, zone).astimezone(timezone.utc)
        hi = datetime.combine(civil_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError):
        fail("LOAD_DAY_INVALID", "台账日期、时区或当前时刻无效")
        return result
    values = worker.get("used_active_minutes_by_date")
    stamps = worker.get("load_baseline_at_by_date")
    baseline = values.get(day) if isinstance(values, dict) else None
    asof = stamps.get(day) if isinstance(stamps, dict) else None
    if not _finite(baseline):
        fail("LOAD_BASELINE_UNKNOWN", "请明确填写当天截至某时刻已活动多少分钟；未填不等于0")
        return result
    try:
        baseline_at = _instant(asof)
        if not lo <= baseline_at < hi or baseline_at > moment:
            raise ValueError("基数时点无效")
    except (ValueError, TypeError):
        fail("LOAD_BASELINE_TIME_UNKNOWN", "当天累计分钟缺少同日且不晚于当前时刻的记录时间")
        return result
    result.update(baseline_minutes=float(baseline), baseline_at=baseline_at.isoformat())

    # 同 ID 去重与更正图检查不取决于上传顺序。
    rows, superseded = {}, set()
    for event in events:
        if not isinstance(event, dict):
            fail("LOAD_EVENT_INVALID", "作业记录格式无效")
            continue
        if event.get("worker_id") != worker.get("id"):
            continue
        ident = event.get("event_id")
        if not isinstance(ident, str) or not ident.strip():
            fail("LOAD_EVENT_ID_UNKNOWN", "作业记录缺稳定编号，无法防止重复计量")
            continue
        if ident in rows:
            if rows[ident] != event:
                fail("LOAD_EVENT_ID_CONFLICT", "同一作业记录编号有不同内容，请先更正")
            else:
                result["deduplicated_event_ids"].append(ident)
            continue
        rows[ident] = deepcopy(event)
    for ident, event in rows.items():
        prior = event.get("supersedes_event_id")
        if not prior:
            continue
        if not isinstance(prior, str):
            fail("LOAD_CORRECTION_INVALID", "作业更正引用格式无效")
            continue
        if prior not in rows:
            # 调用方已传入 current_events 时，旧记录不在数组中是正常的。
            continue
        if prior == ident or rows[prior].get("task_id") != event.get("task_id") or prior in superseded:
            fail("LOAD_CORRECTION_INVALID", "更正记录自引用、重复或农活不一致")
        superseded.add(prior)
        seen, cursor = set(), ident
        while cursor in rows:
            if cursor in seen:
                fail("LOAD_CORRECTION_INVALID", "作业更正链存在循环")
                break
            seen.add(cursor)
            cursor = rows[cursor].get("supersedes_event_id")
            if not isinstance(cursor, str):
                break

    intervals = []
    for ident, event in rows.items():
        if ident in superseded:
            continue
        if is_untimed_not_done(event):
            # 没有实际作业时段，不伪造日期，也不影响已明确的当天累计活动量。
            continue
        try:
            start = _instant(event.get("started_at", event.get("actual_start")))
            end = _instant(event.get("ended_at", event.get("actual_end")))
        except (ValueError, TypeError):
            fail("LOAD_EVENT_TIME_UNKNOWN", "同一人的记录缺实际起止，无法确定当天是否有未计劳动")
            continue
        if end < start:
            fail("LOAD_EVENT_TIME_INVALID", "实际作业起止倒置，不能忽略这条记录")
            continue
        if end <= lo or start >= hi:
            continue
        if start < lo or end > hi:
            fail("LOAD_CROSS_DAY_UNKNOWN", "跨日记录缺可定位休息阶段，请核对当天累计活动量")
            continue
        if end < start or end > moment:
            fail("LOAD_EVENT_TIME_INVALID", "实际作业起止倒置或超出当前时刻")
            continue
        status = event.get("status")
        if status == "unknown" or status not in {"completed", "partial", "interrupted", "not_done"}:
            fail("LOAD_EVENT_UNKNOWN", "存在尚未核对的实际作业记录，不能把可能的劳动记作0")
            continue
        rest = event.get("rest_minutes")
        clock = (end - start).total_seconds() / 60
        if not _finite(rest) or rest > clock:
            fail("LOAD_REST_UNKNOWN", "实际休息分钟缺失或超过经过时间，无法核对非休息负荷")
            continue
        active = clock - rest
        net = event.get("net_minutes")
        if net is not None and (not _finite(net) or net > active + 1e-6):
            fail("LOAD_EVENT_TIME_INVALID", "净劳动分钟与实际非休息时间冲突")
            continue
        fingerprint = (event.get("task_id"), start, end, rest, status)
        duplicate = False
        for previous in intervals:
            if fingerprint == previous["fingerprint"]:
                result["deduplicated_event_ids"].append(ident)
                duplicate = True
                break
            if max(start, previous["start"]) < min(end, previous["end"]):
                fail("LOAD_EVENT_OVERLAP", "同一人的实际作业时间重叠，需先核对或更正")
        if duplicate:
            continue
        intervals.append({"start": start, "end": end, "fingerprint": fingerprint})
        if start < baseline_at < end:
            if rest > 0:
                fail("LOAD_BASELINE_STRADDLE_UNKNOWN", "作业跨越累计基数时点且休息分布不明，请更新累计基数")
                continue
            before = (baseline_at - start).total_seconds() / 60
            after = (end - baseline_at).total_seconds() / 60
        elif end <= baseline_at:
            before, after = active, 0.0
        else:
            before, after = 0.0, active
        result["before_baseline_recorded_minutes"] += before
        result["after_baseline_added_minutes"] += after
        result["included_event_ids"].append(ident)

    if result["before_baseline_recorded_minutes"] > baseline + 1e-6:
        fail("LOAD_BASELINE_CONFLICT", "基数时点以前记录的劳动已超过自述累计量，请核对基数")
    total = baseline + result["after_baseline_added_minutes"]
    if total > 1440:
        fail("LOAD_DAY_OVERFLOW", "累计活动超过一天的分钟数，记录存在冲突")
    if not result["reasons"]:
        result.update(known=True, used_active_minutes=float(total))
    return result
