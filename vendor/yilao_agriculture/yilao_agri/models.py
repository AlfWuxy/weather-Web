"""输入契约：保留未知状态，拒绝含糊单位、无时区时间和无效引用。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


class InputError(ValueError):
    """错误消息包含输入字段路径。"""


UNITS = frozenset({"mu", "sqm", "kg", "trip", "plant", "m", "m3"})
REVIEW_STATES = {"confirmed", "illustrative", "unknown"}
CANONICALIZATION_VERSION = "yilao.request.canonical.v1"
_ID_COLLECTIONS = frozenset({"plots", "workers", "resources", "tasks"})
_SET_LIKE_KEYS = frozenset({
    "depends_on", "required_resources", "tags", "forbidden_tags",
    "blocked_hazards", "required_resource_kinds", "hazards",
})
_JSON_INT_LIMIT = 2 ** 53
CROP_ALIAS_EVIDENCE = {
    "SYNTHETIC", "IMPLEMENTATION_TEST", "SECONDARY_SUMMARY",
    "PRIMARY_SOURCE", "FIELD_OBSERVATION", "PROFESSIONAL_REVIEW",
}
_CROP_ALIAS_MOD = None


def _crop_alias_mod():
    """加载 data/crops/resolve.py；失败时给出字段级错误而不是静默跳过。"""
    global _CROP_ALIAS_MOD
    if _CROP_ALIAS_MOD is None:
        path = Path(__file__).resolve().parents[1] / "data" / "crops" / "resolve.py"
        spec = importlib.util.spec_from_file_location("yilao_data_crops_resolve", path)
        if spec is None or spec.loader is None:
            _fail("tasks.crop_alias", "无法加载作物别名解析器")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CROP_ALIAS_MOD = module
    return _CROP_ALIAS_MOD


def _apply_crop_alias(task, path, plot):
    """可选地方别名入口：多候选保留，不改写 crop_id，歧义不自动选定。"""
    if "crop_alias" not in task:
        return
    alias = task["crop_alias"]
    alias_path = f"{path}.crop_alias"
    _object(alias, alias_path, {"surface_form", "region_id", "confirmation"})
    _text(alias.get("surface_form"), f"{alias_path}.surface_form")
    if "region_id" in alias:
        _text(alias["region_id"], f"{alias_path}.region_id")
        if alias["region_id"] != plot["region_id"]:
            _fail(f"{alias_path}.region_id", "ALIAS_REGION_MISMATCH: 别名地区必须与田块 region_id 一致")
    else:
        alias["region_id"] = plot["region_id"]
    if "confirmation" in alias:
        conf = alias["confirmation"]
        _object(conf, f"{alias_path}.confirmation",
                {"confirmed", "crop_id", "by", "at", "evidence_type", "note"})
        _bool(conf.get("confirmed"), f"{alias_path}.confirmation.confirmed")
        if conf["confirmed"]:
            _text(conf.get("crop_id"), f"{alias_path}.confirmation.crop_id")
            _text(conf.get("by"), f"{alias_path}.confirmation.by")
            _time(conf.get("at"), f"{alias_path}.confirmation.at")
            _enum(conf.get("evidence_type"), CROP_ALIAS_EVIDENCE,
                  f"{alias_path}.confirmation.evidence_type")
            if "note" in conf:
                _text(conf["note"], f"{alias_path}.confirmation.note", empty=True)
    query = {"surface_form": alias["surface_form"], "region_id": alias["region_id"]}
    if "confirmation" in alias:
        query["confirmation"] = alias["confirmation"]
    module = _crop_alias_mod()
    try:
        resolution = module.resolve_crop_alias(query)
    except module.CropAliasError as exc:
        suffix = f".{exc.field}" if exc.field else ""
        _fail(f"{alias_path}{suffix}", f"{exc.code}: {exc.message}")
    if "crop_alias_resolution" in task and task["crop_alias_resolution"] != resolution:
        _fail(f"{path}.crop_alias_resolution", "派生别名结论与重新解析结果不一致，不接受输入自报选定")
    task["crop_alias_resolution"] = resolution
    # 解析结果不得回写 crop_id，避免把多候选锁成单一物种。

SOURCE_PLACEHOLDERS = frozenset({
    "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未确认", "未知", "待定", "待核实", "待补充", "待填写",
})

def is_placeholder_source(value) -> bool:
    if not isinstance(value, str):
        return False
    text = " ".join(value.split()).casefold()
    return not text or text in SOURCE_PLACEHOLDERS


def classify_deadline_basis(task) -> dict:
    """把 tasks[].deadline_basis 归一化成可审查的派生结论（读取，不修改输入）。

    只有「可农艺归因的 kind 且 review_status=confirmed」才构成农艺硬期限；
    kind=habit 永远是个人偏好，不因来源文本或审核状态被升格成农艺依据；
    缺省与 unknown 一律按未知处理，不自动补成农艺或习惯。
    改期条件只透传输入声明，程序不认证其农艺正确性。
    """
    basis = task.get("deadline_basis") if isinstance(task, dict) else None
    if not isinstance(basis, dict):
        return {
            "present": False, "kind": "unknown", "label": "未提供截止理由",
            "personal_preference": False, "agronomic": False,
            "agronomic_hard_deadline": False, "review_status": "unknown",
            "delay_allowed": False, "delay_conditions": [],
            "requires_agronomic_confirmation": False,
            "note": "未提供 deadline_basis；不得据此声称农艺期限或允许改期。",
        }
    kind = basis.get("kind")
    review = basis.get("review_status", "unknown")
    delay = basis.get("delay") if isinstance(basis.get("delay"), dict) else {}
    conditions = delay.get("conditions") if isinstance(delay.get("conditions"), list) else []
    agronomic = kind in DEADLINE_AGRONOMIC_KINDS and review == "confirmed"
    personal = kind == "habit"
    if personal:
        note = "个人习惯时间不是农艺硬期限；程序不把它改写成农艺依据。"
    elif agronomic:
        note = "已确认农艺依据，构成农艺硬期限；仍不得越过身体或天气硬约束。"
    else:
        note = "依据未经确认，只是个人或来源声明，不构成农艺硬期限。"
    return {
        "present": True, "kind": kind, "label": DEADLINE_BASIS_LABELS.get(kind, "未知"),
        "personal_preference": personal, "agronomic": agronomic,
        "agronomic_hard_deadline": agronomic, "review_status": review,
        "delay_allowed": delay.get("allowed", False),
        "delay_conditions": list(conditions),
        "requires_agronomic_confirmation": bool(delay.get("requires_agronomic_confirmation", False)),
        "note": note,
    }


def same_task_parallel_allowed(task, mode="demonstration") -> bool:
    """缺省串行。只有显式 allowed 且本模式可采用的审核状态、非占位来源时才允许同任务时间重叠。

    不把人数折成团队速率，也不把某人速率抄给未提供依据的帮手。
    """
    if not isinstance(task, dict):
        return False
    spec = task.get("parallel_within_task")
    if not isinstance(spec, dict) or spec.get("allowed") is not True:
        return False
    accepted = {"confirmed", "illustrative"} if mode == "demonstration" else {"confirmed"}
    if spec.get("review_status") not in accepted:
        return False
    source = spec.get("source")
    return isinstance(source, str) and not is_placeholder_source(source)


# R14-A04：截止理由（依据）与可改期条件。五类主因来自 R08-A10 访谈协议，另加 unknown。
# 习惯是个人例行时间，不是已确认农艺；程序不把它改写成农艺硬期限。
DEADLINE_BASIS_KINDS = frozenset({
    "maturity", "rain", "water_supply", "machinery", "habit", "unknown",
})
DEADLINE_AGRONOMIC_KINDS = frozenset({"maturity", "rain", "water_supply", "machinery"})
DEADLINE_BASIS_LABELS = {
    "maturity": "成熟标志",
    "rain": "雨水与宜耕",
    "water_supply": "供水到田",
    "machinery": "农机预约",
    "habit": "个人习惯（非农艺）",
    "unknown": "未知",
}
# 可改期条件只接受这三态；字符串只允许 "conditional"，避免把未知写成允许。
DEADLINE_DELAY_OPTIONS = (True, False, "conditional")

AREA_UNITS = frozenset({"mu", "sqm"})
INTEGER_UNITS = frozenset({"trip", "plant"})
REVIEW_STATES = {"confirmed", "illustrative", "unknown"}
# 资源占用口径（R15-A03）：默认整段保留（保守）；仅休息处可声明按阶段细分。
RESOURCE_OCCUPATION_MODES = frozenset({"full_session", "declared_stages"})
RESOURCE_OCCUPATION_STAGES = frozenset({"rest"})
# 与 R10-A03 冻结契约 yilao.workload.r10a03.v1.1 对齐；只按 operation/method 判别。
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


def _fail(path, message, code=None):
    text = f"{path}: {message}" if not code else f"{path}: {code} {message}"
    raise InputError(text, path=path, code=code)


def parse_time(value) -> datetime:
    """只接受显式带 UTC 偏移的 ISO 时间，不猜测本地时区。"""
    if not isinstance(value, str) or not value.strip():
        _fail("time", "必须为含时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        _fail("time", "无效的 ISO 8601 时间")
    if result.tzinfo is None or result.utcoffset() is None:
        _fail("time", "必须包含时区偏移")
    return result


def to_utc(value) -> datetime:
    """将带时区的瞬时转到 UTC。时长比较必须用此结果，避免同一 ZoneInfo 按墙钟相减。"""
    if isinstance(value, datetime):
        result = value
    else:
        result = parse_time(value)
    if result.tzinfo is None or result.utcoffset() is None:
        _fail("time", "必须包含时区偏移")
    try:
        return result.astimezone(UTC)
    except (OverflowError, ValueError, OSError):
        _fail("time", "无法转换到 UTC")


def resolve_timezone(value, path="timezone") -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    if not isinstance(value, str) or not value.strip():
        _fail(path, "必须为非空字符串")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _fail(path, "未知 IANA 时区")


def local_civil_date(instant, zone) -> date:
    """请求 IANA 时区下的民用日期，不是 UTC 日期。"""
    return to_utc(instant).astimezone(resolve_timezone(zone)).date()


def local_day_start_utc(day, zone) -> datetime:
    """当地民用日 00:00 对应的 UTC 瞬时。"""
    zone = resolve_timezone(zone)
    if isinstance(day, datetime):
        _fail("time", "民用日期不得带时钟")
    if isinstance(day, str):
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != day:
            _fail("time", "日期必须为 YYYY-MM-DD")
        day = parsed
    if not isinstance(day, date):
        _fail("time", "日期必须为 YYYY-MM-DD")
    local = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    try:
        utc = local.astimezone(UTC)
    except (OverflowError, ValueError, OSError):
        _fail("time", "无法确定当地日界")
    if utc.astimezone(zone).date() != day:
        _fail("time", "当地 00:00 不落在该民用日")
    return utc


def utc_minutes(start, end) -> float:
    """半开区间时长（分钟），在 UTC 上计算。"""
    return (to_utc(end) - to_utc(start)).total_seconds() / 60


def iter_local_date_spans(start, end, zone):
    """按当地民用日切开 [start, end)，分钟数用 UTC 差值。"""
    zone = resolve_timezone(zone)
    cursor, finish = to_utc(start), to_utc(end)
    if finish < cursor:
        _fail("time", "end 必须晚于 start")
    steps = 0
    while cursor < finish:
        steps += 1
        if steps > 32:
            _fail("time", "当地日期切分超过上限")
        day = cursor.astimezone(zone).date()
        boundary = local_day_start_utc(day + timedelta(days=1), zone)
        if boundary <= cursor:
            _fail("time", "当地日期边界未能前进")
        edge = min(finish, boundary)
        yield day.isoformat(), utc_minutes(cursor, edge)
        cursor = edge


def _time(value, path):
    try:
        return parse_time(value)
    except InputError as exc:
        _fail(path, str(exc).removeprefix("time: "))


def _object(value, path, allowed):
    if not isinstance(value, dict):
        _fail(path, "必须为对象")
    for key in value:
        if key not in allowed:
            _fail(f"{path}.{key}", "未知字段")
    return value


def _list(value, path):
    if not isinstance(value, list):
        _fail(path, "必须为数组")
    return value


def _text(value, path, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail(path, "必须为非空字符串" if not empty else "必须为字符串")
    return value


def _number(value, path, minimum=None, maximum=None, integer=False, positive=False,
            code=None, integer_code=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "必须为有限数值", code)
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        _fail(path, "必须为有限数值", code)
    if integer and int(value) != value:
        _fail(path, "必须为整数", integer_code or code)
    if positive and value <= 0:
        _fail(path, "必须大于零", code)
    if minimum is not None and value < minimum:
        _fail(path, f"不得小于 {minimum}", code)
    if maximum is not None and value > maximum:
        _fail(path, f"不得大于 {maximum}", code)
    return value


def _enum(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        _fail(path, f"须为 {' / '.join(sorted(choices))}")


def _unit(value, path):
    """只接受 schema 1.0 词表；空值与别名一律拒绝，不猜测亩或公斤。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        _fail(path, "缺少或空单位，不猜测亩或公斤", "MISSING_UNIT")
    if not isinstance(value, str) or value not in UNITS:
        shown = value if isinstance(value, str) else type(value).__name__
        _fail(path, f"不支持的工作量单位 {shown!r}，不静默换算", "INVALID_UNIT")
    return value


