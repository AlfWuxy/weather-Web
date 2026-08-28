# -*- coding: utf-8 -*-
"""首次照护路径、角色导航与功能可用状态回归。"""


def _login_user(client, db_session, *, username, role='user'):
    from core.db_models import User

    user = User(
        username=username,
        role=role,
        community='都昌县',
        authorized_community='都昌县' if role == 'community' else None,
    )
    user.set_password('LongUiContractPass1!')
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as session:
        session.clear()
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'ui-contract-csrf'
    return user


def test_elder_mode_is_enabled_by_default(app, client, db_session):
    """老人模式属于默认主路径，家庭账号应看到入口。"""
    _login_user(client, db_session, username='elder-default-user')

    response = client.get('/dashboard')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert app.config['FEATURE_ELDER_MODE'] is True
    assert 'data-nav-key="elder"' in body


def test_family_navigation_keeps_first_path_compact(client, db_session):
    """普通家属主导航只保留日常高频入口，高级工具仍可从工具卡直达。"""
    _login_user(client, db_session, username='compact-family-nav')

    response = client.get('/dashboard')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-nav-key="assessment"' in body
    assert 'data-nav-key="health-diary"' in body
    assert 'data-nav-key="medication"' in body
    assert 'data-nav-key="ml-prediction"' not in body
    assert 'data-nav-key="chronic-risk"' not in body
    assert 'data-nav-key="annual-report"' not in body
    assert 'data-nav-key="ai-qa"' not in body
    assert '健康关注线索' in body
    assert 'AI 疾病预测' not in body
    assert 'AI 提问' not in body


def test_ai_page_exposes_unavailable_state_without_key(
    app,
    client,
    db_session,
):
    """功能或密钥关闭时，AI 页面不能呈现可点击的假入口。"""
    _login_user(client, db_session, username='ai-disabled-user')
    app.config['FEATURE_WEB_AI'] = False
    app.config['SILICONFLOW_API_KEY'] = ''

    response = client.get('/ai-qa')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'AI 问答当前未配置' in body
    assert 'id="openAiChat" disabled aria-disabled="true"' in body
    assert 'const openAiChatButton' not in body
    assert '/static/js/ai-floating-chat.js' not in body
    assert 'data-nav-key="ai-qa"' not in body


def test_admin_sees_ai_navigation_only_when_runtime_is_ready(
    app,
    client,
    db_session,
):
    """本地明确启用且有密钥时，管理员才看到 AI 导航和脚本。"""
    _login_user(client, db_session, username='ai-ready-admin', role='admin')
    app.config['FEATURE_WEB_AI'] = True
    app.config['SILICONFLOW_API_KEY'] = 'test-only-ai-key'

    response = client.get('/ai-qa')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'AI 问答当前未配置' not in body
    assert 'id="openAiChat" disabled' not in body
    assert 'const openAiChatButton' in body
    assert '/static/js/ai-floating-chat.js' in body
    assert 'data-nav-key="ai-qa"' in body


def test_community_navigation_stays_in_community_domain(client, db_session):
    """社区账号的可见导航不再把人带入家庭健康主路径。"""
    _login_user(client, db_session, username='community-nav-user', role='community')

    home = client.get('/').get_data(as_text=True)
    profile = client.get('/profile').get_data(as_text=True)

    assert 'href="/community-risk" data-nav-key="today"' in home
    assert 'data-nav-key="elder"' not in home
    assert 'aria-label="健康"' not in home
    assert 'data-nav-key="assessment"' not in home
    assert 'href="/community-risk" class="btn btn-outline-secondary btn-lg">查看社区风险</a>' in home
    assert 'href="/community" class="btn btn-outline-secondary">取消</a>' in profile


def test_pair_page_distinguishes_manual_and_automatic_reminders(
    client,
    db_session,
):
    """空照护台也要明确手动话术与自动微信推送的边界。"""
    _login_user(client, db_session, username='pair-copy-user', role='caregiver')

    response = client.get('/pairs')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '需要时复制话术手动提醒' in body
    assert '自动微信推送需在个人设置中单独配置并开启' in body


def test_registration_copy_separates_caregiver_and_elder_profiles(
    client,
    db_session,
):
    response = client.get('/register')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '照护者资料' in body
    assert '这里的年龄和性别属于照护者账号' in body
    assert '老人的年龄、慢病和称呼' in body
