# -*- coding: utf-8 -*-
"""公开行动 token 流程回归测试。"""

from datetime import timedelta

from core.db_models import DailyStatus, Pair, PairLink, User
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
