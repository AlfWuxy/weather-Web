"""保存真实天气预报回执及当时计数预测；失败显式不可用，不回退 RR。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import update

from core.extensions import db
from core.pilot_models import (PilotCoverage, PilotEncounter, PilotForecastReceipt,
                               PilotForecastRun, PilotInstitution, PilotModelVersion)
from core.time_utils import utcnow
from services.data_workbench.count_model import (ModelValidationError, _date, _number,
                                                _require, canonical_bytes, predict_daily,
                                                validate_bundle)

FORECAST_PRODUCT = "gfs_global"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fetch_forecast(inst):
    """仅访问固定天气服务地址，不接受模型包提供 URL。"""
    params = {"latitude": inst.latitude, "longitude": inst.longitude,
              "daily": "temperature_2m_mean,relative_humidity_2m_mean,precipitation_sum",
              "past_days": 7, "forecast_days": 8, "timezone": "Asia/Shanghai", "models": FORECAST_PRODUCT}
    try:
        response = requests.get("https://api.open-meteo.com/v1/forecast", params=params,
                                timeout=(5, 30), allow_redirects=False)
        response.raise_for_status()
        _require(len(response.content) <= 2 * 1024 * 1024, "天气回执过大")
        payload = response.json()
        _require(isinstance(payload, dict) and not payload.get("error"), "天气预报响应无效")
    except (requests.RequestException, ValueError) as exc:
        raise ModelValidationError("天气预报暂时无法获取，请稍后重试") from exc
    return {"request": params, "response": payload}


def _forecast_inputs(payload, issued_at):
    daily = payload.get("response", {}).get("daily", {})
    dates = daily.get("time", [])
    temperatures = daily.get("temperature_2m_mean", [])
    _require(isinstance(dates, list) and isinstance(temperatures, list) and len(dates) == len(temperatures),
             "天气日期与温度长度不一致")
    _require(8 <= len(dates) <= 31 and len(set(dates)) == len(dates), "天气预报日历无效")
    mapping = {}
    for day, value in zip(dates, temperatures):
        _date(day)
        mapping[day] = None if value is None else _number(value, -90, 65)
    first_day = _aware(issued_at).astimezone(LOCAL_ZONE).date() + timedelta(days=1)
    rows = []
    for i in range(7):
        day = first_day + timedelta(days=i)
        history = [mapping.get((day - timedelta(days=lag)).isoformat()) for lag in range(8)]
        _require(all(value is not None for value in history), "未来七日或温度滞后输入缺失")
        rows.append({"date": day.isoformat(), "temperature_history": history})
    return rows


def _collect_forecast_locked(inst, fetcher=None, force_refresh=False):
    _require(inst is not None and inst.enabled, "机构试点未开放")
    today = utcnow().astimezone(LOCAL_ZONE).date()
    day_start = datetime.combine(today, datetime.min.time(), LOCAL_ZONE).astimezone(timezone.utc)
    existing = PilotForecastReceipt.query.filter(PilotForecastReceipt.institution_id == inst.id,
                                                 PilotForecastReceipt.received_at >= day_start,
                                                 PilotForecastReceipt.received_at < day_start + timedelta(days=1)).order_by(
        PilotForecastReceipt.received_at.desc()).first()
    if existing and not force_refresh:
        return existing
    payload = (fetcher or fetch_forecast)(inst)
    received_at = utcnow()
    _require(len(canonical_bytes(payload)) <= 2 * 1024 * 1024, "天气回执过大")
    # 回执时刻由服务器读取响应之后产生；绝不把 provider generationtime_ms 当发布时间。
    receipt = PilotForecastReceipt(institution_id=inst.id, issued_at=received_at,
                                   received_at=received_at, provider="Open-Meteo", product=FORECAST_PRODUCT,
                                   payload=payload, sha256=hashlib.sha256(canonical_bytes(payload)).hexdigest())
    db.session.add(receipt)
    db.session.flush()
    return receipt


def collect_forecast_receipt(inst, fetcher=None):
    """机构启用后即可每日采集；尚无模型时只保存天气证据，不生成预测。"""
    try:
        _require(inst is not None, "机构不存在")
        db.session.execute(update(PilotInstitution).where(PilotInstitution.id == inst.id)
                           .values(enabled=PilotInstitution.enabled))
        db.session.refresh(inst)
        receipt = _collect_forecast_locked(inst, fetcher=fetcher)
        db.session.commit()
        return receipt
    except Exception:
        db.session.rollback()
        raise


def _generate_forecast_locked(inst, issued_at=None, fetcher=None):
    """每天至多一个正式预测；明确时间只读取既有记录，禁止事后伪造发布时间。"""
    _require(inst is not None and inst.enabled, "机构试点未开放")
    if issued_at is not None:
        _require(isinstance(issued_at, datetime), "发布时间必须为时间戳")
        existing = PilotForecastRun.query.filter_by(institution_id=inst.id, issued_at=_aware(issued_at)).first()
        _require(existing is not None, "不能补造过去或预填未来的预测记录")
        return existing
    today = utcnow().astimezone(LOCAL_ZONE).date()
    day_start = datetime.combine(today, datetime.min.time(), LOCAL_ZONE).astimezone(timezone.utc)
    existing = PilotForecastRun.query.filter(PilotForecastRun.institution_id == inst.id,
                                             PilotForecastRun.issued_at >= day_start,
                                             PilotForecastRun.issued_at < day_start + timedelta(days=1)).order_by(
        PilotForecastRun.issued_at.desc()).first()
    if existing and existing.payload.get("status") == "available":
        return existing
    model_version = PilotModelVersion.query.filter_by(id=inst.current_model_id, institution_id=inst.id, status="active").first()
    _require(model_version is not None and model_version.validation.get("eligible_for_activation") is True,
             "尚无可用机构计数模型")
    validate_bundle(model_version.payload)
    _require(hashlib.sha256(canonical_bytes(model_version.payload)).hexdigest() == model_version.sha256,
             "当前模型参数已变化")
    # 缺输入的旧结果不可覆盖；明确重试抓取新回执并保留新的接收时间。
    receipt = _collect_forecast_locked(inst, fetcher=fetcher, force_refresh=existing is not None)
    payload, received_at = receipt.payload, _aware(receipt.received_at)
    _require(hashlib.sha256(canonical_bytes(payload)).hexdigest() == receipt.sha256, "预报回执已变化")
    actual_issue = utcnow()
    _require(received_at <= actual_issue, "服务器时钟回退，不能生成时间证据")
    output = {"status": "available", "evaluation_kind": "prospective_forecast",
              "institution_id": inst.id, "model_id": model_version.id,
              "receipt_id": receipt.id, "received_at": received_at.isoformat(), "issued_at": actual_issue.isoformat(),
              "forecast_product": FORECAST_PRODUCT, "training_weather_product": model_version.payload["weather_product"],
              "daily_interval_kind": "conditional_nb2_prediction_interval",
              "interval_limitation": "日区间未包含模型参数与天气预报误差；七日累计不提供区间",
              "daily": [], "day_seven_mean": None, "seven_day_total_mean": None}
    try:
        rows = _forecast_inputs(payload, actual_issue)
        daily = predict_daily(model_version.payload["model"], rows)
        output.update({"daily": daily, "day_seven_mean": daily[-1]["mean"],
                       "seven_day_total_mean": sum(row["mean"] for row in daily)})
    except ModelValidationError as exc:
        output.update({"status": "unavailable", "reason": str(exc)})
    run = PilotForecastRun(institution_id=inst.id, model_id=model_version.id, receipt_id=receipt.id,
                           issued_at=actual_issue, payload=output)
    db.session.add(run)
    return run


def generate_forecast(inst, issued_at=None, fetcher=None):
    """同机构生成串行；异常回滚，重复任务不会并发发布当天多个版本。"""
    try:
        _require(inst is not None, "机构不存在")
        db.session.execute(update(PilotInstitution).where(PilotInstitution.id == inst.id)
                           .values(enabled=PilotInstitution.enabled))
        db.session.refresh(inst)
        run = _generate_forecast_locked(inst, issued_at=issued_at, fetcher=fetcher)
        db.session.commit()
        return run
    except Exception:
        db.session.rollback()
        raise


def prospective_evaluation(inst, limit=90):
    """病例到达后用实际就诊日匹配既有预测，始终与实况天气回测分开。"""
    _require(type(limit) is int and 1 <= limit <= 366, "评估记录上限无效")
    runs = PilotForecastRun.query.filter_by(institution_id=inst.id).order_by(
        PilotForecastRun.issued_at.desc()).limit(limit).all()
    coverage = {row.date: row.status for row in PilotCoverage.query.filter_by(institution_id=inst.id).all()}
    counts = dict(db.session.query(PilotEncounter.encounter_date, db.func.count(PilotEncounter.id)).filter(
        PilotEncounter.institution_id == inst.id, PilotEncounter.active.is_(True), PilotEncounter.age >= 60).group_by(
        PilotEncounter.encounter_date).all())
    records = []
    for run in runs:
        if run.payload.get("status") != "available":
            continue
        receipt = db.session.get(PilotForecastReceipt, run.receipt_id)
        _require(receipt and _aware(receipt.received_at) <= _aware(run.issued_at), "预测回执时间证据无效")
        for prediction in run.payload["daily"]:
            day = _date(prediction["date"])
            actual = counts.get(day, 0) if coverage.get(day) in {"complete", "confirmed_zero"} else None
            records.append({"run_id": run.id, "model_id": run.model_id, "issued_at": _aware(run.issued_at).isoformat(),
                            "date": prediction["date"], "lead_day": (day - _aware(run.issued_at).astimezone(LOCAL_ZONE).date()).days,
                            "predicted_mean": prediction["mean"], "actual": actual,
                            "reporting_status": coverage.get(day, "unreported"),
                            "absolute_error": abs(actual - prediction["mean"]) if actual is not None else None})
    return {"evaluation_kind": "prospective_forecast", "records": records,
            "matched_prediction_days": sum(row["actual"] is not None for row in records)}
