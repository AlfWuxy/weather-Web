#!/usr/bin/env python3
"""测试所有页面"""
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

# Ensure minimal config for app import during tests
os.environ.setdefault('DEBUG', 'true')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-manual-tests-123456')
os.environ.setdefault('PAIR_TOKEN_PEPPER', 'test-pair-token-pepper-1234567890')
os.environ.setdefault('DATABASE_URI', f"sqlite:///{ROOT_DIR / 'tmp' / 'manual_test.db'}")

from app import app

# 公开页面
public_pages = ['/', '/login', '/register', '/guest']

# 需要登录的页面
auth_pages = [
    '/admin', '/admin/users', '/admin/communities', '/admin/records',
    '/user/dashboard', '/user/profile',
    '/health-assessment', '/health-diary', '/medication-reminders',
    '/chronic-risk', '/community-risk',
    '/7day-forecast', '/ml-prediction', '/ai-question',
    '/analysis/history', '/analysis/heatmap'
]

print("=== 公开页面 ===")
with app.test_client() as client:
    for p in public_pages:
        try:
            r = client.get(p)
            status = '✅' if r.status_code == 200 else '❌'
            print(f'{status} {p}: {r.status_code}')
        except Exception as e:
            print(f'❌ {p}: ERROR - {e}')

print("\n=== 需要登录的页面（未登录时应重定向） ===")
with app.test_client() as client:
    for p in auth_pages:
        try:
            r = client.get(p)
            # 302 或 403 都是期望的
            status = '✅' if r.status_code in (302, 403) else '⚠️'
            print(f'{status} {p}: {r.status_code}')
        except Exception as e:
            print(f'❌ {p}: ERROR - {e}')

print("\n=== 登录后测试（admin） ===")
with app.test_client() as client:
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'  # admin user id
        sess['_csrf_token'] = 'test-token'
    
    test_pages = ['/admin', '/user/dashboard']
    for p in test_pages:
        try:
            r = client.get(p)
            status = '✅' if r.status_code == 200 else '❌'
            print(f'{status} {p}: {r.status_code}')
        except Exception as e:
            print(f'❌ {p}: ERROR - {e}')
