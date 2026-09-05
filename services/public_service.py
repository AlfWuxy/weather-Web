# -*- coding: utf-8 -*-
"""Public-facing business logic extracted from blueprints."""
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from core.constants import DEFAULT_CITY_LABEL, GUEST_ID_PREFIX
from core.extensions import db
from core.security import hash_identifier, hash_pair_token, hash_short_code, rate_limit_key, verify_pair_token
from core.time_utils import today_local, utcnow, ensure_utc_aware
from core.usage import log_usage_event
from core.weather import (
    get_consecutive_hot_days,
    get_weather_with_cache,
    is_heat_action_weather_ready,
    is_live_observational_weather,
    normalize_location_name,
    resolve_weather_city_label,
    weather_source_label,
)
from core.guest import GuestUser, is_guest_user
from core.db_models import (
    AlertDelivery,
    Community,
    CoolingResource,
    DailyStatus,
    Debrief,
    Pair,
    PairActionToken,
    PairLink,
    ShortCodeAttempt,
    User,
    WeatherAlert,
)
from core.notifications import create_notification
from services.action_events import (
    InvalidTransition,
    record_event,
    record_seen,
    today_state,
)
from services.cooling_service import present_cooling_cards
from services.heat_action_service import HeatActionService
from utils.parsers import parse_bool
from utils.audit_log import log_security_event
from utils.database import atomic_transaction
from utils.validators import (
    validate_username,
    validate_password,
    validate_email,
    validate_age,
    validate_gender,
    sanitize_input
)

logger = logging.getLogger(__name__)

HEAT_RISK_LABELS = {
    'low': '低风险',
    'medium': '中风险',
    'high': '高风险',
    'extreme': '极高'
}

PAIR_TOKEN_SESSION_KEY = 'pair_token'

def _heat_risk_weather_is_ready(weather_data):
    """基础温湿热行动允许来源明确且新鲜的和风或 Open-Meteo 实况。"""
    return is_heat_action_weather_ready(weather_data)


def _store_pair_token(token):
    if token:
        session[PAIR_TOKEN_SESSION_KEY] = token


def _get_pair_token():
    return session.get(PAIR_TOKEN_SESSION_KEY)


def _clear_pair_token():
    session.pop(PAIR_TOKEN_SESSION_KEY, None)


def _safe_next_url(next_url):
    if not next_url:
        return None
    if '\r' in next_url or '\n' in next_url:
        return None
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return None
    if not next_url.startswith('/'):
        return None
    if next_url.startswith(("//", "\\\\", "/\\")):
        return None
    return next_url


def _role_landing_endpoint(role):
    """返回正式账号的默认落点。"""
    return {
        'admin': 'admin.admin_dashboard',
        'community': 'user.community_dashboard',
        'caregiver': 'user.pair_management',
        'user': 'user.pair_management',
    }.get(role, 'user.user_dashboard')


def _clear_identity_session_state():
    """清理跨身份继承会造成串写的游客资料与行动授权。"""
    for key in (
        'guest_id',
        'guest_profile',
        'guest_assessment',
        PAIR_TOKEN_SESSION_KEY,
        'pair_session_id',
        'pair_session_code',
    ):
        session.pop(key, None)


def _registration_form_data():
    """仅保留注册失败后可以安全回填的非敏感字段。"""
    limits = {
        'username': 80,
        'email': 120,
        'age': 10,
        'gender': 10,
        'community': 100,
    }
    return {
        field: str(request.form.get(field, '') or '')[:max_length]
        for field, max_length in limits.items()
    }


def _location_suggestions():
    """读取配置中的都昌地点建议，不触碰 Community 主数据。"""
    configured = current_app.config.get('COMMUNITY_COORDS_GCJ') or {}
    return list(configured.keys()) if isinstance(configured, dict) else []


def _render_register(form_data=None):
    return render_template(
        'register.html',
        form_data=form_data or {},
        location_suggestions=_location_suggestions(),
    )


def _short_code_guard_config():
    max_failures = current_app.config.get('SHORT_CODE_FAIL_MAX', 5)
    window_minutes = current_app.config.get('SHORT_CODE_FAIL_WINDOW_MINUTES', 30)
    lock_minutes = current_app.config.get('SHORT_CODE_LOCK_MINUTES', 30)
    return max_failures, window_minutes, lock_minutes


def _normalize_login_identifier(username):
    normalized = (str(username or '')).strip().lower()
    return normalized


def _login_lockout_key(username):
    normalized = _normalize_login_identifier(username)
    if not normalized:
        return None
    return f'login_failures:{normalized}'


def _login_attempt_key_hash(username):
    """按用户名生成登录失败计数键（哈希后落库）。"""
    normalized = _normalize_login_identifier(username)
    if not normalized:
        return None
    return hash_identifier(f"login:{normalized}")


def _get_login_attempt_record(username):
    key_hash = _login_attempt_key_hash(username)
    if not key_hash:
        return None
    attempt = ShortCodeAttempt.query.filter_by(key_hash=key_hash).order_by(ShortCodeAttempt.id.desc()).first()
    if attempt is None:
        attempt = ShortCodeAttempt(key_hash=key_hash, failed_count=0)
        db.session.add(attempt)
    return attempt


def _get_login_lock_state_from_db(username, max_failures, lockout_seconds):
    """Redis 不可用时，使用数据库兜底登录锁定。"""
    attempt = _get_login_attempt_record(username)
    if attempt is None:
        return False, 0

    now = utcnow()
    last_failed_at = ensure_utc_aware(attempt.last_failed_at) if attempt.last_failed_at else None
    locked_until = ensure_utc_aware(attempt.locked_until) if attempt.locked_until else None

    if last_failed_at and (now - last_failed_at > timedelta(seconds=max(lockout_seconds, 1))):
        attempt.failed_count = 0
        attempt.first_failed_at = None
        attempt.last_failed_at = None
        attempt.locked_until = None
        db.session.commit()
        return False, 0

    if locked_until and locked_until > now:
        remaining = max(0, int((locked_until - now).total_seconds()))
        return True, remaining

    if locked_until and locked_until <= now and (attempt.failed_count or 0) >= max_failures:
        attempt.failed_count = 0
        attempt.first_failed_at = None
        attempt.last_failed_at = None
        attempt.locked_until = None
        db.session.commit()
    return False, 0


def _record_login_failure_db(username, max_failures, lockout_seconds):
    attempt = _get_login_attempt_record(username)
    if attempt is None:
        return

    now = utcnow()
    last_failed_at = ensure_utc_aware(attempt.last_failed_at) if attempt.last_failed_at else None
    if last_failed_at and (now - last_failed_at > timedelta(seconds=max(lockout_seconds, 1))):
        attempt.failed_count = 0
        attempt.first_failed_at = None
        attempt.locked_until = None

    attempt.failed_count = int(attempt.failed_count or 0) + 1
    if attempt.first_failed_at is None:
        attempt.first_failed_at = now
    attempt.last_failed_at = now
    if attempt.failed_count >= max_failures:
        attempt.locked_until = now + timedelta(seconds=lockout_seconds)
    db.session.commit()


