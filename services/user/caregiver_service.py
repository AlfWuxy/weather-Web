# -*- coding: utf-8 -*-
"""Caregiver-related routes and helpers."""
import logging
import math
from datetime import datetime, timedelta

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user

from core.db_models import Community, DailyStatus, Debrief, FamilyMember, Pair, PairLink
from core.extensions import db
from core.guest import is_guest_user
from core.time_utils import today_local, utcnow, local_datetime_to_utc
from core.weather import (
    get_consecutive_hot_days,
    get_weather_with_cache,
    is_qweather_online_weather,
    normalize_location_name,
)
from core.usage import log_usage_event
from services.heat_action_service import HeatActionService
from services.location_resolver import resolve_location
from services.user.owner_write_guard import OwnerInactiveError, owner_write_guard
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
    _build_pair_action_link,
    _create_pair_link_record,
    _create_pair_record,
    _relay_stage_rank,
    _require_roles,
    _trusted_public_url,
)
from ._helpers import (
    _auto_escalate_overdue_statuses,
    _build_caregiver_message,
    _build_community_snapshot,
    _build_recent_series,
    _refresh_community_daily
)

logger = logging.getLogger(__name__)

_REQUIRED_HEAT_WEATHER_FIELDS = (
    'temperature',
    'temperature_max',
    'temperature_min',
    'humidity',
)
_WEATHER_WAITING_LABEL = '天气更新中'
ELDER_ACTION_LABELS = {
    'drink_water': '少量多次喝水',
    'avoid_noon': '避开中午外出',
    'cool_rest': '到凉快处休息',
    'keep_warm': '及时添衣保暖',
    'avoid_fall': '减少湿滑路面外出',
    'safe_heating': '安全使用取暖设备',
    'check_weather': '出门前看天气',
    'carry_water': '随身带水',
    'contact_family': '和家人报个平安',
}
_UNKNOWN_ELDER_ACTION_LABEL = '其他自护行动（旧版本记录）'


def _build_elder_action_labels(value):
    """把老人自报行动映射为安全展示文案，不回显未知原始值。"""
    parsed = safe_json_loads(value, [])
    if not isinstance(parsed, list):
        return []
    labels = []
    for item in parsed[:20]:
        if not isinstance(item, str) or not item.strip():
            continue
        label = ELDER_ACTION_LABELS.get(item.strip(), _UNKNOWN_ELDER_ACTION_LABEL)
        if label not in labels:
            labels.append(label)
    return labels


def _heat_weather_available(weather_data):
    """仅允许字段完整的真实和风天气进入热风险计算。"""
    if not is_qweather_online_weather(weather_data):
        return False
    for field in _REQUIRED_HEAT_WEATHER_FIELDS:
        try:
            value = float(weather_data.get(field))
        except (AttributeError, TypeError, ValueError):
            return False
        if not math.isfinite(value):
            return False
    return True


def _build_weather_waiting_message(pair, action_link):
    """天气不可用时只保留行动入口，不生成风险结论或风险建议。"""
    location = (pair.location_query or pair.community_code or '').strip()
    lines = [
        '【天气更新中】',
        '风险等级暂不显示。仍可打开行动页完成安全确认或求助。',
    ]
    if location:
        lines.append(f'地点：{location}')
    lines.append(f'（可选）行动页：{action_link}  短码：{pair.short_code}')
    return '\n'.join(lines)


def _load_heat_risk(location):
    """读取真实天气并计算热风险；任一步失败都返回不可用状态。"""
    weather_data, _ = get_weather_with_cache(location)
    if not _heat_weather_available(weather_data):
        return weather_data, None, None
    try:
        consecutive_hot_days = get_consecutive_hot_days(
            location,
            today_max=weather_data.get('temperature_max')
        )
        heat_result = HeatActionService().calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
    except Exception:
        logger.warning("真实天气热风险计算失败，已停止输出结论", exc_info=True)
        return weather_data, None, None
    risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
    return weather_data, heat_result, risk_label


