# -*- coding: utf-8 -*-
"""首页官方预警与应用提醒的 provenance 回归测试。"""
from datetime import datetime, timedelta, timezone


def _alert(now, **overrides):
    from core.db_models import WeatherAlert

    values = {
        'alert_date': now,
        'location': '都昌县',
        'alert_type': '高温',
        'alert_level': '黄色预警',
        'description': '测试提醒',
        'source': 'AppThreshold',
        'is_official': False,
        'starts_at': now,
        'ends_at': None,
        'affected_communities': '[]',
        'disease_correlation': '{}',
    }
    values.update(overrides)
    return WeatherAlert(**values)


def test_dashboard_filters_official_validity_and_recent_application_alerts(
    app,
    db_session,
):
    from services.user.dashboard_service import (
        _dashboard_alert_card,
        _dashboard_visible_alerts,
    )

    now = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)
    active_official = _alert(
        now,
        source='QWeather',
        is_official=True,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=5),
    )
    expired_official = _alert(
        now,
        source='QWeather',
        is_official=True,
        starts_at=now - timedelta(hours=8),
        ends_at=now - timedelta(hours=1),
    )
    missing_window = _alert(
        now,
        source='QWeather',
        is_official=True,
        starts_at=None,
        ends_at=None,
    )
    recent_application = _alert(now - timedelta(hours=2))
    stale_application = _alert(now - timedelta(hours=25))
    legacy_recent = _alert(
        now - timedelta(hours=3),
        source=None,
        is_official=False,
        starts_at=None,
    )
    db_session.add_all([
        active_official,
        expired_official,
        missing_window,
        recent_application,
        stale_application,
        legacy_recent,
    ])
    db_session.commit()

    visible = _dashboard_visible_alerts(['都昌县'], now=now, limit=10)
    visible_ids = {item.id for item in visible}

    assert active_official.id in visible_ids
    assert recent_application.id in visible_ids
    assert legacy_recent.id in visible_ids
    assert expired_official.id not in visible_ids
    assert missing_window.id not in visible_ids
    assert stale_application.id not in visible_ids

    official_card = _dashboard_alert_card(active_official, now=now)
    app_card = _dashboard_alert_card(recent_application, now=now)
    legacy_card = _dashboard_alert_card(legacy_recent, now=now)
    assert official_card['is_official'] is True
    assert official_card['kind_label'] == '官方预警'
    assert official_card['source_label'] == 'QWeather 官方预警'
    assert '有效期' in official_card['validity_label']
    assert app_card['is_official'] is False
    assert app_card['kind_label'] == '应用天气提醒'
    assert app_card['source_label'] == '应用阈值规则'
    assert legacy_card['source_label'] == '来源未标明'


def test_application_alert_never_uses_official_warning_semantics(app, db_session):
    from services.user.dashboard_service import _get_or_create_application_alert

    class FakeWeatherService:
        def generate_weather_alert(self, _location, _weather_data):
            return {
                'location': '都昌县',
                'alert_type': '高温',
                'alert_level': '红色预警',
                'description': '应用规则检测到高温',
            }

    first = _get_or_create_application_alert(
        FakeWeatherService(),
        '都昌县',
        {'temperature_max': 39},
        ['都昌', '都昌县'],
    )
    second = _get_or_create_application_alert(
        FakeWeatherService(),
        '都昌县',
        {'temperature_max': 39},
        ['都昌', '都昌县'],
    )

    assert first.id == second.id
    assert first.source == 'AppThreshold'
    assert first.is_official is False
    assert first.alert_level == '红色提醒'
    assert first.starts_at is not None
    assert first.ends_at is None