def _clear_login_failures_db(username):
    attempt = _get_login_attempt_record(username)
    if attempt is None:
        return
    attempt.failed_count = 0
    attempt.first_failed_at = None
    attempt.last_failed_at = None
    attempt.locked_until = None
    db.session.commit()


def _short_code_attempt_key_hash():
    key = rate_limit_key()
    if not key:
        return None
    return hash_identifier(str(key))


def _get_short_code_attempt():
    key_hash = _short_code_attempt_key_hash()
    if not key_hash:
        return None, None
    attempt = ShortCodeAttempt.query.filter_by(key_hash=key_hash).first()
    return attempt, key_hash


def _refresh_short_code_attempt_window(attempt, now, window_minutes):
    if not attempt or not attempt.last_failed_at:
        return False
    # 确保从数据库读取的 datetime 是 UTC aware 的
    last_failed = ensure_utc_aware(attempt.last_failed_at)
    if now - last_failed > timedelta(minutes=window_minutes):
        with atomic_transaction():
            attempt.failed_count = 0
            attempt.first_failed_at = None
            attempt.last_failed_at = None
            attempt.locked_until = None
        return True
    return False


def _short_code_is_locked():
    attempt, _ = _get_short_code_attempt()
    if not attempt:
        return False
    now = utcnow()
    _, window_minutes, _ = _short_code_guard_config()
    _refresh_short_code_attempt_window(attempt, now, window_minutes)
    # 确保从数据库读取的 datetime 是 UTC aware 的
    if attempt.locked_until and ensure_utc_aware(attempt.locked_until) > now:
        return True
    return False


def _record_short_code_failure():
    attempt, key_hash = _get_short_code_attempt()
    if not key_hash:
        return False
    now = utcnow()
    max_failures, window_minutes, lock_minutes = _short_code_guard_config()
    locked = False
    with atomic_transaction():
        if not attempt:
            attempt = ShortCodeAttempt(key_hash=key_hash, failed_count=0, first_failed_at=now)
            db.session.add(attempt)
        # 确保从数据库读取的 datetime 是 UTC aware 的
        if attempt.last_failed_at and now - ensure_utc_aware(attempt.last_failed_at) > timedelta(minutes=window_minutes):
            attempt.failed_count = 0
            attempt.first_failed_at = now
            attempt.locked_until = None
        attempt.failed_count = (attempt.failed_count or 0) + 1
        attempt.last_failed_at = now
        if attempt.failed_count >= max_failures:
            attempt.locked_until = now + timedelta(minutes=lock_minutes)
            locked = True
    return locked


def _clear_short_code_failures():
    attempt, _ = _get_short_code_attempt()
    if attempt:
        with atomic_transaction():
            db.session.delete(attempt)


def _risk_level_value(label):
    return {
        '低风险': 1,
        '中风险': 2,
        '高风险': 3,
        '极高': 4
    }.get(label, 0)


def _action_plan(risk_label):
    if risk_label == '极高':
        return [
            {'id': 'stay_cool', 'title': '留在有降温条件的室内', 'detail': '尽量避免外出，保持室内通风降温。'},
            {'id': 'contact_now', 'title': '立即联系照护人/邻里', 'detail': '提前告知今日风险与行动安排。'},
            {'id': 'cooling_center', 'title': '条件不足时优先去避暑点', 'detail': '优先选择就近、开放的避暑场所。'}
        ]
    if risk_label == '高风险':
        return [
            {'id': 'stay_indoor', 'title': '尽量待在阴凉通风处', 'detail': '避开正午高温时段外出。'},
            {'id': 'hydrate', 'title': '少量多次补水', 'detail': '身边备好水或淡盐饮品。'},
            {'id': 'check_in', 'title': '安排每日确认', 'detail': '与家人/邻里保持联系。'}
        ]
    if risk_label == '中风险':
        return [
            {'id': 'avoid_sun', 'title': '减少连续暴晒', 'detail': '户外活动分段进行。'},
            {'id': 'cooling', 'title': '准备降温物品', 'detail': '风扇、湿毛巾或遮阳物品。'},
            {'id': 'watch_signs', 'title': '关注体感变化', 'detail': '感到不适及时休息。'}
        ]
    return [
        {'id': 'water', 'title': '规律补水', 'detail': '保持日常饮水习惯。'},
        {'id': 'ventilate', 'title': '室内通风', 'detail': '早晚开窗换气。'},
        {'id': 'shade', 'title': '适度遮阳', 'detail': '外出注意遮阳防晒。'}
    ]


def _resolve_pair(short_code, token):
    short_code_hash = hash_short_code(short_code)
    pair = Pair.query.filter_by(short_code_hash=short_code_hash, status='active').first()
    if pair:
        action_token_valid = bool(token) and _validate_pair_action_token(pair, short_code, token)
        if not _pair_short_code_is_valid(pair) and not action_token_valid:
            return None, '短码已过期，请联系照护人重新生成'
        if token and not action_token_valid and not _validate_pair_token_binding(pair, short_code, token):
            return None, '绑定令牌不匹配'
        return pair, None

    link = PairLink.query.filter_by(short_code_hash=short_code_hash, status='active').first()
    if not link:
        return None, '短码无效或已失效'
    # 确保从数据库读取的 datetime 是 UTC aware 的
    if link.expires_at and ensure_utc_aware(link.expires_at) < utcnow():
        with atomic_transaction():
            link.status = 'expired'
        return None, '短码已过期，请联系照护人重新生成'
    # 防止重复赎回
    if link.redeemed_at:
        return None, '短码已被赎回，无法重复使用'
    if not token:
        return None, '需要绑定令牌'
    if not verify_pair_token(token, link.token_hash):
        return None, '绑定令牌不匹配'

    # 查找或创建 Pair 记录
    pair = None
    if hasattr(link, 'pair_id') and link.pair_id:
        pair = Pair.query.filter_by(id=link.pair_id).first()

    with atomic_transaction():
        if not pair:
            elder_code = None
            while not elder_code:
                candidate = secrets.token_urlsafe(8)
                if not Pair.query.filter_by(elder_code=candidate).first():
                    elder_code = candidate
            pair = Pair(
                caregiver_id=link.caregiver_id,
                community_code=link.community_code,
                elder_code=elder_code,
                short_code=link.short_code,
                short_code_hash=link.short_code_hash or short_code_hash,
                short_code_expires_at=_short_code_expires_at(),
                status='active',
                last_active_at=utcnow()
            )
            db.session.add(pair)
            db.session.flush()
            link.pair_id = pair.id

        link.status = 'redeemed'
        if not link.redeemed_at:
            link.redeemed_at = utcnow()
        log_security_event(
            action='short_code_redeemed',
            actor_id=getattr(current_user, 'id', None) if current_user.is_authenticated else None,
            actor_role=getattr(current_user, 'role', None) if current_user.is_authenticated else None,
            resource_type='pair_link',
            resource_id=str(link.id),
            extra_data={
                'pair_id': pair.id if pair else None,
                'short_code_hash': link.short_code_hash or short_code_hash
            }
        )
    return pair, None


