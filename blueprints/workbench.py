"""机构数据工作台：所有资源查询均重新校验机构成员权限。"""
import io
import json
from datetime import timedelta, timezone
from flask import Blueprint, abort, jsonify, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException
from core.extensions import db
from core.audit import log_audit
from core.guest import is_guest_user
from core.pilot_models import (PilotInstitution, PilotMembership, PilotImportBatch,
    PilotDatasetSnapshot, PilotModelVersion, PilotForecastRun, PilotJob, PilotEncounter, PilotModelActivation)
from core.time_utils import utcnow
from services.data_workbench.access import require_access, PERMISSIONS
from services.data_workbench.jobs import enqueue_job

bp = Blueprint('workbench', __name__)
API = '/api/v1/workbench'


def stamp(value):
    if value is None:
        return None
    if hasattr(value, 'hour') and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def body():
    value = request.get_json()
    if not isinstance(value, dict):
        abort(400, description='请提交 JSON 对象')
    return value


def audit(action, resource):
    log_audit(f'pilot.{action}', 'institution_pilot', resource.id,
              {'institution_id': resource.institution_id if hasattr(resource, 'institution_id') else resource.id})
    db.session.commit()


def resource(model, resource_id, permission='read'):
    value = db.session.get(model, resource_id)
    if not value:
        abort(404)
    require_access(value.institution_id, current_user, permission)
    return value


def job_json(value):
    if not value:
        return None
    return {'id': value.id, 'status': value.status, 'progress': value.progress,
            'error': value.error, 'kind': value.kind, 'resource_id': value.resource_id,
            'attempts': value.attempts, 'created_at': stamp(value.created_at)}


def batch_json(value):
    latest_job = PilotJob.query.filter_by(institution_id=value.institution_id, resource_id=value.id).order_by(PilotJob.created_at.desc()).first()
    return {'id': value.id, 'filename': value.filename, 'status': value.status,
            'coverage_start': stamp(value.coverage_start), 'coverage_end': stamp(value.coverage_end),
            'coverage_mode': value.coverage_mode, 'closed_dates': value.closed_dates,
            'mapping': value.mapping, 'report': value.report, 'revision_of': value.revision_of,
            'created_at': stamp(value.created_at), 'confirmed_at': stamp(value.confirmed_at),
            'original_available': bool(value.storage_path), 'job': job_json(latest_job)}


def dataset_json(value):
    manifest = value.manifest or {}
    from services.data_workbench.ingestion import parse_date
    count = ((parse_date(manifest['date_end']) - parse_date(manifest['date_start'])).days + 1
             if manifest.get('date_start') and manifest.get('date_end') else None)
    return {'id': value.id, 'status': value.status, 'created_at': stamp(value.created_at),
            'cutoff': manifest.get('date_end') or manifest.get('cutoff'),
            'row_count': manifest.get('row_count', count),
            'download_url': url_for('workbench.dataset_download', dataset_id=value.id) if value.status == 'ready' else None}


def model_json(value):
    payload = value.payload or {}
    validation = {**(value.validation or {}), 'passed': bool((value.validation or {}).get('valid'))}
    return {'id': value.id, 'name': value.name, 'family': value.family, 'status': value.status,
            'created_at': stamp(value.created_at), 'metrics': value.metrics,
            'validation': validation, 'periods': payload.get('periods') or payload.get('splits'),
            'eligible_to_activate': validation.get('eligible_for_activation') is True,
            'previously_active': PilotModelActivation.query.filter_by(model_id=value.id).first() is not None,
            'exploratory': payload.get('exploratory', True)}


def memberships():
    return db.session.query(PilotInstitution, PilotMembership).join(
        PilotMembership, PilotMembership.institution_id == PilotInstitution.id).filter(
        PilotMembership.user_id == current_user.id, PilotMembership.active.is_(True),
        PilotInstitution.enabled.is_(True)).order_by(PilotInstitution.name).all()


def institution_json(inst, member):
    permissions = PERMISSIONS.get(member.role, set())
    return {'id': inst.id, 'name': inst.name, 'region_code': inst.region_code,
            'can_manage': 'model' in permissions,
            'can_upload': 'upload' in permissions, 'can_export': 'export' in permissions,
            'raw_storage_approved': inst.raw_storage_approved, 'retention_days': inst.retention_days}


