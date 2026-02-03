# -*- coding: utf-8 -*-
"""Caregiver-related routes and helpers."""
import secrets
from datetime import datetime, timedelta

from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user

from core.db_models import Community, DailyStatus, Debrief, Pair, PairLink
from core.extensions import db
from core.guest import is_guest_user
from core.security import hash_pair_token, hash_short_code
from core.time_utils import today_local, utcnow, local_datetime_to_utc
from core.weather import get_consecutive_hot_days, get_weather_with_cache, is_demo_mode, normalize_location_name
from services.heat_action_service import HeatActionService
from utils.audit_log import log_security_event
from utils.database import atomic_transaction
from utils.parsers import json_or_none, safe_json_loads
from utils.validators import sanitize_input

from ._common import (
    AUTO_ESCALATE_STAGE,
    CARE_ACTION_OPTIONS,
    HEAT_RISK_LABELS,
    RELAY_STAGE_LABELS,
    RELAY_STAGE_ORDER,
    _action_plan,
    _generate_short_code,
    _relay_stage_rank,
    _require_roles
)
from ._helpers import (
    _auto_escalate_overdue_statuses,
    _build_caregiver_message,
    _build_community_snapshot,
    _build_recent_series,
    _ensure_demo_statuses,
    _refresh_community_daily
)


def _create_pair_link(community_code):
    short_code = _generate_short_code()
    short_code_hash = hash_short_code(short_code)
    token = secrets.token_urlsafe(16)
    link = PairLink(
        caregiver_id=current_user.id,
        short_code=short_code,
        short_code_hash=short_code_hash,
        token_hash=hash_pair_token(token),
        community_code=community_code,
        expires_at=utcnow() + timedelta(days=3)
    )
    with atomic_transaction():
        db.session.add(link)
        db.session.flush()
        log_security_event(
            action='short_code_generated',
            actor_id=getattr(current_user, 'id', None),
            actor_role=getattr(current_user, 'role', None),
            resource_type='pair_link',
            resource_id=str(link.id),
            extra_data={
                'community_code': community_code,
                'short_code_hash': short_code_hash
            }
        )
    session['pair_link_token'] = token
    session['pair_link_id'] = link.id
    return link, token


def _load_created_link():
    created_link = None
    created_token = None
    created_id = request.args.get('created', type=int)
    if created_id and session.get('pair_link_id') == created_id:
        created_link = PairLink.query.filter_by(id=created_id, caregiver_id=current_user.id).first()
        created_token = session.pop('pair_link_token', None)
        session.pop('pair_link_id', None)
    return created_link, created_token


