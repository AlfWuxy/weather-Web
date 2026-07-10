# -*- coding: utf-8 -*-
"""社区行动率只统计 active Pair 的回归测试。"""

import json

from core.db_models import CommunityDaily, DailyStatus, Pair, User
from core.security import hash_short_code
from core.time_utils import today_local, utcnow


def _create_pair(db_session, caregiver_id, code, status):
    pair = Pair(
        caregiver_id=caregiver_id,
        community_code='行动率测试社区',
        elder_code=f'elder-{code}',
        short_code=code,
        short_code_hash=hash_short_code(code),
        status=status,
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.flush()
    return pair


def _seed_active_and_inactive_statuses(db_session):
    user = User(username='action-rate-owner', role='caregiver')
    user.set_password('test-password')
    db_session.add(user)
    db_session.flush()

    active_pair = _create_pair(db_session, user.id, '82000001', 'active')
    inactive_pair = _create_pair(db_session, user.id, '82000002', 'inactive')
    status_date = today_local()
    db_session.add_all([
        DailyStatus(
            pair_id=active_pair.id,
            status_date=status_date,
            community_code='行动率测试社区',
            risk_level='低风险',
            confirmed_at=None,
            help_flag=False,
            relay_stage='none',
        ),
        DailyStatus(
            pair_id=inactive_pair.id,
            status_date=status_date,
            community_code='行动率测试社区',
            risk_level='极高',
            confirmed_at=utcnow(),
            help_flag=True,
            relay_stage='backup',
        ),
    ])
    db_session.commit()
    return status_date


def test_snapshot_excludes_inactive_pair_from_all_action_rate_numerators(db_session):
    from services.user._helpers import _build_community_snapshot

    status_date = _seed_active_and_inactive_statuses(db_session)
    stale_record = CommunityDaily(
        community_code='行动率测试社区',
        date=status_date,
        total_people=2,
        confirm_rate=1.0,
        escalation_rate=1.0,
        risk_distribution=json.dumps({'低风险': 1, '中风险': 0, '高风险': 0, '极高': 1}),
        outreach_summary='旧聚合记录',
    )
    db_session.add(stale_record)
    db_session.commit()

    snapshot = _build_community_snapshot('行动率测试社区', status_date)

    assert snapshot['total_people'] == 1
    assert snapshot['confirm_rate'] == 0
    assert snapshot['escalation_rate'] == 0
    assert snapshot['help_rate'] == 0
    assert snapshot['flag_count'] == 0


def test_refresh_persists_active_pair_denominator_and_numerators(db_session):
    from services.user._helpers import _refresh_community_daily

    status_date = _seed_active_and_inactive_statuses(db_session)

    _refresh_community_daily('行动率测试社区', status_date)
    record = CommunityDaily.query.filter_by(
        community_code='行动率测试社区',
        date=status_date,
    ).one()

    assert record.total_people == 1
    assert record.confirm_rate == 0
    assert record.escalation_rate == 0
    risk_distribution = json.loads(record.risk_distribution)
    assert risk_distribution == {'低风险': 1, '中风险': 0, '高风险': 0, '极高': 0}
