# -*- coding: utf-8 -*-
"""面向网页用户输入的严格地点解析。

后台同步仍可继续使用 ``normalize_location_name`` 的历史回退语义。网页表单必须
显式识别地点，避免把拼错或县外地点悄悄替换成都昌县。
"""
from dataclasses import dataclass

from flask import current_app

from core.constants import DEFAULT_CITY_LABEL


@dataclass(frozen=True)
class UserLocationResolution:
    """严格地点解析结果。"""

    raw: str
    value: str | None
    valid: bool
    error: str | None = None


def _clean_name(value):
    if not isinstance(value, str):
        return ''
    return value.strip()


def _configured_aliases(extra_names=()):
    """构造县内可接受名称，排除旧配置里的县外城市和原始坐标。"""
    default_city = _clean_name(current_app.config.get('DEFAULT_CITY'))
    default_location = _clean_name(current_app.config.get('DEFAULT_LOCATION'))
    community_names = {
        _clean_name(name)
        for name in (current_app.config.get('COMMUNITY_COORDS_GCJ') or {})
        if _clean_name(name)
    }
    accepted = {
        DEFAULT_CITY_LABEL,
        '都昌',
        '都昌县',
        *community_names,
        *(_clean_name(name) for name in extra_names),
    }
    accepted.discard('')

    city_map = current_app.config.get('CITY_LOCATION_MAP') or {}
    for name, mapped_value in city_map.items():
        clean_name = _clean_name(name)
        clean_value = _clean_name(mapped_value)
        if clean_name in community_names or (
            clean_value and default_location and clean_value == default_location
        ):
            accepted.add(clean_name)

    aliases = {name: name for name in accepted}
    for name in ('都昌', '都昌县', default_city):
        if name and name in accepted:
            aliases[name] = name

    configured_aliases = current_app.config.get('USER_LOCATION_ALIASES') or {}
    for alias, target in configured_aliases.items():
        clean_alias = _clean_name(alias)
        clean_target = _clean_name(target)
        if clean_alias and clean_target in accepted:
            aliases[clean_alias] = aliases.get(clean_target, clean_target)

    # 支持“都昌县 + 已配置村/社区名”的常见完整写法。
    for name in tuple(accepted):
        if name not in {'都昌', '都昌县'} and not name.startswith('都昌县'):
            aliases[f'都昌县{name}'] = aliases.get(name, name)
    return aliases


def get_user_location_options(*, extra_names=()):
    """返回与严格解析器一致的地点候选，避免表单提示县外旧别名。"""
    aliases = _configured_aliases(extra_names)
    preferred = ['都昌', '都昌县']
    options = [name for name in preferred if name in aliases]
    options.extend(sorted(name for name in aliases if name not in options))
    return options


def resolve_user_location(raw_location, *, extra_names=(), default_if_blank=True):
    """严格解析网页地点。

    空值可按调用方要求回到默认县域；非空且未识别的值一律返回错误，不接受
    QWeather ID、经纬度或旧配置中的县外城市。
    """
    raw = _clean_name(raw_location)
    if not raw:
        if default_if_blank:
            return UserLocationResolution('', DEFAULT_CITY_LABEL, True)
        return UserLocationResolution('', None, True)

    if len(raw) > 100:
        return UserLocationResolution(
            raw[:100],
            None,
            False,
            '地点名称过长，请从都昌县或已配置的乡镇、社区中选择。',
        )

    aliases = _configured_aliases(extra_names)
    resolved = aliases.get(raw)
    if resolved:
        return UserLocationResolution(raw, resolved, True)
    return UserLocationResolution(
        raw,
        None,
        False,
        '未找到这个地点。目前仅支持都昌县及已配置的乡镇、社区，请从提示中选择。',
    )
