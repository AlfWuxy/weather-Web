# -*- coding: utf-8 -*-
"""公开行动 token 流程回归测试。"""

import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest

from core.db_models import CommunityDaily, DailyStatus, Debrief, Pair, PairActionToken, PairLink, UsageEvent, User
from core.security import hash_pair_token, hash_short_code
from core.time_utils import today_local, utcnow
from core.extensions import db


def _login(client, username, password):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "test-csrf-token"
    resp = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": "test-csrf-token",
        },
        follow_redirects=False,
    )
    return resp


def _create_user(username="u_test", password="pass123"):
    user = User(username=username, role="user")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _create_action_token_pair(user, short_code, action_token):
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
    db.session.flush()
    token_record = PairActionToken(
        pair_id=pair.id,
        token_hash=hash_pair_token(action_token),
        expires_at=utcnow() + timedelta(days=90),
        created_at=utcnow(),
    )
    db.session.add(token_record)
    db.session.commit()
    return pair.id, token_record.id


def _block_public_action_external_calls(monkeypatch):
    """行动回归只使用本地模拟天气，任何 HTTP 通知都应立即失败。"""
    from services import public_service

    def local_action_context(pair, status_date):
        status = public_service._get_or_create_daily_status(pair, status_date, None)
        actions = [
            {"id": "drink_water", "title": "喝水", "detail": "本地测试行动"},
            {"id": "cool_rest", "title": "休息", "detail": "本地测试行动"},
        ]
        return status, actions, [], None, None, None, []

    monkeypatch.setattr(public_service, "_build_action_context", local_action_context)
    monkeypatch.setattr(
        public_service,
        "get_weather_with_cache",
        lambda _location: ({"is_mock": True, "data_source": "Demo"}, False),
    )
    monkeypatch.setattr(
        "requests.post",
        lambda *_args, **_kwargs: pytest.fail("行动处理器不得发送真实通知"),
    )


class _WechatResponse:
    status_code = 200

    def __init__(self, openid):
        self.openid = openid

    def json(self):
        return {"openid": self.openid, "session_key": "test-session-key"}


def _wechat_login(app, client, monkeypatch, openid):
    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-test-secret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
        WX_MINIPROGRAM_SESSION_TTL_SECONDS=3600,
    )
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse(openid),
    )
    return client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "wx-login-code", "privacy_consent_version": "privacy-v1"},
    )


