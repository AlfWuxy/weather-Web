# -*- coding: utf-8 -*-
"""日度风险只保存新鲜真实快照观察到的最高档。"""


def test_daily_risk_level_only_promotes_with_valid_current_snapshot(db_session):
    from core.db_models import DailyStatus, Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local, utcnow
    from services.care_action_service import get_or_create_daily_status

    owner = User(username="risk-peak-owner", role="user")
    owner.set_password("safe-test-password")
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code="risk-peak-elder",
        short_code="76110002",
        short_code_hash=hash_short_code("76110002"),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.flush()
    status = DailyStatus(
        pair_id=pair.id,
        status_date=today_local(),
        community_code=pair.community_code,
        risk_level="低风险",
    )
    db_session.add(status)
    db_session.commit()

    promoted = get_or_create_daily_status(
        pair,
        today_local(),
        risk_level_factory=lambda: "高风险",
    )
    assert promoted.risk_level == "高风险"

    unchanged = get_or_create_daily_status(
        pair,
        today_local(),
        risk_level="中风险",
    )
    assert unchanged.risk_level == "高风险"

    unknown = get_or_create_daily_status(
        pair,
        today_local(),
        risk_level="unknown",
    )
    assert unknown.risk_level == "高风险"
