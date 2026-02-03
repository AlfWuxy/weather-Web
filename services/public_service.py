# -*- coding: utf-8 -*-
"""Public-facing business logic extracted from blueprints."""
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import or_

from core.constants import GUEST_ID_PREFIX
from core.extensions import db
from core.security import hash_identifier, hash_short_code, rate_limit_key, verify_pair_token
from core.time_utils import today_local, utcnow, ensure_utc_aware
from core.weather import get_weather_with_cache, normalize_location_name, get_consecutive_hot_days
from core.guest import GuestUser, is_guest_user
from core.db_models import (
    Community,
    CoolingResource,
    DailyStatus,
    Debrief,
    Pair,
    PairLink,
    ShortCodeAttempt,
    User
)
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


def _short_code_guard_config():
    max_failures = current_app.config.get('SHORT_CODE_FAIL_MAX', 5)
    window_minutes = current_app.config.get('SHORT_CODE_FAIL_WINDOW_MINUTES', 30)
    lock_minutes = current_app.config.get('SHORT_CODE_LOCK_MINUTES', 30)
    return max_failures, window_minutes, lock_minutes


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
    from core.db_models import CommunityDaily

    total_people = Pair.query.filter_by(status='active', community_code=community_code).count()
    statuses = DailyStatus.query.filter_by(
        community_code=community_code,
        status_date=status_date
    ).all()
    confirmed_count = sum(1 for s in statuses if s.confirmed_at)
    help_count = sum(1 for s in statuses if s.help_flag)
    escalation_count = sum(1 for s in statuses if s.relay_stage in ('backup', 'community', 'emergency'))
    risk_dist = {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0}
    for status in statuses:
        if status.risk_level in risk_dist:
            risk_dist[status.risk_level] += 1
    if total_people <= 0:
        summary = '暂无可用行动数据。'
    else:
        pending = total_people - confirmed_count
        if escalation_count > 0:
            summary = f'已有{escalation_count}个家庭进入升级链，优先安排社区跟进。'
        elif help_count > 0:
            summary = f'已有{help_count}个家庭发出求助，请尽快联系。'
        elif pending > 0:
            summary = f'仍有{pending}个家庭未确认，建议分批提醒。'
        else:
            summary = '全部家庭已完成确认，继续关注高温变化。'

    confirm_rate = (confirmed_count / total_people) if total_people else 0
    escalation_rate = (escalation_count / total_people) if total_people else 0

    record = CommunityDaily.query.filter_by(
        community_code=community_code,
        date=status_date
    ).first()
    if not record:
        record = CommunityDaily(community_code=community_code, date=status_date)
        db.session.add(record)
    record.total_people = total_people
    record.confirm_rate = round(confirm_rate, 4)
    record.escalation_rate = round(escalation_rate, 4)
    record.risk_distribution = json.dumps(risk_dist, ensure_ascii=False)
    record.outreach_summary = summary
    db.session.commit()


def _build_action_context(pair, status_date):
    location = normalize_location_name(pair.community_code)
    weather_data, _ = get_weather_with_cache(location)
    heat_service = HeatActionService()
    consecutive_hot_days = get_consecutive_hot_days(
        location,
        today_max=weather_data.get('temperature_max')
    )
    heat_result = heat_service.calculate_heat_risk(
        weather_data,
        consecutive_hot_days=consecutive_hot_days
    )
    risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
    risk_reasons = heat_service.build_risk_reasons(heat_result)
    status = _get_or_create_daily_status(pair, status_date, risk_label)
    actions = _action_plan(risk_label)
    resources = CoolingResource.query.filter_by(
        community_code=pair.community_code,
        is_active=True
    ).all()
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
    focus_debrief=False
):
    recent_series = _build_recent_series(pair.id) if pair else []
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
        focus_debrief=focus_debrief
    )


def _resolve_action_routes(token=None, confirm_action=None, help_action=None, debrief_action=None):
    routes = {}
    if token:
        routes['confirm_action'] = url_for('public.elder_token_checkin', token=token)
        routes['help_action'] = url_for('public.elder_token_help', token=token)
        routes['debrief_action'] = url_for('public.elder_token_debrief', token=token)
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
    return render_template(
        'action_checkin.html',
        stage='lookup',
        short_code=short_code,
        entry_action=entry_action
    )


