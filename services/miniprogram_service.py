# -*- coding: utf-8 -*-
"""小程序快照、公开聚合资源与都昌县语义服务。"""

from __future__ import annotations

import json
import math
import uuid
from datetime import timedelta
from pathlib import Path

from flask import current_app, url_for

from core.constants import DEFAULT_CITY_LABEL
from core.db_models import (
    Community,
    CommunityDaily,
    CoolingResource,
    ForecastCache,
    MiniProgramSnapshot,
    Pair,
    User,
    WeatherCache,
)
from core.extensions import db
from core.time_utils import ensure_utc_aware, utcnow
from core.weather import get_qweather_forecast_with_cache
from services.qweather_auth import is_qweather_configured
from services.miniprogram_auth import current_privacy_version
from services.community_daily_service import (
    PUBLIC_AGGREGATE_MIN_SAMPLE,
    bucket_public_count,
    bucket_public_rate,
)
from services.user._common import _action_plan
from utils.parsers import safe_json_loads


SNAPSHOT_TTL_SECONDS = 1800
CANONICAL_LOCATION_NAME = DEFAULT_CITY_LABEL
CANONICAL_LOCATION_CODE = "116.20,29.27"
_GIS_METADATA_CACHE = {"mtime_ns": None, "payload": None}
_SNAPSHOT_RETENTION_LOCK_ID = 1836086096


def _acquire_snapshot_retention_lock(*, dialect_name=None, execute=None):
    """PostgreSQL 中串行化快照写入，防止并发事务突破保留上限。"""
    effective_dialect = dialect_name or db.engine.dialect.name
    if effective_dialect != "postgresql":
        return False
    executor = execute or db.session.execute
    executor(
        db.text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _SNAPSHOT_RETENTION_LOCK_ID},
    )
    return True


def canonical_location() -> dict:
    """所有小程序天气功能只声明都昌县县级范围。"""
    code = str(
        current_app.config.get("QWEATHER_CANONICAL_LOCATION")
        or current_app.config.get("DEFAULT_LOCATION")
        or CANONICAL_LOCATION_CODE
    ).strip()
    return {"name": CANONICAL_LOCATION_NAME, "code": code, "scope": "county"}


def qweather_runtime_configured() -> bool:
    """必须同时具备认证材料和 HTTPS API Host 才允许后台同步。"""
    return bool(current_app.config.get("QWEATHER_API_BASE")) and is_qweather_configured(current_app.config)


def _finite_number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _weather_available(current) -> bool:
    if not isinstance(current, dict) or current.get("is_mock") or current.get("is_demo"):
        return False
    return _finite_number(current.get("temperature")) is not None


def _risk_and_actions(current, warnings):
    """用已同步数据生成面向行动的稳定分级，不读取个人病历。"""
    if not _weather_available(current):
        reasons = ["天气快照尚未可用"]
        return (
            {
                "level": "未知",
                "score": None,
                "summary": reasons[0],
                "reasons": reasons,
                "disclaimer": "仅作天气健康行动提醒，不提供医疗诊断。",
            },
            [],
        )

    temperature = _finite_number(current.get("temperature"))
    temperature_max = _finite_number(current.get("temperature_max"))
    humidity = _finite_number(current.get("humidity"))
    aqi = _finite_number(current.get("aqi"))
    observed = temperature_max if temperature_max is not None else temperature
    score = 12.0
    reasons = []
    if observed is not None and observed >= 40:
        score += 78
        reasons.append("最高温达到或超过 40°C")
    elif observed is not None and observed >= 37:
        score += 62
        reasons.append("最高温达到或超过 37°C")
    elif observed is not None and observed >= 35:
        score += 46
        reasons.append("最高温达到或超过 35°C")
    elif observed is not None and observed >= 32:
        score += 24
        reasons.append("天气较热")
    if humidity is not None and humidity >= 80:
        score += 8
        reasons.append("湿度偏高")
    if aqi is not None and aqi >= 150:
        score += 12
        reasons.append("空气质量风险偏高")
    if isinstance(warnings, list) and warnings:
        score += 12
        reasons.append("当前存在官方气象预警")
    score = round(min(max(score, 0.0), 100.0), 1)
    if score >= 80:
        level = "极高"
    elif score >= 60:
        level = "高风险"
    elif score >= 35:
        level = "中风险"
    else:
        level = "低风险"
    final_reasons = reasons or ["当前未触发主要天气风险阈值"]
    return (
        {
            "level": level,
            "score": score,
            "summary": "；".join(final_reasons[:2]),
            "reasons": final_reasons,
            "disclaimer": "仅作天气健康行动提醒，不提供医疗诊断。",
        },
        _action_plan(level),
    )


