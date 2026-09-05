# -*- coding: utf-8 -*-
"""P4 社区 API 匿名画像脱敏护栏。

I02（本文件主责 list）：
- 匿名 GET /api/community/list → 200，communities[] 不得含
  population / elderly_ratio / chronic_disease_ratio
- 正式 User 登录后 list 仍可有完整画像字段

I03（risk-map 匿名人口）：
- 匿名 GET /api/community/risk-map（及 v1）→ 200，data[] 无 population

数据源：
- list 经 CommunityRiskService 内存 community_profiles（Flask 下从 Community 表加载）
- 空表时 profiles 为空；测试须 seed + 重载单例，避免「空列表虚通过」
"""
from core.db_models import Community

# list 匿名禁止的画像敏感键（验收硬项）
_SENSITIVE_LIST_KEYS = (
    'population',
    'elderly_ratio',
    'chronic_disease_ratio',
)

# risk-map 匿名禁止键
_FORBIDDEN_ANON_RISK_MAP_KEYS = frozenset({
    'population',
    'vulnerability_index',
})

_LIST_SEED_NAME = 'I02隐私测_list试点村'
_MAP_SEED_NAME = '隐私测_匿名地图村'


def _seed_list_community(db_session, app, name=_LIST_SEED_NAME):
    """写入带人口/老龄/慢病的社区，并强制刷新 service 内存档案。"""
    row = Community(
        name=name,
        population=132,
        elderly_ratio=0.6699,
        chronic_disease_ratio=0.15,
        latitude=29.331,
        longitude=116.204,
        risk_level='中',
        vulnerability_index=2.26,
    )
    db_session.add(row)
    db_session.commit()

    from services.community_risk_service import get_community_service

    # 单例可能在空表时已创建；测试内手动重载，保证 get_all_communities 有数据
    with app.app_context():
        svc = get_community_service()
        svc._load_community_profiles()
        assert name in svc.community_profiles, 'seed 后 profiles 应含试点村'

    return row


def _seed_map_community(db_session, name=_MAP_SEED_NAME):
    """写入带人口与 VI 的社区，便于 risk-map 断言脱敏真发生。"""
    row = Community(
        name=name,
        population=219,
        elderly_ratio=0.42,
        chronic_disease_ratio=0.15,
        latitude=29.2731,
        longitude=116.2042,
        risk_level='high',
        vulnerability_index=0.88,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _assert_list_envelope(payload):
    """公共信封：success + 非空 communities。"""
    assert payload is not None
    assert payload.get('success') is True
    communities = payload.get('communities')
    assert isinstance(communities, list)
    assert len(communities) >= 1, 'seed 后 list 应至少一条社区'
    return communities


def _field_union(items):
    keys = set()
    for item in items or []:
        if isinstance(item, dict):
            keys.update(item.keys())
    return keys


# ---------------------------------------------------------------------------
# I02：list 匿名脱敏 + 正式用户完整字段
# ---------------------------------------------------------------------------


def test_anonymous_community_list_omits_sensitive_profile_fields(client, app, db_session):
    """未登录 GET /api/community/list：200，任意 community 不得含敏感画像字段。"""
    _seed_list_community(db_session, app)

    response = client.get('/api/community/list')

    assert response.status_code == 200
    communities = _assert_list_envelope(response.get_json())

    for item in communities:
        assert isinstance(item, dict)
        assert item.get('name'), f'匿名 list 条目缺少 name: {item!r}'
        leaked = [k for k in _SENSITIVE_LIST_KEYS if k in item]
        assert not leaked, (
            f'匿名 community list 泄露敏感字段 {leaked}，条目={item!r}'
        )

    # 种子村必须出现且三敏感键均不在键集合中
    seed = next(
        (c for c in communities if c.get('name') == _LIST_SEED_NAME),
        None,
    )
    assert seed is not None, f'匿名 list 应含种子村 {_LIST_SEED_NAME}'
    for key in _SENSITIVE_LIST_KEYS:
        assert key not in seed


def test_authenticated_user_community_list_keeps_full_profile_fields(
    authenticated_client, app, db_session,
):
    """正式 User 登录后 GET list：200，仍可含完整画像字段。"""
    _seed_list_community(db_session, app)

    response = authenticated_client.get('/api/community/list')

    assert response.status_code == 200
    communities = _assert_list_envelope(response.get_json())

    full = [
        item
        for item in communities
        if isinstance(item, dict)
        and all(k in item for k in _SENSITIVE_LIST_KEYS)
    ]
    assert full, (
        '登录用户 list 应保留 population/elderly_ratio/chronic_disease_ratio；'
        f'实际首条={communities[0]!r}'
    )
    sample = full[0]
    assert sample.get('name')
    for key in _SENSITIVE_LIST_KEYS:
        assert sample[key] is not None, f'{key} 不应为 None: {sample!r}'

    # 种子村画像数值与 seed 一致（防登录路径误裁或错村）
    seed = next(
        (c for c in communities if c.get('name') == _LIST_SEED_NAME),
        None,
    )
    assert seed is not None
    assert seed.get('population') == 132
    assert seed.get('elderly_ratio') == 0.6699
    assert seed.get('chronic_disease_ratio') == 0.15


# ---------------------------------------------------------------------------
# I03：risk-map 匿名无 population（保留，避免并行 I03 护栏丢失）
# ---------------------------------------------------------------------------


def _assert_anonymous_risk_map_payload(payload, seed_name):
    """公共断言：成功、含种子村、无 population 等敏感键。"""
    assert payload is not None
    assert payload.get('success') is True
    data = payload.get('data') or []
    assert data, '匿名 risk-map 应至少返回一条社区数据'

    names = {item.get('name') for item in data if isinstance(item, dict)}
    assert seed_name in names

    keys = _field_union(data)
    assert 'population' not in keys
    assert _FORBIDDEN_ANON_RISK_MAP_KEYS.isdisjoint(keys)

    seed_item = next(item for item in data if item.get('name') == seed_name)
    assert 'name' in seed_item
    assert 'latitude' in seed_item
    assert 'longitude' in seed_item
    assert 'risk_level' in seed_item
    assert 'population' not in seed_item
    assert 'vulnerability_index' not in seed_item


def test_anonymous_risk_map_has_no_population(client, db_session):
    """匿名 GET /api/community/risk-map：响应无 population 字段。"""
    seed = _seed_map_community(db_session)
    response = client.get('/api/community/risk-map')
    assert response.status_code == 200
    _assert_anonymous_risk_map_payload(response.get_json(), seed.name)


def test_anonymous_v1_risk_map_has_no_population(client, db_session):
    """匿名 GET /api/v1/community/risk-map：与兼容路径同一脱敏。"""
    seed = _seed_map_community(db_session, name='隐私测_v1地图村')
    response = client.get('/api/v1/community/risk-map')
    assert response.status_code == 200
    _assert_anonymous_risk_map_payload(response.get_json(), seed.name)
