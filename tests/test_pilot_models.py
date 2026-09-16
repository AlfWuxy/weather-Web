"""计数模型重点验收：真实算法、冻结划分、数据绑定、机构权限和预测证据。"""
import copy
import csv
import hashlib
import io
import json
import math
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from cryptography.fernet import Fernet
from flask import Flask

from services.data_workbench.count_model import (
    FEATURE_VERSION, OUTCOME, ModelValidationError, canonical_bytes,
    predict_daily, read_snapshot, train_snapshot, validate_bundle,
)


def make_snapshot(days=760, dataset_id="snapshot-1", institution_id="institution-1", override=None):
    rng = np.random.default_rng(731)
    start = date(2023, 1, 1)
    rows = []
    for index in range(days):
        day = start + timedelta(days=index)
        temperature = 18 + 12 * math.sin(index * 2 * math.pi / 365.2425) + rng.normal(0, 2)
        mu = math.exp(2.2 + .08 * (day.weekday() == 1) + .01 * max(0, temperature - 25))
        count = int(rng.negative_binomial(8, 8 / (8 + mu)))
        rows.append({"date": day.isoformat(), "cases_60plus": count, "coverage_status": "complete",
                     "tmean": temperature, "rh_mean": 60, "precipitation": 1,
                     "weather_source": "Open-Meteo", "weather_product": "era5"})
    if override:
        override(rows)
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files = {"daily.csv": csv_buffer.getvalue().encode(), "coverage.csv": b"date,status\n", "dictionary.json": b"{}"}
    manifest = {"schema_version": "pilot.dataset.v1", "dataset_id": dataset_id, "institution_id": institution_id,
                "region_code": "360428", "outcome": OUTCOME, "age_min": 60, "feature_version": FEATURE_VERSION,
                "date_start": rows[0]["date"], "date_end": rows[-1]["date"], "cutoff_date": rows[-1]["date"],
                "weather_product": "era5", "files": {key: {"sha256": hashlib.sha256(value).hexdigest()} for key, value in files.items()}}
    files["manifest.json"] = canonical_bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for key, value in files.items():
            archive.writestr(zipfile.ZipInfo(key, (2020, 1, 1, 0, 0, 0)), value)
    return output.getvalue()


@pytest.fixture(scope="module")
def trained():
    raw = make_snapshot()
    return raw, train_snapshot(raw)


def test_actual_nb2_training_is_reproducible_and_time_held_out(trained):
    raw, bundle = trained
    assert train_snapshot(raw) == bundle
    assert bundle["exploratory"] is False
    assert bundle["training_days"] == 760 - 180 - 7
    periods = bundle["periods"]
    assert date.fromisoformat(periods["train"]["end"]) + timedelta(days=1) == date.fromisoformat(periods["development"]["start"])
    assert date.fromisoformat(periods["development"]["end"]) + timedelta(days=1) == date.fromisoformat(periods["holdout"]["start"])
    for models in bundle["metrics"].values():
        assert models["nb2_calendar"]["n_days"] == 90
        assert models["nb2_thermal"]["seven_day_windows"] == 84
    assert .001 < bundle["model"]["alpha"] < 1
    manifest, rows, digest = read_snapshot(raw)
    result = validate_bundle(bundle, manifest=manifest, dataset_sha256=digest, snapshot_rows=rows)
    assert result["snapshot_verified"] and result["eligible_for_activation"]


def test_short_training_is_exploratory():
    bundle = train_snapshot(make_snapshot(days=400))
    assert bundle["exploratory"] is True
    assert validate_bundle(bundle)["eligible_for_activation"] is False


def test_holdout_counts_do_not_change_parameters_or_model_selection(trained):
    raw, bundle = trained
    changed = make_snapshot(override=lambda rows: [row.update(cases_60plus=int(row["cases_60plus"]) + 20) for row in rows[-90:]])
    other = train_snapshot(changed)
    assert other["model"] == bundle["model"]
    assert other["baseline_model"] == bundle["baseline_model"]
    assert other["metrics"]["development"] == bundle["metrics"]["development"]
    assert other["metrics"]["holdout"] != bundle["metrics"]["holdout"]


