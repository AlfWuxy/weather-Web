#!/usr/bin/env python3
"""测试所有页面"""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.manual


def main():
    sys.path.insert(0, str(ROOT_DIR))

    failures = []
    with TemporaryDirectory(prefix='case-weather-pages-') as temp_dir:
        # 手工冒烟测试始终使用独立临时库，避免读写开发或生产数据库。
        database_path = Path(temp_dir) / 'manual_test.db'
        os.environ['DEBUG'] = 'true'
        os.environ['SECRET_KEY'] = 'test-secret-key-for-manual-tests-123456'
        os.environ['PAIR_TOKEN_PEPPER'] = 'test-pair-token-pepper-1234567890'
        os.environ['DATABASE_URI'] = f"sqlite:///{database_path}"

        from app import app
        from core.db_models import User
        from core.extensions import db

        app.config['TESTING'] = True
        with app.app_context():
            db.create_all()
            admin = User(username='manual-page-admin', role='admin')
            admin.set_password('manual-test-password')
            db.session.add(admin)
            db.session.commit()
            admin_id = admin.id

        # /guest 的正常行为是创建游客会话后重定向到仪表板。
        public_pages = [
            ('/', {200}),
            ('/login', {200}),
            ('/register', {200}),
            ('/guest', {302}),
        ]

        auth_pages = [
            '/admin', '/admin/users', '/admin/communities', '/admin/records',
            '/dashboard', '/profile',
            '/health-assessment', '/health-diary', '/medication-reminders',
            '/chronic-risk', '/community-risk',
            '/forecast-7day', '/ml-prediction', '/ai-qa',
            '/analysis/history', '/analysis/heatmap', '/analysis/lag',
        ]

        # 页面巡检不访问真实第三方服务，确保结果可重复且不消耗 API 配额。
        with patch(
            'requests.sessions.Session.request',
            side_effect=RuntimeError('manual smoke 禁止真实网络请求'),
        ):
            print("=== 公开页面 ===")
            with app.test_client() as client:
                for path, expected_codes in public_pages:
                    try:
                        response = client.get(path)
                        passed = response.status_code in expected_codes
                        status = '✅' if passed else '❌'
                        print(f'{status} {path}: {response.status_code}')
                        if not passed:
                            failures.append(f'{path}: {response.status_code}')
                    except Exception as exc:
                        failures.append(f'{path}: {exc}')
                        print(f'❌ {path}: ERROR - {exc}')

            print("\n=== 需要登录的页面（未登录时应拒绝） ===")
            with app.test_client() as client:
                for path in auth_pages:
                    try:
                        response = client.get(path)
                        passed = response.status_code in (302, 401, 403)
                        status = '✅' if passed else '❌'
                        print(f'{status} {path}: {response.status_code}')
                        if not passed:
                            failures.append(f'未登录 {path}: {response.status_code}')
                    except Exception as exc:
                        failures.append(f'未登录 {path}: {exc}')
                        print(f'❌ {path}: ERROR - {exc}')

            print("\n=== 登录后测试（admin） ===")
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess['_user_id'] = str(admin_id)
                    sess['_fresh'] = True
                    sess['_csrf_token'] = 'test-token'

                for path in auth_pages:
                    try:
                        response = client.get(path)
                        passed = response.status_code == 200
                        status = '✅' if passed else '❌'
                        print(f'{status} {path}: {response.status_code}')
                        if not passed:
                            failures.append(f'已登录 {path}: {response.status_code}')
                    except Exception as exc:
                        failures.append(f'已登录 {path}: {exc}')
                        print(f'❌ {path}: ERROR - {exc}')

        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    if failures:
        print(f"\n页面冒烟测试失败：{len(failures)} 项")
        return 1

    print("\n页面冒烟测试通过")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