def _build_pair_management_context(caregiver_mode=False):
    created_link, created_token = _load_created_link()
    status_date = today_local()
    pairs = Pair.query.filter_by(caregiver_id=current_user.id).order_by(Pair.created_at.desc()).all()
    communities = Community.query.order_by(Community.name).all()
    if is_demo_mode() and not pairs:
        demo_community = normalize_location_name(getattr(current_user, 'community', None))
        if not demo_community and communities:
            demo_community = communities[0].name
        if demo_community:
            _ensure_demo_statuses(demo_community, status_date, caregiver_id=current_user.id)
            pairs = Pair.query.filter_by(caregiver_id=current_user.id).order_by(Pair.created_at.desc()).all()

    pair_ids = [pair.id for pair in pairs]
    status_map = {}
    if pair_ids:
        statuses = DailyStatus.query.filter(
            DailyStatus.pair_id.in_(pair_ids),
            DailyStatus.status_date == status_date
        ).all()
        _auto_escalate_overdue_statuses(statuses, status_date)
        status_map = {status.pair_id: status for status in statuses}
    created_link_url = None
    wechat_template_url = None
    if created_link and created_token:
        created_link_url = url_for(
            'public.elder_token_entry',
            token=created_token,
            short_code=created_link.short_code,
            _external=True
        )
        if getattr(current_user, 'role', None) in ('caregiver', 'admin'):
            wechat_template_url = url_for(
                'user.caregiver_wechat_template',
                short_code=created_link.short_code,
                token=created_token,
                community_code=created_link.community_code
            )

    risk_by_community = {}
    if pairs:
        heat_service = HeatActionService()
        for pair in pairs:
            code = pair.community_code
            if not code or code in risk_by_community:
                continue
            location = normalize_location_name(code)
            weather_data, _ = get_weather_with_cache(location)
            consecutive_hot_days = get_consecutive_hot_days(
                location,
                today_max=weather_data.get('temperature_max')
            )
            heat_result = heat_service.calculate_heat_risk(
                weather_data,
                consecutive_hot_days=consecutive_hot_days
            )
            risk_by_community[code] = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')

    pair_cards = []
    now = utcnow()
    # 计算本地时间晚上8点的 UTC 时间
    local_deadline = datetime.combine(status_date, datetime.min.time()).replace(hour=20)
    deadline = local_datetime_to_utc(local_deadline)
    for pair in pairs:
        status = status_map.get(pair.id)
        risk_label = status.risk_level if status and status.risk_level else risk_by_community.get(
            pair.community_code,
            '低风险'
        )
        confirmed = bool(status and status.confirmed_at)
        is_overdue = bool(now >= deadline and not confirmed)
        relay_stage = status.relay_stage if status else None
        relay_stage_label = None
        if relay_stage and relay_stage != 'none':
            relay_stage_label = RELAY_STAGE_LABELS.get(relay_stage, relay_stage)
        pair_cards.append({
            'pair': pair,
            'status': status,
            'risk_label': risk_label,
            'action_link': url_for('public.elder_entry', short_code=pair.short_code, _external=True),
            'reminder_message': _build_caregiver_message(pair, risk_label),
            'help_flag': bool(status and status.help_flag),
            'is_overdue': is_overdue,
            'relay_stage': relay_stage,
            'relay_stage_label': relay_stage_label
        })

    context = {
        'created_link': created_link,
        'created_token': created_token,
        'created_link_url': created_link_url,
        'wechat_template_url': wechat_template_url,
        'pairs': pairs,
        'pair_cards': pair_cards,
        'status_map': status_map,
        'communities': communities,
        'status_date': status_date
    }

    if caregiver_mode:
        context.update({
            'pair_create_action': url_for('user.caregiver_pair_create'),
            'pair_escalate_action': url_for('user.caregiver_relay_escalate'),
            'pair_escalate_requires_id': True,
            'pair_backup_action': url_for('user.caregiver_relay_backup'),
            'pair_backup_requires_id': True,
            'pair_detail_endpoint': 'user.caregiver_pair_detail'
        })

    return context


