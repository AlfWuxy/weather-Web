# -*- coding: utf-8 -*-
"""P4 I05：is_guest 与未登录对社区 list/risk-map 同等脱敏。

不变量：
- 匿名与游客响应字段集合一致
- 敏感键 population / elderly_ratio / chronic_disease_ratio / vulnerability_index 均不可见
- 正式登录用户仍可见完整画像（list）/ 人口+VI（risk-map）
"""
import json
import re

from core.db_models import Community


SENSITIVE_LIST_KEYS = frozenset({
    'population',
    'elderly_ratio',
    'chronic_disease_ratio',
    'vulnerability_index',
})
SENSITIVE_RISK_MAP_KEYS = frozenset({
    'population',
    'vulnerability_index',
})


def _login_guest(client, app, csrf_token='guest-community-redact-csrf'):
    """建立与 test_guest_api_403 同构的游客会话。"""
    from flask import session as flask_session
    from flask_login import login_user

    from core.constants import GUEST_ID_PREFIX
    from core.guest import GuestUser

    guest_id = f'{GUEST_ID_PREFIX}i05_community_redact'
    profile = {
        'username': '游客',
        'age': None,
        'gender': '未知',
        'community': '朝阳社区',
        'has_chronic_disease': False,
        'chronic_diseases': None,
    }
    guest = GuestUser(guest_id, profile)

    with client.session_transaction() as sess:
        sess['guest_id'] = guest_id
        sess['guest_profile'] = profile
        sess['_csrf_token'] = csrf_token

    with app.test_request_context('/'):
        login_user(guest)
        login_keys = {
            key: flask_session[key]
            for key in ('_user_id', '_fresh', '_id')
            if key in flask_session
        }

    with client.session_transaction() as sess:
        sess.update(login_keys)
        sess.setdefault('_user_id', guest_id)
        sess.setdefault('_fresh', True)
        sess['guest_id'] = guest_id
        sess['guest_profile'] = profile
        sess['_csrf_token'] = csrf_token

    return guest


