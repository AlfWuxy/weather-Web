# -*- coding: utf-8 -*-
"""公开行动 token 流程回归测试。"""

from datetime import timedelta

from core.db_models import CommunityDaily, DailyStatus, Pair, PairActionToken, PairLink, User
from core.security import hash_pair_token, hash_short_code
from core.time_utils import ensure_utc_aware, today_local, utcnow
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


def _create_user(username="u_test", password="pass12344"):
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
            short_code_expires_at=utcnow() + timedelta(days=90),
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
        # 明确走纯 token 路径，避免旧 session 残留掩盖鉴权来源。
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

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
            short_code_expires_at=utcnow() + timedelta(days=90),
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
        # 明确走纯 token 路径，避免旧 session 残留掩盖鉴权来源。
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

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
        # 明确无 pair session，验证写库完全依赖 action token。
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

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
        # 明确无 pair session，避免误走 session 授权分支。
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

    resp = client.post(
        "/e/expired-action-token/checkin",
        data={"short_code": "11224466", "csrf_token": "expired-action-token-csrf"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is None or status.confirmed_at is None


def test_cold_short_code_cannot_confirm_without_session_or_token(app, client):
    """冷请求仅 short_code+csrf、无 pair_session、无 action token 时不得写 confirm/help。"""
    with app.app_context():
        db.create_all()
        user = _create_user("cold_short_code_user", "cold_short_code_pass")
        # 短码须全文件唯一：会话级 sqlite 不 drop_all，避免与其它用例撞 UNIQUE
        short_code = "86429753"
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-cold-short",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "cold-short-csrf"
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

    confirm_resp = client.post(
        "/action/confirm",
        data={"short_code": short_code, "csrf_token": "cold-short-csrf"},
        follow_redirects=False,
    )
    assert confirm_resp.status_code == 302

    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is None or status.confirmed_at is None

    help_resp = client.post(
        "/action/help",
        data={"short_code": short_code, "csrf_token": "cold-short-csrf"},
        follow_redirects=False,
    )
    assert help_resp.status_code == 302

    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is None or status.help_flag is not True
        assert status is None or status.confirmed_at is None


def test_generated_action_token_survives_short_code_expiry_and_is_reused(app, client):
    """短码过期后 action token 仍可 checkin；真随机下两次签发 plain 不同且可并存（防看板刷新误杀旧链）。"""
    from services.user._common import _build_pair_action_link

    def _token_from_link(link):
        assert link and "/e/" in link
        return link.rsplit("/e/", 1)[-1].split("?", 1)[0]

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

        first_token = _token_from_link(first_link)
        second_token = _token_from_link(second_link)
        assert first_token
        assert second_token
        # 真随机：两次 plain 必须不同（禁止 derive 复用）
        assert first_token != second_token

        now = utcnow()
        first_row = PairActionToken.query.filter_by(
            token_hash=hash_pair_token(first_token)
        ).first()
        second_row = PairActionToken.query.filter_by(
            token_hash=hash_pair_token(second_token)
        ).first()
        rows = PairActionToken.query.filter(
            PairActionToken.pair_id == pair_id,
        ).order_by(PairActionToken.id.asc()).all()

        assert len(rows) == 2
        assert first_row is not None
        assert second_row is not None
        # 策略：不因二次签发吊销第一条，避免看板刷新误杀已转发链接
        assert first_row.revoked_at is None
        assert second_row.revoked_at is None
        assert ensure_utc_aware(second_row.expires_at) >= now
        active = [
            row for row in rows
            if row.revoked_at is None
            and ensure_utc_aware(row.expires_at) is not None
            and ensure_utc_aware(row.expires_at) >= now
        ]
        assert len(active) == 2

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "generated-token-csrf"
        # 明确无 pair session，锁定「短码过期但 action token 仍可写」语义。
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

    # 第二次签发的 token 必须能走 /e/<token>/checkin 正例
    second_resp = client.post(
        f"/e/{second_token}/checkin",
        data={"short_code": "55667788", "csrf_token": "generated-token-csrf"},
        follow_redirects=False,
    )
    assert second_resp.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        assert status.confirmed_at is not None

    # 第一次签发的 token 仍有效（未因二次签发被 revoke），短码过期也可再 checkin
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "generated-token-csrf-first"
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

    first_resp = client.post(
        f"/e/{first_token}/checkin",
        data={"short_code": "55667788", "csrf_token": "generated-token-csrf-first"},
        follow_redirects=False,
    )
    assert first_resp.status_code == 200


def test_help_does_not_count_as_confirmation(app, client):
    """冷请求仅 short_code 不得求助；lookup 建 session 后可写 help_flag，且不抬确认率。"""
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
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "help-only-csrf"
        # 明确无 pair session：冷请求不得凭短码写 help
        sess.pop("pair_session_id", None)
        sess.pop("pair_session_code", None)

    # 负例：仅 short_code + csrf、无 pair_session / token → 应 302 且不写 help_flag
    cold = client.post(
        "/action/help",
        data={"short_code": "44332211", "csrf_token": "help-only-csrf"},
        follow_redirects=False,
    )
    assert cold.status_code == 302
    with app.app_context():
        cold_status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).first()
        assert cold_status is None or cold_status.help_flag is not True

    # 正例：先 POST /action lookup 写入 pair_session，再 help
    lookup = client.post(
        "/action",
        data={"short_code": "44332211", "csrf_token": "help-only-csrf"},
        follow_redirects=False,
    )
    assert lookup.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("pair_session_id") == pair_id
        assert sess.get("pair_session_code") == "44332211"

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
        # 求助只打 help 标记，不算行动确认
        assert status.help_flag is True
        assert status.confirmed_at is None
        assert aggregate.confirm_rate == 0


def test_action_confirm_counts_only_unique_whitelisted_actions(app, client, monkeypatch):
    """行动确认按首次展示风险档统计合法且去重后的行动 ID。"""
    with app.app_context():
        db.create_all()
        user = _create_user("action_whitelist_user", "action_whitelist_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="行动白名单社区",
            location_query="都昌",
            elder_code="elder-action-whitelist",
            short_code="31415926",
            short_code_hash=hash_short_code("31415926"),
            short_code_expires_at=utcnow() + timedelta(days=1),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        pair_id = pair.id

    def fake_action_context(pair, status_date):
        status = DailyStatus(
            pair_id=pair.id,
            community_code=pair.community_code,
            status_date=status_date,
            risk_level='高风险',
        )
        db.session.add(status)
        return (
            status,
            [
                {'id': 'stay_cool'},
                {'id': 'contact_now'},
                {'id': 'cooling_center'},
            ],
            [],
            None,
            None,
            '极高',
            [],
        )

    monkeypatch.setattr(
        'services.public_service._build_action_context',
        fake_action_context,
    )
    monkeypatch.setattr(
        'services.public_service._render_action_page',
        lambda *_args, **_kwargs: ('ok', 200),
    )

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "action-whitelist-csrf"
        # P1：写 confirm 需已通过 lookup 的 pair session，不能仅靠 short_code
        sess["pair_session_id"] = pair_id
        sess["pair_session_code"] = "31415926"

    response = client.post(
        "/action/confirm",
        data={
            "short_code": "31415926",
            "csrf_token": "action-whitelist-csrf",
            "actions_done": ["hydrate", "hydrate", "forged-action"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    with app.app_context():
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        assert status.actions_done_count == 1


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


def test_session_after_lookup_allows_confirm(app, client):
    """lookup 写入 pair_session 后（或等价直接设 session），confirm 应允许写库。

    P1 修复后：冷请求仅 short_code 不得写 confirmed_at；
    本用例锁定老人主路径——先 POST /action 校验短码，再 POST /action/confirm。
    """
    short_code = "13572468"
    with app.app_context():
        db.create_all()
        user = _create_user("session_lookup_user", "session_lookup_pass")
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder-session-lookup",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess["_csrf_token"] = "session-lookup-csrf"

    # 主路径：POST lookup 建立 pair_session_*（生产老人流程）
    lookup_resp = client.post(
        "/action",
        data={"short_code": short_code, "csrf_token": "session-lookup-csrf"},
        follow_redirects=False,
    )
    assert lookup_resp.status_code == 200

    with client.session_transaction() as sess:
        assert sess.get("pair_session_id") == pair_id
        assert sess.get("pair_session_code") == short_code

    confirm_resp = client.post(
        "/action/confirm",
        data={"short_code": short_code, "csrf_token": "session-lookup-csrf"},
        follow_redirects=False,
    )
    assert confirm_resp.status_code == 200

    with app.app_context():
        status = DailyStatus.query.filter_by(
            pair_id=pair_id,
            status_date=today_local(),
        ).one()
        assert status.confirmed_at is not None


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
