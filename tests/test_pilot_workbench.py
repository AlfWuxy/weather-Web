"""工作台 HTTP 与任务恢复验收；数据均为合成。"""
import io
from datetime import timedelta
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from openpyxl import Workbook
from core.db_models import User
from core.extensions import db
from core.pilot_models import PilotInstitution, PilotMembership, PilotEncounter, PilotJob, PilotDatasetSnapshot
from core.time_utils import utcnow

API = '/api/v1/workbench'


def test_worker_runtime_dependencies_and_flask_context(monkeypatch):
    """按 CI 的正式依赖启动队列入口；不连接 Redis，也能发现缺包。"""
    from flask import Flask, current_app
    from core.pilot_runtime import create_celery

    app = Flask('pilot-runtime-smoke')
    app.config['PILOT_REDIS_URL'] = 'redis://127.0.0.1:6379/15'
    received = []

    def record_job(job_id):
        received.append((job_id, current_app.name))

    monkeypatch.setattr('services.data_workbench.jobs.execute_job', record_job)
    celery = create_celery(app)
    try:
        # 构造传输层会加载 Redis 驱动，但这里不建立网络连接。
        with celery.connection_for_write() as connection:
            assert connection.transport.driver_type == 'redis'
        celery.tasks['pilot.execute'].apply(args=['synthetic-job-id'], throw=True)
        assert received == [('synthetic-job-id', app.name)]
    finally:
        celery.close()


@pytest.fixture
def pilot_http(app, db_session, monkeypatch, tmp_path):
    app.config.update(FEATURE_INSTITUTION_WORKBENCH=True, PILOT_TASKS_EAGER=True,
                      PILOT_STORAGE_DIR=str(tmp_path / 'encrypted'),
                      PILOT_STORAGE_KEY=Fernet.generate_key().decode(), PILOT_REDIS_URL='')
    monkeypatch.setattr('services.data_workbench.weather.sync_history', lambda *args, **kwargs: {})
    alice = User(username='synthetic-doctor-a', password_hash='unused')
    bob = User(username='synthetic-doctor-b', password_hash='unused')
    a = PilotInstitution(name='合成测试机构甲', region_code='360428', latitude=29.2, longitude=116.3,
                         enabled=True, raw_storage_approved=True)
    b = PilotInstitution(name='合成测试机构乙', region_code='360428', latitude=29.3, longitude=116.4,
                         enabled=True, raw_storage_approved=True)
    db_session.add_all([alice, bob, a, b])
    db_session.flush()
    db_session.add_all([PilotMembership(institution_id=a.id, user_id=alice.id, role='manager'),
                       PilotMembership(institution_id=b.id, user_id=bob.id, role='uploader')])
    db_session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'], session['_fresh'], session['_csrf_token'] = alice.get_id(), True, 'synthetic-csrf'
    yield SimpleNamespace(app=app, client=client, user=alice, other=bob, a=a, b=b,
                          headers={'X-CSRF-Token': 'synthetic-csrf'})


def excel():
    wb = Workbook()
    wb.active.append(['挂号日期', '年龄', '诊断', '就诊编号'])
    wb.active.append(['2024-01-01', 70, '合成示例', 'synthetic-a'])
    wb.active.append(['2024-01-02', 59, '合成示例', 'synthetic-b'])
    buff = io.BytesIO()
    wb.save(buff)
    return buff.getvalue()


def upload(env, file_bytes=None):
    file_bytes = excel() if file_bytes is None else file_bytes
    return env.client.post(f'{API}/institutions/{env.a.id}/batches', headers=env.headers,
                           data={'file': (io.BytesIO(file_bytes), 'synthetic.xlsx'),
                                 'coverage_start': '2024-01-01', 'coverage_end': '2024-01-03',
                                 'coverage_mode': 'complete'})


def test_disabled_by_default_and_private_headers(pilot_http):
    env = pilot_http
    env.app.config['FEATURE_INSTITUTION_WORKBENCH'] = False
    assert env.client.get('/workbench').status_code == 404
    assert env.client.get(API + '/institutions').status_code == 404
    env.app.config['FEATURE_INSTITUTION_WORKBENCH'] = True
    response = env.client.get(API + '/institutions')
    assert response.status_code == 200
    assert 'no-store' in response.headers['Cache-Control']
    assert response.json['institutions'][0]['name'] == env.a.name
    assert len(response.json['institutions']) == 1


