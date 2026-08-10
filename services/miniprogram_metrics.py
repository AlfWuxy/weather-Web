# -*- coding: utf-8 -*-
"""微信小程序账号级 cohort 指标，只输出聚合结果。"""

import json
from datetime import timedelta

from core.db_models import MiniProgramIdentity, UsageEvent
from core.time_utils import ensure_utc_aware, utc_to_local_date, utcnow


METRIC_EVENT_TYPES = frozenset({
    'elder_profile_created',
    'checkin_confirmed',
})
RAW_EVENT_RETENTION_DAYS = 30


def _safe_meta(event):
    try:
        value = json.loads(event.meta_json or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_time(event):
    return ensure_utc_aware(event.created_at)


def _within_natural_days(timestamp, start_timestamp, minimum, maximum):
    if timestamp is None or start_timestamp is None or timestamp < start_timestamp:
        return False
    day_offset = (utc_to_local_date(timestamp) - utc_to_local_date(start_timestamp)).days
    return minimum <= day_offset <= maximum


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else 0.0


def _normalized_acquisition(value):
    return value if value in {'direct', 'family_share'} else 'unknown'


def _local_week_start(timestamp):
    """返回首次有效行动所在本地自然周的周一。"""
    local_date = utc_to_local_date(timestamp)
    return local_date - timedelta(days=local_date.weekday())


def compute_miniprogram_metrics(
    events,
    *,
    cohorts,
    as_of=None,
    excluded_user_ids=None,
):
    """从持久化首次登录 cohort 与短期事件计算聚合指标。"""
    reference_time = ensure_utc_aware(as_of or utcnow())
    excluded = {int(value) for value in (excluded_user_ids or ()) if str(value).isdigit()}
    valid_cohorts = [
        cohort for cohort in cohorts
        if cohort.user_id is not None and cohort.created_at is not None
    ]
    excluded_cohort_users = {
        int(cohort.user_id)
        for cohort in valid_cohorts
        if int(cohort.user_id) in excluded
    }
    timelines = {}
    for cohort in sorted(
        valid_cohorts,
        key=lambda item: (
            ensure_utc_aware(item.created_at),
            getattr(item, 'id', 0) or 0,
        ),
    ):
        user_id = int(cohort.user_id)
        if user_id in excluded or user_id in timelines:
            continue
        timelines[user_id] = {
            'user_id': user_id,
            'login': ensure_utc_aware(cohort.created_at),
            'acquisition': _normalized_acquisition(cohort.acquisition_source),
            'profiles': [],
            'actions': [],
        }

    ordered = sorted(
        (
            event for event in events
            if event.user_id is not None
            and event.user_id not in excluded
            and event.event_type in METRIC_EVENT_TYPES
            and event.created_at is not None
            and event.user_id in timelines
        ),
        key=lambda event: (_event_time(event), event.id or 0),
    )
    for event in ordered:
        timeline = timelines[event.user_id]
        timestamp = _event_time(event)
        if event.event_type == 'elder_profile_created':
            timeline['profiles'].append(timestamp)
        elif event.event_type == 'checkin_confirmed':
            done_count = _safe_meta(event).get('actions_done_count')
            if isinstance(done_count, int) and not isinstance(done_count, bool) and done_count >= 1:
                timeline['actions'].append(timestamp)

    login_timelines = list(timelines.values())
    current_local_date = utc_to_local_date(reference_time)
    activation_eligible = []
    activated = []
    profile_converted = []

    for timeline in login_timelines:
        login = timeline['login']
        if (current_local_date - utc_to_local_date(login)).days < 7:
            continue
        activation_eligible.append(timeline)
        profiles = [
            timestamp for timestamp in timeline['profiles']
            if _within_natural_days(timestamp, login, 0, 6)
        ]
        actions = [
            timestamp for timestamp in timeline['actions']
            if _within_natural_days(timestamp, login, 0, 6)
        ]
        if profiles:
            profile_converted.append(timeline)
        if profiles and actions:
            activated.append((timeline, min(actions)))

    retention_eligible = []
    retained = []
    for timeline, first_action in activated:
        # D8 至 D14 在 D15 开始时才拥有完整观察期。
        if (current_local_date - utc_to_local_date(first_action)).days < 15:
            continue
        retention_eligible.append(timeline)
        if any(
            _within_natural_days(timestamp, first_action, 8, 14)
            for timestamp in timeline['actions']
        ):
            retained.append(timeline)

    activated_timelines = [timeline for timeline, _first_action in activated]

    def source_summary(source):
        login_count = sum(
            timeline['acquisition'] == source
            for timeline in login_timelines
        )
        eligible_count = sum(
            timeline['acquisition'] == source
            for timeline in activation_eligible
        )
        activated_count = sum(
            timeline['acquisition'] == source
            for timeline in activated_timelines
        )
        retention_eligible_count = sum(
            timeline['acquisition'] == source
            for timeline in retention_eligible
        )
        retained_count = sum(
            timeline['acquisition'] == source
            for timeline in retained
        )
        return {
            'source': source,
            'login_users': login_count,
            'd7_mature_users': eligible_count,
            'activated_users': activated_count,
            'activation_rate': _rate(activated_count, eligible_count),
            'd15_mature_users': retention_eligible_count,
            'retained_users': retained_count,
            'retention_rate': _rate(retained_count, retention_eligible_count),
        }

    direct = source_summary('direct')
    family = source_summary('family_share')
    unknown = source_summary('unknown')

    source_labels = {
        'direct': '直接访问',
        'family_share': '家庭分享',
        'unknown': '来源未知',
    }
    source_breakdown = []
    for summary in (direct, family, unknown):
        source_breakdown.append({
            **summary,
            'label': source_labels[summary['source']],
        })

    retention_eligible_ids = {
        timeline['user_id'] for timeline in retention_eligible
    }
    retained_ids = {timeline['user_id'] for timeline in retained}
    weekly_rows = {}
    for timeline, first_action in activated:
        week_start = _local_week_start(first_action)
        row = weekly_rows.setdefault(week_start, {
            'week_start': week_start.isoformat(),
            'week_end': (week_start + timedelta(days=6)).isoformat(),
            'activated_users': 0,
            'd15_mature_users': 0,
            'retained_users': 0,
            'retention_rate': 0.0,
            'sources': {
                source: {
                    'activated_users': 0,
                    'd15_mature_users': 0,
                    'retained_users': 0,
                    'retention_rate': 0.0,
                }
                for source in ('direct', 'family_share', 'unknown')
            },
        })
        source = timeline['acquisition']
        row['activated_users'] += 1
        row['sources'][source]['activated_users'] += 1
        if timeline['user_id'] in retention_eligible_ids:
            row['d15_mature_users'] += 1
            row['sources'][source]['d15_mature_users'] += 1
        if timeline['user_id'] in retained_ids:
            row['retained_users'] += 1
            row['sources'][source]['retained_users'] += 1

    weekly_action_cohorts = []
    for week_start in sorted(weekly_rows, reverse=True):
        row = weekly_rows[week_start]
        row['retention_rate'] = _rate(
            row['retained_users'],
            row['d15_mature_users'],
        )
        for source_metrics in row['sources'].values():
            source_metrics['retention_rate'] = _rate(
                source_metrics['retained_users'],
                source_metrics['d15_mature_users'],
            )
        weekly_action_cohorts.append(row)

    activation_denominator = len(activation_eligible)
    retention_denominator = len(retention_eligible)
    return {
        'cohort_login_users': len(login_timelines),
        'activation_eligible_users': activation_denominator,
        'profile_created_users': len(profile_converted),
        'activated_users': len(activated),
        'profile_creation_rate': _rate(len(profile_converted), activation_denominator),
        'activation_rate': _rate(len(activated), activation_denominator),
        'retention_eligible_users': retention_denominator,
        'retained_users': len(retained),
        'week2_retention_rate': _rate(len(retained), retention_denominator),
        'direct_login_users': direct['login_users'],
        'direct_activation_eligible_users': direct['d7_mature_users'],
        'direct_activated_users': direct['activated_users'],
        'direct_activation_rate': direct['activation_rate'],
        'family_share_login_users': family['login_users'],
        'family_share_activation_eligible_users': family['d7_mature_users'],
        'family_share_activated_users': family['activated_users'],
        'family_share_activation_rate': family['activation_rate'],
        'unknown_source_login_users': unknown['login_users'],
        'unknown_source_activation_eligible_users': unknown['d7_mature_users'],
        'unknown_source_activated_users': unknown['activated_users'],
        'unknown_source_share': _rate(unknown['login_users'], len(login_timelines)),
        'test_account_exclusion_enabled': bool(excluded),
        'configured_test_account_ids': len(excluded),
        'excluded_test_account_users': len(excluded_cohort_users),
        'source_breakdown': source_breakdown,
        'weekly_action_cohorts': weekly_action_cohorts,
    }


def load_miniprogram_metrics(start_ts, *, as_of=None, excluded_user_ids=None):
    """加载最长 30 天的首次登录 cohort 与行为事件。"""
    reference_time = ensure_utc_aware(as_of or utcnow())
    requested_start = ensure_utc_aware(start_ts)
    retention_start = reference_time - timedelta(days=RAW_EVENT_RETENTION_DAYS)
    effective_start = max(requested_start, retention_start)
    excluded = {int(value) for value in (excluded_user_ids or ()) if str(value).isdigit()}

    cohort_query = MiniProgramIdentity.query.filter(
        MiniProgramIdentity.created_at.isnot(None),
        MiniProgramIdentity.created_at >= effective_start,
        MiniProgramIdentity.created_at <= reference_time,
    )
    cohorts = cohort_query.order_by(
        MiniProgramIdentity.created_at.asc(),
        MiniProgramIdentity.id.asc(),
    ).all()
    cohort_user_ids = [
        cohort.user_id for cohort in cohorts
        if cohort.user_id not in excluded
    ]

    if not cohort_user_ids:
        return compute_miniprogram_metrics(
            [],
            cohorts=cohorts,
            as_of=reference_time,
            excluded_user_ids=excluded,
        )

    events = UsageEvent.query.filter(
        UsageEvent.user_id.in_(cohort_user_ids),
        UsageEvent.source == 'miniprogram',
        UsageEvent.event_type.in_(METRIC_EVENT_TYPES),
        UsageEvent.created_at >= effective_start,
        UsageEvent.created_at <= reference_time,
    ).order_by(UsageEvent.created_at.asc(), UsageEvent.id.asc()).all()
    return compute_miniprogram_metrics(
        events,
        cohorts=cohorts,
        as_of=reference_time,
        excluded_user_ids=excluded,
    )
