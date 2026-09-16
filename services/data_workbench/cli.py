"""由运维在本机为真实合作机构授权；不自动创建医生账户。"""
import click
from flask.cli import with_appcontext
from core.extensions import db
from core.db_models import User
from core.pilot_models import PilotInstitution, PilotMembership


def register_pilot_cli(app):
    @app.cli.group('pilot')
    def pilot():
        """机构试点开户与检查。"""

    @pilot.command('institution')
    @click.option('--name', required=True)
    @click.option('--region-code', required=True)
    @click.option('--latitude', required=True, type=click.FloatRange(-90, 90))
    @click.option('--longitude', required=True, type=click.FloatRange(-180, 180))
    @click.option('--manager-user-id', required=True, type=int)
    @click.option('--retention-days', required=True, type=click.IntRange(1, 3650))
    @click.option('--storage-approved', is_flag=True, help='已确认合作范围、私有存储位置与保存安排')
    @click.option('--enable', is_flag=True)
    @with_appcontext
    def institution(name, region_code, latitude, longitude, manager_user_id,
                    retention_days, storage_approved, enable):
        user = db.session.get(User, manager_user_id)
        from core.guest import is_guest_user
        if not user or is_guest_user(user) or user.deleted_at:
            raise click.ClickException('请选择已有的有效实名合作账户')
        if not name.strip() or len(name) > 160 or not region_code.isdigit() or len(region_code) != 6:
            raise click.ClickException('需要机构名称和六位行政区代码')
        if enable and not storage_approved:
            raise click.ClickException('启用前必须确认合作范围、存储位置和保存安排')
        if storage_approved:
            from .storage import _root, _cipher
            _root()
            _cipher()
        inst = PilotInstitution(name=name.strip(), region_code=region_code,
                                latitude=latitude, longitude=longitude,
                                raw_storage_approved=storage_approved,
                                retention_days=retention_days, enabled=enable)
        db.session.add(inst)
        db.session.flush()
        db.session.add(PilotMembership(institution_id=inst.id, user_id=user.id, role='manager'))
        db.session.commit()
        click.echo(f'机构已创建：{inst.id}；启用：{inst.enabled}')

    @pilot.command('member')
    @click.option('--institution-id', required=True)
    @click.option('--user-id', required=True, type=int)
    @click.option('--role', required=True, type=click.Choice(['uploader', 'researcher', 'manager']))
    @click.option('--revoke', is_flag=True)
    @with_appcontext
    def member(institution_id, user_id, role, revoke):
        from core.guest import is_guest_user
        inst, user = db.session.get(PilotInstitution, institution_id), db.session.get(User, user_id)
        if not inst or not user or is_guest_user(user) or user.deleted_at:
            raise click.ClickException('机构或账户无效')
        row = PilotMembership.query.filter_by(institution_id=inst.id, user_id=user.id).first()
        if not row:
            row = PilotMembership(institution_id=inst.id, user_id=user.id)
            db.session.add(row)
        row.role, row.active = role, not revoke
        db.session.commit()
        click.echo('机构成员关系已更新')

    @pilot.command('check')
    @with_appcontext
    def check():
        from sqlalchemy import inspect
        from .storage import _root, _cipher
        from flask import current_app
        required = {'pilot_jobs', 'pilot_institutions', 'pilot_weather_days'}
        if not required.issubset(set(inspect(db.engine).get_table_names())):
            raise click.ClickException('请先执行独立数据库迁移')
        _root()
        _cipher()
        if not current_app.config.get('PILOT_REDIS_URL'):
            raise click.ClickException('尚未配置独立任务队列')
        from redis import Redis
        Redis.from_url(current_app.config['PILOT_REDIS_URL'], socket_connect_timeout=3,
                       socket_timeout=3).ping()
        click.echo('数据库、加密目录与队列连接检查通过；仍需启动 worker 和 beat')

    @pilot.command('expire-originals')
    @click.option('--institution-id', required=True)
    @click.option('--apply', is_flag=True, help='删除已到保存期限的加密原件；保留批次哈希和聚合快照')
    @with_appcontext
    def expire_originals(institution_id, apply):
        from datetime import timedelta
        from core.time_utils import utcnow
        from core.pilot_models import PilotImportBatch
        from .storage import delete_bytes
        inst = db.session.get(PilotInstitution, institution_id)
        if not inst:
            raise click.ClickException('机构不存在')
        cutoff = utcnow() - timedelta(days=inst.retention_days)
        rows = PilotImportBatch.query.filter(PilotImportBatch.institution_id == inst.id,
                                             PilotImportBatch.created_at < cutoff,
                                             PilotImportBatch.status == 'confirmed').all()
        count = 0
        for row in rows:
            if row.storage_path:
                count += 1
                if apply:
                    delete_bytes(row.storage_path)
                    row.storage_path = ''
        if apply:
            db.session.commit()
        click.echo(f'{"已处理" if apply else "预览到期原件"}：{count}；聚合快照保持不变')