def _enrich_forecast_risk(items):
    """逐日风险只做非医疗天气行动分级，不增加任何外部请求。"""
    enriched = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        temperature_mean = item.get("temperature_mean")
        if temperature_mean is None:
            tmax = _finite_number(item.get("temperature_max"))
            tmin = _finite_number(item.get("temperature_min"))
            if tmax is not None and tmin is not None:
                temperature_mean = round((tmax + tmin) / 2, 1)
        proxy = {
            "temperature": temperature_mean,
            "temperature_max": item.get("temperature_max"),
            "temperature_min": item.get("temperature_min"),
            "humidity": item.get("humidity"),
            "aqi": item.get("aqi"),
            "data_source": item.get("data_source") or item.get("source") or "QWeather",
            "is_mock": bool(item.get("is_mock")),
        }
        risk, _actions = _risk_and_actions(proxy, [])
        available = risk.get("score") is not None
        item.update(
            risk_available=available,
            risk_score=risk.get("score"),
            risk_level=risk.get("level"),
            reasons=risk.get("reasons") or [],
        )
        enriched.append(item)
    return enriched


def _source_status(
    current,
    forecast,
    warnings,
    forecast_meta=None,
    warning_status=None,
) -> dict:
    source = str((current or {}).get("data_source") or (current or {}).get("source") or "").strip()
    forecast_sources = sorted(
        {
            str(item.get("data_source") or item.get("source") or "").strip()
            for item in (forecast or [])
            if isinstance(item, dict) and (item.get("data_source") or item.get("source"))
        }
    )
    warning_state = warning_status if isinstance(warning_status, dict) else {}
    warning_available = bool(
        warning_state.get("available")
        if "available" in warning_state
        else isinstance(warnings, list)
    )
    return {
        "mode": "scheduled_snapshot_only",
        "refresh_interval_seconds": SNAPSHOT_TTL_SECONDS,
        "canonical_location_only": True,
        "weather": {
            "available": _weather_available(current),
            "provider": source or "unavailable",
            "is_mock": bool((current or {}).get("is_mock")),
        },
        "forecast": {
            "available": bool(forecast),
            "providers": forecast_sources,
            "meta": forecast_meta if isinstance(forecast_meta, dict) else {},
        },
        "warnings": {
            "available": warning_available,
            "count": len(warnings or []),
            "status": str(warning_state.get("status") or ("success" if warning_available else "unavailable")),
        },
        "budget_guard": (
            "enabled"
            if current_app.config.get("QWEATHER_BUDGET_FAIL_CLOSED", True)
            else "disabled"
        ),
    }


def persist_snapshot(
    current,
    forecast=None,
    warnings=None,
    *,
    fetched_at=None,
    forecast_meta=None,
    warning_status=None,
    commit=True,
):
    """在一个事务中保存完整快照，所有消费者共享同一 snapshot_id。"""
    fetched_at = ensure_utc_aware(fetched_at or utcnow())
    current = current if isinstance(current, dict) else {}
    forecast = _enrich_forecast_risk(forecast if isinstance(forecast, list) else [])
    warnings = warnings if isinstance(warnings, list) else []
    risk, actions = _risk_and_actions(current, warnings)
    location = canonical_location()
    _acquire_snapshot_retention_lock()
    record = MiniProgramSnapshot(
        snapshot_id=str(uuid.uuid4()),
        location_name=location["name"],
        location_code=location["code"],
        fetched_at=fetched_at,
        expires_at=fetched_at + timedelta(seconds=SNAPSHOT_TTL_SECONDS),
        available=_weather_available(current),
        current_json=json.dumps(current, ensure_ascii=False),
        forecast_json=json.dumps(forecast, ensure_ascii=False),
        warnings_json=json.dumps(warnings, ensure_ascii=False),
        risk_json=json.dumps(risk, ensure_ascii=False),
        actions_json=json.dumps(actions, ensure_ascii=False),
        source_status_json=json.dumps(
            _source_status(
                current,
                forecast,
                warnings,
                forecast_meta,
                warning_status,
            ),
            ensure_ascii=False,
        ),
        created_at=utcnow(),
    )
    db.session.add(record)
    db.session.flush()
    try:
        retention = int(current_app.config.get("MINIPROGRAM_SNAPSHOT_RETENTION", 96))
    except (TypeError, ValueError):
        retention = 96
    retention = max(2, min(retention, 1000))
    ordered_ids = [
        row[0]
        for row in db.session.query(MiniProgramSnapshot.id)
        .order_by(MiniProgramSnapshot.fetched_at.desc(), MiniProgramSnapshot.id.desc())
        .all()
    ]
    expired_ids = ordered_ids[retention:]
    if expired_ids:
        MiniProgramSnapshot.query.filter(
            MiniProgramSnapshot.id.in_(expired_ids),
        ).delete(synchronize_session=False)
        db.session.flush()
        if record.id in expired_ids:
            # 乱序回填可能立即被保留策略裁掉，调用方应拿到实际可读的最新记录。
            record = latest_snapshot_record()
    if commit:
        db.session.commit()
    return record