def _pair_short_code_is_valid(pair):
    if not pair:
        return False
    expires_at = getattr(pair, 'short_code_expires_at', None)
    if not expires_at:
        return True
    return ensure_utc_aware(expires_at) >= utcnow()


def _short_code_expires_at():
    try:
        days = int(current_app.config.get('SHORT_CODE_TTL_DAYS', 90))
    except (TypeError, ValueError):
        days = 90
    return utcnow() + timedelta(days=max(1, days))


def _validate_pair_action_token(pair, short_code, token):
    token = (token or '').strip()
    short_code = (short_code or '').replace(' ', '').strip()
    if not pair or not token or not short_code:
        return False
    if hash_short_code(short_code) != getattr(pair, 'short_code_hash', None):
        return False
    token_hash = hash_pair_token(token)
    record = PairActionToken.query.filter_by(token_hash=token_hash).order_by(PairActionToken.id.desc()).first()
    if not record:
        return False
    if record.pair_id != pair.id:
        return False
    if record.revoked_at:
        return False
    if ensure_utc_aware(record.expires_at) < utcnow():
        return False
    if not record.used_at:
        record.used_at = utcnow()
    return True


def _get_or_create_daily_status(pair, status_date, risk_label):
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first()
    if not status:
        status = DailyStatus(
            pair_id=pair.id,
            status_date=status_date,
            community_code=pair.community_code,
            risk_level=risk_label
        )
        db.session.add(status)
    elif risk_label and not status.risk_level:
        status.risk_level = risk_label
    return status


def _build_recent_series(pair_id, days=7):
    end_date = today_local()
    start_date = end_date - timedelta(days=days - 1)
    statuses = DailyStatus.query.filter(
        DailyStatus.pair_id == pair_id,
        DailyStatus.status_date >= start_date,
        DailyStatus.status_date <= end_date
    ).all()
    status_map = {item.status_date: item for item in statuses}
    series = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        status = status_map.get(day)
        risk_label = status.risk_level if status else None
        series.append({
            'date': day.strftime('%m-%d'),
            'risk_label': risk_label,
            'risk_value': _risk_level_value(risk_label),
            'confirmed': 1 if status and status.confirmed_at else 0
        })
    return series


def _refresh_community_daily(community_code, status_date):
    from services.user._helpers import _refresh_community_daily as _refresh_community_daily_impl

    return _refresh_community_daily_impl(community_code, status_date)


def _action_channel(token=None):
    if request.path.startswith('/elder-mode'):
        return 'elder_mode'
    if token or (request.view_args or {}).get('token'):
        return 'web_token'
    return 'web_shortcode'


def _wants_json_action():
    if request.is_json:
        return True
    requested = (request.headers.get('X-Requested-With') or '').lower()
    if requested in {'xmlhttprequest', 'fetch'}:
        return True
    accept = (request.headers.get('Accept') or '').lower()
    return 'application/json' in accept and 'text/html' not in accept


def _action_payload():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    return payload


def _ensure_seen_for_write(pair, channel):
    record_seen(pair, channel)


def _json_state(pair):
    return today_state(pair, today_local())


def _notify_help_requested(pair):
    """站内通知 + 可选 WxPusher；推送失败不阻断事件。"""
    action_url = f'/caregiver/pair/{pair.id}'
    try:
        action_url = url_for('user.caregiver_pair_detail', pair_id=pair.id)
    except Exception:
        pass
    try:
        create_notification(
            pair.caregiver_id,
            title='老人需要帮助',
            message='已记录求助，请尽快确认收到。',
            level='warning',
            category='help_requested',
            member_id=getattr(pair, 'member_id', None),
            action_url=action_url,
            meta={'type': 'help_requested', 'pair_id': pair.id},
        )
    except Exception:
        logger.warning('help_requested 站内通知失败', exc_info=True)
        db.session.rollback()

    caregiver = db.session.get(User, pair.caregiver_id) if pair.caregiver_id else None
    if not caregiver or not getattr(caregiver, 'push_enabled', False):
        return
    wx_uid = (getattr(caregiver, 'wxpusher_uid', None) or '').strip()
    if not wx_uid:
        return

    try:
        from services.push.dispatch import _generate_delivery_token
        from services.push.wxpusher import send as wxpusher_send
    except Exception:
        logger.warning('WxPusher 组件不可用', exc_info=True)
        return

    location = pair.location_query or pair.community_code or '未知'
    alert = WeatherAlert.query.filter_by(
        location=location,
        alert_type='help_requested',
    ).order_by(WeatherAlert.id.desc()).first()
    if not alert:
        alert = WeatherAlert(
            alert_date=utcnow(),
            location=location,
            alert_type='help_requested',
            alert_level='求助',
            description='老人端求助推送',
            source='AppThreshold',
            is_official=False,
            starts_at=utcnow(),
            ends_at=None,
        )
        db.session.add(alert)
        db.session.flush()

    delivery_token = _generate_delivery_token()
    status = 'failed'
    error = None
    try:
        result = wxpusher_send(
            wx_uid,
            title='老人需要帮助',
            content='已记录求助，请打开照护详情确认收到。',
            url=None,
        )
        if result.get('ok'):
            status = 'sent'
        else:
            error = result.get('error') or 'wxpusher_failed'
    except Exception as exc:
        error = str(exc) or 'wxpusher_failed'
        logger.info('WxPusher help send failed: %s', exc)

    delivery = AlertDelivery(
        alert_id=alert.id,
        user_id=caregiver.id,
        pair_id=pair.id,
        channel='wxpusher',
        status=status,
        error=error,
        delivery_token=delivery_token,
        sent_at=utcnow(),
    )
    db.session.add(delivery)
    try:
        db.session.commit()
    except Exception:
        logger.warning('求助 AlertDelivery 写入失败', exc_info=True)
        db.session.rollback()


def _build_action_context(pair, status_date):
    location = normalize_location_name(pair.location_query or pair.community_code)
    weather_data, _ = get_weather_with_cache(location)
    resources = CoolingResource.query.filter_by(
        community_code=pair.community_code,
        is_active=True
    ).all()
    if not _heat_risk_weather_is_ready(weather_data):
        status = _get_or_create_daily_status(pair, status_date, None)
        return status, [], resources, None, None, None, []

    heat_service = HeatActionService()
    consecutive_hot_days = get_consecutive_hot_days(
        location,
        today_max=weather_data.get('temperature_max'),
        weather_data=weather_data,
    )
    heat_result = heat_service.calculate_heat_risk(
        weather_data,
        consecutive_hot_days=consecutive_hot_days
    )
    risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
    risk_reasons = heat_service.build_risk_reasons(heat_result)
    status = _get_or_create_daily_status(pair, status_date, risk_label)
    actions = _action_plan(risk_label)
    return status, actions, resources, weather_data, heat_result, risk_label, risk_reasons


