"""候选计数模型登记与人工启用；不接触既有 RR 模型服务。"""
from __future__ import annotations

import hashlib

from sqlalchemy import update

from core.extensions import db
from core.pilot_models import (PilotDatasetSnapshot, PilotInstitution, PilotMembership,
                               PilotModelActivation, PilotModelVersion)
from services.data_workbench.count_model import (ModelValidationError, _require,
                                                _usable_rows, canonical_bytes,
                                                metrics_for, predict_daily, read_snapshot, validate_bundle)
from services.data_workbench.storage import read_bytes


def _grant(inst, user_id):
    _require(inst is not None and inst.enabled, "机构试点未开放")
    member = PilotMembership.query.filter_by(institution_id=inst.id, user_id=user_id, active=True).first()
    _require(member is not None and member.role in {"researcher", "manager"}, "该账户未获得本机构模型管理权限")


def _snapshot(inst, dataset_id):
    snapshot = PilotDatasetSnapshot.query.filter_by(id=dataset_id, institution_id=inst.id).first()
    _require(snapshot is not None and snapshot.status == "ready" and snapshot.storage_path and snapshot.sha256,
             "没有可用的本机构冻结数据版本")
    manifest, rows, digest = read_snapshot(read_bytes(snapshot.storage_path))
    _require(digest == snapshot.sha256 and manifest == snapshot.manifest, "服务器冻结数据已变化")
    _require(manifest["region_code"] == inst.region_code, "冻结数据地区与当前机构不一致")
    return snapshot, manifest, rows, digest


def _audit(action, version, user_id, extra=None):
    # 后台和命令行不依赖 HTTP 上下文；启用记录在独立表中始终保存。
    from flask import has_request_context
    if has_request_context():
        from core.audit import log_audit
        log_audit(action, "pilot_model", version.id,
                  {"institution_id": version.institution_id, "user_id": user_id, **(extra or {})})


def _overlap(a, b):
    return a["start"] <= b["end"] and b["start"] <= a["end"]


def import_model(inst, payload, user_id):
    _grant(inst, user_id)
    validate_bundle(payload)
    _require(payload["institution_id"] == inst.id and payload["region_code"] == inst.region_code,
             "模型不是当前机构的数据")
    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    prior = PilotModelVersion.query.filter_by(institution_id=inst.id, sha256=digest).first()
    if prior:
        return prior
    snapshot, manifest, rows, snapshot_hash = _snapshot(inst, payload["dataset_id"])
    validation = validate_bundle(payload, manifest=manifest, dataset_sha256=snapshot_hash, snapshot_rows=rows)
    # 与其他登记串行消耗保留窗口，防止并发候选都把同一窗口标为独立。
    db.session.execute(update(PilotInstitution).where(PilotInstitution.id == inst.id).values(enabled=PilotInstitution.enabled))
    db.session.refresh(inst)
    _grant(inst, user_id)
    prior = PilotModelVersion.query.filter_by(institution_id=inst.id, sha256=digest).first()
    if prior:
        db.session.commit()
        return prior
    # 已公开结果的窗口不再伪装成独立保留验证；仍可登记以便诚实比较。
    consumed = [version.id for version in PilotModelVersion.query.filter_by(institution_id=inst.id).all()
                if _overlap(version.payload["periods"]["holdout"], payload["periods"]["holdout"])]
    validation["holdout_independent"] = not consumed
    validation["prior_overlapping_evaluations"] = consumed
    validation["eligible_for_activation"] = validation["eligible_for_activation"] and not consumed
    validation["activation_block_reason"] = ("训练不足一年，仅供探索" if payload["exploratory"]
                                              else "保留窗口已用于评估，请积累全新窗口" if consumed else None)
    version = PilotModelVersion(institution_id=inst.id, dataset_id=snapshot.id,
                                name=payload["name"], family=payload["model"]["family"],
                                status="candidate", payload=payload, sha256=digest,
                                metrics=payload["metrics"], validation=validation, created_by=user_id)
    db.session.add(version)
    db.session.flush()
    _audit("pilot_model_import", version, user_id)
    db.session.commit()
    return version


