# -*- coding: utf-8 -*-
"""行动确认必须由已知且实际完成的行动激活。"""

import json
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


def test_effective_confirmation_requires_timestamp_and_completed_action(db_session):
    """确认时间和至少一项完成行动必须同时成立。"""
    from core.time_utils import utcnow
    from services.care_action_service import is_effective_confirmation

    _pair, status = _pair_and_status(
        db_session,
        username="effective-confirmation-owner",
        short_code="76110002",
    )

    assert is_effective_confirmation(None) is False
    assert is_effective_confirmation(status) is False

    status.confirmed_at = utcnow()
    status.actions_done_count = 0
    assert is_effective_confirmation(status) is False

    status.actions_done_count = 1
    assert is_effective_confirmation(status) is True

    status.confirmed_at = None
    assert is_effective_confirmation(status) is False

    status.confirmed_at = utcnow()
    status.actions_done_count = "invalid"
    assert is_effective_confirmation(status) is False


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


def test_shared_action_catalog_accepts_web_and_miniprogram_ids(db_session):
    """共用层接受两端正式目录，并把清单原样留给另一端识别。"""
    from services.care_action_service import stage_confirm_action

    pair, status = _pair_and_status(
        db_session,
        username="cross-client-action-owner",
        short_code="76110004",
    )

    stage_confirm_action(
        pair,
        status,
        actions_done_count=2,
        elder_actions=["hydrate", "drink_water"],
        source="web",
    )

    assert json.loads(status.elder_actions) == ["hydrate", "drink_water"]
    assert status.actions_done_count == 2
    assert status.confirmed_at is not None


@pytest.mark.parametrize(
    ("actions", "error"),
    (
        (["unknown-action-id"], "unknown_elder_action"),
        (["drink_water", "drink_water"], "duplicate_elder_action"),
    ),
)
def test_shared_action_catalog_rejects_unknown_or_duplicate_ids(
    db_session,
    actions,
    error,
):
    """绕过客户端也不能用伪造或重复 ID 抬高确认计数。"""
    pair, status = _pair_and_status(
        db_session,
        username=f"invalid-action-{error}",
        short_code={
            "unknown_elder_action": "76110005",
            "duplicate_elder_action": "76110006",
        }[error],
    )
    from services.care_action_service import stage_confirm_action

    with pytest.raises(ValueError, match=error):
        stage_confirm_action(
            pair,
            status,
            actions_done_count=len(actions),
            elder_actions=actions,
            source="miniprogram",
        )

    assert status.confirmed_at is None
    assert status.actions_done_count == 0
    assert status.elder_actions is None


def test_shared_action_catalog_covers_current_web_plans_and_safe_labels():
    """目录变更时必须同步跨端校验与照护人展示文案。"""
    from services.care_action_service import (
        ELDER_ACTION_LABELS,
        MINIPROGRAM_ELDER_ACTION_IDS,
        WEB_ELDER_ACTION_LABELS,
    )
    from services.user._common import _action_plan
    from services.user.caregiver_service import _build_elder_action_labels

    current_web_ids = {
        item["id"]
        for risk_level in ("极高", "高风险", "中风险", "低风险")
        for item in _action_plan(risk_level)
    }
    assert current_web_ids == set(WEB_ELDER_ACTION_LABELS)
    assert len(MINIPROGRAM_ELDER_ACTION_IDS) == 9
    assert _build_elder_action_labels(
        json.dumps(["hydrate", "drink_water", "unknown-history-id"])
    ) == [
        ELDER_ACTION_LABELS["hydrate"],
        ELDER_ACTION_LABELS["drink_water"],
        "其他自护行动（旧版本记录）",
    ]