def _seed_community(db_session):
    row = Community(
        name='I05脱敏试点村',
        population=219,
        elderly_ratio=0.42,
        chronic_disease_ratio=0.15,
        latitude=29.27,
        longitude=116.20,
        risk_level='high',
        vulnerability_index=0.88,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _field_union(items):
    keys = set()
    for item in items or []:
        if isinstance(item, dict):
            keys.update(item.keys())
    return keys


def _embedded_communities(body):
    match = re.search(
        r'const communities = (.*?);\s*const communityCoords',
        body,
        flags=re.DOTALL,
    )
    assert match, '页面应嵌入 communities JSON'
    return json.loads(match.group(1))


def test_anonymous_and_guest_list_same_redaction(client, app, db_session):
    """匿名与 is_guest 对 /api/community/list 字段集合一致且无敏感键。"""
    # 匿名
    anon = client.get('/api/community/list')
    assert anon.status_code == 200
    anon_body = anon.get_json()
    assert anon_body.get('success') is True
    anon_items = anon_body.get('communities') or []
    anon_keys = _field_union(anon_items)
    assert SENSITIVE_LIST_KEYS.isdisjoint(anon_keys)

    # 游客（is_authenticated=True 但仍应脱敏）
    _login_guest(client, app)
    guest = client.get('/api/community/list')
    assert guest.status_code == 200
    guest_body = guest.get_json()
    assert guest_body.get('success') is True
    guest_items = guest_body.get('communities') or []
    guest_keys = _field_union(guest_items)
    assert SENSITIVE_LIST_KEYS.isdisjoint(guest_keys)

    # 核心：guest 与未登录公开字段集合相同（有数据时）
    if anon_items and guest_items:
        assert anon_keys == guest_keys
        # 白名单仅 name + vulnerability_level
        assert guest_keys <= {'name', 'vulnerability_level'}


def test_anonymous_and_guest_risk_map_same_redaction(client, app, db_session):
    """匿名与 is_guest 对 /api/community/risk-map 同等脱敏。"""
    _seed_community(db_session)

    anon = client.get('/api/community/risk-map')
    assert anon.status_code == 200
    anon_data = (anon.get_json() or {}).get('data') or []
    assert anon_data
    anon_keys = _field_union(anon_data)
    assert SENSITIVE_RISK_MAP_KEYS.isdisjoint(anon_keys)
    assert 'name' in anon_keys
    assert 'latitude' in anon_keys

    _login_guest(client, app)
    guest = client.get('/api/community/risk-map')
    assert guest.status_code == 200
    guest_data = (guest.get_json() or {}).get('data') or []
    assert guest_data
    guest_keys = _field_union(guest_data)
    assert SENSITIVE_RISK_MAP_KEYS.isdisjoint(guest_keys)
    assert anon_keys == guest_keys


def test_guest_community_risk_page_embeds_only_risk_map_whitelist(
    client,
    app,
    db_session,
):
    seed = _seed_community(db_session)
    _login_guest(client, app)

    response = client.get('/community-risk')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    items = _embedded_communities(body)
    seed_item = next(item for item in items if item.get('name') == seed.name)
    assert set(seed_item) <= {'name', 'latitude', 'longitude', 'risk_level'}
    assert SENSITIVE_LIST_KEYS.isdisjoint(seed_item)
    assert '游客预览仅显示社区名称、位置和粗粒度风险等级' in body


def test_formal_user_community_risk_page_keeps_rich_context(
    authenticated_client,
    db_session,
):
    seed = _seed_community(db_session)

    response = authenticated_client.get('/community-risk')

    assert response.status_code == 200
    items = _embedded_communities(response.get_data(as_text=True))
    seed_item = next(item for item in items if item.get('name') == seed.name)
    assert seed_item['population'] == 219
    assert seed_item['elderly_ratio'] == 0.42
    assert seed_item['chronic_disease_ratio'] == 0.15
    assert seed_item['vulnerability_index'] == 0.88


def _seed_list_community_and_reload(db_session, app, name='I05正式用户画像村'):
    """写入 Community 并重载 community_risk_service 内存档案，保证 list 有稳定画像。"""
    row = Community(
        name=name,
        population=219,
        elderly_ratio=0.42,
        chronic_disease_ratio=0.15,
        latitude=29.27,
        longitude=116.20,
        risk_level='high',
        vulnerability_index=0.88,
    )
    db_session.add(row)
    db_session.commit()

    from services.community_risk_service import get_community_service

    with app.app_context():
        svc = get_community_service()
        svc._load_community_profiles()
        assert name in svc.community_profiles
    return row


def _login_role(client, db_session, role, username=None):
    """登录指定正式角色（ORM User，整数 id，无 is_guest）。"""
    from core.db_models import User

    username = username or f'sec02_{role}_user'
    user = User(username=username, role=role)
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf_token = f'sec02-{role}-csrf'
    with client.session_transaction() as sess:
        sess['_csrf_token'] = csrf_token
    resp = client.post('/login', data={
        'username': username,
        'password': 'testpass',
        'csrf_token': csrf_token,
    }, follow_redirects=True)
    assert resp.status_code in (200, 302)
    return user


def test_real_user_still_sees_list_demographics(authenticated_client, app, db_session):
    """正式用户 list 必须保留完整 demographics（方案 A：登录可见，不可误伤）。"""
    seed = _seed_list_community_and_reload(db_session, app)

    response = authenticated_client.get('/api/community/list')
    assert response.status_code == 200
    body = response.get_json()
    assert body.get('success') is True
    items = body.get('communities') or []
    assert items, '正式用户 list 应有社区数据'

    keys = _field_union(items)
    # 完整画像键：与 get_all_communities 拼装一致，不得被误脱敏剥掉
    for key in (
        'population',
        'elderly_ratio',
        'chronic_disease_ratio',
        'vulnerability_index',
        'vulnerability_level',
        'name',
    ):
        assert key in keys, f'正式用户 list 缺少画像键 {key}; 实际={keys}'

    seed_item = next(i for i in items if i.get('name') == seed.name)
    assert seed_item.get('population') == 219
    assert seed_item.get('elderly_ratio') == 0.42
    assert seed_item.get('chronic_disease_ratio') == 0.15


def test_real_user_sees_risk_map_population(authenticated_client, db_session):
    """正式用户 risk-map 可见 population / vulnerability_index。"""
    seed = _seed_community(db_session)
    response = authenticated_client.get('/api/community/risk-map')
    assert response.status_code == 200
    data = (response.get_json() or {}).get('data') or []
    assert data
    keys = _field_union(data)
    assert 'population' in keys
    assert 'vulnerability_index' in keys

    seed_item = next(i for i in data if i.get('name') == seed.name)
    assert seed_item.get('population') == 219
    assert seed_item.get('vulnerability_index') == 0.88
    # 公开字段也在
    assert 'latitude' in seed_item and 'longitude' in seed_item


def test_formal_roles_not_redacted_on_list_and_map(client, app, db_session):
    """user / caregiver / admin / community 登录后均拿完整 demographics，不被误判为 guest。"""
    seed = _seed_list_community_and_reload(db_session, app, name='I05角色矩阵村')
    # risk-map 读 ORM 表；list 读已 reload 的内存档案
    required_list = {
        'population', 'elderly_ratio', 'chronic_disease_ratio', 'vulnerability_index',
    }
    required_map = {'population', 'vulnerability_index'}

    for role in ('user', 'caregiver', 'admin', 'community'):
        _login_role(client, db_session, role, username=f'sec02_matrix_{role}')

        list_resp = client.get('/api/community/list')
        assert list_resp.status_code == 200, role
        list_items = (list_resp.get_json() or {}).get('communities') or []
        assert list_items, role
        list_keys = _field_union(list_items)
        missing = required_list - list_keys
        assert not missing, f'role={role} list 被误伤，缺键 {missing}'

        map_resp = client.get('/api/community/risk-map')
        assert map_resp.status_code == 200, role
        map_data = (map_resp.get_json() or {}).get('data') or []
        assert map_data, role
        map_keys = _field_union(map_data)
        missing_map = required_map - map_keys
        assert not missing_map, f'role={role} risk-map 被误伤，缺键 {missing_map}'
        seed_map = next(i for i in map_data if i.get('name') == seed.name)
        assert seed_map.get('population') == 219, role

        # 登出，避免会话串角色
        with client.session_transaction() as sess:
            csrf = sess.get('_csrf_token', 'sec02-logout')
        client.post('/logout', data={'csrf_token': csrf}, follow_redirects=False)


def test_viewer_helper_guest_equals_anonymous(app):
    """单元：_viewer_can_see_community_demographics 对匿名与 guest 均为 False。"""
    from flask_login import login_user

    from core.constants import GUEST_ID_PREFIX
    from core.guest import GuestUser
    from services.api_service import _viewer_can_see_community_demographics

    with app.test_request_context('/api/community/list'):
        # 匿名
        assert _viewer_can_see_community_demographics() is False

        guest = GuestUser(
            f'{GUEST_ID_PREFIX}helper_check',
            {'username': '游客', 'community': '朝阳社区'},
        )
        login_user(guest)
        assert guest.is_authenticated is True
        assert getattr(guest, 'is_guest', False) is True
        # guest 已登录仍不可看画像
        assert _viewer_can_see_community_demographics() is False


def test_viewer_helper_formal_roles_true(app, db_session):
    """单元：正式角色 helper 为 True（不被 is_guest / role / id 前缀误伤）。"""
    from flask_login import login_user

    from core.db_models import User
    from services.api_service import (
        _is_anonymous_or_guest,
        _viewer_can_see_community_demographics,
    )

    for role in ('user', 'caregiver', 'admin', 'community'):
        user = User(username=f'sec02_helper_{role}', role=role)
        user.set_password('password1')
        db_session.add(user)
        db_session.commit()

        with app.test_request_context('/api/community/list'):
            login_user(user)
            assert getattr(user, 'is_authenticated', False) is True
            assert getattr(user, 'is_guest', False) is False
            assert getattr(user, 'role', None) != 'guest'
            assert not str(user.id).startswith('guest:')
            assert _is_anonymous_or_guest() is False, role
            assert _viewer_can_see_community_demographics() is True, role
