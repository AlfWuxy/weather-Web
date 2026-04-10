#!/usr/bin/env python3
"""重置 admin 密码"""
import argparse
import getpass
import os
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash


def _load_database_uri():
    env_uri = (os.getenv('DATABASE_URI') or '').strip()
    if env_uri:
        return env_uri
    env_path = Path(__file__).resolve().parents[1] / '.env'
    if not env_path.exists():
        return ''
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if not line.startswith('DATABASE_URI='):
            continue
        value = line.split('=', 1)[1].strip()
        value = value.split('#', 1)[0].strip()
        if value and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        return value
    return ''


def _parse_sqlite_path(uri):
    if not uri:
        return None
    if uri.startswith('sqlite:////'):
        path = f"/{uri[len('sqlite:////'):]}"
    elif uri.startswith('sqlite:///'):
        path = uri[len('sqlite:///'):]
    else:
        return None
    if '?' in path:
        path = path.split('?', 1)[0]
    return path or None


def _resolve_new_password(cli_password):
    if cli_password:
        return cli_password

    env_password = (os.getenv('NEW_ADMIN_PASSWORD') or '').strip()
    if env_password:
        return env_password

    if not os.isatty(0):
        raise RuntimeError('请通过 --password 或 NEW_ADMIN_PASSWORD 提供新密码。')

    first = getpass.getpass('请输入新的 admin 密码: ')
    second = getpass.getpass('请再次输入新的 admin 密码: ')
    if not first:
        raise RuntimeError('新密码不能为空。')
    if first != second:
        raise RuntimeError('两次输入的密码不一致。')
    return first


def main():
    parser = argparse.ArgumentParser(description='重置 admin 密码')
    parser.add_argument('--password', help='直接传入新密码。更推荐使用 NEW_ADMIN_PASSWORD 环境变量或交互输入。')
    parser.add_argument('--db-path', help='显式指定 sqlite 数据库路径。')
    args = parser.parse_args()

    new_password = _resolve_new_password(args.password)
    if len(new_password) < 8:
        raise RuntimeError('新密码长度至少为 8 个字符。')

    db_uri = _load_database_uri()
    db_file = args.db_path or _parse_sqlite_path(db_uri) or str(Path(__file__).resolve().parents[1] / 'storage' / 'health_weather.db')
    db_path = Path(db_file)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        new_hash = generate_password_hash(new_password)
        cursor.execute('UPDATE users SET password_hash = ? WHERE username = ?', (new_hash, 'admin'))
        conn.commit()
        print(f'已重置 admin 密码, 更新行数: {cursor.rowcount}')
    finally:
        conn.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
