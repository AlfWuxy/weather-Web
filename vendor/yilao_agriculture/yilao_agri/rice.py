"""水稻专有分支：水层检查与稻作阶段接口。

设计边界（R14-A06）：

* 稻田水层是独立输入条件，**不复用旱作浇水规则**：旱作需水 ``IN = ETcrop - Pe``
  与水稻 ``IN = ETcrop + SAT + PERC + WL - Pe`` 是两条不同身份；本模块只认水稻这条，
  并显式拒绝把旱作公式、面积或看守分钟搬进稻田。
* 生育期是**接口**而不是自由文本：阶段必须能解析到已登记的稻作阶段；多候选或未知时
  保持未解析，不替用户选一个。
* 缺地方审核的水层参数时**关闭能力**（HOLD）：只返回“到田检查”，
  ``irrigation_need_mm`` 恒为 ``None``，绝不编造毫米需水、也不给出工许可。

依据（目录覆盖不等于已验证）：
R06-A09 水稻阶段/操作目录、R07-A05 环节 09 灌排目录、R09-A05 水层状态与输入门提案、
R13 接受基线。FAO/IRRI 摘录的厘米与毫米只作来源，不作为都昌默认。
"""

from __future__ import annotations

from datetime import datetime
import math

__all__ = [
    "INTERFACE_VERSION",
    "RiceWaterError",
    "WATER_SYSTEMS",
    "WATER_REGIMES",
    "STAGE_GROUPS",
    "STAGES",
    "RICE_WATER_OPERATIONS",
    "RULES",
    "FORBIDDEN_DEFAULTS",
    "resolve_rice_stage",
    "water_need_identity",
    "rice_stage_interface",
    "local_parameters_gate",
    "check_water_layer",
]

INTERFACE_VERSION = "yilao.rice.water_layer.v1.0"
SOURCE_BASIS = (
    "R06-A09 rice stage/operation catalog; R07-A05 farm-operation 09 catalog; "
    "R09-A05 rice water-layer states proposal; R13 accepted baseline. "
    "FAO/IRRI excerpt values are sources only, never local defaults."
)

# 水系统身份：旱作与稻田必须分开，绝不能互相复制。
WATER_SYSTEMS = ("unknown", "dryland", "paddy_water_layer", "aquatic_vegetable_hold")

# 稻田灌排制度；unknown 表示未登记，不得用全国默认。
WATER_REGIMES = (
    "unknown",
    "not_applicable",
    "continuous_flood",
    "awd",
    "midseason_drain",
    "preharvest_drain",
    "nursery",
)
PADDY_REGIMES = frozenset(WATER_REGIMES) - {"unknown", "not_applicable"}

RICE_CROP_IDS = frozenset({
    "rice", "paddy", "oryza", "oryza_sativa", "水稻", "crop-ent-rice", "g01",
})
AQUATIC_CROP_IDS = frozenset({
    "lotus", "water_spinach", "water spinach", "水生蔬菜", "v12",
})
DRYLAND_FORMULAS = frozenset({
    "in=etcrop-pe", "in=etc-pe", "etcrop-pe", "etc-pe", "etcrop - pe",
})
PADDY_FORMULA = "IN = ETcrop + SAT + PERC + WL - Pe"
DRYLAND_FORMULA = "IN = ETcrop - Pe"

# R09-A05 提出、R14 采用为禁用默认值；任何一处取单值都视为未审核默认。
FORBIDDEN_DEFAULTS = (200, 100, 4, 5, 6, 8)

STAGE_GROUPS = (
    {"id": "STAGEGRP-RICE-NURSERY", "label_zh": "秧田/育秧", "label_en": "nursery",
     "unit": ("plant", "sqm"), "support_status": "CATALOG_ONLY"},
    {"id": "STAGEGRP-RICE-VEGETATIVE", "label_zh": "营养生长", "label_en": "vegetative",
     "unit": ("mu", "sqm"), "support_status": "CATALOG_ONLY"},
    {"id": "STAGEGRP-RICE-REPRODUCTIVE", "label_zh": "生殖生长", "label_en": "reproductive",
     "unit": ("mu", "sqm"), "support_status": "CATALOG_ONLY"},
    {"id": "STAGEGRP-RICE-RIPENING", "label_zh": "灌浆成熟", "label_en": "ripening",
     "unit": ("kg",), "support_status": "CATALOG_ONLY"},
)

