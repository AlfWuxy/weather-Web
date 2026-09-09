# -*- coding: utf-8 -*-
"""
输入验证工具 - 从 app.py 提取

所有函数保持原有行为不变，仅做代码位置移动。
"""
import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


class _PlainTextExtractor(HTMLParser):
    """只保留 HTML 中可见的纯文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag.lower() in {'script', 'style'}:
            self._blocked_depth += 1

    def handle_startendtag(self, tag, attrs):
        del tag, attrs

    def handle_endtag(self, tag):
        if tag.lower() in {'script', 'style'} and self._blocked_depth > 0:
            self._blocked_depth -= 1

    def handle_data(self, data):
        if self._blocked_depth == 0:
            self._chunks.append(data)

    def text(self):
        return ''.join(self._chunks)


def _strip_html_to_text(text):
    """bleach 不可用时剥离标签，返回未做 HTML 实体转义的纯文本。"""
    cleaned = text
    # 最多三轮可处理实体编码后的标签，同时保留“血压 < 120”这类比较文本。
    for _ in range(3):
        parser = _PlainTextExtractor()
        parser.feed(cleaned)
        parser.close()
        parsed = parser.text()
        if parsed == cleaned:
            break
        cleaned = parsed
    return cleaned


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


def mask_patient_name(name):
    """病历列表脱敏：保留姓/首字，其余 *。

    管理端列表默认不展示完整姓名；明细查看可另开（若产品需要）。
    """
    text = (name or '').strip() if isinstance(name, str) else ''
    if not text:
        return '—'
    if len(text) == 1:
        return '*'
    # 中文姓名通常 2–4 字；长名字最多遮 8 位避免撑版
    star_count = min(len(text) - 1, 8)
    return text[0] + ('*' * star_count)


def mask_phi_text(value, keep=0, max_stars=12):
    """通用短文本 PHI 遮罩（诊断/主诉等列表用）。"""
    text = (value or '').strip() if isinstance(value, str) else ''
    if not text:
        return '—'
    if keep <= 0:
        return '***'
    if len(text) <= keep:
        return text[0] + '*' if len(text) > 1 else '*'
    star_count = min(len(text) - keep, max_stars)
    return text[:keep] + ('*' * star_count)


def validate_password(password):
    """验证密码：至少 8 位（提高弱口令基线，配合 scrypt 哈希）。"""
    if not password or not isinstance(password, str):
        return False, '密码不能为空'
    if len(password) < 8:
        return False, '密码长度至少8位'
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
    if age is None or (isinstance(age, str) and not age.strip()):
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
    - 额外移除 script/style 标签及其内容（防止保留脚本文本片段）
    - 禁止 javascript: data: vbscript: 等危险协议
    - 剥离所有事件属性 (onclick, onerror 等)
    - 保留长度限制与非字符串输入处理
    """
    if not text:
        return None
    if not isinstance(text, str):
        return str(text)[:max_length]

    # 先移除 <script>/<style> 及其内容。bleach.strip=True 会移除标签但保留内容；
    # 对于脚本/样式内容，保留文本没有意义且容易在其它上下文被误用。
    try:
        text = re.sub(
            r'(?is)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>',
            '',
            text,
        )
    except Exception:
        # 正则失败时继续走 bleach 兜底即可
        pass

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
        # 返回纯文本，让模板统一完成一次上下文转义，避免实体被重复转义。
        cleaned = _strip_html_to_text(text)

    return cleaned.strip()[:max_length]