def test_http_upload_confirm_export_and_duplicate(pilot_http):
    env = pilot_http
    # Excel 的 ZIP 时间戳会随生成时间变化；文件幂等必须重传同一份字节。
    file_bytes = excel()
    response = upload(env, file_bytes=file_bytes)
    assert response.status_code == 202, response.data
    batch = response.json['batch']
    assert batch['status'] == 'ready'
    endpoint = f"{API}/batches/{batch['id']}/confirm"
    response = env.client.post(endpoint, json={'decisions': {}}, headers=env.headers)
    assert response.json['job']['status'] == 'succeeded', response.json
    assert response.json['batch']['status'] == 'confirmed'
    again = upload(env, file_bytes=file_bytes)
    assert again.json['batch']['id'] == batch['id']
    assert PilotEncounter.query.count() == 2
    overview = env.client.get(f'{API}/institutions/{env.a.id}/overview').json['overview']
    assert [r['cases_60plus'] for r in overview['daily']] == [1, 0, 0]
    response = env.client.post(f'{API}/institutions/{env.a.id}/datasets', json={}, headers=env.headers)
    assert response.json['job']['status'] == 'succeeded', response.json
    dataset = response.json['dataset']
    assert dataset['status'] == 'ready'
    download = env.client.get(dataset['download_url'])
    assert download.status_code == 200 and download.data[:2] == b'PK'
    assert b'synthetic-a' not in download.data


def test_every_resource_rechecks_institution(pilot_http):
    env = pilot_http
    created = upload(env).json
    batch_id, job_id = created['batch']['id'], created['job']['id']
    with env.client.session_transaction() as session:
        session['_user_id'] = env.other.get_id()
    # db_session fixture 保留外层 app context，需要清除前一次请求的登录缓存。
    from flask import g
    g.pop('_login_user', None)
    for path in (f'/institutions/{env.a.id}/overview', f'/institutions/{env.a.id}/batches',
                 f'/institutions/{env.a.id}/datasets', f'/institutions/{env.a.id}/models',
                 f'/institutions/{env.a.id}/forecasts', f'/batches/{batch_id}',
                 f'/batches/{batch_id}/original', f'/jobs/{job_id}'):
        assert env.client.get(API + path).status_code == 404, path
    assert env.client.post(f'{API}/batches/{batch_id}/confirm', json={}, headers=env.headers).status_code == 404
    assert env.client.post(f'{API}/institutions/{env.b.id}/models', json={}, headers=env.headers).status_code == 404


def test_csrf_and_json_request_limit(pilot_http):
    env = pilot_http
    endpoint = f'{API}/institutions/{env.a.id}/models'
    assert env.client.post(endpoint, json={}).status_code in {400, 403}
    response = env.client.post(endpoint, data='{"x":"' + 'a' * (2 * 1024 * 1024) + '"}',
                               content_type='application/json', headers=env.headers)
    assert response.status_code == 413
    assert env.app.config.get('MAX_CONTENT_LENGTH') == 1024 * 1024


def test_queue_outage_keeps_durable_job_and_recovery(pilot_http):
    env = pilot_http
    env.app.config['PILOT_TASKS_EAGER'] = False
    created = upload(env).json
    job = db.session.get(PilotJob, created['job']['id'])
    assert job.status == 'queued' and job.error
    env.app.config['PILOT_TASKS_EAGER'] = True
    from services.data_workbench.jobs import recover_jobs
    recover_jobs()
    assert job.status == 'succeeded'
    assert env.client.get(f"{API}/batches/{created['batch']['id']}").json['batch']['status'] == 'ready'


def test_lost_worker_lease_resumes_and_revocation_stops_work(pilot_http):
    env = pilot_http
    env.app.config['PILOT_TASKS_EAGER'] = False
    created = upload(env).json
    job = db.session.get(PilotJob, created['job']['id'])
    job.status, job.started_at = 'running', utcnow() - timedelta(minutes=17)
    db.session.commit()
    from services.data_workbench.jobs import execute_job
    execute_job(job.id)
    assert job.status == 'succeeded'
    job.status = 'queued'
    PilotMembership.query.filter_by(user_id=env.user.id).first().active = False
    db.session.commit()
    execute_job(job.id)
    assert job.status == 'failed' and '权限' in job.error


