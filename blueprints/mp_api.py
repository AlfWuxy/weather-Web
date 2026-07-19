# -*- coding: utf-8 -*-
"""微信小程序 API（无 Cookie/CSRF，使用 Bearer 会话或兼容 API token）。

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
import math
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from core.audit import _get_client_ip
from core.db_models import (
    AlertDelivery,
    ApiToken,
    AuditLog,
    DailyStatus,
    Debrief,
    FamilyMember,
    FamilyMemberProfile,
    HealthDiary,
    HealthRiskAssessment,
    MedicationReminder,
    MiniProgramIdentity,
    MiniProgramSession,
    Notification,
    Pair,
    PairActionToken,
    PairLink,
    UsageEvent,
    User,
)
from core.extensions import db, limiter
from core.security import hash_identifier
from core.time_utils import ensure_utc_aware, today_local, utcnow
from core.usage import (
    MINIPROGRAM_CLIENT_PILOT_EVENT_TYPES,
    api_token_has_scope,
    log_usage_event,
    verify_api_token,
)
from core.weather import (
    compact_assessment_weather_condition,
    get_weather_with_cache,
    is_qweather_online_weather,
)
from services.location_resolver import resolve_location
from services.warning_service import get_qweather_warnings
from services.user._common import _create_pair_record
from services.care_action_service import (
    RELAY_STAGES,
    get_or_create_daily_status,
    stage_confirm_action,
    stage_debrief_action,
    stage_help_action,
)
from services.community_daily_service import (
    refresh_community_daily_best_effort,
    refresh_latest_community_daily_best_effort,
)
from services.miniprogram_auth import (
    MiniProgramAuthError,
    current_privacy_version,
    login_with_wechat_code,
    verify_miniprogram_session,
)
from services.miniprogram_service import (
    CANONICAL_LOCATION_NAME,
    get_bootstrap_payload,
    public_communities_payload,
    public_cooling_resources_payload,
    public_gis_metadata_payload,
)
from services.push.locks import push_owner_lock
from utils.parsers import safe_json_loads
from utils.validators import sanitize_input

bp = Blueprint("mp_api", __name__, url_prefix="/mp/api/v1")
MP_EVENT_META_MAX_CHARS = 2048
MAX_PAGE_SIZE = 50
ACQUISITION_SOURCE_FAMILY_SHARE = "family_share"
CREDENTIAL_LAST_USED_INTERVAL = timedelta(minutes=30)


def _success(data=None, status=200):
    return jsonify({"success": True, "data": data if data is not None else {}}), status


def _error(error, message, status=400, data=None):
    payload = {"success": False, "error": error, "message": message}
    if data is not None:
        payload["data"] = data
    return jsonify(payload), status


def _wxpusher_feature_enabled():
    """首发关闭时统一隐藏入口，并阻止客户端保存第三方标识。"""
    return bool(current_app.config.get("FEATURE_WXPUSHER", False))


def _wxpusher_available():
    """只暴露推送通道是否可用，不向客户端返回任何凭证内容。"""
    return bool(
        _wxpusher_feature_enabled()
        and (current_app.config.get("WXPUSHER_APP_TOKEN") or "").strip()
    )


def _wxpusher_consent_is_current(user, required_version=None):
    """同意版本和时间同时存在才是有效回执。"""
    version = required_version or current_privacy_version()
    return bool(
        getattr(user, "wxpusher_consented_at", None) is not None
        and getattr(user, "wxpusher_consent_version", None) == version
    )


def _health_consent_is_current(user, required_version=None):
    """健康敏感信息必须有独立、当前版本且带时间的回执。"""
    version = required_version or current_privacy_version()
    return bool(
        getattr(user, "health_sensitive_consented_at", None) is not None
        and getattr(user, "health_sensitive_consent_version", None) == version
    )


def _health_consent_payload(user):
    """统一健康同意状态字段，时间始终按 UTC 输出。"""
    required_version = current_privacy_version()
    consented_at = getattr(user, "health_sensitive_consented_at", None)
    return {
        "required_health_consent_version": required_version,
        "health_consent_current": _health_consent_is_current(
            user,
            required_version,
        ),
        "health_consented_at": (
            ensure_utc_aware(consented_at).isoformat()
            if consented_at is not None
            else None
        ),
    }


def _json_payload():
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("invalid_payload")
    return payload


def _strict_text(payload, name, max_length, *, required=False, default=None):
    value = payload.get(name, default)
    if value is None:
        if required:
            raise ValueError(f"missing_{name}")
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError(f"invalid_{name}")
    raw = str(value).strip()
    if required and not raw:
        raise ValueError(f"missing_{name}")
    if len(raw) > max_length:
        raise ValueError(f"{name}_too_long")
    cleaned = sanitize_input(raw, max_length=max_length)
    return cleaned.strip() if isinstance(cleaned, str) else cleaned


def _int_value(value, *, minimum=None, maximum=None, field="value", allow_none=True):
    if value in (None, "") and allow_none:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{field}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"invalid_{field}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"invalid_{field}")
    return parsed


def _finite_or_none(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_stored_text_list(value, *, max_items=20, item_max_length=50):
    """把历史 JSON 字段收敛为有界纯文本列表。"""
    parsed = safe_json_loads(value, [])
    if not isinstance(parsed, list):
        return []
    items = []
    for item in parsed[:max_items]:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped or len(stripped) > item_max_length:
            continue
        cleaned = sanitize_input(stripped, max_length=item_max_length)
        if cleaned == stripped:
            items.append(cleaned)
    return items


def _list_of_text(payload, name, *, max_items, item_max_length):
    value = payload.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"invalid_{name}")
    items = []
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > item_max_length:
            raise ValueError(f"invalid_{name}")
        cleaned = sanitize_input(item, max_length=item_max_length)
        if cleaned and cleaned.strip():
            items.append(cleaned.strip())
    return items


def _weather_triggers(payload):
    """兼容旧列表，并严格接收小程序阈值对象。"""
    value = payload.get("weather_triggers")
    if value is None:
        return {}
    if isinstance(value, list):
        return _list_of_text(
            payload,
            "weather_triggers",
            max_items=20,
            item_max_length=50,
        )
    if not isinstance(value, dict):
        raise ValueError("invalid_weather_triggers")
    ranges = {
        "high_temp": (-50.0, 60.0),
        "low_temp": (-50.0, 60.0),
        "high_humidity": (0.0, 100.0),
        "high_aqi": (0.0, 500.0),
    }
    if set(value) - set(ranges):
        raise ValueError("invalid_weather_triggers")
    cleaned = {}
    for key, raw in value.items():
        if raw in (None, ""):
            continue
        if isinstance(raw, bool):
            raise ValueError("invalid_weather_triggers")
        number = _finite_or_none(raw)
        minimum, maximum = ranges[key]
        if number is None or not minimum <= number <= maximum:
            raise ValueError("invalid_weather_triggers")
        cleaned[key] = number
    if (
        "low_temp" in cleaned
        and "high_temp" in cleaned
        and cleaned["low_temp"] > cleaned["high_temp"]
    ):
        raise ValueError("invalid_weather_triggers")
    return cleaned


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


def _credential_last_used_due(record, used_at) -> bool:
    """凭证活跃时间最多每 30 分钟持久化一次。"""
    last_used_at = getattr(record, "last_used_at", None)
    if last_used_at is None:
        return True
    return ensure_utc_aware(last_used_at) <= (
        ensure_utc_aware(used_at) - CREDENTIAL_LAST_USED_INTERVAL
    )


def require_api_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer_token()
        used_at = utcnow()
        credential_record = None
        session_record = verify_miniprogram_session(token)
        if session_record is not None:
            credential_record = session_record
            g.mp_session = session_record
            g.api_token = None
            g.api_user_id = session_record.user_id
            g.auth_kind = "miniprogram_session"
        else:
            record = verify_api_token(token)
            if not record:
                return _error("unauthorized", "登录状态无效或已过期。", 401)
            credential_record = record
            g.api_token = record
            g.api_user_id = record.user_id
            g.auth_kind = "api_token"

        is_write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if is_write:
            # 所有写请求与账号注销共用同一行锁，消除清理后的并发残留。
            user_query = (
                db.select(User)
                .where(User.id == g.api_user_id, User.deleted_at.is_(None))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            active_user = db.session.execute(user_query).scalar_one_or_none()
        else:
            # GET/HEAD 复用凭证 JOIN 已验证的 User，避免重复 owner 查询。
            active_user = getattr(credential_record, "_verified_user", None)
        if active_user is None or active_user.deleted_at is not None:
            db.session.rollback()
            return _error("unauthorized", "账号已失效或已注销。", 401)
        if (
            is_write
            and db.engine.dialect.name == "sqlite"
        ):
            # SQLite 忽略 SELECT FOR UPDATE；条件 no-op UPDATE 会取得写锁并复核墓碑。
            lock_result = db.session.execute(
                db.update(User)
                .where(User.id == g.api_user_id, User.deleted_at.is_(None))
                .values(last_login=User.last_login)
            )
            if lock_result.rowcount != 1:
                db.session.rollback()
                return _error("unauthorized", "账号已失效或已注销。", 401)
        g.api_user = active_user
        if (
            g.auth_kind == "api_token"
            and g.api_token.privacy_consent_version != current_privacy_version()
        ):
            return _error(
                "privacy_consent_required",
                "隐私说明已更新，请在网页端重新生成绑定 Token。",
                428,
                data={"required_privacy_consent_version": current_privacy_version()},
            )
        if (
            g.auth_kind == "api_token"
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not api_token_has_scope(g.api_token, "miniprogram:write")
        ):
            # 兼容 Token 的所有写请求统一收口，敏感接口还会继续校验 sensitive scope。
            return _error(
                "insufficient_scope",
                "该 Token 没有写入权限，请重新生成。",
                403,
            )
        g.api_scope_denied = False
        if is_write:
            # 先取得 User 锁，再将 credential 标记为脏，保持固定锁序。
            if _credential_last_used_due(credential_record, used_at):
                credential_record.last_used_at = used_at
            return fn(*args, **kwargs)

        response = fn(*args, **kwargs)
        if (
            not getattr(g, "api_scope_denied", False)
            and _credential_last_used_due(credential_record, used_at)
        ):
            # GET/HEAD 完成后才开启短写事务，避免整个请求持有 credential 写锁。
            try:
                credential_record.last_used_at = used_at
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.warning("小程序凭证使用时间写入失败", exc_info=True)
        return response

    return wrapper


def require_api_scope(scope):
    """微信会话默认具备小程序能力；兼容 Token 必须显式带 scope。"""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if getattr(g, "auth_kind", None) == "miniprogram_session":
                return fn(*args, **kwargs)
            if api_token_has_scope(getattr(g, "api_token", None), scope):
                return fn(*args, **kwargs)
            g.api_scope_denied = True
            return _error("insufficient_scope", "该 Token 没有此功能权限，请重新生成。", 403)

        return wrapper

    return decorator


def require_health_sensitive_consent(fn):
    """在认证和 sensitive scope 之后阻断未授权的私密健康访问。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        required_version = current_privacy_version()
        if not _health_consent_is_current(
            getattr(g, "api_user", None),
            required_version,
        ):
            return _error(
                "health_sensitive_consent_required",
                "请先阅读并同意健康敏感信息处理说明。",
                428,
                data={
                    "required_health_consent_version": required_version,
                },
            )
        return fn(*args, **kwargs)

    return wrapper


