"""社区作物、农事查找与计量契约；目录本身不授权农技或健康建议。"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any

from .models import InputError, is_placeholder_source

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "community_catalog.json"


def get_community_catalog() -> dict:
    """返回独立副本，可由 GET /api/catalog 直接序列化。"""
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def catalog_summary() -> dict:
    catalog = get_community_catalog()
    return {
        "catalog_version": catalog["catalog_version"],
        "categories": len(catalog["categories"]),
        "vegetable_and_adjacent_categories": sum(c["id"].startswith("V") for c in catalog["categories"]),
        "grain_oil_categories": sum(c["id"].startswith("G") for c in catalog["categories"]),
        "crop_entities": len(catalog["crops"]),
        "lifecycle_stages": len(catalog["lifecycle"]),
        "task_templates": len(catalog["tasks"]),
        "locally_orally_mentioned": [x["local_name"] for x in catalog["local_observations"] if x["status"] == "oral_mention"],
        "field_validated_crop_count": 0,
        "claim": catalog["claim"],
        "limits": catalog["coverage_limits"],
    }


def resolve_crop(query: str) -> dict:
    """只解析目录；地方歧义保留候选，不自动完成现场身份确认。"""
    if not isinstance(query, str) or not query.strip():
        raise InputError("crop: 必须提供非空作物名或目录编号")
    catalog = get_community_catalog()
    name = query.strip()
    if name in catalog["ambiguous_names"]:
        ids = catalog["ambiguous_names"][name]
        return {"query": name, "status": "identity_hold", "crop_id": None,
                "candidates": [c for c in catalog["crops"] if c["id"] in ids],
                "recommendation_enabled": False}
    candidates = [c for c in catalog["crops"] if name == c["id"] or name == c["name"]
                  or name in c["name"].split("/") or name in c["aliases"]]
    status = "catalog_match" if len(candidates) == 1 else "identity_hold" if candidates else "not_catalogued"
    if len(candidates) == 1 and candidates[0]["local_status"] in {"identity_hold", "planting_unconfirmed"}:
        status = candidates[0]["local_status"]
    return {"query": name, "status": status,
            "crop_id": candidates[0]["id"] if status == "catalog_match" else None,
            "candidates": candidates, "recommendation_enabled": False}


def get_task_template(task_code: str) -> dict:
    for task in get_community_catalog()["tasks"]:
        if task["code"] == task_code:
            return task
    raise InputError("task_code: 不在农事目录中")


def _positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise InputError(f"{field}: 必须是有限正数")
    return float(value)


def validate_task_quantity(task_code: str, quantity: float, unit: str,
                           context: dict | None = None) -> dict:
    """核对任务计量与搬运信息；不把任务目录中的空位升级为可排程。"""
    task = get_task_template(task_code)
    value = _positive(quantity, "quantity")
    if unit not in task["valid_units"]:
        raise InputError(f"unit: {task_code} 适用单位为 {', '.join(task['valid_units'])}，不自动换算")
    if unit in {"plant", "trip"} and not value.is_integer():
        raise InputError("quantity: 株或趟必须是整数")
    if context is not None and not isinstance(context, dict):
        raise InputError("context: 必须为对象")
    context = deepcopy(context or {})
    missing = [key for key in task["required_context"] if context.get(key) in (None, "", "unknown", "待确认")]
    if "method" in context and context["method"] not in task["methods"]:
        raise InputError("context.method: 不在该任务已列做法中；需扩展目录后再纳入同口径比较")
    if task_code.startswith("haul_"):
        for key in ("load_kg_per_trip", "distance_m"):
            if key not in missing:
                _positive(context[key], f"context.{key}")
    return {"valid": True, "task_code": task_code, "quantity": value, "unit": unit,
            "missing_context": missing,
            "scheduler_unit_supported": unit in task["scheduler_units"],
            "support_level": task["support_level"], "recommendation_enabled": False,
            "hazard_boundary": task["hazard_boundary"]}


def estimate_from_user_range(task_code: str, quantity: float, unit: str,
                             minutes_per_unit: dict, context: dict | None = None) -> dict:
    """只计算有出处的输入区间；不生成地方历、个人系数或线性多人提速。"""
    checked = validate_task_quantity(task_code, quantity, unit, context)
    if not isinstance(minutes_per_unit, dict):
        raise InputError("minutes_per_unit: 必须是对象")
    allowed = {"low", "high", "unit", "scope", "source", "basis", "record_ids"}
    if set(minutes_per_unit) - allowed:
        raise InputError("minutes_per_unit: 存在未知字段")
    rate = minutes_per_unit
    if rate.get("basis") not in {"user_range", "field_records"}:
        raise InputError("minutes_per_unit.basis: 只能是 user_range 或 field_records")
    if not isinstance(rate.get("source"), str) or is_placeholder_source(rate["source"]):
        raise InputError("minutes_per_unit.source: 必须说明谁给出区间或对应实测来源")
    if rate.get("scope") not in {"net_work", "elapsed"}:
        raise InputError("minutes_per_unit.scope: 必须明确 net_work 或 elapsed")
    if rate.get("unit") != unit:
        raise InputError("minutes_per_unit.unit: 必须与任务数量单位一致")
    low, high = _positive(rate.get("low"), "low"), _positive(rate.get("high"), "high")
    if low > high:
        raise InputError("minutes_per_unit: low 不得大于 high")
    refs = rate.get("record_ids", [])
    if rate["basis"] == "field_records" and (not isinstance(refs, list) or not refs
                                             or any(not isinstance(x, str) or not x.strip() for x in refs)):
        raise InputError("record_ids: 实测区间必须列出可核查记录；本接口不认证这些记录")
    result_low, result_high = quantity * low, quantity * high
    if not math.isfinite(result_low) or not math.isfinite(result_high):
        raise InputError("estimate: 结果溢出")
    return {**checked, "low_minutes": result_low, "high_minutes": result_high,
            "scope": rate["scope"], "source": rate["source"], "basis": rate["basis"],
            "record_ids": refs, "interval_kind": "input_range_not_calibrated_probability",
            "field_evidence_verified": False,
            "note": "净劳动与准备、往返、等待、休息分别记录；此结果不是健康许可"}
