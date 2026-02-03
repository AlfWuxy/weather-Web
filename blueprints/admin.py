# -*- coding: utf-8 -*-
"""Admin routes."""
import json
import logging
from datetime import datetime, timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from core.extensions import db
from core.audit import log_audit
from core.db_models import Community, CoolingResource, HealthRiskAssessment, MedicalRecord, User, WeatherAlert
from core.time_utils import utcnow
from utils.parsers import parse_bool, parse_float, parse_int
from utils.validators import (
    sanitize_input, validate_age, validate_email, validate_gender, validate_password, validate_username
)

logger = logging.getLogger(__name__)

bp = Blueprint('admin', __name__)


@bp.route('/admin', endpoint='admin_dashboard')
@login_required
def admin_dashboard():
    """管理员仪表板"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    # 统计数据
    total_users = User.query.count()
    total_records = MedicalRecord.query.count()
    total_communities = Community.query.count()
    # 使用 UTC-aware 时间比较（alert_date 是 UTC 时间戳）
    active_alerts = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= utcnow() - timedelta(days=1)
    ).count()

    # 统计数据中所有月份的病例趋势（完整时间范围）
    from sqlalchemy import func

    dialect = db.session.bind.dialect.name
    if dialect == 'postgresql':
        month_expr = func.to_char(MedicalRecord.visit_time, 'YYYY-MM')
    elif dialect == 'mysql':
        month_expr = func.date_format(MedicalRecord.visit_time, '%Y-%m')
    else:
        month_expr = func.strftime('%Y-%m', MedicalRecord.visit_time)

    month_rows = db.session.query(
        month_expr.label('month'),
        func.count(MedicalRecord.id)
    ).filter(
        MedicalRecord.visit_time.isnot(None)
    ).group_by(month_expr).order_by(month_expr).all()

    month_labels = [row[0] for row in month_rows if row[0]]
    month_trend = [row[1] for row in month_rows if row[0]]

    # Top10疾病分类统计（管理后台显示前10）
    disease_stats = db.session.query(
        MedicalRecord.disease_category,
        func.count(MedicalRecord.id)
    ).filter(
        MedicalRecord.disease_category.isnot(None)
    ).group_by(MedicalRecord.disease_category).order_by(
        func.count(MedicalRecord.id).desc()
    ).limit(10).all()

    disease_labels = [stat[0] for stat in disease_stats]
    disease_counts = [stat[1] for stat in disease_stats]

    return render_template('admin_dashboard.html',
                         total_users=total_users,
                         total_records=total_records,
                         total_communities=total_communities,
                         active_alerts=active_alerts,
                         month_labels=month_labels,
                         month_trend=month_trend,
                         disease_labels=disease_labels,
                         disease_counts=disease_counts)


@bp.route('/admin/users', endpoint='admin_users')
@login_required
def admin_users():
    """用户管理"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    page = request.args.get('page', 1, type=int)
    users = User.query.paginate(page=page, per_page=20)
    return render_template('admin_users.html', users=users)