def _render_action_page(
    pair,
    status,
    actions,
    resources,
    weather_data,
    heat_result,
    risk_label,
    risk_reasons=None,
    token=None,
    confirm_action=None,
    help_action=None,
    debrief_action=None,
    understood_action=None,
    select_action=None,
    state_action=None,
    focus_debrief=False
):
    channel = _action_channel(token)
    if pair:
        record_seen(pair, channel)
    recent_series = _build_recent_series(pair.id) if pair else []
    state = _json_state(pair) if pair else {}
    return render_template(
        'action_checkin.html',
        stage='respond',
        pair=pair,
        status=status,
        actions=actions,
        resources=resources,
        weather=weather_data,
        heat_result=heat_result,
        risk_label=risk_label,
        risk_reasons=risk_reasons,
        recent_series=recent_series,
        token=token,
        confirm_action=confirm_action,
        help_action=help_action,
        debrief_action=debrief_action,
        understood_action=understood_action,
        select_action=select_action,
        state_action=state_action,
        today_state=state,
        focus_debrief=focus_debrief
    )


def _resolve_action_routes(token=None, confirm_action=None, help_action=None, debrief_action=None):
    routes = {
        'understood_action': url_for('public.action_understood'),
        'select_action': url_for('public.action_select'),
        'state_action': url_for('public.action_state'),
    }
    if token:
        routes['token'] = token
        routes['confirm_action'] = url_for('public.elder_token_checkin', token=token)
        routes['help_action'] = url_for('public.elder_token_help', token=token)
        routes['debrief_action'] = url_for('public.elder_token_debrief', token=token)
        routes['understood_action'] = url_for('public.elder_token_understood', token=token)
        routes['select_action'] = url_for('public.elder_token_select', token=token)
        routes['state_action'] = url_for('public.elder_token_state', token=token)
    if confirm_action:
        routes['confirm_action'] = confirm_action
    if help_action:
        routes['help_action'] = help_action
    if debrief_action:
        routes['debrief_action'] = debrief_action
    return routes


def _handle_action_lookup(token=None, entry_action=None, confirm_action=None, help_action=None, debrief_action=None):
    if token:
        _store_pair_token(token)

    if request.method == 'POST':
        if _short_code_is_locked():
            flash('尝试次数过多，请稍后再试。', 'error')
            return render_template(
                'action_checkin.html',
                stage='lookup',
                short_code=sanitize_input(request.form.get('short_code'), max_length=12) or '',
                entry_action=entry_action
            )

        short_code = sanitize_input(request.form.get('short_code'), max_length=12) or ''
        short_code = short_code.replace(' ', '').strip()
        token = sanitize_input(request.form.get('token'), max_length=200)
        if not token:
            token = _get_pair_token()

        if not short_code:
            flash('请输入短码', 'error')
            return render_template(
                'action_checkin.html',
                stage='lookup',
                short_code=short_code,
                entry_action=entry_action
            )

        pair, error = _resolve_pair(short_code, token)
        if error:
            locked = _record_short_code_failure()
            if locked:
                flash('尝试次数过多，请稍后再试。', 'error')
            else:
                if error in ('需要绑定令牌', '绑定令牌不匹配'):
                    flash('短码或令牌无效，请联系照护人确认。', 'error')
                else:
                    flash(error, 'error')
            return render_template(
                'action_checkin.html',
                stage='lookup',
                short_code=short_code,
                entry_action=entry_action
            )

        session['pair_session_id'] = pair.id
        session['pair_session_code'] = pair.short_code
        pair.last_active_at = utcnow()
        _clear_short_code_failures()
        _clear_pair_token()

        status_date = today_local()
        status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
            pair, status_date
        )
        db.session.commit()
        action_routes = _resolve_action_routes(
            token=token,
            confirm_action=confirm_action,
            help_action=help_action,
            debrief_action=debrief_action
        )
        return _render_action_page(
            pair,
            status,
            actions,
            resources,
            weather_data,
            heat_result,
            risk_label,
            risk_reasons=risk_reasons,
            **action_routes
        )

    short_code = sanitize_input(request.args.get('short_code'), max_length=12)
    if token and short_code:
        pair, error = _resolve_pair(short_code.replace(' ', '').strip(), token)
        if pair and not error:
            session['pair_session_id'] = pair.id
            session['pair_session_code'] = pair.short_code
            pair.last_active_at = utcnow()
            status_date = today_local()
            status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
                pair, status_date
            )
            db.session.commit()
            action_routes = _resolve_action_routes(
                token=token,
                confirm_action=confirm_action,
                help_action=help_action,
                debrief_action=debrief_action
            )
            return _render_action_page(
                pair,
                status,
                actions,
                resources,
                weather_data,
                heat_result,
                risk_label,
                risk_reasons=risk_reasons,
                **action_routes
            )
    return render_template(
        'action_checkin.html',
        stage='lookup',
        short_code=short_code,
        entry_action=entry_action
    )


def _resolve_pair_from_session_or_code(short_code, token=None):
    pair = None
    short_code = (short_code or '').replace(' ', '').strip()
    if token is None:
        route_token = (request.view_args or {}).get('token')
        token = route_token or _get_pair_token()
    session_pair_id = session.get('pair_session_id')
    session_pair_code = session.get('pair_session_code')
    if session_pair_id:
        if not short_code:
            return None
        if session_pair_code and session_pair_code != short_code:
            return None
        pair = Pair.query.filter_by(id=session_pair_id, status='active').first()
        if (
            pair
            and not _pair_short_code_is_valid(pair)
            and not _validate_pair_action_token(pair, short_code, token)
        ):
            return None
    if not pair and short_code:
        short_code_hash = hash_short_code(short_code)
        pair = Pair.query.filter_by(short_code_hash=short_code_hash, status='active').first()
        if (
            pair
            and not _pair_short_code_is_valid(pair)
            and not _validate_pair_action_token(pair, short_code, token)
        ):
            return None
    return pair


def _validate_pair_token_binding(pair, short_code, token):
    """校验 /e/<token>/... 动作与绑定关系。"""
    token = (token or '').strip()
    short_code = (short_code or '').replace(' ', '').strip()
    if not token or not short_code:
        return False
    if _validate_pair_action_token(pair, short_code, token):
        return True
    short_code_hash = hash_short_code(short_code)
    link = PairLink.query.filter_by(short_code_hash=short_code_hash).order_by(PairLink.id.desc()).first()
    if not link:
        return False
    if link.expires_at and ensure_utc_aware(link.expires_at) < utcnow():
        return False
    if not verify_pair_token(token, link.token_hash):
        return False
    if pair and link.pair_id and link.pair_id != pair.id:
        return False
    return True


