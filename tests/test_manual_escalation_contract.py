# -*- coding: utf-8 -*-
"""升级链只能由明确写动作推进，页面 GET 不得隐式写库。"""

import inspect


def test_pair_management_get_has_no_hidden_auto_escalation_write():
    from services.user import caregiver_service

    source = inspect.getsource(caregiver_service._build_pair_management_context)

    assert "_auto_escalate_overdue_statuses(" not in source


def test_community_dashboard_get_has_no_hidden_auto_escalation_write():
    from services.user import community_service

    source = inspect.getsource(community_service.community_dashboard)

    assert "_auto_escalate_overdue_statuses(" not in source


def test_pair_management_uses_effective_confirmation_for_overdue_state():
    """旧记录只有 confirmed_at 时仍应视为未确认并进入人工跟进提示。"""
    from services.user import caregiver_service

    source = inspect.getsource(caregiver_service._build_pair_management_context)

    assert "confirmed = is_effective_confirmation(status)" in source
