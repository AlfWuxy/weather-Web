# -*- coding: utf-8 -*-
"""未标定分位数时，DLNM 不得用 35/32/5/2/22°C 编造极端天气。"""
from services.dlnm_risk_service import DLNMRiskService


def _bare_dlnm():
    service = object.__new__(DLNMRiskService)
    service.percentiles = {}
    service.mmt = None
    service.tmin_p90 = None
    return service


def test_untrained_thresholds_are_unavailable_not_literature_defaults():
    thresholds = _bare_dlnm().get_risk_thresholds()

    assert thresholds['heat_extreme'] is None
    assert thresholds['heat_warning'] is None
    assert thresholds['cold_warning'] is None
    assert thresholds['cold_extreme'] is None
    assert thresholds['hot_night_threshold'] is None
    assert thresholds['mmt'] is None


def test_missing_percentiles_do_not_label_38c_as_extreme_heat():
    events = _bare_dlnm().identify_extreme_weather_events(38)

    assert events == []


def test_missing_tmin_p90_does_not_invent_hot_night_at_22c():
    service = _bare_dlnm()
    service.percentiles = {
        'p95': 35.0,
        'p90': 32.0,
        'p10': 5.0,
        'p5': 2.0,
    }

    events = service.identify_extreme_weather_events(24, is_night_temp=True)

    assert events == []
    assert all(item.get('type') != '热夜' for item in events)
