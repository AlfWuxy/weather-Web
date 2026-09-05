# -*- coding: utf-8 -*-
"""已知跨端求助缺陷：旧实现必须失败，修复后必须通过。

覆盖：短码页表单 action=/None、跨天未结求助、同日结案后再求助、
旧 /actions/<id>/help 适配、record_event 内层 commit、重复通知。
"""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from core.db_models import ActionEvent, AlertDelivery, DailyStatus, FamilyMember, HelpRequest, Notification, NotificationOutbox, Pair, User
from services.help_request_service import (
    ack_help_request,
    create_help_request,
    resolve_help_request,
)
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import today_local, utcnow
from core.usage import create_api_token
from services.action_events import record_event


OPEN_HELP_STATUSES = frozenset(
    {
        "requested",
        "pending_ack",
        "acknowledged",
        "in_progress",
        "open",
    }
)
CLOSED_HELP_STATUSES = frozenset({"resolved", "cancelled", "closed"})


class _FormActionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.actions = []

    def handle_starttag(self, tag, attrs):
        if tag != "form":
            return
        action = dict(attrs).get("action")
        if action is not None:
            self.actions.append(action)


def _user(username, **kwargs):
    user = User(username=username, role=kwargs.pop("role", "user"), **kwargs)
    user.set_password("pass12344")
    db.session.add(user)
    db.session.commit()
    return user


