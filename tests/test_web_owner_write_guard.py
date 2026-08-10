# -*- coding: utf-8 -*-
"""Web 私密写入与账号注销的串行化回归测试。"""

from contextlib import contextmanager
import threading
import time

import pytest

from core.db_models import (
    ApiToken,
    DailyStatus,
    FamilyMember,
    FamilyMemberProfile,
    HealthDiary,
    HealthRiskAssessment,
    MedicationReminder,
    Pair,
    UsageEvent,
    User,
)
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import today_local, utcnow
from services.push.locks import push_owner_lock


def _login_session(client, user_id, csrf_token="web-owner-write-csrf"):
    with client.session_transaction() as session:
        session["_user_id"] = f"{user_id}:1"
        session["_fresh"] = True
        session["_csrf_token"] = csrf_token


def _new_owner(db_session, username, *, role="user"):
    owner = User(username=username, role=role)
    owner.set_password("web-owner-write-test-password")
    db_session.add(owner)
    db_session.commit()
    return owner


@pytest.mark.parametrize(
    ("guard_module", "path", "payload"),
    (
        (
            "health",
            "/family-members",
            {"name": "并发家人甲", "gender": "男"},
        ),
        (
            "health",
            "/family-members/new",
            {"name": "并发家人乙", "gender": "女"},
        ),
        (
            "health",
            "/health-diary",
            {"severity": "mild", "symptoms": "并发日记"},
        ),
        (
            "health",
            "/medication-reminders",
            {"medicine_name": "并发用药", "frequency": "daily"},
        ),
        (
            "caregiver",
            "/pairs",
            {"location_query": "都昌县"},
        ),
        (
            "caregiver",
            "/caregiver/pair/create",
            {"location_query": "都昌县"},
        ),
    ),
)
def test_account_delete_finishes_before_web_write_and_blocks_all_private_rows(
    app,
    db_session,
    monkeypatch,
    guard_module,
    path,
    payload,
):
    """请求已认证但尚未取 owner 锁时，先完成注销必须让写入失败关闭。"""
    from blueprints import health as health_module
    from services.user import caregiver_service
    from services.user.owner_write_guard import owner_write_guard as real_guard

    owner = _new_owner(
        db_session,
        f"delete_first_{guard_module}_{path.rsplit('/', 1)[-1] or 'root'}",
    )
    owner_id = int(owner.id)
    writer_ready = threading.Event()
    release_writer = threading.Event()
    outcomes = {}

    @contextmanager
    def delayed_guard(user_id):
        # 模拟路由已完成表单解析，并先释放认证产生的读事务。
        db.session.rollback()
        writer_ready.set()
        assert release_writer.wait(timeout=5)
        with real_guard(user_id) as locked_owner:
            yield locked_owner

    target_module = health_module if guard_module == "health" else caregiver_service
    monkeypatch.setattr(target_module, "owner_write_guard", delayed_guard)

    def write_private_row():
        with app.test_client() as thread_client:
            _login_session(thread_client, owner_id)
            outcomes["write"] = thread_client.post(
                path,
                data={**payload, "csrf_token": "web-owner-write-csrf"},
                follow_redirects=False,
            )

    writer = threading.Thread(target=write_private_row)
    writer.start()
    assert writer_ready.wait(timeout=5)

    with app.app_context():
        with push_owner_lock(owner_id):
            locked_owner = db.session.get(User, owner_id)
            locked_owner.deleted_at = utcnow()
            db.session.commit()

    release_writer.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert outcomes["write"].status_code in (301, 302)
    assert "/login" in (outcomes["write"].headers.get("Location") or "")
    db_session.expire_all()
    assert FamilyMember.query.filter_by(user_id=owner_id).count() == 0
    assert FamilyMemberProfile.query.count() == 0
    assert HealthDiary.query.filter_by(user_id=owner_id).count() == 0
    assert MedicationReminder.query.filter_by(user_id=owner_id).count() == 0
    assert Pair.query.filter_by(caregiver_id=owner_id).count() == 0
    assert UsageEvent.query.filter_by(user_id=owner_id).count() == 0


