# -*- coding: utf-8 -*-
"""Community-related routes."""
import logging

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from core.db_models import Community, CommunityDaily, CoolingResource, DailyStatus, Debrief, MedicalRecord, Pair
from core.guest import is_guest_user
from core.time_utils import now_local, today_local
from core.weather import (
    get_consecutive_hot_days,
    get_weather_with_cache,
    is_heat_action_weather_ready,
    is_qweather_production_ready,
    normalize_location_name,
    weather_source_label,
)
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
)

logger = logging.getLogger(__name__)

_WEATHER_WAITING_LABEL = '天气更新中'


def _heat_weather_available(weather_data):
    """社区基础温湿热行动允许字段完整的 QWeather 或 Open-Meteo 实况。"""
    return is_heat_action_weather_ready(weather_data)


def _load_heat_risk(location):
    """读取真实天气并计算热风险；失败时不输出风险结论。"""
    weather_data, _ = get_weather_with_cache(location)
    if not _heat_weather_available(weather_data):
        return weather_data, None, None
    try:
        consecutive_hot_days = get_consecutive_hot_days(
            location,
            today_max=weather_data.get('temperature_max'),
            weather_data=weather_data,
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


def _weather_publication_state(weather_data, risk_label):
    """本地基础热可放宽展示，公共传播仅允许新鲜完整的和风实况。"""
    source_label = weather_source_label(weather_data) or '暂无可用来源'
    share_ready = bool(risk_label and is_qweather_production_ready(weather_data))
    return source_label, share_ready


def _build_public_community_message(community_code, risk_label, resources, source_label):
    """生成带动态天气来源的社区转发文本。"""
    message = _build_community_message(community_code, risk_label, resources)
    return f'{message}\n天气来源：{source_label}实况。'


def community_dashboard():
    """社区工作台"""
    if not _require_roles('community', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    status_date = today_local()
    if getattr(current_user, 'role', None) == 'admin':
        communities = Community.query.order_by(Community.name).all()
    else:
        # P10：看板范围跟 ACL 字段，不跟可自改定位
        from services.user._helpers import _user_acl_community
        community_code = _user_acl_community(current_user)
        if not community_code:
            flash('请先由管理员设置管辖社区', 'error')
            return redirect(url_for('user.user_dashboard'))
        communities = Community.query.filter_by(name=community_code).all()

    community_codes = [comm.name for comm in communities]
    statuses_by_comm = {code: [] for code in community_codes}
    community_daily_by_comm = {code: None for code in community_codes}
    resources_by_comm = {code: [] for code in community_codes}
    if community_codes:
        statuses = DailyStatus.query.join(
            Pair,
            Pair.id == DailyStatus.pair_id,
        ).filter(
            DailyStatus.community_code.in_(community_codes),
            DailyStatus.status_date == status_date,
            Pair.status == 'active',
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

    snapshots = []
    for comm in communities:
        statuses = statuses_by_comm.get(comm.name, [])
        snapshot = _build_community_snapshot(
            comm.name,
            status_date,
            record=community_daily_by_comm.get(comm.name),
            statuses=statuses
        )
        risk_statuses = [
            status for status in statuses
            if status.risk_level in HEAT_RISK_LABELS.values()
        ]
        risk_counts, confirmed_counts = _build_risk_counts(risk_statuses)
        confirmed_total = sum(1 for status in statuses if status.confirmed_at)
        help_count = sum(1 for s in statuses if s.help_flag)
        escalation_count = sum(
            1 for s in statuses if _relay_stage_rank(s.relay_stage) >= _relay_stage_rank(AUTO_ESCALATE_STAGE)
        )
        total_people = snapshot.get('total_people', 0)
        help_rate = (help_count / total_people) if total_people else 0

        location = normalize_location_name(comm.name)
        weather_data, _heat_result, risk_label = _load_heat_risk(location)
        source_label, public_share_ready = _weather_publication_state(weather_data, risk_label)
        weather_available = risk_label is not None
        if not weather_available:
            risk_label = _WEATHER_WAITING_LABEL
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
            'weather_available': weather_available,
            'weather_source_label': source_label,
            'public_share_ready': public_share_ready,
            'outreach_suggestions': outreach_suggestions,
            'group_message': (
                _build_public_community_message(comm.name, risk_label, resources, source_label)
                if public_share_ready else None
            )
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
    statuses = DailyStatus.query.join(
        Pair,
        Pair.id == DailyStatus.pair_id,
    ).filter(
        DailyStatus.community_code == community_code,
        DailyStatus.status_date == status_date,
        Pair.status == 'active',
    ).order_by(DailyStatus.updated_at.desc()).all()
    pair_map = {}
    if is_admin:
        pair_ids = {status.pair_id for status in statuses}
        pairs = Pair.query.filter(Pair.id.in_(pair_ids)).all() if pair_ids else []
        pair_map = {pair.id: pair for pair in pairs}

    risk_statuses = [
        status for status in statuses
        if status.risk_level in HEAT_RISK_LABELS.values()
    ]
    risk_counts, confirmed_counts = _build_risk_counts(risk_statuses)
    confirmed_total = sum(1 for status in statuses if status.confirmed_at)
    help_count = sum(1 for s in statuses if s.help_flag)
    escalation_count = sum(
        1 for s in statuses if _relay_stage_rank(s.relay_stage) >= _relay_stage_rank(AUTO_ESCALATE_STAGE)
    )

    location = normalize_location_name(community_code)
    weather_data, _heat_result, risk_label = _load_heat_risk(location)
    source_label, public_share_ready = _weather_publication_state(weather_data, risk_label)
    weather_available = risk_label is not None
    if not weather_available:
        risk_label = _WEATHER_WAITING_LABEL

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
        confirmed_total=confirmed_total,
        help_count=help_count,
        escalation_count=escalation_count,
        risk_label=risk_label,
        # 详情模板里的天气开关同时控制群发复制，必须使用公共传播门。
        weather_available=public_share_ready,
        weather_source_label=source_label,
        public_share_ready=public_share_ready,
        outreach_suggestions=outreach_suggestions,
        group_message=(
            _build_public_community_message(community_code, risk_label, resources, source_label)
            if public_share_ready else None
        ),
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
    weather_data, _heat_result, risk_label = _load_heat_risk(location)
    source_label, public_share_ready = _weather_publication_state(weather_data, risk_label)
    weather_available = risk_label is not None
    actions = _action_plan(risk_label) if weather_available else []
    resources = CoolingResource.query.filter_by(
        community_code=community_code,
        is_active=True
    ).all()

    message_lines = []
    if public_share_ready:
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
        message_lines.append(f'天气来源：{source_label}实况。')
        message_lines.append('如需帮助请联系社区服务人员。')

    return render_template(
        'community_wechat.html',
        message='\n'.join(message_lines),
        community_code=community_code,
        risk_label=risk_label if weather_available else _WEATHER_WAITING_LABEL,
        weather_available=weather_available,
        weather_source_label=source_label,
        public_share_ready=public_share_ready,
        actions=actions,
        resources=resources
    )


def community_announce():
    """公共传播包生成器"""
    if not _require_roles('community', 'caregiver', 'admin'):
        return redirect(url_for('user.user_dashboard'))

    requested_community = sanitize_input(request.args.get('community'), max_length=100)
    if not requested_community:
        # P10：默认落到 ACL 管辖村，不跟可自改定位
        from services.user._helpers import _user_acl_community
        requested_community = _user_acl_community(current_user)
    community_code = _normalize_code(requested_community)
    if not community_code or not _community_access_allowed(community_code):
        # announce 允许 caregiver，但非管理员仍只能访问所属社区。
        flash('无权访问该社区', 'error')
        if getattr(current_user, 'role', None) in ('community', 'admin'):
            return redirect(url_for('user.community_dashboard'))
        return redirect(url_for('user.user_dashboard'))

    location = normalize_location_name(community_code)
    display_location = community_code or location
    weather_data, _heat_result, risk_label = _load_heat_risk(location)
    source_label, public_share_ready = _weather_publication_state(weather_data, risk_label)
    weather_available = risk_label is not None
    actions = _action_plan(risk_label) if weather_available else []
    updated_at = now_local()

    messages = {}
    if public_share_ready:
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
        risk_label=risk_label if weather_available else _WEATHER_WAITING_LABEL,
        weather_available=weather_available,
        weather_source_label=source_label,
        public_share_ready=public_share_ready,
        updated_at=updated_at,
        disclaimer_lines=ANNOUNCE_DISCLAIMER_LINES,
        source_lines=[
            f'当前天气来源：{source_label}。',
            '公开传播门：仅新鲜且字段完整的和风天气实况可生成转发内容。',
            *ANNOUNCE_SOURCE_LINES[1:],
        ]
    )


def community_risk():
    """社区风险地图"""
    coords_map = current_app.config.get('COMMUNITY_COORDS_GCJ', {})
    communities = Community.query.all()
    guest_view = is_guest_user(current_user)
    disease_options = []
    if not guest_view:
        disease_options = [
            row[0] for row in (
                MedicalRecord.query.with_entities(MedicalRecord.disease_category)
                .filter(MedicalRecord.disease_category.isnot(None))
                .distinct()
                .order_by(MedicalRecord.disease_category)
                .all()
            ) if row[0]
        ]

    # 转换为字典列表，避免JSON序列化错误
    communities_data = []
    for comm in communities:
        coords = coords_map.get(comm.name)
        if coords and len(coords) == 2:
            longitude, latitude = coords[0], coords[1]
        else:
            longitude, latitude = comm.longitude, comm.latitude
        item = {
            'name': comm.name,
            'latitude': latitude,
            'longitude': longitude,
            'risk_level': comm.risk_level
        }
        if not guest_view:
            item.update({
                'id': comm.id,
                'location': comm.location,
                'population': comm.population,
                'elderly_ratio': comm.elderly_ratio,
                'chronic_disease_ratio': comm.chronic_disease_ratio,
                'vulnerability_index': comm.vulnerability_index,
            })
        communities_data.append(item)
    return render_template('community_risk.html',
                           communities=communities_data,
                           community_coords=coords_map,
                           disease_options=disease_options,
                           community_data_redacted=guest_view,
                           default_analysis_date=today_local().isoformat(),
                           default_window_days=30)
