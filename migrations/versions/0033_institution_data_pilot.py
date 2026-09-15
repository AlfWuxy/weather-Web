"""机构数据试点独立表；降级拒绝删除任何已存在的试点记录。"""
import importlib
import re

from alembic import op
import sqlalchemy as sa

revision = '0033_institution_data_pilot'
down_revision = '0032_weather_alert_provenance'
branch_labels = None
depends_on = None

TABLE_NAMES = ['pilot_institutions', 'pilot_dataset_snapshots', 'pilot_forecast_receipts', 'pilot_import_batches', 'pilot_jobs', 'pilot_memberships', 'pilot_weather_days', 'pilot_coverage', 'pilot_encounters', 'pilot_model_versions', 'pilot_forecast_runs', 'pilot_model_activations']


def _create_schema(operations):
    operations.create_table('pilot_institutions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('region_code', sa.String(length=32), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=False),
        sa.Column('longitude', sa.Float(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('raw_storage_approved', sa.Boolean(), nullable=False),
        sa.Column('retention_days', sa.Integer(), nullable=False),
        sa.Column('field_mapping', sa.JSON(), nullable=False),
        sa.Column('current_model_id', sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table('pilot_dataset_snapshots',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('manifest', sa.JSON(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('storage_path', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table('pilot_forecast_receipts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('provider', sa.String(length=80), nullable=False),
        sa.Column('product', sa.String(length=80), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table('pilot_import_batches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('idempotency_key', sa.String(length=64), nullable=False),
        sa.Column('filename', sa.String(length=200), nullable=False),
        sa.Column('storage_path', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('revision_of', sa.String(length=36), nullable=True),
        sa.Column('coverage_start', sa.Date(), nullable=False),
        sa.Column('coverage_end', sa.Date(), nullable=False),
        sa.Column('coverage_mode', sa.String(length=20), nullable=False),
        sa.Column('closed_dates', sa.JSON(), nullable=False),
        sa.Column('mapping', sa.JSON(), nullable=False),
        sa.Column('report', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['revision_of'], ['pilot_import_batches.id']),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('institution_id', 'idempotency_key', name='uq_pilot_import_idempotency'),
    )
    operations.create_table('pilot_jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('resource_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('progress', sa.Integer(), nullable=False),
        sa.Column('error', sa.String(length=200), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table('pilot_memberships',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('institution_id', 'user_id', name='uq_pilot_member'),
    )
    operations.create_table('pilot_weather_days',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('tmean', sa.Float(), nullable=True),
        sa.Column('rh_mean', sa.Float(), nullable=True),
        sa.Column('precipitation', sa.Float(), nullable=True),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('product', sa.String(length=80), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('institution_id', 'date', 'product', name='uq_pilot_weather_day'),
    )
    operations.create_table('pilot_coverage',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('batch_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(['batch_id'], ['pilot_import_batches.id']),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('institution_id', 'date', name='uq_pilot_coverage_day'),
    )
    operations.create_table('pilot_encounters',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('batch_id', sa.String(length=36), nullable=False),
        sa.Column('encounter_date', sa.Date(), nullable=False),
        sa.Column('age', sa.Integer(), nullable=False),
        sa.Column('source_key', sa.String(length=64), nullable=True),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('suspected_key', sa.String(length=64), nullable=True),
        sa.Column('diagnosis_category', sa.String(length=32), nullable=False),
        sa.Column('residence_region_code', sa.String(length=12), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('supersedes_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['batch_id'], ['pilot_import_batches.id']),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.ForeignKeyConstraint(['supersedes_id'], ['pilot_encounters.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table('pilot_model_versions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('dataset_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('family', sa.String(length=80), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=False),
        sa.Column('validation', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.ForeignKeyConstraint(['dataset_id'], ['pilot_dataset_snapshots.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('institution_id', 'sha256', name='uq_pilot_model_hash'),
    )
    operations.create_table('pilot_forecast_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('model_id', sa.String(length=36), nullable=False),
        sa.Column('receipt_id', sa.String(length=36), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.ForeignKeyConstraint(['model_id'], ['pilot_model_versions.id']),
        sa.ForeignKeyConstraint(['receipt_id'], ['pilot_forecast_receipts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('institution_id', 'issued_at', name='uq_pilot_forecast_issue'),
        sa.UniqueConstraint('model_id', 'receipt_id', name='uq_pilot_forecast_run'),
    )
    operations.create_table('pilot_model_activations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('institution_id', sa.String(length=36), nullable=False),
        sa.Column('model_id', sa.String(length=36), nullable=False),
        sa.Column('previous_model_id', sa.String(length=36), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['institution_id'], ['pilot_institutions.id']),
        sa.ForeignKeyConstraint(['model_id'], ['pilot_model_versions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_index('ix_pilot_dataset_snapshots_institution_id', 'pilot_dataset_snapshots', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_forecast_receipts_institution_id', 'pilot_forecast_receipts', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_import_batches_institution_id', 'pilot_import_batches', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_jobs_institution_id', 'pilot_jobs', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_coverage_institution_id', 'pilot_coverage', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_encounters_encounter_date', 'pilot_encounters', ['encounter_date'], unique=False)
    operations.create_index('ix_pilot_encounters_suspected_key', 'pilot_encounters', ['suspected_key'], unique=False)
    operations.create_index('ix_pilot_encounters_fingerprint', 'pilot_encounters', ['fingerprint'], unique=False)
    operations.create_index('ix_pilot_encounters_institution_id', 'pilot_encounters', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_encounters_source_key', 'pilot_encounters', ['source_key'], unique=False)
    operations.create_index('uq_pilot_active_source', 'pilot_encounters', ['institution_id', 'source_key'], unique=True, sqlite_where=sa.text('active = 1 AND source_key IS NOT NULL'), postgresql_where=sa.text('active AND source_key IS NOT NULL'))
    operations.create_index('ix_pilot_model_versions_institution_id', 'pilot_model_versions', ['institution_id'], unique=False)
    operations.create_index('ix_pilot_forecast_runs_institution_id', 'pilot_forecast_runs', ['institution_id'], unique=False)


class _SchemaDefinition:
    """复用本迁移的静态声明建立期望结构，不依赖会继续演进的应用模型。"""
    def __init__(self):
        self.metadata = sa.MetaData()
        sa.Table('users', self.metadata, sa.Column('id', sa.Integer, primary_key=True))

    def create_table(self, name, *columns):
        return sa.Table(name, self.metadata, *columns)

    def create_index(self, name, table, columns, **options):
        return sa.Index(name, *(self.metadata.tables[table].c[column] for column in columns), **options)


def _predicate(value):
    # 数据库反射会增加引号与括号；不移除操作符，避免误认不同的唯一范围。
    return re.sub(r'[\s"`()]+', '', str(value if value is not None else '')).lower()


def _validate_existing(bind):
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names()).intersection(TABLE_NAMES)
    if not present:
        return False
    if present != set(TABLE_NAMES):
        raise RuntimeError('pilot migration aborted: partial_schema')
    definition = _SchemaDefinition()
    _create_schema(definition)
    for name in TABLE_NAMES:
        expected = definition.metadata.tables[name]
        columns = {column['name']: column for column in inspector.get_columns(name)}
        if set(columns) != set(expected.columns.keys()):
            raise RuntimeError(f'pilot migration aborted: invalid_columns={name}')
        for column in expected.columns:
            actual = columns[column.name]
            invalid = (not isinstance(actual['type'], type(column.type))
                       or actual['nullable'] is not column.nullable
                       or actual.get('default') is not None
                       or bool(actual.get('computed')) or bool(actual.get('identity')))
            if isinstance(column.type, sa.String):
                invalid = invalid or actual['type'].length != column.type.length
            if isinstance(column.type, sa.DateTime) and bind.dialect.name == 'postgresql':
                invalid = invalid or actual['type'].timezone is not column.type.timezone
            if invalid:
                raise RuntimeError(f'pilot migration aborted: invalid_column={name}.{column.name}')
        if inspector.get_pk_constraint(name).get('constrained_columns') != [c.name for c in expected.primary_key.columns]:
            raise RuntimeError(f'pilot migration aborted: invalid_primary_key={name}')
        uniques = {c.name: tuple(column.name for column in c.columns)
                   for c in expected.constraints if isinstance(c, sa.UniqueConstraint)}
        actual_uniques = {c['name']: tuple(c['column_names']) for c in inspector.get_unique_constraints(name)}
        if actual_uniques != uniques:
            raise RuntimeError(f'pilot migration aborted: invalid_unique_constraints={name}')
        foreign_keys = sorted((tuple(c.name for c in constraint.columns),
                               constraint.referred_table.name,
                               tuple(element.column.name for element in constraint.elements))
                              for constraint in expected.foreign_key_constraints)
        actual_fks = inspector.get_foreign_keys(name)
        actual_foreign_keys = sorted((tuple(item['constrained_columns']), item['referred_table'],
                                      tuple(item['referred_columns'])) for item in actual_fks)
        if foreign_keys != actual_foreign_keys or any(any(v not in (None, False, 'NO ACTION') for v in (item.get('options') or {}).values()) for item in actual_fks):
            raise RuntimeError(f'pilot migration aborted: invalid_foreign_keys={name}')
        indexes = {index.name: index for index in expected.indexes}
        actual_indexes = {item['name']: item for item in inspector.get_indexes(name) if not item.get('duplicates_constraint')}
        if set(indexes) != set(actual_indexes):
            raise RuntimeError(f'pilot migration aborted: invalid_indexes={name}')
        for index_name, index in indexes.items():
            actual = actual_indexes[index_name]
            actual_where = (actual.get('dialect_options') or {}).get(bind.dialect.name + '_where')
            expected_where = index.dialect_options[bind.dialect.name].get('where')
            if (actual['column_names'] != [c.name for c in index.columns]
                    or bool(actual['unique']) is not bool(index.unique)
                    or _predicate(actual_where) != _predicate(expected_where)):
                raise RuntimeError(f'pilot migration aborted: invalid_index={index_name}')
        if inspector.get_check_constraints(name):
            raise RuntimeError(f'pilot migration aborted: unexpected_check_constraints={name}')
    return True


def _previous_migration():
    return importlib.import_module('migrations.versions.0032_weather_alert_provenance')


def _preflight_lower_downgrade(bind):
    """在删除首张试点表前运行 0032 自身及其下游的全部数据保护检查。"""
    context = op.get_context()
    environment = getattr(context, 'environment_context', None)
    if environment is None:
        return
    try:
        planned = tuple(environment.script.iterate_revisions(
            context.get_current_heads(), environment.get_revision_argument(), select_for_downgrade=True))
    except Exception as exc:
        raise RuntimeError('pilot migration aborted: unable_to_resolve_downgrade_plan') from exc
    if '0032_weather_alert_provenance' not in {item.revision for item in planned}:
        return
    previous = _previous_migration()
    inspector = sa.inspect(bind)
    columns = previous._columns(inspector)
    previous._validate_dedupe_baseline(inspector, columns)
    previous._validate_existing(columns, bind.dialect.name)
    previous._preflight_lower_downgrade(bind)
    protected = previous._protected_row_count(bind, columns)
    if protected:
        raise RuntimeError(f'alert provenance downgrade aborted: protected_count={protected}; provenance columns were preserved')


def upgrade():
    bind = op.get_bind()
    if _validate_existing(bind):
        return
    if 'users' not in sa.inspect(bind).get_table_names():
        raise RuntimeError('pilot migration aborted: missing_users')
    _previous_migration()._begin_sqlite_write_transaction(bind)
    _create_schema(op)


def downgrade():
    bind = op.get_bind()
    if not _validate_existing(bind):
        raise RuntimeError('pilot migration aborted: missing_schema')
    _preflight_lower_downgrade(bind)
    # 临床报送和版本记录不能因回滚功能代码而被静默删除。
    for name in TABLE_NAMES:
        table = sa.table(name)
        if bind.execute(sa.select(sa.func.count()).select_from(table)).scalar_one():
            raise RuntimeError('试点表包含记录，请保留数据并仅关闭功能开关')
    _previous_migration()._begin_sqlite_write_transaction(bind)
    for name in reversed(TABLE_NAMES):
        op.drop_table(name)
