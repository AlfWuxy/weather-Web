"""机构数据隔离与冻结版本验收；只使用人工构造的虚拟就诊。"""
import csv
import hashlib
import importlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from flask import Flask
from openpyxl import Workbook
from werkzeug.exceptions import NotFound
from sqlalchemy import create_engine, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations

from core.db_models import User
from core.extensions import db
from core.pilot_models import PilotInstitution, PilotMembership, PilotEncounter, PilotCoverage, PilotImportBatch, PilotWeatherDay
from services.data_workbench import ingestion, storage, datasets, access, weather


@pytest.fixture
def pilot_env(tmp_path):
    app = Flask('pilot-data-test')
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', TESTING=True,
                      PILOT_STORAGE_DIR=str(tmp_path / 'private'), PILOT_STORAGE_KEY=Fernet.generate_key().decode(),
                      FEATURE_INSTITUTION_WORKBENCH=True)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username='synthetic-uploader', password_hash='no-password')
        outsider = User(username='synthetic-outsider', password_hash='no-password')
        a = PilotInstitution(name='虚拟测试机构甲', region_code='360428', latitude=29.2, longitude=116.3, enabled=True, raw_storage_approved=True)
        b = PilotInstitution(name='虚拟测试机构乙', region_code='360429', latitude=29.3, longitude=116.4, enabled=True, raw_storage_approved=True)
        db.session.add_all([user, outsider, a, b])
        db.session.flush()
        db.session.add(PilotMembership(institution_id=a.id, user_id=user.id, role='uploader'))
        db.session.commit()
        yield SimpleNamespace(app=app, user=user, outsider=outsider, a=a, b=b)
        db.session.remove()
        db.drop_all()


def excel(rows, headers=('挂号日期', '年龄', '门诊诊断', '就诊编号')):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def create(env, rows, *, mode='complete', closed=None, revision=None, dates=('2024-01-01', '2024-01-03'), headers=None):
    coverage = {'start': dates[0], 'end': dates[1], 'mode': mode, 'closed_dates': closed or [], 'revision_of': revision}
    batch = ingestion.create_batch(env.a, excel(rows, headers) if headers else excel(rows), 'synthetic.xlsx', env.user.id, coverage)
    ingestion.process_batch(batch.id)
    return batch


def confirm(env, batch, **decisions):
    return ingestion.confirm_batch(batch.id, env.user.id, decisions)


def test_membership_and_feature_are_both_required(pilot_env):
    env = pilot_env
    assert access.require_access(env.a.id, env.user).id == env.a.id
    for inst, user, permission in [(env.b.id, env.user, 'read'), (env.a.id, env.outsider, 'read'), (env.a.id, env.user, 'model')]:
        with pytest.raises(NotFound):
            access.require_access(inst, user, permission)
    env.app.config['FEATURE_INSTITUTION_WORKBENCH'] = False
    with pytest.raises(NotFound):
        access.require_access(env.a.id, env.user)


def test_ciphertext_private_path_and_required_key(pilot_env):
    payload = b'artificial-private-source'
    path = storage.store_bytes(payload)
    assert payload not in (Path(pilot_env.app.config['PILOT_STORAGE_DIR']) / path).read_bytes()
    assert storage.read_bytes(path) == payload
    assert (Path(pilot_env.app.config['PILOT_STORAGE_DIR']) / path).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        storage.read_bytes('../elsewhere.enc')
    pilot_env.app.config['PILOT_STORAGE_KEY'] = ''
    with pytest.raises(ValueError, match='不能自动生成'):
        storage.store_bytes(payload)


def test_explicitly_unused_identifier_is_not_auto_reenabled(pilot_env):
    env = pilot_env
    batch = ingestion.create_batch(env.a, excel([('2024-01-01', 70, '合成', 'untrusted-number')]),
        'synthetic.xlsx', env.user.id, {'start': '2024-01-01', 'end': '2024-01-03', 'mode': 'complete'},
        mapping={'encounter_date': '挂号日期', 'age': '年龄', 'source_id': None})
    ingestion.process_batch(batch.id)
    assert batch.mapping['source_id'] is None
    confirm(env, batch)
    assert PilotEncounter.query.one().source_key is None


