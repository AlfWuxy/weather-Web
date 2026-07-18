# -*- coding: utf-8 -*-
"""微信小程序运行时：快照、认证、owner scope 与输入边界。"""

import json
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest

from core.time_utils import utcnow


CURRENT = {
    "temperature": 36.0,
    "temperature_max": 38.0,
    "temperature_min": 28.0,
    "humidity": 72.0,
    "weather_condition": "晴",
    "is_mock": False,
    "data_source": "QWeather",
}

FORECAST = [
    {
        "date": "2026-07-17",
        "temperature_mean": 35.0,
        "temperature_max": 39.0,
        "temperature_min": 29.0,
        "humidity": 70.0,
        "is_mock": False,
        "data_source": "QWeather",
    }
]


def _user_and_token(db_session, username):
    from core.db_models import User
    from core.usage import create_api_token

    user = User(username=username, role="user")
    user.set_password("safe-test-password")
    db_session.add(user)
    db_session.commit()
    return user, create_api_token(user.id, name="runtime-test")


def _pair(db_session, user, code, *, member=None):
    from core.db_models import Pair
    from core.security import hash_short_code

    record = Pair(
        caregiver_id=user.id,
        member_id=member.id if member else None,
        community_code="都昌县",
        location_query="都昌县",
        elder_code=f"elder-{code}",
        short_code=code,
        short_code_hash=hash_short_code(code),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(record)
    db_session.commit()
    return record


def _persist_snapshot(app, *, fetched_at=None):
    from services.miniprogram_service import persist_snapshot

    with app.app_context():
        record = persist_snapshot(
            CURRENT,
            FORECAST,
            [],
            fetched_at=fetched_at or utcnow(),
        )
        return record.snapshot_id


def test_snapshot_fresh_at_2959_and_stale_at_3001(app, db_session):
    from services.miniprogram_service import snapshot_payload

    fetched_at = utcnow()
    snapshot_id = _persist_snapshot(app, fetched_at=fetched_at)
    with app.app_context():
        from core.db_models import MiniProgramSnapshot

        record = MiniProgramSnapshot.query.filter_by(snapshot_id=snapshot_id).one()
        fresh = snapshot_payload(record, now=fetched_at + timedelta(minutes=29, seconds=59))
        stale = snapshot_payload(record, now=fetched_at + timedelta(minutes=30, seconds=1))

    assert fresh["ttl_seconds"] == 1800
    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert fresh["snapshot_id"] == stale["snapshot_id"]


def test_bootstrap_is_database_only_and_keeps_same_snapshot_id(
    app,
    client,
    db_session,
    monkeypatch,
):
    snapshot_id = _persist_snapshot(app)
    monkeypatch.setattr(
        "requests.get",
        lambda *_args, **_kwargs: pytest.fail("bootstrap 不得访问外网"),
    )

    first = client.get("/mp/api/v1/bootstrap")
    second = client.get("/mp/api/v1/bootstrap")

    assert first.status_code == 200
    assert first.get_json()["data"]["snapshot_id"] == snapshot_id
    assert second.get_json()["data"]["snapshot_id"] == snapshot_id
    assert first.get_json()["data"]["location"]["name"] == "都昌县"
    assert first.get_json()["data"]["required_privacy_consent_version"]
    source_status = first.get_json()["data"]["source_status"]
    assert source_status["budget_guard"] == "enabled"
    assert "limit" not in str(source_status).lower()
    assert "remaining" not in str(source_status).lower()


def test_all_elders_share_one_snapshot_without_weather_fanout(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember

    user, token = _user_and_token(db_session, "shared_snapshot_user")
    for index in range(3):
        member = FamilyMember(user_id=user.id, name=f"老人{index}", relation="家人")
        db_session.add(member)
        db_session.flush()
        _pair(db_session, user, f"7000000{index}", member=member)
    snapshot_id = _persist_snapshot(app)
    monkeypatch.setattr(
        "blueprints.mp_api.get_weather_with_cache",
        lambda *_args, **_kwargs: pytest.fail("elders 不得逐人获取天气"),
    )
    monkeypatch.setattr(
        "blueprints.mp_api.get_qweather_warnings",
        lambda *_args, **_kwargs: pytest.fail("elders 不得逐人获取预警"),
    )

    response = client.get(
        "/mp/api/v1/elders",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    rows = response.get_json()["data"]
    assert len(rows) == 3
    assert {row["snapshot_id"] for row in rows} == {snapshot_id}
    assert {row["location"]["name"] for row in rows} == {"都昌县"}


def test_elders_batch_loads_only_current_day_elder_actions(
    app,
    client,
    db_session,
):
    """老人列表只批量读取当天自护状态，并安全收敛历史 JSON。"""
    from sqlalchemy import event

    from core.db_models import DailyStatus, FamilyMember
    from core.extensions import db
    from core.time_utils import today_local

    user, token = _user_and_token(db_session, "elder_today_state_user")
    pairs = []
    for index in range(3):
        member = FamilyMember(user_id=user.id, name=f"状态老人{index}", relation="家人")
        db_session.add(member)
        db_session.flush()
        pairs.append(_pair(db_session, user, f"7100000{index}", member=member))

    status_date = today_local()
    confirmed_at = utcnow()
    db_session.add_all([
        DailyStatus(
            pair_id=pairs[0].id,
            status_date=status_date,
            community_code="都昌县",
            confirmed_at=confirmed_at,
            actions_done_count=25,
            help_flag=True,
            relay_stage="caregiver",
            elder_actions=json.dumps(
                ["drink_water", "<script>alert(1)</script>"]
                + [f"legacy_{index}" for index in range(25)]
                + [123],
                ensure_ascii=False,
            ),
        ),
        DailyStatus(
            pair_id=pairs[1].id,
            status_date=status_date - timedelta(days=1),
            community_code="都昌县",
            confirmed_at=confirmed_at,
            actions_done_count=1,
            elder_actions=json.dumps(["carry_water"], ensure_ascii=False),
        ),
        DailyStatus(
            pair_id=pairs[2].id,
            status_date=status_date,
            community_code="都昌县",
            actions_done_count=1,
            elder_actions='{"not": "a list"}',
        ),
    ])
    db_session.commit()
    _persist_snapshot(app)

    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "daily_status" in statement.lower():
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record_statement)
    try:
        response = client.get(
            "/mp/api/v1/elders",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    rows = {row["pair_id"]: row for row in response.get_json()["data"]}
    current = rows[pairs[0].id]["today"]
    assert current["status_date"] == status_date.isoformat()
    assert current["confirmed_at"] == confirmed_at.replace(tzinfo=None).isoformat()
    assert current["actions_done_count"] == 20
    assert current["elder_actions"][0] == "drink_water"
    assert len(current["elder_actions"]) <= 20
    assert all(isinstance(item, str) for item in current["elder_actions"])
    assert "alert(1)" not in current["elder_actions"]
    assert current["help_flag"] is True
    assert current["relay_stage"] == "caregiver"

    yesterday_only = rows[pairs[1].id]["today"]
    assert yesterday_only["status_date"] == status_date.isoformat()
    assert yesterday_only["confirmed_at"] is None
    assert yesterday_only["actions_done_count"] == 0
    assert yesterday_only["elder_actions"] == []
    assert rows[pairs[2].id]["today"]["elder_actions"] == []
    assert rows[pairs[2].id]["today"]["help_flag"] is False
    assert rows[pairs[2].id]["today"]["relay_stage"] == "none"
    assert len(statements) == 1


class _WechatResponse:
    status_code = 200

    def __init__(self, openid="openid-sensitive-value"):
        self.openid = openid

    def json(self):
        return {"openid": self.openid, "session_key": "never-store-session-key"}


def _configure_wechat(app):
    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-appsecret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
        WX_MINIPROGRAM_SESSION_TTL_SECONDS=3600,
    )


def _wechat_login(
    app,
    client,
    monkeypatch,
    openid="openid-sensitive-value",
    acquisition_source=None,
):
    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse(openid),
    )
    payload = {"code": "wx-login-code", "privacy_consent_version": "privacy-v1"}
    if acquisition_source is not None:
        payload["acquisition_source"] = acquisition_source
    return client.post(
        "/mp/api/v1/auth/wechat",
        json=payload,
    )


def test_wechat_login_hashes_openid_and_issues_expiring_signed_session(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, MiniProgramSession
    from services.miniprogram_auth import hash_openid, verify_miniprogram_session

    response = _wechat_login(app, client, monkeypatch)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["session_token"] == data["token"]
    assert data["user"] == {"id": data["user"]["id"], "display_name": "微信用户"}
    assert "username" not in data["user"]
    serialized = str(response.get_json())
    assert "server-only-appsecret" not in serialized
    assert "openid-sensitive-value" not in serialized
    assert "never-store-session-key" not in serialized

    with app.app_context():
        identity = MiniProgramIdentity.query.one()
        session = MiniProgramSession.query.one()
        assert identity.openid_hash == hash_openid("openid-sensitive-value")
        assert "openid-sensitive-value" not in identity.openid_hash
        assert session.token_hash != data["session_token"]
        assert verify_miniprogram_session(data["session_token"]).id == session.id


def test_wechat_identity_keeps_first_acquisition_source(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity
    from core.time_utils import ensure_utc_aware

    first = _wechat_login(
        app,
        client,
        monkeypatch,
        openid="stable-acquisition-openid",
        acquisition_source="family_share",
    )
    assert first.status_code == 200
    identity = MiniProgramIdentity.query.one()
    first_created_at = ensure_utc_aware(identity.created_at)
    first_last_login = ensure_utc_aware(identity.last_login_at)

    second = _wechat_login(
        app,
        client,
        monkeypatch,
        openid="stable-acquisition-openid",
        acquisition_source="direct",
    )
    assert second.status_code == 200
    db_session.expire_all()
    identity = MiniProgramIdentity.query.one()

    assert identity.acquisition_source == "family_share"
    assert ensure_utc_aware(identity.created_at) == first_created_at
    assert ensure_utc_aware(identity.last_login_at) >= first_last_login


def test_wechat_identity_preserves_legacy_unknown_acquisition_source(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity

    first = _wechat_login(
        app,
        client,
        monkeypatch,
        openid="legacy-unknown-acquisition-openid",
    )
    assert first.status_code == 200

    identity = MiniProgramIdentity.query.one()
    identity.acquisition_source = "unknown"
    db_session.commit()

    second = _wechat_login(
        app,
        client,
        monkeypatch,
        openid="legacy-unknown-acquisition-openid",
        acquisition_source="direct",
    )
    assert second.status_code == 200
    db_session.expire_all()
    assert MiniProgramIdentity.query.one().acquisition_source == "unknown"


def test_privacy_consent_is_required_before_code_exchange(app, client, db_session, monkeypatch):
    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: pytest.fail("未同意隐私指引时不得换取 OpenID"),
    )
    response = client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "wx-login-code", "privacy_consent_version": "old-version"},
    )
    assert response.status_code == 428
    assert response.get_json()["error"] == "privacy_consent_required"
    assert response.get_json()["data"]["required_privacy_consent_version"] == "privacy-v1"


def test_logout_revokes_session_and_next_request_is_unauthorized(
    app,
    client,
    db_session,
    monkeypatch,
):
    login = _wechat_login(app, client, monkeypatch, openid="logout-openid")
    token = login.get_json()["data"]["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    logout = client.post("/mp/api/v1/auth/logout", headers=headers)
    after = client.get("/mp/api/v1/me", headers=headers)

    assert logout.status_code == 200
    assert logout.get_json()["data"]["revoked"] is True
    assert after.status_code == 401


def test_expired_session_is_rejected(app, client, db_session, monkeypatch):
    from core.db_models import MiniProgramSession

    login = _wechat_login(app, client, monkeypatch, openid="expired-openid")
    token = login.get_json()["data"]["session_token"]
    with app.app_context():
        record = MiniProgramSession.query.one()
        record.expires_at = utcnow() - timedelta(seconds=1)
        db_session.commit()

    response = client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_owner_scope_and_input_boundaries_for_diary_medication_and_actions(
    app,
    client,
    db_session,
):
    from core.db_models import FamilyMember

    owner, owner_token = _user_and_token(db_session, "runtime_owner")
    outsider, outsider_token = _user_and_token(db_session, "runtime_outsider")
    member = FamilyMember(user_id=owner.id, name="自己的老人", relation="父亲")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "71111111", member=member)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    created = client.post(
        "/mp/api/v1/health/diary",
        headers=owner_headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "有点乏力",
            "notes": "已补水",
        },
    )
    assert created.status_code == 201
    notes_only = client.post(
        "/mp/api/v1/health/diary",
        headers=owner_headers,
        json={"pair_id": pair.id, "severity": "none", "symptoms": "", "notes": "状态正常"},
    )
    assert notes_only.status_code == 201
    assert client.get(
        f"/mp/api/v1/health/diary?pair_id={pair.id}", headers=outsider_headers
    ).status_code in {400, 404}

    medication = client.post(
        "/mp/api/v1/medications",
        headers=owner_headers,
        json={
            "pair_id": pair.id,
            "medicine_name": "日常药物",
            "dosage": "1片",
            "frequency": "daily",
            "time_of_day": "08:00",
            "weather_triggers": {
                "high_temp": 35,
                "low_temp": 5,
                "high_humidity": 85,
                "high_aqi": 150,
            },
        },
    )
    assert medication.status_code == 201
    assert medication.get_json()["data"]["medication"]["weather_triggers"]["high_temp"] == 35.0
    medication_id = medication.get_json()["data"]["medication"]["id"]
    invalid_trigger = client.post(
        "/mp/api/v1/medications",
        headers=owner_headers,
        json={
            "pair_id": pair.id,
            "medicine_name": "无效阈值",
            "weather_triggers": {"high_humidity": 101},
        },
    )
    assert invalid_trigger.status_code == 400
    assert invalid_trigger.get_json()["error"] == "invalid_weather_triggers"
    assert client.delete(
        f"/mp/api/v1/medications/{medication_id}", headers=outsider_headers
    ).status_code == 404
    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=outsider_headers,
        json={"actions_done": ["hydrate"]},
    ).status_code == 404
    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=owner_headers,
        json={"actions_done": ["hydrate"]},
    ).status_code == 200


