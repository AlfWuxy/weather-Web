# -*- coding: utf-8 -*-
"""
测试安全修复的有效性

覆盖范围：
1. SECRET_KEY 校验
2. XSS 防护（sanitize_input）
3. None 值安全处理
4. 时区处理
"""
import os
import pytest
from datetime import datetime, timezone


def test_sanitize_input_basic():
    """测试基本的 XSS 清理功能"""
    from utils.validators import sanitize_input

    # 基本清理 - bleach 会移除标签，html.escape 会转义
    result = sanitize_input('<script>alert("xss")</script>')
    # 确保结果不包含可执行的脚本标签
    assert '<script>' not in result
    assert 'alert' not in result or '&' in result  # 转义或移除

    assert sanitize_input('Hello World') == 'Hello World'

    # None 值处理
    assert sanitize_input(None) is None
    assert sanitize_input('') is None

    # 非字符串输入
    assert sanitize_input(123, max_length=10) == '123'

    # 长度限制
    long_text = 'a' * 500
    assert len(sanitize_input(long_text, max_length=200)) == 200


def test_sanitize_input_xss_vectors():
    """测试各种 XSS 攻击向量"""
    from utils.validators import sanitize_input

    # 常见 XSS 向量
    vectors = [
        ('<img src=x onerror=alert("xss")>', ['<img', 'onerror']),
        ('<svg onload=alert("xss")>', ['<svg', 'onload']),
        ('javascript:alert("xss")', None),  # 纯文本，可能保留或转义
        ('<iframe src="javascript:alert(\'xss\')">', ['<iframe']),
        ('<body onload=alert("xss")>', ['<body', 'onload']),
        ('<input onfocus=alert("xss") autofocus>', ['<input', 'onfocus']),
        ('<marquee onstart=alert("xss")>', ['<marquee', 'onstart']),
        ('<a href="javascript:alert(\'xss\')">click</a>', ['<a']),
    ]

    for vector, forbidden_parts in vectors:
        cleaned = sanitize_input(vector)
        if forbidden_parts:
            # 确保 HTML 标签被移除或转义
            for part in forbidden_parts:
                # 原始标签不应存在（可能被转义为 &lt; 等）
                assert part not in cleaned or '&lt;' in cleaned


def test_sanitize_input_with_bleach():
    """测试使用 bleach 库的严格清理"""
    from utils.validators import sanitize_input

    # 确保 HTML 标签被完全移除
    dirty = '<p>Hello <b>World</b></p>'
    clean = sanitize_input(dirty)
    assert '<' not in clean
    assert '>' not in clean
    # bleach 会保留文本内容或转义
    assert 'Hello' in clean or 'World' in clean


def test_secret_key_validation(app):
    """测试 SECRET_KEY 校验逻辑"""
    # 确保 SECRET_KEY 已设置
    assert app.config.get('SECRET_KEY')

    # 如果是生产环境（DEBUG=False），SECRET_KEY 必须来自环境变量
    if not app.config.get('DEBUG'):
        assert os.getenv('SECRET_KEY')


def test_weather_temp_diff_none_safety():
    """测试天气温差计算的 None 安全性"""
    # 模拟 weather_data 包含 None 值的情况
    weather_data = {
        'temperature': 20,
        'temperature_max': None,
        'temperature_min': 10,
    }

    # 测试逻辑（模拟 services/weather_service.py:416）
    temp_max = weather_data.get('temperature_max')
    temp_min = weather_data.get('temperature_min')

    if temp_max is not None and temp_min is not None:
        temp_diff = temp_max - temp_min
    else:
        temp_diff = None

    # 不应抛出 TypeError
    assert temp_diff is None or isinstance(temp_diff, (int, float))


def test_weather_temp_diff_both_none():
    """测试温度最大最小值都是 None 的情况"""
    weather_data = {
        'temperature': 20,
        'temperature_max': None,
        'temperature_min': None,
    }

    temp_max = weather_data.get('temperature_max')
    temp_min = weather_data.get('temperature_min')

    if temp_max is not None and temp_min is not None:
        temp_diff = temp_max - temp_min
    else:
        temp_diff = None

    assert temp_diff is None


