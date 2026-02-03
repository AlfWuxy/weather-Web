# -*- coding: utf-8 -*-
"""
输入验证工具 - 从 app.py 提取

所有函数保持原有行为不变，仅做代码位置移动。
"""
import logging
import re

logger = logging.getLogger(__name__)


def validate_username(username):
    """验证用户名：3-25字符，只能包含字母、数字、下划线和中文"""
    if not username or not isinstance(username, str):
        return False, '用户名不能为空'
    username = username.strip()
    if len(username) < 3 or len(username) > 25:
        return False, '用户名长度需在3-25字符之间'
    if not re.match(r'^[\w\u4e00-\u9fa5]+$', username):
        return False, '用户名只能包含字母、数字、下划线和中文'
    return True, username


def validate_password(password):
    """验证密码：至少6位"""
    if not password or not isinstance(password, str):
        return False, '密码不能为空'
    if len(password) < 6:
        return False, '密码长度至少6位'
    if len(password) > 100:
        return False, '密码长度不能超过100位'
    return True, password


def validate_email(email):
    """验证邮箱格式"""
    if not email:
        return True, None  # 邮箱可选
    email = email.strip()
    if len(email) > 120:
        return False, '邮箱长度不能超过120字符'
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_pattern, email):
        return False, '邮箱格式不正确'
    return True, email


def validate_age(age):
    """验证年龄：1-150岁"""
    if not age:
        return True, None  # 年龄可选
    try:
        age = int(age)
        if age < 1 or age > 150:
            return False, '年龄需在1-150之间'
        return True, age
    except (ValueError, TypeError):
        return False, '年龄必须是数字'


def validate_gender(gender):
    """验证性别"""
    if not gender:
        return True, None
    gender = gender.strip()
    gender_map = {
        '男': '男性',
        '男性': '男性',
        '女': '女性',
        '女性': '女性',
        '其他': '其他',
        '未知': '未知'
    }
    if gender not in gender_map:
        return False, '性别选择不正确'
    return True, gender_map[gender]


def sanitize_input(text, max_length=200):
    """清理输入文本，防止XSS

    使用 bleach 库进行严格的 HTML 清理：
    - 移除所有 HTML 标签
    - 禁止 javascript: data: vbscript: 等危险协议
    - 剥离所有事件属性 (onclick, onerror 等)
    - 保留长度限制与非字符串输入处理
    """
    if not text:
        return None
    if not isinstance(text, str):
        return str(text)[:max_length]

    # 使用 bleach 进行严格清理（不允许任何标签）
    try:
        import bleach
        # 不允许任何标签，不允许任何属性，不允许任何协议
        cleaned = bleach.clean(
            text,
            tags=[],           # 不允许任何 HTML 标签
            attributes={},     # 不允许任何属性
            protocols=[],      # 不允许任何协议（阻止 javascript: data: 等）
            strip=True         # 剥离标签而非转义
        )
    except ImportError:
        logger.warning("bleach 未安装，已使用降级清理逻辑。")
        # 兜底方案：使用 html.escape + 正则清理（不如 bleach 严格但可用）
        import html
        cleaned = html.escape(text)
        # 额外移除可能的标签残留
        cleaned = re.sub(r'<[^>]+>', '', cleaned)

    return cleaned.strip()[:max_length]