def _resolve_pair_from_session_or_code(short_code):
    pair = None
    session_pair_id = session.get('pair_session_id')
    if session_pair_id:
        pair = Pair.query.filter_by(id=session_pair_id, status='active').first()
    if not pair and short_code:
        short_code_hash = hash_short_code(short_code)
        pair = Pair.query.filter_by(short_code_hash=short_code_hash, status='active').first()
    return pair


def _handle_action_confirm(token=None, confirm_action=None, debrief_action=None):
    short_code = sanitize_input(request.form.get('short_code'), max_length=12)
    pair = _resolve_pair_from_session_or_code(short_code)
    if not pair:
        flash('短码无效或已失效', 'error')
        return redirect(url_for('public.action_check'))

    token = sanitize_input(request.form.get('token') or token, max_length=200)
    status_date = today_local()
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    actions_done = request.form.getlist('actions_done')
    status.actions_done_count = len(actions_done)
    status.confirmed_at = utcnow()
    pair.last_active_at = utcnow()
    db.session.commit()
    _refresh_community_daily(pair.community_code, status_date)
    flash('已记录今日确认。', 'success')
    action_routes = _resolve_action_routes(confirm_action=confirm_action, debrief_action=debrief_action)
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
    short_code = sanitize_input(request.form.get('short_code'), max_length=12)
    pair = _resolve_pair_from_session_or_code(short_code)
    if not pair:
        flash('短码无效或已失效', 'error')
        return redirect(url_for('public.action_check'))

    token = sanitize_input(request.form.get('token') or token, max_length=200)
    status_date = today_local()
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    status.confirmed_at = utcnow()
    status.help_flag = True
    if not status.relay_stage or status.relay_stage == 'none':
        status.relay_stage = 'caregiver'
    pair.last_active_at = utcnow()
    db.session.commit()
    _refresh_community_daily(pair.community_code, status_date)
    flash('已记录求助，照护人将收到提醒。', 'success')
    action_routes = _resolve_action_routes(confirm_action=confirm_action, debrief_action=debrief_action)
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


def _handle_action_debrief(token=None, confirm_action=None, debrief_action=None, focus_debrief=False):
    short_code = sanitize_input(request.form.get('short_code'), max_length=12)
    pair = _resolve_pair_from_session_or_code(short_code)
    if not pair:
        flash('短码无效或已失效', 'error')
        return redirect(url_for('public.action_check'))

    token = sanitize_input(request.form.get('token') or token, max_length=200)
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
    db.session.commit()
    _refresh_community_daily(pair.community_code, status_date)
    flash('复盘已提交，感谢反馈。', 'success')

    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    action_routes = _resolve_action_routes(confirm_action=confirm_action, debrief_action=debrief_action)
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
    is_real_user = current_user.is_authenticated and not is_guest_user(current_user)
    caregiver_next = url_for('user.caregiver_dashboard')
    community_next = url_for('user.community_dashboard')
    caregiver_target = caregiver_next if is_real_user else url_for('public.login', next=caregiver_next)
    community_target = community_next if is_real_user else url_for('public.login', next=community_next)
    return render_template(
        'role_entry.html',
        elder_target=url_for('public.elder_entry'),
        caregiver_target=caregiver_target,
        community_target=community_target,
        caregiver_requires_login=not is_real_user,
        community_requires_login=not is_real_user
    )


def handle_login(next_url):
    if request.method == 'POST':
        # 输入验证
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # 基本验证
        if not username or not password:
            flash('请输入用户名和密码', 'error')
            return render_template('login.html', next=next_url)

        # 限制长度防止攻击
        if len(username) > 50 or len(password) > 100:
            flash('输入内容过长', 'error')
            return render_template('login.html', next=next_url)

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            user.last_login = utcnow()
            db.session.commit()
            logger.info("用户登录成功: %s", username)

            safe_next = _safe_next_url(next_url)
            if safe_next:
                return redirect(safe_next)

            if user.role == 'admin':
                return redirect(url_for('admin.admin_dashboard'))
            return redirect(url_for('user.user_dashboard'))

        logger.warning("登录失败: %s", username)
        flash('用户名或密码错误', 'error')

    return render_template('login.html', next=next_url)


