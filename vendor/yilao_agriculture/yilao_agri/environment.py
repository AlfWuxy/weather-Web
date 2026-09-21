"""核查一个完整作业区间；结果只表示给定规则是否满足，不保证个人安全。"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from . import alerts, op_scope
from .coverage import accumulation_exceeds, coverage_report, precipitation_exposure
from .gridmatch import assess_grid_match

__all__ = ["assess_interval", "noaa_heat_index_c", "check_agronomy_preconditions"]


_UNKNOWN = {
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未知", "待定", "待核实", "待补充", "待填写",
}
# R14-A05：条件值的「未观测」占位词。与 _UNKNOWN 分开维护，只作用于农艺前提与现场
# 条件核对；命中即保持未确认，不允许「未测 == 未测」被当成前提已满足。
_NOT_OBSERVED_TOKENS = _UNKNOWN | {
    "unconfirmed", "undefined", "unmeasured", "nil", "nan", "n.a.",
    "not available", "not measured", "not observed", "no data", "nodata",
    "missing", "pending", "unspecified", "not determined",
    "not applicable", "not_applicable", "to_be_confirmed",
    "未测", "未测量", "未观测", "未记录", "无记录", "无观测", "待观测",
    "待测", "不清楚", "不知道", "不详", "不明", "缺失", "缺", "没测",
    "不适用",
}
# R14-A05：阶段条件键族。声明这些键的农艺前提按「阶段条件核对」处理。
_STAGE_CONDITION_KEYS = frozenset({
    "stage", "crop_stage", "growth_stage", "phenology_stage", "stage_id",
})
# R14-A05：土壤/田水条件键族。声明这些键的农艺前提按「土壤田水核对」处理。
# 键名只是分类标签，不引入阈值、换算或灌溉处方。
_SOIL_WATER_CONDITION_KEYS = frozenset({
    "soil", "soil_moisture", "soil_water", "soil_water_state", "moisture",
    "field_water", "surface_water", "standing_water", "water_layer",
    "water_layer_cm", "water_depth", "water_depth_cm", "paddy_water",
    "paddy_water_layer_cm", "drainage", "drainage_state", "irrigated",
})
_LIMIT_FIELDS = {
    "max_temperature_c": ("temperature_c", "max"),
    "min_temperature_c": ("temperature_c", "min"),
    "max_wbgt_c": ("wbgt_c", "max"),
    "max_precipitation_mm": ("precipitation_mm", "max"),
    "max_wind_m_s": ("wind_m_s", "max"),
}


def _number(value: Any) -> float | None:
    # bool 在 Python 中属于 int，但不能冒充测量值。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _UNKNOWN
    return True


def _source(value: Any) -> bool:
    return isinstance(value, str) and _known(value)


def _observed(value: Any) -> bool:
    """条件值是否为「已观测」标量；缺失、占位、纯标点与非标量一律算未观测。

    与 _known 的差别只体现在条件核对里，因此不会改动天气、来源或标签的既有口径。
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _NOT_OBSERVED_TOKENS:
            return False
        # 只有标点（如 "-"、"?"、"—"）不是观测值；CJK 字符 isalnum 为真。
        return any(ch.isalnum() for ch in text)
    return False


def _precondition_family(key: str) -> str:
    name = key.strip().lower()
    if name in _STAGE_CONDITION_KEYS:
        return "stage"
    if name in _SOIL_WATER_CONDITION_KEYS:
        return "soil_water"
    return "other"


def _preconditions_blank() -> dict:
    return {"status": "not_evaluated", "task_stage": None, "families": {},
            "checked": [], "confirmed": [], "unconfirmed": [],
            "mismatched": [], "notes": []}


def check_agronomy_preconditions(task: dict, plot: dict, reject, warn) -> dict:
    """核对农艺前提里的土壤田水与阶段条件；缺观测值一律保持未确认。

    只做形状与相等核对：不认证现场观测方法、不换算单位、不跨词表映射阶段，
    也不生成灌溉、排水或用药处方。布尔与数值即使 Python 里相等也不得互当。
    """
    report = _preconditions_blank()
    report["status"] = "evaluated"
    agronomy = task.get("agronomy") if isinstance(task.get("agronomy"), dict) else {}
    expected_conditions = agronomy.get("conditions", {})
    actual_conditions = plot.get("conditions", {})
    task_stage = task.get("stage")
    report["task_stage"] = task_stage if isinstance(task_stage, str) else None
    if not isinstance(expected_conditions, dict) or not isinstance(actual_conditions, dict):
        reject("AGRONOMY_CONDITIONS_UNKNOWN", "农艺前提或现场条件格式无效。")
        report["status"] = "invalid"
        return report
    stage_required = False
    for key, expected in expected_conditions.items():
        name = key if isinstance(key, str) else str(key)
        family = _precondition_family(name)
        stage_required = stage_required or family == "stage"
        actual = actual_conditions.get(key)
        row = {"key": name, "family": family,
               "requirement_observed": _observed(expected),
               "observation_observed": _observed(actual)}
        if not row["requirement_observed"] or not row["observation_observed"]:
            row["verdict"] = "unconfirmed"
            report["unconfirmed"].append(row)
            reject("AGRONOMY_CONDITION_UNKNOWN", f"农艺前提 {key} 的要求或现场状态未知。")
        elif actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            # 布尔现场条件与数值不能因 Python 的 True == 1 而误判相同。
            row["verdict"] = "mismatched"
            report["mismatched"].append(row)
            reject("AGRONOMY_CONDITION_MISMATCH", f"现场条件 {key} 不满足任务农艺前提。")
        else:
            row["verdict"] = "confirmed"
            report["confirmed"].append(row)
            if (family == "stage" and isinstance(expected, str)
                    and isinstance(task_stage, str)
                    and task_stage.strip() != expected.strip()):
                warn(f"任务阶段 {task_stage} 与农艺前提 {key}={expected} 字面不同；"
                     "程序按不透明字符串比较，不代为跨词表映射阶段，请人工确认口径。")
        report["checked"].append(row)
    for row in report["checked"]:
        report["families"][row["family"]] = report["families"].get(row["family"], 0) + 1
    if stage_required and not _observed(task_stage):
        report["notes"].append("存在阶段前提，但任务自身阶段未确认。")
        reject("AGRONOMY_STAGE_UNKNOWN", "农艺前提含阶段条件，但任务声明的阶段未观测或为占位词。")
    if any(row["family"] == "soil_water" and row["verdict"] == "unconfirmed"
           for row in report["checked"]):
        report["notes"].append("土壤/田水前提含未观测项，保持未确认。")
        warn("土壤/田水前提存在未观测项：不得按已观测状态安排作业，"
             "也不得据此生成灌溉或排水处方。")
    return report


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("时间必须是带时区的 ISO 字符串或 datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间缺少时区")
    # 转为 UTC 后再比较和积分，避免夏令时折返或跨时区计算错误。
    return result.astimezone(timezone.utc)


