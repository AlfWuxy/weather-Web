# -*- coding: utf-8 -*-
"""Tooling and prediction pages."""
from flask import Blueprint, current_app, flash, render_template, request
from flask_login import current_user, login_required

from core.constants import CHRONIC_OPTIONS
from core.db_models import FamilyMember
from core.weather import ensure_user_location_valid, get_weather_with_cache, normalize_location_name
from services.chronic_risk_service import get_chronic_service
from services.ml_prediction_service import get_ml_service
from utils.parsers import parse_float, parse_int, safe_json_loads
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

ML_CHRONIC_ALIASES = {
    '慢性阻塞性肺病': '慢阻肺',
    '慢性呼吸道疾病': '慢阻肺',
    'COPD': '慢阻肺',
    '脑卒中史': '脑卒中',
    '骨关节病': '关节炎',
}

ML_CHRONIC_OPTIONS = CHRONIC_OPTIONS + ['慢性肾病']


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


def _build_ml_factor_cards(result, age, weather_info):
    """把服务返回转换成模板可直接消费的因子卡片。"""
    factor_cards = []
    raw_factors = result.get('risk_factors') or []
    for index, factor_name in enumerate(raw_factors[:4]):
        factor_cards.append({
            'name': factor_name,
            'value': max(36, 84 - index * 12),
            'effect': '+',
        })

    if factor_cards:
        return factor_cards

    temperature = int(round(float(weather_info.get('temperature', 20) or 20)))
    humidity = int(round(float(weather_info.get('humidity', 60) or 60)))
    aqi = int(round(float(weather_info.get('aqi', 40) or 40)))
    return [
        {'name': '最高气温变化', 'value': min(96, max(28, temperature * 2)), 'effect': '+'},
        {'name': '相对湿度', 'value': min(92, max(22, humidity)), 'effect': '+'},
        {'name': '空气质量', 'value': min(90, max(18, aqi)), 'effect': '+'},
        {'name': '年龄', 'value': min(95, max(24, age)), 'effect': '+'},
    ]


def _normalize_ml_chronic_list(values):
    """统一慢病标签，保证页面回填与既有档案名称一致。"""
    normalized = []
    for raw_value in values or []:
        value = sanitize_input(raw_value, max_length=50)
        if not value:
            continue
        value = ML_CHRONIC_ALIASES.get(value, value)
        if value not in ML_CHRONIC_OPTIONS:
            continue
        if value not in normalized:
            normalized.append(value)
    return normalized


def _build_chronic_breakdown(result, adherence, symptoms):
    """把慢病服务输出映射成页面分解条。"""
    breakdown = []
    for disease_key, payload in (result.get('disease_risks') or {}).items():
        risk_score = int(round(payload.get('risk_score', 0) or 0))
        breakdown.append({
            'name': DISEASE_BREAKDOWN_LABELS.get(disease_key, disease_key),
            'value': risk_score,
            'level': _level_bucket(risk_score),
        })

    vital_adjustment = result.get('vital_adjustment') or {}
    if vital_adjustment.get('score_adjustment'):
        vital_score = min(100, max(20, int(round(vital_adjustment.get('score_adjustment', 0) * 5))))
        breakdown.append({
            'name': '血压/血糖修正',
            'value': vital_score,
            'level': _level_bucket(vital_score),
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
    })

    symptom_score = 22
    if symptoms:
        symptom_score = 58 if len(symptoms) >= 4 else 42
    breakdown.append({
        'name': '自觉症状',
        'value': symptom_score,
        'level': _level_bucket(symptom_score),
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
    """ML预测页面。"""
    family_members = _tool_family_members()
    current_location = ensure_user_location_valid()
    form_state = {
        'member_id': '',
        'location': current_location,
        'age': current_user.age or 65,
        'chronic': [],
    }
    prediction = None
    factors = None
    prediction_error = None

    if request.method == 'POST':
        selected_member = _selected_member(request.form.get('member_id'))
        default_age = selected_member.age if selected_member and selected_member.age else current_user.age or 65
        default_gender = selected_member.gender if selected_member and selected_member.gender else current_user.gender or '男'
        selected_chronic = _normalize_ml_chronic_list(request.form.getlist('chronic'))
        if not selected_chronic and selected_member:
            selected_chronic = _normalize_ml_chronic_list(
                safe_json_loads(selected_member.chronic_diseases, [])
            )

        form_state = {
            'member_id': str(selected_member.id) if selected_member else '',
            'location': _normalized_location(request.form.get('location')),
            'age': _coerce_age(request.form.get('age'), default_age),
            'chronic': selected_chronic,
        }

        weather_info, _ = get_weather_with_cache(form_state['location'])
        user_info = {
            'age': form_state['age'],
            'gender': default_gender,
        }
        result = get_ml_service().predict_disease_risk(user_info, weather_info)
        if result.get('success'):
            prediction = []
            for item in (result.get('predictions') or [])[:3]:
                score = int(round((item.get('probability') or 0) * 100))
                prediction.append({
                    'disease': item.get('disease', '未知风险'),
                    'score': score,
                    'label': _score_level(score),
                })
            factors = _build_ml_factor_cards(result, form_state['age'], weather_info)
        else:
            prediction_error = result.get('error') or '预测暂时不可用，请稍后再试。'

    return render_template(
        'ml_prediction.html',
        family_members=family_members,
        form_state=form_state,
        prediction=prediction,
        factors=factors,
        prediction_error=prediction_error,
    )


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
    return render_template(
        'forecast_7day.html',
        family_members=_tool_family_members(),
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

    if request.method == 'POST':
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
        vitals = _parse_chronic_vitals(form_state)
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
    )