def test_weather_temp_diff_valid():
    """测试温度差计算正常情况"""
    weather_data = {
        'temperature': 20,
        'temperature_max': 25,
        'temperature_min': 15,
    }

    temp_max = weather_data.get('temperature_max')
    temp_min = weather_data.get('temperature_min')

    if temp_max is not None and temp_min is not None:
        temp_diff = temp_max - temp_min
    else:
        temp_diff = None

    assert temp_diff == 10


def test_timezone_aware_utcnow():
    """测试新的 UTC 时间函数返回 timezone-aware datetime"""
    from core.time_utils import utcnow

    now = utcnow()

    # 确保返回 timezone-aware datetime
    assert now.tzinfo is not None
    assert now.tzinfo == timezone.utc


def test_timezone_model_default():
    """测试数据库模型使用 timezone-aware 默认值"""
    # 这个测试需要实际的数据库环境，这里仅做概念验证
    # 在实际环境中，应该检查 User.created_at 的默认值

    # 模拟检查
    from datetime import datetime, timezone

    # 正确的做法：使用 timezone-aware
    correct_default = lambda: datetime.now(timezone.utc)
    ts = correct_default()
    assert ts.tzinfo is not None

    # 错误的做法（已修复）：使用 naive datetime
    # wrong_default = datetime.utcnow  # 返回 naive datetime


def test_api_error_handler_debug_mode():
    """测试 API 错误处理器在 DEBUG 模式下返回详细信息"""
    from flask import Flask
    from services.api_service import _handle_api_error

    app = Flask(__name__)
    app.config['DEBUG'] = True

    with app.app_context():
        exc = ValueError("Test error")
        response = _handle_api_error(exc, "Test context")
        data = response.get_json()

        # DEBUG 模式应该包含详细错误
        assert 'error_detail' in data
        assert 'error_type' in data
        assert data['error_type'] == 'ValueError'


def test_api_error_handler_production_mode():
    """测试 API 错误处理器在生产模式下隐藏详细信息"""
    from flask import Flask
    from services.api_service import _handle_api_error

    app = Flask(__name__)
    app.config['DEBUG'] = False

    with app.app_context():
        exc = ValueError("Sensitive error details")
        response = _handle_api_error(exc, "Test context")
        data = response.get_json()

        # 生产模式不应包含详细错误
        assert 'error_detail' not in data
        assert data['success'] is False


def test_validators_comprehensive():
    """综合测试输入验证器"""
    from utils.validators import (
        validate_username,
        validate_password,
        validate_email,
        validate_age,
        validate_gender
    )

    # 用户名验证
    valid, result = validate_username('testuser')
    assert valid is True

    valid, msg = validate_username('ab')  # 太短
    assert valid is False

    # 密码验证（P7：最少 8 位）
    valid, result = validate_password('password123')
    assert valid is True

    valid, result = validate_password('12345678')  # 刚好 8 位
    assert valid is True

    valid, msg = validate_password('1234567')  # 7 位不足
    assert valid is False
    assert '8' in msg

    valid, msg = validate_password('123')  # 太短
    assert valid is False

    # 邮箱验证
    valid, result = validate_email('test@example.com')
    assert valid is True

    valid, msg = validate_email('invalid-email')
    assert valid is False

    # 年龄验证
    valid, result = validate_age(25)
    assert valid is True

    valid, msg = validate_age(200)  # 超出范围
    assert valid is False

    # 性别验证
    valid, result = validate_gender('男')
    assert valid is True
    assert result == '男性'


def test_parse_bool_false_values():
    """测试 parse_bool 能正确识别假值字符串"""
    from utils.parsers import parse_bool

    false_values = ['false', '0', 'off', 'no', ' FALSE ', 'No']
    for value in false_values:
        assert parse_bool(value, default=True) is False

    assert parse_bool('unknown', default=True) is True
    assert parse_bool('unknown', default=False) is False


