# -*- coding: utf-8 -*-
"""MiniProgram API (no CSRF; Bearer API token auth).

Endpoints:
- GET  /mp/api/v1/me
- GET  /mp/api/v1/elders
- POST /mp/api/v1/elders
- PATCH /mp/api/v1/elders/<pair_id>
- GET  /mp/api/v1/alerts?pair_id=...
- POST /mp/api/v1/events
"""

from __future__ import annotations

import json
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from core.audit import _get_client_ip
from core.db_models import FamilyMember, FamilyMemberProfile, Pair, User
from core.extensions import db, limiter
from core.security import hash_identifier
from core.time_utils import utcnow
from core.usage import log_usage_event, verify_api_token
from core.weather import get_weather_with_cache, is_qweather_online_weather
from services.api_service import PILOT_EVENT_TYPES
from services.location_resolver import resolve_location
from services.warning_service import get_qweather_warnings
from services.user._common import _create_pair_record
from utils.parsers import safe_json_loads
from utils.validators import sanitize_input

bp = Blueprint("mp_api", __name__, url_prefix="/mp/api/v1")
MP_EVENT_META_MAX_CHARS = 2048


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
    q = Pair.query.filter_by(id=pair_id)
    # admin token is not supported in pilot; restrict to owner
    q = q.filter_by(caregiver_id=g.api_user_id)
    return q.first()


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
    payload = request.get_json(silent=True) or {}
    wx_uid = sanitize_input(payload.get("wxpusher_uid"), max_length=80)
    wx_uid = (wx_uid.strip() if isinstance(wx_uid, str) else None) or None
    push_enabled = bool(payload.get("push_enabled"))
    if push_enabled and not wx_uid:
        push_enabled = False

    user.wxpusher_uid = wx_uid
    user.push_enabled = bool(push_enabled)
    db.session.commit()
    log_usage_event(
        "settings_updated",
        user_id=user.id,
        source="miniprogram",
        meta={"fields": ["wxpusher_uid", "push_enabled"]},
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
            is_qweather_online_weather(weather_data)
            and tmax_value is not None
            and tmin_value is not None
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
                },
            }
        )

    return jsonify({"success": True, "data": result})


@bp.route("/elders", methods=["POST"], endpoint="elders_create")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def elders_create():
    payload = request.get_json(silent=True) or {}
    name = sanitize_input(payload.get("name"), max_length=50) or ""
    relation = sanitize_input(payload.get("relation"), max_length=20) or ""
    location_query = sanitize_input(payload.get("location_query"), max_length=200) or ""
    if not name or not location_query:
        return jsonify({"success": False, "error": "missing_fields"}), 400

    age = payload.get("age")
    try:
        age = int(age) if age is not None and str(age).strip() else None
    except Exception:
        age = None
    gender = sanitize_input(payload.get("gender"), max_length=10)
    chronic = payload.get("chronic_diseases")
    chronic = chronic if isinstance(chronic, list) else []
    chronic = [sanitize_input(item, max_length=50) for item in chronic if item]
    chronic = [c for c in chronic if c]

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

    payload = request.get_json(silent=True) or {}
    location_query = sanitize_input(payload.get("location_query"), max_length=200)
    if location_query is not None:
        pair.location_query = location_query
        if location_query:
            pair.community_code = location_query[:100]

    chronic = payload.get("chronic_diseases")
    if chronic is not None and pair.member_id:
        chronic = chronic if isinstance(chronic, list) else []
        chronic = [sanitize_input(item, max_length=50) for item in chronic if item]
        chronic = [c for c in chronic if c]
        member = FamilyMember.query.filter_by(id=pair.member_id, user_id=g.api_user_id).first()
        if member:
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
    weather_available = is_qweather_online_weather(weather_data)

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
                },
            },
        }
    )


@bp.route("/events", methods=["POST"], endpoint="events")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_EVENTS", "60 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def events():
    payload = request.get_json(silent=True) or {}
    event_type = sanitize_input(payload.get("event_type"), max_length=50) or ""
    if event_type not in PILOT_EVENT_TYPES:
        return jsonify({"success": False, "error": "invalid_event_type"}), 400
    pair_id = payload.get("pair_id")
    member_id = payload.get("member_id")
    meta = payload.get("meta") if isinstance(payload.get("meta"), (dict, list)) else None
    if meta is not None:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "invalid_meta"}), 400
        if len(meta_json) > MP_EVENT_META_MAX_CHARS:
            return jsonify({"success": False, "error": "meta_too_large"}), 400

    resolved_pair_id = None
    if pair_id is not None:
        try:
            pair_id_int = int(pair_id)
        except Exception:
            pair_id_int = None
        if pair_id_int:
            pair = _pair_for_user(pair_id_int)
            if pair:
                resolved_pair_id = pair.id

    resolved_member_id = None
    if member_id is not None:
        try:
            member_id_int = int(member_id)
        except Exception:
            member_id_int = None
        if member_id_int:
            member = FamilyMember.query.filter_by(id=member_id_int, user_id=g.api_user_id).first()
            if member:
                resolved_member_id = member.id

    log_usage_event(
        event_type,
        user_id=g.api_user_id,
        pair_id=resolved_pair_id,
        member_id=resolved_member_id,
        source="miniprogram",
        meta=meta,
    )
    return jsonify({"success": True})
