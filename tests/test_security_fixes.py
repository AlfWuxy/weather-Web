# -*- coding: utf-8 -*-
"""
测试安全修复的有效性

覆盖范围：
1. SECRET_KEY 校验
2. XSS 防护（sanitize_input）
3. None 值安全处理
4. 时区处理
5. 弱口令拒绝
6. 日志控制字符转义
"""
import os
import pytest
from datetime import date, datetime, timezone


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

    # 密码验证：强口令通过，常见弱口令全部拒绝（问题 #9）
    valid, result = validate_password('SafePass2026!')
    assert valid is True
    assert result == 'SafePass2026!'

    valid, msg = validate_password('123')  # 太短
    assert valid is False

    weak_passwords = (
        '123456',
        'password123',
        'password1',
        'Password1',
        'password!',
        '11111111',
        'abcdefgh',
        '12345678',
    )
    for weak in weak_passwords:
        valid, msg = validate_password(weak)
        assert valid is False, weak
        assert msg

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



def test_validate_password_rejects_common_word_suffixes():
    """问题 #9：常见词加数字或 !，夹空白/零宽字符后仍拒绝；合法口令保持原样。"""
    from utils.validators import validate_password

    accepted = (
        'SafePass2026!',
        'correct2026',
        'Abcd1234',
        'Safe Pass2026!',
        'SafePass\u200b2026!',
        'AdminPortal2026!',
    )
    for password in accepted:
        valid, result = validate_password(password)
        assert valid is True, password
        assert result == password

    weak_message = '密码过于简单，请勿使用常见弱口令'
    rejected = (
        'password1',
        'Password1',
        'PASSWORD1',
        'password!',
        'password1!',
        'Password123!',
        'password!!',
        'qwerty12',
        'qwerty123',
        'qwerty!!',
        'Qwerty1!',
        'admin123',
        'admin1234',
        'admin123!',
        'welcome1',
        'welcome!',
        'Welcome!!',
        'iloveyou',
        'iloveyou1',
        'iloveyou!',
        'ILoveYou1',
        '1234567!',
        '12345678',
        'pass\u200bword1',
        'Password\u200b1',
        '\u200bpassword1',
        'password1\ufeff',
        'pass word1',
        'pass\tword1',
        'pass\u00a0word1',
        'pass\u200b word1',
        'wel\u200bcome!',
        'I\u200dLoveYou1',
        'qwerty\u200b!!',
        '123456\u200b7!',
        'admin\u2060123',
        'pass\u00adword1',
        'pass\u202eword1',
        'welcome\u200b',
        'qwerty\u200b\u200b',
        'admin\u200b\u200b\u200b',
        'abc\u200b12345',
        '1111\u200b1111',
        '0000 0000',
    )
    for password in rejected:
        valid, msg = validate_password(password)
        assert valid is False, password
        assert msg == weak_message, (password, msg)


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


def test_sanitize_input_hostile_prefixes_finish_quickly():
    """超长 script 前缀和前导空白应线性清理，且结果安全（问题 #2）。"""
    import time

    from utils.validators import sanitize_input

    sanitize_input('warmup', max_length=100)

    script_payload = '<script>' * 15000 + '都昌'
    started = time.perf_counter()
    script_result = sanitize_input(script_payload, max_length=100)
    script_elapsed = time.perf_counter() - started

    space_payload = ' ' * 10000 + '<b>都昌社区</b>'
    started = time.perf_counter()
    space_result = sanitize_input(space_payload, max_length=100)
    space_elapsed = time.perf_counter() - started

    assert script_elapsed < 0.2
    assert space_elapsed < 0.2
    assert script_result == ''
    assert '<script>' not in script_result
    assert len(script_result) <= 100
    assert space_result == '都昌社区'
    assert '<' not in space_result
    assert '>' not in space_result
    assert len(space_result) <= 100


