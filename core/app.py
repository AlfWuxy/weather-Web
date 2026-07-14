# -*- coding: utf-8 -*-
"""
天气变化与社区居民健康风险预测系统
主应用入口（蓝图注册）
"""
import logging
import os
from pathlib import Path
from dotenv import load_dotenv
import click
from flask import Flask, current_app
from sqlalchemy import inspect

from core.auth import register_user_loader
from core.config import configure_app
from core.constants import CHRONIC_OPTIONS, DEFAULT_CITY_LABEL, GUEST_ID_PREFIX, RISK_TAG_OPTIONS
from core.extensions import db, init_extensions, login_manager
from core.hooks import register_hooks
from core.db_models import (
    AlertDelivery,
    AuditLog,
    ApiToken,
    Community,
    CommunityDaily,
    CoolingResource,
    DailyStatus,
    Debrief,
    FamilyMember,
    FamilyMemberProfile,
    ForecastCache,
    HealthDiary,
    HealthRiskAssessment,
    MedicalRecord,
    MedicationReminder,
    LocationCache,
    Notification,
    Pair,
    PairLink,
    User,
    UsageEvent,
    WeatherAlert,
    WeatherCache,
    WeatherData
)
from utils.parsers import parse_int
from services import init_services

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 加载环境变量（.env）
load_dotenv()

# 创建Flask应用
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DB_BOOTSTRAPPED = False


def create_app(register_blueprints=True):
    """Application factory."""
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / 'templates'),
        static_folder=str(PROJECT_ROOT / 'static')
    )
    configure_app(app, logger)
    init_extensions(app)
    register_user_loader(login_manager)
    register_hooks(app)
    if register_blueprints:
        _register_blueprints(app)
    register_cli(app)
    init_services(app)
    return app


def register_blueprints(app):
    """Register all application blueprints."""
    from blueprints.public import bp as public_bp
    from blueprints.user import bp as user_bp
    from blueprints.analysis import bp as analysis_bp
    from blueprints.health import bp as health_bp
    from blueprints.admin import bp as admin_bp
    from blueprints.tools import bp as tools_bp
    from blueprints.api import bp as api_bp
    from blueprints.mp_api import bp as mp_api_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(mp_api_bp)


_register_blueprints = register_blueprints


def register_cli(app):
    """Register CLI commands."""
    @app.cli.command('init-db')
    def init_db_command():
        """Initialize the database schema and default data."""
        run_migrations(app)
        init_db(app)
        click.echo('Database initialized.')


# ======================== 初始化 ========================


def run_migrations(app):
    """Run Alembic migrations."""
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        logger.error("Alembic is not installed; cannot run migrations: %s", exc)
        raise
    config_path = PROJECT_ROOT / 'alembic.ini'
    alembic_config = Config(str(config_path))
    db_url = app.config.get('SQLALCHEMY_DATABASE_URI')
    if db_url:
        alembic_config.set_main_option('sqlalchemy.url', db_url)
    alembic_config.set_main_option('script_location', str(PROJECT_ROOT / 'migrations'))

    # 历史首个迁移依赖早期业务表，无法直接作用于完全空白的数据库。
    # 空库使用当前模型创建完整结构后标记到最新版本；已有数据库仍走标准升级链。
    with app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
        is_empty_database = not (table_names - {'alembic_version'})
        if is_empty_database:
            db.create_all()
            command.stamp(alembic_config, 'head', purge=True)
            return

    command.upgrade(alembic_config, 'head')


def init_db(app=None):
    """初始化数据库"""
    # 数据库结构变更请通过 Alembic 迁移：alembic upgrade head
    global _DB_BOOTSTRAPPED
    target_app = app if app is not None else current_app._get_current_object()
    with target_app.app_context():
        # 创建默认管理员（仅在配置了环境变量时）
        admin_username = target_app.config.get('DEFAULT_ADMIN_USERNAME') or os.getenv('DEFAULT_ADMIN_USERNAME')
        admin_password = target_app.config.get('DEFAULT_ADMIN_PASSWORD') or os.getenv('DEFAULT_ADMIN_PASSWORD')
        admin_email = target_app.config.get('DEFAULT_ADMIN_EMAIL') or os.getenv(
            'DEFAULT_ADMIN_EMAIL',
            'admin@example.com'
        )

        if admin_username and admin_password:
            admin = User.query.filter_by(username=admin_username).first()
            if not admin:
                admin = User(
                    username=admin_username,
                    email=admin_email,
                    role='admin'
                )
                admin.set_password(admin_password)
                db.session.add(admin)
                db.session.commit()
            logger.info("默认管理员创建成功：%s", admin_username)
        else:
            logger.warning("未设置默认管理员账号/密码，已跳过创建。")
    _DB_BOOTSTRAPPED = True


def ensure_db_ready(app=None):
    """Ensure database schema is ready for runtime queries."""
    if _DB_BOOTSTRAPPED:
        return
    target_app = app if app is not None else current_app._get_current_object()
    with target_app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
        if 'users' not in table_names:
            logger.warning("数据库尚未初始化，请运行 flask init-db 或 alembic upgrade head。")
            return
    init_db(app)


def main(app_instance=None):
    """Run the Flask development server."""
    target_app = app_instance if app_instance is not None else create_app()
    ensure_db_ready(target_app)
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    port = parse_int(os.getenv('FLASK_PORT'), default=5000)
    target_app.run(debug=target_app.config.get('DEBUG', False), host=host, port=port)


if __name__ == '__main__':
    main()