# 稻作阶段接口：IRRI SES 1-9 与开花单列（均来自 R06-A09 阶段表）。
STAGES = (
    {"id": "STAGE-RICE-SES-1", "ses": 1, "label_zh": "萌发", "label_en": "germination",
     "group": "STAGEGRP-RICE-NURSERY", "aliases": ("germination", "萌发", "ses1", "ses-1", "1")},
    {"id": "STAGE-RICE-SES-2", "ses": 2, "label_zh": "幼苗/秧苗", "label_en": "seedling",
     "group": "STAGEGRP-RICE-NURSERY", "aliases": ("seedling", "幼苗", "秧苗", "ses2", "ses-2", "2")},
    {"id": "STAGE-RICE-SES-3", "ses": 3, "label_zh": "分蘖", "label_en": "tillering",
     "group": "STAGEGRP-RICE-VEGETATIVE", "aliases": ("tillering", "tiller", "分蘖", "ses3", "ses-3", "3")},
    {"id": "STAGE-RICE-SES-4", "ses": 4, "label_zh": "拔节", "label_en": "stem_elongation",
     "group": "STAGEGRP-RICE-VEGETATIVE",
     "aliases": ("stem_elongation", "stem elongation", "拔节", "ses4", "ses-4", "4")},
    {"id": "STAGE-RICE-SES-5", "ses": 5, "label_zh": "孕穗", "label_en": "booting",
     "group": "STAGEGRP-RICE-REPRODUCTIVE",
     "aliases": ("booting", "boot", "孕穗", "ses5", "ses-5", "5")},
    {"id": "STAGE-RICE-SES-6", "ses": 6, "label_zh": "抽穗", "label_en": "heading",
     "group": "STAGEGRP-RICE-REPRODUCTIVE",
     "aliases": ("heading", "head", "抽穗", "ses6", "ses-6", "6")},
    {"id": "STAGE-RICE-FLOWERING", "ses": 6, "label_zh": "开花/扬花", "label_en": "flowering",
     "group": "STAGEGRP-RICE-REPRODUCTIVE",
     "aliases": ("flowering", "flower", "anthesis", "开花", "扬花", "ses6", "ses-6", "6")},
    {"id": "STAGE-RICE-SES-7", "ses": 7, "label_zh": "乳熟", "label_en": "milk",
     "group": "STAGEGRP-RICE-RIPENING", "aliases": ("milk", "乳熟", "ses7", "ses-7", "7")},
    {"id": "STAGE-RICE-SES-8", "ses": 8, "label_zh": "蜡熟", "label_en": "dough",
     "group": "STAGEGRP-RICE-RIPENING", "aliases": ("dough", "蜡熟", "ses8", "ses-8", "8")},
    {"id": "STAGE-RICE-SES-9", "ses": 9, "label_zh": "完熟", "label_en": "mature_grain",
     "group": "STAGEGRP-RICE-RIPENING",
     "aliases": ("mature_grain", "mature grain", "完熟", "ses9", "ses-9", "9")},
)

# 抽穗开花期对田水敏感：只提醒检查，不写入公顷/厘米默认阈值。
FLOWERING_SENSITIVE = frozenset({"STAGE-RICE-SES-6", "STAGE-RICE-FLOWERING"})

# 水稻专有灌排操作：劳动口径（operate/watch/wait_equipment）与旱地净速率分列。
RICE_WATER_OPERATIONS = {
    "OP09-RICE-INLET": "operate",
    "OP09-RICE-DRAIN": "operate",
    "OP09-RICE-BUND-SEAL": "operate",
    "OP09-RICE-CHANNEL": "operate",
    "OP09-RICE-START-PUMP": "operate",
    "OP09-RICE-NURSERY-WATER": "operate",
    "OP09-RICE-MIDSEASON-DRAIN": "operate",
    "OP09-RICE-PREHARVEST-DRAIN": "operate",
    "OP09-RICE-AWD-OPTIONAL": "operate",
    "WATCH09-RICE-HOLD": "watch",
    "WATCH09-RICE-BUND-INSPECT": "watch",
    "WATCH09-RICE-FILL": "watch",
    "WATCH09-RICE-HEADING-FLOWER": "watch",
    "WATCH09-RICE-AWD-TUBE": "watch",
    "WAIT09-RICE-CANAL-TURN": "wait_equipment",
    "WAIT09-RICE-PUMP-SERVICE": "wait_equipment",
    "WAIT09-RICE-DRAIN-WINDOW": "wait_equipment",
}