def test_snapshot_rejects_hash_tamper_and_extra_members(trained):
    raw, _ = trained
    for replace_name in ("daily.csv", "../payload.py"):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                content = source.read(item.filename)
                target.writestr(item, content + b"x" if item.filename == replace_name else content)
            if replace_name.startswith(".."):
                target.writestr(replace_name, "print('not run')")
        with pytest.raises(ModelValidationError):
            read_snapshot(output.getvalue())


@pytest.mark.parametrize("change", [
    lambda b: b.update(python="import os"),
    lambda b: b.update(age_min=40),
    lambda b: b.update(evaluation_kind="prospective_forecast"),
    lambda b: b["model"]["coefficients"].append(0),
    lambda b: b["model"].update(alpha=float("nan")),
    lambda b: b["golden"][0].update(mean=999),
    lambda b: b["periods"]["holdout"].update(start="2025-01-01"),
    lambda b: b.update(exploratory=True),
])
def test_rejects_unsafe_or_inconsistent_json(trained, change):
    _, bundle = trained
    invalid = copy.deepcopy(bundle)
    change(invalid)
    with pytest.raises((ModelValidationError, ValueError)):
        validate_bundle(invalid)


def test_server_recomputes_metrics_and_temperature_quantiles(trained):
    raw, bundle = trained
    manifest, rows, digest = read_snapshot(raw)
    for key in ("metrics", "parameters"):
        invalid = copy.deepcopy(bundle)
        if key == "metrics":
            invalid["metrics"]["holdout"]["nb2_calendar"]["mae"] += 0.1
        else:
            for name in ("model", "baseline_model", "thermal_model"):
                invalid[name]["feature_params"]["temperature_p10"] += 1
        with pytest.raises(ModelValidationError):
            validate_bundle(invalid, manifest, digest, rows)


def test_partial_days_are_not_zero_and_break_seven_day_labels():
    def modify(rows):
        rows[-20].update(coverage_status="partial", cases_60plus="")
    raw = make_snapshot(override=modify)
    bundle = train_snapshot(raw)
    assert bundle["metrics"]["holdout"]["nb2_calendar"]["n_days"] == 89
    assert bundle["metrics"]["holdout"]["nb2_calendar"]["seven_day_windows"] == 77
    with pytest.raises(ModelValidationError):
        read_snapshot(make_snapshot(override=lambda rows: rows[-1].update(coverage_status="partial")))


def test_cli_writes_safe_artifact_and_will_not_overwrite(tmp_path):
    raw = make_snapshot(days=400)
    source, target = tmp_path / "snapshot.zip", tmp_path / "candidate.json"
    source.write_bytes(raw)
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/pilot_train.py"), str(source), "--output", str(target)]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(target.read_text())["exploratory"] is True
    original = target.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert target.read_bytes() == original


@pytest.fixture
def registry(tmp_path):
    from core.extensions import db
    from core.db_models import User
    from core.pilot_models import PilotInstitution, PilotMembership
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'registry.sqlite'}", SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      PILOT_STORAGE_DIR=str(tmp_path / "encrypted"), PILOT_STORAGE_KEY=Fernet.generate_key().decode())
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(id=987, username="pilot-model-test", password_hash="disabled-test-only")
        first = PilotInstitution(id="institution-1", name="测试机构甲", region_code="360428", latitude=29.27,
                                 longitude=116.20, enabled=True, raw_storage_approved=True)
        second = PilotInstitution(id="institution-2", name="测试机构乙", region_code="360428", latitude=29.28,
                                  longitude=116.21, enabled=True, raw_storage_approved=True)
        db.session.add_all([user, first, second])
        db.session.flush()
        db.session.add(PilotMembership(institution_id=first.id, user_id=user.id, role="researcher", active=True))
        db.session.commit()
        yield first, second, user
        db.session.remove()
        db.drop_all()


def store_snapshot(raw, user):
    from core.extensions import db
    from core.pilot_models import PilotDatasetSnapshot
    from services.data_workbench.storage import store_bytes
    manifest, _, digest = read_snapshot(raw)
    snapshot = PilotDatasetSnapshot(id=manifest["dataset_id"], institution_id=manifest["institution_id"],
                                     created_by=user.id, status="ready", manifest=manifest, sha256=digest,
                                     storage_path=store_bytes(raw, kind="snapshot"))
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


