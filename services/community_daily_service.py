# -*- coding: utf-8 -*-
"""社区日度行动聚合的唯一写入实现。"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from core.db_models import CommunityDaily, DailyStatus, Pair, User
from core.extensions import db
from services.push.locks import community_projection_file_lock


logger = logging.getLogger(__name__)
ESCALATED_RELAY_STAGES = frozenset({"backup", "community", "emergency"})
RISK_LEVELS = ("低风险", "中风险", "高风险", "极高")
PUBLIC_AGGREGATE_MIN_SAMPLE = 5
PUBLIC_AGGREGATE_COUNT_BUCKET = 5
PUBLIC_AGGREGATE_RATE_BUCKET = 0.1
_RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}
_SESSION_PROJECTION_LOCKS_KEY = "community_projection_file_locks"


def bucket_public_count(value):
    """公开户数向下收敛到五户一档，避免前后差分暴露单户变化。"""
    count = max(int(value or 0), 0)
    return (count // PUBLIC_AGGREGATE_COUNT_BUCKET) * PUBLIC_AGGREGATE_COUNT_BUCKET


def bucket_public_rate(value):
    """公开比例只保留十个百分点一档。"""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    rate = min(max(rate, 0.0), 1.0)
    return round(round(rate / PUBLIC_AGGREGATE_RATE_BUCKET) * PUBLIC_AGGREGATE_RATE_BUCKET, 1)


@event.listens_for(Session, "after_transaction_end")
def _release_community_projection_locks(session, transaction):
    """调用者事务结束时释放 commit=False 留下的跨进程作用域锁。"""
    if transaction.parent is not None:
        return
    held_locks = session.info.pop(_SESSION_PROJECTION_LOCKS_KEY, {})
    for lock in held_locks.values():
        lock.release()


def _hold_community_projection_file_lock(community_code, status_date):
    """让同键文件锁覆盖当前数据库事务，避免 flush 后、commit 前被抢写。"""
    session = db.session()
    lock = community_projection_file_lock(community_code, status_date)
    held_locks = session.info.setdefault(_SESSION_PROJECTION_LOCKS_KEY, {})
    if lock.identity in held_locks:
        return False
    lock.acquire()
    held_locks[lock.identity] = lock
    return True


def _community_projection_advisory_lock_id(community_code, status_date):
    """生成 PostgreSQL bigint 范围内稳定、按社区日期隔离的锁编号。"""
    normalized_date = (
        status_date.isoformat()
        if hasattr(status_date, "isoformat")
        else str(status_date or "").strip()
    )
    payload = f"{str(community_code or '').strip()}\x1f{normalized_date}".encode("utf-8")
    lock_id = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)
    return lock_id or 1


def _acquire_community_projection_db_lock(
    community_code,
    status_date,
    *,
    dialect_name=None,
    execute=None,
):
    """多主机 PostgreSQL 使用事务 advisory lock 补足本机文件锁边界。"""
    effective_dialect = dialect_name or db.engine.dialect.name
    if effective_dialect != "postgresql":
        return False
    executor = execute or db.session.execute
    executor(
        db.text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _community_projection_advisory_lock_id(community_code, status_date)},
    )
    return True


def outreach_summary(total_people, confirmed_count, help_count, escalation_count):
    if total_people <= 0:
        return "暂无可用行动数据。"
    pending = max(total_people - confirmed_count, 0)
    if escalation_count > 0:
        return f"已有{escalation_count}个家庭进入升级链，优先安排社区跟进。"
    if help_count > 0:
        return f"已有{help_count}个家庭发出求助，请尽快联系。"
    if pending > 0:
        return f"仍有{pending}个家庭未确认，建议分批提醒。"
    return "全部家庭已完成确认，继续关注高温变化。"


def build_community_household_metrics(
    community_code,
    status_date,
    *,
    statuses=None,
):
    """按独立照护账号聚合一户，避免同一家庭的多位老人重复计数。"""
    active_pairs = (
        db.session.query(Pair.id, Pair.caregiver_id)
        .join(User, User.id == Pair.caregiver_id)
        .filter(
            Pair.status == "active",
            Pair.community_code == community_code,
            User.deleted_at.is_(None),
        )
        .all()
    )
    pair_owner = {
        int(pair_id): int(caregiver_id)
        for pair_id, caregiver_id in active_pairs
        if caregiver_id is not None
    }
    if statuses is None:
        status_rows = (
            DailyStatus.query.filter(
                DailyStatus.community_code == community_code,
                DailyStatus.status_date == status_date,
                DailyStatus.pair_id.in_(list(pair_owner)),
            ).all()
            if pair_owner
            else []
        )
    else:
        status_rows = [
            status
            for status in statuses
            if status.pair_id in pair_owner
            and status.community_code == community_code
            and status.status_date == status_date
        ]

    owner_states = {
        owner_id: {
            "confirmed": False,
            "help": False,
            "escalated": False,
            "risk_level": None,
        }
        for owner_id in set(pair_owner.values())
    }
    for status in status_rows:
        owner_id = pair_owner.get(status.pair_id)
        if owner_id is None:
            continue
        state = owner_states[owner_id]
        state["confirmed"] = state["confirmed"] or bool(status.confirmed_at)
        state["help"] = state["help"] or bool(status.help_flag)
        state["escalated"] = state["escalated"] or (
            status.relay_stage in ESCALATED_RELAY_STAGES
        )
        risk_level = status.risk_level
        if risk_level not in _RISK_RANK:
            continue
        current_level = state["risk_level"]
        if current_level is None or _RISK_RANK[risk_level] > _RISK_RANK[current_level]:
            # 一户多位老人只计一次，并采用当日最高风险作为家庭级风险。
            state["risk_level"] = risk_level

    risk_distribution = {level: 0 for level in RISK_LEVELS}
    confirmed_risk_distribution = {level: 0 for level in RISK_LEVELS}
    for state in owner_states.values():
        risk_level = state["risk_level"]
        if risk_level:
            risk_distribution[risk_level] += 1
            if state["confirmed"]:
                confirmed_risk_distribution[risk_level] += 1

    total_people = len(owner_states)
    confirmed_count = sum(1 for state in owner_states.values() if state["confirmed"])
    help_count = sum(1 for state in owner_states.values() if state["help"])
    escalation_count = sum(1 for state in owner_states.values() if state["escalated"])
    return {
        "total_people": total_people,
        "confirmed_count": confirmed_count,
        "help_count": help_count,
        "escalation_count": escalation_count,
        "risk_distribution": risk_distribution,
        "confirmed_risk_distribution": confirmed_risk_distribution,
    }


def refresh_community_daily(community_code, status_date, *, commit=True):
    """按 active 独立家庭重算并保存一个社区日期的派生汇总。"""
    # 锁先于任何投影读取获取，并延续到当前事务结束；commit=False 不改变调用者事务边界。
    _hold_community_projection_file_lock(community_code, status_date)
    try:
        _acquire_community_projection_db_lock(community_code, status_date)
        metrics = build_community_household_metrics(community_code, status_date)
        total_people = metrics["total_people"]
        confirmed_count = metrics["confirmed_count"]
        help_count = metrics["help_count"]
        escalation_count = metrics["escalation_count"]
        risk_distribution = metrics["risk_distribution"]

        record = CommunityDaily.query.filter_by(
            community_code=community_code,
            date=status_date,
        ).first()
        if record is None:
            record = CommunityDaily(community_code=community_code, date=status_date)
            db.session.add(record)
        record.total_people = total_people
        record.confirm_rate = round(
            confirmed_count / total_people if total_people else 0,
            4,
        )
        record.escalation_rate = round(
            escalation_count / total_people if total_people else 0,
            4,
        )
        record.risk_distribution = json.dumps(risk_distribution, ensure_ascii=False)
        record.outreach_summary = outreach_summary(
            total_people,
            confirmed_count,
            help_count,
            escalation_count,
        )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return record
    except Exception:
        # commit=True 完整拥有该事务，失败时立即回滚并释放锁；commit=False 仍交由调用者决定。
        if commit:
            db.session.rollback()
        raise


def refresh_community_daily_best_effort(
    community_code,
    status_date,
    *,
    event_logger=None,
) -> bool:
    """主动作提交后刷新派生表；失败只记录并回滚当前新事务。"""
    target_logger = event_logger or logger
    try:
        refresh_community_daily(community_code, status_date, commit=True)
        return True
    except Exception:
        # 同键刷新已在事务级串行；剩余异常多为数据库故障，交给后续行动或定时同步修复。
        db.session.rollback()
        target_logger.exception(
            "社区日度聚合刷新失败，主动作已成功提交: community=%s date=%s",
            community_code,
            status_date,
        )
        return False


def refresh_latest_community_daily_best_effort(
    community_codes,
    *,
    event_logger=None,
) -> bool:
    """照护关系变化后刷新各社区最新公开投影，失败时由读取侧继续兜底。"""
    target_logger = event_logger or logger
    normalized_codes = sorted(
        {
            str(code).strip()
            for code in (community_codes or [])
            if str(code or "").strip()
        }
    )
    if not normalized_codes:
        return True

    try:
        latest_rows = (
            db.session.query(
                CommunityDaily.community_code,
                db.func.max(CommunityDaily.date),
            )
            .filter(CommunityDaily.community_code.in_(normalized_codes))
            .group_by(CommunityDaily.community_code)
            .all()
        )
    except Exception:
        db.session.rollback()
        target_logger.exception(
            "社区关系变化后的最新投影日期查询失败: communities=%s",
            normalized_codes,
        )
        return False

    refreshed = True
    for community_code, status_date in latest_rows:
        if status_date is None:
            continue
        if not refresh_community_daily_best_effort(
            community_code,
            status_date,
            event_logger=target_logger,
        ):
            refreshed = False
    return refreshed