@pytest.mark.parametrize(
    ("action_path", "short_code", "action_token", "extra_form"),
    (
        (
            "checkin",
            "61000001",
            "formal-readonly-confirm-token",
            {"actions_done": ["drink_water"]},
        ),
        ("help", "61000002", "formal-readonly-help-token", {}),
        (
            "debrief",
            "61000003",
            "formal-readonly-debrief-token",
            {"question_2": "不应保存", "debrief_optin": "1"},
        ),
    ),
)
def test_formal_wechat_runtime_rejects_web_token_writes_before_pair_resolution(
    app,
    client,
    monkeypatch,
    action_path,
    short_code,
    action_token,
    extra_form,
):
    """正式微信态应在解析 Pair 和构建天气前拒绝三类 Web 写入。"""
    from services import public_service

    app.config["WECHAT_FORMAL_RUNTIME"] = True

    def fail_before_pair_resolution(*_args, **_kwargs):
        pytest.fail("正式微信态不应解析 Web 家庭行动 Pair")

    def fail_before_weather_context(*_args, **_kwargs):
        pytest.fail("正式微信态不应为 Web 写动作构建天气上下文")

    monkeypatch.setattr(
        public_service,
        "_resolve_pair_from_session_or_code",
        fail_before_pair_resolution,
    )
    monkeypatch.setattr(
        public_service,
        "_build_action_context",
        fail_before_weather_context,
    )

    with app.app_context():
        db.create_all()
        user = _create_user(
            f"formal_readonly_{action_path}_owner",
            "formal-readonly-password",
        )
        user_id = user.id
        pair_id, token_id = _create_action_token_pair(user, short_code, action_token)

    csrf_token = f"formal-readonly-{action_path}-csrf"
    with client.session_transaction() as sess:
        sess["_csrf_token"] = csrf_token

    response = client.post(
        f"/e/{action_token}/{action_path}",
        data={
            "short_code": short_code,
            "csrf_token": csrf_token,
            **extra_form,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/action" in (response.headers.get("Location") or "")
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes", [])
    assert any(
        "微信小程序中登录后" in message
        for _category, message in flashes
    )

    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert Debrief.query.filter_by(owner_user_id=user_id).count() == 0
        assert UsageEvent.query.filter_by(user_id=user_id).count() == 0
        assert db.session.get(PairActionToken, token_id).used_at is None


def test_formal_wechat_runtime_stops_web_entry_before_reading_family_data(
    app,
    client,
    monkeypatch,
):
    """正式微信态只显示停用说明，不解析短码、家庭或天气。"""
    from services import public_service

    app.config["WECHAT_FORMAL_RUNTIME"] = True

    def fail_before_family_resolution(*_args, **_kwargs):
        pytest.fail("正式微信态不应解析家庭短码")

    def fail_before_weather_context(*_args, **_kwargs):
        pytest.fail("正式微信态不应构建家庭天气上下文")

    monkeypatch.setattr(public_service, "_resolve_pair", fail_before_family_resolution)
    monkeypatch.setattr(public_service, "_build_action_context", fail_before_weather_context)

    with app.app_context():
        db.create_all()
        user = _create_user("formal_readonly_page_owner", "formal-page-password")
        pair_id, token_id = _create_action_token_pair(
            user,
            "61000004",
            "formal-readonly-page-token",
        )

    csrf_token = "formal-readonly-page-csrf"
    with client.session_transaction() as sess:
        sess["_csrf_token"] = csrf_token

    entry = client.get("/e/formal-readonly-page-token", follow_redirects=False)
    assert entry.status_code == 200
    entry_body = entry.get_data(as_text=True)
    assert "当前网页仅供查看停用说明" in entry_body
    assert 'name="short_code"' not in entry_body
    response = client.post(
        "/elder/enter",
        data={"short_code": "61000004", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "当前网页仅供查看停用说明" in body
    assert "不会读取短码、兑换安全链接或写入家庭记录" in body
    assert 'name="short_code"' not in body
    assert 'action="/e/formal-readonly-page-token/checkin"' not in body
    assert 'action="/e/formal-readonly-page-token/help"' not in body
    assert 'action="/e/formal-readonly-page-token/debrief"' not in body
    assert ">我很安全<" not in body
    assert ">我需要帮助<" not in body
    assert ">提交复盘<" not in body
    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert db.session.get(PairActionToken, token_id).used_at is None


def test_formal_wechat_runtime_stops_debrief_get_before_family_lookup(
    app,
    client,
    monkeypatch,
):
    """复盘 GET 也必须在短码、Pair、天气和状态查询前显示停用说明。"""
    from services import public_service

    app.config["WECHAT_FORMAL_RUNTIME"] = True

    def fail_before_family_access(*_args, **_kwargs):
        pytest.fail("正式微信态的复盘 GET 不得访问家庭资料")

    monkeypatch.setattr(
        "blueprints.public._resolve_pair_from_session_or_code",
        fail_before_family_access,
    )
    monkeypatch.setattr(
        "blueprints.public._validate_pair_token_binding",
        fail_before_family_access,
    )
    monkeypatch.setattr(
        "blueprints.public._build_action_context",
        fail_before_family_access,
    )
    monkeypatch.setattr(
        public_service,
        "_resolve_pair",
        fail_before_family_access,
    )

    response = client.get(
        "/e/formal-debrief-get-token/debrief?short_code=61000005",
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "当前网页仅供查看停用说明" in body
    assert 'name="short_code"' not in body


def test_action_context_does_not_persist_mock_weather_risk(app, monkeypatch):
    """模拟天气只能保留安全确认入口，不能落库风险等级。"""
    from services.public_service import _build_action_context

    with app.app_context():
        db.create_all()
        user = _create_user("mock_action_user", "mock_action_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-mock-action",
            short_code="12121212",
            short_code_hash=hash_short_code("12121212"),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()

        monkeypatch.setattr(
            'services.public_service.get_weather_with_cache',
            lambda _location: ({
                'temperature': 20.0,
                'temperature_max': 25.0,
                'temperature_min': 15.0,
                'humidity': 60.0,
                'data_source': 'Demo',
                'is_mock': True,
            }, False),
        )

        status, actions, _resources, weather, heat_result, risk_label, reasons = (
            _build_action_context(pair, today_local())
        )
        db.session.commit()

        assert status.risk_level is None
        assert actions == []
        assert weather is None
        assert heat_result is None
        assert risk_label is None
        assert reasons == []


def test_pair_management_can_create_pair(app, client):
    """Web 端创建绑定不应因 _generate_elder_code 缺失而失败。"""
    with app.app_context():
        db.create_all()
        user = _create_user("pair_user", "pair_pass")
        user_id = user.id

    resp = _login(client, "pair_user", "pair_pass")
    assert resp.status_code == 302

    with client.session_transaction() as sess:
        csrf_token = sess.get("_csrf_token")

    resp = client.post(
        "/pairs",
        data={"location_query": "北京市", "csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        pair = Pair.query.filter_by(caregiver_id=user_id).first()
        assert pair is not None
        assert bool(pair.elder_code)
        assert bool(pair.short_code)
        assert pair.short_code_expires_at is not None


def test_token_route_rejects_mismatched_token(app, client):
    """带 token 路由必须校验 token 与短码绑定关系。"""
    with app.app_context():
        db.create_all()
        user = _create_user("token_user_a", "token_pass_a")

        short_code = "99887766"
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-a",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()

        link = PairLink(
            caregiver_id=user.id,
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            token_hash=hash_pair_token("valid-token-a"),
            community_code="都昌",
            status="redeemed",
            pair_id=pair.id,
            expires_at=utcnow() + timedelta(days=1),
            created_at=utcnow(),
        )
        db.session.add(link)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "token-csrf-a"

    resp = client.post(
        "/e/wrong-token-a/checkin",
        data={"short_code": "99887766", "csrf_token": "token-csrf-a"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/action" in (resp.headers.get("Location") or "")

    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is None or status.confirmed_at is None


def test_token_route_accepts_valid_token(app, client):
    """带 token 路由在 token 正确时应允许正常提交。"""
    with app.app_context():
        db.create_all()
        user = _create_user("token_user_b", "token_pass_b")

        short_code = "88776655"
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-b",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()

        link = PairLink(
            caregiver_id=user.id,
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            token_hash=hash_pair_token("valid-token-b"),
            community_code="都昌",
            status="redeemed",
            pair_id=pair.id,
            expires_at=utcnow() + timedelta(days=1),
            created_at=utcnow(),
        )
        db.session.add(link)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "token-csrf-b"

    resp = client.post(
        "/e/valid-token-b/checkin",
        data={"short_code": "88776655", "csrf_token": "token-csrf-b"},
        follow_redirects=False,
    )
    assert resp.status_code == 200

    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is not None
        assert status.confirmed_at is not None


def test_pair_action_token_route_accepts_valid_token(app, client):
    """新的行动 token 表应支持带 token 的确认路径。"""
    with app.app_context():
        db.create_all()
        user = _create_user("action_token_user", "action_token_pass")

        short_code = "77889900"
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-action-token",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()

        db.session.add(PairActionToken(
            pair_id=pair.id,
            token_hash=hash_pair_token("valid-action-token"),
            expires_at=utcnow() + timedelta(days=90),
            created_at=utcnow(),
        ))
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "action-token-csrf"

    resp = client.post(
        "/e/valid-action-token/checkin",
        data={"short_code": "77889900", "csrf_token": "action-token-csrf"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is not None
        assert status.confirmed_at is not None


def test_web_action_token_confirm_persists_status_and_safe_event(
    app,
    client,
    monkeypatch,
):
    """Web token 确认应同步落库状态与账号级匿名事件。"""
    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_confirm_owner", "web_confirm_pass")
        user_id = user.id
        pair_id, _token_id = _create_action_token_pair(
            user,
            "31415926",
            "web-confirm-token",
        )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-confirm-csrf"

    response = client.post(
        "/e/web-confirm-token/checkin",
        data={
            "short_code": "31415926",
            "actions_done": ["drink_water", "cool_rest"],
            "csrf_token": "web-confirm-csrf",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        event = UsageEvent.query.filter_by(
            user_id=user_id,
            event_type="checkin_confirmed",
        ).one()
        assert status.confirmed_at is not None
        assert status.actions_done_count == 2
        assert json.loads(status.elder_actions) == ["drink_water", "cool_rest"]
        assert event.pair_id is None
        assert event.member_id is None
        assert event.source == "web"
        assert json.loads(event.meta_json) == {"actions_done_count": 2}


def test_web_action_rejects_duplicate_or_unknown_items_without_count_drift(
    app,
    client,
    monkeypatch,
):
    """伪造的重复或未知行动不能污染数量与明细。"""
    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_invalid_action_owner", "web_invalid_action_pass")
        user_id = user.id
        pair_id, _token_id = _create_action_token_pair(
            user,
            "31415928",
            "web-invalid-action-token",
        )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-invalid-action-csrf"

    duplicate = client.post(
        "/e/web-invalid-action-token/checkin",
        data={
            "short_code": "31415928",
            "actions_done": ["drink_water", "drink_water"],
            "csrf_token": "web-invalid-action-csrf",
        },
        follow_redirects=False,
    )
    unknown = client.post(
        "/e/web-invalid-action-token/checkin",
        data={
            "short_code": "31415928",
            "actions_done": ["unknown_action"],
            "csrf_token": "web-invalid-action-csrf",
        },
        follow_redirects=False,
    )

    assert duplicate.status_code == 400
    assert unknown.status_code == 400
    with app.app_context():
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).first()
        assert status is None or status.confirmed_at is None
        assert UsageEvent.query.filter_by(
            user_id=user_id,
            event_type="checkin_confirmed",
        ).count() == 0


def test_web_action_succeeds_when_community_projection_fails(
    app,
    client,
    monkeypatch,
):
    """派生投影失败时，已提交的 Web 主动作仍应返回成功。"""
    from services import community_daily_service

    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_projection_failure_owner", "web_projection_pass")
        pair_id, _token_id = _create_action_token_pair(
            user,
            "31415927",
            "web-projection-failure-token",
        )
        pair = db.session.get(Pair, pair_id)
        pair.community_code = "Web投影失败社区"
        db.session.commit()

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(
        community_daily_service,
        "refresh_community_daily",
        fail_projection,
    )
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-projection-failure-csrf"

    response = client.post(
        "/e/web-projection-failure-token/checkin",
        data={
            "short_code": "31415927",
            "actions_done": ["drink_water"],
            "csrf_token": "web-projection-failure-csrf",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        assert status.confirmed_at is not None
        assert CommunityDaily.query.filter_by(
            community_code="Web投影失败社区",
            date=today_local(),
        ).count() == 0


def test_web_action_token_help_records_manual_contact_guidance_and_safe_event(
    app,
    client,
    monkeypatch,
):
    """Web token 求助应明示直接联系，不宣称已自动通知。"""
    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_help_owner", "web_help_pass")
        user_id = user.id
        pair_id, _token_id = _create_action_token_pair(
            user,
            "27182818",
            "web-help-token",
        )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-help-csrf"

    response = client.post(
        "/e/web-help-token/help",
        data={"short_code": "27182818", "csrf_token": "web-help-csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    response_text = response.get_data(as_text=True)
    assert "求助需求已记录，请同时直接联系照护人" in response_text
    assert "照护人将收到提醒" not in response_text
    with app.app_context():
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        event = UsageEvent.query.filter_by(
            user_id=user_id,
            event_type="help_flagged",
        ).one()
        assert status.help_flag is True
        assert status.relay_stage == "caregiver"
        assert event.pair_id is None
        assert event.member_id is None
        assert event.source == "web"
        assert json.loads(event.meta_json) == {"relay_stage": "caregiver"}


def test_web_debrief_toggle_reuses_one_record_across_link_unlink_relink(
    app,
    client,
    monkeypatch,
):
    """Web 复盘关联开关往返切换时应始终更新同一行。"""
    from core.db_models import Debrief

    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_debrief_toggle_owner", "web_debrief_toggle_pass")
        user_id = user.id
        pair_id, _token_id = _create_action_token_pair(
            user,
            "24494897",
            "web-debrief-toggle-token",
        )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-debrief-toggle-csrf"

    def submit(question_2, optin):
        form = {
            "short_code": "24494897",
            "question_2": question_2,
            "csrf_token": "web-debrief-toggle-csrf",
        }
        if optin:
            form["debrief_optin"] = "1"
        return client.post(
            "/e/web-debrief-toggle-token/debrief",
            data=form,
            follow_redirects=False,
        )

    assert submit("首次关联", True).status_code == 200
    with app.app_context():
        linked = Debrief.query.filter_by(owner_user_id=user_id).one()
        debrief_id = linked.id
        assert linked.origin_pair_id == pair_id
        assert linked.pair_id == pair_id
        assert linked.community_code == "都昌"

    assert submit("关闭关联", False).status_code == 200
    with app.app_context():
        unlinked = Debrief.query.filter_by(owner_user_id=user_id).one()
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        assert unlinked.id == debrief_id
        assert unlinked.origin_pair_id == pair_id
        assert unlinked.pair_id is None
        assert unlinked.question_2 == "关闭关联"
        assert status.debrief_optin is False
        assert Debrief.query.filter_by(pair_id=pair_id, date=today_local()).first() is None

    assert submit("再次关联", True).status_code == 200
    with app.app_context():
        relinked = Debrief.query.filter_by(owner_user_id=user_id).one()
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        events = UsageEvent.query.filter_by(
            user_id=user_id,
            event_type="feedback_submitted",
        ).order_by(UsageEvent.id.asc()).all()
        assert relinked.id == debrief_id
        assert relinked.origin_pair_id == pair_id
        assert relinked.pair_id == pair_id
        assert relinked.question_2 == "再次关联"
        assert status.debrief_optin is True
        assert len(events) == 3
        assert [json.loads(event.meta_json)["optin"] for event in events] == [
            True,
            False,
            True,
        ]
        assert all(event.pair_id is None and event.member_id is None for event in events)


def test_web_debrief_repeated_optout_updates_the_same_unlinked_record(
    app,
    client,
    monkeypatch,
):
    """Web 复盘重复 optout 只更新 owner/date 下的同一未关联记录。"""
    from core.db_models import Debrief

    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_debrief_optout_owner", "web_debrief_optout_pass")
        user_id = user.id
        pair_id, _token_id = _create_action_token_pair(
            user,
            "26457513",
            "web-debrief-optout-token",
        )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-debrief-optout-csrf"

    first = client.post(
        "/e/web-debrief-optout-token/debrief",
        data={
            "short_code": "26457513",
            "question_2": "首次不关联",
            "csrf_token": "web-debrief-optout-csrf",
        },
        follow_redirects=False,
    )
    assert first.status_code == 200
    with app.app_context():
        first_record = Debrief.query.filter_by(owner_user_id=user_id).one()
        debrief_id = first_record.id
        assert first_record.origin_pair_id == pair_id
        assert first_record.pair_id is None

    second = client.post(
        "/e/web-debrief-optout-token/debrief",
        data={
            "short_code": "26457513",
            "question_2": "重复不关联",
            "csrf_token": "web-debrief-optout-csrf",
        },
        follow_redirects=False,
    )
    assert second.status_code == 200
    with app.app_context():
        repeated = Debrief.query.filter_by(owner_user_id=user_id).one()
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        events = UsageEvent.query.filter_by(
            user_id=user_id,
            event_type="feedback_submitted",
        ).order_by(UsageEvent.id.asc()).all()
        assert repeated.id == debrief_id
        assert repeated.origin_pair_id == pair_id
        assert repeated.pair_id is None
        assert repeated.question_2 == "重复不关联"
        assert status.debrief_optin is False
        assert len(events) == 2
        assert all(json.loads(event.meta_json)["optin"] is False for event in events)
        assert all(event.pair_id is None and event.member_id is None for event in events)


def test_web_debrief_origin_keeps_two_family_pairs_isolated(
    app,
    client,
    monkeypatch,
):
    """同一账号两位家人的未关联复盘不能互相覆盖或错误重联。"""
    from core.db_models import Debrief

    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user("web_debrief_two_pair_owner", "web_debrief_two_pair_pass")
        user_id = user.id
        first_pair_id, _first_token_id = _create_action_token_pair(
            user,
            "31622776",
            "web-debrief-first-pair-token",
        )
        second_pair_id, _second_token_id = _create_action_token_pair(
            user,
            "34641016",
            "web-debrief-second-pair-token",
        )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "web-debrief-two-pair-csrf"

    def submit(token, short_code, question_2, optin=False):
        form = {
            "short_code": short_code,
            "question_2": question_2,
            "csrf_token": "web-debrief-two-pair-csrf",
        }
        if optin:
            form["debrief_optin"] = "1"
        return client.post(
            f"/e/{token}/debrief",
            data=form,
            follow_redirects=False,
        )

    assert submit(
        "web-debrief-first-pair-token",
        "31622776",
        "第一位家人不关联",
    ).status_code == 200
    assert submit(
        "web-debrief-second-pair-token",
        "34641016",
        "第二位家人不关联",
    ).status_code == 200

    with app.app_context():
        first = Debrief.query.filter_by(
            owner_user_id=user_id,
            origin_pair_id=first_pair_id,
        ).one()
        second = Debrief.query.filter_by(
            owner_user_id=user_id,
            origin_pair_id=second_pair_id,
        ).one()
        first_id = first.id
        second_id = second.id
        assert first_id != second_id
        assert first.pair_id is None
        assert second.pair_id is None

    assert submit(
        "web-debrief-first-pair-token",
        "31622776",
        "第一位家人再次关联",
        optin=True,
    ).status_code == 200
    assert submit(
        "web-debrief-second-pair-token",
        "34641016",
        "第二位家人重复不关联",
    ).status_code == 200

    with app.app_context():
        records = Debrief.query.filter_by(owner_user_id=user_id).order_by(
            Debrief.id.asc()
        ).all()
        assert len(records) == 2
        first = next(row for row in records if row.origin_pair_id == first_pair_id)
        second = next(row for row in records if row.origin_pair_id == second_pair_id)
        assert first.id == first_id
        assert first.pair_id == first_pair_id
        assert first.question_2 == "第一位家人再次关联"
        assert second.id == second_id
        assert second.pair_id is None
        assert second.question_2 == "第二位家人重复不关联"


def test_pair_action_token_route_rejects_expired_token(app, client):
    """过期行动 token 不能提交确认。"""
    with app.app_context():
        db.create_all()
        user = _create_user("expired_action_token_user", "expired_action_token_pass")

        short_code = "11224466"
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-expired-token",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        db.session.add(PairActionToken(
            pair_id=pair.id,
            token_hash=hash_pair_token("expired-action-token"),
            expires_at=utcnow() - timedelta(seconds=1),
            created_at=utcnow() - timedelta(days=91),
        ))
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "expired-action-token-csrf"

    resp = client.post(
        "/e/expired-action-token/checkin",
        data={"short_code": "11224466", "csrf_token": "expired-action-token-csrf"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is None or status.confirmed_at is None


def test_generated_action_token_survives_short_code_expiry_and_is_reused(app, client):
    """新行动 token 使用自己的有效期，重复渲染链接不应持续新增记录。"""
    from services.user._common import _build_pair_action_link

    with app.app_context():
        db.create_all()
        user = _create_user("generated_token_user", "generated_token_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-generated-token",
            short_code="55667788",
            short_code_hash=hash_short_code("55667788"),
            short_code_expires_at=utcnow() - timedelta(seconds=1),
            status="active",
            created_at=utcnow() - timedelta(days=91),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        pair_id = pair.id

        with app.test_request_context("/pairs"):
            first_link = _build_pair_action_link(pair, external=False)
        db.session.commit()
        db.session.expire_all()

        pair = db.session.get(Pair, pair_id)
        with app.test_request_context("/pairs"):
            second_link = _build_pair_action_link(pair, external=False)
        db.session.commit()

        assert first_link == second_link
        assert PairActionToken.query.filter_by(pair_id=pair_id).count() == 1
        token = first_link.rsplit("/e/", 1)[-1].split("?", 1)[0]

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "generated-token-csrf"

    response = client.post(
        f"/e/{token}/checkin",
        data={"short_code": "55667788", "csrf_token": "generated-token-csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        assert status.confirmed_at is not None
        assert PairActionToken.query.filter_by(pair_id=pair_id).count() == 1


def test_sensitive_action_link_uses_trusted_public_base_not_request_host(
    app,
    db_session,
    monkeypatch,
):
    """带令牌链接只能使用可信配置 origin，恶意 Host 不得进入输出。"""
    from services.user._common import _build_pair_action_link

    user = _create_user("trusted-link-user", "trusted-link-password")
    pair = Pair(
        caregiver_id=user.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code="trusted-link-elder",
        short_code="66778899",
        short_code_hash=hash_short_code("66778899"),
        short_code_expires_at=utcnow() + timedelta(days=1),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", "https://yilaoweather.org")

    with app.test_request_context("/pairs", base_url="https://evil.example"):
        trusted_link = _build_pair_action_link(pair)
    db_session.commit()

    assert trusted_link.startswith("https://yilaoweather.org/e/")
    assert "evil.example" not in trusted_link

    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", "https://good.example/path")
    with app.test_request_context("/pairs", base_url="https://evil.example"):
        safe_relative_link = _build_pair_action_link(pair)
    db_session.commit()
    assert safe_relative_link.startswith("/e/")
    assert "evil.example" not in safe_relative_link


def test_caregiver_get_creates_token_only_for_active_pair(
    app,
    client,
    db_session,
    monkeypatch,
):
    """照护 GET 保留一键链接体验，同时 inactive Pair 始终保持无凭证。"""
    from services.user import caregiver_service

    user = _create_user("caregiver-get-token-user", "caregiver-get-token-pass")
    user.role = "caregiver"
    pairs = []
    for index, status in enumerate(("active", "inactive"), start=1):
        short_code = f"7788990{index}"
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌县",
            location_query="都昌县",
            elder_code=f"caregiver-get-elder-{index}",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            short_code_expires_at=utcnow() + timedelta(days=1),
            status=status,
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.flush()
        pairs.append(pair)
    db_session.commit()
    active_pair, inactive_pair = pairs
    _login(client, user.username, "caregiver-get-token-pass")
    monkeypatch.setattr(
        caregiver_service,
        "resolve_location",
        lambda label: {"location_code": "", "display_name": label},
    )

    inactive_detail = client.get(f"/caregiver/pair/{inactive_pair.id}")
    active_detail_first = client.get(f"/caregiver/pair/{active_pair.id}")
    active_detail_second = client.get(f"/caregiver/pair/{active_pair.id}")
    management = client.get("/caregiver")

    assert inactive_detail.status_code == 200
    assert active_detail_first.status_code == 200
    assert active_detail_second.status_code == 200
    assert management.status_code == 200
    assert PairActionToken.query.filter_by(pair_id=inactive_pair.id).count() == 0
    assert PairActionToken.query.filter_by(pair_id=active_pair.id).count() == 1


def test_auth_and_caregiver_logs_do_not_include_raw_canaries(
    app,
    client,
    db_session,
    monkeypatch,
    caplog,
):
    """认证用户名与自由地点只能以 ID 或长度进入日志。"""
    from services.user import caregiver_service

    username_canary = "raw-username-canary-7429"
    location_canary = "raw-location-canary-5931"
    monkeypatch.setattr(logging.root.manager, "disable", logging.NOTSET)
    for logger_name in ("services.public_service", "services.user.caregiver_service"):
        target_logger = logging.getLogger(logger_name)
        monkeypatch.setattr(target_logger, "disabled", False)
        monkeypatch.setattr(target_logger, "propagate", True)
        caplog.set_level(logging.INFO, logger=logger_name)
    with client.session_transaction() as session_record:
        session_record["_csrf_token"] = "log-canary-login-csrf"
    client.post(
        "/login",
        data={
            "username": username_canary,
            "password": "wrong-password",
            "csrf_token": "log-canary-login-csrf",
        },
    )

    user = _create_user("caregiver-log-user", "caregiver-log-password")
    user.role = "caregiver"
    db_session.commit()
    _login(client, user.username, "caregiver-log-password")
    monkeypatch.setattr(
        caregiver_service,
        "_create_pair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("canary failure")),
    )
    with client.session_transaction() as session_record:
        session_record["_csrf_token"] = "log-canary-caregiver-csrf"
    client.post(
        "/caregiver/pair/create",
        data={
            "location_query": location_canary,
            "csrf_token": "log-canary-caregiver-csrf",
        },
    )

    assert username_canary not in caplog.text
    assert location_canary not in caplog.text
    assert "identifier_len=" in caplog.text
    assert "location_len=" in caplog.text


def test_help_does_not_count_as_confirmation(app, client):
    """求助只写 help_flag，不能抬高行动确认率。"""
    with app.app_context():
        db.create_all()
        user = _create_user("help_only_user", "help_only_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="求助口径社区",
            location_query="都昌",
            elder_code="elder-help-only",
            short_code="44332211",
            short_code_hash=hash_short_code("44332211"),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "help-only-csrf"

    response = client.post(
        "/action/help",
        data={"short_code": "44332211", "csrf_token": "help-only-csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        aggregate = CommunityDaily.query.filter_by(
            community_code="求助口径社区",
            date=today_local(),
        ).one()
        assert status.help_flag is True
        assert status.confirmed_at is None
        assert aggregate.confirm_rate == 0


def test_token_debrief_get_rejects_mismatched_token(app, client):
    """复盘 GET 页面也必须校验 token 与短码绑定。"""
    with app.app_context():
        db.create_all()
        user = _create_user("debrief_get_user", "debrief_get_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-debrief-get",
            short_code="33445566",
            short_code_hash=hash_short_code("33445566"),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        db.session.add(PairActionToken(
            pair_id=pair.id,
            token_hash=hash_pair_token("right-debrief-token"),
            expires_at=utcnow() + timedelta(days=90),
            created_at=utcnow(),
        ))
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["pair_session_id"] = pair_id
        sess["pair_session_code"] = "33445566"

    resp = client.get("/e/wrong-debrief-token/debrief?short_code=33445566", follow_redirects=False)

    assert resp.status_code == 302
    assert "/e/wrong-debrief-token" in resp.headers["Location"]


def test_legacy_short_code_rejects_expired_pair(app, client):
    """旧短码入口超过过渡期后不能提交行动。"""
    with app.app_context():
        db.create_all()
        user = _create_user("expired_short_code_user", "expired_short_code_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-expired-short",
            short_code="66554433",
            short_code_hash=hash_short_code("66554433"),
            short_code_expires_at=utcnow() - timedelta(seconds=1),
            status="active",
            created_at=utcnow() - timedelta(days=91),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "expired-short-csrf"

    resp = client.post(
        "/action/confirm",
        data={"short_code": "66554433", "csrf_token": "expired-short-csrf"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is None or status.confirmed_at is None


def test_session_pair_must_match_submitted_short_code(app, client):
    """旧 session 不能覆盖表单里提交的另一个短码。"""
    with app.app_context():
        db.create_all()
        user = _create_user("session_mismatch_user", "session_mismatch_pass")
        pair_a = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-session-a",
            short_code="10101010",
            short_code_hash=hash_short_code("10101010"),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        pair_b = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-session-b",
            short_code="20202020",
            short_code_hash=hash_short_code("20202020"),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add_all([pair_a, pair_b])
        db.session.commit()
        pair_a_id = pair_a.id
        pair_b_id = pair_b.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "session-mismatch-csrf"
        sess["pair_session_id"] = pair_a_id
        sess["pair_session_code"] = "10101010"

    resp = client.post(
        "/action/confirm",
        data={"short_code": "20202020", "csrf_token": "session-mismatch-csrf"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_a_id, status_date=today_local()).first() is None
        assert DailyStatus.query.filter_by(pair_id=pair_b_id, status_date=today_local()).first() is None


@pytest.mark.parametrize(
    ("action_path", "short_code", "action_token", "extra_form"),
    (
        (
            "checkin",
            "16180339",
            "deleted-owner-confirm-token",
            {"actions_done": ["drink_water"]},
        ),
        ("help", "14142135", "deleted-owner-help-token", {}),
    ),
)
def test_web_action_rejects_an_already_deleted_owner_before_writing(
    app,
    client,
    monkeypatch,
    action_path,
    short_code,
    action_token,
    extra_form,
):
    """已注销 owner 的 Web token 不能再写当日状态或分析事件。"""
    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        user = _create_user(
            f"deleted_web_{action_path}_owner",
            "deleted_web_owner_pass",
        )
        user_id = user.id
        pair_id, token_id = _create_action_token_pair(user, short_code, action_token)
        user = db.session.get(User, user_id)
        user.deleted_at = utcnow()
        db.session.commit()

    csrf_token = f"deleted-owner-{action_path}-csrf"
    with client.session_transaction() as sess:
        sess["_csrf_token"] = csrf_token
    form = {
        "short_code": short_code,
        "csrf_token": csrf_token,
        **extra_form,
    }

    response = client.post(
        f"/e/{action_token}/{action_path}",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/action" in (response.headers.get("Location") or "")
    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert UsageEvent.query.filter_by(user_id=user_id).count() == 0
        assert db.session.get(PairActionToken, token_id).used_at is None


@pytest.mark.parametrize(
    ("action_path", "short_code", "action_token", "extra_form"),
    (
        (
            "checkin",
            "31415926",
            "stop-first-confirm-token",
            {"actions_done": ["drink_water"]},
        ),
        ("help", "27182818", "stop-first-help-token", {}),
        (
            "debrief",
            "24494897",
            "stop-first-debrief-token",
            {"question_2": "不应保存"},
        ),
    ),
)
def test_pair_stop_first_blocks_inflight_public_action(
    app,
    monkeypatch,
    action_path,
    short_code,
    action_token,
    extra_form,
):
    """公开页解析完旧 Pair 后若停用先提交，动作与 token 使用痕迹均不得落库。"""
    from core.db_models import Debrief
    from services import public_service
    from services.user.owner_write_guard import owner_write_guard

    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
        owner = _create_user(
            f"stop-first-public-{action_path}",
            "stop-first-public-password",
        )
        owner_id = int(owner.id)
        pair_id, token_id = _create_action_token_pair(
            owner,
            short_code,
            action_token,
        )

    writer_ready = threading.Event()
    release_writer = threading.Event()
    outcome = {}
    original_guard = public_service._active_pair_write_guard

    @contextmanager
    def delayed_pair_guard(pair):
        db.session.rollback()
        writer_ready.set()
        assert release_writer.wait(timeout=5)
        with original_guard(pair) as locked_pair:
            yield locked_pair

    monkeypatch.setattr(public_service, "_active_pair_write_guard", delayed_pair_guard)

    def write_action():
        with app.test_client() as thread_client:
            csrf_token = f"stop-first-{action_path}-csrf"
            with thread_client.session_transaction() as session_record:
                session_record["_csrf_token"] = csrf_token
            outcome["response"] = thread_client.post(
                f"/e/{action_token}/{action_path}",
                data={
                    "short_code": short_code,
                    "csrf_token": csrf_token,
                    **extra_form,
                },
                follow_redirects=False,
            )

    writer = threading.Thread(target=write_action)
    writer.start()
    assert writer_ready.wait(timeout=5)
    with app.app_context():
        with owner_write_guard(owner_id):
            pair = db.session.get(Pair, pair_id)
            pair.status = "inactive"
            db.session.commit()
    release_writer.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert outcome["response"].status_code in (301, 302)
    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert Debrief.query.filter_by(owner_user_id=owner_id).count() == 0
        assert UsageEvent.query.filter_by(user_id=owner_id).count() == 0
        assert db.session.get(PairActionToken, token_id).used_at is None


@pytest.mark.parametrize(
    (
        "action_path",
        "short_code",
        "action_token",
        "extra_form",
    ),
    (
        (
            "checkin",
            "17320508",
            "race-confirm-token",
            {"actions_done": ["drink_water"]},
        ),
        ("help", "22360679", "race-help-token", {}),
        (
            "debrief",
            "28284271",
            "race-debrief-token",
            {"question_2": "并发复盘", "debrief_optin": "1"},
        ),
    ),
)
def test_account_delete_serializes_inflight_web_actions(
    app,
    client,
    monkeypatch,
    action_path,
    short_code,
    action_token,
    extra_form,
):
    """Web 写入先完成时，紧随其后的注销必须清除账号记录。"""
    from core.db_models import Debrief
    from services import public_service

    _block_public_action_external_calls(monkeypatch)
    with app.app_context():
        db.create_all()
    login = _wechat_login(
        app,
        client,
        monkeypatch,
        openid=f"web-{action_path}-race-openid",
    )
    assert login.status_code == 200
    login_data = login.get_json()["data"]
    user_id = login_data["user"]["id"]
    headers = {"Authorization": f"Bearer {login_data['session_token']}"}
    with app.app_context():
        owner = db.session.get(User, user_id)
        pair_id, _token_id = _create_action_token_pair(
            owner,
            short_code,
            action_token,
        )

    writer_locked = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    original_owner_guard = public_service._active_pair_write_guard

    @contextmanager
    def blocked_owner_guard(*args, **kwargs):
        with original_owner_guard(*args, **kwargs) as locked_pair:
            assert locked_pair is not None
            writer_locked.set()
            assert release_writer.wait(timeout=5)
            yield locked_pair

    monkeypatch.setattr(
        public_service,
        "_active_pair_write_guard",
        blocked_owner_guard,
    )

    def write_action():
        with app.test_client() as thread_client:
            csrf_token = f"race-{action_path}-csrf"
            with thread_client.session_transaction() as sess:
                sess["_csrf_token"] = csrf_token
            outcomes["write"] = thread_client.post(
                f"/e/{action_token}/{action_path}",
                data={
                    "short_code": short_code,
                    "csrf_token": csrf_token,
                    **extra_form,
                },
                follow_redirects=False,
            )

    def delete_account():
        with app.test_client() as thread_client:
            outcomes["delete"] = thread_client.delete(
                "/mp/api/v1/me",
                headers=headers,
                json={"confirm": True, "user_id": user_id},
            )

    writer = threading.Thread(target=write_action)
    deleter = threading.Thread(target=delete_account)
    writer.start()
    assert writer_locked.wait(timeout=5)
    deleter.start()
    time.sleep(0.2)
    assert deleter.is_alive()

    release_writer.set()
    writer.join(timeout=5)
    deleter.join(timeout=5)

    assert not writer.is_alive()
    assert not deleter.is_alive()
    assert outcomes["write"].status_code == 200
    assert outcomes["delete"].status_code == 200
    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert Debrief.query.filter_by(owner_user_id=user_id).count() == 0
        assert UsageEvent.query.filter_by(user_id=user_id).count() == 0
