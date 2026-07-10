# -*- coding: utf-8 -*-


def test_dispatch_alerts_dedupes_success(app, db_session, monkeypatch):
    from core.db_models import AlertDelivery, Pair, UsageEvent, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
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

        # Force WxPusher send success without network
        monkeypatch.setattr(dispatch_mod, "wxpusher_send", lambda *args, **kwargs: {"ok": True, "msg_id": "1"})
        monkeypatch.setattr(dispatch_mod, "get_qweather_warnings", lambda _location: [])
        monkeypatch.setattr(
            dispatch_mod,
            "get_weather_with_cache",
            lambda _location: ({
                "temperature": 36,
                "temperature_max": 38,
                "temperature_min": 27,
                "data_source": "QWeather",
                "is_mock": False,
            }, False),
        )

        result1 = dispatch_mod.dispatch_alerts()
        assert result1["deliveries"] == 1
        assert AlertDelivery.query.count() == 1
        assert UsageEvent.query.filter_by(event_type="push_sent").count() == 1

        # second run should dedupe (no new delivery)
        result2 = dispatch_mod.dispatch_alerts()
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
        lambda _location: ({
            "temperature": 36,
            "temperature_max": 38,
            "temperature_min": 27,
            "data_source": "QWeather",
            "is_mock": False,
        }, False),
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
        assert "地点：所在地区" in captured["content"]