def latest_snapshot_record():
    return MiniProgramSnapshot.query.order_by(
        MiniProgramSnapshot.fetched_at.desc(),
        MiniProgramSnapshot.id.desc(),
    ).first()


def snapshot_payload(record=None, *, now=None) -> dict:
    """序列化快照；陈旧判断只依赖持久化时间，不触发任何上游调用。"""
    location = canonical_location()
    if record is None:
        return {
            "snapshot_id": None,
            "location": location,
            "fetched_at": None,
            "expires_at": None,
            "ttl_seconds": SNAPSHOT_TTL_SECONDS,
            "available": False,
            "stale": True,
            "current": {"is_mock": True},
            "forecast": [],
            "warnings": [],
            "risk": {
                "level": "未知",
                "score": None,
                "summary": "后台天气快照尚未生成",
                "reasons": ["后台天气快照尚未生成"],
                "disclaimer": "仅作天气健康行动提醒，不提供医疗诊断。",
            },
            "actions": [],
            "source_status": {
                "mode": "scheduled_snapshot_only",
                "status": "missing",
                "refresh_interval_seconds": SNAPSHOT_TTL_SECONDS,
                "canonical_location_only": True,
            },
            "required_privacy_consent_version": current_privacy_version(),
        }
    current_time = ensure_utc_aware(now or utcnow())
    expires_at = ensure_utc_aware(record.expires_at)
    return {
        "snapshot_id": record.snapshot_id,
        "location": {
            "name": record.location_name or location["name"],
            "code": record.location_code or location["code"],
            "scope": "county",
        },
        "fetched_at": ensure_utc_aware(record.fetched_at).isoformat(),
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": SNAPSHOT_TTL_SECONDS,
        "available": bool(record.available),
        "stale": current_time > expires_at,
        "current": safe_json_loads(record.current_json, {}),
        "forecast": safe_json_loads(record.forecast_json, []),
        "warnings": safe_json_loads(record.warnings_json, []),
        "risk": safe_json_loads(record.risk_json, {}),
        "actions": safe_json_loads(record.actions_json, []),
        "source_status": safe_json_loads(record.source_status_json, {}),
        "required_privacy_consent_version": current_privacy_version(),
    }


def get_bootstrap_payload(*, now=None) -> dict:
    """公共 bootstrap 的唯一读取入口，严禁在这里增加 fetcher。"""
    return snapshot_payload(latest_snapshot_record(), now=now)


def load_cached_weather_inputs():
    """开发/测试未配置 QWeather 时只读已有数据库缓存，绝不走备用外网。"""
    current_record = WeatherCache.query.filter_by(location=CANONICAL_LOCATION_NAME).order_by(
        WeatherCache.fetched_at.desc(), WeatherCache.id.desc()
    ).first()
    current = safe_json_loads(current_record.payload, {}) if current_record else {}
    forecast_record = ForecastCache.query.filter_by(
        location=f"qweather-only:{CANONICAL_LOCATION_NAME}", days=7
    ).order_by(ForecastCache.fetched_at.desc(), ForecastCache.id.desc()).first()
    parsed = safe_json_loads(forecast_record.payload, {}) if forecast_record else {}
    if isinstance(parsed, dict):
        forecast = parsed.get("daily") or parsed.get("forecast") or []
        forecast_meta = parsed.get("meta") or {}
    else:
        forecast = parsed if isinstance(parsed, list) else []
        forecast_meta = {}
    fetched_candidates = [
        ensure_utc_aware(record.fetched_at)
        for record in (current_record, forecast_record)
        if record is not None and record.fetched_at is not None
    ]
    cached_fetched_at = min(fetched_candidates) if fetched_candidates else None
    return current, forecast, forecast_meta, cached_fetched_at