def _pair(user, code, elder_code, relation="妈"):
    member = FamilyMember(
        user_id=user.id,
        name="测试称呼对象",
        relation=relation,
        created_at=utcnow(),
    )
    db.session.add(member)
    db.session.flush()
    pair = Pair(
        caregiver_id=user.id,
        community_code="都昌",
        location_query="都昌",
        member_id=member.id,
        elder_code=elder_code,
        short_code=code,
        short_code_hash=hash_short_code(code),
        short_code_expires_at=utcnow() + timedelta(days=90),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db.session.add(pair)
    db.session.commit()
    return pair


def _csrf(client, token="help-reg-csrf"):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token
    return token


def _auth(user_id):
    return {"Authorization": f"Bearer {create_api_token(user_id, name='help-reg')}"}


def _open_help_items(payload):
    """新旧 pending 契约共用：必须能找出未终结求助。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    requests = data.get("help_requests")
    if isinstance(requests, list):
        return [
            item
            for item in requests
            if str(item.get("status") or "") in OPEN_HELP_STATUSES
            and str(item.get("status") or "") not in CLOSED_HELP_STATUSES
        ]
    items = []
    for pair in data.get("pairs") or payload.get("pairs") or []:
        today = pair.get("today") or {}
        if today.get("help_requested") and not today.get("help_acknowledged") and not today.get("closed"):
            items.append(pair)
    return items


def test_action_lookup_help_form_posts_to_real_route(app, client, db_session):
    """真实渲染后的求助表单不得指向 /None 或空 action。"""
    with app.app_context():
        user = _user("help_form_owner")
        _pair(user, "71000001", "elder-help-form")

    token = _csrf(client, "help-form-csrf")
    lookup = client.post(
        "/action",
        data={"short_code": "71000001", "csrf_token": token},
    )
    assert lookup.status_code == 200
    html = lookup.get_data(as_text=True)
    parser = _FormActionParser()
    parser.feed(html)
    assert parser.actions, "行动页必须渲染表单"
    for action in parser.actions:
        assert action not in {"None", "none", "/None", ""}, action
        assert "None" not in action, action
    assert any("/action/help" in action or "/e/" in action for action in parser.actions)


def test_pending_keeps_yesterday_unacked_help(app, client, db_session):
    with app.app_context():
        owner = _user("pending_yday_owner")
        pair = _pair(owner, "71000002", "elder-pending-yday")
        headers = _auth(owner.id)
        pair_id = pair.id
        yesterday_evening = datetime.combine(
            today_local() - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) + timedelta(hours=15, minutes=59)

    with app.app_context():
        pair = db.session.get(Pair, pair_id)
        body, created = create_help_request(
            db.session.get(User, pair.caregiver_id),
            pair,
            origin_channel="miniprogram",
            is_proxy=True,
            commit=True,
        )
        assert created is True
        row = HelpRequest.query.filter_by(public_id=body["id"]).one()
        row.created_at = yesterday_evening
        row.updated_at = yesterday_evening
        db.session.commit()

    pending = client.get("/mp/api/v1/pending", headers=headers)
    assert pending.status_code == 200
    body = pending.get_json()
    open_items = _open_help_items(body)
    assert open_items, "前一天未接收的求助次日必须仍出现在待处理"


def test_pending_new_help_after_same_day_close_is_a_new_open_item(app, client, db_session):
    with app.app_context():
        owner = _user("pending_reopen_owner")
        pair = _pair(owner, "71000003", "elder-pending-reopen")
        headers = _auth(owner.id)
        pair_id = pair.id
        first, _ = create_help_request(owner, pair, origin_channel="miniprogram", is_proxy=True, commit=True)
        ack_help_request(owner, first["id"], expected_version=first["version"], commit=True)
        latest = HelpRequest.query.filter_by(public_id=first["id"]).one()
        resolve_help_request(
            owner,
            first["id"],
            expected_version=latest.version,
            resolution_code="reached_elder",
            commit=True,
        )
        second, created = create_help_request(
            owner,
            pair,
            origin_channel="miniprogram",
            is_proxy=True,
            idempotency_key="reopen-2",
            commit=True,
        )
        assert created is True
        assert second["id"] != first["id"]

    pending = client.get("/mp/api/v1/pending", headers=headers)
    assert pending.status_code == 200
    open_items = _open_help_items(pending.get_json())
    assert open_items, "同日结案后再求助必须生成新的未结项，不能被旧 ack/closed 布尔值盖住"

    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).one()
        # 投影可以保留历史标记，但列表契约不能把新工单当成已结束。
        assert status.help_flag is True


def test_legacy_actions_help_route_creates_open_help(app, client, db_session):
    """v1.1 客户端 POST /mp/api/v1/actions/<pair_id>/help 必须落到同一求助服务。"""
    with app.app_context():
        owner = _user("legacy_help_owner")
        pair = _pair(owner, "71000004", "elder-legacy-help")
        headers = _auth(owner.id)
        pair_id = pair.id
        record_event(pair, "seen", "system", "miniprogram")

    response = client.post(
        f"/mp/api/v1/actions/{pair_id}/help",
        json={"note": ""},
        headers=headers,
    )
    assert response.status_code in {200, 201}, response.get_data(as_text=True)
    body = response.get_json() or {}
    data = body.get("data") or body
    request_id = data.get("id") or data.get("help_request_id") or data.get("request_id")
    assert request_id, "旧求助接口必须返回稳定求助编号"
    assert data.get("help_flag") is True or data.get("status") in OPEN_HELP_STATUSES

    pending = client.get("/mp/api/v1/pending", headers=headers)
    assert pending.status_code == 200
    assert _open_help_items(pending.get_json())


def test_next_day_ack_does_not_require_today_predecessor(app, client, db_session):
    with app.app_context():
        owner = _user("nextday_ack_owner")
        pair = _pair(owner, "71000005", "elder-nextday-ack")
        headers = _auth(owner.id)
        pair_id = pair.id
        yesterday = datetime.combine(
            today_local() - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) + timedelta(hours=16)
        record_event(pair, "seen", "system", "web_shortcode", now=yesterday)
        body, _ = create_help_request(
            owner,
            pair,
            origin_channel="miniprogram",
            is_proxy=True,
            commit=True,
        )
        row = HelpRequest.query.filter_by(public_id=body["id"]).one()
        row.created_at = yesterday
        row.updated_at = yesterday
        db.session.commit()

    ack = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "help_acknowledged"},
        headers=headers,
    )
    assert ack.status_code != 400 or (ack.get_json() or {}).get("error") != "invalid_transition"
    assert ack.status_code in {200, 201, 409}


def test_record_event_inner_commit_cannot_survive_outer_rollback(app, db_session):
    """求助/事件/outbox 必须能被外层事务一起回滚。"""
    owner = _user("tx_owner")
    pair = _pair(owner, "71000006", "elder-tx")
    before_help = HelpRequest.query.count()
    before_outbox = NotificationOutbox.query.count()
    try:
        create_help_request(
            owner,
            pair,
            origin_channel="web_shortcode",
            is_proxy=False,
            actor_role="elder",
            commit=False,
        )
        raise RuntimeError("inject-failure")
    except RuntimeError:
        db.session.rollback()

    assert HelpRequest.query.count() == before_help
    assert NotificationOutbox.query.count() == before_outbox


def test_duplicate_help_notify_does_not_create_two_deliveries(app, db_session, monkeypatch):
    app.config["FEATURE_NOTIFICATIONS"] = True
    owner = _user("dup_notify_owner", wxpusher_uid="UID_DUP", push_enabled=True)
    pair = _pair(owner, "71000007", "elder-dup-notify")
    now = utcnow()
    record_event(pair, "seen", "system", "web_shortcode", now=now)
    create_help_request(owner, pair, origin_channel="web_shortcode", is_proxy=False, actor_role="elder", commit=True)
    outbox_before = NotificationOutbox.query.count()

    sent = []

    def fake_send(*_args, **_kwargs):
        sent.append("wx")
        return {"ok": True}

    monkeypatch.setattr("services.push.wxpusher.send", fake_send)
    from services.public_service import _notify_help_requested

    _notify_help_requested(pair)
    _notify_help_requested(pair)

    assert NotificationOutbox.query.count() == outbox_before
    assert Notification.query.filter_by(user_id=owner.id, category="help_requested").count() <= 1
    assert AlertDelivery.query.filter_by(pair_id=pair.id, channel="wxpusher").count() == 0
    assert len(sent) == 0
