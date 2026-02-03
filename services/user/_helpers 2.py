# -*- coding: utf-8 -*-
"""User-facing helper utilities."""
import json
from datetime import timedelta

from flask import url_for
from flask_login import current_user

from core.extensions import db
from core.security import hash_short_code
from core.time_utils import now_local, today_local, utcnow, ensure_utc_aware
from core.weather import is_demo_mode
from core.db_models import CommunityDaily, DailyStatus, Pair
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
    _risk_level_value
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


def _build_risk_counts(statuses):
    counts = {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0}
    confirmed = {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0}
    for status in statuses:
        label = status.risk_level or '低风险'
        if label not in counts:
            continue
        counts[label] += 1
        if status.confirmed_at:
            confirmed[label] += 1
    return counts, confirmed


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


def _build_caregiver_message(pair, risk_label):
    action_link = url_for(
        'public.elder_entry',
        short_code=pair.short_code,
        _external=True
    )
    lines = [
        '【高温行动提醒】',
        f'社区：{pair.community_code}',
        f'今日热风险：{risk_label}',
        f'行动链接：{action_link}',
        f'短码：{pair.short_code}',
        '行动建议（非医疗诊断/治疗）：'
    ]
    for item in _action_plan(risk_label):
        lines.append(f'- {item["title"]}：{item["detail"]}')
    lines.append('如需帮助请在页面内点击“我需要帮助”。')
    return '\n'.join(lines)


def _build_community_message(community_code, risk_label, resources):
    action_link = url_for('public.action_check', _external=True)
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
    if record is _MISSING:
        record = CommunityDaily.query.filter_by(
            community_code=community_code,
            date=status_date
        ).first()
    if statuses is _MISSING:
        statuses = DailyStatus.query.filter_by(
            community_code=community_code,
            status_date=status_date
        ).all()
    confirmed_count = sum(1 for s in statuses if s.confirmed_at)
    help_count = sum(1 for s in statuses if s.help_flag)
    flag_count = sum(
        1 for s in statuses if _relay_stage_rank(s.relay_stage) >= _relay_stage_rank(AUTO_ESCALATE_STAGE)
    )
    if record:
        risk_dist = safe_json_loads(
            record.risk_distribution,
            {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0}
        )
        for key in ('低风险', '中风险', '高风险', '极高'):
            risk_dist.setdefault(key, 0)
        total_people = record.total_people or 0
        help_rate = (help_count / total_people) if total_people else 0
        return {
            'total_people': total_people,
            'confirm_rate': record.confirm_rate or 0,
            'escalation_rate': record.escalation_rate or 0,
            'risk_distribution': risk_dist,
            'outreach_summary': record.outreach_summary or '',
            'help_rate': round(help_rate, 4),
            'flag_count': flag_count
        }

    total_people = Pair.query.filter_by(status='active', community_code=community_code).count()
    escalation_count = flag_count
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
    help_rate = (help_count / total_people) if total_people else 0
    return {
        'total_people': total_people,
        'confirm_rate': round(confirm_rate, 4),
        'escalation_rate': round(escalation_rate, 4),
        'risk_distribution': risk_dist,
        'outreach_summary': summary,
        'help_rate': round(help_rate, 4),
        'flag_count': flag_count
    }


def _refresh_community_daily(community_code, status_date):
    total_people = Pair.query.filter_by(status='active', community_code=community_code).count()
    statuses = DailyStatus.query.filter_by(
        community_code=community_code,
        status_date=status_date
    ).all()
    confirmed_count = sum(1 for s in statuses if s.confirmed_at)
    help_count = sum(1 for s in statuses if s.help_flag)
    escalation_count = sum(
        1 for s in statuses if _relay_stage_rank(s.relay_stage) >= _relay_stage_rank(AUTO_ESCALATE_STAGE)
    )
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