def test_institution_isolation_manual_activation_and_holdout_reuse(registry, trained):
    from services.data_workbench.model_registry import import_model, activate_model
    inst, other, user = registry
    raw, bundle = trained
    store_snapshot(raw, user)
    with pytest.raises(ModelValidationError, match="权限"):
        import_model(other, bundle, user.id)
    version = import_model(inst, copy.deepcopy(bundle), user.id)
    assert version.status == "candidate" and inst.current_model_id is None
    assert import_model(inst, copy.deepcopy(bundle), user.id).id == version.id
    activate_model(inst, version.id, user.id)
    assert inst.current_model_id == version.id and version.status == "active"
    revised = copy.deepcopy(bundle)
    revised["name"] = "已查看相同保留集的第二次候选"
    repeated = import_model(inst, revised, user.id)
    assert not repeated.validation["holdout_independent"]
    with pytest.raises(ModelValidationError, match="保留窗口"):
        activate_model(inst, repeated.id, user.id)


def test_exploratory_model_cannot_be_activated(registry):
    from services.data_workbench.model_registry import import_model, activate_model
    inst, _, user = registry
    raw = make_snapshot(days=400)
    store_snapshot(raw, user)
    version = import_model(inst, train_snapshot(raw), user.id)
    with pytest.raises(ModelValidationError, match="探索"):
        activate_model(inst, version.id, user.id)


def forecast_payload(day, missing=False):
    days = [(day + timedelta(days=i)).isoformat() for i in range(-7, 8)]
    temperatures = [19.3] * len(days)
    if missing:
        temperatures[-1] = None
    return {"request": {"models": "gfs_global"}, "response": {"daily": {"time": days, "temperature_2m_mean": temperatures}}}


def test_forecasts_preserve_receipts_raw_means_and_no_backdating(registry, trained, monkeypatch):
    from core.pilot_models import PilotForecastReceipt, PilotForecastRun
    from services.data_workbench import predictions
    from services.data_workbench.model_registry import import_model, activate_model
    inst, _, user = registry
    raw, bundle = trained
    store_snapshot(raw, user)
    version = import_model(inst, copy.deepcopy(bundle), user.id)
    activate_model(inst, version.id, user.id)
    now = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(predictions, "utcnow", lambda: now)
    run = predictions.generate_forecast(inst, fetcher=lambda _: forecast_payload(date(2026, 9, 15)))
    receipt = PilotForecastReceipt.query.one()
    assert predictions._aware(receipt.received_at) <= predictions._aware(run.issued_at)
    assert run.payload["daily"][0]["date"] == "2026-09-16"
    assert run.payload["day_seven_mean"] == run.payload["daily"][-1]["mean"]
    assert run.payload["seven_day_total_mean"] == sum(row["mean"] for row in run.payload["daily"])
    assert "seven_day_lower_95" not in run.payload
    assert predictions.generate_forecast(inst, fetcher=lambda _: pytest.fail("重复运行不再次抓取")).id == run.id
    assert PilotForecastRun.query.count() == 1
    with pytest.raises(ModelValidationError, match="不能补造"):
        predictions.generate_forecast(inst, issued_at=now - timedelta(days=7))


def test_missing_forecast_input_is_unavailable_and_receipt_remains(registry, trained, monkeypatch):
    from core.pilot_models import PilotForecastReceipt
    from services.data_workbench import predictions
    from services.data_workbench.model_registry import import_model, activate_model
    inst, _, user = registry
    raw, bundle = trained
    store_snapshot(raw, user)
    version = import_model(inst, copy.deepcopy(bundle), user.id)
    activate_model(inst, version.id, user.id)
    monkeypatch.setattr(predictions, "utcnow", lambda: datetime(2026, 9, 15, 1, tzinfo=timezone.utc))
    run = predictions.generate_forecast(inst, fetcher=lambda _: forecast_payload(date(2026, 9, 15), missing=True))
    assert run.payload["status"] == "unavailable"
    assert run.payload["daily"] == [] and run.payload["seven_day_total_mean"] is None
    assert PilotForecastReceipt.query.count() == 1