@bp.route('/admin/records', endpoint='admin_records')
@login_required
def admin_records():
    """病历记录管理"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    # 获取筛选参数
    search_query = request.args.get('search', '')
    department_filter = request.args.get('department', '')
    community_filter = request.args.get('community', '')
    disease_filter = request.args.get('disease', '')
    page = request.args.get('page', 1, type=int)

    log_audit(
        'records_view',
        resource_type='medical_records',
        metadata={
            'filters': {
                'search': bool(search_query),
                'department': bool(department_filter),
                'community': bool(community_filter),
                'disease': bool(disease_filter)
            },
            'page': page
        }
    )

    # 构建查询
    query = MedicalRecord.query

    # 搜索功能（姓名、姓氏）
    if search_query:
        query = query.filter(
            MedicalRecord.patient_name.like(f'%{search_query}%')
        )

    # 科室筛选
    if department_filter:
        query = query.filter(MedicalRecord.department == department_filter)

    # 社区筛选
    if community_filter:
        query = query.filter(MedicalRecord.community == community_filter)

    # 疾病筛选
    if disease_filter:
        query = query.filter(MedicalRecord.disease_category == disease_filter)

    # 排序和分页
    records = query.order_by(
        MedicalRecord.visit_time.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    # 获取所有科室列表（用于下拉）
    departments = db.session.query(MedicalRecord.department).filter(
        MedicalRecord.department.isnot(None)
    ).distinct().order_by(MedicalRecord.department).all()
    departments = [d[0] for d in departments]

    # 获取所有社区列表
    communities_list = db.session.query(MedicalRecord.community).filter(
        MedicalRecord.community.isnot(None)
    ).distinct().order_by(MedicalRecord.community).all()
    communities_list = [c[0] for c in communities_list]

    # 获取所有疾病分类列表
    diseases = db.session.query(MedicalRecord.disease_category).filter(
        MedicalRecord.disease_category.isnot(None)
    ).distinct().order_by(MedicalRecord.disease_category).all()
    diseases = [d[0] for d in diseases]

    return render_template('admin_records.html',
                         records=records,
                         departments=departments,
                         communities_list=communities_list,
                         diseases=diseases,
                         search_query=search_query,
                         department_filter=department_filter,
                         community_filter=community_filter,
                         disease_filter=disease_filter)


@bp.route('/admin/communities', endpoint='admin_communities')
@login_required
def admin_communities():
    """社区管理"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    communities = Community.query.all()
    return render_template('admin_communities.html', communities=communities)


