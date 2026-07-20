# -*- coding: utf-8 -*-
"""Health management routes."""
import json
import logging
from types import SimpleNamespace

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from core.constants import CHRONIC_OPTIONS, RISK_TAG_OPTIONS
from core.extensions import db
from core.guest import is_guest_user
from core.health_profiles import (
    _build_member_profile_form_payload,
    _parse_chronic_diseases_from_form,
    compute_member_risk,
    compute_profile_completion,
    member_weather_triggered,
    profile_to_context
)
from core.time_utils import today_local
from core.usage import log_usage_event
from core.weather import ensure_user_location_valid, get_weather_with_cache, is_qweather_online_weather
from core.db_models import (
    FamilyMember,
    FamilyMemberProfile,
    HealthDiary,
    HealthRiskAssessment,
    MedicationReminder,
    Notification,
    Pair,
    UsageEvent,
    WeatherData,
)
from services.community_daily_service import refresh_latest_community_daily_best_effort
from services.user.owner_write_guard import OwnerInactiveError, owner_write_guard
from utils.parsers import parse_int, parse_date, parse_float, safe_json_loads
from utils.validators import sanitize_input, validate_gender

logger = logging.getLogger(__name__)

bp = Blueprint('health', __name__)


def _empty_family_member():
    """构造空白成员对象，供新增表单复用。"""
    return SimpleNamespace(
        id=None,
        name='',
        relation='',
        age=None,
        gender='',
        chronic_diseases=None
    )


def _apply_family_member_form(member):
    """将表单内容写回成员对象，并返回画像载荷。"""
    member.name = sanitize_input(request.form.get('name'), max_length=50)
    member.relation = sanitize_input(request.form.get('relation'), max_length=20)
    member.age = parse_int(request.form.get('age'))

    raw_gender = request.form.get('gender')
    member.gender = sanitize_input(raw_gender, max_length=10)

    chronic_diseases = _parse_chronic_diseases_from_form(request.form)
    member.chronic_diseases = json.dumps(chronic_diseases, ensure_ascii=False) if chronic_diseases else None

    if not member.name:
        return None, '成员姓名不能为空'

    valid, gender = validate_gender(raw_gender)
    if not valid:
        return None, gender
    member.gender = gender
    return _build_member_profile_form_payload(request.form), None


def _render_family_member_form(member, profile=None, *, is_create_mode=False):
    """统一渲染新增/编辑成员表单。"""
    chronic_diseases = safe_json_loads(getattr(member, 'chronic_diseases', None), [])
    return render_template(
        'family_member_edit.html',
        member=member,
        profile=profile_to_context(profile),
        chronic_diseases=chronic_diseases,
        chronic_diseases_list=chronic_diseases,
        risk_tag_options=RISK_TAG_OPTIONS,
        chronic_options=CHRONIC_OPTIONS,
        is_create_mode=is_create_mode,
        page_title='添加家庭成员' if is_create_mode else '编辑家庭成员',
        submit_label='创建成员' if is_create_mode else '保存修改',
        action_url=url_for('health.family_member_new') if is_create_mode else url_for('health.family_member_edit', member_id=member.id)
    )


