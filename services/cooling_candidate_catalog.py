# -*- coding: utf-8 -*-
"""合并只读纳凉候选目录，历史证据与当前开放状态分开呈现。"""

import json
import logging
import math
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AMAP_CANDIDATES_PATH = PROJECT_ROOT / "data/cooling_resource_candidates.json"
GIS_INVENTORY_PATH = PROJECT_ROOT / "data/gis/duchang_resource_inventory.json"
GIS_RESOURCES_PATH = PROJECT_ROOT / "data/gis/duchang_resources.geojson"
PUBLIC_CANDIDATE_LIMIT = 200
ROLE_LABELS = {"cooling_candidate": "候选公共纳凉场所", "service_candidate": "候选志愿服务点"}
CATEGORY_LABELS = {"public_culture": "公共文化场所", "community_service": "社区服务场所", "volunteer_service": "志愿服务组织"}
logger = logging.getLogger(__name__)


def _text(value, limit=500):
    if isinstance(value, list):
        value = "；".join(str(item) for item in value if item)
    return str(value or "").strip()[:limit]


def _source_url(value):
    value = _text(value, 1000)
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _rows(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, list) else []


def _public_classification(role, category):
    return (isinstance(role, str) and role in ROLE_LABELS
            and isinstance(category, str) and category in CATEGORY_LABELS)


@lru_cache(maxsize=12)
def _read_cached(path, mtime_ns, size):
    del mtime_ns, size
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read(path):
    try:
        stat = path.stat()
        payload = _read_cached(str(path), stat.st_mtime_ns, stat.st_size)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        logger.warning("候选资料暂不可读: %s", path.name)
        return {}


def _valid_coordinates(lon, lat):
    try:
        if isinstance(lon, bool) or isinstance(lat, bool):
            return None
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    from services.heat_risk_workbench_service import haversine_km

    return (lon, lat) if haversine_km(lon, lat, 116.20, 29.27) <= 80 else None


def _location_verification(row):
    """地点核验只说明位置与项目负责人确认，不授予空调、营业时段或人工核验有效期。"""
    verified = (row.get("location_verification_status") == "verified"
                and row.get("location_verification_method") == "amap_poi_and_user_confirmation")
    return {
        "location_verification_status": "verified" if verified else "pending",
        "location_verification_method": "amap_poi_and_user_confirmation" if verified else None,
        "location_verified_at": _text(row.get("location_verified_at"), 32) if verified else None,
        "location_verification_note": _text(row.get("location_verification_note")) if verified else "",
        "public_access_confirmation": "user_confirmed" if verified and row.get("public_access_confirmation") == "user_confirmed" else None,
    }


def _legacy_items(payload):
    if payload.get("publication_status") != "candidate_only" or payload.get("coordinate_system") != "GCJ-02":
        return []
    items = []
    for row in _rows(payload, "items"):
        if not isinstance(row, dict) or row.get("verification_status") != "pending_human_verification" or row.get("is_active") is not False:
            continue
        if not row.get("source_id") or not row.get("name"):
            continue
        coords = _valid_coordinates(row.get("longitude"), row.get("latitude"))
        item = {**row, "source_id": _text(row["source_id"], 64), "name": _text(row["name"], 120),
                "source_label": _text(row.get("source_label")) or "高德公开 POI",
                "source_url": _source_url(row.get("source_url")), "source_date": payload.get("generated_at"),
                "audience_hint": _text(row.get("audience_hint")) or "接待人群与条件待确认",
                "facilities_hint": _text(row.get("facilities_hint")) or "空调、无障碍等设施待核验",
                "verification_note": "POI 信息不能证明当前开放、允许公众纳凉或适合行动不便老人。",
                "current_opening_status": "unknown", "has_ac": None, "is_accessible": None,
                "longitude": coords[0] if coords else None, "latitude": coords[1] if coords else None,
                "coordinate_system": "GCJ-02" if coords else None,
                "coordinate_source": f"高德 Place Text API v5 候选 {row['source_id']}，仍需管理员现场或电话人工核验" if coords else "",
                "prefill_open_hours": _text(row.get("opening_hours_hint"), 160), "catalog_origin": "amap",
                **_location_verification(row)}
        items.append(item)
    return items


