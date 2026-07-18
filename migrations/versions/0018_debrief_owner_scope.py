"""为行动复盘增加独立 owner 与稳定来源边界

Revision ID: 0018_debrief_owner_scope
Revises: 0017_delivery_review_workflow
Create Date: 2026-07-18 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0018_debrief_owner_scope'
down_revision = '0017_delivery_review_workflow'
branch_labels = None
depends_on = None


OWNER_INDEX_NAME = 'ix_debriefs_owner_user_id'
OWNER_FK_NAME = 'fk_debriefs_owner_user_id_users'
ORIGIN_INDEX_NAME = 'ix_debriefs_origin_pair_id'
ORIGIN_FK_NAME = 'fk_debriefs_origin_pair_id_pairs'
DISPLAY_FK_NAME = 'fk_debriefs_pair_id_pairs'
BATCH_NAMING_CONVENTION = {
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
}


def _foreign_key(inspector, column_name, referred_table):
    return next(
        (
            foreign_key
            for foreign_key in inspector.get_foreign_keys('debriefs')
            if foreign_key.get('constrained_columns') == [column_name]
            and foreign_key.get('referred_table') == referred_table
            and foreign_key.get('referred_columns') == ['id']
        ),
        None,
    )


def _uses_set_null(foreign_key):
    if foreign_key is None:
        return False
    ondelete = (foreign_key.get('options') or {}).get('ondelete') or ''
    return str(ondelete).upper().replace(' ', '') == 'SETNULL'


def _foreign_key_name(foreign_key, fallback):
    return foreign_key.get('name') or fallback


def _count_unowned_legacy_rows(bind, columns):
    """在任何 DDL 前确认旧记录都能由 pair 无损推导 owner 与来源。"""
    if 'pair_id' not in columns:
        raise RuntimeError(
            'debrief owner migration aborted: pair_id is missing; '
            'no schema changes were applied'
        )

    invalid_conditions = [
        'debrief.pair_id IS NULL',
        'source_pair.id IS NULL',
        'owner.id IS NULL',
    ]
    if 'owner_user_id' in columns:
        invalid_conditions.append(
            '('
            'debrief.owner_user_id IS NOT NULL '
            'AND debrief.owner_user_id != source_pair.caregiver_id'
            ')'
        )
    if 'origin_pair_id' in columns:
        invalid_conditions.append(
            '('
            'debrief.origin_pair_id IS NOT NULL '
            'AND debrief.origin_pair_id != debrief.pair_id'
            ')'
        )

    result = bind.execute(
        sa.text(
            f'''
            SELECT COUNT(*)
            FROM debriefs AS debrief
            LEFT JOIN pairs AS source_pair ON source_pair.id = debrief.pair_id
            LEFT JOIN users AS owner ON owner.id = source_pair.caregiver_id
            WHERE {' OR '.join(invalid_conditions)}
            '''
        )
    ).scalar_one()
    return int(result)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if 'debriefs' not in tables:
        return
    if 'users' not in tables or 'pairs' not in tables:
        raise RuntimeError(
            'debrief owner migration aborted: users or pairs table is missing'
        )

    columns = {column['name']: column for column in inspector.get_columns('debriefs')}
    if 'owner_user_id' not in columns or 'origin_pair_id' not in columns:
        # SQLite 的部分 DDL 无法可靠回滚，旧数据可归属性必须先于 add_column 校验。
        orphan_count = _count_unowned_legacy_rows(bind, set(columns))
        if orphan_count:
            raise RuntimeError(
                'debrief owner migration aborted: '
                f'orphan_count={orphan_count}; no rows were deleted'
            )
        with op.batch_alter_table('debriefs') as batch_op:
            if 'owner_user_id' not in columns:
                batch_op.add_column(
                    sa.Column(
                        'owner_user_id',
                        sa.Integer(),
                        nullable=True,
                    )
                )
            if 'origin_pair_id' not in columns:
                batch_op.add_column(
                    sa.Column(
                        'origin_pair_id',
                        sa.Integer(),
                        nullable=True,
                    )
                )

    # 旧结构只有 pair_id；它同时是唯一可靠的 owner 与稳定来源依据。
    bind.execute(
        sa.text(
            '''
            UPDATE debriefs
            SET owner_user_id = (
                SELECT pairs.caregiver_id
                FROM pairs
                WHERE pairs.id = debriefs.pair_id
            )
            WHERE owner_user_id IS NULL
              AND pair_id IS NOT NULL
            '''
        )
    )
    bind.execute(
        sa.text(
            '''
            UPDATE debriefs
            SET origin_pair_id = pair_id
            WHERE origin_pair_id IS NULL
              AND pair_id IS NOT NULL
            '''
        )
    )

    orphan_count = int(
        bind.execute(
            sa.text(
                '''
                SELECT COUNT(*)
                FROM debriefs AS debrief
                LEFT JOIN users AS owner ON owner.id = debrief.owner_user_id
                LEFT JOIN pairs AS origin_pair ON origin_pair.id = debrief.origin_pair_id
                LEFT JOIN pairs AS display_pair ON display_pair.id = debrief.pair_id
                WHERE debrief.owner_user_id IS NULL
                   OR owner.id IS NULL
                   OR debrief.origin_pair_id IS NULL
                   OR origin_pair.id IS NULL
                   OR origin_pair.caregiver_id != debrief.owner_user_id
                   OR (
                       debrief.pair_id IS NOT NULL
                       AND (
                           display_pair.id IS NULL
                           OR debrief.pair_id != debrief.origin_pair_id
                       )
                   )
                '''
            )
        ).scalar_one()
    )
    if orphan_count:
        # 失败时保留所有旧记录，由部署前人工确认 owner 与来源家人。
        raise RuntimeError(
            'debrief owner migration aborted: '
            f'orphan_count={orphan_count}; no rows were deleted'
        )

    inspector = inspect(bind)
    columns = {column['name']: column for column in inspector.get_columns('debriefs')}
    indexes = {index.get('name') for index in inspector.get_indexes('debriefs')}
    owner_fk = _foreign_key(inspector, 'owner_user_id', 'users')
    origin_fk = _foreign_key(inspector, 'origin_pair_id', 'pairs')
    display_fk = _foreign_key(inspector, 'pair_id', 'pairs')
    needs_owner_fk = owner_fk is None
    needs_origin_fk = origin_fk is None or not _uses_set_null(origin_fk)
    needs_display_fk = display_fk is None or not _uses_set_null(display_fk)
    needs_not_null = bool(columns['owner_user_id'].get('nullable'))
    if needs_owner_fk or needs_origin_fk or needs_display_fk or needs_not_null:
        with op.batch_alter_table(
            'debriefs',
            naming_convention=BATCH_NAMING_CONVENTION,
        ) as batch_op:
            if origin_fk is not None and not _uses_set_null(origin_fk):
                batch_op.drop_constraint(
                    _foreign_key_name(origin_fk, ORIGIN_FK_NAME),
                    type_='foreignkey',
                )
            if display_fk is not None and not _uses_set_null(display_fk):
                batch_op.drop_constraint(
                    _foreign_key_name(display_fk, DISPLAY_FK_NAME),
                    type_='foreignkey',
                )
            if needs_owner_fk:
                batch_op.create_foreign_key(
                    OWNER_FK_NAME,
                    'users',
                    ['owner_user_id'],
                    ['id'],
                )
            if needs_origin_fk:
                batch_op.create_foreign_key(
                    ORIGIN_FK_NAME,
                    'pairs',
                    ['origin_pair_id'],
                    ['id'],
                    ondelete='SET NULL',
                )
            if needs_display_fk:
                batch_op.create_foreign_key(
                    DISPLAY_FK_NAME,
                    'pairs',
                    ['pair_id'],
                    ['id'],
                    ondelete='SET NULL',
                )
            if needs_not_null:
                batch_op.alter_column(
                    'owner_user_id',
                    existing_type=sa.Integer(),
                    nullable=False,
                )
    if OWNER_INDEX_NAME not in indexes:
        op.create_index(
            OWNER_INDEX_NAME,
            'debriefs',
            ['owner_user_id'],
            unique=False,
        )
    if ORIGIN_INDEX_NAME not in indexes:
        op.create_index(
            ORIGIN_INDEX_NAME,
            'debriefs',
            ['origin_pair_id'],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'debriefs' not in inspector.get_table_names():
        return
    columns = {column['name'] for column in inspector.get_columns('debriefs')}
    removable_columns = {'owner_user_id', 'origin_pair_id'} & columns
    if not removable_columns:
        return

    # 旧结构只能通过 pair_id 表达复盘归属。已经关闭家人关联，或归属与
    # 展示关系不一致的记录无法无损降级，必须在删列前中止并由人工处理。
    if {'pair_id', 'owner_user_id', 'origin_pair_id'} <= columns:
        unrepresentable_count = int(
            bind.execute(
                sa.text(
                    '''
                    SELECT COUNT(*)
                    FROM debriefs AS debrief
                    LEFT JOIN pairs AS display_pair
                      ON display_pair.id = debrief.pair_id
                    WHERE debrief.pair_id IS NULL
                       OR display_pair.id IS NULL
                       OR debrief.origin_pair_id IS NULL
                       OR debrief.origin_pair_id != debrief.pair_id
                       OR debrief.owner_user_id IS NULL
                       OR display_pair.caregiver_id != debrief.owner_user_id
                    '''
                )
            ).scalar_one()
        )
        if unrepresentable_count:
            raise RuntimeError(
                'debrief owner downgrade aborted: '
                f'unrepresentable_count={unrepresentable_count}; '
                'owner and origin columns were preserved'
            )
    elif 'pair_id' not in columns:
        raise RuntimeError(
            'debrief owner downgrade aborted: pair_id is missing; '
            'owner and origin columns were preserved'
        )

    indexes = {index.get('name') for index in inspector.get_indexes('debriefs')}
    display_fk = _foreign_key(inspector, 'pair_id', 'pairs')
    restore_display_fk = _uses_set_null(display_fk)
    with op.batch_alter_table(
        'debriefs',
        naming_convention=BATCH_NAMING_CONVENTION,
    ) as batch_op:
        if ORIGIN_INDEX_NAME in indexes:
            batch_op.drop_index(ORIGIN_INDEX_NAME)
        if OWNER_INDEX_NAME in indexes:
            batch_op.drop_index(OWNER_INDEX_NAME)
        if restore_display_fk:
            batch_op.drop_constraint(
                _foreign_key_name(display_fk, DISPLAY_FK_NAME),
                type_='foreignkey',
            )
            batch_op.create_foreign_key(
                DISPLAY_FK_NAME,
                'pairs',
                ['pair_id'],
                ['id'],
            )
        if 'origin_pair_id' in removable_columns:
            batch_op.drop_column('origin_pair_id')
        if 'owner_user_id' in removable_columns:
            batch_op.drop_column('owner_user_id')
