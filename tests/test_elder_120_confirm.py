# -*- coding: utf-8 -*-
"""老人模式顶部 120 确认卡回归测试。"""
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username='elder_120_user', role='user', community='都昌'):
    from core.db_models import User

    user = User(username=username, role=role, community=community)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def _add_member_with_prefs(db_session, user_id, name, contact_prefs):
    from core.db_models import FamilyMember, FamilyMemberProfile

    member = FamilyMember(user_id=user_id, name=name, relation='父亲', age=76)
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        contact_prefs=json.dumps(contact_prefs, ensure_ascii=False),
    ))
    db_session.commit()
    return member


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


def _sos_html(body):
    start = body.find('class="yl-elder-sos"')
    end = body.find('</aside>', start)
    assert start != -1
    assert end != -1
    return body[start:end]


def _dialog_html(body):
    start = body.find('id="yl-elder-120-dialog"')
    end = body.find('</dialog>', start)
    assert start != -1
    assert end != -1
    return body[start:end]


def _call_card_html(body):
    start = body.find('class="yl-elder-call-card"')
    end = body.find('</div>', start)
    assert start != -1
    return body[start:end + len('</div>')]


def test_guest_elder_mode_shows_120_dialog_and_register_not_family_members(
    client,
    db_session,
    monkeypatch,
):
    _patch_unavailable_weather(monkeypatch)

    guest = client.get('/guest?next=/elder-mode', follow_redirects=True)
    assert guest.status_code == 200
    body = guest.get_data(as_text=True)

    sos = _sos_html(body)
    dialog = _dialog_html(body)
    call_card = _call_card_html(body)

    assert 'data-yl-open-120' in sos
    assert 'href="tel:120"' not in sos
    assert '请让身边人联系家属或社区' in sos
    assert 'href="/register"' in sos
    assert '/family-members' not in sos
    assert '/family-members' not in body
    assert 'yl-elder-sos-family' not in body

    assert '<dialog' in body
    assert 'href="tel:120"' in dialog
    assert 'showModal' in body
    assert 'window.confirm' not in body
    assert 'a[href^="tel:"]' not in body
    assert "a[href^='tel:']" not in body

    assert '<h2>一键联系</h2>' in call_card
    assert '还没有设置紧急联系人。' in call_card
    assert '注册后设置' in call_card
    assert '天气更新中' in body
    assert '补水、通风并避免暴晒' in body
    assert '36.5' not in body


def test_family_phone_is_direct_tel_and_does_not_use_emergency_phone(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session, username='elder_120_family_user')
    _add_member_with_prefs(
        db_session,
        user.id,
        '父亲',
        {
            'phone': '13800138000',
            'emergency_name': '邻居王叔',
            'emergency_phone': '13900139000',
        },
    )
    _login_as(client, user.id)
    _patch_unavailable_weather(monkeypatch)

    response = client.get('/elder-mode')
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    sos = _sos_html(body)
    dialog = _dialog_html(body)
    call_card = _call_card_html(body)

    assert 'yl-elder-sos-family' in sos
    assert 'href="tel:13800138000"' in sos
    assert '13900139000' not in sos
    assert '请让身边人联系家属或社区' not in body
    assert 'href="/register"' not in sos

    assert 'href="tel:120"' in dialog
    assert '13800138000' not in dialog
    assert '13900139000' not in dialog
    assert 'data-yl-open-120' in sos
    assert 'window.confirm' not in body
    assert 'a[href^="tel:"]' not in body

    assert '<h2>一键联系</h2>' in call_card
    assert '邻居王叔' in call_card
    assert 'href="tel:13900139000"' in call_card
    assert '现在拨打' in call_card
    assert '天气更新中' in body
    assert '补水、通风并避免暴晒' in body
    assert '36.5' not in body


def test_top_family_tel_never_falls_back_to_emergency_phone(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session, username='elder_120_emergency_only')
    _add_member_with_prefs(
        db_session,
        user.id,
        '母亲',
        {
            'emergency_name': '紧急联系人',
            'emergency_phone': '13700137000',
        },
    )
    _login_as(client, user.id)
    _patch_unavailable_weather(monkeypatch)

    body = client.get('/elder-mode').get_data(as_text=True)
    sos = _sos_html(body)
    call_card = _call_card_html(body)

    assert 'yl-elder-sos-family' not in sos
    assert '13700137000' not in sos
    assert 'href="tel:13700137000"' in call_card
    assert '<h2>一键联系</h2>' in call_card
    assert '去设置联系人' not in call_card


def test_logged_in_without_family_phone_does_not_link_sos_to_family_members(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session, username='elder_120_no_phone')
    _login_as(client, user.id)
    _patch_unavailable_weather(monkeypatch)

    body = client.get('/elder-mode').get_data(as_text=True)
    sos = _sos_html(body)
    call_card = _call_card_html(body)

    assert '/family-members' not in sos
    assert 'yl-elder-sos-family' not in sos
    assert 'href="/family-members"' in call_card
    assert '去设置联系人' in call_card


def test_sos_css_is_sticky_scoped_and_below_navbar_zindex():
    css = (PROJECT_ROOT / 'static/css/yilao.css').read_text(encoding='utf-8')
    sos_match = re.search(
        r'body\.elder-focus-page \.yl-elder-sos \{(?P<body>.*?)\n\}',
        css,
        re.S,
    )
    assert sos_match, 'SOS 卡样式必须写在 body.elder-focus-page 下'
    sos_css = sos_match.group('body')
    assert 'position: sticky' in sos_css
    assert 'position: fixed' not in sos_css
    assert 'top: 4.5rem' in sos_css
    z_match = re.search(r'z-index:\s*(\d+)', sos_css)
    assert z_match
    assert int(z_match.group(1)) < 1020

    base = (PROJECT_ROOT / 'templates/base.html').read_text(encoding='utf-8')
    assert 'yl-elder-sos' not in base
    assert 'tel:120' not in base
    assert 'yl-elder-120-dialog' not in base

    config = (PROJECT_ROOT / 'core/config.py').read_text(encoding='utf-8')
    assert "os.getenv('FEATURE_ELDER_MODE', '0')" in config
