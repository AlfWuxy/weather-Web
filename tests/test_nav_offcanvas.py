# -*- coding: utf-8 -*-
"""Regression tests for the offcanvas navigation + local vendor assets."""
import re
import pytest


def _set_logged_in_user(client, db_session, *, username, role):
    """建立指定角色的测试会话。"""
    from core.db_models import User

    user = User(username=username, role=role, community='朝阳社区')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as session:
        session.clear()
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
        session['_csrf_token'] = 'nav-csrf'
    return user


def _read_response_text(client, path):
    """读取响应正文，并立即释放静态文件句柄。"""
    response = client.get(path)
    try:
        assert response.status_code == 200
        return response.get_data(as_text=True)
    finally:
        response.close()


def _assert_response_ok(client, path):
    """确认资源可访问，并立即释放响应。"""
    response = client.get(path)
    try:
        assert response.status_code == 200
    finally:
        response.close()


def test_nav_offcanvas_present(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'data-bs-toggle="offcanvas"' in body
    assert 'id="appNavDrawer"' in body
    assert '/static/vendor/bootstrap/bootstrap.bundle.min.js' in body


def test_base_loads_light_motion_assets(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'data-motion="m1 m2 m4 m5"' in body
    assert '/static/css/yilao-motion.css' in body
    assert '/static/css/yilao-data-fx.css' in body
    assert '/static/css/yilao-data-fx-extra.css' in body
    assert '/static/css/apple-polish.css' in body
    assert '/static/js/yilao-motion.js' in body
    assert '/static/js/yilao-data-fx.js' in body
    assert '/static/js/yilao-data-fx-extra.js' in body
    for path in (
        '/static/css/yilao-motion.css',
        '/static/css/yilao-data-fx.css',
        '/static/css/yilao-data-fx-extra.css',
        '/static/js/yilao-motion.js',
        '/static/js/yilao-data-fx.js',
        '/static/js/yilao-data-fx-extra.js',
    ):
        _assert_response_ok(client, path)


def test_base_has_skip_link_nav_hooks_and_footer_links(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'href="#main-content"' in body
    assert 'id="main-content"' in body
    assert 'aria-label="主导航"' in body
    assert 'aria-label="页脚导航"' in body
    assert 'href="/transparency#privacy"' in body


def test_narrow_guest_nav_moves_registration_into_drawer(client):
    """320px 顶栏隐藏注册按钮，抽屉仍保留注册入口。"""
    body = _read_response_text(client, '/')
    css = _read_response_text(client, '/static/css/apple-polish.css')

    assert 'data-nav-auth="register"' in body
    assert '<i class="bi bi-person-plus me-2"></i>注册</a>' in body
    assert '@media (max-width: 359.98px)' in css
    assert '.navbar .nav-auth-register' in css
    assert 'display: none;' in css


def test_guest_navigation_only_offers_available_destinations(client):
    assert client.get('/guest').status_code == 302
    body = client.get('/').get_data(as_text=True)

    assert 'href="/register" data-nav-key="care"' in body
    assert '注册开启照护' in body
    assert 'href="/family-members"' not in body
    assert 'href="/health-diary"' not in body
    assert 'href="/medication-reminders"' not in body
    assert 'href="/annual-report"' not in body
    assert 'href="/profile"' not in body


@pytest.mark.parametrize('role', ['guest', 'user', 'caregiver', 'community', 'admin'])
def test_mobile_navigation_keeps_community_risk_available(client, db_session, role):
    """所有身份都能从移动抽屉进入社区风险页。"""
    if role == 'guest':
        assert client.get('/guest').status_code == 302
    else:
        _set_logged_in_user(client, db_session, username=f'mobile-community-{role}', role=role)

    body = client.get('/').get_data(as_text=True)
    drawer = body.split('id="appNavDrawer"', 1)[1]
    assert 'href="/community-risk" data-nav-key="community-risk"' in drawer


@pytest.mark.parametrize(
    ('role', 'family_target', 'community_target', 'community_label'),
    [
        ('user', '/pairs', '/community-risk', '查看社区风险'),
        ('caregiver', '/caregiver', '/community-risk', '查看社区风险'),
        ('community', '/pairs', '/community', '进入社区工作台'),
        ('admin', '/caregiver', '/community', '进入社区工作台'),
    ],
)
def test_home_role_cards_match_role_destinations(
    client, db_session, role, family_target, community_target, community_label
):
    """首页角色卡的文字与实际目标保持一致。"""
    _set_logged_in_user(client, db_session, username=f'home-role-{role}', role=role)
    body = client.get('/').get_data(as_text=True)

    assert f'href="{family_target}" class="yl-role-card variant-family" data-role-card="family"' in body
    assert f'href="{community_target}" class="yl-role-card variant-doctor" data-role-card="community"' in body
    assert community_label in body


def test_home_copy_is_capability_focused_and_community_icon_exists(client):
    body = client.get('/').get_data(as_text=True)

    assert '今天的风险、提醒和行动，一眼看清。' in body
    assert '先看风险，再提醒家人或社区，并记录是否已经做到。' in body
    assert 'bi bi-building-heart' not in body
    assert 'data-role-card="community"' in body
    assert 'bi bi-building' in body
    assert '页面不追求堆满功能' not in body
    assert '页面只把需要行动的部分放到前面' not in body


def test_anonymous_elder_card_enters_guest_elder_mode(client):
    body = client.get('/').get_data(as_text=True)
    assert 'href="/guest?next=/elder-mode" class="yl-role-card variant-elder"' in body

    response = client.get('/guest?next=/elder-mode', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/elder-mode')


def test_community_navigation_has_one_workspace_entry_per_view(client, db_session):
    _set_logged_in_user(client, db_session, username='community-single-entry', role='community')
    body = client.get('/community').get_data(as_text=True)
    drawer = body.split('id="appNavDrawer"', 1)[1]

    assert '<div class="app-mega-kicker">社区与群体</div>' not in body
    assert 'data-nav-key="community-workspace"' not in drawer
    assert body.count('aria-current="page"') == 2


def test_admin_more_trigger_is_active_without_claiming_current_page(client, db_session):
    _set_logged_in_user(client, db_session, username='admin-community-more', role='admin')
    body = client.get('/community').get_data(as_text=True)
    trigger = body.split('data-nav-more-trigger="desktop"', 1)[0].rsplit('<button', 1)[1]

    assert 'app-more-trigger active' in trigger
    assert 'aria-current="page"' not in trigger
    assert 'data-nav-key="community-workspace" aria-current="page"' in body


def test_role_entry_uses_consistent_community_role_name(client):
    body = client.get('/entry').get_data(as_text=True)
    assert '<h5 class="mb-0">社区人员</h5>' in body
    assert '<h5 class="mb-0">老人自用</h5>' in body
    assert '<h5 class="mb-0">家属 / 子女</h5>' in body
    assert '选择适合你的入口' in body
    assert '家属也能代为记录' in body
    assert '试点核心闭环' not in body


@pytest.mark.parametrize(
    ('role', 'destination'),
    [
        ('user', '/pairs'),
        ('caregiver', '/caregiver'),
        ('community', '/community'),
        ('admin', '/caregiver'),
    ],
)
def test_care_destination_is_role_aware(client, db_session, role, destination):
    _set_logged_in_user(client, db_session, username=f'nav-{role}', role=role)
    body = client.get('/').get_data(as_text=True)
    assert f'href="{destination}" data-nav-key="care"' in body


@pytest.mark.parametrize(
    ('role', 'expected_target', 'expected_label'),
    [
        ('user', '/community-risk', '查看社区风险'),
        ('caregiver', '/community-risk', '查看社区风险'),
        ('community', '/community', '进入社区看板'),
        ('admin', '/community', '进入社区看板'),
    ],
)
def test_role_entry_uses_authorized_community_destination(
    client, db_session, role, expected_target, expected_label
):
    _set_logged_in_user(client, db_session, username=f'entry-{role}', role=role)
    body = client.get('/entry').get_data(as_text=True)
    target = re.search(
        r'data-entry-key="community"\s+href="([^"]+)"[^>]*>\s*([^<]+)\s*</a>',
        body,
    )
    assert target and target.group(1) == expected_target
    assert target.group(2).strip() == expected_label


@pytest.mark.parametrize(
    ('role', 'expected_target'),
    [
        ('user', '/pairs'),
        ('caregiver', '/caregiver'),
        ('community', '/pairs'),
        ('admin', '/caregiver'),
    ],
)
def test_role_entry_uses_role_aware_care_destination(client, db_session, role, expected_target):
    _set_logged_in_user(client, db_session, username=f'entry-care-{role}', role=role)
    body = client.get('/entry').get_data(as_text=True)
    assert f'data-entry-key="care" href="{expected_target}"' in body


def test_guest_role_entry_offers_registration_instead_of_restricted_care(client):
    assert client.get('/guest').status_code == 302
    body = client.get('/entry').get_data(as_text=True)

    assert 'data-entry-key="care" href="/register"' in body
    assert '注册开启照护' in body


def test_flash_categories_keep_their_visual_severity(client):
    with client.session_transaction() as session:
        session['_flashes'] = [
            ('warning', '警告消息'),
            ('info', '提示消息'),
            ('success', '成功消息'),
            ('error', '错误消息'),
        ]

    body = client.get('/').get_data(as_text=True)
    assert 'alert-warning' in body
    assert 'alert-info' in body
    assert 'alert-success' in body
    assert 'alert-danger' in body


def test_login_required_message_is_warning(client):
    response = client.get('/profile', follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '请先登录' in body
    assert 'alert-warning' in body


def test_more_menu_escape_only_restores_focus_when_open(client):
    body = client.get('/').get_data(as_text=True)

    assert "event.key === 'Escape' && menuRoot.classList.contains('is-open')" in body
    assert 'trigger.focus();' in body
