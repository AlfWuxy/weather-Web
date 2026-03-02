# -*- coding: utf-8 -*-
"""
工具模块 - 从 app.py 提取的公共函数

本模块提供向后兼容的导入路径：
    from utils import parse_int, parse_float, validate_username, ...

注意：这是 P0 重构的一部分，所有函数保持原有行为不变。
"""

from utils.validators import (
    validate_username,
    validate_password,
    validate_email,
    validate_age,
    validate_gender,
    sanitize_input,
)

from utils.parsers import (
    parse_int,
    parse_float,
    parse_bool,
    parse_date,
    parse_age,
    get_age_group,
    get_age_group_name,
    safe_json_loads,
    compact_dict,
    json_or_none,
)

__all__ = [
    # validators
    'validate_username',
    'validate_password',
    'validate_email',
    'validate_age',
    'validate_gender',
    'sanitize_input',
    # parsers
    'parse_int',
    'parse_float',
    'parse_bool',
    'parse_date',
    'parse_age',
    'get_age_group',
    'get_age_group_name',
    'safe_json_loads',
    'compact_dict',
    'json_or_none',
]
