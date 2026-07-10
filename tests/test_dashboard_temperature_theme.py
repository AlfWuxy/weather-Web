# -*- coding: utf-8 -*-
import re
from datetime import datetime, timezone

from services.user.dashboard_service import _dashboard_hero_theme


def _primary_saturation(theme):
    match = re.search(r"--yl-hero-primary: hsl\(\d+, (\d+)%, \d+%\)", theme["style"])
    assert match
    return int(match.group(1))


def test_dashboard_hero_theme_is_linear_and_clamped():
    low = _dashboard_hero_theme(8)
    mid = _dashboard_hero_theme(21.5)
    hot = _dashboard_hero_theme(35)
    over_hot = _dashboard_hero_theme(42)

    assert low["intensity"] == 0.0
    assert mid["intensity"] == 0.5
    assert hot["intensity"] == 1.0
    assert over_hot["intensity"] == 1.0
    assert _primary_saturation(low) < _primary_saturation(mid) < _primary_saturation(hot)


def test_dashboard_hero_theme_handles_invalid_temperature_safely():
    theme = _dashboard_hero_theme("bad-value")

    assert theme["temperature"] is None
    assert 0 <= theme["intensity"] <= 1
    assert "--yl-hero-primary:" in theme["style"]
    assert "None" not in theme["style"]
    assert "nan" not in theme["style"].lower()
    assert "javascript" not in theme["style"].lower()


def test_dashboard_renders_temperature_theme(authenticated_client):
    response = authenticated_client.get("/dashboard")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-temp-theme="dynamic"' in html
    assert 'data-temp-intensity="' in html
    assert "--yl-hero-primary:" in html
    assert "家庭照护今日页" in html


def test_dashboard_renders_weather_alert_real_fields_with_local_date(
    authenticated_client,
    db_session,
    monkeypatch,
):
    from core.db_models import User, WeatherAlert

    fixed_now = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)
    user = User.query.filter_by(username='testuser').one()
    user.community = '都昌'
    db_session.add(WeatherAlert(
        alert_date=datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc),
        location='都昌',
        alert_type='高温预警',
        alert_level='红色',
        description='测试预警详情',
    ))
    db_session.commit()

    monkeypatch.setattr('services.user.dashboard_service.utcnow', lambda: fixed_now)
    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda _location: ({'data_source': 'Demo', 'is_mock': True}, False),
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda _location, days=7: ([], False, {'error': 'qweather_unavailable'}),
    )

    response = authenticated_client.get('/dashboard')

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '高温预警 · 红色' in html
    assert '2026-01-02 · 都昌' in html
    assert '测试预警详情' in html
    assert 'yl-alert-item level-high' in html
    assert '<strong></strong>' not in html
