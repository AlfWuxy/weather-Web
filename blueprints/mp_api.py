# -*- coding: utf-8 -*-
"""MiniProgram API (no CSRF; Bearer API token auth).

Endpoints:
- GET  /mp/api/v1/me
- GET  /mp/api/v1/elders
- POST /mp/api/v1/elders
- PATCH /mp/api/v1/elders/<pair_id>
- GET  /mp/api/v1/alerts?pair_id=...
- GET  /mp/api/v1/pending
- POST /mp/api/v1/pairs/<pair_id>/events
- POST /mp/api/v1/events
"""

from __future__ import annotations

import json
import math
import re
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from core.audit import _get_client_ip
from core.db_models import FamilyMember, FamilyMemberProfile, Pair, User
from core.extensions import db, limiter
from core.security import hash_identifier
from core.time_utils import utcnow
from core.usage import log_usage_event, verify_api_token
from core.weather import get_weather_with_cache, is_qweather_production_ready
from services.action_events import InvalidTransition, record_event, today_state
from services.location_resolver import resolve_location
from services.warning_service import get_qweather_warnings
from services.user._common import _create_pair_record
from utils.parsers import safe_json_loads
from utils.validators import sanitize_input

bp = Blueprint("mp_api", __name__, url_prefix="/mp/api/v1")
MP_EVENT_META_MAX_CHARS = 2048
MP_CHRONIC_DISEASES_MAX_ITEMS = 20
MP_CLIENT_EVENT_TYPES = frozenset(
    {
        "template_view",
        "template_copy",
        "feedback_submitted",
    }
)
MP_CAREGIVER_STAGES = frozenset(
    {
        "delivered",
        "help_acknowledged",
        "caregiver_verified",
        "closed",
    }
)
MP_PENDING_TODAY_KEYS = (
    "delivered",
    "seen",
    "understood",
    "self_reported",
    "help_requested",
    "help_acknowledged",
    "caregiver_verified",
    "closed",
)
TEMPLATE_COPY_META_FIELDS = (
    "script_version",
    "messenger_role",
    "channel",
    "scenario",
)
_GENDER_ALIASES = {
    "男": "男性",
    "男性": "男性",
    "女": "女性",
    "女性": "女性",
    "其他": "其他",
    "未知": "未知",
}
_MISSING = object()


def _bearer_token() -> str:
    auth = request.headers.get("Authorization") or ""
    auth = auth.strip()
    if not auth:
        return ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _mp_rate_limit_key() -> str:
    """外层限流使用稳定客户端 IP，避免轮换无效 Bearer 换桶。"""
    client_ip = _get_client_ip() or request.remote_addr or "unknown"
    return f"mp-ip:{hash_identifier(client_ip)}"


def require_api_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer_token()
        record = verify_api_token(token)
        if not record:
            return jsonify({"success": False, "error": "unauthorized"}), 401
        try:
            record.last_used_at = utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()
        g.api_token = record
        g.api_user_id = record.user_id
        return fn(*args, **kwargs)

    return wrapper


def _pair_for_user(pair_id: int):
    q = Pair.query.filter_by(id=pair_id, status="active")
    # admin token is not supported in pilot; restrict to owner
    q = q.filter_by(caregiver_id=g.api_user_id)
    return q.first()


def _parse_strict_bool(value) -> bool:
    """严格解析布尔值，同时兼容小程序可能提交的字符串形式。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("push_enabled must be a boolean")


def _parse_optional_age(value):
    """严格解析可选年龄，拒绝布尔值、小数和越界值。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_age")
    if isinstance(value, int):
        age = value
    elif isinstance(value, str) and re.fullmatch(r"\d{1,3}", value.strip()):
        age = int(value.strip())
    else:
        raise ValueError("invalid_age")
    if age < 1 or age > 150:
        raise ValueError("invalid_age")
    return age


