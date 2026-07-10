# -*- coding: utf-8 -*-
import re

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
