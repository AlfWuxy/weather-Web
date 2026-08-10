# -*- coding: utf-8 -*-
"""今日行动提醒模板选择回归测试。"""

from datetime import date, datetime, timedelta, timezone
from itertools import combinations


def test_reminder_library_has_120_unique_easy_follow_up_entries():
    from services.action_reminder_service import load_action_reminder_templates

    templates = load_action_reminder_templates()

    assert len(templates) == 120
    assert len({item["id"] for item in templates}) == 120
    assert len({item["message"] for item in templates}) == 120
    assert all(item["message"] for item in templates)
    assert all(item["follow_up_question"].endswith(("？", "吗？")) for item in templates)
    for risk_level in ("low", "medium", "high", "extreme"):
        for weather_tag in ("heat", "cold", "storm", "rain", "general"):
            shareable = [
                item
                for item in templates
                if item["risk_level"] == risk_level
                and item["weather_tags"] == [weather_tag]
                and item["audience"] != "neighbor_helper"
            ]
            assert len(shareable) == 5


def test_reminder_selection_is_stable_within_duchang_date():
    from services.action_reminder_service import select_action_reminder

    first = select_action_reminder(
        date_value=datetime(2026, 7, 30, 4, tzinfo=timezone.utc),
        risk_level="高风险",
        weather_tags=["heat"],
        audience="family_group",
    )
    second = select_action_reminder(
        date_value=datetime(2026, 7, 30, 15, tzinfo=timezone.utc),
        risk_level="高风险",
        weather_tags=["heat"],
        audience="family_group",
    )

    assert first == second
    assert first["date"] == "2026-07-30"
    assert first["risk_level"] == "high"
    assert first["weather_tags"] == ["heat"]
    assert first["audience"] in {
        "older_adult",
        "family_caregiver",
        "family_group",
    }
    assert first["text"] == (
        f"{first['message']}\n{first['follow_up_question']}"
    )


def test_family_reminder_changes_on_consecutive_days_for_each_context():
    from services.action_reminder_service import select_action_reminder

    for weather_tag in ("heat", "cold", "storm", "rain", "general"):
        for risk_level in ("low", "medium", "high", "extreme"):
            first = select_action_reminder(
                date_value="2026-07-30",
                risk_level=risk_level,
                weather_tags=[weather_tag],
                audience="family_group",
            )
            second = select_action_reminder(
                date_value="2026-07-31",
                risk_level=risk_level,
                weather_tags=[weather_tag],
                audience="family_group",
            )

            assert first["id"] != second["id"]
            assert first["audience"] != "neighbor_helper"
            assert second["audience"] != "neighbor_helper"


def test_family_reminder_has_120_distinct_days_for_fixed_context():
    from services.action_reminder_service import select_action_reminder

    start = date(2026, 8, 1)
    reminders = [
        select_action_reminder(
            date_value=start + timedelta(days=offset),
            risk_level="high",
            weather_tags=["heat"],
            audience="family_group",
        )
        for offset in range(120)
    ]
    repeated = select_action_reminder(
        date_value=start + timedelta(days=120),
        risk_level="high",
        weather_tags=["heat"],
        audience="family_group",
    )

    assert len({item["id"] for item in reminders}) == 120
    assert len({item["text"] for item in reminders}) == 120
    assert all(item["template_id"] for item in reminders)
    assert all(item["audience"] != "neighbor_helper" for item in reminders)
    assert repeated["id"] == reminders[0]["id"]
    assert repeated["text"] == reminders[0]["text"]


def test_family_reminder_slots_do_not_overlap_when_context_changes():
    from services.action_reminder_service import select_action_reminder

    weather_tags = ("heat", "cold", "storm", "rain")
    contexts = [
        (risk_level, list(tag_group))
        for risk_level in ("low", "medium", "high", "extreme")
        for group_size in range(1, len(weather_tags) + 1)
        for tag_group in combinations(weather_tags, group_size)
    ]
    contexts.extend(
        (risk_level, ["general"])
        for risk_level in ("low", "medium", "high", "extreme")
    )
    start = date(2026, 8, 1)
    outputs_by_slot = []
    seen = {}

    for offset in range(120):
        outputs = {
            (
                reminder["id"],
                reminder["text"],
            )
            for risk_level, tags in contexts
            for reminder in (
                select_action_reminder(
                    date_value=start + timedelta(days=offset),
                    risk_level=risk_level,
                    weather_tags=tags,
                    audience="family_group",
                ),
            )
        }
        for output in outputs:
            assert output not in seen, (
                f"第 {offset} 天与第 {seen[output]} 天提醒重复"
            )
            seen[output] = offset
        outputs_by_slot.append(outputs)

    assert len(outputs_by_slot) == 120


def test_family_reminder_repeats_after_120_days_for_every_exact_context():
    from services.action_reminder_service import select_action_reminder

    start = date(2026, 8, 1)
    for risk_level in ("low", "medium", "high", "extreme"):
        for weather_tag in ("heat", "cold", "storm", "rain", "general"):
            first_cycle = [
                select_action_reminder(
                    date_value=start + timedelta(days=offset),
                    risk_level=risk_level,
                    weather_tags=[weather_tag],
                    audience="family_group",
                )
                for offset in range(120)
            ]
            repeated = select_action_reminder(
                date_value=start + timedelta(days=120),
                risk_level=risk_level,
                weather_tags=[weather_tag],
                audience="family_group",
            )

            assert len({item["id"] for item in first_cycle}) == 120
            assert len({item["text"] for item in first_cycle}) == 120
            assert repeated["id"] == first_cycle[0]["id"]
            assert repeated["text"] == first_cycle[0]["text"]


def test_family_reminder_uses_one_priority_weather_context():
    from services.action_reminder_service import select_action_reminder

    reminder = select_action_reminder(
        date_value="2026-08-01",
        risk_level="high",
        weather_tags=["rain", "heat", "storm"],
        audience="family_group",
    )

    assert reminder["weather_tags"] == ["storm"]


def test_weather_tag_inference_uses_snapshot_without_network(monkeypatch):
    from services import action_reminder_service

    monkeypatch.setattr(
        action_reminder_service,
        "load_action_reminder_templates",
        action_reminder_service.load_action_reminder_templates,
    )
    tags = action_reminder_service.infer_weather_tags(
        {
            "temperature_max": 36,
            "temperature_min": 27,
            "weather_condition": "雷阵雨",
        },
        [{"title": "雷电黄色预警"}],
    )

    assert tags == ["heat", "storm", "rain"]


def test_unknown_context_falls_back_to_safe_general_reminder():
    from services.action_reminder_service import select_action_reminder

    reminder = select_action_reminder(
        date_value="2026-08-01",
        risk_level="unknown",
        weather_tags=["unknown"],
        audience="unknown",
    )

    assert reminder["risk_level"] == "low"
    assert reminder["weather_tags"] == ["general"]
    assert reminder["audience"] in {
        "older_adult",
        "family_caregiver",
        "family_group",
        "neighbor_helper",
    }