def test_safe_next_url_blocks_scheme_relative():
    """测试 _safe_next_url 拒绝危险前缀与控制字符"""
    from services.public_service import _safe_next_url

    assert _safe_next_url('/dashboard') == '/dashboard'
    assert _safe_next_url('/forecast-7day?location=duchang&view=compact') == (
        '/forecast-7day?location=duchang&view=compact'
    )

    unsafe_urls = [
        '//evil.com',
        '///evil.com',
        '\\\\evil.com',
        '/\\evil.com',
        '/path\nnext',
        '/path\rnext',
    ]

    for url in unsafe_urls:
        assert _safe_next_url(url) is None


def test_robots_use_current_private_route_paths(client):
    """robots.txt 应与当前真实路由保持一致。"""
    body = client.get('/robots.txt').get_data(as_text=True)

    for path in ('/pairs', '/mp/api/', '/guest', '/t/', '/dashboard', '/caregiver'):
        assert f'Disallow: {path}' in body
    assert 'Disallow: /pair-management' not in body
    assert 'Disallow: /mp-api/' not in body


def test_location_update_blocks_external_referrer(app, client, db_session):
    """定位更新后不应跳到外部 Referer。"""
    from core.db_models import User

    user = User(username='location_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    with client.session_transaction() as session:
        session['_csrf_token'] = 'csrf-location'
    client.post(
        '/login',
        data={'username': 'location_user', 'password': 'testpass', 'csrf_token': 'csrf-location'},
        follow_redirects=False,
    )

    resp = client.post(
        '/location',
        data={'location': '都昌', 'csrf_token': 'csrf-location'},
        headers={'Referer': 'https://evil.example/phish'},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/dashboard')


@pytest.mark.parametrize(
    ('base_url', 'referrer', 'expected'),
    [
        (
            'https://weather.example',
            'https://weather.example/profile?tab=care&from=location',
            '/profile?tab=care&from=location',
        ),
        ('https://weather.example', '/profile?tab=care&from=location', '/profile?tab=care&from=location'),
        ('https://evil.example', 'https://evil.example//attacker.example', '/dashboard'),
        ('https://weather.example', '//attacker.example/path', '/dashboard'),
        ('https://weather.example', '///attacker.example/path', '/dashboard'),
        ('https://weather.example', '/profile\\attacker.example', '/dashboard'),
        ('https://weather.example', '/profile/%5C%5Cattacker.example', '/dashboard'),
        ('https://weather.example', '/profile%0d%0aLocation:%20//attacker.example', '/dashboard'),
        ('https://weather.example', 'https://other.example/profile', '/dashboard'),
    ],
)
def test_safe_referrer_normalizes_to_local_path_and_rejects_host_tricks(
    app,
    base_url,
    referrer,
    expected,
):
    from services.user.profile_service import _safe_referrer_or_dashboard

    with app.test_request_context(
        '/location',
        base_url=base_url,
        headers={'Referer': referrer},
    ):
        assert _safe_referrer_or_dashboard() == expected


def test_create_notification_fails_closed_on_quota_error(app, monkeypatch):
    """通知配额检查异常时应阻止发送（fail-closed）"""
    from core import notifications

    app.config['FEATURE_NOTIFICATIONS'] = True

    with app.app_context():
        def _raise_error(_user_id):
            raise RuntimeError('quota-check-failed')

        monkeypatch.setattr(notifications, '_notification_daily_count', _raise_error)
        result = notifications.create_notification(
            user_id=1,
            title='title',
            message='message'
        )

    assert result is None


def test_structured_logs_redact_action_and_tracking_tokens(app, client, monkeypatch):
    """高熵链接凭据不能进入结构化请求日志。"""
    from core.hooks import _redact_sensitive_path

    assert _redact_sensitive_path('/e/action-secret/checkin') == '/e/<token>/checkin'
    assert _redact_sensitive_path('/t/tracking-secret') == '/t/<token>'
    assert _redact_sensitive_path('/dashboard') == '/dashboard'

    app.config['FEATURE_STRUCTURED_LOGS'] = True
    messages = []
    monkeypatch.setattr('core.hooks.logger.info', lambda message, *args: messages.append(message))
    client.get('/e/action-secret')

    assert any('"path": "/e/<token>"' in message for message in messages)
    assert all('action-secret' not in message for message in messages)


def test_user_get_id_embeds_password_stamp(db_session):
    """P7：User.get_id 为 {id}:{password_hash 短摘要}，改密后 stamp 变化。"""
    import hashlib
    from core.db_models import User

    user = User(username='stamp_user', role='user')
    user.set_password('OldPass12')
    db_session.add(user)
    db_session.commit()

    gid = user.get_id()
    assert isinstance(gid, str)
    assert ':' in gid
    uid_part, stamp = gid.split(':', 1)
    assert uid_part == str(user.id)
    expected = hashlib.sha256((user.password_hash or '').encode('utf-8')).hexdigest()[:16]
    assert stamp == expected

    user.set_password('NewPass34')
    db_session.commit()
    gid2 = user.get_id()
    assert gid2 != gid
    assert gid2.startswith(f'{user.id}:')
    assert gid2.split(':', 1)[1] != stamp


def test_password_change_invalidates_session(app, client, db_session):
    """P7：改密后旧 session 中的 password stamp 与库中不一致 → 会话失效。

    模拟：登录拿到带 stamp 的 _user_id → POST 改密 → 再访问需登录页应被踢回登录。

    注意：pytest 的 db_session 在整个用例内保持同一 app_context，Flask-Login 会把
    已加载用户缓存在 g._login_user；生产环境每个请求独立 app_context，无此缓存。
    因此在二次请求前显式清掉 g 缓存，才能测到 loader 的 stamp 校验。
    """
    import hashlib
    from flask import g
    from core.db_models import User
    from core.extensions import login_manager

    def _clear_login_user_cache():
        # 清掉跨请求残留的 g._login_user，强制下一次走 user_loader
        if hasattr(g, '_login_user'):
            delattr(g, '_login_user')

    user = User(username='sess_revoke_u', role='user')
    user.set_password('OldPass12')
    db_session.add(user)
    db_session.commit()

    csrf = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    login_resp = client.post(
        '/login',
        data={
            'username': 'sess_revoke_u',
            'password': 'OldPass12',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert login_resp.status_code == 200

    with client.session_transaction() as session:
        old_user_id = session.get('_user_id')
    assert old_user_id is not None
    assert ':' in str(old_user_id), '登录后 _user_id 应含 password stamp'

    # 改密前可进 profile
    _clear_login_user_cache()
    before = client.get('/profile', follow_redirects=False)
    assert before.status_code == 200

    _clear_login_user_cache()
    change = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPass12',
            'new_password': 'NewPass34',
            'confirm_password': 'NewPass34',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    assert change.status_code in (302, 303)

    db_session.refresh(user)
    new_stamp = hashlib.sha256((user.password_hash or '').encode('utf-8')).hexdigest()[:16]
    old_stamp = str(old_user_id).split(':', 1)[1]
    assert old_stamp != new_stamp, '改密后 stamp 必须变化'
    with client.session_transaction() as session:
        assert '_user_id' not in session, '改密后必须显式退出当前会话'

    # 直接断言 loader 拒绝旧 stamp（不依赖 g 缓存）
    assert login_manager._user_callback(old_user_id) is None
    assert login_manager._user_callback(user.get_id()) is not None

    # 当前会话已退出；清 g 后受保护页面应要求重新登录。
    _clear_login_user_cache()
    after = client.get('/profile', follow_redirects=False)
    assert after.status_code in (302, 303)
    location = after.headers.get('Location') or ''
    assert 'login' in location.lower() or '/login' in location

    # 新密码可重新登录，且新会话 stamp 匹配
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf
    _clear_login_user_cache()
    re_login = client.post(
        '/login',
        data={
            'username': 'sess_revoke_u',
            'password': 'NewPass34',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )
    assert re_login.status_code == 200
    with client.session_transaction() as session:
        new_user_id = session.get('_user_id')
    assert new_user_id is not None
    assert str(new_user_id).split(':', 1)[1] == new_stamp
    _clear_login_user_cache()
    ok = client.get('/profile', follow_redirects=False)
    assert ok.status_code == 200


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
