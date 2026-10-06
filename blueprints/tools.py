# -*- coding: utf-8 -*-
"""Tooling and prediction pages."""
from datetime import datetime
import math

from flask import redirect, url_for, Blueprint, current_app, flash, render_template, request
from flask_login import current_user, login_required

from core.guest import is_guest_user
from core.db_models import FamilyMember
from core.time_utils import today_local
from core.weather import (
    ensure_user_location_valid,
    get_location_options,
    get_openmeteo_forecast_with_cache,
    get_qweather_forecast_with_cache,
    get_weather_with_cache,
    normalize_health_model_weather,
    normalize_location_name,
)
from services.chronic_risk_service import get_chronic_service
from services.forecast_cards import (
    build_forecast_cards,
    build_weather_only_forecast_cards,
)
from services.forecast_service import get_forecast_service
from services.ml_prediction_service import get_ml_service
from utils.parsers import parse_float, parse_int
from utils.validators import sanitize_input

bp = Blueprint('tools', __name__)


CHRONIC_FORM_LABELS = {
    'hypertension': '高血压',
    'diabetes': '糖尿病',
    'chd': '冠心病',
    'copd': '慢性阻塞性肺病',
}

DISEASE_BREAKDOWN_LABELS = {
    'cardiovascular': '心血管风险',
    'respiratory': '呼吸系统风险',
    'general': '综合基础风险',
    'musculoskeletal': '骨关节风险',
}

def _tool_family_members():
    """返回当前用户可选的家庭成员。"""
    if getattr(current_user, 'role', None) == 'guest':
        return []
    return FamilyMember.query.filter_by(user_id=current_user.id).order_by(FamilyMember.created_at.desc()).all()


def _selected_member(member_id):
    """按当前用户范围解析家庭成员。"""
    parsed_id = parse_int(member_id)
    if not parsed_id or getattr(current_user, 'role', None) == 'guest':
        return None
    return FamilyMember.query.filter_by(id=parsed_id, user_id=current_user.id).first()


def _normalized_location(raw_location):
    """清洗并标准化地点输入。"""
    location = sanitize_input(raw_location, max_length=100)
    if location:
        return normalize_location_name(location)
    return ensure_user_location_valid()


def _coerce_age(raw_age, default_age):
    """安全转换年龄，避免模板和服务层接到异常值。"""
    age = parse_int(raw_age)
    if age is None:
        age = default_age
    if age is None:
        age = 65
    return max(1, min(int(age), 120))


def _score_level(score):
    """按分值映射页面展示等级。"""
    if score >= 70:
        return '高风险'
    if score >= 40:
        return '中等风险'
    return '低风险'


def _level_bucket(score):
    """按分值映射条形图样式。"""
    if score >= 70:
        return 'high'
    if score >= 40:
        return 'mid'
    return 'low'


def _format_qweather_update_time(raw_value):
    if not raw_value:
        return ''
    try:
        normalized = str(raw_value).replace('Z', '+00:00')
        return datetime.fromisoformat(normalized).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(raw_value)


def _forecast_weather_context(weather_data):
    """实况空气质量不代替未来逐日空气预报。"""
    return {}


def _build_ml_factor_cards(result, age, weather_info):
    """把模型元数据中的真实全局特征重要性转换成页面卡片。"""
    del age, weather_info
    model_info = result.get('model_info') or {}
    raw_importance = model_info.get('feature_importance') or {}
    if not isinstance(raw_importance, dict):
        return []

    ranked = []
    for name, value in raw_importance.items():
        try:
            importance = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
        ranked.append({
            'name': str(name),
            'value': round(importance * 100.0, 1),
            'effect': '全局',
        })
    ranked.sort(key=lambda item: item['value'], reverse=True)
    return ranked[:6]