def test_mapping_requires_recheck_before_import(pilot_http):
    env = pilot_http
    created = upload(env).json
    response = env.client.post(f"{API}/batches/{created['batch']['id']}/confirm",
        json={'mapping': {'encounter_date': '挂号日期', 'age': '年龄', 'diagnosis': '诊断', 'source_id': '就诊编号'}},
        headers=env.headers)
    assert response.json['job']['kind'] == 'parse'
    assert response.json['batch']['status'] == 'ready'
    assert PilotEncounter.query.count() == 0


def test_raw_storage_approval_is_server_enforced(pilot_http):
    env = pilot_http
    env.a.raw_storage_approved = False
    db.session.commit()
    response = upload(env)
    assert response.status_code == 400 and '存储' in response.json['error']


def test_training_cutoff_is_fixed_in_snapshot(pilot_http):
    env = pilot_http
    created = upload(env).json
    env.client.post(f"{API}/batches/{created['batch']['id']}/confirm", json={}, headers=env.headers)
    response = env.client.post(f'{API}/institutions/{env.a.id}/datasets', json={'cutoff': '2024-01-02'}, headers=env.headers)
    assert response.json['job']['status'] == 'succeeded', response.json
    snapshot = db.session.get(PilotDatasetSnapshot, response.json['dataset']['id'])
    assert snapshot.manifest['cutoff_date'] == '2024-01-02'


def test_queued_export_different_cutoff_is_explicit_conflict(pilot_http):
    env = pilot_http
    created = upload(env).json
    env.client.post(f"{API}/batches/{created['batch']['id']}/confirm", json={}, headers=env.headers)
    env.app.config['PILOT_TASKS_EAGER'] = False
    first = env.client.post(f'{API}/institutions/{env.a.id}/datasets', json={'cutoff': '2024-01-02'}, headers=env.headers)
    second = env.client.post(f'{API}/institutions/{env.a.id}/datasets', json={'cutoff': '2024-01-03'}, headers=env.headers)
    assert first.status_code == 202
    assert second.status_code == 409
    assert '不同日期' in second.json['error']


def test_snapshot_commit_then_worker_exit_keeps_same_freeze(pilot_http, monkeypatch):
    env = pilot_http
    created = upload(env).json
    env.client.post(f"{API}/batches/{created['batch']['id']}/confirm", json={}, headers=env.headers)
    env.app.config['PILOT_TASKS_EAGER'] = False
    result = env.client.post(f'{API}/institutions/{env.a.id}/datasets', json={}, headers=env.headers).json
    job = db.session.get(PilotJob, result['job']['id'])
    snapshot_id = job.resource_id
    from services.data_workbench import datasets
    from services.data_workbench.jobs import execute_job
    original = datasets.create_snapshot
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit('模拟进程在快照提交后中断')
    monkeypatch.setattr(datasets, 'create_snapshot', interrupted)
    with pytest.raises(SystemExit):
        execute_job(job.id)
    assert PilotDatasetSnapshot.query.count() == 1
    # 中断期间出现病例修订，重试仍必须导出原快照。
    PilotEncounter.query.filter_by(age=70).first().age = 50
    job.status = 'queued'
    db.session.commit()
    monkeypatch.setattr(datasets, 'create_snapshot', original)
    execute_job(job.id)
    snapshot = db.session.get(PilotDatasetSnapshot, snapshot_id)
    assert job.status == 'succeeded' and job.resource_id == snapshot_id
    assert PilotDatasetSnapshot.query.count() == 1
    import csv, zipfile
    with zipfile.ZipFile(io.BytesIO(datasets.export_snapshot(snapshot))) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('daily.csv').decode())))
    assert rows[0]['cases_60plus'] == '1'


def test_daily_schedule_skips_deleted_member(pilot_http):
    env = pilot_http
    env.app.config['PILOT_TASKS_EAGER'] = False
    env.user.deleted_at = utcnow()
    db.session.add(PilotMembership(institution_id=env.a.id, user_id=env.other.id, role='manager'))
    db.session.commit()
    from services.data_workbench.jobs import daily_forecasts
    daily_forecasts()
    jobs = PilotJob.query.filter_by(institution_id=env.a.id).all()
    assert {j.kind for j in jobs} == {'forecast'}
    assert {j.user_id for j in jobs} == {env.other.id}


