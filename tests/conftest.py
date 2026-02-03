# -*- coding: utf-8 -*-
"""
Pytest 配置文件 - 测试隔离与环境设置

解决测试数据库路径问题：
- 确保测试使用临时数据库，避免污染生产数据
- 在导入 app 之前设置环境变量
"""
import os
import tempfile
import pytest
from pathlib import Path


@pytest.fixture(scope='session', autouse=True)
def setup_test_environment():
    """
    自动设置测试环境变量（在所有测试之前执行）

    优先级：显式环境变量 > conftest 默认值
    """
    # 创建临时数据库文件
    temp_db = tempfile.NamedTemporaryFile(
        prefix='test_health_weather_',
        suffix='.db',
        delete=False
    )
    temp_db_path = temp_db.name
    temp_db.close()

    # 设置测试环境变量（仅在未设置时）
    test_env = {
        'DATABASE_URI': f'sqlite:///{temp_db_path}',
        'SECRET_KEY': 'test-secret-key-for-pytest',
        'DEBUG': 'true',
        'QWEATHER_KEY': '',  # 测试中禁用外部 API
        'AMAP_KEY': '',
        'SILICONFLOW_API_KEY': '',
        'DEMO_MODE': '1',  # 启用演示模式，使用 mock 数据
    }

    for key, value in test_env.items():
        if key not in os.environ:
            os.environ[key] = value

    yield temp_db_path

    # 清理临时数据库
    try:
        Path(temp_db_path).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture(scope='function')
def app():
    """
    提供 Flask app 实例（每个测试函数独立）

    依赖 setup_test_environment 确保环境变量已设置
    """
    # 延迟导入，确保环境变量已设置
    from core.app import create_app

    app = create_app()
    app.config['TESTING'] = True

    # 确保使用内存存储（避免速率限制干扰测试）
    app.config['RATE_LIMIT_STORAGE_URI'] = 'memory://'

    return app


@pytest.fixture(scope='function')
def client(app):
    """提供测试客户端"""
    return app.test_client()


@pytest.fixture(scope='function')
def db_session(app):
    """
    提供数据库会话（每个测试后自动回滚）

    用法:
        def test_example(db_session):
            user = User(username='test')
            db_session.add(user)
            db_session.commit()
    """
    from core.extensions import db

    with app.app_context():
        # 创建所有表
        db.create_all()

        yield db.session

        # 测试结束后清理
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope='function')
def authenticated_client(client, db_session):
    """
    提供已登录的测试客户端

    用法:
        def test_protected_route(authenticated_client):
            response = authenticated_client.get('/user/dashboard')
            assert response.status_code == 200
    """
    from core.db_models import User

    # 创建测试用户
    user = User(username='testuser', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    # 登录
    csrf_token = 'test-csrf-token'
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf_token
    client.post('/login', data={
        'username': 'testuser',
        'password': 'testpass',
        'csrf_token': csrf_token
    }, follow_redirects=True)

    yield client

    # 登出
    client.get('/logout', follow_redirects=True)
