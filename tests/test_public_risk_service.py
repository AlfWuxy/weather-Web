# -*- coding: utf-8 -*-
"""公开家庭提醒的数据依赖合同回归。"""

from services.public_risk_service import build_public_family_reminder


CURRENT = {
    "temperature": 36,
    "temperature_max": 38,
    "temperature_min": 28,
    "humidity": 70,
    "data_source": "QWeather",
    "is_mock": False,
}
WARNING = {"title": "都昌县高温橙色预警"}


def test_family_reminder_declares_current_only_dependency():
    reminder = build_public_family_reminder(
        CURRENT,
        [],
        risk={"level": "高风险"},
        available=True,
        date_value="2026-08-12",
    )

    assert reminder["depends_on"] == ["current"]


def test_family_reminder_declares_current_and_warning_dependencies():
    reminder = build_public_family_reminder(
        CURRENT,
        [WARNING],
        risk={"level": "高风险"},
        available=True,
        date_value="2026-08-12",
    )

    assert reminder["depends_on"] == ["current", "warnings"]


def test_warning_only_reminder_keeps_warning_provenance():
    reminder = build_public_family_reminder(
        {},
        [WARNING],
        risk={"level": "未知"},
        available=False,
        date_value="2026-08-12",
    )

    assert reminder["depends_on"] == ["warnings"]
    assert reminder["weather_tags"] == ["heat"]


def test_generic_reminder_explicitly_declares_zero_dependencies():
    reminder = build_public_family_reminder(
        {},
        [],
        risk={"level": "未知"},
        available=False,
        date_value="2026-08-12",
    )

    assert reminder["depends_on"] == []
    assert reminder["weather_tags"] == ["general"]