@bp.route('/family-members', methods=['GET', 'POST'], endpoint='family_members')
@login_required
def family_members():
    """家庭成员管理"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    risk_tag_options = RISK_TAG_OPTIONS
    chronic_options = CHRONIC_OPTIONS

    if request.method == 'POST':
        owner_user_id = int(current_user.id)
        member = FamilyMember(user_id=owner_user_id)
        profile_payload, error_message = _apply_family_member_form(member)
        if error_message:
            flash(error_message, 'error')
            return redirect(url_for('health.family_members'))

        try:
            with owner_write_guard(owner_user_id):
                db.session.add(member)
                db.session.flush()
                member_id = int(member.id)
                db.session.add(FamilyMemberProfile(
                    member_id=member_id,
                    **profile_payload
                ))
                db.session.commit()
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('成员授权锁不可用，家庭成员未添加')
            flash('家庭成员暂时无法添加，请稍后重试。', 'error')
            return redirect(url_for('health.family_members'))
        log_usage_event(
            'elder_profile_created',
            user_id=owner_user_id,
            member_id=member_id,
            source='web',
            meta={'via': 'family_members'},
        )
        flash('家庭成员已添加', 'success')
        return redirect(url_for('health.family_members'))

    members = FamilyMember.query.filter_by(user_id=current_user.id).order_by(FamilyMember.created_at.desc()).all()
    member_ids = [m.id for m in members]
    profiles = []
    if member_ids:
        profiles = FamilyMemberProfile.query.filter(FamilyMemberProfile.member_id.in_(member_ids)).all()
    profile_map = {p.member_id: p for p in profiles}

    diary_entries = HealthDiary.query.filter_by(user_id=current_user.id).order_by(HealthDiary.entry_date.desc()).all()
    last_diary_map = {}
    for entry in diary_entries:
        if entry.member_id and entry.member_id not in last_diary_map:
            last_diary_map[entry.member_id] = entry

    reminders = MedicationReminder.query.filter_by(user_id=current_user.id).order_by(MedicationReminder.created_at.desc()).all()
    last_reminder_map = {}
    for reminder in reminders:
        if reminder.member_id and reminder.member_id not in last_reminder_map:
            last_reminder_map[reminder.member_id] = reminder

    user_location = ensure_user_location_valid()
    weather_data, _ = get_weather_with_cache(user_location)
    weather_available = is_qweather_online_weather(weather_data)
    weather = SimpleNamespace(**weather_data) if weather_available else None

    member_cards = []
    risk_counts = {'low': 0, 'medium': 0, 'high': 0}
    completion_values = []
    chronic_count = 0
    alert_trigger_count = 0

    for member in members:
        profile = profile_map.get(member.id)
        profile_ctx = profile_to_context(profile)
        diseases = safe_json_loads(member.chronic_diseases, [])
        if diseases:
            chronic_count += 1

        risk = compute_member_risk(member, profile)
        risk_counts[risk['level']] += 1
        completion = compute_profile_completion(member, profile)
        completion_values.append(completion['percent'])

        alerts = []
        if profile_ctx['alert_enabled'] and weather_available:
            alerts = member_weather_triggered(profile, weather)
        if alerts:
            alert_trigger_count += 1

        member_cards.append({
            'id': member.id,
            'name': member.name,
            'relation': member.relation,
            'age': member.age,
            'gender': member.gender,
            'chronic_diseases': diseases,
            'chronic': diseases,
            'location': user_location,
            'risk': risk,
            'risk_level': risk['level'],
            'risk_label': risk['label'],
            'completion': completion,
            'completion_percent': completion['percent'],
            'profile': profile_ctx,
            'alerts': alerts,
            'today_tip': alerts[0] if alerts else None,
            'last_diary': last_diary_map.get(member.id),
            'last_reminder': last_reminder_map.get(member.id)
        })

    search_query = sanitize_input(request.args.get('q'), max_length=50)
    risk_filter = sanitize_input(request.args.get('risk_level'), max_length=20)
    filtered_cards = []
    for card in member_cards:
        if search_query:
            if search_query not in (card['name'] or '') and search_query not in (card['relation'] or ''):
                continue
        if risk_filter in ('low', 'medium', 'high'):
            if card['risk']['level'] != risk_filter:
                continue
        filtered_cards.append(card)

    relation_counts = {}
    for member in members:
        relation = member.relation or '未填写'
        relation_counts[relation] = relation_counts.get(relation, 0) + 1

    avg_completion = int(round(sum(completion_values) / len(completion_values))) if completion_values else 0
    risk_chart_labels = ['低风险', '中风险', '高风险']
    risk_chart_values = [risk_counts['low'], risk_counts['medium'], risk_counts['high']]

    return render_template(
        'family_members.html',
        members=filtered_cards,
        family_members=filtered_cards,
        total_members=len(members),
        risk_counts=risk_counts,
        high_risk_count=risk_counts['high'],
        chronic_count=chronic_count,
        avg_completion=avg_completion,
        feedback_rate=avg_completion,
        relation_counts=relation_counts,
        alert_trigger_count=alert_trigger_count,
        notified_count=alert_trigger_count,
        today_weather=weather,
        weather_available=weather_available,
        search_query=search_query,
        risk_filter=risk_filter,
        risk_chart_labels=risk_chart_labels,
        risk_chart_values=risk_chart_values,
        risk_tag_options=risk_tag_options,
        chronic_options=chronic_options
    )


@bp.route('/family-members/new', methods=['GET', 'POST'], endpoint='family_member_new')
@login_required
def family_member_new():
    """新增家庭成员。"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        owner_user_id = int(current_user.id)
        member = FamilyMember(user_id=owner_user_id)
        profile_payload, error_message = _apply_family_member_form(member)
        if error_message:
            flash(error_message, 'error')
            return _render_family_member_form(member, None, is_create_mode=True)

        try:
            with owner_write_guard(owner_user_id):
                db.session.add(member)
                db.session.flush()
                member_id = int(member.id)
                db.session.add(FamilyMemberProfile(member_id=member_id, **profile_payload))
                db.session.commit()
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('成员授权锁不可用，家庭成员未添加')
            flash('家庭成员暂时无法添加，请稍后重试。', 'error')
            return _render_family_member_form(member, None, is_create_mode=True)
        log_usage_event(
            'elder_profile_created',
            user_id=owner_user_id,
            member_id=member_id,
            source='web',
            meta={'via': 'family_member_new'},
        )
        flash('家庭成员已添加', 'success')
        return redirect(url_for('health.family_members'))

    return _render_family_member_form(_empty_family_member(), None, is_create_mode=True)