def _noaa_formula_f(temperature_f: float, humidity: float) -> float:
    """NOAA WPC 的简单分支、Rothfusz 回归及湿度修正；单位为华氏度。"""
    t, rh = temperature_f, humidity
    simple = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    result = (simple + t) / 2.0
    if result >= 80.0:
        result = (
            -42.379 + 2.04901523 * t + 10.14333127 * rh
            - 0.22475541 * t * rh - 0.00683783 * t * t
            - 0.05481717 * rh * rh + 0.00122874 * t * t * rh
            + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh
        )
        if rh < 13.0 and 80.0 <= t <= 112.0:
            result -= (13.0 - rh) / 4.0 * math.sqrt((17.0 - abs(t - 95.0)) / 17.0)
        elif rh > 85.0 and 80.0 <= t <= 87.0:
            result += (rh - 85.0) / 10.0 * (87.0 - t) / 5.0
    return result


def _alert_state(weather: dict, plot: dict, demonstration: bool, now: datetime, reject, warn):
    """派生 unknown / unmarked / marked；缺信封或空数组冒充时阻断。

    预警信封的完整语义放在 yilao_agri.alerts（独立预警适配层）；这里只做与
    小时天气的接线：weather 是否带 alert_feed，以及演示模式的历史空数组兼容。
    """
    item_by_id: dict[str, dict] = {}
    item_tags: set[str] = set()
    feed = weather.get("alert_feed")
    if feed is None:
        records = weather.get("records")
        if demonstration:
            warn("演示模式缺少 alert_feed；不得把该结果迁到 shadow/assistance。")
            if isinstance(records, list) and records and all(
                isinstance(record, dict) and record.get("hazards") == [] for record in records
            ):
                warn("LEGACY_DEMO_EMPTY：演示允许 hazards=[]，语义是合成声明，不是已核对无预警。")
                return alerts.STATE_UNMARKED, None, item_by_id, item_tags
            return alerts.STATE_UNKNOWN, None, item_by_id, item_tags
        reject("ALERT_FEED_MISSING", "shadow/assistance 必须给出 alert_feed，缺省不能当成已核对无预警。")
        return alerts.STATE_UNKNOWN, None, item_by_id, item_tags
    if not isinstance(feed, dict):
        reject("ALERT_FEED_INVALID", "alert_feed 必须为对象。")
        return alerts.STATE_UNKNOWN, None, item_by_id, item_tags
    state, item_by_id, item_tags = alerts.classify_feed(
        feed, plot=plot, now=now, demonstration=demonstration, reject=reject, warn=warn,
        hourly_issued_at=weather.get("issued_at"),
    )
    return state, feed, item_by_id, item_tags


_PLOT_ONLY_WARNING = (
    "分地点暴露状态为 plot_only_proxy：所声明天气只代表该地块作业区间，"
    "往返路线与休息处未单独观测，不得自称已实测全路线或个体全程暴露。"
)
_EXPOSURE_METRIC_KEYS = (
    "max_temperature_c", "max_wbgt_c", "temperature_degree_minutes",
    "wbgt_degree_minutes", "max_precipitation_mm",
)


def _exposure_blank(status: str = "not_evaluated", note: str | None = None) -> dict:
    return {
        "status": status,
        "scope": None,
        "route_represented": False,
        "rest_represented": False,
        "route_measured": False,
        "rest_measured": False,
        "locations": [],
        "notes": [note] if note else [],
    }


