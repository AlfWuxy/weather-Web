# -*- coding: utf-8 -*-
"""公开短码查询的并发写入边界回归。"""

from contextlib import contextmanager
from datetime import timedelta
import threading
import time

from core.db_models import DailyStatus, Pair, User
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import today_local, utcnow


def test_two_initial_action_lookups_share_one_daily_status(
    app,
    db_session,
    monkeypatch,
):
    """同一家庭首次并发打开行动页时只创建一条记录，并保留较高风险。"""
    from services import public_service

    app.config["WECHAT_FORMAL_RUNTIME"] = False
    owner = User(username="lookup-race-owner", role="caregiver")
    owner.set_password("lookup-race-password")
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code="短码并发测试社区",
        location_query="都昌",
        elder_code="lookup-race-elder",
        short_code="86428642",
        short_code_hash=hash_short_code("86428642"),
        short_code_expires_at=utcnow() + timedelta(days=1),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    pair_id = int(pair.id)

    first_locked = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    failures = []
    responses = {}
    original_guard = public_service._active_pair_write_guard

    @contextmanager
    def controlled_guard(candidate):
        with original_guard(candidate) as locked_pair:
            if threading.current_thread().name == "lookup-high":
                first_locked.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("首个短码查询未按时释放")
            yield locked_pair

    def local_action_context(locked_pair, status_date):
        if threading.current_thread().name == "lookup-low":
            second_started.set()
            risk_level = "中风险"
        else:
            risk_level = "高风险"
        status = public_service._get_or_create_daily_status(
            locked_pair,
            status_date,
            risk_level,
        )
        return status, [], [], None, None, risk_level, []

    monkeypatch.setattr(public_service, "_active_pair_write_guard", controlled_guard)
    monkeypatch.setattr(public_service, "_build_action_context", local_action_context)

    def open_action_page(name):
        try:
            with app.test_client() as thread_client:
                csrf_token = f"{name}-csrf"
                with thread_client.session_transaction() as session_record:
                    session_record["_csrf_token"] = csrf_token
                responses[name] = thread_client.post(
                    "/action",
                    data={"short_code": "86428642", "csrf_token": csrf_token},
                    follow_redirects=False,
                )
        except Exception as exc:  # pragma: no cover - 主线程统一展示异常
            failures.append(exc)
        finally:
            with app.app_context():
                db.session.remove()

    high_writer = threading.Thread(
        target=open_action_page,
        args=("high",),
        name="lookup-high",
    )
    low_writer = threading.Thread(
        target=open_action_page,
        args=("low",),
        name="lookup-low",
    )
    high_writer.start()
    assert first_locked.wait(timeout=5)
    low_writer.start()
    time.sleep(0.2)
    assert low_writer.is_alive()
    assert not second_started.is_set()

    release_first.set()
    high_writer.join(timeout=6)
    low_writer.join(timeout=6)

    assert not high_writer.is_alive()
    assert not low_writer.is_alive()
    assert failures == []
    assert responses["high"].status_code == 200
    assert responses["low"].status_code == 200
    db_session.expire_all()
    statuses = DailyStatus.query.filter_by(
        pair_id=pair_id,
        status_date=today_local(),
    ).all()
    assert len(statuses) == 1
    assert statuses[0].risk_level == "高风险"


def test_action_lookup_fails_closed_when_pair_lock_is_unavailable(
    app,
    db_session,
    client,
    monkeypatch,
):
    """锁服务异常时不创建状态，并把用户带回安全入口。"""
    from services import public_service

    app.config["WECHAT_FORMAL_RUNTIME"] = False
    owner = User(username="lookup-lock-owner", role="caregiver")
    owner.set_password("lookup-lock-password")
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code="短码锁失败测试社区",
        location_query="都昌",
        elder_code="lookup-lock-elder",
        short_code="97539753",
        short_code_hash=hash_short_code("97539753"),
        short_code_expires_at=utcnow() + timedelta(days=1),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    pair_id = int(pair.id)

    @contextmanager
    def unavailable_guard(_pair):
        raise OSError("test lock failure")
        yield  # pragma: no cover

    monkeypatch.setattr(public_service, "_active_pair_write_guard", unavailable_guard)
    with client.session_transaction() as session_record:
        session_record["_csrf_token"] = "lookup-lock-csrf"
    response = client.post(
        "/action",
        data={"short_code": "97539753", "csrf_token": "lookup-lock-csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/action")
    assert DailyStatus.query.filter_by(pair_id=pair_id).count() == 0
