# -*- coding: utf-8 -*-
"""Profile and assessment routes."""
import json
import logging

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user

from core.analytics import get_high_risk_streak
from core.db_models import Community, HealthRiskAssessment
from core.extensions import db
from core.guest import build_guest_profile, get_guest_assessment, is_guest_user
from core.notifications import create_notification
from core.time_utils import utcnow
from core.weather import ensure_user_location_valid, get_weather_with_cache, normalize_location_name
from utils.parsers import json_or_none, safe_json_loads
from utils.validators import (
    sanitize_input,
    validate_age,
    validate_email,
    validate_gender,
    validate_password
)

logger = logging.getLogger(__name__)


def health_assessment():
    """健康风险评估"""
    if request.method == 'POST':
        try:
            # 执行风险评估 - 简化版本
            from services.weather_service import WeatherService

            weather_service = WeatherService()
            user_location = ensure_user_location_valid()
            weather_data, _ = get_weather_with_cache(user_location)

            # 构建用户健康档案
            user_health_profile = {
                'age': current_user.age or 30,
                'gender': current_user.gender or '未知',
                'has_chronic_disease': current_user.has_chronic_disease or False,
                'chronic_diseases': safe_json_loads(current_user.chronic_diseases, [])
            }

            # 计算天气健康风险
            risk_result = weather_service.calculate_risk_index(weather_data, user_health_profile)

            # 生成健康建议
            recommendations = []
            if risk_result['risk_score'] > 60:
                recommendations.append({'category': '高风险提醒', 'advice': '当前天气条件对您的健康影响较大，建议减少外出，加强防护措施'})
            elif risk_result['risk_score'] > 30:
                recommendations.append({'category': '中风险提醒', 'advice': '天气条件可能对健康产生一定影响，建议适当注意防护'})
            else:
                recommendations.append({'category': '低风险', 'advice': '当前天气条件对您的健康影响较小，可正常活动'})

            explain_payload = None
            if current_app.config.get('FEATURE_EXPLAIN_OUTPUT'):
                try:
                    from services.chronic_risk_service import ChronicRiskService
                    chronic_service = ChronicRiskService()
                    rr_proxy = 1.0 + (min(max(risk_result['risk_score'], 0), 100) / 100.0) * 0.8
                    chronic_diseases = user_health_profile.get('chronic_diseases', [])
                    explain_context = {
                        'age': user_health_profile.get('age', 30),
                        'temperature': weather_data.get('temperature', 20),
                        'rr': rr_proxy,
                        'disease_type': 'general',
                        'chronic_diseases': chronic_diseases,
                        'has_chronic_disease': user_health_profile.get('has_chronic_disease', False),
                        'disease_count': len(chronic_diseases),
                        'aqi': weather_data.get('aqi', 50),
                        'hot_night': weather_data.get('temperature_min', 15) >= 22,
                        'hot_night_temp': weather_data.get('temperature_min', 22),
                        'heat_wave_days': weather_data.get('heat_wave_days', 0),
                        'cold_wave_days': weather_data.get('cold_wave_days', 0)
                    }
                    explain, triggered_rules = chronic_service.build_explain(explain_context, recommendations)
                    explain_payload = {
                        'explain': explain,
                        'rule_version': chronic_service.rules_version,
                        'triggered_rules': triggered_rules
                    }
                except Exception:
                    explain_payload = None

            if is_guest_user(current_user):
                session['guest_assessment'] = {
                    'assessment_date': utcnow().isoformat(),
                    'risk_score': risk_result['risk_score'],
                    'risk_level': risk_result['risk_level'],
                    'recommendations': json.dumps(recommendations),
                    'explain': json_or_none(explain_payload)
                }
                flash('健康风险评估完成（游客模式不保存记录）', 'success')
            else:
                # 保存评估记录
                assessment = HealthRiskAssessment(
                    user_id=current_user.id,
                    assessment_date=utcnow(),
                    weather_condition=json.dumps(weather_data),
                    risk_score=risk_result['risk_score'],
                    risk_level=risk_result['risk_level'],
                    disease_risks=json.dumps({}),
                    recommendations=json.dumps(recommendations),
                    explain=json_or_none(explain_payload)
                )

                db.session.add(assessment)
                db.session.commit()

                if current_app.config.get('FEATURE_NOTIFICATIONS'):
                    if risk_result['risk_level'] == '高风险':
                        create_notification(
                            current_user.id,
                            title='健康风险偏高',
                            message='今日天气对健康影响较大，建议减少外出并加强防护。',
                            level='warning',
                            category='risk',
                            action_url=url_for('user.health_assessment')
                        )
                    streak = get_high_risk_streak(current_user.id)
                    threshold_days = current_app.config.get('NOTIFICATION_ESCALATION_DAYS', 3)
                    if threshold_days and streak >= threshold_days:
                        create_notification(
                            current_user.id,
                            title='高风险持续提醒',
                            message=f'已连续{streak}天高风险，建议联系家属或村医协助。',
                            level='danger',
                            category='risk',
                            action_url=url_for('user.health_assessment')
                        )

                flash('健康风险评估完成', 'success')
        except Exception:
            logger.exception("健康风险评估失败")
            flash('评估过程出现异常，请稍后重试。', 'error')

        return redirect(url_for('user.user_dashboard'))

    latest_assessment = None
    if is_guest_user(current_user):
        latest_assessment = get_guest_assessment()
    else:
        latest_assessment = HealthRiskAssessment.query.filter_by(
            user_id=current_user.id
        ).order_by(HealthRiskAssessment.assessment_date.desc()).first()
    explain_data = {}
    if latest_assessment and getattr(latest_assessment, 'explain', None):
        explain_data = safe_json_loads(latest_assessment.explain, {})
    return render_template('health_assessment.html', assessment=latest_assessment, assessment_explain=explain_data)


