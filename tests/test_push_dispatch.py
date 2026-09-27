# -*- coding: utf-8 -*-

from datetime import datetime, timezone

from core.time_utils import utcnow


def _production_weather(**overrides):
    """生成通过生产门的实时和风天气。"""
    payload = {
        "temperature": 36,
        "temperature_max": 38,
        "temperature_min": 27,
        "humidity": 68,
        "pressure": 1003,
        "weather_condition": "晴",
        "wind_speed": 2.0,
        "data_source": "QWeather",
        "observed_at": utcnow().isoformat(),
        "quality_version": 1,
        "is_mock": False,
    }
    payload.update(overrides)
    return payload


def test_dispatch_alerts_dedupes_success(app, db_session, monkeypatch):
    from core.db_models import AlertDelivery, Pair, UsageEvent, User, WeatherAlert
    from core.security import hash_short_code
    from core.time_utils import ensure_utc_aware, utcnow
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        # user with push enabled
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

        # 模拟发送成功并保留第三方收到的文案，避免真实网络调用。
        sent = []
        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda *args, **kwargs: sent.append((args, kwargs)) or {"ok": True, "msg_id": "1"},
        )
        monkeypatch.setattr(dispatch_mod, "get_qweather_warnings", lambda _location: [])
        monkeypatch.setattr(
            dispatch_mod,
            "get_weather_with_cache",
            lambda _location: (_production_weather(), False),
        )

        fixed_now = datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc)
        result1 = dispatch_mod.dispatch_alerts(now=fixed_now)
        assert result1["deliveries"] == 1
        assert AlertDelivery.query.count() == 1
        assert UsageEvent.query.filter_by(event_type="push_sent").count() == 1
        alert = WeatherAlert.query.one()
        assert alert.source == "AppThreshold"
        assert alert.is_official is False
        assert alert.alert_level == "行动阈值"
        assert ensure_utc_aware(alert.starts_at) == fixed_now
        assert alert.ends_at is None
        assert "行动阈值" in alert.description
        assert "官方预警" not in alert.description
        assert "天气提醒" in sent[0][1]["content"]
        assert "行动阈值" in sent[0][1]["content"]
        assert "官方预警" not in sent[0][1]["content"]

        # second run should dedupe (no new delivery)
        result2 = dispatch_mod.dispatch_alerts(now=fixed_now)
        assert AlertDelivery.query.count() == 1
        assert result2["deliveries"] == 0 or result2["sent"] == 0


def test_threshold_alert_rejects_mock_weather():
    from services.push.dispatch import _threshold_alert

    assert _threshold_alert({
        "temperature_max": 39,
        "temperature_min": 29,
        "data_source": "Demo",
        "is_mock": True,
    }) is None
    threshold = _threshold_alert(_production_weather(
        temperature_max=39,
        temperature_min=29,
    ))
    assert threshold is not None
    assert threshold[1] == "行动阈值"
    assert "行动阈值" in threshold[2]
    assert "官方预警" not in threshold[2]


def test_parse_warning_datetime_returns_utc_aware():
    from services.push.dispatch import _parse_warning_datetime

    starts_at = _parse_warning_datetime("2026-02-09T08:00:00+08:00")
    ends_at = _parse_warning_datetime("2026-02-09T12:30:00Z")
    naive_qweather_time = _parse_warning_datetime("2026-02-09T20:00:00")

    assert starts_at == datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc)
    assert ends_at == datetime(2026, 2, 9, 12, 30, tzinfo=timezone.utc)
    assert naive_qweather_time == datetime(2026, 2, 9, 12, 0, tzinfo=timezone.utc)
    assert starts_at.tzinfo is timezone.utc
    assert _parse_warning_datetime("不是时间") is None


