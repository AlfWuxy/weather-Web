# -*- coding: utf-8 -*-
"""Profile and assessment routes."""
import json
import logging
import math
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user
from sqlalchemy.exc import IntegrityError

from core.analytics import get_high_risk_streak
from core.db_models import (
    ApiToken,
    Community,
    HealthRiskAssessment,
    MiniProgramSession,
    User,
)
from core.extensions import db
from core.guest import build_guest_profile, get_guest_assessment, is_guest_user
from core.notifications import create_notification
from core.time_utils import utcnow
from core.usage import create_api_token
from core.weather import (
    compact_assessment_weather_condition,
    ensure_user_location_valid,
    get_weather_with_cache,
    is_qweather_online_weather,
    normalize_location_name,
)
from utils.parsers import json_or_none, safe_json_loads
from utils.validators import (
    sanitize_input,
    validate_age,
    validate_email,
    validate_gender,
    validate_password
)
from services.user.owner_write_guard import OwnerInactiveError, owner_write_guard

logger = logging.getLogger(__name__)


def _personal_weather_available(weather_data):
    """个人评估只接受来源明确且温度可计算的真实和风天气。"""
    if not is_qweather_online_weather(weather_data):
        return False
    try:
        temperature = float(weather_data.get('temperature'))
    except (AttributeError, TypeError, ValueError):
        return False
    return math.isfinite(temperature)


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
                owner_user_id = int(current_user.id)
                with owner_write_guard(owner_user_id):
                    # 评估、通知与账号状态在同一受保护事务内落库。
                    assessment = HealthRiskAssessment(
                        user_id=owner_user_id,
                        assessment_date=utcnow(),
                        weather_condition=compact_assessment_weather_condition(weather_data),
                        risk_score=risk_result['risk_score'],
                        risk_level=risk_result['risk_level'],
                        disease_risks=json.dumps(disease_risks, ensure_ascii=False),
                        recommendations=json.dumps(recommendations, ensure_ascii=False),
                        explain=json_or_none(explain_payload)
                    )
                    db.session.add(assessment)

                    if current_app.config.get('FEATURE_NOTIFICATIONS'):
                        if risk_result['risk_level'] == '高风险':
                            create_notification(
                                owner_user_id,
                                title='健康风险偏高',
                                message='今日天气对健康影响较大，建议减少外出并加强防护。',
                                level='warning',
                                category='risk',
                                action_url=url_for('user.health_assessment'),
                                commit=False,
                            )
                        streak = get_high_risk_streak(owner_user_id)
                        threshold_days = current_app.config.get('NOTIFICATION_ESCALATION_DAYS', 3)
                        if threshold_days and streak >= threshold_days:
                            create_notification(
                                owner_user_id,
                                title='高风险持续提醒',
                                message=f'已连续{streak}天高风险，建议联系家属或村医协助。',
                                level='danger',
                                category='risk',
                                action_url=url_for('user.health_assessment'),
                                commit=False,
                            )
                    db.session.commit()

                flash('健康风险评估完成', 'success')
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
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
            if request.form.get('miniprogram_privacy_consent') != '1':
                flash('请先阅读并同意小程序隐私说明，再生成绑定凭证。', 'error')
                return redirect(url_for('user.profile'))
            try:
                owner_user_id = int(current_user.id)
                with owner_write_guard(owner_user_id):
                    plain = create_api_token(
                        owner_user_id,
                        name=token_name,
                        privacy_consent_version=current_app.config.get(
                            'WX_MINIPROGRAM_PRIVACY_VERSION'
                        ),
                        commit=False,
                    )
                    db.session.commit()
                session['last_api_token_plain'] = plain
                ttl_days = current_app.config.get('API_TOKEN_TTL_DAYS', 30)
                flash(
                    f'API Token 已生成，有效期 {ttl_days} 天（仅展示一次，请立即复制保存）',
                    'success',
                )
            except OwnerInactiveError:
                flash('账号已失效，请重新登录。', 'error')
                return redirect(url_for('public.login'))
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
            if new_password:
                valid, result = validate_password(new_password)
                if not valid:
                    flash(result, 'error')
                    return redirect(url_for('user.profile'))
                try:
                    owner_user_id = int(current_user.id)
                    remember_cookie_name = current_app.config.get(
                        'REMEMBER_COOKIE_NAME',
                        'remember_token',
                    )
                    refresh_remember_cookie = bool(
                        request.cookies.get(remember_cookie_name)
                    )
                    with owner_write_guard(owner_user_id) as locked_user:
                        if not locked_user.check_password(old_password):
                            flash('当前密码不正确', 'error')
                            return redirect(url_for('user.profile'))
                        locked_user.set_password(result)
                        locked_user.auth_version = int(locked_user.auth_version) + 1
                        now = utcnow()
                        ApiToken.query.filter(
                            ApiToken.user_id == owner_user_id,
                            ApiToken.revoked_at.is_(None),
                        ).update(
                            {ApiToken.revoked_at: now},
                            synchronize_session=False,
                        )
                        MiniProgramSession.query.filter(
                            MiniProgramSession.user_id == owner_user_id,
                            MiniProgramSession.revoked_at.is_(None),
                        ).update(
                            {MiniProgramSession.revoked_at: now},
                            synchronize_session=False,
                        )
                        db.session.commit()
                        # 当前浏览器已再次验证密码，签发新版本会话并按需轮换记住登录。
                        login_user(
                            locked_user,
                            remember=refresh_remember_cookie,
                            duration=(
                                timedelta(days=30)
                                if refresh_remember_cookie
                                else None
                            ),
                        )
                    flash(
                        '密码已更新，其他网页登录、小程序会话及绑定凭证均已失效',
                        'success',
                    )
                except OwnerInactiveError:
                    flash('账号已失效，请重新登录。', 'error')
                    return redirect(url_for('public.login'))
                except (OSError, RuntimeError, ValueError):
                    db.session.rollback()
                    logger.exception('密码更新授权锁不可用，修改未保存')
                    flash('密码暂时无法更新，请稍后重试。', 'error')
            else:
                flash('未填写新密码', 'info')
            return redirect(url_for('user.profile'))

        # default: basic profile update
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

        # 先完整校验第三方推送，再统一修改用户对象，避免配置缺失时部分保存档案。
        wx_uid = sanitize_input(request.form.get('wxpusher_uid'), max_length=80)
        wx_uid = (wx_uid.strip() if isinstance(wx_uid, str) else None) or None
        push_enabled = request.form.get('push_enabled') == 'on'
        wxpusher_available = bool(
            (current_app.config.get('WXPUSHER_APP_TOKEN') or '').strip()
        )
        if push_enabled and not wxpusher_available:
            flash('第三方推送服务暂不可用，本次更改未保存。', 'error')
            return redirect(url_for('user.profile'))
        if push_enabled and not wx_uid:
            push_enabled = False
            flash('已关闭自动推送：需要先填写 WxPusher UID', 'warning')

        try:
            owner_user_id = int(current_user.id)
            with owner_write_guard(owner_user_id) as locked_user:
                if (
                    push_enabled
                    and not bool(locked_user.push_enabled)
                    and request.form.get('wxpusher_consent') != '1'
                ):
                    db.session.rollback()
                    flash('请先确认本次开启涉及的第三方传输范围。', 'error')
                    return redirect(url_for('user.profile'))

                locked_user.age = age
                locked_user.gender = gender
                locked_user.community = community
                locked_user.email = email

                has_chronic = request.form.get('has_chronic_disease') == 'on'
                locked_user.has_chronic_disease = has_chronic
                if has_chronic:
                    chronic_diseases = request.form.getlist('chronic_diseases')
                    chronic_diseases = [
                        sanitize_input(d, max_length=50)
                        for d in chronic_diseases
                        if d
                    ]
                    locked_user.chronic_diseases = json.dumps(chronic_diseases)
                else:
                    locked_user.chronic_diseases = None

                locked_user.wxpusher_uid = wx_uid
                locked_user.push_enabled = bool(push_enabled)
                db.session.commit()
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except IntegrityError:
            # 并发更新时仍以数据库唯一约束为最终防线。
            db.session.rollback()
            flash('该邮箱已被其他账号使用，请更换邮箱。', 'error')
            return redirect(url_for('user.profile'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('推送授权锁不可用，个人信息未保存')
            flash('个人信息暂时无法保存，请稍后重试。', 'error')
            return redirect(url_for('user.profile'))
        logger.info("用户更新个人信息: user_id=%s", owner_user_id)
        flash('个人信息更新成功', 'success')
        return redirect(url_for('user.profile'))

    communities = Community.query.all()
    chronic_diseases_list = safe_json_loads(current_user.chronic_diseases, [])

    last_api_token_plain = session.pop('last_api_token_plain', None)
    return render_template(
        'profile.html',
        communities=communities,
        chronic_diseases_list=chronic_diseases_list,
        last_api_token_plain=last_api_token_plain,
        wxpusher_available=bool(
            (current_app.config.get('WXPUSHER_APP_TOKEN') or '').strip()
        ),
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
        try:
            owner_user_id = int(current_user.id)
            with owner_write_guard(owner_user_id) as locked_user:
                locked_user.community = normalized
                db.session.commit()
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('位置更新授权锁不可用，修改未保存')
            flash('位置暂时无法更新，请稍后重试。', 'error')
            return redirect(_safe_referrer_or_dashboard())

    flash(f'定位已更新为 {normalized}', 'success')
    return redirect(_safe_referrer_or_dashboard())