def test_health_diary_matches_miniprogram_free_text_boundaries(client, db_session):
    from core.db_models import FamilyMember

    owner, owner_token = _user_and_token(db_session, "diary_length_owner")
    member = FamilyMember(user_id=owner.id, name="边界测试家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "72222222", member=member)
    headers = {"Authorization": f"Bearer {owner_token}"}

    symptoms_at_limit = client.post(
        "/mp/api/v1/health/diary",
        headers=headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "症" * 200,
            "notes": "",
        },
    )
    assert symptoms_at_limit.status_code == 201

    symptoms_over_limit = client.post(
        "/mp/api/v1/health/diary",
        headers=headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "症" * 201,
            "notes": "",
        },
    )
    assert symptoms_over_limit.status_code == 400
    assert symptoms_over_limit.get_json()["error"] == "symptoms_too_long"

    notes_at_limit = client.post(
        "/mp/api/v1/health/diary",
        headers=headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "状态记录",
            "notes": "注" * 500,
        },
    )
    assert notes_at_limit.status_code == 201

    notes_over_limit = client.post(
        "/mp/api/v1/health/diary",
        headers=headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "状态记录",
            "notes": "注" * 501,
        },
    )
    assert notes_over_limit.status_code == 400
    assert notes_over_limit.get_json()["error"] == "notes_too_long"


def test_action_confirm_separates_elder_actions_from_caregiver_fields(client, db_session):
    from core.db_models import DailyStatus, FamilyMember
    from core.time_utils import today_local

    owner, owner_token = _user_and_token(db_session, "action_note_owner")
    member = FamilyMember(user_id=owner.id, name="行动记录家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73333333", member=member)
    headers = {"Authorization": f"Bearer {owner_token}"}

    help_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/help",
        headers=headers,
        json={"note": "请尽快回电并带水"},
    )
    assert help_response.status_code == 200
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    status.caregiver_actions = json.dumps(["remind"], ensure_ascii=False)
    db_session.commit()

    confirm_without_note = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=headers,
        json={"actions_done": ["drink_water"]},
    )
    assert confirm_without_note.status_code == 200
    db_session.expire_all()
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.help_flag is True
    assert status.caregiver_note == "请尽快回电并带水"
    assert json.loads(status.caregiver_actions) == ["remind"]
    assert json.loads(status.elder_actions) == ["drink_water"]

    confirm_with_note = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=headers,
        json={"actions_done": ["cool_rest"], "note": "已电话确认在家休息"},
    )
    assert confirm_with_note.status_code == 200
    db_session.expire_all()
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.help_flag is True
    assert status.caregiver_note == "请尽快回电并带水"
    assert json.loads(status.caregiver_actions) == ["remind"]
    assert json.loads(status.elder_actions) == ["cool_rest"]