def test_same_file_is_idempotent_and_stable_source_id_deduplicates(pilot_env):
    env = pilot_env
    rows = [('2024-01-01', 70, '模拟感冒', 'virtual-001')]
    batch = create(env, rows)
    confirm(env, batch)
    again = create(env, rows)
    assert again.id == batch.id
    later = create(env, rows + [('2024-01-02', 59, '模拟感冒', 'virtual-002')])
    report = confirm(env, later)
    assert report['imported_rows'] == 1
    assert PilotEncounter.query.filter_by(active=True).count() == 2
    assert all('virtual-' not in (r.source_key or '') for r in PilotEncounter.query.all())


def test_invalid_date_never_becomes_today_and_no_partial_commit(pilot_env):
    env = pilot_env
    batch = create(env, [('invalid-date', 70, '模拟咳嗽', 'a'), ('2024-01-02', 75, '模拟咳嗽', 'b')])
    assert batch.status == 'invalid'
    assert batch.report['counts']['invalid'] == 1
    with pytest.raises(ValueError):
        confirm(env, batch)
    assert PilotEncounter.query.count() == 0
    assert PilotCoverage.query.count() == 0


def test_formulas_and_external_links_are_rejected(pilot_env):
    batch = create(pilot_env, [('2024-01-01', '=60+1', '模拟咳嗽', 'a')])
    assert batch.status == 'invalid'
    original = excel([('2024-01-01', 70, '模拟', 'a')])
    buff = io.BytesIO(original)
    with zipfile.ZipFile(buff, 'a') as archive:
        archive.writestr('xl/externalLinks/externalLink1.xml', '<x/>')
    with pytest.raises(ValueError, match='外部'):
        ingestion.create_batch(pilot_env.a, buff.getvalue(), 'x.xlsx', pilot_env.user.id,
                               {'start': '2024-01-01', 'end': '2024-01-03', 'mode': 'complete'})


def test_unidentified_duplicates_require_explicit_decision(pilot_env):
    env = pilot_env
    batch = create(env, [('2024-01-01', 70, '模拟感冒', None), ('2024-01-01', 70, '模拟感冒', None)])
    group = batch.report['duplicate_groups'][0]
    with pytest.raises(ValueError, match='逐组'):
        confirm(env, batch)
    assert PilotEncounter.query.count() == 0
    confirm(env, batch, duplicates={group['id']: 'keep_first'})
    assert PilotEncounter.query.count() == 1
    assert batch.status == 'confirmed'


def test_overlap_without_ids_does_not_silently_drop_records(pilot_env):
    env = pilot_env
    first = create(env, [('2024-01-01', 70, '模拟感冒', None)])
    confirm(env, first)
    second = create(env, [('2024-01-01', 70, '模拟感冒', None), ('2024-01-02', 80, '模拟感冒', None)])
    group = second.report['duplicate_groups'][0]
    assert group['existing_count'] == 1
    confirm(env, second, duplicates={group['id']: 'keep_all'})
    assert PilotEncounter.query.filter_by(active=True).count() == 3


def test_stable_id_revision_requires_confirmation_and_preserves_history(pilot_env):
    env = pilot_env
    first = create(env, [('2024-01-01', 59, '模拟感冒', 'a')])
    confirm(env, first)
    revision = create(env, [('2024-01-01', 70, '模拟感冒', 'a')])
    assert revision.report['revision_rows'] == [2]
    with pytest.raises(ValueError, match='逐行'):
        confirm(env, revision)
    confirm(env, revision, revision_rows=[2])
    assert PilotEncounter.query.count() == 2
    active = PilotEncounter.query.filter_by(active=True).one()
    assert active.age == 70 and active.supersedes_id


def test_coverage_distinguishes_zero_partial_closed_and_unreported(pilot_env):
    env = pilot_env
    complete = create(env, [('2024-01-01', 70, '模拟', 'a')], closed=['2024-01-03'])
    confirm(env, complete)
    partial = create(env, [('2024-01-04', 75, '模拟', 'b')], mode='partial', dates=('2024-01-04', '2024-01-04'))
    confirm(env, partial)
    rows = datasets.overview(env.a, '2024-01-01', '2024-01-05')['daily']
    assert [r['cases_60plus'] for r in rows] == [1, 0, None, None, None]
    assert [r['coverage_status'] for r in rows] == ['complete', 'complete', 'closed', 'partial', 'unreported']
    assert rows[3]['observed_rows_60plus'] == 1