@bp.before_request
def authenticated_api():
    if request.path.startswith(API):
        if not current_user.is_authenticated:
            abort(401)
        if is_guest_user(current_user):
            abort(403)


@bp.errorhandler(HTTPException)
def api_http_error(exc):
    if request.path.startswith(API):
        descriptions = {401: '请先登录', 403: '当前账户没有机构权限', 404: '资源不存在或无权访问',
                        413: '文件超过上传上限'}
        return jsonify(ok=False, error=descriptions.get(exc.code, exc.description)), exc.code
    return exc


@bp.errorhandler(ValueError)
def api_value_error(exc):
    db.session.rollback()
    # 服务校验的提示不含患者原文；JSON 格式错误单独使用固定文案。
    message = '输入格式无效' if isinstance(exc, json.JSONDecodeError) else str(exc)[:180]
    return jsonify(ok=False, error=message), 400


@bp.get('/workbench')
@login_required
def page():
    if is_guest_user(current_user):
        abort(403)
    rows = [institution_json(i, m) for i, m in memberships()]
    if not rows:
        abort(404)
    return render_template('workbench.html', institutions=rows, api_base=API,
                           can_manage=any(i['can_manage'] for i in rows))


@bp.get(API + '/institutions')
def institutions():
    return jsonify(ok=True, institutions=[institution_json(i, m) for i, m in memberships()])


@bp.get(API + '/institutions/<institution_id>/overview')
def institution_overview(institution_id):
    inst = require_access(institution_id, current_user)
    from services.data_workbench.datasets import overview
    data = overview(inst, start=request.args.get('start'), end=request.args.get('end'))
    query = db.session.query(PilotEncounter.encounter_date, db.func.count(PilotEncounter.id)).filter(
        PilotEncounter.institution_id == inst.id, PilotEncounter.active.is_(True))
    from services.data_workbench.ingestion import parse_date
    if data['date_start']:
        query = query.filter(PilotEncounter.encounter_date >= parse_date(data['date_start']),
                             PilotEncounter.encounter_date <= parse_date(data['date_end']))
    counts = dict(query.group_by(PilotEncounter.encounter_date).all())
    daily = data['daily']
    data['summary'].update(records=sum(counts.values()) if daily else None,
                           days=sum(r['coverage_status'] != 'unreported' for r in daily),
                           ready_days=sum(r['coverage_status'] == 'complete' and all(r[k] is not None for k in ('tmean', 'rh_mean', 'precipitation')) for r in daily),
                           last_received=data['latest_reported_at'])
    for month in data['months']:
        days = [r for r in daily if r['date'].startswith(month['month'])]
        month.update(reported_days=month['complete'] + month['partial'] + month['closed'],
                     expected_days=len(days), weather_days=len(days) - month['missing_weather'],
                     ready_days=sum(r['coverage_status'] == 'complete' and all(r[k] is not None for k in ('tmean', 'rh_mean', 'precipitation')) for r in days),
                     records=sum(n for day, n in counts.items() if day.strftime('%Y-%m') == month['month']),
                     status='unreported' if month['unreported'] == len(days) else 'complete' if month['complete'] + month['closed'] == len(days) else 'partial')
    return jsonify(ok=True, overview=data)


@bp.get(API + '/institutions/<institution_id>/jobs')
def institution_jobs(institution_id):
    inst = require_access(institution_id, current_user)
    jobs = PilotJob.query.filter_by(institution_id=inst.id).order_by(PilotJob.created_at.desc()).limit(100).all()
    return jsonify(ok=True, jobs=[job_json(job) for job in jobs])


