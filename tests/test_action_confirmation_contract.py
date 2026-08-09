# -*- coding: utf-8 -*-
"""行动确认必须由至少一项实际完成行动激活。"""

import pytest


def _pair_and_status(db_session, *, username, short_code):
    from core.db_models import DailyStatus, Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local, utcnow

    owner = User(username=username, role="user")
    owner.set_password("safe-test-password")
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code=f"elder-{short_code}",
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
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
    )
    db_session.add(status)
    db_session.flush()
    return pair, status


def test_shared_action_confirmation_rejects_empty_checklist(db_session):
    from services.care_action_service import stage_confirm_action

    pair, status = _pair_and_status(
        db_session,
        username="empty-action-owner",
        short_code="76110001",
    )

    with pytest.raises(ValueError, match="actions_required"):
        stage_confirm_action(
            pair,
            status,
            actions_done_count=0,
            elder_actions=[],
            source="miniprogram",
        )

    assert status.confirmed_at is None
    assert status.actions_done_count == 0
    assert status.elder_actions is None


def test_legacy_empty_confirmation_does_not_enter_community_rate(db_session):
    from core.time_utils import today_local, utcnow
    from services.community_daily_service import build_community_household_metrics

    _pair, status = _pair_and_status(
        db_session,
        username="legacy-empty-action-owner",
        short_code="76110003",
    )
    status.confirmed_at = utcnow()
    status.actions_done_count = 0
    db_session.commit()

    metrics = build_community_household_metrics("都昌县", today_local())

    assert metrics["total_people"] == 1
    assert metrics["confirmed_count"] == 0