def _synthetic_multipart(payload):
    """直接构造内存请求体，避免测试客户端自己的临时文件影响验证。"""
    boundary = 'synthetic-pilot-upload-boundary'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="synthetic.xlsx"\r\nContent-Type: application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet\r\n\r\n').encode()
    return body + payload + f'\r\n--{boundary}--\r\n'.encode(), f'multipart/form-data; boundary={boundary}'


@pytest.fixture
def pilot_stream_probe():
    from flask import Flask, request
    from core.pilot_runtime import PilotRequest

    app = Flask('synthetic-pilot-stream-probe')
    app.request_class = PilotRequest
    app.config.update(TESTING=True, FEATURE_INSTITUTION_WORKBENCH=True,
                      MAX_CONTENT_LENGTH=1024 * 1024)

    @app.post(API + '/stream-probe')
    @app.post('/legacy-upload')
    def inspect_upload():
        upload = request.files['file']
        return {'memory_only': isinstance(upload.stream, io.BytesIO),
                'rolled_to_disk': bool(getattr(upload.stream, '_rolled', False)),
                'length': len(upload.read())}

    return SimpleNamespace(app=app, client=app.test_client())


def _reject_disk_upload_stream(*args, **kwargs):
    raise AssertionError('工作台上传在加密前调用了磁盘临时文件流')


def test_large_workbench_multipart_never_uses_disk_spool(pilot_stream_probe, monkeypatch):
    # 超过 Werkzeug 默认 500 KiB 阈值，实际执行 multipart 解析器。
    payload = b'SYNTHETIC-ONLY-' * 50000
    assert len(payload) > 500 * 1024
    body, content_type = _synthetic_multipart(payload)
    monkeypatch.setattr('werkzeug.formparser.SpooledTemporaryFile', _reject_disk_upload_stream)
    monkeypatch.setattr('werkzeug.formparser.TemporaryFile', _reject_disk_upload_stream, raising=False)
    response = pilot_stream_probe.client.post(API + '/stream-probe', data=body, content_type=content_type)
    assert response.status_code == 200
    assert response.json == {'memory_only': True, 'rolled_to_disk': False, 'length': len(payload)}


@pytest.mark.parametrize('enabled,path', [(True, '/legacy-upload'), (False, API + '/stream-probe')])
def test_nonpilot_upload_keeps_default_stream_and_size_limit(pilot_stream_probe, enabled, path):
    pilot_stream_probe.app.config['FEATURE_INSTITUTION_WORKBENCH'] = enabled
    body, content_type = _synthetic_multipart(b'SYNTHETIC-ONLY-' * 50000)
    response = pilot_stream_probe.client.post(path, data=body, content_type=content_type)
    assert response.status_code == 200
    assert response.json['memory_only'] is False
    assert response.json['rolled_to_disk'] is True
    body, content_type = _synthetic_multipart(b'S' * (1024 * 1024))
    assert pilot_stream_probe.client.post(path, data=body, content_type=content_type).status_code == 413


def test_workbench_multipart_still_rejects_requests_over_twenty_mib(pilot_http, monkeypatch):
    body, content_type = _synthetic_multipart(b'S' * (20 * 1024 * 1024))
    assert len(body) > 20 * 1024 * 1024
    monkeypatch.setattr('werkzeug.formparser.SpooledTemporaryFile', _reject_disk_upload_stream)
    monkeypatch.setattr('werkzeug.formparser.TemporaryFile', _reject_disk_upload_stream, raising=False)
    response = pilot_http.client.post(f'{API}/institutions/{pilot_http.a.id}/batches',
                                     data=body, content_type=content_type, headers=pilot_http.headers)
    assert response.status_code == 413


def test_workbench_file_still_rejects_uploads_over_ten_mib(pilot_http, monkeypatch):
    # 请求在总量限制内，但文件本身超过 10 MiB，必须在内容解析前拒绝。
    monkeypatch.setattr('werkzeug.formparser.SpooledTemporaryFile', _reject_disk_upload_stream)
    monkeypatch.setattr('werkzeug.formparser.TemporaryFile', _reject_disk_upload_stream, raising=False)
    response = pilot_http.client.post(f'{API}/institutions/{pilot_http.a.id}/batches',
        headers=pilot_http.headers, data={
            'file': (io.BytesIO(b'S' * (10 * 1024 * 1024 + 1)), 'synthetic.xlsx'),
            'coverage_start': '2024-01-01', 'coverage_end': '2024-01-03', 'coverage_mode': 'complete'})
    assert response.status_code == 400
    assert '超过上传上限' in response.json['error']
