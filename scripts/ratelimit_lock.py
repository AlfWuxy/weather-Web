# -*- coding: utf-8 -*-
"""限流锁：对比两份代码在同一请求序列下的限流行为。

行为锁录制时会关闭限流，这个脚本专门补上：把各类限流阈值调低，
按固定顺序交替请求兼容地址与 v1 地址，记录每次的状态码（含 429）
以及限流存储里的全部计数键和计数值。

用法:
    python scripts/ratelimit_lock.py record --root <代码目录> --out base.json
    python scripts/ratelimit_lock.py record --root . --out head.json
    python scripts/ratelimit_lock.py diff base.json head.json
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

# 统一用小时窗口：固定窗口从首次请求起算，即使 CI 机器很慢，整个序列也不会跨窗口
LIMIT_ENV = {
    'RATE_LIMITS': '6 per hour',
    'RATE_LIMIT_WEATHER': '4 per hour',
    'RATE_LIMIT_ML': '3 per hour',
    'RATE_LIMIT_FORECAST': '3 per hour',
    'RATE_LIMIT_CHRONIC': '3 per hour',
    'RATE_LIMIT_AI': '3 per hour',
}

# (方法, 兼容地址, v1 地址, 请求体, 轮数)：每轮先兼容地址再 v1 地址
SEQUENCE = [
    ('GET', '/api/weather/current', '/api/v1/weather/current', None, 6),
    ('GET', '/api/weather/nowcast', '/api/v1/weather/nowcast', None, 6),
    ('GET', '/api/community/list', '/api/v1/community/list', None, 8),
    ('GET', '/api/ml/status', '/api/v1/ml/status', None, 8),
    ('POST', '/api/ai/ask', '/api/v1/ai/ask', {'question': 'x'}, 5),
    ('POST', '/api/ml/predict', '/api/v1/ml/predict', {'age': 70, 'gender': '女'}, 5),
    ('POST', '/api/forecast/7day', '/api/v1/forecast/7day', {}, 5),
    ('POST', '/api/ml/predict-community', '/api/v1/ml/predict-community', {'community': '牛家垄周村'}, 5),
    ('POST', '/api/forecast/daily', '/api/v1/forecast/daily', {}, 5),
    ('POST', '/api/chronic/individual', '/api/v1/chronic/individual', {'age': 70}, 5),
    ('POST', '/api/alert/comprehensive', '/api/v1/alert/comprehensive', {}, 5),
    ('POST', '/api/dlnm/risk', '/api/v1/dlnm/risk', {'temperature': 36}, 8),
]


def _block_network():
    """禁止一切外网：外部接口统一走兜底分支，请求耗时稳定。返回被拦截次数的计数器。"""
    import socket
    blocked = {'count': 0}

    def _refuse(*_args, **_kwargs):
        blocked['count'] += 1
        raise OSError('ratelimit_lock: network disabled')
    socket.socket.connect = _refuse
    socket.create_connection = _refuse
    return blocked


def _record_in_process(root, out):
    import logging
    import time
    import warnings
    logging.disable(logging.CRITICAL)
    warnings.simplefilter('ignore')
    root = os.path.abspath(root)
    os.chdir(root)
    sys.path.insert(0, root)
    db_path = os.path.join(tempfile.mkdtemp(prefix='ratelimit_lock_'), 'rl.db')
    os.environ.update({
        'DATABASE_URI': f'sqlite:///{db_path}', 'SECRET_KEY': 'ratelimit-lock-secret-key-0123456789abcdef',
        'DEBUG': 'true', 'DEMO_MODE': '1', 'QWEATHER_KEY': '', 'RATE_LIMIT_STORAGE_URI': 'memory://',
        'REDIS_URL': '', 'SILICONFLOW_API_KEY': '', 'AMAP_KEY': '',
    })
    os.environ.update(LIMIT_ENV)
    blocked = _block_network()
    started = time.monotonic()

    from core.app import create_app
    from core.db_models import User
    from core.extensions import db, limiter

    # 不冻结时间：限流的内存存储按真实时间过期计数键；小时窗口保证序列不会跨窗口
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        db.create_all()
        user = User(username='rl_user', role='user')
        user.set_password('rl-pass')
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'rl-csrf'
    client.post('/login', data={'username': 'rl_user', 'password': 'rl-pass', 'csrf_token': 'rl-csrf'})
    with client.session_transaction() as sess:
        assert sess.get('_user_id'), '限流锁预备登录失败'

    statuses = []
    for method, compat, v1, payload, rounds in SEQUENCE:
        for _ in range(rounds):
            for path in (compat, v1):
                if method == 'GET':
                    resp = client.get(path)
                else:
                    resp = client.post(path, json=payload, headers={'X-CSRF-Token': 'rl-csrf'})
                statuses.append(f'{method} {path} {resp.status_code}')
    counters = sorted(f'{key}={value}' for key, value in limiter._storage.storage.items())

    with open(out, 'w', encoding='utf-8') as fh:
        json.dump({'statuses': statuses, 'counters': counters}, fh, ensure_ascii=False, indent=1)
    throttled = sum(1 for item in statuses if item.endswith(' 429'))
    elapsed = time.monotonic() - started
    print(f'recorded {len(statuses)} requests ({throttled} throttled), {len(counters)} counters, '
          f'{blocked["count"]} outbound connections blocked, {elapsed:.1f}s -> {out}')


def record(root, out):
    """在独立子进程中录制，避免进程级状态互相影响。"""
    subprocess.run([sys.executable, os.path.abspath(__file__), '_record', '--root', root,
                    '--out', os.path.abspath(out)], check=True, env=dict(os.environ, PYTHONHASHSEED='0'))


def diff(base_path, head_path):
    with open(base_path, encoding='utf-8') as fh:
        base = json.load(fh)
    with open(head_path, encoding='utf-8') as fh:
        head = json.load(fh)
    failed = False
    for field in ('statuses', 'counters'):
        if base[field] != head[field]:
            failed = True
            print(f'[变化] {field}')
            for a, b in zip(base[field], head[field]):
                if a != b:
                    print(f'  base: {a}\n  head: {b}')
            if len(base[field]) != len(head[field]):
                print(f'  数量: {len(base[field])} -> {len(head[field])}')
    print('限流行为一致' if not failed else '限流行为不一致')
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('record', '_record'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--root', default='.')
        cmd.add_argument('--out', required=True)
    dif = sub.add_parser('diff')
    dif.add_argument('base')
    dif.add_argument('head')
    args = parser.parse_args()
    if args.cmd == 'record':
        record(args.root, args.out)
        return 0
    if args.cmd == '_record':
        _record_in_process(args.root, args.out)
        return 0
    return diff(args.base, args.head)


if __name__ == '__main__':
    sys.exit(main())
