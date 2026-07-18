# -*- coding: utf-8 -*-
"""公共社区行动统计的小样本隐私回归测试。"""

from datetime import date

from core.time_utils import utcnow


class _WechatResponse:
    status_code = 200

    def __init__(self, openid):
        self.openid = openid

    def json(self):
        return {"openid": self.openid, "session_key": "test-session-key"}


def _configure_wechat(app):
    app.config.update(
        WX_MINIPROGRAM_APPID="wx-test-appid",
        WX_MINIPROGRAM_SECRET="server-only-appsecret",
        WX_MINIPROGRAM_OPENID_PEPPER="p" * 64,
        WX_MINIPROGRAM_SESSION_SECRET="s" * 64,
        WX_MINIPROGRAM_PRIVACY_VERSION="privacy-v1",
        WX_MINIPROGRAM_SESSION_TTL_SECONDS=3600,
    )


def _wechat_login(app, client, monkeypatch, *, openid):
    _configure_wechat(app)
    monkeypatch.setattr(
        "services.miniprogram_auth.requests.get",
        lambda *_args, **_kwargs: _WechatResponse(openid),
    )
    return client.post(
        "/mp/api/v1/auth/wechat",
        json={
            "code": "wx-login-code",
            "privacy_consent_version": "privacy-v1",
        },
    )


def _new_owner(db_session, username):
    from core.db_models import User

    owner = User(username=username, role="caregiver")
    owner.set_password("test-password")
    db_session.add(owner)
    db_session.flush()
    return owner