def _screen_exposure_series(role, entry, coverage, blocked, require_daylight, rules,
                            now, start, end, demonstration, reject, warn) -> dict:
    """分地点逐时记录按与地块相同的边界保守筛查；只描述环境，不批准出工。"""
    tag = role.upper()
    metrics: dict[str, Any] = {key: None for key in _EXPOSURE_METRIC_KEYS}
    metrics["coverage_complete"] = False
    if not _source(entry.get("source")):
        reject(f"EXPOSURE_{tag}_SOURCE_UNKNOWN", "分地点暴露缺少可辨识来源。")
    kind = entry.get("kind")
    if kind == "synthetic":
        if demonstration:
            warn("分地点暴露使用合成记录演示，不能作为实际往返路线或休息处天气。")
        else:
            reject(f"EXPOSURE_{tag}_SYNTHETIC_FORBIDDEN", "真实运行模式不能使用合成分地点暴露。")
            return metrics
    elif kind not in {"forecast", "measured"}:
        reject(f"EXPOSURE_{tag}_KIND_UNKNOWN", "分地点暴露未明确区分预报、实测和合成。")
    if entry.get("environment") not in {"outdoor", "greenhouse"}:
        reject(f"EXPOSURE_{tag}_ENVIRONMENT_UNKNOWN", "分地点暴露未确认露天或棚内环境。")
    try:
        if _time(entry.get("issued_at")) > now:
            reject(f"EXPOSURE_{tag}_ISSUED_IN_FUTURE", "分地点暴露发布时间晚于当前决策时间。")
    except (ValueError, TypeError, OverflowError):
        reject(f"EXPOSURE_{tag}_ISSUED_TIME_UNKNOWN", "分地点暴露发布时间无效或缺少时区。")

    records = entry.get("records")
    if not isinstance(records, list):
        reject(f"EXPOSURE_{tag}_RECORDS_INVALID", "分地点暴露记录必须是区间列表。")
        return metrics
    spans: list[tuple[datetime, datetime, dict]] = []
    for record in records:
        try:
            a, b = _time(record.get("start")), _time(record.get("end"))
            if b <= a:
                raise ValueError("非正长度")
            spans.append((a, b, record))
        except (AttributeError, ValueError, TypeError, OverflowError):
            reject(f"EXPOSURE_{tag}_RECORD_INTERVAL_INVALID", "分地点暴露存在无效或无时区的记录区间。")
            return metrics
    spans.sort(key=lambda value: (value[0], value[1]))
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            reject(f"EXPOSURE_{tag}_RECORDS_OVERLAP", "分地点暴露记录区间重叠，不能确定唯一暴露。")
            return metrics
    cursor = start
    pieces: list[tuple[float, dict]] = []
    for a, b, record in spans:
        if b <= start or a >= end:
            continue
        left, right = max(a, start), min(b, end)
        if left > cursor:
            reject(f"EXPOSURE_{tag}_COVERAGE_GAP", "该地点的记录未完整覆盖作业区间。")
            return metrics
        cursor = max(cursor, right)
        pieces.append(((right - left).total_seconds() / 60.0, record))
    if cursor < end or not pieces:
        reject(f"EXPOSURE_{tag}_COVERAGE_GAP", "该地点的记录未完整覆盖作业区间。")
        return metrics

    temperatures: list[float] = []
    wbgt_values: list[float] = []
    precipitations: list[float] = []
    temperature_integral = wbgt_integral = 0.0
    wbgt_supported = _source(entry.get("source")) and _source(entry.get("wbgt_method"))
    for minutes, record in pieces:
        values: dict[str, float | None] = {}
        for field in ("temperature_c", "relative_humidity_pct", "wind_m_s", "shortwave_w_m2", "precipitation_mm"):
            value = _number(record.get(field))
            invalid = value is None
            if value is not None:
                invalid |= field == "relative_humidity_pct" and not 0 <= value <= 100
                invalid |= field in {"wind_m_s", "shortwave_w_m2", "precipitation_mm"} and value < 0
                invalid |= field == "temperature_c" and value <= -273.15
            if invalid:
                reject(f"EXPOSURE_{tag}_VALUE_UNKNOWN",
                       f"分地点暴露字段 {field} 缺失、非有限数值或越过物理边界。")
                value = None
            values[field] = value
        wbgt = _number(record.get("wbgt_c"))
        if "wbgt_c" in record and (wbgt is None or wbgt <= -273.15):
            reject(f"EXPOSURE_{tag}_WBGT_INVALID", "分地点暴露的 WBGT 不是有效有限数值。")
            wbgt = None
        if wbgt is not None and not wbgt_supported:
            reject(f"EXPOSURE_{tag}_WBGT_PROVENANCE_UNKNOWN",
                   "分地点暴露提供了 WBGT，但缺少来源或测量/估计方法。")
            wbgt = None
        values["wbgt_c"] = wbgt
        if not isinstance(record.get("daylight"), bool):
            reject(f"EXPOSURE_{tag}_DAYLIGHT_UNKNOWN", "分地点暴露区间是否有日光未知。")
        elif require_daylight is True and not record["daylight"]:
            reject(f"EXPOSURE_{tag}_DAYLIGHT_REQUIRED", "策略要求日光，但分地点暴露区间包含无日光时段。")
        hazards = record.get("hazards") if "hazards" in record else None
        known_list = isinstance(hazards, list) and all(isinstance(h, str) and _known(h) for h in hazards)
        if not known_list:
            reject(f"EXPOSURE_{tag}_HAZARDS_UNKNOWN", "分地点暴露的危险天气状态未知，不能把缺项当无预警。")
        else:
            # 集成适配：A03 把派生态重命名为 alerts.STATE_UNMARKED（原 queried_clear）。
            # 这里必须用同一常量，否则“已核对无预警”会被误判为未知。
            if not hazards and coverage != alerts.STATE_UNMARKED:
                if demonstration:
                    warn("演示用空 hazards 分地点记录，不能表示往返路线已核对无预警。")
                else:
                    reject(f"EXPOSURE_{tag}_HAZARDS_UNKNOWN",
                           "分地点空 hazards 仅在预警集合已核对为空时可用，不能冒充无预警。")
            for hazard in sorted(set(hazards)):
                if hazard in blocked:
                    reject("BLOCKED_HAZARD", f"分地点作业区间存在被策略禁止的危险天气：{hazard}。")
                else:
                    reject("UNREVIEWED_HAZARD",
                           f"分地点作业区间存在尚未纳入策略的危险天气标记：{hazard}，需先审核。")
        for code, field, comparator, threshold in rules:
            value = values[field]
            if value is None:
                reject(f"EXPOSURE_{tag}_{code}_UNKNOWN",
                       f"无法核查分地点天气限制 {code}，所需字段 {field} 未知。")
            elif (comparator == "max" and value > threshold) or (comparator == "min" and value < threshold):
                reject(f"EXPOSURE_{tag}_{code}_EXCEEDED",
                       f"分地点天气字段 {field} 不满足已提供的 {code} 限制。")
        t = values["temperature_c"]
        if t is not None:
            temperatures.append(t)
            temperature_integral += t * minutes
        if wbgt is not None:
            wbgt_values.append(wbgt)
            wbgt_integral += wbgt * minutes
        p = values["precipitation_mm"]
        if p is not None:
            precipitations.append(p)
    if len(temperatures) == len(pieces):
        metrics["max_temperature_c"] = max(temperatures)
        metrics["temperature_degree_minutes"] = temperature_integral
    if len(wbgt_values) == len(pieces):
        metrics["max_wbgt_c"] = max(wbgt_values)
        metrics["wbgt_degree_minutes"] = wbgt_integral
    if len(precipitations) == len(pieces):
        metrics["max_precipitation_mm"] = max(precipitations)
    metrics["coverage_complete"] = True
    return metrics


