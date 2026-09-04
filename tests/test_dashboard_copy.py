# -*- coding: utf-8 -*-
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_dashboard_family_cards_do_not_invent_low_when_risk_missing():
    today_html = (_REPO_ROOT / 'templates' / 'user_dashboard.html').read_text(encoding='utf-8')
    family_html = (_REPO_ROOT / 'templates' / 'family_members.html').read_text(encoding='utf-8')
    assert "m.risk_label or '低'" not in today_html
    assert "m.risk_level or 'low'" not in today_html
    assert "m.risk_label or '低风险'" not in family_html
    assert "m.risk_level or 'low'" not in family_html
    assert "m.risk_label or '风险未知'" in family_html
    assert '{{ m.age }}岁' not in family_html
    assert '{{ m.age }}岁' not in today_html


def test_dashboard_templates_read_headlines_from_json():
    from core.dashboard_copy import load_dashboard_copy

    copy = load_dashboard_copy()
    today_html = (_REPO_ROOT / 'templates' / 'user_dashboard.html').read_text(encoding='utf-8')
    elder_html = (_REPO_ROOT / 'templates' / 'elder_dashboard.html').read_text(encoding='utf-8')

    assert '{{ today_headline }}' in today_html
    assert '{{ elder_headline }}' in elder_html
    assert copy['today']['headlines']['low'] not in today_html
    assert copy['elder']['headlines']['low'] not in elder_html
    assert 'dashboard_copy.today.empty_plan.title' in today_html
    assert 'dashboard_copy.elder.empty_plan.title' in elder_html
    assert 'dashboard_copy.elder.empty_plan.detail' in elder_html
    assert '先补水、通风并避免暴晒，不舒服时及时联系家人。' not in elder_html
