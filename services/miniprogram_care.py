# -*- coding: utf-8 -*-
"""小程序照护私密读写：健康同意、日记、用药、筛查记录。本轮不返回健康概率。"""
from __future__ import annotations

import json
from datetime import datetime

from core.db_models import (
    FamilyMember,
    HealthDiary,
    HealthRiskAssessment,
    MedicationReminder,
    Pair,
)
from core.extensions import db
from core.time_utils import today_local, utcnow
from services.family_access import can_access_pair
from services.miniprogram_auth import current_privacy_version
from services.miniprogram_bootstrap import get_bootstrap_payload
from utils.parsers import safe_json_loads


class MiniProgramCareError(Exception):
    def __init__(self, code, message, status_code=400, extra=None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


def health_consent_payload(user):
    required = current_privacy_version()
    current = str(getattr(user, "health_sensitive_consent_version", None) or "").strip()
    consented_at = getattr(user, "health_sensitive_consented_at", None)
    return {
        "required_health_consent_version": required,
        "health_consent_version": current or None,
        "health_consented_at": consented_at.isoformat() if consented_at else None,
        "current": bool(current) and current == required,
    }


def require_current_health_consent(user):
    payload = health_consent_payload(user)
    if payload["current"]:
        return payload
    raise MiniProgramCareError(
        "health_sensitive_consent_required",
        "请先阅读并单独同意健康敏感个人信息处理说明。",
        428,
        extra={
            "required_health_consent_version": payload["required_health_consent_version"],
            "data": {
                "required_health_consent_version": payload["required_health_consent_version"],
            },
        },
    )


def save_health_consent(user, *, consent, version):
    required = current_privacy_version()
    if consent is not True:
        raise MiniProgramCareError(
            "health_sensitive_consent_required",
            "必须明确同意后才能使用健康功能。",
            400,
            extra={"required_health_consent_version": required, "data": {"required_health_consent_version": required}},
        )
    submitted = str(version or "").strip()
    if submitted != required:
        raise MiniProgramCareError(
            "health_consent_version_mismatch",
            "健康敏感信息处理说明已更新，请重新阅读并确认。",
            400,
            extra={"required_health_consent_version": required, "data": {"required_health_consent_version": required}},
        )
    user.health_sensitive_consent_version = required
    user.health_sensitive_consented_at = utcnow()
    db.session.commit()
    return health_consent_payload(user)


def withdraw_health_consent(user):
    user.health_sensitive_consent_version = None
    user.health_sensitive_consented_at = None
    db.session.commit()
    return health_consent_payload(user)


def _owned_pair(user, pair_id):
    try:
        pair_id = int(pair_id or 0)
    except (TypeError, ValueError):
        raise MiniProgramCareError("invalid_pair_id", "照护对象无效。", 400)
    pair = Pair.query.filter_by(id=pair_id, status="active").first()
    if not pair or not can_access_pair(user, pair, "read"):
        raise MiniProgramCareError("not_found", "对象不存在或无权访问。", 404)
    return pair


def resolve_member(user, payload=None, args=None):
    source = payload if isinstance(payload, dict) else {}
    query = args if isinstance(args, dict) else {}
    pair_id = source.get("pair_id") or query.get("pair_id")
    member_id = source.get("member_id") or query.get("member_id")
    pair = None
    member = None
    if pair_id:
        pair = _owned_pair(user, pair_id)
        if pair.member_id:
            member = db.session.get(FamilyMember, pair.member_id)
    elif member_id:
        try:
            member_id = int(member_id)
        except (TypeError, ValueError):
            raise MiniProgramCareError("invalid_member_id", "家人档案无效。", 400)
        member = FamilyMember.query.filter_by(id=member_id).first()
        if member is None:
            raise MiniProgramCareError("not_found", "对象不存在或无权访问。", 404)
        pair = Pair.query.filter_by(member_id=member.id, status="active").first()
        if pair is None or not can_access_pair(user, pair, "read"):
            raise MiniProgramCareError("not_found", "对象不存在或无权访问。", 404)
    return pair, member


def deactivate_pair(user, pair_id):
    pair = _owned_pair(user, pair_id)
    if not can_access_pair(user, pair, "manage"):
        raise MiniProgramCareError("not_found", "对象不存在或无权访问。", 404)
    pair.status = "inactive"
    pair.last_active_at = utcnow()
    db.session.commit()
    return {"pair_id": pair.id, "status": "inactive"}


def _diary_json(record):
    return {
        "id": record.id,
        "member_id": record.member_id,
        "entry_date": record.entry_date.isoformat() if record.entry_date else None,
        "symptoms": record.symptoms or "",
        "severity": record.severity or "",
        "notes": record.notes or "",
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def list_or_create_diary(user, *, method, payload, args):
    require_current_health_consent(user)
    pair, member = resolve_member(user, payload=payload, args=args)
    if method == "GET":
        try:
            limit = int((args or {}).get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 50))
        query = HealthDiary.query.filter_by(user_id=user.id)
        if member is not None:
            query = query.filter_by(member_id=member.id)
        records = query.order_by(HealthDiary.entry_date.desc(), HealthDiary.id.desc()).limit(limit).all()
        return {"items": [_diary_json(row) for row in records]}

    entry_date = today_local()
    raw_date = str((payload or {}).get("entry_date") or "").strip()
    if raw_date:
        try:
            entry_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise MiniProgramCareError("invalid_entry_date", "日期无效。", 400) from exc
    if entry_date > today_local():
        raise MiniProgramCareError("future_entry_date", "不能填写未来日期。", 400)
    severity = str((payload or {}).get("severity") or "").strip()
    if severity not in {"none", "mild", "moderate", "severe", "无", "轻微", "中等", "严重"}:
        raise MiniProgramCareError("invalid_severity", "不适程度无效。", 400)
    symptoms = str((payload or {}).get("symptoms") or "").strip()[:200]
    notes = str((payload or {}).get("notes") or "").strip()[:500]
    if not symptoms and not notes:
        raise MiniProgramCareError("diary_content_required", "请至少填写症状或备注。", 400)
    record = HealthDiary(
        user_id=user.id,
        member_id=member.id if member else None,
        entry_date=entry_date,
        symptoms=symptoms,
        severity=severity,
        notes=notes,
        created_at=utcnow(),
    )
    db.session.add(record)
    db.session.commit()
    payload = _diary_json(record)
    return {"item": payload, "entry": payload, "id": record.id, "pair_id": pair.id if pair else None}


def _medication_json(record):
    return {
        "id": record.id,
        "member_id": record.member_id,
        "medicine_name": record.medicine_name,
        "dosage": record.dosage or "",
        "frequency": record.frequency or "daily",
        "time_of_day": record.time_of_day or "",
        "weather_triggers": safe_json_loads(record.weather_triggers, []),
        "is_active": bool(record.is_active),
    }


def list_or_mutate_medications(user, *, method, payload, args):
    require_current_health_consent(user)
    if method == "GET":
        pair, member = resolve_member(user, payload=payload, args=args)
        query = MedicationReminder.query.filter_by(user_id=user.id)
        if member is not None:
            query = query.filter_by(member_id=member.id)
        items = query.order_by(MedicationReminder.id.desc()).limit(50).all()
        return {"items": [_medication_json(row) for row in items], "pair_id": pair.id if pair else None}
    if method == "DELETE":
        try:
            record_id = int((payload or {}).get("id") or (args or {}).get("id") or 0)
        except (TypeError, ValueError):
            record_id = 0
        record = MedicationReminder.query.filter_by(id=record_id, user_id=user.id).first()
        if record is None:
            raise MiniProgramCareError("not_found", "用药提醒不存在。", 404)
        db.session.delete(record)
        db.session.commit()
        return {"deleted_id": record_id}

    pair, member = resolve_member(user, payload=payload, args=args)
    medicine_name = str((payload or {}).get("medicine_name") or "").strip()[:100]
    if not medicine_name:
        raise MiniProgramCareError("missing_medicine_name", "请填写药品名称。", 400)
    frequency = str((payload or {}).get("frequency") or "daily").strip() or "daily"
    if frequency not in {"daily", "weekly", "as_needed"}:
        raise MiniProgramCareError("invalid_frequency", "用药频率无效。", 400)
    time_of_day = str((payload or {}).get("time_of_day") or "").strip()[:10]
    if time_of_day:
        try:
            datetime.strptime(time_of_day, "%H:%M")
        except ValueError as exc:
            raise MiniProgramCareError("invalid_time_of_day", "用药时间无效。", 400) from exc
    record = MedicationReminder(
        user_id=user.id,
        member_id=member.id if member else None,
        medicine_name=medicine_name,
        dosage=str((payload or {}).get("dosage") or "").strip()[:100],
        frequency=frequency,
        time_of_day=time_of_day or None,
        weather_triggers=json.dumps((payload or {}).get("weather_triggers") or [], ensure_ascii=False),
        is_active=True,
        created_at=utcnow(),
    )
    db.session.add(record)
    db.session.commit()
    payload = _medication_json(record)
    return {"medication": payload, "id": record.id, "pair_id": pair.id if pair else None}


def delete_medication(user, record_id):
    require_current_health_consent(user)
    record = MedicationReminder.query.filter_by(id=record_id, user_id=user.id).first()
    if record is None:
        raise MiniProgramCareError("not_found", "用药提醒不存在。", 404)
    db.session.delete(record)
    db.session.commit()
    return {"deleted_id": record_id}


def _assessment_json(record):
    if record is None:
        return None
    return {
        "id": record.id,
        "member_id": getattr(record, "member_id", None),
        "assessment_date": record.assessment_date.isoformat() if record.assessment_date else None,
        "weather_condition": record.weather_condition or "",
        "risk_score": None,
        "risk_level": "已记录",
        "disease_risks": {},
        "recommendations": [
            "本轮不展示健康概率或诊断结论，筛查只作照护记录。",
            "身体明显不适时请直接联系家人并及时就医或求助。",
        ],
        "explain": safe_json_loads(record.explain, {}),
    }


def get_or_record_assessment(user, *, method, payload, args):
    require_current_health_consent(user)
    pair, member = resolve_member(user, payload=payload, args=args)
    query = HealthRiskAssessment.query.filter_by(user_id=user.id)
    if getattr(HealthRiskAssessment, "member_id", None) is not None and member is not None:
        query = query.filter_by(member_id=member.id)
    if method == "GET":
        latest = query.order_by(
            HealthRiskAssessment.assessment_date.desc(),
            HealthRiskAssessment.id.desc(),
        ).first()
        return {"latest": _assessment_json(latest), "pair_id": pair.id if pair else None}

    allowed = {
        "outdoor_exposure": {"low", "medium", "high"},
        "symptom_level": {"none", "mild", "moderate", "severe"},
        "hydration": {"good", "normal", "poor"},
        "medication_adherence": {"good", "partial", "poor"},
        "sleep_quality": {"good", "fair", "poor"},
    }
    screening = {}
    for field, choices in allowed.items():
        value = str((payload or {}).get(field) or "").strip()
        if value not in choices:
            raise MiniProgramCareError(f"invalid_{field}", "筛查选项不完整或无效。", 400)
        screening[field] = value
    snapshot = get_bootstrap_payload()
    current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
    weather_condition = str(current.get("condition") or current.get("text") or "")[:100]
    explain = {
        "screening": screening,
        "snapshot_id": snapshot.get("snapshot_id"),
        "probability_hold": True,
        "pair_id": pair.id if pair else None,
        "member_id": member.id if member else None,
    }
    record = HealthRiskAssessment(
        user_id=user.id,
        assessment_date=utcnow(),
        weather_condition=weather_condition,
        risk_score=None,
        risk_level="已记录",
        disease_risks=json.dumps({}, ensure_ascii=False),
        recommendations=json.dumps(
            ["本轮不展示健康概率或诊断结论，筛查只作照护记录。"],
            ensure_ascii=False,
        ),
        explain=json.dumps(explain, ensure_ascii=False),
    )
    if hasattr(record, "member_id"):
        record.member_id = member.id if member else None
    db.session.add(record)
    db.session.commit()
    return {"assessment": _assessment_json(record), "pair_id": pair.id if pair else None}