def test_batch_revision_keeps_snapshot_immutable(pilot_env):
    env = pilot_env
    first = create(env, [('2024-01-01', 70, '模拟', 'a')])
    confirm(env, first)
    frozen = datasets.create_snapshot(env.a, env.user.id)
    revision = create(env, [('2024-01-02', 75, '模拟', 'b')], revision=first.id)
    with pytest.raises(ValueError, match='替代'):
        confirm(env, revision)
    confirm(env, revision, confirm_revision=True)
    datasets.build_snapshot(frozen.id)
    data = datasets.export_snapshot(frozen)
    assert hashlib.sha256(data).hexdigest() == frozen.sha256
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        rows = list(csv.DictReader(io.TextIOWrapper(archive.open('daily.csv'), encoding='utf-8')))
        manifest = json.loads(archive.read('manifest.json'))
        assert [r['cases_60plus'] for r in rows] == ['1', '0', '0']
        assert manifest['files']['daily.csv']['sha256'] == hashlib.sha256(archive.read('daily.csv')).hexdigest()
        assert manifest['outcome'] == 'daily_encounters_60plus'
    assert [r['cases_60plus'] for r in datasets.overview(env.a)['daily']] == [0, 1, 0]
    datasets.build_snapshot(frozen.id)
    assert datasets.export_snapshot(frozen) == data


def test_missing_diagnosis_requires_explicit_acceptance(pilot_env):
    batch = create(pilot_env, [('2024-01-01', 70, None, 'a')])
    with pytest.raises(ValueError, match='缺失诊断'):
        confirm(pilot_env, batch)
    confirm(pilot_env, batch, accept_missing_diagnosis=True)
    assert PilotEncounter.query.one().diagnosis_category == 'unknown'


def test_residence_not_inferred_or_detailed_address_retained(pilot_env):
    batch = create(pilot_env, [('2024-01-01', 70, '模拟', 'a')])
    confirm(pilot_env, batch)
    assert PilotEncounter.query.one().residence_region_code is None
    detailed = create(pilot_env, [('2024-01-02', 70, '模拟', 'b', '某街道详细地址')], headers=('挂号日期', '年龄', '门诊诊断', '就诊编号', '居住地行政区代码'))
    assert detailed.status == 'invalid'
    assert '某街道详细地址' not in json.dumps(detailed.report, ensure_ascii=False)


def test_no_raw_storage_before_approval(pilot_env):
    pilot_env.a.raw_storage_approved = False
    with pytest.raises(ValueError, match='保存期限'):
        create(pilot_env, [('2024-01-01', 70, '模拟', 'a')])


def test_weather_fixed_product_cache_and_missing_are_preserved(pilot_env, monkeypatch):
    calls = []
    daily = {'time': ['2024-01-01', '2024-01-02'], 'temperature_2m_mean': [10.5, None],
             'relative_humidity_2m_mean': [70, 80], 'precipitation_sum': [0, 1]}
    def get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=json.dumps({'daily': daily}).encode(), raise_for_status=lambda: None, json=lambda: {'daily': daily})
    monkeypatch.setattr(weather.requests, 'get', get)
    result = weather.sync_history(pilot_env.a, '2024-01-01', '2024-01-02')
    assert result['product'] == 'era5'
    assert calls[0]['params']['models'] == 'era5'
    assert calls[0]['params']['timezone'] == 'Asia/Shanghai'
    assert PilotWeatherDay.query.filter_by(date=date(2024, 1, 2)).one().tmean is None
    weather.sync_history(pilot_env.a, '2024-01-01', '2024-01-01')
    assert len(calls) == 1


def test_migration_creates_all_tables_and_refuses_destructive_downgrade():
    migration = importlib.import_module('migrations.versions.0033_institution_data_pilot')
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            migration.upgrade()
            assert set(migration.TABLE_NAMES).issubset(inspect(conn).get_table_names())
            conn.exec_driver_sql("INSERT INTO pilot_institutions (id,name,region_code,latitude,longitude,enabled,raw_storage_approved,retention_days,field_mapping) VALUES ('synthetic','virtual','000000',0,0,0,0,30,'{}')")
            with pytest.raises(RuntimeError, match='包含记录'):
                migration.downgrade()
            conn.exec_driver_sql('DELETE FROM pilot_institutions')
            migration.downgrade()
            assert not set(migration.TABLE_NAMES).intersection(inspect(conn).get_table_names())
    engine.dispose()