def compare_models(inst, model_id):
    """对照当前版本时，用候选的同一快照和同一组日期重算双方指标。"""
    candidate = PilotModelVersion.query.filter_by(id=model_id, institution_id=inst.id).first()
    _require(candidate is not None, "模型不存在")
    _, _, rows, digest = _snapshot(inst, candidate.dataset_id)
    period = candidate.payload["periods"]["holdout"]
    evaluation_rows = [row for row in _usable_rows(rows) if period["start"] <= row["date"] <= period["end"]]
    result = {"dataset_id": candidate.dataset_id, "dataset_sha256": digest, "period": period,
              "evaluation_kind": "retrospective_observed_weather",
              "reporting_time_evidence": "historical_availability_unverified", "delay_replay_performed": False,
              "candidate": metrics_for(candidate.payload["model"], evaluation_rows), "current": None,
              "current_model_id": inst.current_model_id, "same_dates": True}
    predicted = predict_daily(candidate.payload["model"], evaluation_rows)
    result["series"] = [{**value, "actual": row["cases_60plus"]} for row, value in zip(evaluation_rows, predicted)]
    if inst.current_model_id:
        current = PilotModelVersion.query.filter_by(id=inst.current_model_id, institution_id=inst.id).first()
        _require(current is not None, "当前模型引用异常")
        result["current"] = metrics_for(current.payload["model"], evaluation_rows)
    return result


def _change_current(inst, version, user_id, comparison=None):
    previous_id = inst.current_model_id
    condition = PilotInstitution.current_model_id.is_(None) if previous_id is None else PilotInstitution.current_model_id == previous_id
    changed = db.session.execute(update(PilotInstitution).where(PilotInstitution.id == inst.id, condition)
                                 .values(current_model_id=version.id), execution_options={"synchronize_session": False})
    _require(changed.rowcount == 1, "模型已由另一操作更新，请刷新后重试")
    previous = db.session.get(PilotModelVersion, previous_id) if previous_id else None
    if previous and previous.id != version.id:
        previous.status = "retired"
    version.status = "active"
    if comparison is not None:
        version.validation = {**version.validation, "activation_comparison": comparison}
    db.session.add(PilotModelActivation(institution_id=inst.id, model_id=version.id,
                                       previous_model_id=previous_id, user_id=user_id))
    _audit("pilot_model_activate", version, user_id, {"previous_model_id": previous_id})
    db.session.commit()
    db.session.refresh(inst)
    return version


def activate_model(inst, model_id, user_id):
    _grant(inst, user_id)
    version = PilotModelVersion.query.filter_by(id=model_id, institution_id=inst.id).first()
    _require(version is not None, "模型不存在")
    if inst.current_model_id == version.id:
        return version
    _require(version.validation.get("eligible_for_activation") is True,
             version.validation.get("activation_block_reason") or "模型未通过启用检查")
    _, manifest, rows, digest = _snapshot(inst, version.dataset_id)
    validate_bundle(version.payload, manifest=manifest, dataset_sha256=digest, snapshot_rows=rows)
    _require(hashlib.sha256(canonical_bytes(version.payload)).hexdigest() == version.sha256, "模型参数已变化")
    comparison = compare_models(inst, model_id)
    if comparison["current"] is not None:
        _require(comparison["candidate"]["mean_nll"] <= comparison["current"]["mean_nll"] + 1e-10,
                 "相同保留窗口的计数分布误差未改善，保留当前版本")
    return _change_current(inst, version, user_id, comparison)


def rollback_model(inst, user_id):
    _grant(inst, user_id)
    _require(inst.current_model_id, "没有正在使用的模型")
    activation = PilotModelActivation.query.filter_by(institution_id=inst.id, model_id=inst.current_model_id).order_by(
        PilotModelActivation.created_at.desc(), PilotModelActivation.id.desc()).first()
    _require(activation is not None and activation.previous_model_id, "没有可回退的上一版本")
    previous = PilotModelVersion.query.filter_by(id=activation.previous_model_id, institution_id=inst.id).first()
    _require(previous is not None and previous.validation.get("eligible_for_activation") is True,
             "上一版本不可用")
    _require(hashlib.sha256(canonical_bytes(previous.payload)).hexdigest() == previous.sha256, "上一模型参数已变化")
    validate_bundle(previous.payload)
    return _change_current(inst, previous, user_id)
