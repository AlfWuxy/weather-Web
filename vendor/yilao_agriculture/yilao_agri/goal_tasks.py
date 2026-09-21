"""用户目标 → 候选任务生成。

把「老人想完成的事」转成候选农事任务草案，规则固定为三条：

1. 保留本人选择：谁做、想什么时候做、为什么急，只作声明性字段原样保留，
   模块不替他改选、不代填帮手、不把紧迫改写成硬期限。
2. 保留未知字段：无法从自述确定的字段写入 ``unknown_fields``，不拿默认值、
   不拿总面积、不拿排序首条候选把缺口填满。
3. 不发明数字：自由文本里的面积、期限、时长一律不解析成数值；缺少个人速率、
   当次成熟量或载量时保持未知并给出可解释错误码。

本模块只读输入、只写本地文件；不发通知、不接生产系统、不认证来源资质。
关键词映射是词表匹配，不是农艺确认；候选成立不等于个人健康安全。
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

from .models import (
    AREA_UNITS,
    FAMILY_UNITS,
    INTEGER_UNITS,
    UNITS,
    InputError,
    _crop_alias_mod,
    _infer_quantity_family,
)

INTAKE_SCHEMA_ID = "yilao.goal_intake.v0.1"
GENERATED_SCHEMA_ID = "yilao.goal_candidates.v0.1"
VOCABULARY_VERSION = "yilao.goal_operations.v0.1"

#: 直接判为 blocked 的错误码：不能凭猜测补齐，必须回到本人/家属重新取得输入。
HARD_BLOCK_CODES = frozenset({
    "OPERATION_UNMAPPED",
    "CROP_IDENTITY_UNRESOLVED",
    "HARVEST_AREA_AS_REMAINING",
    "TRANSPORT_AREA_AS_REMAINING",
    "UNIT_WEIGHT_NOT_TRIP",
    "FORBIDDEN_UNIT_FAMILY",
    "MISSING_QUANTITY_FAMILY",
    "MERGE_HARVEST_AND_CARRY",
})

# 冻结词表：表面说法 → 规范 operation / 环节号。只做文字匹配，不做农艺判断。
# 环节号沿用 context/作物农事责任矩阵.md 的 01—18。
OPERATION_VOCABULARY = (
    {"operation": "plan_material", "stage_code": "01", "stage_label": "田块与种植计划",
     "tokens": ("计划", "备料")},
    {"operation": "carry_material", "stage_code": "03", "stage_label": "有机肥及农资搬运",
     "tokens": ("挑粪", "搬粪", "运肥", "搬运农资", "carry_material", "manure_carry")},
    {"operation": "tillage", "stage_code": "04", "stage_label": "整地与田间设施",
     "tokens": ("整地", "翻地", "犁地", "tillage", "land_prep")},
    {"operation": "fertilization_base", "stage_code": "05", "stage_label": "基肥与土壤改良",
     "tokens": ("基肥", "底肥")},
    {"operation": "seedling_prep", "stage_code": "06", "stage_label": "种子与育苗育秧",
     "tokens": ("育苗", "育秧", "催芽", "播种")},
    {"operation": "transplant", "stage_code": "07", "stage_label": "播种与移栽",
     "tokens": ("移栽", "定植", "栽苗", "插秧", "transplant")},
    {"operation": "germination_check", "stage_code": "08", "stage_label": "出苗与成苗管理",
     "tokens": ("出苗", "补苗", "查苗")},
    {"operation": "irrigation", "stage_code": "09", "stage_label": "灌溉与排水",
     "tokens": ("灌溉", "浇水", "灌水", "排水", "irrigation", "irrigate")},
    {"operation": "fertilization", "stage_code": "10", "stage_label": "追肥与分阶段养分管理",
     "tokens": ("追肥", "施肥", "fertilization", "apply_material")},
    {"operation": "weeding", "stage_code": "11", "stage_label": "除草与中耕培土",
     "tokens": ("除草", "拔草", "锄草", "中耕", "培土", "weeding")},
    {"operation": "trellising", "stage_code": "12", "stage_label": "支架及植株整理",
     "tokens": ("支架", "搭架", "引蔓", "绑蔓", "整枝", "打杈", "压蔓", "修剪", "trellising")},
    {"operation": "pollination", "stage_code": "13", "stage_label": "开花结果与产品管理",
     "tokens": ("人工授粉", "授粉", "点花", "蘸花", "pollination"), "agronomy_sensitive": True},
    {"operation": "product_management", "stage_code": "13", "stage_label": "开花结果与产品管理",
     "tokens": ("疏花", "疏果", "套袋", "垫瓜", "翻瓜", "摘心", "打叶",
                "product_management", "thinning"), "agronomy_sensitive": True},
    {"operation": "pest_scouting", "stage_code": "14", "stage_label": "病虫与异常巡查处理",
     "tokens": ("巡田", "查虫", "看病", "病虫", "喷药", "打药", "pest_scouting"),
     "agronomy_sensitive": True},
    {"operation": "harvest", "stage_code": "15", "stage_label": "收获",
     "tokens": ("采收", "收割", "收获", "摘果", "摘叶", "采摘", "harvest")},
    {"operation": "harvest_repeated", "stage_code": "15", "stage_label": "收获（多茬/多年生）",
     "tokens": ("割韭菜", "割茬", "多茬", "再生", "刈割", "割"), "perennial_relevant": True},
    {"operation": "field_carry", "stage_code": "16", "stage_label": "田间搬运与初处理",
     "tokens": ("挑回", "运回", "转运", "field_carry", "carry")},
    {"operation": "dry_store", "stage_code": "17", "stage_label": "干燥储藏与收尾",
     "tokens": ("晾晒", "储藏", "收仓", "dry_store", "sun_dry")},
    {"operation": "field_cleanup", "stage_code": "17", "stage_label": "清田换茬与收尾",
     "tokens": ("清田", "换茬", "清园")},
    {"operation": "disaster_prep", "stage_code": "18", "stage_label": "极端天气前后处理",
     "tokens": ("防台", "防冻", "防涝", "灾前", "防灾", "灾后")},
)

_INTAKE_KEYS = frozenset({
    "schema_id", "mode", "now", "region_id", "stated_by", "person", "plots", "wants", "notes",
})
_PERSON_KEYS = frozenset({"id", "display_name", "wants_to_do_self", "noted_conditions"})
_PLOT_KEYS = frozenset({
    "id", "region_id", "crop_surface_form", "crop_id", "alias_confirmation", "total_area", "environment",
})
_WANT_KEYS = frozenset({
    "want_id", "task_id", "plot_id", "region_id", "surface_text", "stated_by", "crop_surface_form",
    "crop_id", "alias_confirmation",
    "stage_surface", "operation_surface", "method_surface", "quantity_text", "plot_total_area_text",
    "remaining_quantity", "plot_total_area", "rates", "earliest_start", "deadline", "deadline_text",
    "deadline_source", "priority", "divisible", "min_chunk_minutes", "quantity_step", "depends_on",
    "required_resources", "tags", "load_per_trip_kg", "session", "once", "agronomy", "crop_cycle",
    "wants_self", "preferred_window_text", "urgency_reason_text", "can_split", "helpers_text",
    "helper_worker_ids", "source",
})
_CROP_CYCLES = frozenset({"annual", "perennial", "unknown"})
_SESSION_KEYS = ("setup_minutes", "outbound_minutes", "return_minutes", "cleanup_minutes", "buffer_minutes")
_ONCE_KEYS = ("setup_minutes", "cleanup_minutes")


def _err(path, message, code=None):
    raise InputError(f"{path}: {message}" if not code else f"{path}: {code} {message}")


def _object(value, path, allowed):
    if not isinstance(value, dict):
        _err(path, "必须为对象")
    for key in value:
        if allowed is not None and key not in allowed:
            _err(f"{path}.{key}", "未知字段")
    return value


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _err(path, "必须为非空字符串")
    return value


def _optional_text(value, path):
    if value is None:
        return None
    return _text(value, path)


def _bool_or_none(value, path):
    if value is None:
        return None
    if not isinstance(value, bool):
        _err(path, "必须为布尔值或省略")
    return value


def _quantity(value, path, units=UNITS):
    """只接受显式 {value, unit}；字符串里的数字不在这里解析。"""
    item = _object(value, path, {"value", "unit"})
    unit = item.get("unit")
    if unit not in units:
        _err(f"{path}.unit", f"不支持的单位 {unit!r}，不猜测亩、公斤或趟")
    number = item.get("value")
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        _err(f"{path}.value", "必须为数值")
    if number < 0:
        _err(f"{path}.value", "不得为负")
    if unit in INTEGER_UNITS and int(number) != number:
        _err(f"{path}.value", "趟数或株数必须为整数")
    return {"value": number, "unit": unit}


def _match_length(surface, token):
    """中文按子串、英文按词内子串匹配，返回命中长度；不做拼音或近形模糊匹配。"""
    blob = str(surface or "").strip().casefold()
    if not blob:
        return 0
    token_cf = token.casefold()
    if any("\u4e00" <= ch <= "\u9fff" for ch in token):
        return len(token) if token_cf in blob else 0
    return len(token) if token_cf in blob.replace("-", "_") else 0


def localize_operation(surface):
    """把自由说法映射到冻结词表条目。

    取最长命中；命中长度相同时判为歧义，不按表内顺序任选一条。
    返回 ``(entry, None)`` 或 ``(None, code)``。
    """
    best, best_len, ties = None, 0, []
    for entry in OPERATION_VOCABULARY:
        hit = max((_match_length(surface, token) for token in entry["tokens"]), default=0)
        if hit > best_len:
            best, best_len, ties = entry, hit, [entry]
        elif hit and hit == best_len:
            ties.append(entry)
    if best is None:
        return None, "OPERATION_UNMAPPED"
    if len(ties) > 1:
        return None, "OPERATION_AMBIGUOUS"
    return best, None


def _crop_lookup(surface, region_id, confirmation=None):
    query = {"surface_form": surface, "region_id": region_id}
    if confirmation is not None:
        query["confirmation"] = confirmation
    module = _crop_alias_mod()
    try:
        return module.resolve_crop_alias(query)
    except module.CropAliasError as exc:
        _err(f"crop_alias.{exc.field or 'query'}", f"{exc.code}: {exc.message}")


def load_intake(raw):
    """校验目标输入信封；未知字段拒绝，数字一律要求显式给出。"""
    intake = _object(raw, "intake", _INTAKE_KEYS)
    schema_id = intake.get("schema_id", INTAKE_SCHEMA_ID)
    if schema_id != INTAKE_SCHEMA_ID:
        _err("intake.schema_id", f"须为 {INTAKE_SCHEMA_ID}")
    plots = intake.setdefault("plots", [])
    if not isinstance(plots, list):
        _err("intake.plots", "必须为数组")
    seen = set()
    for i, plot in enumerate(plots):
        path = f"intake.plots[{i}]"
        _object(plot, path, _PLOT_KEYS)
        pid = _text(plot.get("id"), f"{path}.id")
        if pid in seen:
            _err(f"{path}.id", "田块标识重复")
        seen.add(pid)
        _text(plot.get("region_id"), f"{path}.region_id")
        if "total_area" in plot:
            plot["total_area"] = _quantity(plot["total_area"], f"{path}.total_area", AREA_UNITS)
    wants = intake.setdefault("wants", [])
    if not isinstance(wants, list) or not wants:
        _err("intake.wants", "必须为非空数组")
    for i, want in enumerate(wants):
        path = f"intake.wants[{i}]"
        _object(want, path, _WANT_KEYS)
        _text(want.get("want_id"), f"{path}.want_id")
        _text(want.get("surface_text"), f"{path}.surface_text")
        if want.get("crop_cycle") is not None and want["crop_cycle"] not in _CROP_CYCLES:
            _err(f"{path}.crop_cycle", "须为 annual / perennial / unknown")
    if "person" in intake:
        _object(intake["person"], "intake.person", _PERSON_KEYS)
    return deepcopy(intake)


def _plot_index(intake):
    return {plot["id"]: plot for plot in intake.get("plots", [])}


def _crop_identity(want, plot, region_id, reasons):
    """作物身份：确认值优先，其次占位 ID，最后保持未解析。绝不取候选首条。"""
    declared = _optional_text(want.get("crop_id"), "crop_id")
    if declared is not None:
        return {"crop_id": declared, "identity_status": "declared_by_intake", "selected_crop_id": declared,
                "placeholder_crop_id": None, "alias_status": None, "candidates": [],
                "requires_human_confirmation": False, "local_confirmed": False,
                "region_id": region_id, "note": "crop_id 由输入方声明，本模块不认证其依据"}
    surface = want.get("crop_surface_form") or plot.get("crop_surface_form")
    plot_declared = plot.get("crop_id")
    if plot_declared is not None and want.get("crop_surface_form") is None:
        return {"crop_id": plot_declared, "identity_status": "declared_by_intake", "selected_crop_id": plot_declared,
                "placeholder_crop_id": None, "alias_status": None, "candidates": [],
                "requires_human_confirmation": False, "local_confirmed": False,
                "region_id": region_id, "note": "crop_id 由田块声明，本模块不认证其依据"}
    if surface is None:
        reasons.append({"code": "CROP_IDENTITY_UNRESOLVED",
                        "message": "既未声明 crop_id，也没有本地作物名，不能猜一种作物排进去"})
        return {"crop_id": None, "identity_status": "unresolved", "selected_crop_id": None,
                "placeholder_crop_id": None, "alias_status": None, "candidates": [],
                "requires_human_confirmation": True, "local_confirmed": False,
                "region_id": region_id, "note": "缺少作物身份"}
    confirmation = want.get("alias_confirmation") if want.get("alias_confirmation") is not None else plot.get("alias_confirmation")
    resolved = _crop_lookup(surface, region_id, confirmation)
    status = resolved.get("status")
    selected = resolved.get("selected_crop_id")
    if selected:
        return {"crop_id": selected, "identity_status": "confirmed", "selected_crop_id": selected,
                "placeholder_crop_id": None, "alias_status": status,
                "candidates": resolved.get("candidates", []), "requires_human_confirmation": False,
                "local_confirmed": bool(resolved.get("local_confirmed")), "region_id": region_id,
                "surface_form": surface, "note": "人工确认或目录已地方确认；仍非田间形态鉴定"}
    placeholder_row = None
    for row in _catalog_rows():
        if row.get("surface_form") == surface and row.get("region_id") in (region_id, "national_candidate"):
            placeholder_row = row
            break
    placeholder = (placeholder_row or {}).get("placeholder_crop_id")
    catalog_status = (placeholder_row or {}).get("status")
    identity = {"crop_id": placeholder, "identity_status": status or "unresolved",
                "selected_crop_id": None, "placeholder_crop_id": placeholder,
                "alias_status": status, "catalog_status": catalog_status, "surface_form": surface,
                "candidates": resolved.get("candidates", []),
                "requires_human_confirmation": True,
                "local_confirmed": bool(resolved.get("local_confirmed")), "region_id": region_id,
                "note": "保留多候选或未地方确认状态；占位 ID 不是物种鉴定"}
    if placeholder is not None:
        reasons.append({"code": "CROP_IDENTITY_NEEDS_CONFIRMATION",
                        "message": "作物身份待人工确认；占位 ID 只为占位，不得当作已鉴定"})
        return identity
    if resolved.get("candidates"):
        identity["crop_id"] = None
        reasons.append({"code": "CROP_IDENTITY_NEEDS_CONFIRMATION",
                        "message": "有候选但未确认，且无占位 ID；不用首个候选填入 crop_id"})
        if catalog_status == "UNCONFIRMED_PRESENCE":
            reasons.append({"code": "CROP_PRESENCE_UNCONFIRMED",
                            "message": "该地区是否种植此作物尚未核实，不得写成已种或未种"})
        return identity
    identity["crop_id"] = None
    reasons.append({"code": "CROP_IDENTITY_UNRESOLVED",
                    "message": "作物身份未解析且无候选，不能用全国名或首个候选填入"})
    return identity


def _catalog_rows():
    module = _crop_alias_mod()
    try:
        return module.load_catalog().get("aliases", [])
    except module.CropAliasError:
        return []


def _quantity_assessment(want, operation, method, plot, reasons, unknown_fields):
    """判数量族并挡住会静默换算的组合；剩余量缺失时保持未知。"""
    fields = {"operation": operation or "", "method": method or ""}
    family = _infer_quantity_family(fields) if operation else None
    remaining = want.get("remaining_quantity")
    total = want.get("plot_total_area") or plot.get("total_area")
    assessment = {"family": family, "family_source": "frozen_word_list_from_operation_method",
                  "remaining_quantity": None, "plot_total_area": total, "surface_text": want.get("quantity_text")}
    if remaining is None:
        unknown_fields.append("remaining_quantity")
        if total is not None:
            reasons.append({"code": "PLOT_TOTAL_NOT_REMAINING",
                            "message": "只给了田块总面积；本次仍要处理多少必须单独回答，不按总面积排"})
        elif want.get("quantity_text"):
            reasons.append({"code": "QUANTITY_TEXT_NOT_PARSED",
                            "message": "自述里的数量是自由文本，不自动解析成数值"})
        else:
            reasons.append({"code": "REMAINING_QUANTITY_MISSING", "message": "本次剩余工作量缺失"})
        return assessment, None
    quantity = _quantity(remaining, "remaining_quantity")
    unit = quantity["unit"]
    assessment["remaining_quantity"] = quantity
    if family == "merge_harvest_and_carry":
        reasons.append({"code": "MERGE_HARVEST_AND_CARRY",
                        "message": "采收与搬运不得合成一条剩余量，须分开记录"})
    elif family:
        allowed = FAMILY_UNITS[family]
        if unit not in allowed:
            if family == "mature_batch" and unit in AREA_UNITS:
                reasons.append({"code": "HARVEST_AREA_AS_REMAINING",
                                "message": "采收剩余不得为亩或平方米，不用面积乘统一产量"})
            elif family == "remaining_trips" and unit in AREA_UNITS:
                reasons.append({"code": "TRANSPORT_AREA_AS_REMAINING",
                                "message": "搬运剩余不得为面积，不把亩静默当成趟数"})
            elif family == "remaining_trips" and unit == "kg":
                reasons.append({"code": "UNIT_WEIGHT_NOT_TRIP",
                                "message": "搬运剩余必须是 trip；有载量也只作上下文，不换算趟数"})
            else:
                reasons.append({"code": "FORBIDDEN_UNIT_FAMILY",
                                "message": f"单位 {unit} 不属于数量族 {family}，不静默换算"})
    elif unit == "kg":
        reasons.append({"code": "MISSING_QUANTITY_FAMILY",
                        "message": "该说法无法判数量族，kg 不能猜是采收、晾晒还是未换趟的搬运"})
    return assessment, family


def _person_choices(intake, want):
    person = intake.get("person") or {}
    wants_self = want.get("wants_self")
    if wants_self is None:
        wants_self = person.get("wants_to_do_self")
    stated_by = want.get("stated_by") or intake.get("stated_by") or "unknown"
    if wants_self is True:
        mode = "self_declared"
    elif wants_self is False:
        mode = "helper_or_shared"
    else:
        mode = "unknown"
    return {
        "stated_by": stated_by,
        "wants_self": wants_self,
        "worker_assignment_mode": mode,
        "preferred_worker_id": person.get("id") if mode == "self_declared" else None,
        "preferred_window_text": want.get("preferred_window_text"),
        "urgency_reason_text": want.get("urgency_reason_text"),
        "can_split": want.get("can_split"),
        "helpers_text": want.get("helpers_text"),
        "helper_worker_ids": list(want.get("helper_worker_ids") or []),
        "person_notes": list(person.get("noted_conditions") or []),
        "rule": "本人选择原样保留；本模块不替本人改选，也不把帮手安排成默认方案",
    }


def _deadline_block(want, unknown_fields, reasons):
    deadline = want.get("deadline")
    source = want.get("deadline_source")
    text = want.get("deadline_text")
    block = {"deadline": None, "deadline_source": None, "deadline_text": text,
             "earliest_start": want.get("earliest_start"),
             "interpretation": "personal_statement_preserved"}
    if deadline is None:
        unknown_fields.append("deadline")
        reasons.append({"code": "DEADLINE_NOT_MACHINE_READABLE",
                        "message": "没有可机器判读的截止；自述文字原样保留，不自动变成日期"})
    else:
        block["deadline"] = deadline
    if source is None:
        unknown_fields.append("deadline_source")
        reasons.append({"code": "DEADLINE_SOURCE_MISSING",
                        "message": "截止缺少依据；紧迫程度不能凭空升级为期限"})
    else:
        block["deadline_source"] = source
    return block


def _build_task_fields(want, plot, plot_ids, crop, operation_entry, quantity_assessment, deadline_block,
                       unknown_fields, reasons):
    """仅在字段齐全时组装 schema 1.0 任务；缺字段只登记未知，不填猜测值。"""
    task_id = want.get("task_id") or f"goal-{want['want_id']}"
    fields = {"id": task_id, "plot_id": want.get("plot_id") or plot.get("id")}
    if fields["plot_id"] not in plot_ids:
        unknown_fields.append("plot_id")
        reasons.append({"code": "PLOT_UNKNOWN", "message": "自述里的田块未在 plots 中登记"})
    fields["crop_id"] = crop["crop_id"]
    if fields["crop_id"] is None:
        unknown_fields.append("crop_id")
    stage = _optional_text(want.get("stage_surface"), "stage_surface")
    if stage is None:
        unknown_fields.append("stage")
        reasons.append({"code": "STAGE_UNSPECIFIED",
                        "message": "本人/家属没说到生育阶段；不替填农艺阶段"})
    else:
        fields["stage"] = stage
    if operation_entry is None:
        unknown_fields.append("operation")
    else:
        fields["operation"] = operation_entry["operation"]
    method = _optional_text(want.get("method_surface"), "method_surface")
    if method is None:
        unknown_fields.append("method")
        reasons.append({"code": "METHOD_UNSPECIFIED", "message": "工具和做法未说明"})
    else:
        fields["method"] = method
    if quantity_assessment["remaining_quantity"] is None:
        pass
    else:
        fields["remaining_quantity"] = quantity_assessment["remaining_quantity"]
    rates = want.get("rates")
    if not isinstance(rates, dict) or not rates:
        unknown_fields.append("rates")
        reasons.append({"code": "RATES_MISSING",
                        "message": "缺少该人该做法的净用时依据；不借用他人速率，也不填估计"})
    else:
        for worker_id, rate in rates.items():
            _object(rate, f"rates.{worker_id}", {"unit", "low", "high", "scope", "source"})
            for key in ("unit", "low", "high", "scope", "source"):
                if key not in rate:
                    unknown_fields.append(f"rates.{worker_id}.{key}")
        fields["rates"] = deepcopy(rates)
    if deadline_block["earliest_start"] is not None:
        fields["earliest_start"] = deadline_block["earliest_start"]
    elif want.get("earliest_start") is not None:
        fields["earliest_start"] = want["earliest_start"]
    if deadline_block["deadline"] is not None:
        fields["deadline"] = deadline_block["deadline"]
    if deadline_block["deadline_source"] is not None:
        fields["deadline_source"] = deadline_block["deadline_source"]
    if want.get("priority") is not None:
        fields["priority"] = want["priority"]
    for key in ("divisible", "min_chunk_minutes", "quantity_step", "load_per_trip_kg"):
        if want.get(key) is not None:
            fields[key] = want[key]
    for key in ("depends_on", "required_resources", "tags"):
        fields[key] = list(want.get(key) or [])
    for key, allowed in (("session", _SESSION_KEYS), ("once", _ONCE_KEYS)):
        block = want.get(key)
        if not isinstance(block, dict) or not block:
            unknown_fields.append(key)
            reasons.append({"code": f"{key.upper()}_OVERHEAD_UNSPECIFIED",
                            "message": f"{key} 附加时间未提供；不代填为 0 也不代填经验值"})
        else:
            _object(block, key, set(allowed))
            for field in allowed:
                if field not in block:
                    unknown_fields.append(f"{key}.{field}")
            fields[key] = deepcopy(block)
    agronomy = want.get("agronomy")
    if not isinstance(agronomy, dict) or not agronomy:
        unknown_fields.append("agronomy")
        reasons.append({"code": "AGRONOMY_UNSPECIFIED", "message": "缺农艺前提与来源"})
    else:
        status = agronomy.get("status", "unknown")
        fields["agronomy"] = deepcopy(agronomy)
        if status == "unknown":
            unknown_fields.append("agronomy.status")
            reasons.append({"code": "AGRONOMY_STATUS_UNKNOWN", "message": "农艺状态未确认"})
    alias_surface = crop.get("surface_form")
    if alias_surface and crop["selected_crop_id"] is None:
        fields["crop_alias"] = {"surface_form": alias_surface, "region_id": crop["region_id"]}
    return fields


_plot_ids = frozenset()


def _dedupe_reasons(reasons):
    """同一错误码只保留首次出现，避免同一缺口被记两次。"""
    seen, deduped = set(), []
    for reason in reasons:
        if reason["code"] in seen:
            continue
        seen.add(reason["code"])
        deduped.append(reason)
    return deduped


def _evaluate_status(candidate):
    codes = {reason["code"] for reason in candidate["reasons"]}
    if codes & HARD_BLOCK_CODES:
        return "blocked"
    if candidate["unknown_fields"] or candidate["crop"]["requires_human_confirmation"]:
        return "needs_confirmation"
    return "ready_for_review"


def generate_candidates(raw):
    """主入口：目标输入 → 候选任务草案集合。"""
    global _plot_ids
    intake = load_intake(raw)
    plots = _plot_index(intake)
    _plot_ids = frozenset(plots)
    region_default = intake.get("region_id")
    candidates = []
    seen_task_ids = set()
    for want in intake["wants"]:
        want_id = want["want_id"]
        plot_id = want.get("plot_id") or (intake["plots"][0]["id"] if intake.get("plots") else None)
        plot = plots.get(plot_id, {})
        region_id = want.get("region_id") or plot.get("region_id") or region_default
        if not isinstance(region_id, str) or not region_id.strip():
            _err(f"intake.wants[{want_id}].region_id", "缺少地区标识，不能拿全国候选当地方确认")
        reasons = []
        unknown_fields = []
        op_surface = want.get("operation_surface")
        operation_entry, op_code = localize_operation(op_surface)
        if operation_entry is None:
            reasons.append({"code": op_code,
                            "message": f"说法「{op_surface}」未在冻结词表命中，不猜是哪种农事"})
        elif operation_entry.get("agronomy_sensitive"):
            reasons.append({"code": "OPERATION_NEEDS_AGRONOMY_REVIEW",
                            "message": "该说法涉及开花授粉/植株产品管理或病虫处理，须农艺确认后再执行"})
        crop = _crop_identity(want, plot, region_id, reasons)
        if not plot:
            reasons.append({"code": "PLOT_UNKNOWN", "message": "田块未登记"})
            unknown_fields.append("plot_id")
        quantity_assessment, family = _quantity_assessment(
            want, operation_entry["operation"] if operation_entry else None,
            want.get("method_surface"), plot, reasons, unknown_fields)
        deadline_block = _deadline_block(want, unknown_fields, reasons)
        person_choices = _person_choices(intake, want)
        if person_choices["worker_assignment_mode"] == "helper_or_shared" and not person_choices["helper_worker_ids"]:
            unknown_fields.append("helper_worker_id")
            reasons.append({"code": "HELPER_NOT_NAMED",
                            "message": "本人已说不是自己做，但没给帮手；不把帮手安排在自述之外"})
        if operation_entry is not None and operation_entry.get("perennial_relevant"):
            cycle = want.get("crop_cycle")
            if cycle in (None, "unknown"):
                unknown_fields.append("crop_cycle")
                reasons.append({"code": "PERENNIAL_CYCLE_UNCONFIRMED",
                                "message": "多茬/多年生不确定时，不按多年生或单季任一假设排"})
        task_id = want.get("task_id") or f"goal-{want_id}"
        if task_id in seen_task_ids:
            _err(f"intake.wants[{want_id}].task_id", "任务标识重复")
        seen_task_ids.add(task_id)
        candidate = {
            "candidate_id": f"CAND-{want_id}",
            "want_id": want_id,
            "task_id": task_id,
            "surface_text": want["surface_text"],
            "source": want.get("source"),
            "status": None,
            "reasons": reasons,
            "unknown_fields": sorted(set(unknown_fields)),
            "crop": crop,
            "operation": None if operation_entry is None else {
                "operation": operation_entry["operation"],
                "stage_code": operation_entry["stage_code"],
                "stage_label": operation_entry["stage_label"],
                "mapping_source": VOCABULARY_VERSION,
                "mapping_kind": "keyword_match_not_agronomy_confirmation",
                "agronomy_sensitive": bool(operation_entry.get("agronomy_sensitive")),
                "perennial_relevant": bool(operation_entry.get("perennial_relevant")),
            },
            "quantity": quantity_assessment,
            "deadline": deadline_block,
            "person_choices": person_choices,
            "crop_cycle_declared": want.get("crop_cycle"),
            "preserved_text": {
                "surface_text": want["surface_text"],
                "quantity_text": want.get("quantity_text"),
                "plot_total_area_text": want.get("plot_total_area_text"),
                "deadline_text": want.get("deadline_text"),
                "preferred_window_text": want.get("preferred_window_text"),
                "urgency_reason_text": want.get("urgency_reason_text"),
                "helpers_text": want.get("helpers_text"),
            },
            "non_claims": [
                "关键词映射是词表匹配，不是农艺确认。",
                "候选可排不等于个人健康安全；排程仍以本人状态、限制和现场条件输入为准。",
                "占位 crop_id 不是物种鉴定，未确认前不得当作已识别作物。",
            ],
        }
        candidate["task_fields"] = _build_task_fields(
            want, plot, _plot_ids, crop, operation_entry, quantity_assessment, deadline_block,
            unknown_fields, reasons)
        candidate["unknown_fields"] = sorted(set(unknown_fields))
        candidate["reasons"] = _dedupe_reasons(reasons)
        candidate["status"] = _evaluate_status(candidate)
        if candidate["status"] != "ready_for_review":
            candidate["task_fields"] = None
        candidates.append(candidate)
    blocked = sum(1 for c in candidates if c["status"] == "blocked")
    needs = sum(1 for c in candidates if c["status"] == "needs_confirmation")
    return {
        "schema_id": GENERATED_SCHEMA_ID,
        "vocabulary_version": VOCABULARY_VERSION,
        "intake_schema_id": INTAKE_SCHEMA_ID,
        "mode": intake.get("mode", "demonstration"),
        "region_id": region_default,
        "candidates": candidates,
        "summary": {"candidate_count": len(candidates), "ready_for_review": len(candidates) - blocked - needs,
                    "needs_confirmation": needs, "blocked": blocked},
        "warnings": [
            "候选是待人工填缺的任务草案，不是排程结果，也不是个人健康许可。",
            "本模块不解析自由文本数字，不认证来源资质，也不接生产系统。",
            "未知字段必须由本人或家属补齐；补齐后仍要经 validate_request 与排程硬约束。",
        ],
    }


def build_task(candidate):
    """把 ready 候选还原成 schema 1.0 任务对象；有未知字段则拒绝组装。"""
    if not isinstance(candidate, dict):
        _err("candidate", "必须为对象")
    unknown = candidate.get("unknown_fields") or []
    if unknown:
        _err("candidate", f"UNKNOWN_FIELDS_PRESENT 仍有未确定字段：{', '.join(unknown)}")
    task = candidate.get("task_fields")
    if not isinstance(task, dict):
        _err("candidate", "BUILD_BLOCKED 该候选没有可组装字段")
    return deepcopy(task)


def inject_tasks(base_request, generated, replace_tasks=True):
    """把 ready 候选注入基础请求的副本；不修改传入对象。"""
    request = deepcopy(base_request)
    built = [build_task(c) for c in generated["candidates"] if c["status"] == "ready_for_review"]
    if replace_tasks:
        request["tasks"] = built
    else:
        request.setdefault("tasks", [])
        request["tasks"] = list(request["tasks"]) + built
    return request


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="用户目标 → 候选农事任务（不排程、不发通知）")
    parser.add_argument("intake")
    parser.add_argument("--base", help="可选：schema 1.0 请求，用于把 ready 候选组装校验")
    parser.add_argument("--out")
    parser.add_argument("--summary")
    args = parser.parse_args(argv)
    generated = generate_candidates(_load(args.intake))
    text = json.dumps(generated, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    if args.base:
        from .models import validate_request
        from .engine import plan
        request = inject_tasks(_load(args.base), generated)
        validate_request(request)
        result = plan(request)
        lines = [f"ready={generated['summary']['ready_for_review']} "
                 f"needs={generated['summary']['needs_confirmation']} "
                 f"blocked={generated['summary']['blocked']}",
                 f"plan status={result['status']} sessions={len(result['sessions'])}"]
        if args.summary:
            Path(args.summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
