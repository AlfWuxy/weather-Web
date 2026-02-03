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
from core.weather import ensure_user_location_valid, get_weather_with_cache
from core.db_models import FamilyMember, FamilyMemberProfile, HealthDiary, MedicationReminder
from utils.parsers import parse_int, parse_date, parse_float, safe_json_loads
from utils.validators import sanitize_input, validate_gender

logger = logging.getLogger(__name__)

bp = Blueprint('health', __name__)


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
        name = sanitize_input(request.form.get('name'), max_length=50)
        if not name:
            flash('成员姓名不能为空', 'error')
            return redirect(url_for('health.family_members'))

        valid, gender = validate_gender(request.form.get('gender'))
        if not valid:
            flash(gender, 'error')
            return redirect(url_for('health.family_members'))

        age = parse_int(request.form.get('age'))
        relation = sanitize_input(request.form.get('relation'), max_length=20)
        chronic_diseases = _parse_chronic_diseases_from_form(request.form)

        member = FamilyMember(
            user_id=current_user.id,
            name=name,
            relation=relation,
            age=age,
            gender=gender,
            chronic_diseases=json.dumps(chronic_diseases, ensure_ascii=False) if chronic_diseases else None
        )
        db.session.add(member)
        db.session.commit()

        profile_payload = _build_member_profile_form_payload(request.form)
        profile = FamilyMemberProfile(
            member_id=member.id,
            **profile_payload
        )
        db.session.add(profile)
        db.session.commit()
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
    weather = SimpleNamespace(**weather_data)

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
        if profile_ctx['alert_enabled']:
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
            'risk': risk,
            'completion': completion,
            'profile': profile_ctx,
            'alerts': alerts,
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
        total_members=len(members),
        risk_counts=risk_counts,
        chronic_count=chronic_count,
        avg_completion=avg_completion,
        relation_counts=relation_counts,
        alert_trigger_count=alert_trigger_count,
        today_weather=weather,
        search_query=search_query,
        risk_filter=risk_filter,
        risk_chart_labels=risk_chart_labels,
        risk_chart_values=risk_chart_values,
        risk_tag_options=risk_tag_options,
        chronic_options=chronic_options
    )


@bp.route('/family-members/<int:member_id>/edit', methods=['GET', 'POST'], endpoint='family_member_edit')
@login_required
def family_member_edit(member_id):
    """编辑家庭成员"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    member = FamilyMember.query.filter_by(id=member_id, user_id=current_user.id).first_or_404()
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()

    if request.method == 'POST':
        member.name = sanitize_input(request.form.get('name'), max_length=50)
        member.relation = sanitize_input(request.form.get('relation'), max_length=20)
        member.age = parse_int(request.form.get('age'))
        valid, gender = validate_gender(request.form.get('gender'))
        if not valid:
            flash(gender, 'error')
            return redirect(url_for('health.family_member_edit', member_id=member_id))
        member.gender = gender
        member.chronic_diseases = json.dumps(
            _parse_chronic_diseases_from_form(request.form), ensure_ascii=False
        )

        profile_payload = _build_member_profile_form_payload(request.form)
        if profile:
            for key, value in profile_payload.items():
                setattr(profile, key, value)
        else:
            profile = FamilyMemberProfile(member_id=member.id, **profile_payload)
            db.session.add(profile)

        db.session.commit()
        flash('家庭成员信息已更新', 'success')
        return redirect(url_for('health.family_members'))

    profile_ctx = profile_to_context(profile)
    chronic_diseases = safe_json_loads(member.chronic_diseases, [])
    return render_template(
        'family_member_edit.html',
        member=member,
        profile=profile_ctx,
        chronic_diseases=chronic_diseases,
        risk_tag_options=RISK_TAG_OPTIONS,
        chronic_options=CHRONIC_OPTIONS
    )


@bp.route('/family-members/<int:member_id>/delete', methods=['POST'], endpoint='family_member_delete')
@login_required
def family_member_delete(member_id):
    """删除家庭成员"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    member = FamilyMember.query.filter_by(id=member_id, user_id=current_user.id).first_or_404()
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
    if profile:
        db.session.delete(profile)
    db.session.delete(member)
    db.session.commit()
    flash('家庭成员已删除', 'success')
    return redirect(url_for('health.family_members'))


@bp.route('/family-members/<int:member_id>/toggle-alert', methods=['POST'], endpoint='family_member_toggle_alert')
@login_required
def family_member_toggle_alert(member_id):
    """启用/关闭成员提醒"""
    if is_guest_user(current_user):
        flash('游客模式无法管理家庭成员，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    profile = FamilyMemberProfile.query.join(FamilyMember).filter(
        FamilyMemberProfile.member_id == member_id,
        FamilyMember.user_id == current_user.id
    ).first_or_404()

    profile.alert_enabled = not bool(profile.alert_enabled)
    db.session.commit()
    status = '已开启' if profile.alert_enabled else '已关闭'
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
    weather = SimpleNamespace(**weather_data)
    alerts = []
    if profile_ctx['alert_enabled']:
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
        entry_date = parse_date(request.form.get('entry_date')) or today_local()
        member_id = parse_int(request.form.get('member_id'))
        symptoms = sanitize_input(request.form.get('symptoms'), max_length=200)
        severity = sanitize_input(request.form.get('severity'), max_length=20)
        notes = sanitize_input(request.form.get('notes'), max_length=500)

        diary = HealthDiary(
            user_id=current_user.id,
            member_id=member_id,
            entry_date=entry_date,
            symptoms=symptoms,
            severity=severity,
            notes=notes
        )
        db.session.add(diary)
        db.session.commit()
        flash('健康日记已保存', 'success')
        return redirect(url_for('health.health_diary'))

    members = FamilyMember.query.filter_by(user_id=current_user.id).order_by(FamilyMember.created_at.desc()).all()
    entries = HealthDiary.query.filter_by(user_id=current_user.id).order_by(HealthDiary.entry_date.desc()).all()
    return render_template('health_diary.html', members=members, entries=entries)


@bp.route('/medication-reminders', methods=['GET', 'POST'], endpoint='medication_reminders')
@login_required
def medication_reminders():
    """用药提醒管理"""
    if is_guest_user(current_user):
        flash('游客模式无法管理用药提醒，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
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

        reminder = MedicationReminder(
            user_id=current_user.id,
            member_id=member_id,
            medicine_name=medicine_name,
            dosage=dosage,
            frequency=frequency,
            time_of_day=time_of_day,
            weather_triggers=json.dumps(triggers, ensure_ascii=False) if triggers else None
        )
        db.session.add(reminder)
        db.session.commit()
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
    reminder = MedicationReminder.query.filter_by(id=reminder_id, user_id=current_user.id).first_or_404()
    db.session.delete(reminder)
    db.session.commit()
    flash('用药提醒已删除', 'success')
    return redirect(url_for('health.medication_reminders'))
