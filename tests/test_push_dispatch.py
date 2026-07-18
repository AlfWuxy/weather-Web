# -*- coding: utf-8 -*-

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


DISPATCH_CURRENT = {
    "temperature": 36,
    "temperature_max": 38,
    "temperature_min": 27,
    "data_source": "QWeather",
    "is_mock": False,
}


def _persist_dispatch_snapshot(*, current=None, warnings=None, fetched_at=None):
    from services.miniprogram_service import persist_snapshot

    return persist_snapshot(
        DISPATCH_CURRENT if current is None else current,
        [],
        [] if warnings is None else warnings,
        fetched_at=fetched_at,
    )


def _forbid_weather_upstream(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("dispatch 不得调用天气上游或地址解析")

    monkeypatch.setattr("services.location_resolver.resolve_location", forbidden)
    monkeypatch.setattr("services.warning_service.get_qweather_warnings", forbidden)
    monkeypatch.setattr("core.weather.get_weather_with_cache", forbidden)


def _create_push_recipient(db_session, *, username, short_code):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow

    user = User(
        username=username,
        role="user",
        wxpusher_uid=f"UID_{username}",
        push_enabled=True,
    )
    user.set_password("pw123456")
    db_session.add(user)
    db_session.flush()
    pair = Pair(
        caregiver_id=user.id,
        community_code="都昌",
        location_query="都昌",
        elder_code=f"elder_{username}",
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        status="active",
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.flush()
    user_id = int(user.id)
    pair_id = int(pair.id)
    db_session.commit()
    return user_id, pair_id


def _create_tracking_delivery(
    db_session,
    *,
    token,
    sent_at,
    alert_at,
    status="sent",
):
    """创建不依赖天气上游的点击跟踪记录。"""
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow

    user = User(username=f"tracking_{token}", role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.flush()

    alert = WeatherAlert(
        alert_date=alert_at if alert_at is not None else utcnow(),
        location="116.20,29.27",
        alert_type="heat_threshold",
        alert_level="阈值",
        description="tracking test",
        affected_communities="[]",
        disease_correlation="{}",
    )
    db_session.add(alert)
    db_session.flush()
    if alert_at is None:
        WeatherAlert.query.filter_by(id=alert.id).update(
            {WeatherAlert.alert_date: None},
            synchronize_session=False,
        )

    delivery = AlertDelivery(
        alert_id=alert.id,
        user_id=user.id,
        channel="wxpusher",
        status=status,
        delivery_token=token,
        sent_at=sent_at,
        error="provider timeout" if status == "uncertain" else None,
    )
    db_session.add(delivery)
    db_session.commit()
    return int(delivery.id)


def test_dispatch_alerts_dedupes_success(app, db_session, monkeypatch):
    from core.db_models import AlertDelivery, Pair, UsageEvent, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        # 创建已开启推送的用户。
        user = User(username="u1", role="user", wxpusher_uid="UID_TEST", push_enabled=True)
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()

        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder_x",
            short_code="12345678",
            short_code_hash=hash_short_code("12345678"),
            status="active",
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.commit()

        app.config["PUBLIC_BASE_URL"] = "https://example.com"

        # WxPusher 使用本地桩，天气只从数据库快照读取。
        monkeypatch.setattr(dispatch_mod, "wxpusher_send", lambda *args, **kwargs: {"ok": True, "msg_id": "1"})
        _forbid_weather_upstream(monkeypatch)
        _persist_dispatch_snapshot()

        result1 = dispatch_mod.dispatch_alerts()
        assert result1["deliveries"] == 1
        assert AlertDelivery.query.count() == 1
        assert UsageEvent.query.filter_by(event_type="push_sent").count() == 1

        # 第二次运行必须去重，不能新增送达记录。
        result2 = dispatch_mod.dispatch_alerts()
        assert AlertDelivery.query.count() == 1
        assert result2["deliveries"] == 0 or result2["sent"] == 0


def test_dispatch_commits_alert_and_claim_before_external_call(
    app,
    db_session,
    monkeypatch,
):
    """外呼开始前，新预警和 sending 占位必须已提交且会话无事务。"""
    from core.db_models import AlertDelivery
    from core.extensions import db
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        _create_push_recipient(
            db_session,
            username="claim_commit",
            short_code="92000001",
        )
        now = utcnow()
        _persist_dispatch_snapshot(fetched_at=now)
        _forbid_weather_upstream(monkeypatch)
        database_path = db.engine.url.database
        observed = {}

        def fake_send(*_args, **_kwargs):
            observed["session_in_transaction"] = db.session().in_transaction()
            with sqlite3.connect(database_path) as connection:
                observed["alert_count"] = connection.execute(
                    "SELECT COUNT(*) FROM weather_alerts"
                ).fetchone()[0]
                observed["claim"] = connection.execute(
                    """
                    SELECT status, channel
                    FROM alert_deliveries
                    """
                ).fetchone()
            return {"ok": True, "msg_id": "committed-before-network"}

        monkeypatch.setattr(dispatch_mod, "wxpusher_send", fake_send)

        result = dispatch_mod.dispatch_alerts(now=now)

        assert result["sent"] == 1
        assert observed == {
            "session_in_transaction": False,
            "alert_count": 1,
            "claim": ("sending", "wxpusher"),
        }
        assert AlertDelivery.query.one().status == "sent"


def test_stale_sending_becomes_uncertain_without_retry(
    app,
    db_session,
    monkeypatch,
):
    """新鲜占位跳过；超过十分钟后转人工确认，仍然不得重发。"""
    from core.db_models import AlertDelivery, WeatherAlert
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        user_id, pair_id = _create_push_recipient(
            db_session,
            username="stale_claim",
            short_code="92000002",
        )
        now = utcnow()
        _persist_dispatch_snapshot(fetched_at=now)
        alert = WeatherAlert(
            alert_date=now,
            location="116.20,29.27",
            alert_type="heat_threshold",
            alert_level="阈值",
            description="最高气温提醒",
            affected_communities="[]",
            disease_correlation="{}",
        )
        db_session.add(alert)
        db_session.flush()
        delivery = AlertDelivery(
            alert_id=alert.id,
            user_id=user_id,
            pair_id=pair_id,
            channel="wxpusher",
            status="sending",
            delivery_token="stale-claim-token",
            sent_at=now,
        )
        db_session.add(delivery)
        db_session.commit()

        _forbid_weather_upstream(monkeypatch)
        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *_args, **_kwargs: pytest.fail("sending 占位不得重复外呼"),
        )

        recent = dispatch_mod.dispatch_alerts(now=now + timedelta(minutes=5))
        assert recent["deliveries"] == 0
        assert recent["failed"] == 0
        assert db_session.get(AlertDelivery, delivery.id).status == "sending"

        stale = dispatch_mod.dispatch_alerts(now=now + timedelta(minutes=11))
        db_session.expire_all()
        refreshed = db_session.get(AlertDelivery, delivery.id)
        assert stale["deliveries"] == 0
        assert stale["failed"] == 1
        assert stale["review_required"] == 1
        assert refreshed.status == "uncertain"
        assert "禁止自动重试" in refreshed.error

        repeated = dispatch_mod.dispatch_alerts(now=now + timedelta(minutes=12))
        assert repeated["deliveries"] == 0
        assert repeated["review_required"] == 1


@pytest.mark.parametrize(
    ("provider_error", "expected_status"),
    [
        ("ReadTimeout after upload", "uncertain"),
        ("missing WXPUSHER_APP_TOKEN", "failed"),
    ],
)
def test_non_success_delivery_is_terminal_until_manual_review(
    app,
    db_session,
    monkeypatch,
    provider_error,
    expected_status,
):
    """供应商不成功结果和本地失败都禁止自动重试。"""
    from core.db_models import AlertDelivery
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        _create_push_recipient(
            db_session,
            username=f"terminal_{expected_status}",
            short_code={"uncertain": "92000003", "failed": "92000004"}[expected_status],
        )
        now = utcnow()
        _persist_dispatch_snapshot(fetched_at=now)
        _forbid_weather_upstream(monkeypatch)
        calls = []

        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *_args, **_kwargs: calls.append(1)
            or {"ok": False, "error": provider_error},
        )
        first = dispatch_mod.dispatch_alerts(now=now)
        assert first["deliveries"] == 1
        assert first["failed"] == 1
        assert AlertDelivery.query.one().status == expected_status

        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *_args, **_kwargs: pytest.fail("终态投递不得自动重试"),
        )
        second = dispatch_mod.dispatch_alerts(now=now + timedelta(minutes=1))

        assert calls == [1]
        assert second["deliveries"] == 0
        assert second["failed"] == 1
        assert second["review_required"] == 1