RULES = (
    {"id": "R-RICE-01", "code": "DRYLAND_REUSE_FORBIDDEN",
     "text": "作物为水稻或 water_system=paddy_water_layer 时，禁止 IN=ETcrop-Pe 与 copy_from_dryland。"},
    {"id": "R-RICE-02", "code": "CHECK_FIELD_WATER",
     "text": "ponding_depth_cm 与 AWD 管读数皆缺时只输出 CHECK_FIELD_WATER，irrigation_need_mm=null。"},
    {"id": "R-RICE-03", "code": "HOLD_NO_DOSE",
     "text": "即使已观察水层，没有地方审核的水层平衡也不得输出毫米需水。"},
    {"id": "R-RICE-04", "code": "CHECK_FLOWERING_WATER",
     "text": "抽穗扬花缺田水只追加检查提醒，不把 IRRI 厘米写成都昌阈值或健康安全结论。"},
    {"id": "R-RICE-05", "code": "LABOR_KIND_SPLIT",
     "text": "操作/看守/等待设备继续分口径；看守分钟不得当净速率。"},
    {"id": "R-RICE-06", "code": "CROSS_CROP_FORBIDDEN",
     "text": "水生蔬菜不得套水稻公式；旱作缺田面水层不走水稻检查。"},
    {"id": "R-RICE-07", "code": "AWD_NATIONAL_ONLY",
     "text": "AWD 仅全国候选，都昌未确认时不得启用。"},
    {"id": "R-RICE-08", "code": "STAGE_INTERFACE_REQUIRED",
     "text": "生育期须解析到已登记阶段；多候选或未知时保持未解析，不自动选定。"},
)

_ACTION_MESSAGES = {
    "CHECK_FIELD_WATER": "缺少田面水层或搁田管读数，只提醒到田检查，不算灌水量。",
    "CHECK_AWD_TUBE": "声明间歇灌溉但没有田水管读数，只提醒检查水管，不套旱地浇水。",
    "CHECK_REGIME": "灌排制度未知（连续淹水/AWD/中期排水/收获前排水），不能用全国默认。",
    "CHECK_FLOWERING_WATER": "抽穗扬花对缺水敏感，仍只提醒检查田水，不填厘米默认。",
    "HOLD_NO_DOSE": "即使已有田水观察，没有地方审核的水层平衡也不能算毫米需水。",
    "CHECK_STAGE": "稻作生育期未解析，先确认阶段再做水层判断。",
    "CHECK_OBSERVATION_TIME": "有水深但缺观察时刻，观察视为未完成。",
    "AWD_REGIME_NOT_ENABLED": "AWD 仅为全国候选，都昌未审核，不得启用。",
    "WATER_LAYER_BELOW_TARGET": "观察水层低于该阶段已审核目标下限，需复核田水，不下灌水指令。",
    "WATER_LAYER_ABOVE_TARGET": "观察水层高于该阶段已审核目标上限，需复核排水，不下灌水指令。",
    "WATER_LAYER_WITHIN_TARGET": "观察水层落在该阶段已审核目标区间内；仍不是出工许可。",
}

_REJECT_MESSAGES = {
    "COPY_FROM_DRYLAND": "禁止把旱作浇水模板套到水稻水层。",
    "DRYLAND_FORMULA_ON_PADDY": "水稻需水不是 IN=ETcrop-Pe。",
    "RICE_LABELLED_AS_DRYLAND": "作物是水稻时不能把水系统标成旱作。",
    "UNREVIEWED_FAO_TRAINING_DEFAULT": "FAO 训练 SAT/PERC/WL 假定值不是都昌参数。",
    "PLOT_AREA_AS_DOSE": "田块面积不能冒充灌水量。",
    "WATCH_MINUTES_AS_NET_RATE": "看守或等水分钟不能当旱地净作业速率。",
    "DOSE_WITHOUT_LOCAL_WATER_BALANCE": "没有地方审核的水层平衡，不得输出或接受毫米需水。",
    "COPY_FROM_RICE_TO_AQUATIC": "水生蔬菜不得套水稻 SAT/PERC/WL 公式。",
}

