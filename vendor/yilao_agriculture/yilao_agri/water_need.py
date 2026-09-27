"""需水检查试验接口（R14-A08；原要求 U15：灌排、积温、需水须先满足输入条件）。

本卡目标：**只在土壤水量等条件齐备时估算；结果不直接生成浇水处方。**

本模块只做四件事，不改动 engine / environment / models / weather / coverage /
recalc / wbgt 的既有硬约束，也不向它们写入：

1. 受控声明词表：把调用方想声称的“需水结论”收成受控码，并把无依据的推导
   （ET0 当灌溉、ETc 当浇水量、降水量当有效雨、水稻套旱作恒等式、静默把 10 m 风
   当 2 m、把训练例毫米当地方默认、棚室当标准条件）标为 ELIMINATE。
2. 分层输入门：L1 参考蒸散 / L2 标准作物蒸散 / L3 土壤水量 / L4 水量平衡 /
   L5 田间实浇，逐层列出必填项、是否齐备与缺口。条件不齐时**只报缺口，不估算**。
3. 关闭的估计接口：``estimate_water_need()`` 只在输入门放行（L1–L4 齐备）时给出
   “是否要去田里看水”的检查判定；``water_need_mm`` 与
   ``irrigation_prescription_mm`` **恒为 None**，绝不把估算变成浇水处方。
4. 缺项表：``missing_inputs_report()`` 输出「层 × 关键输入 × 当前可得性」，供主控整合。

事实边界：FAO-56 的 ET0 是参考草面蒸散，ETc=Kc·ET0 是**标准条件**下的作物蒸散，
两者都不等于要浇的水；净灌溉深 I 是水量平衡项（FAO-56 第 8 章），田间实浇体积还
需要灌水方法效率与输水损失。都昌的 Kc、θFC/θWP/Zr、有效雨方法、灌溉史与效率
尚未取得（现场 n_real=0）。本模块只被合成夹具与程序测试验证，**不是现场、预报
精度或健康证据**，也不认证所声明的来源。
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "0.1-r14a08"

# 本模块永远不生成的输出：浇水处方 / 需水毫米。写成常量便于测试与审计引用。
PRESCRIPTION_ALWAYS_NULL = True
PRESCRIPTION_FIELD = "irrigation_prescription_mm"

# ---------------------------------------------------------------- 分层定义

# 每层的必填项与「为什么需要」。availability 只描述当前 schema 1.0 的宽口径，
# 不代表某次调用已经给了。layers 是结构核对，不是都昌参数。
LAYERS: dict[str, dict[str, Any]] = {
    "L1_reference_et0": {
        "title": "参考草面蒸散 ET0（FAO-56 第 2/3/4 章）",
        "meaning": "参考蒸散，不是作物需水，更不是浇水量。",
        "required_inputs": ["et0_mm", "et0_source", "et0_method"],
        "source_ids": ["SRC-FAO56-CH2", "SRC-FAO56-CH4", "SRC-OPENMETEO-ET0"],
    },
    "L2_standard_etc": {
        "title": "标准条件作物蒸散 ETc = Kc × ET0（FAO-56 第 5 章式 56）",
        "meaning": "大田、无病、肥水适宜、根区水分不受限的**标准条件**下的作物蒸散。",
        "required_inputs": ["kc", "kc_source", "standard_conditions", "environment"],
        "source_ids": ["SRC-FAO56-CH5", "SRC-FAO56-CH1"],
    },
    "L3_soil_water": {
        "title": "土壤水量（根区含水与可耗竭水量，FAO-56 第 8 章）",
        "meaning": "TAW=1000(θFC−θWP)Zr、RAW=p·TAW、Dr 由初始含水与逐日平衡给出。",
        "required_inputs": ["theta_fc", "theta_wp", "zr_m", "p_fraction", "dr_initial_mm"],
        "source_ids": ["SRC-FAO56-CH8"],
    },
    "L4_water_balance": {
        "title": "水量平衡项（P/Pe/I/DP，FAO-56 第 8 章式 85）",
        "meaning": "Dr,i = Dr,i−1 −(P−RO)i −Ii −CRi +ETc,i +DPi；I 是入渗净深不是实浇体积。",
        "required_inputs": [
            "precipitation_mm", "pe_method", "irrigation_history_declared",
            "deep_percolation_declared",
        ],
        "source_ids": ["SRC-FAO56-CH8", "SRC-FAO-S2022E-IN"],
    },
    "L5_field_application": {
        "title": "田间实浇体积（灌水方法与效率；FAO-56 第 8 章）",
        "meaning": "把净灌溉深换成实浇/实抽还需要效率、输水损失与淋洗，本模块不采用任何默认效率。",
        "required_inputs": ["application_efficiency", "irrigation_method"],
        "source_ids": ["SRC-FAO56-CH8"],
    },
}

# 会放行「检查判定」的最低层集合：L1–L4 齐备（L5 只影响实浇体积声称）。
GATE_LAYERS = ("L1_reference_et0", "L2_standard_etc", "L3_soil_water", "L4_water_balance")

# 当前 schema 1.0 与已探明来源下的宽口径可得性（仅结构说明，不含未接通数据）。
_INPUT_AVAILABILITY: dict[str, str] = {
    "et0_mm": "schema_absent_provider_only",
    "et0_source": "schema",
    "et0_method": "schema",
    "kc": "missing",
    "kc_source": "missing",
    "standard_conditions": "missing",
    "environment": "schema",
    "theta_fc": "missing",
    "theta_wp": "missing",
    "zr_m": "missing",
    "p_fraction": "missing",
    "dr_initial_mm": "missing",
    "precipitation_mm": "schema",
    "pe_method": "missing",
    "irrigation_history_declared": "missing",
    "deep_percolation_declared": "missing",
    "application_efficiency": "missing",
    "irrigation_method": "missing",
    "ponding_depth_cm_or_awd_reading": "missing",
}

# ---------------------------------------------------------------- 受控声明词表

# 允许作为「标签/结构声称」的声明码。
ALLOWED_CLAIMS: dict[str, str] = {
    "grass_reference_et0": "L1 参考草面蒸散标签（供应商 ET0 只可作此用途）。",
    "et0_fao56": "按 FAO-56 计算的参考蒸散（本模块不计算，只登记）。",
    "etc_standard": "标准条件下的作物蒸散 ETc=Kc·ET0。",
    "etc_adjusted": "水分胁迫下的 ETc_adj=Ks·Kc·ET0。",
    "net_irrigation_identity": "净灌溉深作为水量平衡项（不是实浇体积）。",
    "check_field_water": "只提示去田里看水（本模块唯一可产出的输出）。",
}

# 被淘汰的推导：字符串 → (受控码, 说明)。
ELIMINATED_CLAIMS: dict[str, tuple[str, str]] = {
    "et0_as_irrigation": ("REJECT_ET0_AS_IRRIGATION", "参考蒸散不是浇水量。"),
    "etc_as_irrigation": ("REJECT_ETC_AS_IRRIGATION", "作物蒸散不是浇水量。"),
    "p_as_pe": ("REJECT_P_AS_PE", "时段降水不是有效雨 Pe。"),
    "rice_dryland_identity": ("REJECT_RICE_DRYLAND_IDENTITY", "水稻不能用 IN=ETcrop−Pe。"),
    "silent_wind_conversion": ("REJECT_WIND_HEIGHT_UNDECLARED", "10 m 风不得静默当 2 m 风。"),
    "local_default_dose": ("REJECT_LOCAL_DEFAULT_DOSE", "训练例毫米不得当地方默认值。"),
    "greenhouse_as_standard": ("REJECT_GREENHOUSE_AS_STANDARD", "棚室不是标准条件大田。"),
}

_CLAIM_ALIASES: dict[str, tuple[str, ...]] = {
    "grass_reference_et0": ("grass reference et0", "vendor et0", "参考蒸散", "et0 label"),
    "et0_fao56": ("fao56 et0", "fao-56 et0", "penman-monteith"),
    "etc_standard": ("etc", "etc_standard", "kc x et0", "kc*et0", "作物蒸散"),
    "etc_adjusted": ("etc_adj", "ks kc et0", "水分胁迫"),
    "net_irrigation_identity": ("net irrigation", "净灌溉", "water balance", "水量平衡"),
    "check_field_water": ("check field water", "看水", "检查田水", "提醒检查"),
    "et0_as_irrigation": ("et0 as irrigation", "et0 当浇水", "直接浇 et0"),
    "etc_as_irrigation": ("etc as irrigation", "etc 当浇水", "按 etc 浇"),
    "p_as_pe": ("p as pe", "降水当有效雨", "precipitation as effective rain"),
    "rice_dryland_identity": ("rice dryland", "水稻套旱作", "in=etcrop-pe"),
    "silent_wind_conversion": ("silent wind", "静默换风高", "10m to 2m"),
    "local_default_dose": ("local default dose", "地方默认毫米", "训练例当默认"),
    "greenhouse_as_standard": ("greenhouse standard", "棚室当标准"),
}

_PLACEHOLDERS = frozenset({
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未知", "待定", "待核实", "待补充", "待填写",
})

# 本卡明确不可声称的结论（防止把结构核对写成农艺或健康结论）。
NON_CLAIMABLE = (
    "参考蒸散 ET0 不是作物需水；作物蒸散 ETc 不是要浇的水量。",
    "净灌溉深 I 是水量平衡项，不等于田间实浇体积；本模块不采用任何默认效率。",
    "条件齐备时也只产出“是否要去田里看水”的检查判定，不产出浇水处方。",
    "程序通过不等于都昌现场精度、排程已支持水稻灌排或个人健康安全。",
)


def _normalize(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def is_placeholder(value: Any) -> bool:
    text = _normalize(value)
    return text is None or text in _PLACEHOLDERS


def _finite(value: Any) -> float | None:
    """只接受有限非负数值；bool 是 int 的子类，不能冒充测量值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")) or result < 0:
        return None
    return result


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------- 声明分类


