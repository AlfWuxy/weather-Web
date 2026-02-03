#!/usr/bin/env python3
"""重置 admin 密码"""
import os
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

DEFAULT_DB_FILE = Path('/opt/case-weather/storage/health_weather.db')


def _load_database_uri():
    env_uri = (os.getenv('DATABASE_URI') or '').strip()
    if env_uri:
        return env_uri
    env_path = Path(__file__).resolve().parents[2] / '.env'
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


db_uri = _load_database_uri()
db_file = _parse_sqlite_path(db_uri) or str(DEFAULT_DB_FILE)
db_path = Path(db_file)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
new_hash = generate_password_hash('Admin123')
cursor.execute('UPDATE users SET password_hash = ? WHERE username = ?', (new_hash, 'admin'))
conn.commit()
print(f'已重置 admin 密码, 更新行数: {cursor.rowcount}')
conn.close()