def _create_pair_link(community_code):
    with atomic_transaction():
        link, token = _create_pair_link_record(
            caregiver_id=current_user.id,
            community_code=community_code,
            expires_after=timedelta(days=3),
            flush=True
        )
        log_security_event(
            action='short_code_generated',
            actor_id=getattr(current_user, 'id', None),
            actor_role=getattr(current_user, 'role', None),
            resource_type='pair_link',
            resource_id=str(link.id),
            extra_data={
                'location_scope': 'duchang_county',
                'community_code_length': len(community_code),
                'short_code_hash': link.short_code_hash
            }
        )
    session['pair_link_token'] = token
    session['pair_link_id'] = link.id
    return link, token


def _create_pair(location_query, member_id=None):
    """Create a Pair directly (pilot default: child creates without elder redemption)."""
    location_query = sanitize_input(location_query, max_length=200) or ''
    location_query = location_query.strip()
    if not location_query:
        raise ValueError('location_query is required')

    owner_user_id = int(current_user.id)
    with owner_write_guard(owner_user_id):
        # member_id 只能在 owner 锁内重新解析，避免成员删除后的陈旧关联。
        if member_id and FamilyMember.query.filter_by(
            id=member_id,
            user_id=owner_user_id,
        ).first() is None:
            member_id = None
        pair = _create_pair_record(
            caregiver_id=owner_user_id,
            location_query=location_query,
            member_id=member_id,
            flush=True
        )
        pair_id = int(pair.id)
        db.session.commit()
    # one-time banner
    session['created_pair_id'] = pair_id
    log_usage_event(
        'pair_created',
        user_id=owner_user_id,
        pair_id=pair_id,
        member_id=member_id,
        source='web',
        meta={'location_query': location_query},
    )
    # 锁外只返回稳定标量，避免账号注销后继续访问已被清理的 ORM 对象。
    return pair_id


def _active_pair_for_actor(pair_id, actor_user_id, actor_role):
    """按当前操作者权限读取 active Pair；写入前后均调用一次。"""
    query = Pair.query.filter_by(id=pair_id, status='active')
    if actor_role != 'admin':
        query = query.filter_by(caregiver_id=actor_user_id)
    return query.first_or_404()


def _load_created_pair():
    created_pair = None
    created_id = request.args.get('created', type=int)
    if created_id and session.get('created_pair_id') == created_id:
        created_pair = Pair.query.filter_by(
            id=created_id,
            caregiver_id=current_user.id,
            status='active',
        ).first()
        session.pop('created_pair_id', None)
    return created_pair


def _build_active_pair_action_links(owner_user_id, pair_ids):
    """在 owner 守卫内仅为当前 active Pair 创建或复用行动令牌。"""
    normalized_ids = sorted({int(pair_id) for pair_id in pair_ids if pair_id})
    if not normalized_ids:
        return {}
    links = {}
    with owner_write_guard(owner_user_id):
        pairs = db.session.execute(
            db.select(Pair)
            .where(
                Pair.id.in_(normalized_ids),
                Pair.caregiver_id == owner_user_id,
                Pair.status == 'active',
            )
            .order_by(Pair.id)
            .execution_options(populate_existing=True)
        ).scalars().all()
        for pair in pairs:
            links[pair.id] = _build_pair_action_link(pair)
        db.session.commit()
    return links