def test_snapshot_cutoff_rejects_out_of_coverage_and_cannot_include_later_rows(pilot_env):
    batch = create(pilot_env, [('2024-01-01', 70, '模拟', 'a'), ('2024-01-03', 75, '模拟', 'b')])
    confirm(pilot_env, batch)
    with pytest.raises(ValueError, match='截止日期'):
        datasets.create_snapshot(pilot_env.a, pilot_env.user.id, cutoff='2025-01-01')
    snapshot = datasets.create_snapshot(pilot_env.a, pilot_env.user.id, cutoff='2024-01-02')
    datasets.build_snapshot(snapshot.id)
    assert snapshot.manifest['cutoff_date'] == '2024-01-02'
    with zipfile.ZipFile(io.BytesIO(datasets.export_snapshot(snapshot))) as archive:
        rows = list(csv.DictReader(io.TextIOWrapper(archive.open('daily.csv'), encoding='utf-8')))
    assert len(rows) == 2


def test_stale_whole_batch_revision_cannot_fork_history(pilot_env):
    env = pilot_env
    original = create(env, [('2024-01-01', 70, '模拟', 'a')])
    confirm(env, original)
    revision = create(env, [('2024-01-02', 75, '模拟', 'b')], revision=original.id)
    confirm(env, revision, confirm_revision=True)
    with pytest.raises(ValueError, match='最新修订'):
        create(env, [('2024-01-03', 80, '模拟', 'c')], revision=original.id)


@pytest.mark.parametrize('text,expected', [('0岁3月', 0), ('7岁2月3天', 7), ('60岁11月30天', 60), ('11月', 0), ('13月', 1)])
def test_completed_years_parse_real_export_age_shapes(text, expected):
    assert ingestion._age(text) == expected


def test_patient_key_disambiguates_people_without_becoming_encounter_id(pilot_env):
    headers = ('挂号日期', '年龄', '诊断', '病案号')
    batch = create(pilot_env, [('2024-01-01', '60岁11月', '模拟', 'virtual-person-a'), ('2024-01-01', '60岁11月', '模拟', 'virtual-person-b')], headers=headers)
    assert batch.report['duplicate_groups'] == []
    confirm(pilot_env, batch)
    assert PilotEncounter.query.count() == 2
    assert all(r.source_key is None for r in PilotEncounter.query.all())
    assert 'virtual-person-' not in json.dumps(batch.report, ensure_ascii=False)


def test_fingerprint_preserves_registration_time_precision(pilot_env):
    batch = create(pilot_env, [('2024-01-01 09:00:00', 70, '模拟', 'virtual-person'), ('2024-01-01 10:00:00', 70, '模拟', 'virtual-person')], headers=('挂号时间', '年龄', '诊断', '病案号'))
    assert batch.report['duplicate_groups'] == []
    confirm(pilot_env, batch)
    assert PilotEncounter.query.count() == 2


@pytest.mark.parametrize('new_age,new_diagnosis', [(70, '模拟修订诊断'), (71, '模拟原诊断')])
def test_changed_content_same_patient_timestamp_requires_overlap_review(pilot_env, new_age, new_diagnosis):
    env = pilot_env
    headers = ('挂号时间', '年龄', '诊断', '病案号')
    original = create(env, [('2024-01-01 09:00:00', 70, '模拟原诊断', 'SYNTHETIC-1')], headers=headers)
    confirm(env, original)
    changed = create(env, [('2024-01-01 09:00:00', new_age, new_diagnosis, 'SYNTHETIC-1')], headers=headers)
    assert changed.report['counts']['overlap'] == 1
    group = changed.report['duplicate_groups'][0]
    with pytest.raises(ValueError, match='逐组'):
        confirm(env, changed)
    assert PilotEncounter.query.filter_by(active=True).count() == 1
    confirm(env, changed, duplicates={group['id']: 'keep_all'})
    assert PilotEncounter.query.filter_by(active=True).count() == 2


def test_changed_content_without_encounter_id_can_use_explicit_batch_revision(pilot_env):
    env = pilot_env
    headers = ('挂号时间', '年龄', '诊断', '病案号')
    original = create(env, [('2024-01-01 09:00:00', 70, '模拟原诊断', 'SYNTHETIC-1')], headers=headers)
    confirm(env, original)
    changed = create(env, [('2024-01-01 09:00:00', 71, '模拟修订诊断', 'SYNTHETIC-1')], headers=headers, revision=original.id)
    assert changed.report['duplicate_groups'] == []
    confirm(env, changed, confirm_revision=True)
    assert PilotEncounter.query.filter_by(active=True).one().age == 71
    assert PilotEncounter.query.count() == 2


