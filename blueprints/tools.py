# -*- coding: utf-8 -*-
"""Tooling and prediction pages."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import re

from flask import Blueprint, current_app, flash, render_template, request
from flask_login import current_user, login_required

from core.guest import is_guest_user
from core.location_resolution import get_user_location_options, resolve_user_location
from core.db_models import Community, FamilyMember
from core.time_utils import today_local
from core.weather import (
    ensure_user_location_valid,
    get_qweather_forecast_with_cache,
    get_weather_with_cache,
    is_qweather_online_weather,
)
from services.chronic_risk_service import get_chronic_service
from services.forecast_cards import build_forecast_cards
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


def _tool_location_options():
    """合并静态县内别名与管理员维护的社区表。"""
    dynamic_names = []
    try:
        communities = Community.query.with_entities(
            Community.name,
        ).all()
    except Exception as exc:
        current_app.logger.warning('读取工具页社区候选失败: %s', exc)
        communities = []
    for (name,) in communities:
        clean_name = str(name or '').strip()
        if clean_name:
            dynamic_names.append(clean_name)
    return get_user_location_options(extra_names=dynamic_names)


def _resolve_tool_location(raw_location, *, location_options=None):
    """解析工具页地点，非法输入不得回退到默认县域。"""
    raw = raw_location.strip() if isinstance(raw_location, str) else ''
    candidate = raw or ensure_user_location_valid()
    return resolve_user_location(
        candidate,
        extra_names=location_options or _tool_location_options(),
    )


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
    """仅把真实和风实况中的有限空气质量值传给未来日风险计算。"""
    if not is_qweather_online_weather(weather_data):
        return {}
    context = {}
    for field in ('pm25', 'aqi'):
        value = parse_float((weather_data or {}).get(field))
        if value is not None and math.isfinite(value):
            context[field] = value
    return context


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


_PLAIN_DECIMAL_RE = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$')


def _parse_optional_vital(raw_value, *, label, minimum, maximum):
    """只接受短、有限且不使用指数写法的十进制生命体征。"""
    if raw_value is None:
        return None, None
    raw = str(raw_value).strip()
    if not raw:
        return None, None
    if len(raw) > 20:
        return None, f'{label}输入过长，请重新填写。'
    if not _PLAIN_DECIMAL_RE.fullmatch(raw):
        return None, f'{label}格式不正确，请填写普通数字。'
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, f'{label}格式不正确，请填写普通数字。'
    if not value.is_finite():
        return None, f'{label}格式不正确，请填写有限数字。'
    if value < Decimal(str(minimum)) or value > Decimal(str(maximum)):
        return None, f'{label}需在 {minimum} 到 {maximum} 之间。'
    return float(value), None


def _parse_chronic_vitals(form_state):
    """严格解析慢病表单中的可选血压与血糖。"""
    vitals = {}
    errors = {}
    sbp, sbp_error = _parse_optional_vital(
        form_state.get('sbp'),
        label='最高收缩压',
        minimum=60,
        maximum=260,
    )
    fbg, fbg_error = _parse_optional_vital(
        form_state.get('fbg'),
        label='空腹血糖',
        minimum=2,
        maximum=30,
    )
    if sbp_error:
        errors['sbp'] = sbp_error
    elif sbp is not None:
        vitals['sbp'] = sbp
    if fbg_error:
        errors['fbg'] = fbg_error
    elif fbg is not None:
        vitals['fbg'] = fbg
    return vitals, errors


def _classified_ml_error(raw_error=None, *, code='model_unavailable'):
    """把模型内部错误收敛为可安全展示的有限分类。"""
    normalized = str(raw_error or '').strip().lower()
    if code == 'auth_required':
        message = '游客可以浏览说明；生成个人类别线索需要注册或登录正式账号。'
    elif code == 'invalid_location':
        message = str(raw_error)
    elif code == 'weather_unavailable':
        message = '健康关注线索暂时无法生成：官方天气快照正在更新，请稍后再试。'
    elif any(marker in normalized for marker in ('model', '模型', 'load', '加载')):
        message = '类别线索模型正在维护，请稍后再试。'
    else:
        message = '类别线索服务暂时不可用，请稍后再试。'
    return {'code': code, 'message': message}


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
    """ML预测页面。"""
    family_members = _tool_family_members()
    current_location = ensure_user_location_valid()
    location_options = _tool_location_options()
    form_state = {
        'member_id': '',
        'location': current_location,
        'age': current_user.age or 65,
    }
    prediction = None
    factors = None
    prediction_error = None
    response_status = 200

    if request.method == 'POST':
        selected_member = _selected_member(request.form.get('member_id'))
        default_age = selected_member.age if selected_member and selected_member.age else current_user.age or 65
        default_gender = selected_member.gender if selected_member and selected_member.gender else current_user.gender or '男'
        raw_location = request.form.get('location')
        location_resolution = _resolve_tool_location(
            raw_location,
            location_options=location_options,
        )
        form_state = {
            'member_id': str(selected_member.id) if selected_member else '',
            'location': (
                location_resolution.raw
                if not location_resolution.valid
                else location_resolution.value
            ),
            'age': _coerce_age(request.form.get('age'), default_age),
        }

        if is_guest_user(current_user):
            prediction_error = _classified_ml_error(code='auth_required')
            response_status = 403
        elif not location_resolution.valid:
            prediction_error = _classified_ml_error(
                location_resolution.error,
                code='invalid_location',
            )
            response_status = 422
        else:
            weather_info, _ = get_weather_with_cache(form_state['location'])
            if not is_qweather_online_weather(weather_info):
                prediction_error = _classified_ml_error(code='weather_unavailable')
            else:
                user_info = {
                    'age': form_state['age'],
                    'gender': default_gender,
                }
                try:
                    result = get_ml_service().predict_disease_risk(user_info, weather_info)
                except Exception:
                    current_app.logger.exception('ML 类别线索服务调用失败')
                    result = {'success': False, 'error': 'service_exception'}
                if result.get('success'):
                    prediction = []
                    for rank, item in enumerate((result.get('predictions') or [])[:3], start=1):
                        adjusted_probability = float(item.get('probability') or 0.0)
                        original_probability = float(
                            item.get('original_probability')
                            if item.get('original_probability') is not None
                            else adjusted_probability
                        )
                        multiplier = item.get('weather_multiplier')
                        if multiplier is None:
                            multiplier = adjusted_probability / original_probability if original_probability > 0 else 1.0
                        prediction.append({
                            'disease': item.get('disease', '未知风险'),
                            'score': round(adjusted_probability * 100.0, 1),
                            'original_score': round(original_probability * 100.0, 1),
                            'weather_multiplier': round(float(multiplier), 3),
                            'label': f'关注排序第 {rank}',
                        })
                    factors = _build_ml_factor_cards(result, form_state['age'], weather_info)
                else:
                    prediction_error = _classified_ml_error(result.get('error'))

    return render_template(
        'ml_prediction.html',
        family_members=family_members,
        form_state=form_state,
        prediction=prediction,
        factors=factors,
        prediction_error=prediction_error,
        is_guest=is_guest_user(current_user),
        location_options=location_options,
    ), response_status


@bp.route('/ai-qa', endpoint='ai_qa')
@login_required
def ai_qa():
    """AI问答页面"""
    models = current_app.config.get('AI_ALLOWED_MODELS', [])
    return render_template('ai_question.html', models=models)


@bp.route('/forecast-7day', endpoint='forecast_7day')
@login_required
def forecast_7day():
    """7天健康预测页面"""
    location_options = _tool_location_options()
    location_resolution = _resolve_tool_location(
        request.args.get('location'),
        location_options=location_options,
    )
    current_location = (
        location_resolution.value
        if location_resolution.valid
        else location_resolution.raw
    )
    start_date = today_local()
    forecast_days = []
    weekly_tips = None
    forecast_error = None
    location_error = None
    forecast_meta = {'source': 'QWeather'}
    if not location_resolution.valid:
        location_error = location_resolution.error
        return render_template(
            'forecast_7day.html',
            family_members=_tool_family_members(),
            current_location=current_location,
            location_input=location_resolution.raw,
            location_options=location_options,
            location_error=location_error,
            forecast_days=[],
            forecast_error=None,
            forecast_meta=forecast_meta,
            weekly_tips=None,
        ), 422

    qweather_days, from_cache, forecast_meta = get_qweather_forecast_with_cache(current_location, days=7)
    forecast_meta = dict(forecast_meta or {})
    forecast_meta['source_label'] = '和风天气'
    forecast_meta['from_cache'] = bool(from_cache)
    forecast_meta['update_time_label'] = _format_qweather_update_time(forecast_meta.get('update_time'))

    if len(qweather_days or []) < 7:
        forecast_error = '7 天天气正在更新，预报暂不显示。请稍后重试。'
    else:
        health_forecasts = []
        current_weather, _ = get_weather_with_cache(current_location)
        weather_context = _forecast_weather_context(current_weather)
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
        if not forecast_days:
            forecast_error = '7 天天气正在更新，预报暂不显示。请稍后重试。'

    return render_template(
        'forecast_7day.html',
        family_members=_tool_family_members(),
        current_location=current_location,
        location_input=current_location,
        location_options=location_options,
        location_error=location_error,
        forecast_days=forecast_days,
        forecast_error=forecast_error,
        forecast_meta=forecast_meta,
        weekly_tips=weekly_tips,
    )


@bp.route('/chronic-risk', methods=['GET', 'POST'], endpoint='chronic_risk')
@login_required
def chronic_risk():
    """慢病风险预测页面"""
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
    vital_errors = {}
    response_status = 200

    if request.method == 'POST':
        disease_key = sanitize_input(request.form.get('disease'), max_length=32) or 'hypertension'
        disease_key = disease_key if disease_key in CHRONIC_FORM_LABELS else 'hypertension'
        form_state = {
            'disease': disease_key,
            'sbp': sanitize_input(request.form.get('sbp'), max_length=32) or '',
            'fbg': sanitize_input(request.form.get('fbg'), max_length=32) or '',
            'adherence': sanitize_input(request.form.get('adherence'), max_length=20) or 'strict',
            'symptoms': sanitize_input(request.form.get('symptoms'), max_length=100) or '',
        }

        vitals, vital_errors = _parse_chronic_vitals({
            'sbp': request.form.get('sbp'),
            'fbg': request.form.get('fbg'),
        })
        if vital_errors:
            risk_error = '请先修正生命体征输入，再生成慢病风险提示。'
            response_status = 422
        else:
            weather_data, _ = get_weather_with_cache(ensure_user_location_valid())
            if not is_qweather_online_weather(weather_data):
                risk_error = '天气正在更新，本次提醒暂未生成。请稍后再试。'
            else:
                result = get_chronic_service().predict_individual_risk(
                    {
                        'age': current_user.age or 65,
                        'gender': current_user.gender or '未知',
                        'chronic_diseases': [CHRONIC_FORM_LABELS[disease_key]],
                        'vitals': vitals,
                        'sbp': vitals.get('sbp'),
                        'fbg': vitals.get('fbg'),
                    },
                    weather_data,
                )

                overall = result.get('overall_risk') or {}
                risk_score = int(round(overall.get('score', 0) or 0))
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
        form_state=form_state,
        risk_score=risk_score,
        risk_comment=risk_comment,
        breakdown=breakdown,
        suggestions=suggestions,
        risk_error=risk_error,
        vital_errors=vital_errors,
    ), response_status
