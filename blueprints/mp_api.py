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
from core.time_utils import ensure_utc_aware, utcnow
from core.usage import log_usage_event, verify_api_token
from core.weather import get_weather_with_cache, is_qweather_production_ready
from services.action_events import InvalidTransition, record_event, today_state
from services.content_scripts import script_catalog
from services.family_access import can_access_pair
from services.help_http import error_payload, handle_domain_error, json_body
from services.help_request_service import (
    ack_help_request,
    apply_pair_help_stage,
    capabilities,
    cancel_help_request,
    create_help_request,
    get_help_request,
    list_help_requests,
    resolve_help_request,
    start_help_request,
)
from services.location_resolver import resolve_location
from services.notification_outbox import process_outbox_batch
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


def _mp_token_rate_key() -> str:
    """已出示的 Bearer 分桶；无效令牌仍会落到 IP 桶。"""
    token = _bearer_token()
    if token:
        return f"mp-tok:{hash_identifier(token)}"
    return _mp_rate_limit_key()


def _touch_last_used(record):
    """5 秒轮询不得每次写 last_used_at。"""
    if record is None:
        return
    now = utcnow()
    last = ensure_utc_aware(getattr(record, "last_used_at", None))
    try:
        interval = int(current_app.config.get("WX_MINIPROGRAM_LAST_USED_TOUCH_SECONDS", 60) or 60)
    except (TypeError, ValueError):
        interval = 60
    interval = max(0, min(interval, 3600))
    if last is not None and (now - last).total_seconds() < interval:
        return
    record.last_used_at = now
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def require_api_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer_token()
        session_record = None
        try:
            from services.miniprogram_auth import verify_miniprogram_session
            session_record = verify_miniprogram_session(token)
        except Exception:
            session_record = None
        if session_record is not None:
            g.mp_session = session_record
            g.api_token = None
            g.api_user_id = session_record.user_id
            g.auth_kind = "miniprogram_session"
            g.api_user = db.session.get(User, session_record.user_id)
            _touch_last_used(session_record)
            return fn(*args, **kwargs)
        record = verify_api_token(token)
        if not record:
            return jsonify({"success": False, "error": "unauthorized", "request_id": getattr(g, "request_id", None)}), 401
        _touch_last_used(record)
        g.api_token = record
        g.api_user_id = record.user_id
        g.auth_kind = "api_token"
        g.mp_session = None
        g.api_user = db.session.get(User, record.user_id)
        return fn(*args, **kwargs)

    return wrapper


def _current_api_user():
    return db.session.get(User, g.api_user_id)


def _pair_for_user(pair_id: int, action: str = "read"):
    pair = Pair.query.filter_by(id=pair_id, status="active").first()
    if not pair:
        return None
    if can_access_pair(_current_api_user(), pair, action):
        return pair
    return None


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
    from services.family_access import visible_pair_ids_for_user

    pair_ids = visible_pair_ids_for_user(g.api_user_id)
    pairs = (
        Pair.query.filter(Pair.id.in_(pair_ids or [-1]), Pair.status == "active")
        .order_by(Pair.created_at.desc())
        .all()
        if pair_ids
        else []
    )
    member_ids = [p.member_id for p in pairs if p.member_id]
    members = (
        FamilyMember.query.filter(FamilyMember.id.in_(member_ids)).all() if member_ids else []
    )
    member_map = {m.id: m for m in members}
    from services.miniprogram_bootstrap import get_bootstrap_payload

    snapshot = get_bootstrap_payload()
    current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
    weather_available = bool(snapshot.get("available")) and not bool(snapshot.get("stale"))
    tmax_value = current.get("temperature_max")
    tmin_value = current.get("temperature_min")
    trigger = None
    try:
        tmax_value = float(tmax_value) if tmax_value is not None else None
        tmin_value = float(tmin_value) if tmin_value is not None else None
    except (TypeError, ValueError):
        tmax_value = None
        tmin_value = None
    if weather_available and tmax_value is not None and tmax_value >= 35:
        trigger = "heat"
    elif weather_available and tmin_value is not None and tmin_value <= 5:
        trigger = "cold"

    result = []
    for p in pairs:
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
                    "is_mock": bool(current.get("is_mock")),
                    "location": snapshot.get("location"),
                    "observed_at": snapshot.get("observed_at") if weather_available else None,
                    "stale": bool(snapshot.get("stale")),
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
    pair = _pair_for_user(pair_id, "manage")
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