@pytest.mark.parametrize(
    ("path", "payload"),
    (
        (
            "/health-assessment",
            {
                "outdoor_exposure": "low",
                "symptom_level": "none",
                "hydration": "good",
                "medication_adherence": "good",
                "sleep_quality": "good",
            },
        ),
        (
            "/profile",
            {
                "form_id": "api_token",
                "token_name": "删除竞态令牌",
                "miniprogram_privacy_consent": "1",
            },
        ),
        (
            "/profile",
            {
                "form_id": "password",
                "old_password": "web-owner-write-test-password",
                "new_password": "NewConcurrentPassword!",
            },
        ),
        ("/location", {"location": "都昌县"}),
    ),
    ids=("assessment", "api-token", "password", "location"),
)
def test_profile_delete_first_blocks_all_profile_private_writes(
    app,
    db_session,
    monkeypatch,
    path,
    payload,
):
    """资料路由完成计算后若注销先提交，不得重建评估、凭证或位置。"""
    from services.health_risk_service import HealthRiskService
    from services.user import profile_service
    from services.user.owner_write_guard import owner_write_guard as real_guard

    owner = _new_owner(db_session, f"profile-delete-first-{path.rsplit('/', 1)[-1]}")
    owner_id = int(owner.id)
    original_password_hash = owner.password_hash
    app.config["FEATURE_NOTIFICATIONS"] = False
    monkeypatch.setattr(profile_service, "ensure_user_location_valid", lambda: "都昌县")
    monkeypatch.setattr(
        profile_service,
        "get_weather_with_cache",
        lambda _location: (
            {
                "temperature": 31.0,
                "temperature_max": 35.0,
                "temperature_min": 25.0,
                "humidity": 65.0,
                "weather_condition": "晴",
                "data_source": "QWeather",
                "is_mock": False,
            },
            False,
        ),
    )
    monkeypatch.setattr(
        HealthRiskService,
        "assess_personal_weather_health_risk",
        lambda *_args, **_kwargs: {
            "risk_score": 20.0,
            "risk_level": "低风险",
            "recommendations": ["补水"],
            "disease_risks": {},
        },
    )
    writer_ready = threading.Event()
    release_writer = threading.Event()
    outcome = {}

    @contextmanager
    def delayed_guard(user_id):
        db.session.rollback()
        writer_ready.set()
        assert release_writer.wait(timeout=5)
        with real_guard(user_id) as locked_owner:
            yield locked_owner

    monkeypatch.setattr(profile_service, "owner_write_guard", delayed_guard)

    def write_profile_data():
        with app.test_client() as thread_client:
            _login_session(thread_client, owner_id)
            outcome["response"] = thread_client.post(
                path,
                data={**payload, "csrf_token": "web-owner-write-csrf"},
                follow_redirects=False,
            )

    writer = threading.Thread(target=write_profile_data)
    writer.start()
    assert writer_ready.wait(timeout=5)
    with app.app_context():
        with push_owner_lock(owner_id):
            locked_owner = db.session.get(User, owner_id)
            locked_owner.deleted_at = utcnow()
            db.session.commit()
    release_writer.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert outcome["response"].status_code in (301, 302)
    db_session.expire_all()
    deleted_owner = db_session.get(User, owner_id)
    assert deleted_owner.community is None
    assert deleted_owner.password_hash == original_password_hash
    assert HealthRiskAssessment.query.filter_by(user_id=owner_id).count() == 0
    assert ApiToken.query.filter_by(user_id=owner_id).count() == 0


def test_web_write_finishes_first_then_account_delete_cleans_it(
    app,
    db_session,
    monkeypatch,
):
    """Web 写入先取得 owner 锁时，注销等待提交后再清理全部 owner 数据。"""
    from blueprints import mp_api
    from services.user import owner_write_guard as guard_module

    owner = _new_owner(db_session, "write_first_then_delete")
    owner_id = int(owner.id)
    writer_locked = threading.Event()
    release_writer = threading.Event()
    outcomes = {}
    original_lock = guard_module._lock_active_owner_for_write

    def delayed_owner_lock(user_id):
        locked_owner = original_lock(user_id)
        assert locked_owner is not None
        writer_locked.set()
        assert release_writer.wait(timeout=5)
        return locked_owner

    monkeypatch.setattr(
        guard_module,
        "_lock_active_owner_for_write",
        delayed_owner_lock,
    )

    def write_member():
        with app.test_client() as thread_client:
            _login_session(thread_client, owner_id)
            outcomes["write"] = thread_client.post(
                "/family-members",
                data={
                    "name": "先提交后清理",
                    "gender": "男",
                    "csrf_token": "web-owner-write-csrf",
                },
                follow_redirects=False,
            )

    def delete_account():
        with app.app_context():
            with push_owner_lock(owner_id):
                locked_owner = db.session.get(User, owner_id)
                mp_api._anonymize_miniprogram_owner(locked_owner)
                outcomes["delete"] = True

    writer = threading.Thread(target=write_member)
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
    assert outcomes["write"].status_code in (301, 302)
    assert outcomes["delete"] is True
    db_session.expire_all()
    assert db_session.get(User, owner_id).deleted_at is not None
    assert FamilyMember.query.filter_by(user_id=owner_id).count() == 0
    assert FamilyMemberProfile.query.count() == 0
    assert UsageEvent.query.filter_by(user_id=owner_id).count() == 0


