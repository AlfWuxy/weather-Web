# -*- coding: utf-8 -*-
"""
数据解析工具 - 从 app.py 和 services 提取

统一的解析函数，消除重复代码。
所有函数保持原有行为不变。
"""
import json
from datetime import datetime


def parse_int(value, default=None):
    """安全转换为整数"""
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_float(value, default=None):
    """安全转换为浮点数"""
    if value in (None, ''):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_bool(value, default=False):
    """安全转换为布尔值"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('1', 'true', 'yes', 'on'):
            return True
        if normalized in ('0', 'false', 'no', 'off'):
            return False
        return default
    return default


def parse_date(value):
    """安全解析日期（YYYY-MM-DD）"""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def parse_age(age_str):
    """
    解析年龄字符串
    
    支持格式：
    - "45岁" -> 45
    - "45" -> 45
    - "6月" -> 0 (婴儿)
    - "10天" -> 0 (婴儿)
    
    此函数统一了多处重复的 parse_age 实现。
    """
    if age_str is None:
        return None
    age_str = str(age_str).strip()
    if not age_str:
        return None
    
    if '岁' in age_str:
        try:
            return float(age_str.replace('岁', '').strip())
        except (ValueError, TypeError):
            return None
    elif '月' in age_str or '天' in age_str:
        return 0  # 婴儿算0岁
    else:
        try:
            return float(age_str)
        except (ValueError, TypeError):
            return None


def get_age_group(age):
    """
    获取年龄段编码
    
    返回数值编码，用于模型输入：
    - 0: 0-17岁 (未成年)
    - 1: 18-39岁 (青年)
    - 2: 40-59岁 (中年)
    - 3: 60-79岁 (老年)
    - 4: 80岁以上 (高龄)
    """
    if age is None:
        return 2  # 默认中年
    try:
        age = float(age)
    except (TypeError, ValueError):
        return 2
    
    if age < 18:
        return 0
    elif age < 40:
        return 1
    elif age < 60:
        return 2
    elif age < 80:
        return 3
    else:
        return 4


def get_age_group_name(age):
    """
    获取年龄段名称
    
    返回中文描述：
    - "0-17岁(未成年)"
    - "18-39岁(青年)"
    - "40-59岁(中年)"
    - "60-79岁(老年)"
    - "80岁以上(高龄)"
    """
    names = [
        '0-17岁(未成年)',
        '18-39岁(青年)',
        '40-59岁(中年)',
        '60-79岁(老年)',
        '80岁以上(高龄)'
    ]
    group = get_age_group(age)
    return names[group] if 0 <= group < len(names) else '未知'


def safe_json_loads(value, default=None):
    """安全解析JSON"""
    if default is None:
        default = {}
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def compact_dict(data):
    """剔除空值字段"""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if v not in (None, '', [], {})}


def json_or_none(value):
    """空JSON返回None，否则序列化"""
    if value in (None, '', [], {}):
        return None
    return json.dumps(value, ensure_ascii=False)
