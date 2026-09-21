"""地区×品种×种植方式×阶段 的农艺规则匹配；其他地区规则不得静默命中。

设计要点（R14-A02，原要求 U10：都昌先行、全国扩展须分地区证据）：

- 命中需要四项同时对上：``region_id``、``crop_id``（可再限定 ``cultivar_group``）、
  ``growing_system``、``stage_id``。缺任何一项都返回关闭路径，不做"就近"匹配。
- 只在同一 ``region_id`` 内查找。查不到本地规则即 ``NO_MATCH``；即使外地有同作物
  同阶段的规则，也不得自动顶替。跨地区借用必须由调用方显式声明
  ``allow_national_fallback=True``，且只在 ``national_candidate`` 里找，结果标为
  ``region_match="national_fallback"``、``local_evidence=false``、推荐保持关闭。
- 物候阶段、农事环节（YL.STAGE.*）、FAO-56 Kc 阶段（fao56.kc.*）分开；
  农事环节与 Kc 阶段不得冒充物候参与匹配。
- 水田（paddy）规则只对水稻成立；旱作查询不得套用水层规则。
- 品种限定行只有在查询显式给出同一 ``cultivar_group`` 时才命中；无品种不得借用。
- 所有数值参数保持 null：没有审核依据不填数字。

本模块只做匹配与隔离判断，不预测生育期、不生成剂量/农时/安全阈值，
也不改 ``yilao_agri``（``engine_loads=false``）。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

CATALOG_PATH = Path(__file__).with_name("region_stage_rules.json")

ALLOWED_QUERY = frozenset({
    "region_id", "crop_id", "stage_id", "growing_system",
    "cultivar_group", "allow_national_fallback",
})
AUTO_SELECT_KEYS = frozenset({
    "auto_select", "pick", "preferred", "preferred_rule_id", "rank", "score", "best", "top1",
})
COORDINATE_KEYS = frozenset({"latitude", "longitude", "lat", "lon", "coordinates", "gps"})
PARAM_NUMERIC_KEYS = ("max_temperature_c", "min_temperature_c", "max_precipitation_mm", "max_wind_m_s")
RICE_CROP_IDS = frozenset({"CROP-ENT-RICE"})
ALLOWED_EVIDENCE = frozenset({
    "none", "oral_mention", "catalog_only", "illustrative", "hold",
    "field_observation", "primary_source",
})

_CATALOG_CACHE = None


class RegionRuleError(ValueError):
    """规则目录或查询错误；code 供校验入口写入字段路径。"""

    def __init__(self, code, message, field=""):
        self.code = code
        self.message = message
        self.field = field
        prefix = f"{field}: " if field else ""
        super().__init__(f"{prefix}{code}: {message}")


# ---------------------------------------------------------------- 目录加载与校验

def load_catalog(path=None) -> dict:
    """读取规则目录；``path`` 可为 dict（夹具）、文件路径或省略（默认 JSON）。"""
    global _CATALOG_CACHE
    if isinstance(path, dict):
        catalog = deepcopy(path)
        validate_catalog(catalog)
        return catalog
    target = Path(path) if path else CATALOG_PATH
    if _CATALOG_CACHE is not None and path is None:
        return deepcopy(_CATALOG_CACHE)
    if not target.is_file():
        raise RegionRuleError("CATALOG_MISSING", f"找不到规则目录：{target}")
    catalog = json.loads(target.read_text(encoding="utf-8"))
    validate_catalog(catalog)
    if path is None:
        _CATALOG_CACHE = catalog
        return deepcopy(_CATALOG_CACHE)
    return catalog


def _index_regions(catalog):
    table = {}
    for i, row in enumerate(catalog.get("regions") or []):
        if not isinstance(row, dict):
            raise RegionRuleError("BAD_TYPE", "地区行必须为对象", f"regions[{i}]")
        rid = row.get("region_id")
        if not isinstance(rid, str) or not rid.strip():
            raise RegionRuleError("EMPTY_STRING", "region_id 不能为空", f"regions[{i}].region_id")
        if rid in table:
            raise RegionRuleError("DUPLICATE_REGION", f"重复 region_id {rid}", f"regions[{i}]")
        table[rid] = row
    if not table:
        raise RegionRuleError("BAD_TYPE", "regions 不能为空")
    return table


def _vocab_of(catalog, crop_id):
    vocab_id = (catalog.get("crop_vocab") or {}).get(crop_id)
    if not vocab_id:
        return None
    return (catalog.get("phenology_vocabularies") or {}).get(vocab_id)


def _non_phenology_family(catalog, stage_id):
    for name, spec in (catalog.get("non_phenology_stage_families") or {}).items():
        prefix = spec.get("prefix") if isinstance(spec, dict) else None
        if isinstance(prefix, str) and stage_id.startswith(prefix):
            return name
    return None


def validate_catalog(catalog):
    """目录自检。任何跨地区继承、推荐开启、环节冒充物候都在这里拦住。"""
    if not isinstance(catalog, dict):
        raise RegionRuleError("BAD_TYPE", "目录必须为对象")
    regions = _index_regions(catalog)
    for rid, row in regions.items():
        inherits = row.get("inherits_agronomy_from", [])
        if inherits:
            raise RegionRuleError(
                "REGION_RULE_CROSSWALK", f"{rid} 声明继承其他地区农艺规则", f"regions[{rid}]")
        if row.get("coordinates_assign_region") is True:
            raise RegionRuleError(
                "COORDINATE_ASSIGNED_REGION", f"{rid} 允许用经纬度指定地区", f"regions[{rid}]")
        if row.get("kind") not in {"intended_local", "national_candidate", "demonstration",
                                   "foreign_citation", "unresolved_local"}:
            raise RegionRuleError("BAD_TYPE", "未知地区类型", f"regions[{rid}].kind")
        if row.get("evidence_status") not in ALLOWED_EVIDENCE:
            raise RegionRuleError("BAD_TYPE", "未知证据状态", f"regions[{rid}].evidence_status")

    allowed_systems = set(catalog.get("growing_systems") or [])
    hold_ids = set(catalog.get("identity_hold_crop_ids") or [])
    seen = set()
    for i, rule in enumerate(catalog.get("rules") or []):
        if not isinstance(rule, dict):
            raise RegionRuleError("BAD_TYPE", "规则行必须为对象", f"rules[{i}]")
        path = f"rules[{i}]"
        rid = rule.get("region_id")
        if rid not in regions:
            raise RegionRuleError("REGION_UNKNOWN", f"未登记地区 {rid}", f"{path}.region_id")
        crop_id = rule.get("crop_id")
        if not isinstance(crop_id, str) or not crop_id.strip():
            raise RegionRuleError("EMPTY_STRING", "crop_id 不能为空", f"{path}.crop_id")
        if crop_id in hold_ids:
            raise RegionRuleError(
                "CROP_IDENTITY_LOCKED", "身份 HOLD 作物不得挂专属规则", f"{path}.crop_id")
        vocabs = catalog.get("phenology_vocabularies") or {}
        vocab_id = (catalog.get("crop_vocab") or {}).get(crop_id)
        if not vocab_id or vocab_id not in vocabs:
            raise RegionRuleError(
                "CROP_VOCAB_UNKNOWN", f"作物 {crop_id} 未登记物候词表", f"{path}.crop_id")
        stage_id = rule.get("stage_id")
        if not isinstance(stage_id, str) or not stage_id.strip():
            raise RegionRuleError("EMPTY_STRING", "stage_id 不能为空", f"{path}.stage_id")
        family = _non_phenology_family(catalog, stage_id)
        if family:
            raise RegionRuleError(
                "STAGE_VOCAB_CROSSWALK",
                f"{family} 阶段 {stage_id} 不是物候，不得参与匹配", f"{path}.stage_id")
        if stage_id not in (vocabs[vocab_id].get("stages") or []):
            raise RegionRuleError(
                "STAGE_CROP_FAMILY_MISMATCH",
                f"阶段 {stage_id} 不属于 {crop_id} 的词表 {vocab_id}", f"{path}.stage_id")
        system = rule.get("growing_system")
        if system not in allowed_systems:
            raise RegionRuleError("BAD_UNIT", f"未知种植方式 {system}", f"{path}.growing_system")
        if system == "paddy" and crop_id not in RICE_CROP_IDS:
            raise RegionRuleError(
                "PADDY_ON_NON_RICE", f"{crop_id} 不得用 paddy 生长方式", f"{path}.growing_system")
        if rule.get("recommendation_enabled") is True:
            raise RegionRuleError(
                "RECOMMENDATION_WITHOUT_EVIDENCE", "本版规则不得打开推荐", f"{path}.recommendation_enabled")
        if rule.get("transfer_allowed_to_duchang") is True:
            raise RegionRuleError(
                "TRANSFER_CLAIM_FORBIDDEN", "v0.1 禁止声称可迁移到都昌", f"{path}.transfer_allowed_to_duchang")
        params = rule.get("params")
        if not isinstance(params, dict):
            raise RegionRuleError("BAD_TYPE", "params 必须为对象", f"{path}.params")
        limits = params.get("weather_limits")
        if limits is not None:
            if not isinstance(limits, dict):
                raise RegionRuleError("BAD_TYPE", "weather_limits 必须为对象", f"{path}.params.weather_limits")
            for key in limits:
                if key not in PARAM_NUMERIC_KEYS:
                    raise RegionRuleError(
                        "UNKNOWN_FIELD", "未知天气限制字段", f"{path}.params.weather_limits.{key}")
        key = (rid, crop_id, rule.get("cultivar_group"), system, stage_id)
        if key in seen:
            raise RegionRuleError("DUPLICATE_RULE", f"重复规则键 {key}", path)
        seen.add(key)
    return True


# ---------------------------------------------------------------- 查询与匹配

def _validate_query(query):
    if not isinstance(query, dict):
        raise RegionRuleError("BAD_TYPE", "查询必须为对象")
    for key in query:
        if key in AUTO_SELECT_KEYS:
            raise RegionRuleError("AUTO_SELECT_FORBIDDEN", "禁止按排序、分数或首条规则自动选定", key)
        if key in COORDINATE_KEYS:
            raise RegionRuleError(
                "COORDINATE_ASSIGNED_REGION", "经纬度不得用于指定 region_id", key)
        if key not in ALLOWED_QUERY:
            raise RegionRuleError("UNKNOWN_FIELD", "未知字段", key)
    for field in ("region_id", "crop_id", "stage_id"):
        value = query.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RegionRuleError("EMPTY_STRING", f"{field} 必须为非空字符串", field)
    system = query.get("growing_system", "unknown")
    if not isinstance(system, str) or not system.strip():
        raise RegionRuleError("EMPTY_STRING", "growing_system 必须为非空字符串", "growing_system")
    cultivar = query.get("cultivar_group")
    if cultivar is not None and (not isinstance(cultivar, str) or not cultivar.strip()):
        raise RegionRuleError("EMPTY_STRING", "cultivar_group 必须为非空字符串或省略", "cultivar_group")
    fallback = query.get("allow_national_fallback", False)
    if not isinstance(fallback, bool):
        raise RegionRuleError("BAD_TYPE", "allow_national_fallback 必须为布尔值", "allow_national_fallback")


def _crop_vocab_id(catalog, crop_id):
    return (catalog.get("crop_vocab") or {}).get(crop_id)


def _stage_family(catalog, crop_id, stage_id):
    """返回 (family, code)。family 为 'crop' / 'farm_operation' / 'kc_water' / 'unknown_crop'。"""
    vocab_id = _crop_vocab_id(catalog, crop_id)
    if not vocab_id:
        return "unknown_crop", "CROP_VOCAB_UNKNOWN"
    family = _non_phenology_family(catalog, stage_id)
    if family:
        return family, "STAGE_VOCAB_CROSSWALK"
    stages = ((catalog.get("phenology_vocabularies") or {}).get(vocab_id) or {}).get("stages") or []
    if stage_id not in stages:
        return "mismatch", "STAGE_CROP_FAMILY_MISMATCH"
    return "crop", None


def _blank_params():
    return {
        "rates": None,
        "load_per_trip_kg": None,
        "doses": None,
        "windows": None,
        "water_rule_id": None,
        "weather_limits": {key: None for key in PARAM_NUMERIC_KEYS},
    }


def _closed(query, status, code, message, reasons=None, region_match="none"):
    return {
        "status": status,
        "region_match": region_match,
        "region_id": query.get("region_id"),
        "crop_id": query.get("crop_id"),
        "stage_id": query.get("stage_id"),
        "growing_system": query.get("growing_system", "unknown"),
        "cultivar_group": query.get("cultivar_group"),
        "rule": None,
        "candidate_rule_ids": [],
        "bound_operation_ids": [],
        "params": _blank_params(),
        "numbers_filled": False,
        "local_evidence": False,
        "recommendation_enabled": False,
        "requires_human_confirmation": True,
        "matched_key": None,
        "blocked": {"code": code, "message": message},
        "reasons": list(reasons or []) + [message],
    }


def match_rule(query, catalog=None) -> dict:
    """按地区+作物(+品种)+种植方式+阶段匹配一条农艺规则。

    未命中即关闭；不跨地区、不跨阶段、不跨方式、不借品种静默命中。
    """
    _validate_query(query)
    catalog = load_catalog(catalog)
    region_id = query["region_id"]
    crop_id = query["crop_id"]
    stage_id = query["stage_id"]
    system = query.get("growing_system", "unknown")
    cultivar = query.get("cultivar_group")
    fallback = bool(query.get("allow_national_fallback", False))

    regions = _index_regions(catalog)
    if region_id not in regions:
        return _closed(query, "NO_MATCH", "REGION_UNKNOWN",
                       f"地区 {region_id} 未登记；不得套用其他地区规则")
    if system not in set(catalog.get("growing_systems") or []):
        raise RegionRuleError("BAD_UNIT", f"未知种植方式 {system}", "growing_system")

    family, code = _stage_family(catalog, crop_id, stage_id)
    if family == "unknown_crop":
        return _closed(query, "BLOCKED", code,
                       f"作物 {crop_id} 未登记物候词表；不得匹配规则")
    if family in {"farm_operation", "kc_water"}:
        return _closed(query, "BLOCKED", code,
                       f"{stage_id} 属于{family}，不是作物物候，不得参与规则匹配")
    if family == "mismatch":
        vocab_id = _crop_vocab_id(catalog, crop_id)
        return _closed(query, "BLOCKED", code,
                       f"阶段 {stage_id} 不属于 {crop_id} 的词表 {vocab_id}")
    if system == "paddy" and crop_id not in RICE_CROP_IDS:
        return _closed(query, "BLOCKED", "PADDY_ON_NON_RICE",
                       f"{crop_id} 不是水稻，不得用水田规则")
    if system == "dryland" and crop_id in RICE_CROP_IDS:
        # 水稻查旱作方式：本地无水田规则可命中，且不得借水层规则。
        has_paddy = any(r.get("region_id") == region_id and r.get("crop_id") == crop_id
                        and r.get("growing_system") == "paddy"
                        for r in catalog.get("rules") or [])
        if has_paddy:
            return _closed(query, "BLOCKED", "RICE_DRYLAND_RULE_MIX",
                           "水稻按旱作方式查询；不得把水田水层规则套到旱地")

    def matches(rule, region):
        if rule.get("region_id") != region:
            return False
        if rule.get("crop_id") != crop_id:
            return False
        if rule.get("growing_system") != system:
            return False
        if rule.get("stage_id") != stage_id:
            return False
        r_cultivar = rule.get("cultivar_group")
        if r_cultivar is None:
            return True
        return r_cultivar == cultivar

    rules = catalog.get("rules") or []
    exact = [r for r in rules if matches(r, region_id)]

    if not exact:
        # 只看同地区是否存在"因为品种/方式/阶段没对上而被排除"的规则，用于解释，不用于替代。
        same_region = [r for r in rules if r.get("region_id") == region_id]
        same_stage = [r for r in same_region
                      if r.get("crop_id") == crop_id and r.get("stage_id") == stage_id]
        same_system = [r for r in same_stage if r.get("growing_system") == system]
        if cultivar is None and any(r.get("cultivar_group") is not None for r in same_system):
            return _closed(query, "BLOCKED", "CULTIVAR_REQUIRED",
                           "该规则只在指定品种下成立；未给出 cultivar_group 时不得套用")
        if same_stage:
            return _closed(query, "NO_MATCH", "NO_RULE_FOR_SYSTEM_OR_CULTIVAR",
                           "同地区同作物同阶段存在规则，但种植方式或品种未对上；不得就近借用",
                           reasons=[f"接近但未命中的规则：{', '.join(sorted(r['rule_id'] for r in same_stage))}"])
        if fallback and region_id != "national_candidate":
            nat = [r for r in rules if matches(r, "national_candidate")]
            if nat:
                rule = sorted(nat, key=lambda r: r["rule_id"])[0]
                return _result(query, rule, "national_fallback",
                               ["仅命中全国候选行，不得当作都昌已确认"])
        other = sorted({r.get("region_id") for r in rules
                        if r.get("crop_id") == crop_id and r.get("stage_id") == stage_id
                        and r.get("region_id") != region_id})
        reasons = []
        if other:
            reasons.append(f"其他地区存在同作物同阶段规则（{', '.join(other)}），按地区隔离不予命中")
        return _closed(query, "NO_MATCH", "NO_LOCAL_RULE",
                       f"未找到 {region_id} 本地规则；不得用其他地区规则代替", reasons=reasons)

    # 品种限定优先：显式给出品种时先取品种行，否则取不限定行。
    if cultivar is not None:
        cultivar_rows = [r for r in exact if r.get("cultivar_group") == cultivar]
        if cultivar_rows:
            exact = cultivar_rows
    rule = sorted(exact, key=lambda r: r["rule_id"])[0]
    return _result(query, rule, "exact", [])


def _result(query, rule, region_match, extra_reasons):
    params = deepcopy(_blank_params())
    source_params = rule.get("params") or {}
    for key, value in source_params.items():
        if key == "weather_limits":
            limits = deepcopy(_blank_params()["weather_limits"])
            for limit_key, limit_value in (value or {}).items():
                if limit_key in limits:
                    limits[limit_key] = limit_value
            params["weather_limits"] = limits
        elif key in params:
            params[key] = value
    local = region_match == "exact"
    result = {
        "status": "MATCHED_LOCAL" if local else "MATCHED_NATIONAL_FALLBACK",
        "region_match": region_match,
        "region_id": query["region_id"],
        "crop_id": query["crop_id"],
        "stage_id": query["stage_id"],
        "growing_system": query.get("growing_system", "unknown"),
        "cultivar_group": query.get("cultivar_group"),
        "rule": {
            "rule_id": rule["rule_id"],
            "region_id": rule["region_id"],
            "crop_id": rule["crop_id"],
            "cultivar_group": rule.get("cultivar_group"),
            "growing_system": rule.get("growing_system"),
            "stage_id": rule.get("stage_id"),
            "stage_status": rule.get("stage_status"),
            "evidence_status": rule.get("evidence_status"),
            "review_status": rule.get("review_status"),
            "sources": list(rule.get("sources") or []),
            "note": rule.get("note"),
        },
        "candidate_rule_ids": [rule["rule_id"]],
        "bound_operation_ids": sorted(rule.get("bound_operation_ids") or []),
        "params": params,
        "numbers_filled": False,
        "local_evidence": local,
        "recommendation_enabled": False,
        "requires_human_confirmation": True,
        "matched_key": {
            "region_id": rule["region_id"],
            "crop_id": rule["crop_id"],
            "cultivar_group": rule.get("cultivar_group"),
            "growing_system": rule.get("growing_system"),
            "stage_id": rule.get("stage_id"),
        },
        "blocked": None,
        "reasons": [],
    }
    if not local:
        result["reasons"] = list(extra_reasons) + ["全国候选不是都昌证据；不得据此打开推荐"]
    if not result["bound_operation_ids"]:
        result["reasons"].append("该规则未绑定任何农事环节")
    if all(value is None for value in [params["rates"], params["load_per_trip_kg"],
                                       params["doses"], params["windows"]]) \
            and all(value is None for value in params["weather_limits"].values()):
        result["reasons"].append("所有数值参数仍为 null：缺少有依据的本地参数")
    return result


def matched_or_none(result):
    """只有本地命中的规则才算可用；全国回退与关闭路径一律返回 None。"""
    if not isinstance(result, dict):
        return None
    if result.get("status") != "MATCHED_LOCAL":
        return None
    rule = result.get("rule") or {}
    return rule.get("rule_id")
