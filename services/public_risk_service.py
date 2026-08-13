# -*- coding: utf-8 -*-
"""网页与小程序共用的公开天气风险和家庭提醒。"""

from __future__ import annotations

from core.weather import is_complete_qweather_weather
from services.action_reminder_service import (
    infer_weather_tags,
    select_action_reminder,
)
from services.heat_action_service import HeatActionService
from services.user._common import _action_plan


HEAT_RISK_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "extreme": "极高",
}
RISK_DISCLAIMER = "仅作天气健康行动提醒，不提供医疗诊断。"
PUBLIC_RISK_SCHEMA_VERSION = 2


def public_risk_weather_is_ready(current) -> bool:
    """公开风险只接受字段完整、来源明确的和风天气快照。"""
    return is_complete_qweather_weather(current)


def _unknown_risk(summary: str) -> dict:
    return {
        "available": False,
        "schema_version": PUBLIC_RISK_SCHEMA_VERSION,
        "level": "未知",
        "score": None,
        "summary": summary,
        "reasons": [summary],
        "calculation": None,
        "disclaimer": RISK_DISCLAIMER,
    }


def _risk_reason_texts(heat_result) -> list[str]:
    """把模型因子转成两个客户端都能直接展示的简短解释。"""
    factors = heat_result.get("factor_scores") or []
    active = [
        f"{item.get('label')} {item.get('value')}"
        for item in factors
        if float(item.get("score") or 0) > 0
    ]
    return active or ["当前未触发主要高温风险阈值"]


def calculate_public_risk(current) -> dict:
    """基于一份持久化天气计算风险；函数本身不读取数据库或网络。"""
    weather = current if isinstance(current, dict) else {}
    if not public_risk_weather_is_ready(weather):
        return {
            "available": False,
            "risk": _unknown_risk("天气快照尚未具备完整风险计算条件"),
            "actions": [],
            "heat_result": None,
            "risk_reasons": [],
        }

    heat_service = HeatActionService()
    heat_result = heat_service.calculate_heat_risk(
        weather,
        consecutive_hot_days=weather.get("consecutive_hot_days", 0),
    )
    risk_label = HEAT_RISK_LABELS.get(
        heat_result.get("risk_level"),
        "低风险",
    )
    reasons = _risk_reason_texts(heat_result)
    risk_reasons = heat_service.build_risk_reasons(heat_result)
    risk = {
        "available": True,
        "schema_version": PUBLIC_RISK_SCHEMA_VERSION,
        "level": risk_label,
        "score": heat_result.get("risk_score"),
        "summary": "；".join(reasons[:2]),
        "reasons": reasons,
        "calculation": {
            "heat_result": heat_result,
            "risk_reasons": risk_reasons,
        },
        "disclaimer": RISK_DISCLAIMER,
    }
    return {
        "available": True,
        "risk": risk,
        "actions": _action_plan(risk_label),
        "heat_result": heat_result,
        "risk_reasons": risk_reasons,
    }


def build_public_family_reminder(
    current=None,
    warnings=None,
    *,
    risk=None,
    available=False,
    date_value=None,
) -> dict:
    """只使用已持久化天气与风险，稳定生成当日家庭提醒。"""
    weather = current if isinstance(current, dict) and current and available is True else {}
    # 官方预警有独立来源状态，当前天气不可用时仍可生成 warning-only 提醒。
    warning_rows = warnings if isinstance(warnings, list) else []
    risk_payload = risk if isinstance(risk, dict) else {}
    reminder = select_action_reminder(
        date_value=date_value,
        risk_level=(risk_payload.get("level") if available is True else "low"),
        weather_tags=infer_weather_tags(weather, warning_rows),
        audience="family_group",
    )
    dependencies = []
    if weather:
        dependencies.append("current")
    if warning_rows:
        dependencies.append("warnings")
    return {**reminder, "depends_on": dependencies}


def build_public_risk_context(
    current=None,
    warnings=None,
    *,
    date_value=None,
    available=None,
) -> dict:
    """一次生成风险、行动和当日提醒，供 Web 与 bootstrap 共同消费。"""
    weather = current if isinstance(current, dict) else {}
    warning_rows = warnings if isinstance(warnings, list) else []
    calculated = calculate_public_risk(weather)
    if available is not None and not bool(available):
        calculated = {
            "available": False,
            "risk": _unknown_risk("天气快照尚未可用或已过期"),
            "actions": [],
            "heat_result": None,
            "risk_reasons": [],
        }

    family_reminder = build_public_family_reminder(
        weather,
        warning_rows,
        date_value=date_value,
        risk=calculated["risk"],
        available=calculated["available"],
    )
    return {
        **calculated,
        "family_reminder": family_reminder,
    }