def _authorize_pair_action_write(pair, short_code, token):
    """授权 confirm/help/debrief 写库：须 lookup session 绑定，或有效 token 绑定。

    禁止冷请求仅凭 short_code 写 confirmed_at / help_flag / debrief。
    """
    if not pair:
        return False
    short_code = (short_code or '').replace(' ', '').strip()
    session_pair_id = session.get('pair_session_id')
    session_pair_code = session.get('pair_session_code')
    # 路径一：lookup 成功写入的 pair_session 与当前 pair、短码一致
    if session_pair_id is not None and session_pair_code:
        try:
            same_pair = int(session_pair_id) == int(pair.id)
        except (TypeError, ValueError):
            same_pair = session_pair_id == pair.id
        pair_code = getattr(pair, 'short_code', None) or ''
        code_ok = (
            session_pair_code == short_code
            or (pair_code and session_pair_code == pair_code)
        )
        if same_pair and code_ok:
            return True
    # 路径二：有效 PairActionToken / PairLink token 绑定
    token = (token or '').strip()
    if token and _validate_pair_token_binding(pair, short_code, token):
        return True
    return False


def _handle_action_confirm(token=None, confirm_action=None, debrief_action=None):
    # 入口：短码失败锁定则拒绝写
    if _short_code_is_locked():
        flash('尝试次数过多，请稍后再试。', 'error')
        return redirect(url_for('public.action_check'))

    short_code = sanitize_input(request.form.get('short_code'), max_length=12) or ''
    short_code = short_code.replace(' ', '').strip()
    token = sanitize_input(request.form.get('token') or token, max_length=200)
    pair = _resolve_pair_from_session_or_code(short_code, token=token)
    if not pair:
        _record_short_code_failure()
        flash('短码无效或已失效', 'error')
        return redirect(url_for('public.action_check'))

    # 须 session 绑定或 token 绑定；禁止仅 short_code 冷写
    if not _authorize_pair_action_write(pair, short_code, token):
        _record_short_code_failure()
        flash('请先校验短码或使用完整链接', 'error')
        return redirect(url_for('public.action_check'))

    status_date = today_local()
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    # 使用首次展示时保存的风险档，避免提交时天气变化导致合法行动被少计。
    displayed_actions = (
        _action_plan(status.risk_level)
        if status.risk_level
        else []
    )
    allowed_action_ids = {
        str(action.get('id'))
        for action in displayed_actions
        if isinstance(action, dict) and action.get('id') is not None
    }
    actions_done = []
    seen_action_ids = set()
    payload = _action_payload()
    submitted_actions = request.form.getlist('actions_done')
    if not submitted_actions:
        raw = payload.get('actions_done') or payload.get('action_id')
        if isinstance(raw, str):
            submitted_actions = [raw]
        elif isinstance(raw, list):
            submitted_actions = raw
    for action_id in submitted_actions:
        action_id = str(action_id).strip()
        if action_id not in allowed_action_ids or action_id in seen_action_ids:
            continue
        actions_done.append(action_id)
        seen_action_ids.add(action_id)
    # 授权通过后清零短码失败计数，再写库
    _clear_short_code_failures()
    channel = _action_channel(token)
    _ensure_seen_for_write(pair, channel)
    try:
        if actions_done:
            for action_key in actions_done:
                record_event(
                    pair,
                    'self_reported',
                    'elder',
                    channel,
                    action_id=action_key,
                )
        else:
            record_event(pair, 'self_reported', 'elder', channel)
    except InvalidTransition as exc:
        db.session.rollback()
        return exc.to_response()
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first() or status
    log_usage_event(
        'checkin_confirmed',
        user_id=pair.caregiver_id,
        pair_id=pair.id,
        member_id=getattr(pair, 'member_id', None),
        source='web',
        meta={'actions_done_count': len(actions_done)},
    )
    _refresh_community_daily(pair.community_code, status_date)
    flash('已记录今日完成情况。', 'success')
    if _wants_json_action():
        return jsonify({'ok': True, 'state': _json_state(pair), 'actions_done_count': len(actions_done)})
    action_routes = _resolve_action_routes(token=token, confirm_action=confirm_action, debrief_action=debrief_action)
    return _render_action_page(
        pair,
        status,
        actions,
        resources,
        weather_data,
        heat_result,
        risk_label,
        risk_reasons=risk_reasons,
        **action_routes
    )


def _handle_action_help(token=None, confirm_action=None, debrief_action=None):
    # 入口：短码失败锁定则拒绝写
    if _short_code_is_locked():
        flash('尝试次数过多，请稍后再试。', 'error')
        return redirect(url_for('public.action_check'))

    short_code = sanitize_input(request.form.get('short_code'), max_length=12) or ''
    short_code = short_code.replace(' ', '').strip()
    token = sanitize_input(request.form.get('token') or token, max_length=200)
    pair = _resolve_pair_from_session_or_code(short_code, token=token)
    if not pair:
        _record_short_code_failure()
        flash('短码无效或已失效', 'error')
        return redirect(url_for('public.action_check'))

    # 须 session 绑定或 token 绑定；禁止仅 short_code 冷写
    if not _authorize_pair_action_write(pair, short_code, token):
        _record_short_code_failure()
        flash('请先校验短码或使用完整链接', 'error')
        return redirect(url_for('public.action_check'))

    status_date = today_local()
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    # 授权通过后清零短码失败计数，再写库
    _clear_short_code_failures()
    channel = _action_channel(token)
    _ensure_seen_for_write(pair, channel)
    try:
        record_event(pair, 'help_requested', 'elder', channel)
    except InvalidTransition as exc:
        db.session.rollback()
        return exc.to_response()
    _notify_help_requested(pair)
    log_usage_event(
        'help_flagged',
        user_id=pair.caregiver_id,
        pair_id=pair.id,
        member_id=getattr(pair, 'member_id', None),
        source='web',
        meta={'relay_stage': 'caregiver'},
    )
    _refresh_community_daily(pair.community_code, status_date)
    flash('已记录，正在通知家属。', 'success')
    if _wants_json_action():
        return jsonify({
            'ok': True,
            'state': _json_state(pair),
            'message': '已记录，正在通知家属；家属确认收到后这里会变绿',
        })
    action_routes = _resolve_action_routes(token=token, confirm_action=confirm_action, debrief_action=debrief_action)
    return _render_action_page(
        pair,
        status,
        actions,
        resources,
        weather_data,
        heat_result,
        risk_label,
        risk_reasons=risk_reasons,
        **action_routes
    )