def test_official_warning_window_requires_complete_current_validity():
    from services.push.dispatch import _active_warning_window

    now = datetime(2026, 2, 9, 2, 0, tzinfo=timezone.utc)
    active = {
        'start_time': '2026-02-09T08:00:00+08:00',
        'end_time': '2026-02-09T12:00:00+08:00',
    }
    assert _active_warning_window(active, now) == (
        datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 9, 4, 0, tzinfo=timezone.utc),
    )
    assert _active_warning_window({**active, 'end_time': ''}, now) is None
    assert _active_warning_window({
        **active,
        'end_time': '2026-02-09T09:00:00+08:00',
    }, now) is None
    assert _active_warning_window({
        **active,
        'start_time': '2026-02-09T11:00:00+08:00',
    }, now) is None


def test_dispatch_persists_qweather_provenance_and_validity(app, db_session, monkeypatch):
    from core.db_models import Pair, User, WeatherAlert
    from core.security import hash_short_code
    from core.time_utils import ensure_utc_aware
    from services.push import dispatch as dispatch_mod

    with app.app_context():
        user = User(username="official_push", role="user", wxpusher_uid="UID_OFFICIAL", push_enabled=True)
        user.set_password("pw123456")
        db_session.add(user)
        db_session.commit()

        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌",
            location_query="都昌",
            elder_code="official_elder",
            short_code="24681357",
            short_code_hash=hash_short_code("24681357"),
            status="active",
        )
        db_session.add(pair)
        db_session.commit()

        warning = {
            "title": "高温黄色预警",
            "type": "高温",
            "level": "黄色",
            "text": "预计白天最高气温将超过 35°C，请注意防暑降温。",
            "start_time": "2026-02-09T08:00:00+08:00",
            "end_time": "2026-02-09T20:00:00+08:00",
        }
        captured = {}
        monkeypatch.setattr(dispatch_mod, "get_qweather_warnings", lambda _location: [warning])
        monkeypatch.setattr(
            dispatch_mod,
            "get_weather_with_cache",
            lambda _location: (_production_weather(temperature_max=30, temperature_min=20), False),
        )
        monkeypatch.setattr(
            dispatch_mod,
            "wxpusher_send",
            lambda _uid, title, content, url=None: captured.update({"title": title, "content": content}) or {"ok": True},
        )

        result = dispatch_mod.dispatch_alerts(
            now=datetime(2026, 2, 9, 1, 30, tzinfo=timezone.utc)
        )

        assert result["deliveries"] == 1
        alert = WeatherAlert.query.one()
        assert alert.source == "QWeather"
        assert alert.is_official is True
        assert ensure_utc_aware(alert.starts_at) == datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc)
        assert ensure_utc_aware(alert.ends_at) == datetime(2026, 2, 9, 12, 0, tzinfo=timezone.utc)
        assert alert.description == warning["text"]
        assert "官方预警" in captured["content"]


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
            status="sent",
            delivery_token="tok_test_123",
            sent_at=utcnow(),
        )
        db_session.add(delivery)
        db_session.commit()

    resp = client.get("/t/tok_test_123", follow_redirects=False)
    assert resp.status_code in (301, 302)

    with app.app_context():
        refreshed = AlertDelivery.query.filter_by(delivery_token="tok_test_123").first()
        assert refreshed is not None
        assert refreshed.clicked_at is not None
        assert UsageEvent.query.filter_by(event_type="push_click").count() == 1


def _stub_dispatch_weather(monkeypatch, dispatch_mod):
    monkeypatch.setattr(dispatch_mod, "get_qweather_warnings", lambda _location: [])
    monkeypatch.setattr(
        dispatch_mod,
        "get_weather_with_cache",
        lambda _location: (_production_weather(), False),
    )


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
        _stub_dispatch_weather(monkeypatch, dispatch_mod)

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
        monkeypatch.setattr(
            dispatch_mod,
            "resolve_location",
            lambda _query: {
                "location_code": "101240201",
                "provider": "amap",
                "display_name": "都昌某路123号",
            },
        )
        _stub_dispatch_weather(monkeypatch, dispatch_mod)

        result = dispatch_mod.dispatch_alerts()

        assert result["deliveries"] == 1
        assert "不应外发的姓名" not in captured["content"]
        assert "都昌某路123号" not in captured["content"]
        assert "地点：都昌县" in captured["content"]
