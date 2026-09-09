# -*- coding: utf-8 -*-
"""P6/P10 community ACL — authorized_community 与定位字段拆分。

验收：
- role=community 可改 profile.community（定位），但 authorized_community 不可自改
- `_community_access_allowed` 只认 authorized_community（空则 fail closed）
- profile 提交 authorized_community 字段被忽略（防 mass-assignment）
"""
from __future__ import annotations


def test_community_role_cannot_change_acl_via_profile(app, client, db_session):
    """community 角色改定位到他村后，ACL 仍跟 authorized_community。"""
    from core.db_models import Community, User
    from flask_login import login_user
    from services.user._helpers import _community_access_allowed

    village_a = '村A'
    village_b = '村B'

    # 种子社区：避免 ensure_user_location_valid 把未知地名归一成默认城市，干扰断言
    db_session.add_all(
        [
            Community(name=village_a),
            Community(name=village_b),
        ]
    )
    db_session.commit()

    user = User(
        username='acl_comm_a',
        role='community',
        community=village_a,
        authorized_community=village_a,
        age=45,
        gender='男性',
    )
    user.set_password('TestPass123!')
    db_session.add(user)
    db_session.commit()
    user_id = user.id

    csrf = 'test-csrf-community-acl'
    with client.session_transaction() as sess:
        sess['_csrf_token'] = csrf

    login_resp = client.post(
        '/login',
        data={
            'username': 'acl_comm_a',
            'password': 'TestPass123!',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code in (200, 302, 303)

    # 改定位到村B，并试图 mass-assign authorized_community
    resp = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'community': village_b,
            'authorized_community': village_b,
            'age': '45',
            'gender': '男性',
            'email': '',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    # 重新读库，避免 identity map 假绿
    db_session.expire_all()
    reloaded = db_session.get(User, user_id)
    assert reloaded is not None
    # 定位可自改
    assert reloaded.community == village_b
    # ACL 字段不可自改
    assert reloaded.authorized_community == village_a

    # ACL 仍基于 authorized_community：本村允许、他村拒绝
    with app.test_request_context():
        login_user(reloaded)
        assert _community_access_allowed(village_a) is True
        assert _community_access_allowed(village_b) is False


def _seed_two_villages(db_session):
    from core.db_models import Community

    village_a = '村A'
    village_b = '村B'
    db_session.add_all([Community(name=village_a), Community(name=village_b)])
    db_session.commit()
    return village_a, village_b


def _make_user(db_session, *, username, role, community, password='TestPass123!', authorized_community=None):
    from core.db_models import User

    # community 角色默认把 ACL 钉到 community，模拟迁移 backfill
    if authorized_community is None and role == 'community':
        authorized_community = community
    user = User(
        username=username,
        role=role,
        community=community,
        authorized_community=authorized_community,
        age=45,
        gender='男性',
        email=f'{username}@example.com',
    )
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _login_client(client, username, password='TestPass123!', csrf='test-csrf-community-acl'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = csrf
    resp = client.post(
        '/login',
        data={'username': username, 'password': password, 'csrf_token': csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302, 303)
    return csrf


def test_admin_edit_non_community_fields_leaves_community_unchanged(app, client, db_session):
    """admin 改 community 用户的邮箱/年龄等，不改社区下拉时，管辖社区字段必须原样保留。

    验收口径：「admin 编辑用户 → community 不受影响」（非故意改村时）。
    """
    from core.db_models import Community, User
    from flask_login import login_user
    from services.user._helpers import _community_access_allowed

    village_a, village_b = _seed_two_villages(db_session)
    target = _make_user(
        db_session,
        username='acl_admin_edit_target',
        role='community',
        community=village_a,
    )
    other = _make_user(
        db_session,
        username='acl_other_village',
        role='community',
        community=village_b,
    )
    admin = _make_user(
        db_session,
        username='acl_admin_editor',
        role='admin',
        community=None,
    )
    target_id = target.id
    other_id = other.id
    other_community_before = other.community
    village_a_count = Community.query.filter_by(name=village_a).count()
    village_b_count = Community.query.filter_by(name=village_b).count()

    csrf = _login_client(client, 'acl_admin_editor')

    # 故意只改邮箱/年龄，community/ACL 仍提交村A（与库存一致）
    resp = client.post(
        f'/admin/user/{target_id}/edit',
        data={
            'username': 'acl_admin_edit_target',
            'email': 'new-mail@example.com',
            'age': '50',
            'gender': '男性',
            'role': 'community',
            'community': village_a,
            'authorized_community': village_a,
            'password': '',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db_session.expire_all()
    reloaded = db_session.get(User, target_id)
    other_reloaded = db_session.get(User, other_id)
    assert reloaded is not None
    assert reloaded.community == village_a, (
        f'admin 未改村时 community 必须不变，期望 {village_a!r}，实际 {reloaded.community!r}'
    )
    assert reloaded.authorized_community == village_a
    assert reloaded.email == 'new-mail@example.com'
    assert reloaded.age == 50
    assert reloaded.role == 'community'

    # 其他 community 账号与 Community 主数据不受影响
    assert other_reloaded.community == other_community_before == village_b
    assert Community.query.filter_by(name=village_a).count() == village_a_count
    assert Community.query.filter_by(name=village_b).count() == village_b_count

    with app.test_request_context():
        login_user(reloaded)
        assert _community_access_allowed(village_a) is True
        assert _community_access_allowed(village_b) is False


def test_admin_can_reassign_community_acl_only_for_that_user(app, client, db_session):
    """admin 故意把某 community 用户的管辖村从 A 改到 B：仅该用户 ACL 变化，他村账号隔离仍在。"""
    from core.db_models import User
    from flask_login import login_user
    from services.user._helpers import _community_access_allowed

    village_a, village_b = _seed_two_villages(db_session)
    target = _make_user(
        db_session,
        username='acl_admin_reassign',
        role='community',
        community=village_a,
    )
    peer = _make_user(
        db_session,
        username='acl_peer_stay_a',
        role='community',
        community=village_a,
    )
    _make_user(db_session, username='acl_admin_reassigner', role='admin', community=None)
    target_id = target.id
    peer_id = peer.id

    csrf = _login_client(client, 'acl_admin_reassigner')

    resp = client.post(
        f'/admin/user/{target_id}/edit',
        data={
            'username': 'acl_admin_reassign',
            'email': 'acl_admin_reassign@example.com',
            'age': '45',
            'gender': '男性',
            'role': 'community',
            'community': village_a,  # 定位可保持 A
            'authorized_community': village_b,  # ACL 故意改到 B
            'password': '',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db_session.expire_all()
    reloaded = db_session.get(User, target_id)
    peer_reloaded = db_session.get(User, peer_id)
    assert reloaded.authorized_community == village_b
    assert reloaded.community == village_a
    # 同村另一运营账号不得被连带改写
    assert peer_reloaded.community == village_a
    assert peer_reloaded.authorized_community == village_a

    with app.test_request_context():
        login_user(reloaded)
        assert _community_access_allowed(village_b) is True
        assert _community_access_allowed(village_a) is False

    with app.test_request_context():
        login_user(peer_reloaded)
        assert _community_access_allowed(village_a) is True
        assert _community_access_allowed(village_b) is False


def test_non_admin_cannot_edit_user_community_via_admin_route(client, db_session):
    """非 admin 打 admin 编辑接口不得改他人 community（权限门）。"""
    from core.db_models import User

    village_a, village_b = _seed_two_villages(db_session)
    target = _make_user(
        db_session,
        username='acl_victim_comm',
        role='community',
        community=village_a,
    )
    attacker = _make_user(
        db_session,
        username='acl_attacker_user',
        role='user',
        community=village_b,
    )
    target_id = target.id

    csrf = _login_client(client, 'acl_attacker_user')
    resp = client.post(
        f'/admin/user/{target_id}/edit',
        data={
            'username': 'acl_victim_comm',
            'email': 'hacked@example.com',
            'age': '45',
            'gender': '男性',
            'role': 'community',
            'community': village_b,
            'password': '',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    # 权限不足：redirect 出管理端，不应 200 完成编辑
    assert resp.status_code in (302, 303, 401, 403)

    db_session.expire_all()
    reloaded = db_session.get(User, target_id)
    assert reloaded.community == village_a
    assert reloaded.email != 'hacked@example.com' or reloaded.email == 'acl_victim_comm@example.com'


def test_authorized_empty_fails_closed_for_acl(app, db_session):
    """authorized_community 为空时拒绝社区 ACL，不信任可自改定位。

    验收：
    - authorized_community is None 或 '' 时，_user_acl_community / _community_access_allowed
      均关闭访问
    - 非空 authorized 仍优先于 community（钉优先级）
    """
    from core.db_models import User
    from flask_login import login_user
    from services.user._helpers import _community_access_allowed, _user_acl_community

    village_a, village_b = _seed_two_villages(db_session)

    # 旧行形态：仅有 community，授权列空
    # 注意：_make_user 对 community 角色在 authorized=None 时会模拟迁移 backfill，
    # 这里要显式制造「未回填」行，故先建再清列。
    legacy_none = _make_user(
        db_session,
        username='acl_legacy_none',
        role='community',
        community=village_a,
        authorized_community=village_a,
    )
    legacy_none.authorized_community = None
    db_session.commit()

    legacy_empty = _make_user(
        db_session,
        username='acl_legacy_empty',
        role='community',
        community=village_a,
        authorized_community='',  # '' 不是 None，_make_user 不会回填
    )
    # 对照：授权钉在 A、定位改到 B 时 ACL 仍跟授权
    pinned = _make_user(
        db_session,
        username='acl_pinned_auth',
        role='community',
        community=village_b,
        authorized_community=village_a,
    )

    with app.test_request_context():
        login_user(legacy_none)
        assert not _user_acl_community(legacy_none)
        assert _community_access_allowed(village_a) is False
        assert _community_access_allowed(village_b) is False

    with app.test_request_context():
        login_user(legacy_empty)
        assert not _user_acl_community(legacy_empty)
        assert _community_access_allowed(village_a) is False
        assert _community_access_allowed(village_b) is False

    with app.test_request_context():
        login_user(pinned)
        assert _user_acl_community(pinned) == village_a
        assert _community_access_allowed(village_a) is True
        assert _community_access_allowed(village_b) is False

    # 双保险：库内 authorized 仍为空（_make_user 对 community 角色默认 backfill 已显式关掉）
    db_session.expire_all()
    re_none = db_session.get(User, legacy_none.id)
    re_empty = db_session.get(User, legacy_empty.id)
    assert re_none.authorized_community in (None, '')
    assert re_empty.authorized_community in (None, '')


def test_regular_user_change_community_does_not_affect_others(app, client, db_session):
    """普通 role=user 改自己的 community 定位，不影响其他账号字段与 ACL 边界。"""
    from core.db_models import Community, User
    from flask_login import login_user
    from services.user._helpers import _community_access_allowed, _user_acl_community

    village_a, village_b = _seed_two_villages(db_session)
    # 第三村供 user 自改定位
    village_c = '村C'
    db_session.add(Community(name=village_c))
    db_session.commit()

    actor = _make_user(
        db_session,
        username='acl_user_actor',
        role='user',
        community=village_a,
        authorized_community=None,
    )
    peer_user = _make_user(
        db_session,
        username='acl_user_peer',
        role='user',
        community=village_b,
        authorized_community=None,
    )
    peer_comm = _make_user(
        db_session,
        username='acl_comm_peer',
        role='community',
        community=village_a,
        authorized_community=village_a,
    )
    actor_id = actor.id
    peer_user_id = peer_user.id
    peer_comm_id = peer_comm.id
    peer_user_before = {
        'community': peer_user.community,
        'authorized_community': peer_user.authorized_community,
        'email': peer_user.email,
        'role': peer_user.role,
    }
    peer_comm_before = {
        'community': peer_comm.community,
        'authorized_community': peer_comm.authorized_community,
        'email': peer_comm.email,
        'role': peer_comm.role,
    }
    village_a_count = Community.query.filter_by(name=village_a).count()
    village_b_count = Community.query.filter_by(name=village_b).count()
    village_c_count = Community.query.filter_by(name=village_c).count()

    csrf = _login_client(client, 'acl_user_actor')
    resp = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'community': village_c,
            'authorized_community': village_b,  # mass-assign 应被忽略
            'age': '45',
            'gender': '男性',
            'email': 'acl_user_actor@example.com',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db_session.expire_all()
    actor_re = db_session.get(User, actor_id)
    peer_user_re = db_session.get(User, peer_user_id)
    peer_comm_re = db_session.get(User, peer_comm_id)

    # 本人：定位可改；authorized 仍空（profile 不可写 ACL）
    assert actor_re.community == village_c
    assert actor_re.authorized_community in (None, '')

    # 他人：字段原样
    assert peer_user_re.community == peer_user_before['community'] == village_b
    assert peer_user_re.authorized_community == peer_user_before['authorized_community']
    assert peer_user_re.email == peer_user_before['email']
    assert peer_user_re.role == peer_user_before['role']

    assert peer_comm_re.community == peer_comm_before['community'] == village_a
    assert peer_comm_re.authorized_community == peer_comm_before['authorized_community'] == village_a
    assert peer_comm_re.email == peer_comm_before['email']
    assert peer_comm_re.role == peer_comm_before['role']

    # Community 主数据不被 profile 改写连带
    assert Community.query.filter_by(name=village_a).count() == village_a_count
    assert Community.query.filter_by(name=village_b).count() == village_b_count
    assert Community.query.filter_by(name=village_c).count() == village_c_count

    # 他人 ACL 边界不变；actor 的定位可改，授权为空时仍无社区 ACL。
    with app.test_request_context():
        login_user(peer_comm_re)
        assert _user_acl_community(peer_comm_re) == village_a
        assert _community_access_allowed(village_a) is True
        assert _community_access_allowed(village_b) is False
        assert _community_access_allowed(village_c) is False

    with app.test_request_context():
        login_user(actor_re)
        assert not _user_acl_community(actor_re)
        assert _community_access_allowed(village_c) is False
        assert _community_access_allowed(village_a) is False
