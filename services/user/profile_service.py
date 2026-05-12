# -*- coding: utf-8 -*-
"""Profile and assessment routes."""
import json
import logging
from urllib.parse import urlparse

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user

from core.analytics import get_high_risk_streak
from core.db_models import Community, HealthRiskAssessment
from core.extensions import db
from core.guest import build_guest_profile, get_guest_assessment, is_guest_user
from core.notifications import create_notification
from core.time_utils import utcnow
from core.usage import create_api_token
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


def _safe_referrer_or_dashboard():
    referrer = request.referrer or ''
    fallback = url_for('user.user_dashboard')
    if not referrer or '\r' in referrer or '\n' in referrer:
        return fallback
    parsed = urlparse(referrer)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in ('http', 'https') or parsed.netloc != request.host:
            return fallback
        path = parsed.path or fallback
        if parsed.query:
            path = f'{path}?{parsed.query}'
        return path
    if not referrer.startswith('/') or referrer.startswith(("//", "\\\\", "/\\")):
        return fallback
    return referrer


def health_assessment():
    """健康风险评估"""
    if request.method == 'POST':
        try:
            # 执行风险评估（多路径融合版）
            from services.health_risk_service import HealthRiskService

            health_service = HealthRiskService()
            user_location = ensure_user_location_valid()
            weather_data, _ = get_weather_with_cache(user_location)

            # 构建用户健康档案
            user_health_profile = {
                'age': current_user.age or 30,
                'gender': current_user.gender or '未知',
                'community': current_user.community or '',
                'has_chronic_disease': current_user.has_chronic_disease or False,
                'chronic_diseases': safe_json_loads(current_user.chronic_diseases, [])
            }

            # 个人即时筛查（可选项）
            def _select(name, allowed, default):
                value = sanitize_input(request.form.get(name), max_length=20) or default
                value = value.strip().lower()
                return value if value in allowed else default

            screening = {
                'outdoor_exposure': _select('outdoor_exposure', {'low', 'medium', 'high'}, 'medium'),
                'symptom_level': _select('symptom_level', {'none', 'mild', 'moderate', 'severe'}, 'none'),
                'hydration': _select('hydration', {'good', 'normal', 'poor'}, 'normal'),
                'medication_adherence': _select('medication_adherence', {'good', 'partial', 'poor'}, 'good'),
                'sleep_quality': _select('sleep_quality', {'good', 'fair', 'poor'}, 'good')
            }

            risk_result = health_service.assess_personal_weather_health_risk(
                user_health_profile,
                weather_data,
                screening=screening
            )

            recommendations = risk_result.get('recommendations', [])
            disease_risks = risk_result.get('disease_risks', {})

            explain_payload = {
                'explain': risk_result.get('explain', {}),
                'rule_version': risk_result.get('rule_version'),
                'triggered_rules': risk_result.get('triggered_rules', []),
                'academic_profile': {
                    'model_version': risk_result.get('model_version'),
                    'risk_interval': risk_result.get('risk_interval', {}),
                    'risk_probabilities': risk_result.get('risk_probabilities', {}),
                    'high_risk_probability': risk_result.get('high_risk_probability'),
                    'cap_semantics': risk_result.get('cap_semantics', {}),
                    'impact_likelihood': risk_result.get('impact_likelihood', {}),
                    'model_paths': risk_result.get('model_paths', []),
                    'component_scores': risk_result.get('component_scores', {}),
                    'community_context': risk_result.get('community_context', {}),
                    'screening': risk_result.get('screening', {}),
                    'weather': risk_result.get('weather', {}),
                    'methodology': risk_result.get('methodology', []),
                    'rr_breakdown': risk_result.get('rr_breakdown', {})
                }
            }

            if is_guest_user(current_user):
                session['guest_assessment'] = {
                    'assessment_date': utcnow().isoformat(),
                    'risk_score': risk_result['risk_score'],
                    'risk_level': risk_result['risk_level'],
                    'recommendations': json.dumps(recommendations, ensure_ascii=False),
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
                    disease_risks=json.dumps(disease_risks, ensure_ascii=False),
                    recommendations=json.dumps(recommendations, ensure_ascii=False),
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

        return redirect(url_for('user.health_assessment'))

    latest_assessment = None
    if is_guest_user(current_user):
        latest_assessment = get_guest_assessment()
    else:
        latest_assessment = HealthRiskAssessment.query.filter_by(
            user_id=current_user.id
        ).order_by(HealthRiskAssessment.assessment_date.desc()).first()
    explain_data = {}
    disease_risks_data = {}
    academic_profile = {}
    if latest_assessment and getattr(latest_assessment, 'explain', None):
        explain_data = safe_json_loads(latest_assessment.explain, {})
    if latest_assessment and getattr(latest_assessment, 'disease_risks', None):
        disease_risks_data = safe_json_loads(latest_assessment.disease_risks, {})
    if isinstance(explain_data, dict):
        academic_profile = explain_data.get('academic_profile', {})
    if not isinstance(disease_risks_data, dict):
        disease_risks_data = {}

    return render_template(
        'health_assessment.html',
        assessment=latest_assessment,
        assessment_explain=explain_data,
        assessment_disease_risks=disease_risks_data,
        assessment_academic=academic_profile
    )


def profile():
    """个人设置"""
    if is_guest_user(current_user):
        flash('游客模式无法修改个人信息，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))
    if request.method == 'POST':
        form_id = sanitize_input(request.form.get('form_id'), max_length=30) or 'basic'

        if form_id == 'api_token':
            token_name = sanitize_input(request.form.get('token_name'), max_length=80)
            try:
                plain = create_api_token(current_user.id, name=token_name)
                session['last_api_token_plain'] = plain
                flash('API Token 已生成（仅展示一次，请立即复制保存）', 'success')
            except Exception:
                logger.exception("API token create failed")
                flash('生成失败，请稍后重试。', 'error')
            return redirect(url_for('user.profile'))

        if form_id == 'password':
            old_password = request.form.get('old_password', '')
            new_password = request.form.get('new_password')
            if not old_password:
                flash('请输入当前密码', 'error')
                return redirect(url_for('user.profile'))
            if not current_user.check_password(old_password):
                flash('当前密码不正确', 'error')
                return redirect(url_for('user.profile'))
            if new_password:
                valid, result = validate_password(new_password)
                if not valid:
                    flash(result, 'error')
                    return redirect(url_for('user.profile'))
                current_user.set_password(result)
                db.session.commit()
                flash('密码已更新', 'success')
            else:
                flash('未填写新密码', 'info')
            return redirect(url_for('user.profile'))

        # default: basic profile update
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
        # 密码更新已拆分到 form_id=password

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

        # 试点推送设置
        wx_uid = sanitize_input(request.form.get('wxpusher_uid'), max_length=80)
        current_user.wxpusher_uid = (wx_uid.strip() if isinstance(wx_uid, str) else None) or None
        push_enabled = request.form.get('push_enabled') == 'on'
        if push_enabled and not current_user.wxpusher_uid:
            push_enabled = False
            flash('已关闭自动推送：需要先填写 WxPusher UID', 'warning')
        current_user.push_enabled = bool(push_enabled)

        db.session.commit()
        logger.info("用户更新个人信息: %s", current_user.username)
        flash('个人信息更新成功', 'success')
        return redirect(url_for('user.profile'))

    communities = Community.query.all()
    chronic_diseases_list = safe_json_loads(current_user.chronic_diseases, [])

    last_api_token_plain = session.pop('last_api_token_plain', None)
    return render_template(
        'profile.html',
        communities=communities,
        chronic_diseases_list=chronic_diseases_list,
        last_api_token_plain=last_api_token_plain
    )


def update_location():
    """更新当前位置"""
    location = sanitize_input(request.form.get('location'), max_length=100)
    if not location:
        flash('请填写有效的地点', 'error')
        return redirect(_safe_referrer_or_dashboard())

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
    return redirect(_safe_referrer_or_dashboard())
