"""擦除存量短码明文、放宽短码列可空，并补齐用量事件复合索引。

Revision ID: 0013_daybreak_security_hardening
Revises: 0012_security_merge
"""
import hashlib
import os

from alembic import op
import sqlalchemy as sa


# 恰好 32 字符，以适配 alembic_version.version_num VARCHAR(32)。
revision = '0013_daybreak_security_hardening'
down_revision = '0012_security_merge'
branch_labels = None
depends_on = None

_USAGE_INDEX = 'ix_usage_events_user_created_at'
_USAGE_INDEX_COLUMNS = ['user_id', 'created_at']


def _column(inspector, table_name, column_name):
    if table_name not in inspector.get_table_names():
        return None
    for column in inspector.get_columns(table_name):
        if column.get('name') == column_name:
            return column
    return None


def _repo_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


_EXAMPLE_PEPPERS = ('your-pair-token-pepper-here', 'change-me-min-32-chars')


def _normalized_env(key, default=None):
    """与 core.config._normalized_env_value 一致：缺省或空白都走 default。"""
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _debug_enabled():
    """与 utils.parsers.parse_bool(os.getenv('DEBUG'), default=False) 一致。"""
    value = os.getenv('DEBUG')
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in ('1', 'true', 'yes', 'on'):
        return True
    if normalized in ('0', 'false', 'no', 'off'):
        return False
    return False


def _read_persisted_pair_token_pepper():
    """只读 Flask 默认 instance_path 下的 pair_token_pepper.txt，不创建、不覆盖。

    仅当 DEBUG 已打开，且 PAIR_TOKEN_PEPPER 与稳定 SECRET_KEY 都为空时调用，
    对应 core.config.configure_app 的最后一档。读失败则中止：运行时会另生成
    随机 pepper，迁移无法证明那是同一个值。
    """
    pepper_path = os.path.join(_repo_root(), 'instance', 'pair_token_pepper.txt')
    try:
        with open(pepper_path, 'r', encoding='utf-8') as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ''
    except OSError as exc:
        raise RuntimeError(f'读取 pepper 文件失败: {exc}') from exc


def _migration_pair_token_pepper():
    """与 core.config.configure_app 选择 PAIR_TOKEN_PEPPER 的顺序一致。

    生产环境（DEBUG 未打开）会在 validate_production_config 里直接失败：
    pepper 为空或仍是示例值时，运行时不会改用 SECRET_KEY，也不会读文件。
    这种情况下返回空串，让 upgrade 在仍有未哈希明文时中止。

    DEBUG 打开、且环境变量 pepper 为空时：
    1. 稳定 SECRET_KEY（环境变量非空）。运行时此时不读文件，也不再生成。
    2. 否则只读 instance/pair_token_pepper.txt。

    运行时最后才会生成新 pepper。那个随机值无法在迁移里重现，所以这里不生成。
    返回空串且仍有未哈希明文时，upgrade 抛 RuntimeError，不用别的 pepper 哈希后再擦明文。
    """
    pepper = _normalized_env('PAIR_TOKEN_PEPPER', '')
    if not _debug_enabled():
        if not pepper or pepper in _EXAMPLE_PEPPERS:
            return ''
    if pepper:
        return pepper
    secret_key = _normalized_env('SECRET_KEY', None)
    if secret_key:
        return secret_key
    return _read_persisted_pair_token_pepper()


def _backfill_short_code_hash(bind, table_name, pepper):
    """只补空哈希。已有 short_code_hash 的行绝不覆盖。"""
    if not pepper:
        return
    inspector = sa.inspect(bind)
    if _column(inspector, table_name, 'short_code') is None:
        return
    if _column(inspector, table_name, 'short_code_hash') is None:
        return
    table = sa.table(
        table_name,
        sa.column('id', sa.Integer),
        sa.column('short_code', sa.String),
        sa.column('short_code_hash', sa.String),
    )
    rows = bind.execute(
        sa.select(table.c.id, table.c.short_code).where(
            table.c.short_code.is_not(None),
            sa.or_(table.c.short_code_hash.is_(None), table.c.short_code_hash == ''),
        )
    ).fetchall()
    for row in rows:
        code = row.short_code
        if not code:
            continue
        digest = hashlib.sha256(f"{code}{pepper}".encode('utf-8')).hexdigest()
        bind.execute(
            table.update()
            .where(
                table.c.id == row.id,
                sa.or_(table.c.short_code_hash.is_(None), table.c.short_code_hash == ''),
            )
            .values(short_code_hash=digest)
        )


