# -*- coding: utf-8 -*-
"""社区日度投影并发与事务边界回归测试。"""

from datetime import date
import threading

from core.db_models import CommunityDaily, DailyStatus, Pair, User
from core.security import hash_short_code
from core.time_utils import utcnow


COMMUNITY_CODE = "并发投影测试社区"
STATUS_DATE = date(2026, 7, 18)


def _seed_households(db_session, count=2):
    status_ids = []
    for index in range(count):
        user = User(username=f"projection-owner-{index}", role="caregiver")
        user.set_password("safe-test-password")
        db_session.add(user)
        db_session.flush()
        short_code = f"8300000{index}"
        pair = Pair(
            caregiver_id=user.id,
            community_code=COMMUNITY_CODE,
            elder_code=f"projection-elder-{index}",
            short_code=short_code,
            short_code_hash=hash_short_code(short_code),
            status="active",
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.flush()
        status = DailyStatus(
            pair_id=pair.id,
            status_date=STATUS_DATE,
            community_code=COMMUNITY_CODE,
            risk_level="低风险",
            help_flag=False,
            relay_stage="none",
        )
        db_session.add(status)
        db_session.flush()
        status_ids.append(status.id)
    db_session.commit()
    return status_ids


def test_same_key_refresh_serializes_stale_writer_and_keeps_both_actions(
    app,
    db_session,
    monkeypatch,
    tmp_path,
):
    """先读到旧快照的线程不得覆盖随后已提交动作的最终投影。"""
    from core.extensions import db
    from services import community_daily_service as service

    app.config["DISPATCH_LOCK_PATH"] = str(tmp_path / "dispatch.lock")
    first_status_id, second_status_id = _seed_households(db_session)
    first_metrics_ready = threading.Event()
    second_attempting_refresh = threading.Event()
    second_metrics_started = threading.Event()
    failures = []
    original_builder = service.build_community_household_metrics

    def controlled_builder(community_code, status_date, *, statuses=None):
        if threading.current_thread().name == "projection-first":
            first_metrics_ready.set()
            if not second_attempting_refresh.wait(timeout=5):
                raise AssertionError("第二个刷新线程未按时启动")
            if second_metrics_started.wait(timeout=0.15):
                raise AssertionError("同键刷新在首个事务结束前进入了聚合阶段")
            return {
                "total_people": 2,
                "confirmed_count": 1,
                "help_count": 0,
                "escalation_count": 0,
                "risk_distribution": {
                    "低风险": 2,
                    "中风险": 0,
                    "高风险": 0,
                    "极高": 0,
                },
                "confirmed_risk_distribution": {
                    "低风险": 1,
                    "中风险": 0,
                    "高风险": 0,
                    "极高": 0,
                },
            }
        second_metrics_started.set()
        return original_builder(
            community_code,
            status_date,
            statuses=statuses,
        )

    monkeypatch.setattr(service, "build_community_household_metrics", controlled_builder)

    def first_writer():
        try:
            with app.app_context():
                status = db.session.get(DailyStatus, first_status_id)
                status.confirmed_at = utcnow()
                db.session.commit()
                service.refresh_community_daily(COMMUNITY_CODE, STATUS_DATE)
        except Exception as exc:  # pragma: no cover - 断言在主线程统一展示
            failures.append(exc)
        finally:
            with app.app_context():
                db.session.remove()

    def second_writer():
        try:
            if not first_metrics_ready.wait(timeout=5):
                raise AssertionError("首个刷新线程未进入受控聚合阶段")
            with app.app_context():
                status = db.session.get(DailyStatus, second_status_id)
                status.confirmed_at = utcnow()
                db.session.commit()
                second_attempting_refresh.set()
                service.refresh_community_daily(COMMUNITY_CODE, STATUS_DATE)
        except Exception as exc:  # pragma: no cover - 断言在主线程统一展示
            failures.append(exc)
        finally:
            with app.app_context():
                db.session.remove()

    first = threading.Thread(target=first_writer, name="projection-first")
    second = threading.Thread(target=second_writer, name="projection-second")
    first.start()
    second.start()
    first.join(timeout=8)
    second.join(timeout=8)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert second_metrics_started.is_set()
    db_session.expire_all()
    record = CommunityDaily.query.filter_by(
        community_code=COMMUNITY_CODE,
        date=STATUS_DATE,
    ).one()
    assert record.total_people == 2
    assert record.confirm_rate == 1


def test_commit_false_keeps_projection_in_callers_transaction(
    app,
    db_session,
    tmp_path,
):
    """commit=False 只 flush，并把文件锁保持到调用者 rollback。"""
    from core.extensions import db
    from services import community_daily_service as service

    app.config["DISPATCH_LOCK_PATH"] = str(tmp_path / "dispatch.lock")
    _seed_households(db_session, count=1)

    service.refresh_community_daily(COMMUNITY_CODE, STATUS_DATE, commit=False)

    session = db.session()
    assert session.in_transaction()
    assert CommunityDaily.query.filter_by(
        community_code=COMMUNITY_CODE,
        date=STATUS_DATE,
    ).count() == 1
    assert session.info[service._SESSION_PROJECTION_LOCKS_KEY]

    db_session.rollback()

    assert service._SESSION_PROJECTION_LOCKS_KEY not in session.info
    assert CommunityDaily.query.filter_by(
        community_code=COMMUNITY_CODE,
        date=STATUS_DATE,
    ).count() == 0


def test_different_projection_keys_do_not_share_file_lock(app, tmp_path):
    """一个社区持锁时，另一个社区可立即进入自己的独立锁。"""
    from services.push.locks import community_projection_file_lock

    app.config["DISPATCH_LOCK_PATH"] = str(tmp_path / "dispatch.lock")
    first_holding = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def hold_first():
        with app.app_context():
            with community_projection_file_lock("社区甲", STATUS_DATE):
                first_holding.set()
                assert release_first.wait(timeout=5)

    def enter_second():
        assert first_holding.wait(timeout=5)
        with app.app_context():
            with community_projection_file_lock("社区乙", STATUS_DATE):
                second_entered.set()

    first = threading.Thread(target=hold_first)
    second = threading.Thread(target=enter_second)
    first.start()
    second.start()
    try:
        assert second_entered.wait(timeout=1)
    finally:
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()


def test_push_owner_lock_keeps_legacy_filename_during_rolling_release(app, tmp_path):
    """泛化锁实现仍与旧进程竞争同一个 owner 锁文件。"""
    from services.push.locks import push_owner_lock

    app.config["DISPATCH_LOCK_PATH"] = str(tmp_path / "dispatch.lock")
    with app.app_context():
        with push_owner_lock(17):
            legacy_path = tmp_path / "push-owner-locks" / "user-17.lock"
            assert legacy_path.is_file()


def test_postgresql_projection_lock_uses_keyed_transaction_advisory_lock():
    from services.community_daily_service import (
        _acquire_community_projection_db_lock,
        _community_projection_advisory_lock_id,
    )

    calls = []

    def capture(statement, params):
        calls.append((str(statement), params))

    assert _acquire_community_projection_db_lock(
        COMMUNITY_CODE,
        STATUS_DATE,
        dialect_name="sqlite",
        execute=capture,
    ) is False
    assert calls == []

    assert _acquire_community_projection_db_lock(
        COMMUNITY_CODE,
        STATUS_DATE,
        dialect_name="postgresql",
        execute=capture,
    ) is True
    assert "pg_advisory_xact_lock" in calls[0][0]
    assert calls[0][1]["lock_id"] == _community_projection_advisory_lock_id(
        COMMUNITY_CODE,
        STATUS_DATE,
    )
    assert calls[0][1]["lock_id"] != _community_projection_advisory_lock_id(
        "另一个社区",
        STATUS_DATE,
    )
