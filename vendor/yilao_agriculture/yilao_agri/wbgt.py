"""WBGT 研究插件（R13-A08；原要求 U14：WBGT 与其他热指标的方法、适用域与测量）。

本模块只做三件事，不改动 environment.py / models.py / engine.py 的既有硬约束：

1. 受控方法词表：把外部声明的 wbgt_method 字符串归一到受控方法码，并显式拒绝
   被前轮证据淘汰的近似（sWBGT、湿球冒充、HI 冒充、室外 Bernard、未读全文的 Dimiceli）。
2. 关闭的估计接口：Liljegren 室外估计因缺 surface_pressure_hpa、wind_height_m、
   solar_exposure 与地块坐标绑定而保持 CLOSED；estimate_outdoor_wbgt() 只返回
   缺口清单，绝不返回 WBGT 数值，也不写 wbgt_c。
3. 缺项表：missing_inputs_report() 输出「方法 × 关键输入 × 当前状态」，供主控整合。

事实边界：WBGT 是环境筛查指数，不是个人可劳动时长；程序不认证所声明的方法；
估计或预报 WBGT 不是田间实测。本模块只被合成夹具验证，不是现场或健康证据。
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "0.1-r13a08"

# 自由填写的方法字符串 → 受控方法码。空/占位单独处理，未命中即未识别，不默认放行。
_ALIASES: dict[str, tuple[str, ...]] = {
    "M01_iso7243_instrument": (
        "iso 7243", "iso7243", "iso-7243", "natural wet bulb + globe", "globe thermometer",
        "作业点仪器",
    ),
    "M02_liljegren_outdoor_estimate": (
        "liljegren", "osha outdoor wbgt calculator", "osha wbgt calculator",
    ),
    "M08_external_declared": (
        "external", "provider declared", "provider-declared", "由提供者声明的估计方法",
        "外部声明",
    ),
    "M04_abm_swbgt": (
        "swbgt", "abm", "bom", "australian bureau", "bureau of meteorology",
    ),
    "M06_wet_bulb_as_wbgt": (
        "psychrometric wet bulb", "wet bulb only", "wet_bulb_as_wbgt", "湿球", "湿球温度",
    ),
    "M07_heat_index_as_wbgt": (
        "heat index", "hi as wbgt", "noaa hi", "热指数",
    ),
    "M03_bernard_indoor": ("bernard",),
    "M05_dimiceli_globe": ("dimiceli",),
}

# 受控词表：状态、可否携带 wbgt_c、关键输入与来源。来源 ID 与 R04 证据登记一致。
METHODS: dict[str, dict[str, Any]] = {
    "M01_iso7243_instrument": {
        "title": "作业点仪器 WBGT（ISO 7243:2017 / NIOSH 2016-106 §9.3.2）",
        "status": "REFERENCE_MEASUREMENT",
        "accepts_wbgt_c": True,
        "wbgt_kind_if_used": "measured",
        "required_inputs": [
            "natural_wet_bulb_c", "globe_temperature_c", "dry_bulb_c",
            "instrument_at_work_location", "calibration_and_placement", "plot_bound_coordinates",
        ],
        "adoption_condition": "获准田间仪器、校准、日晒一致、坐标绑定后方可作为参考测量；仍不得当作个人健康安全",
        "source_ids": ["ISO-7243-2017-OBP", "NIOSH-2016-106", "OSHA-HEAT-HAZARD-RECOGNITION"],
    },
    "M02_liljegren_outdoor_estimate": {
        "title": "Liljegren 2008 室外气象估计（OSHA Outdoor WBGT Calculator 算法）",
        "status": "CANDIDATE_ESTIMATE_CLOSED",
        "accepts_wbgt_c": False,
        "wbgt_kind_if_used": "forecast_estimate",
        "required_inputs": [
            "dry_bulb_c", "relative_humidity_pct", "wind_m_s", "wind_height_m",
            "shortwave_w_m2_or_solar_geometry", "surface_pressure_hpa", "solar_exposure",
            "plot_bound_coordinates",
        ],
        "adoption_condition": "补齐关键字段并以作业点仪器评估环境误差后由主控放开；缺任一关键输入不得写 wbgt_c",
        "source_ids": ["LILJEGREN-2008", "OSHA-WBGT-CALCULATOR", "KONG-2024-ANALYTIC", "CLARK-2024-ACCURACY"],
    },
    "M08_external_declared": {
        "title": "外部声明方法的 wbgt_c 输入（现行 v0.1.0 路径）",
        "status": "KEEP_CURRENT_PATH",
        "accepts_wbgt_c": True,
        "wbgt_kind_if_used": "as_declared",
        "required_inputs": ["wbgt_c_every_record", "non_placeholder_wbgt_method", "weather_source"],
        "adoption_condition": "保留现行路径；method 收成受控词表后应拒绝淘汰近似",
        "source_ids": ["INTERFACE-1.0"],
    },
    "M04_abm_swbgt": {
        "title": "澳大利亚气象局 sWBGT 温湿近似",
        "status": "ELIMINATE",
        "accepts_wbgt_c": False,
        "wbgt_kind_if_used": None,
        "required_inputs": ["dry_bulb_c", "water_vapour_pressure_hpa"],
        "adoption_condition": "禁止写入 wbgt_c；只能作带标签的研究对照，不作筛查依据",
        "source_ids": ["BOM-THERMAL-STRESS", "LEMKE-KJELLSTROM-2012"],
    },
    "M06_wet_bulb_as_wbgt": {
        "title": "把（导出）湿球温度当作 WBGT",
        "status": "ELIMINATE",
        "accepts_wbgt_c": False,
        "wbgt_kind_if_used": None,
        "required_inputs": ["psychrometric_wet_bulb_c"],
        "adoption_condition": "禁止；接口约定已禁止把湿球温度冒充 WBGT",
        "source_ids": ["INTERFACE-1.0", "NIOSH-2016-106", "LEMKE-KJELLSTROM-2012"],
    },
    "M07_heat_index_as_wbgt": {
        "title": "用 NOAA Heat Index 冒充 WBGT",
        "status": "ELIMINATE",
        "accepts_wbgt_c": False,
        "wbgt_kind_if_used": None,
        "required_inputs": ["temperature_c", "relative_humidity_pct"],
        "adoption_condition": "禁止；HI 只描述阴影静息温湿组合，不含风、日晒与负荷",
        "source_ids": ["OSHA-HEAT-HAZARD-RECOGNITION", "NOAA-WPC-HEAT-INDEX"],
    },
    "M03_bernard_indoor": {
        "title": "Bernard & Pourmoghani 1999 室内估计",
        "status": "ELIMINATE_AS_OUTDOOR_DEFAULT",
        "accepts_wbgt_c": False,
        "wbgt_kind_if_used": None,
        "required_inputs": ["psychrometric_wet_bulb_c", "globe_temperature_c"],
        "adoption_condition": "不含阳光下黑球；仅无太阳负荷且有依据的室内/棚室可再评估",
        "source_ids": ["BERNARD-1999-ABSTRACT", "LEMKE-KJELLSTROM-2012"],
    },
    "M05_dimiceli_globe": {
        "title": "Dimiceli/NWS 黑球估计再合成 WBGT",
        "status": "HOLD_FULL_TEXT",
        "accepts_wbgt_c": False,
        "wbgt_kind_if_used": None,
        "required_inputs": [
            "air_temperature", "solar_irradiance", "wind_speed", "solar_zenith",
            "surface_albedo", "direct_diffuse_fraction", "natural_wet_bulb_estimator",
        ],
        "adoption_condition": "未读全文不实现；不优先于 Liljegren",
        "source_ids": ["DIMICELI-2011-ABSTRACT", "RCC-WP-25-001-SECONDARY"],
    },
}

# 与 environment.py 一致的占位判别：空/未知字符串不构成方法声明。
_PLACEHOLDERS = frozenset({
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未知", "待定", "待核实", "待补充", "待填写",
})

# 关键输入的当前可得性（当前 schema 与已探明的县城网格为准，不含未接通数据）。
_INPUT_AVAILABILITY: dict[str, str] = {
    "dry_bulb_c": "schema",
    "temperature_c": "schema",
    "relative_humidity_pct": "schema",
    "wind_m_s": "schema",
    "shortwave_w_m2_or_solar_geometry": "schema_partial",
    "water_vapour_pressure_hpa": "derivable_from_schema",
    "wind_height_m": "missing",
    "surface_pressure_hpa": "missing",
    "solar_exposure": "missing",
    "plot_bound_coordinates": "missing",
    "globe_temperature_c": "missing",
    "natural_wet_bulb_c": "missing",
    "psychrometric_wet_bulb_c": "derivable_from_schema",
    "instrument_at_work_location": "missing",
    "calibration_and_placement": "missing",
    "wbgt_c_every_record": "schema",
    "non_placeholder_wbgt_method": "schema",
    "weather_source": "schema",
}

# 本卡明确不可声称的结论（防止把环境筛查写成个人安全）。
NON_CLAIMABLE = (
    "WBGT 本身是环境指数，不是个人可劳动时长或个人发病概率。",
    "排程 allowed 或搜到会话不等于健康效果或现场安全。",
    "估计/预报 WBGT 不是田间实测；程序不认证所声明的方法。",
    "演示或合成夹具通过不等于都昌现场误差或推广有效性。",
)


def _normalize(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def is_placeholder(value: Any) -> bool:
    text = _normalize(value)
    return text is None or text in _PLACEHOLDERS


def classify_method(value: Any) -> dict[str, Any]:
    """把方法字符串归一到受控方法码；占位/未识别不默认放行。"""
    text = _normalize(value)
    if is_placeholder(value):
        return {
            "input": value,
            "method_id": None,
            "status": "METHOD_PLACEHOLDER",
            "accepts_wbgt_c": False,
            "code": "WBGT_METHOD_PLACEHOLDER",
            "message": "未提供可识别的方法声明，不能据此携带 WBGT。",
            "source_ids": [],
        }
    for method_id, aliases in _ALIASES.items():
        if any(alias in text for alias in aliases):
            meta = METHODS[method_id]
            return {
                "input": value,
                "method_id": method_id,
                "status": meta["status"],
                "accepts_wbgt_c": meta["accepts_wbgt_c"],
                "code": "WBGT_METHOD_CLASSIFIED",
                "message": meta["title"],
                "source_ids": list(meta["source_ids"]),
            }
    return {
        "input": value,
        "method_id": None,
        "status": "METHOD_UNRECOGNIZED",
        "accepts_wbgt_c": False,
        "code": "WBGT_METHOD_UNRECOGNIZED",
        "message": "方法字符串不在受控词表中，未审核前不得作为 WBGT 来源。",
        "source_ids": [],
    }


def is_admissible_method(value: Any) -> bool:
    """仅受控词表中允许携带 wbgt_c 的方法返回 True。"""
    return bool(classify_method(value)["accepts_wbgt_c"])


def _present_inputs(record: dict, context: dict | None) -> tuple[list[str], list[str]]:
    """按候选估计方法的必填项，检查 record/context 中已给出的输入，返回 (已有, 缺失)。"""
    context = context or {}
    available: set[str] = set()
    for field in ("temperature_c", "relative_humidity_pct", "wind_m_s", "shortwave_w_m2"):
        if record.get(field) is not None:
            available.add(field)
    for field in ("wind_height_m", "surface_pressure_hpa", "solar_exposure",
                  "latitude", "longitude", "timezone"):
        if context.get(field) is not None:
            available.add(field)
    if ("latitude" in available and "longitude" in available and "timezone" in available):
        available.add("plot_bound_coordinates")
    if "shortwave_w_m2" in available or "plot_bound_coordinates" in available:
        available.add("shortwave_w_m2_or_solar_geometry")
    # 只有 record 的干球才能满足估计的 dry_bulb_c。
    if "temperature_c" in available:
        available.add("dry_bulb_c")

    required = [i for i in METHODS["M02_liljegren_outdoor_estimate"]["required_inputs"]]
    present = [i for i in required if i in available]
    missing = [i for i in required if i not in available]
    return present, missing


def estimate_outdoor_wbgt(
    record: dict, *, method: str = "liljegren_outdoor_estimate", context: dict | None = None
) -> dict[str, Any]:
    """关闭的估计接口：只报告缺口，绝不返回 WBGT 数值。

    - 非候选方法（含被淘汰的近似）直接拒绝，并给出该方法码。
    - 候选方法即使全部输入齐备，本轮仍返回 CLOSED，由主控在有获准字段与评价后放开。
    """
    verdict = classify_method(method)
    if verdict["method_id"] != "M02_liljegren_outdoor_estimate":
        return {
            "ok": False,
            "code": "WBGT_ESTIMATOR_METHOD_NOT_CANDIDATE",
            "method_id": verdict["method_id"],
            "method_status": verdict["status"],
            "wbgt_c": None,
            "missing_inputs": [],
            "present_inputs": [],
            "note": "本接口只服务受控候选估计方法；其他近似不得用于 WBGT。",
        }
    present, missing = _present_inputs(record, context)
    return {
        "ok": False,
        "code": "WBGT_ESTIMATE_INTERFACE_CLOSED",
        "method_id": "M02_liljegren_outdoor_estimate",
        "method_status": "CANDIDATE_ESTIMATE_CLOSED",
        "wbgt_c": None,
        "missing_inputs": missing,
        "present_inputs": present,
        "note": "接口保持关闭：不返回数值、不写 wbgt_c、不把估计当实测；放开需主控补字段并评价环境误差。",
    }


def check_series_method(weather: dict) -> dict[str, Any]:
    """对单个 weather 序列做方法审核（供 environment.py 将来调用，本卡不改环境模块）。

    只在记录已携带 wbgt_c 时判定：占位/未识别/淘汰方法一律不允许携带 WBGT。
    """
    series = weather if isinstance(weather, dict) else {}
    method = series.get("wbgt_method")
    carries = any(
        isinstance(record, dict) and record.get("wbgt_c") is not None
        for record in (series.get("records") or [])
    )
    verdict = classify_method(method)
    ok = (not carries) or bool(verdict["accepts_wbgt_c"])
    return {
        "ok": ok,
        "carries_wbgt": carries,
        "method_id": verdict["method_id"],
        "method_status": verdict["status"],
        "code": "WBGT_METHOD_ADMISSIBLE" if ok else "WBGT_METHOD_NOT_ADMISSIBLE",
    }


def missing_inputs_report() -> dict[str, Any]:
    """方法 × 关键输入 × 当前状态，供主控整合为缺项表。"""
    rows: list[dict[str, Any]] = []
    for method_id, meta in METHODS.items():
        for item in meta["required_inputs"]:
            rows.append({
                "method_id": method_id,
                "method_status": meta["status"],
                "required_input": item,
                "availability": _INPUT_AVAILABILITY.get(item, "unknown"),
                "accepts_wbgt_c": meta["accepts_wbgt_c"],
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "rows": rows,
        "closed_methods": sorted(
            mid for mid, meta in METHODS.items() if meta["status"] == "CANDIDATE_ESTIMATE_CLOSED"
        ),
        "eliminated_methods": sorted(
            mid for mid, meta in METHODS.items() if meta["status"].startswith("ELIMINATE")
        ),
        "non_claimable": list(NON_CLAIMABLE),
    }