def _exposure_state(weather, demonstration, coverage, blocked, require_daylight, rules,
                    now, start, end, reject, warn) -> dict:
    """确定分地点暴露状态；缺声明就显式标成地块代理，不默认它是全路线实测。"""
    state = _exposure_blank("plot_only_proxy")
    state["scope"] = "plot_only"
    block = weather.get("exposure")
    if block is None:
        state["notes"].append("未提供分地点暴露声明；按 plot_only_proxy 处理。")
        warn(_PLOT_ONLY_WARNING)
        return state
    if not isinstance(block, dict):
        reject("EXPOSURE_DECLARATION_INVALID", "exposure 必须为对象。")
        return state
    scope = block.get("scope")
    if scope not in {"plot_only", "plot_and_access_route"}:
        reject("EXPOSURE_SCOPE_UNKNOWN", "exposure.scope 须为 plot_only 或 plot_and_access_route。")
        return state
    declared = [role for role in ("access_route", "rest_place") if isinstance(block.get(role), dict)]
    if scope == "plot_only":
        if declared:
            reject("EXPOSURE_SCOPE_MISMATCH", "scope=plot_only 不得同时声明分地点数据。")
        state["notes"].append("已显式声明 scope=plot_only。")
        warn(_PLOT_ONLY_WARNING)
        return state
    state["scope"] = scope
    if not declared:
        reject("EXPOSURE_DECLARATION_EMPTY", "声明分地点覆盖时必须给出 access_route 或 rest_place。")
        return state
    measured_roles: set[str] = set()
    for role in declared:
        entry = block[role]
        tag = role.upper()
        source_ok = _source(entry.get("source"))
        if not source_ok:
            reject(f"EXPOSURE_{tag}_SOURCE_UNKNOWN", "分地点暴露缺少可辨识来源。")
        review = entry.get("review_status")
        if review == "illustrative" and demonstration:
            warn("分地点暴露为演示参数，不是已核实的实际测量。")
        elif review != "confirmed":
            reject(f"EXPOSURE_{tag}_UNCONFIRMED", "分地点暴露的审核状态不可用于当前模式。")
        measured = entry.get("measured") is True
        has_series = isinstance(entry.get("records"), list) and bool(entry.get("records"))
        if measured:
            if not _source(entry.get("method")):
                reject(f"EXPOSURE_{tag}_METHOD_MISSING", "声称实测必须写明测量或估计方法。")
            if not has_series:
                reject(f"EXPOSURE_{tag}_SERIES_MISSING",
                       "声称实测但未给出该地点逐时记录；不能把声明当作已实测分地点覆盖。")
            elif entry.get("kind") != "measured":
                reject(f"EXPOSURE_{tag}_KIND_MISMATCH",
                       "逐时记录 kind 不是 measured；不能把预报或合成当作已实测分地点暴露。")
        location = {"role": role, "claimed_measured": measured, "series_screened": False,
                    "measured_claim_verified": False, "metrics": None}
        if has_series:
            location["series_screened"] = True
            location["metrics"] = _screen_exposure_series(
                role, entry, coverage, blocked, require_daylight, rules, now, start, end,
                demonstration, reject, warn)
            verified = (measured and entry.get("kind") == "measured" and source_ok
                        and (review == "confirmed" or (demonstration and review == "illustrative")))
            location["measured_claim_verified"] = verified
            state["route_represented" if role == "access_route" else "rest_represented"] = True
            if verified:
                measured_roles.add(role)
        state["locations"].append(location)
    state["route_measured"] = "access_route" in measured_roles
    state["rest_measured"] = "rest_place" in measured_roles
    if measured_roles == {"access_route", "rest_place"}:
        state["status"] = "route_declared_measured"
        warn("分地点逐时记录声明为实测；程序不认证测量方法，也不等同个人实际全程暴露。")
    else:
        state["status"] = "route_declared_unmeasured"
        state["notes"].append("已提供分地点覆盖声明，但未构成往返与休息处均为已实测的完整证据。")
    return state


