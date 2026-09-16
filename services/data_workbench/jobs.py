"""持久化任务状态与 Celery 调度；重试仍以数据库状态为准。"""
from datetime import timedelta
from uuid import uuid4
from flask import current_app
from sqlalchemy import or_, update
from werkzeug.exceptions import HTTPException, Conflict
from core.db_models import User
from core.extensions import db
from core.pilot_models import (PilotJob, PilotInstitution, PilotMembership,
                               PilotImportBatch, PilotDatasetSnapshot, PilotCoverage)
from core.time_utils import utcnow, today_local
from .access import require_access

KINDS = {'parse': 'upload', 'confirm': 'upload', 'dataset': 'export',
         'weather': 'read', 'forecast': 'model'}


def dispatch(job):
    if current_app.config.get('PILOT_TASKS_EAGER'):
        if not current_app.testing:
            raise RuntimeError('同步任务仅允许在测试环境使用')
        execute_job(job.id)
        return
    try:
        from core.pilot_runtime import create_celery
        celery = current_app.extensions.get('pilot_celery') or create_celery(current_app._get_current_object())
        celery.send_task('pilot.execute', args=[job.id], retry=False)
    except Exception:
        # 不输出异常原文，连接字符串可能包含队列密码。
        job.error = '后台队列暂不可用；任务已保存，恢复后会继续'
        db.session.commit()


def enqueue_job(institution_id, user_id, kind, resource_id, parameters=None):
    if kind not in KINDS or not isinstance(parameters or {}, dict):
        raise ValueError('不支持的任务类型')
    # 同机构请求串行检查，避免两个网页同时创建等价任务。
    db.session.execute(update(PilotInstitution).where(PilotInstitution.id == institution_id)
                       .values(enabled=PilotInstitution.enabled))
    query = PilotJob.query.filter_by(institution_id=institution_id, kind=kind).filter(
        PilotJob.status.in_(['queued', 'running']))
    if kind != 'dataset':
        query = query.filter_by(resource_id=resource_id)
    existing = query.first()
    if existing:
        db.session.commit()
        if existing.parameters != (parameters or {}):
            raise Conflict('当前任务使用不同日期或核对选项，请等处理完成后重新提交')
        return existing
    if kind == 'dataset':
        # 创建快照前先持久化其 ID，硬中断重投仍指向同一冻结版本。
        resource_id = str(uuid4())
    job = PilotJob(institution_id=institution_id, user_id=user_id, kind=kind,
                   resource_id=resource_id, parameters=parameters or {})
    db.session.add(job)
    db.session.commit()
    dispatch(job)
    return job


def execute_job(job_id):
    now = utcnow()
    stale = now - timedelta(minutes=16)
    changed = db.session.execute(update(PilotJob).where(
        PilotJob.id == job_id, PilotJob.attempts < 4,
        or_(PilotJob.status == 'queued',
            (PilotJob.status == 'running') & (PilotJob.started_at < stale))
    ).values(status='running', started_at=now, attempts=PilotJob.attempts + 1,
             progress=10, error=None), execution_options={'synchronize_session': False}).rowcount
    db.session.commit()
    if changed != 1:
        return
    job = db.session.get(PilotJob, job_id)
    try:
        user = db.session.get(User, job.user_id)
        inst = require_access(job.institution_id, user, KINDS[job.kind])
        if job.kind == 'parse':
            from .ingestion import process_batch
            process_batch(job.resource_id)
        elif job.kind == 'confirm':
            from .ingestion import confirm_batch
            confirm_batch(job.resource_id, user.id, job.parameters.get('decisions', {}))
        elif job.kind in {'dataset', 'weather'}:
            from .weather import sync_history
            bounds = db.session.query(db.func.min(PilotCoverage.date), db.func.max(PilotCoverage.date)).filter(
                PilotCoverage.institution_id == inst.id).first()
            if not bounds[0]:
                raise ValueError('请先完成一次数据导入')
            from .ingestion import parse_date
            cutoff = parse_date(job.parameters['cutoff']) if job.parameters.get('cutoff') else bounds[1]
            if cutoff < bounds[0] or cutoff > bounds[1]:
                raise ValueError('截止日期须在已报送范围内')
            snapshot = db.session.get(PilotDatasetSnapshot, job.resource_id) if job.kind == 'dataset' else None
            if not snapshot:
                sync_history(inst, bounds[0] - timedelta(days=14), cutoff)
            if job.kind == 'dataset':
                from .datasets import create_snapshot, build_snapshot
                if not snapshot:
                    snapshot = create_snapshot(inst, user.id, cutoff=cutoff, snapshot_id=job.resource_id)
                    job.progress = 70
                    db.session.commit()
                build_snapshot(snapshot.id)
        elif job.kind == 'forecast':
            from .predictions import generate_forecast, collect_forecast_receipt
            # 重投已生成结果的任务不重复请求天气。
            if not job.parameters.get('forecast_run_id') and not job.parameters.get('receipt_id'):
                if inst.current_model_id:
                    run = generate_forecast(inst)
                    job.parameters = {**job.parameters, 'forecast_run_id': run.id}
                else:
                    receipt = collect_forecast_receipt(inst)
                    job.parameters = {**job.parameters, 'receipt_id': receipt.id}
        job.status, job.progress, job.finished_at = 'succeeded', 100, utcnow()
        job.error = None
        db.session.commit()
        if job.kind == 'confirm':
            enqueue_job(inst.id, user.id, 'weather', inst.id)
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(PilotJob, job_id)
        job.status, job.finished_at = 'failed', utcnow()
        # 只显示服务自己定义的校验提示，不回传解析器、网络或数据库原文。
        if type(exc) is ValueError:
            job.error = str(exc)[:180]
        elif isinstance(exc, HTTPException):
            job.error = '机构权限已变化，任务停止；请联系机构管理员'
        else:
            job.error = '处理失败，请重试；持续失败时请联系管理员并提供任务编号'
        db.session.commit()


def recover_jobs():
    if not current_app.config.get('FEATURE_INSTITUTION_WORKBENCH'):
        return
    stale = utcnow() - timedelta(minutes=16)
    rows = PilotJob.query.filter(or_(PilotJob.status == 'queued',
                                     (PilotJob.status == 'running') & (PilotJob.started_at < stale))).limit(100).all()
    for job in rows:
        if job.attempts >= 4:
            job.status, job.error = 'failed', '任务多次中断，请核对后手动重试'
            db.session.commit()
        else:
            dispatch(job)


def daily_forecasts():
    if not current_app.config.get('FEATURE_INSTITUTION_WORKBENCH'):
        return
    for inst in PilotInstitution.query.filter_by(enabled=True).all():
        member = PilotMembership.query.join(User, User.id == PilotMembership.user_id).filter(
            PilotMembership.institution_id == inst.id, PilotMembership.active.is_(True),
            PilotMembership.role.in_(['manager', 'researcher']), User.deleted_at.is_(None),
            User.role != 'guest').order_by(PilotMembership.user_id).first()
        if not member:
            continue
        key = today_local().isoformat()
        # 同一调度日的资源键使 beat 重启后也不会重复采集。
        kinds = ['forecast']
        if PilotCoverage.query.filter_by(institution_id=inst.id).first():
            kinds.insert(0, 'weather')
        for kind in kinds:
            resource = f'{kind}:{key}'
            previous = PilotJob.query.filter_by(institution_id=inst.id, kind=kind, resource_id=resource).first()
            if not previous:
                enqueue_job(inst.id, member.user_id, kind, resource, {'scheduled_date': key})