def test_repeated_help_updates_note_without_duplicate_event(client, db_session):
    """同一天再次保存求助说明只更新内容，不重复计一次新求助。"""
    from core.db_models import DailyStatus, FamilyMember, UsageEvent
    from core.time_utils import today_local

    owner, token = _user_and_token(db_session, "help_update_owner")
    member = FamilyMember(user_id=owner.id, name="求助家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73333334", member=member)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        f"/mp/api/v1/actions/{pair.id}/help",
        headers=headers,
        json={"note": "请回电话"},
    )
    second = client.post(
        f"/mp/api/v1/actions/{pair.id}/help",
        headers=headers,
        json={"note": "请带水并回电话"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.help_flag is True
    assert status.caregiver_note == "请带水并回电话"
    assert UsageEvent.query.filter_by(
        user_id=owner.id,
        event_type="help_flagged",
    ).count() == 1


def test_inactive_pair_blocks_profile_health_and_action_writes(client, db_session):
    """停止管理后，旧页面持有的 pair_id 不能继续产生任何写入。"""
    from core.db_models import FamilyMember, HealthDiary, MedicationReminder

    owner, token = _user_and_token(db_session, "inactive_pair_owner")
    member = FamilyMember(user_id=owner.id, name="停用家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73333335", member=member)
    member_id = member.id
    headers = {"Authorization": f"Bearer {token}"}

    deleted = client.delete(f"/mp/api/v1/elders/{pair.id}", headers=headers)
    assert deleted.status_code == 200

    patch_response = client.patch(
        f"/mp/api/v1/elders/{pair.id}",
        headers=headers,
        json={"name": "不应写入"},
    )
    diary_response = client.post(
        "/mp/api/v1/health/diary",
        headers=headers,
        json={
            "pair_id": pair.id,
            "severity": "mild",
            "symptoms": "不应写入",
        },
    )
    medication_response = client.post(
        "/mp/api/v1/medications",
        headers=headers,
        json={"pair_id": pair.id, "medicine_name": "不应写入"},
    )
    assessment_response = client.post(
        "/mp/api/v1/health/assessment",
        headers=headers,
        json={"pair_id": pair.id},
    )
    action_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/help",
        headers=headers,
        json={"note": "不应写入"},
    )

    assert patch_response.status_code == 404
    assert diary_response.status_code == 404
    assert medication_response.status_code == 404
    assert assessment_response.status_code == 404
    assert action_response.status_code == 404
    db_session.expire_all()
    assert db_session.get(FamilyMember, member_id).name == "停用家人"
    assert HealthDiary.query.filter_by(user_id=owner.id).count() == 0
    assert MedicationReminder.query.filter_by(user_id=owner.id).count() == 0


def test_explicit_unlinked_pair_scope_never_falls_back_to_all_health_records(
    client,
    db_session,
):
    """显式但无成员的 Pair 必须拒绝，不能退化成账号全量查询。"""
    from core.db_models import FamilyMember, HealthDiary, MedicationReminder
    from core.time_utils import today_local

    owner, token = _user_and_token(db_session, "unlinked_scope_owner")
    first_member = FamilyMember(user_id=owner.id, name="甲家人", relation="家人")
    second_member = FamilyMember(user_id=owner.id, name="乙家人", relation="家人")
    db_session.add_all([first_member, second_member])
    db_session.flush()
    unlinked_pair = _pair(db_session, owner, "73333336")
    db_session.add_all([
        HealthDiary(
            user_id=owner.id,
            member_id=first_member.id,
            entry_date=today_local(),
            symptoms="甲记录",
            severity="mild",
        ),
        HealthDiary(
            user_id=owner.id,
            member_id=second_member.id,
            entry_date=today_local(),
            symptoms="乙记录",
            severity="mild",
        ),
        MedicationReminder(user_id=owner.id, member_id=first_member.id, medicine_name="甲药"),
        MedicationReminder(user_id=owner.id, member_id=second_member.id, medicine_name="乙药"),
    ])
    db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    diary = client.get(
        f"/mp/api/v1/health/diary?pair_id={unlinked_pair.id}",
        headers=headers,
    )
    medications = client.get(
        f"/mp/api/v1/medications?pair_id={unlinked_pair.id}",
        headers=headers,
    )

    assert diary.status_code == 400
    assert diary.get_json()["error"] == "pair_member_not_found"
    assert medications.status_code == 400
    assert medications.get_json()["error"] == "pair_member_not_found"


def test_stale_snapshot_never_persists_risk_level_on_action_confirm(
    app,
    client,
    db_session,
):
    """较早快照只供页面参考，不能进入当天风险聚合。"""
    from core.db_models import DailyStatus, FamilyMember
    from core.time_utils import today_local

    owner, token = _user_and_token(db_session, "stale_action_risk_owner")
    member = FamilyMember(user_id=owner.id, name="陈旧风险家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73333337", member=member)
    _persist_snapshot(app, fetched_at=utcnow() - timedelta(minutes=31))

    response = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers={"Authorization": f"Bearer {token}"},
        json={"actions_done": ["check_weather"]},
    )

    assert response.status_code == 200
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.risk_level is None


@pytest.mark.parametrize(
    "actions_done",
    (
        ["行动"] * 21,
        ["行" * 51],
    ),
    ids=("too-many-items", "item-too-long"),
)
def test_action_confirm_rejects_elder_action_collection_boundaries(
    client,
    db_session,
    actions_done,
):
    """小程序适配层应在共享服务前拒绝过量或过长的自护行动。"""
    from core.db_models import DailyStatus, FamilyMember

    owner, owner_token = _user_and_token(db_session, "action_boundary_owner")
    member = FamilyMember(user_id=owner.id, name="行动边界家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73444444", member=member)

    response = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"actions_done": actions_done},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_actions_done"
    assert DailyStatus.query.filter_by(pair_id=pair.id).count() == 0


def test_shared_confirm_rejects_elder_action_count_drift(db_session):
    """直接调用共享层时，行动数量和自护列表也必须保持一致。"""
    from core.db_models import DailyStatus
    from core.time_utils import today_local
    from services.care_action_service import stage_confirm_action

    owner, _owner_token = _user_and_token(db_session, "action_count_drift_owner")
    pair = _pair(db_session, owner, "73445555")
    status = DailyStatus(
        pair_id=pair.id,
        status_date=today_local(),
        community_code=pair.community_code,
    )
    db_session.add(status)
    db_session.flush()

    with pytest.raises(ValueError, match="elder_action_count_mismatch"):
        stage_confirm_action(
            pair,
            status,
            actions_done_count=2,
            elder_actions=["drink_water"],
            source="miniprogram",
        )

    assert status.confirmed_at is None
    assert status.actions_done_count == 0
    assert status.elder_actions is None


def test_miniprogram_actions_share_events_and_community_projection(client, db_session):
    """三类小程序行动应写入同一事件口径并刷新社区投影。"""
    from core.db_models import CommunityDaily, DailyStatus, FamilyMember, Pair, UsageEvent
    from core.time_utils import ensure_utc_aware, today_local

    owner, owner_token = _user_and_token(db_session, "action_projection_owner")
    member = FamilyMember(user_id=owner.id, name="行动投影家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73555555", member=member)
    headers = {"Authorization": f"Bearer {owner_token}"}

    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers=headers,
        json={"actions_done": ["drink_water"]},
    ).status_code == 200
    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/help",
        headers=headers,
        json={"note": "请联系家人"},
    ).status_code == 200

    inactive_at = utcnow() - timedelta(days=3)
    persisted_pair = db_session.get(Pair, pair.id)
    persisted_pair.last_active_at = inactive_at
    db_session.commit()
    assert client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=headers,
        json={"question_2": "已经联系", "debrief_optin": False},
    ).status_code == 200

    db_session.expire_all()
    status = DailyStatus.query.filter_by(
        pair_id=pair.id,
        status_date=today_local(),
    ).one()
    aggregate = CommunityDaily.query.filter_by(
        community_code=pair.community_code,
        date=today_local(),
    ).one()
    events = UsageEvent.query.filter(
        UsageEvent.user_id == owner.id,
        UsageEvent.event_type.in_((
            "checkin_confirmed",
            "help_flagged",
            "feedback_submitted",
        )),
    ).order_by(UsageEvent.id.asc()).all()

    assert json.loads(status.elder_actions) == ["drink_water"]
    assert status.caregiver_note == "请联系家人"
    assert aggregate.total_people == 1
    assert aggregate.confirm_rate == 1
    assert [event.event_type for event in events] == [
        "checkin_confirmed",
        "help_flagged",
        "feedback_submitted",
    ]
    assert all(event.source == "miniprogram" for event in events)
    assert ensure_utc_aware(
        db_session.get(Pair, pair.id).last_active_at
    ) > ensure_utc_aware(inactive_at)