def noaa_heat_index_c(temperature_c: Any, relative_humidity_pct: Any) -> float | None:
    """只在 NWS 公布图表覆盖的保守子域输出 HI；其余返回 None。

    图表列为 80–110°F、40–100% RH，极热高湿处有空白。
    非网格点采用相邻较热一行的最大湿度，避免越过图表空白外推。
    这是本实现的计算支持范围，不是人体安全阈值；也不表示图表内个体安全。
    """
    t_c, rh = _number(temperature_c), _number(relative_humidity_pct)
    if t_c is None or rh is None:
        return None
    t_f = t_c * 1.8 + 32.0
    if not 80.0 <= t_f <= 110.0 or not 40.0 <= rh <= 100.0:
        return None
    # 来源：NWS Dodge City Heat Index 表；不复制其风险分类。
    maximum_rh = (100, 100, 100, 100, 100, 100, 90, 85, 75, 70, 65, 60, 55, 50, 45, 40)
    row = min(15, math.ceil((t_f - 80.0) / 2.0))
    if rh > maximum_rh[row]:
        return None
    return (_noaa_formula_f(t_f, rh) - 32.0) / 1.8


def assess_interval(
    request: dict, task: dict, worker: dict, start: datetime, end: datetime
) -> dict:
    """对完整半开区间检查约束，并按记录重叠分钟积分，不改变输入。"""
    reasons: list[dict[str, str]] = []
    warnings: list[str] = [
        "通过只表示满足输入中已确认的约束，不代表个人健康安全或疾病概率。",
        "温度积分与 WBGT 积分分别为摄氏度×分钟，不是生理剂量，不得相加。",
        "暖热排序辅助积分逐段采用 max(0,摄氏值)；0 只是算术下界，不是舒适或安全阈值，低温风险仍需已确认的最低温限制。",
        "区间指标使用所声明的田块天气；往返路线和休息处若环境不同，未单独观测，不能视为个人实际全程暴露。",
    ]
    metrics: dict[str, float | None] = {
        "max_temperature_c": None,
        "max_wbgt_c": None,
        "temperature_degree_minutes": None,
        "wbgt_degree_minutes": None,
        "temperature_positive_degree_minutes": None,
        "wbgt_positive_degree_minutes": None,
        "heat_index_max_c": None,
        "grid_plot_distance_km": None,
        "grid_cell_id": None,
        "grid_resolution_km": None,
    }

    def reject(code: str, message: str) -> None:
        reason = {"code": code, "message": message}
        if reason not in reasons:
            reasons.append(reason)

    def warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    exposure: dict = _exposure_blank("not_evaluated", "未完成环境评估，未确定分地点暴露状态。")
    agronomy_preconditions: dict = _preconditions_blank()

    def finish() -> dict:
        return {"allowed": not reasons, "reasons": reasons, "metrics": metrics,
                "warnings": warnings, "exposure": exposure,
                "agronomy_preconditions": agronomy_preconditions}

    try:
        start, end, now = _time(start), _time(end), _time(request.get("now"))
    except (ValueError, TypeError, OverflowError):
        reject("INTERVAL_TIME_INVALID", "区间和当前时间必须是有效的带时区时间。")
        return finish()
    if end <= start:
        reject("INTERVAL_EMPTY", "评估区间必须具有正时长。")
        return finish()
    for name, direction in (("horizon_start", "start"), ("horizon_end", "end")):
        try:
            boundary = _time(request.get(name))
            if (direction == "start" and start < boundary) or (direction == "end" and end > boundary):
                reject("OUTSIDE_PLANNING_HORIZON", "作业区间超出声明的规划范围。")
        except (ValueError, TypeError, OverflowError):
            reject("HORIZON_INVALID", "规划范围缺少有效的带时区时间。")

    mode = request.get("mode")
    if mode not in {"demonstration", "shadow", "assistance"}:
        reject("MODE_UNKNOWN", "运行模式未知。")
    demonstration = mode == "demonstration"
    policy = request.get("policy") or {}
    agronomy = task.get("agronomy") or {}
    limits = worker.get("limits") or {}
    for obj, status_key, prefix, label in (
        (policy, "review_status", "POLICY", "策略"),
        (agronomy, "status", "AGRONOMY", "农艺规则"),
        (limits, "review_status", "WORKER_LIMITS", "个人限制"),
    ):
        status = obj.get(status_key)
        if status == "illustrative" and demonstration:
            warn(f"{label}是演示参数，不是已核实的实际规则。")
        elif status != "confirmed":
            reject(f"{prefix}_UNCONFIRMED", f"{label}未确认，不能用于当前模式的安排。")
        if not _source(obj.get("source")):
            reject(f"{prefix}_SOURCE_UNKNOWN", f"{label}缺少来源说明。")
    if worker.get("state") != "clear":
        reject("WORKER_STATE_NOT_CLEAR", "人员当前状态不是已确认可进入排程的 clear 状态。")
    if not _source(task.get("deadline_source")):
        reject("DEADLINE_SOURCE_UNKNOWN", "任务截止时间缺少可辨识来源，不能以未知占位作为农时依据。")
    task_tags = task.get("tags", [])
    forbidden = limits.get("forbidden_tags", [])
    if not isinstance(task_tags, list) or not isinstance(forbidden, list):
        reject("TASK_TAGS_INVALID", "任务标签或禁止标签格式无效。")
    elif not all(isinstance(t, str) and _known(t) for t in task_tags + forbidden):
        reject("TASK_TAGS_UNKNOWN", "任务标签或禁止标签含未知项。")
    else:
        for tag in sorted(set(task_tags).intersection(forbidden)):
            reject("WORKER_TAG_FORBIDDEN", f"个人限制禁止任务标签：{tag}。")
    if "max_load_kg" in limits:
        load, maximum_load = _number(task.get("load_per_trip_kg")), _number(limits["max_load_kg"])
        if maximum_load is None or maximum_load < 0:
            reject("WORKER_LOAD_LIMIT_UNKNOWN", "个人最大负重限制无效。")
        elif load is None or load < 0:
            reject("TASK_LOAD_UNKNOWN", "已有个人负重限制，但任务单趟负重未知。")
        elif load > maximum_load:
            reject("WORKER_LOAD_EXCEEDED", "任务单趟负重超过已提供的个人限制。")
    elif "load_per_trip_kg" in task:
        load = _number(task["load_per_trip_kg"])
        if load is None or load < 0:
            reject("TASK_LOAD_UNKNOWN", "任务单趟负重值无效。")
        elif load > 0:
            reject("WORKER_LOAD_LIMIT_UNKNOWN", "任务有实际负重，但尚未提供个人负重上限，不能视为无限制。")

    # R14-A09：喷药/粪肥只按显式审核范围开放；夹带剂量/用药请求一律关闭。
    scope_result = op_scope.check_operation_scope(task, mode)
    if scope_result["blocked"]:
        reject(scope_result["code"], scope_result["message"])
    elif scope_result["required"]:
        warn("高风险操作已按声明的审核范围开放；仍不生成剂量、稀释倍数或施药许可，也不代表现场有效或个人安全。")

    plots = [p for p in request.get("plots", []) if isinstance(p, dict) and p.get("id") == task.get("plot_id")]
    if len(plots) != 1:
        reject("PLOT_UNKNOWN", "任务地块不能唯一确定。")
        return finish()
    plot = plots[0]
    # R14-A05：农艺前提里的土壤田水与阶段条件集中核对；缺观测值一律保持未确认。
    agronomy_preconditions = check_agronomy_preconditions(task, plot, reject, warn)

    weather_map = request.get("weather") or {}
    weather = weather_map.get(task.get("plot_id")) if isinstance(weather_map, dict) else None
    if not isinstance(weather, dict):
        reject("WEATHER_MISSING", "该地块没有天气记录。")
        return finish()
    if not _source(weather.get("source")):
        reject("WEATHER_SOURCE_UNKNOWN", "天气记录没有明确来源。")
    kind = weather.get("kind")
    if kind == "synthetic":
        if demonstration:
            warn("正在使用合成天气演示，不能作为实际天气或现场验证。")
        else:
            reject("SYNTHETIC_WEATHER_FORBIDDEN", "真实运行模式不能使用合成天气。")
    elif kind not in {"forecast", "measured"}:
        reject("WEATHER_KIND_UNKNOWN", "天气数据未明确区分预报、实测和合成。")
    if plot.get("environment") not in {"outdoor", "greenhouse"}:
        reject("PLOT_ENVIRONMENT_UNKNOWN", "地块未确认露天或棚内环境。")
    if weather.get("environment") != plot.get("environment"):
        reject("WEATHER_ENVIRONMENT_MISMATCH", "天气记录的露天或棚内环境与地块不符。")
    # 网格/站点与田块核对：坐标、距离、环境。容差只来自已确认策略。
    grid_result = assess_grid_match(plot, weather.get("grid"), policy, demonstration)
    for reason in grid_result["reasons"]:
        reject(reason["code"], reason["message"])
    for message in grid_result["warnings"]:
        warn(message)
    metrics["grid_plot_distance_km"] = grid_result["metrics"]["grid_plot_distance_km"]
    metrics["grid_cell_id"] = grid_result["metrics"]["grid_cell_id"]
    metrics["grid_resolution_km"] = grid_result["metrics"]["grid_resolution_km"]
    issued = None
    try:
        if "forecast_run_snapshot" in weather:
            from .forecast_run import validate_run_snapshot, validate_run_location
            # 归档生成与下载不刷新模式年龄，也不延长预测跨度。
            validate_run_location(weather["forecast_run_snapshot"], plot)
            issued = validate_run_snapshot(weather, now)
        else:
            issued = _time(weather.get("issued_at"))
        if issued > now:
            reject("WEATHER_ISSUED_IN_FUTURE", "天气发布时间晚于当前决策时间。")
    except (ValueError, TypeError, OverflowError):
        if "forecast_run_snapshot" in weather:
            reject("FORECAST_RUN_SNAPSHOT_INVALID", "单次运行来源、时间或天气内容未通过核对。")
        else:
            reject("WEATHER_ISSUED_TIME_UNKNOWN", "天气发布时间无效或缺少时区。")
    age_limit = _number(policy.get("max_forecast_age_minutes"))
    horizon_limit = _number(policy.get("max_forecast_horizon_hours"))
    if age_limit is None or age_limit < 0 or horizon_limit is None or horizon_limit < 0:
        reject("WEATHER_TIME_POLICY_UNKNOWN", "数据最大年龄或预测跨度缺少有效策略。")
    elif issued is not None:
        if (now - issued).total_seconds() / 60.0 > age_limit:
            reject("WEATHER_STALE", "天气数据在当前决策时已超过策略最大年龄。")
        if kind in {"forecast", "synthetic"} and (end - issued).total_seconds() / 3600.0 > horizon_limit:
            reject("FORECAST_HORIZON_EXCEEDED", "作业区间末端超过自发布时间或已核对模式初始化起的允许预测跨度。")
    if kind == "measured" and end > now:
        reject("MEASURED_WEATHER_IS_NOT_FORECAST", "过去实测天气不能代替未来作业区间的预报。")

    rules: list[tuple[str, str, str, float]] = []
    for group, owner in (("POLICY", policy), ("AGRONOMY", agronomy)):
        weather_limits = owner.get("weather_limits", {})
        if not isinstance(weather_limits, dict):
            reject(f"{group}_WEATHER_LIMITS_INVALID", "天气限制格式无效。")
            continue
        for name, value in weather_limits.items():
            threshold = _number(value)
            if name not in _LIMIT_FIELDS or threshold is None:
                reject(f"{group}_WEATHER_LIMIT_UNKNOWN", f"不支持或无效的天气限制：{name}。")
            else:
                field, comparator = _LIMIT_FIELDS[name]
                rules.append((f"{group}_{name.upper()}", field, comparator, threshold))
    heat_limits = policy.get("weather_limits")
    if not isinstance(heat_limits, dict) or not any(
        field in heat_limits for field in ("max_temperature_c", "max_wbgt_c")
    ):
        if demonstration:
            warn("演示策略缺少最高气温或 WBGT 热筛查规则，只能比较环境，不能据此批准实际出工。")
        else:
            reject("HEAT_SCREENING_RULE_MISSING", "真实运行策略缺少最高气温或 WBGT 热筛查规则，需提供有依据的限制。")
    if not isinstance(policy.get("require_daylight"), bool):
        reject("DAYLIGHT_POLICY_UNKNOWN", "策略未明确是否要求日光。")
    blocked = policy.get("blocked_hazards")
    if not isinstance(blocked, list) or not all(isinstance(h, str) and _known(h) for h in blocked):
        reject("HAZARD_POLICY_UNKNOWN", "策略的禁止危险天气列表无效或未知。")
        blocked = []
    coverage, _feed, item_by_id, item_tags = _alert_state(
        weather, plot, demonstration, now, reject, warn)
    exposure = _exposure_state(weather, demonstration, coverage, blocked,
                               policy.get("require_daylight"), rules, now, start, end, reject, warn)

    records = weather.get("records")
    spans: list[tuple[datetime, datetime, dict]] = []
    structurally_valid = True
    if not isinstance(records, list):
        reject("WEATHER_RECORDS_INVALID", "天气记录必须是区间列表。")
        return finish()
    for record in records:
        try:
            a, b = _time(record.get("start")), _time(record.get("end"))
            if b <= a:
                raise ValueError("非正长度")
            spans.append((a, b, record))
        except (AttributeError, ValueError, TypeError, OverflowError):
            reject("WEATHER_RECORD_INTERVAL_INVALID", "存在无效或无时区的天气记录区间。")
            structurally_valid = False
    spans.sort(key=lambda value: (value[0], value[1]))
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            reject("WEATHER_RECORDS_OVERLAP", "天气记录区间重叠，不能确定唯一暴露或重复积分。")
            structurally_valid = False

    # 半开区间覆盖：缺口判定统一交给 coverage.coverage_report，规则只有 [start,end)。
    # 注意变量名不能叫 coverage：alert 派生状态已占用该名字。
    span_coverage = coverage_report(spans, start, end)
    if span_coverage["gaps"]:
        reject("WEATHER_COVERAGE_GAP", "作业区间内存在天气数据缺口。")
    pieces: list[tuple[float, dict]] = []
    for a, b, record in spans:
        if b <= start or a >= end:
            continue
        pieces.append(((min(b, end) - max(a, start)).total_seconds() / 60.0, record))
        if kind == "measured" and (b > now or (issued is not None and b > issued)):
            reject("MEASURED_RECORD_NOT_AVAILABLE", "实测记录的结束时间晚于当前时间或其发布时间。")
        if kind == "measured" and age_limit is not None and (now - b).total_seconds() / 60.0 > age_limit:
            reject("MEASURED_RECORD_STALE", "实测有效时间已超过策略最大年龄。")
    coverage_complete = structurally_valid and span_coverage["complete"]

    wbgt_supported = _source(weather.get("source")) and _source(weather.get("wbgt_method"))
    temperature_values: list[float] = []
    wbgt_values: list[float] = []
    heat_index_values: list[float] = []
    temperature_integral = wbgt_integral = 0.0
    temperature_positive_integral = wbgt_positive_integral = 0.0
    for minutes, record in pieces:
        values: dict[str, float | None] = {}
        for field in ("temperature_c", "relative_humidity_pct", "wind_m_s", "shortwave_w_m2", "precipitation_mm"):
            value = _number(record.get(field))
            invalid = value is None
            if value is not None:
                invalid |= field == "relative_humidity_pct" and not 0 <= value <= 100
                invalid |= field in {"wind_m_s", "shortwave_w_m2", "precipitation_mm"} and value < 0
                invalid |= field == "temperature_c" and value <= -273.15
            if invalid:
                reject("WEATHER_VALUE_UNKNOWN", f"天气字段 {field} 缺失、非有限数值或越过物理边界。")
                value = None
            values[field] = value
        wbgt = _number(record.get("wbgt_c"))
        if "wbgt_c" in record and wbgt is None:
            reject("WBGT_VALUE_INVALID", "已提供的 WBGT 不是有效有限数值。")
        if wbgt is not None and not wbgt_supported:
            reject("WBGT_PROVENANCE_UNKNOWN", "已提供 WBGT，但缺少天气来源或测量/估计方法。")
            wbgt = None
        if wbgt is not None and wbgt <= -273.15:
            reject("WBGT_VALUE_INVALID", "WBGT 低于物理温度边界。")
            wbgt = None
        values["wbgt_c"] = wbgt
        if not isinstance(record.get("daylight"), bool):
            reject("DAYLIGHT_UNKNOWN", "该区间是否有日光未知。")
        elif policy.get("require_daylight") is True and not record["daylight"]:
            reject("DAYLIGHT_REQUIRED", "策略要求日光，但作业区间包含无日光时段。")
        alerts.apply_record_hazards(record, coverage, item_by_id, item_tags, blocked, reject)
        for code, field, comparator, threshold in rules:
            value = values[field]
            if value is None:
                reject(f"{code}_UNKNOWN", f"无法核查天气限制 {code}，所需字段 {field} 未知。")
            elif (comparator == "max" and value > threshold) or (comparator == "min" and value < threshold):
                reject(f"{code}_EXCEEDED", f"天气字段 {field} 不满足已提供的 {code} 限制。")
        # 降水禁忌用原记录期间的总量，不按作业重叠分钟稀释。
        t = values["temperature_c"]
        if t is not None:
            temperature_values.append(t)
            temperature_integral += t * minutes
            temperature_positive_integral += max(0.0, t) * minutes
        if wbgt is not None:
            wbgt_values.append(wbgt)
            wbgt_integral += wbgt * minutes
            wbgt_positive_integral += max(0.0, wbgt) * minutes
        hi = noaa_heat_index_c(t, values["relative_humidity_pct"])
        if hi is not None:
            heat_index_values.append(hi)

    # 降水：单条记录已按声明的整段值比较（不按作业重叠分钟摊薄）；这里再补跨记录
    # 合计的保守判定，避免把同一场雨拆成多条较短记录后逐条放行。
    precipitation_rules = [(code, threshold) for code, field, _comparator, threshold in rules
                           if field == "precipitation_mm"]
    if precipitation_rules:
        # 集成适配：此处局部名不能用 exposure，否则会覆盖 A09 的分地点暴露状态（同名局部变量）。
        precipitation_exposure_result = precipitation_exposure(spans, start, end)
        for code, threshold in precipitation_rules:
            if accumulation_exceeds(precipitation_exposure_result, threshold):
                reject(f"{code}_ACCUMULATION_EXCEEDED",
                       f"作业区间内多条记录合计降水 {precipitation_exposure_result['interval_total_mm']} mm 超过 {code} 限制；"
                       "降水按各记录声明值直接合计，不按作业分钟摊薄。")
                warn("跨记录降水合计只覆盖与作业区间相交的记录；若来源的累积窗口与限制口径不同，"
                     "需要来源声明窗口，不能靠缩短作业时间通过。")
            elif (precipitation_exposure_result["interval_total_mm"] or 0.0) > 0.0:
                warn("降水按记录声明的整段值计入，不按作业分钟缩放；限制口径应与来源的累积窗口一致。")

    if coverage_complete and len(temperature_values) == len(pieces):
        if math.isfinite(temperature_integral) and math.isfinite(temperature_positive_integral):
            metrics["max_temperature_c"] = max(temperature_values)
            metrics["temperature_degree_minutes"] = temperature_integral
            metrics["temperature_positive_degree_minutes"] = temperature_positive_integral
        else:
            reject("ENVIRONMENT_METRIC_OVERFLOW", "环境积分超出有限数值范围。")
    if coverage_complete and len(wbgt_values) == len(pieces):
        if math.isfinite(wbgt_integral) and math.isfinite(wbgt_positive_integral):
            metrics["max_wbgt_c"] = max(wbgt_values)
            metrics["wbgt_degree_minutes"] = wbgt_integral
            metrics["wbgt_positive_degree_minutes"] = wbgt_positive_integral
        else:
            reject("ENVIRONMENT_METRIC_OVERFLOW", "环境积分超出有限数值范围。")
    else:
        warn("完整区间缺少有来源且方法一致的 WBGT；WBGT 指标返回 null，不能以 HI 或零补齐。")
    if coverage_complete and len(heat_index_values) == len(pieces):
        metrics["heat_index_max_c"] = max(heat_index_values)
    else:
        warn("完整区间的 HI 输入缺失或超出本实现采用的 NWS 图表子域，HI 最大值返回 null。")
    if heat_index_values:
        warn("NOAA HI 只描述阴影、静息条件下的温湿组合，不覆盖田间日晒、风、衣着或劳动产热。")
    if any(record.get("wbgt_c") is not None for _, record in pieces) and kind != "measured":
        warn("当前 WBGT 来自输入的预报或合成估计，不能称为田间实测；程序不认证所声明的方法。")
    return finish()
