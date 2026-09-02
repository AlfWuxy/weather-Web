# -*- coding: utf-8 -*-
from types import SimpleNamespace

from services.user._helpers import _build_risk_counts


def test_missing_risk_level_is_not_counted_as_low():
    counts, confirmed = _build_risk_counts([
        SimpleNamespace(risk_level=None, confirmed_at=None),
        SimpleNamespace(risk_level='风险未知', confirmed_at=None),
        SimpleNamespace(risk_level='中风险', confirmed_at=None),
    ])

    assert counts['低风险'] == 0
    assert counts['中风险'] == 1
    assert confirmed['低风险'] == 0
