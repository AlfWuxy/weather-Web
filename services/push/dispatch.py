# -*- coding: utf-8 -*-
"""Alert dispatch pipeline (pilot).

Strategy:
- Prefer official QWeather warnings (weatheralert v1)
- Otherwise use simple threshold rules (heat/cold)
- Deduplicate per (alert_id, user_id) for successful sends
- Track deliveries and clicks (AlertDelivery + /t/<token>)
"""

from __future__ import annotations

import json
import logging
import secrets
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, has_app_context

from core.db_models import AlertDelivery, FamilyMemberProfile, Pair, User, WeatherAlert
from core.extensions import db
from core.time_utils import utcnow
from core.usage import log_usage_event
from core.weather import get_weather_with_cache, is_qweather_online_weather
from services.location_resolver import resolve_location
from services.warning_service import get_qweather_warnings
from services.push.wxpusher import send as wxpusher_send

logger = logging.getLogger(__name__)


def _cfg(key: str, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def _generate_delivery_token() -> str:
    # 32~43 chars; URL-safe.
    for _ in range(5):
        token = secrets.token_urlsafe(24)
        if not AlertDelivery.query.filter_by(delivery_token=token).first():
            return token
    # Extremely unlikely; fall back to longer token.
    return secrets.token_urlsafe(32)


def _build_tracking_url(delivery_token: str) -> str:
    base = (_cfg("PUBLIC_BASE_URL") or "").strip()
    if not base:
        return ""
    return f"{base.rstrip('/')}/t/{delivery_token}"


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
    """Return (alert_type, alert_level, description) for threshold rules."""
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


def _push_location_label(resolved: Dict[str, Any]) -> str:
    """第三方推送只使用静态公开地点标签，地址解析结果统一降为泛称。"""
    provider = str((resolved or {}).get("provider") or "").strip().lower()
    display_name = str((resolved or {}).get("display_name") or "").strip()
    if provider in {"map", "default"} and display_name:
        return display_name
    return "所在地区"


def _render_push_content(
    display_name: str,
    elder_names: List[str],
    warning: Optional[Dict[str, Any]],
    threshold_desc: Optional[str],
    location_query: str,
) -> Tuple[str, str]:
    # Title (WxPusher summary)
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
    """Main entry: compute + send alerts for all active pairs."""
    if not has_app_context():
        raise RuntimeError("dispatch_alerts must run inside Flask app context")

    now = now or utcnow()
    pairs = Pair.query.filter_by(status="active").all()
    if not pairs:
        return {"pairs": 0, "locations": 0, "alerts": 0, "deliveries": 0, "sent": 0, "failed": 0}

    profile_map = _load_family_member_profile_map(pairs)
    eligible_pairs = [pair for pair in pairs if _pair_allows_family_push(pair, profile_map)]

    caregiver_ids = sorted({p.caregiver_id for p in eligible_pairs if getattr(p, "caregiver_id", None)})
    users = User.query.filter(User.id.in_(caregiver_ids)).all() if caregiver_ids else []
    user_map = {u.id: u for u in users}

    # Resolve and group by location_code (QWeather compatible)
    groups: Dict[str, Dict[str, Any]] = {}
    for pair in eligible_pairs:
        query = (pair.location_query or pair.community_code or "").strip()
        resolved = resolve_location(query)
        if query and resolved.get("provider") == "fallback":
            logger.warning("跳过未成功解析地点的推送分组，pair_id=%s query=%s", getattr(pair, "id", None), query)
            continue
        code = resolved.get("location_code") or ""
        if not code:
            continue
        group = groups.setdefault(code, {"resolved": resolved, "pairs": []})
        group["pairs"].append(pair)

    stats = {"pairs": len(pairs), "locations": len(groups), "alerts": 0, "deliveries": 0, "sent": 0, "failed": 0}

    for location_code, group in groups.items():
        resolved = group.get("resolved") or {}
        display_name = _push_location_label(resolved)

        # Prefer official warnings.
        warnings = get_qweather_warnings(location_code)
        primary_warning = _choose_primary_warning(warnings)

        weather_data, _ = get_weather_with_cache(location_code)
        threshold = _threshold_alert(weather_data)

        if primary_warning:
            alert_type = primary_warning.get("type") or "qweather_warning"
            alert_level = primary_warning.get("level") or primary_warning.get("severity") or ""
            description = primary_warning.get("title") or primary_warning.get("text") or "官方预警"
        elif threshold:
            alert_type, alert_level, description = threshold
        else:
            continue

        # Create / reuse alert record.
        location_key = location_code  # stable for dedupe
        weather_alert = _get_or_create_weather_alert(
            now=now,
            location_key=location_key,
            alert_type=alert_type,
            alert_level=alert_level,
            description=description,
            dedupe_hours=dedupe_hours,
        )
        stats["alerts"] += 1

        # Group pairs by caregiver
        by_user: Dict[int, List[Pair]] = defaultdict(list)
        for p in group.get("pairs") or []:
            if getattr(p, "caregiver_id", None):
                by_user[p.caregiver_id].append(p)

        for user_id, user_pairs in by_user.items():
            user = user_map.get(user_id)
            if not user:
                continue
            if not getattr(user, "push_enabled", False):
                continue
            wx_uid = (getattr(user, "wxpusher_uid", None) or "").strip()
            if not wx_uid:
                continue

            # Dedupe: do not re-send if a successful delivery already exists.
            sent_delivery = AlertDelivery.query.filter_by(
                alert_id=weather_alert.id, user_id=user_id, status="sent"
            ).first()
            if sent_delivery:
                continue

            # Build content
            threshold_desc = threshold[2] if (not primary_warning and threshold) else None
            title, content = _render_push_content(
                display_name=display_name,
                # WxPusher 是第三方渠道，不外发老人姓名或细粒度地址。
                elder_names=[],
                warning=primary_warning,
                threshold_desc=threshold_desc,
                location_query=display_name,
            )

            delivery_token = _generate_delivery_token()
            tracking_url = _build_tracking_url(delivery_token)

            result = wxpusher_send(wx_uid, title=title, content=content, url=tracking_url or None)
            ok = bool(result.get("ok"))
            status = "sent" if ok else "failed"

            delivery = AlertDelivery(
                alert_id=weather_alert.id,
                user_id=user_id,
                pair_id=user_pairs[0].id if user_pairs else None,
                channel="wxpusher",
                status=status,
                error=(None if ok else (result.get("error") or "")) or None,
                delivery_token=delivery_token,
                sent_at=now,
            )
            db.session.add(delivery)
            db.session.commit()

            stats["deliveries"] += 1
            if ok:
                stats["sent"] += 1
                log_usage_event(
                    "push_sent",
                    user_id=user_id,
                    pair_id=delivery.pair_id,
                    member_id=(user_pairs[0].member_id if user_pairs else None),
                    source="cron",
                    meta={
                        "channel": "wxpusher",
                        "alert_id": weather_alert.id,
                        "alert_type": alert_type,
                        "location_code": location_code,
                    },
                )
            else:
                stats["failed"] += 1
                log_usage_event(
                    "push_failed",
                    user_id=user_id,
                    pair_id=delivery.pair_id,
                    member_id=(user_pairs[0].member_id if user_pairs else None),
                    source="cron",
                    meta={
                        "channel": "wxpusher",
                        "alert_id": weather_alert.id,
                        "alert_type": alert_type,
                        "location_code": location_code,
                        "error": result.get("error") or "",
                    },
                )

    return stats
