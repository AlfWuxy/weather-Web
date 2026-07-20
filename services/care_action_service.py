# -*- coding: utf-8 -*-
"""Web 与小程序共用的照护行动写入规则。

本模块只在调用方已经持有账号 owner 锁时暂存 ORM 变更。事务提交、天气读取、
HTTP 响应和页面渲染继续由各自适配层负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping

from core.db_models import DailyStatus, Debrief, Pair, UsageEvent
from core.extensions import db
from core.time_utils import utcnow


ACTION_SOURCES = frozenset({"web", "miniprogram"})
RELAY_STAGES = frozenset({"none", "caregiver", "backup", "community", "emergency"})
MAX_ELDER_ACTIONS = 20
MAX_ELDER_ACTION_LENGTH = 50
ABSENT = object()


@dataclass(frozen=True)
class ActionMutation:
    """提交前冻结的响应与派生刷新所需标量。"""

    pair_id: int
    status_date: date
    community_code: str
    confirmed_at: datetime | None = None
    relay_stage: str | None = None
    debrief_id: int | None = None
    linked_pair_id: int | None = None


def _require_active_pair(pair: Pair) -> None:
    if pair is None or pair.status != "active":
        raise ValueError("inactive_pair")


def _normalize_source(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if normalized not in ACTION_SOURCES:
        raise ValueError("invalid_action_source")
    return normalized


def _stage_usage_event(*, pair: Pair, event_type: str, source: str, meta: Mapping, now):
    """在主事务内暂存最小匿名事件，不触发独立提交。"""
    event = UsageEvent(
        user_id=pair.caregiver_id,
        pair_id=None,
        member_id=None,
        event_type=event_type,
        meta_json=json.dumps(
            dict(meta),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        source=_normalize_source(source),
        created_at=now,
    )
    db.session.add(event)
    return event


def get_or_create_daily_status(
    pair: Pair,
    status_date: date,
    risk_level=None,
    *,
    risk_level_factory=None,
    now=None,
) -> DailyStatus:
    """按照护关系和本地日期取得状态，必要时使用延迟风险工厂创建。"""
    _require_active_pair(pair)
    record = DailyStatus.query.filter_by(
        pair_id=pair.id,
        status_date=status_date,
    ).first()
    if record is None:
        resolved_risk = risk_level
        if resolved_risk is None and callable(risk_level_factory):
            resolved_risk = risk_level_factory()
        timestamp = now or utcnow()
        record = DailyStatus(
            pair_id=pair.id,
            status_date=status_date,
            community_code=pair.community_code,
            risk_level=resolved_risk,
            created_at=timestamp,
            updated_at=timestamp,
        )
        db.session.add(record)
        db.session.flush()
    else:
        # 聚合键始终跟随 Pair，避免不同客户端写出两套社区口径。
        record.community_code = pair.community_code
        if risk_level and not record.risk_level:
            record.risk_level = risk_level
    return record


def stage_confirm_action(
    pair: Pair,
    status: DailyStatus,
    *,
    actions_done_count: int,
    source: str,
    elder_actions=ABSENT,
    now=None,
) -> ActionMutation:
    """暂存今日确认；具体自护项使用独立字段，避免覆盖照护端行动。"""
    _require_active_pair(pair)
    normalized_source = _normalize_source(source)
    if (
        not isinstance(actions_done_count, int)
        or isinstance(actions_done_count, bool)
        or actions_done_count < 0
    ):
        raise ValueError("invalid_actions_done_count")

    normalized_elder_actions = ABSENT
    if elder_actions is not ABSENT:
        if not isinstance(elder_actions, (list, tuple)):
            raise ValueError("invalid_elder_actions")
        if len(elder_actions) > MAX_ELDER_ACTIONS:
            raise ValueError("invalid_elder_actions")
        normalized_elder_actions = []
        for item in elder_actions:
            if not isinstance(item, str):
                raise ValueError("invalid_elder_actions")
            normalized_item = item.strip()
            if not normalized_item or len(normalized_item) > MAX_ELDER_ACTION_LENGTH:
                raise ValueError("invalid_elder_actions")
            normalized_elder_actions.append(normalized_item)
        if actions_done_count != len(normalized_elder_actions):
            raise ValueError("elder_action_count_mismatch")

    timestamp = now or utcnow()
    status.confirmed_at = timestamp
    status.actions_done_count = actions_done_count
    if normalized_elder_actions is not ABSENT:
        status.elder_actions = json.dumps(
            normalized_elder_actions,
            ensure_ascii=False,
        )
    status.community_code = pair.community_code
    status.updated_at = timestamp
    pair.last_active_at = timestamp
    if actions_done_count >= 1:
        _stage_usage_event(
            pair=pair,
            event_type="checkin_confirmed",
            source=normalized_source,
            meta={"actions_done_count": min(actions_done_count, 1000)},
            now=timestamp,
        )
    return ActionMutation(
        pair_id=int(pair.id),
        status_date=status.status_date,
        community_code=str(pair.community_code),
        confirmed_at=timestamp,
    )


def stage_help_action(
    pair: Pair,
    status: DailyStatus,
    *,
    source: str,
    note=None,
    note_provided=False,
    now=None,
) -> ActionMutation:
    """暂存求助和匿名事件；空请求不会清除已有照护备注。"""
    _require_active_pair(pair)
    normalized_source = _normalize_source(source)
    timestamp = now or utcnow()
    first_help_request = not bool(status.help_flag)
    status.help_flag = True
    if status.relay_stage in (None, "", "none"):
        status.relay_stage = "caregiver"
    if note_provided and str(note or "").strip():
        status.caregiver_note = str(note).strip()
    status.community_code = pair.community_code
    status.updated_at = timestamp
    pair.last_active_at = timestamp
    safe_relay_stage = (
        status.relay_stage if status.relay_stage in RELAY_STAGES else "caregiver"
    )
    if first_help_request:
        _stage_usage_event(
            pair=pair,
            event_type="help_flagged",
            source=normalized_source,
            meta={"relay_stage": safe_relay_stage},
            now=timestamp,
        )
    return ActionMutation(
        pair_id=int(pair.id),
        status_date=status.status_date,
        community_code=str(pair.community_code),
        relay_stage=safe_relay_stage,
    )


def stage_debrief_action(
    pair: Pair,
    status: DailyStatus,
    *,
    answers: Mapping[str, str],
    difficulty: str,
    opt_in: bool,
    source: str,
    now=None,
) -> ActionMutation:
    """暂存当日复盘、展示关联与匿名反馈事件。"""
    _require_active_pair(pair)
    normalized_source = _normalize_source(source)
    if not isinstance(opt_in, bool):
        raise ValueError("invalid_debrief_optin")
    timestamp = now or utcnow()
    record = Debrief.query.filter_by(
        owner_user_id=pair.caregiver_id,
        origin_pair_id=pair.id,
        date=status.status_date,
    ).order_by(Debrief.id.desc()).first()
    if record is None:
        record = Debrief(
            owner_user_id=pair.caregiver_id,
            origin_pair_id=pair.id,
            pair_id=pair.id if opt_in else None,
            date=status.status_date,
            community_code=pair.community_code,
            created_at=timestamp,
        )
        db.session.add(record)

    record.owner_user_id = pair.caregiver_id
    record.origin_pair_id = pair.id
    record.community_code = pair.community_code
    record.pair_id = pair.id if opt_in else None
    record.question_1 = str(answers.get("question_1") or "")
    record.question_2 = str(answers.get("question_2") or "")
    record.question_3 = str(answers.get("question_3") or "")
    record.difficulty = str(difficulty or "")
    status.debrief_optin = opt_in
    status.community_code = pair.community_code
    status.updated_at = timestamp
    pair.last_active_at = timestamp
    _stage_usage_event(
        pair=pair,
        event_type="feedback_submitted",
        source=normalized_source,
        meta={
            "difficulty_len": min(len(record.difficulty), 300),
            "optin": opt_in,
        },
        now=timestamp,
    )
    db.session.flush()
    return ActionMutation(
        pair_id=int(pair.id),
        status_date=status.status_date,
        community_code=str(pair.community_code),
        debrief_id=int(record.id),
        linked_pair_id=int(record.pair_id) if record.pair_id is not None else None,
    )
