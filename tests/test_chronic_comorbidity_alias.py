# -*- coding: utf-8 -*-
from services.chronic_risk_service import ChronicRiskService


def test_family_copd_label_matches_amplifier():
    service = ChronicRiskService()
    copd = service.get_comorbidity_amplifier(['COPD'], 'respiratory')
    alias = service.get_comorbidity_amplifier(['慢阻肺'], 'respiratory')
    long_name = service.get_comorbidity_amplifier(['慢性阻塞性肺病'], 'respiratory')

    assert copd > 1.0
    assert alias == copd
    assert long_name == copd