@bp.route(API + '/institutions/<institution_id>/batches', methods=['GET', 'POST'])
def institution_batches(institution_id):
    inst = require_access(institution_id, current_user, 'upload' if request.method == 'POST' else 'read')
    if request.method == 'GET':
        rows = PilotImportBatch.query.filter_by(institution_id=inst.id).order_by(PilotImportBatch.created_at.desc()).limit(100).all()
        return jsonify(ok=True, batches=[batch_json(row) for row in rows])
    file = request.files.get('file')
    if not file:
        abort(400, description='请选择 Excel 文件')
    mapping = json.loads(request.form.get('mapping') or '{}')
    closed = json.loads(request.form.get('closed_dates') or '[]')
    if not isinstance(mapping, dict) or not isinstance(closed, list):
        abort(400, description='字段对应或停诊日期格式无效')
    from services.data_workbench.ingestion import create_batch
    batch = create_batch(inst, file.read(10 * 1024 * 1024 + 1), file.filename, current_user.id,
                         {'start': request.form.get('coverage_start'), 'end': request.form.get('coverage_end'),
                          'mode': request.form.get('coverage_mode'), 'closed_dates': closed,
                          'revision_of': request.form.get('revision_of')}, mapping)
    job = None
    if batch.status != 'confirmed':
        job = enqueue_job(inst.id, current_user.id, 'parse', batch.id)
    audit('upload', batch)
    return jsonify(ok=True, batch=batch_json(batch), job=job_json(job)), 202 if job else 200


@bp.get(API + '/batches/<batch_id>')
def batch_detail(batch_id):
    batch = resource(PilotImportBatch, batch_id)
    job = PilotJob.query.filter_by(institution_id=batch.institution_id, resource_id=batch.id).order_by(PilotJob.created_at.desc()).first()
    return jsonify(ok=True, batch=batch_json(batch), job=job_json(job))


@bp.post(API + '/batches/<batch_id>/confirm')
def batch_confirm(batch_id):
    batch = resource(PilotImportBatch, batch_id, 'upload')
    data = body()
    if 'mapping' in data:
        if not isinstance(data['mapping'], dict) or batch.status in {'confirmed', 'confirming'}:
            abort(400, description='已确认批次不能修改字段；请另建修订批次')
        if PilotJob.query.filter_by(resource_id=batch.id).filter(PilotJob.status.in_(['running', 'queued'])).first():
            abort(409, description='请等待当前处理结束')
        batch.mapping, batch.status = data['mapping'], 'queued'
        db.session.commit()
        job = enqueue_job(batch.institution_id, current_user.id, 'parse', batch.id)
    else:
        decisions = data.get('decisions', {})
        if not isinstance(decisions, dict):
            abort(400, description='核对结果格式无效')
        job = enqueue_job(batch.institution_id, current_user.id, 'confirm', batch.id, {'decisions': decisions})
    audit('confirm_request', batch)
    return jsonify(ok=True, batch=batch_json(batch), job=job_json(job)), 202


