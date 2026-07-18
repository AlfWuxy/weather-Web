# -*- coding: utf-8 -*-
"""User-facing helper utilities."""
import json
from datetime import timedelta

from flask_login import current_user

from core.extensions import db
from core.security import hash_short_code
from core.time_utils import now_local, today_local, utcnow, ensure_utc_aware
from core.weather import is_demo_mode
from core.db_models import DailyStatus, Pair
from services.community_daily_service import (
    build_community_household_metrics,
    outreach_summary,
    refresh_community_daily as _refresh_community_daily,
)
from utils.parsers import safe_json_loads

from ._common import (
    ANNOUNCE_DISCLAIMER_LINES,
    ANNOUNCE_SOURCE_LINES,
    AUTO_ESCALATE_AFTER,
    AUTO_ESCALATE_STAGE,
    _action_plan,
    _generate_elder_code,
    _generate_short_code,
    _normalize_code,
    _relay_stage_rank,
    _risk_level_value,
    _trusted_public_url,
)

_MISSING = object()


def _auto_escalate_overdue_statuses(statuses, status_date, target_stage=AUTO_ESCALATE_STAGE):
    if not statuses:
        return 0
    now = utcnow()
    target_rank = _relay_stage_rank(target_stage)
    updated_communities = set()
    updated_count = 0
    for status in statuses:
        if status.confirmed_at:
            continue
        if not status.created_at:
            continue
        # 确保从数据库读取的 datetime 是 UTC aware 的
        if now - ensure_utc_aware(status.created_at) <= AUTO_ESCALATE_AFTER:
            continue
        if _relay_stage_rank(status.relay_stage) >= target_rank:
            continue
        status.relay_stage = target_stage
        updated_communities.add(status.community_code)
        updated_count += 1
    if updated_communities:
        db.session.commit()
        for code in updated_communities:
            _refresh_community_daily(code, status_date)
    return updated_count

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


def _build_outreach_suggestions(total_people, confirmed_count, help_count, escalation_count, risk_distribution):
    suggestions = []
    extreme_count = risk_distribution.get('极高', 0)
    high_count = risk_distribution.get('高风险', 0) + extreme_count
    if total_people <= 0:
        suggestions.append('优先完成短码绑定，建立行动名单。')
        return suggestions
    pending = max(total_people - confirmed_count, 0)
    if pending > 0:
        suggestions.append(f'仍有{pending}户未确认，优先联系高风险家庭。')
    if extreme_count > 0:
        suggestions.append(f'极高风险{extreme_count}户，建议当天优先确认。')
    if high_count > 0:
        suggestions.append(f'高风险{high_count}户，建议当天上门或电话提醒。')
    if escalation_count > 0:
        suggestions.append('升级链已触发，安排社区人员跟进并记录进展。')
    if help_count > 0:
        suggestions.append('已有家庭求助，请优先处理并回访。')
    if not suggestions:
        suggestions.append('确认率良好，继续关注天气变化与行动完成度。')
    return suggestions[:3]


def _personalized_care_notes(chronic_diseases):
    diseases = chronic_diseases or []
    text = '、'.join([d for d in diseases if d])
    if not text:
        return []
    notes = [f'慢病提示（可选登记）：{text}']
    # Light personalization only; avoid medical claims.
    cold_sensitive = any('呼吸' in d or '慢阻肺' in d or '支气管' in d for d in diseases)
    heat_sensitive = any('高血压' in d or '冠心病' in d or '脑卒中' in d for d in diseases)
    if cold_sensitive:
        notes.append('寒冷时更要注意保暖、减少外出，预防感冒与呼吸道不适。')
    if heat_sensitive:
        notes.append('高温时注意补水、避免暴晒和剧烈活动，留意头晕胸闷等不适。')
    return notes


def _build_caregiver_message(pair, alert_kind=None, weather_data=None, member=None, action_link=None):
    """Build a one-click message the caregiver can forward to the elder."""
    weather_data = weather_data or {}
    location = (getattr(pair, 'location_query', None) or getattr(pair, 'community_code', None) or '').strip()
    elder_name = getattr(member, 'name', None) if member else None
    relation = (getattr(member, 'relation', None) or '').strip() if member else ''

    # Pick a natural address term.
    address = '你'
    if relation in ('母亲', '妈妈', '妈'):
        address = '妈'
    elif relation in ('父亲', '爸爸', '爸'):
        address = '爸'
    elif elder_name:
        address = elder_name

    try:
        tmax = weather_data.get('temperature_max')
        tmin = weather_data.get('temperature_min')
        tmax_s = f"{float(tmax):.0f}" if tmax is not None else None
        tmin_s = f"{float(tmin):.0f}" if tmin is not None else None
    except Exception:
        tmax_s = None
        tmin_s = None

    if not action_link:
        action_link = _trusted_public_url(
            'public.elder_entry',
            short_code=pair.short_code,
        )

    lines = []
    if alert_kind == 'cold':
        lines.append('【寒潮行动提醒】')
        summary = f'{address}，我看到你那边今天可能比较冷'
        if tmin_s is not None:
            summary += f'（最低约 {tmin_s}°C）'
        summary += '。'
        lines.append(summary)
        lines.append('建议：尽量少出门，外出注意保暖防滑；室内注意保暖，别受凉。')
    elif alert_kind == 'heat':
        lines.append('【高温行动提醒】')
        summary = f'{address}，我看到你那边今天可能会很热'
        if tmax_s is not None:
            summary += f'（最高约 {tmax_s}°C）'
        summary += '。'
        lines.append(summary)
        lines.append('建议：避开中午外出，多喝水；室内开风扇/空调或找阴凉处休息。')
    else:
        lines.append('【日常提醒】')
        lines.append(f'{address}，我这边看看你那边天气有变化，注意劳逸结合，出门记得带水/外套。')

    if location:
        lines.append(f'地点：{location}')

    chronic_diseases = safe_json_loads(getattr(member, 'chronic_diseases', None), []) if member else []
    lines.extend(_personalized_care_notes(chronic_diseases))

    lines.append('说明：这是行动提醒，不提供医疗诊断/治疗建议；如明显不适请及时就医。')
    lines.append(f'（可选）行动页：{action_link}  短码：{pair.short_code}')
    return '\n'.join([line for line in lines if line])


