"""客户端埋点数据的统一边界。"""
import json
from flask.json.provider import DefaultJSONProvider

from core.resource_budget import setting


class SafeJSONProvider(DefaultJSONProvider):
    def loads(self, value, **kwargs):
        # 极深 JSON 在字段验证前即可触发解释器递归限制，按解析失败处理。
        try:
            return super().loads(value, **kwargs)
        except RecursionError as exc:
            raise ValueError('JSON nesting too deep') from exc


def validate_event_meta(meta):
    if meta is None:
        return None
    if not isinstance(meta, (dict, list)):
        raise ValueError('invalid_meta')
    max_depth = setting('EVENT_META_MAX_DEPTH', 5)
    max_fields = setting('EVENT_META_MAX_FIELDS', 64)
    stack = [(meta, 1)]
    fields = 0
    while stack:
        value, depth = stack.pop()
        if depth > max_depth:
            raise ValueError('meta_too_deep')
        if isinstance(value, (dict, list)):
            children = value.values() if isinstance(value, dict) else value
            fields += len(value)
            if fields > max_fields:
                raise ValueError('meta_too_many_fields')
            stack.extend((child, depth + 1) for child in children)
    try:
        raw = json.dumps(meta, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('invalid_meta') from exc
    if len(raw) > setting('EVENT_META_MAX_BYTES', 2048):
        raise ValueError('meta_too_large')
    return meta