def test_pair_create_redirect_uses_scalar_id_after_immediate_account_delete(
    app,
    db_session,
    monkeypatch,
):
    """创建提交后立即注销时，重定向不能再访问已删除的 Pair ORM 对象。"""
    from blueprints import mp_api
    from services.user import caregiver_service

    owner = _new_owner(db_session, "pair_scalar_after_delete")
    owner_id = int(owner.id)
    created = threading.Event()
    release_writer = threading.Event()
    outcome = {}
    real_create_pair = caregiver_service._create_pair

    def delayed_create_pair(*args, **kwargs):
        pair_id = real_create_pair(*args, **kwargs)
        assert isinstance(pair_id, int)
        created.set()
        assert release_writer.wait(timeout=5)
        return pair_id

    monkeypatch.setattr(caregiver_service, "_create_pair", delayed_create_pair)

    def create_pair_request():
        with app.test_client() as thread_client:
            _login_session(thread_client, owner_id)
            outcome["response"] = thread_client.post(
                "/pairs",
                data={
                    "location_query": "都昌县",
                    "csrf_token": "web-owner-write-csrf",
                },
                follow_redirects=False,
            )

    writer = threading.Thread(target=create_pair_request)
    writer.start()
    assert created.wait(timeout=5)

    with app.app_context():
        with push_owner_lock(owner_id):
            locked_owner = db.session.get(User, owner_id)
            mp_api._anonymize_miniprogram_owner(locked_owner)

    release_writer.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    response = outcome["response"]
    assert response.status_code in (301, 302)
    assert "created=" in (response.headers.get("Location") or "")
    db_session.expire_all()
    assert Pair.query.filter_by(caregiver_id=owner_id).count() == 0


def test_pair_creation_rechecks_member_owner_inside_guard(client, db_session):
    """绑定创建不能沿用锁前取得的跨账号 member_id。"""
    owner = _new_owner(db_session, "pair_member_owner")
    outsider = _new_owner(db_session, "pair_member_outsider")
    outsider_member = FamilyMember(user_id=outsider.id, name="其他账号家人")
    db_session.add(outsider_member)
    db_session.commit()
    _login_session(client, owner.id)

    response = client.post(
        "/pairs",
        data={
            "location_query": "都昌县",
            "member_id": outsider_member.id,
            "csrf_token": "web-owner-write-csrf",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302)
    pair = Pair.query.filter_by(caregiver_id=owner.id).one()
    assert pair.member_id is None


def test_caregiver_writes_accept_active_pair_and_reject_inactive_pair(
    client,
    db_session,
    monkeypatch,
):
    """行动记录和升级链只能改 active Pair，历史 inactive Pair 保持只读。"""
    from services.user import caregiver_service

    owner = _new_owner(db_session, "active_pair_web_writer", role="caregiver")
    pairs = []
    for index, status in enumerate(("active", "inactive"), start=1):
        short_code = f"8333333{index}"
        pair = Pair(
            caregiver_id=owner.id,
            community_code="都昌县",
            location_query="都昌县",
            elder_code=f"active-guard-{index}",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            status=status,
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.flush()
        db_session.add(DailyStatus(
            pair_id=pair.id,
            status_date=today_local(),
            community_code="都昌县",
            risk_level="低风险",
            relay_stage="none",
        ))
        pairs.append(pair)
    db_session.commit()
    active_pair, inactive_pair = pairs
    _login_session(client, owner.id)
    monkeypatch.setattr(
        caregiver_service,
        "_load_heat_risk",
        lambda *_args, **_kwargs: pytest.fail("已有状态时不应读取天气"),
    )
    monkeypatch.setattr(caregiver_service, "_refresh_community_daily", lambda *_args: None)

    inactive_action = client.post(
        f"/caregiver/pair/{inactive_pair.id}/action-log",
        data={
            "caregiver_actions": "remind",
            "caregiver_note": "不应写入",
            "csrf_token": "web-owner-write-csrf",
        },
        follow_redirects=False,
    )
    inactive_escalation = client.post(
        f"/pairs/{inactive_pair.id}/escalate",
        data={"csrf_token": "web-owner-write-csrf"},
        follow_redirects=False,
    )
    active_action = client.post(
        f"/caregiver/pair/{active_pair.id}/action-log",
        data={
            "caregiver_actions": "remind",
            "caregiver_note": "已联系",
            "csrf_token": "web-owner-write-csrf",
        },
        follow_redirects=False,
    )
    active_escalation = client.post(
        f"/pairs/{active_pair.id}/escalate",
        data={"csrf_token": "web-owner-write-csrf"},
        follow_redirects=False,
    )

    assert inactive_action.status_code == 404
    assert inactive_escalation.status_code == 404
    assert active_action.status_code in (301, 302)
    assert active_escalation.status_code in (301, 302)
    db_session.expire_all()
    inactive_status = DailyStatus.query.filter_by(pair_id=inactive_pair.id).one()
    assert inactive_status.caregiver_actions is None
    assert inactive_status.caregiver_note is None
    assert inactive_status.relay_stage == "none"
    active_status = DailyStatus.query.filter_by(pair_id=active_pair.id).one()
    assert active_status.caregiver_actions == '["remind"]'
    assert active_status.caregiver_note == "已联系"
    assert active_status.relay_stage == "caregiver"