def _build_community_message(community_code, risk_label, resources):
    action_link = _trusted_public_url('public.action_check')
    lines = [
        '【社区高温行动提醒】',
        f'社区：{community_code}',
        f'今日热风险：{risk_label}',
        '行动建议（非医疗诊断/治疗）：'
    ]
    for item in _action_plan(risk_label):
        lines.append(f'- {item["title"]}：{item["detail"]}')
    if resources:
        lines.append('附近避暑点可参考：')
        for item in resources[:3]:
            name_line = f'- {item.name}'
            if item.address_hint:
                name_line += f'（{item.address_hint}）'
            lines.append(name_line)
    lines.append(f'行动入口：{action_link}')
    lines.append('如需帮助请联系社区服务人员。')
    return '\n'.join(lines)


def _build_announce_message(title, location, risk_label, actions, extra_lines=None, updated_at=None):
    if updated_at is None:
        updated_at = now_local()
    lines = [
        f'【{title}】',
        f'地点：{location}',
        f'今日热风险：{risk_label}'
    ]
    if extra_lines:
        lines.extend(extra_lines)
    if actions:
        lines.append('行动建议：')
        for item in actions[:3]:
            lines.append(f'- {item["title"]}：{item["detail"]}')
    lines.append('免责声明：')
    lines.extend([f'- {item}' for item in ANNOUNCE_DISCLAIMER_LINES])
    lines.append('数据来源：')
    lines.extend([f'- {item}' for item in ANNOUNCE_SOURCE_LINES])
    lines.append(f'更新时间：{updated_at.strftime("%Y-%m-%d %H:%M")}')
    return '\n'.join(lines)


def _ensure_demo_statuses(community_code, status_date, caregiver_id=None, pair_count=3):
    if not is_demo_mode():
        return
    if not community_code:
        return
    existing = DailyStatus.query.filter_by(
        community_code=community_code,
        status_date=status_date
    ).count()
    if existing:
        return

    pairs = Pair.query.filter_by(
        community_code=community_code,
        status='active'
    ).limit(pair_count).all()
    if not pairs:
        if caregiver_id is None:
            caregiver_id = current_user.id
        for _ in range(pair_count):
            short_code = _generate_short_code()
            pair = Pair(
                caregiver_id=caregiver_id,
                community_code=community_code,
                elder_code=_generate_elder_code(),
                short_code=short_code,
                short_code_hash=hash_short_code(short_code),
                status='active',
                last_active_at=utcnow()
            )
            db.session.add(pair)
            pairs.append(pair)
        db.session.flush()

    now = utcnow()
    risk_labels = ['低风险', '中风险', '高风险', '极高']
    for idx, pair in enumerate(pairs):
        status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=status_date).first()
        if status:
            continue
        label = risk_labels[min(idx, len(risk_labels) - 1)]
        status = DailyStatus(
            pair_id=pair.id,
            status_date=status_date,
            community_code=pair.community_code,
            risk_level=label,
            confirmed_at=now - timedelta(hours=idx + 1) if idx % 2 == 0 else None,
            help_flag=idx == 2,
            relay_stage='caregiver' if idx == 2 else 'none'
        )
        db.session.add(status)
    db.session.commit()
    _refresh_community_daily(community_code, status_date)


def _community_access_allowed(community_code):
    if getattr(current_user, 'role', None) == 'admin':
        return True
    user_code = _normalize_code(getattr(current_user, 'community', None))
    return bool(user_code) and user_code == community_code


def _build_community_snapshot(community_code, status_date, record=_MISSING, statuses=_MISSING):
    # record 仅为旧批量调用保留；所有派生值统一从当前 active 家庭重算。
    _ = record
    metrics = build_community_household_metrics(
        community_code,
        status_date,
        statuses=None if statuses is _MISSING else statuses,
    )
    total_people = metrics['total_people']
    confirmed_count = metrics['confirmed_count']
    help_count = metrics['help_count']
    escalation_count = metrics['escalation_count']
    confirm_rate = (confirmed_count / total_people) if total_people else 0
    escalation_rate = (escalation_count / total_people) if total_people else 0
    help_rate = (help_count / total_people) if total_people else 0
    return {
        'total_people': total_people,
        'confirm_rate': round(confirm_rate, 4),
        'escalation_rate': round(escalation_rate, 4),
        'risk_distribution': metrics['risk_distribution'],
        'confirmed_risk_distribution': metrics['confirmed_risk_distribution'],
        'confirmed_count': confirmed_count,
        'help_count': help_count,
        'escalation_count': escalation_count,
        'outreach_summary': outreach_summary(
            total_people,
            confirmed_count,
            help_count,
            escalation_count,
        ),
        'help_rate': round(help_rate, 4),
        'flag_count': escalation_count
    }