def test_miniprogram_action_succeeds_when_community_projection_fails(
    client,
    db_session,
    monkeypatch,
):
    """派生投影失败时，已提交的小程序主动作仍返回成功。"""
    from core.db_models import CommunityDaily, DailyStatus, FamilyMember
    from core.time_utils import today_local
    from services import community_daily_service

    owner, owner_token = _user_and_token(db_session, "action_projection_failure_owner")
    member = FamilyMember(user_id=owner.id, name="投影失败家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "73666666", member=member)

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(
        community_daily_service,
        "refresh_community_daily",
        fail_projection,
    )
    response = client.post(
        f"/mp/api/v1/actions/{pair.id}/confirm",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"actions_done": ["drink_water"]},
    )

    assert response.status_code == 200
    db_session.expire_all()
    status = DailyStatus.query.filter_by(
        pair_id=pair.id,
        status_date=today_local(),
    ).one()
    assert status.confirmed_at is not None
    assert CommunityDaily.query.filter_by(
        community_code=pair.community_code,
        date=today_local(),
    ).count() == 0


def test_action_debrief_respects_pair_optin_on_create_and_update(client, db_session):
    from core.db_models import DailyStatus, Debrief, FamilyMember, Pair
    from core.time_utils import ensure_utc_aware, today_local

    owner, owner_token = _user_and_token(db_session, "debrief_optin_owner")
    outsider, outsider_token = _user_and_token(db_session, "debrief_optin_outsider")
    member = FamilyMember(user_id=owner.id, name="复盘记录家人", relation="家人")
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "74444444", member=member)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    outsider_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=outsider_headers,
        json={"question_2": "越权内容", "debrief_optin": False},
    )
    assert outsider_response.status_code == 404
    assert Debrief.query.count() == 0
    assert DailyStatus.query.count() == 0

    linked_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=owner_headers,
        json={"question_2": "第一次复盘", "debrief_optin": True},
    )
    assert linked_response.status_code == 200
    linked_id = linked_response.get_json()["data"]["debrief_id"]
    linked = db_session.get(Debrief, linked_id)
    assert linked.owner_user_id == owner.id
    assert linked.origin_pair_id == pair.id
    assert linked.pair_id == pair.id
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.debrief_optin is True

    inactive_at = utcnow() - timedelta(days=3)
    persisted_pair = db_session.get(Pair, pair.id)
    persisted_pair.last_active_at = inactive_at
    db_session.commit()
    unlinked_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=owner_headers,
        json={"question_2": "更新后不关联", "debrief_optin": False},
    )
    assert unlinked_response.status_code == 200
    assert unlinked_response.get_json()["data"]["debrief_id"] == linked_id
    assert unlinked_response.get_json()["data"]["pair_id"] is None
    db_session.expire_all()
    unlinked = db_session.get(Debrief, linked_id)
    assert unlinked.owner_user_id == owner.id
    assert unlinked.origin_pair_id == pair.id
    assert unlinked.pair_id is None
    assert unlinked.question_2 == "更新后不关联"
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.debrief_optin is False
    assert ensure_utc_aware(
        db_session.get(Pair, pair.id).last_active_at
    ) > ensure_utc_aware(inactive_at)

    relinked_response = client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=owner_headers,
        json={"question_2": "再次选择关联", "debrief_optin": True},
    )
    assert relinked_response.status_code == 200
    relinked_id = relinked_response.get_json()["data"]["debrief_id"]
    assert relinked_id == linked_id
    relinked = db_session.get(Debrief, relinked_id)
    assert relinked.origin_pair_id == pair.id
    assert relinked.pair_id == pair.id
    assert relinked.question_2 == "再次选择关联"
    assert Debrief.query.filter_by(owner_user_id=owner.id, date=today_local()).count() == 1

    second_member = FamilyMember(user_id=owner.id, name="另一位家人", relation="家人")
    db_session.add(second_member)
    db_session.commit()
    second_pair = _pair(db_session, owner, "75555555", member=second_member)
    second_response = client.post(
        f"/mp/api/v1/actions/{second_pair.id}/debrief",
        headers=owner_headers,
        json={"question_2": "第二位家人不关联", "debrief_optin": False},
    )
    assert second_response.status_code == 200
    second_id = second_response.get_json()["data"]["debrief_id"]
    assert second_id != relinked_id
    second_record = db_session.get(Debrief, second_id)
    assert second_record.origin_pair_id == second_pair.id
    assert second_record.pair_id is None
    assert db_session.get(Debrief, relinked_id).pair_id == pair.id

    repeated_second = client.post(
        f"/mp/api/v1/actions/{second_pair.id}/debrief",
        headers=owner_headers,
        json={"question_2": "第二位家人重复不关联", "debrief_optin": False},
    )
    assert repeated_second.status_code == 200
    assert repeated_second.get_json()["data"]["debrief_id"] == second_id

    relinked_second = client.post(
        f"/mp/api/v1/actions/{second_pair.id}/debrief",
        headers=owner_headers,
        json={"question_2": "第二位家人再次关联", "debrief_optin": True},
    )
    assert relinked_second.status_code == 200
    assert relinked_second.get_json()["data"]["debrief_id"] == second_id
    db_session.expire_all()
    first_record = db_session.get(Debrief, relinked_id)
    second_record = db_session.get(Debrief, second_id)
    assert first_record.origin_pair_id == pair.id
    assert first_record.pair_id == pair.id
    assert first_record.question_2 == "再次选择关联"
    assert second_record.origin_pair_id == second_pair.id
    assert second_record.pair_id == second_pair.id
    assert second_record.question_2 == "第二位家人再次关联"
    assert Debrief.query.filter_by(owner_user_id=owner.id, date=today_local()).count() == 2

    difficulty_at_limit = client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=owner_headers,
        json={"difficulty": "难" * 500, "debrief_optin": True},
    )
    assert difficulty_at_limit.status_code == 200
    db_session.expire_all()
    assert db_session.get(Debrief, relinked_id).difficulty == "难" * 500

    difficulty_over_limit = client.post(
        f"/mp/api/v1/actions/{pair.id}/debrief",
        headers=owner_headers,
        json={"difficulty": "难" * 501, "debrief_optin": True},
    )
    assert difficulty_over_limit.status_code == 400
    assert difficulty_over_limit.get_json()["error"] == "difficulty_too_long"