def _reauthorize_locked_api_user(*, require_health_consent=False):
    """在 owner 文件锁内重新验证凭证、账号墓碑和写权限。"""
    expected_user_id = int(g.api_user_id)
    expected_kind = str(g.auth_kind)
    token = _bearer_token()
    used_at = utcnow()
    if expected_kind == "miniprogram_session":
        credential = verify_miniprogram_session(token)
        if credential is None or int(credential.user_id) != expected_user_id:
            db.session.rollback()
            return _error("unauthorized", "登录状态无效或已过期。", 401)
        g.mp_session = credential
        g.api_token = None
    else:
        credential = verify_api_token(token)
        if credential is None or int(credential.user_id) != expected_user_id:
            db.session.rollback()
            return _error("unauthorized", "登录状态无效或已过期。", 401)
        g.api_token = credential
        if credential.privacy_consent_version != current_privacy_version():
            db.session.rollback()
            return _error(
                "privacy_consent_required",
                "隐私说明已更新，请在网页端重新生成绑定 Token。",
                428,
                data={"required_privacy_consent_version": current_privacy_version()},
            )
        if not api_token_has_scope(credential, "miniprogram:write"):
            db.session.rollback()
            return _error("insufficient_scope", "该 Token 没有写入权限，请重新生成。", 403)
        if (
            require_health_consent
            and not api_token_has_scope(credential, "miniprogram:sensitive")
        ):
            db.session.rollback()
            return _error("insufficient_scope", "该 Token 没有此功能权限，请重新生成。", 403)

    user = db.session.execute(
        db.select(User)
        .where(User.id == expected_user_id, User.deleted_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if user is None:
        db.session.rollback()
        return _error("unauthorized", "账号已失效或已注销。", 401)
    if db.engine.dialect.name == "sqlite":
        changed = db.session.execute(
            db.update(User)
            .where(User.id == expected_user_id, User.deleted_at.is_(None))
            .values(last_login=User.last_login)
        ).rowcount
        if changed != 1:
            db.session.rollback()
            return _error("unauthorized", "账号已失效或已注销。", 401)
    if require_health_consent and not _health_consent_is_current(user):
        db.session.rollback()
        return _error(
            "health_sensitive_consent_required",
            "请先阅读并同意健康敏感信息处理说明。",
            428,
            data={
                "required_health_consent_version": current_privacy_version(),
            },
        )
    if _credential_last_used_due(credential, used_at):
        credential.last_used_at = used_at
    g.api_user = user
    return None


def _pair_for_user(pair_id: int):
    q = Pair.query.filter_by(id=pair_id, status="active")
    # admin token is not supported in pilot; restrict to owner
    q = q.filter_by(caregiver_id=g.api_user_id)
    return q.first()


def _resolve_owned_member(*, payload=None, required=False):
    payload = payload or {}
    pair_raw = payload.get("pair_id", request.args.get("pair_id"))
    member_raw = payload.get("member_id", request.args.get("member_id"))
    pair = None
    member = None
    if pair_raw not in (None, ""):
        pair_id = _int_value(pair_raw, minimum=1, field="pair_id", allow_none=False)
        pair = _pair_for_user(pair_id)
        if pair is None:
            raise LookupError("pair_not_found")
        if not pair.member_id:
            raise LookupError("pair_member_not_found")
        member = FamilyMember.query.filter_by(
            id=pair.member_id,
            user_id=g.api_user_id,
        ).first()
        if member is None:
            raise LookupError("member_not_found")
    if member_raw not in (None, ""):
        member_id = _int_value(member_raw, minimum=1, field="member_id", allow_none=False)
        direct_member = FamilyMember.query.filter_by(id=member_id, user_id=g.api_user_id).first()
        if direct_member is None:
            raise LookupError("member_not_found")
        if member is not None and member.id != direct_member.id:
            raise ValueError("pair_member_mismatch")
        member = direct_member
    if required and member is None:
        raise ValueError("member_required")
    return pair, member


def _adult_profile_incomplete(member) -> bool:
    """历史档案继续可见，但健康功能只接受 18 至 120 岁成年人。"""
    if member is None or isinstance(getattr(member, "age", None), bool):
        return True
    try:
        age = int(member.age)
    except (TypeError, ValueError):
        return True
    return age < 18 or age > 120


def _adult_age_value(value):
    """年龄只接受整数或纯整数字符串，禁止浮点截断和布尔值混入。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid_age")
    return _int_value(
        value,
        minimum=18,
        maximum=120,
        field="age",
        allow_none=False,
    )


def _adult_profile_required_error(member, *, pair_id=None):
    if not _adult_profile_incomplete(member):
        return None
    data = {
        "required_min_age": 18,
        "required_max_age": 120,
    }
    if pair_id is not None:
        data["pair_id"] = pair_id
    if member is not None:
        data["member_id"] = member.id
    return _error(
        "adult_family_profile_required",
        "请先把家人档案年龄补充为 18 至 120 岁。",
        409,
        data=data,
    )


def _adult_profile_required_for_pair(pair):
    member = None
    if pair is not None and pair.member_id:
        member = FamilyMember.query.filter_by(
            id=pair.member_id,
            user_id=g.api_user_id,
        ).first()
    return _adult_profile_required_error(
        member,
        pair_id=pair.id if pair is not None else None,
    )


def _date_value(value, *, default_today=False):
    if value in (None, ""):
        return today_local() if default_today else None
    if not isinstance(value, str) or len(value) > 10:
        raise ValueError("invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid_date") from exc


def _diary_json(record):
    return {
        "id": record.id,
        "member_id": record.member_id,
        "entry_date": record.entry_date.isoformat() if record.entry_date else None,
        "symptoms": record.symptoms,
        "severity": record.severity,
        "notes": record.notes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _medication_json(record):
    return {
        "id": record.id,
        "member_id": record.member_id,
        "medicine_name": record.medicine_name,
        "dosage": record.dosage,
        "frequency": record.frequency,
        "time_of_day": record.time_of_day,
        "weather_triggers": safe_json_loads(record.weather_triggers, []),
        "is_active": bool(record.is_active),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _assessment_json(record):
    if record is None:
        return None
    return {
        "id": record.id,
        "member_id": record.member_id,
        "assessment_date": record.assessment_date.isoformat() if record.assessment_date else None,
        "risk_score": record.risk_score,
        "risk_level": record.risk_level,
        "disease_risks": safe_json_loads(record.disease_risks, {}),
        "recommendations": safe_json_loads(record.recommendations, []),
        "explain": safe_json_loads(record.explain, {}),
    }


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


def _normalize_acquisition_source(payload) -> str:
    """只接受固定家庭分享来源，所有其他输入统一归入直接访问。"""
    if payload.get("acquisition_source") == ACQUISITION_SOURCE_FAMILY_SHARE:
        return ACQUISITION_SOURCE_FAMILY_SHARE
    return "direct"


@bp.route("/bootstrap", endpoint="bootstrap")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def bootstrap():
    """公共启动数据只读持久化快照，用户请求绝不会触发天气供应商。"""
    return _success(get_bootstrap_payload())


@bp.route("/auth/wechat", methods=["POST"], endpoint="wechat_login")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_AUTH", "10 per 5 minutes"), key_func=_mp_rate_limit_key)
def wechat_login():
    try:
        payload = _json_payload()
        code = _strict_text(payload, "code", 128, required=True)
        consent = _strict_text(payload, "privacy_consent_version", 64, required=True)
        acquisition_source = _normalize_acquisition_source(payload)
        result = login_with_wechat_code(code, consent, acquisition_source)
    except ValueError as exc:
        return _error(str(exc), "登录请求格式不正确。", 400)
    except MiniProgramAuthError as exc:
        data = None
        if exc.code == "privacy_consent_required":
            data = {"required_privacy_consent_version": current_privacy_version()}
        return _error(exc.code, exc.message, exc.status_code, data=data)
    user_data = result.get("user") if isinstance(result, dict) else None
    log_usage_event(
        "wechat_login_success",
        user_id=(user_data or {}).get("id"),
        source="miniprogram",
        meta={"from": acquisition_source},
    )
    return _success(result)


@bp.route("/auth/logout", methods=["POST"], endpoint="wechat_logout")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def wechat_logout():
    if getattr(g, "auth_kind", None) != "miniprogram_session":
        return _error("miniprogram_session_required", "该操作仅支持微信小程序会话。", 403)
    session_record = g.mp_session
    session_record.revoked_at = utcnow()
    db.session.commit()
    return _success({"revoked": True})


@bp.route("/public/communities", endpoint="public_communities")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def public_communities():
    return _success(public_communities_payload())


@bp.route("/public/cooling-resources", endpoint="public_cooling_resources")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def public_cooling_resources():
    return _success(public_cooling_resources_payload())


@bp.route("/public/gis-metadata", endpoint="public_gis_metadata")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def public_gis_metadata():
    try:
        data = public_gis_metadata_payload()
    except (OSError, ValueError, json.JSONDecodeError):
        current_app.logger.exception("小程序 GIS 元数据读取失败")
        data = {"available": False, "scope": CANONICAL_LOCATION_NAME}
    return _success(data)


@bp.route("/public/community", endpoint="public_community_bundle")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_PUBLIC", "120 per minute"), key_func=_mp_rate_limit_key)
def public_community_bundle():
    """公共端首屏的聚合兼容接口。"""
    communities = public_communities_payload()
    cooling = public_cooling_resources_payload()
    try:
        gis = public_gis_metadata_payload()
    except (OSError, ValueError, json.JSONDecodeError):
        gis = {"available": False, "scope": CANONICAL_LOCATION_NAME}
    return _success(
        {
            "communities": communities["items"],
            "summary": communities["summary"],
            "cooling": cooling["items"],
            "gis": gis,
            "source": "server_aggregated_deidentified",
        }
    )


@bp.route("/me", endpoint="me")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def me():
    user = getattr(g, "api_user", None)
    if not user:
        return jsonify({"success": False, "error": "user_not_found"}), 404
    wxpusher_feature_enabled = _wxpusher_feature_enabled()
    required_wxpusher_version = current_privacy_version()
    data = {
        "id": user.id,
        "display_name": "微信用户" if getattr(g, "auth_kind", None) == "miniprogram_session" else user.username,
        "wxpusher_uid": user.wxpusher_uid if wxpusher_feature_enabled else None,
        "push_enabled": bool(user.push_enabled) if wxpusher_feature_enabled else False,
        "wxpusher_feature_enabled": wxpusher_feature_enabled,
        "wxpusher_available": _wxpusher_available(),
        "required_wxpusher_consent_version": required_wxpusher_version,
        "wxpusher_reconsent_required": bool(
            wxpusher_feature_enabled
            and user.push_enabled
            and not _wxpusher_consent_is_current(user, required_wxpusher_version)
        ),
        **_health_consent_payload(user),
    }
    # 旧 API token 客户端继续使用 username；微信会话不暴露内部哈希前缀账号名。
    if getattr(g, "auth_kind", None) == "api_token":
        data["username"] = user.username
    return _success(data)


@bp.route("/health-consent", methods=["GET"], endpoint="health_consent_get")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def health_consent_get():
    """读取独立健康敏感信息同意状态，不返回任何健康内容。"""
    return _success(_health_consent_payload(g.api_user))


@bp.route("/health-consent", methods=["POST"], endpoint="health_consent_post")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
def health_consent_post():
    """只接受明确的 true 和当前说明版本，并保存 UTC 回执。"""
    required_version = current_privacy_version()
    try:
        payload = _json_payload()
        if payload.get("consent") is not True:
            return _error(
                "health_sensitive_consent_required",
                "必须明确同意后才能使用健康功能。",
                400,
                data={
                    "required_health_consent_version": required_version,
                },
            )
        submitted_version = _strict_text(
            payload,
            "health_consent_version",
            64,
            required=True,
        )
    except ValueError as exc:
        return _error(str(exc), "健康敏感信息同意请求无效。", 400)

    if submitted_version != required_version:
        return _error(
            "health_consent_version_mismatch",
            "健康敏感信息处理说明已更新，请重新阅读并确认。",
            400,
            data={
                "required_health_consent_version": required_version,
            },
        )

    try:
        user = g.api_user
        user.health_sensitive_consent_version = required_version
        user.health_sensitive_consented_at = utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("健康敏感信息同意回执保存失败")
        return _error(
            "health_consent_write_failed",
            "健康敏感信息同意暂时无法保存，请稍后重试。",
            503,
        )
    return _success(_health_consent_payload(user))


@bp.route("/health-consent", methods=["DELETE"], endpoint="health_consent_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
def health_consent_delete():
    """撤回只清空回执并停止后续访问，已有私密资料继续保留。"""
    try:
        user = g.api_user
        user.health_sensitive_consent_version = None
        user.health_sensitive_consented_at = None
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("健康敏感信息同意撤回失败")
        return _error(
            "health_consent_withdraw_failed",
            "健康敏感信息同意暂时无法撤回，请稍后重试。",
            503,
        )
    return _success(_health_consent_payload(user))


@bp.route("/me", methods=["PATCH"], endpoint="me_patch")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def me_patch():
    """Update pilot push settings (WxPusher UID + enabled flag)."""
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "invalid_payload"}), 400
    if not _wxpusher_feature_enabled():
        return _error(
            "wxpusher_disabled",
            "首发版本暂未开放第三方推送设置。",
            403,
        )

    updated_fields = []
    requested_wx_uid = None
    requested_push_enabled = None
    wxpusher_consent = False
    wxpusher_consent_version = None

    if "wxpusher_uid" in payload:
        requested_wx_uid = sanitize_input(payload.get("wxpusher_uid"), max_length=80)
        requested_wx_uid = (
            requested_wx_uid.strip()
            if isinstance(requested_wx_uid, str)
            else None
        ) or None
        updated_fields.append("wxpusher_uid")

    if "push_enabled" in payload:
        try:
            requested_push_enabled = _parse_strict_bool(payload.get("push_enabled"))
        except ValueError:
            return jsonify({"success": False, "error": "invalid_push_enabled"}), 400
        updated_fields.append("push_enabled")

    if "wxpusher_consent" in payload:
        try:
            wxpusher_consent = _parse_strict_bool(payload.get("wxpusher_consent"))
        except ValueError:
            return jsonify({"success": False, "error": "invalid_wxpusher_consent"}), 400

    if "wxpusher_consent_version" in payload:
        try:
            wxpusher_consent_version = _strict_text(
                payload,
                "wxpusher_consent_version",
                64,
            )
        except ValueError as exc:
            return _error(str(exc), "第三方推送同意版本无效。", 400)

    wxpusher_available = _wxpusher_available()
    required_wxpusher_version = current_privacy_version()
    owner_user_id = int(g.api_user_id)
    db.session.rollback()
    try:
        with push_owner_lock(owner_user_id):
            authorization_error = _reauthorize_locked_api_user()
            if authorization_error is not None:
                return authorization_error
            user = g.api_user
            wx_uid = (
                requested_wx_uid
                if "wxpusher_uid" in payload
                else user.wxpusher_uid
            )
            push_enabled = (
                requested_push_enabled
                if "push_enabled" in payload
                else bool(user.push_enabled)
            )

            # UID 被移除时必须关闭推送，避免保留无法投递的开启状态。
            if not wx_uid:
                push_enabled = False
                if "wxpusher_uid" in updated_fields and "push_enabled" not in updated_fields:
                    updated_fields.append("push_enabled")
            if push_enabled and not wxpusher_available:
                db.session.rollback()
                return _error("wxpusher_unavailable", "第三方推送服务暂不可用。", 503)
            consent_refresh_required = bool(
                push_enabled
                and (
                    not bool(user.push_enabled)
                    or not _wxpusher_consent_is_current(
                        user,
                        required_wxpusher_version,
                    )
                )
            )
            if consent_refresh_required and not wxpusher_consent:
                db.session.rollback()
                return jsonify({"success": False, "error": "wxpusher_consent_required"}), 400
            if (
                consent_refresh_required
                and wxpusher_consent_version != required_wxpusher_version
            ):
                db.session.rollback()
                return _error(
                    "wxpusher_consent_version_mismatch",
                    "推送传输说明已更新，请重新阅读并确认。",
                    400,
                    data={
                        "required_wxpusher_consent_version": required_wxpusher_version,
                    },
                )

            if updated_fields:
                user.wxpusher_uid = wx_uid
                user.push_enabled = bool(push_enabled)
            if consent_refresh_required:
                user.wxpusher_consent_version = required_wxpusher_version
                user.wxpusher_consented_at = utcnow()
            db.session.commit()
            response_data = {
                "wxpusher_uid": wx_uid,
                "push_enabled": bool(push_enabled),
                "wxpusher_available": wxpusher_available,
                "required_wxpusher_consent_version": required_wxpusher_version,
                "wxpusher_reconsent_required": bool(
                    push_enabled
                    and not _wxpusher_consent_is_current(
                        user,
                        required_wxpusher_version,
                    )
                ),
            }
    except (OSError, RuntimeError, ValueError):
        db.session.rollback()
        current_app.logger.exception("小程序推送授权锁不可用")
        return _error("settings_update_unavailable", "设置暂时无法保存，请稍后重试。", 503)

    if updated_fields:
        log_usage_event(
            "settings_updated",
            user_id=owner_user_id,
            source="miniprogram",
            meta={"fields": updated_fields},
        )
    return jsonify(
        {
            "success": True,
            "data": response_data,
        }
    )


def _anonymize_miniprogram_owner(user):
    """在 owner 文件锁与数据库写事务内清理账号数据。"""
    user_id = int(user.id)
    user.deleted_at = utcnow()
    db.session.flush()

    pair_rows = (
        db.session.query(Pair.id, Pair.community_code)
        .filter_by(caregiver_id=user_id)
        .all()
    )
    pair_ids = [row[0] for row in pair_rows]
    affected_community_codes = {row[1] for row in pair_rows if row[1]}
    member_ids = [row[0] for row in db.session.query(FamilyMember.id).filter_by(user_id=user_id).all()]

    # 先冻结即将删除的资源主键，删除后仍能按资源类型和主键精确匿名化历史审计。
    pair_link_predicates = [PairLink.caregiver_id == user_id]
    alert_delivery_predicates = [AlertDelivery.user_id == user_id]
    if pair_ids:
        pair_link_predicates.append(PairLink.pair_id.in_(pair_ids))
        alert_delivery_predicates.append(AlertDelivery.pair_id.in_(pair_ids))
    pair_link_ids = [
        row[0]
        for row in db.session.query(PairLink.id)
        .filter(db.or_(*pair_link_predicates))
        .all()
    ]
    alert_delivery_ids = [
        row[0]
        for row in db.session.query(AlertDelivery.id)
        .filter(db.or_(*alert_delivery_predicates))
        .all()
    ]

    # pair_id 可为空，复盘的注销边界必须依据独立 owner 字段。
    Debrief.query.filter_by(owner_user_id=user_id).delete(synchronize_session=False)
    if pair_ids:
        PairActionToken.query.filter(PairActionToken.pair_id.in_(pair_ids)).delete(synchronize_session=False)
        DailyStatus.query.filter(DailyStatus.pair_id.in_(pair_ids)).delete(synchronize_session=False)
        AlertDelivery.query.filter(AlertDelivery.pair_id.in_(pair_ids)).delete(synchronize_session=False)
        PairLink.query.filter(PairLink.pair_id.in_(pair_ids)).delete(synchronize_session=False)
        UsageEvent.query.filter(UsageEvent.pair_id.in_(pair_ids)).delete(synchronize_session=False)
    PairLink.query.filter_by(caregiver_id=user_id).delete(synchronize_session=False)
    Pair.query.filter_by(caregiver_id=user_id).delete(synchronize_session=False)

    if member_ids:
        FamilyMemberProfile.query.filter(FamilyMemberProfile.member_id.in_(member_ids)).delete(
            synchronize_session=False
        )
    HealthDiary.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    MedicationReminder.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    HealthRiskAssessment.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    AlertDelivery.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    UsageEvent.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    FamilyMember.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    ApiToken.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    MiniProgramSession.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    MiniProgramIdentity.query.filter_by(user_id=user_id).delete(synchronize_session=False)

    # 历史审计可能来自旧版本或曾经启用的环境。注销时同时去除账号、网络与资源关联，
    # 只保留动作类别和发生时间用于无身份故障核对。
    audit_owner_predicates = [
        AuditLog.actor_id == user_id,
        db.and_(
            AuditLog.resource_type == "user",
            AuditLog.resource_id == str(user_id),
        ),
    ]
    if pair_link_ids:
        audit_owner_predicates.append(
            db.and_(
                AuditLog.resource_type == "pair_link",
                AuditLog.resource_id.in_([str(value) for value in pair_link_ids]),
            )
        )
    if alert_delivery_ids:
        audit_owner_predicates.append(
            db.and_(
                AuditLog.resource_type == "alert_delivery",
                AuditLog.resource_id.in_([str(value) for value in alert_delivery_ids]),
            )
        )
    audit_rows = AuditLog.query.filter(db.or_(*audit_owner_predicates))
    audit_rows.update(
        {
            AuditLog.actor_id: None,
            AuditLog.actor_role: "deleted_miniprogram_user",
            AuditLog.resource_type: None,
            AuditLog.resource_id: None,
            AuditLog.extra_data: json.dumps({"account_reference_removed": True}),
            AuditLog.ip_address: None,
            AuditLog.user_agent: None,
            AuditLog.request_id: None,
        },
        synchronize_session=False,
    )

    user.username = f"deleted_mp_{user_id}_{secrets.token_hex(6)}"
    user.email = None
    user.age = None
    user.gender = None
    user.community = None
    user.has_chronic_disease = False
    user.chronic_diseases = None
    user.wxpusher_uid = None
    user.push_enabled = False
    user.wxpusher_consent_version = None
    user.wxpusher_consented_at = None
    user.health_sensitive_consent_version = None
    user.health_sensitive_consented_at = None
    user.role = "user"
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(
        AuditLog(
            actor_id=None,
            actor_role="deleted_miniprogram_user",
            action="miniprogram_account_anonymized",
            resource_type=None,
            resource_id=None,
            extra_data=json.dumps({"owner_data_removed": True}),
            created_at=utcnow(),
        )
    )
    db.session.commit()
    return affected_community_codes


@bp.route("/me", methods=["DELETE"], endpoint="me_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
def me_delete():
    """删除小程序身份和 owner 数据，并保留无身份审计占位用户。"""
    if getattr(g, "auth_kind", None) != "miniprogram_session":
        return _error("miniprogram_session_required", "账号注销仅支持微信小程序登录会话。", 403)
    owner_user_id = int(g.api_user_id)
    try:
        payload = _json_payload()
        requested_user_id = payload.get("user_id")
        if requested_user_id not in (None, ""):
            requested_user_id = _int_value(
                requested_user_id,
                minimum=1,
                field="user_id",
                allow_none=False,
            )
            if requested_user_id != owner_user_id:
                return _error("owner_scope_violation", "不能注销其他用户的账号。", 403)
        if payload.get("confirm", False) is not True:
            return _error("delete_confirmation_required", "请明确确认账号注销。", 400)

        # 装饰器的认证写锁必须先释放，后续统一执行 file lock -> DB。
        db.session.rollback()
        with push_owner_lock(owner_user_id):
            authorization_error = _reauthorize_locked_api_user()
            if authorization_error is not None:
                return authorization_error
            user = g.api_user
            if user.role == "admin":
                db.session.rollback()
                return _error("admin_account_delete_forbidden", "管理员账号不能通过小程序注销。", 403)
            affected_community_codes = _anonymize_miniprogram_owner(user)
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "账号注销请求无效。", 400)
    except (OSError, RuntimeError):
        db.session.rollback()
        current_app.logger.exception("小程序账号注销锁不可用")
        return _error("account_delete_unavailable", "账号注销暂时不可用，请稍后重试。", 503)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("小程序账号注销失败")
        return _error("account_delete_failed", "账号注销失败，请稍后重试。", 503)
    refresh_latest_community_daily_best_effort(
        affected_community_codes,
        event_logger=current_app.logger,
    )
    return _success({"deleted": True, "anonymized": True, "session_revoked": True})


@bp.route("/elders", endpoint="elders_list")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_READ", "120 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def elders_list():
    pairs = Pair.query.filter_by(caregiver_id=g.api_user_id, status="active").order_by(Pair.created_at.desc()).all()
    status_date = today_local()
    pair_ids = [pair.id for pair in pairs]
    statuses = (
        DailyStatus.query.filter(
            DailyStatus.pair_id.in_(pair_ids),
            DailyStatus.status_date == status_date,
        ).all()
        if pair_ids
        else []
    )
    status_map = {status.pair_id: status for status in statuses}
    member_ids = [p.member_id for p in pairs if p.member_id]
    members = (
        FamilyMember.query.filter(
            FamilyMember.id.in_(member_ids),
            FamilyMember.user_id == g.api_user_id,
        ).all()
        if member_ids
        else []
    )
    member_map = {m.id: m for m in members}

    # 一个请求只读取一次县级快照，所有老人共享相同 snapshot_id。
    snapshot = get_bootstrap_payload()
    current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
    weather_available = bool(snapshot.get("available"))
    tmax_value = _finite_or_none(current.get("temperature_max")) if weather_available else None
    tmin_value = _finite_or_none(current.get("temperature_min")) if weather_available else None
    trigger = None
    if tmax_value is not None and tmax_value >= 35:
        trigger = "heat"
    elif tmin_value is not None and tmin_value <= 5:
        trigger = "cold"

    result = []
    for p in pairs:
        member = member_map.get(p.member_id) if p.member_id else None
        status = status_map.get(p.id)
        try:
            actions_done_count = int(status.actions_done_count or 0) if status else 0
        except (TypeError, ValueError):
            actions_done_count = 0
        actions_done_count = min(20, max(0, actions_done_count))
        result.append(
            {
                "pair_id": p.id,
                "snapshot_id": snapshot.get("snapshot_id"),
                "location": snapshot.get("location"),
                "location_query": CANONICAL_LOCATION_NAME,
                "community_code": CANONICAL_LOCATION_NAME,
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
                "adult_profile_incomplete": _adult_profile_incomplete(member),
                "today": {
                    "status_date": status_date.isoformat(),
                    "confirmed_at": (
                        status.confirmed_at.isoformat()
                        if status and status.confirmed_at
                        else None
                    ),
                    "actions_done_count": actions_done_count,
                    "elder_actions": _safe_stored_text_list(
                        status.elder_actions if status else None,
                        max_items=20,
                        item_max_length=50,
                    ),
                    "help_flag": bool(status.help_flag) if status else False,
                    "relay_stage": (
                        status.relay_stage
                        if status and status.relay_stage in RELAY_STAGES
                        else "none"
                    ),
                    "trigger": trigger,
                    "temperature_max": tmax_value if weather_available else None,
                    "temperature_min": tmin_value if weather_available else None,
                    "weather_available": weather_available,
                    "stale": bool(snapshot.get("stale")),
                    "is_mock": bool(current.get("is_mock")),
                },
            }
        )

    return jsonify({"success": True, "data": result})


@bp.route("/elders", methods=["POST"], endpoint="elders_create")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def elders_create():
    try:
        payload = _json_payload()
        name = _strict_text(payload, "name", 50, required=True)
        relation = _strict_text(payload, "relation", 20, default="") or ""
        age = _adult_age_value(payload.get("age"))
        gender = _strict_text(payload, "gender", 10)
        chronic = _list_of_text(
            payload,
            "chronic_diseases",
            max_items=20,
            item_max_length=50,
        )
    except ValueError as exc:
        return _error(str(exc), "老人档案输入不完整或超出长度限制。", 400)

    location_query = CANONICAL_LOCATION_NAME

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
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def elders_patch(pair_id: int):
    pair = _pair_for_user(pair_id)
    if not pair:
        return jsonify({"success": False, "error": "not_found"}), 404

    try:
        payload = _json_payload()
        member = FamilyMember.query.filter_by(id=pair.member_id, user_id=g.api_user_id).first()
        if member:
            candidate_age = payload.get("age") if "age" in payload else member.age
            validated_age = _adult_age_value(candidate_age)
            if "name" in payload:
                member.name = _strict_text(payload, "name", 50, required=True)
            if "relation" in payload:
                member.relation = _strict_text(payload, "relation", 20) or ""
            if "age" in payload:
                member.age = validated_age
            if "gender" in payload:
                member.gender = _strict_text(payload, "gender", 10)
            if "chronic_diseases" in payload:
                chronic = _list_of_text(
                    payload,
                    "chronic_diseases",
                    max_items=20,
                    item_max_length=50,
                )
                member.chronic_diseases = json.dumps(chronic, ensure_ascii=False) if chronic else None
        # 历史记录也强制收敛为都昌县县级天气语义。
        pair.location_query = CANONICAL_LOCATION_NAME
        pair.community_code = CANONICAL_LOCATION_NAME
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "老人档案输入不合法。", 400)
    except Exception:
        db.session.rollback()
        return _error("update_failed", "老人档案更新失败，请稍后重试。", 503)
    log_usage_event(
        "elder_profile_updated",
        user_id=g.api_user_id,
        pair_id=pair.id,
        member_id=pair.member_id,
        source="miniprogram",
        meta={"updated_fields": list(payload.keys())[:20]},
    )
    return _success({"pair_id": pair.id, "updated": True})


@bp.route("/elders/<int:pair_id>", methods=["DELETE"], endpoint="elders_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def elders_delete(pair_id: int):
    owner_user_id = int(g.api_user_id)
    db.session.rollback()
    try:
        with push_owner_lock(owner_user_id):
            authorization_error = _reauthorize_locked_api_user(
                require_health_consent=True,
            )
            if authorization_error is not None:
                return authorization_error
            pair = Pair.query.filter_by(
                id=pair_id,
                caregiver_id=owner_user_id,
                status="active",
            ).first()
            if not pair:
                db.session.rollback()
                return _error("not_found", "老人档案不存在。", 404)
            affected_community_codes = {
                pair.community_code,
                CANONICAL_LOCATION_NAME,
            }
            pair.status = "inactive"
            pair.location_query = CANONICAL_LOCATION_NAME
            pair.community_code = CANONICAL_LOCATION_NAME
            pair.last_active_at = utcnow()
            db.session.commit()
    except (OSError, RuntimeError, ValueError):
        db.session.rollback()
        current_app.logger.exception("小程序老人档案撤权锁不可用")
        return _error("elder_delete_unavailable", "老人档案暂时无法删除，请稍后重试。", 503)
    refresh_latest_community_daily_best_effort(
        affected_community_codes,
        event_logger=current_app.logger,
    )
    return _success({"pair_id": pair_id, "status": "inactive"})


@bp.route("/health/diary", methods=["GET", "POST"], endpoint="health_diary")
@limiter.limit(
    lambda: current_app.config.get(
        "RATE_LIMIT_MP_WRITE" if request.method == "POST" else "RATE_LIMIT_MP_READ",
        "30 per minute" if request.method == "POST" else "120 per minute",
    ),
    key_func=_mp_rate_limit_key,
)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def health_diary():
    if request.method == "GET":
        try:
            _pair, member = _resolve_owned_member()
            limit = _int_value(
                request.args.get("limit", 20),
                minimum=1,
                maximum=MAX_PAGE_SIZE,
                field="limit",
                allow_none=False,
            )
        except (ValueError, LookupError) as exc:
            return _error(str(exc), "查询条件无效或资源不属于当前用户。", 400)
        query = HealthDiary.query.filter_by(user_id=g.api_user_id)
        if member is not None:
            query = query.filter_by(member_id=member.id)
        records = query.order_by(HealthDiary.entry_date.desc(), HealthDiary.id.desc()).limit(limit).all()
        return _success({"items": [_diary_json(record) for record in records]})

    try:
        payload = _json_payload()
        pair, member = _resolve_owned_member(payload=payload)
        if member is not None:
            adult_error = _adult_profile_required_error(
                member,
                pair_id=pair.id if pair is not None else None,
            )
            if adult_error is not None:
                return adult_error
        entry_date = _date_value(payload.get("entry_date"), default_today=True)
        if entry_date > today_local():
            raise ValueError("future_entry_date")
        severity = _strict_text(payload, "severity", 20, required=True)
        if severity not in {"none", "mild", "moderate", "severe", "无", "轻微", "中等", "严重"}:
            raise ValueError("invalid_severity")
        # 服务端边界与小程序表单保持一致，避免绕过客户端提交更长的健康自由文本。
        symptoms = _strict_text(payload, "symptoms", 200, default="") or ""
        notes = _strict_text(payload, "notes", 500, default="") or ""
        if not symptoms and not notes:
            raise ValueError("diary_content_required")
        record = HealthDiary(
            user_id=g.api_user_id,
            member_id=member.id if member else None,
            entry_date=entry_date,
            symptoms=symptoms,
            severity=severity,
            notes=notes,
            created_at=utcnow(),
        )
        db.session.add(record)
        db.session.commit()
    except LookupError as exc:
        db.session.rollback()
        return _error(str(exc), "关联老人不存在或不属于当前用户。", 404)
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "健康日记输入无效或超出长度限制。", 400)
    except Exception:
        db.session.rollback()
        return _error("diary_write_failed", "健康日记保存失败，请稍后重试。", 503)
    return _success({"entry": _diary_json(record)}, 201)


@bp.route("/medications", methods=["GET", "POST", "DELETE"], endpoint="medications")
@limiter.limit(
    lambda: current_app.config.get(
        "RATE_LIMIT_MP_READ" if request.method == "GET" else "RATE_LIMIT_MP_WRITE",
        "120 per minute" if request.method == "GET" else "30 per minute",
    ),
    key_func=_mp_rate_limit_key,
)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def medications():
    if request.method == "GET":
        try:
            _pair, member = _resolve_owned_member()
            limit = _int_value(
                request.args.get("limit", 50),
                minimum=1,
                maximum=MAX_PAGE_SIZE,
                field="limit",
                allow_none=False,
            )
        except (ValueError, LookupError) as exc:
            return _error(str(exc), "查询条件无效或资源不属于当前用户。", 400)
        query = MedicationReminder.query.filter_by(user_id=g.api_user_id)
        if member is not None:
            query = query.filter_by(member_id=member.id)
        items = query.order_by(MedicationReminder.id.desc()).limit(limit).all()
        return _success({"items": [_medication_json(item) for item in items]})

    if request.method == "DELETE":
        try:
            payload = _json_payload()
            record_id = _int_value(
                payload.get("id", request.args.get("id")),
                minimum=1,
                field="id",
                allow_none=False,
            )
        except ValueError as exc:
            return _error(str(exc), "缺少有效的用药提醒 ID。", 400)
        record = MedicationReminder.query.filter_by(id=record_id, user_id=g.api_user_id).first()
        if record is None:
            return _error("not_found", "用药提醒不存在。", 404)
        db.session.delete(record)
        db.session.commit()
        return _success({"deleted_id": record_id})

    try:
        payload = _json_payload()
        pair, member = _resolve_owned_member(payload=payload)
        if member is not None:
            adult_error = _adult_profile_required_error(
                member,
                pair_id=pair.id if pair is not None else None,
            )
            if adult_error is not None:
                return adult_error
        medicine_name = _strict_text(payload, "medicine_name", 100, required=True)
        dosage = _strict_text(payload, "dosage", 100, default="") or ""
        frequency = _strict_text(payload, "frequency", 20, default="daily") or "daily"
        if frequency not in {"daily", "weekly", "as_needed"}:
            raise ValueError("invalid_frequency")
        time_of_day = _strict_text(payload, "time_of_day", 10)
        if time_of_day:
            try:
                datetime.strptime(time_of_day, "%H:%M")
            except ValueError as exc:
                raise ValueError("invalid_time_of_day") from exc
        triggers = _weather_triggers(payload)
        record = MedicationReminder(
            user_id=g.api_user_id,
            member_id=member.id if member else None,
            medicine_name=medicine_name,
            dosage=dosage,
            frequency=frequency,
            time_of_day=time_of_day,
            weather_triggers=json.dumps(triggers, ensure_ascii=False),
            is_active=True,
            created_at=utcnow(),
        )
        db.session.add(record)
        db.session.commit()
    except LookupError as exc:
        db.session.rollback()
        return _error(str(exc), "关联老人不存在或不属于当前用户。", 404)
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "用药提醒输入无效或超出长度限制。", 400)
    except Exception:
        db.session.rollback()
        return _error("medication_write_failed", "用药提醒保存失败，请稍后重试。", 503)
    return _success({"medication": _medication_json(record)}, 201)


@bp.route("/medications/<int:record_id>", methods=["DELETE"], endpoint="medication_delete")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def medication_delete(record_id):
    record = MedicationReminder.query.filter_by(id=record_id, user_id=g.api_user_id).first()
    if record is None:
        return _error("not_found", "用药提醒不存在。", 404)
    db.session.delete(record)
    db.session.commit()
    return _success({"deleted_id": record_id})


@bp.route("/health/assessment", methods=["GET", "POST"], endpoint="health_assessment")
@limiter.limit(
    lambda: current_app.config.get(
        "RATE_LIMIT_MP_WRITE" if request.method == "POST" else "RATE_LIMIT_MP_READ",
        "30 per minute" if request.method == "POST" else "120 per minute",
    ),
    key_func=_mp_rate_limit_key,
)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def health_assessment():
    try:
        payload = _json_payload() if request.method == "POST" else {}
        pair, member = _resolve_owned_member(payload=payload)
        if request.method == "POST" and member is not None:
            adult_error = _adult_profile_required_error(
                member,
                pair_id=pair.id if pair is not None else None,
            )
            if adult_error is not None:
                return adult_error
    except LookupError as exc:
        return _error(str(exc), "关联老人不存在或不属于当前用户。", 404)
    except ValueError as exc:
        return _error(str(exc), "评估对象无效。", 400)

    query = HealthRiskAssessment.query.filter_by(user_id=g.api_user_id)
    if member is not None:
        query = query.filter_by(member_id=member.id)
    elif request.method == "GET" and (request.args.get("pair_id") or request.args.get("member_id")):
        query = query.filter(HealthRiskAssessment.member_id.is_(None))
    if request.method == "GET":
        latest = query.order_by(
            HealthRiskAssessment.assessment_date.desc(),
            HealthRiskAssessment.id.desc(),
        ).first()
        return _success({"latest": _assessment_json(latest)})

    allowed = {
        "outdoor_exposure": {"low", "medium", "high"},
        "symptom_level": {"none", "mild", "moderate", "severe"},
        "hydration": {"good", "normal", "poor"},
        "medication_adherence": {"good", "partial", "poor"},
        "sleep_quality": {"good", "fair", "poor"},
    }
    try:
        screening = {}
        for field, choices in allowed.items():
            value = _strict_text(payload, field, 20, required=True)
            if value not in choices:
                raise ValueError(f"invalid_{field}")
            screening[field] = value
        snapshot = get_bootstrap_payload()
        if not snapshot.get("available"):
            return _error("weather_snapshot_unavailable", "天气快照尚未可用，请稍后重试。", 503)
        if snapshot.get("stale"):
            return _error("weather_snapshot_stale", "天气快照正在更新，请稍后重试。", 503)
        current = snapshot.get("current") or {}
        user = db.session.get(User, g.api_user_id)
        profile = {
            "age": member.age if member and member.age is not None else (user.age or 45),
            "gender": member.gender if member else (user.gender or "未知"),
            "community": CANONICAL_LOCATION_NAME,
            "has_chronic_disease": bool(
                (member and member.chronic_diseases) or user.has_chronic_disease
            ),
            "chronic_diseases": safe_json_loads(
                member.chronic_diseases if member else user.chronic_diseases,
                [],
            ),
        }
        from services.health_risk_service import HealthRiskService

        result = HealthRiskService().assess_personal_weather_health_risk(
            profile,
            current,
            screening=screening,
        )
        explain = {
            "snapshot_id": snapshot.get("snapshot_id"),
            "screening": screening,
            "explain": result.get("explain") or {},
            "risk_interval": result.get("risk_interval") or {},
            "model_version": result.get("model_version"),
            "rule_version": result.get("rule_version"),
        }
        record = HealthRiskAssessment(
            user_id=g.api_user_id,
            member_id=member.id if member else None,
            assessment_date=utcnow(),
            weather_condition=compact_assessment_weather_condition(current),
            risk_score=result.get("risk_score"),
            risk_level=result.get("risk_level"),
            disease_risks=json.dumps(result.get("disease_risks") or {}, ensure_ascii=False),
            recommendations=json.dumps(result.get("recommendations") or [], ensure_ascii=False),
            explain=json.dumps(explain, ensure_ascii=False),
        )
        db.session.add(record)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "健康筛查输入不完整或无效。", 400)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("小程序健康评估失败")
        return _error("assessment_failed", "健康评估暂时无法完成，请稍后重试。", 503)
    return _success({"assessment": _assessment_json(record)}, 201)


def _daily_status_for_pair(pair):
    status_date = today_local()

    def snapshot_risk_level():
        snapshot = get_bootstrap_payload()
        current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
        if (
            not snapshot.get("available")
            or snapshot.get("stale")
            or current.get("is_mock")
            or current.get("is_demo")
        ):
            return None
        level = str((snapshot.get("risk") or {}).get("level") or "").strip()
        return level if level and level not in {"未知", "unknown"} else None

    return get_or_create_daily_status(
        pair,
        status_date,
        risk_level_factory=snapshot_risk_level,
    )


@bp.route("/actions/<int:pair_id>/confirm", methods=["POST"], endpoint="action_confirm")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_CONFIRM", "30 per hour"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def action_confirm(pair_id):
    pair = _pair_for_user(pair_id)
    if pair is None or pair.status != "active":
        return _error("not_found", "照护关系不存在。", 404)
    adult_error = _adult_profile_required_for_pair(pair)
    if adult_error is not None:
        return adult_error
    try:
        payload = _json_payload()
        actions_done = _list_of_text(
            payload,
            "actions_done",
            max_items=20,
            item_max_length=50,
        )
        status = _daily_status_for_pair(pair)
        mutation = stage_confirm_action(
            pair,
            status,
            actions_done_count=len(actions_done),
            elder_actions=actions_done,
            source="miniprogram",
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "行动确认输入无效。", 400)
    except Exception:
        db.session.rollback()
        return _error("confirm_failed", "行动确认保存失败。", 503)
    refresh_community_daily_best_effort(
        mutation.community_code,
        mutation.status_date,
        event_logger=current_app.logger,
    )
    return _success(
        {
            "pair_id": mutation.pair_id,
            "confirmed_at": mutation.confirmed_at.isoformat(),
        }
    )


@bp.route("/actions/<int:pair_id>/help", methods=["POST"], endpoint="action_help")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_HELP", "10 per hour"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def action_help(pair_id):
    pair = _pair_for_user(pair_id)
    if pair is None or pair.status != "active":
        return _error("not_found", "照护关系不存在。", 404)
    adult_error = _adult_profile_required_for_pair(pair)
    if adult_error is not None:
        return adult_error
    try:
        payload = _json_payload()
        note_provided = "note" in payload
        note = (
            _strict_text(payload, "note", 300, default="") or ""
            if note_provided
            else None
        )
        status = _daily_status_for_pair(pair)
        mutation = stage_help_action(
            pair,
            status,
            source="miniprogram",
            note=note,
            note_provided=note_provided,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "求助说明输入无效。", 400)
    except Exception:
        db.session.rollback()
        return _error("help_failed", "求助状态保存失败。", 503)
    refresh_community_daily_best_effort(
        mutation.community_code,
        mutation.status_date,
        event_logger=current_app.logger,
    )
    return _success(
        {
            "pair_id": mutation.pair_id,
            "help_flag": True,
            "relay_stage": mutation.relay_stage,
        }
    )


@bp.route("/actions/<int:pair_id>/debrief", methods=["POST"], endpoint="action_debrief")
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_MP_WRITE", "30 per minute"), key_func=_mp_rate_limit_key)
@require_api_token
@require_api_scope("miniprogram:sensitive")
@require_health_sensitive_consent
def action_debrief(pair_id):
    pair = _pair_for_user(pair_id)
    if pair is None or pair.status != "active":
        return _error("not_found", "照护关系不存在。", 404)
    adult_error = _adult_profile_required_for_pair(pair)
    if adult_error is not None:
        return adult_error
    try:
        payload = _json_payload()
        answers = {
            name: _strict_text(payload, name, 200, default="") or ""
            for name in ("question_1", "question_2", "question_3")
        }
        difficulty = _strict_text(payload, "difficulty", 500, default="") or ""
        optin = payload.get("debrief_optin", True)
        if not isinstance(optin, bool):
            raise ValueError("invalid_debrief_optin")
        status = _daily_status_for_pair(pair)
        mutation = stage_debrief_action(
            pair,
            status,
            answers=answers,
            difficulty=difficulty,
            opt_in=optin,
            source="miniprogram",
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), "复盘输入无效或超出长度限制。", 400)
    except Exception:
        db.session.rollback()
        return _error("debrief_failed", "行动复盘保存失败。", 503)
    refresh_community_daily_best_effort(
        mutation.community_code,
        mutation.status_date,
        event_logger=current_app.logger,
    )
    return _success(
        {
            "debrief_id": mutation.debrief_id,
            "pair_id": mutation.linked_pair_id,
        }
    )


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

    snapshot = get_bootstrap_payload()
    weather_data = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
    weather_available = bool(snapshot.get("available"))
    location = snapshot.get("location") or {}

    return jsonify(
        {
            "success": True,
            "data": {
                "snapshot_id": snapshot.get("snapshot_id"),
                "location": location,
                "warnings": snapshot.get("warnings") or [],
                "weather": {
                    "temperature_max": weather_data.get("temperature_max") if weather_available else None,
                    "temperature_min": weather_data.get("temperature_min") if weather_available else None,
                    "weather_available": weather_available,
                    "stale": bool(snapshot.get("stale")),
                    "is_mock": bool(weather_data.get("is_mock")),
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
    if event_type not in MINIPROGRAM_CLIENT_PILOT_EVENT_TYPES:
        return jsonify({"success": False, "error": "invalid_event_type"}), 400
    raw_meta = payload.get("meta")
    if raw_meta is not None and not isinstance(raw_meta, dict):
        return jsonify({"success": False, "error": "invalid_meta"}), 400
    meta = raw_meta
    if meta is not None:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "invalid_meta"}), 400
        if len(meta_json) > MP_EVENT_META_MAX_CHARS:
            return jsonify({"success": False, "error": "meta_too_large"}), 400

    event = log_usage_event(
        event_type,
        user_id=g.api_user_id,
        source="miniprogram",
        meta=meta,
    )
    if event is None:
        return jsonify({"success": False, "error": "event_write_failed"}), 503
    return _success({"recorded": True, "event_type": event_type})