def pair_management():
    """照护绑定管理"""
    if is_guest_user(current_user):
        flash('游客模式无法创建绑定，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        community_code = sanitize_input(request.form.get('community_code'), max_length=100)
        if not community_code:
            community_code = normalize_location_name(current_user.community)
        if not community_code:
            flash('请填写社区', 'error')
            return redirect(url_for('user.pair_management'))

        link, _ = _create_pair_link(community_code)
        return redirect(url_for('user.pair_management', created=link.id))

    context = _build_pair_management_context()
    return render_template('pair_management.html', **context)


def caregiver_dashboard():
    """照护人工作台"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))
    if is_guest_user(current_user):
        flash('游客模式无法进入照护工作台', 'error')
        return redirect(url_for('user.user_dashboard'))

    context = _build_pair_management_context(caregiver_mode=True)
    return render_template('pair_management.html', **context)


def caregiver_pair_create():
    """照护人创建绑定短码"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))
    if is_guest_user(current_user):
        flash('游客模式无法创建绑定', 'error')
        return redirect(url_for('user.user_dashboard'))

    community_code = sanitize_input(request.form.get('community_code'), max_length=100)
    if not community_code:
        community_code = normalize_location_name(current_user.community)
    if not community_code:
        flash('请填写社区', 'error')
        return redirect(url_for('user.caregiver_dashboard'))

    link, _ = _create_pair_link(community_code)
    return redirect(url_for('user.caregiver_dashboard', created=link.id))


def caregiver_pair_detail(pair_id):
    """照护关系详情"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))
    if is_guest_user(current_user):
        flash('游客模式无法查看详情', 'error')
        return redirect(url_for('user.user_dashboard'))

    query = Pair.query.filter_by(id=pair_id)
    if getattr(current_user, 'role', None) != 'admin':
        query = query.filter_by(caregiver_id=current_user.id)
    pair = query.first_or_404()

    status_date = today_local()
    status_today = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first()
    recent_statuses = DailyStatus.query.filter_by(pair_id=pair.id).order_by(
        DailyStatus.status_date.desc()
    ).limit(7).all()
    recent_series = _build_recent_series(pair.id, days=7)
    debrief_today = Debrief.query.filter_by(pair_id=pair.id, date=status_date).first()
    community_snapshot = _build_community_snapshot(pair.community_code, status_date)
    wechat_template_url = url_for(
        'user.caregiver_wechat_template',
        short_code=pair.short_code,
        community_code=pair.community_code
    )
    actions_today = safe_json_loads(status_today.caregiver_actions, []) if status_today else []
    if not isinstance(actions_today, list):
        actions_today = []
    caregiver_note = status_today.caregiver_note if status_today else None

    return render_template(
        'caregiver_pair_detail.html',
        pair=pair,
        status_today=status_today,
        recent_statuses=recent_statuses,
        recent_series=recent_series,
        debrief_today=debrief_today,
        community_snapshot=community_snapshot,
        status_date=status_date,
        wechat_template_url=wechat_template_url,
        action_options=CARE_ACTION_OPTIONS,
        actions_today=actions_today,
        caregiver_note=caregiver_note
    )


def caregiver_action_log(pair_id):
    """照护行动记录"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))
    if is_guest_user(current_user):
        flash('游客模式无法记录行动', 'error')
        return redirect(url_for('user.user_dashboard'))

    query = Pair.query.filter_by(id=pair_id)
    if getattr(current_user, 'role', None) != 'admin':
        query = query.filter_by(caregiver_id=current_user.id)
    pair = query.first_or_404()

    status_date = today_local()
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first()
    if not status:
        location = normalize_location_name(pair.community_code)
        weather_data, _ = get_weather_with_cache(location)
        consecutive_hot_days = get_consecutive_hot_days(
            location,
            today_max=weather_data.get('temperature_max')
        )
        heat_result = HeatActionService().calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
        risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
        status = DailyStatus(
            pair_id=pair.id,
            status_date=status_date,
            community_code=pair.community_code,
            risk_level=risk_label
        )
        db.session.add(status)

    allowed_actions = {item['id'] for item in CARE_ACTION_OPTIONS}
    actions = [item for item in request.form.getlist('caregiver_actions') if item in allowed_actions]
    note = sanitize_input(request.form.get('caregiver_note'), max_length=300)
    status.caregiver_actions = json_or_none(actions)
    status.caregiver_note = note or None
    db.session.commit()
    flash('行动记录已保存。', 'success')
    return redirect(url_for('user.caregiver_pair_detail', pair_id=pair.id))


def caregiver_wechat_template():
    """照护人微信模板"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    short_code = sanitize_input(request.args.get('short_code'), max_length=12)
    token = sanitize_input(request.args.get('token'), max_length=200)
    community_code = sanitize_input(request.args.get('community_code'), max_length=100)

    if token:
        action_link = url_for(
            'public.elder_token_entry',
            token=token,
            short_code=short_code,
            _external=True
        )
    else:
        action_link = url_for(
            'public.elder_entry',
            short_code=short_code,
            _external=True
        )

    risk_label = None
    actions = _action_plan('低风险')
    weather_data = None
    if community_code:
        location = normalize_location_name(community_code)
        weather_data, _ = get_weather_with_cache(location)
        consecutive_hot_days = get_consecutive_hot_days(
            location,
            today_max=weather_data.get('temperature_max')
        )
        heat_result = HeatActionService().calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
        risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
        actions = _action_plan(risk_label)

    message_lines = [
        '【高温行动提醒】',
        f'行动链接：{action_link}',
        f'短码：{short_code or "请填写"}'
    ]
    if community_code:
        message_lines.insert(1, f'社区：{community_code}')
    if risk_label:
        message_lines.append(f'今日热风险：{risk_label}')
    message_lines.append('行动建议（非医疗诊断/治疗）：')
    for item in actions:
        message_lines.append(f'- {item["title"]}：{item["detail"]}')
    message_lines.append('如需帮助请在页面内点击“我需要帮助”。')

    return render_template(
        'caregiver_wechat_template.html',
        message='\n'.join(message_lines),
        action_link=action_link,
        short_code=short_code,
        community_code=community_code,
        weather=weather_data
    )


def _handle_pair_escalate(pair_id, redirect_url, target_stage=None):
    if is_guest_user(current_user):
        flash('游客模式无法升级', 'error')
        return redirect(url_for('user.user_dashboard'))

    query = Pair.query.filter_by(id=pair_id)
    if getattr(current_user, 'role', None) != 'admin':
        query = query.filter_by(caregiver_id=current_user.id)
    pair = query.first_or_404()

    status_date = today_local()
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first()

    if not status:
        location = normalize_location_name(pair.community_code)
        weather_data, _ = get_weather_with_cache(location)
        consecutive_hot_days = get_consecutive_hot_days(
            location,
            today_max=weather_data.get('temperature_max')
        )
        heat_result = HeatActionService().calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
        risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
        status = DailyStatus(
            pair_id=pair.id,
            status_date=status_date,
            community_code=pair.community_code,
            risk_level=risk_label
        )
        db.session.add(status)

    stages = RELAY_STAGE_ORDER
    current_stage = status.relay_stage or 'none'
    if target_stage:
        if target_stage not in stages:
            flash('升级阶段无效', 'error')
            return redirect(redirect_url)
        if _relay_stage_rank(current_stage) >= _relay_stage_rank(target_stage):
            flash('已在更高阶段', 'info')
        else:
            status.relay_stage = target_stage
            stage_label = RELAY_STAGE_LABELS.get(target_stage, target_stage)
            flash(f'已标记为{stage_label}', 'success')
    else:
        try:
            next_index = stages.index(current_stage) + 1
        except ValueError:
            next_index = 1
        if next_index >= len(stages):
            flash('已是最高级别', 'info')
        else:
            next_stage = stages[next_index]
            status.relay_stage = next_stage
            stage_label = RELAY_STAGE_LABELS.get(next_stage, next_stage)
            flash(f'升级已记录（{stage_label}）', 'success')
    db.session.commit()
    _refresh_community_daily(pair.community_code, status_date)
    return redirect(redirect_url)


def pair_escalate(pair_id):
    """升级链推进"""
    return _handle_pair_escalate(pair_id, url_for('user.pair_management'))


def pair_backup_contact(pair_id):
    """标记已联系备选联系人"""
    return _handle_pair_escalate(pair_id, url_for('user.pair_management'), target_stage=AUTO_ESCALATE_STAGE)


def caregiver_relay_escalate():
    """照护人升级链推进"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    pair_id = request.form.get('pair_id', type=int)
    if not pair_id:
        flash('缺少照护关系', 'error')
        return redirect(url_for('user.caregiver_dashboard'))
    return _handle_pair_escalate(pair_id, url_for('user.caregiver_pair_detail', pair_id=pair_id))


def caregiver_relay_backup():
    """照护人标记备选联系人已联系"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    pair_id = request.form.get('pair_id', type=int)
    if not pair_id:
        flash('缺少照护关系', 'error')
        return redirect(url_for('user.caregiver_dashboard'))
    return _handle_pair_escalate(
        pair_id,
        url_for('user.caregiver_pair_detail', pair_id=pair_id),
        target_stage=AUTO_ESCALATE_STAGE
    )
