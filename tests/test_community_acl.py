# -*- coding: utf-8 -*-
"""运营社区授权必须与用户自填定位彻底分离。"""

from flask_login import login_user, logout_user


def _new_user(db_session, *, username, role, community=None, authorized=None):
    from core.db_models import User

    user = User(username=username, role=role, community=community)
    if authorized is not None:
        user.authorized_community = authorized
    user.set_password("StrongPassword1!")
    db_session.add(user)
    db_session.commit()
    return user


def test_null_authorized_community_never_falls_back_to_profile_location(
    app,
    db_session,
):
    """运营授权为空时，即使定位字段命中也必须拒绝。"""
    from services.user._helpers import (
        _community_access_allowed,
        _user_acl_community,
    )

    user = _new_user(
        db_session,
        username="acl_null_user",
        role="community",
        community="甲村",
    )

    with app.test_request_context("/"):
        login_user(user)
        assert _user_acl_community() is None
        assert _community_access_allowed("甲村") is False
        logout_user()


def test_explicit_authorized_community_is_exact_and_admin_remains_global(
    app,
    db_session,
):
    from services.user._helpers import _community_access_allowed

    operator = _new_user(
        db_session,
        username="acl_explicit_user",
        role="community",
        community="甲村",
        authorized="乙村",
    )
    admin = _new_user(
        db_session,
        username="acl_admin_user",
        role="admin",
    )

    with app.test_request_context("/"):
        login_user(operator)
        assert _community_access_allowed("乙村") is True
        assert _community_access_allowed("甲村") is False
        logout_user()

        login_user(admin)
        assert _community_access_allowed("甲村") is True
        assert _community_access_allowed("乙村") is True
        logout_user()


def test_admin_cannot_create_operating_role_without_valid_authorized_community(
    admin_client,
    db_session,
):
    from core.db_models import Community, User

    db_session.add(Community(name="合法社区"))
    db_session.commit()

    base_form = {
        "username": "unmapped_operator",
        "password": "StrongPassword1!",
        "email": "",
        "age": "",
        "gender": "",
        "community": "合法社区",
        "role": "community",
        "csrf_token": "test-csrf-token",
    }
    missing = admin_client.post(
        "/admin/user/add",
        data=base_form,
        follow_redirects=False,
    )
    assert missing.status_code in (301, 302, 303)
    assert User.query.filter_by(username="unmapped_operator").first() is None

    invalid_form = dict(base_form)
    invalid_form["username"] = "invalid_operator"
    invalid_form["authorized_community"] = "不存在社区"
    admin_client.post(
        "/admin/user/add",
        data=invalid_form,
        follow_redirects=False,
    )
    assert User.query.filter_by(username="invalid_operator").first() is None

    valid_form = dict(base_form)
    valid_form["username"] = "mapped_operator"
    valid_form["authorized_community"] = "合法社区"
    created = admin_client.post(
        "/admin/user/add",
        data=valid_form,
        follow_redirects=False,
    )
    assert created.status_code in (301, 302, 303)
    user = User.query.filter_by(username="mapped_operator").one()
    assert user.authorized_community == "合法社区"
