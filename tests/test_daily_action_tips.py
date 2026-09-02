# -*- coding: utf-8 -*-
"""每日行动提示从 JSON 读取，两处调用保持同一套文案。"""


def test_daily_action_tips_cover_all_risk_tiers():
    from core.daily_tips import action_plan_for_risk

    for label in ('极高', '高风险', '中风险', '低风险'):
        plan = action_plan_for_risk(label)
        assert len(plan) == 3
        assert all(item.get('id') and item.get('title') and item.get('detail') for item in plan)


def test_public_and_user_action_plans_share_json():
    from core.daily_tips import action_plan_for_risk
    from services.public_service import _action_plan as public_plan
    from services.user._common import _action_plan as user_plan

    for label in ('极高', '高风险', '中风险', '未知'):
        expected = action_plan_for_risk(label)
        assert public_plan(label) == expected
        assert user_plan(label) == expected