@bp.get(API + '/batches/<batch_id>/original')
def batch_original(batch_id):
    batch = resource(PilotImportBatch, batch_id, 'export')
    inst = require_access(batch.institution_id, current_user, 'export')
    created = batch.created_at.replace(tzinfo=timezone.utc) if batch.created_at.tzinfo is None else batch.created_at
    if not batch.storage_path or utcnow() >= created + timedelta(days=inst.retention_days):
        abort(410, description='原件已超过约定保存期限，批次来源与哈希仍保留')
    from services.data_workbench.storage import read_bytes
    content = read_bytes(batch.storage_path)
    audit('original_download', batch)
    return send_file(io.BytesIO(content), as_attachment=True, download_name=f'batch-{batch.id}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route(API + '/institutions/<institution_id>/datasets', methods=['GET', 'POST'])
def institution_datasets(institution_id):
    inst = require_access(institution_id, current_user, 'export')
    if request.method == 'GET':
        rows = PilotDatasetSnapshot.query.filter_by(institution_id=inst.id).order_by(PilotDatasetSnapshot.created_at.desc()).limit(100).all()
        return jsonify(ok=True, datasets=[dataset_json(row) for row in rows])
    data = body()
    job = enqueue_job(inst.id, current_user.id, 'dataset', inst.id, {'cutoff': data.get('cutoff')})
    dataset = db.session.get(PilotDatasetSnapshot, job.resource_id)
    audit('export_request', inst)
    return jsonify(ok=True, dataset=dataset_json(dataset) if dataset else None, job=job_json(job)), 202


@bp.get(API + '/datasets/<dataset_id>/download')
def dataset_download(dataset_id):
    dataset = resource(PilotDatasetSnapshot, dataset_id, 'export')
    if not dataset.storage_path:
        abort(409, description='训练包仍在生成')
    from services.data_workbench.datasets import export_snapshot
    content = export_snapshot(dataset)
    audit('dataset_download', dataset)
    return send_file(io.BytesIO(content), as_attachment=True, download_name=f'dataset-{dataset.id}.zip', mimetype='application/zip')


@bp.route(API + '/institutions/<institution_id>/models', methods=['GET', 'POST'])
def institution_models(institution_id):
    inst = require_access(institution_id, current_user, 'model' if request.method == 'POST' else 'read')
    if request.method == 'GET':
        rows = PilotModelVersion.query.filter_by(institution_id=inst.id).order_by(PilotModelVersion.created_at.desc()).limit(100).all()
        return jsonify(ok=True, models=[model_json(row) for row in rows], current_model_id=inst.current_model_id)
    from services.data_workbench.model_registry import import_model
    version = import_model(inst, body(), current_user.id)
    audit('model_import', version)
    return jsonify(ok=True, model=model_json(version)), 201


@bp.post(API + '/models/<model_id>/activate')
def model_activate(model_id):
    model = resource(PilotModelVersion, model_id, 'model')
    inst = require_access(model.institution_id, current_user, 'model')
    from services.data_workbench.model_registry import activate_model
    version = activate_model(inst, model.id, current_user.id)
    audit('model_activate', version)
    return jsonify(ok=True, model=model_json(version))


@bp.get(API + '/models/<model_id>/comparison')
def model_comparison(model_id):
    model = resource(PilotModelVersion, model_id)
    inst = require_access(model.institution_id, current_user)
    from services.data_workbench.model_registry import compare_models
    return jsonify(ok=True, comparison=compare_models(inst, model.id))


@bp.post(API + '/institutions/<institution_id>/rollback')
def institution_rollback(institution_id):
    inst = require_access(institution_id, current_user, 'model')
    from services.data_workbench.model_registry import rollback_model
    version = rollback_model(inst, current_user.id)
    audit('model_rollback', version)
    return jsonify(ok=True, model=model_json(version))


@bp.route(API + '/institutions/<institution_id>/forecasts', methods=['GET', 'POST'])
def institution_forecasts(institution_id):
    inst = require_access(institution_id, current_user, 'model' if request.method == 'POST' else 'read')
    if request.method == 'POST':
        job = enqueue_job(inst.id, current_user.id, 'forecast', inst.id)
        return jsonify(ok=True, job=job_json(job)), 202
    from services.data_workbench.datasets import overview
    rows = PilotForecastRun.query.filter_by(institution_id=inst.id).order_by(PilotForecastRun.issued_at.desc()).limit(30).all()
    forecasts = []
    for row in rows:
        payload = dict(row.payload or {})
        daily = payload.get('daily', [])
        if daily:
            observations = {d['date']: d['cases_60plus'] for d in overview(inst, daily[0]['date'], daily[-1]['date'])['daily']}
            payload['daily'] = [{**d, 'observed': observations.get(d['date'])} for d in daily]
        forecasts.append({**payload, 'id': row.id, 'issued_at': stamp(row.issued_at),
                          'day7_mean': payload.get('day_seven_mean'),
                          'seven_day_total': payload.get('seven_day_total_mean')})
    from services.data_workbench.predictions import prospective_evaluation
    return jsonify(ok=True, forecasts=forecasts, evaluation=prospective_evaluation(inst))


@bp.get(API + '/jobs/<job_id>')
def job_detail(job_id):
    job = resource(PilotJob, job_id)
    return jsonify(ok=True, job=job_json(job))


@bp.post(API + '/jobs/<job_id>/retry')
def job_retry(job_id):
    from services.data_workbench.jobs import KINDS, dispatch
    job = resource(PilotJob, job_id)
    require_access(job.institution_id, current_user, KINDS[job.kind])
    if job.status != 'failed':
        abort(409, description='只可重试已失败的任务')
    job.status, job.error, job.attempts, job.user_id = 'queued', None, 0, current_user.id
    db.session.commit()
    dispatch(job)
    audit('job_retry', job)
    return jsonify(ok=True, job=job_json(job)), 202
