# -*- coding: utf-8 -*-
"""用药/家人天气触发不得把缺测当成 0°C 或 AQI 0。"""
import json
from types import SimpleNamespace

from core.health_profiles import member_weather_triggered, reminder_triggered


def test_reminder_does_not_treat_missing_temp_as_zero_cold():
    reminder = SimpleNamespace(weather_triggers=json.dumps({'low_temp': 5}))
    weather = SimpleNamespace(temperature=None, humidity=60, aqi=40)

    triggered, reason = reminder_triggered(reminder, weather)

    assert triggered is False
    assert reason is None


def test_reminder_does_not_treat_missing_aqi_as_clean_air():
    reminder = SimpleNamespace(weather_triggers=json.dumps({'high_aqi': 80}))
    weather = SimpleNamespace(temperature=28, humidity=60, aqi=None)

    triggered, reason = reminder_triggered(reminder, weather)

    assert triggered is False
    assert reason is None


def test_member_weather_does_not_invent_zero_when_fields_missing():
    profile = SimpleNamespace(weather_thresholds=json.dumps({
        'low_temp': 5,
        'high_humidity': 80,
        'high_aqi': 100,
    }))
    weather = SimpleNamespace(temperature=None, humidity=None, aqi=None)

    assert member_weather_triggered(profile, weather) == []