def _authorize_action_or_redirect(token=None):
    if _short_code_is_locked():
        if _wants_json_action():
            return None, jsonify({'error': 'locked'}), 429
        flash('尝试次数过多，请稍后再试。', 'error')
        return None, redirect(url_for('public.action_check')), None
    payload = _action_payload()
    short_code = sanitize_input(
        request.form.get('short_code') or payload.get('short_code') or request.args.get('short_code'),
        max_length=12,
    ) or ''
    short_code = short_code.replace(' ', '').strip()
    token = sanitize_input(request.form.get('token') or payload.get('token') or token, max_length=200)
    pair = _resolve_pair_from_session_or_code(short_code, token=token)
    if not pair:
        _record_short_code_failure()
        if _wants_json_action() or request.method == 'GET':
            return None, jsonify({'error': 'unauthorized'}), 400
        flash('短码无效或已失效', 'error')
        return None, redirect(url_for('public.action_check')), None
    if not _authorize_pair_action_write(pair, short_code, token):
        _record_short_code_failure()
        if _wants_json_action() or request.method == 'GET':
            return None, jsonify({'error': 'unauthorized'}), 400
        flash('请先校验短码或使用完整链接', 'error')
        return None, redirect(url_for('public.action_check')), None
    _clear_short_code_failures()
    return (pair, token, short_code), None, None


def _handle_action_understood(token=None, confirm_action=None, debrief_action=None):
    authorized, error_response, status_code = _authorize_action_or_redirect(token=token)
    if error_response is not None:
        return error_response if status_code is None else (error_response, status_code)
    pair, token, _short_code = authorized
    channel = _action_channel(token)
    try:
        event = record_event(pair, 'understood', 'elder', channel)
    except InvalidTransition as exc:
        db.session.rollback()
        return exc.to_response()
    _refresh_community_daily(pair.community_code, today_local())
    if _wants_json_action():
        return jsonify({'ok': True, 'event_id': event.id, 'state': _json_state(pair), 'teachback': True})
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, today_local()
    )
    action_routes = _resolve_action_routes(token=token, confirm_action=confirm_action, debrief_action=debrief_action)
    return _render_action_page(
        pair,
        status,
        actions,
        resources,
        weather_data,
        heat_result,
        risk_label,
        risk_reasons=risk_reasons,
        **action_routes
    )


def _handle_action_select(token=None, confirm_action=None, debrief_action=None):
    authorized, error_response, status_code = _authorize_action_or_redirect(token=token)
    if error_response is not None:
        return error_response if status_code is None else (error_response, status_code)
    pair, token, _short_code = authorized
    payload = _action_payload()
    action_id = sanitize_input(
        request.form.get('action_id') or payload.get('action_id'),
        max_length=32,
    ) or 'undecided'
    channel = _action_channel(token)
    try:
        event = record_event(
            pair,
            'action_selected',
            'elder',
            channel,
            action_id=action_id,
            meta={'teachback_action_id': action_id},
        )
    except InvalidTransition as exc:
        db.session.rollback()
        return exc.to_response()
    if _wants_json_action():
        return jsonify({'ok': True, 'event_id': event.id, 'state': _json_state(pair), 'action_id': action_id})
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, today_local()
    )
    action_routes = _resolve_action_routes(token=token, confirm_action=confirm_action, debrief_action=debrief_action)
    return _render_action_page(
        pair,
        status,
        actions,
        resources,
        weather_data,
        heat_result,
        risk_label,
        risk_reasons=risk_reasons,
        **action_routes
    )


def _handle_action_state(token=None):
    authorized, error_response, status_code = _authorize_action_or_redirect(token=token)
    if error_response is not None:
        return error_response if status_code is None else (error_response, status_code)
    pair, _token, _short_code = authorized
    return jsonify({'ok': True, 'state': _json_state(pair)})


def _handle_action_debrief(token=None, confirm_action=None, debrief_action=None, focus_debrief=False):
    # 入口：短码失败锁定则拒绝写
    if _short_code_is_locked():
        flash('尝试次数过多，请稍后再试。', 'error')
        return redirect(url_for('public.action_check'))

    short_code = sanitize_input(request.form.get('short_code'), max_length=12) or ''
    short_code = short_code.replace(' ', '').strip()
    token = sanitize_input(request.form.get('token') or token, max_length=200)
    pair = _resolve_pair_from_session_or_code(short_code, token=token)
    if not pair:
        _record_short_code_failure()
        flash('短码无效或已失效', 'error')
        return redirect(url_for('public.action_check'))

    # 须 session 绑定或 token 绑定；禁止仅 short_code 冷写
    if not _authorize_pair_action_write(pair, short_code, token):
        _record_short_code_failure()
        flash('请先校验短码或使用完整链接', 'error')
        return redirect(url_for('public.action_check'))

    status_date = today_local()
    q1 = sanitize_input(request.form.get('question_1'), max_length=200)
    q2 = sanitize_input(request.form.get('question_2'), max_length=200)
    q3 = sanitize_input(request.form.get('question_3'), max_length=200)
    difficulty = sanitize_input(request.form.get('difficulty'), max_length=500)
    optin = request.form.get('debrief_optin') == '1'

    if optin:
        debrief = Debrief.query.filter_by(pair_id=pair.id, date=status_date).first()
    else:
        debrief = None

    if not debrief:
        debrief = Debrief(
            date=status_date,
            community_code=pair.community_code,
            pair_id=pair.id if optin else None
        )
        db.session.add(debrief)

    debrief.question_1 = q1
    debrief.question_2 = q2
    debrief.question_3 = q3
    debrief.difficulty = difficulty

    status = _get_or_create_daily_status(pair, status_date, None)
    status.debrief_optin = optin
    # 授权通过后清零短码失败计数，再写库
    _clear_short_code_failures()
    db.session.commit()
    log_usage_event(
        'feedback_submitted',
        user_id=pair.caregiver_id,
        pair_id=pair.id,
        member_id=getattr(pair, 'member_id', None),
        source='web',
        meta={'optin': bool(optin), 'difficulty_len': len(difficulty or '')},
    )
    _refresh_community_daily(pair.community_code, status_date)
    flash('复盘已提交，感谢反馈。', 'success')

    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    action_routes = _resolve_action_routes(token=token, confirm_action=confirm_action, debrief_action=debrief_action)
    return _render_action_page(
        pair,
        status,
        actions,
        resources,
        weather_data,
        heat_result,
        risk_label,
        risk_reasons=risk_reasons,
        focus_debrief=focus_debrief,
        **action_routes
    )


