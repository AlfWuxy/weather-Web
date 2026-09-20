"""地方作物别名解析：多候选保留，歧义不自动选定。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json
import unicodedata


CATALOG_PATH = Path(__file__).with_name("local_alias_catalog.json")
CATEGORY_CATALOG_PATH = Path(__file__).with_name("category_catalog.json")

# 可推荐门槛：三项证据齐全才允许打开推荐。收录( catalog_included )与此分开。
RECOMMENDATION_GATES = ("agronomy_available", "local_reviewed", "field_validated")
REQUIRED_CATALOG_VERSION_KEY = "catalog_version"

ALLOWED_QUERY = frozenset({"surface_form", "region_id", "confirmation"})
ALLOWED_CONFIRM = frozenset({"confirmed", "crop_id", "by", "at", "evidence_type", "note"})
EVIDENCE_TYPES = frozenset({
    "SYNTHETIC", "IMPLEMENTATION_TEST", "SECONDARY_SUMMARY",
    "PRIMARY_SOURCE", "FIELD_OBSERVATION", "PROFESSIONAL_REVIEW",
})
LOCK_BLOCKED_STATUS = frozenset({"IDENTITY_HOLD", "HOLD"})
LEGACY_STATUS = frozenset({"LEGACY_UNBOUND"})
AUTO_SELECT_KEYS = frozenset({
    "auto_select", "pick", "preferred", "preferred_crop_id",
    "rank", "score", "best", "best_candidate", "top1",
})

_CATALOG_CACHE = None
_CATEGORY_CACHE = None


class CropAliasError(ValueError):
    """别名解析或人工确认错误；code 供校验入口写入字段路径。"""

    def __init__(self, code, message, field=""):
        self.code = code
        self.message = message
        self.field = field
        prefix = f"{field}: " if field else ""
        super().__init__(f"{prefix}{code}: {message}")


def normalize_surface(value) -> str:
    """去掉首尾空白并做大小写折叠；不做模糊匹配，避免近音近形误锁。"""
    if not isinstance(value, str):
        raise CropAliasError("BAD_TYPE", "surface_form 必须为字符串", "surface_form")
    text = unicodedata.normalize("NFC", value).strip()
    text = " ".join(text.split())
    return text.casefold()


def _require_catalog_version(catalog):
    """版本化目录必须声明非空 catalog_version；缺失即拒绝，避免静默换版。"""
    if not isinstance(catalog, dict):
        raise CropAliasError("BAD_TYPE", "目录必须为对象")
    version = catalog.get(REQUIRED_CATALOG_VERSION_KEY)
    if not isinstance(version, str) or not version.strip():
        raise CropAliasError(
            "CATALOG_VERSION_MISSING",
            f"版本化目录必须声明非空 {REQUIRED_CATALOG_VERSION_KEY}",
        )
    return version.strip()


def _gate_open(catalog) -> bool:
    """目录级推荐总闸；缺省或 open!=true 时按关闭处理，本版内置目录为关闭。"""
    gate = catalog.get("recommendation_gate")
    if gate is None:
        return False
    if not isinstance(gate, dict):
        raise CropAliasError("BAD_TYPE", "recommendation_gate 必须为对象")
    return gate.get("open") is True


def load_catalog(path=None) -> dict:
    """读取别名目录；缺省为本目录 JSON。测试可传入 dict（合成夹具不强制版本）。

    从文件（含内置默认目录）加载时强制要求版本字段；注入的 dict 视为合成夹具，
    不要求版本，以兼容既有测试。
    """
    global _CATALOG_CACHE
    if isinstance(path, dict):
        catalog = deepcopy(path)
        _validate_catalog(catalog)
        return catalog
    target = Path(path) if path else CATALOG_PATH
    if _CATALOG_CACHE is not None and path is None:
        return deepcopy(_CATALOG_CACHE)
    if not target.is_file():
        raise CropAliasError("CATALOG_MISSING", f"找不到别名目录：{target}")
    catalog = json.loads(target.read_text(encoding="utf-8"))
    _validate_catalog(catalog, require_version=True)
    if path is None:
        _CATALOG_CACHE = catalog
        return deepcopy(_CATALOG_CACHE)
    return catalog


def catalog_version(path=None) -> str:
    """返回目录声明的版本串（不返回目录内容）。"""
    return _require_catalog_version(load_catalog(path))


def recommendation_decision(states) -> dict:
    """把「已收录」与「可推荐」分开：收录不等于可推荐。

    可推荐需要 agronomy_available / local_reviewed / field_validated 三项同时为真；
    任一缺失即 not allowed。本函数不认证证据真伪，也不改变任何硬约束。
    """
    if not isinstance(states, dict):
        raise CropAliasError("BAD_TYPE", "状态必须为对象")
    missing = [g for g in RECOMMENDATION_GATES if states.get(g) is not True]
    return {
        "allowed": not missing,
        "catalog_included": states.get("catalog_included") is True,
        "gates": list(RECOMMENDATION_GATES),
        "missing_gates": missing,
        "reason": "三项门槛齐全" if not missing else "缺门槛：" + ",".join(missing),
    }


def load_category_catalog(path=None) -> dict:
    """读取版本化类别库（15蔬莱/邻近分组 + 5粮油模块）；文件路径版本必填。"""
    global _CATEGORY_CACHE
    if isinstance(path, dict):
        catalog = deepcopy(path)
        _validate_category_catalog(catalog, require_version=True)
        return catalog
    target = Path(path) if path else CATEGORY_CATALOG_PATH
    if _CATEGORY_CACHE is not None and path is None:
        return deepcopy(_CATEGORY_CACHE)
    if not target.is_file():
        raise CropAliasError("CATALOG_MISSING", f"找不到类别库：{target}")
    catalog = json.loads(target.read_text(encoding="utf-8"))
    _validate_category_catalog(catalog, require_version=True)
    if path is None:
        _CATEGORY_CACHE = catalog
        return deepcopy(_CATEGORY_CACHE)
    return catalog


def _validate_category_catalog(catalog, require_version=True):
    if not isinstance(catalog, dict):
        raise CropAliasError("BAD_TYPE", "类别库必须为对象")
    if require_version:
        _require_catalog_version(catalog)
    gate_open = _gate_open(catalog)
    categories = catalog.get("categories")
    if not isinstance(categories, list):
        raise CropAliasError("BAD_TYPE", "categories 必须为数组")
    seen = set()
    for i, row in enumerate(categories):
        if not isinstance(row, dict):
            raise CropAliasError("BAD_TYPE", "类别行必须为对象", f"categories[{i}]")
        cid = row.get("category_id")
        if not isinstance(cid, str) or not cid.strip():
            raise CropAliasError("EMPTY_STRING", "category_id 不能为空", f"categories[{i}].category_id")
        if cid in seen:
            raise CropAliasError("DUPLICATE_CATEGORY", f"重复类别 {cid}", f"categories[{i}].category_id")
        seen.add(cid)
        if not isinstance(row.get("catalog_included"), bool):
            raise CropAliasError("BAD_TYPE", "catalog_included 必须为布尔值",
                                 f"categories[{i}].catalog_included")
        for field in RECOMMENDATION_GATES:
            if not isinstance(row.get(field), bool):
                raise CropAliasError("BAD_TYPE", f"{field} 必须为布尔值", f"categories[{i}].{field}")
        declared = row.get("recommendation_enabled", False)
        if not isinstance(declared, bool):
            raise CropAliasError("BAD_TYPE", "recommendation_enabled 必须为布尔值",
                                 f"categories[{i}].recommendation_enabled")
        if declared and (not gate_open or not recommendation_decision(row)["allowed"]):
            raise CropAliasError(
                "RECOMMENDATION_WITHOUT_EVIDENCE",
                "类别未满足推荐门槛或总闸未开，不得打开推荐",
                f"categories[{i}].recommendation_enabled",
            )


def _validate_catalog(catalog, require_version=False):
    if not isinstance(catalog, dict):
        raise CropAliasError("BAD_TYPE", "目录必须为对象")
    if require_version:
        _require_catalog_version(catalog)
    gate_open = _gate_open(catalog)
    aliases = catalog.get("aliases")
    if not isinstance(aliases, list):
        raise CropAliasError("BAD_TYPE", "aliases 必须为数组")
    seen = set()
    for i, row in enumerate(aliases):
        if not isinstance(row, dict):
            raise CropAliasError("BAD_TYPE", "别名行必须为对象", f"aliases[{i}]")
        surface = normalize_surface(row.get("surface_form", ""))
        region = row.get("region_id")
        if not surface:
            raise CropAliasError("EMPTY_STRING", "surface_form 不能为空", f"aliases[{i}].surface_form")
        if not isinstance(region, str) or not region.strip():
            raise CropAliasError("EMPTY_STRING", "region_id 不能为空", f"aliases[{i}].region_id")
        key = (surface, region)
        if key in seen:
            raise CropAliasError("DUPLICATE_ALIAS", f"重复别名 {surface} @ {region}", f"aliases[{i}]")
        seen.add(key)
        candidates = row.get("candidates", [])
        if not isinstance(candidates, list):
            raise CropAliasError("BAD_TYPE", "candidates 必须为数组", f"aliases[{i}].candidates")
        ids = []
        for j, item in enumerate(candidates):
            if not isinstance(item, dict) or not str(item.get("crop_id") or "").strip():
                raise CropAliasError("BAD_TYPE", "候选必须含 crop_id", f"aliases[{i}].candidates[{j}]")
            cid = item["crop_id"]
            if cid in ids:
                raise CropAliasError("DUPLICATE_ALIAS", f"重复候选 {cid}", f"aliases[{i}].candidates[{j}]")
            ids.append(cid)
        if row.get("local_confirmed") is True and row.get("region_id") == "CN-JX-duchang":
            raise CropAliasError("ALIAS_NATIONAL_AS_LOCAL", "目录不得把都昌别名标成已现场确认")
        declared = row.get("recommendation_enabled", False)
        if not isinstance(declared, bool):
            raise CropAliasError("BAD_TYPE", "recommendation_enabled 必须为布尔值",
                                 f"aliases[{i}].recommendation_enabled")
        # 收录≠可推荐：未凑齐三项证据门槛或总闸未开而声明可推荐，一律拒绝。
        if declared and (not gate_open or not recommendation_decision(row)["allowed"]):
            raise CropAliasError("RECOMMENDATION_WITHOUT_EVIDENCE", "本版目录不得打开推荐",
                                 f"aliases[{i}].recommendation_enabled")


def _index(catalog):
    table = {}
    for row in catalog.get("aliases", []):
        table[(normalize_surface(row["surface_form"]), row["region_id"])] = row
    return table


def _candidate_ids(row):
    return [item["crop_id"] for item in row.get("candidates") or []]


def _public_candidates(row):
    public = []
    for item in row.get("candidates") or []:
        entry = {
            "crop_id": item["crop_id"],
            "label": item.get("label") or "",
            "why": item.get("why") or "",
            "status": item.get("status") or "",
        }
        public.append(entry)
    return public


def _lock_blocked(row):
    if "lock_blocked" in row:
        return bool(row["lock_blocked"])
    return row.get("status") in LOCK_BLOCKED_STATUS


def _parse_confirm_time(value):
    if not isinstance(value, str) or not value.strip():
        raise CropAliasError("BAD_TYPE", "confirmation.at 必须为含时区的 ISO 8601 字符串", "confirmation.at")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise CropAliasError("BAD_TYPE", "confirmation.at 不是有效 ISO 8601", "confirmation.at") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise CropAliasError("BAD_TYPE", "confirmation.at 必须包含时区偏移", "confirmation.at")
    return value


def _validate_query(query):
    if not isinstance(query, dict):
        raise CropAliasError("BAD_TYPE", "查询必须为对象")
    for key in query:
        if key in AUTO_SELECT_KEYS:
            raise CropAliasError("AUTO_SELECT_FORBIDDEN", "禁止按排序、分数或首条候选自动选定", key)
        if key not in ALLOWED_QUERY:
            raise CropAliasError("UNKNOWN_FIELD", "未知字段", key)
    surface = query.get("surface_form")
    region = query.get("region_id")
    if not isinstance(surface, str) or not surface.strip():
        raise CropAliasError("EMPTY_STRING", "surface_form 必须为非空字符串", "surface_form")
    if not isinstance(region, str) or not region.strip():
        raise CropAliasError("EMPTY_STRING", "region_id 必须为非空字符串", "region_id")
    confirmation = query.get("confirmation")
    if confirmation is None:
        return
    if not isinstance(confirmation, dict):
        raise CropAliasError("BAD_TYPE", "confirmation 必须为对象", "confirmation")
    for key in confirmation:
        if key not in ALLOWED_CONFIRM:
            raise CropAliasError("UNKNOWN_FIELD", "未知字段", f"confirmation.{key}")
    confirmed = confirmation.get("confirmed")
    if not isinstance(confirmed, bool):
        raise CropAliasError("BAD_TYPE", "confirmed 必须为布尔值", "confirmation.confirmed")
    if not confirmed:
        return
    for field in ("crop_id", "by", "evidence_type"):
        value = confirmation.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CropAliasError("MISSING_FIELD", f"{field} 在确认时必填", f"confirmation.{field}")
    if confirmation["evidence_type"] not in EVIDENCE_TYPES:
        raise CropAliasError("BAD_TYPE", "evidence_type 不在允许集合", "confirmation.evidence_type")
    _parse_confirm_time(confirmation.get("at"))
    if "note" in confirmation and not isinstance(confirmation["note"], str):
        raise CropAliasError("BAD_TYPE", "note 必须为字符串", "confirmation.note")


def _empty_result(surface, normalized, region_id, status, reasons, match="unresolved"):
    return {
        "surface_form": surface.strip(),
        "normalized": normalized,
        "region_id": region_id,
        "alias_id": None,
        "status": status,
        "match": match,
        "region_match": "none",
        "selected_crop_id": None,
        "locked": False,
        "candidates": [],
        "candidate_count": 0,
        "catalog_included": False,
        "local_confirmed": False,
        "recommendation_enabled": False,
        "recommendation_gate": recommendation_decision({}),
        "requires_human_confirmation": True,
        "lock_blocked": status in LOCK_BLOCKED_STATUS,
        "reasons": reasons,
    }


def resolve_crop_alias(query, catalog=None) -> dict:
    """按地区+表面名解析。

    多候选或未地方确认时 selected_crop_id 保持 null。
    不取 candidates[0]，不看 score。IDENTITY_HOLD 即使有人工确认也不锁物种。
    """
    _validate_query(query)
    catalog = load_catalog(catalog)
    gate_open = _gate_open(catalog)
    surface = query["surface_form"]
    region_id = query["region_id"]
    normalized = normalize_surface(surface)
    table = _index(catalog)
    row = table.get((normalized, region_id))
    region_match = "exact"
    if row is None and region_id != "national_candidate":
        fallback = table.get((normalized, "national_candidate"))
        if fallback is not None:
            row = fallback
            region_match = "national_fallback"
    if row is None:
        confirmation = query.get("confirmation") or {}
        if confirmation.get("confirmed") is True:
            raise CropAliasError(
                "CROP_IDENTITY_UNRESOLVED_REQUIRED",
                "未登记别名不能确认到物种",
                "confirmation.crop_id",
            )
        return _empty_result(
            surface, normalized, region_id, "UNRESOLVED",
            ["未登记的表面名；不得猜测映射"],
        )

    candidates = _public_candidates(row)
    candidate_ids = _candidate_ids(row)
    status = row.get("status") or "UNRESOLVED"
    lock_blocked = _lock_blocked(row)
    local_confirmed = bool(row.get("local_confirmed"))
    result = {
        "surface_form": surface.strip(),
        "normalized": normalized,
        "region_id": region_id,
        "alias_id": row.get("alias_id"),
        "status": status,
        "match": row.get("match") or "unresolved",
        "region_match": region_match,
        "selected_crop_id": None,
        "locked": False,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "catalog_included": True,
        "local_confirmed": local_confirmed,
        "recommendation_enabled": bool(gate_open and recommendation_decision(row)["allowed"]),
        "recommendation_gate": recommendation_decision(row),
        "requires_human_confirmation": True,
        "lock_blocked": lock_blocked,
        "reasons": [],
    }
    if region_match == "national_fallback":
        result["reasons"].append("仅命中全国候选行，不得当作都昌已确认")
        result["local_confirmed"] = False

    confirmation = query.get("confirmation") or {}
    wants_confirm = confirmation.get("confirmed") is True
    if wants_confirm:
        chosen = confirmation["crop_id"]
        if lock_blocked:
            raise CropAliasError(
                "CROP_IDENTITY_LOCKED",
                "IDENTITY_HOLD/HOLD 别名不得锁到单一物种",
                "confirmation.crop_id",
            )
        if chosen not in candidate_ids:
            raise CropAliasError(
                "CANDIDATE_NOT_IN_LIST",
                "确认的作物不在候选列表中",
                "confirmation.crop_id",
            )
        if status in LEGACY_STATUS:
            raise CropAliasError(
                "LEGACY_BOUND_AS_CONFIRMED",
                "演示自由文本不得标成已确认实体",
                "confirmation.crop_id",
            )
        result["selected_crop_id"] = chosen
        result["status"] = "CONFIRMED"
        result["requires_human_confirmation"] = False
        result["locked"] = False
        result["reasons"].append("人工确认已选定候选；推荐仍关闭")
        if confirmation.get("evidence_type") == "SYNTHETIC":
            result["reasons"].append("确认为 SYNTHETIC，不是田间核对")
        return result

    if status in LEGACY_STATUS:
        result["requires_human_confirmation"] = False
        result["reasons"].append("LEGACY_UNBOUND：演示 crop_id 不映射为地方实体")
        return result

    if lock_blocked:
        result["status"] = status if status in LOCK_BLOCKED_STATUS else "IDENTITY_HOLD"
        result["reasons"].append("身份 HOLD：保留多候选，不自动选定")
        return result

    if len(candidates) > 1:
        result["status"] = "AMBIGUOUS"
        result["reasons"].append("多个候选，缺少人工确认，不取首条")
        return result

    if len(candidates) == 1:
        if local_confirmed and region_match == "exact" and not lock_blocked:
            result["selected_crop_id"] = candidates[0]["crop_id"]
            result["status"] = "RESOLVED"
            result["requires_human_confirmation"] = False
            result["reasons"].append("目录已标记地方确认的唯一实体；推荐仍关闭")
            return result
        result["status"] = "UNIQUE_UNCONFIRMED"
        result["reasons"].append("唯一候选但未地方确认，不自动选定")
        return result

    if status in {"NOT_MENTIONED", "HOLD"} or row.get("match") in {"not_mentioned", "absent"}:
        result["status"] = status if status in {"HOLD", "NOT_MENTIONED"} else "NOT_MENTIONED"
        result["match"] = row.get("match") or "not_mentioned"
        result["reasons"].append("该地区未提及或显式缺席，不得用全国名填入")
        return result

    result["status"] = "UNRESOLVED"
    result["reasons"].append("无候选实体，保持未解析")
    return result


def selected_or_none(result):
    """只返回人工确认或目录已地方确认的选定值；有候选不等于已选定。"""
    if not isinstance(result, dict):
        return None
    return result.get("selected_crop_id")