@bp.route('/admin/communities/sync-coordinates', methods=['POST'], endpoint='admin_sync_community_coordinates')
@login_required
def admin_sync_community_coordinates():
    """同步社区经纬度到数据库"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    coords_map = current_app.config.get('COMMUNITY_COORDS_GCJ', {}) or {}
    if not coords_map:
        flash('坐标映射为空，无法同步', 'error')
        return redirect(url_for('admin.admin_communities'))

    communities = Community.query.all()
    updated = 0
    missing = 0

    for comm in communities:
        coords = coords_map.get(comm.name)
        if not coords or len(coords) != 2:
            missing += 1
            continue
        longitude, latitude = coords[0], coords[1]
        changed = False
        if comm.latitude != latitude:
            comm.latitude = latitude
            changed = True
        if comm.longitude != longitude:
            comm.longitude = longitude
            changed = True
        if changed:
            updated += 1

    if updated:
        db.session.commit()
        flash(f'已同步 {updated} 个社区坐标，{missing} 个社区未匹配', 'success')
    else:
        db.session.rollback()
        flash(f'坐标已是最新或未匹配社区：{missing} 个', 'info')

    return redirect(url_for('admin.admin_communities'))


@bp.route('/admin/statistics', endpoint='admin_statistics')
@login_required
def admin_statistics():
    """统计分析"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    # 获取筛选参数
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')
    community_filter = sanitize_input(request.args.get('community', ''), max_length=100)

    # 构建基础查询
    query = MedicalRecord.query

    # 解析日期
    start_date = None
    end_date = None

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        except ValueError as exc:
            logger.warning("开始日期解析失败: %s, 错误: %s", start_date_str, exc)
            start_date = None

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_date = end_date.replace(hour=23, minute=59, second=59)
        except ValueError as exc:
            logger.warning("结束日期解析失败: %s, 错误: %s", end_date_str, exc)
            end_date = None

    # 应用时间筛选
    if start_date:
        query = query.filter(MedicalRecord.visit_time >= start_date)
    if end_date:
        query = query.filter(MedicalRecord.visit_time <= end_date)

    # 社区筛选
    if community_filter:
        query = query.filter(MedicalRecord.community == community_filter)

    # 疾病分类统计 - 显示所有分类
    disease_stats = db.session.query(
        MedicalRecord.disease_category,
        db.func.count(MedicalRecord.id)
    ).filter(
        MedicalRecord.disease_category.isnot(None)
    )

    if start_date:
        disease_stats = disease_stats.filter(MedicalRecord.visit_time >= start_date)
    if end_date:
        disease_stats = disease_stats.filter(MedicalRecord.visit_time <= end_date)

    if community_filter:
        disease_stats = disease_stats.filter(MedicalRecord.community == community_filter)

    disease_stats = disease_stats.group_by(MedicalRecord.disease_category).order_by(
        db.func.count(MedicalRecord.id).desc()
    ).all()

    disease_labels = [stat[0] for stat in disease_stats]
    disease_counts = [stat[1] for stat in disease_stats]

    # 年龄分布统计（根据筛选条件）
    age_ranges = {
        '0-18岁': 0,
        '19-35岁': 0,
        '36-50岁': 0,
        '51-65岁': 0,
        '65岁以上': 0
    }

    age_query = MedicalRecord.query.filter(MedicalRecord.age.isnot(None))
    if start_date:
        age_query = age_query.filter(MedicalRecord.visit_time >= start_date)
    if end_date:
        age_query = age_query.filter(MedicalRecord.visit_time <= end_date)
    if community_filter:
        age_query = age_query.filter(MedicalRecord.community == community_filter)

    all_records = age_query.all()
    for record in all_records:
        age = record.age
        if age <= 18:
            age_ranges['0-18岁'] += 1
        elif age <= 35:
            age_ranges['19-35岁'] += 1
        elif age <= 50:
            age_ranges['36-50岁'] += 1
        elif age <= 65:
            age_ranges['51-65岁'] += 1
        else:
            age_ranges['65岁以上'] += 1

    # 性别统计（根据筛选条件）
    gender_query = db.session.query(
        MedicalRecord.gender,
        db.func.count(MedicalRecord.id)
    ).filter(MedicalRecord.gender.isnot(None))

    if start_date:
        gender_query = gender_query.filter(MedicalRecord.visit_time >= start_date)
    if end_date:
        gender_query = gender_query.filter(MedicalRecord.visit_time <= end_date)
    if community_filter:
        gender_query = gender_query.filter(MedicalRecord.community == community_filter)

    gender_stats = gender_query.group_by(MedicalRecord.gender).all()

    # 社区病例统计（根据时间筛选，不受社区筛选影响）
    community_query = db.session.query(
        MedicalRecord.community,
        db.func.count(MedicalRecord.id)
    ).filter(MedicalRecord.community.isnot(None))

    if start_date:
        community_query = community_query.filter(MedicalRecord.visit_time >= start_date)
    if end_date:
        community_query = community_query.filter(MedicalRecord.visit_time <= end_date)

    community_stats = community_query.group_by(MedicalRecord.community).all()

    community_labels = [stat[0] for stat in community_stats]
    community_counts = [stat[1] for stat in community_stats]

    # 月度就诊趋势 - 基于真实数据（根据筛选条件）
    # 获取病历记录（根据筛选）
    trend_query = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None))

    if start_date:
        trend_query = trend_query.filter(MedicalRecord.visit_time >= start_date)
    if end_date:
        trend_query = trend_query.filter(MedicalRecord.visit_time <= end_date)
    if community_filter:
        trend_query = trend_query.filter(MedicalRecord.community == community_filter)

    dialect = db.session.bind.dialect.name
    if dialect == 'postgresql':
        month_expr = db.func.to_char(MedicalRecord.visit_time, 'YYYY-MM')
    else:
        month_expr = db.func.strftime('%Y-%m', MedicalRecord.visit_time)

    month_rows = trend_query.with_entities(
        month_expr.label('month'),
        db.func.count(MedicalRecord.id)
    ).group_by(month_expr).order_by(month_expr).all()

    month_labels = [row[0] for row in month_rows if row[0]]
    month_counts = [row[1] for row in month_rows if row[0]]

    # 社区风险对比
    communities = Community.query.all()
    community_risk_labels = [c.name for c in communities]
    community_risk_values = [c.vulnerability_index or 0 for c in communities]

    # 获取社区（村庄）列表用于下拉
    all_communities = Community.query.all()

    # 社区按性别分类统计（根据时间筛选）
    community_gender_stats = {
        community: {'male': 0, 'female': 0} for community in community_labels
    }
    if community_labels:
        gender_by_comm = db.session.query(
            MedicalRecord.community,
            MedicalRecord.gender,
            db.func.count(MedicalRecord.id)
        ).filter(
            MedicalRecord.community.isnot(None),
            MedicalRecord.gender.isnot(None)
        )

        if start_date:
            gender_by_comm = gender_by_comm.filter(MedicalRecord.visit_time >= start_date)
        if end_date:
            gender_by_comm = gender_by_comm.filter(MedicalRecord.visit_time <= end_date)

        gender_by_comm = gender_by_comm.filter(
            MedicalRecord.community.in_(community_labels)
        ).group_by(
            MedicalRecord.community,
            MedicalRecord.gender
        ).all()

        for community, gender, count in gender_by_comm:
            stats = community_gender_stats.get(community)
            if not stats:
                continue
            if gender == '男':
                stats['male'] = count
            elif gender == '女':
                stats['female'] = count

    return render_template('admin_statistics.html',
                         disease_labels=disease_labels,
                         disease_counts=disease_counts,
                         age_labels=list(age_ranges.keys()),
                         age_counts=list(age_ranges.values()),
                         gender_stats=gender_stats,
                         community_labels=community_labels,
                         community_counts=community_counts,
                         month_labels=month_labels,
                         month_counts=month_counts,
                         community_risk_labels=community_risk_labels,
                         community_risk_values=community_risk_values,
                         community_gender_stats=community_gender_stats,
                         all_communities=all_communities,
                         start_date_str=start_date_str,
                         end_date_str=end_date_str,
                         community_filter=community_filter,
                         total_filtered=sum(disease_counts) if disease_counts else 0)


