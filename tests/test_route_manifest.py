# -*- coding: utf-8 -*-
"""路由清单锁：任何已有 URL、HTTP 方法或 endpoint 名都不允许在重构中消失。

新增路由后，运行下面的命令刷新清单并随 PR 一起提交：
    UPDATE_ROUTE_MANIFEST=1 python -m pytest tests/test_route_manifest.py
"""
import json
import os
from pathlib import Path

MANIFEST_PATH = Path(__file__).parent / 'fixtures' / 'route_manifest.json'


def _current_manifest(app):
    routes = []
    for rule in app.url_map.iter_rules():
        methods = sorted(rule.methods - {'HEAD', 'OPTIONS'})
        routes.append({'rule': rule.rule, 'methods': methods, 'endpoint': rule.endpoint})
    return sorted(routes, key=lambda item: (item['rule'], item['endpoint']))


def test_route_manifest_is_stable(app):
    current = _current_manifest(app)
    if os.environ.get('UPDATE_ROUTE_MANIFEST') == '1':
        MANIFEST_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    expected = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))

    current_keys = {(r['rule'], tuple(r['methods']), r['endpoint']) for r in current}
    expected_keys = {(r['rule'], tuple(r['methods']), r['endpoint']) for r in expected}
    missing = sorted(expected_keys - current_keys)
    added = sorted(current_keys - expected_keys)
    assert not missing, f'路由消失或改变（会破坏书签、url_for 或客户端）：{missing}'
    assert not added, f'新增了路由，请刷新清单：{added}'