def test_manually_approved_retry_reuses_claim_once(app, db_session):
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow
    from services.push.dispatch import _claim_delivery, _finalize_delivery

    with app.app_context():
        user = User(username='manual-retry-user', role='user')
        user.set_password('testpass')
        db_session.add(user)
        alert = WeatherAlert(
            alert_date=utcnow(),
            location='116.20,29.27',
            alert_type='heat_threshold',
            alert_level='阈值',
            description='test',
            affected_communities='[]',
            disease_correlation='{}',
        )
        db_session.add(alert)
        db_session.flush()
        delivery = AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            channel='wxpusher',
            status='retry_ready',
            delivery_token='manual-retry-token',
            sent_at=utcnow(),
            attempt_count=1,
            review_action='allow_retry',
            reviewed_at=utcnow(),
            reviewed_by_user_id=user.id,
        )
        db_session.add(delivery)
        db_session.commit()

        claim = _claim_delivery(
            alert_id=alert.id,
            user_id=user.id,
            pair_id=None,
            now=utcnow(),
        )

        assert claim['action'] == 'send'
        assert claim['delivery_id'] == delivery.id
        assert claim['delivery_token'] == 'manual-retry-token'
        db_session.expire_all()
        refreshed = db_session.get(AlertDelivery, delivery.id)
        assert refreshed.status == 'sending'
        assert refreshed.attempt_count == 2
        assert refreshed.review_action is None
        assert refreshed.reviewed_at is None
        assert refreshed.reviewed_by_user_id is None

        retry_state = _finalize_delivery(
            delivery.id,
            {"ok": False, "error": "ReadTimeout after retry"},
            utcnow(),
        )
        db_session.expire_all()
        retried = db_session.get(AlertDelivery, delivery.id)
        assert retry_state == "uncertain"
        assert retried.status == "uncertain"
        assert retried.review_action is None
        assert retried.reviewed_at is None
        assert retried.reviewed_by_user_id is None