@bp.route("/elders/<int:pair_id>", methods=["DELETE"], endpoint="elders_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def elders_delete(pair_id: int):
    from services.miniprogram_care import MiniProgramCareError, deactivate_pair

    try:
        return _ok(deactivate_pair(_current_api_user(), pair_id))
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


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
@limiter.limit(
    lambda: current_app.config.get("RATE_LIMIT_MP_PENDING_IP", "400 per minute"),
    key_func=_mp_rate_limit_key,
)
@limiter.limit(
    lambda: current_app.config.get("RATE_LIMIT_MP_PENDING_USER", "360 per minute"),
    key_func=_mp_token_rate_key,
)
@require_api_token
def pending():
    """未结求助列表。禁止在此路径调用天气供应商或 get_weather_with_cache。"""
    user = _current_api_user()
    listed = list_help_requests(user, status="open", limit=50)
    from services.family_access import visible_pair_ids_for_user

    pair_ids = visible_pair_ids_for_user(g.api_user_id)
    pair_rows = (
        Pair.query.filter(Pair.id.in_(pair_ids or [-1]), Pair.status == "active")
        .order_by(Pair.created_at.desc())
        .all()
        if pair_ids
        else []
    )
    member_ids = [p.member_id for p in pair_rows if p.member_id]
    members = FamilyMember.query.filter(FamilyMember.id.in_(member_ids)).all() if member_ids else []
    member_map = {m.id: m for m in members}

    items = []
    for pair in pair_rows:
        member = member_map.get(pair.member_id) if pair.member_id else None
        items.append(
            {
                "pair_id": pair.id,
                "elder_label": _elder_label(member),
                "today": _pending_today(today_state(pair)),
            }
        )

    payload = {
        "schema_version": listed["schema_version"],
        "pairs": items,
        "help_requests": listed["items"],
        "open_count": listed["open_count"],
        "pending_ack_count": listed["pending_ack_count"],
        "unavailable": False,
    }
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
    user = _current_api_user()

    if stage in {"help_acknowledged", "closed"}:
        from services.help_request_service import open_help_for_pair

        if open_help_for_pair(pair.id):
            try:
                help_body = apply_pair_help_stage(
                    user,
                    pair,
                    stage,
                    origin_channel="miniprogram",
                    commit=True,
                )
            except Exception as exc:
                db.session.rollback()
                return handle_domain_error(exc)
            state = _pending_today(today_state(pair))
            return jsonify(
                {
                    "success": True,
                    "data": {
                        "event_id": None,
                        "state": state,
                        "help_request": help_body,
                    },
                }
            )

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


def _ok(data, status=200):
    return jsonify({"success": True, "data": data, "request_id": getattr(g, "request_id", None)}), status


@bp.route("/capabilities", endpoint="capabilities")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
def mp_capabilities():
    return _ok(capabilities())


@bp.route("/scripts", endpoint="scripts")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_scripts():
    return _ok(script_catalog())


@bp.route("/bootstrap", endpoint="bootstrap")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
def mp_bootstrap():
    from services.miniprogram_bootstrap import get_bootstrap_payload
    return _ok(get_bootstrap_payload())


@bp.route("/public/communities", endpoint="public_communities")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def mp_public_communities():
    from services.miniprogram_public import public_communities_payload
    return _ok(public_communities_payload())


@bp.route("/public/cooling-resources", endpoint="public_cooling_resources")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def mp_public_cooling_resources():
    from services.miniprogram_public import public_cooling_resources_payload
    return _ok(public_cooling_resources_payload())


@bp.route("/public/gis-metadata", endpoint="public_gis_metadata")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def mp_public_gis_metadata():
    from services.miniprogram_public import public_gis_metadata_payload
    try:
        return _ok(public_gis_metadata_payload())
    except (OSError, ValueError, json.JSONDecodeError):
        return _ok({"available": False, "scope": "都昌县", "hold": True})


@bp.route("/public/community", endpoint="public_community_bundle")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def mp_public_community_bundle():
    from services.miniprogram_public import public_community_bundle
    return _ok(public_community_bundle())


@bp.route("/health-consent", methods=["GET"], endpoint="health_consent_get")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_health_consent_get():
    from services.miniprogram_care import health_consent_payload
    return _ok(health_consent_payload(_current_api_user()))


@bp.route("/health-consent", methods=["POST"], endpoint="health_consent_post")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_health_consent_post():
    from services.miniprogram_care import MiniProgramCareError, save_health_consent
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        return _ok(save_health_consent(
            _current_api_user(),
            consent=payload.get("consent"),
            version=payload.get("health_consent_version") or payload.get("version"),
        ))
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


@bp.route("/health-consent", methods=["DELETE"], endpoint="health_consent_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_health_consent_delete():
    from services.miniprogram_care import MiniProgramCareError, withdraw_health_consent
    try:
        return _ok(withdraw_health_consent(_current_api_user()))
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


def _strict_text(payload, field, limit):
    raw = "" if not isinstance(payload, dict) else payload.get(field)
    text = str(raw or "").strip()
    return text[: int(limit)]


@bp.route("/health/diary", methods=["GET", "POST"], endpoint="health_diary")
@limiter.limit(
    lambda: current_app.config.get(
        "RATE_LIMIT_MP_WRITE" if request.method == "POST" else "RATE_LIMIT_MP_READ",
        "30 per minute" if request.method == "POST" else "120 per minute",
    ),
    key_func=_mp_rate_limit_key,
)
@require_api_token
def mp_health_diary():
    from services.miniprogram_care import MiniProgramCareError, list_or_create_diary
    payload = json_body() if request.get_json(silent=True) is not None else {}
    if request.method == "POST" and isinstance(payload, dict):
        symptoms = _strict_text(payload, "symptoms", 200)
        notes = _strict_text(payload, "notes", 500)
        payload = dict(payload)
        payload["symptoms"] = symptoms
        payload["notes"] = notes
    try:
        data = list_or_create_diary(
            _current_api_user(),
            method=request.method,
            payload=payload,
            args=request.args,
        )
        return _ok(data, 201 if request.method == "POST" else 200)
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


@bp.route("/medications", methods=["GET", "POST", "DELETE"], endpoint="medications")
@limiter.limit(
    lambda: current_app.config.get(
        "RATE_LIMIT_MP_WRITE" if request.method != "GET" else "RATE_LIMIT_MP_READ",
        "30 per minute" if request.method != "GET" else "120 per minute",
    ),
    key_func=_mp_rate_limit_key,
)
@require_api_token
def mp_medications():
    from services.miniprogram_care import MiniProgramCareError, list_or_mutate_medications
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        data = list_or_mutate_medications(
            _current_api_user(),
            method=request.method,
            payload=payload,
            args=request.args,
        )
        return _ok(data, 201 if request.method == "POST" else 200)
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


@bp.route("/medications/<int:record_id>", methods=["DELETE"], endpoint="medication_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_medication_delete(record_id):
    from services.miniprogram_care import MiniProgramCareError, delete_medication
    try:
        return _ok(delete_medication(_current_api_user(), record_id))
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


@bp.route("/health/assessment", methods=["GET", "POST"], endpoint="health_assessment")
@limiter.limit(
    lambda: current_app.config.get(
        "RATE_LIMIT_MP_WRITE" if request.method == "POST" else "RATE_LIMIT_MP_READ",
        "30 per minute" if request.method == "POST" else "120 per minute",
    ),
    key_func=_mp_rate_limit_key,
)
@require_api_token
def mp_health_assessment():
    from services.miniprogram_care import MiniProgramCareError, get_or_record_assessment
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        data = get_or_record_assessment(
            _current_api_user(),
            method=request.method,
            payload=payload,
            args=request.args,
        )
        return _ok(data, 201 if request.method == "POST" else 200)
    except MiniProgramCareError as exc:
        return error_payload(exc.code, exc.message, exc.status_code, exc.extra)


@bp.route("/help-requests", methods=["POST"], endpoint="help_requests_create")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_create():
    try:
        payload = json_body()
        pair_id = int(payload.get("pair_id") or 0)
    except (TypeError, ValueError):
        return error_payload("invalid_pair_id", "照护对象无效。", 400)
    pair = _pair_for_user(pair_id, "create_help")
    if not pair:
        return error_payload("not_found", "对象不存在或无权访问。", 404)
    try:
        body, _created = create_help_request(
            _current_api_user(),
            pair,
            category=payload.get("category") or "cannot_complete",
            origin_channel="miniprogram",
            idempotency_key=payload.get("idempotency_key"),
            is_proxy=True,
            actor_role="elder_proxy",
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/help-requests", endpoint="help_requests_list")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_list():
    try:
        data = list_help_requests(
            _current_api_user(),
            status=request.args.get("status") or "open",
            cursor=request.args.get("cursor"),
            limit=request.args.get("limit") or 20,
        )
        return _ok(data)
    except Exception as exc:
        return handle_domain_error(exc)


@bp.route("/help-requests/<public_id>", endpoint="help_requests_detail")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_detail(public_id):
    try:
        return _ok(get_help_request(_current_api_user(), public_id))
    except Exception as exc:
        return handle_domain_error(exc)


@bp.route("/help-requests/<public_id>/ack", methods=["POST"], endpoint="help_requests_ack")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_ack(public_id):
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        body = ack_help_request(
            _current_api_user(),
            public_id,
            expected_version=payload.get("expected_version"),
            idempotency_key=payload.get("idempotency_key"),
            origin_channel="miniprogram",
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/help-requests/<public_id>/start", methods=["POST"], endpoint="help_requests_start")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_start(public_id):
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        body = start_help_request(
            _current_api_user(),
            public_id,
            expected_version=payload.get("expected_version"),
            idempotency_key=payload.get("idempotency_key"),
            origin_channel="miniprogram",
            commit=True,
        )
        return _ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/help-requests/<public_id>/resolve", methods=["POST"], endpoint="help_requests_resolve")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_resolve(public_id):
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        body = resolve_help_request(
            _current_api_user(),
            public_id,
            expected_version=payload.get("expected_version"),
            resolution_code=payload.get("resolution_code") or "reached_elder",
            idempotency_key=payload.get("idempotency_key"),
            origin_channel="miniprogram",
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/help-requests/<public_id>/cancel", methods=["POST"], endpoint="help_requests_cancel")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_help_cancel(public_id):
    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        body = cancel_help_request(
            _current_api_user(),
            public_id,
            expected_version=payload.get("expected_version"),
            reason_code=payload.get("cancel_reason") or payload.get("reason_code") or "other",
            idempotency_key=payload.get("idempotency_key"),
            origin_channel="miniprogram",
            commit=True,
        )
        return _ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/actions/<int:pair_id>/help", methods=["POST"], endpoint="action_help")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_legacy_action_help(pair_id):
    """存量小程序求助入口，适配同一求助服务。"""
    payload = json_body() if request.get_json(silent=True) is not None else {}
    pair = _pair_for_user(pair_id, "create_help")
    if not pair:
        return error_payload("not_found", "对象不存在或无权访问。", 404)
    try:
        body, _created = create_help_request(
            _current_api_user(),
            pair,
            category="cannot_complete",
            origin_channel="miniprogram",
            idempotency_key=payload.get("idempotency_key"),
            is_proxy=True,
            actor_role="elder_proxy",
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _ok({
            "help_flag": True,
            "id": body["id"],
            "status": body["status"],
            "status_label": body.get("status_label"),
            "version": body["version"],
            "created_at": body["created_at"],
            "replayed": body.get("replayed", False),
        })
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/auth/wechat", methods=["POST"], endpoint="wechat_login")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_AUTH", "10 per 5 minutes"), key_func=_mp_rate_limit_key)
def wechat_login():
    from services.miniprogram_auth import MiniProgramAuthError, current_privacy_version, login_with_wechat_code

    payload = json_body() if request.get_json(silent=True) is not None else {}
    try:
        result = login_with_wechat_code(
            payload.get("code") or "",
            payload.get("privacy_consent_version") or "",
            payload.get("acquisition_source") or "direct",
        )
    except MiniProgramAuthError as exc:
        extra = None
        if exc.code == "privacy_consent_required":
            version = current_privacy_version()
            extra = {
                "required_privacy_consent_version": version,
                "data": {"required_privacy_consent_version": version},
            }
        return error_payload(exc.code, exc.message, exc.status_code, extra)
    log_usage_event(
        "wechat_login_success",
        user_id=(result.get("user") or {}).get("id"),
        source="miniprogram",
        meta={"from": payload.get("acquisition_source") or "direct"},
    )
    return _ok(result)


@bp.route("/auth/logout", methods=["POST"], endpoint="wechat_logout")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def wechat_logout():
    if getattr(g, "auth_kind", None) != "miniprogram_session":
        return error_payload("miniprogram_session_required", "该操作仅支持微信小程序会话。", 403)
    session_record = g.mp_session
    session_record.revoked_at = utcnow()
    db.session.commit()
    return _ok({"revoked": True})


@bp.route("/family-invites/<code>", endpoint="mp_family_invite_preview")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_family_invite_preview(code):
    from services.family_access import preview_invite

    try:
        return _ok(preview_invite(code))
    except Exception as exc:
        return handle_domain_error(exc)


@bp.route("/family-invites/<code>/accept", methods=["POST"], endpoint="mp_family_invite_accept")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_family_invite_accept(code):
    from services.family_access import consume_invite

    try:
        membership, invite = consume_invite(_current_api_user(), code)
        db.session.commit()
        return _ok({"role": membership.role, "family_space_id": invite.family_space_id})
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route("/actions/<int:pair_id>/confirm", methods=["POST"], endpoint="action_confirm")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_CONFIRM", "30 per hour"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_action_confirm(pair_id):
    pair = _pair_for_user(pair_id, "read")
    if not pair:
        return error_payload("not_found", "对象不存在或无权访问。", 404)
    try:
        event = record_event(pair, "self_reported", "elder", "miniprogram", commit=True)
    except InvalidTransition as exc:
        db.session.rollback()
        return exc.to_response()
    return _ok({
        "pair_id": pair.id,
        "confirmed_at": event.created_at.isoformat() if event.created_at else utcnow().isoformat(),
    })


@bp.route("/actions/<int:pair_id>/debrief", methods=["POST"], endpoint="action_debrief")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_action_debrief(pair_id):
    from core.db_models import Debrief
    from core.time_utils import today_local

    pair = _pair_for_user(pair_id, "read")
    if not pair:
        return error_payload("not_found", "对象不存在或无权访问。", 404)
    payload = json_body() if request.get_json(silent=True) is not None else {}
    row = Debrief(
        date=today_local(),
        community_code=pair.community_code or "",
        pair_id=pair.id,
        question_1=sanitize_input(payload.get("question_1"), max_length=200) or "",
        question_2=sanitize_input(payload.get("question_2"), max_length=200) or "",
        question_3=sanitize_input(payload.get("question_3"), max_length=200) or "",
        difficulty=sanitize_input(payload.get("difficulty"), max_length=500) or "",
        created_at=utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    return _ok({"debrief_id": row.id, "pair_id": pair.id})


@bp.route("/me", methods=["DELETE"], endpoint="me_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def mp_me_delete():
    """微信会话账号撤销：立即收回家庭读写并作废会话。"""
    if getattr(g, "auth_kind", None) != "miniprogram_session":
        return error_payload("miniprogram_session_required", "账号注销仅支持微信小程序登录会话。", 403)
    payload = json_body() if request.get_json(silent=True) is not None else {}
    if payload.get("confirm") is not True:
        return error_payload("delete_confirmation_required", "请明确确认账号注销。", 400)
    from core.db_models import FamilyMembership, MiniProgramSession

    user = _current_api_user()
    user.deleted_at = utcnow()
    user.health_sensitive_consent_version = None
    user.health_sensitive_consented_at = None
    FamilyMembership.query.filter_by(user_id=user.id, status="active").update(
        {"status": "revoked", "revoked_at": utcnow()},
        synchronize_session=False,
    )
    MiniProgramSession.query.filter_by(user_id=user.id, revoked_at=None).update(
        {"revoked_at": utcnow()},
        synchronize_session=False,
    )
    db.session.commit()
    return _ok({"deleted": True, "session_revoked": True})

