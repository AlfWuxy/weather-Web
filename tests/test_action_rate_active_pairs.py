# -*- coding: utf-8 -*-
"""社区行动率只统计 active Pair 的回归测试。"""

import json

from core.db_models import Community, CommunityDaily, DailyStatus, Pair, User
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


def test_public_refresh_uses_the_same_active_pair_population(db_session):
    from services.public_service import _refresh_community_daily

    status_date = _seed_active_and_inactive_statuses(db_session)

    _refresh_community_daily('行动率测试社区', status_date)
    record = CommunityDaily.query.filter_by(
        community_code='行动率测试社区',
        date=status_date,
    ).one()

    assert record.total_people == 1
    assert record.confirm_rate == 0
    assert record.escalation_rate == 0
    assert record.confirm_rate <= 1
    assert json.loads(record.risk_distribution) == {
        '低风险': 1,
        '中风险': 0,
        '高风险': 0,
        '极高': 0,
    }

    active_status = DailyStatus.query.join(Pair).filter(
        Pair.status == 'active',
        DailyStatus.status_date == status_date,
    ).one()
    active_status.relay_stage = 'backup'
    db_session.commit()

    _refresh_community_daily('行动率测试社区', status_date)
    assert record.escalation_rate == 1


def test_household_snapshot_and_web_dashboard_count_one_caregiver_once(
    app,
    db_session,
    monkeypatch,
):
    """同一照护账号多位老人按最高风险和任一行动状态汇总为一户。"""
    from flask_login import login_user
    from services.user import community_service
    from services.user._helpers import _build_community_snapshot

    caregiver = User(username='household-owner', role='caregiver')
    caregiver.set_password('safe-test-password')
    admin = User(username='household-admin', role='admin')
    admin.set_password('safe-test-password')
    db_session.add_all([caregiver, admin, Community(name='行动率测试社区')])
    db_session.flush()

    risk_rows = [
        ('84000001', '低风险', True, False, 'none'),
        ('84000002', '极高', False, True, 'backup'),
        ('84000003', '高风险', False, False, 'none'),
    ]
    status_date = today_local()
    for code, risk_level, confirmed, help_flag, relay_stage in risk_rows:
        pair = _create_pair(db_session, caregiver.id, code, 'active')
        db_session.add(DailyStatus(
            pair_id=pair.id,
            status_date=status_date,
            community_code='行动率测试社区',
            risk_level=risk_level,
            confirmed_at=utcnow() if confirmed else None,
            help_flag=help_flag,
            relay_stage=relay_stage,
        ))
    db_session.commit()

    snapshot = _build_community_snapshot('行动率测试社区', status_date)
    assert snapshot['total_people'] == 1
    assert snapshot['confirmed_count'] == 1
    assert snapshot['help_count'] == 1
    assert snapshot['escalation_count'] == 1
    assert snapshot['confirm_rate'] == 1
    assert snapshot['help_rate'] == 1
    assert snapshot['escalation_rate'] == 1
    assert snapshot['risk_distribution'] == {
        '低风险': 0,
        '中风险': 0,
        '高风险': 0,
        '极高': 1,
    }
    assert snapshot['confirmed_risk_distribution'] == snapshot['risk_distribution']

    rendered = []

    def capture_template(template_name, **context):
        rendered.append((template_name, context))
        return context

    monkeypatch.setattr(community_service, 'render_template', capture_template)
    monkeypatch.setattr(
        community_service,
        '_load_heat_risk',
        lambda _location: ({}, None, None),
    )

    with app.test_request_context('/'):
        login_user(admin)
        dashboard_context = community_service.community_dashboard()
        dashboard_item = dashboard_context['snapshots'][0]
        assert dashboard_item['confirmed_total'] == 1
        assert dashboard_item['help_count'] == 1
        assert dashboard_item['escalation_count'] == 1
        assert dashboard_item['risk_counts'] == snapshot['risk_distribution']
        assert dashboard_item['confirmed_counts'] == snapshot['confirmed_risk_distribution']

        detail_context = community_service.community_detail('行动率测试社区')
        assert detail_context['confirmed_total'] == 1
        assert detail_context['help_count'] == 1
        assert detail_context['escalation_count'] == 1
        assert detail_context['risk_counts'] == snapshot['risk_distribution']
        assert detail_context['confirmed_counts'] == snapshot['confirmed_risk_distribution']
        # 管理员明细保留逐 Pair 行，聚合展示仍只计算一户。
        assert len(detail_context['statuses']) == 3
        assert len(detail_context['pair_map']) == 3

    assert [item[0] for item in rendered] == [
        'community_dashboard.html',
        'community_detail.html',
    ]
