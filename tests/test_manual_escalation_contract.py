# -*- coding: utf-8 -*-
"""升级链只能由明确写动作推进，页面 GET 不得隐式写库。"""

import inspect


def test_pair_management_get_has_no_hidden_auto_escalation_write():
    from services.user import caregiver_service

    source = inspect.getsource(caregiver_service._build_pair_management_context)

    assert "_auto_escalate_overdue_statuses(" not in source