def classify_claim(value: Any) -> dict[str, Any]:
    """把声称字符串归一到受控码；占位/未识别/被淘汰都不默认放行。"""
    text = _normalize(value)
    if is_placeholder(value):
        return {
            "input": value, "claim_id": None, "status": "CLAIM_PLACEHOLDER",
            "allowed": False, "code": "WATER_CLAIM_PLACEHOLDER",
            "message": "未提供可识别的声称，不能据此输出任何需水结论。",
        }
    # 先判定被淘汰的推导，再做允许标签匹配：避免 "etc as irrigation" 被 "etc" 抢先。
    ordered_ids = list(ELIMINATED_CLAIMS) + [c for c in ALLOWED_CLAIMS]
    for claim_id in ordered_ids:
        aliases = _CLAIM_ALIASES.get(claim_id, ())
        if text == claim_id or any(alias in text for alias in aliases):
            if claim_id in ELIMINATED_CLAIMS:
                code, why = ELIMINATED_CLAIMS[claim_id]
                return {
                    "input": value, "claim_id": claim_id, "status": "ELIMINATE",
                    "allowed": False, "code": code, "message": why,
                }
            return {
                "input": value, "claim_id": claim_id, "status": "ALLOWED_AS_LABEL",
                "allowed": True, "code": "WATER_CLAIM_ALLOWED_AS_LABEL",
                "message": ALLOWED_CLAIMS[claim_id],
            }
    return {
        "input": value, "claim_id": None, "status": "CLAIM_UNRECOGNIZED",
        "allowed": False, "code": "WATER_CLAIM_UNRECOGNIZED",
        "message": "声称不在受控词表中，未审核前不得作为需水结论。",
    }


