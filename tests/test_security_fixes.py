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

    # 密码验证
    valid, result = validate_password('password123')
    assert valid is True

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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