def test_same_file_coverage_revision_creates_new_version_without_duplicate_encounters(pilot_env):
    env = pilot_env
    content = excel([('2024-01-01', 70, '模拟', 'SYNTHETIC-1')])
    coverage = {'start': '2024-01-01', 'end': '2024-01-03', 'mode': 'partial'}
    first = ingestion.create_batch(env.a, content, 'synthetic.xlsx', env.user.id, coverage, {})
    ingestion.process_batch(first.id)
    confirm(env, first)
    frozen = datasets.create_snapshot(env.a, env.user.id)
    with pytest.raises(ValueError, match='请选择原批次'):
        ingestion.create_batch(env.a, content, 'synthetic.xlsx', env.user.id, {**coverage, 'mode': 'complete'}, {})
    revision = ingestion.create_batch(env.a, content, 'synthetic.xlsx', env.user.id, {**coverage, 'mode': 'complete', 'revision_of': first.id}, {})
    assert revision.id != first.id and revision.sha256 == first.sha256
    assert revision.idempotency_key != first.idempotency_key and revision.storage_path != first.storage_path
    ingestion.process_batch(revision.id)
    confirm(env, revision, confirm_revision=True)
    assert PilotEncounter.query.filter_by(active=True).count() == 1
    assert all(c.status == 'complete' for c in PilotCoverage.query.all())
    datasets.build_snapshot(frozen.id)
    with zipfile.ZipFile(io.BytesIO(datasets.export_snapshot(frozen))) as archive:
        rows = list(csv.DictReader(io.TextIOWrapper(archive.open('daily.csv'), encoding='utf-8')))
    assert all(row['coverage_status'] == 'partial' and row['cases_60plus'] == '' for row in rows)
    retry = ingestion.create_batch(env.a, content, 'synthetic.xlsx', env.user.id, {**coverage, 'mode': 'complete', 'revision_of': first.id}, {})
    assert retry.id == revision.id


def test_wrong_unconfirmed_coverage_can_be_corrected_with_same_file(pilot_env):
    env = pilot_env
    content = excel([('2024-01-01', 70, '模拟', 'SYNTHETIC-1')])
    wrong = {'start': '2024-01-02', 'end': '2024-01-03', 'mode': 'complete'}
    first = ingestion.create_batch(env.a, content, 'synthetic.xlsx', env.user.id, wrong)
    ingestion.process_batch(first.id)
    assert first.status == 'invalid'
    fixed = ingestion.create_batch(env.a, content, 'synthetic.xlsx', env.user.id, {**wrong, 'start': '2024-01-01'})
    assert fixed.id == first.id and fixed.status == 'queued' and fixed.report == {}
    ingestion.process_batch(fixed.id)
    confirm(env, fixed)
    assert PilotEncounter.query.count() == 1


def test_preallocated_snapshot_retry_does_not_refreeze_changed_data(pilot_env):
    from uuid import uuid4
    env = pilot_env
    first = create(env, [('2024-01-01', 70, '模拟', 'SYNTHETIC-1')])
    confirm(env, first)
    reserved = str(uuid4())
    frozen = datasets.create_snapshot(env.a, env.user.id, snapshot_id=reserved)
    second = create(env, [('2024-01-02', 71, '模拟', 'SYNTHETIC-2')])
    confirm(env, second)
    retry = datasets.create_snapshot(env.a, env.user.id, snapshot_id=reserved)
    assert retry.id == frozen.id and retry.storage_path == frozen.storage_path
    datasets.build_snapshot(retry.id)
    with zipfile.ZipFile(io.BytesIO(datasets.export_snapshot(retry))) as archive:
        rows = list(csv.DictReader(io.TextIOWrapper(archive.open('daily.csv'), encoding='utf-8')))
    assert [r['cases_60plus'] for r in rows] == ['1', '0', '0']
    with pytest.raises(ValueError, match='不符'):
        datasets.create_snapshot(env.b, env.user.id, snapshot_id=reserved)