def test_retry_missing_forecast_gets_new_receipt_without_overwriting(registry, trained, monkeypatch):
    from core.pilot_models import PilotForecastReceipt, PilotForecastRun
    from services.data_workbench import predictions
    from services.data_workbench.model_registry import import_model, activate_model
    inst, _, user = registry
    raw, bundle = trained
    store_snapshot(raw, user)
    version = import_model(inst, copy.deepcopy(bundle), user.id)
    activate_model(inst, version.id, user.id)
    now = datetime(2026, 9, 15, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(predictions, 'utcnow', lambda: now)
    failed = predictions.generate_forecast(inst, fetcher=lambda _: forecast_payload(date(2026, 9, 15), missing=True))
    now += timedelta(minutes=5)
    recovered = predictions.generate_forecast(inst, fetcher=lambda _: forecast_payload(date(2026, 9, 15)))
    assert failed.payload['status'] == 'unavailable'
    assert recovered.payload['status'] == 'available'
    assert recovered.receipt_id != failed.receipt_id
    assert predictions._aware(recovered.issued_at) > predictions._aware(failed.issued_at)
    assert predictions.generate_forecast(inst, fetcher=lambda _: pytest.fail('成功后不可重复请求')).id == recovered.id
    assert PilotForecastReceipt.query.count() == PilotForecastRun.query.count() == 2


def test_rollback_records_actor_and_previous_version(registry, trained, monkeypatch):
    from core.pilot_models import PilotModelActivation
    from services.data_workbench import model_registry
    inst, _, user = registry
    raw, bundle = trained
    store_snapshot(raw, user)
    first = model_registry.import_model(inst, copy.deepcopy(bundle), user.id)
    model_registry.activate_model(inst, first.id, user.id)
    newer_raw = make_snapshot(days=860, dataset_id="snapshot-2")
    store_snapshot(newer_raw, user)
    second = model_registry.import_model(inst, train_snapshot(newer_raw), user.id)
    assert second.validation["holdout_independent"]
    real_comparison = model_registry.compare_models(inst, second.id)
    assert real_comparison["same_dates"] and real_comparison["current"]["n_days"] == real_comparison["candidate"]["n_days"]
    # 该用例专测状态切换；误差门槛由真实同窗比较单独覆盖。
    real_comparison["current"]["mean_nll"] = real_comparison["candidate"]["mean_nll"] + 1
    monkeypatch.setattr(model_registry, "compare_models", lambda *_: real_comparison)
    model_registry.activate_model(inst, second.id, user.id)
    assert inst.current_model_id == second.id
    model_registry.rollback_model(inst, user.id)
    assert inst.current_model_id == first.id
    assert PilotModelActivation.query.count() == 3
    assert {row.user_id for row in PilotModelActivation.query.all()} == {user.id}


def test_real_excel_to_frozen_zip_local_cli_and_server_activation(registry, tmp_path):
    from openpyxl import Workbook
    from core.extensions import db
    from core.pilot_models import PilotWeatherDay
    from services.data_workbench import datasets, ingestion
    from services.data_workbench.model_registry import import_model, activate_model
    from services.data_workbench.weather import PRODUCT, SOURCE
    inst, _, user = registry
    _, synthetic, _ = read_snapshot(make_snapshot(days=560))
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["挂号日期", "年龄", "门诊诊断", "就诊编号"])
    for row in synthetic:
        for index in range(int(row["cases_60plus"])):
            sheet.append([row["date"], 60 + index % 35, "合成测试咳嗽", f"virtual-{row['date']}-{index}"])
        # 每天加一条 59 岁记录，检查实际聚合年龄边界而不污染训练人数。
        sheet.append([row["date"], 59, "合成测试咳嗽", f"virtual-younger-{row['date']}"])
    excel = io.BytesIO()
    workbook.save(excel)
    batch = ingestion.create_batch(inst, excel.getvalue(), "synthetic-only.xlsx", user.id,
                                    {"start": synthetic[0]["date"], "end": synthetic[-1]["date"], "mode": "complete"})
    ingestion.process_batch(batch.id)
    assert batch.status == "ready"
    ingestion.confirm_batch(batch.id, user.id)
    for row in synthetic:
        db.session.add(PilotWeatherDay(institution_id=inst.id, date=date.fromisoformat(row["date"]),
                                       tmean=row["tmean"], rh_mean=row["rh_mean"], precipitation=row["precipitation"],
                                       source=SOURCE, product=PRODUCT))
    db.session.commit()
    snapshot = datasets.create_snapshot(inst, user.id)
    assert snapshot.status == "frozen"
    datasets.build_snapshot(snapshot.id)
    assert snapshot.status == "ready"
    exported = datasets.export_snapshot(snapshot)
    manifest, actual_daily, digest = read_snapshot(exported)
    assert manifest["dataset_id"] == snapshot.id and digest == snapshot.sha256
    assert [row["cases_60plus"] for row in actual_daily] == [row["cases_60plus"] for row in synthetic]
    source, target = tmp_path / "real-export.zip", tmp_path / "real-bundle.json"
    source.write_bytes(exported)
    run = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "scripts/pilot_train.py"),
                          str(source), "--output", str(target)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    bundle = json.loads(target.read_text())
    version = import_model(inst, bundle, user.id)
    assert version.validation["snapshot_verified"] is True
    activate_model(inst, version.id, user.id)
    assert inst.current_model_id == version.id


