# -*- coding: utf-8 -*-
"""试点预警推送流水线。

策略：
- 只读取都昌县共享的 MiniProgramSnapshot
- 优先使用快照预警，否则使用高温/低温阈值
- 按 (alert_id, user_id, channel) 对所有投递状态做数据库级单次占位
- 记录送达与点击（AlertDelivery + /t/<token>）
"""

from __future__ import annotations

import json
import logging
import secrets
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError

from core.db_models import AlertDelivery, FamilyMemberProfile, Pair, User, WeatherAlert
from core.extensions import db
from core.time_utils import ensure_utc_aware, utcnow
from core.usage import log_usage_event
from core.weather import is_qweather_online_weather
from services.miniprogram_service import canonical_location, get_bootstrap_payload
from services.push.wxpusher import send as wxpusher_send

logger = logging.getLogger(__name__)

DELIVERY_CHANNEL = "wxpusher"
DELIVERY_CLAIM_TTL = timedelta(minutes=10)
DELIVERY_TERMINAL_STATES = frozenset({"sent", "failed", "uncertain"})
DELIVERY_LOCAL_FAILURES = frozenset({"missing uid", "missing WXPUSHER_APP_TOKEN"})


def _cfg(key: str, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def _generate_delivery_token() -> str:
    # 生成 32 到 43 个字符的 URL 安全令牌。
    for _ in range(5):
        token = secrets.token_urlsafe(24)
        if not AlertDelivery.query.filter_by(delivery_token=token).first():
            return token
    # 极低概率碰撞时改用更长令牌。
    return secrets.token_urlsafe(32)


def _build_tracking_url(delivery_token: str) -> str:
    base = (_cfg("PUBLIC_BASE_URL") or "").strip()
    if not base:
        return ""
    return f"{base.rstrip('/')}/t/{delivery_token}"


def _existing_delivery_decision(delivery: AlertDelivery, now) -> Dict[str, Any]:
    """对已有占位做保守决策，任何不明确结果都禁止自动重发。"""
    state = str(delivery.status or "").strip().lower()
    delivery_id = int(delivery.id)

    if state == "sent":
        db.session.commit()
        return {"action": "skip", "state": "sent", "review_required": False}

    if state == "retry_ready":
        delivery.status = "sending"
        delivery.error = None
        delivery.sent_at = now
        delivery.attempt_count = max(int(delivery.attempt_count or 1), 1) + 1
        db.session.commit()
        return {
            "action": "send",
            "state": "sending",
            "review_required": False,
            "delivery_id": delivery_id,
            "delivery_token": delivery.delivery_token,
            "pair_id": delivery.pair_id,
        }

    if state == "sending":
        claimed_at = ensure_utc_aware(delivery.sent_at) if delivery.sent_at else None
        if claimed_at is not None and claimed_at >= now - DELIVERY_CLAIM_TTL:
            db.session.commit()
            return {
                "action": "skip",
                "state": "sending",
                "review_required": False,
            }

        # 进程可能在供应商已接收后崩溃。租约过期只能转人工协调，不能重发。
        delivery.status = "uncertain"
        delivery.error = "发送占位已过期，供应商是否接收未知，禁止自动重试"
        db.session.commit()
        logger.warning("投递 %s 的发送占位已过期，已转人工确认", delivery_id)
        return {
            "action": "skip",
            "state": "uncertain",
            "review_required": True,
        }

    if state in DELIVERY_TERMINAL_STATES:
        db.session.commit()
        return {
            "action": "skip",
            "state": state,
            "review_required": state != "sent",
        }

    delivery.status = "uncertain"
    delivery.error = delivery.error or "投递状态无法识别，禁止自动重试"
    db.session.commit()
    return {
        "action": "skip",
        "state": "uncertain",
        "review_required": True,
    }


def _claim_delivery(
    *,
    alert_id: int,
    user_id: int,
    pair_id: Optional[int],
    now,
) -> Dict[str, Any]:
    """用短事务占位；返回 send 时数据库写事务已经提交。"""
    existing = AlertDelivery.query.filter_by(
        alert_id=alert_id,
        user_id=user_id,
        channel=DELIVERY_CHANNEL,
    ).first()
    if existing is not None:
        return _existing_delivery_decision(existing, now)

    for _attempt in range(5):
        delivery_token = _generate_delivery_token()
        delivery = AlertDelivery(
            alert_id=alert_id,
            user_id=user_id,
            pair_id=pair_id,
            channel=DELIVERY_CHANNEL,
            status="sending",
            error=None,
            delivery_token=delivery_token,
            # sending 状态暂借 sent_at 记录占位时间，结束后改为本次结果时间。
            sent_at=now,
            attempt_count=1,
        )
        db.session.add(delivery)
        try:
            db.session.flush()
            delivery_id = int(delivery.id)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # 并发调度会命中三元唯一约束；令牌碰撞则换新令牌再试。
            existing = AlertDelivery.query.filter_by(
                alert_id=alert_id,
                user_id=user_id,
                channel=DELIVERY_CHANNEL,
            ).first()
            if existing is not None:
                return _existing_delivery_decision(existing, now)
            continue
        return {
            "action": "send",
            "state": "sending",
            "review_required": False,
            "delivery_id": delivery_id,
            "delivery_token": delivery_token,
            "pair_id": pair_id,
        }

    raise RuntimeError("无法建立唯一投递占位")


def _finalize_delivery(delivery_id: int, result: Dict[str, Any], now) -> str:
    """外呼结束后用另一个短事务固化结果。"""
    ok = bool(result.get("ok"))
    error = str(result.get("error") or "").strip()
    if ok:
        state = "sent"
        stored_error = None
    elif error in DELIVERY_LOCAL_FAILURES:
        # 本地前置条件失败可以确定没有发出，仍需人工修复并明确重置后才能重试。
        state = "failed"
        stored_error = error[:1000]
    else:
        # 网络超时和供应商错误都可能发生在对方接收之后，统一按不明确处理。
        state = "uncertain"
        stored_error = (error or "供应商未返回可确认的投递结果")[:1000]

    delivery = db.session.get(AlertDelivery, delivery_id)
    if delivery is None:
        db.session.rollback()
        raise RuntimeError(f"投递占位不存在: {delivery_id}")
    delivery.status = state
    delivery.error = stored_error
    delivery.sent_at = now
    db.session.commit()
    return state


def _warning_severity_rank(level: str, severity: str = "") -> int:
    # 官方中文色级优先，其次使用 CAP 严重度。
    level = str(level or "")
    if "红" in level:
        return 4
    if "橙" in level:
        return 3
    if "黄" in level:
        return 2
    if "蓝" in level:
        return 1
    cap_rank = {
        "extreme": 5,
        "severe": 4,
        "moderate": 3,
        "minor": 2,
        "unknown": 0,
    }
    return cap_rank.get(str(severity or "").strip().lower(), 0)


def _choose_primary_warning(warnings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not warnings:
        return None
    return sorted(
        warnings,
        key=lambda w: (
            _warning_severity_rank(w.get("level"), w.get("severity")),
            len(str(w.get("text") or "")),
        ),
        reverse=True,
    )[0]


def _threshold_alert(weather_data: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """返回阈值规则对应的预警类型、级别与说明。"""
    if not is_qweather_online_weather(weather_data):
        return None
    try:
        tmax = weather_data.get("temperature_max")
        tmin = weather_data.get("temperature_min")
        tmax_v = float(tmax) if tmax is not None else None
        tmin_v = float(tmin) if tmin is not None else None
    except Exception:
        tmax_v = None
        tmin_v = None

    if tmax_v is not None and tmax_v >= 35:
        return (
            "heat_threshold",
            "阈值",
            f"最高气温预计 ≥ 35°C（当前估计 {tmax_v:.1f}°C）",
        )
    if tmin_v is not None and tmin_v <= 5:
        return (
            "cold_threshold",
            "阈值",
            f"最低气温预计 ≤ 5°C（当前估计 {tmin_v:.1f}°C）",
        )
    return None


def _get_or_create_weather_alert(
    now,
    location_key: str,
    alert_type: str,
    alert_level: str,
    description: str,
    dedupe_hours: int = 6,
) -> WeatherAlert:
    # 查询与写入使用相同长度，避免 v1 长事件名绕过去重。
    alert_type = str(alert_type or "")[:50]
    alert_level = str(alert_level or "")[:20]
    cutoff = now - timedelta(hours=max(int(dedupe_hours), 1))
    recent = WeatherAlert.query.filter(
        WeatherAlert.location == location_key,
        WeatherAlert.alert_type == alert_type,
        WeatherAlert.alert_level == alert_level,
        WeatherAlert.alert_date >= cutoff,
    ).order_by(WeatherAlert.alert_date.desc()).first()
    if recent:
        return recent

    record = WeatherAlert(
        alert_date=now,
        location=location_key,
        alert_type=alert_type,
        alert_level=alert_level,
        description=description,
        affected_communities=json.dumps([location_key], ensure_ascii=False),
        disease_correlation=json.dumps({}, ensure_ascii=False),
    )
    db.session.add(record)
    db.session.flush()
    return record


def _load_family_member_profile_map(pairs: List[Pair]) -> Dict[int, FamilyMemberProfile]:
    """只加载推送授权所需画像，避免读取不必要的成员身份信息。"""
    member_ids = sorted({p.member_id for p in pairs if getattr(p, "member_id", None)})
    if not member_ids:
        return {}
    rows = FamilyMemberProfile.query.filter(
        FamilyMemberProfile.member_id.in_(member_ids)
    ).all()
    return {profile.member_id: profile for profile in rows}


def _pair_allows_family_push(pair: Pair, profile_map: Dict[int, FamilyMemberProfile]) -> bool:
    """成员级开关和隐私级别必须先于第三方推送生效。"""
    member_id = getattr(pair, "member_id", None)
    if not member_id:
        return True
    profile = profile_map.get(member_id)
    if profile is None:
        return True
    if getattr(profile, "alert_enabled", True) is False:
        return False
    privacy_level = str(getattr(profile, "privacy_level", None) or "family").strip().lower()
    return privacy_level == "family"


def _load_dispatch_snapshot(now) -> Optional[Tuple[Dict[str, Any], Dict[str, str]]]:
    """只接收新鲜、可用且属于都昌县的持久化快照。"""
    canonical = canonical_location()
    try:
        payload = get_bootstrap_payload(now=now)
    except Exception:
        logger.exception("读取小程序天气快照失败，本轮推送已关闭")
        return None

    if not isinstance(payload, dict):
        logger.warning("小程序天气快照格式无效，本轮推送已关闭")
        return None
    if (
        not payload.get("snapshot_id")
        or payload.get("available") is not True
        or bool(payload.get("stale", True))
    ):
        logger.info("小程序天气快照缺失、不可用或已陈旧，本轮推送已关闭")
        return None

    location = payload.get("location")
    if not isinstance(location, dict):
        logger.warning("小程序天气快照缺少地点范围，本轮推送已关闭")
        return None
    snapshot_name = str(location.get("name") or "").strip()
    snapshot_code = str(location.get("code") or "").strip()
    if snapshot_name != canonical["name"] or snapshot_code != canonical["code"]:
        logger.warning("小程序天气快照不属于当前都昌县范围，本轮推送已关闭")
        return None
    return payload, canonical


def _render_push_content(
    display_name: str,
    elder_names: List[str],
    warning: Optional[Dict[str, Any]],
    threshold_desc: Optional[str],
    location_query: str,
) -> Tuple[str, str]:
    # 生成 WxPusher 摘要标题。
    if warning:
        title = warning.get("title") or "天气预警"
    elif threshold_desc:
        title = "天气提醒"
    else:
        title = "天气提醒"
    title = f"【宜老天气通】{display_name} {title}".strip()

    lines = []
    lines.append("你收到一条面向家中老人的天气行动提醒（请由你转述给家人）。")
    if elder_names:
        names = "、".join([n for n in elder_names if n])
        if names:
            lines.append(f"关联老人：{names}")
    if location_query and location_query != display_name:
        lines.append(f"地点：{location_query}")
    else:
        lines.append(f"地点：{display_name}")

    if warning:
        level = warning.get("level") or warning.get("severity") or ""
        wtype = warning.get("type") or ""
        if level or wtype:
            lines.append(f"官方预警：{wtype}{level}".strip())
        text = (warning.get("text") or "").strip()
        if text:
            lines.append(text[:220] + ("…" if len(text) > 220 else ""))
        lines.append("数据来源：和风天气（QWeather）；预警可能延迟或过期，请以官方最新发布为准。")
    elif threshold_desc:
        lines.append(f"阈值触发：{threshold_desc}")

    lines.append("提示：本工具不提供医疗诊断；如出现明显不适请及时就医或联系当地卫生服务。")
    content = "\n".join(lines)
    return title[:80], content


def dispatch_alerts(now=None, dedupe_hours: int = 6) -> Dict[str, Any]:
    """从唯一的都昌县数据库快照计算并发送预警。"""
    if not has_app_context():
        raise RuntimeError("dispatch_alerts must run inside Flask app context")

    fixed_reference_time = now is not None
    reference_time = ensure_utc_aware(now or utcnow())
    pairs = Pair.query.filter_by(status="active").all()
    if not pairs:
        return {
            "status": "idle_no_pairs",
            "pairs": 0,
            "locations": 0,
            "alerts": 0,
            "deliveries": 0,
            "sent": 0,
            "failed": 0,
            "review_required": 0,
        }

    profile_map = _load_family_member_profile_map(pairs)
    eligible_pairs = [pair for pair in pairs if _pair_allows_family_push(pair, profile_map)]

    caregiver_ids = sorted({p.caregiver_id for p in eligible_pairs if getattr(p, "caregiver_id", None)})
    users = User.query.filter(User.id.in_(caregiver_ids)).all() if caregiver_ids else []
    user_map = {u.id: u for u in users}

    stats = {
        "status": "idle_no_recipients",
        "pairs": len(pairs),
        "locations": 1 if eligible_pairs else 0,
        "alerts": 0,
        "deliveries": 0,
        "sent": 0,
        "failed": 0,
        "review_required": 0,
    }
    if not eligible_pairs:
        return stats

    loaded = _load_dispatch_snapshot(reference_time)
    if loaded is None:
        stats["status"] = "snapshot_unavailable"
        return stats
    snapshot, canonical = loaded
    location_code = canonical["code"]
    display_name = canonical["name"]

    raw_warnings = snapshot.get("warnings")
    warnings = (
        [warning for warning in raw_warnings if isinstance(warning, dict)]
        if isinstance(raw_warnings, list)
        else []
    )
    primary_warning = _choose_primary_warning(warnings)
    weather_data = snapshot.get("current")
    threshold = _threshold_alert(weather_data if isinstance(weather_data, dict) else {})

    if primary_warning:
        alert_type = primary_warning.get("type") or "qweather_warning"
        alert_level = primary_warning.get("level") or primary_warning.get("severity") or ""
        description = primary_warning.get("title") or primary_warning.get("text") or "官方预警"
    elif threshold:
        alert_type, alert_level, description = threshold
    else:
        stats["status"] = "idle_no_alert"
        return stats

    weather_alert = _get_or_create_weather_alert(
        now=reference_time,
        location_key=location_code,
        alert_type=alert_type,
        alert_level=alert_level,
        description=description,
        dedupe_hours=dedupe_hours,
    )
    weather_alert_id = int(weather_alert.id)
    # 新预警必须先独立提交，后续 10 秒外呼期间不得持有 SQLite 写锁。
    db.session.commit()
    stats["alerts"] += 1

    # 所有授权家庭共享都昌县县级快照，避免按地址产生天气调用扇出。
    by_user: Dict[int, List[Pair]] = defaultdict(list)
    for pair in eligible_pairs:
        if getattr(pair, "caregiver_id", None):
            by_user[pair.caregiver_id].append(pair)

    for user_id, user_pairs in by_user.items():
        user = user_map.get(user_id)
        if not user:
            continue
        if not getattr(user, "push_enabled", False):
            continue
        wx_uid = (getattr(user, "wxpusher_uid", None) or "").strip()
        if not wx_uid:
            continue

        threshold_desc = threshold[2] if (not primary_warning and threshold) else None
        title, content = _render_push_content(
            display_name=display_name,
            # WxPusher 是第三方渠道，不外发老人姓名或细粒度地址。
            elder_names=[],
            warning=primary_warning,
            threshold_desc=threshold_desc,
            location_query=display_name,
        )

        pair_id = user_pairs[0].id if user_pairs else None
        member_id = user_pairs[0].member_id if user_pairs else None
        claim_time = reference_time if fixed_reference_time else ensure_utc_aware(utcnow())
        claim = _claim_delivery(
            alert_id=weather_alert_id,
            user_id=user_id,
            pair_id=pair_id,
            now=claim_time,
        )
        if claim["action"] != "send":
            if claim.get("review_required"):
                # 保持定时任务非零退出，直到人工确认该次投递是否到达。
                stats["failed"] += 1
                stats["review_required"] += 1
            continue

        delivery_id = int(claim["delivery_id"])
        delivery_token = str(claim["delivery_token"])
        tracking_url = _build_tracking_url(delivery_token)

        try:
            result = wxpusher_send(
                wx_uid,
                title=title,
                content=content,
                url=tracking_url or None,
            )
        except Exception as exc:  # pragma: no cover - 正式客户端已自行收敛异常
            logger.exception("WxPusher 外呼异常，投递结果按 uncertain 处理")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            result = {"ok": False, "error": "供应商返回格式无效"}

        completion_time = reference_time if fixed_reference_time else ensure_utc_aware(utcnow())
        status = _finalize_delivery(delivery_id, result, completion_time)
        ok = status == "sent"

        stats["deliveries"] += 1
        if ok:
            stats["sent"] += 1
            log_usage_event(
                "push_sent",
                user_id=user_id,
                pair_id=pair_id,
                member_id=member_id,
                source="cron",
                meta={
                    "channel": DELIVERY_CHANNEL,
                    "alert_id": weather_alert_id,
                    "alert_type": alert_type,
                    "location_code": location_code,
                },
            )
        else:
            stats["failed"] += 1
            if status == "uncertain":
                stats["review_required"] += 1
            log_usage_event(
                "push_failed",
                user_id=user_id,
                pair_id=pair_id,
                member_id=member_id,
                source="cron",
                meta={
                    "channel": DELIVERY_CHANNEL,
                    "alert_id": weather_alert_id,
                    "alert_type": alert_type,
                    "location_code": location_code,
                    "error": result.get("error") or "",
                },
            )

    stats["status"] = "delivery_failed" if stats["failed"] else "completed"
    return stats
