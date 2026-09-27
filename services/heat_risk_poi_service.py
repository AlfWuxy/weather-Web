# -*- coding: utf-8 -*-
"""工作台点位目录：保留来源与未知值，缓存静态空间匹配。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from core.time_utils import ensure_utc_aware, today_local, utcnow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTLEMENTS_PATH = PROJECT_ROOT / "data/gis/duchang_settlements.geojson"
RESOURCES_PATH = PROJECT_ROOT / "data/gis/duchang_resources.geojson"
RESOURCE_INVENTORY_PATH = PROJECT_ROOT / "data/gis/duchang_resource_inventory.json"


def stable_legacy_id(name):
    return "legacy-village:" + hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:16]


def priority_exclusion_reason(village):
    """身份未核验或网格数据缺失时保留地图展示，暂不自动安排巡访。"""
    reasons = []
    if village.get("potential_duplicate"):
        reasons.append("疑似同名点身份或坐标待核验，暂不进入自动巡访排名")
    if village.get("static_level") is None or village.get("risk_data_status") == "no_grid_data":
        reasons.append("网格数据缺失，仅供县级天气参考，暂不进入自动巡访排名")
    return "；".join(reasons) or None


def _stamp(path):
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), 0, 0


def _safe_url(value):
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"http", "https"} and parsed.netloc else None
    except ValueError:
        return None


def _coordinates(values):
    try:
        if len(values) < 2 or any(isinstance(v, bool) for v in values[:2]):
            return None
        lon, lat = float(values[0]), float(values[1])
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            return lon, lat
    except (TypeError, ValueError):
        pass
    return None


def _spatial_signature():
    from services import heat_risk_workbench_service as workbench

    return (_stamp(workbench.static_asset_path(workbench.CELLS_GEOJSON_FILENAME)),
            _stamp(workbench.static_asset_path(workbench.WORKBENCH_FILENAME)))


def _bounds(geometry):
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    points = [point for polygon in polygons for point in polygon[0]]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


@lru_cache(maxsize=4)
def _spatial_context(signature):
    from services import heat_risk_workbench_service as workbench

    # 生产版沿用冻结摘要校验，点位扩展不放宽原始网格数据的可信边界。
    collection = workbench.load_validated_public_geojson(Path(signature[0][0]))
    boundary = next(feature["geometry"] for feature in collection["features"]
                    if feature["properties"].get("feature_type") == "study_boundary")
    payload = workbench.load_workbench()
    townships = sorted(payload["townships"]["features"], key=lambda f: f["properties"]["name_zh"])
    return boundary, [(f["properties"]["name_zh"], _bounds(f["geometry"]), f["geometry"]) for f in townships], payload


@lru_cache(maxsize=16000)
def _match_point(lon, lat, signature):
    from services import heat_risk_workbench_service as workbench

    boundary, townships, payload = _spatial_context(signature)
    if not workbench._geometry_covers(lon, lat, boundary):
        return None
    township = None
    for name, (west, south, east, north), geometry in townships:
        if west <= lon <= east and south <= lat <= north and workbench._geometry_covers(lon, lat, geometry):
            township = name
            break
    cell_id = workbench.locate_cell(lon, lat)
    fields = payload["cells"]
    try:
        index = fields["cell_id"].index(cell_id)
    except ValueError:
        index = None
    return {
        "township": township, "cell_id": cell_id,
        "static_score": fields["score"][index] if index is not None else None,
        "static_level": fields["level"][index] if index is not None else None,
        "hotspot": fields["gi_bin"][index] if index is not None else 0,
        "facility_km": fields["facility_km"][index] if index is not None else None,
        "risk_data_status": "available" if index is not None and fields["level"][index] is not None else "no_grid_data",
    }


def _read_collection(stamp):
    if not stamp[1]:
        return {"features": [], "sources": [], "coverage_note": "该类点位目录尚未提供"}
    payload = json.loads(Path(stamp[0]).read_text(encoding="utf-8"))
    return payload if payload.get("type") == "FeatureCollection" else {"features": [], "sources": []}


def _normal_name(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _same_place(a, b, max_km):
    from services.heat_risk_workbench_service import haversine_km

    if not a.get("township") or a.get("township") != b.get("township"):
        return False
    names_a = {_normal_name(name) for name in [a.get("name"), *(a.get("aliases") or [])] if name}
    names_b = {_normal_name(name) for name in [b.get("name"), *(b.get("aliases") or [])] if name}
    return bool(names_a & names_b) and haversine_km(a["lon_wgs84"], a["lat_wgs84"], b["lon_wgs84"], b["lat_wgs84"]) <= max_km


def _merge_sources(base, extra):
    result = dict(base)
    sources = [*(base.get("sources") or []), *(extra.get("sources") or [])]
    for point in (base, extra):
        if point.get("source_url"):
            sources.append({"id": point.get("source_id"), "name": point.get("source_label"), "url": point["source_url"]})
    merged_sources = {}
    for item in sources:
        source_key = str(item.get("url") or item.get("id"))
        retained = merged_sources.setdefault(source_key, {})
        # 简略的来源链接只能补缺，不能覆盖上游保留的坐标、日期和许可。
        for key, value in item.items():
            if retained.get(key) is None:
                retained[key] = value
    result["sources"] = list(merged_sources.values())
    if not result.get("source_url"):
        result["source_url"] = extra.get("source_url")
    for field in ("official_record_ids", "inventory_ids"):
        left, right = base.get(field) or [], extra.get(field) or []
        left = [left] if isinstance(left, str) else left
        right = [right] if isinstance(right, str) else right
        if left or right:
            result[field] = sorted(set(left) | set(right))
    if base.get("potential_duplicate") or extra.get("potential_duplicate"):
        result["potential_duplicate"] = True
        result["potential_duplicate_ids"] = sorted(set(base.get("potential_duplicate_ids") or []) | set(extra.get("potential_duplicate_ids") or []))
    return result


def _deduplicate(points, max_km):
    result, by_name, seen_ids = [], {}, set()
    for point in sorted(points, key=lambda p: p["id"]):
        if point["id"] in seen_ids:
            continue
        seen_ids.add(point["id"])
        names = {_normal_name(n) for n in [point["name"], *(point.get("aliases") or [])]}
        candidates = {i for name in names for i in by_name.get((point.get("township"), name), [])}
        matched = next((i for i in sorted(candidates) if _same_place(result[i], point, max_km)), None)
        if matched is not None:
            result[matched] = _merge_sources(result[matched], point)
            continue
        index = len(result)
        result.append(point)
        for name in names:
            by_name.setdefault((point.get("township"), name), []).append(index)
    return result


def _link_inventory(points, inventory):
    """只按显式记录ID或同乡唯一同名关联，保留待复核记录，原始名录不改动。"""
    linked_ids, output = set(), []
    for original in points:
        point = dict(original)
        ids = point.get("official_record_ids") or point.get("inventory_ids") or []
        ids = {ids} if isinstance(ids, str) else set(ids)
        names = {_normal_name(name) for name in [point["name"], *(point.get("aliases") or [])]}
        eligible = [row for row in inventory if row.get("verification_status") != "needs_review"
                    and ((point.get("kind") == "medical" and row.get("kind") == "medical")
                         or (point.get("kind") in {"cooling_candidate", "cooling_verified"} and row.get("kind") in {"cooling_candidate", "cooling_verified", "cooling"}))]
        matches = [row for row in eligible if row.get("id") in ids]
        if not matches and not ids and point.get("township"):
            named = [row for row in eligible if row.get("township_name") == point["township"] and _normal_name(row.get("name")) in names]
            matches = named if len(named) == 1 else []
        if matches:
            linked = sorted(str(row["id"]) for row in matches)
            point["official_record_ids"] = linked
            linked_ids.update(linked)
            for row in matches:
                point = _merge_sources(point, {"source_url": row["source_url"], "source_label": "官方列名资源目录", "source_id": row["id"]})
        output.append(point)
    return output, linked_ids


@lru_cache(maxsize=4)
def _static_catalog(settlement_stamp, resource_stamp, inventory_stamp, signature):
    catalogs = [_read_collection(settlement_stamp), _read_collection(resource_stamp)]
    points, rejected = [], 0
    source_labels = {source.get("id"): source.get("name") for catalog in catalogs for source in catalog.get("sources", [])}
    for catalog in catalogs:
        for feature in catalog.get("features", []):
            props, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
            coords = _coordinates(geometry.get("coordinates")) if geometry.get("type") == "Point" else None
            crs = str(props.get("coordinate_system") or "WGS84").upper().replace(" ", "")
            point_id = feature.get("id") or props.get("id")
            source_url = _safe_url(props.get("source_url"))
            if not coords or not point_id or not props.get("name") or not source_url or crs not in {"WGS84", "WGS-84", "EPSG:4326"}:
                rejected += 1
                continue
            match = _match_point(*coords, signature)
            if match is None:
                rejected += 1
                continue
            point = {**props, **match, "id": str(point_id), "name": str(props["name"]),
                     "lon_wgs84": coords[0], "lat_wgs84": coords[1], "coordinate_system": "WGS84",
                     "source_township": props.get("township_name"), "source_url": source_url,
                     "source_label": source_labels.get(props.get("source_id")) or {"osm": "OpenStreetMap", "geonames": "GeoNames"}.get(props.get("source_type")) or props.get("source_type") or "公开点位目录",
                     "verification_status": props.get("verification_status") or "pending",
                     "coordinate_precision": props.get("coordinate_precision") or "approximate",
                     "settlement_level": (props.get("settlement_level") or "unknown") if props.get("kind") == "settlement" else None,
                     "population": None, "elderly_ratio": None}
            if props.get("kind") != "settlement":
                for key in ("type", "open_hours", "has_ac", "is_accessible", "verified_at", "valid_until"):
                    point.setdefault(key, None)
            if match["township"] is None:
                point["township_warning"] = "点位位于县内乡界缝隙，乡镇归属待核验"
            elif props.get("township_name") and props["township_name"] != match["township"]:
                point["township_warning"] = f"来源标注为{props['township_name']}，空间落界为{match['township']}，归属待核验"
            points.append(point)
    # 冻结模型中的唯一真实医疗点参与目录去重；其余驻地仅作为可达性参考点。
    for facility in _spatial_context(signature)[2]["facilities"]:
        if facility.get("precision") != "exact":
            continue
        match = _match_point(facility["lon"], facility["lat"], signature)
        if match is None:
            continue
        osm = re.search(r"OSM (node|way|relation) (\d+)", facility.get("source", ""))
        if not osm:
            continue
        osm_type, osm_id = osm.groups()
        points.append({**match, "id": f"osm:{osm_type}:{osm_id}", "name": facility["name"], "kind": "medical",
                       "lon_wgs84": facility["lon"], "lat_wgs84": facility["lat"], "coordinate_system": "WGS84",
                       "source_id": "osm", "source_label": "OpenStreetMap", "source_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
                       "coordinate_source": facility["source"],
                       "verification_status": "publicly_listed", "coordinate_precision": "building_centroid",
                       "settlement_level": None, "type": facility.get("kind"), "open_hours": None,
                       "has_ac": None, "is_accessible": None, "verified_at": None, "valid_until": None})
    # 导入目录已保留待核验同名点；不能把近距离当成同一聚落的证据。
    settlements = list({p["id"]: p for p in points if p.get("kind") == "settlement"}.values())
    settlements.sort(key=lambda p: p["id"])
    medical = _deduplicate([p for p in points if p.get("kind") == "medical"], 0.1)
    cooling = _deduplicate([p for p in points if p.get("kind") in {"cooling_candidate", "cooling_verified"}], 0.1)
    sources = [source for catalog in catalogs for source in catalog.get("sources", [])]
    sources = list({str(source.get("id") or source.get("url")): source for source in sources}.values())
    metadata = {"schema_version": "1.0", "sources": sources,
                "retrieved_at": max((str(c.get("retrieved_at") or "") for c in catalogs), default="") or None,
                "coverage_note": "；".join(str(c.get("coverage_note")) for c in catalogs if c.get("coverage_note")),
                "verification_note": "收录不代表完整覆盖；公开列名不代表仍在营业，避暑候选点不代表已开放。",
                "rejected_points": rejected}
    inventory = json.loads(Path(inventory_stamp[0]).read_text(encoding="utf-8")) if inventory_stamp[1] else {}
    inventory_rows = inventory if isinstance(inventory, list) else inventory.get("records", inventory.get("items", []))
    townships = {name for name, _, _ in _spatial_context(signature)[1]}
    unmapped = [{**row, "source_township_name": row.get("township_name"),
                "township_name": row.get("township_name") if row.get("township_name") in townships else None}
               for row in inventory_rows if row.get("status") == "unmapped"
               and row.get("name") and _safe_url(row.get("source_url"))]
    if isinstance(inventory, dict):
        inventory_sources = [{"id": "inventory:" + hashlib.sha256(row["source_url"].encode("utf-8")).hexdigest()[:12],
                              "name": str(row.get("source_label") or "公开列名资源目录"), "url": row["source_url"], "license": None}
                             for row in unmapped]
        metadata["sources"] = list({str(source.get("url") or source.get("id")): source
                                    for source in [*sources, *inventory.get("sources", []), *inventory_sources]}.values())
        metadata["inventory_note"] = "官方列名条目未获得可信坐标；与地图条目可能重叠，计数不能相加作为机构总数。拟注销及类别待核验记录单独标注。"
    medical, linked_medical = _link_inventory(medical, unmapped)
    cooling, linked_cooling = _link_inventory(cooling, unmapped)
    linked = linked_medical | linked_cooling
    unmapped = [row for row in unmapped if str(row.get("id")) not in linked]
    metadata["inventory_linked_count"] = len(linked)
    return settlements, medical, cooling, unmapped, metadata


def _valid_cooling_dates(verified_at, valid_until):
    try:
        verified = datetime.fromisoformat(str(verified_at).replace("Z", "+00:00"))
        expires = date.fromisoformat(str(valid_until)[:10])
        return ensure_utc_aware(verified) <= utcnow() + timedelta(minutes=5) and today_local() <= expires and verified.date() <= expires
    except (TypeError, ValueError):
        return False


def cooling_row_points(rows):
    """后台资源仅接收明确坐标系及有效人工核验，不读取联系人或内部备注。"""
    from flask import current_app, has_app_context
    from services.heat_risk_workbench_service import gcj02_to_wgs84

    ttl = current_app.config.get("COOLING_COORDINATE_VERIFICATION_TTL_DAYS", 365) if has_app_context() else 365
    try:
        ttl = max(30, min(int(ttl), 730))
    except (TypeError, ValueError):
        ttl = 365
    result = []
    for row in rows:
        coords = _coordinates([getattr(row, "longitude", None), getattr(row, "latitude", None)])
        crs = str(getattr(row, "coordinate_system", "") or "").upper().replace(" ", "")
        source = str(getattr(row, "coordinate_source", "") or "").strip()
        verified = getattr(row, "coordinate_verified_at", None)
        if not coords or not source or not verified or not getattr(row, "is_active", False):
            continue
        try:
            verified = ensure_utc_aware(verified)
            if verified > utcnow() + timedelta(minutes=5) or utcnow() - verified > timedelta(days=ttl):
                continue
        except (TypeError, ValueError, AttributeError):
            continue
        if crs in {"GCJ-02", "GCJ02"}:
            coords = gcj02_to_wgs84(*coords)
        elif crs not in {"WGS84", "WGS-84", "EPSG:4326"}:
            continue
        match = _match_point(*coords, _spatial_signature())
        if match is None:
            continue
        result.append({**match, "id": f"cooling-db:{row.id}", "name": row.name, "kind": "cooling_verified",
                       "lon_wgs84": coords[0], "lat_wgs84": coords[1], "coordinate_system": "WGS84",
                       "coordinate_source": source, "coordinate_precision": "mapped_point",
                       "source_url": _safe_url(source), "source_label": "后台人工核验", "verification_status": "verified",
                       "settlement_level": None, "cooling_status": "verified", "type": row.resource_type,
                       "open_hours": row.open_hours, "has_ac": row.has_ac, "is_accessible": row.is_accessible,
                       "verification_note": "点位已核验，开放情况请确认",
                       "verified_at": verified.isoformat(), "valid_until": (verified + timedelta(days=ttl)).date().isoformat()})
    return result


def build_poi_payload(legacy_villages, cooling_rows=()):
    """把静态目录和原16村合并；新聚落不继承仅按旧村名维护的人口档案。"""
    from services.heat_risk_workbench_service import haversine_km

    signature = _spatial_signature()
    settlements, medical, cooling, unmapped, metadata = _static_catalog(
        _stamp(SETTLEMENTS_PATH), _stamp(RESOURCES_PATH), _stamp(RESOURCE_INVENTORY_PATH), signature)
    villages = [dict(village) for village in legacy_villages]
    for point in settlements:
        match = next((i for i, legacy in enumerate(villages[:len(legacy_villages)]) if _same_place(legacy, point, 0.25)), None)
        if match is not None:
            villages[match] = _merge_sources(villages[match], point)
        else:
            villages.append(dict(point))
    # 已收录真实医疗点的距离只作目录参考，不改变冻结的静态分与可达性分量。
    for village in villages:
        village["priority_exclusion_reason"] = priority_exclusion_reason(village)
        village["priority_eligible"] = village["priority_exclusion_reason"] is None
        village["nearest_medical_km"] = round(min(
            (haversine_km(village["lon_wgs84"], village["lat_wgs84"], p["lon_wgs84"], p["lat_wgs84"]) for p in medical),
            default=math.inf), 2) if medical else None
    verified, candidates = cooling_row_points(cooling_rows), []
    for resource in cooling:
        point = dict(resource)
        if point.get("kind") == "cooling_verified" and point.get("cooling_status") == "verified" and point.get("verification_status") in {"verified", "publicly_listed"} and _valid_cooling_dates(point.get("verified_at"), point.get("valid_until")):
            point["verification_note"] = "点位已核验，开放情况请确认"
            verified.append(point)
        else:
            point.update(kind="cooling_candidate", cooling_status="candidate")
            candidates.append(point)
    verified = _deduplicate(verified, 0.1)
    candidates = [p for p in candidates if not any(_same_place(p, v, 0.1) for v in verified)]
    coverage = [{"township": township, "settlement_count": sum(v.get("township") == township for v in villages),
                 "medical_count": sum(v.get("township") == township for v in medical),
                 "cooling_candidate_count": sum(v.get("township") == township for v in candidates),
                 "cooling_verified_count": sum(v.get("township") == township for v in verified),
                 "unmapped_medical_count": sum(v.get("township_name") == township and v.get("kind") == "medical" and v.get("verification_status") != "needs_review" for v in unmapped),
                 "unmapped_cooling_count": sum(v.get("township_name") == township and v.get("kind") in {"cooling_candidate", "cooling_verified", "cooling"} and v.get("verification_status") != "needs_review" for v in unmapped),
                 "needs_review_count": sum(v.get("township_name") == township and v.get("verification_status") == "needs_review" for v in unmapped),
                 "completeness": "unknown"}
                for township, _, _ in _spatial_context(signature)[1]]
    metadata = {**metadata, "counts": {"settlements": len(villages), "medical": len(medical),
                "cooling_candidates": len(candidates), "cooling_verified": len(verified),
                "unassigned_township": sum(v.get("township") is None for v in villages),
                "potential_duplicates": sum(bool(v.get("potential_duplicate")) for v in villages),
                "priority_excluded": sum(not v["priority_eligible"] for v in villages),
                "township_warnings": sum(bool(v.get("township_warning")) for v in [*villages, *medical, *candidates, *verified]),
                "unmapped_unassigned_township": sum(v.get("township_name") is None for v in unmapped),
                "needs_review_resources": sum(v.get("verification_status") == "needs_review" for v in unmapped),
                "unmapped_resources": len(unmapped)}}
    return {"villages": villages, "medical_pois": medical, "cooling_candidates": candidates,
            "cooling_resources": verified, "unmapped_resources": unmapped,
            "poi_coverage": coverage, "poi_metadata": metadata}