def _parse_optional_gender(value):
    """严格解析可选性别，并归一化为数据库现有枚举。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_gender")
    normalized = _GENDER_ALIASES.get(value.strip())
    if not normalized:
        raise ValueError("invalid_gender")
    return normalized


def _parse_chronic_diseases(value):
    """严格解析慢病类别列表，避免错误类型被静默当成清空。"""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MP_CHRONIC_DISEASES_MAX_ITEMS:
        raise ValueError("invalid_chronic_diseases")

    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("invalid_chronic_diseases")
        raw = item.strip()
        if not raw or len(raw) > 50:
            raise ValueError("invalid_chronic_diseases")
        cleaned = sanitize_input(raw, max_length=50)
        if not cleaned:
            raise ValueError("invalid_chronic_diseases")
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _parse_required_location(value):
    """严格解析必填地点，拒绝空字符串和非文本值。"""
    if not isinstance(value, str):
        raise ValueError("invalid_location_query")
    location = sanitize_input(value, max_length=200)
    location = location.strip() if isinstance(location, str) else ""
    if not location:
        raise ValueError("invalid_location_query")
    return location


def _parse_positive_id(value, error_code):
    """严格解析客户端提交的正整数关联 ID。"""
    if isinstance(value, bool):
        raise ValueError(error_code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(error_code)
    if parsed <= 0:
        raise ValueError(error_code)
    return parsed


def _elder_label(member):
    """家属自设称呼（relation），绝不返回档案姓名。"""
    if member is None:
        return ""
    relation = getattr(member, "relation", None)
    if not isinstance(relation, str):
        return ""
    return relation.strip()


def _pending_today(state):
    source = state or {}
    return {key: bool(source.get(key)) for key in MP_PENDING_TODAY_KEYS}


def _template_copy_meta_invalid(meta):
    if not isinstance(meta, dict):
        return True
    for key in TEMPLATE_COPY_META_FIELDS:
        value = meta.get(key)
        if value is None:
            return True
        if isinstance(value, bool):
            return True
        if not str(value).strip():
            return True
    return False


@bp.route("/me", endpoint="me")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def me():
    user = db.session.get(User, g.api_user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    return jsonify(
        {
            "success": True,
            "data": {
                "id": user.id,
                "username": user.username,
                "wxpusher_uid": user.wxpusher_uid,
                "push_enabled": bool(user.push_enabled),
            },
        }
    )


@bp.route("/me", methods=["PATCH"], endpoint="me_patch")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def me_patch():
    """Update pilot push settings (WxPusher UID + enabled flag)."""
    user = db.session.get(User, g.api_user_id)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "invalid_payload"}), 400

    updated_fields = []
    wx_uid = user.wxpusher_uid
    push_enabled = bool(user.push_enabled)

    if "wxpusher_uid" in payload:
        wx_uid = sanitize_input(payload.get("wxpusher_uid"), max_length=80)
        wx_uid = (wx_uid.strip() if isinstance(wx_uid, str) else None) or None
        updated_fields.append("wxpusher_uid")

    if "push_enabled" in payload:
        try:
            push_enabled = _parse_strict_bool(payload.get("push_enabled"))
        except ValueError:
            return jsonify({"success": False, "error": "invalid_push_enabled"}), 400
        updated_fields.append("push_enabled")

    # UID 被移除时必须关闭推送，避免保留无法投递的开启状态。
    if not wx_uid:
        push_enabled = False
        if "wxpusher_uid" in updated_fields and "push_enabled" not in updated_fields:
            updated_fields.append("push_enabled")

    if updated_fields:
        user.wxpusher_uid = wx_uid
        user.push_enabled = push_enabled
        db.session.commit()
        log_usage_event(
            "settings_updated",
            user_id=user.id,
            source="miniprogram",
            meta={"fields": updated_fields},
        )
    return jsonify({"success": True, "data": {"wxpusher_uid": user.wxpusher_uid, "push_enabled": bool(user.push_enabled)}})


@bp.route("/elders", endpoint="elders_list")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def elders_list():
    pairs = Pair.query.filter_by(caregiver_id=g.api_user_id, status="active").order_by(Pair.created_at.desc()).all()
    member_ids = [p.member_id for p in pairs if p.member_id]
    members = (
        FamilyMember.query.filter(FamilyMember.id.in_(member_ids)).all() if member_ids else []
    )
    member_map = {m.id: m for m in members}

    result = []
    for p in pairs:
        label = (p.location_query or p.community_code or "").strip()
        resolved = resolve_location(label)
        code = resolved.get("location_code") or ""
        weather_data, _ = get_weather_with_cache(code or label)
        # Lightweight summary; detailed warnings via /alerts
        trigger = None
        tmax_value = None
        tmin_value = None
        try:
            tmax = weather_data.get("temperature_max")
            tmin = weather_data.get("temperature_min")
            tmax_value = float(tmax) if tmax is not None else None
            tmin_value = float(tmin) if tmin is not None else None
        except (AttributeError, TypeError, ValueError):
            tmax_value = None
            tmin_value = None
        weather_available = (
            is_qweather_production_ready(weather_data)
            and tmax_value is not None
            and tmin_value is not None
            and math.isfinite(tmax_value)
            and math.isfinite(tmin_value)
        )
        if weather_available:
            if tmax_value >= 35:
                trigger = "heat"
            elif tmin_value <= 5:
                trigger = "cold"

        member = member_map.get(p.member_id) if p.member_id else None
        result.append(
            {
                "pair_id": p.id,
                "location_query": p.location_query,
                "community_code": p.community_code,
                "member": (
                    {
                        "id": member.id,
                        "name": member.name,
                        "relation": member.relation,
                        "age": member.age,
                        "gender": member.gender,
                        "chronic_diseases": safe_json_loads(member.chronic_diseases, []),
                    }
                    if member
                    else None
                ),
                "today": {
                    "trigger": trigger,
                    "temperature_max": tmax_value if weather_available else None,
                    "temperature_min": tmin_value if weather_available else None,
                    "weather_available": weather_available,
                    "is_mock": bool(weather_data.get("is_mock")),
                    "location": weather_data.get("location"),
                    "observed_at": weather_data.get("observed_at") if weather_available else None,
                },
            }
        )

    return jsonify({"success": True, "data": result})


@bp.route("/elders", methods=["POST"], endpoint="elders_create")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def elders_create():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "invalid_payload"}), 400

    name = sanitize_input(payload.get("name"), max_length=50) or ""
    relation = sanitize_input(payload.get("relation"), max_length=20) or ""
    if not name:
        return jsonify({"success": False, "error": "missing_fields"}), 400

    try:
        location_query = _parse_required_location(payload.get("location_query"))
        age = _parse_optional_age(payload.get("age"))
        gender = _parse_optional_gender(payload.get("gender"))
        chronic = _parse_chronic_diseases(payload.get("chronic_diseases"))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    try:
        member = FamilyMember(
            user_id=g.api_user_id,
            name=name,
            relation=relation,
            age=age,
            gender=gender,
            chronic_diseases=(json.dumps(chronic, ensure_ascii=False) if chronic else None),
            created_at=utcnow(),
        )
        db.session.add(member)
        db.session.flush()  # 获取 member.id，但不提交

        profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
        if not profile:
            profile = FamilyMemberProfile(member_id=member.id, alert_enabled=True)
            db.session.add(profile)

        pair = _create_pair_record(
            caregiver_id=g.api_user_id,
            location_query=location_query,
            member_id=member.id,
            flush=True
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"success": False, "error": "create_failed"}), 500

    log_usage_event(
        "elder_profile_created",
        user_id=g.api_user_id,
        member_id=member.id,
        source="miniprogram",
        meta={"via": "mp_api"},
    )
    log_usage_event(
        "pair_created",
        user_id=g.api_user_id,
        pair_id=pair.id,
        member_id=member.id,
        source="miniprogram",
        meta={"location_query": location_query},
    )

    return jsonify({"success": True, "data": {"pair_id": pair.id, "member_id": member.id}})


@bp.route("/elders/<int:pair_id>", methods=["PATCH"], endpoint="elders_patch")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def elders_patch(pair_id: int):
    pair = _pair_for_user(pair_id)
    if not pair:
        return jsonify({"success": False, "error": "not_found"}), 404

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "invalid_payload"}), 400

    location_query = _MISSING
    age = _MISSING
    gender = _MISSING
    chronic = _MISSING
    try:
        if "location_query" in payload:
            location_query = _parse_required_location(payload.get("location_query"))
        if "age" in payload:
            age = _parse_optional_age(payload.get("age"))
        if "gender" in payload:
            gender = _parse_optional_gender(payload.get("gender"))
        if "chronic_diseases" in payload:
            chronic = _parse_chronic_diseases(payload.get("chronic_diseases"))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    member = None
    if age is not _MISSING or gender is not _MISSING or chronic is not _MISSING:
        member = FamilyMember.query.filter_by(id=pair.member_id, user_id=g.api_user_id).first()
        if not member:
            return jsonify({"success": False, "error": "member_not_found"}), 404

    # 所有字段校验完成后才写入 ORM，避免后续失败留下部分更新。
    if location_query is not _MISSING:
        pair.location_query = location_query
        pair.community_code = location_query[:100]

    if member:
        if age is not _MISSING:
            member.age = age
        if gender is not _MISSING:
            member.gender = gender
        if chronic is not _MISSING:
            member.chronic_diseases = json.dumps(chronic, ensure_ascii=False) if chronic else None

    db.session.commit()
    log_usage_event(
        "elder_profile_updated",
        user_id=g.api_user_id,
        pair_id=pair.id,
        member_id=pair.member_id,
        source="miniprogram",
        meta={"updated_fields": list(payload.keys())[:20]},
    )
    return jsonify({"success": True})


@bp.route("/alerts", endpoint="alerts_list")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_ALERTS", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def alerts_list():
    pair_id = request.args.get("pair_id", type=int)
    if not pair_id:
        return jsonify({"success": False, "error": "missing pair_id"}), 400
    pair = _pair_for_user(pair_id)
    if not pair:
        return jsonify({"success": False, "error": "not_found"}), 404

    label = (pair.location_query or pair.community_code or "").strip()
    resolved = resolve_location(label)
    code = resolved.get("location_code") or ""
    warnings = get_qweather_warnings(code) if code else []
    weather_data, _ = get_weather_with_cache(code or label)
    weather_available = is_qweather_production_ready(weather_data)

    return jsonify(
        {
            "success": True,
            "data": {
                "location": {"query": label, "code": code, "provider": resolved.get("provider")},
                "warnings": warnings,
                "weather": {
                    "temperature_max": weather_data.get("temperature_max") if weather_available else None,
                    "temperature_min": weather_data.get("temperature_min") if weather_available else None,
                    "weather_available": weather_available,
                    "is_mock": bool(weather_data.get("is_mock")),
                    "location": weather_data.get("location"),
                    "observed_at": weather_data.get("observed_at") if weather_available else None,
                },
            },
        }
    )


@bp.route("/events", methods=["POST"], endpoint="events")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_EVENTS", "60 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def events():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "invalid_payload"}), 400

    event_type = sanitize_input(payload.get("event_type"), max_length=50) or ""
    if event_type not in MP_CLIENT_EVENT_TYPES:
        return jsonify({"success": False, "error": "invalid_event_type"}), 400

    if "meta" in payload and payload.get("meta") is not None and not isinstance(payload.get("meta"), (dict, list)):
        return jsonify({"success": False, "error": "invalid_meta"}), 400
    meta = payload.get("meta")
    if meta is not None:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "invalid_meta"}), 400
        if len(meta_json) > MP_EVENT_META_MAX_CHARS:
            return jsonify({"success": False, "error": "meta_too_large"}), 400

    if event_type == "template_copy" and "meta" in payload:
        if _template_copy_meta_invalid(meta):
            return jsonify({"success": False, "error": "invalid_meta"}), 400

    pair = None
    resolved_pair_id = None
    if "pair_id" in payload:
        try:
            pair_id = _parse_positive_id(payload.get("pair_id"), "invalid_pair_id")
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        pair = _pair_for_user(pair_id)
        if not pair:
            return jsonify({"success": False, "error": "not_found"}), 404
        resolved_pair_id = pair.id

    member = None
    resolved_member_id = None
    if "member_id" in payload:
        try:
            member_id = _parse_positive_id(payload.get("member_id"), "invalid_member_id")
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        member = FamilyMember.query.filter_by(id=member_id, user_id=g.api_user_id).first()
        if not member:
            return jsonify({"success": False, "error": "member_not_found"}), 404
        resolved_member_id = member.id

    if pair and member and pair.member_id != member.id:
        return jsonify({"success": False, "error": "pair_member_mismatch"}), 400

    event = log_usage_event(
        event_type,
        user_id=g.api_user_id,
        pair_id=resolved_pair_id,
        member_id=resolved_member_id,
        source="miniprogram",
        meta=meta,
    )
    if event is None:
        return jsonify({"success": False, "error": "event_write_failed"}), 503
    return jsonify({"success": True})


@bp.route("/pending", endpoint="pending")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def pending():
    pairs = (
        Pair.query.filter_by(caregiver_id=g.api_user_id, status="active")
        .order_by(Pair.created_at.desc())
        .all()
    )
    member_ids = [p.member_id for p in pairs if p.member_id]
    members = FamilyMember.query.filter(FamilyMember.id.in_(member_ids)).all() if member_ids else []
    member_map = {m.id: m for m in members}

    items = []
    for pair in pairs:
        member = member_map.get(pair.member_id) if pair.member_id else None
        items.append(
            {
                "pair_id": pair.id,
                "elder_label": _elder_label(member),
                "today": _pending_today(today_state(pair)),
            }
        )

    payload = {"pairs": items}
    return jsonify({"success": True, "data": payload, "pairs": items})


@bp.route("/pairs/<int:pair_id>/events", methods=["POST"], endpoint="pair_events")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def pair_events(pair_id: int):
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "invalid_payload"}), 400

    pair = _pair_for_user(pair_id)
    if not pair:
        return jsonify({"success": False, "error": "not_found"}), 404

    stage = sanitize_input(payload.get("stage"), max_length=32) or ""
    if stage not in MP_CAREGIVER_STAGES:
        return jsonify({"success": False, "error": "forbidden"}), 403

    messenger_role = sanitize_input(payload.get("messenger_role"), max_length=20)
    messenger_channel = sanitize_input(payload.get("channel"), max_length=24)
    script_version = sanitize_input(payload.get("script_version"), max_length=16)

    try:
        event = record_event(
            pair,
            stage,
            "caregiver",
            "miniprogram",
            script_version=script_version,
        )
    except InvalidTransition as exc:
        db.session.rollback()
        return exc.to_response()

    if stage == "delivered":
        log_usage_event(
            "caregiver_delivered",
            user_id=g.api_user_id,
            pair_id=pair.id,
            member_id=getattr(pair, "member_id", None),
            source="miniprogram",
            meta={
                "messenger_role": messenger_role,
                "channel": messenger_channel,
                "event": "delivered",
            },
        )

    state = _pending_today(today_state(pair))
    return jsonify(
        {
            "success": True,
            "data": {"event_id": event.id, "state": state},
        }
    )
