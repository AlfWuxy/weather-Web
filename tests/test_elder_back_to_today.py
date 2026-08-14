# -*- coding: utf-8 -*-
"""老人模式左下角「回到今天」回归测试。"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username='elder_back_today_user', role='user', community='都昌'):
    from core.db_models import User

    user = User(username=username, role=role, community=community)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def _patch_unavailable_weather(monkeypatch, temperature=36.5):
    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda location: ({
            'temperature': temperature,
            'temperature_max': 39,
            'temperature_min': None,
            'humidity': 80,
            'pressure': 1000,
            'weather_condition': '晴',
            'wind_speed': 2,
            'aqi': 88,
            'is_mock': False,
            'data_source': 'QWeather',
        }, False),
        raising=False,
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'qweather_unavailable'}),
        raising=False,
    )


def _back_today_html(body):
    match = re.search(
        r'<a\b[^>]*\byl-elder-back-today\b[^>]*>.*?</a>',
        body,
        re.S,
    )
    assert match, '老人模式必须有「回到今天」锚点按钮'
    return match.group(0)


def _hero_html(body):
    match = re.search(
        r'<section\b[^>]*\byl-elder-hero\b[^>]*>',
        body,
    )
    assert match, '老人模式必须有今日小结 hero'
    return match.group(0)


def _css_rule(css, selector):
    match = re.search(
        re.escape(selector) + r' \{(?P<body>.*?)\n\}',
        css,
        re.S,
    )
    assert match, f'必须存在选择器 {selector}'
    return match.group('body')


def test_elder_mode_has_in_page_back_to_today_not_self_refresh(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session)
    _login_as(client, user.id)
    _patch_unavailable_weather(monkeypatch)

    response = client.get('/elder-mode')
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    hero = _hero_html(body)
    assert 'id="today"' in hero

    back = _back_today_html(body)
    assert '回到今天' in back
    assert 'href="#today"' in back
    assert 'href="/elder-mode"' not in back
    assert 'btn-danger' not in back
    assert 'hidden' in back
    assert 'id="yl-elder-back-today"' in back

    scripts = re.findall(r'<script>(.*?)</script>', body, re.S)
    back_script = next((script for script in scripts if 'IntersectionObserver' in script), '')
    assert back_script
    assert "getElementById('today')" in back_script
    assert "querySelector('.yl-elder-call-card')" in back_script
    assert 'href="/elder-mode"' not in back_script

    assert '<h2>一键联系</h2>' in body
    assert '天气更新中' in body
    assert '补水、通风并避免暴晒' in body
    assert '36.5' not in body
    assert 'data-yl-open-120' in body
    assert 'showModal' in body
    assert 'window.confirm' not in body


def test_guest_elder_mode_also_has_back_to_today(client, db_session, monkeypatch):
    _patch_unavailable_weather(monkeypatch)

    guest = client.get('/guest?next=/elder-mode', follow_redirects=True)
    assert guest.status_code == 200
    body = guest.get_data(as_text=True)

    back = _back_today_html(body)
    assert 'href="#today"' in back
    assert '回到今天' in back
    assert 'href="/elder-mode"' not in back
    assert 'id="today"' in _hero_html(body)
    assert '<h2>一键联系</h2>' in body
    assert '天气更新中' in body
    assert '36.5' not in body


def test_back_to_today_css_is_left_fixed_scoped_and_not_full_width():
    css = (PROJECT_ROOT / 'static/css/yilao.css').read_text(encoding='utf-8')
    back_css = _css_rule(css, 'body.elder-focus-page .yl-elder-back-today')

    assert 'position: fixed' in back_css
    assert 'left:' in back_css
    assert 'bottom:' in back_css
    assert 'right: auto' in back_css
    assert 'width: auto' in back_css
    assert 'width: 100%' not in back_css
    assert 'btn-danger' not in back_css
    z_match = re.search(r'z-index:\s*(\d+)', back_css)
    assert z_match
    assert int(z_match.group(1)) < 1030

    assert 'body.elder-mode .yl-elder-back-today' not in css
    assert 'body.elder-mode a.yl-elder-back-today' not in css

    visible_css = _css_rule(css, 'body.elder-focus-page .yl-elder-back-today.is-visible')
    assert 'inline-flex' in visible_css
    hidden_css = _css_rule(css, 'body.elder-focus-page .yl-elder-back-today[hidden]')
    assert 'display: none' in hidden_css


def test_back_to_today_markup_and_js_stay_in_elder_template_only():
    template = (PROJECT_ROOT / 'templates/elder_dashboard.html').read_text(encoding='utf-8')
    extra = template.split('{% block extra_js %}', 1)[1]

    assert 'id="today"' in template
    assert 'yl-elder-back-today' in template
    assert '回到今天' in template
    assert 'href="#today"' in template
    assert 'href="/elder-mode"' not in template.split('{% block extra_js %}', 1)[0]
    assert 'IntersectionObserver' in extra
    assert "querySelector('.yl-elder-call-card')" in extra
    assert 'showModal' in extra
    assert 'window.confirm' not in extra

    hits = []
    for path in (PROJECT_ROOT / 'templates').rglob('*.html'):
        text = path.read_text(encoding='utf-8')
        if '回到今天' in text or 'yl-elder-back-today' in text:
            hits.append(str(path.relative_to(PROJECT_ROOT)))
    assert hits == ['templates/elder_dashboard.html']

    base = (PROJECT_ROOT / 'templates/base.html').read_text(encoding='utf-8')
    assert '回到今天' not in base
    assert 'yl-elder-back-today' not in base
    assert 'yl-elder-back-today' not in (PROJECT_ROOT / 'static/js/ai-floating-chat.js').read_text(encoding='utf-8')


def test_home_action_and_429_do_not_render_back_to_today(client, db_session):
    home = client.get('/').get_data(as_text=True)
    action = client.get('/action').get_data(as_text=True)

    for body in (home, action):
        assert '回到今天' not in body
        assert 'yl-elder-back-today' not in body

    page_429 = PROJECT_ROOT / 'templates/429.html'
    if page_429.exists():
        text = page_429.read_text(encoding='utf-8')
        assert '回到今天' not in text
        assert 'yl-elder-back-today' not in text
