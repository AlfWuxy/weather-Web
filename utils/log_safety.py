# -*- coding: utf-8 -*-
"""日志文本安全处理。

把会拆行、伪造日志或藏起内容的控制字符、格式字符、代理项和
行/段分隔符换成可见转义，避免日志注入。
"""
import logging
import unicodedata

_UNSAFE_CATEGORIES = frozenset({'Cc', 'Cf', 'Cs', 'Zl', 'Zp'})
_FILTER_NAME = 'utils.log_safety.ControlCharLogFilter'


def has_unsafe_log_chars(value: str) -> bool:
    """字符串里是否含有不适合直接写入日志的字符。"""
    if not isinstance(value, str) or not value:
        return False
    for char in value:
        if char in '\r\n\t\x1b' or unicodedata.category(char) in _UNSAFE_CATEGORIES:
            return True
    return False


def _visible_log_piece(char: str) -> str:
    """把单个危险字符换成可见转义；普通字符原样返回。"""
    if char == '\r':
        return '\\r'
    if char == '\n':
        return '\\n'
    if char == '\t':
        return '\\t'
    if unicodedata.category(char) not in _UNSAFE_CATEGORIES:
        return char
    code = ord(char)
    if code > 0xFFFF:
        return '\\U%08x' % code
    return '\\u%04x' % code


def sanitize_log_text(value, max_length=200) -> str:
    """把日志文本里的危险字符转成可见形式，并截断到 max_length。

    None 返回空串。``\\r`` ``\\n`` ``\\t`` 变成两个可见字符；
    其余 Cc/Cf/Cs/Zl/Zp 变成 ``\\uXXXX`` 或 ``\\UXXXXXXXX``。
    """
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    try:
        limit = int(max_length)
    except (TypeError, ValueError):
        limit = 200
    if limit <= 0 or not value:
        return ''

    parts = []
    size = 0
    for char in value:
        piece = _visible_log_piece(char)
        remain = limit - size
        if len(piece) <= remain:
            parts.append(piece)
            size += len(piece)
            if size == limit:
                break
            continue
        parts.append(piece[:remain])
        break
    return ''.join(parts)


class ControlCharLogFilter(logging.Filter):
    """把日志记录格式化后的消息洗成单行可见文本，不丢弃记录。"""

    def __init__(self, max_length=2000):
        super().__init__(_FILTER_NAME)
        try:
            limit = int(max_length)
        except (TypeError, ValueError):
            limit = 2000
        self.max_length = 2000 if limit < 0 else limit

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            raw = record.msg
            message = raw if isinstance(raw, str) else str(raw)
        sanitized = sanitize_log_text(message, max_length=self.max_length)
        record.msg = sanitized
        record.args = ()
        if hasattr(record, 'message'):
            record.message = sanitized
        return True


def install_control_char_filter(target_logger):
    """给 logger 装上控制字符过滤器。重复调用不会重复添加。"""
    if not isinstance(target_logger, logging.Logger):
        raise TypeError('target_logger 必须是 logging.Logger')
    for existing in target_logger.filters:
        if isinstance(existing, ControlCharLogFilter):
            return existing
    log_filter = ControlCharLogFilter()
    target_logger.addFilter(log_filter)
    if not any(item is log_filter for item in target_logger.filters):
        target_logger.filters.append(log_filter)
    return log_filter