def test_concurrent_forecast_tasks_issue_only_once_and_failure_releases_lock(registry, trained, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from flask import current_app
    from core.extensions import db
    from core.pilot_models import PilotForecastReceipt, PilotForecastRun, PilotInstitution
    from services.data_workbench import predictions
    from services.data_workbench.model_registry import import_model, activate_model
    inst, _, user = registry
    raw, bundle = trained
    store_snapshot(raw, user)
    version = import_model(inst, copy.deepcopy(bundle), user.id)
    activate_model(inst, version.id, user.id)
    monkeypatch.setattr(predictions, "utcnow", lambda: datetime(2026, 9, 15, 1, tzinfo=timezone.utc))
    def fail(_):
        raise ModelValidationError("天气服务失败")
    with pytest.raises(ModelValidationError, match="服务失败"):
        predictions.generate_forecast(inst, fetcher=fail)
    assert PilotForecastReceipt.query.count() == 0
    application = current_app._get_current_object()
    institution_id = inst.id
    barrier, counter_lock = Barrier(2), Lock()
    calls = []
    def fetch(_):
        with counter_lock:
            calls.append(1)
        return forecast_payload(date(2026, 9, 15))
    def generate(_):
        with application.app_context():
            institution = db.session.get(PilotInstitution, institution_id)
            barrier.wait(timeout=10)
            return predictions.generate_forecast(institution, fetcher=fetch).id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(generate, range(2)))
    assert results[0] == results[1]
    assert len(calls) == 1
    db.session.expire_all()
    assert PilotForecastRun.query.count() == 1 and PilotForecastReceipt.query.count() == 1


def test_collects_weather_before_model_and_reuses_actual_receipt(registry, trained, monkeypatch):
    from core.pilot_models import PilotForecastReceipt, PilotForecastRun
    from services.data_workbench import predictions
    from services.data_workbench.model_registry import import_model, activate_model
    inst, _, user = registry
    monkeypatch.setattr(predictions, "utcnow", lambda: datetime(2026, 9, 15, 1, tzinfo=timezone.utc))
    receipt = predictions.collect_forecast_receipt(inst, fetcher=lambda _: forecast_payload(date(2026, 9, 15)))
    assert PilotForecastRun.query.count() == 0 and inst.current_model_id is None
    assert predictions.collect_forecast_receipt(inst, fetcher=lambda _: pytest.fail("当天回执不得覆盖")).id == receipt.id
    raw, bundle = trained
    store_snapshot(raw, user)
    version = import_model(inst, copy.deepcopy(bundle), user.id)
    activate_model(inst, version.id, user.id)
    monkeypatch.setattr(predictions, "utcnow", lambda: datetime(2026, 9, 15, 2, tzinfo=timezone.utc))
    run = predictions.generate_forecast(inst, fetcher=lambda _: pytest.fail("已有真实回执应复用"))
    assert run.receipt_id == receipt.id and run.payload["status"] == "available"
    assert PilotForecastReceipt.query.count() == 1
