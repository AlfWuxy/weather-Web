# -*- coding: utf-8 -*-
"""Community-related routes."""
from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from core.db_models import Community, CommunityDaily, CoolingResource, DailyStatus, Debrief, Pair
from core.time_utils import now_local, today_local
from core.weather import get_consecutive_hot_days, get_weather_with_cache, is_demo_mode, normalize_location_name
from services.heat_action_service import HeatActionService
from utils.validators import sanitize_input

from ._common import (
    ANNOUNCE_DISCLAIMER_LINES,
    ANNOUNCE_SOURCE_LINES,
    AUTO_ESCALATE_STAGE,
    HEAT_RISK_LABELS,
    _action_plan,
    _normalize_code,
    _relay_stage_rank,
    _require_roles
)
from ._helpers import (
    _auto_escalate_overdue_statuses,
    _build_announce_message,
    _build_community_message,
    _build_community_snapshot,
    _build_outreach_suggestions,
    _build_risk_counts,
    _community_access_allowed,
    _ensure_demo_statuses
)


def community_dashboard():
    """社区工作台"""
    if not _require_roles('community', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    status_date = today_local()
    if getattr(current_user, 'role', None) == 'admin':
        communities = Community.query.order_by(Community.name).all()
    else:
        community_code = _normalize_code(getattr(current_user, 'community', None))
        if not community_code:
            flash('请先设置所属社区', 'error')
            return redirect(url_for('user.user_dashboard'))
        communities = Community.query.filter_by(name=community_code).all()

    if is_demo_mode():
        for comm in communities:
            _ensure_demo_statuses(comm.name, status_date, caregiver_id=current_user.id)

    community_codes = [comm.name for comm in communities]
    statuses_by_comm = {code: [] for code in community_codes}
    community_daily_by_comm = {code: None for code in community_codes}
    resources_by_comm = {code: [] for code in community_codes}
    if community_codes:
        statuses = DailyStatus.query.filter(
            DailyStatus.community_code.in_(community_codes),
            DailyStatus.status_date == status_date
        ).all()
        _auto_escalate_overdue_statuses(statuses, status_date)
        for status in statuses:
            statuses_by_comm.setdefault(status.community_code, []).append(status)

        community_dailies = CommunityDaily.query.filter(
            CommunityDaily.community_code.in_(community_codes),
            CommunityDaily.date == status_date
        ).all()
        for record in community_dailies:
            community_daily_by_comm[record.community_code] = record

        resources = CoolingResource.query.filter(
            CoolingResource.community_code.in_(community_codes),
            CoolingResource.is_active == True
        ).all()
        for resource in resources:
            resources_by_comm.setdefault(resource.community_code, []).append(resource)

    heat_service = HeatActionService()
    snapshots = []
    for comm in communities:
        statuses = statuses_by_comm.get(comm.name, [])
        snapshot = _build_community_snapshot(
            comm.name,
            status_date,
            record=community_daily_by_comm.get(comm.name),
            statuses=statuses
        )
        risk_counts, confirmed_counts = _build_risk_counts(statuses)
        confirmed_total = sum(confirmed_counts.values())
        help_count = sum(1 for s in statuses if s.help_flag)
        escalation_count = sum(
            1 for s in statuses if _relay_stage_rank(s.relay_stage) >= _relay_stage_rank(AUTO_ESCALATE_STAGE)
        )
        total_people = snapshot.get('total_people', 0)
        help_rate = (help_count / total_people) if total_people else 0

        location = normalize_location_name(comm.name)
        weather_data, _ = get_weather_with_cache(location)
        consecutive_hot_days = get_consecutive_hot_days(
            location,
            today_max=weather_data.get('temperature_max')
        )
        heat_result = heat_service.calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
        risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
        resources = resources_by_comm.get(comm.name, [])
        outreach_suggestions = _build_outreach_suggestions(
            snapshot.get('total_people', 0),
            confirmed_total,
            help_count,
            escalation_count,
            snapshot.get('risk_distribution', {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0})
        )
        snapshots.append({
            'community': comm,
            **snapshot,
            'risk_counts': risk_counts,
            'confirmed_counts': confirmed_counts,
            'confirmed_total': confirmed_total,
            'help_count': help_count,
            'escalation_count': escalation_count,
            'help_rate': round(help_rate, 4),
            'flag_count': escalation_count,
            'risk_label': risk_label,
            'outreach_suggestions': outreach_suggestions,
            'group_message': _build_community_message(comm.name, risk_label, resources)
        })

    return render_template(
        'community_dashboard.html',
        snapshots=snapshots,
        status_date=status_date
    )


def community_detail(community_code):
    """社区详情"""
    if not _require_roles('community', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    community_code = _normalize_code(community_code)
    if not community_code or not _community_access_allowed(community_code):
        flash('无权访问该社区', 'error')
        return redirect(url_for('user.community_dashboard'))

    community = Community.query.filter_by(name=community_code).first_or_404()
    status_date = today_local()
    is_admin = getattr(current_user, 'role', None) == 'admin'
    snapshot = _build_community_snapshot(community_code, status_date)
    statuses = DailyStatus.query.filter_by(
        community_code=community_code,
        status_date=status_date
    ).order_by(DailyStatus.updated_at.desc()).all()
    pair_map = {}
    if is_admin:
        pair_ids = {status.pair_id for status in statuses}
        pairs = Pair.query.filter(Pair.id.in_(pair_ids)).all() if pair_ids else []
        pair_map = {pair.id: pair for pair in pairs}

    risk_counts, confirmed_counts = _build_risk_counts(statuses)
    confirmed_total = sum(confirmed_counts.values())
    help_count = sum(1 for s in statuses if s.help_flag)
    escalation_count = sum(
        1 for s in statuses if _relay_stage_rank(s.relay_stage) >= _relay_stage_rank(AUTO_ESCALATE_STAGE)
    )

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

    debrief_total = Debrief.query.filter_by(
        community_code=community_code,
        date=status_date
    ).count()
    debrief_optin = Debrief.query.filter(
        Debrief.community_code == community_code,
        Debrief.date == status_date,
        Debrief.pair_id.isnot(None)
    ).count()
    resources = CoolingResource.query.filter_by(
        community_code=community_code,
        is_active=True
    ).all()
    outreach_suggestions = _build_outreach_suggestions(
        snapshot.get('total_people', 0),
        confirmed_total,
        help_count,
        escalation_count,
        snapshot.get('risk_distribution', {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0})
    )

    return render_template(
        'community_detail.html',
        community=community,
        snapshot=snapshot,
        statuses=statuses,
        pair_map=pair_map,
        debrief_total=debrief_total,
        debrief_optin=debrief_optin,
        resources=resources,
        risk_counts=risk_counts,
        confirmed_counts=confirmed_counts,
        risk_label=risk_label,
        outreach_suggestions=outreach_suggestions,
        group_message=_build_community_message(community_code, risk_label, resources),
        status_date=status_date
    )


def community_wechat(community_code):
    """社区微信模板"""
    if not _require_roles('community', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    community_code = _normalize_code(community_code)
    if not community_code or not _community_access_allowed(community_code):
        flash('无权访问该社区', 'error')
        return redirect(url_for('user.community_dashboard'))

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
    resources = CoolingResource.query.filter_by(
        community_code=community_code,
        is_active=True
    ).all()

    message_lines = [
        '【社区高温行动提醒】',
        f'社区：{community_code}',
        f'今日热风险：{risk_label}',
        '行动建议（非医疗诊断/治疗）：'
    ]
    for item in actions:
        message_lines.append(f'- {item["title"]}：{item["detail"]}')
    if resources:
        message_lines.append('附近避暑点可参考：')
        for item in resources[:3]:
            name_line = f'- {item.name}'
            if item.address_hint:
                name_line += f'（{item.address_hint}）'
            message_lines.append(name_line)
    message_lines.append('如需帮助请联系社区服务人员。')

    return render_template(
        'community_wechat.html',
        message='\n'.join(message_lines),
        community_code=community_code,
        risk_label=risk_label,
        actions=actions,
        resources=resources
    )


def community_announce():
    """公共传播包生成器"""
    if not _require_roles('community', 'caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    community_code = sanitize_input(request.args.get('community'), max_length=100)
    if not community_code:
        community_code = getattr(current_user, 'community', None)
    location = normalize_location_name(community_code)
    display_location = community_code or location
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
    updated_at = now_local()

    messages = {
        'elder': _build_announce_message(
            '高温提醒｜老人版',
            display_location,
            risk_label,
            actions,
            extra_lines=['如有不适请及时联系家人或社区。'],
            updated_at=updated_at
        ),
        'caregiver': _build_announce_message(
            '高温提醒｜家属照护版',
            display_location,
            risk_label,
            actions,
            extra_lines=['请联系老人确认状态，提醒补水与避暑。'],
            updated_at=updated_at
        ),
        'community': _build_announce_message(
            '社区高温行动提醒｜社区版',
            display_location,
            risk_label,
            actions,
            extra_lines=['请优先关注高风险家庭与未确认对象。'],
            updated_at=updated_at
        )
    }

    return render_template(
        'community_announce.html',
        messages=messages,
        location=display_location,
        risk_label=risk_label,
        updated_at=updated_at,
        disclaimer_lines=ANNOUNCE_DISCLAIMER_LINES,
        source_lines=ANNOUNCE_SOURCE_LINES
    )


def community_risk():
    """社区风险地图"""
    coords_map = current_app.config.get('COMMUNITY_COORDS_GCJ', {})
    communities = Community.query.all()
    # 转换为字典列表，避免JSON序列化错误
    communities_data = []
    for comm in communities:
        coords = coords_map.get(comm.name)
        if coords and len(coords) == 2:
            longitude, latitude = coords[0], coords[1]
        else:
            longitude, latitude = comm.longitude, comm.latitude
        communities_data.append({
            'id': comm.id,
            'name': comm.name,
            'location': comm.location,
            'latitude': latitude,
            'longitude': longitude,
            'population': comm.population,
            'elderly_ratio': comm.elderly_ratio,
            'chronic_disease_ratio': comm.chronic_disease_ratio,
            'vulnerability_index': comm.vulnerability_index,
            'risk_level': comm.risk_level
        })
    return render_template('community_risk.html',
                           communities=communities_data,
                           community_coords=coords_map)