@bp.route('/family-members/<int:member_id>/edit', methods=['GET', 'POST'], endpoint='family_member_edit')
@login_required
def family_member_edit(member_id):
    """编辑家庭成员"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    if member_id == 0:
        return redirect(url_for('health.family_member_new'))

    if request.method == 'POST':
        draft = _empty_family_member()
        profile_payload, error_message = _apply_family_member_form(draft)
        if error_message:
            flash(error_message, 'error')
            return redirect(url_for('health.family_member_edit', member_id=member_id))
        owner_user_id = int(current_user.id)
        try:
            with owner_write_guard(owner_user_id):
                member = FamilyMember.query.filter_by(
                    id=member_id,
                    user_id=owner_user_id,
                ).first_or_404()
                for field in ('name', 'relation', 'age', 'gender', 'chronic_diseases'):
                    setattr(member, field, getattr(draft, field))
                profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
                if profile:
                    for key, value in profile_payload.items():
                        setattr(profile, key, value)
                else:
                    profile = FamilyMemberProfile(member_id=member.id, **profile_payload)
                    db.session.add(profile)
                db.session.commit()
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('成员授权锁不可用，家庭成员信息未保存')
            flash('家庭成员信息暂时无法保存，请稍后重试。', 'error')
            return redirect(url_for('health.family_members'))
        log_usage_event(
            'elder_profile_updated',
            user_id=owner_user_id,
            member_id=member.id,
            source='web',
            meta={'via': 'family_member_edit'},
        )
        flash('家庭成员信息已更新', 'success')
        return redirect(url_for('health.family_members'))

    member = FamilyMember.query.filter_by(id=member_id, user_id=current_user.id).first_or_404()
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
    return _render_family_member_form(member, profile, is_create_mode=False)


@bp.route('/family-members/<int:member_id>/delete', methods=['POST'], endpoint='family_member_delete')
@login_required
def family_member_delete(member_id):
    """删除家庭成员"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    owner_user_id = int(current_user.id)
    affected_community_codes = set()
    try:
        with owner_write_guard(owner_user_id):
            member = FamilyMember.query.filter_by(
                id=member_id,
                user_id=owner_user_id,
            ).first_or_404()
            # 先撤销成员对应关系，避免删除成员后关系退化为默认允许推送。
            member_pairs = Pair.query.filter_by(
                caregiver_id=owner_user_id,
                member_id=member.id,
            )
            affected_community_codes = {
                row[0]
                for row in member_pairs.with_entities(Pair.community_code).all()
                if row[0]
            }
            member_pairs.update(
                {Pair.status: 'inactive', Pair.member_id: None},
                synchronize_session=False,
            )
            HealthRiskAssessment.query.filter_by(
                member_id=member.id,
                user_id=owner_user_id,
            ).delete(synchronize_session=False)
            Notification.query.filter_by(
                member_id=member.id,
                user_id=owner_user_id,
            ).delete(synchronize_session=False)
            HealthDiary.query.filter_by(
                member_id=member.id,
                user_id=owner_user_id,
            ).delete(synchronize_session=False)
            MedicationReminder.query.filter_by(
                member_id=member.id,
                user_id=owner_user_id,
            ).delete(synchronize_session=False)
            # 成员删除后不保留可回溯到该成员的使用事件。
            UsageEvent.query.filter_by(member_id=member.id).delete(
                synchronize_session=False,
            )
            FamilyMemberProfile.query.filter_by(member_id=member.id).delete(
                synchronize_session=False,
            )
            FamilyMember.query.filter_by(
                id=member.id,
                user_id=owner_user_id,
            ).delete(synchronize_session=False)
            db.session.commit()
        refresh_latest_community_daily_best_effort(
            affected_community_codes,
            event_logger=logger,
        )
        flash('家庭成员已删除', 'success')
    except OwnerInactiveError:
        flash('账号已失效，请重新登录。', 'error')
        return redirect(url_for('public.login'))
    except Exception:
        db.session.rollback()
        logger.exception('删除家庭成员失败，事务已回滚')
        flash('删除失败，请稍后重试', 'error')
    return redirect(url_for('health.family_members'))