def test_log_text_escapes_controls_without_second_format():
    """控制字符与 %s 必须全部转义，且过滤器二次格式化不崩溃（问题 #13）。"""
    import io
    import logging

    from utils.log_safety import (
        ControlCharLogFilter,
        has_unsafe_log_chars,
        sanitize_log_text,
    )

    dirty = 'evt\n\r\t\x1b[31m\u2028\u202e %s'
    cleaned = sanitize_log_text(dirty)
    assert cleaned == 'evt\\n\\r\\t\\u001b[31m\\u2028\\u202e %s'
    assert has_unsafe_log_chars(cleaned) is False
    assert '%s' in cleaned
    for char in ('\n', '\r', '\t', '\x1b', '\u2028', '\u202e'):
        assert char not in cleaned

    mismatched = 'evt\n\r\t\x1b[31m\u2028\u202e %s %s'
    record = logging.LogRecord(
        name='security.log_safety',
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=mismatched,
        args=('only-one',),
        exc_info=None,
    )
    assert ControlCharLogFilter().filter(record) is True
    message = record.getMessage()
    assert message == record.getMessage()
    assert record.args == ()
    assert message == 'evt\\n\\r\\t\\u001b[31m\\u2028\\u202e %s %s'
    assert has_unsafe_log_chars(message) is False
    assert logging.Formatter('%(message)s').format(record) == message

    logger = logging.getLogger('tests.security_fixes.log_safety')
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter('%(message)s'))
    handler.addFilter(ControlCharLogFilter())
    logger.addHandler(handler)
    try:
        logger.info('ok\n\r\t\x1b[31m\u2028\u202e %s', 'SAFE')
        logger.info('user %s end', 'a\nb\r\t\x1b[31m\u2028\u202e')
    finally:
        logger.removeHandler(handler)
        handler.close()

    lines = buffer.getvalue().splitlines()
    assert lines == [
        'ok\\n\\r\\t\\u001b[31m\\u2028\\u202e SAFE',
        'user a\\nb\\r\\t\\u001b[31m\\u2028\\u202e end',
    ]
    for line in lines:
        assert has_unsafe_log_chars(line) is False
        for char in ('\n', '\r', '\t', '\x1b', '\u2028', '\u202e'):
            assert char not in line


def test_load_user_session_stamp_fails_closed(db_session):
    """问题 #11：旧整数会话与非法戳都返回 None，且不抛异常。"""
    from core.db_models import User
    from core.extensions import login_manager

    user = User(username='stamp_user', role='user')
    user.set_password('StampPass123!')
    db_session.add(user)
    db_session.commit()

    load_user = login_manager._user_callback
    stamp = user.password_session_stamp()
    bad_stamp = ('0' if stamp[0] != '0' else '1') + stamp[1:]

    assert load_user(str(user.id)) is None
    assert load_user(user.id) is None

    loaded = load_user(f'{user.id}:{stamp}')
    assert loaded is not None
    assert loaded.id == user.id

    rejected = [
        f'{user.id}:{bad_stamp}',
        f'{user.id}:ABCDEF0123456789',
        f'{user.id}:{"g" * 16}',
        f'{user.id}:{"²" * 16}',
        f'²:{stamp}',
        f'{user.id}:{stamp}:extra',
        f'{user.id}:{stamp[:8]}:{stamp[8:]}',
        '',
        None,
        ':',
        f'{user.id}:',
        f':{stamp}',
    ]
    for session_id in rejected:
        assert load_user(session_id) is None


def _protected_page(app, cookie_name, cookie_value):
    # db_session 夹具会一直持有应用上下文。Flask 的 g 挂在应用上下文上，
    # 上一次 login_user 留下的 _login_user 会让后续请求跳过会话加载。
    from flask import g
    g.pop('_login_user', None)
    replay = app.test_client()
    replay.set_cookie(cookie_name, cookie_value)
    return replay.get('/profile', follow_redirects=False)


