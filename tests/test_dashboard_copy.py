# -*- coding: utf-8 -*-
from pathlib import Path


def test_dashboard_headlines_do_not_treat_unknown_as_low():
    from core.dashboard_copy import load_dashboard_copy, select_dashboard_headline

    load_dashboard_copy.cache_clear()
    copy = load_dashboard_copy()
    unknown_today = select_dashboard_headline(
        copy, section='today', risk_level='mystery', weather_available=True
    )
    unknown_elder = select_dashboard_headline(
        copy, section='elder', risk_level='mystery', weather_available=True
    )
    low_today = select_dashboard_headline(
        copy, section='today', risk_level='low', weather_available=True
    )

    assert '风险较低' not in unknown_today
    assert '可以按平常来' not in unknown_elder
    assert low_today == copy['today']['headlines']['low']
    assert copy['today']['empty_plan']['title'] == '先做好日常防护'
    assert copy['elder']['empty_plan']['title'] == '先做好日常防护'


def test_dashboard_templates_read_headlines_from_json():
    from core.dashboard_copy import load_dashboard_copy

    copy = load_dashboard_copy()
    today_html = Path('/workspace/templates/user_dashboard.html').read_text(encoding='utf-8')
    elder_html = Path('/workspace/templates/elder_dashboard.html').read_text(encoding='utf-8')

    assert '{{ today_headline }}' in today_html
    assert '{{ elder_headline }}' in elder_html
    assert copy['today']['headlines']['low'] not in today_html
    assert copy['elder']['headlines']['low'] not in elder_html
    assert 'dashboard_copy.today.empty_plan.title' in today_html
    assert 'dashboard_copy.elder.empty_plan.title' in elder_html
