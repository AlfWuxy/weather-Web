# -*- coding: utf-8 -*-
"""
输入验证工具 - 从 app.py 提取

用户名、邮箱、年龄、性别保持原有行为。
密码校验和输入清理按安全要求收紧。
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


_WEAK_PASSWORDS = frozenset({
    '123456',
    '12345678',
    '123456789',
    '1234567890',
    'password',
    'password123',
    'qwerty',
    'qwerty123',
    '111111',
    '11111111',
    '00000000',
    'admin',
    'admin123',
    'abc12345',
    'iloveyou',
    'welcome1',
    '654321',
    '88888888',
})

# 整段去掉空白和零宽字符后，若只是这些词再加数字或 !，仍视为同一条弱口令。
_WEAK_PASSWORD_STEMS = (
    'password',
    '123456',
    'qwerty',
    'admin',
    'welcome',
    'iloveyou',
)

# 常见零宽 / 不可见格式字符。空白另由 str.isspace() 去掉。
_PASSWORD_INVISIBLE = frozenset((
    '\u00ad',  # 软连字符
    '\u180e',  # 蒙古文元音分隔符
    '\u200b',  # 零宽空格
    '\u200c',  # 零宽非连接符
    '\u200d',  # 零宽连接符
    '\u200e',  # 从左到右标记
    '\u200f',  # 从右到左标记
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
    '\u2060',  # 词连接符
    '\u2066', '\u2067', '\u2068', '\u2069',
    '\ufeff',  # 零宽不换行空格
))


def _password_min_length():
    """从当前应用配置读取最短密码长度，读不到或非法时用 8。"""
    default = 8
    try:
        from flask import current_app, has_app_context
        if not has_app_context():
            return default
        raw = current_app.config.get('PASSWORD_MIN_LENGTH', default)
        if raw is None:
            return default
        min_len = int(raw)
    except Exception:
        return default
    if min_len < 1 or min_len > 100:
        return default
    return min_len


def _password_weak_key(password):
    """去掉空白和常见零宽字符后再 casefold，只用于弱口令比较。"""
    kept = []
    for char in password:
        if char.isspace() or char in _PASSWORD_INVISIBLE:
            continue
        kept.append(char)
    return ''.join(kept).casefold()


def _is_weak_password(password):
    """常见弱口令，或这些词后面只追加了数字和 !。"""
    key = _password_weak_key(password)
    if key in _WEAK_PASSWORDS:
        return True
    for stem in _WEAK_PASSWORD_STEMS:
        if not key.startswith(stem):
            continue
        suffix = key[len(stem):]
        if all(char.isdigit() or char == '!' for char in suffix):
            return True
    return False


def _password_categories(password):
    """统计大写、小写（含无大小写字母）、数字、特殊字符四类里出现了几类。"""
    has_upper = False
    has_lower = False
    has_digit = False
    has_special = False
    for char in password:
        if char.isupper():
            has_upper = True
        elif char.islower() or char.isalpha():
            has_lower = True
        elif char.isdigit():
            has_digit = True
        else:
            has_special = True
    return int(has_upper) + int(has_lower) + int(has_digit) + int(has_special)


def validate_password(password):
    """验证密码强度。

    非空字符串，首尾不能有空白，长度在配置的最短长度和 100 之间。
    拒绝常见弱口令（比较前去掉空白和常见零宽字符；常见词加数字或 ! 同样拒绝）、
    单字符重复、纯数字和纯字母，并至少包含两类字符。
    """
    if not isinstance(password, str) or not password:
        return False, '密码不能为空'
    if password != password.strip():
        return False, '密码首尾不能包含空白'
    min_len = _password_min_length()
    if len(password) < min_len:
        return False, '密码长度至少%d位' % min_len
    if len(password) > 100:
        return False, '密码长度不能超过100位'
    if _is_weak_password(password):
        return False, '密码过于简单，请勿使用常见弱口令'
    if len(set(password)) == 1:
        return False, '密码不能为单一字符重复'
    if password.isdigit():
        return False, '密码不能为纯数字'
    if password.isalpha():
        return False, '密码不能为纯字母'
    if _password_categories(password) < 2:
        return False, '密码需至少包含大写字母、小写字母、数字和特殊字符中的两类'
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


def _name_at(text, index, name):
    """text[index:] 是否以 ASCII 标签名 name 开头（忽略大小写，不改变长度）。"""
    end = index + len(name)
    if end > len(text):
        return False
    for offset, expected in enumerate(name):
        lowered = text[index + offset].lower()
        if lowered != expected:
            return False
    return True


def _match_dangerous_open(text, start):
    """若 text[start] 是 <script...> 或 <style...>，返回 (name, 内容起点)。"""
    length = len(text)
    if start >= length or text[start] != '<':
        return None
    index = start + 1
    while index < length and text[index].isspace():
        index += 1
    if index >= length or text[index] in '/!':
        return None
    name = None
    for candidate in ('script', 'style'):
        if _name_at(text, index, candidate):
            name = candidate
            break
    if name is None:
        return None
    end = index + len(name)
    if end < length and (not text[end].isspace()) and text[end] not in '/>':
        return None
    gt = text.find('>', end)
    if gt == -1:
        # 标签名已经匹配，后面却没有 '>'。从这里到结尾不可能再组成标签，
        # 交给调用方整段保留，避免每个 '<' 都重新扫到末尾。
        return (None, None)
    return name, gt + 1


def _find_dangerous_close(text, name, start):
    """从 start 起线性查找 </name>，返回闭合标签之后的位置；没有则返回 None。"""
    length = len(text)
    index = start
    while index < length:
        lt = text.find('<', index)
        if lt == -1:
            return None
        cursor = lt + 1
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length or text[cursor] != '/':
            index = lt + 1
            continue
        cursor += 1
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if not _name_at(text, cursor, name):
            index = lt + 1
            continue
        boundary = cursor + len(name)
        if boundary < length and (not text[boundary].isspace()) and text[boundary] != '>':
            index = lt + 1
            continue
        gt = text.find('>', boundary)
        if gt == -1:
            return None
        return gt + 1
    return None


def _strip_dangerous_blocks(text):
    """单向扫描删除 script/style 块。未闭合时从开始标签一直删到末尾。"""
    if not text or '<' not in text:
        return text or ''
    parts = []
    length = len(text)
    index = 0
    while index < length:
        lt = text.find('<', index)
        if lt == -1:
            parts.append(text[index:])
            break
        if lt > index:
            parts.append(text[index:lt])
        matched = _match_dangerous_open(text, lt)
        if matched is None:
            parts.append('<')
            index = lt + 1
            continue
        name, content_start = matched
        if name is None:
            parts.append(text[lt:])
            break
        close_end = _find_dangerous_close(text, name, content_start)
        if close_end is None:
            break
        index = close_end
    return ''.join(parts)


def _strip_tags_linear(text):
    """线性去掉 <...> 标签。没有 > 时保留剩余文本，交给 html.escape。"""
    if not text or '<' not in text:
        return text or ''
    parts = []
    length = len(text)
    index = 0
    while index < length:
        lt = text.find('<', index)
        if lt == -1:
            parts.append(text[index:])
            break
        if lt > index:
            parts.append(text[index:lt])
        gt = text.find('>', lt + 1)
        if gt == -1:
            parts.append(text[lt:])
            break
        index = gt + 1
    return ''.join(parts)


def _clean_html(text):
    """优先用 bleach 去标签；没有 bleach 时线性去标签再转义。"""
    try:
        import bleach
    except ImportError:
        import html
        logger.warning("bleach 未安装，已使用降级清理逻辑。")
        return html.escape(_strip_tags_linear(text))
    return bleach.clean(
        text,
        tags=[],
        attributes={},
        protocols=[],
        strip=True,
    )


def sanitize_input(text, max_length=200):
    """清理输入文本，防止 XSS。

    空串、None 和非字符串空值返回 None。字符串先去掉左侧空白，
    因此大段前导空格不会把正文截掉。script/style 块用线性扫描删除，
    再交给 bleach（或线性去标签）处理。清洗后的空结果返回空串。
    """
    try:
        max_length = int(max_length)
    except (TypeError, ValueError):
        max_length = 200
    if max_length < 0:
        max_length = 0

    if not isinstance(text, str):
        if not text:
            return None
        return str(text)[:max_length]

    text = text.lstrip()
    if text == '':
        return None

    raw_cap = min(max(max_length * 8, 1024), 4096)
    text = text[:raw_cap]
    encoded = text.encode('utf-8')
    if len(encoded) > raw_cap:
        text = encoded[:raw_cap].decode('utf-8', errors='ignore')

    text = _strip_dangerous_blocks(text)
    cleaned = _clean_html(text)
    return cleaned.strip()[:max_length]
