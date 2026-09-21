"""积温阶段试验接口（R14-A07）。

目标：只对**有依据的品种配置**启用积温阶段估算；没有参数时走关闭路径。

本模块是**试验接口**，与 R09-A03 已验收的字段草案/结论同源：

- 默认不接入 engine / models / environment / workload，也不修改它们。
- 应用层若要使用，必须显式调用 ``gate_config`` / ``estimate_stage``，
  并自行承担证据、来源与地方核验责任。
- 关闭路径（fail-closed）：缺参数、身份未核实、方法未声明或未实现、
  单位不符、未地方核验、证据类型不足等情况一律返回 ``status="CLOSED"``，
  **绝不生成阶段日期**，也不编造任何数字。

本模块只做「门禁 + 已声明方法的确定性累积」，不发明农时，不声称现场效果，
不替代用户/观察提供的阶段，也不产生个人健康安全结论。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import math
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "gdd" / "variety_configs.json"

SCHEMA_ID = "yilao.gdd.stage_trial.v0.1"
INTERFACE_VERSION = "docs/接口约定.md schema 1.0"

MODES = frozenset({"demonstration", "shadow", "assistance"})
REGIONS_DUCHANG = "CN-JX-duchang"

# 已声明且本版有确定性实现的积温方法（单位见 METHOD_UNIT）。
IMPLEMENTED_METHODS = frozenset({
    "average_mean_reset",
    "average_minmax_reset",
    "single_sine_horizontal",
    "corn_86_50",
    "rice_dd50_arkansas",
})
# 名称已登记但本版未实现：宁走关闭路径，也不近似冒充。
RECOGNIZED_UNIMPLEMENTED_METHODS = frozenset({
    "single_sine_vertical",
    "single_triangle_horizontal",
    "baskerville_emin",
})
ALLOWED_METHODS = IMPLEMENTED_METHODS | RECOGNIZED_UNIMPLEMENTED_METHODS

# 方法与建模单位绑定；调用时记录单位必须一致，禁止混用 C_day / F_day。
METHOD_UNIT = {
    "average_mean_reset": ("C", "F"),
    "average_minmax_reset": ("C", "F"),
    "single_sine_horizontal": ("C", "F"),
    "corn_86_50": ("F",),
    "rice_dd50_arkansas": ("F",),
}

# 方法隐含常数：声明了该方法就必须给出与之一致的基温/上限，防止贴错标签。
METHOD_CONSTANTS = {
    "corn_86_50": {"tbase": 50.0, "tupper": 86.0, "unit": "F"},
    "rice_dd50_arkansas": {"tbase": 50.0, "tupper": 94.0, "unit": "F", "tmin_ceiling": 70.0, "daily_cap": 32.0},
}

METHOD_CUTOFF = {
    "single_sine_horizontal": "horizontal",
    "single_sine_vertical": "vertical",
    "single_triangle_horizontal": "horizontal",
}

ALLOWED_CUTOFF = frozenset({"horizontal", "vertical", "intermediate", "minmax_cap", "none"})
ALLOWED_BIOFIX = frozenset({
    "planting", "day_after_planting", "emergence", "transplant", "greenup", "local_observation",
})
ALLOWED_TEMP_ENTITY = frozenset({"air_2m", "canopy", "floodwater", "soil"})
HEAT_UNITS = frozenset({"C_day", "F_day"})
UNIT_TO_HEAT = {"C": "C_day", "F": "F_day"}

EVIDENCE_TYPES = frozenset({
    "SYNTHETIC", "IMPLEMENTATION_TEST", "SECONDARY_SUMMARY",
    "PRIMARY_SOURCE", "FIELD_OBSERVATION", "PROFESSIONAL_REVIEW",
})
# 非演示模式可启用的真实证据类型（合成/工程测试/摘要都不算现场依据）。
REAL_EVIDENCE_TYPES = frozenset({"PRIMARY_SOURCE", "FIELD_OBSERVATION", "PROFESSIONAL_REVIEW"})

PLACEHOLDER_SOURCES = frozenset({
    "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "unspecified", "待确认", "未确认", "未知", "待定", "待核实", "待补充",
})

# gate 需要的配置字段（R09-A03 gdd_field_schema.required_to_compute 的可机读投影）。
REQUIRED_CONFIG_FIELDS = (
    "crop_entity_id", "variety_id", "tbase", "tupper", "tbase_unit", "heat_unit",
    "calculation_method", "cutoff_method", "biofix", "tmax_tmin_definition",
    "day_boundary", "temperature_entity", "source_id", "region_id",
    "evidence_type", "transfer_allowed_to_duchang", "local_validated",
)


class GddStageError(ValueError):
    """结构错误（目录损坏等）；门禁与估算本身不抛错，走 CLOSED。"""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _placeholder(value) -> bool:
    text = " ".join(_text(value).split()).casefold()
    return not text or text in PLACEHOLDER_SOURCES


def _closed(reasons, *, config_id=None, kind="gate"):
    """统一的关闭结果：不给日期，不给累计值。"""
    return {
        "status": "CLOSED",
        "enabled": False,
        "kind": kind,
        "config_id": config_id,
        "crossing_date": None,
        "accumulated": None,
        "stage_gdd": None,
        "unit": None,
        "method": None,
        "fact_tag": None,
        "not_field_evidence": True,
        "locally_validated": False,
        "reasons": reasons,
    }


def _reason(code, message):
    return {"code": code, "message": message}


def _registry_path(path=None):
    return Path(path) if path else REGISTRY_PATH


def load_variety_registry(path=None) -> dict:
    """读取品种积温配置登记表；结构非法时报 GddStageError，不静默通过。"""
    target = _registry_path(path)
    if not target.is_file():
        raise GddStageError("REGISTRY_MISSING", f"找不到积温配置登记表：{target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:  # 目录损坏要响，而不是假装关闭
        raise GddStageError("REGISTRY_UNREADABLE", f"配置登记表无法解析：{exc}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("configs"), list):
        raise GddStageError("REGISTRY_MALFORMED", "登记表必须含 configs 数组")
    for i, row in enumerate(payload["configs"]):
        if not isinstance(row, dict):
            raise GddStageError("REGISTRY_MALFORMED", f"configs[{i}] 必须为对象")
        if not _text(row.get("config_id")):
            raise GddStageError("REGISTRY_MALFORMED", f"configs[{i}] 缺 config_id")
    return deepcopy(payload)


def gate_config(config, *, mode="demonstration") -> dict:
    """证据与参数门禁。

    返回 dict：``enabled`` 为 True 只表示"结构齐备且未声称成现场依据"，
    不表示已地方核验、更不表示能上线。任何缺失/冲突都返回关闭并给出原因码。
    """
    reasons = []
    if mode not in MODES:
        return _closed([_reason("MODE_INVALID", f"未知模式：{mode!r}")], kind="gate")
    if not isinstance(config, dict):
        return _closed([_reason("CONFIG_NOT_OBJECT", "配置必须为对象")], kind="gate")

    cid = config.get("config_id")
    for field in REQUIRED_CONFIG_FIELDS:
        if field not in config:
            reasons.append(_reason("MISSING_FIELD", f"缺字段 {field}"))

    if not _text(config.get("crop_entity_id")):
        reasons.append(_reason("CROP_REQUIRED", "crop_entity_id 必须非空"))
    if config.get("identity_hold") is True:
        reasons.append(_reason("IDENTITY_HOLD", "身份 HOLD（如菜瓜/芝麻），禁止生成积温窗"))

    variety = config.get("variety_id")
    if _placeholder(variety):
        reasons.append(_reason("VARIETY_REQUIRED", "缺品种或已记录品种组，不能用阶段积温需求"))

    tbase = config.get("tbase")
    if not _is_number(tbase):
        reasons.append(_reason("TBASE_REQUIRED", "tbase 必须为有限数字"))

    unit = config.get("tbase_unit")
    if unit not in {"C", "F"}:
        reasons.append(_reason("UNIT_REQUIRED", "tbase_unit 必须是 C 或 F"))

    heat_unit = config.get("heat_unit")
    if heat_unit not in HEAT_UNITS:
        reasons.append(_reason("HEAT_UNIT_REQUIRED", "heat_unit 必须是 C_day 或 F_day（禁止无单位积温）"))
    elif unit in UNIT_TO_HEAT and heat_unit != UNIT_TO_HEAT[unit]:
        reasons.append(_reason("HEAT_UNIT_MISMATCH", f"{unit} 基温必须配 {UNIT_TO_HEAT[unit]}，禁止混用 C_day/F_day"))

    method = config.get("calculation_method")
    if method in RECOGNIZED_UNIMPLEMENTED_METHODS:
        reasons.append(_reason("METHOD_NOT_IMPLEMENTED", f"{method} 已登记但本版未实现，宁关闭不近似"))
    elif method not in IMPLEMENTED_METHODS:
        reasons.append(_reason("METHOD_REQUIRED", "calculation_method 必须为已声明且已实现的方法"))

    cutoff = config.get("cutoff_method")
    if cutoff is not None and cutoff not in ALLOWED_CUTOFF:
        reasons.append(_reason("CUTOFF_INVALID", "cutoff_method 不在允许集合"))
    if method in METHOD_CUTOFF and cutoff != METHOD_CUTOFF[method]:
        reasons.append(_reason("CUTOFF_METHOD_MISMATCH",
                               f"{method} 必须配 cutoff_method={METHOD_CUTOFF[method]}"))

    if _placeholder(config.get("biofix")):
        reasons.append(_reason("BIOFIX_REQUIRED", "biofix 不能空（植期/出苗/移栽等须明确）"))
    elif config["biofix"] not in ALLOWED_BIOFIX:
        reasons.append(_reason("BIOFIX_INVALID", "biofix 不在允许集合"))

    if _placeholder(config.get("tmax_tmin_definition")):
        reasons.append(_reason("TBOUNDS_DEFINITION_REQUIRED", "必须声明逐日最高最低温的口径"))
    if _placeholder(config.get("day_boundary")):
        reasons.append(_reason("DAY_BOUNDARY_REQUIRED", "必须声明日界"))
    if _placeholder(config.get("source_id")):
        reasons.append(_reason("SOURCE_REQUIRED", "source_id 不能为占位或空"))

    region = config.get("region_id")
    if _placeholder(region):
        reasons.append(_reason("REGION_REQUIRED", "region_id 必须非空"))

    entity = config.get("temperature_entity")
    if entity not in ALLOWED_TEMP_ENTITY:
        reasons.append(_reason("TEMP_ENTITY_REQUIRED",
                               "必须声明气温实体（air_2m/canopy/floodwater/soil）"))

    ev = config.get("evidence_type")
    if ev not in EVIDENCE_TYPES:
        reasons.append(_reason("EVIDENCE_TYPE_INVALID", "evidence_type 不在允许集合"))
    elif mode != "demonstration" and ev not in REAL_EVIDENCE_TYPES:
        reasons.append(_reason("EVIDENCE_NOT_REAL",
                               f"{mode} 模式不接受 {ev} 作为已核实参数"))

    if not isinstance(config.get("transfer_allowed_to_duchang"), bool):
        reasons.append(_reason("TRANSFER_FLAG_REQUIRED", "transfer_allowed_to_duchang 必须为布尔值"))
    if not isinstance(config.get("local_validated"), bool):
        reasons.append(_reason("LOCAL_FLAG_REQUIRED", "local_validated 必须为布尔值"))

    local_validated = config.get("local_validated") is True
    transfer = config.get("transfer_allowed_to_duchang") is True
    if region == REGIONS_DUCHANG and not local_validated:
        reasons.append(_reason("DUCHANG_NOT_VALIDATED", "都昌积温未地方核验，保持关闭"))
    if transfer and not local_validated:
        reasons.append(_reason("TRANSFER_WITHOUT_LOCAL", "未地方核验不得把外地参数改成可转入都昌"))

    # 方法隐含常数：贴错标签直接关闭。
    implied = METHOD_CONSTANTS.get(method if isinstance(method, str) else None)
    if implied:
        if unit != implied["unit"]:
            reasons.append(_reason("METHOD_CONSTANT_MISMATCH", f"{method} 只以 °F 定义"))
        if not _is_number(tbase) or float(tbase) != implied["tbase"]:
            reasons.append(_reason("METHOD_CONSTANT_MISMATCH", f"{method} 基温必须为 {implied['tbase']}°F"))
        if not _is_number(config.get("tupper")) or float(config["tupper"]) != implied["tupper"]:
            reasons.append(_reason("METHOD_CONSTANT_MISMATCH", f"{method} 上限必须为 {implied['tupper']}°F"))

    # 水稻专用：需品种、光周期与栽培方式，且不得套玉米/平均法。
    rice_like = bool(config.get("is_rice")) or "RICE" in _text(config.get("crop_entity_id")).upper()
    if rice_like:
        if _placeholder(config.get("photoperiod_class")):
            reasons.append(_reason("RICE_PHOTOPERIOD", "水稻缺光周期敏感性"))
        if _placeholder(config.get("growing_system")):
            reasons.append(_reason("RICE_SYSTEM", "水稻缺栽培方式"))
        if method in {"corn_86_50", "average_mean_reset"}:
            reasons.append(_reason("RICE_NOT_CORN_FORMULA", "水稻不得套玉米 86/50 或未声明的平均法"))

    if config.get("recommendation_enabled") is True:
        reasons.append(_reason("RECOMMENDATION_PREMATURE", "复核前不得打开推荐"))

    if config.get("fill_null_duchang_from_foreign") is True:
        reasons.append(_reason("NO_FILL_NULL", "禁止用外地表填都昌空槽"))

    enabled = not reasons
    return {
        "status": "GATE_OPEN" if enabled else "CLOSED",
        "enabled": enabled,
        "kind": "gate",
        "config_id": cid,
        "method": method if isinstance(method, str) else None,
        "fact_tag": ev if enabled else None,
        "not_field_evidence": True,
        "locally_validated": local_validated,
        "reasons": reasons,
    }


def enabled_configs(registry, *, mode="demonstration"):
    """返回登记表中通过门禁、可做试验计算的配置（默认表应为空）。"""
    result = []
    for row in (registry or {}).get("configs", []) or []:
        if gate_config(row, mode=mode)["enabled"]:
            result.append(row)
    return result


# ---------------------------------------------------------------------------
# 已声明方法的逐日积温（标准公式；本模块不发明新方法）
# ---------------------------------------------------------------------------

def _average_mean_reset(tmax, tmin, tbase, tupper=None):
    """McMaster 方法1：日均温低于基温则当日为 0。"""
    tavg = (tmax + tmin) / 2.0
    return 0.0 if tavg < tbase else tavg - tbase


def _average_minmax_reset(tmax, tmin, tbase, tupper=None):
    """McMaster 方法2：先把低于基温的 Tmax/Tmin 抬到基温再平均。"""
    tmax_p = tbase if tmax < tbase else tmax
    tmin_p = tbase if tmin < tbase else tmin
    value = (tmax_p + tmin_p) / 2.0 - tbase
    return 0.0 if value < 0.0 else value


def _sine_horizontal(tmax, tmin, tbase, tupper=None):
    """单正弦 + 水平截断（Baskerville-Emin 积分）。"""
    if tupper is not None:
        tmax = min(tmax, tupper)
        tmin = min(tmin, tupper)
    if tmax <= tbase:
        return 0.0
    if tmin >= tbase:
        return (tmax + tmin) / 2.0 - tbase
    amplitude = (tmax - tmin) / 2.0
    if amplitude == 0.0:
        return 0.0
    tavg = (tmax + tmin) / 2.0
    theta = math.asin((tbase - tavg) / amplitude)
    return ((tavg - tbase) * (math.pi / 2.0 - theta) + amplitude * math.cos(theta)) / math.pi


def _corn_86_50(tmax_f, tmin_f, tbase=50.0, tupper=86.0):
    """NDAWN 玉米：下限 50°F、上限 86°F，抬低截高后再减 50。"""
    tmax_p = min(tmax_f, 86.0)
    tmin_p = max(tmin_f, 50.0)
    if tmax_p < 50.0:
        tmax_p = 50.0
    return (tmax_p + tmin_p) / 2.0 - 50.0


def _rice_dd50(tmax_f, tmin_f, tbase=50.0, tupper=94.0):
    """Arkansas DD50：>94 记 94，最低温 >70 记 70，再减 50，单日最多 32。"""
    tmax_p = min(tmax_f, 94.0)
    tmin_p = min(tmin_f, 70.0)
    value = (tmax_p + tmin_p) / 2.0 - 50.0
    if value < 0.0:
        value = 0.0
    if value > 32.0:
        value = 32.0
    return value


_METHOD_FUNCS = {
    "average_mean_reset": _average_mean_reset,
    "average_minmax_reset": _average_minmax_reset,
    "single_sine_horizontal": _sine_horizontal,
    "corn_86_50": _corn_86_50,
    "rice_dd50_arkansas": _rice_dd50,
}


def _gdd_for_day(method, tmax, tmin, tbase, tupper):
    func = _METHOD_FUNCS[method]
    if method in {"corn_86_50", "rice_dd50_arkansas"}:
        return func(tmax, tmin)
    return func(tmax, tmin, tbase, tupper)


# ---------------------------------------------------------------------------
# 试验估算入口
# ---------------------------------------------------------------------------

def _parse_day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def estimate_stage(config, records, *, stage_gdd, biofix_date, temperature_unit,
                   mode="demonstration"):
    """按已声明方法累积积温，返回达到阶段阈值的日期；关闭时不给任何日期。

    records: ``[{"date": "YYYY-MM-DD", "tmax": <num>, "tmin": <num>}, ...]``
    temperature_unit 必须与 config['tbase_unit'] 相同，否则关闭（禁止 C/F 混用）。
    """
    gate = gate_config(config, mode=mode)
    cid = config.get("config_id") if isinstance(config, dict) else None
    if not gate["enabled"]:
        return _closed(gate["reasons"], config_id=cid, kind="estimate")

    if not _is_number(stage_gdd) or float(stage_gdd) <= 0.0:
        return _closed([_reason("STAGE_THRESHOLD_REQUIRED", "stage_gdd 必须为正数")],
                       config_id=cid, kind="estimate")
    if temperature_unit != config.get("tbase_unit"):
        return _closed([_reason("TEMPERATURE_UNIT_MISMATCH",
                                "记录单位必须与配置 tbase_unit 一致，禁止 C/F 混用")],
                       config_id=cid, kind="estimate")
    biofix = _parse_day(biofix_date)
    if biofix is None:
        return _closed([_reason("BIOFIX_DATE_REQUIRED", "biofix_date 必须为 YYYY-MM-DD")],
                       config_id=cid, kind="estimate")
    if not isinstance(records, list) or not records:
        return _closed([_reason("RECORDS_REQUIRED", "records 必须为非空数组")],
                       config_id=cid, kind="estimate")

    method = config["calculation_method"]
    tbase = float(config["tbase"])
    tupper = float(config["tupper"]) if _is_number(config.get("tupper")) else None

    accumulated = 0.0
    crossing = None
    rows = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            return _closed([_reason("RECORD_NOT_OBJECT", f"records[{i}] 必须为对象")],
                           config_id=cid, kind="estimate")
        day = _parse_day(rec.get("date"))
        if day is None or day < biofix:
            continue
        tmax, tmin = rec.get("tmax"), rec.get("tmin")
        if not _is_number(tmax) or not _is_number(tmin):
            return _closed([_reason("RECORD_TEMPS_REQUIRED",
                                    f"records[{i}] 需要有限 tmax/tmin")],
                           config_id=cid, kind="estimate")
        if float(tmin) > float(tmax):
            return _closed([_reason("RECORD_TMIN_GT_TMAX", f"records[{i}] tmin 大于 tmax")],
                           config_id=cid, kind="estimate")
        daily = _gdd_for_day(method, float(tmax), float(tmin), tbase, tupper)
        accumulated += daily
        rows.append({"date": day.isoformat(), "gdd": round(daily, 6), "cumulative": round(accumulated, 6)})
        if crossing is None and accumulated >= float(stage_gdd):
            crossing = day.isoformat()

    fact_tag = "SYNTHETIC" if config.get("evidence_type") in {"SYNTHETIC", "IMPLEMENTATION_TEST"} \
        else config.get("evidence_type")
    return {
        "status": "ESTIMATED" if crossing else "NOT_REACHED",
        "enabled": True,
        "kind": "estimate",
        "config_id": cid,
        "method": method,
        "unit": config["heat_unit"],
        "stage_gdd": float(stage_gdd),
        "biofix_date": biofix.isoformat(),
        "crossing_date": crossing,
        "accumulated": round(accumulated, 6),
        "daily": rows,
        "fact_tag": fact_tag,
        "not_field_evidence": True,
        "locally_validated": config.get("local_validated") is True,
        "reasons": [],
    }
