# -*- coding: utf-8 -*-
"""公开行动 token 流程回归测试。"""

from datetime import timedelta

from core.db_models import DailyStatus, Pair, PairActionToken, PairLink, User
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
