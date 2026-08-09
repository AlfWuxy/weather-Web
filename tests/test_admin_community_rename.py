# -*- coding: utf-8 -*-
"""管理员社区改名必须保护所有以名称为键的持久业务引用。"""

from datetime import date

import pytest
from sqlalchemy.exc import SQLAlchemyError


OLD_NAME = "旧社区"
NEW_NAME = "新社区"


def _login_admin(client, admin_id):
    csrf_token = "admin-community-rename-csrf"
    with client.session_transaction() as session:
        session["_user_id"] = f"{admin_id}:1"
        session["_fresh"] = True
        session["_csrf_token"] = csrf_token
    return csrf_token


def _base_rows(db_session):
    from core.db_models import Community, User

    admin = User(username="community-rename-admin", role="admin")
    admin.set_password("AdminPassword1!")
    owner = User(username="community-rename-owner", role="user")
    owner.set_password("OwnerPassword1!")
    community = Community(
        name=OLD_NAME,
        location="原地址",
        population=100,
        elderly_ratio=0.2,
        chronic_disease_ratio=0.1,
    )
    db_session.add_all([admin, owner, community])
    db_session.flush()
    return admin, owner, community


def _seed_reference(db_session, owner, reference_name):
    from core.db_models import (
        CommunityDaily,
        CoolingResource,
        DailyStatus,
        Debrief,
        MedicalRecord,
        Pair,
        PairLink,
        User,
    )
    from core.time_utils import today_local, utcnow

    if reference_name == "user_authorized_community":
        if not hasattr(User, "authorized_community"):
            pytest.skip("当前分支尚未包含 authorized_community 模型列")
        target = User(username="authorized-community-user", role="community")
        target.set_password("TargetPassword1!")
        target.authorized_community = OLD_NAME
        db_session.add(target)
        return "用户运营授权"
    if reference_name == "user_community":
        target = User(username="display-community-user", role="user", community=OLD_NAME)
        target.set_password("TargetPassword1!")
        db_session.add(target)
        return "用户定位或展示社区"
    if reference_name == "medical_record":
        db_session.add(MedicalRecord(patient_name="测试病例", community=OLD_NAME))
        return "病例社区"
    if reference_name == "pair_link":
        db_session.add(PairLink(
            caregiver_id=owner.id,
            short_code="RENAME-LINK",
            token_hash="rename-link-token-hash",
            community_code=OLD_NAME,
            status="active",
            expires_at=utcnow(),
        ))
        return "绑定链接社区"
    if reference_name == "pair":
        db_session.add(Pair(
            caregiver_id=owner.id,
            community_code=OLD_NAME,
            location_query=OLD_NAME,
            elder_code="rename-pair-elder",
            short_code="RENAMEPAIR",
            status="active",
        ))
        return "照护关系社区"
    if reference_name == "daily_status":
        pair = Pair(
            caregiver_id=owner.id,
            community_code="其他社区",
            location_query="其他社区",
            elder_code="rename-status-elder",
            short_code="RENAMESTAT",
            status="active",
        )
        db_session.add(pair)
        db_session.flush()
        db_session.add(DailyStatus(
            pair_id=pair.id,
            status_date=today_local(),
            community_code=OLD_NAME,
        ))
        return "日度行动社区"
    if reference_name == "community_daily":
        db_session.add(CommunityDaily(
            community_code=OLD_NAME,
            date=today_local(),
        ))
        return "社区日汇总"
    if reference_name == "cooling_resource":
        db_session.add(CoolingResource(
            community_code=OLD_NAME,
            name="测试避暑点",
            is_active=False,
        ))
        return "避暑资源社区"
    if reference_name == "debrief":
        db_session.add(Debrief(
            date=date.today(),
            community_code=OLD_NAME,
            owner_user_id=owner.id,
        ))
        return "行动复盘社区"
    raise AssertionError(f"unknown reference: {reference_name}")


def _rename_payload(csrf_token):
    return {
        "name": NEW_NAME,
        "location": "新地址",
        "latitude": "29.27",
        "longitude": "116.20",
        "population": "200",
        "elderly_ratio": "0.3",
        "chronic_disease_ratio": "0.2",
        "csrf_token": csrf_token,
    }


@pytest.mark.parametrize(
    "reference_name",
    [
        "user_authorized_community",
        "user_community",
        "medical_record",
        "pair_link",
        "pair",
        "daily_status",
        "community_daily",
        "cooling_resource",
        "debrief",
    ],
)
def test_admin_rejects_rename_when_persistent_reference_exists(
    client,
    db_session,
    reference_name,
):
    """任何持久业务引用存在时，名称和其他待提交字段都保持原值。"""
    from core.db_models import Community

    admin, owner, community = _base_rows(db_session)
    expected_label = _seed_reference(db_session, owner, reference_name)
    db_session.commit()
    community_id = int(community.id)
    csrf_token = _login_admin(client, int(admin.id))

    response = client.post(
        f"/admin/community/{community_id}/edit",
        data=_rename_payload(csrf_token),
        follow_redirects=True,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "社区名称仍被业务数据使用，未执行改名" in body
    assert expected_label in body
    db_session.expire_all()
    preserved = db_session.get(Community, community_id)
    assert preserved.name == OLD_NAME
    assert preserved.location == "原地址"
    assert preserved.population == 100
    assert Community.query.filter_by(name=NEW_NAME).first() is None


def test_admin_allows_rename_when_no_persistent_reference_exists(client, db_session):
    """无引用时仍允许管理员正常修改社区名称和资料。"""
    from core.db_models import Community

    admin, _owner, community = _base_rows(db_session)
    db_session.commit()
    community_id = int(community.id)
    csrf_token = _login_admin(client, int(admin.id))

    response = client.post(
        f"/admin/community/{community_id}/edit",
        data=_rename_payload(csrf_token),
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/admin/communities")
    db_session.expire_all()
    renamed = db_session.get(Community, community_id)
    assert renamed.name == NEW_NAME
    assert renamed.location == "新地址"
    assert renamed.population == 200


def test_admin_rename_reference_check_failure_keeps_original_data(
    client,
    db_session,
    monkeypatch,
):
    """引用检查本身异常时必须失败关闭，不能提交任何表单字段。"""
    from blueprints import admin as admin_blueprint
    from core.db_models import Community

    admin, _owner, community = _base_rows(db_session)
    db_session.commit()
    community_id = int(community.id)
    csrf_token = _login_admin(client, int(admin.id))

    def fail_reference_check(_community_name):
        raise SQLAlchemyError("reference lookup failed")

    monkeypatch.setattr(
        admin_blueprint,
        "_community_name_reference_counts",
        fail_reference_check,
    )
    response = client.post(
        f"/admin/community/{community_id}/edit",
        data=_rename_payload(csrf_token),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "社区引用检查暂时不可用，未执行改名" in response.get_data(as_text=True)
    db_session.expire_all()
    preserved = db_session.get(Community, community_id)
    assert preserved.name == OLD_NAME
    assert preserved.location == "原地址"
    assert preserved.population == 100
