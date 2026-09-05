# -*- coding: utf-8 -*-
"""PRD-05 小程序家属端待处理：pending 列表、stage 白名单、事件写入。"""

import json

from core.db_models import ActionEvent, FamilyMember, Pair, UsageEvent, User
from core.security import hash_short_code
from core.time_utils import utcnow
from core.usage import create_api_token
from services.action_events import record_event


def _make_user(db_session, username):
    user = User(username=username, role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.flush()
    return user


def _make_pair(
    db_session,
    user,
    *,
    short_code,
    elder_code,
    relation="妈",
    name="机密姓名",
    status="active",
    with_member=True,
):
    member = None
    if with_member:
        member = FamilyMember(
            user_id=user.id,
            name=name,
            relation=relation,
            created_at=utcnow(),
        )
        db_session.add(member)
        db_session.flush()
    pair = Pair(
        caregiver_id=user.id,
        community_code="都昌",
        location_query="都昌",
        member_id=member.id if member else None,
        elder_code=elder_code,
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        status=status,
        last_active_at=utcnow(),
        created_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    return pair, member


def _auth(user_id):
    return {"Authorization": f"Bearer {create_api_token(user_id, name='mp-pending')}"}


def test_pending_returns_only_token_holder_pairs(app, client, db_session):
    with app.app_context():
        owner = _make_user(db_session, "pending_owner")
        other = _make_user(db_session, "pending_other")
        own_pair, _ = _make_pair(
            db_session,
            owner,
            short_code="51000001",
            elder_code="elder-pending-own",
            relation="妈",
            name="机密姓名甲",
        )
        inactive, _ = _make_pair(
            db_session,
            owner,
            short_code="51000002",
            elder_code="elder-pending-inactive",
            relation="爸",
            name="机密姓名乙",
            status="inactive",
        )
        other_pair, _ = _make_pair(
            db_session,
            other,
            short_code="51000003",
            elder_code="elder-pending-other",
            relation="邻居称呼",
            name="机密姓名丙",
        )
        owner_headers = _auth(owner.id)
        other_headers = _auth(other.id)
        own_pair_id = own_pair.id
        inactive_id = inactive.id
        other_pair_id = other_pair.id

    resp = client.get("/mp/api/v1/pending", headers=owner_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "pairs" in body
    ids = [item["pair_id"] for item in body["pairs"]]
    assert own_pair_id in ids
    assert inactive_id not in ids
    assert other_pair_id not in ids
    assert all(item["pair_id"] != other_pair_id for item in body["pairs"])

    own_item = next(item for item in body["pairs"] if item["pair_id"] == own_pair_id)
    assert own_item["elder_label"] == "妈"
    assert "name" not in own_item
    raw = resp.get_data(as_text=True)
    assert "机密姓名甲" not in raw
    assert "机密姓名乙" not in raw
    assert "机密姓名丙" not in raw
    today = own_item["today"]
    for key in (
        "delivered",
        "seen",
        "understood",
        "self_reported",
        "help_requested",
        "help_acknowledged",
        "caregiver_verified",
        "closed",
    ):
        assert key in today
        assert today[key] is False

    other_resp = client.get("/mp/api/v1/pending", headers=other_headers)
    assert other_resp.status_code == 200
    other_ids = [item["pair_id"] for item in other_resp.get_json()["pairs"]]
    assert other_ids == [other_pair_id]


def test_pair_events_stage_whitelist_and_wrong_owner(app, client, db_session):
    with app.app_context():
        owner = _make_user(db_session, "pending_stage_owner")
        other = _make_user(db_session, "pending_stage_other")
        pair, _ = _make_pair(
            db_session,
            owner,
            short_code="51000011",
            elder_code="elder-stage-own",
        )
        _make_pair(
            db_session,
            other,
            short_code="51000012",
            elder_code="elder-stage-other",
        )
        owner_headers = _auth(owner.id)
        other_headers = _auth(other.id)
        pair_id = pair.id
        before = ActionEvent.query.count()

    understood = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "understood"},
        headers=owner_headers,
    )
    assert understood.status_code == 403
    assert understood.get_json()["error"] == "forbidden"

    seen = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "seen"},
        headers=owner_headers,
    )
    assert seen.status_code == 403

    garbage = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "not_a_stage"},
        headers=owner_headers,
    )
    assert garbage.status_code == 403

    stolen = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "delivered", "messenger_role": "child", "channel": "wechat_text"},
        headers=other_headers,
    )
    assert stolen.status_code == 404
    assert stolen.get_json()["error"] == "not_found"

    stolen_understood = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "understood"},
        headers=other_headers,
    )
    assert stolen_understood.status_code == 404

    with app.app_context():
        assert ActionEvent.query.count() == before