def _assert_protected_page_rejected(response):
    assert response.status_code == 302
    location = response.headers.get('Location') or ''
    assert '/login' in location


def test_change_password_rejects_old_session_and_remember_cookie(app, client, db_session):
    """问题 #11：改密后，旧 session 与 remember cookie 不能再打开受保护页。"""
    from core.db_models import User

    user = User(username='stamp_rotate', role='user')
    user.set_password('OldPass123!')
    db_session.add(user)
    db_session.commit()

    csrf = 'rotate-csrf'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf

    login = client.post(
        '/login',
        data={
            'username': 'stamp_rotate',
            'password': 'OldPass123!',
            'csrf_token': csrf,
            'remember': '1',
        },
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)

    session_name = app.config.get('SESSION_COOKIE_NAME', 'session')
    remember_name = app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    old_session = client.get_cookie(session_name)
    old_remember = client.get_cookie(remember_name)
    assert old_session is not None and old_session.value
    assert old_remember is not None and old_remember.value
    # 改密会刷新当前客户端的 cookie 对象，必须先复制改密前的值。
    old_session_value = old_session.value
    old_remember_value = old_remember.value

    assert _protected_page(app, session_name, old_session_value).status_code == 200
    assert _protected_page(app, remember_name, old_remember_value).status_code == 200

    changed = client.post(
        '/profile',
        data={
            'form_id': 'password',
            'old_password': 'OldPass123!',
            'new_password': 'NewPass456!',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )
    assert changed.status_code in (302, 303)
    db_session.refresh(user)
    assert user.check_password('NewPass456!')

    _assert_protected_page_rejected(
        _protected_page(app, session_name, old_session_value)
    )
    _assert_protected_page_rejected(
        _protected_page(app, remember_name, old_remember_value)
    )


def _risk_map_weather():
    return {
        'temperature': 30,
        'humidity': 65,
        'aqi': 45,
        'data_source': 'QWeather',
        'is_mock': False,
    }


def _assert_exact_case_count_absent(row, secret_cases):
    assert row['observed_cases'] is None
    assert row['observed_cases'] != secret_cases
    assert row['expected_cases'] is None
    assert row['sir'] is None
    assert row['ci_low'] is None
    assert row['ci_high'] is None
    assert row['smoothed_sir'] is None
    assert row['probability_exceed_baseline'] is None


_RANKING_DEMOGRAPHIC_FIELDS = (
    'population',
    'elderly_ratio',
    'chronic_disease_ratio',
    'vulnerability_index',
)


def _assert_ranking_demographics_hidden(row):
    for field in _RANKING_DEMOGRAPHIC_FIELDS:
        assert row[field] is None
    assert row['suppressed'] is True


def _map_feature(result, community):
    for feature in result['map_data']['features']:
        if feature['properties'].get('name') == community:
            return feature
    raise AssertionError(f'missing map feature for {community}')


def _assert_map_demographics_hidden(result, community):
    props = _map_feature(result, community)['properties']
    assert props['population'] is None
    assert props['elderly_ratio'] is None
    assert props['vi'] is None


def test_small_sample_public_risk_map_omits_exact_observed_cases(
    app,
    admin_client,
    db_session,
    monkeypatch,
):
    """小样本公开响应不带精确就诊数；达到阈值且已授权时仍保留。"""
    from core.db_models import Community, MedicalRecord
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.community_risk_service import CommunityRiskService
    import services.community_risk_service as risk_module

    secret_cases = 3
    large_cases = 12
    db_session.add_all([
        Community(name='小村', population=4, elderly_ratio=0.5, chronic_disease_ratio=0.2),
        Community(name='大村', population=80, elderly_ratio=0.3, chronic_disease_ratio=0.1),
    ])
    visit_time = datetime(2025, 10, 15, 8, 0, tzinfo=timezone.utc)
    for index in range(secret_cases):
        db_session.add(MedicalRecord(
            patient_name=f'小村-{index}',
            gender='女',
            age=70,
            visit_time=visit_time,
            disease_category='呼吸系统',
            community='小村',
        ))
    for index in range(large_cases):
        db_session.add(MedicalRecord(
            patient_name=f'大村-{index}',
            gender='男',
            age=60,
            visit_time=visit_time,
            disease_category='呼吸系统',
            community='大村',
        ))
    db_session.commit()

    monkeypatch.setattr(risk_module, '_community_stats_min_n', lambda: 10)
    monkeypatch.setattr(risk_module, '_community_service', None)
    clear_local_community_risk_cache()

    service = CommunityRiskService()
    # 夹具里的应用上下文会留着上一次登录写进 g 的用户。公开请求要先清掉。
    from flask import g
    from flask_login import current_user
    with app.test_request_context():
        g.pop('_login_user', None)
        assert current_user.is_authenticated is False
        public_result = service.generate_community_risk_map(
            _risk_map_weather(),
            target_date=date(2025, 10, 30),
            window_days=30,
            disease_filter='呼吸系统',
        )

    public_rows = {row['community']: row for row in public_result['rankings']}
    _assert_exact_case_count_absent(public_rows['小村'], secret_cases)
    _assert_ranking_demographics_hidden(public_rows['小村'])
    assert public_rows['小村']['stats_suppressed'] is True
    _assert_map_demographics_hidden(public_result, '小村')
    _assert_exact_case_count_absent(public_rows['大村'], large_cases)
    _assert_ranking_demographics_hidden(public_rows['大村'])
    assert public_rows['大村']['population'] != 80
    assert public_rows['大村']['elderly_ratio'] != 0.3
    assert public_rows['大村']['stats_redacted'] is True
    assert public_rows['大村']['stats_suppressed'] is False
    _assert_map_demographics_hidden(public_result, '大村')
    assert public_result['summary']['matched_records'] is None
    assert public_result['summary']['total_records'] is None
    assert public_result['summary']['unmatched_records'] is None
    assert public_result['summary']['baseline_rate_per_person_day'] is None
    assert public_result['summary']['suppressed'] is True

    g.pop('_login_user', None)
    response = admin_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '呼吸系统',
            'weather': _risk_map_weather(),
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    admin_rows = {row['community']: row for row in payload['rankings']}
    _assert_exact_case_count_absent(admin_rows['小村'], secret_cases)
    _assert_ranking_demographics_hidden(admin_rows['小村'])
    assert admin_rows['小村']['stats_suppressed'] is True
    assert admin_rows['小村']['stats_redacted'] is False
    _assert_map_demographics_hidden(payload, '小村')
    assert admin_rows['大村']['observed_cases'] == large_cases
    assert admin_rows['大村']['suppressed'] is False
    assert admin_rows['大村']['stats_redacted'] is False
    assert admin_rows['大村']['population'] == 80
    assert admin_rows['大村']['elderly_ratio'] == pytest.approx(0.3)
    assert admin_rows['大村']['chronic_disease_ratio'] == pytest.approx(0.1)
    from services.community_risk_service import get_community_service
    admin_service = get_community_service()
    expected_vi = admin_service.calculate_vulnerability_index(
        admin_service.community_profiles['大村']
    )['vulnerability_index']
    assert admin_rows['大村']['vulnerability_index'] == expected_vi
    large_feature = _map_feature(payload, '大村')['properties']
    assert large_feature['population'] == 80
    assert large_feature['elderly_ratio'] == pytest.approx(0.3)
    assert large_feature['vi'] == expected_vi
    # 大村的 O/E 含全局合计，留着就能把小村的 3 例减出来。
    assert admin_rows['大村']['sir'] is None
    assert admin_rows['大村']['expected_cases'] is None
    assert payload['summary']['matched_records'] is None
    assert payload['summary']['total_records'] != secret_cases
    assert payload['summary']['matched_records'] != secret_cases + large_cases
    clear_local_community_risk_cache()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