def test_dispatch_recovers_stale_sending_before_no_pair_early_return(app, db_session):
    """即使没有活跃关系，旧 sending 也必须进入人工复核。"""
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        user = User(username="stale_without_pair", role="user")
        user.set_password("testpass")
        db_session.add(user)
        alert = WeatherAlert(
            alert_date=utcnow(),
            location="116.20,29.27",
            alert_type="heat_threshold",
            alert_level="阈值",
            description="test",
            affected_communities="[]",
            disease_correlation="{}",
        )
        db_session.add(alert)
        db_session.flush()
        delivery = AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            channel="wxpusher",
            status="sending",
            delivery_token="stale-without-pair-token",
            sent_at=utcnow() - timedelta(minutes=11),
            review_action="allow_retry",
            reviewed_at=utcnow() - timedelta(minutes=12),
            reviewed_by_user_id=user.id,
        )
        db_session.add(delivery)
        db_session.commit()

        result = dispatch_mod.dispatch_alerts(now=utcnow())

        db_session.expire_all()
        refreshed = db_session.get(AlertDelivery, delivery.id)
        assert result["status"] == "idle_no_pairs"
        assert result["recovered_stale_sending"] == 1
        assert refreshed.status == "uncertain"
        assert "禁止自动重试" in refreshed.error
        assert refreshed.review_action is None
        assert refreshed.reviewed_at is None
        assert refreshed.reviewed_by_user_id is None


@pytest.mark.parametrize(
    ("payload", "expected_ok", "expected_error", "expected_message_id"),
    [
        (
            {"code": 1000, "data": [{"uid": "UID_TARGET", "status": "fail", "code": 1001, "msg": "UID不存在"}]},
            False,
            "UID不存在",
            None,
        ),
        ({"code": 1000, "data": []}, False, "empty delivery result", None),
        (
            {"code": "1000", "data": [{"uid": "UID_TARGET", "status": "success", "code": "1000", "messageId": 42}]},
            True,
            None,
            "42",
        ),
        (
            {"code": 1000, "data": [{"uid": "UID_OTHER", "status": "success", "code": 1000}]},
            False,
            "uid mismatch",
            None,
        ),
    ],
)
def test_wxpusher_requires_successful_matching_nested_delivery(
    app,
    monkeypatch,
    payload,
    expected_ok,
    expected_error,
    expected_message_id,
):
    from services.push import wxpusher

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return payload

    app.config["WXPUSHER_APP_TOKEN"] = "AT_abcdefghijklmnop"
    app.config["WXPUSHER_API_BASE"] = wxpusher.WXPUSHER_OFFICIAL_API_BASE
    monkeypatch.setattr(wxpusher.requests, "post", lambda *_args, **_kwargs: FakeResponse())

    with app.app_context():
        result = wxpusher.send("UID_TARGET", "标题", "正文")

    assert result["ok"] is expected_ok
    assert result.get("msg_id") == expected_message_id
    if expected_error:
        assert expected_error in result.get("error", "")