def test_reparse_confirmed_batch_preserves_import_receipt(pilot_env):
    batch = create(pilot_env, [('2024-01-01', 70, '模拟', 'SYNTHETIC-1')])
    confirmed = confirm(pilot_env, batch)
    assert confirmed['imported_rows'] == 1
    report = ingestion.process_batch(batch.id)
    assert report == confirmed and report['imported_rows'] == 1
    assert batch.status == 'confirmed'


def test_concurrent_same_file_upload_has_one_durable_batch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    app = Flask('pilot-concurrent-test')
    app.config.update(SQLALCHEMY_DATABASE_URI=f'sqlite:///{tmp_path / "concurrency.sqlite"}',
        PILOT_STORAGE_DIR=str(tmp_path / 'private'), PILOT_STORAGE_KEY=Fernet.generate_key().decode())
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username='synthetic-concurrent', password_hash='unused')
        inst = PilotInstitution(name='并发合成机构', region_code='360428', latitude=29.2, longitude=116.3,
            enabled=True, raw_storage_approved=True)
        db.session.add_all([user, inst])
        db.session.commit()
        user_id, institution_id = user.id, inst.id
    content = excel([('2024-01-01', 70, '模拟', 'SYNTHETIC-1')])
    barrier = Barrier(2)
    def upload_once():
        with app.app_context():
            inst = db.session.get(PilotInstitution, institution_id)
            barrier.wait(timeout=5)
            batch = ingestion.create_batch(inst, content, 'synthetic.xlsx', user_id,
                {'start': '2024-01-01', 'end': '2024-01-03', 'mode': 'complete'})
            return batch.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload_once) for _ in range(2)]
        ids = [future.result(timeout=10) for future in futures]
    with app.app_context():
        assert ids[0] == ids[1]
        assert PilotImportBatch.query.count() == 1
        db.session.remove()
        db.engine.dispose()


def test_migration_valid_existing_schema_is_idempotent(pilot_env):
    migration = importlib.import_module('migrations.versions.0033_institution_data_pilot')
    with db.engine.connect() as connection:
        before = set(inspect(connection).get_table_names())
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert set(inspect(connection).get_table_names()) == before
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM pilot_institutions').scalar_one() == 2


@pytest.mark.parametrize('damage,reason', [
    ('DROP TABLE pilot_model_activations', 'partial_schema'),
    ('ALTER TABLE pilot_institutions ADD COLUMN untrusted TEXT', 'invalid_columns'),
    ('DROP INDEX ix_pilot_encounters_suspected_key', 'invalid_indexes'),
])
def test_migration_rejects_partial_or_incompatible_schema_before_writes(damage, reason):
    migration = importlib.import_module('migrations.versions.0033_institution_data_pilot')
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            connection.exec_driver_sql(damage)
            before = connection.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').all()
            with pytest.raises(RuntimeError, match=reason):
                migration.upgrade()
            after = connection.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').all()
            assert before == after
    engine.dispose()


def test_migration_rejects_broadened_unique_index_predicate():
    migration = importlib.import_module('migrations.versions.0033_institution_data_pilot')
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            connection.exec_driver_sql('DROP INDEX uq_pilot_active_source')
            connection.exec_driver_sql('CREATE UNIQUE INDEX uq_pilot_active_source ON pilot_encounters (institution_id,source_key)')
            with pytest.raises(RuntimeError, match='invalid_index=uq_pilot_active_source'):
                migration.upgrade()
    engine.dispose()


def test_migration_mid_upgrade_failure_rolls_back_every_pilot_table(monkeypatch, tmp_path):
    migration = importlib.import_module('migrations.versions.0033_institution_data_pilot')
    engine = create_engine(f'sqlite:///{tmp_path / "migration-rollback.sqlite"}')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
    original = migration._create_schema
    def fail_after_first(operations):
        class InterruptedOperations:
            calls = 0
            def create_table(self, *args, **kwargs):
                self.calls += 1
                result = operations.create_table(*args, **kwargs)
                if self.calls == 2:
                    raise RuntimeError('synthetic-interrupted-ddl')
                return result
            def create_index(self, *args, **kwargs):
                return operations.create_index(*args, **kwargs)
        original(InterruptedOperations())
    monkeypatch.setattr(migration, '_create_schema', fail_after_first)
    with engine.connect() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match='synthetic-interrupted'):
                migration.upgrade()
            connection.rollback()
        assert not set(migration.TABLE_NAMES).intersection(inspect(connection).get_table_names())
    engine.dispose()
