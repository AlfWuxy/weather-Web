# -*- coding: utf-8 -*-
"""P4: web check-in stays posted; Mini Program only exposes a reverse /action entry."""

import re
from datetime import timedelta
from pathlib import Path

from core.db_models import DailyStatus, Pair, PairActionToken, User
from core.extensions import db
from core.security import hash_pair_token, hash_short_code
from core.time_utils import today_local, utcnow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTION_TEMPLATE = PROJECT_ROOT / "templates" / "action_checkin.html"
MP_BRIDGE_PARTIAL = PROJECT_ROOT / "templates" / "partials" / "mp_bridge.html"
YILAO_CSS = PROJECT_ROOT / "static" / "css" / "yilao.css"
MP_CONFIG = PROJECT_ROOT / "miniprogram" / "config.js"
ELDERS_WXML = PROJECT_ROOT / "miniprogram" / "pages" / "elders" / "index.wxml"
ELDERS_JS = PROJECT_ROOT / "miniprogram" / "pages" / "elders" / "index.js"
BIND_WXML = PROJECT_ROOT / "miniprogram" / "pages" / "bind-token" / "index.wxml"
BIND_JS = PROJECT_ROOT / "miniprogram" / "pages" / "bind-token" / "index.js"
QR_PATH = PROJECT_ROOT / "static" / "brand" / "mp-qrcode.png"
AVATAR_PATH = PROJECT_ROOT / "static" / "brand" / "yilao-avatar.png"