def _new_pair(db_session, owner, community_code, index):
    from core.db_models import Pair
    from core.security import hash_short_code

    short_code = f"93{index:06d}"
    pair = Pair(
        caregiver_id=owner.id,
        community_code=community_code,
        location_query="都昌县",
        elder_code=f"privacy-elder-{index}",
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        status="active",
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.flush()
    return pair


def _seed_community_daily(db_session, community_code, *, total_people):
    import json

    from core.db_models import Community, CommunityDaily

    db_session.add(Community(name=community_code, location="都昌县"))
    record = CommunityDaily(
        community_code=community_code,
        date=date(2026, 7, 18),
        total_people=total_people,
        confirm_rate=2 / 3,
        escalation_rate=1 / 3,
        risk_distribution=json.dumps({"低风险": 1, "高风险": 2}, ensure_ascii=False),
        outreach_summary="小样本内部摘要",
    )
    db_session.add(record)
    db_session.flush()
    return record


def _public_summary(client, community_code):
    response = client.get("/mp/api/v1/public/communities")
    assert response.status_code == 200
    items = response.get_json()["data"]["items"]
    return next(item["latest_action_summary"] for item in items if item["name"] == community_code)


def _assert_action_statistics_suppressed(summary):
    assert summary["sample_suppressed"] is True
    assert summary["total_people"] is None
    assert summary["confirm_rate"] is None
    assert summary["escalation_rate"] is None
    assert "risk_distribution" not in summary
    assert "outreach_summary" not in summary


def test_public_read_suppresses_when_projected_count_is_below_threshold(
    client,
    db_session,
):
    """实时人数不足五户时，投影统计必须隐藏。"""
    community_code = "投影过小社区"
    owners = [_new_owner(db_session, f"projected-small-{index}") for index in range(3)]
    for index, owner in enumerate(owners, start=1):
        _new_pair(db_session, owner, community_code, index)
    _seed_community_daily(db_session, community_code, total_people=2)
    db_session.commit()

    _assert_action_statistics_suppressed(_public_summary(client, community_code))


def test_public_read_buckets_counts_and_rates_at_five_households(
    client,
    db_session,
):
    """达到五户后只公开分桶结果，避免前后差分暴露单户变化。"""
    community_code = "五户分桶社区"
    owners = [_new_owner(db_session, f"bucket-owner-{index}") for index in range(7)]
    for index, owner in enumerate(owners, start=100):
        _new_pair(db_session, owner, community_code, index)
    _seed_community_daily(db_session, community_code, total_people=7)
    db_session.commit()

    summary = _public_summary(client, community_code)
    assert summary == {
        "date": "2026-07-18",
        "total_people": 5,
        "confirm_rate": 0.7,
        "escalation_rate": 0.3,
        "sample_suppressed": False,
    }


def test_three_pairs_from_one_household_do_not_unlock_public_statistics(
    client,
    db_session,
):
    """同一照护账号的三位老人仍只算一户。"""
    community_code = "单户多老人社区"
    owner = _new_owner(db_session, "single-household-many-pairs")
    for index in range(3):
        _new_pair(db_session, owner, community_code, index + 60)
    _seed_community_daily(db_session, community_code, total_people=3)
    db_session.commit()

    _assert_action_statistics_suppressed(_public_summary(client, community_code))


def test_four_pairs_from_two_households_remain_suppressed(
    client,
    db_session,
):
    """两户即使共有四位老人，也不能越过三户隐私门槛。"""
    community_code = "两户多老人社区"
    owners = [_new_owner(db_session, f"two-households-{index}") for index in range(2)]
    for index in range(4):
        _new_pair(db_session, owners[index % 2], community_code, index + 70)
    _seed_community_daily(db_session, community_code, total_people=4)
    db_session.commit()

    _assert_action_statistics_suppressed(_public_summary(client, community_code))


def test_elder_deactivation_hides_stale_three_person_projection(
    app,
    client,
    db_session,
    monkeypatch,
):
    """真实停用造成 3→2 后，即使投影刷新失败也不能泄露统计。"""
    from core.db_models import CommunityDaily, Pair
    from core.usage import create_api_token

    community_code = "停用隐私社区"
    owners = [_new_owner(db_session, f"deactivate-owner-{index}") for index in range(3)]
    pairs = [
        _new_pair(db_session, owner, community_code, index + 10)
        for index, owner in enumerate(owners)
    ]
    _seed_community_daily(db_session, community_code, total_people=3)
    db_session.commit()
    token = create_api_token(owners[0].id, name="privacy-deactivate")
    refresh_targets = []

    def fail_projection_refresh(community_codes, **_kwargs):
        refresh_targets.append(set(community_codes))
        return False

    monkeypatch.setattr(
        "blueprints.mp_api.refresh_latest_community_daily_best_effort",
        fail_projection_refresh,
    )

    deleted = client.delete(
        f"/mp/api/v1/elders/{pairs[0].id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert deleted.status_code == 200
    db_session.expire_all()
    assert db_session.get(Pair, pairs[0].id).status == "inactive"
    assert CommunityDaily.query.filter_by(community_code=community_code).one().total_people == 3
    assert refresh_targets == [{community_code, "都昌县"}]
    _assert_action_statistics_suppressed(_public_summary(client, community_code))


def test_account_deletion_refreshes_projection_and_hides_three_to_two(
    app,
    client,
    db_session,
    monkeypatch,
):
    """真实账号注销删除一条关系后刷新投影，并隐藏剩余两人的统计。"""
    from core.db_models import CommunityDaily, Pair, User

    login = _wechat_login(
        app,
        client,
        monkeypatch,
        openid="community-privacy-delete-openid",
    )
    assert login.status_code == 200
    login_data = login.get_json()["data"]
    deleting_owner = db_session.get(User, login_data["user"]["id"])
    other_owners = [
        _new_owner(db_session, f"account-delete-peer-{index}")
        for index in range(2)
    ]
    community_code = "注销隐私社区"
    owners = [deleting_owner, *other_owners]
    for index, owner in enumerate(owners, start=30):
        _new_pair(db_session, owner, community_code, index)
    _seed_community_daily(db_session, community_code, total_people=3)
    db_session.commit()

    deleted = client.delete(
        "/mp/api/v1/me",
        headers={"Authorization": f"Bearer {login_data['session_token']}"},
        json={"confirm": True, "user_id": deleting_owner.id},
    )

    assert deleted.status_code == 200
    db_session.expire_all()
    assert Pair.query.filter_by(community_code=community_code, status="active").count() == 2
    projection = CommunityDaily.query.filter_by(community_code=community_code).one()
    assert projection.total_people == 2
    _assert_action_statistics_suppressed(_public_summary(client, community_code))