def refresh_snapshot_from_cycle(current, weather_service=None, *, fetched_at=None):
    """完成一次 canonical 同步周期的预报/预警收集并落库。"""
    forecast = []
    forecast_meta = {}
    warnings = []
    warning_status = {"available": False, "status": "not_refreshed"}
    if weather_service is not None and qweather_runtime_configured():
        try:
            forecast, _, forecast_meta = get_qweather_forecast_with_cache(
                CANONICAL_LOCATION_NAME,
                days=7,
                cache_only=False,
                fetcher=weather_service,
            )
        except Exception:
            current_app.logger.exception("小程序预报同步失败，保留实况快照")
            forecast_meta = {"source": "QWeather", "error": "fetch_failed"}
        try:
            from services.warning_service import get_qweather_warnings_result

            warning_result = get_qweather_warnings_result(canonical_location()["code"])
            if isinstance(warning_result, dict):
                warnings = warning_result.get("warnings") or []
                warning_status = {
                    "available": bool(warning_result.get("available")),
                    "status": str(warning_result.get("status") or "unavailable"),
                }
            else:
                # 测试桩或旧扩展返回 list 时继续兼容。
                warnings = warning_result if isinstance(warning_result, list) else []
                warning_status = {"available": True, "status": "success"}
        except Exception:
            current_app.logger.exception("小程序预警同步失败，保留天气快照")
            warnings = []
            warning_status = {"available": False, "status": "fetch_failed"}
    else:
        cached_current, forecast, forecast_meta, cached_fetched_at = load_cached_weather_inputs()
        if not current:
            current = cached_current
        # 离线周期必须继承原始缓存时间，禁止把旧天气重新包装成新鲜快照。
        if cached_fetched_at is not None:
            fetched_at = cached_fetched_at

    # now 请求关闭 7d/空气质量 enrichment；复用本周期唯一 7d 的首日极值。
    if isinstance(current, dict) and forecast and isinstance(forecast[0], dict):
        current = dict(current)
        first_day = forecast[0]
        if current.get("temperature_max") is None:
            current["temperature_max"] = first_day.get("temperature_max")
        if current.get("temperature_min") is None:
            current["temperature_min"] = first_day.get("temperature_min")

    if not _weather_available(current):
        existing = latest_snapshot_record()
        if existing is not None and existing.available:
            return existing
    return persist_snapshot(
        current,
        forecast,
        warnings,
        fetched_at=fetched_at,
        forecast_meta=forecast_meta,
        warning_status=warning_status,
    )


def public_communities_payload() -> dict:
    """仅公开社区级聚合字段，小样本行动率统一抑制。"""
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
    # 先限定每个社区的最新日期，再用最大 id 兼容同日历史重复记录。
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
                        "total_people": None if sample_suppressed else bucket_public_count(count),
                        "confirm_rate": None if sample_suppressed else bucket_public_rate(daily.confirm_rate),
                        "escalation_rate": None if sample_suppressed else bucket_public_rate(daily.escalation_rate),
                        "sample_suppressed": sample_suppressed,
                    }
                    if daily
                    else None
                ),
            }
        )
    return {
        "items": items,
        "summary": {"community_count": len(items), "scope": CANONICAL_LOCATION_NAME},
    }


def public_cooling_resources_payload() -> dict:
    records = CoolingResource.query.filter_by(is_active=True).order_by(
        CoolingResource.community_code.asc(), CoolingResource.name.asc()
    ).all()
    return {
        "items": [
            {
                "id": record.id,
                "community_code": record.community_code,
                "name": record.name,
                "resource_type": record.resource_type,
                "address_hint": record.address_hint,
                "latitude": record.latitude,
                "longitude": record.longitude,
                "open_hours": record.open_hours,
                "has_ac": bool(record.has_ac),
                "is_accessible": bool(record.is_accessible),
                "contact_hint": record.contact_hint,
                "notes": record.notes,
            }
            for record in records
        ]
    }


def public_gis_metadata_payload() -> dict:
    from services.heat_exposure_gis_service import PUBLIC_GEOJSON_FILENAME

    path = Path(current_app.static_folder) / PUBLIC_GEOJSON_FILENAME
    if not current_app.config.get("FEATURE_HEAT_EXPOSURE_GIS") or not path.exists():
        return {"available": False, "scope": CANONICAL_LOCATION_NAME}
    stat = path.stat()
    if _GIS_METADATA_CACHE.get("mtime_ns") != stat.st_mtime_ns:
        collection = json.loads(path.read_text(encoding="utf-8"))
        metadata = collection.get("metadata") if isinstance(collection, dict) else {}
        _GIS_METADATA_CACHE.update(mtime_ns=stat.st_mtime_ns, payload=metadata or {})
    metadata = _GIS_METADATA_CACHE.get("payload") or {}
    url_values = {
        "filename": PUBLIC_GEOJSON_FILENAME,
        # 文件版本进入 URL，避免微信/CDN 在数据更新后继续返回旧 GeoJSON。
        "v": stat.st_mtime_ns,
    }
    return {
        "available": True,
        "scope": CANONICAL_LOCATION_NAME,
        # 返回同源相对路径，避免反向代理 Host/协议误配置污染小程序请求目标。
        "geojson_url": url_for("static", _external=False, **url_values),
        "title": metadata.get("title"),
        "schema_version": metadata.get("schema_version"),
        "size_bytes": stat.st_size,
        "generated_at": metadata.get("generated_at_utc"),
        "layers": metadata.get("layers") or {},
        "metadata": metadata,
    }