@bp.route('/admin/user/<int:user_id>/delete', methods=['POST'], endpoint='admin_delete_user')
@login_required
def admin_delete_user(user_id):
    """删除用户"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('不能删除管理员账户', 'error')
        return redirect(url_for('admin.admin_users'))

    db.session.delete(user)
    db.session.commit()
    flash(f'用户 {user.username} 已删除', 'success')
    return redirect(url_for('admin.admin_users'))


@bp.route('/admin/user/<int:user_id>/edit', methods=['GET', 'POST'], endpoint='admin_edit_user')
@login_required
def admin_edit_user(user_id):
    """编辑用户"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        # 验证用户名
        valid, result = validate_username(request.form.get('username'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_edit_user', user_id=user_id))
        username = result
        if username != user.username and User.query.filter_by(username=username).first():
            flash('用户名已存在', 'error')
            return redirect(url_for('admin.admin_edit_user', user_id=user_id))

        # 验证邮箱
        valid, result = validate_email(request.form.get('email'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_edit_user', user_id=user_id))
        email = result
        if email and email != user.email and User.query.filter_by(email=email).first():
            flash('邮箱已被注册', 'error')
            return redirect(url_for('admin.admin_edit_user', user_id=user_id))

        # 验证年龄
        valid, result = validate_age(request.form.get('age'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_edit_user', user_id=user_id))
        age = result

        # 验证性别
        valid, result = validate_gender(request.form.get('gender'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_edit_user', user_id=user_id))
        gender = result

        community = sanitize_input(request.form.get('community'), max_length=100)

        new_password = request.form.get('password')
        if new_password:
            valid, result = validate_password(new_password)
            if not valid:
                flash(result, 'error')
                return redirect(url_for('admin.admin_edit_user', user_id=user_id))
            user.set_password(result)

        role = request.form.get('role', user.role or 'user')
        if role not in ['admin', 'user', 'caregiver', 'community']:
            role = user.role or 'user'

        user.username = username
        user.email = email
        user.age = age
        user.gender = gender
        user.community = community
        user.role = role

        db.session.commit()
        flash('用户信息更新成功', 'success')
        return redirect(url_for('admin.admin_users'))

    communities = Community.query.all()
    return render_template('admin_edit_user.html', user=user, communities=communities)


@bp.route('/admin/user/add', methods=['GET', 'POST'], endpoint='admin_add_user')
@login_required
def admin_add_user():
    """添加用户"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        # 验证用户名
        valid, result = validate_username(request.form.get('username'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_add_user'))
        username = result

        # 验证密码
        valid, result = validate_password(request.form.get('password'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_add_user'))
        password = result

        # 验证邮箱
        valid, result = validate_email(request.form.get('email'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_add_user'))
        email = result

        # 验证年龄
        valid, result = validate_age(request.form.get('age'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_add_user'))
        age = result

        # 验证性别
        valid, result = validate_gender(request.form.get('gender'))
        if not valid:
            flash(result, 'error')
            return redirect(url_for('admin.admin_add_user'))
        gender = result

        community = sanitize_input(request.form.get('community'), max_length=100)
        role = request.form.get('role', 'user')
        if role not in ['admin', 'user', 'caregiver', 'community']:
            role = 'user'

        if User.query.filter_by(username=username).first():
            flash('用户名已存在', 'error')
            return redirect(url_for('admin.admin_add_user'))

        if email and User.query.filter_by(email=email).first():
            flash('邮箱已被注册', 'error')
            return redirect(url_for('admin.admin_add_user'))

        user = User(
            username=username,
            email=email,
            age=age,
            gender=gender,
            community=community,
            role=role
        )
        user.set_password(password)

        db.session.add(user)
        db.session.commit()
        flash('用户添加成功', 'success')
        return redirect(url_for('admin.admin_users'))

    communities = Community.query.all()
    return render_template('admin_add_user.html', communities=communities)


@bp.route('/admin/community/add', methods=['GET', 'POST'], endpoint='admin_add_community')
@login_required
def admin_add_community():
    """添加社区"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        name = sanitize_input(request.form.get('name'), max_length=100)
        if not name:
            flash('社区名称不能为空', 'error')
            return redirect(url_for('admin.admin_add_community'))

        # 检查社区名称是否已存在
        if Community.query.filter_by(name=name).first():
            flash('社区名称已存在', 'error')
            return redirect(url_for('admin.admin_add_community'))

        # 创建新社区
        community = Community(
            name=name,
            location=sanitize_input(request.form.get('location'), max_length=200),
            latitude=parse_float(request.form.get('latitude')),
            longitude=parse_float(request.form.get('longitude')),
            population=parse_int(request.form.get('population')),
            elderly_ratio=parse_float(request.form.get('elderly_ratio'), default=0),
            chronic_disease_ratio=parse_float(request.form.get('chronic_disease_ratio'), default=0)
        )

        # 计算脆弱性指数
        from services.health_risk_service import HealthRiskService
        service = HealthRiskService()
        result = service.calculate_community_vulnerability_index({
            'elderly_ratio': community.elderly_ratio or 0,
            'chronic_disease_ratio': community.chronic_disease_ratio or 0,
            'medical_accessibility': 60,
            'env_quality_score': 70
        })
        community.vulnerability_index = result['vulnerability_index']
        community.risk_level = result['risk_level']

        db.session.add(community)
        db.session.commit()

        flash('社区添加成功', 'success')
        return redirect(url_for('admin.admin_communities'))

    return render_template('admin_add_community.html')


@bp.route('/admin/community/<int:community_id>/edit', methods=['GET', 'POST'], endpoint='admin_edit_community')
@login_required
def admin_edit_community(community_id):
    """编辑社区"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    community = Community.query.get_or_404(community_id)

    if request.method == 'POST':
        name = sanitize_input(request.form.get('name'), max_length=100)
        if not name:
            flash('社区名称不能为空', 'error')
            return redirect(url_for('admin.admin_edit_community', community_id=community_id))
        if name != community.name and Community.query.filter_by(name=name).first():
            flash('社区名称已存在', 'error')
            return redirect(url_for('admin.admin_edit_community', community_id=community_id))

        community.name = name
        community.location = sanitize_input(request.form.get('location'), max_length=200)
        community.latitude = parse_float(request.form.get('latitude'))
        community.longitude = parse_float(request.form.get('longitude'))
        community.population = parse_int(request.form.get('population'))
        community.elderly_ratio = parse_float(request.form.get('elderly_ratio'), default=0)
        community.chronic_disease_ratio = parse_float(request.form.get('chronic_disease_ratio'), default=0)

        # 重新计算脆弱性指数
        from services.health_risk_service import HealthRiskService
        service = HealthRiskService()
        result = service.calculate_community_vulnerability_index({
            'elderly_ratio': community.elderly_ratio,
            'chronic_disease_ratio': community.chronic_disease_ratio,
            'medical_accessibility': 60,
            'env_quality_score': 70
        })
        community.vulnerability_index = result['vulnerability_index']
        community.risk_level = result['risk_level']

        db.session.commit()
        flash('社区信息更新成功', 'success')
        return redirect(url_for('admin.admin_communities'))

    return render_template('admin_edit_community.html', community=community)


@bp.route('/admin/cooling', endpoint='admin_cooling_resources')
@login_required
def admin_cooling_resources():
    """避暑资源管理"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    community = sanitize_input(request.args.get('community'), max_length=100)
    query = CoolingResource.query
    if community:
        query = query.filter(CoolingResource.community_code == community)
    resources = query.order_by(
        CoolingResource.community_code,
        CoolingResource.name
    ).all()
    communities = Community.query.order_by(Community.name).all()
    return render_template(
        'admin_cooling_resources.html',
        resources=resources,
        communities=communities,
        selected_community=community or ''
    )


@bp.route('/admin/cooling/add', methods=['GET', 'POST'], endpoint='admin_add_cooling_resource')
@login_required
def admin_add_cooling_resource():
    """添加避暑资源"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    if request.method == 'POST':
        community_code = sanitize_input(request.form.get('community_code'), max_length=100)
        name = sanitize_input(request.form.get('name'), max_length=120)
        if not community_code or not name:
            flash('请填写社区和名称', 'error')
            return redirect(url_for('admin.admin_add_cooling_resource'))

        resource = CoolingResource(
            community_code=community_code,
            name=name,
            resource_type=sanitize_input(request.form.get('resource_type'), max_length=50),
            address_hint=sanitize_input(request.form.get('address_hint'), max_length=200),
            latitude=parse_float(request.form.get('latitude')),
            longitude=parse_float(request.form.get('longitude')),
            open_hours=sanitize_input(request.form.get('open_hours'), max_length=100),
            has_ac=parse_bool(request.form.get('has_ac'), default=False),
            is_accessible=parse_bool(request.form.get('is_accessible'), default=False),
            contact_hint=sanitize_input(request.form.get('contact_hint'), max_length=100),
            notes=sanitize_input(request.form.get('notes'), max_length=500),
            is_active=parse_bool(request.form.get('is_active'), default=True)
        )
        db.session.add(resource)
        db.session.commit()
        flash('避暑资源已添加', 'success')
        return redirect(url_for('admin.admin_cooling_resources'))

    communities = Community.query.order_by(Community.name).all()
    return render_template('admin_add_cooling_resource.html', communities=communities)


@bp.route('/admin/cooling/<int:resource_id>/edit', methods=['GET', 'POST'], endpoint='admin_edit_cooling_resource')
@login_required
def admin_edit_cooling_resource(resource_id):
    """编辑避暑资源"""
    if current_user.role != 'admin':
        flash('权限不足', 'error')
        return redirect(url_for('user.user_dashboard'))

    resource = CoolingResource.query.get_or_404(resource_id)
    if request.method == 'POST':
        community_code = sanitize_input(request.form.get('community_code'), max_length=100)
        name = sanitize_input(request.form.get('name'), max_length=120)
        if not community_code or not name:
            flash('请填写社区和名称', 'error')
            return redirect(url_for('admin.admin_edit_cooling_resource', resource_id=resource_id))

        resource.community_code = community_code
        resource.name = name
        resource.resource_type = sanitize_input(request.form.get('resource_type'), max_length=50)
        resource.address_hint = sanitize_input(request.form.get('address_hint'), max_length=200)
        resource.latitude = parse_float(request.form.get('latitude'))
        resource.longitude = parse_float(request.form.get('longitude'))
        resource.open_hours = sanitize_input(request.form.get('open_hours'), max_length=100)
        resource.has_ac = parse_bool(request.form.get('has_ac'), default=False)
        resource.is_accessible = parse_bool(request.form.get('is_accessible'), default=False)
        resource.contact_hint = sanitize_input(request.form.get('contact_hint'), max_length=100)
        resource.notes = sanitize_input(request.form.get('notes'), max_length=500)
        resource.is_active = parse_bool(request.form.get('is_active'), default=True)

        db.session.commit()
        flash('避暑资源已更新', 'success')
        return redirect(url_for('admin.admin_cooling_resources'))

    communities = Community.query.order_by(Community.name).all()
    return render_template(
        'admin_edit_cooling_resource.html',
        resource=resource,
        communities=communities
    )
