# -*- coding: utf-8 -*-
"""Flask extensions initialization."""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
# SQLAlchemy 连接池配置（在 core/config.py 的 configure_app 中设置）
# app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
#     'pool_pre_ping': True,  # 连接前先 ping，避免使用过期连接
#     'pool_size': 5,         # 连接池大小
#     'pool_recycle': 3600,   # 连接回收时间（秒）
#     'max_overflow': 10      # 超出 pool_size 后允许的最大连接数
# }

login_manager = LoginManager()
limiter = Limiter(get_remote_address)


def init_extensions(app):
    """Bind extensions to the Flask app."""
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'public.login'
    login_manager.login_message = '请先登录'

    rate_limits = app.config.get('RATE_LIMITS', '200 per minute')
    rate_storage = app.config.get('RATE_LIMIT_STORAGE_URI', 'memory://')
    limiter.default_limits = [rate_limits]
    limiter.storage_uri = rate_storage
    limiter.init_app(app)