def render_role_entry():
    is_authenticated = current_user.is_authenticated
    is_guest = is_authenticated and is_guest_user(current_user)
    is_real_user = is_authenticated and not is_guest
    role = getattr(current_user, 'role', None) if is_authenticated else None
    # Pilot定位：老人不一定会用网页；主要入口是子女端（照护工作台）
    default_caregiver_next = url_for('user.pair_management')
    caregiver_next = default_caregiver_next
    community_next = url_for('user.community_dashboard')

    if is_guest:
        caregiver_target = url_for('public.register')
        caregiver_action_label = '注册开启照护'
        caregiver_requires_login = False
    elif is_real_user:
        caregiver_target = caregiver_next
        caregiver_action_label = '进入照护工作台'
        caregiver_requires_login = False
    else:
        caregiver_target = url_for('public.login', next=default_caregiver_next)
        caregiver_action_label = '进入照护工作台'
        caregiver_requires_login = True

    if is_real_user:
        if role in ('community', 'admin'):
            community_target = community_next
            community_action_label = '进入社区看板'
        else:
            community_target = url_for('user.community_risk')
            community_action_label = '查看社区风险'
        community_requires_login = False
    elif is_guest:
        community_target = url_for('user.community_risk')
        community_action_label = '查看社区风险'
        community_requires_login = False
    else:
        # 新注册家庭账号无社区工作台权限，匿名入口先落到可访问的风险页。
        community_target = url_for(
            'public.login',
            next=url_for('user.community_risk'),
        )
        community_action_label = '登录后查看社区风险'
        community_requires_login = True

    return render_template(
        'role_entry.html',
        elder_target=url_for('public.elder_entry'),
        caregiver_target=caregiver_target,
        community_target=community_target,
        caregiver_action_label=caregiver_action_label,
        community_action_label=community_action_label,
        caregiver_requires_login=caregiver_requires_login,
        community_requires_login=community_requires_login,
    )


def handle_login(next_url):
    if request.method == 'POST':
        # 输入验证
        username = request.form.get('username', '').strip()
        normalized_username = _normalize_login_identifier(username)
        password = request.form.get('password', '')
        remember_flag = request.form.get('remember') in ('1', 'on', 'true', 'yes')

        # 基本验证
        if not username or not password:
            flash('请输入用户名和密码', 'error')
            return render_template('login.html', next=next_url)

        # 限制长度防止攻击
        if len(username) > 50 or len(password) > 100:
            flash('输入内容过长', 'error')
            return render_template('login.html', next=next_url)

        user = User.query.filter_by(username=username).first()

        # 账户锁定检查（防暴力破解）
        lockout_key = _login_lockout_key(normalized_username)
        max_failures = current_app.config.get('LOGIN_MAX_FAILURES', 5)
        lockout_seconds = current_app.config.get('LOGIN_LOCKOUT_SECONDS', 300)
        redis_client = None
        try:
            from core.weather import _get_redis_client
            redis_client = _get_redis_client()
        except Exception:
            logger.warning("Redis 客户端初始化失败，登录锁定将回退数据库兜底", exc_info=True)

        if redis_client:
            try:
                fail_count = int(redis_client.get(lockout_key) or 0)
                if fail_count >= max_failures:
                    ttl = redis_client.ttl(lockout_key)
                    remaining = max(ttl, 0)
                    logger.warning("账户被锁定(redis): %s (剩余%ds)", username, remaining)
                    flash(f'登录失败次数过多，请 {remaining // 60 + 1} 分钟后再试', 'error')
                    return render_template('login.html', next=next_url)
            except Exception:
                logger.warning("Redis 锁定检查失败，回退数据库兜底", exc_info=True)
                redis_client = None

        if not redis_client:
            try:
                db_locked, db_remaining = _get_login_lock_state_from_db(normalized_username, max_failures, lockout_seconds)
                if db_locked:
                    logger.warning("账户被锁定(db): %s (剩余%ds)", username, db_remaining)
                    flash(f'登录失败次数过多，请 {db_remaining // 60 + 1} 分钟后再试', 'error')
                    return render_template('login.html', next=next_url)
            except Exception:
                logger.warning("数据库锁定检查失败", exc_info=True)

        if user and user.check_password(password):
            # 登录成功，清除失败计数
            if redis_client:
                try:
                    redis_client.delete(lockout_key)
                except Exception:
                    logger.warning("Redis 清除失败计数失败，回退数据库兜底", exc_info=True)
                    redis_client = None
            if not redis_client:
                try:
                    _clear_login_failures_db(normalized_username)
                except Exception:
                    logger.warning("数据库清除失败计数失败", exc_info=True)
            user.last_login = utcnow()
            db.session.commit()
            # 正式登录前清理游客资料与匿名行动授权，再写入正式身份。
            _clear_identity_session_state()
            login_user(
                user,
                remember=remember_flag,
                duration=timedelta(days=30) if remember_flag else None,
            )
            logger.info("用户登录成功: %s", username)

            safe_next = _safe_next_url(next_url)
            if safe_next:
                return redirect(safe_next)

            # 没有显式 next 时，让每种角色直达自己的主工作台。
            return redirect(url_for(_role_landing_endpoint(user.role)))

        # 登录失败，递增失败计数
        if redis_client:
            try:
                pipe = redis_client.pipeline()
                pipe.incr(lockout_key)
                pipe.expire(lockout_key, lockout_seconds)
                pipe.execute()
            except Exception:
                logger.warning("Redis 递增失败计数失败，回退数据库兜底", exc_info=True)
                redis_client = None
        if not redis_client:
            try:
                _record_login_failure_db(normalized_username, max_failures, lockout_seconds)
            except Exception:
                logger.warning("数据库递增失败计数失败", exc_info=True)

        logger.warning("登录失败: %s", username)
        flash('用户名或密码错误', 'error')

    return render_template('login.html', next=next_url)


def handle_register():
    if current_user.is_authenticated and not is_guest_user(current_user):
        return redirect(url_for(_role_landing_endpoint(getattr(current_user, 'role', None))))

    if request.method == 'POST':
        form_data = _registration_form_data()

        # 验证用户名
        valid, result = validate_username(request.form.get('username'))
        if not valid:
            flash(result, 'error')
            return _render_register(form_data)
        username = result

        # 验证密码
        password_raw = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        valid, result = validate_password(password_raw)
        if not valid:
            flash(result, 'error')
            return _render_register(form_data)
        password = result
        if password != confirm_password:
            flash('两次输入的密码不一致', 'error')
            return _render_register(form_data)

        # 验证邮箱
        valid, result = validate_email(request.form.get('email'))
        if not valid:
            flash(result, 'error')
            return _render_register(form_data)
        email = result

        # 验证年龄
        valid, result = validate_age(request.form.get('age'))
        if not valid:
            flash(result, 'error')
            return _render_register(form_data)
        age = result

        # 验证性别
        valid, result = validate_gender(request.form.get('gender'))
        if not valid:
            flash(result, 'error')
            return _render_register(form_data)
        gender = result

        # 社区信息
        community = sanitize_input(request.form.get('community'), max_length=100)

        # 检查用户名是否已存在
        if User.query.filter_by(username=username).first():
            flash('用户名已存在', 'error')
            return _render_register(form_data)

        # 检查邮箱是否已存在
        if email and User.query.filter(db.func.lower(User.email) == email.lower()).first():
            flash('邮箱已被注册', 'error')
            return _render_register(form_data)

        user = User(
            username=username,
            email=email,
            age=age,
            gender=gender,
            community=community,
            role='caregiver',
            last_login=utcnow(),
        )
        user.set_password(password)

        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # 预检查与提交之间仍可能出现唯一键竞争，以数据库约束为准。
            db.session.rollback()
            flash('用户名或邮箱已被注册，请更换后重试', 'error')
            return _render_register(form_data)

        logger.info("新用户注册: %s", username)
        # 用户落库成功后再切换会话，避免失败时丢失当前游客体验。
        _clear_identity_session_state()
        login_user(user)
        flash('注册成功，已进入照护工作台', 'success')
        return redirect(url_for('user.pair_management'))

    return _render_register()