def _has_op_token(text, tokens):
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


def _infer_quantity_family(task):
    """按冻结词表判别数量族；无法判别时返回 None，禁止猜测 kg 含义。"""
    blob = f"{task.get('operation', '')} {task.get('method', '')}"
    operation = str(task.get("operation") or "").strip().casefold()
    harvest = _has_op_token(blob, HARVEST_TOKENS) or operation in {"pick", "harvest"}
    transport = _has_op_token(blob, TRANSPORT_TOKENS)
    if harvest and transport:
        return "merge_harvest_and_carry"
    if harvest:
        return "mature_batch"
    if transport:
        return "remaining_trips"
    if _has_op_token(blob, DRY_TOKENS):
        return "remaining_mass"
    if _has_op_token(blob, PLANT_TOKENS):
        return "remaining_plants"
    if _has_op_token(blob, IRRIGATION_TOKENS):
        return "remaining_volume"
    if _has_op_token(blob, AREA_TOKENS):
        return "remaining_area"
    return None


def _check_remaining_family(task, path):
    unit = task["remaining_quantity"]["unit"]
    family = _infer_quantity_family(task)
    unit_path = f"{path}.remaining_quantity.unit"
    if family == "merge_harvest_and_carry":
        _fail(f"{path}.remaining_quantity",
              "采收与搬运不得合成一条 remaining，不猜测该用 kg 还是 trip",
              "MERGE_HARVEST_AND_CARRY")
    if family:
        allowed = FAMILY_UNITS[family]
        if unit not in allowed:
            if family == "mature_batch" and unit in AREA_UNITS:
                _fail(unit_path, "采收 remaining 不得为亩或平方米，不用面积乘统一产量",
                      "HARVEST_AREA_AS_REMAINING")
            if family == "remaining_trips" and unit in AREA_UNITS:
                _fail(unit_path, "搬运 remaining 不得为面积，不把亩静默当成趟数",
                      "TRANSPORT_AREA_AS_REMAINING")
            if family == "remaining_trips" and unit == "kg":
                _fail(unit_path,
                      "搬运 remaining 必须是 trip；即使有 load_per_trip_kg 也不在校验层换算趟数",
                      "UNIT_WEIGHT_NOT_TRIP")
            _fail(unit_path, f"单位 {unit} 不属于数量族 {family}，不静默换算",
                  "FORBIDDEN_UNIT_FOR_FAMILY")
    elif unit == "kg":
        _fail(unit_path, "未知操作的 kg 不能猜是采收、晾晒还是未换趟的搬运",
              "MISSING_QUANTITY_FAMILY")


ALERT_COVERAGE_UNKNOWN = frozenset({"not_queried", "feed_unavailable", "query_incomplete"})
ALERT_COVERAGE_CLEAR = frozenset({"queried_clear"})
ALERT_COVERAGE_MARKED = frozenset({"active_alerts"})
ALERT_COVERAGE_ALL = (
    ALERT_COVERAGE_UNKNOWN | ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED | {"synthetic_declared"}
)
ALERT_PROTOCOLS = frozenset({"CAP-1.2", "NWS-API-alerts", "official_equivalent"})
# 本版工程保守假设，不是官方预警有效期、个人健康阈值或医学依据。
# 只限制最近一次真实查询的年龄；持续有效事件可以具有较早的签发时间。
ALERT_QUERY_MAX_AGE_MINUTES = 60
ALERT_QUERY_FRESHNESS_BASIS = "预警查询须在60分钟内；这是本版工程保守假设，不代表预警有效期或个人安全。"


def alert_query_issues(feed, now, *, demonstration=False):
    """共享查询时效与HTTP矛盾检查；不改写缓存时间，不认证提供方身份。"""
    if not isinstance(feed, dict):
        return []
    issues = []
    if "query_snapshot" in feed:
        # 即使覆盖状态被改成未知或演示，也必须从原响应核对，不能借状态绕过快照校验。
        from .qweather_alert_snapshot import validate_qweather_snapshot
        try:
            validate_qweather_snapshot(feed, now)
        except ValueError:
            issues.append(("ALERT_QUERY_SNAPSHOT_INVALID", "预警原生查询快照与原响应、地点、状态或时间不一致，请重新查询；快照校验不认证来源身份。"))
    coverage = feed.get("coverage_status")
    if not isinstance(coverage, str) or coverage not in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        return issues
    status = feed.get("http_status")
    if status is not None and (type(status) is not int or not 200 <= status < 300):
        issues.append(("ALERT_HTTP_STATUS_CONFLICT", "预警查询HTTP响应并非成功，不能声明已核对无预警或已有完整预警；请重新查询。"))
    try:
        queried = parse_time(feed.get("queried_at"))
    except (ValueError, TypeError, OverflowError):
        issues.append(("ALERT_QUERIED_TIME_MISSING", "缺少有效的预警查询时刻，不能把下载或缓存取用时刻当作新查询。"))
        return issues
    age = (now - queried).total_seconds() / 60
    if age < 0:
        issues.append(("ALERT_QUERIED_IN_FUTURE", "预警查询时刻晚于当前时间，请重新核对。"))
    elif not demonstration and age > ALERT_QUERY_MAX_AGE_MINUTES:
        issues.append(("ALERT_QUERY_STALE", "预警查询已超过60分钟，请重新核对当地官方预警；60分钟为本版工程保守假设，不是事件有效期。"))
    return issues


