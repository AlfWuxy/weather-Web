# -*- coding: utf-8 -*-

import sqlite3
from datetime import timedelta

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
    from services.push.dispatch import _claim_delivery

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


def test_tracking_route_marks_clicked(client, app, db_session):
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

    resp = client.get("/t/tok_test_123", follow_redirects=False)
    assert resp.status_code in (301, 302)

    with app.app_context():
        refreshed = AlertDelivery.query.filter_by(delivery_token="tok_test_123").first()
        assert refreshed is not None
        assert refreshed.clicked_at is not None
        assert refreshed.status == "sent"
        assert refreshed.review_action == "click_confirmed"
        assert refreshed.reviewed_at is not None
        assert refreshed.error is None
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