def is_admissible_claim(value: Any) -> bool:
    """仅受控词表中「允许作标签/结构声称」的字符串返回 True。"""
    return bool(classify_claim(value)["allowed"])


# ---------------------------------------------------------------- 输入门


def _present(value: Any) -> bool:
    """输入是否「已给出且非占位」：None / 占位字符串 / 非有限数都不算。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return not is_placeholder(value)
    if isinstance(value, (int, float)):
        return _finite(value) is not None
    return False


# 这些键必须是显式 True 才算「已声明」，False 与缺失同样不放行。
_DECLARED_TRUE_KEYS = frozenset({
    "standard_conditions", "irrigation_history_declared", "deep_percolation_declared",
})


def _layer_missing(layer_id: str, payload: dict) -> tuple[list[str], list[str]]:
    required = LAYERS[layer_id]["required_inputs"]
    present, missing = [], []
    for key in required:
        if key in _DECLARED_TRUE_KEYS:
            ok = payload.get(key) is True
        else:
            ok = _present(payload.get(key))
        if ok:
            present.append(key)
        else:
            missing.append(key)
    # 水稻水层是旱作以外**额外**的必填项，缺了不算齐备（不填 SAT/PERC/WL 默认值）。
    if layer_id == "L3_soil_water" and payload.get("crop_system") == "paddy":
        extra = "ponding_depth_cm_or_awd_reading"
        if _present(payload.get(extra)):
            present.append(extra)
        else:
            missing.append(extra)
    return present, missing


def gate(payload: dict) -> dict[str, Any]:
    """分层输入门：逐层列出必填项、是否齐备与缺口。**不做任何估算。**"""
    payload = payload if isinstance(payload, dict) else {}
    layers: dict[str, Any] = {}
    all_missing: list[str] = []
    all_present: list[str] = []
    gate_missing: list[str] = []
    for layer_id in LAYERS:
        present, missing = _layer_missing(layer_id, payload)
        layers[layer_id] = {
            "title": LAYERS[layer_id]["title"],
            "present_inputs": present,
            "missing_inputs": missing,
            "complete": not missing,
        }
        all_present.extend(present)
        all_missing.extend(missing)
        if layer_id in GATE_LAYERS:
            gate_missing.extend(missing)
    gate_complete = all(layers[layer_id]["complete"] for layer_id in GATE_LAYERS)
    return {
        "complete": gate_complete,
        "soil_water_complete": layers["L3_soil_water"]["complete"],
        "environment_complete": layers["L1_reference_et0"]["complete"]
        and layers["L2_standard_etc"]["complete"],
        "layers": layers,
        "present_inputs": sorted(set(all_present)),
        "missing_inputs": sorted(set(gate_missing)),
        "missing_non_gate": sorted(set(all_missing) - set(gate_missing)),
    }


# ---------------------------------------------------------------- 淘汰推导的探测


def reject_codes(payload: dict) -> list[str]:
    """逐条拒绝无依据的推导。只读 payload，不修改它。"""
    payload = payload if isinstance(payload, dict) else {}
    codes: list[str] = []

    verdict = classify_claim(payload.get("claim"))
    if verdict["status"] in {"ELIMINATE", "CLAIM_PLACEHOLDER", "CLAIM_UNRECOGNIZED"}:
        codes.append(verdict["code"])

    et0 = _finite(payload.get("et0_mm"))
    kc = _finite(payload.get("kc"))
    etc = _finite(payload.get("etc_mm"))
    pe = _finite(payload.get("pe_mm"))
    precip = _finite(payload.get("precipitation_mm"))
    irrig = _finite(payload.get("irrigation_mm"))
    system = payload.get("crop_system")

    if (payload.get("et0_source") or "") in {"open-meteo et0_fao_evapotranspiration",
                                             "open-meteo-et0_fao_evapotranspiration"}:
        codes.append("REJECT_MODEL_ET_AS_FAO_ET0")
        if payload.get("need_from_et0_declared"):
            codes.append("REJECT_ET0_AS_IRRIGATION")

    if etc is not None and et0 is not None and _close(etc, et0):
        codes.append("REJECT_ET0_AS_ETC")
    if etc is not None and et0 is not None and kc is not None and not _close(etc, et0 * kc):
        codes.append("REJECT_ETC_NOT_KC_ET0")

    if precip is not None and pe is not None and _close(precip, pe) and not _present(payload.get("pe_method")):
        codes.append("REJECT_P_AS_PE")

    if system == "paddy" and payload.get("declared_identity") == "IN=ETcrop-Pe":
        codes.append("REJECT_RICE_DRYLAND_IDENTITY")
    if system == "paddy" and irrig is not None and etc is not None and _close(irrig, etc):
        codes.append("REJECT_RICE_ETC_AS_IN")

    if irrig is not None and etc is not None and _close(irrig, etc):
        codes.append("REJECT_ETC_AS_IRRIGATION")
    if irrig is not None and et0 is not None and _close(irrig, et0):
        codes.append("REJECT_ET0_AS_IRRIGATION")

    if payload.get("uses_wind") and payload.get("wind_height_m") not in (2, 2.0) \
            and not payload.get("wind_adjusted_to_2m"):
        codes.append("REJECT_WIND_HEIGHT_UNDECLARED")

    if payload.get("environment") == "greenhouse" and payload.get("standard_conditions"):
        codes.append("REJECT_GREENHOUSE_AS_STANDARD")

    if payload.get("training_mm") is not None and payload.get("training_as_local_default"):
        codes.append("REJECT_LOCAL_DEFAULT_DOSE")

    if payload.get(PRESCRIPTION_FIELD) is not None:
        codes.append("REJECT_PRESCRIPTION_REQUESTED")

    seen, ordered = set(), []
    for code in codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered


# ---------------------------------------------------------------- 关闭的估计接口


def estimate_water_need(payload: dict) -> dict[str, Any]:
    """需水检查试验接口：只在 L1–L4 齐备时给“是否要去田里看水”的检查判定。

    硬不变量：``water_need_mm`` 与 ``irrigation_prescription_mm`` 在任何路径下都是
    None；即使输入齐备也只给检查判定，绝不生成浇水处方。
    """
    payload = payload if isinstance(payload, dict) else {}
    codes = reject_codes(payload)
    holes = gate(payload)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "water_need_mm": None,
        PRESCRIPTION_FIELD: None,
        "prescription_generated": False,
        "codes": codes,
        "missing_inputs": holes["missing_inputs"],
        "missing_non_gate": holes["missing_non_gate"],
        "present_inputs": holes["present_inputs"],
        "layer_status": {k: v["complete"] for k, v in holes["layers"].items()},
        "check": None,
    }
    if codes:
        result["status"] = "WATER_NEED_CLAIM_REJECTED"
        result["note"] = "存在无依据的推导，接口关闭：不估算、不产出处方。"
        return result
    if not holes["complete"]:
        result["status"] = "WATER_NEED_INPUTS_INCOMPLETE"
        result["note"] = "土壤水量等输入未齐备：只报缺口，不估算、不产出处方。"
        return result

    et0 = _finite(payload.get("et0_mm"))
    kc = _finite(payload.get("kc"))
    theta_fc = _finite(payload.get("theta_fc"))
    theta_wp = _finite(payload.get("theta_wp"))
    zr = _finite(payload.get("zr_m"))
    p_frac = _finite(payload.get("p_fraction"))
    dr = _finite(payload.get("dr_initial_mm"))
    etc = kc * et0
    taw = 1000.0 * (theta_fc - theta_wp) * zr
    raw = p_frac * taw
    above_raw = dr >= raw
    result["status"] = "WATER_NEED_CHECK_ONLY"
    result["check"] = {
        "etc_mm": round(etc, 6),
        "taw_mm": round(taw, 6),
        "raw_mm": round(raw, 6),
        "depletion_mm": round(dr, 6),
        "depletion_at_or_above_raw": above_raw,
        "recommend_field_check": above_raw,
        "output_kind": "check_field_water",
    }
    result["note"] = (
        "输入齐备时也只给检查判定（是否要去田里看水）；"
        "water_need_mm 与 irrigation_prescription_mm 保持 None，不生成浇水处方。"
    )
    return result


def check_series_water(weather: dict) -> dict[str, Any]:
    """序列级核对：记录若携带 ET0 字段，只能当参考蒸散标签，不得当需水/浇水。

    本卡不改 environment/weather，仅供主控整合时调用。
    """
    series = weather if isinstance(weather, dict) else {}
    records = series.get("records") or []
    carries_et0 = any(
        isinstance(record, dict) and record.get("et0_fao_evapotranspiration") is not None
        for record in records
    )
    return {
        "ok": True,
        "carries_et0": carries_et0,
        "code": "WATER_ET0_FIELD_IS_REFERENCE_LABEL",
        "message": (
            "et0_fao_evapotranspiration 只是参考草面蒸散标签，"
            "不得当作物需水或灌溉毫米。"
        ),
        PRESCRIPTION_FIELD: None,
    }


# ---------------------------------------------------------------- 缺项表


def missing_inputs_report() -> dict[str, Any]:
    """层 × 关键输入 × 当前可得性，供主控整合为缺项表。"""
    rows: list[dict[str, Any]] = []
    for layer_id, meta in LAYERS.items():
        for item in meta["required_inputs"]:
            rows.append({
                "layer_id": layer_id,
                "layer_title": meta["title"],
                "required_input": item,
                "availability": _INPUT_AVAILABILITY.get(item, "unknown"),
                "in_gate": layer_id in GATE_LAYERS,
            })
    rows.append({
        "layer_id": "L3_soil_water",
        "layer_title": LAYERS["L3_soil_water"]["title"],
        "required_input": "ponding_depth_cm_or_awd_reading",
        "availability": _INPUT_AVAILABILITY["ponding_depth_cm_or_awd_reading"],
        "in_gate": True,
        "note": "仅 crop_system=paddy 时额外必填。",
    })
    return {
        "schema_version": SCHEMA_VERSION,
        "rows": rows,
        "gate_layers": list(GATE_LAYERS),
        "prescription_always_null": PRESCRIPTION_ALWAYS_NULL,
        "non_claimable": list(NON_CLAIMABLE),
    }