def test_pair_hard_delete_keeps_owned_debrief_until_account_delete(
    app,
    client,
    db_session,
    monkeypatch,
):
    """删除来源家人只清空两种 pair 关联，账号注销才删除复盘。"""
    from sqlalchemy import inspect

    from core.db_models import Debrief, User
    from core.extensions import db
    from core.time_utils import today_local

    login = _wechat_login(app, client, monkeypatch, openid="debrief-pair-delete-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    owner = db_session.get(User, user_id)
    pair = _pair(db_session, owner, "74555555")
    pair_id = int(pair.id)
    record = Debrief(
        owner_user_id=user_id,
        origin_pair_id=pair_id,
        pair_id=pair_id,
        date=today_local(),
        community_code="都昌县",
        question_2="删除家人后仍由账号持有",
        created_at=utcnow(),
    )
    db_session.add(record)
    db_session.commit()
    debrief_id = int(record.id)

    foreign_keys = inspect(db.engine).get_foreign_keys("debriefs")
    for column_name in ("origin_pair_id", "pair_id"):
        foreign_key = next(
            item
            for item in foreign_keys
            if item.get("constrained_columns") == [column_name]
        )
        assert foreign_key.get("options", {}).get("ondelete") == "SET NULL"

    db_session.delete(pair)
    db_session.commit()
    db_session.expire_all()
    retained = db_session.get(Debrief, debrief_id)
    assert retained is not None
    assert retained.owner_user_id == user_id
    assert retained.origin_pair_id is None
    assert retained.pair_id is None
    assert retained.question_2 == "删除家人后仍由账号持有"

    deleted = client.delete(
        "/mp/api/v1/me",
        headers=headers,
        json={"confirm": True, "user_id": user_id},
    )
    assert deleted.status_code == 200
    db_session.expire_all()
    assert db_session.get(Debrief, debrief_id) is None


def test_health_assessment_submit_latest_and_owner_scope(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember

    owner, token = _user_and_token(db_session, "assessment_owner")
    outsider, outsider_token = _user_and_token(db_session, "assessment_outsider")
    member = FamilyMember(
        user_id=owner.id,
        name="评估老人",
        relation="母亲",
        age=72,
        chronic_diseases='["高血压"]',
    )
    db_session.add(member)
    db_session.commit()
    pair = _pair(db_session, owner, "72222222", member=member)
    _persist_snapshot(app)
    monkeypatch.setattr(
        "services.health_risk_service.HealthRiskService.assess_personal_weather_health_risk",
        lambda _self, profile, weather, screening=None: {
            "risk_score": 62.5,
            "risk_level": "中风险",
            "disease_risks": {"heat": 0.6},
            "recommendations": ["减少高温时段外出"],
            "explain": {"reasons": ["高温"]},
            "risk_interval": {"low": 50, "high": 70},
            "model_version": "test-model",
            "rule_version": "test-rule",
        },
    )
    payload = {
        "pair_id": pair.id,
        "outdoor_exposure": "medium",
        "symptom_level": "none",
        "hydration": "normal",
        "medication_adherence": "good",
        "sleep_quality": "fair",
    }
    owner_headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/mp/api/v1/health/assessment", headers=owner_headers, json=payload)

    assert created.status_code == 201
    assessment = created.get_json()["data"]["assessment"]
    assert assessment["member_id"] == member.id
    assert assessment["risk_score"] == 62.5
    assert "weather_condition" not in assessment
    with app.app_context():
        from core.db_models import HealthRiskAssessment

        stored = db_session.get(HealthRiskAssessment, assessment["id"])
        assert len(stored.weather_condition) <= 100
        assert json.loads(stored.weather_condition)["t"] == 36.0
    latest = client.get(
        f"/mp/api/v1/health/assessment?pair_id={pair.id}", headers=owner_headers
    )
    assert latest.get_json()["data"]["latest"]["id"] == assessment["id"]
    assert client.post(
        "/mp/api/v1/health/assessment",
        headers={"Authorization": f"Bearer {outsider_token}"},
        json=payload,
    ).status_code == 404


def test_public_resources_are_aggregated_and_small_samples_suppressed(
    app,
    client,
    db_session,
):
    from core.db_models import Community, CommunityDaily, CoolingResource
    from core.time_utils import today_local

    db_session.add_all(
        [
            Community(
                name="测试社区",
                population=1000,
                elderly_ratio=0.25,
                vulnerability_index=45,
                risk_level="中",
            ),
            CommunityDaily(
                community_code="测试社区",
                date=today_local(),
                total_people=2,
                confirm_rate=0.5,
                escalation_rate=0.5,
            ),
            CoolingResource(
                community_code="测试社区",
                name="社区活动室",
                address_hint="社区内",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/mp/api/v1/public/community")
    assert response.status_code == 200
    data = response.get_json()["data"]
    summary = data["communities"][0]["latest_action_summary"]
    assert summary["sample_suppressed"] is True
    assert summary["total_people"] is None
    assert summary["confirm_rate"] is None
    assert data["cooling"][0]["name"] == "社区活动室"
    serialized = str(data).lower()
    assert "diagnosis" not in serialized
    assert "medical_history" not in serialized


def test_public_gis_metadata_uses_same_origin_relative_url(app, client, db_session):
    app.config["FEATURE_HEAT_EXPOSURE_GIS"] = True
    response = client.get("/mp/api/v1/public/gis-metadata")
    assert response.status_code == 200
    gis = response.get_json()["data"]
    assert gis["available"] is True
    assert gis["geojson_url"].startswith("/static/")
    assert "://" not in gis["geojson_url"]
    assert "?v=" in gis["geojson_url"]
    assert gis["size_bytes"] > 0
    assert gis["title"]
    assert app.config["RATE_LIMIT_MP_PUBLIC"] == "600 per minute"


def test_public_gis_metadata_url_changes_with_file_version(app, tmp_path):
    import json
    import os

    from services.miniprogram_service import public_gis_metadata_payload

    static_root = tmp_path / "static"
    geojson = static_root / "data" / "gis" / "duchang_heat_exposure_cells.geojson"
    geojson.parent.mkdir(parents=True)
    geojson.write_text(
        json.dumps({"type": "FeatureCollection", "metadata": {"title": "版本一"}}),
        encoding="utf-8",
    )
    first_ns = 1_800_000_000_000_000_000
    second_ns = first_ns + 1_000_000_000
    original_static_folder = app.static_folder
    app.static_folder = str(static_root)
    app.config["FEATURE_HEAT_EXPOSURE_GIS"] = True
    try:
        os.utime(geojson, ns=(first_ns, first_ns))
        with app.test_request_context():
            first_url = public_gis_metadata_payload()["geojson_url"]
        os.utime(geojson, ns=(second_ns, second_ns))
        with app.test_request_context():
            second_url = public_gis_metadata_payload()["geojson_url"]
    finally:
        app.static_folder = original_static_folder

    assert first_url.startswith("/static/")
    assert "://" not in first_url
    assert first_url != second_url
    assert f"v={first_ns}" in first_url
    assert f"v={second_ns}" in second_url


def test_account_delete_rejects_cross_user_and_anonymizes_owner_data(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import Debrief, HealthDiary, MiniProgramIdentity, MiniProgramSession, User
    from core.time_utils import today_local

    login = _wechat_login(app, client, monkeypatch, openid="delete-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    token = data["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    with app.app_context():
        db_session.add(
            HealthDiary(
                user_id=user_id,
                symptoms="待删除",
                severity="mild",
                created_at=utcnow(),
            )
        )
        db_session.add(
            Debrief(
                owner_user_id=user_id,
                pair_id=None,
                date=today_local(),
                community_code="都昌县",
                question_1="关闭家人关联后仍属于账号",
                created_at=utcnow(),
            )
        )
        db_session.commit()

    cross_user = client.delete(
        "/mp/api/v1/me",
        headers=headers,
        json={"confirm": True, "user_id": user_id + 999},
    )
    assert cross_user.status_code == 403

    assert client.delete("/mp/api/v1/me", headers=headers).status_code == 400
    assert client.delete(
        "/mp/api/v1/me", headers=headers, json={"confirm": False}
    ).status_code == 400

    deleted = client.delete(
        "/mp/api/v1/me",
        headers=headers,
        json={"confirm": True, "user_id": user_id},
    )
    assert deleted.status_code == 200
    assert client.get("/mp/api/v1/me", headers=headers).status_code == 401
    with app.app_context():
        user = db_session.get(User, user_id)
        assert user.username.startswith("deleted_mp_")
        assert user.email is None
        assert user.deleted_at is not None
        assert HealthDiary.query.filter_by(user_id=user_id).count() == 0
        assert Debrief.query.filter_by(owner_user_id=user_id).count() == 0
        assert MiniProgramIdentity.query.filter_by(user_id=user_id).count() == 0
        assert MiniProgramSession.query.filter_by(user_id=user_id).count() == 0


def test_account_delete_serializes_an_inflight_diary_write(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import HealthDiary
    from blueprints import mp_api

    login = _wechat_login(app, client, monkeypatch, openid="delete-race-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    writer_locked = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    original_resolver = mp_api._resolve_owned_member

    def blocked_resolver(*args, **kwargs):
        writer_locked.set()
        assert release_writer.wait(timeout=5)
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(mp_api, "_resolve_owned_member", blocked_resolver)

    def write_diary():
        with app.test_client() as thread_client:
            outcomes["write"] = thread_client.post(
                "/mp/api/v1/health/diary",
                headers=headers,
                json={"severity": "mild", "symptoms": "并发写入"},
            )

    def delete_account():
        with app.test_client() as thread_client:
            outcomes["delete"] = thread_client.delete(
                "/mp/api/v1/me",
                headers=headers,
                json={"confirm": True, "user_id": user_id},
            )

    writer = threading.Thread(target=write_diary)
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
    assert outcomes["write"].status_code == 201
    assert outcomes["delete"].status_code == 200
    with app.app_context():
        assert HealthDiary.query.filter_by(user_id=user_id).count() == 0


def test_account_delete_serializes_atomic_miniprogram_action_confirm(
    app,
    client,
    db_session,
    monkeypatch,
):
    """行动状态与匿名事件一次提交，注销等待事务后清除全部 owner 数据。"""
    from blueprints import mp_api
    from core.db_models import DailyStatus, UsageEvent, User
    from services import care_action_service

    login = _wechat_login(app, client, monkeypatch, openid="delete-action-race-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    owner = db_session.get(User, user_id)
    pair = _pair(db_session, owner, "76555555")
    pair_id = int(pair.id)
    writer_staged = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    captured_event = {}
    original_stage_event = care_action_service._stage_usage_event

    monkeypatch.setattr(
        mp_api,
        "get_bootstrap_payload",
        lambda: {"risk": {"level": "低风险"}},
    )
    monkeypatch.setattr(
        mp_api,
        "log_usage_event",
        lambda *_args, **_kwargs: pytest.fail("行动确认不得进行第二次事件提交"),
    )

    def blocked_stage_event(*args, **kwargs):
        event = original_stage_event(*args, **kwargs)
        captured_event.update(
            {
                "user_id": event.user_id,
                "pair_id": event.pair_id,
                "member_id": event.member_id,
                "meta": json.loads(event.meta_json),
            }
        )
        writer_staged.set()
        assert release_writer.wait(timeout=5)
        return event

    monkeypatch.setattr(
        care_action_service,
        "_stage_usage_event",
        blocked_stage_event,
    )

    def write_action_confirm():
        with app.test_client() as thread_client:
            outcomes["write"] = thread_client.post(
                f"/mp/api/v1/actions/{pair_id}/confirm",
                headers=headers,
                json={"actions_done": ["drink_water"]},
            )

    def delete_account():
        with app.test_client() as thread_client:
            outcomes["delete"] = thread_client.delete(
                "/mp/api/v1/me",
                headers=headers,
                json={"confirm": True, "user_id": user_id},
            )

    writer = threading.Thread(target=write_action_confirm)
    deleter = threading.Thread(target=delete_account)
    writer.start()
    assert writer_staged.wait(timeout=5)
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
    assert captured_event == {
        "user_id": user_id,
        "pair_id": None,
        "member_id": None,
        "meta": {"actions_done_count": 1},
    }
    with app.app_context():
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert UsageEvent.query.count() == 0


def test_account_delete_serializes_an_inflight_miniprogram_debrief_write(
    app,
    client,
    db_session,
    monkeypatch,
):
    """小程序复盘与注销共用 owner 锁，注销后不留解除关联的记录。"""
    from blueprints import mp_api
    from core.db_models import Debrief, User

    login = _wechat_login(app, client, monkeypatch, openid="delete-debrief-race-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    owner = db_session.get(User, user_id)
    pair = _pair(db_session, owner, "76666666")
    pair_id = int(pair.id)
    writer_locked = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    original_status_resolver = mp_api._daily_status_for_pair

    def blocked_status_resolver(*args, **kwargs):
        writer_locked.set()
        assert release_writer.wait(timeout=5)
        return original_status_resolver(*args, **kwargs)

    monkeypatch.setattr(mp_api, "_daily_status_for_pair", blocked_status_resolver)

    def write_debrief():
        with app.test_client() as thread_client:
            outcomes["write"] = thread_client.post(
                f"/mp/api/v1/actions/{pair_id}/debrief",
                headers=headers,
                json={
                    "question_2": "并发中的复盘",
                    "debrief_optin": False,
                },
            )

    def delete_account():
        with app.test_client() as thread_client:
            outcomes["delete"] = thread_client.delete(
                "/mp/api/v1/me",
                headers=headers,
                json={"confirm": True, "user_id": user_id},
            )

    writer = threading.Thread(target=write_debrief)
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
    assert outcomes["write"].get_json()["data"]["pair_id"] is None
    assert outcomes["delete"].status_code == 200
    with app.app_context():
        assert Debrief.query.filter_by(owner_user_id=user_id).count() == 0


@pytest.mark.parametrize(
    ("action_path", "short_code", "payload"),
    (
        ("help", "76777777", {"note": "并发求助"}),
        (
            "debrief",
            "76888888",
            {"question_2": "提交后并发复盘", "debrief_optin": False},
        ),
    ),
)
def test_miniprogram_action_response_survives_post_commit_account_delete(
    app,
    client,
    db_session,
    monkeypatch,
    action_path,
    short_code,
    payload,
):
    """注销在写入提交后立即清理时，响应只读取提交前冻结的标量。"""
    from blueprints import mp_api
    from core.db_models import DailyStatus, Debrief, Pair, User

    login = _wechat_login(
        app,
        client,
        monkeypatch,
        openid=f"post-commit-{action_path}-delete-openid",
    )
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    owner = db_session.get(User, user_id)
    pair = _pair(db_session, owner, short_code)
    pair_id = int(pair.id)

    writer_locked = threading.Event()
    release_writer = threading.Event()
    writer_committed = threading.Event()
    delete_finished = threading.Event()
    outcomes = {}
    original_status_resolver = mp_api._daily_status_for_pair
    original_commit = mp_api.db.session.commit
    writer_name = f"miniprogram-{action_path}-writer"

    def blocked_status_resolver(*args, **kwargs):
        writer_locked.set()
        assert release_writer.wait(timeout=5)
        return original_status_resolver(*args, **kwargs)

    def coordinated_commit():
        original_commit()
        if threading.current_thread().name == writer_name:
            writer_committed.set()
            assert delete_finished.wait(timeout=5)

    monkeypatch.setattr(mp_api, "_daily_status_for_pair", blocked_status_resolver)
    monkeypatch.setattr(mp_api.db.session, "commit", coordinated_commit)

    def write_action():
        with app.test_client() as thread_client:
            outcomes["write"] = thread_client.post(
                f"/mp/api/v1/actions/{pair_id}/{action_path}",
                headers=headers,
                json=payload,
            )

    def delete_account():
        try:
            with app.test_client() as thread_client:
                outcomes["delete"] = thread_client.delete(
                    "/mp/api/v1/me",
                    headers=headers,
                    json={"confirm": True, "user_id": user_id},
                )
        finally:
            delete_finished.set()

    writer = threading.Thread(target=write_action, name=writer_name)
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
    assert writer_committed.is_set()
    assert outcomes["write"].status_code == 200
    assert outcomes["delete"].status_code == 200
    response_data = outcomes["write"].get_json()["data"]
    if action_path == "help":
        assert response_data == {
            "pair_id": pair_id,
            "help_flag": True,
            "relay_stage": "caregiver",
        }
    else:
        assert isinstance(response_data["debrief_id"], int)
        assert response_data["pair_id"] is None
    with app.app_context():
        assert Pair.query.filter_by(caregiver_id=user_id).count() == 0
        assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
        assert Debrief.query.filter_by(owner_user_id=user_id).count() == 0


def test_web_debrief_persists_owner_and_clamps_usage_metadata(
    app,
    client,
    db_session,
    monkeypatch,
):
    """Web 解除家人关联时仍保留 owner，分析长度遵循 300 上限。"""
    from core.db_models import Debrief, PairActionToken, UsageEvent, User
    from core.security import hash_pair_token
    from services import public_service

    login = _wechat_login(app, client, monkeypatch, openid="web-debrief-owner-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    owner = db_session.get(User, user_id)
    pair = _pair(db_session, owner, "78888888")
    pair_id = int(pair.id)
    pair_short_code = str(pair.short_code)
    action_token = "web-debrief-owner-token"
    db_session.add(
        PairActionToken(
            pair_id=pair_id,
            token_hash=hash_pair_token(action_token),
            expires_at=utcnow() + timedelta(days=1),
            created_at=utcnow(),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        public_service,
        "get_weather_with_cache",
        lambda _location: ({"is_mock": True, "data_source": "Demo"}, False),
    )
    with client.session_transaction() as session_record:
        session_record["_csrf_token"] = "web-debrief-owner-csrf"

    response = client.post(
        f"/e/{action_token}/debrief",
        data={
            "short_code": pair_short_code,
            "question_2": "Web owner 边界",
            "difficulty": "难" * 500,
            "csrf_token": "web-debrief-owner-csrf",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    db_session.expire_all()
    debrief = Debrief.query.one()
    event = UsageEvent.query.filter_by(
        user_id=user_id,
        event_type="feedback_submitted",
    ).one()
    assert debrief.owner_user_id == user_id
    assert debrief.pair_id is None
    assert debrief.difficulty == "难" * 500
    assert json.loads(event.meta_json) == {
        "difficulty_len": 300,
        "optin": False,
    }


def test_account_delete_serializes_an_inflight_web_debrief_write(
    app,
    client,
    db_session,
    monkeypatch,
):
    """Web token 复盘也必须在 owner 锁内一次提交所有账号写入。"""
    from core.db_models import Debrief, PairActionToken, UsageEvent, User
    from core.security import hash_pair_token
    from services import public_service

    login = _wechat_login(app, client, monkeypatch, openid="delete-web-debrief-race-openid")
    data = login.get_json()["data"]
    user_id = data["user"]["id"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    owner = db_session.get(User, user_id)
    pair = _pair(db_session, owner, "77777777")
    action_token = "web-debrief-race-token"
    db_session.add(
        PairActionToken(
            pair_id=pair.id,
            token_hash=hash_pair_token(action_token),
            expires_at=utcnow() + timedelta(days=1),
            created_at=utcnow(),
        )
    )
    db_session.commit()
    pair_short_code = str(pair.short_code)

    monkeypatch.setattr(
        public_service,
        "get_weather_with_cache",
        lambda _location: ({"is_mock": True, "data_source": "Demo"}, False),
    )
    writer_locked = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    original_owner_guard = public_service._active_pair_write_guard

    @contextmanager
    def blocked_owner_guard(*args, **kwargs):
        with original_owner_guard(*args, **kwargs) as locked_pair:
            writer_locked.set()
            assert release_writer.wait(timeout=5)
            yield locked_pair

    monkeypatch.setattr(
        public_service,
        "_active_pair_write_guard",
        blocked_owner_guard,
    )

    def write_debrief():
        with app.test_client() as thread_client:
            with thread_client.session_transaction() as session_record:
                session_record["_csrf_token"] = "web-debrief-race-csrf"
            outcomes["write"] = thread_client.post(
                f"/e/{action_token}/debrief",
                data={
                    "short_code": pair_short_code,
                    "question_2": "Web 并发复盘",
                    "csrf_token": "web-debrief-race-csrf",
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

    writer = threading.Thread(target=write_debrief)
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
        assert Debrief.query.filter_by(owner_user_id=user_id).count() == 0
        assert UsageEvent.query.filter_by(user_id=user_id).count() == 0


def test_session_owner_is_enforced_by_sqlite_foreign_keys(app, db_session):
    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError

    from core.db_models import MiniProgramIdentity, MiniProgramSession, User
    from core.extensions import db

    assert db.session.execute(db.text("PRAGMA foreign_keys")).scalar() == 1
    owner_fk = next(
        foreign_key
        for foreign_key in inspect(db.engine).get_foreign_keys("miniprogram_sessions")
        if foreign_key.get("constrained_columns") == ["identity_id", "user_id"]
    )
    assert owner_fk["referred_table"] == "miniprogram_identities"
    assert owner_fk["referred_columns"] == ["id", "user_id"]
    assert owner_fk.get("options", {}).get("ondelete") == "CASCADE"

    first = User(username="session-owner-first", role="user")
    second = User(username="session-owner-second", role="user")
    first.set_password("safe-test-password")
    second.set_password("safe-test-password")
    db_session.add_all([first, second])
    db_session.commit()
    identity = MiniProgramIdentity(
        user_id=first.id,
        openid_hash="owner-invariant-openid",
        privacy_consent_version="privacy-v1",
        privacy_consented_at=utcnow(),
    )
    db_session.add(identity)
    db_session.commit()
    first_id = first.id
    identity_id = identity.id

    db_session.add(
        MiniProgramSession(
            identity_id=identity_id,
            user_id=second.id,
            token_hash="owner-invariant-invalid-token",
            privacy_consent_version="privacy-v1",
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        MiniProgramSession(
            identity_id=identity_id,
            user_id=first_id,
            token_hash="owner-invariant-valid-token",
            privacy_consent_version="privacy-v1",
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db_session.commit()
    db_session.delete(db_session.get(User, first_id))
    db_session.commit()

    assert MiniProgramIdentity.query.filter_by(id=identity_id).count() == 0
    assert MiniProgramSession.query.filter_by(identity_id=identity_id).count() == 0


def test_admin_delete_removes_miniprogram_identity_and_invalidates_bearer(
    app,
    admin_client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramIdentity, MiniProgramSession, User

    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse("admin-delete-target"),
    )
    login = admin_client.post(
        "/mp/api/v1/auth/wechat",
        json={"code": "one-time-code", "privacy_consent_version": "privacy-v1"},
    )
    assert login.status_code == 200
    data = login.get_json()["data"]
    token = data["session_token"]
    user_id = data["user"]["id"]

    deleted = admin_client.post(
        f"/admin/user/{user_id}/delete",
        data={"csrf_token": "test-csrf-token"},
    )
    assert deleted.status_code == 302
    assert db_session.get(User, user_id) is None
    assert MiniProgramIdentity.query.filter_by(user_id=user_id).count() == 0
    assert MiniProgramSession.query.filter_by(user_id=user_id).count() == 0
    assert admin_client.get(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 401


def test_sync_cycle_calls_each_qweather_endpoint_at_most_once_and_enriches_forecast(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramSnapshot
    from core.time_utils import today_local
    from services.pipelines import sync_weather_cache as pipeline

    calls = {"current": [], "forecast": [], "nowcast": [], "warning": []}
    forecast_start = today_local()
    complete_forecast = [
        {
            **FORECAST[0],
            "date": (forecast_start + timedelta(days=index)).isoformat(),
            "forecast_date": (forecast_start + timedelta(days=index)).isoformat(),
        }
        for index in range(7)
    ]

    class WeatherStub:
        def get_current_weather(self, location, *, include_enrichment=True):
            calls["current"].append((location, include_enrichment))
            return {
                "temperature": 36,
                "temperature_max": None,
                "temperature_min": None,
                "humidity": 70,
                "data_source": "QWeather",
                "is_mock": False,
            }

        def get_qweather_daily_forecast(self, location, days=7):
            calls["forecast"].append((location, days))
            return {
                "success": True,
                "daily": complete_forecast,
                "meta": {"source": "QWeather"},
            }

        def get_short_term_nowcast(self, location, hours=24):
            calls["nowcast"].append((location, hours))
            return {
                "available": True,
                "source": "Open-Meteo",
                "timeline": [{
                    "time": "2026-07-18T08:00",
                    "precipitation_probability": 20.0,
                    "precipitation_mm": 0.0,
                    "temperature": 31.0,
                    "condition": "多云",
                    "risk_level": "低",
                }],
            }

    app.config.update(
        QWEATHER_AUTH_MODE="api_key",
        QWEATHER_KEY="test-only-key",
        QWEATHER_API_BASE="https://qweather.invalid/v7",
        QWEATHER_CANONICAL_LOCATION="116.20,29.27",
    )
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "WeatherService", WeatherStub)
    monkeypatch.setattr(
        "services.warning_service.get_qweather_warnings_result",
        lambda location: calls["warning"].append(location)
        or {"available": True, "status": "ok", "warnings": []},
    )

    result = pipeline.sync_weather_cache(locations=["都昌县", "九江"], update_daily=False)

    assert calls["current"] == [("都昌县", False)]
    assert calls["forecast"] == [("都昌县", 7)]
    assert calls["nowcast"] == [("都昌县", 24)]
    assert calls["warning"] == ["116.20,29.27"]
    assert result["locations"] == 1
    assert result["nowcast_updated"] == 1
    record = MiniProgramSnapshot.query.filter_by(snapshot_id=result["snapshot_id"]).one()
    payload = __import__("services.miniprogram_service", fromlist=["snapshot_payload"]).snapshot_payload(record)
    assert payload["current"]["temperature_max"] == 39.0
    assert payload["forecast"][0]["risk_available"] is True
    assert payload["forecast"][0]["risk_score"] is not None
    assert payload["risk"]["summary"]

    calls["nowcast"].clear()
    smoke_result = pipeline.sync_weather_cache(
        locations=["都昌县"],
        update_daily=False,
        include_nowcast=False,
    )
    assert calls["nowcast"] == []
    assert smoke_result["nowcast_updated"] == 0


def test_sync_with_qweather_empty_never_instantiates_fetcher_or_uses_network(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import ForecastCache, MiniProgramSnapshot, WeatherCache
    from services.miniprogram_service import snapshot_payload
    from services.pipelines import sync_weather_cache as pipeline

    app.config.update(QWEATHER_AUTH_MODE="disabled", QWEATHER_KEY="", QWEATHER_API_BASE="")
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(
        pipeline,
        "WeatherService",
        lambda: pytest.fail("QWeather 为空时不应创建联网 fetcher"),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *_args, **_kwargs: pytest.fail("测试禁止外网"),
    )
    old_fetched_at = utcnow() - timedelta(hours=2)
    db_session.add_all(
        [
            WeatherCache(
                location="都昌县",
                fetched_at=old_fetched_at,
                payload=json.dumps(CURRENT, ensure_ascii=False),
                is_mock=False,
            ),
            ForecastCache(
                location="qweather-only:都昌县",
                days=7,
                fetched_at=old_fetched_at,
                payload=json.dumps({"daily": FORECAST}, ensure_ascii=False),
                is_mock=False,
            ),
        ]
    )
    db_session.commit()

    result = pipeline.sync_weather_cache(update_daily=False)

    assert result["locations"] == 1
    assert result["snapshot_id"]
    record = MiniProgramSnapshot.query.filter_by(snapshot_id=result["snapshot_id"]).one()
    payload = snapshot_payload(record)
    assert payload["stale"] is True
    assert record.fetched_at == old_fetched_at.replace(tzinfo=None)
    cached = WeatherCache.query.filter_by(location="都昌县").one()
    assert cached.fetched_at == old_fetched_at.replace(tzinfo=None)


def test_failed_current_cycle_does_not_reuse_fresh_snapshot_as_success(
    app,
    db_session,
    monkeypatch,
):
    from services.miniprogram_service import persist_snapshot
    from services.pipelines import sync_weather_cache as pipeline

    with app.app_context():
        existing = persist_snapshot(CURRENT, FORECAST, [], fetched_at=utcnow())
        existing_snapshot_id = existing.snapshot_id

    class FailingWeather:
        def get_current_weather(self, *_args, **_kwargs):
            raise RuntimeError("test current failure")

    app.config.update(
        QWEATHER_AUTH_MODE="api_key",
        QWEATHER_KEY="test-only-key",
        QWEATHER_API_BASE="https://qweather.invalid/v7",
    )
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "WeatherService", FailingWeather)
    monkeypatch.setattr(
        pipeline,
        "refresh_snapshot_from_cycle",
        lambda *_args, **_kwargs: __import__(
            "services.miniprogram_service",
            fromlist=["latest_snapshot_record"],
        ).latest_snapshot_record(),
    )

    result = pipeline.sync_weather_cache(update_daily=False)

    assert result["updated"] == 0
    assert result["snapshot_id"] == existing_snapshot_id
    assert result["snapshot_stale"] is False
    assert result["snapshot_ready"] is False


def test_mock_fallback_does_not_trigger_dispatch_success(
    app,
    db_session,
    monkeypatch,
):
    from services.miniprogram_service import persist_snapshot
    from services.pipelines import sync_weather_cache as pipeline

    with app.app_context():
        existing = persist_snapshot(CURRENT, FORECAST, [], fetched_at=utcnow())
        existing_snapshot_id = existing.snapshot_id

    class MockFallbackWeather:
        def get_current_weather(self, *_args, **_kwargs):
            return {
                "temperature": 36,
                "humidity": 60,
                "is_mock": True,
                "data_source": "Mock",
            }

    app.config.update(
        QWEATHER_AUTH_MODE="api_key",
        QWEATHER_KEY="test-only-key",
        QWEATHER_API_BASE="https://qweather.invalid/v7",
    )
    monkeypatch.setattr(pipeline, "app", app)
    monkeypatch.setattr(pipeline, "WeatherService", MockFallbackWeather)
    monkeypatch.setattr(
        pipeline,
        "refresh_snapshot_from_cycle",
        lambda *_args, **_kwargs: __import__(
            "services.miniprogram_service",
            fromlist=["latest_snapshot_record"],
        ).latest_snapshot_record(),
    )

    result = pipeline.sync_weather_cache(update_daily=False)

    assert result["updated"] == 0
    assert result["snapshot_id"] == existing_snapshot_id
    assert result["snapshot_ready"] is False


def test_snapshot_retention_keeps_current_and_prunes_old_rows(app, db_session):
    from core.db_models import MiniProgramSnapshot
    from services.miniprogram_service import persist_snapshot

    app.config["MINIPROGRAM_SNAPSHOT_RETENTION"] = 3
    start = utcnow() - timedelta(hours=3)
    snapshot_ids = []
    with app.app_context():
        for index in range(5):
            record = persist_snapshot(
                CURRENT,
                FORECAST,
                [],
                fetched_at=start + timedelta(minutes=30 * index),
            )
            snapshot_ids.append(record.snapshot_id)
        remaining = MiniProgramSnapshot.query.order_by(MiniProgramSnapshot.fetched_at.asc()).all()

    assert len(remaining) == 3
    assert remaining[-1].snapshot_id == snapshot_ids[-1]
    assert snapshot_ids[0] not in {item.snapshot_id for item in remaining}


def test_snapshot_retention_stays_bounded_for_out_of_order_backfill(app, db_session):
    from core.db_models import MiniProgramSnapshot
    from services.miniprogram_service import persist_snapshot

    app.config["MINIPROGRAM_SNAPSHOT_RETENTION"] = 3
    now = utcnow()
    with app.app_context():
        for index in range(3):
            persist_snapshot(CURRENT, FORECAST, [], fetched_at=now + timedelta(minutes=index))
        returned = persist_snapshot(CURRENT, FORECAST, [], fetched_at=now - timedelta(days=2))
        remaining = MiniProgramSnapshot.query.all()

    assert len(remaining) == 3
    assert returned.id in {item.id for item in remaining}


def test_postgresql_snapshot_retention_uses_transaction_lock():
    from services.miniprogram_service import _acquire_snapshot_retention_lock

    calls = []

    def capture(statement, params):
        calls.append((str(statement), params))

    assert _acquire_snapshot_retention_lock(
        dialect_name="sqlite",
        execute=capture,
    ) is False
    assert calls == []

    assert _acquire_snapshot_retention_lock(
        dialect_name="postgresql",
        execute=capture,
    ) is True
    assert len(calls) == 1
    assert "pg_advisory_xact_lock" in calls[0][0]
    assert calls[0][1]["lock_id"] > 0


def test_weather_sync_persists_nowcast_for_read_only_web_route(app, db_session):
    from core.db_models import ForecastCache
    from services.pipelines import sync_weather_cache as pipeline

    fetched_at = utcnow()
    nowcast = {
        "available": True,
        "source": "Open-Meteo",
        "timeline": [
            {
                "time": "2026-07-18T08:00",
                "precipitation_probability": 20.0,
                "precipitation_mm": 0.0,
                "temperature": 31.0,
                "condition": "多云",
                "risk_level": "低",
            }
        ],
    }

    with app.app_context():
        assert pipeline._upsert_nowcast(nowcast, fetched_at) is True
        db_session.commit()
        record = ForecastCache.query.filter_by(
            location="nowcast:都昌县",
            days=24,
        ).one()

    assert record.is_mock is False
    assert record.fetched_at == fetched_at.replace(tzinfo=None)
    assert json.loads(record.payload) == nowcast


def test_weather_sync_rejects_untrusted_or_malformed_nowcast(app, db_session):
    from core.db_models import ForecastCache
    from services.pipelines import sync_weather_cache as pipeline

    valid_entry = {
        "time": "2026-07-18T08:00",
        "precipitation_probability": 20.0,
        "precipitation_mm": 0.0,
        "temperature": 31.0,
        "condition": "多云",
        "risk_level": "低",
    }
    invalid_payloads = [
        {
            "available": True,
            "source": "unknown",
            "timeline": [dict(valid_entry)],
        },
        {
            "available": True,
            "source": "Open-Meteo",
            "is_mock": True,
            "timeline": [dict(valid_entry)],
        },
        {
            "available": True,
            "source": "Open-Meteo",
            "timeline": [{**valid_entry, "time": "invalid"}],
        },
        {
            "available": True,
            "source": "Open-Meteo",
            "timeline": [{**valid_entry, "precipitation_probability": 120.0}],
        },
        {
            "available": True,
            "source": "Open-Meteo",
            "timeline": [{**valid_entry, "risk_level": "高"}],
        },
    ]

    with app.app_context():
        for payload in invalid_payloads:
            assert pipeline._upsert_nowcast(payload, utcnow()) is False
        assert ForecastCache.query.filter_by(
            location="nowcast:都昌县",
            days=24,
        ).count() == 0


def test_wechat_login_limits_active_sessions_per_identity(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramSession

    _configure_wechat(app)
    app.config["WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS"] = 5
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse("multi-device-openid"),
    )
    tokens = []
    for index in range(6):
        response = client.post(
            "/mp/api/v1/auth/wechat",
            json={"code": f"code-{index}", "privacy_consent_version": "privacy-v1"},
            environ_overrides={"REMOTE_ADDR": f"203.0.113.{index + 1}"},
        )
        assert response.status_code == 200
        tokens.append(response.get_json()["data"]["session_token"])

    with app.app_context():
        active = MiniProgramSession.query.filter(MiniProgramSession.revoked_at.is_(None)).count()
        assert active == 5
    assert client.get(
        "/mp/api/v1/me", headers={"Authorization": f"Bearer {tokens[0]}"}
    ).status_code == 401
    assert client.get(
        "/mp/api/v1/me", headers={"Authorization": f"Bearer {tokens[-1]}"}
    ).status_code == 200


def test_concurrent_wechat_login_keeps_one_identity_and_session_cap(
    app,
    db_session,
    monkeypatch,
):
    """同一 OpenID 并发首次登录只能建一份身份，活跃会话不得突破上限。"""
    from core.db_models import MiniProgramIdentity, MiniProgramSession, User

    _configure_wechat(app)
    app.config["WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS"] = 2
    barrier = threading.Barrier(4)

    def exchange_once(*_args, **_kwargs):
        barrier.wait(timeout=5)
        return _WechatResponse("concurrent-first-login-openid")

    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        exchange_once,
    )
    responses = []
    response_lock = threading.Lock()

    def login(index):
        with app.test_client() as thread_client:
            response = thread_client.post(
                "/mp/api/v1/auth/wechat",
                json={
                    "code": f"concurrent-code-{index}",
                    "privacy_consent_version": "privacy-v1",
                },
                environ_overrides={"REMOTE_ADDR": f"198.51.100.{index + 1}"},
            )
            with response_lock:
                responses.append(response)

    threads = [threading.Thread(target=login, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)

    assert all(not thread.is_alive() for thread in threads)
    assert len(responses) == 4
    assert all(response.status_code == 200 for response in responses)
    with app.app_context():
        assert MiniProgramIdentity.query.count() == 1
        identity = MiniProgramIdentity.query.one()
        assert User.query.filter_by(id=identity.user_id).count() == 1
        assert MiniProgramSession.query.filter(
            MiniProgramSession.identity_id == identity.id,
            MiniProgramSession.revoked_at.is_(None),
        ).count() == 2