def render_cooling_resources_page(community, resource_type, has_ac_raw, is_accessible_raw, open_only):
    open_only_flag = parse_bool(open_only, default=False)
    location_query = sanitize_input(request.args.get('location'), max_length=100)
    weather_location = normalize_location_name(community or location_query or None)
    cooling_weather = {}
    try:
        cooling_weather, _ = get_weather_with_cache(weather_location)
    except Exception as exc:
        logger.warning("避暑资源页天气读取失败，已隐藏室外温度计: %s", exc)
        cooling_weather = {}
    outdoor_temp = None
    if is_live_observational_weather(cooling_weather):
        outdoor_temp = cooling_weather.get('temperature')

    query = CoolingResource.query.filter_by(is_active=True)
    if community:
        query = query.filter(CoolingResource.community_code == community)
    if resource_type:
        query = query.filter(CoolingResource.resource_type == resource_type)
    if has_ac_raw not in (None, ''):
        has_ac_flag = parse_bool(has_ac_raw)
        if has_ac_flag:
            query = query.filter(CoolingResource.has_ac.is_(True))
        else:
            query = query.filter(or_(CoolingResource.has_ac.is_(False), CoolingResource.has_ac.is_(None)))
    if is_accessible_raw not in (None, ''):
        accessible_flag = parse_bool(is_accessible_raw)
        if accessible_flag:
            query = query.filter(CoolingResource.is_accessible.is_(True))
        else:
            query = query.filter(or_(CoolingResource.is_accessible.is_(False), CoolingResource.is_accessible.is_(None)))
    if open_only_flag:
        query = query.filter(
            CoolingResource.open_hours.isnot(None),
            CoolingResource.open_hours != ''
        )

    resources = query.all()
    resource_cards = present_cooling_cards(resources, utcnow())
    all_resources = CoolingResource.query.filter_by(is_active=True).all()
    communities = sorted({item.community_code for item in all_resources if item.community_code})
    resource_types = sorted({item.resource_type for item in all_resources if item.resource_type})
    grouped = {}
    map_points = []
    for card in resource_cards:
        grouped.setdefault(card.community_code or '未标注社区', []).append(card)
        if card.latitude is not None and card.longitude is not None:
            map_points.append({
                'name': card.name,
                'community': card.community_code,
                'type': card.resource_type,
                'address': card.address_hint,
                'open_hours': card.open_hours,
                'has_ac': bool(card.has_ac),
                'is_accessible': bool(card.is_accessible),
                'lat': card.latitude,
                'lng': card.longitude
            })

    amap_key = current_app.config.get('AMAP_KEY')
    amap_security_js_code = current_app.config.get('AMAP_SECURITY_JS_CODE')
    return render_template(
        'cooling.html',
        resource_cards=resource_cards,
        resources_by_community=grouped,
        total=len(resources),
        communities=communities,
        resource_types=resource_types,
        selected_community=community or '',
        selected_resource_type=resource_type or '',
        selected_has_ac=has_ac_raw if has_ac_raw is not None else '',
        selected_is_accessible=is_accessible_raw if is_accessible_raw is not None else '',
        open_only=open_only_flag,
        map_points=map_points,
        amap_key=amap_key,
        amap_security_js_code=amap_security_js_code,
        cooling_weather=cooling_weather,
        cooling_weather_location=weather_location,
        cooling_weather_source=weather_source_label(cooling_weather),
        outdoor_temp=outdoor_temp
    )


def render_public_risk_page(location):
    location = normalize_location_name(location) if location else normalize_location_name(None)
    weather_data, _ = get_weather_with_cache(location)
    if not _heat_risk_weather_is_ready(weather_data):
        return render_template(
            'risk.html',
            location=location,
            weather_source_city=resolve_weather_city_label(location),
            weather_source_label=weather_source_label(weather_data),
            weather=None,
            heat_result=None,
            risk_label=None,
            actions=[],
            risk_reasons=[]
        )

    heat_service = HeatActionService()
    consecutive_hot_days = get_consecutive_hot_days(
        location,
        today_max=weather_data.get('temperature_max'),
        weather_data=weather_data,
    )
    heat_result = heat_service.calculate_heat_risk(
        weather_data,
        consecutive_hot_days=consecutive_hot_days
    )
    risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
    actions = _action_plan(risk_label)
    risk_reasons = heat_service.build_risk_reasons(heat_result)
    return render_template(
        'risk.html',
        location=location,
        weather_source_city=resolve_weather_city_label(location),
        weather_source_label=weather_source_label(weather_data),
        weather=weather_data,
        heat_result=heat_result,
        risk_label=risk_label,
        actions=actions,
        risk_reasons=risk_reasons
    )


def handle_guest_login(next_url=None):
    if current_user.is_authenticated and not is_guest_user(current_user):
        return redirect(url_for(_role_landing_endpoint(getattr(current_user, 'role', None))))

    session['guest_profile'] = {
        'username': '游客',
        'age': None,
        'gender': '未知',
        'community': DEFAULT_CITY_LABEL,
        'has_chronic_disease': False,
        'chronic_diseases': None
    }
    session.pop('guest_assessment', None)
    existing_guest_id = session.get('guest_id')
    # 复用已有游客 ID，减少频繁轮换带来的标识面扩大，有助于降低滥用绕过空间。
    if (
        isinstance(existing_guest_id, str)
        and existing_guest_id.startswith(GUEST_ID_PREFIX)
        and len(existing_guest_id) > len(GUEST_ID_PREFIX)
    ):
        guest_id = existing_guest_id
    else:
        guest_id = f"{GUEST_ID_PREFIX}{secrets.token_urlsafe(12)}"
    session['guest_id'] = guest_id
    guest_user = GuestUser(guest_id, session['guest_profile'])
    login_user(guest_user)
    flash('已进入游客模式（数据不会保存）', 'success')
    safe_next = _safe_next_url(next_url)
    return redirect(safe_next or url_for('user.user_dashboard'))


def handle_logout():
    # 退出后清除身份资料与行动授权，避免共享浏览器继续写入上一位长者状态。
    _clear_identity_session_state()
    logout_user()
    return redirect(url_for('public.index'))