def profile():
    """个人设置"""
    if is_guest_user(current_user):
        flash('游客模式无法修改个人信息，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))
    if request.method == 'POST':
        # 验证年龄
        valid, result = validate_age(request.form.get('age'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('user.profile'))
        current_user.age = result

        # 验证性别
        valid, result = validate_gender(request.form.get('gender'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('user.profile'))
        current_user.gender = result

        # 清理社区输入并校验
        community_value = sanitize_input(request.form.get('community'), max_length=100)
        current_user.community = normalize_location_name(community_value)

        # 验证邮箱
        valid, result = validate_email(request.form.get('email'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('user.profile'))
        current_user.email = result

        # 更新密码
        new_password = request.form.get('new_password')
        if new_password:
            valid, result = validate_password(new_password)
            if not valid:
                flash(result, 'error')
                return redirect(url_for('user.profile'))
            current_user.set_password(new_password)

        # 更新慢性病信息
        has_chronic = request.form.get('has_chronic_disease') == 'on'
        current_user.has_chronic_disease = has_chronic

        if has_chronic:
            chronic_diseases = request.form.getlist('chronic_diseases')
            # 清理慢性病输入
            chronic_diseases = [sanitize_input(d, max_length=50) for d in chronic_diseases if d]
            current_user.chronic_diseases = json.dumps(chronic_diseases)
        else:
            current_user.chronic_diseases = None

        db.session.commit()
        logger.info("用户更新个人信息: %s", current_user.username)
        flash('个人信息更新成功', 'success')
        return redirect(url_for('user.profile'))

    communities = Community.query.all()
    chronic_diseases_list = safe_json_loads(current_user.chronic_diseases, [])

    return render_template('profile.html', communities=communities, chronic_diseases_list=chronic_diseases_list)


def update_location():
    """更新当前位置"""
    location = sanitize_input(request.form.get('location'), max_length=100)
    if not location:
        flash('请填写有效的地点', 'error')
        return redirect(request.referrer or url_for('user.user_dashboard'))

    normalized = normalize_location_name(location)
    if normalized != location:
        flash(f'未识别的地点，已自动调整为 {normalized}', 'error')

    if is_guest_user(current_user):
        profile = build_guest_profile()
        profile['community'] = normalized
        session['guest_profile'] = profile
    else:
        current_user.community = normalized
        db.session.commit()

    flash(f'定位已更新为 {normalized}', 'success')
    return redirect(request.referrer or url_for('user.user_dashboard'))