ALERT_CLEAR_QUERY_TYPES = frozenset({"point", "county", "full_snapshot"})
ALERT_QUERY_TYPES = ALERT_CLEAR_QUERY_TYPES | {"admin", "zone"}
# 网格/站点声明类型；region 只声明区域，不能当作田块级代表点。
GRID_KINDS = frozenset({"point", "grid_cell", "station", "region"})
GRID_ENVIRONMENTS = frozenset({"outdoor", "greenhouse"})
NWP_ALERT_SOURCE_TOKENS = (
    "open-meteo", "open_meteo", "ecmwf", "ifs025", "ifs 0.25", "gfs", "era5",
    "weather_code", "wmo weather code", "grapes", "nwp", "icon-eu", "icon_global",
)


def _fail(path, message, code=None):
    text = f"{path}: {message}" if not code else f"{path}: {code} {message}"
    raise InputError(text)


def parse_time(value) -> datetime:
    """只接受显式带 UTC 偏移的 ISO 时间，不猜测本地时区。"""
    if not isinstance(value, str) or not value.strip():
        _fail("time", "必须为含时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        _fail("time", "无效的 ISO 8601 时间")
    if result.tzinfo is None or result.utcoffset() is None:
        _fail("time", "必须包含时区偏移")
    return result


def _time(value, path):
    try:
        return parse_time(value)
    except InputError as exc:
        _fail(path, str(exc).removeprefix("time: "))


def _object(value, path, allowed):
    if not isinstance(value, dict):
        _fail(path, "必须为对象")
    for key in value:
        if key not in allowed:
            _fail(f"{path}.{key}", "未知字段")
    return value


def _list(value, path):
    if not isinstance(value, list):
        _fail(path, "必须为数组")
    return value


def _text(value, path, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail(path, "必须为非空字符串" if not empty else "必须为字符串")
    return value


def _number(value, path, minimum=None, maximum=None, integer=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "必须为有限数值")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        _fail(path, "必须为有限数值")
    if integer and int(value) != value:
        _fail(path, "必须为整数")
    if positive and value <= 0:
        _fail(path, "必须大于零")
    if minimum is not None and value < minimum:
        _fail(path, f"不得小于 {minimum}")
    if maximum is not None and value > maximum:
        _fail(path, f"不得大于 {maximum}")
    return value


def _enum(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        _fail(path, f"须为 {' / '.join(sorted(choices))}")


def _bool(value, path):
    if not isinstance(value, bool):
        _fail(path, "必须为布尔值")


def _strings(value, path):
    _list(value, path)
    seen = set()
    for i, entry in enumerate(value):
        _text(entry, f"{path}[{i}]")
        if entry in seen:
            _fail(f"{path}[{i}]", "重复值")
        seen.add(entry)


def _intervals(value, path):
    _list(value, path)
    for i, entry in enumerate(value):
        item_path = f"{path}[{i}]"
        _object(entry, item_path, {"start", "end"})
        _interval(entry, item_path)
    value.sort(key=lambda entry: parse_time(entry["start"]))
    _nonoverlap(value, path)


def _interval(entry, path):
    start = _time(entry.get("start"), f"{path}.start")
    end = _time(entry.get("end"), f"{path}.end")
    if end <= start:
        _fail(path, "end 必须晚于 start")


def _nonoverlap(records, path):
    for previous, current in zip(records, records[1:]):
        if parse_time(current["start"]) < parse_time(previous["end"]):
            _fail(path, "时间区间不得重叠；相邻半开区间允许")


def _conditions(value, path):
    if not isinstance(value, dict):
        _fail(path, "必须为对象")
    for key, entry in value.items():
        _text(key, path)
        if isinstance(entry, (dict, list)) or entry is None:
            _fail(f"{path}.{key}", "条件须为字符串、布尔值或有限数值")
        if isinstance(entry, (int, float)) and not isinstance(entry, bool):
            _number(entry, f"{path}.{key}")
        elif not isinstance(entry, (str, bool)):
            _fail(f"{path}.{key}", "无效条件值")


def _weather_limits(value, path, policy=False):
    allowed = {"max_precipitation_mm", "max_wind_m_s", "max_temperature_c"}
    allowed.add("max_wbgt_c" if policy else "min_temperature_c")
    _object(value, path, allowed)
    for key, entry in value.items():
        if key.endswith("temperature_c") or key == "max_wbgt_c":
            _number(entry, f"{path}.{key}", -100, 100)
        else:
            _number(entry, f"{path}.{key}", 0)
    if ("min_temperature_c" in value and "max_temperature_c" in value
            and value["min_temperature_c"] > value["max_temperature_c"]):
        _fail(path, "最低温限制不得大于最高温限制")


def _review(value, path, field="review_status"):
    value.setdefault(field, "unknown")
    value.setdefault("source", "")
    _enum(value[field], REVIEW_STATES, f"{path}.{field}")
    _text(value["source"], f"{path}.source", empty=value[field] == "unknown")


def _deadline_basis(task, path, mode):
    """校验可选截止理由块；缺省不改行为，也不自动补写字段。

    习惯/未知不得被声明为农艺期限；非演示模式不得用未知或未确认依据排程。
    """
    if "deadline_basis" not in task:
        return
    block = f"{path}.deadline_basis"
    basis = task["deadline_basis"]
    _object(basis, block, {"kind", "text", "source", "review_status",
                           "claimed_agronomic", "delay"})
    kind = basis.get("kind")
    if not isinstance(kind, str) or kind not in DEADLINE_BASIS_KINDS:
        _fail(f"{block}.kind",
              "须为 maturity / rain / water_supply / machinery / habit / unknown",
              "DEADLINE_BASIS_KIND_UNKNOWN")
    _text(basis.get("text"), f"{block}.text")
    _text(basis.get("source"), f"{block}.source")
    if is_placeholder_source(basis.get("source")):
        _fail(f"{block}.source",
              "截止理由来源不得为占位词；确实未知应显式 kind=unknown",
              "DEADLINE_BASIS_SOURCE_PLACEHOLDER")
    if "review_status" in basis:
        _enum(basis["review_status"], REVIEW_STATES, f"{block}.review_status")
    review = basis.get("review_status", "unknown")
    claimed = basis.get("claimed_agronomic", False)
    _bool(claimed, f"{block}.claimed_agronomic")
    if claimed:
        if kind not in DEADLINE_AGRONOMIC_KINDS:
            _fail(f"{block}.claimed_agronomic",
                  "习惯或未知理由不得声明为农艺期限；习惯不是已核实农艺",
                  "DEADLINE_BASIS_NOT_AGRONOMIC")
        if review != "confirmed":
            _fail(f"{block}.claimed_agronomic",
                  "声明农艺期限需要 review_status=confirmed 的确认记录",
                  "DEADLINE_BASIS_NOT_CONFIRMED")
    if mode != "demonstration":
        if kind == "unknown":
            _fail(f"{block}.kind",
                  "非演示模式不得以未知理由的截止进入安排",
                  "DEADLINE_BASIS_UNKNOWN")
        if kind in DEADLINE_AGRONOMIC_KINDS and review != "confirmed":
            _fail(f"{block}.review_status",
                  "非演示模式不得以未确认的农艺截止理由进入安排",
                  "DEADLINE_BASIS_NOT_ADOPTABLE")
    if "delay" in basis:
        delay_path = f"{block}.delay"
        delay = basis["delay"]
        _object(delay, delay_path, {"allowed", "conditions", "requires_agronomic_confirmation"})
        allowed = delay.get("allowed", False)
        if not (isinstance(allowed, bool) or allowed == "conditional"):
            _fail(f"{delay_path}.allowed", "须为 true / false / conditional",
                  "DELAY_ALLOWED_INVALID")
        conditions = delay.get("conditions", [])
        _strings(conditions, f"{delay_path}.conditions")
        if allowed == "conditional" and not conditions:
            _fail(f"{delay_path}.conditions",
                  "有条件改期必须给出具体条件，不能留空",
                  "DELAY_CONDITIONS_MISSING")
        if allowed is False and conditions:
            _fail(f"{delay_path}.conditions",
                  "不允许改期时不得列出改期条件",
                  "DELAY_CONDITIONS_NOT_APPLICABLE")
        if "requires_agronomic_confirmation" in delay:
            _bool(delay["requires_agronomic_confirmation"],
                  f"{delay_path}.requires_agronomic_confirmation")


HIGH_RISK_OPERATION_CLASSES = frozenset({"pesticide_application", "manure_handling"})


def _operation_scope(value, path):
    """高风险操作的显式审核范围（R14-A09）；只开放声明的类别，不生成剂量。"""
    _object(value, path, {"classes", "review_status", "source", "scope_note"})
    classes = value.get("classes")
    if not isinstance(classes, list) or not classes:
        _fail(f"{path}.classes", "审核范围必须列出所覆盖的高风险作业类别")
    for i, entry in enumerate(classes):
        _enum(entry, HIGH_RISK_OPERATION_CLASSES, f"{path}.classes[{i}]")
    if len(set(classes)) != len(classes):
        _fail(f"{path}.classes", "重复的作业类别")
    _review(value, path)
    if "scope_note" in value:
        _text(value["scope_note"], f"{path}.scope_note", empty=True)


def _index(items, path):
    result = {}
    first = {}
    for i, entry in enumerate(items):
        if not isinstance(entry, dict):
            _fail(f"{path}[{i}]", "必须为对象")
        item_id = _text(entry.get("id"), f"{path}[{i}].id")
        if item_id in result:
            _fail(f"{path}[{i}].id", f"重复 ID，已见于 {path}[{first[item_id]}].id")
        result[item_id] = entry
        first[item_id] = i
    return result


def _one_cycle(graph):
    """在 Kahn 剩余图中找一条环；邻居按 id 排序，保证报错文本确定。"""
    visiting = set()
    done = set()
    stack = []

    def dfs(node):
        if node in visiting:
            return stack[stack.index(node):]
        if node in done:
            return None
        visiting.add(node)
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in graph:
                continue
            found = dfs(nxt)
            if found is not None:
                return found
        stack.pop()
        visiting.remove(node)
        done.add(node)
        return None

    for start in sorted(graph):
        found = dfs(start)
        if found:
            return found
    return sorted(graph)


def _reject_cycles(tasks):
    """拓扑消除避免深依赖链递归；无就绪节点则存在环。"""
    pending = {key: set(task["depends_on"]) for key, task in tasks.items()}
    while pending:
        ready = {key for key, dependencies in pending.items() if not dependencies}
        if not ready:
            cycle = _one_cycle(pending)
            trail = "→".join(list(cycle) + [cycle[0]]) if cycle else ""
            _fail("tasks.depends_on", "依赖存在循环" + (f"：{trail}" if trail else ""))
        pending = {key: dependencies - ready for key, dependencies in pending.items() if key not in ready}


def alert_source_is_nwp(source) -> bool:
    """数值预报、再分析或天气代码名称不能充当官方预警源。"""
    if not isinstance(source, str):
        return False
    text = source.strip().lower()
    return any(token in text for token in NWP_ALERT_SOURCE_TOKENS)


def _optional_time(value, path):
    if value is None:
        return None
    return _time(value, path)


def _validate_alert_feed(feed, path, plot, mode, now):
    """校验预警信封；缺省 coverage 不能变成 queried_clear。返回派生三态与 items 索引。"""
    _object(feed, path, {"source", "protocol", "queried_at", "issued_at", "coverage_status",
                         "query", "items", "http_status", "query_snapshot", "attributions"})
    demonstration = mode == "demonstration"
    for code, message in alert_query_issues(feed, now, demonstration=demonstration):
        _fail(path, f"{code}：{message}")
    if "attributions" in feed:
        _strings(feed["attributions"], f"{path}.attributions")
    coverage = feed.get("coverage_status")
    if coverage is not None and (not isinstance(coverage, str) or coverage not in ALERT_COVERAGE_ALL):
        _fail(f"{path}.coverage_status",
              "ALERT_COVERAGE_STATUS_UNKNOWN：须为 not_queried / feed_unavailable / "
              "query_incomplete / queried_clear / active_alerts / synthetic_declared")
    if coverage == "synthetic_declared" and not demonstration:
        _fail(f"{path}.coverage_status",
              "SYNTHETIC_ALERT_FORBIDDEN：synthetic_declared 只能用于 demonstration")

    source = feed.get("source")
    protocol = feed.get("protocol")
    if coverage in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        if not isinstance(source, str) or not source.strip():
            _fail(f"{path}.source", "已核对或已有标记时必须给出可辨识预警源")
        if alert_source_is_nwp(source):
            _fail(f"{path}.source", "ALERT_SOURCE_IS_NWP：数值预报/天气代码/再分析名称不能当作官方预警源")
        if protocol not in ALERT_PROTOCOLS:
            _fail(f"{path}.protocol",
                  "ALERT_PROTOCOL_UNKNOWN：须为 CAP-1.2 / NWS-API-alerts / official_equivalent")
    else:
        if alert_source_is_nwp(source):
            _fail(f"{path}.source", "ALERT_SOURCE_IS_NWP：即使声明未知，也不得把 NWP 源标成预警源")
        if protocol is not None and protocol not in ALERT_PROTOCOLS:
            _fail(f"{path}.protocol",
                  "ALERT_PROTOCOL_UNKNOWN：须为 CAP-1.2 / NWS-API-alerts / official_equivalent")

    if "http_status" in feed and feed["http_status"] is not None:
        _number(feed["http_status"], f"{path}.http_status", 100, 599, integer=True)

    query = feed.get("query")
    if not isinstance(query, dict):
        _fail(f"{path}.query", "ALERT_QUERY_MISSING：必须保存查询类型与地点")
    _object(query, f"{path}.query", {
        "type", "latitude", "longitude", "spatial_id", "spatial_id_system",
        "snapshot_complete", "authority_domain", "official_cap_url",
    })
    q_type = query.get("type")
    if q_type is not None:
        _enum(q_type, ALERT_QUERY_TYPES, f"{path}.query.type")
    for key, minimum, maximum in (("latitude", -90, 90), ("longitude", -180, 180)):
        if query.get(key) is not None:
            _number(query[key], f"{path}.query.{key}", minimum, maximum)
    for key in ("spatial_id", "spatial_id_system", "authority_domain", "official_cap_url"):
        if query.get(key) is not None:
            _text(query[key], f"{path}.query.{key}")

    if q_type == "zone" and coverage in ALERT_COVERAGE_CLEAR:
        _fail(f"{path}.query.type", "ALERT_ZONE_QUERY_NOT_CLEAR：zone 空结果不得标 queried_clear")

    if coverage in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED:
        if q_type not in ALERT_CLEAR_QUERY_TYPES:
            _fail(f"{path}.query.type",
                  "ALERT_QUERY_TYPE_INCOMPLETE：已核对/已有标记须为 point、county 或 full_snapshot")
        if q_type == "point":
            if query.get("latitude") is None or query.get("longitude") is None:
                _fail(f"{path}.query", "ALERT_LOCATION_MISSING：point 查询必须保存 latitude/longitude")
            else:
                plot_lat, plot_lon = plot.get("latitude"), plot.get("longitude")
                if float(query["latitude"]) != float(plot_lat) or float(query["longitude"]) != float(plot_lon):
                    _fail(f"{path}.query",
                          "ALERT_QUERY_POINT_NEQ_PLOT：point 查询必须使用 plot 坐标，不得改写成数值预报格点")
        elif q_type == "county":
            spatial_id = query.get("spatial_id")
            if not isinstance(spatial_id, str) or not spatial_id.strip():
                _fail(f"{path}.query.spatial_id", "ALERT_LOCATION_MISSING：county 查询必须保存 spatial_id")
        elif q_type == "full_snapshot":
            domain = query.get("authority_domain")
            if not isinstance(domain, str) or not domain.strip():
                _fail(f"{path}.query.authority_domain",
                      "ALERT_AUTHORITY_DOMAIN_MISSING：全量快照必须声明 authority_domain")
            if query.get("snapshot_complete") is not True:
                _fail(f"{path}.query.snapshot_complete",
                      "ALERT_SNAPSHOT_INCOMPLETE：必须为 true 才能 queried_clear")

    queried_at = _optional_time(feed.get("queried_at"), f"{path}.queried_at")
    alert_issued = _optional_time(feed.get("issued_at"), f"{path}.issued_at")
    if coverage in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED | {"feed_unavailable", "query_incomplete"}:
        if queried_at is None:
            _fail(f"{path}.queried_at", "ALERT_QUERIED_TIME_MISSING：必须保存 queried_at（含时区）")
    # 原生快照已从原响应完整复核；整体查询无签发时间时只保留每个事件自己的时间。
    if coverage in ALERT_COVERAGE_CLEAR | ALERT_COVERAGE_MARKED and alert_issued is None and "query_snapshot" not in feed:
        _fail(f"{path}.issued_at", "ALERT_ISSUED_TIME_MISSING：不得用小时天气 weather.issued_at 冒充")
    if queried_at is not None and queried_at > now:
        _fail(f"{path}.queried_at", "ALERT_QUERIED_IN_FUTURE：queried_at 不得晚于 now")
    if alert_issued is not None and alert_issued > now:
        _fail(f"{path}.issued_at", "ALERT_ISSUED_IN_FUTURE：预警产品 issued_at 不得晚于 now")

    if "items" in feed:
        _list(feed["items"], f"{path}.items")
        items = feed["items"]
    else:
        items = []
    item_ids = {}
    for i, item in enumerate(items):
        item_path = f"{path}.items[{i}]"
        _object(item, item_path, {"id", "sent", "hazard_tag", "event", "effective", "onset", "expires",
                                  "area_desc", "geocode", "polygon", "circle"})
        item_id = _text(item.get("id"), f"{item_path}.id")
        if item_id in item_ids:
            _fail(f"{item_path}.id", "ALERT_ITEM_ID_DUPLICATE：id 重复")
        item_ids[item_id] = item
        sent = _time(item.get("sent"), f"{item_path}.sent")
        if sent > now:
            _fail(f"{item_path}.sent", "ALERT_ITEM_SENT_IN_FUTURE：sent 不得晚于 now")
        _text(item.get("hazard_tag"), f"{item_path}.hazard_tag")
        for key in ("event", "area_desc"):
            if item.get(key) is not None:
                _text(item[key], f"{item_path}.{key}")
        for key in ("effective", "onset", "expires"):
            if item.get(key) is not None:
                _time(item[key], f"{item_path}.{key}")
        for key in ("polygon", "circle"):
            if item.get(key) is not None:
                _text(item[key], f"{item_path}.{key}")
        if item.get("geocode") is not None:
            _list(item["geocode"], f"{item_path}.geocode")
            for k, geo in enumerate(item["geocode"]):
                geo_path = f"{item_path}.geocode[{k}]"
                _object(geo, geo_path, {"system", "value"})
                if geo.get("system") is not None:
                    _text(geo["system"], f"{geo_path}.system")
                if geo.get("value") is not None:
                    _text(geo["value"], f"{geo_path}.value")

    if coverage in ALERT_COVERAGE_CLEAR and items:
        _fail(f"{path}.items", "ALERT_CLEAR_WITH_ITEMS：queried_clear 的 items 必须为空数组")
    if coverage in ALERT_COVERAGE_MARKED and not items:
        _fail(f"{path}.items", "ALERT_MARKED_WITHOUT_ITEMS：active_alerts 必须有非空 items")

    derived = "unknown"
    if coverage in ALERT_COVERAGE_CLEAR:
        derived = "queried_clear"
    elif coverage in ALERT_COVERAGE_MARKED:
        derived = "marked"
    elif coverage == "synthetic_declared":
        derived = "marked" if items else "queried_clear"
    return derived, item_ids


def _check_hazards_against_coverage(records, derived, path, item_ids):
    """三态与 records[].hazards 对齐；空数组不能冒充无预警。"""
    tags = {item.get("hazard_tag") for item in item_ids.values()}
    for j, record in enumerate(records):
        record_path = f"{path}.records[{j}]"
        hazards = record["hazards"] if "hazards" in record else None
        refs = record.get("hazard_item_ids") or []
        for k, ref in enumerate(refs):
            if ref not in item_ids:
                _fail(f"{record_path}.hazard_item_ids[{k}]", "ALERT_ITEM_REF_UNKNOWN：未指向 items[].id")
        if derived == "unknown":
            if hazards == []:
                _fail(f"{record_path}.hazards", "ALERT_EMPTY_IMPERSONATION：空数组不能冒充无预警")
            if isinstance(hazards, list) and hazards:
                _fail(f"{record_path}.hazards",
                      "ALERT_HAZARDS_PRESENT_WHEN_UNKNOWN：未知覆盖下不得带非空 hazards")
        elif derived == "queried_clear":
            if hazards is None:
                _fail(f"{record_path}.hazards",
                      "HAZARDS_MISSING_WHEN_CLEAR：queried_clear 必须显式写出 hazards=[]")
            elif hazards:
                _fail(f"{record_path}.hazards", "ALERT_CLEAR_NONEMPTY_HAZARDS：queried_clear 时 hazards 必须为空")
        elif derived == "marked":
            if not hazards:
                _fail(f"{record_path}.hazards",
                      "ALERT_ACTIVE_REWRITTEN_EMPTY：active_alerts 时 hazards 不得为空或省略")
            else:
                for tag in hazards:
                    if tag not in tags:
                        _fail(f"{record_path}.hazards",
                              f"ALERT_TAG_NOT_IN_ITEMS：标记 {tag} 未出现在 items[].hazard_tag")


EXPOSURE_SCOPES = frozenset({"plot_only", "plot_and_access_route"})
EXPOSURE_ROLES = ("access_route", "rest_place")
EXPOSURE_RECORD_KEYS = frozenset({
    "start", "end", "temperature_c", "relative_humidity_pct", "wind_m_s",
    "shortwave_w_m2", "precipitation_mm", "daylight", "hazards", "wbgt_c",
})


def _exposure_records(records, path, has_method):
    """分地点逐时记录沿用同一条边界；这里不补默认值，也不把缺项当零。"""
    _list(records, path)
    for j, record in enumerate(records):
        record_path = f"{path}[{j}]"
        _object(record, record_path, EXPOSURE_RECORD_KEYS)
        _interval(record, record_path)
        _number(record.get("temperature_c"), f"{record_path}.temperature_c", -100, 100)
        _number(record.get("relative_humidity_pct"), f"{record_path}.relative_humidity_pct", 0, 100)
        for key in ("wind_m_s", "shortwave_w_m2", "precipitation_mm"):
            _number(record.get(key), f"{record_path}.{key}", 0)
        _bool(record.get("daylight"), f"{record_path}.daylight")
        if "hazards" in record:
            _strings(record["hazards"], f"{record_path}.hazards")
        if "wbgt_c" in record:
            _number(record["wbgt_c"], f"{record_path}.wbgt_c", -100, 100)
            if not has_method:
                _fail(path, "WBGT_METHOD_MISSING：分地点记录含 WBGT 时必须说明来源方法")
    records.sort(key=lambda record: parse_time(record["start"]))
    _nonoverlap(records, path)


def _validate_exposure(block, path, mode):
    """分地点暴露声明；缺省不在这里补，缺声明由环境层标成显式代理状态。"""
    _object(block, path, {"scope", "access_route", "rest_place"})
    _enum(block.get("scope"), EXPOSURE_SCOPES, f"{path}.scope")
    scope = block["scope"]
    declared = [role for role in EXPOSURE_ROLES if role in block]
    if scope == "plot_only":
        if declared:
            _fail(path, "EXPOSURE_SCOPE_MISMATCH：scope=plot_only 不得同时声明分地点数据")
        return
    if not declared:
        _fail(path, "EXPOSURE_DECLARATION_EMPTY：声明分地点覆盖时必须给出 access_route 或 rest_place")
    for role in declared:
        role_path = f"{path}.{role}"
        entry = block[role]
        _object(entry, role_path, {"source", "review_status", "measured", "method", "kind",
                                   "environment", "issued_at", "wbgt_method", "records"})
        _text(entry.get("source"), f"{role_path}.source")
        if is_placeholder_source(entry.get("source")):
            _fail(f"{role_path}.source", "EXPOSURE_SOURCE_PLACEHOLDER：分地点暴露来源不得为占位词")
        _enum(entry.get("review_status"), REVIEW_STATES, f"{role_path}.review_status")
        entry.setdefault("measured", False)
        _bool(entry["measured"], f"{role_path}.measured")
        if "method" in entry:
            _text(entry["method"], f"{role_path}.method")
        if "wbgt_method" in entry:
            _text(entry["wbgt_method"], f"{role_path}.wbgt_method")
        if "issued_at" in entry:
            _time(entry["issued_at"], f"{role_path}.issued_at")
        elif "records" in entry:
            _fail(f"{role_path}.issued_at", "EXPOSURE_ISSUED_TIME_MISSING：给出逐时记录时必须保存发布时间")
        if entry["measured"] and not entry.get("method"):
            _fail(role_path, "EXPOSURE_MEASURED_METHOD_MISSING：声称实测必须写明测量或估计方法")
        has_records = "records" in entry
        if has_records:
            _enum(entry.get("kind"), {"forecast", "measured", "synthetic"}, f"{role_path}.kind")
            _enum(entry.get("environment"), {"outdoor", "greenhouse"}, f"{role_path}.environment")
        if entry["measured"] and not has_records:
            _fail(role_path, "EXPOSURE_MEASURED_SERIES_MISSING：声称实测必须给出该地点逐时记录，不能只声明")
        if has_records:
            _exposure_records(entry["records"], f"{role_path}.records",
                              bool(entry.get("wbgt_method")))


def resource_occupation(resource, mode="shadow") -> dict:
    """资源的占用口径（读取，不修改输入）。

    默认 full_session：整段出工保留容量，是保守近似。
    declared_stages：仅在声明的阶段保留容量，需可辨识来源与可采用审核状态；
    目前只允许 kind=rest_place 声明 rest 阶段（水源等不得细分）。任何不符合条件的
    声明一律关闭为 full_session 并给出 reason，绝不静默放大占用假设。
    本函数不抛异常，供引擎与复查复用；输入契约的硬错误在 validate_request 层拒绝。
    """
    kind = resource.get("kind") if isinstance(resource, dict) else None
    block = resource.get("occupation") if isinstance(resource, dict) else None
    base = {"mode": "full_session", "stages": [], "subdivided": False,
            "source": None, "review_status": None,
            "reason": "未声明占用口径，按整段保留（保守）"}
    if not isinstance(block, dict):
        return base
    declared = block.get("mode", "full_session")
    if declared == "full_session":
        return dict(base, reason="显式声明整段保留")
    if declared != "declared_stages":
        return dict(base, reason=f"未知占用口径 {declared!r}，按整段保留（保守）")
    if kind != "rest_place":
        return dict(base, reason="只有休息处可细分占用阶段，该资源按整段保留（保守）")
    stages = block.get("stages")
    if not isinstance(stages, list) or not stages:
        return dict(base, reason="细分占用未给出阶段，按整段保留（保守）")
    if any(not isinstance(entry, str) for entry in stages):
        return dict(base, reason="细分占用阶段含非字符串项，按整段保留（保守）")
    normalized = sorted(set(stages))
    if any(entry not in RESOURCE_OCCUPATION_STAGES for entry in normalized):
        return dict(base, reason="细分占用只支持 rest 阶段，按整段保留（保守）")
    source = block.get("source")
    if is_placeholder_source(source):
        return dict(base, reason="细分占用缺少可辨识来源，按整段保留（保守）")
    review = block.get("review_status", "unknown")
    accepted = {"confirmed", "illustrative"} if mode == "demonstration" else {"confirmed"}
    if review not in accepted:
        return dict(base, reason=f"细分占用审核状态 {review!r} 在本模式不可采用，按整段保留（保守）")
    return {"mode": "declared_stages", "stages": normalized, "subdivided": True,
            "source": source, "review_status": review, "reason": None}


def _resource_occupation_declaration(block, path, kind, mode):
    """校验可选的资源占用声明块；不满足依据时直接拒绝，不静默放行（R15-A03）。"""
    _object(block, path, {"mode", "stages", "source", "review_status"})
    block.setdefault("mode", "full_session")
    _enum(block["mode"], RESOURCE_OCCUPATION_MODES, f"{path}.mode")
    if block["mode"] == "full_session":
        if "stages" in block:
            _list(block["stages"], f"{path}.stages")
        else:
            block["stages"] = []
        return
    if kind != "rest_place":
        _fail(path, "只有 rest_place 可细分占用阶段", "RESOURCE_OCCUPATION_SCOPE")
    stages = block.get("stages")
    if not isinstance(stages, list) or not stages:
        _fail(f"{path}.stages", "declared_stages 必须给出非空阶段列表",
              "RESOURCE_OCCUPATION_STAGES_MISSING")
    _strings(stages, f"{path}.stages")
    for entry in stages:
        if entry not in RESOURCE_OCCUPATION_STAGES:
            _fail(f"{path}.stages", f"暂不支持阶段 {entry!r}（仅 rest）",
                  "RESOURCE_OCCUPATION_STAGE_UNKNOWN")
    _text(block.get("source"), f"{path}.source")
    if is_placeholder_source(block.get("source")):
        _fail(f"{path}.source", "细分占用必须给出可辨识来源，占位词不能当作依据",
              "RESOURCE_OCCUPATION_SOURCE_PLACEHOLDER")
    _enum(block.get("review_status"), REVIEW_STATES, f"{path}.review_status")
    if block.get("review_status") == "unknown":
        _fail(f"{path}.review_status",
              "声明细分占用时审核状态不得为 unknown；缺证据应省略或保持 full_session",
              "RESOURCE_OCCUPATION_REVIEW_UNKNOWN")
    if mode != "demonstration" and block.get("review_status") != "confirmed":
        _fail(f"{path}.review_status", "非演示模式不得用 illustrative 细分资源占用",
              "RESOURCE_OCCUPATION_REVIEW_NOT_ADOPTABLE")


def worker_profile_status(worker, start, end=None):
    """资料有效只表示输入声明覆盖本段；不认证专业资格或健康安全。"""
    profile = worker.get("worker_profile")
    if profile is None:
        return "legacy_unspecified"
    status = profile["status"]
    if status != "valid":
        return status
    lo, hi = to_utc(start), to_utc(end or start)
    if any(to_utc(profile[key]) > lo for key in ("observed_at", "state_observed_at")):
        return "unknown"
    if any(to_utc(profile[key]) < hi for key in ("valid_until", "state_valid_until")):
        return "expired"
    return "valid"


def _worker_profile(profile, path, now):
    _object(profile, path, {"status", "source", "observed_at", "valid_until",
                            "state_observed_at", "state_valid_until"})
    profile.setdefault("status", "unfilled")
    _enum(profile["status"], {"unfilled", "unknown", "valid", "expired"}, path + ".status")
    if "source" in profile:
        _text(profile["source"], path + ".source")
    for key in ("observed_at", "valid_until", "state_observed_at", "state_valid_until"):
        if key in profile:
            _time(profile[key], path + "." + key)
    if profile["status"] == "valid":
        if is_placeholder_source(profile.get("source", "")):
            _fail(path + ".source", "valid 必须给出非占位来源；这仍只是输入声明")
        for observed, until in (("observed_at", "valid_until"), ("state_observed_at", "state_valid_until")):
            at = _time(profile.get(observed), path + "." + observed)
            stop = _time(profile.get(until), path + "." + until)
            if at > now or stop <= at:
                _fail(path, "观察时间不得晚于 now，有效期必须晚于观察时间")


def _wait_duty(duty, path):
    _object(duty, path, {"status", "minutes", "reason", "source", "posture",
                        "is_physical_labor", "recovery_claimed"})
    duty.setdefault("status", "unknown")
    _enum(duty["status"], {"known", "unknown"}, path + ".status")
    for key in ("is_physical_labor", "recovery_claimed"):
        duty.setdefault(key, False)
        _bool(duty[key], path + "." + key)
    if duty["recovery_claimed"] or duty.get("reason") in {"rest", "recovery", "休息", "恢复"}:
        _fail(path, "WAIT_AS_REST 等待不能充当恢复休息")
    if duty["is_physical_labor"] or duty.get("posture") in {"walking", "operating"}:
        _fail(path, "WAIT_MARKED_PHYSICAL 走动或操作应另记作业或行程")
    if "minutes" in duty:
        _number(duty["minutes"], path + ".minutes", 0, 10080)
    if duty["status"] == "known":
        _number(duty.get("minutes"), path + ".minutes", 0, 10080)
        _enum(duty.get("reason"), {"equipment", "helper", "material", "weather", "watch", "other"}, path + ".reason")
        if not isinstance(duty.get("source"), str) or is_placeholder_source(duty["source"]):
            _fail(path + ".source", "WAIT_SOURCE_UNKNOWN 已知等待必须有非占位来源")


def validate_request(raw) -> dict:
    """深拷贝并补充保守默认值；审核资格和现场事实由业务流程确认。"""
    allowed = {"schema_version", "mode", "now", "horizon_start", "horizon_end",
               "timezone", "step_minutes", "beam_width", "max_search_states",
               "plots", "workers", "resources", "tasks", "weather", "policy"}
    _object(raw, "request", allowed)
    request = deepcopy(raw)
    request.setdefault("schema_version", "1.0")
    _enum(request["schema_version"], {"1.0", "1.1"}, "schema_version")
    request.setdefault("mode", "shadow")
    _enum(request["mode"], {"demonstration", "shadow", "assistance"}, "mode")
    now = _time(request.get("now"), "now")
    start = _time(request.get("horizon_start"), "horizon_start")
    end = _time(request.get("horizon_end"), "horizon_end")
    if end <= start:
        _fail("horizon_end", "必须晚于 horizon_start")
    if start < now:
        _fail("horizon_start", "不得早于 now；历史回放应采用保存的原 now")
    if (end - start).total_seconds() > 7 * 86400:
        _fail("horizon_end", "规划范围不得超过 7 天")
    resolve_timezone(request.get("timezone"))
    for key, default, maximum in (("step_minutes", 15, 60), ("beam_width", 12, 64), ("max_search_states", 1500, 20000)):
        request.setdefault(key, default)
        _number(request[key], key, maximum=maximum, integer=True, positive=True)
        request[key] = int(request[key])
    for key in ("plots", "workers", "resources", "tasks"):
        request.setdefault(key, [])
        _list(request[key], key)
        if len(request[key]) > {"plots": 20, "workers": 12, "resources": 50, "tasks": 20}[key]:
            _fail(key, "数量超过本版有界搜索上限")
    plots = _index(request["plots"], "plots")
    workers = _index(request["workers"], "workers")
    resources = _index(request["resources"], "resources")
    tasks = _index(request["tasks"], "tasks")

    for i, plot in enumerate(request["plots"]):
        path = f"plots[{i}]"
        _object(plot, path, {"id", "region_id", "latitude", "longitude", "environment", "conditions"})
        _text(plot.get("region_id"), f"{path}.region_id")
        _number(plot.get("latitude"), f"{path}.latitude", -90, 90)
        _number(plot.get("longitude"), f"{path}.longitude", -180, 180)
        _enum(plot.get("environment"), {"outdoor", "greenhouse"}, f"{path}.environment")
        plot.setdefault("conditions", {})
        _conditions(plot["conditions"], f"{path}.conditions")

    for i, worker in enumerate(request["workers"]):
        path = f"workers[{i}]"
        worker_keys = {"id", "state", "availability", "limits", "used_active_minutes_by_date", "initial_rest_confirmed", "role"}
        if request["schema_version"] == "1.1":
            worker_keys.add("worker_profile")
        _object(worker, path, worker_keys)
        if "role" in worker:
            _enum(worker["role"], {"elder_self", "helper", "other", "unknown"}, path + ".role")
        if request["schema_version"] == "1.1":
            worker.setdefault("worker_profile", {"status": "unfilled"})
            _worker_profile(worker["worker_profile"], path + ".worker_profile", now)
        worker.setdefault("state", "unknown")
        _enum(worker["state"], {"clear", "stop", "unknown"}, f"{path}.state")
        # clear 只声明当前状态，不能同时被解释成已完成出工前休息。
        worker.setdefault("initial_rest_confirmed", False)
        _bool(worker["initial_rest_confirmed"], f"{path}.initial_rest_confirmed")
        worker.setdefault("availability", [])
        _intervals(worker["availability"], f"{path}.availability")
        worker.setdefault("limits", {})
        limits = worker["limits"]
        _object(limits, f"{path}.limits", {"max_active_minutes_per_day", "max_continuous_active_minutes",
                "min_rest_minutes", "max_load_kg", "forbidden_tags", "source", "review_status"})
        _review(limits, f"{path}.limits")
        for key in ("max_active_minutes_per_day", "max_continuous_active_minutes", "min_rest_minutes"):
            if key not in limits and limits["review_status"] != "unknown":
                _fail(f"{path}.limits.{key}", "已审核参数必须显式提供")
            limits.setdefault(key, 0)
            lower = 1 if limits["review_status"] != "unknown" and key != "max_active_minutes_per_day" else 0
            upper = 1440 if key == "max_active_minutes_per_day" else 10080
            _number(limits[key], f"{path}.limits.{key}", lower, upper,
                    positive=limits["review_status"] != "unknown")
        if "max_load_kg" in limits:
            _number(limits["max_load_kg"], f"{path}.limits.max_load_kg", 0)
        limits.setdefault("forbidden_tags", [])
        _strings(limits["forbidden_tags"], f"{path}.limits.forbidden_tags")
        worker.setdefault("used_active_minutes_by_date", {})
        used = worker["used_active_minutes_by_date"]
        if not isinstance(used, dict):
            _fail(f"{path}.used_active_minutes_by_date", "必须为对象")
        for day, minutes in used.items():
            try:
                valid = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
            except ValueError:
                valid = False
            if not valid:
                _fail(f"{path}.used_active_minutes_by_date", "日期键必须为 YYYY-MM-DD")
            _number(minutes, f"{path}.used_active_minutes_by_date.{day}", 0, 1440)

    for i, resource in enumerate(request["resources"]):
        path = f"resources[{i}]"
        _object(resource, path, {"id", "kind", "capacity", "availability", "occupation"})
        _text(resource.get("kind"), f"{path}.kind")
        resource.setdefault("capacity", 1)
        _number(resource["capacity"], f"{path}.capacity", integer=True, positive=True)
        resource["capacity"] = int(resource["capacity"])
        resource.setdefault("availability", [])
        _intervals(resource["availability"], f"{path}.availability")
        if "occupation" in resource:
            _resource_occupation_declaration(resource["occupation"], f"{path}.occupation",
                                             resource.get("kind"), request["mode"])

    for i, task in enumerate(request["tasks"]):
        path = f"tasks[{i}]"
        _object(task, path, {"id", "plot_id", "crop_id", "stage", "operation", "method",
            "remaining_quantity", "rates", "earliest_start", "deadline", "deadline_source", "priority",
            "divisible", "min_chunk_minutes", "quantity_step", "depends_on", "required_resources", "tags",
            "load_per_trip_kg", "session", "once", "agronomy", "crop_alias", "parallel_within_task",
            "deadline_basis", "operation_scope", "wait_duty", "crop_alias_resolution"})
        if "crop_alias_resolution" in task and "crop_alias" not in task:
            _fail(path + ".crop_alias_resolution", "派生解析必须绑定原 crop_alias")
        if "wait_duty" in task:
            _wait_duty(task["wait_duty"], path + ".wait_duty")
        for key in ("plot_id", "crop_id", "stage", "operation", "method", "deadline_source"):
            _text(task.get(key), f"{path}.{key}")
        if task["plot_id"] not in plots:
            _fail(f"{path}.plot_id", "引用的田块不存在")
        quantity = task.get("remaining_quantity")
        _object(quantity, f"{path}.remaining_quantity", {"value", "unit"})
        _enum(quantity.get("unit"), UNITS, f"{path}.remaining_quantity.unit")
        _check_remaining_family(task, path)
        _number(quantity.get("value"), f"{path}.remaining_quantity.value", 0,
                integer=quantity["unit"] in {"trip", "plant"})
        task.setdefault("rates", {})
        if not isinstance(task["rates"], dict):
            _fail(f"{path}.rates", "必须为按 worker_id 索引的对象")
        for worker_id, rate in task["rates"].items():
            rate_path = f"{path}.rates.{worker_id}"
            if worker_id not in workers:
                # 空名单是排程缺口，不在校验层丢任务；有人名单时缺引用仍拒绝。
                if workers:
                    _fail(rate_path, "引用的劳动者不存在")
            _object(rate, rate_path, {"unit", "low", "high", "scope", "source"})
            _enum(rate.get("unit"), UNITS, f"{rate_path}.unit")
            if not (rate["unit"] == quantity["unit"] or {rate["unit"], quantity["unit"]} <= {"mu", "sqm"}):
                _fail(f"{rate_path}.unit", "与剩余工作量单位不兼容")
            for key in ("low", "high"):
                _number(rate.get(key), f"{rate_path}.{key}", positive=True)
            if rate["low"] > rate["high"]:
                _fail(rate_path, "low 不得大于 high")
            _enum(rate.get("scope"), {"net_work", "whole_session"}, f"{rate_path}.scope")
            _text(rate.get("source"), f"{rate_path}.source")
            if is_placeholder_source(rate.get("source")):
                _fail(f"{rate_path}.source", "待核实或占位来源不得进入精细排程", "SOURCE_PLACEHOLDER")
        task.setdefault("earliest_start", request["horizon_start"])
        earliest = _time(task["earliest_start"], f"{path}.earliest_start")
        deadline = _time(task.get("deadline"), f"{path}.deadline")
        if deadline <= earliest:
            _fail(f"{path}.deadline", "必须晚于 earliest_start")
        _deadline_basis(task, path, request["mode"])
        task.setdefault("priority", 3)
        _number(task["priority"], f"{path}.priority", 1, 5, integer=True)
        task["priority"] = int(task["priority"])
        task.setdefault("divisible", True)
        _bool(task["divisible"], f"{path}.divisible")
        task.setdefault("min_chunk_minutes", 15)
        _number(task["min_chunk_minutes"], f"{path}.min_chunk_minutes", positive=True)
        if quantity["unit"] in {"trip", "plant"}:
            task.setdefault("quantity_step", 1)
        if "quantity_step" in task:
            _number(task["quantity_step"], f"{path}.quantity_step", positive=True,
                    integer=quantity["unit"] in {"trip", "plant"})
            if quantity["unit"] in {"trip", "plant"} and quantity["value"] % task["quantity_step"] != 0:
                _fail(f"{path}.remaining_quantity.value", "趟数和株数须为 quantity_step 的整数倍")
        for key in ("depends_on", "required_resources", "tags"):
            task.setdefault(key, [])
            _strings(task[key], f"{path}.{key}")
        for j, dependency in enumerate(task["depends_on"]):
            if dependency not in tasks:
                _fail(f"{path}.depends_on[{j}]", f"引用的任务不存在：{dependency}")
            if dependency == task["id"]:
                _fail(f"{path}.depends_on[{j}]", "依赖存在循环")
        for j, resource_id in enumerate(task["required_resources"]):
            if resource_id not in resources:
                _fail(f"{path}.required_resources[{j}]", f"引用的资源不存在：{resource_id}")
        if "load_per_trip_kg" in task:
            _number(task["load_per_trip_kg"], f"{path}.load_per_trip_kg", 0)
        task.setdefault("session", {})
        session_keys = {"setup_minutes", "outbound_minutes", "return_minutes", "cleanup_minutes", "buffer_minutes"}
        _object(task["session"], f"{path}.session", session_keys)
        for key in sorted(session_keys):
            task["session"].setdefault(key, 0)
            _number(task["session"][key], f"{path}.session.{key}", 0, 10080)
        # once 为整任务一次；往返/预留/等待仍属每次出工或未冻结阶段，不得写入。
        task.setdefault("once", {})
        once_keys = {"setup_minutes", "cleanup_minutes"}
        _object(task["once"], f"{path}.once", once_keys)
        for key in sorted(once_keys):
            task["once"].setdefault(key, 0)
            _number(task["once"][key], f"{path}.once.{key}", 0, 10080)
        task.setdefault("agronomy", {})
        agronomy = task["agronomy"]
        _object(agronomy, f"{path}.agronomy", {"status", "source", "conditions", "weather_limits"})
        _review(agronomy, f"{path}.agronomy", "status")
        agronomy.setdefault("conditions", {})
        _conditions(agronomy["conditions"], f"{path}.agronomy.conditions")
        agronomy.setdefault("weather_limits", {})
        _weather_limits(agronomy["weather_limits"], f"{path}.agronomy.weather_limits")
        if "operation_scope" in task:
            _operation_scope(task["operation_scope"], f"{path}.operation_scope")
        if "parallel_within_task" in task:
            spec = task["parallel_within_task"]
            spec_path = f"{path}.parallel_within_task"
            _object(spec, spec_path, {"allowed", "source", "review_status"})
            spec.setdefault("allowed", False)
            _bool(spec["allowed"], f"{spec_path}.allowed")
            if spec["allowed"]:
                _text(spec.get("source"), f"{spec_path}.source")
                if is_placeholder_source(spec.get("source")):
                    _fail(f"{spec_path}.source",
                          "同任务并行必须给出可辨识来源，占位词不能当作协作依据",
                          "PARALLEL_SOURCE_PLACEHOLDER")
                _enum(spec.get("review_status"), REVIEW_STATES, f"{spec_path}.review_status")
                if spec["review_status"] == "unknown":
                    _fail(f"{spec_path}.review_status",
                          "声明可并行时审核状态不得为 unknown；缺证据应省略或 allowed=false",
                          "PARALLEL_REVIEW_UNKNOWN")
                if request["mode"] != "demonstration" and spec["review_status"] != "confirmed":
                    _fail(f"{spec_path}.review_status",
                          "非演示模式不得用 illustrative 打开同任务并行",
                          "PARALLEL_REVIEW_NOT_ADOPTABLE")
            else:
                if "source" in spec:
                    _text(spec["source"], f"{spec_path}.source", empty=True)
                if "review_status" in spec:
                    _enum(spec["review_status"], REVIEW_STATES, f"{spec_path}.review_status")
        _apply_crop_alias(task, path, plots[task["plot_id"]])

    _reject_cycles(tasks)

    request.setdefault("weather", {})
    if not isinstance(request["weather"], dict):
        _fail("weather", "必须为按 plot_id 索引的对象")
    for plot_id, weather in request["weather"].items():
        path = f"weather.{plot_id}"
        if plot_id not in plots:
            _fail(path, "引用的田块不存在")
        _object(weather, path, {"source", "issued_at", "kind", "environment", "wbgt_method", "records",
                                "alert_feed", "grid", "exposure", "forecast_run_snapshot"})
        _text(weather.get("source"), f"{path}.source")
        if "forecast_run_snapshot" in weather:
            from .forecast_run import validate_run_snapshot, validate_run_location
            try:
                validate_run_snapshot(weather, now)
                validate_run_location(weather["forecast_run_snapshot"], plots[plot_id])
            except ValueError:
                _fail(f"{path}.forecast_run_snapshot", "FORECAST_RUN_SNAPSHOT_INVALID：单次运行来源、时间或天气内容未通过核对")
        else:
            _time(weather.get("issued_at"), f"{path}.issued_at")
        _enum(weather.get("kind"), {"forecast", "measured", "synthetic"}, f"{path}.kind")
        _enum(weather.get("environment"), {"outdoor", "greenhouse"}, f"{path}.environment")
        if "wbgt_method" in weather:
            _text(weather["wbgt_method"], f"{path}.wbgt_method")
        if "grid" in weather:
            grid_path = f"{path}.grid"
            grid = weather["grid"]
            _object(grid, grid_path, {"kind", "latitude", "longitude", "environment",
                                      "cell_id", "resolution_km", "source"})
            _enum(grid.get("kind"), GRID_KINDS, f"{grid_path}.kind")
            _number(grid.get("latitude"), f"{grid_path}.latitude", -90, 90)
            _number(grid.get("longitude"), f"{grid_path}.longitude", -180, 180)
            if "environment" in grid:
                _enum(grid["environment"], {"outdoor", "greenhouse"}, f"{grid_path}.environment")
            if "cell_id" in grid:
                _text(grid["cell_id"], f"{grid_path}.cell_id")
            if "resolution_km" in grid:
                _number(grid["resolution_km"], f"{grid_path}.resolution_km", positive=True)
            _text(grid.get("source"), f"{grid_path}.source")
        weather.setdefault("records", [])
        _list(weather["records"], f"{path}.records")
        for j, record in enumerate(weather["records"]):
            record_path = f"{path}.records[{j}]"
            _object(record, record_path, {"start", "end", "temperature_c", "relative_humidity_pct", "wind_m_s",
                "shortwave_w_m2", "precipitation_mm", "daylight", "hazards", "wbgt_c", "hazard_item_ids"})
            _interval(record, record_path)
            _number(record.get("temperature_c"), f"{record_path}.temperature_c", -100, 100)
            _number(record.get("relative_humidity_pct"), f"{record_path}.relative_humidity_pct", 0, 100)
            for key in ("wind_m_s", "shortwave_w_m2", "precipitation_mm"):
                _number(record.get(key), f"{record_path}.{key}", 0)
            _bool(record.get("daylight"), f"{record_path}.daylight")
            # 省略 hazards 表示未知；不得在校验层把缺项补成 []。
            if "hazards" in record:
                _strings(record.get("hazards"), f"{record_path}.hazards")
            if "hazard_item_ids" in record:
                _strings(record["hazard_item_ids"], f"{record_path}.hazard_item_ids")
            if "wbgt_c" in record:
                _number(record["wbgt_c"], f"{record_path}.wbgt_c", -100, 100)
                if not weather.get("wbgt_method"):
                    _fail(f"{path}.wbgt_method", "输入 WBGT 时必须说明来源方法")
        weather["records"].sort(key=lambda record: parse_time(record["start"]))
        _nonoverlap(weather["records"], f"{path}.records")
        if "alert_feed" in weather:
            derived, item_ids = _validate_alert_feed(
                weather["alert_feed"], f"{path}.alert_feed", plots[plot_id], request["mode"], now)
            _check_hazards_against_coverage(weather["records"], derived, path, item_ids)
        elif request["mode"] != "demonstration":
            if any(record.get("hazards") == [] for record in weather["records"]):
                _fail(f"{path}.alert_feed",
                      "ALERT_FEED_MISSING / ALERT_EMPTY_IMPERSONATION：非演示空 hazards[] 且无预警信封，不能冒充无预警")
            _fail(f"{path}.alert_feed",
                  "ALERT_FEED_MISSING：shadow/assistance 必须给出 alert_feed，缺省不能当成已核对无预警")
        if "exposure" in weather:
            _validate_exposure(weather["exposure"], f"{path}.exposure", request["mode"])

    request.setdefault("policy", {})
    policy = request["policy"]
    _object(policy, "policy", {"source", "review_status", "max_forecast_age_minutes", "max_forecast_horizon_hours",
            "require_daylight", "blocked_hazards", "required_resource_kinds", "weather_limits",
            "grid_matching"})
    _review(policy, "policy")
    for key in ("max_forecast_age_minutes", "max_forecast_horizon_hours"):
        if key not in policy and policy["review_status"] != "unknown":
            _fail(f"policy.{key}", "已审核政策必须显式提供时效参数")
        policy.setdefault(key, 0)
        _number(policy[key], f"policy.{key}", 0)
    policy.setdefault("require_daylight", True)
    _bool(policy["require_daylight"], "policy.require_daylight")
    policy.setdefault("blocked_hazards", [])
    policy.setdefault("required_resource_kinds", ["water", "rest_place"])
    for key in ("blocked_hazards", "required_resource_kinds"):
        _strings(policy[key], f"policy.{key}")
    policy.setdefault("weather_limits", {})
    _weather_limits(policy["weather_limits"], "policy.weather_limits", policy=True)
    if "grid_matching" in policy:
        matching_path = "policy.grid_matching"
        matching = policy["grid_matching"]
        _object(matching, matching_path, {"source", "review_status", "max_distance_km"})
        _review(matching, matching_path)
        if "max_distance_km" not in matching:
            _fail(f"{matching_path}.max_distance_km",
                  "已提供空间匹配策略时必须给出容差；不得由程序自定默认值")
        _number(matching["max_distance_km"], f"{matching_path}.max_distance_km", 0)
    return request


def _canonical_number(value):
    """整数形态的有限浮点写入 JSON 整数，避免 3 与 3.0 分成两个摘要。"""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and abs(value) <= _JSON_INT_LIMIT:
        return int(value)
    return value


def _canonicalize(value, key=None):
    """纯函数快照：排序无序集合，不改调用方对象，不提升审核或健康状态。"""
    if isinstance(value, dict):
        return {item_key: _canonicalize(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        items = [_canonicalize(item) for item in value]
        if key in _ID_COLLECTIONS and all(isinstance(item, dict) and "id" in item for item in items):
            items.sort(key=lambda item: item["id"])
        elif items and all(isinstance(item, dict) and "start" in item and "end" in item for item in items):
            items.sort(key=lambda item: (item["start"], item["end"]))
        elif key in _SET_LIKE_KEYS and all(isinstance(item, str) for item in items):
            items.sort()
        return items
    return _canonical_number(value)


def canonical_dumps(value) -> str:
    """UTF-8 JSON：排序键、无空白、禁止 NaN。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_request(raw=None, *, validated=None) -> dict:
    """规范化信封：绑定规范化版本与 schema，补默认后的请求快照。原输入不进入此结构。"""
    if validated is None:
        request = validate_request(raw)
    else:
        request = validated
    return {
        "canonicalization_version": CANONICALIZATION_VERSION,
        "schema_version": request["schema_version"],
        "request": _canonicalize(request),
    }


def request_digest(raw=None, *, validated=None) -> str:
    """规范化输入的 SHA-256 十六进制摘要；不修改 raw 或 validated。"""
    payload = canonical_request(raw, validated=validated)
    return sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()
