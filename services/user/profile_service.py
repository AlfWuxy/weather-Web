# -*- coding: utf-8 -*-
"""Profile and assessment routes."""
import json
import logging
from urllib.parse import unquote, urlsplit

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, logout_user
from sqlalchemy.exc import IntegrityError

from core.analytics import get_high_risk_streak
from core.db_models import Community, HealthRiskAssessment, User
from core.extensions import db
from core.guest import build_guest_profile, get_guest_assessment, is_guest_user
from core.notifications import create_notification
from core.time_utils import utcnow
from core.usage import create_api_token
from core.weather import (
    ensure_user_location_valid,
    get_weather_with_cache,
    is_air_quality_available,
    is_qweather_production_ready,
    normalize_location_name,
)
from utils.parsers import json_or_none, safe_json_loads
from utils.validators import (
    sanitize_input,
    validate_age,
    validate_email,
    validate_gender,
    validate_password,
    validate_username,
)

logger = logging.getLogger(__name__)


def _personal_weather_available(weather_data):
    """个人健康评分只接受完整和风实况与真实空气质量。"""
    return (
        is_qweather_production_ready(weather_data)
        and is_air_quality_available(weather_data)
    )


def _safe_referrer_or_dashboard():
    referrer = request.referrer or ''
    fallback = url_for('user.user_dashboard')
    if (
        not referrer
        or referrer.startswith('//')
        or any(char in referrer for char in ('\r', '\n', '\\'))
    ):
        return fallback

    parsed = urlsplit(referrer)
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme not in ('http', 'https')
            or parsed.netloc.lower() != request.host.lower()
        ):
            return fallback

    # 绝对与相对 Referer 最终都收敛成单斜杠开头的本地 path + query。
    local_target = parsed.path or fallback
    if parsed.query:
        local_target = f'{local_target}?{parsed.query}'
    decoded_target = unquote(local_target)
    for candidate in (local_target, decoded_target):
        if (
            any(char in candidate for char in ('\r', '\n', '\\'))
            or not candidate.startswith('/')
            or candidate.startswith('//')
        ):
            return fallback
    local_parts = urlsplit(local_target)
    if local_parts.scheme or local_parts.netloc:
        return fallback
    return local_target


def health_assessment():
    """健康风险评估"""
    if request.method == 'POST':
        screening_options = {
            'outdoor_exposure': {'low', 'medium', 'high'},
            'symptom_level': {'none', 'mild', 'moderate', 'severe'},
            'hydration': {'good', 'normal', 'poor'},
            'medication_adherence': {'good', 'partial', 'poor'},
            'sleep_quality': {'good', 'fair', 'poor'},
        }
        screening = {}
        for name, allowed in screening_options.items():
            value = sanitize_input(request.form.get(name), max_length=20)
            value = value.strip().lower() if isinstance(value, str) else ''
            if value not in allowed:
                flash('请完整选择全部 5 项健康筛查后再提交。', 'error')
                return redirect(url_for('user.health_assessment'))
            screening[name] = value

        try:
            # 执行风险评估（多路径融合版）
            from services.health_risk_service import HealthRiskService

            user_location = ensure_user_location_valid()
            weather_data, _ = get_weather_with_cache(user_location)
            if not _personal_weather_available(weather_data):
                flash(
                    '天气正在更新，本次评估暂未完成。请稍后再试；身体明显不适时请及时就医。',
                    'warning'
                )
                return redirect(url_for('user.health_assessment'))
            health_service = HealthRiskService()

            # 构建用户健康档案
            user_health_profile = {
                'age': current_user.age or 30,
                'gender': current_user.gender or '未知',
                'community': current_user.community or '',
                'has_chronic_disease': current_user.has_chronic_disease or False,
                'chronic_diseases': safe_json_loads(current_user.chronic_diseases, [])
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
                    'fusion_breakdown': risk_result.get('fusion_breakdown', {}),
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
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            if not old_password:
                flash('请输入当前密码', 'error')
                return redirect(url_for('user.profile'))
            if not current_user.check_password(old_password):
                flash('当前密码不正确', 'error')
                return redirect(url_for('user.profile'))
            valid, result = validate_password(new_password)
            if not valid:
                flash(result, 'error')
                return redirect(url_for('user.profile'))
            if new_password != confirm_password:
                flash('两次输入的新密码不一致', 'error')
                return redirect(url_for('user.profile'))
            current_user.set_password(result)
            db.session.commit()
            # get_id 含密码摘要，改密后显式退出，给用户清晰的重新认证路径。
            for key in (
                'guest_id',
                'guest_profile',
                'guest_assessment',
                'pair_token',
                'pair_session_id',
                'pair_session_code',
            ):
                session.pop(key, None)
            logout_user()
            flash('密码已更新，请使用新密码重新登录', 'success')
            return redirect(url_for('public.login'))

        # default: basic profile update
        submitted_username = request.form.get('username')
        if submitted_username is not None:
            valid, result = validate_username(submitted_username)
            if not valid:
                flash(result, 'error')
                return redirect(url_for('user.profile'))
            if result != current_user.username:
                flash('用户名不可更改', 'error')
                return redirect(url_for('user.profile'))

        # 验证年龄
        valid, result = validate_age(request.form.get('age'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('user.profile'))
        age = result

        # 验证性别
        valid, result = validate_gender(request.form.get('gender'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('user.profile'))
        gender = result

        # 清理社区输入并校验
        community_value = sanitize_input(request.form.get('community'), max_length=100)
        community = normalize_location_name(community_value)

        # 验证邮箱
        valid, result = validate_email(request.form.get('email'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('user.profile'))
        email = result
        duplicate_email = None
        if email:
            duplicate_email = User.query.filter(
                User.id != current_user.id,
                db.func.lower(User.email) == email.lower()
            ).first()
        if duplicate_email:
            flash('该邮箱已被其他账号使用，请更换邮箱。', 'error')
            return redirect(url_for('user.profile'))

        current_user.age = age
        current_user.gender = gender
        # P10：community = 定位/展示，可自改；authorized_community = ACL，仅 admin
        # 忽略客户端提交的 authorized_community（防 mass-assignment）
        current_user.community = community
        current_user.email = email

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
            flash('已关闭自动推送：需要先填写微信提醒接收码', 'warning')
        current_user.push_enabled = bool(push_enabled)

        try:
            db.session.commit()
        except IntegrityError:
            # 并发更新时仍以数据库唯一约束为最终防线。
            db.session.rollback()
            flash('该邮箱已被其他账号使用，请更换邮箱。', 'error')
            return redirect(url_for('user.profile'))
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
        # P10：定位只写 community；ACL 看 authorized_community，横向越权已拆字段
        current_user.community = normalized
        db.session.commit()

    flash(f'定位已更新为 {normalized}', 'success')
    return redirect(_safe_referrer_or_dashboard())