def _pending_short_code_tables(bind):
    pending = []
    for table_name in ('pairs', 'pair_links'):
        inspector = sa.inspect(bind)
        column = _column(inspector, table_name, 'short_code')
        if column is not None and not column.get('nullable'):
            pending.append(table_name)
    return pending


def _alter_short_code_nullable(bind, table_name):
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name, recreate='always') as batch_op:
            batch_op.alter_column(
                'short_code',
                existing_type=sa.String(length=12),
                nullable=True,
            )
    else:
        op.alter_column(
            table_name,
            'short_code',
            existing_type=sa.String(length=12),
            nullable=True,
        )
    # 每次 DDL 后重新反射，确认可空性已经生效。
    inspector = sa.inspect(op.get_bind())
    column = _column(inspector, table_name, 'short_code')
    if column is None or not column.get('nullable'):
        raise RuntimeError(f'{table_name}.short_code 仍不可空，停止迁移')


def _sqlite_dbapi(connection):
    fairy = connection.connection
    dbapi = getattr(fairy, 'dbapi_connection', None)
    return dbapi if dbapi is not None else fairy


def _read_sqlite_foreign_keys(connection):
    """回读当前连接的外键开关。事务内外查询都返回实际值。"""
    try:
        value = connection.execute(
            sa.text('SELECT foreign_keys FROM pragma_foreign_keys')
        ).scalar()
    except Exception:
        value = connection.execute(sa.text('PRAGMA foreign_keys')).scalar()
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_sqlite_foreign_keys(connection, enabled):
    """在没有挂起 BEGIN 时切换外键。事务内的 PRAGMA 会被 SQLite 静默忽略。"""
    expected = 1 if enabled else 0
    statement = 'PRAGMA foreign_keys=ON' if enabled else 'PRAGMA foreign_keys=OFF'
    dbapi = _sqlite_dbapi(connection)
    if getattr(dbapi, 'in_transaction', False):
        dbapi.commit()
    connection.execute(sa.text(statement))
    if getattr(dbapi, 'in_transaction', False):
        dbapi.commit()
        connection.execute(sa.text(statement))
    return _read_sqlite_foreign_keys(connection) == expected


def _restore_sqlite_foreign_keys():
    """离开当前事务后再打开外键，并在事务外与恢复后各回读一次。"""
    with op.get_context().autocommit_block():
        if not _set_sqlite_foreign_keys(op.get_bind(), True):
            raise RuntimeError('PRAGMA foreign_keys 未能在事务外恢复为 ON')
    if _read_sqlite_foreign_keys(op.get_bind()) != 1:
        raise RuntimeError('PRAGMA foreign_keys 未能在事务外恢复为 ON')


def _relax_short_code_nullable(bind):
    pending = _pending_short_code_tables(bind)
    if not pending:
        return
    if bind.dialect.name != 'sqlite':
        for table_name in pending:
            _alter_short_code_nullable(bind, table_name)
        return
    # SQLite 在事务内忽略 PRAGMA foreign_keys。应用连接默认开启外键，
    # 直接 batch 重建 pairs 会被仍引用它的子表挡住，所以要先离开当前事务。
    previous_fk = _read_sqlite_foreign_keys(bind)
    if previous_fk is None:
        raise RuntimeError('无法回读 PRAGMA foreign_keys')
    was_fk_on = previous_fk == 1
    try:
        with op.get_context().autocommit_block():
            if not _set_sqlite_foreign_keys(op.get_bind(), False):
                raise RuntimeError('PRAGMA foreign_keys 未能在事务外关闭，停止重建')
            for table_name in pending:
                _alter_short_code_nullable(bind, table_name)
    finally:
        # autocommit_block 退出时会重新 BEGIN。恢复必须再进事务外并回读。
        if was_fk_on:
            _restore_sqlite_foreign_keys()