def test_pair_events_invalid_transition_is_400(app, client, db_session):
    with app.app_context():
        owner = _make_user(db_session, "pending_transition_owner")
        pair, _ = _make_pair(
            db_session,
            owner,
            short_code="51000021",
            elder_code="elder-transition",
        )
        headers = _auth(owner.id)
        pair_id = pair.id
        before_events = ActionEvent.query.count()
        before_usage = UsageEvent.query.count()

    ack = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "help_acknowledged"},
        headers=headers,
    )
    assert ack.status_code == 400
    body = ack.get_json()
    assert body["error"] == "invalid_transition"
    assert body["to"] == "help_acknowledged"
    assert "from" in body

    closed = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={"stage": "closed"},
        headers=headers,
    )
    assert closed.status_code == 400
    assert closed.get_json()["error"] == "invalid_transition"

    with app.app_context():
        assert ActionEvent.query.count() == before_events
        assert UsageEvent.query.count() == before_usage


def test_delivered_writes_action_event_and_usage_meta(app, client, db_session):
    with app.app_context():
        owner = _make_user(db_session, "pending_delivered_owner")
        pair, member = _make_pair(
            db_session,
            owner,
            short_code="51000031",
            elder_code="elder-delivered",
            relation="妈",
        )
        headers = _auth(owner.id)
        pair_id = pair.id
        member_id = member.id
        owner_id = owner.id

    resp = client.post(
        f"/mp/api/v1/pairs/{pair_id}/events",
        json={
            "stage": "delivered",
            "messenger_role": "child",
            "channel": "wechat_text",
            "script_version": "v1",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["data"]["state"]["delivered"] is True

    with app.app_context():
        event = ActionEvent.query.filter_by(pair_id=pair_id, stage="delivered").one()
        assert event.actor_role == "caregiver"
        assert event.channel == "miniprogram"
        assert event.script_version == "v1"
        usage = UsageEvent.query.filter_by(
            event_type="caregiver_delivered",
            pair_id=pair_id,
        ).one()
        assert usage.user_id == owner_id
        assert usage.member_id == member_id
        assert usage.source == "miniprogram"
        meta = json.loads(usage.meta_json)
        assert meta["messenger_role"] == "child"
        assert meta["channel"] == "wechat_text"
        assert meta["event"] == "delivered"


def test_template_copy_requires_four_meta_fields(app, client, db_session):
    with app.app_context():
        owner = _make_user(db_session, "pending_copy_owner")
        pair, member = _make_pair(
            db_session,
            owner,
            short_code="51000041",
            elder_code="elder-copy",
        )
        headers = _auth(owner.id)
        pair_id = pair.id
        member_id = member.id
        before = UsageEvent.query.count()

    missing = client.post(
        "/mp/api/v1/events",
        json={
            "event_type": "template_copy",
            "pair_id": pair_id,
            "meta": {"script_version": "v1"},
        },
        headers=headers,
    )
    assert missing.status_code == 400
    assert missing.get_json()["error"] == "invalid_meta"

    empty = client.post(
        "/mp/api/v1/events",
        json={
            "event_type": "template_copy",
            "pair_id": pair_id,
            "meta": {},
        },
        headers=headers,
    )
    assert empty.status_code == 400
    assert empty.get_json()["error"] == "invalid_meta"

    ok = client.post(
        "/mp/api/v1/events",
        json={
            "event_type": "template_copy",
            "pair_id": pair_id,
            "member_id": member_id,
            "meta": {
                "script_version": "v1",
                "messenger_role": "child",
                "channel": "wechat_text",
                "scenario": "heat",
            },
        },
        headers=headers,
    )
    assert ok.status_code == 200
    assert ok.get_json()["success"] is True

    with app.app_context():
        assert UsageEvent.query.count() == before + 1
