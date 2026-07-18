# -*- coding: utf-8 -*-
"""埋点权限、匿名化与 30 天保留策略回归测试。"""

from datetime import timedelta
import json
import threading
import time

import pytest
from sqlalchemy import text

from core.time_utils import utcnow


SERVER_ONLY_EVENTS = (
    "pair_created",
    "elder_profile_created",
    "elder_profile_updated",
    "template_view",
    "push_sent",
    "push_failed",
    "push_click",
    "help_flagged",
    "checkin_confirmed",
    "wxoa_land",
    "wechat_login_success",
)


def _create_miniprogram_user(db_session, username):
    from core.db_models import User
    from core.usage import create_api_token

    user = User(username=username, role="user")
    user.set_password("safe-test-password")
    db_session.add(user)
    db_session.commit()
    return user, create_api_token(user.id, name="usage-event-test")


def _create_pair(db_session, user, code):
    from core.db_models import Pair
    from core.security import hash_short_code

    pair = Pair(
        caregiver_id=user.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code=f"elder-{code}",
        short_code=code,
        short_code_hash=hash_short_code(code),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    return pair


def test_usage_event_ignores_member_id_and_requires_a_completed_action(app, db_session):
    from core.db_models import FamilyMember, UsageEvent, User
    from core.usage import log_usage_event

    user = User(username="anonymous-event-owner", role="user")
    user.set_password("safe-test-password")
    db_session.add(user)
    db_session.flush()
    member = FamilyMember(user_id=user.id, name="测试成员")
    db_session.add(member)
    db_session.commit()

    ordinary = log_usage_event(
        "template_view",
        user_id=user.id,
        member_id=member.id,
        source="web",
    )
    empty_action = log_usage_event(
        "checkin_confirmed",
        user_id=user.id,
        member_id=member.id,
        source="web",
        meta={"actions_done_count": 0},
    )
    valid_action = log_usage_event(
        "checkin_confirmed",
        user_id=user.id,
        member_id=member.id,
        source="web",
        meta={"actions_done_count": 1},
    )

    assert ordinary.member_id is None
    assert empty_action is None
    assert valid_action.member_id is None
    assert UsageEvent.query.filter_by(
        event_type="checkin_confirmed",
        user_id=user.id,
    ).count() == 1


def test_usage_event_drops_profile_field_name_lists(app, db_session):
    from core.db_models import UsageEvent
    from core.usage import log_usage_event

    with app.app_context():
        event = log_usage_event(
            "template_view",
            source="web",
            meta={
                "channel": "web",
                "fields": ["name", "age"],
                "updated_fields": ["chronic_diseases", "push_enabled"],
            },
        )

        stored = db_session.get(UsageEvent, event.id)
        assert json.loads(stored.meta_json) == {"channel": "web"}


def test_usage_event_skips_deleted_or_missing_owner(app, db_session):
    """已注销或不存在的账号不能在清理后复活分析事件。"""
    from core.db_models import UsageEvent, User
    from core.usage import log_usage_event

    owner = User(username="deleted-usage-owner", role="user", deleted_at=utcnow())
    owner.set_password("safe-test-password")
    db_session.add(owner)
    db_session.commit()

    deleted_event = log_usage_event(
        "template_view",
        user_id=owner.id,
        source="web",
        meta={"channel": "web"},
    )
    missing_event = log_usage_event(
        "template_view",
        user_id=owner.id + 999_999,
        source="web",
        meta={"channel": "web"},
    )

    assert deleted_event is None
    assert missing_event is None
    assert UsageEvent.query.count() == 0


def test_userless_system_usage_event_does_not_require_an_owner_lock(
    app,
    db_session,
    monkeypatch,
):
    """无 user_id 的系统事件保持原有 best-effort 写入语义。"""
    from core import usage as usage_module
    from core.db_models import UsageEvent

    monkeypatch.setattr(
        usage_module,
        "_lock_active_usage_event_owner_for_write",
        lambda _user_id: pytest.fail("无 user_id 事件不应请求 User 行锁"),
    )

    event = usage_module.log_usage_event(
        "template_view",
        user_id=None,
        source="system",
        meta={"channel": "web"},
    )

    assert event is not None
    stored = db_session.get(UsageEvent, event.id)
    assert stored.user_id is None
    assert stored.source == "system"
    assert json.loads(stored.meta_json) == {"channel": "web"}


def test_sqlite_usage_event_and_account_delete_share_the_owner_write_lock(
    app,
    db_session,
    monkeypatch,
):
    """埋点先取锁时，注销必须等待并在随后清除该事件。"""
    from core import usage as usage_module
    from core.db_models import UsageEvent, User
    from core.extensions import db

    if db.engine.dialect.name != "sqlite":
        pytest.skip("此回归专门验证 SQLite no-op UPDATE 写锁")

    owner = User(username="usage-delete-race-owner", role="user")
    owner.set_password("safe-test-password")
    db_session.add(owner)
    db_session.commit()
    owner_id = int(owner.id)

    writer_locked = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    original_owner_lock = usage_module._lock_active_usage_event_owner_for_write

    def blocked_owner_lock(user_id):
        owner_record = original_owner_lock(user_id)
        assert owner_record is not None
        writer_locked.set()
        assert release_writer.wait(timeout=5)
        return owner_record

    monkeypatch.setattr(
        usage_module,
        "_lock_active_usage_event_owner_for_write",
        blocked_owner_lock,
    )

    def write_event():
        with app.app_context():
            outcomes["write"] = usage_module.log_usage_event(
                "template_view",
                user_id=owner_id,
                source="web",
                meta={"channel": "web"},
            ) is not None

    def delete_account_data():
        with app.app_context():
            lock_result = db.session.execute(
                db.update(User)
                .where(User.id == owner_id, User.deleted_at.is_(None))
                .values(last_login=User.last_login)
            )
            outcomes["delete_lock_rowcount"] = lock_result.rowcount
            owner_record = db.session.get(User, owner_id)
            owner_record.deleted_at = utcnow()
            db.session.flush()
            UsageEvent.query.filter_by(user_id=owner_id).delete(
                synchronize_session=False,
            )
            db.session.commit()
            outcomes["delete"] = True

    writer = threading.Thread(target=write_event)
    deleter = threading.Thread(target=delete_account_data)
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
    assert outcomes == {
        "write": True,
        "delete_lock_rowcount": 1,
        "delete": True,
    }
    db_session.expire_all()
    assert db_session.get(User, owner_id).deleted_at is not None
    assert UsageEvent.query.filter_by(user_id=owner_id).count() == 0


def test_client_event_sources_are_fixed_and_member_id_is_not_stored(
    app,
    authenticated_client,
    client,
    db_session,
):
    from core.db_models import FamilyMember, UsageEvent, User

    web_user = User.query.filter_by(username="testuser").one()
    web_member = FamilyMember(user_id=web_user.id, name="Web 成员")
    db_session.add(web_member)
    db_session.commit()
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "usage-source-csrf"

    web_response = authenticated_client.post(
        "/api/v1/events",
        json={
            "event_type": "template_copy",
            "source": "cron",
            "member_id": web_member.id,
            "meta": {"channel": "web"},
        },
        headers={"X-CSRF-Token": "usage-source-csrf"},
    )
    assert web_response.status_code == 200

    mini_user, token = _create_miniprogram_user(db_session, "mini-source-owner")
    mini_member = FamilyMember(user_id=mini_user.id, name="小程序成员")
    db_session.add(mini_member)
    db_session.commit()
    mini_response = client.post(
        "/mp/api/v1/events",
        json={
            "event_type": "template_copy",
            "source": "system",
            "member_id": mini_member.id,
            "meta": {"channel": "wechat_miniprogram"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert mini_response.status_code == 200

    web_event = UsageEvent.query.filter_by(event_type="template_copy", source="web").one()
    mini_event = UsageEvent.query.filter_by(
        event_type="template_copy",
        source="miniprogram",
    ).one()
    assert (web_event.source, web_event.member_id) == ("web", None)
    assert (mini_event.source, mini_event.member_id) == ("miniprogram", None)


def test_web_feedback_event_remains_available_but_miniprogram_cannot_forge_it(
    app,
    authenticated_client,
    client,
    db_session,
):
    from core.db_models import UsageEvent

    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "web-feedback-csrf"

    web_response = authenticated_client.post(
        "/api/v1/events",
        json={"event_type": "feedback_submitted", "meta": {"kind": "acted"}},
        headers={"X-CSRF-Token": "web-feedback-csrf"},
    )
    _user, token = _create_miniprogram_user(db_session, "mini-feedback-owner")
    mini_response = client.post(
        "/mp/api/v1/events",
        json={"event_type": "feedback_submitted", "meta": {"kind": "acted"}},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert web_response.status_code == 200
    assert mini_response.status_code == 400
    event = UsageEvent.query.filter_by(event_type="feedback_submitted").one()
    assert event.source == "web"
    assert event.member_id is None


@pytest.mark.parametrize("event_type", SERVER_ONLY_EVENTS)
def test_clients_cannot_submit_server_only_events(
    event_type,
    app,
    authenticated_client,
    client,
    db_session,
):
    from core.db_models import UsageEvent

    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "server-event-csrf"
    web_response = authenticated_client.post(
        "/api/v1/events",
        json={"event_type": event_type, "source": "cron"},
        headers={"X-CSRF-Token": "server-event-csrf"},
    )
    _user, token = _create_miniprogram_user(
        db_session,
        f"server-event-{event_type}",
    )
    mini_response = client.post(
        "/mp/api/v1/events",
        json={"event_type": event_type, "source": "system"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert web_response.status_code == 400
    assert mini_response.status_code == 400
    assert UsageEvent.query.filter_by(event_type=event_type).count() == 0


def test_client_event_metadata_rejects_top_level_lists(
    app,
    authenticated_client,
    client,
    db_session,
):
    with authenticated_client.session_transaction() as session:
        session["_csrf_token"] = "list-meta-csrf"
    web_response = authenticated_client.post(
        "/api/v1/events",
        json={"event_type": "template_copy", "meta": ["channel", "web"]},
        headers={"X-CSRF-Token": "list-meta-csrf"},
    )
    _user, token = _create_miniprogram_user(db_session, "list-meta-owner")
    mini_response = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy", "meta": ["channel", "web"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert web_response.status_code == 400
    assert web_response.get_json()["error"] == "invalid_meta"
    assert mini_response.status_code == 400
    assert mini_response.get_json()["error"] == "invalid_meta"


def test_wechat_login_records_only_strict_acquisition_enum(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import UsageEvent, User

    user = User(username="wechat-acquisition-owner", role="user")
    user.set_password("safe-test-password")
    db_session.add(user)
    db_session.commit()
    login_result = {
        "session_token": "signed-session",
        "token": "signed-session",
        "user": {"id": user.id, "display_name": "微信用户"},
    }
    received_sources = []

    def fake_login(_code, _consent, acquisition_source):
        received_sources.append(acquisition_source)
        return login_result

    monkeypatch.setattr("blueprints.mp_api.login_with_wechat_code", fake_login)

    payloads = (
        {
            "acquisition_source": "family_share",
            "member_id": 999,
            "device": "private-device",
            "ip": "203.0.113.99",
        },
        {"acquisition_source": "campaign-free-text"},
        {"acquisition_source": ["family_share"]},
    )
    for extra_payload in payloads:
        response = client.post(
            "/mp/api/v1/auth/wechat",
            json={
                "code": "wx-login-code",
                "privacy_consent_version": "privacy-v1",
                **extra_payload,
            },
        )
        assert response.status_code == 200

    events = UsageEvent.query.filter_by(event_type="wechat_login_success").order_by(
        UsageEvent.id.asc()
    ).all()
    assert [json.loads(event.meta_json) for event in events] == [
        {"from": "family_share"},
        {"from": "direct"},
        {"from": "direct"},
    ]
    assert all(event.user_id == user.id for event in events)
    assert all(event.member_id is None for event in events)
    assert all(event.pair_id is None for event in events)
    assert all(event.source == "miniprogram" for event in events)
    serialized = " ".join(event.meta_json or "" for event in events)
    assert "private-device" not in serialized
    assert "203.0.113.99" not in serialized
    assert "campaign-free-text" not in serialized
    assert received_sources == ["family_share", "direct", "direct"]


def test_miniprogram_valid_action_event_requires_one_completed_action(
    app,
    client,
    db_session,
):
    from core.db_models import UsageEvent

    user, token = _create_miniprogram_user(db_session, "valid-action-owner")
    pair = _create_pair(db_session, user, "60606060")
    headers = {"Authorization": f"Bearer {token}"}

    empty_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        json={"actions_done": []},
        headers=headers,
    )
    assert empty_response.status_code == 200
    assert UsageEvent.query.filter_by(event_type="checkin_confirmed").count() == 0

    valid_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        json={"actions_done": ["hydrate"]},
        headers=headers,
    )
    assert valid_response.status_code == 200
    event = UsageEvent.query.filter_by(event_type="checkin_confirmed").one()
    assert event.source == "miniprogram"
    assert event.member_id is None


def test_usage_event_retention_deletes_only_rows_older_than_30_days_in_batches(
    app,
    db_session,
):
    from core.db_models import UsageEvent
    from core.usage import delete_expired_usage_events

    now = utcnow()
    rows = {
        "old": UsageEvent(event_type="template_view", source="web", created_at=now - timedelta(days=31)),
        "boundary": UsageEvent(event_type="template_view", source="web", created_at=now - timedelta(days=30)),
        "fresh": UsageEvent(event_type="template_view", source="web", created_at=now - timedelta(days=29)),
        "unknown": UsageEvent(event_type="template_view", source="web", created_at=now),
    }
    db_session.add_all(rows.values())
    db_session.commit()
    db_session.execute(
        text("UPDATE usage_events SET created_at = NULL WHERE id = :event_id"),
        {"event_id": rows["unknown"].id},
    )
    db_session.commit()

    result = delete_expired_usage_events(now=now, batch_size=1, max_batches=2)

    assert result["deleted"] == 2
    assert result["complete"] is False
    remaining_ids = {row.id for row in UsageEvent.query.all()}
    assert remaining_ids == {rows["boundary"].id, rows["fresh"].id}


def test_alert_delivery_click_retention_clears_only_timestamps_older_than_30_days(
    app,
    db_session,
):
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.usage import clear_expired_alert_delivery_clicks

    now = utcnow()
    user = User(username="click-retention-owner", role="user")
    user.set_password("safe-test-password")
    alert = WeatherAlert(
        alert_date=now,
        location="116.20,29.27",
        alert_type="heat_threshold",
        alert_level="阈值",
        description="test",
        affected_communities="[]",
        disease_correlation="{}",
    )
    db_session.add_all((user, alert))
    db_session.flush()
    rows = {
        "oldest": AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            channel="wxpusher",
            status="sent",
            delivery_token="click-retention-oldest",
            clicked_at=now - timedelta(days=32),
            reviewed_at=now - timedelta(days=32),
            review_action="click_confirmed",
        ),
        "old": AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            channel="wxpusher_test",
            status="sent",
            delivery_token="click-retention-old",
            clicked_at=now - timedelta(days=31),
            reviewed_at=now - timedelta(days=31),
            review_action="allow_retry",
        ),
        "boundary": AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            channel="wxpusher_boundary",
            status="sent",
            delivery_token="click-retention-boundary",
            clicked_at=now - timedelta(days=30),
        ),
        "fresh": AlertDelivery(
            alert_id=alert.id,
            user_id=user.id,
            channel="wxpusher_fresh",
            status="sent",
            delivery_token="click-retention-fresh",
            clicked_at=now - timedelta(days=29),
        ),
    }
    db_session.add_all(rows.values())
    db_session.commit()

    result = clear_expired_alert_delivery_clicks(
        now=now,
        batch_size=1,
        max_batches=2,
    )

    assert result["cleared"] == 2
    assert result["complete"] is False
    db_session.expire_all()
    oldest = db_session.get(AlertDelivery, rows["oldest"].id)
    old_manual_review = db_session.get(AlertDelivery, rows["old"].id)
    assert oldest.clicked_at is None
    assert oldest.reviewed_at is None
    assert oldest.review_action is None
    assert old_manual_review.clicked_at is None
    assert old_manual_review.reviewed_at is not None
    assert old_manual_review.review_action == "allow_retry"
    assert db_session.get(AlertDelivery, rows["boundary"].id).clicked_at is not None
    assert db_session.get(AlertDelivery, rows["fresh"].id).clicked_at is not None


def test_scheduled_weather_sync_leaves_usage_event_retention_to_daily_cleanup(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import UsageEvent
    from services.pipelines import sync_weather_cache as pipeline

    db_session.add(
        UsageEvent(
            event_type="template_view",
            source="web",
            created_at=utcnow() - timedelta(days=31),
        )
    )
    db_session.commit()

    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "qweather_runtime_configured", lambda: False)
    monkeypatch.setattr(
        pipeline,
        "WeatherService",
        lambda: pytest.fail("未配置和风天气时不应启动天气客户端"),
    )
    monkeypatch.setattr(pipeline, "refresh_snapshot_from_cycle", lambda *_args, **_kwargs: None)

    result = pipeline.sync_weather_cache(update_daily=False)

    assert "usage_events_deleted" not in result
    assert UsageEvent.query.count() == 1