@pytest.mark.parametrize("snapshot_state", ["missing", "unavailable", "stale"])
def test_dispatch_fails_closed_without_fresh_available_snapshot(
    app,
    db_session,
    monkeypatch,
    snapshot_state,
):
    from core.db_models import AlertDelivery, Pair, User, WeatherAlert
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        user = User(
            username=f"snapshot_{snapshot_state}",
            role="user",
            wxpusher_uid="UID_FAIL_CLOSED",
            push_enabled=True,
        )
        user.set_password("pw123456")
        db_session.add(user)
        db_session.flush()
        short_code = {
            "missing": "91000001",
            "unavailable": "91000002",
            "stale": "91000003",
        }[snapshot_state]
        db_session.add(Pair(
            caregiver_id=user.id,
            community_code="任意社区",
            location_query="任意细粒度地址",
            elder_code=f"elder-{snapshot_state}",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            status="active",
            last_active_at=utcnow(),
        ))
        db_session.commit()

        now = utcnow()
        if snapshot_state == "unavailable":
            _persist_dispatch_snapshot(current={}, fetched_at=now)
        elif snapshot_state == "stale":
            _persist_dispatch_snapshot(fetched_at=now - timedelta(minutes=31))

        _forbid_weather_upstream(monkeypatch)
        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *_args, **_kwargs: pytest.fail("fail-closed 状态不得发送推送"),
        )

        result = dispatch_mod.dispatch_alerts(now=now)

        assert result["alerts"] == 0
        assert result["deliveries"] == 0
        assert WeatherAlert.query.count() == 0
        assert AlertDelivery.query.count() == 0


def test_threshold_alert_rejects_mock_weather():
    from services.push.dispatch import _threshold_alert

    assert _threshold_alert({
        "temperature_max": 39,
        "temperature_min": 29,
        "data_source": "Demo",
        "is_mock": True,
    }) is None
    assert _threshold_alert({
        "temperature": 36,
        "temperature_max": 39,
        "temperature_min": 29,
        "data_source": "QWeather",
        "is_mock": False,
    }) is not None


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, 7),
        ("invalid", 7),
        ("0", 1),
        ("999", 7),
    ],
)
def test_push_tracking_link_ttl_config_is_bounded(monkeypatch, configured, expected):
    import logging

    from flask import Flask

    from core.config import configure_app

    if configured is None:
        monkeypatch.delenv("PUSH_TRACKING_LINK_TTL_DAYS", raising=False)
    else:
        monkeypatch.setenv("PUSH_TRACKING_LINK_TTL_DAYS", configured)

    test_app = Flask("push-tracking-config")
    configure_app(test_app, logging.getLogger("push-tracking-config"))
    assert test_app.config["PUSH_TRACKING_LINK_TTL_DAYS"] == expected