_PLACEHOLDER_SOURCES = frozenset({
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "未确认", "未知", "待定", "待核实", "待补充", "待填写",
})


class RiceWaterError(ValueError):
    """水稻水层输入的结构错误；code/field 供上层写成字段路径。"""

    def __init__(self, code, message, field=""):
        self.code = code
        self.message = message
        self.field = field
        prefix = f"{field}: " if field else ""
        super().__init__(f"{prefix}{code}: {message}")


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    return " ".join(str(value).strip().casefold().split())


def _is_placeholder(value) -> bool:
    return not isinstance(value, str) or _norm(value) in _PLACEHOLDER_SOURCES


def _finite_number(value, field):
    if value is None:
        return None
    if isinstance(value, bool):
        raise RiceWaterError("BAD_TYPE", "必须为有限数值", field)
    if not isinstance(value, (int, float)):
        raise RiceWaterError("BAD_TYPE", "必须为有限数值", field)
    if not math.isfinite(float(value)):
        raise RiceWaterError("BAD_TYPE", "必须为有限数值", field)
    return float(value)


def _parse_instant(value, field):
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            raise RiceWaterError("BAD_TYPE", "必须为含时区的 ISO 8601 时间", field) from None
    else:
        raise RiceWaterError("BAD_TYPE", "必须为含时区的 ISO 8601 时间", field)
    if result.tzinfo is None or result.utcoffset() is None:
        raise RiceWaterError("BAD_TYPE", "必须包含时区偏移", field)
    return result


# --------------------------------------------------------------------------
# 阶段接口
# --------------------------------------------------------------------------

_STAGE_BY_ID = {stage["id"]: stage for stage in STAGES}
_ALIAS_INDEX = {}
for _stage in STAGES:
    for _alias in (_stage["id"], _stage["label_en"], *_stage["aliases"]):
        _ALIAS_INDEX.setdefault(_norm(_alias), set()).add(_stage["id"])


def resolve_rice_stage(value) -> dict:
    """把阶段输入解析到已登记的稻作阶段。

    多候选或未知一律保持未解析（``resolved=False``、``stage_id=None``），
    与作物别名同策略：不取首条、不猜、不把自由文本当阶段。
    """
    text = _norm(value)
    result = {
        "input": value if isinstance(value, (str, int)) and not isinstance(value, bool) else None,
        "resolved": False,
        "stage_id": None,
        "ses": None,
        "group": None,
        "flowering_sensitive": False,
        "candidate_stage_ids": [],
        "requires_human_confirmation": True,
        "reasons": [],
    }
    if not text:
        result["reasons"].append("阶段为空，未解析")
        return result
    # 纯数字按 SES 编码处理（"3" 与 "ses-3" 同入口）。
    candidates = _ALIAS_INDEX.get(text)
    if candidates is None:
        result["reasons"].append("未登记的稻作阶段文本；不得猜测")
        return result
    ordered = sorted(candidates)
    result["candidate_stage_ids"] = list(ordered)
    if len(ordered) > 1:
        result["reasons"].append("多个阶段候选（抽穗与扬花同 SES=6），不自动选定")
        return result
    stage = _STAGE_BY_ID[ordered[0]]
    result.update({
        "resolved": True,
        "stage_id": stage["id"],
        "ses": stage["ses"],
        "group": stage["group"],
        "flowering_sensitive": stage["id"] in FLOWERING_SENSITIVE,
        "requires_human_confirmation": False,
    })
    return result


def _flowering_from_candidates(candidate_ids) -> bool:
    return any(stage_id in FLOWERING_SENSITIVE for stage_id in candidate_ids)


# --------------------------------------------------------------------------
# 需水身份
# --------------------------------------------------------------------------