def _create_user(username, password):
    user = User(username=username, role="user")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _create_pair(user, short_code):
    pair = Pair(
        caregiver_id=user.id,
        community_code="都昌",
        location_query="都昌",
        elder_code=f"elder-{short_code}",
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        short_code_expires_at=utcnow() + timedelta(days=90),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db.session.add(pair)
    db.session.commit()
    return pair


def test_action_checkin_title_and_preserved_forms():
    template = ACTION_TEMPLATE.read_text(encoding="utf-8")
    assert "{% block title %}行动打卡 · 宜老天气通{% endblock %}" in template
    assert template.count('{% include "partials/mp_bridge.html" %}') == 2
    assert 'action="{{ entry_action or url_for(\'public.action_check\') }}"' in template
    assert 'action="{{ confirm_action or url_for(\'public.action_confirm\') }}"' in template
    assert 'action="{{ help_action or url_for(\'public.action_help\') }}"' in template
    assert 'action="{{ debrief_action or url_for(\'public.action_debrief\') }}"' in template
    assert 'name="csrf_token"' in template
    assert 'name="short_code"' in template
    assert "进入行动页面" in template
    assert "我很安全" in template
    assert "我需要帮助" in template
    assert "提交复盘" in template
    assert "window.matchMedia('(prefers-reduced-motion: reduce)').matches" in template
    assert "behavior: reduceMotion ? 'auto' : 'smooth'" in template


def test_mp_bridge_partial_uses_qr_path_with_onerror_not_avatar():
    partial = MP_BRIDGE_PARTIAL.read_text(encoding="utf-8")
    assert "brand/mp-qrcode.png" in partial
    assert "yilao-avatar" not in partial
    assert "onerror=" in partial
    assert "data-qr-missing" in partial
    assert "宜老天气通" in partial
    assert "家属在小程序里绑定 Token" in partial
    assert "老人仍在本页输入短码完成确认" in partial
    assert "小程序本批不能打卡" in partial
    assert "weixin://" not in partial
    assert "web-view" not in partial
    if QR_PATH.exists() and AVATAR_PATH.exists():
        assert QR_PATH.read_bytes() != AVATAR_PATH.read_bytes()


def test_mp_bridge_steps_are_css_numbered():
    css = YILAO_CSS.read_text(encoding="utf-8")
    assert "counter-reset: yl-mp-step" in css
    assert "counter-increment: yl-mp-step" in css
    assert "content: counter(yl-mp-step)" in css


def test_miniprogram_copies_web_action_url_without_native_checkin():
    config = MP_CONFIG.read_text(encoding="utf-8")
    elders_wxml = ELDERS_WXML.read_text(encoding="utf-8")
    elders_js = ELDERS_JS.read_text(encoding="utf-8")
    bind_wxml = BIND_WXML.read_text(encoding="utf-8")
    bind_js = BIND_JS.read_text(encoding="utf-8")

    assert "WEB_ACTION_URL: '/action'" in config
    for blob in (elders_wxml, bind_wxml):
        assert "网页也能打卡" in blob
        assert "copyWebAction" in blob
        assert "{{webActionUrl}}" in blob
        assert "web-view" not in blob
        assert "weixin://" not in blob
    for blob in (elders_js, bind_js):
        assert "WEB_ACTION_URL" in blob
        assert "wx.setClipboardData" in blob
        assert "web-view" not in blob
        assert "weixin://" not in blob
        assert "navigateToMiniProgram" not in blob
        assert "/mp/api/v1/checkin" not in blob


def test_action_lookup_renders_mp_bridge(app, client):
    with app.app_context():
        db.create_all()
    resp = client.get("/action")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "行动打卡 · 宜老天气通" in html
    assert "data-mp-bridge" in html
    assert "/static/brand/mp-qrcode.png" in html
    assert "微信小程序名称" in html
    assert "宜老天气通" in html
    assert "老人仍在本页输入短码完成确认" in html
    assert 'onerror=' in html
    qr_tags = re.findall(r'<img\b[^>]*class="yl-mp-bridge__qr"[^>]*>', html, flags=re.I | re.S)
    assert qr_tags
    for tag in qr_tags:
        assert 'mp-qrcode.png' in tag
        assert 'yilao-avatar' not in tag
        assert 'onerror=' in tag


def test_action_respond_keeps_confirm_help_and_shows_bridge(app, client):
    with app.app_context():
        db.create_all()
        user = _create_user("bridge_lookup_user", "bridge_lookup_pass")
        pair = _create_pair(user, "13572468")
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "bridge-lookup-csrf"

    lookup = client.post(
        "/action",
        data={"short_code": "13572468", "csrf_token": "bridge-lookup-csrf"},
        follow_redirects=False,
    )
    assert lookup.status_code == 200
    html = lookup.get_data(as_text=True)
    assert "我很安全" in html
    assert "我需要帮助" in html
    assert "data-mp-bridge" in html
    assert "/static/brand/mp-qrcode.png" in html

    confirm = client.post(
        "/action/confirm",
        data={"short_code": "13572468", "csrf_token": "bridge-lookup-csrf"},
        follow_redirects=False,
    )
    assert confirm.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        assert status.confirmed_at is not None
        assert status.help_flag is not True


def test_action_help_post_still_sets_help_flag(app, client):
    with app.app_context():
        db.create_all()
        user = _create_user("bridge_help_user", "bridge_help_pass")
        pair = _create_pair(user, "24681357")
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "bridge-help-csrf"

    resp = client.post(
        "/action/help",
        data={"short_code": "24681357", "csrf_token": "bridge-help-csrf"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        assert status.help_flag is True
        assert status.confirmed_at is None


def test_token_checkin_post_still_confirms(app, client):
    with app.app_context():
        db.create_all()
        user = _create_user("bridge_token_user", "bridge_token_pass")
        pair = _create_pair(user, "11223344")
        db.session.add(PairActionToken(
            pair_id=pair.id,
            token_hash=hash_pair_token("bridge-action-token"),
            expires_at=utcnow() + timedelta(days=90),
            created_at=utcnow(),
        ))
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "bridge-token-csrf"

    resp = client.post(
        "/e/bridge-action-token/checkin",
        data={"short_code": "11223344", "csrf_token": "bridge-token-csrf"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        assert status.confirmed_at is not None


def test_mp_checkin_api_is_not_added(app, client):
    resp = client.post("/mp/api/v1/checkin", json={"short_code": "000000"})
    assert resp.status_code in (404, 405)
    mp_api = (PROJECT_ROOT / "blueprints" / "mp_api.py").read_text(encoding="utf-8")
    assert "/checkin" not in mp_api
    assert "action_confirm" not in mp_api