@bp.route('/family-members/<int:member_id>/toggle-alert', methods=['POST'], endpoint='family_member_toggle_alert')
@login_required
def family_member_toggle_alert(member_id):
    """启用/关闭成员提醒"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    owner_user_id = int(current_user.id)
    try:
        with owner_write_guard(owner_user_id):
            profile = FamilyMemberProfile.query.join(FamilyMember).filter(
                FamilyMemberProfile.member_id == member_id,
                FamilyMember.user_id == owner_user_id,
            ).first_or_404()
            profile.alert_enabled = not bool(profile.alert_enabled)
            enabled = bool(profile.alert_enabled)
            db.session.commit()
    except OwnerInactiveError:
        flash('账号已失效，请重新登录。', 'error')
        return redirect(url_for('public.login'))
    except (OSError, RuntimeError, ValueError):
        db.session.rollback()
        logger.exception('成员授权锁不可用，提醒状态未修改')
        flash('提醒状态暂时无法修改，请稍后重试。', 'error')
        return redirect(url_for('health.family_members'))
    status = '已开启' if enabled else '已关闭'
    flash(f'提醒{status}', 'success')
    return redirect(url_for('health.family_members'))


@bp.route('/family-members/<int:member_id>', endpoint='family_member_detail')
@login_required
def family_member_detail(member_id):
    """家庭成员详情"""
    if is_guest_user(current_user):
        flash('游客模式无法查看家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    member = FamilyMember.query.filter_by(id=member_id, user_id=current_user.id).first_or_404()
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
    profile_ctx = profile_to_context(profile)
    chronic_diseases = safe_json_loads(member.chronic_diseases, [])
    risk = compute_member_risk(member, profile)
    completion = compute_profile_completion(member, profile)

    entries = HealthDiary.query.filter_by(user_id=current_user.id, member_id=member.id).order_by(
        HealthDiary.entry_date.desc()
    ).all()
    reminders = MedicationReminder.query.filter_by(user_id=current_user.id, member_id=member.id).order_by(
        MedicationReminder.created_at.desc()
    ).all()
    user_location = ensure_user_location_valid()
    weather_data, _ = get_weather_with_cache(user_location)
    weather_available = is_qweather_online_weather(weather_data)
    weather = SimpleNamespace(**weather_data) if weather_available else None
    alerts = []
    if profile_ctx['alert_enabled'] and weather_available:
        alerts = member_weather_triggered(profile, weather)

    return render_template(
        'family_member_detail.html',
        member=member,
        profile=profile_ctx,
        chronic_diseases=chronic_diseases,
        risk=risk,
        completion=completion,
        diary_entries=entries,
        reminders=reminders,
        weather=weather,
        weather_available=weather_available,
        alerts=alerts
    )


@bp.route('/health-diary', methods=['GET', 'POST'], endpoint='health_diary')
@login_required
def health_diary():
    """健康日记"""
    if is_guest_user(current_user):
        flash('游客模式无法记录健康日记，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        owner_user_id = int(current_user.id)
        entry_date = parse_date(request.form.get('entry_date')) or today_local()
        member_id = parse_int(request.form.get('member_id'))
        symptoms = sanitize_input(request.form.get('symptoms'), max_length=200)
        severity = sanitize_input(request.form.get('severity'), max_length=20)
        notes = sanitize_input(request.form.get('notes'), max_length=500)

        try:
            with owner_write_guard(owner_user_id):
                # 必须在 owner 锁内重新校验成员归属，避免注销或删除成员后的陈旧写入。
                if member_id and FamilyMember.query.filter_by(
                    id=member_id,
                    user_id=owner_user_id,
                ).first() is None:
                    raise LookupError('member_not_found')
                db.session.add(HealthDiary(
                    user_id=owner_user_id,
                    member_id=member_id,
                    entry_date=entry_date,
                    symptoms=symptoms,
                    severity=severity,
                    notes=notes
                ))
                db.session.commit()
        except LookupError:
            flash('无效的家庭成员', 'error')
            return redirect(url_for('health.health_diary'))
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('健康日记授权锁不可用，记录未保存')
            flash('健康日记暂时无法保存，请稍后重试。', 'error')
            return redirect(url_for('health.health_diary'))
        flash('健康日记已保存', 'success')
        return redirect(url_for('health.health_diary'))

    members = FamilyMember.query.filter_by(user_id=current_user.id).order_by(FamilyMember.created_at.desc()).all()
    entries = HealthDiary.query.filter_by(user_id=current_user.id).order_by(HealthDiary.entry_date.desc()).all()
    member_map = {member.id: member.name for member in members}

    entry_dates = sorted({entry.entry_date for entry in entries if entry.entry_date})
    weather_map = {}
    if entry_dates and current_user.community:
        weather_rows = WeatherData.query.filter(
            WeatherData.location == current_user.community,
            WeatherData.date.in_(entry_dates)
        ).all()
        weather_map = {item.date: item for item in weather_rows}

    return render_template(
        'health_diary.html',
        members=members,
        entries=entries,
        member_map=member_map,
        weather_map=weather_map
    )


@bp.route('/medication-reminders', methods=['GET', 'POST'], endpoint='medication_reminders')
@login_required
def medication_reminders():
    """用药提醒管理"""
    if is_guest_user(current_user):
        flash('游客模式无法管理用药提醒，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        owner_user_id = int(current_user.id)
        medicine_name = sanitize_input(request.form.get('medicine_name'), max_length=100)
        if not medicine_name:
            flash('请输入药品名称', 'error')
            return redirect(url_for('health.medication_reminders'))

        member_id = parse_int(request.form.get('member_id'))
        dosage = sanitize_input(request.form.get('dosage'), max_length=100)
        frequency = sanitize_input(request.form.get('frequency'), max_length=20) or 'daily'
        time_of_day = sanitize_input(request.form.get('time_of_day'), max_length=10)

        triggers = {}
        triggers['high_temp'] = parse_float(request.form.get('high_temp'))
        triggers['low_temp'] = parse_float(request.form.get('low_temp'))
        triggers['high_humidity'] = parse_float(request.form.get('high_humidity'))
        triggers['high_aqi'] = parse_float(request.form.get('high_aqi'))
        triggers = {k: v for k, v in triggers.items() if v is not None}

        try:
            with owner_write_guard(owner_user_id):
                # 与健康日记保持相同 owner/member 授权边界。
                if member_id and FamilyMember.query.filter_by(
                    id=member_id,
                    user_id=owner_user_id,
                ).first() is None:
                    raise LookupError('member_not_found')
                db.session.add(MedicationReminder(
                    user_id=owner_user_id,
                    member_id=member_id,
                    medicine_name=medicine_name,
                    dosage=dosage,
                    frequency=frequency,
                    time_of_day=time_of_day,
                    weather_triggers=json.dumps(triggers, ensure_ascii=False) if triggers else None
                ))
                db.session.commit()
        except LookupError:
            flash('无效的家庭成员', 'error')
            return redirect(url_for('health.medication_reminders'))
        except OwnerInactiveError:
            flash('账号已失效，请重新登录。', 'error')
            return redirect(url_for('public.login'))
        except (OSError, RuntimeError, ValueError):
            db.session.rollback()
            logger.exception('用药提醒授权锁不可用，记录未保存')
            flash('用药提醒暂时无法保存，请稍后重试。', 'error')
            return redirect(url_for('health.medication_reminders'))
        flash('用药提醒已添加', 'success')
        return redirect(url_for('health.medication_reminders'))

    reminders = MedicationReminder.query.filter_by(user_id=current_user.id).order_by(
        MedicationReminder.created_at.desc()
    ).all()
    members = FamilyMember.query.filter_by(user_id=current_user.id).order_by(FamilyMember.created_at.desc()).all()
    member_map = {m.id: m for m in members}

    return render_template('medication_reminders.html', reminders=reminders, members=members, member_map=member_map)


@bp.route('/medication-reminders/<int:reminder_id>/delete', methods=['POST'], endpoint='medication_reminder_delete')
@login_required
def medication_reminder_delete(reminder_id):
    """删除用药提醒"""
    owner_user_id = int(current_user.id)
    try:
        with owner_write_guard(owner_user_id):
            reminder = MedicationReminder.query.filter_by(
                id=reminder_id,
                user_id=owner_user_id,
            ).first_or_404()
            db.session.delete(reminder)
            db.session.commit()
    except OwnerInactiveError:
        flash('账号已失效，请重新登录。', 'error')
        return redirect(url_for('public.login'))
    except (OSError, RuntimeError, ValueError):
        db.session.rollback()
        logger.exception('用药提醒授权锁不可用，记录未删除')
        flash('用药提醒暂时无法删除，请稍后重试。', 'error')
        return redirect(url_for('health.medication_reminders'))
    flash('用药提醒已删除', 'success')
    return redirect(url_for('health.medication_reminders'))