def water_need_identity(water_system) -> dict:
    """返回需水身份；旱作与稻田互为禁止复制。"""
    system = water_system if water_system in WATER_SYSTEMS else "unknown"
    if system == "paddy_water_layer":
        return {
            "water_system": system,
            "identity_id": "WNEED-RICE-ET-SAT-PERC-WL",
            "formula": PADDY_FORMULA,
            "copy_to_dryland": False,
            "copy_from_dryland": False,
            "dose_supported": False,
            "note": "SAT/PERC/WL 需地方农技审核；未审核不得取单值。",
        }
    if system == "dryland":
        return {
            "water_system": system,
            "identity_id": "WNEED-DRY-ET-PE",
            "formula": DRYLAND_FORMULA,
            "copy_to_dryland": False,
            "copy_from_dryland": False,
            "dose_supported": False,
            "note": "旱作需水走既有旱作路径；不得套用到稻田。",
        }
    if system == "aquatic_vegetable_hold":
        return {
            "water_system": system,
            "identity_id": "WNEED-AQUATIC-HOLD",
            "formula": None,
            "copy_to_dryland": False,
            "copy_from_dryland": False,
            "dose_supported": False,
            "note": "水生蔬菜既非旱作也非水稻，保持 HOLD。",
        }
    return {
        "water_system": "unknown",
        "identity_id": "WNEED-UNKNOWN",
        "formula": None,
        "copy_to_dryland": False,
        "copy_from_dryland": False,
        "dose_supported": False,
        "note": "水系统未知，无身份可用。",
    }


# --------------------------------------------------------------------------
# 地方参数闸门
# --------------------------------------------------------------------------

def local_parameters_gate(local_parameters, mode="shadow") -> dict:
    """判断是否可用地方审核参数；缺审核即关闭（HOLD）。

    只有 ``review_status=confirmed`` 且来源可辨识才启用；
    demonstration 额外允许 ``illustrative``，但结果标记为非采纳。
    """
    mode = mode if mode in {"demonstration", "shadow", "assistance"} else "shadow"
    gate = {
        "enabled": False,
        "adoptable": False,
        "reason": "",
        "stage_targets": {},
        "awd_enabled": False,
    }
    if not isinstance(local_parameters, dict):
        gate["reason"] = "缺少地方审核参数，水层检查保持 HOLD。"
        return gate
    source = local_parameters.get("source")
    if _is_placeholder(source):
        gate["reason"] = "地方参数来源为占位或缺失，水层检查保持 HOLD。"
        return gate
    review = local_parameters.get("review_status")
    if review == "confirmed":
        gate["enabled"] = True
        gate["adoptable"] = True
    elif review == "illustrative" and mode == "demonstration":
        gate["enabled"] = True
        gate["adoptable"] = False
        gate["reason"] = "演示参数：可跑路径，不是都昌已审核规则。"
    else:
        gate["reason"] = "地方参数未确认，水层检查保持 HOLD。"
        return gate
    targets = local_parameters.get("stage_targets")
    if targets is not None:
        if not isinstance(targets, dict):
            raise RiceWaterError("BAD_TYPE", "stage_targets 必须为对象", "stage_targets")
        clean = {}
        for stage_id, spec in targets.items():
            if stage_id not in _STAGE_BY_ID:
                raise RiceWaterError("STAGE_UNKNOWN", f"未登记阶段 {stage_id}", "stage_targets")
            if not isinstance(spec, dict):
                raise RiceWaterError("BAD_TYPE", "阶段目标必须为对象", f"stage_targets.{stage_id}")
            low = _finite_number(spec.get("low_cm"), f"stage_targets.{stage_id}.low_cm")
            high = _finite_number(spec.get("high_cm"), f"stage_targets.{stage_id}.high_cm")
            if low is None or high is None or low > high:
                raise RiceWaterError("BAD_RANGE", "阶段目标需 low_cm <= high_cm", f"stage_targets.{stage_id}")
            clean[stage_id] = {"low_cm": low, "high_cm": high}
        if clean:
            gate["stage_targets"] = clean
    if local_parameters.get("awd_enabled") is True:
        gate["awd_enabled"] = True
    return gate


# --------------------------------------------------------------------------
# 主检查
# --------------------------------------------------------------------------

def _is_rice(observation) -> bool:
    system = observation.get("water_system")
    crop = _norm(observation.get("crop_id"))
    if system == "paddy_water_layer":
        return True
    if system in {"aquatic_vegetable_hold"}:
        return False
    return crop in RICE_CROP_IDS


