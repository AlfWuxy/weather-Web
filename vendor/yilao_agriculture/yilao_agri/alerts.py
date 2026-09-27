"""预警独立适配层（R13-A03）。

本模块只处理预警产品，不读小时天气数值、不发明阈值、不发通知、不改生产系统。
它把外部预警信封/原始响应规范化成内部 `weather[plot_id].alert_feed`，并显式
区分三种状态：

- `unknown`   预警未知：未查询、查询失败、查询不完整，或覆盖状态缺省/null。
              缺适用数据时必须可解释地阻断（返回 reasons），不得当成“无标记”。
- `unmarked`  无标记：已对适用地点完成官方预警查询且集合为空（queried_clear）。
              只有此状态才允许 `records[].hazards == []`。
- `marked`    已有标记：查询结果含活动预警条目，存在非空 hazards。

重要边界：`unmarked` 只表示该次官方预警查询集合为空，不是田间无危险，也不是
个人健康安全许可。本模块不认证来源真实性，程序不代替登记当局。

依据（沿用 R04-A03 已核查记录，本轮未新增联网核查）：
- Open-Meteo / ECMWF IFS / weather_code 等数值预报产品没有官方预警通道，
  普通小时天气接口缺预警键不等于该地无预警。
- WMO 规定官方预警由登记当局以 CAP 1.2 发布；按 forecast zone 查询会漏掉
  县/多边形短时预警，空结果不是 queried_clear。
- DWD 等机构的 CAP 全量快照：空结果表示当前无活动预警，但仅在确实完成
  查询（snapshot_complete=true、有 authority_domain）时才成立。
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Callable

from .models import (
    ALERT_CLEAR_QUERY_TYPES,
    ALERT_COVERAGE_ALL,
    ALERT_COVERAGE_CLEAR,
    ALERT_COVERAGE_MARKED,
    ALERT_COVERAGE_UNKNOWN,
    ALERT_PROTOCOLS,
    ALERT_QUERY_FRESHNESS_BASIS,
    alert_query_issues,
    alert_source_is_nwp,
)

# 三态名称：unknown（预警未知）/ unmarked（无标记）/ marked（已有标记）。
STATE_UNKNOWN = "unknown"
STATE_UNMARKED = "unmarked"
STATE_MARKED = "marked"
ALERT_STATES = (STATE_UNKNOWN, STATE_UNMARKED, STATE_MARKED)

# 原始 coverage_status -> 三态。queried_clear 是唯一允许“无标记”的原始状态。
COVERAGE_TO_STATE = {
    "not_queried": STATE_UNKNOWN,
    "feed_unavailable": STATE_UNKNOWN,
    "query_incomplete": STATE_UNKNOWN,
    "queried_clear": STATE_UNMARKED,
    "active_alerts": STATE_MARKED,
    "synthetic_declared": STATE_UNMARKED,  # 仅演示；含 items 时由 items 决定
}

COVERAGE_FOR_STATE = {
    STATE_UNKNOWN: ("not_queried", "feed_unavailable", "query_incomplete"),
    STATE_UNMARKED: ("queried_clear",),
    STATE_MARKED: ("active_alerts",),
}

# 外部提供者类型。hourly_forecast_without_alert_channel 表示该提供者只有普通
# 小时预报、没有官方预警通道：适配结果必须是 unknown，而不是空数组。
PROVIDER_HOURLY_NO_ALERT_CHANNEL = "hourly_forecast_without_alert_channel"
PROVIDER_CAP_FULL_SNAPSHOT = "cap_full_snapshot"
PROVIDER_NWS_POINT = "nws_alerts_point"
PROVIDER_NWS_ZONE = "nws_alerts_zone"
PROVIDER_FEED_UNAVAILABLE = "official_alert_feed_unavailable"
PROVIDER_NOT_QUERIED = "official_alert_not_queried"
PROVIDER_KINDS = (
    PROVIDER_HOURLY_NO_ALERT_CHANNEL,
    PROVIDER_CAP_FULL_SNAPSHOT,
    PROVIDER_NWS_POINT,
    PROVIDER_NWS_ZONE,
    PROVIDER_FEED_UNAVAILABLE,
    PROVIDER_NOT_QUERIED,
)

_UNKNOWN_WORDS = {
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未知", "待定", "待核实", "待补充", "待填写",
}


def _number(value: Any) -> float | None:
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
        return value.strip().lower() not in _UNKNOWN_WORDS
    return True


def _source(value: Any) -> bool:
    return isinstance(value, str) and _known(value)


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("时间必须是带时区的 ISO 字符串或 datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间缺少时区")
    return result.astimezone(timezone.utc)


def state_for_coverage(coverage_status: Any, has_items: bool = False) -> str:
    """按原始覆盖状态派生三态。缺省/null 一律 unknown，不默认 queried_clear。"""
    if coverage_status == "synthetic_declared":
        return STATE_MARKED if has_items else STATE_UNMARKED
    return COVERAGE_TO_STATE.get(coverage_status, STATE_UNKNOWN)


def classify_feed(
    feed: dict,
    *,
    plot: dict,
    now: datetime,
    demonstration: bool,
    reject: Callable[[str, str], None],
    warn: Callable[[str], None],
    hourly_issued_at: Any = None,
) -> tuple[str, dict, set]:
    """校验预警信封并派生三态；返回 (state, items_by_id, item_tags)。

    语义与 R11-A05 的 runtime 三态一致：先在这里集中，环境层只做接线。
    缺信封、缺 query、缺 queried_at/issued_at、zone 空结果、point 改格点等
    都给出可解释的 reason 码；空数组只有在 queried_clear 下才被接受。
    """
    item_by_id: dict[str, dict] = {}
    item_tags: set[str] = set()

    coverage_status = feed.get("coverage_status")
    if not isinstance(coverage_status, str) or coverage_status not in ALERT_COVERAGE_ALL:
        reject("ALERT_COVERAGE_STATUS_UNKNOWN",
               "coverage_status 须为 not_queried / feed_unavailable / query_incomplete / "
               "queried_clear / active_alerts / synthetic_declared；缺省或 null 不是 queried_clear。")
        coverage_status = None
    if coverage_status == "synthetic_declared" and not demonstration:
        reject("SYNTHETIC_ALERT_FORBIDDEN", "synthetic_declared 只能用于 demonstration。")

    source = feed.get("source")
    protocol = feed.get("protocol")
    if coverage_status in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        if not _source(source):
            reject("ALERT_SOURCE_UNKNOWN", "已核对或已有标记时必须给出可辨识预警源，不能用空串或占位词。")
        if alert_source_is_nwp(source):
            reject("ALERT_SOURCE_IS_NWP", "数值预报/天气代码/再分析名称不能当作官方预警源。")
        if protocol not in ALERT_PROTOCOLS:
            reject("ALERT_PROTOCOL_UNKNOWN", "protocol 须为 CAP-1.2 / NWS-API-alerts / official_equivalent。")
    elif alert_source_is_nwp(source):
        reject("ALERT_SOURCE_IS_NWP", "即使声明未知，也不得把 NWP 源标成预警源。")

    query = feed.get("query") if isinstance(feed.get("query"), dict) else None
    if query is None:
        reject("ALERT_QUERY_MISSING", "alert_feed.query 必须保存查询类型与地点，缺省不能推断。")
        query = {}
    q_type = query.get("type")
    q_lat, q_lon = query.get("latitude"), query.get("longitude")
    plot_lat, plot_lon = plot.get("latitude"), plot.get("longitude")
    plot_ok = _number(plot_lat) is not None and _number(plot_lon) is not None

    if q_type == "zone":
        if coverage_status in ALERT_COVERAGE_CLEAR:
            reject("ALERT_ZONE_QUERY_NOT_CLEAR",
                   "仅按 forecast zone 查询会漏掉县/多边形短时预警，空结果不得标 queried_clear。")
        elif coverage_status not in ALERT_COVERAGE_UNKNOWN | {None, "synthetic_declared"}:
            reject("ALERT_ZONE_QUERY_NOT_CLEAR", "zone 查询不能当作完整地点核对。")

    if coverage_status in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        if q_type not in ALERT_CLEAR_QUERY_TYPES:
            reject("ALERT_QUERY_TYPE_INCOMPLETE",
                   "已核对/已有标记的 query.type 须为 point、county 或 full_snapshot。")
        if q_type == "point":
            if _number(q_lat) is None or _number(q_lon) is None:
                reject("ALERT_LOCATION_MISSING", "point 查询必须保存 latitude/longitude。")
            elif plot_ok and (float(q_lat) != float(plot_lat) or float(q_lon) != float(plot_lon)):
                reject("ALERT_QUERY_POINT_NEQ_PLOT",
                       "point 查询必须使用 plot 坐标，不得改写成数值预报格点。")
        elif q_type == "county" and not _source(query.get("spatial_id")):
            reject("ALERT_LOCATION_MISSING", "county 查询必须保存 spatial_id。")
        elif q_type == "full_snapshot":
            if not _source(query.get("authority_domain")):
                reject("ALERT_AUTHORITY_DOMAIN_MISSING",
                       "全量快照必须声明 authority_domain；空域不能把全国快照当成田块已核对。")
            if query.get("snapshot_complete") is not True:
                reject("ALERT_SNAPSHOT_INCOMPLETE",
                       "full_snapshot 的 snapshot_complete 必须为 true 才能 queried_clear。")

    query_issues = alert_query_issues(feed, now, demonstration=demonstration)
    native_query_valid = "query_snapshot" in feed and not any(
        code == "ALERT_QUERY_SNAPSHOT_INVALID" for code, _ in query_issues)
    queried_at = alert_issued = None
    raw_queried, raw_issued = feed.get("queried_at"), feed.get("issued_at")
    if raw_queried is not None:
        try:
            queried_at = _time(raw_queried)
        except (ValueError, TypeError, OverflowError):
            reject("ALERT_QUERIED_TIME_MISSING", "queried_at 无效或缺少时区。")
    if raw_issued is not None:
        try:
            alert_issued = _time(raw_issued)
        except (ValueError, TypeError, OverflowError):
            reject("ALERT_ISSUED_TIME_MISSING",
                   "预警产品 issued_at 无效或缺少时区，不得用小时天气 weather.issued_at 冒充。")
    if coverage_status in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED | {"feed_unavailable", "query_incomplete"}:
        if queried_at is None:
            reject("ALERT_QUERIED_TIME_MISSING", "已查询/失败/不完整状态必须保存 queried_at（含时区）。")
    if coverage_status in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        if alert_issued is None and not native_query_valid:
            reject("ALERT_ISSUED_TIME_MISSING",
                   "已查询状态必须保存预警产品 issued_at，不得用小时天气 weather.issued_at 冒充。")
        elif raw_issued is not None and hourly_issued_at == raw_issued:
            warn("alert_feed.issued_at 与 weather.issued_at 字符串相同；请确认前者是预警产品时间。")
    if queried_at is not None and queried_at > now and coverage_status not in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        reject("ALERT_QUERIED_IN_FUTURE", "queried_at 不得晚于 now。")
    if alert_issued is not None and alert_issued > now:
        reject("ALERT_ISSUED_IN_FUTURE", "预警产品 issued_at 不得晚于 now。")
    for code, message in query_issues:
        reject(code, message)
    if not demonstration and coverage_status in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        warn(ALERT_QUERY_FRESHNESS_BASIS)

    items = feed.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        reject("ALERT_ITEMS_INVALID", "items 必须为数组。")
        items = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            reject("ALERT_ITEM_INVALID", f"alert_feed.items[{i}] 必须为对象。")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not _known(item_id):
            reject("ALERT_ITEM_ID_MISSING", f"alert_feed.items[{i}].id 必须为非空字符串。")
            continue
        if item_id in item_by_id:
            reject("ALERT_ITEM_ID_DUPLICATE", f"alert_feed.items[{i}].id 重复。")
        item_by_id[item_id] = item
        try:
            sent = _time(item.get("sent"))
            if sent > now:
                reject("ALERT_ITEM_SENT_IN_FUTURE", f"alert_feed.items[{i}].sent 不得晚于 now。")
        except (ValueError, TypeError, OverflowError):
            reject("ALERT_ITEM_SENT_MISSING", f"alert_feed.items[{i}].sent 必须保存该条发布时间（含时区）。")
        tag = item.get("hazard_tag")
        if not isinstance(tag, str) or not _known(tag):
            reject("ALERT_ITEM_TAG_MISSING", f"alert_feed.items[{i}].hazard_tag 必须为可辨识标记。")
        else:
            item_tags.add(tag)

    if coverage_status in ALERT_COVERAGE_CLEAR and items:
        reject("ALERT_CLEAR_WITH_ITEMS", "queried_clear 的 items 必须为空数组。")
    if coverage_status in ALERT_COVERAGE_MARKED and not items:
        reject("ALERT_MARKED_WITHOUT_ITEMS", "active_alerts 必须有非空 items，不能改写成空列表。")

    state = STATE_UNKNOWN
    if coverage_status in ALERT_COVERAGE_CLEAR:
        state = STATE_UNMARKED
    elif coverage_status in ALERT_COVERAGE_MARKED:
        state = STATE_MARKED
    elif coverage_status == "synthetic_declared":
        state = STATE_MARKED if items else STATE_UNMARKED
        warn("synthetic_declared 只用于演示，不能写成田间已核对。")
    if coverage_status in ALERT_COVERAGE_UNKNOWN:
        state = STATE_UNKNOWN
        warn("未知覆盖只表示未完成官方预警查询，不是田间安全。")
    if query_issues:
        state = STATE_UNKNOWN
    if state == STATE_UNMARKED:
        warn("queried_clear 只表示该次官方预警查询集合为空，不是个人健康许可或田间无危险。")
    return state, item_by_id, item_tags


def apply_record_hazards(record, state, item_by_id, item_tags, blocked, reject) -> None:
    """按三态检查单条记录的 hazards；非空标记仍一律阻断。

    - unknown：必须省略 hazards；写出 [] 视为冒充无预警（ALERT_EMPTY_IMPERSONATION）。
    - unmarked：必须显式写出 hazards=[]；非空或被省略都阻断。
    - marked：hazards 不得为空，且每个标记必须出现在 items[].hazard_tag。
    """
    hazards = record.get("hazards") if "hazards" in record else None
    refs = record.get("hazard_item_ids")
    if refs is None:
        refs = []
    if not isinstance(refs, list):
        reject("ALERT_ITEM_REF_INVALID", "hazard_item_ids 必须为数组。")
        refs = []
    for ref in refs:
        if ref not in item_by_id:
            reject("ALERT_ITEM_REF_UNKNOWN", f"hazard_item_ids 未指向 items[].id：{ref}。")

    known_list = isinstance(hazards, list) and all(isinstance(h, str) and _known(h) for h in hazards)
    if hazards is not None and not isinstance(hazards, list):
        reject("HAZARDS_UNKNOWN", "危险天气状态未知，不能将缺项当无预警。")
        return
    if isinstance(hazards, list) and not known_list:
        reject("HAZARDS_UNKNOWN", "危险天气状态未知，不能将缺项当无预警。")

    if state == STATE_UNKNOWN:
        if hazards == []:
            reject("ALERT_EMPTY_IMPERSONATION", "hazards=[] 但 coverage 不是 queried_clear，不能冒充无预警。")
        elif hazards is None:
            reject("HAZARDS_UNKNOWN", "危险天气状态未知，不能将缺项当无预警。")
        elif known_list and hazards:
            reject("ALERT_HAZARDS_PRESENT_WHEN_UNKNOWN", "未知覆盖下不得带非空 hazards；应先标 active_alerts。")
        return
    if state == STATE_UNMARKED:
        if hazards is None:
            reject("HAZARDS_UNKNOWN", "危险天气状态未知，不能将缺项当无预警。")
            reject("HAZARDS_MISSING_WHEN_CLEAR", "queried_clear 必须显式写出 hazards=[]，不能省略。")
        elif known_list and hazards:
            reject("ALERT_CLEAR_NONEMPTY_HAZARDS", "queried_clear 时 hazards 必须为空。")
        elif not known_list and hazards is not None:
            return
        else:
            return
    elif state == STATE_MARKED:
        if not hazards:
            reject("ALERT_ACTIVE_REWRITTEN_EMPTY", "active_alerts 时 hazards 不得为空或省略。")
            if hazards is None:
                reject("HAZARDS_UNKNOWN", "危险天气状态未知，不能将缺项当无预警。")
            return
        if not known_list:
            return

    if known_list:
        for hazard in sorted(set(hazards)):
            if state == STATE_MARKED and hazard not in item_tags:
                reject("ALERT_TAG_NOT_IN_ITEMS", f"标记 {hazard} 未出现在 items[].hazard_tag。")
            if hazard in blocked:
                reject("BLOCKED_HAZARD", f"作业区间存在被策略禁止的危险天气：{hazard}。")
            else:
                reject("UNREVIEWED_HAZARD", f"作业区间存在尚未纳入策略的危险天气标记：{hazard}，需先审核。")


# --------------------------------------------------------------------------
# 外部提供者适配：把原始响应规范化成内部 alert_feed，并解释性阻断缺适用数据
# --------------------------------------------------------------------------

def _result(provider, applicable, feed, state, reasons, warnings, evidence):
    return {
        "provider": provider,
        "applicable": applicable,
        "feed": feed,
        "state": state,
        "reasons": reasons,
        "warnings": warnings,
        "evidence": evidence,
    }


def _plot_point(plot, reasons):
    lat, lon = _number(plot.get("latitude")), _number(plot.get("longitude"))
    if lat is None or lon is None:
        reasons.append({"code": "ALERT_LOCATION_MISSING",
                        "message": "田块缺少有效的 latitude/longitude，无法定位官方预警查询。"})
    return lat, lon


def _normalize_cap_item(item, index, reasons):
    """把一条 CAP 条目转成内部 items[]；必须自带可辨识 hazard_tag，不本地发明对照。"""
    item_id = item.get("identifier") or item.get("id")
    if not isinstance(item_id, str) or not _known(item_id):
        reasons.append({"code": "ALERT_ITEM_ID_MISSING",
                        "message": f"CAP alerts[{index}] 缺少可辨识 identifier。"})
        return None
    sent = item.get("sent")
    tag = item.get("hazard_tag")
    if not isinstance(tag, str) or not _known(tag):
        reasons.append({"code": "ALERT_TAG_MAPPING_ABSENT",
                        "message": f"CAP alerts[{index}] 只给出 event，缺本地 event→hazard_tag 对照；"
                                   "不发明标记，先阻断。"})
        return None
    normalized = {"id": item_id, "sent": sent, "hazard_tag": tag}
    for key in ("event", "area_desc"):
        if isinstance(item.get(key), str):
            normalized[key] = item[key]
    return normalized


def adapt_provider(provider: str, payload: Any, *, plot: dict, mode: str, now: datetime) -> dict:
    """把一种外部预警提供者的响应转成内部信封或给出解释性阻断。

    返回 dict：provider / applicable / feed / state / reasons / warnings / evidence。
    - applicable=False 表示该提供者没有可用的官方预警通道，或响应本身不完整；
      此时 state 必须为 unknown，reasons 说明缺什么，绝不把“没查到”写成空数组。
    - applicable=True 且 state=unmarked 才表示“已查询且无标记”。
    """
    payload = payload if isinstance(payload, dict) else {}
    now = _time(now)
    demonstration = mode == "demonstration"
    reasons: list[dict] = []
    warnings: list[str] = []

    if provider not in PROVIDER_KINDS and not alert_source_is_nwp(provider):
        reasons.append({"code": "ALERT_PROVIDER_UNKNOWN",
                        "message": f"未知预警提供者 {provider!r}，不能推断其预警完整性。"})
        return _result(provider, False, None, STATE_UNKNOWN, reasons, warnings,
                       "未列入 PROVIDER_KINDS，缺适用数据，解释性阻断。")

    if provider == PROVIDER_HOURLY_NO_ALERT_CHANNEL or (
        provider not in (PROVIDER_CAP_FULL_SNAPSHOT, PROVIDER_NWS_POINT, PROVIDER_NWS_ZONE)
        and alert_source_is_nwp(provider)
    ):
        lat, lon = _plot_point(plot, reasons)
        reasons.append({"code": "ALERT_CHANNEL_ABSENT",
                        "message": "该提供者只有普通小时数值预报，没有官方预警通道；"
                                   "缺预警键不等于该地无预警，不得填 hazards=[]。"})
        feed = {
            "coverage_status": "not_queried",
            "source": None,
            "protocol": None,
            "queried_at": None,
            "issued_at": None,
            "query": {"type": None, "latitude": lat, "longitude": lon},
            "items": [],
        }
        return _result(provider, False, feed, STATE_UNKNOWN, reasons, warnings,
                       "数值预报产品不含官方预警（R04-A03：Open-Meteo/ECMWF 无 alerts API）。")

    if provider == PROVIDER_NOT_QUERIED:
        lat, lon = _plot_point(plot, reasons)
        reasons.append({"code": "ALERT_NOT_QUERIED",
                        "message": "尚未执行官方预警查询；未知状态必须阻断，不能默认空集合。"})
        feed = {"coverage_status": "not_queried", "source": None, "protocol": None,
                "queried_at": None, "issued_at": None,
                "query": {"type": None, "latitude": lat, "longitude": lon}, "items": []}
        return _result(provider, False, feed, STATE_UNKNOWN, reasons, warnings,
                       "调用方显式声明未查询官方预警。")

    if provider == PROVIDER_FEED_UNAVAILABLE:
        queried_at = payload.get("queried_at")
        http_status = payload.get("http_status")
        if queried_at is None:
            reasons.append({"code": "ALERT_QUERIED_TIME_MISSING",
                            "message": "查询失败也必须保存 queried_at，不能只留空结果。"})
        if http_status is not None and (_number(http_status) is None
                                        or not 100 <= _number(http_status) <= 599):
            reasons.append({"code": "ALERT_HTTP_STATUS_INVALID", "message": "http_status 无效。"})
        reasons.append({"code": "ALERT_FEED_UNAVAILABLE",
                        "message": f"官方预警源不可用（HTTP {http_status}）；未知状态必须阻断。"})
        feed = {"coverage_status": "feed_unavailable", "source": payload.get("source"),
                "protocol": None, "queried_at": queried_at, "issued_at": None,
                "query": {"type": payload.get("query_type"),
                          "latitude": _number(plot.get("latitude")),
                          "longitude": _number(plot.get("longitude"))},
                "items": []}
        if isinstance(http_status, int) and not isinstance(http_status, bool):
            feed["http_status"] = http_status
        return _result(provider, False, feed, STATE_UNKNOWN, reasons, warnings,
                       "查询失败/超时属于未知覆盖，不是无预警。")

    if provider == PROVIDER_CAP_FULL_SNAPSHOT:
        authority = payload.get("authority_domain")
        complete = payload.get("snapshot_complete")
        queried_at = payload.get("queried_at")
        issued_at = payload.get("issued_at")
        alerts = payload.get("alerts") or []
        if not _source(authority):
            reasons.append({"code": "ALERT_AUTHORITY_DOMAIN_MISSING",
                            "message": "全量快照必须声明 authority_domain，空域不能当田块已核对。"})
        if complete is not True:
            reasons.append({"code": "ALERT_SNAPSHOT_INCOMPLETE",
                            "message": "snapshot_complete 必须为 true 才能声明 queried_clear。"})
        if not isinstance(alerts, list):
            reasons.append({"code": "ALERT_ITEMS_INVALID", "message": "alerts 必须为数组。"})
            alerts = []
        if not _source(payload.get("source")):
            reasons.append({"code": "ALERT_SOURCE_UNKNOWN", "message": "快照必须给出可辨识预警源。"})
        if _source(payload.get("source")) and alert_source_is_nwp(payload.get("source")):
            reasons.append({"code": "ALERT_SOURCE_IS_NWP",
                            "message": "数值预报/天气代码不能当作官方预警源。"})
        if queried_at is None:
            reasons.append({"code": "ALERT_QUERIED_TIME_MISSING",
                            "message": "已查询状态必须保存 queried_at，不能用缺省推断查询时间。"})
        if issued_at is None:
            reasons.append({"code": "ALERT_ISSUED_TIME_MISSING",
                            "message": "已查询状态必须保存预警产品 issued_at，不得用小时天气时间冒充。"})
        items = []
        for i, item in enumerate(alerts):
            if not isinstance(item, dict):
                reasons.append({"code": "ALERT_ITEM_INVALID", "message": f"alerts[{i}] 必须为对象。"})
                continue
            normalized = _normalize_cap_item(item, i, reasons)
            if normalized:
                items.append(normalized)
        coverage = "active_alerts" if items else "queried_clear"
        if reasons and coverage == "queried_clear":
            # 缺 authority/完整声明/来源时不得声明“无标记”，退化为未知。
            coverage = "query_incomplete"
        feed = {"coverage_status": coverage, "source": payload.get("source"),
                "protocol": "CAP-1.2" if _source(payload.get("source")) else None,
                "queried_at": queried_at, "issued_at": issued_at,
                "query": {"type": "full_snapshot", "authority_domain": authority,
                          "snapshot_complete": complete}, "items": items}
        applicable = coverage != "query_incomplete" and not reasons
        state = state_for_coverage(coverage, bool(items))
        return _result(provider, applicable, feed, state, reasons, warnings,
                       "DWD/登记当局 CAP 全量快照：空结果=无活动预警仅在完整查询时成立。")

    if provider == PROVIDER_NWS_POINT:
        features = payload.get("features")
        lat, lon = _plot_point(plot, reasons)
        q_lat, q_lon = _number(payload.get("latitude")), _number(payload.get("longitude"))
        if q_lat is None or q_lon is None:
            reasons.append({"code": "ALERT_LOCATION_MISSING", "message": "point 查询必须给出查询坐标。"})
        elif lat is not None and (q_lat != lat or q_lon != lon):
            reasons.append({"code": "ALERT_QUERY_POINT_NEQ_PLOT",
                            "message": "point 查询坐标必须等于 plot，不得改写成预报格点。"})
        if not isinstance(features, list):
            reasons.append({"code": "ALERT_ITEMS_INVALID", "message": "features 必须为数组。"})
            features = []
        items = []
        for i, feature in enumerate(features):
            if not isinstance(feature, dict):
                reasons.append({"code": "ALERT_ITEM_INVALID", "message": f"features[{i}] 必须为对象。"})
                continue
            props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            normalized = _normalize_cap_item(
                {"identifier": feature.get("id") or props.get("id"),
                 "sent": props.get("sent"), "hazard_tag": feature.get("hazard_tag") or props.get("hazard_tag"),
                 "event": props.get("event"), "area_desc": props.get("areaDesc")},
                i, reasons)
            if normalized:
                items.append(normalized)
        if payload.get("queried_at") is None:
            reasons.append({"code": "ALERT_QUERIED_TIME_MISSING",
                            "message": "NWS point 查询必须保存 queried_at。"})
        if payload.get("issued_at") is None:
            reasons.append({"code": "ALERT_ISSUED_TIME_MISSING",
                            "message": "NWS point 查询必须保存预警产品 issued_at，不得用小时天气时间冒充。"})
        coverage = "active_alerts" if items else "queried_clear"
        if reasons and coverage == "queried_clear":
            coverage = "query_incomplete"
        feed = {"coverage_status": coverage,
                "source": payload.get("source") or "NWS API alerts (point)",
                "protocol": "NWS-API-alerts",
                "queried_at": payload.get("queried_at"), "issued_at": payload.get("issued_at"),
                "query": {"type": "point", "latitude": q_lat, "longitude": q_lon}, "items": items}
        applicable = coverage != "query_incomplete" and not reasons
        state = state_for_coverage(coverage, bool(items))
        return _result(provider, applicable, feed, state, reasons, warnings,
                       "NWS /alerts/active?point= 是按点查询；空 features 只能在该点查询成立时算无标记。")

    # PROVIDER_NWS_ZONE
    lat, lon = _plot_point(plot, reasons)
    reasons.append({"code": "ALERT_ZONE_QUERY_NOT_CLEAR",
                    "message": "仅按 forecast zone 查询会漏掉县/多边形短时预警，空结果不得当无标记。"})
    feed = {"coverage_status": "query_incomplete", "source": payload.get("source") or "NWS API alerts (zone)",
            "protocol": "NWS-API-alerts", "queried_at": payload.get("queried_at"), "issued_at": None,
            "query": {"type": "zone", "spatial_id": payload.get("zone_id"),
                      "latitude": lat, "longitude": lon}, "items": []}
    return _result(provider, False, feed, STATE_UNKNOWN, reasons, warnings,
                   "NWS 按 zone 查询不覆盖县/多边形短时预警，属查询不完整。")