def _build_chronic_breakdown(result, adherence, symptoms):
    """把慢病服务输出映射成页面分解条。"""
    def _display_number(value, digits=4):
        if value is None:
            return '--'
        try:
            parsed = round(float(value), digits)
        except (TypeError, ValueError):
            return str(value)
        return f'{parsed:g}'

    breakdown = []
    for disease_key, payload in (result.get('disease_risks') or {}).items():
        risk_score = int(round(payload.get('risk_score', 0) or 0))
        vital_contribution = float(payload.get('vital_adjustment', 0) or 0)
        raw_dlnm_rr = payload.get('raw_dlnm_rr', payload.get('base_rr'))
        dlnm_disease_modifier = payload.get('dlnm_disease_modifier', 1.0)
        dlnm_age_modifier = payload.get('dlnm_age_modifier', 1.0)
        dlnm_adjusted_rr = payload.get('dlnm_adjusted_rr', payload.get('base_rr'))
        chronic_age_amplifier = payload.get('chronic_age_amplifier', payload.get('age_amplifier'))
        comorbidity_amplifier = payload.get('comorbidity_amplifier')
        personal_rr = payload.get('personal_rr')
        cap_value = payload.get('dlnm_rr_cap')
        if cap_value is None:
            cap_note = f" = {_display_number(dlnm_adjusted_rr)}"
        elif payload.get('dlnm_rr_cap_applied'):
            cap_note = f"，触发上限 {_display_number(cap_value)} 后得 {_display_number(dlnm_adjusted_rr)}"
        else:
            cap_note = f"，上限 {_display_number(cap_value)} 未触发，得 {_display_number(dlnm_adjusted_rr)}"
        breakdown.append({
            'name': DISEASE_BREAKDOWN_LABELS.get(disease_key, disease_key),
            'value': risk_score,
            'level': _level_bucket(risk_score),
            'included': True,
            'rr_components': [
                {'label': 'Raw DLNM RR', 'value': _display_number(raw_dlnm_rr)},
                {'label': 'DLNM病种修正', 'value': f"×{_display_number(dlnm_disease_modifier)}"},
                {'label': 'DLNM年龄修正', 'value': f"×{_display_number(dlnm_age_modifier)}"},
                {'label': '慢病层年龄修正', 'value': f"×{_display_number(chronic_age_amplifier)}"},
                {'label': '共病修正', 'value': f"×{_display_number(comorbidity_amplifier)}"},
                {'label': 'Personal RR', 'value': _display_number(personal_rr)},
            ],
            'calculation': (
                f"DLNM内层：{_display_number(raw_dlnm_rr)} × {_display_number(dlnm_disease_modifier)} "
                f"× {_display_number(dlnm_age_modifier)}{cap_note}；"
                f"慢病层：{_display_number(dlnm_adjusted_rr)} × {_display_number(chronic_age_amplifier)} "
                f"× {_display_number(comorbidity_amplifier)} = Personal RR {_display_number(personal_rr)}；"
                f"分数：min(100, Personal RR × 30 + 生命体征修正 {vital_contribution:+.1f}) = {risk_score}"
            ),
        })

    adherence_scores = {
        'strict': 22,
        'loose': 52,
        'none': 78,
    }
    adherence_score = adherence_scores.get(adherence, 32)
    breakdown.append({
        'name': '用药依从',
        'value': adherence_score,
        'level': _level_bucket(adherence_score),
        'included': False,
        'calculation': '问卷观察项，当前不进入总分',
    })

    symptom_score = 22
    if symptoms:
        symptom_score = 58 if len(symptoms) >= 4 else 42
    breakdown.append({
        'name': '自觉症状',
        'value': symptom_score,
        'level': _level_bucket(symptom_score),
        'included': False,
        'calculation': '自由文本观察项，当前不进入总分',
    })

    return breakdown[:4]