def _mapped_inventory_points(payload):
    from services.heat_risk_workbench_service import wgs84_to_gcj02

    points = {}
    for feature in _rows(payload, "features"):
        if not isinstance(feature, dict):
            continue
        props, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
        if not isinstance(props, dict) or not isinstance(geometry, dict):
            continue
        if props.get("kind") != "cooling_candidate" or geometry.get("type") != "Point":
            continue
        crs = str(props.get("coordinate_system") or "WGS84").upper().replace(" ", "")
        values = geometry.get("coordinates")
        if crs not in {"WGS84", "WGS-84", "EPSG:4326"} or not isinstance(values, list) or len(values) < 2:
            continue
        coordinates = _valid_coordinates(*values[:2])
        if not coordinates:
            continue
        coordinates = wgs84_to_gcj02(*coordinates)
        linked = props.get("inventory_ids") or props.get("official_record_ids") or []
        linked = [linked] if isinstance(linked, str) else list(linked) if isinstance(linked, list) else []
        linked.append(feature.get("id") or props.get("id"))
        coordinate_url = _source_url(props.get("coordinate_source_url"))
        coordinate_source = "GIS 公开点位 WGS84 转 GCJ-02"
        if props.get("source_id"):
            coordinate_source += "；POI " + _text(props["source_id"], 64)
        if coordinate_url:
            coordinate_source += "；" + coordinate_url[:250]
        coordinate_source = (coordinate_source + "；" + _text(props.get("coordinate_source"), 150))[:500]
        for record_id in linked:
            if record_id:
                points.setdefault(str(record_id), {"longitude": round(coordinates[0], 6), "latitude": round(coordinates[1], 6),
                    "coordinate_system": "GCJ-02", "coordinate_source": coordinate_source,
                    "coordinate_source_url": coordinate_url, "map_source_id": props.get("source_id")})
    return points


def load_cooling_candidate_catalog(amap_path=None):
    """只加入明确允许公开预览的官方候选，读取过程不接触数据库。"""
    legacy = _read(Path(amap_path or AMAP_CANDIDATES_PATH))
    inventory = _read(GIS_INVENTORY_PATH)
    mapped = _mapped_inventory_points(_read(GIS_RESOURCES_PATH))
    items = []
    for row in _rows(inventory, "records"):
        if not isinstance(row, dict) or row.get("public_preview") is not True or row.get("kind") != "cooling_candidate":
            continue
        if not _public_classification(row.get("public_role"), row.get("category")) or row.get("is_active") is True:
            continue
        record_id, name = _text(row.get("id"), 64), _text(row.get("name"), 120)
        if not record_id or not name:
            continue
        items.append({"source_id": record_id, "name": name, "category": row["category"], "public_role": row["public_role"],
                      "address": _text(row.get("address"), 200), "opening_hours_hint": _text(row.get("opening_hours_hint"), 160),
                      "source_label": _text(row.get("source_label")) or "官方公开资料",
                      "source_url": _source_url(row.get("source_url")), "source_date": row.get("source_date"),
                      "audience_hint": _text(row.get("audience_hint")), "facilities_hint": _text(row.get("facilities_hint")),
                      "verification_note": _text(row.get("verification_note")) or "历史资料仅供核验，当前开放与接待条件未知。",
                      "current_opening_status": "unknown", "verification_status": "pending_human_verification", "is_active": False,
                      "has_ac": None, "is_accessible": None, "latitude": None, "longitude": None, "coordinate_system": None,
                      "coordinate_source": "", "prefill_open_hours": "", "catalog_origin": "official_inventory",
                      **mapped.get(record_id, {}), **_location_verification(row)})
    # 新目录复用旧高德 ID 时保留新核验记录，避免旧预览覆盖或重复展示。
    inventory_ids = {item["source_id"] for item in items}
    items.extend(item for item in _legacy_items(legacy) if item["source_id"] not in inventory_ids)
    return {"publication_status": "candidate_only", "coordinate_system": "GCJ-02（仅有坐标的管理预填）",
            "generated_at": inventory.get("collected_at") or legacy.get("generated_at"),
            "notice": "地点核验与纳凉设施核验分别记录；已核验地点保留高德来源及项目负责人确认，开放时间、空调和无障碍条件以各条记录为准。",
            "items": list({item["source_id"]: item for item in items}.values())}


def public_candidate_previews(payload):
    """公开文字字段白名单：不输出坐标、候选ID、地图查询或管理员预填来源。"""
    result = []
    for item in _rows(payload, "items"):
        if not isinstance(item, dict) or not _text(item.get("name"), 120):
            continue
        role, category = item.get("public_role"), item.get("category")
        if not _public_classification(role, category):
            continue
        if item.get("verification_status") != "pending_human_verification" or item.get("is_active") is not False:
            continue
        result.append({"name": _text(item.get("name"), 120), "address": _text(item.get("address"), 200),
                       "opening_hours_hint": _text(item.get("opening_hours_hint"), 160),
                       "role_label": ROLE_LABELS[role], "category_label": CATEGORY_LABELS[category],
                       "source_label": item.get("source_label"), "source_url": _source_url(item.get("source_url")),
                       "source_date": item.get("source_date"), "audience_hint": item.get("audience_hint"),
                       "facilities_hint": item.get("facilities_hint"), "verification_note": item.get("verification_note"),
                       "current_opening_status": "unknown", **_location_verification(item)})
    return result[:PUBLIC_CANDIDATE_LIMIT]