def _plaintext_missing_hash(bind, table_name):
    """是否仍有明文短码，且 short_code_hash 为空、空串，或哈希列还不存在。"""
    inspector = sa.inspect(bind)
    if _column(inspector, table_name, 'short_code') is None:
        return False
    short_code = sa.column('short_code', sa.String)
    if _column(inspector, table_name, 'short_code_hash') is None:
        table = sa.table(table_name, short_code)
        predicate = table.c.short_code.is_not(None)
    else:
        table = sa.table(
            table_name,
            short_code,
            sa.column('short_code_hash', sa.String),
        )
        predicate = sa.and_(
            table.c.short_code.is_not(None),
            sa.or_(table.c.short_code_hash.is_(None), table.c.short_code_hash == ''),
        )
    row = bind.execute(
        sa.select(sa.literal(1)).select_from(table).where(predicate).limit(1)
    ).first()
    return row is not None


def _require_safe_plaintext_clear(bind, pepper):
    """pepper 为空时，禁止擦掉还无法按现网 pepper 补回的明文。"""
    if pepper:
        return
    for table_name in ('pairs', 'pair_links'):
        if _plaintext_missing_hash(bind, table_name):
            raise RuntimeError('PAIR_TOKEN_PEPPER 未设置，无法安全擦除明文短码')


def _clear_plaintext_short_code(bind, table_name, statement, pepper):
    inspector = sa.inspect(bind)
    if _column(inspector, table_name, 'short_code') is None:
        return
    if not pepper and _plaintext_missing_hash(bind, table_name):
        raise RuntimeError('PAIR_TOKEN_PEPPER 未设置，无法安全擦除明文短码')
    bind.execute(sa.text(statement))


def _ensure_usage_event_index(bind):
    inspector = sa.inspect(bind)
    if 'usage_events' not in inspector.get_table_names():
        return
    indexes = {index['name']: index for index in inspector.get_indexes('usage_events')}
    existing = indexes.get(_USAGE_INDEX)
    if existing is None:
        op.create_index(_USAGE_INDEX, 'usage_events', _USAGE_INDEX_COLUMNS, unique=False)
        return
    if list(existing.get('column_names') or []) != _USAGE_INDEX_COLUMNS:
        raise RuntimeError(f'{_USAGE_INDEX} 结构不兼容，停止迁移')


def upgrade():
    bind = op.get_bind()
    pepper = _migration_pair_token_pepper()
    for table_name in ('pairs', 'pair_links'):
        _backfill_short_code_hash(bind, table_name, pepper)
    # 先于会提交事务的 SQLite 重建做检查，避免明文已经擦不掉时留下半截 DDL。
    _require_safe_plaintext_clear(bind, pepper)

    _relax_short_code_nullable(bind)
    bind = op.get_bind()

    _clear_plaintext_short_code(
        bind,
        'pairs',
        'UPDATE pairs SET short_code = NULL WHERE short_code IS NOT NULL',
        pepper,
    )
    _clear_plaintext_short_code(
        bind,
        'pair_links',
        'UPDATE pair_links SET short_code = NULL WHERE short_code IS NOT NULL',
        pepper,
    )
    _ensure_usage_event_index(bind)


def downgrade():
    # 明文短码已安全单向擦除，降级不恢复 short_code 原文，也不把列改回 NOT NULL。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'usage_events' not in inspector.get_table_names():
        return
    names = {index['name'] for index in inspector.get_indexes('usage_events')}
    if _USAGE_INDEX in names:
        op.drop_index(_USAGE_INDEX, table_name='usage_events')