def _build_pair_management_context(caregiver_mode=False):
    created_pair = _load_created_pair()
    status_date = today_local()
    pairs = Pair.query.filter_by(
        caregiver_id=current_user.id,
        status='active',
    ).order_by(Pair.created_at.desc()).all()
    communities = Community.query.order_by(Community.name).all()
    family_members = []
    try:
        family_members = FamilyMember.query.filter_by(user_id=current_user.id).order_by(
            FamilyMember.created_at.desc()
        ).all()
    except Exception:
        db.session.rollback()
        logger.warning("加载家庭成员失败，已降级为空列表", exc_info=True)
    pair_ids = [pair.id for pair in pairs]
    status_map = {}
    if pair_ids:
        statuses = DailyStatus.query.filter(
            DailyStatus.pair_id.in_(pair_ids),
            DailyStatus.status_date == status_date
        ).all()
        _auto_escalate_overdue_statuses(statuses, status_date)
        status_map = {status.pair_id: status for status in statuses}
    action_links = _build_active_pair_action_links(
        int(current_user.id),
        pair_ids,
    )
    created_action_link = action_links.get(created_pair.id) if created_pair else None

    # Resolve per-location once (supports arbitrary CN input via AMap)
    location_meta = {}
    weather_by_code = {}
    if pairs:
        for pair in pairs:
            label = (pair.location_query or pair.community_code or '').strip()
            resolved = resolve_location(label)
            code = resolved.get('location_code') or ''
            if not code:
                continue
            if code not in location_meta:
                location_meta[code] = resolved
        for code in list(location_meta.keys()):
            try:
                weather_data, _ = get_weather_with_cache(code)
                weather_by_code[code] = weather_data or {}
            except Exception:
                weather_by_code[code] = {}
                logger.warning("加载天气缓存失败，code=%s", code, exc_info=True)

    pair_cards = []
    now = utcnow()
    # 计算本地时间晚上8点的 UTC 时间
    local_deadline = datetime.combine(status_date, datetime.min.time()).replace(hour=20)
    deadline = local_datetime_to_utc(local_deadline)
    member_map = {}
    member_ids = [p.member_id for p in pairs if getattr(p, 'member_id', None)]
    if member_ids:
        try:
            members = FamilyMember.query.filter(
                FamilyMember.user_id == current_user.id,
                FamilyMember.id.in_(member_ids)
            ).all()
            member_map = {m.id: m for m in members}
        except Exception:
            db.session.rollback()
            logger.warning("加载成员映射失败，已降级为空映射", exc_info=True)

    heat_service = HeatActionService()

    for pair in pairs:
        status = status_map.get(pair.id)

        label = (pair.location_query or pair.community_code or '').strip()
        resolved = resolve_location(label)
        code = resolved.get('location_code') or ''
        display_name = resolved.get('display_name') or label or code
        weather_data = weather_by_code.get(code, {}) if code else {}

        weather_available = _heat_weather_available(weather_data)
        risk_label = _WEATHER_WAITING_LABEL
        heat_result = {}
        if weather_available:
            try:
                consecutive_hot_days = get_consecutive_hot_days(
                    code or normalize_location_name(pair.community_code),
                    today_max=weather_data.get('temperature_max')
                )
                heat_result = heat_service.calculate_heat_risk(
                    weather_data,
                    consecutive_hot_days=consecutive_hot_days
                )
                risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
            except Exception:
                weather_available = False
                heat_result = {}
                risk_label = _WEATHER_WAITING_LABEL
                logger.warning("真实天气热风险计算失败，已停止输出结论", exc_info=True)

        # Pilot alert label (heat/cold threshold)
        alert_kind = None
        alert_label = '天气待更新'
        if weather_available:
            tmax = weather_data.get('temperature_max')
            tmin = weather_data.get('temperature_min')
            if tmax is not None and float(tmax) >= 35:
                alert_kind = 'heat'
                alert_label = f'高温（{float(tmax):.0f}°C）'
            elif tmin is not None and float(tmin) <= 5:
                alert_kind = 'cold'
                alert_label = f'寒潮（{float(tmin):.0f}°C）'
            else:
                alert_label = '暂无预警'

        confirmed = bool(status and status.confirmed_at)
        is_overdue = bool(now >= deadline and not confirmed)
        relay_stage = status.relay_stage if status else None
        relay_stage_label = None
        if relay_stage and relay_stage != 'none':
            relay_stage_label = RELAY_STAGE_LABELS.get(relay_stage, relay_stage)
        member = member_map.get(pair.member_id) if getattr(pair, 'member_id', None) else None
        action_link = action_links.get(pair.id)
        reminder_message = (
            _build_caregiver_message(
                pair,
                alert_kind=alert_kind,
                weather_data=weather_data,
                member=member,
                action_link=action_link
            )
            if weather_available
            else _build_weather_waiting_message(pair, action_link)
        )
        pair_cards.append({
            'pair': pair,
            'status': status,
            'risk_label': risk_label,
            'heat_result': heat_result,
            'weather_available': weather_available,
            'alert_kind': alert_kind,
            'alert_label': alert_label,
            'location_display': display_name,
            'temperature_max': weather_data.get('temperature_max') if weather_available else None,
            'temperature_min': weather_data.get('temperature_min') if weather_available else None,
            'elder_name': (member.name if member else None),
            'action_link': action_link,
            'reminder_message': reminder_message,
            'help_flag': bool(status and status.help_flag),
            'is_overdue': is_overdue,
            'relay_stage': relay_stage,
            'relay_stage_label': relay_stage_label
        })

    wxpusher_feature_enabled = bool(
        current_app.config.get('FEATURE_WXPUSHER', False)
    )
    push_channel_ready = bool(
        wxpusher_feature_enabled
        and (current_app.config.get('WXPUSHER_APP_TOKEN') or '').strip()
    )
    context = {
        'created_pair': created_pair,
        'created_action_link': created_action_link,
        'pairs': pairs,
        'pair_cards': pair_cards,
        'status_map': status_map,
        'communities': communities,
        'family_members': family_members,
        'status_date': status_date,
        'wxpusher_feature_enabled': wxpusher_feature_enabled,
        'push_channel_ready': push_channel_ready,
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
        location_query = sanitize_input(request.form.get('location_query'), max_length=200)
        if not location_query:
            # backward-compatible name
            location_query = sanitize_input(request.form.get('community_code'), max_length=100)
        location_query = (location_query or '').strip()
        if not location_query:
            flash('请填写老人所在地（支持任意中文地点）', 'error')
            return redirect(url_for('user.pair_management'))

        member_id = request.form.get('member_id')
        try:
            member_id = int(member_id) if member_id and str(member_id).strip() else None
        except (TypeError, ValueError):
            member_id = None
        try:
            pair_id = _create_pair(location_query, member_id=member_id)
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except Exception:
            logger.warning(
                "创建绑定失败(owner_user_id=%s location_len=%s)",
                getattr(current_user, 'id', None),
                len(location_query),
                exc_info=True,
            )
            flash('创建失败，请检查输入后重试。', 'error')
            return redirect(url_for('user.pair_management'))
        return redirect(url_for('user.pair_management', created=pair_id))

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
    if is_guest_user(current_user):
        flash('游客模式无法创建绑定', 'error')
        return redirect(url_for('user.caregiver_dashboard'))

    location_query = sanitize_input(request.form.get('location_query'), max_length=200)
    if not location_query:
        location_query = sanitize_input(request.form.get('community_code'), max_length=100)
    location_query = (location_query or '').strip()
    if not location_query:
        flash('请填写老人所在地（支持任意中文地点）', 'error')
        return redirect(url_for('user.caregiver_dashboard'))

    member_id = request.form.get('member_id')
    try:
        member_id = int(member_id) if member_id and str(member_id).strip() else None
    except (TypeError, ValueError):
        member_id = None
    try:
        pair_id = _create_pair(location_query, member_id=member_id)
    except OwnerInactiveError:
        flash('账号已失效，请重新登录。', 'error')
        return redirect(url_for('public.login'))
    except Exception:
        logger.warning(
            "照护端创建绑定失败(owner_user_id=%s location_len=%s)",
            getattr(current_user, 'id', None),
            len(location_query),
            exc_info=True,
        )
        flash('创建失败，请检查输入后重试。', 'error')
        return redirect(url_for('user.caregiver_dashboard'))
    return redirect(url_for('user.caregiver_dashboard', created=pair_id))


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
    action_link = None
    if pair.status == 'active':
        action_link = _build_active_pair_action_links(
            int(pair.caregiver_id),
            [pair.id],
        ).get(pair.id)
        # owner 守卫会结束旧读事务，重新加载详情快照。
        pair = query.first_or_404()

    status_date = today_local()
    status_today = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first()
    recent_statuses = DailyStatus.query.filter_by(pair_id=pair.id).order_by(
        DailyStatus.status_date.desc()
    ).limit(7).all()
    recent_series = _build_recent_series(pair.id, days=7)
    debrief_today = Debrief.query.filter_by(pair_id=pair.id, date=status_date).first()
    community_snapshot = _build_community_snapshot(pair.community_code, status_date)
    wechat_template_url = None
    if action_link:
        wechat_template_url = url_for(
            'user.caregiver_wechat_template',
            short_code=pair.short_code,
            token=action_link.rsplit('/e/', 1)[-1].split('?', 1)[0] if '/e/' in action_link else None,
            community_code=pair.community_code
        )
    actions_today = safe_json_loads(status_today.caregiver_actions, []) if status_today else []
    if not isinstance(actions_today, list):
        actions_today = []
    elder_actions_today = _build_elder_action_labels(
        status_today.elder_actions if status_today else None
    )
    caregiver_note = status_today.caregiver_note if status_today else None

    return render_template(
        'caregiver_pair_detail.html',
        pair=pair,
        action_link=action_link,
        status_today=status_today,
        recent_statuses=recent_statuses,
        recent_series=recent_series,
        debrief_today=debrief_today,
        community_snapshot=community_snapshot,
        status_date=status_date,
        wechat_template_url=wechat_template_url,
        action_options=CARE_ACTION_OPTIONS,
        actions_today=actions_today,
        elder_actions_today=elder_actions_today,
        caregiver_note=caregiver_note
    )


def caregiver_action_log(pair_id):
    """照护行动记录"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))
    if is_guest_user(current_user):
        flash('游客模式无法记录行动', 'error')
        return redirect(url_for('user.user_dashboard'))

    actor_user_id = int(current_user.id)
    actor_role = getattr(current_user, 'role', None)
    initial_pair = _active_pair_for_actor(pair_id, actor_user_id, actor_role)
    owner_user_id = int(initial_pair.caregiver_id)
    status_date = today_local()
    prepared_location = normalize_location_name(
        initial_pair.location_query or initial_pair.community_code
    )
    prepared_risk_label = None
    if DailyStatus.query.filter_by(pair_id=pair_id, status_date=status_date).first() is None:
        location = prepared_location
        _weather_data, _heat_result, risk_label = _load_heat_risk(location)
        prepared_risk_label = risk_label

    allowed_actions = {item['id'] for item in CARE_ACTION_OPTIONS}
    actions = [item for item in request.form.getlist('caregiver_actions') if item in allowed_actions]
    note = sanitize_input(request.form.get('caregiver_note'), max_length=300)
    weather_waiting = False
    try:
        with owner_write_guard(owner_user_id):
            pair = _active_pair_for_actor(pair_id, actor_user_id, actor_role)
            status = DailyStatus.query.filter_by(
                pair_id=pair.id,
                status_date=status_date,
            ).first()
            if not status:
                current_location = normalize_location_name(
                    pair.location_query or pair.community_code
                )
                risk_label = (
                    prepared_risk_label
                    if current_location == prepared_location
                    else None
                )
                weather_waiting = risk_label is None
                status = DailyStatus(
                    pair_id=pair.id,
                    status_date=status_date,
                    community_code=pair.community_code,
                    risk_level=risk_label
                )
                db.session.add(status)
            status.caregiver_actions = json_or_none(actions)
            status.caregiver_note = note or None
            logged_pair_id = int(pair.id)
            logged_member_id = pair.member_id
            db.session.commit()
    except OwnerInactiveError:
        if actor_user_id == owner_user_id:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        flash('照护关系所属账号已失效，无法记录行动。', 'error')
        return redirect(url_for('user.caregiver_dashboard'))
    except (OSError, RuntimeError, ValueError):
        db.session.rollback()
        logger.exception('照护行动授权锁不可用，记录未保存')
        flash('行动记录暂时无法保存，请稍后重试。', 'error')
        return redirect(url_for('user.caregiver_pair_detail', pair_id=pair_id))
    log_usage_event(
        'feedback_submitted',
        user_id=actor_user_id,
        pair_id=logged_pair_id,
        member_id=logged_member_id,
        source='web',
        meta={'caregiver_actions_count': len(actions), 'has_note': bool(note)},
    )
    if weather_waiting:
        flash('行动记录已保存。天气更新后会补充今日风险。', 'warning')
    else:
        flash('行动记录已保存。', 'success')
    return redirect(url_for('user.caregiver_pair_detail', pair_id=logged_pair_id))


def caregiver_wechat_template():
    """照护人微信模板"""
    if not _require_roles('caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    short_code = sanitize_input(request.args.get('short_code'), max_length=12)
    token = sanitize_input(request.args.get('token'), max_length=200)
    community_code = sanitize_input(request.args.get('community_code'), max_length=100)

    if not token and short_code:
        pair_query = Pair.query.filter_by(short_code=short_code, status='active')
        if getattr(current_user, 'role', None) != 'admin':
            pair_query = pair_query.filter_by(caregiver_id=current_user.id)
        pair = pair_query.first()
        if pair:
            action_link = _build_active_pair_action_links(
                int(pair.caregiver_id),
                [pair.id],
            ).get(pair.id)
            if action_link:
                token = (
                    action_link.rsplit('/e/', 1)[-1].split('?', 1)[0]
                    if '/e/' in action_link
                    else None
                )

    if token:
        action_link = _trusted_public_url(
            'public.elder_token_entry',
            token=token,
            short_code=short_code,
        )
    else:
        action_link = _trusted_public_url(
            'public.elder_entry',
            short_code=short_code,
        )

    risk_label = None
    actions = []
    weather_data = None
    weather_available = False
    if community_code:
        location = normalize_location_name(community_code)
        weather_data, _heat_result, risk_label = _load_heat_risk(location)
        weather_available = risk_label is not None
        if weather_available:
            actions = _action_plan(risk_label)

    if weather_available:
        message_lines = [
            '【高温行动提醒】',
            f'行动链接：{action_link}',
            f'短码：{short_code or "请填写"}'
        ]
        if community_code:
            message_lines.insert(1, f'社区：{community_code}')
        message_lines.append(f'今日热风险：{risk_label}')
        message_lines.append('行动建议（非医疗诊断/治疗）：')
        for item in actions:
            message_lines.append(f'- {item["title"]}：{item["detail"]}')
        message_lines.append('如需帮助请在页面内点击“我需要帮助”。')
    else:
        message_lines = [
            '【天气更新中】',
            '风险等级暂不显示。仍可打开行动页完成安全确认或求助。',
            f'行动链接：{action_link}',
            f'短码：{short_code or "请填写"}',
        ]

    return render_template(
        'caregiver_wechat_template.html',
        message='\n'.join(message_lines),
        action_link=action_link,
        short_code=short_code,
        community_code=community_code,
        weather=weather_data,
        weather_available=weather_available,
    )


def _handle_pair_escalate(pair_id, redirect_url, target_stage=None):
    if is_guest_user(current_user):
        flash('游客模式无法升级', 'error')
        return redirect(url_for('user.user_dashboard'))

    if target_stage and target_stage not in RELAY_STAGE_ORDER:
        flash('升级阶段无效', 'error')
        return redirect(redirect_url)

    actor_user_id = int(current_user.id)
    actor_role = getattr(current_user, 'role', None)
    initial_pair = _active_pair_for_actor(pair_id, actor_user_id, actor_role)
    owner_user_id = int(initial_pair.caregiver_id)
    status_date = today_local()
    prepared_location = normalize_location_name(
        initial_pair.location_query or initial_pair.community_code
    )
    prepared_risk_label = None
    if DailyStatus.query.filter_by(pair_id=pair_id, status_date=status_date).first() is None:
        location = prepared_location
        _weather_data, _heat_result, risk_label = _load_heat_risk(location)
        prepared_risk_label = risk_label

    try:
        with owner_write_guard(owner_user_id):
            pair = _active_pair_for_actor(pair_id, actor_user_id, actor_role)
            status = DailyStatus.query.filter_by(
                pair_id=pair.id,
                status_date=status_date,
            ).first()
            if not status:
                current_location = normalize_location_name(
                    pair.location_query or pair.community_code
                )
                risk_label = (
                    prepared_risk_label
                    if current_location == prepared_location
                    else None
                )
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
            community_code = pair.community_code
            db.session.commit()
    except OwnerInactiveError:
        if actor_user_id == owner_user_id:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        flash('照护关系所属账号已失效，无法升级。', 'error')
        return redirect(redirect_url)
    except (OSError, RuntimeError, ValueError):
        db.session.rollback()
        logger.exception('升级链授权锁不可用，本次升级未保存')
        flash('升级暂时无法保存，请稍后重试。', 'error')
        return redirect(redirect_url)
    _refresh_community_daily(community_code, status_date)
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
