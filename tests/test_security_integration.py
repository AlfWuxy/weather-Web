"""跨修复组验证迁移顺序和后台任务的预算归属。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect


def test_security_upgrade_from_0010_preserves_users(monkeypatch, tmp_path):
    from tests.test_database_bootstrap import _create_test_app
    from core.app import run_migrations
    from core.extensions import db

    app = _create_test_app(monkeypatch, tmp_path / 'security-upgrade.db')
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'migrations'))
    config.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        # 模拟完整的旧模型库，移除本轮新增表和字段。
        with db.engine.begin() as conn:
            conn.exec_driver_sql('DROP TABLE resource_leases')
            conn.exec_driver_sql('DROP TABLE resource_budgets')
            conn.exec_driver_sql('ALTER TABLE users DROP COLUMN authorized_community')
            conn.exec_driver_sql('ALTER TABLE api_tokens DROP COLUMN expires_at')
            conn.exec_driver_sql("INSERT INTO users (username, password_hash) VALUES ('preserved-owner', 'unused')")
            conn.exec_driver_sql("INSERT INTO api_tokens (user_id, token_hash) VALUES (1, 'synthetic-token-hash')")
        command.stamp(config, '0010_action_token_hardening')
        run_migrations(app)
        with db.engine.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() == '0012_security_merge'
            assert conn.exec_driver_sql('SELECT username FROM users').scalar() == 'preserved-owner'
            assert conn.exec_driver_sql('SELECT authorized_community FROM users').scalar() is None
            assert conn.exec_driver_sql('SELECT expires_at FROM api_tokens').scalar() is not None
            assert {'resource_budgets', 'resource_leases'} <= set(inspect(conn).get_table_names())
        db.session.remove()
        db.engine.dispose()


def test_push_geocoding_uses_each_pair_owner(app, db_session, monkeypatch):
    from core.db_models import Pair, User
    from services.push import dispatch
    users = [User(username=f'budget-owner-{i}', password_hash='unused', push_enabled=True,
                  wxpusher_uid=f'UID_{i}') for i in range(2)]
    db_session.add_all(users)
    db_session.flush()
    for index, user in enumerate(users):
        db_session.add(Pair(caregiver_id=user.id, community_code='合成地点',
                            location_query=f'合成地点{index}', elder_code=f'budget-elder-{index}',
                            short_code=f'budget-short-{index}', status='active'))
    db_session.commit()
    seen = []
    def resolve(query, *, user_id):
        seen.append(user_id)
        return {'provider': 'fallback'}
    monkeypatch.setattr(dispatch, 'resolve_location', resolve)
    dispatch.dispatch_alerts()
    assert seen == [user.id for user in users]