def _parse_chronic_vitals(form_state):
    """解析慢病表单中的自测血压/血糖。"""
    sbp = parse_float(form_state.get('sbp'))
    fbg = parse_float(form_state.get('fbg'))
    vitals = {}
    if sbp is not None and 60 <= sbp <= 260:
        vitals['sbp'] = sbp
    if fbg is not None and 2 <= fbg <= 30:
        vitals['fbg'] = fbg
    return vitals


def _normalize_chronic_suggestions(items):
    suggestions = []
    for item in items or []:
        if isinstance(item, dict):
            text = item.get('advice') or item.get('category')
        else:
            text = str(item) if item else ''
        if text and text not in suggestions:
            suggestions.append(text)
    return suggestions


@bp.route('/ml-prediction', methods=['GET', 'POST'], endpoint='ml_prediction')
@login_required
def ml_prediction():
    """RF 留作研究说明，生产页面不再运行推理。"""
    return render_template('ml_prediction.html')


@bp.route('/ai-qa', endpoint='ai_qa')
@login_required
def ai_qa():
    """AI问答页面"""
    models = current_app.config.get('AI_ALLOWED_MODELS', [])
    ai_available = bool(
        current_app.config.get('FEATURE_WEB_AI')
        and current_app.config.get('SILICONFLOW_API_KEY')
    )
    return render_template(
        'ai_question.html',
        models=models,
        ai_available=ai_available,
    )


@bp.route('/forecast-7day', endpoint='forecast_7day')
@login_required
def forecast_7day():
    """7天预报页面；请求只读后台缓存，健康模型仅使用和风完整输入。"""
    current_location = _normalized_location(request.args.get('location'))
    start_date = today_local()
    forecast_days = []
    weekly_tips = None
    forecast_error = None
    forecast_meta = {'source': 'QWeather'}
    qweather_days, from_cache, forecast_meta = get_qweather_forecast_with_cache(current_location, days=7)
    forecast_meta = dict(forecast_meta or {})

    if len(qweather_days or []) >= 7:
        forecast_meta['source'] = 'QWeather'
        forecast_meta['source_label'] = '和风天气'
        forecast_meta['from_cache'] = bool(from_cache)
        forecast_meta['update_time_label'] = _format_qweather_update_time(
            forecast_meta.get('update_time') or forecast_meta.get('fetched_at')
        )
        health_forecasts = []
        if forecast_meta.get('stale') is not True:
            weather_context = {}
            try:
                health_forecasts, summary = get_forecast_service().generate_7day_forecast(
                    qweather_days,
                    start_date=start_date,
                    context=weather_context,
                )
                recommendations = (summary or {}).get('recommendations') or []
                if recommendations:
                    weekly_tips = [
                        {
                            'icon': 'lightbulb',
                            'title': item.get('category') or item.get('priority') or '健康提醒',
                            'detail': item.get('advice') or item.get('description') or '',
                        }
                        for item in recommendations[:4]
                        if isinstance(item, dict)
                    ] or None
            except Exception as exc:
                current_app.logger.warning("7天健康预测生成失败，仅展示和风天气: %s", exc)
        forecast_days = build_forecast_cards(qweather_days, health_forecasts, start_date)
    else:
        openmeteo_days, from_cache, forecast_meta = get_openmeteo_forecast_with_cache(
            current_location,
            days=7,
        )
        forecast_meta = dict(forecast_meta or {})
        forecast_meta['source'] = 'Open-Meteo'
        forecast_meta['source_label'] = 'Open-Meteo'
        forecast_meta['from_cache'] = bool(from_cache)
        forecast_meta['update_time_label'] = _format_qweather_update_time(
            forecast_meta.get('update_time') or forecast_meta.get('fetched_at')
        )
        if len(openmeteo_days or []) >= 7:
            forecast_days = build_weather_only_forecast_cards(
                openmeteo_days,
                start_date,
            )

    if not forecast_days:
        forecast_error = '7 天天气正在更新，预报暂不显示。请稍后重试。'

    return render_template(
        'forecast_7day.html',
        family_members=_tool_family_members(),
        current_location=current_location,
        location_options=get_location_options(),
        forecast_days=forecast_days,
        forecast_error=forecast_error,
        forecast_meta=forecast_meta,
        weekly_tips=weekly_tips,
    )