def handle_register():
    if request.method == 'POST':
        # 验证用户名
        valid, result = validate_username(request.form.get('username'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('public.register'))
        username = result

        # 验证密码
        valid, result = validate_password(request.form.get('password'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('public.register'))
        password = result

        # 验证邮箱
        valid, result = validate_email(request.form.get('email'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('public.register'))
        email = result

        # 验证年龄
        valid, result = validate_age(request.form.get('age'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('public.register'))
        age = result

        # 验证性别
        valid, result = validate_gender(request.form.get('gender'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('public.register'))
        gender = result

        # 社区信息
        community = sanitize_input(request.form.get('community'), max_length=100)

        # 检查用户名是否已存在
        if User.query.filter_by(username=username).first():
            flash('用户名已存在', 'error')
            return redirect(url_for('public.register'))

        # 检查邮箱是否已存在
        if email and User.query.filter_by(email=email).first():
            flash('邮箱已被注册', 'error')
            return redirect(url_for('public.register'))

        user = User(
            username=username,
            email=email,
            age=age,
            gender=gender,
            community=community
        )
        user.set_password(password)

        db.session.add(user)
        db.session.commit()

        logger.info("新用户注册: %s", username)
        flash('注册成功，请登录', 'success')
        return redirect(url_for('public.login'))

    communities = Community.query.all()
    return render_template('register.html', communities=communities)


def render_cooling_resources_page(community, resource_type, has_ac_raw, is_accessible_raw, open_only):
    open_only_flag = parse_bool(open_only, default=False)

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

    resources = query.order_by(
        CoolingResource.community_code,
        CoolingResource.name
    ).all()
    all_resources = CoolingResource.query.filter_by(is_active=True).all()
    communities = sorted({item.community_code for item in all_resources if item.community_code})
    resource_types = sorted({item.resource_type for item in all_resources if item.resource_type})
    grouped = {}
    map_points = []
    for item in resources:
        grouped.setdefault(item.community_code or '未标注社区', []).append(item)
        if item.latitude is not None and item.longitude is not None:
            map_points.append({
                'name': item.name,
                'community': item.community_code,
                'type': item.resource_type,
                'address': item.address_hint,
                'open_hours': item.open_hours,
                'has_ac': bool(item.has_ac),
                'is_accessible': bool(item.is_accessible),
                'lat': item.latitude,
                'lng': item.longitude
            })

    amap_key = current_app.config.get('AMAP_KEY')
    amap_security_js_code = current_app.config.get('AMAP_SECURITY_JS_CODE')
    return render_template(
        'cooling.html',
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
        amap_security_js_code=amap_security_js_code
    )


def render_public_risk_page(location):
    location = normalize_location_name(location) if location else normalize_location_name(None)
    weather_data, _ = get_weather_with_cache(location)
    heat_service = HeatActionService()
    consecutive_hot_days = get_consecutive_hot_days(
        location,
        today_max=weather_data.get('temperature_max')
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
        weather=weather_data,
        heat_result=heat_result,
        risk_label=risk_label,
        actions=actions,
        risk_reasons=risk_reasons
    )


def handle_guest_login():
    if current_user.is_authenticated and not is_guest_user(current_user):
        return redirect(url_for('user.user_dashboard'))

    session['guest_profile'] = {
        'username': '游客',
        'age': None,
        'gender': '未知',
        'community': '朝阳社区',
        'has_chronic_disease': False,
        'chronic_diseases': None
    }
    session.pop('guest_assessment', None)
    guest_id = f"{GUEST_ID_PREFIX}{secrets.token_urlsafe(12)}"
    session['guest_id'] = guest_id
    guest_user = GuestUser(guest_id, session['guest_profile'])
    login_user(guest_user)
    flash('已进入游客模式（数据不会保存）', 'success')
    return redirect(url_for('user.user_dashboard'))


def handle_logout():
    if is_guest_user(current_user):
        session.pop('guest_profile', None)
        session.pop('guest_assessment', None)
        session.pop('guest_id', None)
    logout_user()
    return redirect(url_for('public.index'))