def _is_aquatic(observation) -> bool:
    system = observation.get("water_system")
    crop = _norm(observation.get("crop_id"))
    return system == "aquatic_vegetable_hold" or crop in AQUATIC_CROP_IDS


def check_water_layer(observation, *, local_parameters=None, mode="shadow") -> dict:
    """检查一条稻田水层观察。

    永不返回灌水量或毫米需水；``irrigation_need_mm`` 恒为 ``None``。
    结构错误抛 :class:`RiceWaterError`，语义误用返回 ``REJECT``。
    """
    if not isinstance(observation, dict):
        raise RiceWaterError("BAD_TYPE", "observation 必须为对象")
    mode = mode if mode in {"demonstration", "shadow", "assistance"} else "shadow"

    system = observation.get("water_system")
    if system is None:
        system = "unknown"
    if system not in WATER_SYSTEMS:
        raise RiceWaterError("BAD_TYPE", "water_system 不在允许集合", "water_system")
    regime = observation.get("regime") or "unknown"
    if regime not in WATER_REGIMES:
        raise RiceWaterError("BAD_TYPE", "regime 不在允许集合", "regime")
    ponding = _finite_number(observation.get("ponding_depth_cm"), "ponding_depth_cm")
    tube = _finite_number(observation.get("awd_tube_cm_below_surface"), "awd_tube_cm_below_surface")
    proposed = _finite_number(observation.get("proposed_irrigation_need_mm"), "proposed_irrigation_need_mm")
    _parse_instant(observation.get("observed_at"), "observed_at")

    stage = resolve_rice_stage(observation.get("stage"))
    rice = _is_rice(observation)
    aquatic = _is_aquatic(observation)

    actions: list[dict] = []
    rejects: list[dict] = []

    def add_action(code):
        if all(item["code"] != code for item in actions):
            actions.append({"code": code, "message": _ACTION_MESSAGES[code]})

    def add_reject(code):
        if all(item["code"] != code for item in rejects):
            rejects.append({"code": code, "message": _REJECT_MESSAGES[code]})

    formula = _norm(observation.get("formula_used"))
    water_layer_context = rice or aquatic

    # 反例：把旱作规则搬进稻田。
    if rice and system == "dryland":
        add_reject("RICE_LABELLED_AS_DRYLAND")
    if rice and observation.get("copy_from_dryland") is True:
        add_reject("COPY_FROM_DRYLAND")
    if rice and formula in DRYLAND_FORMULAS:
        add_reject("DRYLAND_FORMULA_ON_PADDY")
    if water_layer_context and observation.get("use_fao_training_defaults") is True:
        add_reject("UNREVIEWED_FAO_TRAINING_DEFAULT")
    if water_layer_context and observation.get("use_plot_area_as_dose") is True:
        add_reject("PLOT_AREA_AS_DOSE")
    if water_layer_context and observation.get("watch_minutes_as_net_rate") is True:
        add_reject("WATCH_MINUTES_AS_NET_RATE")
    if aquatic and (observation.get("copy_from_rice") is True or formula == _norm(PADDY_FORMULA)):
        add_reject("COPY_FROM_RICE_TO_AQUATIC")
    # 任何未审核的毫米需水都拒绝（含仅数字、无地方平衡）。
    if water_layer_context and proposed is not None:
        add_reject("DOSE_WITHOUT_LOCAL_WATER_BALANCE")

    gate = local_parameters_gate(local_parameters, mode)

    if rice:
        if not stage["resolved"]:
            add_action("CHECK_STAGE")
        missing_ponding = ponding is None
        if missing_ponding and tube is None:
            add_action("CHECK_FIELD_WATER")
        if regime not in PADDY_REGIMES:
            add_action("CHECK_REGIME")
        if regime == "awd":
            if tube is None:
                add_action("CHECK_AWD_TUBE")
            if not gate["awd_enabled"]:
                add_action("AWD_REGIME_NOT_ENABLED")
        if (stage["flowering_sensitive"] or _flowering_from_candidates(stage["candidate_stage_ids"])) \
                and missing_ponding:
            add_action("CHECK_FLOWERING_WATER")
        if not missing_ponding:
            if observation.get("observed_at") is None:
                add_action("CHECK_OBSERVATION_TIME")
            add_action("HOLD_NO_DOSE")

    # 只有解析出唯一步骤、且地方参数闸门打开且该阶段有目标时才比对。
    checked = False
    if rice and gate["enabled"] and stage["resolved"] and ponding is not None:
        target = gate["stage_targets"].get(stage["stage_id"])
        if target is not None:
            checked = True
            if ponding < target["low_cm"]:
                add_action("WATER_LAYER_BELOW_TARGET")
            elif ponding > target["high_cm"]:
                add_action("WATER_LAYER_ABOVE_TARGET")
            else:
                add_action("WATER_LAYER_WITHIN_TARGET")

    if not rice and not aquatic:
        status = "NOT_RICE_PATH"
    elif rejects:
        status = "REJECT"
    elif not rice:
        status = "NOT_RICE_PATH"
    elif checked:
        status = "CHECKED_AGAINST_LOCAL_TARGETS"
    elif ponding is None and tube is None:
        status = "CHECK_ONLY"
    else:
        status = "HOLD_NO_DOSE"

    # 能力开关是**提案**，最终由主控按证据赋权；无真实田水来源前只到演示。
    capability = {
        "rice_water_layer_check": "ENABLED_FOR_DEMO" if gate["enabled"] else "HOLD",
        "rice_irrigation_dose": "UNSUPPORTED",
        "duchang_rice_irrigation_recommendation": "UNSUPPORTED",
        "copy_dryland_to_rice": "UNSUPPORTED",
    }

    notes = [
        "本工具不输出灌水量或毫米需水；irrigation_need_mm 恒为 null。",
        "提醒检查不是出工许可，也不是个人健康安全结论。",
        "目录覆盖不等于已验证；FAO/IRRI 摘录值不是都昌默认。",
    ]
    if gate["reason"]:
        notes.append(gate["reason"] or "")

    return {
        "interface_version": INTERFACE_VERSION,
        "water_system": system,
        "regime": regime,
        "crop_is_rice": rice,
        "crop_is_aquatic": aquatic,
        "stage": stage,
        "capability": capability,
        "status": status,
        "allowed_to_compute_dose": False,
        "irrigation_need_mm": None,
        "observed_ponding_depth_cm": ponding,
        "observed_awd_tube_cm_below_surface": tube,
        "action_codes": [item["code"] for item in actions],
        "reject_codes": [item["code"] for item in rejects],
        "actions": actions,
        "rejects": rejects,
        "water_need_identity": water_need_identity(system),
        "local_parameters": {"enabled": gate["enabled"], "adoptable": gate["adoptable"]},
        "notes": [note for note in notes if note],
    }