@bp.route('/chronic-risk', methods=['GET', 'POST'], endpoint='chronic_risk')
@login_required
def chronic_risk():
    """慢病风险预测页面"""
    from services.account_service import has_health_consent
    guest_demo = is_guest_user(current_user)
    form_state = {
        'disease': 'hypertension',
        'sbp': '',
        'fbg': '',
        'adherence': 'strict',
        'symptoms': '',
    }
    risk_score = None
    risk_comment = None
    breakdown = None
    suggestions = None
    risk_error = None

    if request.method == 'POST':
        from services.account_service import has_health_consent
        if not guest_demo and not has_health_consent(current_user):
            flash('填写慢病评估前，请先单独同意健康信息处理。', 'warning')
            return redirect(url_for('public.account_security'))

        disease_key = sanitize_input(request.form.get('disease'), max_length=32) or 'hypertension'
        disease_key = disease_key if disease_key in CHRONIC_FORM_LABELS else 'hypertension'
        form_state = {
            'disease': disease_key,
            'sbp': sanitize_input(request.form.get('sbp'), max_length=10) or '',
            'fbg': sanitize_input(request.form.get('fbg'), max_length=10) or '',
            'adherence': sanitize_input(request.form.get('adherence'), max_length=20) or 'strict',
            'symptoms': sanitize_input(request.form.get('symptoms'), max_length=100) or '',
        }

        weather_data, _ = get_weather_with_cache(ensure_user_location_valid())
        health_weather = normalize_health_model_weather(weather_data)
        if health_weather is None:
            risk_error = '天气正在更新，慢病风险提示暂不显示。请稍后再试。'
        else:
            vitals = _parse_chronic_vitals(form_state)
            result = get_chronic_service().predict_individual_risk(
                {
                    'age': current_user.age,
                    'gender': current_user.gender or '未知',
                    'chronic_diseases': [CHRONIC_FORM_LABELS[disease_key]],
                    'vitals': vitals,
                    'sbp': vitals.get('sbp'),
                    'fbg': vitals.get('fbg'),
                },
                health_weather,
            )

            overall = result.get('overall_risk') or {}
            parsed_risk_score = parse_float(overall.get('score'))
            if parsed_risk_score is None or not math.isfinite(parsed_risk_score):
                risk_error = (result.get('data_quality') or {}).get('reason') or '风险未知，请补充资料并回访。'
                return render_template(
                    'chronic_risk.html',
                    health_consent=guest_demo or has_health_consent(current_user),
                    form_state=form_state,
                    risk_score=None,
                    risk_comment=None,
                    breakdown=None,
                    suggestions=None,
                    risk_error=risk_error,
                    guest_demo=guest_demo,
                )
            risk_score = int(round(parsed_risk_score))
            risk_level = overall.get('level') or _score_level(risk_score)
            risk_comment = (
                f"当前以{CHRONIC_FORM_LABELS[disease_key]}为重点观察对象，结合天气条件判定为{risk_level}。"
            )
            vital_factors = ((result.get('vital_adjustment') or {}).get('factors') or [])
            if vital_factors:
                risk_comment = f"{risk_comment} 已参考{'；'.join(vital_factors[:2])}。"
            breakdown = _build_chronic_breakdown(result, form_state['adherence'], form_state['symptoms'])
            suggestions = _normalize_chronic_suggestions(result.get('recommendations'))[:5]

    return render_template(
        'chronic_risk.html',
        health_consent=guest_demo or has_health_consent(current_user),
        form_state=form_state,
        risk_score=risk_score,
        risk_comment=risk_comment,
        breakdown=breakdown,
        suggestions=suggestions,
        risk_error=risk_error,
    )