def _tracking_csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def test_tracking_route_requires_explicit_csrf_confirmation_and_marks_only_once(
    client,
    app,
    db_session,
):
    from core.db_models import AlertDelivery, Pair, User, WeatherAlert, UsageEvent
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        user = User(username="u2", role="user")
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()

        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="elder_y",
            short_code="87654321",
            short_code_hash=hash_short_code("87654321"),
            status="active",
        )
        db_session.add(pair)
        db_session.commit()

        alert = WeatherAlert(
            alert_date=utcnow(),
            location="116.20,29.27",
            alert_type="heat_threshold",
            alert_level="阈值",
            description="test",
            affected_communities="[]",
            disease_correlation="{}",
        )
        db_session.add(alert)
        db_session.commit()

        delivery = AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            pair_id=pair.id,
            channel="wxpusher",
            status="uncertain",
            delivery_token="tok_test_123",
            sent_at=utcnow(),
            error="provider timeout",
        )
        db_session.add(delivery)
        db_session.commit()

    first = client.get("/t/tok_test_123", follow_redirects=False)
    second = client.get("/t/tok_test_123", follow_redirects=False)
    head = client.head("/t/tok_test_123", follow_redirects=False)
    assert first.status_code == 200
    assert second.status_code == 200
    assert head.status_code == 200
    assert "我已看到这条提醒" in first.get_data(as_text=True)
    assert first.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert first.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    with app.app_context():
        refreshed = AlertDelivery.query.filter_by(delivery_token="tok_test_123").first()
        assert refreshed is not None
        assert refreshed.clicked_at is None
        assert refreshed.status == "uncertain"
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 0

    rejected = client.post("/t/tok_test_123", follow_redirects=False)
    assert rejected.status_code == 400

    csrf_token = _tracking_csrf_token(client)
    assert csrf_token
    confirmed = client.post(
        "/t/tok_test_123",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    repeated = client.post(
        "/t/tok_test_123",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert confirmed.status_code in (301, 302)
    assert repeated.status_code in (301, 302)

    with app.app_context():
        refreshed = AlertDelivery.query.filter_by(delivery_token="tok_test_123").first()
        assert refreshed is not None
        assert refreshed.clicked_at is not None
        assert refreshed.status == "sent"
        assert refreshed.review_action == "click_confirmed"
        assert refreshed.reviewed_at is not None
        assert refreshed.error is None
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 1


@pytest.mark.parametrize(
    ("status", "error", "review_action", "expected_status", "expected_review"),
    [
        ("failed", "管理员确认未送达", "confirm_failed", "failed", "confirm_failed"),
        ("retry_ready", "允许下一轮重试", "allow_retry", "retry_ready", "allow_retry"),
        (
            "failed",
            "push authorization revoked",
            "auth_revoked",
            "failed",
            "auth_revoked",
        ),
    ],
)
def test_tracking_confirmation_never_overrides_existing_review_or_local_failure(
    client,
    app,
    db_session,
    status,
    error,
    review_action,
    expected_status,
    expected_review,
):
    from core.db_models import AlertDelivery
    from core.time_utils import utcnow

    token = f"click-{status}-{review_action}"
    delivery_id = _create_tracking_delivery(
        db_session,
        token=token,
        sent_at=utcnow(),
        alert_at=utcnow(),
        status=status,
    )
    delivery = db_session.get(AlertDelivery, delivery_id)
    delivery.error = error
    delivery.review_action = review_action
    delivery.reviewed_at = utcnow()
    db_session.commit()

    page = client.get(f"/t/{token}", follow_redirects=False)
    assert page.status_code == 200
    csrf_token = _tracking_csrf_token(client)
    response = client.post(
        f"/t/{token}",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)
    db_session.expire_all()
    refreshed = db_session.get(AlertDelivery, delivery_id)
    assert refreshed.clicked_at is not None
    assert refreshed.status == expected_status
    assert refreshed.review_action == expected_review


def test_tracking_route_enforces_before_at_and_after_seven_day_boundary(
    client,
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import AlertDelivery, UsageEvent

    fixed_now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("blueprints.public.utcnow", lambda: fixed_now)
    app.config["PUSH_TRACKING_LINK_TTL_DAYS"] = 7

    with app.app_context():
        _create_tracking_delivery(
            db_session,
            token="tok_before_boundary",
            sent_at=fixed_now - timedelta(days=7) + timedelta(microseconds=1),
            alert_at=fixed_now - timedelta(days=8),
        )
        _create_tracking_delivery(
            db_session,
            token="tok_at_boundary",
            sent_at=fixed_now - timedelta(days=7),
            alert_at=fixed_now - timedelta(days=8),
        )
        _create_tracking_delivery(
            db_session,
            token="tok_after_boundary",
            sent_at=fixed_now - timedelta(days=7) - timedelta(microseconds=1),
            alert_at=fixed_now - timedelta(days=8),
        )

    before = client.get("/t/tok_before_boundary", follow_redirects=False)
    at = client.get("/t/tok_at_boundary", follow_redirects=False)
    after = client.get("/t/tok_after_boundary", follow_redirects=False)

    assert before.status_code == 200
    assert at.status_code == 200
    assert after.headers["Location"].endswith("/")
    with app.app_context():
        records = {
            item.delivery_token: item
            for item in AlertDelivery.query.filter(
                AlertDelivery.delivery_token.in_(
                    {
                        "tok_before_boundary",
                        "tok_at_boundary",
                        "tok_after_boundary",
                    }
                )
            ).all()
        }
        assert records["tok_before_boundary"].clicked_at is None
        assert records["tok_at_boundary"].clicked_at is None
        assert records["tok_after_boundary"].clicked_at is None
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 0

    csrf_token = _tracking_csrf_token(client)
    before_confirm = client.post(
        "/t/tok_before_boundary",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    at_confirm = client.post(
        "/t/tok_at_boundary",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    expired_confirm = client.post(
        "/t/tok_after_boundary",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert "/login" in before_confirm.headers["Location"]
    assert "/login" in at_confirm.headers["Location"]
    assert expired_confirm.headers["Location"].endswith("/")
    with app.app_context():
        records = {
            item.delivery_token: item
            for item in AlertDelivery.query.filter(
                AlertDelivery.delivery_token.in_(
                    {"tok_before_boundary", "tok_at_boundary", "tok_after_boundary"}
                )
            ).all()
        }
        assert records["tok_before_boundary"].clicked_at is not None
        assert records["tok_at_boundary"].clicked_at is not None
        assert records["tok_after_boundary"].clicked_at is None
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 2


def test_tracking_route_uses_safe_alert_time_fallback_and_rejects_missing_time(
    client,
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import AlertDelivery, UsageEvent
    from core.time_utils import ensure_utc_aware

    fixed_now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    fallback_time = fixed_now - timedelta(days=1)
    monkeypatch.setattr("blueprints.public.utcnow", lambda: fixed_now)
    app.config["PUSH_TRACKING_LINK_TTL_DAYS"] = 7

    with app.app_context():
        _create_tracking_delivery(
            db_session,
            token="tok_alert_fallback",
            sent_at=None,
            alert_at=fallback_time,
            status="uncertain",
        )
        _create_tracking_delivery(
            db_session,
            token="tok_missing_time",
            sent_at=None,
            alert_at=None,
            status="uncertain",
        )

    fallback = client.get("/t/tok_alert_fallback", follow_redirects=False)
    missing = client.get("/t/tok_missing_time", follow_redirects=False)

    assert fallback.status_code == 200
    assert missing.headers["Location"].endswith("/")
    with app.app_context():
        fallback_record = AlertDelivery.query.filter_by(
            delivery_token="tok_alert_fallback"
        ).first()
        missing_record = AlertDelivery.query.filter_by(
            delivery_token="tok_missing_time"
        ).first()
        assert fallback_record.clicked_at is None
        assert fallback_record.status == "uncertain"
        assert missing_record.clicked_at is None
        assert missing_record.status == "uncertain"
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 0

    csrf_token = _tracking_csrf_token(client)
    confirmed = client.post(
        "/t/tok_alert_fallback",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert "/login" in confirmed.headers["Location"]
    with app.app_context():
        fallback_record = AlertDelivery.query.filter_by(
            delivery_token="tok_alert_fallback"
        ).first()
        assert fallback_record.clicked_at is not None
        assert fallback_record.status == "sent"
        assert ensure_utc_aware(fallback_record.sent_at) == fallback_time
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 1


def test_dispatch_respects_member_alert_and_privacy_settings(app, db_session, monkeypatch):
    from core.db_models import FamilyMember, FamilyMemberProfile, Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        user = User(username="push_privacy", role="user", wxpusher_uid="UID_PRIVATE", push_enabled=True)
        user.set_password("pw123456")
        db_session.add(user)
        db_session.flush()

        disabled_member = FamilyMember(user_id=user.id, name="关闭预警成员")
        private_member = FamilyMember(user_id=user.id, name="私密成员")
        db_session.add_all([disabled_member, private_member])
        db_session.flush()
        db_session.add_all([
            FamilyMemberProfile(
                member_id=disabled_member.id,
                alert_enabled=False,
                privacy_level="family",
            ),
            FamilyMemberProfile(
                member_id=private_member.id,
                alert_enabled=True,
                privacy_level="private",
            ),
        ])
        for index, member in enumerate((disabled_member, private_member), start=1):
            code = f"9900000{index}"
            db_session.add(Pair(
                caregiver_id=user.id,
                member_id=member.id,
                community_code="都昌",
                location_query="都昌",
                elder_code=f"privacy-{index}",
                short_code=code,
                short_code_hash=hash_short_code(code),
                status="active",
                last_active_at=utcnow(),
            ))
        db_session.commit()

        sent = []
        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *args, **kwargs: sent.append((args, kwargs)) or {"ok": True},
        )
        _forbid_weather_upstream(monkeypatch)

        result = dispatch_mod.dispatch_alerts()

        assert result["deliveries"] == 0
        assert sent == []


def test_dispatch_minimizes_identity_data_sent_to_third_party(app, db_session, monkeypatch):
    from core.db_models import FamilyMember, FamilyMemberProfile, Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        user = User(username="push_minimized", role="user", wxpusher_uid="UID_MIN", push_enabled=True)
        user.set_password("pw123456")
        db_session.add(user)
        db_session.flush()
        member = FamilyMember(user_id=user.id, name="不应外发的姓名")
        db_session.add(member)
        db_session.flush()
        db_session.add(FamilyMemberProfile(
            member_id=member.id,
            alert_enabled=True,
            privacy_level="family",
        ))
        code = "99112233"
        db_session.add(Pair(
            caregiver_id=user.id,
            member_id=member.id,
            community_code="都昌",
            location_query="都昌某路123号",
            elder_code="minimized-elder",
            short_code=code,
            short_code_hash=hash_short_code(code),
            status="active",
            last_active_at=utcnow(),
        ))
        db_session.commit()

        captured = {}

        def fake_send(_uid, title, content, url=None):
            captured.update({"title": title, "content": content, "url": url})
            return {"ok": True}

        monkeypatch.setattr(dispatch_mod, "wxpusher_send", fake_send)
        _forbid_weather_upstream(monkeypatch)
        _persist_dispatch_snapshot()

        result = dispatch_mod.dispatch_alerts()

        assert result["deliveries"] == 1
        assert "不应外发的姓名" not in captured["content"]
        assert "都昌某路123号" not in captured["content"]
        assert "地点：都昌县" in captured["content"]


def test_choose_primary_warning_supports_cap_severity():
    from services.push.dispatch import _choose_primary_warning

    warnings = [
        {"title": "严重预警", "level": "", "severity": "Severe", "text": "较长说明"},
        {"title": "极端预警", "level": "", "severity": "Extreme", "text": "短"},
    ]

    assert _choose_primary_warning(warnings)["title"] == "极端预警"


def test_render_push_content_falls_back_to_cap_severity():
    from services.push.dispatch import _render_push_content

    _title, content = _render_push_content(
        display_name="都昌县",
        elder_names=[],
        warning={
            "title": "高温预警",
            "type": "高温",
            "level": "",
            "severity": "Extreme",
            "text": "减少户外活动",
        },
        threshold_desc=None,
        location_query="都昌县",
    )

    assert "官方预警：高温Extreme" in content
    assert "数据来源：和风天气（QWeather）" in content


def test_weather_alert_dedupe_uses_database_field_lengths(app, db_session):
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow
    from services.push.dispatch import _get_or_create_weather_alert

    long_type = "高温预警" * 30
    long_level = "Extreme" * 10

    with app.app_context():
        now = utcnow()
        first = _get_or_create_weather_alert(
            now=now,
            location_key="116.20,29.27",
            alert_type=long_type,
            alert_level=long_level,
            description="第一次",
        )
        db_session.commit()
        second = _get_or_create_weather_alert(
            now=now,
            location_key="116.20,29.27",
            alert_type=long_type,
            alert_level=long_level,
            description="第二次",
        )

        assert first.id == second.id
        assert WeatherAlert.query.count() == 1
        assert len(first.alert_type) == 50
        assert len(first.alert_level) == 20


def test_weather_alert_level_upgrade_creates_new_delivery_record(app, db_session):
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow
    from services.push.dispatch import _get_or_create_weather_alert

    with app.app_context():
        now = utcnow()
        first = _get_or_create_weather_alert(
            now=now,
            location_key="116.20,29.27",
            alert_type="高温",
            alert_level="黄色",
            description="高温黄色预警",
        )
        db_session.commit()
        upgraded = _get_or_create_weather_alert(
            now=now,
            location_key="116.20,29.27",
            alert_type="高温",
            alert_level="橙色",
            description="高温橙色预警",
        )

        assert first.id != upgraded.id
        assert WeatherAlert.query.count() == 2


def _official_warning(**overrides):
    warning = {
        "source_id": "alert-20260718-001",
        "start_time": "2026-07-18T08:00:00+08:00",
        "end_time": "2026-07-18T20:00:00+08:00",
        "type": "高温",
        "level": "黄色",
        "title": "高温黄色预警",
        "text": "请注意防暑降温",
        "severity": "Severe",
        "certainty": "Observed",
        "urgency": "Immediate",
        "message_type": "alert",
        "supersedes": [],
        "raw": {"updateTime": "2026-07-18T08:01:00+08:00"},
    }
    warning.update(overrides)
    return warning


def _create_official_alert(db_session, *, now, warning):
    from services.push.dispatch import (
        _build_alert_dedupe_key,
        _get_or_create_weather_alert,
    )

    alert_type = warning.get("type") or "qweather_warning"
    alert_level = warning.get("level") or warning.get("severity") or ""
    dedupe_key = _build_alert_dedupe_key(
        now=now,
        location_key="101240210",
        alert_type=alert_type,
        alert_level=alert_level,
        dedupe_hours=6,
        warning=warning,
    )
    record = _get_or_create_weather_alert(
        now=now,
        location_key="101240210",
        alert_type=alert_type,
        alert_level=alert_level,
        description=warning.get("title") or warning.get("text") or "官方预警",
        dedupe_hours=6,
        dedupe_key=dedupe_key,
        exact_dedupe=True,
    )
    db_session.commit()
    return record


def test_official_weather_alert_same_revision_is_idempotent(app, db_session):
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow

    with app.app_context():
        now = utcnow()
        first = _create_official_alert(
            db_session,
            now=now,
            warning=_official_warning(),
        )
        repeated = _create_official_alert(
            db_session,
            now=now + timedelta(minutes=1),
            warning=_official_warning(
                source_id="  alert-20260718-001  ",
                start_time="2026-07-18T00:00:00Z",
                title="高温黄色预警  ",
                text="请注意防暑降温\n",
                severity="severe",
                certainty="observed",
                urgency="immediate",
                message_type="ALERT",
                raw={"updateTime": "2026-07-18T00:01:00Z"},
            ),
        )

        assert first.id == repeated.id
        assert WeatherAlert.query.count() == 1
        assert len(first.dedupe_key) == 64
        assert "alert-20260718-001" not in first.dedupe_key


@pytest.mark.parametrize(
    "changed_identity",
    [
        {"source_id": "alert-20260718-002"},
        {"start_time": "2026-07-18T09:00:00+08:00"},
    ],
)
def test_official_weather_alert_distinct_events_do_not_collapse(
    app,
    db_session,
    changed_identity,
):
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow

    with app.app_context():
        now = utcnow()
        first = _create_official_alert(
            db_session,
            now=now,
            warning=_official_warning(),
        )
        distinct = _create_official_alert(
            db_session,
            now=now + timedelta(minutes=1),
            warning=_official_warning(**changed_identity),
        )

        assert first.id != distinct.id
        assert WeatherAlert.query.count() == 2


@pytest.mark.parametrize(
    "revision_changes",
    [
        {
            "level": "橙色",
            "title": "高温橙色预警",
            "message_type": "update",
        },
        {
            "message_type": "update",
            "supersedes": ["alert-20260718-000"],
            "text": "预警防御建议已更新",
            "raw": {"updateTime": "2026-07-18T08:15:00+08:00"},
        },
    ],
)
def test_official_weather_alert_level_or_revision_upgrade_creates_new_revision(
    app,
    db_session,
    revision_changes,
):
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow

    with app.app_context():
        now = utcnow()
        first = _create_official_alert(
            db_session,
            now=now,
            warning=_official_warning(),
        )
        upgraded = _create_official_alert(
            db_session,
            now=now + timedelta(minutes=1),
            warning=_official_warning(**revision_changes),
        )

        assert first.id != upgraded.id
        assert WeatherAlert.query.count() == 2


def test_official_level_upgrade_creates_a_new_delivery(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import AlertDelivery, WeatherAlert
    from core.time_utils import utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        _create_push_recipient(
            db_session,
            username="official_level_upgrade",
            short_code="92000020",
        )
        now = utcnow()
        _forbid_weather_upstream(monkeypatch)
        send_calls = []
        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *_args, **_kwargs: send_calls.append(1) or {"ok": True},
        )

        _persist_dispatch_snapshot(
            warnings=[_official_warning()],
            fetched_at=now,
        )
        first = dispatch_mod.dispatch_alerts(now=now)
        _persist_dispatch_snapshot(
            warnings=[_official_warning(
                level="橙色",
                title="高温橙色预警",
                message_type="update",
                raw={"updateTime": "2026-07-18T08:15:00+08:00"},
            )],
            fetched_at=now + timedelta(minutes=1),
        )
        upgraded = dispatch_mod.dispatch_alerts(now=now + timedelta(minutes=1))

        assert first["deliveries"] == 1
        assert upgraded["deliveries"] == 1
        assert send_calls == [1, 1]
        assert WeatherAlert.query.count() == 2
        assert AlertDelivery.query.count() == 2
        assert {item.alert_level for item in WeatherAlert.query.all()} == {"黄色", "橙色"}


def test_threshold_alert_keeps_rolling_dedupe_across_hash_window_boundary(
    app,
    db_session,
):
    from core.db_models import WeatherAlert
    from services.push.dispatch import (
        _build_alert_dedupe_key,
        _get_or_create_weather_alert,
    )

    with app.app_context():
        first_time = datetime(2026, 7, 18, 5, 59, tzinfo=timezone.utc)
        second_time = first_time + timedelta(minutes=2)
        first_key = _build_alert_dedupe_key(
            now=first_time,
            location_key="101240210",
            alert_type="heat_threshold",
            alert_level="阈值",
            dedupe_hours=6,
        )
        second_key = _build_alert_dedupe_key(
            now=second_time,
            location_key="101240210",
            alert_type="heat_threshold",
            alert_level="阈值",
            dedupe_hours=6,
        )
        assert first_key != second_key

        first = _get_or_create_weather_alert(
            now=first_time,
            location_key="101240210",
            alert_type="heat_threshold",
            alert_level="阈值",
            description="第一次阈值提醒",
            dedupe_hours=6,
            dedupe_key=first_key,
        )
        db_session.commit()
        repeated = _get_or_create_weather_alert(
            now=second_time,
            location_key="101240210",
            alert_type="heat_threshold",
            alert_level="阈值",
            description="第二次阈值提醒",
            dedupe_hours=6,
            dedupe_key=second_key,
        )

        assert first.id == repeated.id
        assert WeatherAlert.query.count() == 1
