# -*- coding: utf-8 -*-
"""首次路径、能力降级与可信文案回归测试。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_elder_mode_defaults_on_and_guest_defaults_to_duchang(app):
    from core.constants import DEFAULT_CITY_LABEL
    from core.guest import build_guest_profile

    assert app.config['FEATURE_ELDER_MODE'] is True
    with app.test_request_context('/guest'):
        profile = build_guest_profile()
        assert profile['community'] == DEFAULT_CITY_LABEL == '都昌县'


def test_elder_mode_direct_route_follows_feature_flag(
    authenticated_client,
    app,
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        'blueprints.user.user_service.elder_dashboard',
        lambda: calls.append('called') or 'elder-ok',
    )

    app.config['FEATURE_ELDER_MODE'] = False
    disabled = authenticated_client.get('/elder-mode')
    assert disabled.status_code == 404
    assert calls == []

    app.config['FEATURE_ELDER_MODE'] = True
    enabled = authenticated_client.get('/elder-mode')
    assert enabled.status_code == 200
    assert calls == ['called']


def test_ai_floating_chat_is_hidden_from_guest_even_when_configured(
    client,
    app,
    db_session,
):
    app.config['SILICONFLOW_API_KEY'] = 'configured-for-ui-test'
    app.config['AI_ALLOWED_MODELS'] = ['deepseek-ai/DeepSeek-V3.2']

    assert client.get('/guest').status_code == 302
    guest_response = client.get('/dashboard')
    assert guest_response.status_code == 200
    guest_body = guest_response.get_data(as_text=True)
    guest_body_tag = guest_body.split('<body', 1)[1].split('>', 1)[0]
    assert 'data-user-role="guest"' in guest_body_tag, guest_body_tag
    assert 'id="ai-floating-chat"' not in guest_body

    script = (PROJECT_ROOT / 'static/js/ai-floating-chat.js').read_text(encoding='utf-8')
    assert "userRole !== 'guest'" in script
    assert 'if (!canUseAi)' in script


def test_ai_floating_chat_is_rendered_for_formal_user_when_configured(
    authenticated_client,
    app,
):
    app.config['SILICONFLOW_API_KEY'] = 'configured-for-ui-test'
    app.config['AI_ALLOWED_MODELS'] = ['deepseek-ai/DeepSeek-V3.2']

    body = authenticated_client.get('/dashboard').get_data(as_text=True)

    assert 'data-user-role="user"' in body
    assert 'id="ai-floating-chat"' in body


def test_community_dashboard_family_entry_returns_to_role_entry(
    client,
    db_session,
):
    from core.db_models import User

    user = User(username='ui_community_dashboard', role='community', community='都昌县')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True

    response = client.get('/dashboard')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'href="/entry"' in body
    assert 'href="/family-members"' not in body


def test_unconfigured_ai_is_disabled_in_page_hidden_globally_and_503(
    authenticated_client,
):
    page = authenticated_client.get('/ai-qa')
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'AI 服务当前未配置，本页问答功能已停用' in body
    assert 'id="openAiChat" disabled aria-disabled="true"' in body
    assert 'id="ai-floating-chat"' not in body

    response = authenticated_client.post(
        '/api/ai/ask',
        json={
            'question': '今天需要注意什么？',
            'model': 'deepseek-ai/DeepSeek-V3.2',
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    assert response.status_code == 503
    payload = response.get_json()
    assert payload == {
        'success': False,
        'error': 'ai_service_unavailable',
        'message': 'AI 服务当前未配置，请稍后再试。',
    }


def test_pair_push_notice_follows_capability_state(
    authenticated_client,
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import Pair, User
    from services.user import caregiver_service

    monkeypatch.setattr(
        caregiver_service,
        'resolve_location',
        lambda label: {'location_code': '', 'display_name': label},
    )
    monkeypatch.setattr(
        caregiver_service,
        '_build_pair_action_link',
        lambda pair: f'/e/test-token?short_code={pair.short_code}',
    )

    app.config['WXPUSHER_APP_TOKEN'] = ''
    empty_body = authenticated_client.get('/pairs').get_data(as_text=True)
    assert '自动微信提醒尚未开放' not in empty_body
    assert '如需微信提醒，请先完成账号设置' not in empty_body

    user = User.query.filter_by(username='testuser').one()
    db_session.add(Pair(
        caregiver_id=user.id,
        community_code='牛家垄周村',
        location_query='牛家垄周村',
        elder_code='trust-elder-01',
        short_code='881001',
        status='active',
    ))
    db_session.commit()

    unavailable_body = authenticated_client.get('/pairs').get_data(as_text=True)
    assert '自动微信提醒尚未开放' in unavailable_body
    assert '当前仍可复制提醒话术并手动发送' in unavailable_body

    app.config['WXPUSHER_APP_TOKEN'] = 'configured-channel-token'
    setup_body = authenticated_client.get('/pairs').get_data(as_text=True)
    assert '如需微信提醒，请先完成账号设置' in setup_body
    assert '实际送达以发送记录为准' in setup_body

    user.wxpusher_uid = 'UID_READY'
    user.push_enabled = True
    db_session.commit()
    ready_body = authenticated_client.get('/pairs').get_data(as_text=True)
    assert '自动微信提醒尚未开放' not in ready_body
    assert '如需微信提醒，请先完成账号设置' not in ready_body


def test_wxoa_tracking_badges_are_debug_only(client, app, db_session):
    app.config['DEBUG'] = False
    production_body = client.get(
        '/wxoa?from=menu&article=summer-guide'
    ).get_data(as_text=True)
    assert '调试来源：' not in production_body
    assert '调试文章：' not in production_body
    assert 'from=menu' not in production_body
    assert 'article=summer-guide' not in production_body

    app.config['DEBUG'] = True
    debug_body = client.get(
        '/wxoa?from=menu&article=summer-guide'
    ).get_data(as_text=True)
    assert '调试来源：menu' in debug_body
    assert '调试文章：summer-guide' in debug_body


def test_ui_copy_avoids_unverified_claims_and_old_brand_titles():
    trust_files = [
        'templates/base.html',
        'templates/user_dashboard.html',
        'templates/pair_management.html',
        'templates/family_members.html',
        'templates/family_member_detail.html',
        'templates/cooling.html',
        'templates/wxoa_landing.html',
        'templates/about_trust_network.html',
        'templates/ml_prediction.html',
    ]
    combined = '\n'.join(
        (PROJECT_ROOT / path).read_text(encoding='utf-8') for path in trust_files
    )
    for forbidden in (
        'AI 疾病预测',
        '第一时间收到提醒',
        '论文核心洞察',
        '论文闭环',
        '多数免费',
        '身份证',
        '离你最近的纳凉点',
        '请先在左侧添加',
        '授权医生可见',
        '授权医生查看',
        '授权社区查看',
        '凉爽舒适',
        '当前适合短时户外活动',
    ):
        assert forbidden not in combined

    title_files = [
        'templates/action_checkin.html',
        'templates/health_diary.html',
        'templates/medication_reminders.html',
        'templates/caregiver_pair_detail.html',
        'templates/analysis_history.html',
        'templates/annual_report.html',
        'templates/admin_dashboard.html',
        'templates/reports.html',
    ]
    for path in title_files:
        html = (PROJECT_ROOT / path).read_text(encoding='utf-8')
        assert '宜老天气通' in html
        assert '天气健康风险预测系统' not in html

    history = (PROJECT_ROOT / 'templates/analysis_history.html').read_text(encoding='utf-8')
    assert '低保真线框' not in history
    assert '线框预览版' not in history
    assert 'runtime_root' not in history
    assert 'ui_version' not in history

    pair_template = (PROJECT_ROOT / 'templates/pair_management.html').read_text(encoding='utf-8')
    assert '天气统一使用都昌县观测' in pair_template
    assert '只用于县内照护备注' in pair_template


def test_mobile_and_today_shortcuts_keep_core_routes_without_disease_claim(
    authenticated_client,
):
    body = authenticated_client.get('/dashboard').get_data(as_text=True)
    drawer = body.split('id="appNavDrawer"', 1)[1]

    assert 'href="/pairs" data-nav-key="care"' in body
    assert 'href="/forecast-7day" data-nav-key="forecast"' in drawer
    assert 'href="/community-risk" data-nav-key="community-risk"' in drawer
    assert '<h5>7 天预报</h5>' in body
    assert '<h5>社区风险</h5>' in body
    assert 'AI 疾病预测' not in body


def test_ai_profile_and_elder_entry_copy_use_current_product_semantics(client):
    """用户可见文案与 AI 身份不再暴露旧品牌或通道实现名。"""
    ai_service = (PROJECT_ROOT / 'services/ai_question_service.py').read_text(encoding='utf-8')
    profile = (PROJECT_ROOT / 'templates/profile.html').read_text(encoding='utf-8')
    elder_entry = client.get('/elder').get_data(as_text=True)

    assert '你是宜老天气通的天气健康信息助手' in ai_service
    assert '天气健康风险预测系统的智能助手' not in ai_service
    assert '微信提醒接收码（选填）' in profile
    assert 'WxPusher 微信接收码' not in profile
    assert '打开大字今日页' in elder_entry
    assert '/guest?next=/elder-mode' in elder_entry


def test_community_date_stays_iso_without_locale_reformatting():
    template = (PROJECT_ROOT / 'templates/community_risk.html').read_text(
        encoding='utf-8'
    )
    assert '日期格式：YYYY-MM-DD（ISO）' in template
    assert 'toLocaleDateString' not in template
    assert 'toLocaleString' not in template


def test_community_risk_production_ignores_client_weather_provenance(
    authenticated_client,
    app,
    monkeypatch,
):
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()
    seen = {'weather_location': None, 'temperature': None}

    class FakeCommunityService:
        def generate_community_risk_map(
            self,
            weather_data,
            target_date=None,
            window_days=None,
            disease_filter=None,
        ):
            seen['temperature'] = weather_data.get('temperature')
            return {
                'map_data': {},
                'rankings': [],
                'summary': {},
                'macro_weather': {},
                'layers': {},
                'impact_likelihood_matrix': {},
                'equity_stratification': {},
                'methodology': [],
                'management_suggestions': [],
            }

    def fake_get_weather(location):
        from core.time_utils import utcnow

        seen['weather_location'] = location
        return ({
            'temperature': 26,
            'temperature_max': 31,
            'temperature_min': 21,
            'humidity': 61,
            'pressure': 1008,
            'wind_speed': 2.4,
            'weather_condition': '多云',
            'aqi': 42,
            'pm25': 18,
            'air_quality_available': True,
            'observed_at': utcnow().isoformat(),
            'air_observed_at': utcnow().isoformat(),
            'data_source': 'QWeather',
            'is_mock': False,
        }, True)

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        fake_get_weather,
    )
    monkeypatch.setattr(
        'services.community_risk_service.get_community_service',
        lambda: FakeCommunityService(),
    )
    app.config['TESTING'] = False
    app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'city': '上海',
            'weather': {
                'temperature': 49,
                'humidity': 99,
                'aqi': 500,
                'data_source': 'QWeather',
                'is_mock': False,
            },
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 200
    assert seen == {
        'weather_location': '116.2,29.27',
        'temperature': 26,
    }
    clear_local_community_risk_cache()


def _fresh_publication_weather(source):
    from core.time_utils import utcnow

    return {
        'temperature': 35,
        'temperature_max': 38,
        'temperature_min': 27,
        'humidity': 70,
        'pressure': 1005,
        'wind_speed': 3.1,
        'weather_condition': '晴',
        'aqi': 50 if source == 'QWeather' else None,
        'pm25': 20 if source == 'QWeather' else None,
        'air_quality_available': source == 'QWeather',
        'observed_at': utcnow().isoformat(),
        'data_source': source,
        'is_mock': False,
    }


def test_public_community_copy_requires_fresh_qweather_production_ready(
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import Community, User

    admin = User(username='ui_publication_admin', role='admin')
    admin.set_password('testpass')
    community = Community(name='UI传播门测试村')
    db_session.add_all([admin, community])
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = admin.get_id()
        session['_fresh'] = True

    from services.user import community_service

    openmeteo = _fresh_publication_weather('Open-Meteo')
    monkeypatch.setattr(
        community_service,
        '_load_heat_risk',
        lambda _location: (openmeteo, {'risk_level': 'high'}, '高风险'),
    )
    local_only = client.get('/community/announce?community=都昌县')
    local_body = local_only.get_data(as_text=True)
    assert local_only.status_code == 200
    assert '本地基础热风险：高风险 · 天气来源：Open-Meteo' in local_body
    assert '公共传播内容待恢复' in local_body
    assert 'id="announceElder"' not in local_body

    local_detail = client.get('/community/UI传播门测试村')
    local_detail_body = local_detail.get_data(as_text=True)
    assert local_detail.status_code == 200
    assert 'id="detailGroupMessage"' not in local_detail_body

    qweather = _fresh_publication_weather('QWeather')
    monkeypatch.setattr(
        community_service,
        '_load_heat_risk',
        lambda _location: (qweather, {'risk_level': 'high'}, '高风险'),
    )
    publishable = client.get('/community/announce?community=都昌县')
    publishable_body = publishable.get_data(as_text=True)
    assert publishable.status_code == 200
    assert 'id="announceElder"' in publishable_body
    assert '当前天气来源：QWeather。' in publishable_body

    publishable_detail = client.get('/community/UI传播门测试村')
    publishable_detail_body = publishable_detail.get_data(as_text=True)
    assert publishable_detail.status_code == 200
    assert 'id="detailGroupMessage"' in publishable_detail_body
    assert '天气来源：QWeather实况。' in publishable_detail_body
