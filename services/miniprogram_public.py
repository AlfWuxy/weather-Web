# -*- coding: utf-8 -*-
"""小程序公开聚合：社区、已核验避暑点、GIS 元数据。不把社区指数当作个人风险。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from flask import current_app, url_for

from core.constants import DEFAULT_CITY_LABEL
from core.db_models import Community, CommunityDaily, CoolingResource, Pair, User
from core.extensions import db
from core.time_utils import utcnow
from services.cooling_service import compute_verify_status

CANONICAL_LOCATION_NAME = DEFAULT_CITY_LABEL
PUBLIC_AGGREGATE_MIN_SAMPLE = 10
_GIS_METADATA_CACHE = {"mtime_ns": None, "payload": None}


def _bucket_count(value):
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return None
    if count < PUBLIC_AGGREGATE_MIN_SAMPLE:
        return None
    if count < 20:
        return 10
    return (count // 10) * 10


def _bucket_rate(value):
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate):
        return None
    return round(max(0.0, min(rate, 1.0)), 2)


def public_communities_payload() -> dict:
    """社区级公开视图。样本不足时抑制行动率，避免把社区指数当成个人风险。"""
    communities = Community.query.order_by(Community.name.asc()).all()
    community_names = [community.name for community in communities]
    active_pair_counts = {}
    if community_names:
        active_pair_counts = {
            community_code: int(count or 0)
            for community_code, count in (
                db.session.query(
                    Pair.community_code,
                    db.func.count(db.distinct(Pair.caregiver_id)),
                )
                .join(User, User.id == Pair.caregiver_id)
                .filter(
                    Pair.status == "active",
                    Pair.community_code.in_(community_names),
                    User.deleted_at.is_(None),
                )
                .group_by(Pair.community_code)
                .all()
            )
        }
    latest_dates = db.session.query(
        CommunityDaily.community_code.label("community_code"),
        db.func.max(CommunityDaily.date).label("latest_date"),
    ).group_by(CommunityDaily.community_code).subquery()
    latest_ids = db.session.query(
        CommunityDaily.community_code.label("community_code"),
        db.func.max(CommunityDaily.id).label("latest_id"),
    ).join(
        latest_dates,
        (CommunityDaily.community_code == latest_dates.c.community_code)
        & (CommunityDaily.date == latest_dates.c.latest_date),
    ).group_by(CommunityDaily.community_code).subquery()
    latest_records = CommunityDaily.query.join(
        latest_ids,
        CommunityDaily.id == latest_ids.c.latest_id,
    ).all()
    latest_daily = {record.community_code: record for record in latest_records}
    items = []
    for community in communities:
        daily = latest_daily.get(community.name)
        count = int(daily.total_people or 0) if daily else 0
        active_count = active_pair_counts.get(community.name, 0)
        sample_suppressed = bool(
            daily
            and (
                count < PUBLIC_AGGREGATE_MIN_SAMPLE
                or active_count < PUBLIC_AGGREGATE_MIN_SAMPLE
            )
        )
        items.append(
            {
                "id": community.id,
                "name": community.name,
                "location": community.location,
                "latitude": community.latitude,
                "longitude": community.longitude,
                "population": community.population,
                "elderly_ratio": community.elderly_ratio,
                "vulnerability_index": community.vulnerability_index,
                "risk_level": community.risk_level,
                "latest_action_summary": (
                    {
                        "date": daily.date.isoformat(),
                        "total_people": None if sample_suppressed else _bucket_count(count),
                        "confirm_rate": None if sample_suppressed else _bucket_rate(daily.confirm_rate),
                        "escalation_rate": None if sample_suppressed else _bucket_rate(daily.escalation_rate),
                        "sample_suppressed": sample_suppressed,
                    }
                    if daily
                    else None
                ),
            }
        )
    return {
        "items": items,
        "summary": {
            "community_count": len(items),
            "scope": CANONICAL_LOCATION_NAME,
            "not_personal_risk": True,
        },
    }


def public_cooling_resources_payload() -> dict:
    """未核验点可出现在文字列表，但不带坐标、不进入已核验推荐。"""
    now = utcnow()
    records = CoolingResource.query.filter_by(is_active=True).order_by(
        CoolingResource.community_code.asc(), CoolingResource.name.asc()
    ).all()
    items = []
    for record in records:
        status = compute_verify_status(record, now)
        verified = status == "verified"
        items.append(
            {
                "id": record.id,
                "community_code": record.community_code,
                "name": record.name,
                "resource_type": record.resource_type,
                "address_hint": record.address_hint,
                "latitude": float(record.latitude) if verified and record.latitude is not None else None,
                "longitude": float(record.longitude) if verified and record.longitude is not None else None,
                "coordinate_system": "GCJ-02" if verified and record.latitude is not None else None,
                "open_hours": record.open_hours,
                "has_ac": bool(record.has_ac),
                "is_accessible": bool(record.is_accessible),
                "contact_hint": record.contact_hint,
                "notes": record.notes,
                "verify_status": status,
                "verified": verified,
            }
        )
    return {
        "items": items,
        "coordinate_system": "GCJ-02",
        "verified_only_coordinates": True,
    }


def public_gis_metadata_payload() -> dict:
    from services.heat_exposure_gis_service import PUBLIC_GEOJSON_FILENAME

    path = Path(current_app.static_folder) / PUBLIC_GEOJSON_FILENAME
    if not current_app.config.get("FEATURE_HEAT_EXPOSURE_GIS") or not path.exists():
        return {"available": False, "scope": CANONICAL_LOCATION_NAME, "hold": True}
    stat = path.stat()
    if _GIS_METADATA_CACHE.get("mtime_ns") != stat.st_mtime_ns:
        collection = json.loads(path.read_text(encoding="utf-8"))
        metadata = collection.get("metadata") if isinstance(collection, dict) else {}
        _GIS_METADATA_CACHE.update(mtime_ns=stat.st_mtime_ns, payload=metadata or {})
    metadata = _GIS_METADATA_CACHE.get("payload") or {}
    return {
        "available": True,
        "scope": CANONICAL_LOCATION_NAME,
        "geojson_url": url_for(
            "static",
            _external=False,
            filename=PUBLIC_GEOJSON_FILENAME,
            v=stat.st_mtime_ns,
        ),
        "title": metadata.get("title"),
        "schema_version": metadata.get("schema_version"),
        "size_bytes": stat.st_size,
        "generated_at": metadata.get("generated_at_utc"),
        "layers": metadata.get("layers") or {},
        "metadata": metadata,
        "hold": True,
        "disclaimer": "GIS 描述地区暴露，不解释个人健康结果，也不作为求助依据。",
    }


def public_community_bundle() -> dict:
    communities = public_communities_payload()
    cooling = public_cooling_resources_payload()
    try:
        gis = public_gis_metadata_payload()
    except (OSError, ValueError, json.JSONDecodeError):
        gis = {"available": False, "scope": CANONICAL_LOCATION_NAME, "hold": True}
    return {
        "communities": communities["items"],
        "summary": communities["summary"],
        "cooling": cooling["items"],
        "gis": gis,
        "source": "server_aggregated_deidentified",
        "not_personal_risk": True,
    }
