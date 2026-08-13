# -*- coding: utf-8 -*-
"""小程序快照、公开聚合资源与都昌县语义服务。"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta

from flask import current_app, url_for
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
from core.time_utils import (
    ensure_utc_aware,
    today_local,
    utc_to_local_date,
    utc_to_local_datetime,
    utcnow,
)
from core.weather import get_consecutive_hot_days, get_qweather_forecast_with_cache
from services.qweather_auth import is_qweather_configured
from services.miniprogram_auth import current_privacy_version
from services.community_daily_service import (
    PUBLIC_AGGREGATE_MIN_SAMPLE,
    bucket_public_count,
    bucket_public_rate,
)
from services.public_risk_service import (
    HEAT_RISK_LABELS,
    PUBLIC_RISK_SCHEMA_VERSION,
    build_public_family_reminder,
    build_public_risk_context,
    calculate_public_risk,
    public_risk_weather_is_ready,
)
from utils.parsers import safe_json_loads


SNAPSHOT_TTL_SECONDS = 1800
CANONICAL_LOCATION_NAME = DEFAULT_CITY_LABEL
CANONICAL_LOCATION_CODE = "116.20,29.27"
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
    """兼容旧调用点，实际计算统一委托给公共风险服务。"""
    context = build_public_risk_context(current, warnings)
    return context["risk"], context["actions"]


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
        public_risk = calculate_public_risk(proxy)
        risk = public_risk["risk"]
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
    source_timing=None,
    current_available=None,
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
    timing = source_timing if isinstance(source_timing, dict) else {}
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
            "available": (
                _weather_available(current)
                if current_available is None
                else current_available is True
            ),
            "provider": source or "unavailable",
            "is_mock": bool((current or {}).get("is_mock")),
            **(timing.get("current") or {}),
        },
        "forecast": {
            "available": bool(forecast),
            "providers": forecast_sources,
            "meta": forecast_meta if isinstance(forecast_meta, dict) else {},
            **(timing.get("forecast") or {}),
        },
        "warnings": {
            "available": warning_available,
            "count": len(warnings or []),
            "status": str(warning_state.get("status") or ("success" if warning_available else "unavailable")),
            **(timing.get("warnings") or {}),
        },
        # 正式运行语义固定为 fail-closed；旧环境变量不再改变该事实。
        "budget_guard": "enabled",
    }


def _source_datetime(value):
    """把来源时间规范为 UTC aware datetime；无效值不会被伪造成当前时间。"""
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        try:
            return ensure_utc_aware(value)
        except (TypeError, ValueError):
            return None
    try:
        return ensure_utc_aware(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def snapshot_component_status(payload, component, *, now=None) -> dict:
    """按精确组件合同判断可用性，旧快照才回退到总状态。"""
    data = payload if isinstance(payload, dict) else {}
    component_name = str(component or "").strip().lower()
    aliases = {
        "current": ("current", "current_weather", "weather"),
        "risk": ("risk",),
        "forecast": ("forecast",),
        "warnings": ("warnings",),
    }
    source_status = data.get("source_status")
    source_status = source_status if isinstance(source_status, dict) else {}
    state = None
    state_present = False
    for key in aliases.get(component_name, (component_name,)):
        if key in source_status:
            state_present = True
            candidate = source_status.get(key)
            state = candidate if isinstance(candidate, dict) else {}
            break
    state_is_mapping = isinstance(state, dict)
    state = state if state_is_mapping else {}

    root_available = data.get("available") is True
    root_stale = data.get("stale") is not False
    state_has_availability = isinstance(state.get("available"), bool)
    state_stale_value = state.get("stale")
    state_stale_valid = (
        "stale" not in state or isinstance(state_stale_value, bool)
    )
    raw_expires_at = state.get("expires_at")
    expires_at = _source_datetime(raw_expires_at)
    state_expiry_valid = raw_expires_at in (None, "") or expires_at is not None
    state_contract_valid = (
        (not state_present)
        or (
            state_is_mapping
            and state_has_availability
            and state_stale_valid
            and state_expiry_valid
        )
    )
    component_available = (
        state["available"]
        if state_contract_valid and state_has_availability
        else False if state_present else root_available
    )

    stale_signals = []
    top_stale = data.get(f"{component_name}_stale")
    if isinstance(top_stale, bool):
        stale_signals.append(top_stale)
    state_stale = state_stale_value
    if isinstance(state_stale, bool):
        stale_signals.append(state_stale)
    if expires_at is not None:
        stale_signals.append(ensure_utc_aware(now or utcnow()) >= expires_at)

    explicit_contract = (
        state_present
        and state_contract_valid
        and state_has_availability
        and bool(stale_signals)
    )
    component_stale = (
        True
        if state_present and not state_contract_valid
        else any(stale_signals) if stale_signals else root_stale
    )
    if component_name in {"current", "risk"}:
        # 根 available 表示当前天气是否可用，风险必须继承这一硬门禁。
        available = root_available and component_available
    elif explicit_contract:
        # 预报与官方预警可在当前天气失败时独立存活。
        available = component_available
    else:
        available = root_available and component_available

    return {
        "available": available,
        "stale": component_stale,
        "usable": available and not component_stale,
        "explicit": explicit_contract,
        "fetched_at": state.get("fetched_at"),
        "expires_at": state.get("expires_at"),
    }


def snapshot_display_time(value):
    """把快照 UTC 时间转换为都昌页面使用的本地时间文字。"""
    parsed = _source_datetime(value)
    if parsed is None:
        return None
    return utc_to_local_datetime(parsed).strftime("%Y-%m-%d %H:%M")


def _component_state(
    source_status,
    name,
    *,
    now,
    fallback_expires_at,
    fallback_available,
):
    """在读取时计算单个来源的新鲜度，旧快照继续回退到总过期时间。"""
    source = source_status if isinstance(source_status, dict) else {}
    aliases = {"current": ("current", "weather")}
    raw = {}
    for key in aliases.get(name, (name,)):
        candidate = source.get(key)
        if isinstance(candidate, dict):
            raw = dict(candidate)
            break
    expires_at = _source_datetime(raw.get("expires_at")) or fallback_expires_at
    fetched_at = _source_datetime(raw.get("fetched_at"))
    stale = expires_at is None or now >= expires_at
    available = bool(raw.get("available", fallback_available))
    state = {
        **raw,
        "available": available,
        "stale": stale,
    }
    if fetched_at is not None:
        state["fetched_at"] = fetched_at.isoformat()
    if expires_at is not None:
        state["expires_at"] = expires_at.isoformat()
    return state


def _payload_source_status(record, current, forecast, warnings, *, now, expires_at):
    """补齐各来源状态；总 stale 保留兼容，页面按组件状态决定是否展示。"""
    stored = safe_json_loads(record.source_status_json, {})
    stored = dict(stored) if isinstance(stored, dict) else {}
    current_state = _component_state(
        stored,
        "current",
        now=now,
        fallback_expires_at=expires_at,
        fallback_available=bool(record.available) and _weather_available(current),
    )
    forecast_state = _component_state(
        stored,
        "forecast",
        now=now,
        fallback_expires_at=expires_at,
        fallback_available=bool(forecast),
    )
    warnings_state = _component_state(
        stored,
        "warnings",
        now=now,
        fallback_expires_at=expires_at,
        # 未知预警来源不能因为 payload 恰好是列表就被视为核验成功。
        fallback_available=False,
    )
    risk_stale = current_state["stale"] or not current_state["available"]
    risk_state = {
        "available": not risk_stale and public_risk_weather_is_ready(current),
        "stale": risk_stale,
        "depends_on": ["current"],
        "fetched_at": current_state.get("fetched_at"),
        "expires_at": current_state.get("expires_at"),
    }
    # weather 是旧客户端使用的键，current 是新合同的直观别名。
    stored.update({
        "weather": dict(current_state),
        "current": dict(current_state),
        "forecast": forecast_state,
        "warnings": warnings_state,
        "risk": risk_state,
    })
    return stored


def _normalize_source_timing(
    *,
    current,
    forecast,
    warning_status,
    fetched_at,
    forecast_meta,
    source_timing,
):
    """生成各必要来源的真实时间，并返回快照最保守的有效窗口。"""
    supplied = source_timing if isinstance(source_timing, dict) else {}
    warning_state = warning_status if isinstance(warning_status, dict) else {}
    forecast_state = forecast_meta if isinstance(forecast_meta, dict) else {}
    defaults = {
        "current": supplied.get("current") or {},
        "forecast": supplied.get("forecast") or forecast_state,
        "warnings": supplied.get("warnings") or warning_state,
    }
    required = []
    if _weather_available(current):
        required.append("current")
    if forecast:
        required.append("forecast")
    if warning_state.get("available"):
        # 成功确认“无预警”也是本快照的必要来源。
        required.append("warnings")

    normalized = {}
    fetched_values = []
    expiry_values = []
    for name in required:
        raw = defaults.get(name) or {}
        component_fetched = _source_datetime(raw.get("fetched_at")) or fetched_at
        component_expires = _source_datetime(raw.get("expires_at")) or (
            component_fetched + timedelta(seconds=SNAPSHOT_TTL_SECONDS)
        )
        normalized[name] = {
            "fetched_at": component_fetched.isoformat(),
            "expires_at": component_expires.isoformat(),
        }
        fetched_values.append(component_fetched)
        expiry_values.append(component_expires)

    snapshot_fetched_at = min(fetched_values) if fetched_values else fetched_at
    snapshot_expires_at = min(expiry_values) if expiry_values else (
        snapshot_fetched_at + timedelta(seconds=SNAPSHOT_TTL_SECONDS)
    )
    return normalized, snapshot_fetched_at, snapshot_expires_at


def persist_snapshot(
    current,
    forecast=None,
    warnings=None,
    *,
    fetched_at=None,
    forecast_meta=None,
    warning_status=None,
    source_timing=None,
    current_available=None,
    commit=True,
):
    """在一个事务中保存完整快照，所有消费者共享同一 snapshot_id。"""
    fetched_at = ensure_utc_aware(fetched_at or utcnow())
    current = current if isinstance(current, dict) else {}
    forecast = _enrich_forecast_risk(forecast if isinstance(forecast, list) else [])
    warnings = warnings if isinstance(warnings, list) else []
    source_timing, fetched_at, expires_at = _normalize_source_timing(
        current=current,
        forecast=forecast,
        warning_status=warning_status,
        fetched_at=fetched_at,
        forecast_meta=forecast_meta,
        source_timing=source_timing,
    )
    public_context = build_public_risk_context(
        current,
        warnings,
        date_value=fetched_at,
    )
    risk = public_context["risk"]
    actions = public_context["actions"]
    location = canonical_location()
    _acquire_snapshot_retention_lock()
    record = MiniProgramSnapshot(
        snapshot_id=str(uuid.uuid4()),
        location_name=location["name"],
        location_code=location["code"],
        fetched_at=fetched_at,
        expires_at=expires_at,
        available=(
            _weather_available(current)
            if current_available is None
            else current_available is True
        ),
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
                source_timing,
                current_available,
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


def _persisted_public_context(record, current, warnings, *, available, date_value):
    """新鲜快照优先读取落库风险，保证同一 snapshot_id 的语义稳定。"""
    if not available or not public_risk_weather_is_ready(current):
        return build_public_risk_context(
            current,
            warnings,
            date_value=date_value,
            available=False,
        )

    risk = safe_json_loads(record.risk_json, {})
    actions = safe_json_loads(record.actions_json, [])
    calculation = risk.get("calculation") if isinstance(risk, dict) else None
    heat_result = (
        calculation.get("heat_result")
        if isinstance(calculation, dict)
        else None
    )
    risk_reasons = (
        calculation.get("risk_reasons")
        if isinstance(calculation, dict)
        else None
    )
    stored_score = _finite_number(risk.get("score")) if isinstance(risk, dict) else None
    calculated_score = (
        _finite_number(heat_result.get("risk_score"))
        if isinstance(heat_result, dict)
        else None
    )
    calculated_label = (
        HEAT_RISK_LABELS.get(heat_result.get("risk_level"))
        if isinstance(heat_result, dict)
        else None
    )
    if (
        not isinstance(risk, dict)
        or risk.get("available") is False
        or risk.get("schema_version") != PUBLIC_RISK_SCHEMA_VERSION
        or stored_score is None
        or calculated_score is None
        or abs(stored_score - calculated_score) > 1e-9
        or not str(risk.get("level") or "").strip()
        or risk.get("level") != calculated_label
        or not isinstance(actions, list)
        or not isinstance(risk_reasons, list)
    ):
        return build_public_risk_context(
            current,
            warnings,
            date_value=date_value,
            available=False,
        )

    risk = dict(risk)
    risk.setdefault("available", True)
    risk.setdefault("schema_version", PUBLIC_RISK_SCHEMA_VERSION)
    return {
        "available": True,
        "risk": risk,
        "actions": actions,
        "heat_result": heat_result,
        "risk_reasons": risk_reasons,
        "family_reminder": build_public_family_reminder(
            current,
            warnings,
            risk=risk,
            available=True,
            date_value=date_value,
        ),
    }


def snapshot_payload(record=None, *, now=None) -> dict:
    """序列化快照；陈旧判断只依赖持久化时间，不触发任何上游调用。"""
    location = canonical_location()
    current_time = ensure_utc_aware(now or utcnow())
    if record is None:
        public_context = build_public_risk_context(
            {},
            [],
            date_value=current_time,
            available=False,
        )
        return {
            "snapshot_id": None,
            "location": location,
            "fetched_at": None,
            "expires_at": None,
            "ttl_seconds": SNAPSHOT_TTL_SECONDS,
            "available": False,
            "stale": True,
            "current_stale": True,
            "forecast_stale": True,
            "warnings_stale": True,
            "risk_stale": True,
            "current": {"is_mock": True},
            "forecast": [],
            "warnings": [],
            "risk": public_context["risk"],
            "actions": public_context["actions"],
            "family_reminder": public_context["family_reminder"],
            "source_status": {
                "mode": "scheduled_snapshot_only",
                "status": "missing",
                "refresh_interval_seconds": SNAPSHOT_TTL_SECONDS,
                "canonical_location_only": True,
                "current": {"available": False, "stale": True},
                "weather": {"available": False, "stale": True},
                "forecast": {"available": False, "stale": True},
                "warnings": {"available": False, "stale": True},
                "risk": {
                    "available": False,
                    "stale": True,
                    "depends_on": ["current"],
                },
            },
            "required_privacy_consent_version": current_privacy_version(),
        }
    expires_at = ensure_utc_aware(record.expires_at)
    current = safe_json_loads(record.current_json, {})
    forecast = safe_json_loads(record.forecast_json, [])
    warnings = safe_json_loads(record.warnings_json, [])
    stale = current_time >= expires_at
    source_status = _payload_source_status(
        record,
        current,
        forecast,
        warnings,
        now=current_time,
        expires_at=expires_at,
    )
    current_stale = bool(source_status["current"]["stale"])
    forecast_stale = bool(source_status["forecast"]["stale"])
    warnings_stale = bool(source_status["warnings"]["stale"])
    risk_stale = bool(source_status["risk"]["stale"])
    warnings_usable = (
        not warnings_stale
        and source_status["warnings"]["available"] is True
    )
    public_warnings = warnings if warnings_usable else []
    public_context = _persisted_public_context(
        record,
        current,
        # 过期预警不能继续参与家庭提醒话术，当前天气风险仍可独立使用。
        public_warnings,
        available=bool(record.available) and not risk_stale,
        date_value=current_time,
    )
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
        "stale": stale,
        "current_stale": current_stale,
        "forecast_stale": forecast_stale,
        "warnings_stale": warnings_stale,
        "risk_stale": risk_stale,
        "current": current,
        "forecast": forecast,
        "warnings": public_warnings,
        "risk": public_context["risk"],
        "actions": public_context["actions"],
        "family_reminder": public_context["family_reminder"],
        "source_status": source_status,
        "required_privacy_consent_version": current_privacy_version(),
    }


def get_bootstrap_payload(*, now=None) -> dict:
    """通过独立只读会话取得 bootstrap，绝不影响调用方事务。"""
    try:
        with Session(bind=db.engine) as read_session:
            record = read_session.query(MiniProgramSnapshot).order_by(
                MiniProgramSnapshot.fetched_at.desc(),
                MiniProgramSnapshot.id.desc(),
            ).first()
            return snapshot_payload(record, now=now)
    except SQLAlchemyError:
        # 独立读取失败只降级本次结果，不能回滚调用方正在进行的写事务。
        current_app.logger.exception("读取公共天气快照失败，返回安全降级结果")
        return snapshot_payload(None, now=now)


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
    source_timing = {}
    if current_record is not None and current_record.fetched_at is not None:
        current_fetched_at = ensure_utc_aware(current_record.fetched_at)
        current_ttl = max(
            int(current_app.config.get("WEATHER_CACHE_TTL_MINUTES", 30) or 30),
            1,
        )
        source_timing["current"] = {
            "fetched_at": current_fetched_at,
            "expires_at": current_fetched_at + timedelta(minutes=current_ttl),
        }
    if forecast_record is not None and forecast_record.fetched_at is not None:
        forecast_fetched_at = ensure_utc_aware(forecast_record.fetched_at)
        forecast_ttl = max(
            int(current_app.config.get("FORECAST_CACHE_TTL_MINUTES", 30) or 30),
            1,
        )
        forecast_meta = dict(forecast_meta or {})
        forecast_meta.setdefault("fetched_at", forecast_fetched_at.isoformat())
        forecast_meta.setdefault(
            "expires_at",
            (forecast_fetched_at + timedelta(minutes=forecast_ttl)).isoformat(),
        )
        source_timing["forecast"] = forecast_meta
    return current, forecast, forecast_meta, cached_fetched_at, source_timing


def refresh_snapshot_from_cycle(
    current,
    weather_service=None,
    *,
    fetched_at=None,
    current_fetched_at=None,
    force_refresh_sources=False,
    commit=True,
):
    """完成一次 canonical 同步周期的预报/预警收集并落库。"""
    current_available = None
    forecast = []
    forecast_meta = {}
    warnings = []
    warning_status = {"available": False, "status": "not_refreshed"}
    cycle_source_timing = {}
    if weather_service is not None and qweather_runtime_configured():
        try:
            forecast, _, forecast_meta = get_qweather_forecast_with_cache(
                CANONICAL_LOCATION_NAME,
                days=7,
                cache_only=False,
                fetcher=weather_service,
                force_refresh=force_refresh_sources,
            )
        except Exception:
            current_app.logger.exception("小程序预报同步失败，保留实况快照")
            forecast_meta = {"source": "QWeather", "error": "fetch_failed"}
        try:
            from services.warning_service import get_qweather_warnings_result

            warning_result = get_qweather_warnings_result(
                canonical_location()["code"],
                force_refresh=force_refresh_sources,
            )
            if isinstance(warning_result, dict):
                warnings = warning_result.get("warnings") or []
                warning_status = {
                    "available": bool(warning_result.get("available")),
                    "status": str(warning_result.get("status") or "unavailable"),
                    "fetched_at": warning_result.get("fetched_at"),
                    "expires_at": warning_result.get("expires_at"),
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
        (
            cached_current,
            forecast,
            forecast_meta,
            cached_fetched_at,
            cycle_source_timing,
        ) = load_cached_weather_inputs()
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

    if isinstance(current, dict) and current.get("consecutive_hot_days") is None:
        # 连续高温只读取本地历史表，绝不增加天气供应商请求。
        snapshot_date = (
            utc_to_local_date(fetched_at)
            if fetched_at is not None
            else today_local()
        )
        current = dict(current)
        current["consecutive_hot_days"] = get_consecutive_hot_days(
            CANONICAL_LOCATION_NAME,
            target_date=snapshot_date,
            today_max=current.get("temperature_max"),
        )

    source_timing = dict(cycle_source_timing)
    if not _weather_available(current):
        existing = latest_snapshot_record()
        independent_update = bool(forecast) or warning_status.get("available") is True
        if existing is not None:
            stored_current = safe_json_loads(existing.current_json, {})
            if _weather_available(stored_current):
                # 保留旧实况仅供溯源；current_available=False 会阻止任何消费者使用它。
                current = stored_current
            stored_status = safe_json_loads(existing.source_status_json, {})
            stored_status = stored_status if isinstance(stored_status, dict) else {}
            stored_current_status = (
                stored_status.get("current")
                or stored_status.get("current_weather")
                or stored_status.get("weather")
                or {}
            )
            if isinstance(stored_current_status, dict):
                source_timing.setdefault(
                    "current",
                    {
                        key: stored_current_status.get(key)
                        for key in ("fetched_at", "expires_at")
                        if stored_current_status.get(key) is not None
                    },
                )
            if not independent_update:
                return existing
        current_available = False

    if "current" not in source_timing and (current_fetched_at or fetched_at):
        source_time = ensure_utc_aware(current_fetched_at or fetched_at)
        source_timing["current"] = {
            "fetched_at": source_time,
            "expires_at": source_time + timedelta(seconds=SNAPSHOT_TTL_SECONDS),
        }
    if forecast and "forecast" not in source_timing:
        source_timing["forecast"] = forecast_meta
    if warning_status.get("available") is True and "warnings" not in source_timing:
        source_timing["warnings"] = warning_status

    return persist_snapshot(
        current,
        forecast,
        warnings,
        fetched_at=fetched_at,
        forecast_meta=forecast_meta,
        warning_status=warning_status,
        source_timing=source_timing,
        current_available=current_available,
        commit=commit,
    )


def public_communities_payload() -> dict:
    """仅公开社区级聚合字段，小样本行动率统一抑制。"""
    communities = Community.query.order_by(Community.name.asc()).all()
    community_coordinates = current_app.config.get("COMMUNITY_COORDS_GCJ") or {}
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
        coordinates = community_coordinates.get(community.name)
        latitude = None
        longitude = None
        coordinate_system = None
        if isinstance(coordinates, (list, tuple)) and len(coordinates) == 2:
            configured_longitude = _finite_number(coordinates[0])
            configured_latitude = _finite_number(coordinates[1])
            if (
                configured_longitude is not None
                and configured_latitude is not None
                and -180 <= configured_longitude <= 180
                and -90 <= configured_latitude <= 90
            ):
                longitude = configured_longitude
                latitude = configured_latitude
                coordinate_system = "GCJ-02"
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
                # 小程序原生地图只接收项目内已核对的高德坐标；数据库位置不会兜底。
                "latitude": latitude,
                "longitude": longitude,
                "coordinate_system": coordinate_system,
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


def _public_cooling_coordinates(record):
    """只公开具备 GCJ-02 人工核验回执的有效坐标。"""
    if (
        record.coordinate_verified_at is None
        or record.coordinate_system != 'GCJ-02'
        or not str(record.coordinate_source or '').strip()
        or record.latitude is None
        or record.longitude is None
    ):
        return None, None, None
    try:
        latitude = float(record.latitude)
        longitude = float(record.longitude)
        verified_at = ensure_utc_aware(record.coordinate_verified_at)
    except (TypeError, ValueError):
        return None, None, None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None, None, None

    verification_ttl_days = current_app.config.get(
        'COOLING_COORDINATE_VERIFICATION_TTL_DAYS',
        365,
    )
    try:
        verification_ttl_days = max(
            30,
            min(int(verification_ttl_days), 730),
        )
    except (TypeError, ValueError):
        verification_ttl_days = 365
    now = utcnow()
    if (
        verified_at > now + timedelta(minutes=5)
        or now - verified_at > timedelta(days=verification_ttl_days)
    ):
        return None, None, None

    # 后台录入已经校验一次，公开接口再次限制都昌服务区，防止异常导入绕过。
    center_latitude = math.radians(29.27)
    point_latitude = math.radians(latitude)
    latitude_delta = point_latitude - center_latitude
    longitude_delta = math.radians(longitude - 116.20)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(center_latitude)
        * math.cos(point_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    distance_km = 6371.0088 * 2 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(max(0.0, 1 - haversine)),
    )
    if distance_km > 80.0:
        return None, None, None
    return latitude, longitude, 'GCJ-02'


def public_cooling_resources_payload() -> dict:
    records = CoolingResource.query.filter_by(is_active=True).order_by(
        CoolingResource.community_code.asc(), CoolingResource.name.asc()
    ).all()
    items = []
    for record in records:
        latitude, longitude, coordinate_system = _public_cooling_coordinates(record)
        items.append(
            {
                "id": record.id,
                "community_code": record.community_code,
                "name": record.name,
                "resource_type": record.resource_type,
                "address_hint": record.address_hint,
                "latitude": latitude,
                "longitude": longitude,
                "coordinate_system": coordinate_system,
                "open_hours": record.open_hours,
                "has_ac": bool(record.has_ac),
                "is_accessible": bool(record.is_accessible),
                "contact_hint": record.contact_hint,
                "notes": record.notes,
            }
        )
    return {
        "items": items,
        "coordinate_system": "GCJ-02",
    }


def public_gis_metadata_payload() -> dict:
    from services.heat_exposure_gis_service import (
        PUBLIC_GEOJSON_PATH,
        PUBLIC_GEOJSON_SHA256,
        load_validated_public_geojson,
    )

    path = PUBLIC_GEOJSON_PATH
    if not current_app.config.get("FEATURE_HEAT_EXPOSURE_GIS") or not path.exists():
        return {"available": False, "scope": CANONICAL_LOCATION_NAME}
    try:
        collection = load_validated_public_geojson(path)
        stat = path.stat()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"available": False, "scope": CANONICAL_LOCATION_NAME}
    metadata = collection.get("metadata") or {}
    return {
        "available": True,
        "scope": CANONICAL_LOCATION_NAME,
        # 返回同源相对路径，避免反向代理 Host/协议误配置污染小程序请求目标。
        "geojson_url": url_for(
            "public.public_heat_geojson",
            _external=False,
            v=PUBLIC_GEOJSON_SHA256[:16],
        ),
        "title": metadata.get("title"),
        "schema_version": metadata.get("schema_version"),
        "size_bytes": stat.st_size,
        "generated_at": metadata.get("generated_at_utc"),
        "layers": metadata.get("layers") or {},
        "metadata": metadata,
    }