# --------------------------------------------------------------------------
# 接口描述
# --------------------------------------------------------------------------

def rice_stage_interface() -> dict:
    """机器可读接口描述：阶段、制度、操作口径、规则与能力开关。"""
    return {
        "interface_version": INTERFACE_VERSION,
        "source_basis": SOURCE_BASIS,
        "field_n": 0,
        "recommendation_enabled": False,
        "water_systems": list(WATER_SYSTEMS),
        "water_regimes": list(WATER_REGIMES),
        "stage_groups": [dict(group) for group in STAGE_GROUPS],
        "stages": [
            {"id": stage["id"], "ses": stage["ses"], "label_zh": stage["label_zh"],
             "label_en": stage["label_en"], "group": stage["group"],
             "flowering_sensitive": stage["id"] in FLOWERING_SENSITIVE}
            for stage in STAGES
        ],
        "rice_water_operations": dict(sorted(RICE_WATER_OPERATIONS.items())),
        "rules": [dict(rule) for rule in RULES],
        "forbidden_defaults": {
            "values": list(FORBIDDEN_DEFAULTS),
            "note": "FAO 训练 SAT=200/WL=100 与 PERC 4-8 冲突，禁止取单值默认。",
        },
        "identities": {
            "dryland": water_need_identity("dryland"),
            "paddy": water_need_identity("paddy_water_layer"),
            "aquatic": water_need_identity("aquatic_vegetable_hold"),
        },
        "capability_proposal": {
            "rice_water_layer_check": "HOLD",
            "rice_irrigation_dose": "UNSUPPORTED",
            "duchang_rice_irrigation_recommendation": "UNSUPPORTED",
            "copy_dryland_to_rice": "UNSUPPORTED",
        },
    }
