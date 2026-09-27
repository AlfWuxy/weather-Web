# -*- coding: utf-8 -*-
"""Analysis and report routes."""
import csv
import io
import json
import logging
import math
from collections import defaultdict
from datetime import timedelta
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from core.constants import DEFAULT_CITY_LABEL
from core.extensions import db
from core.guest import is_guest_user
from core.analytics import pearson_corr
from core.audit import log_audit
from core.db_models import (
    AlertDelivery,
    Community,
    HealthDiary,
    HealthRiskAssessment,
    MedicalRecord,
    Pair,
    UsageEvent,
    WeatherAlert,
    WeatherData
)
from core.time_utils import today_local, date_to_utc_start, date_to_utc_end, utc_to_local_date, utcnow
from utils.parsers import parse_date
from utils.validators import sanitize_input
from services.analysis_stats import (
    CERTAINTY_LABELS,
    OUTCOME_LABELS,
    SEVERITY_LABELS,
    STRATUM_LABELS,
    URGENCY_LABELS,
    action_from_alert_semantics,
    action_level,
    alert_cap_semantics,
    build_daily_weather,
    build_quantile_bins,
    certainty_level,
    certainty_to_probability,
    compute_contingency_scores,
    compute_date_overlap,
    corr_with_ci,
    date_span,
    find_bin,
    format_bucket_label,
    gini,
    heatmap_cell_color,
    impact_bucket_from_severity,
    is_significant,
    json_loads_safe,
    lag_exposure_for_date,
    likelihood_bucket_from_certainty,
    percentile,
    record_matches_stratum,
    roc_auc_from_pairs,
    rr_with_ci,
    safe_int,
    safe_ratio,
)

logger = logging.getLogger(__name__)

bp = Blueprint('analysis', __name__)


def _require_admin():
    if getattr(current_user, 'role', None) != 'admin':
        flash('权限不足', 'error')
        return False
    return True


def admin_route(rule, **options):
    """注册仅管理员可访问的页面：先要求登录，非管理员提示后回到用户首页。"""
    def decorator(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            if not _require_admin():
                return redirect(url_for('user.user_dashboard'))
            return view(*args, **kwargs)
        return bp.route(rule, **options)(login_required(guarded))
    return decorator


def _default_city():
    return current_app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL


def _weather_source_label(location, default_city):
    if location and location == default_city:
        return f"{location}（县级）"
    return location or default_city


def _load_weather_records(start_date, end_date, community_filter):
    base_query = WeatherData.query.filter(
        WeatherData.date >= start_date,
        WeatherData.date <= end_date
    )
    default_city = _default_city()
    weather_location = community_filter or default_city
    used_fallback = False
    weather_records = []

    if community_filter:
        weather_records = base_query.filter(WeatherData.location == community_filter).all()
        if not weather_records:
            weather_records = base_query.filter(WeatherData.location == default_city).all()
            used_fallback = True
            weather_location = default_city
    else:
        weather_records = base_query.filter(WeatherData.location == default_city).all()
        if not weather_records:
            weather_records = base_query.all()
            if weather_records:
                weather_location = weather_records[0].location
                used_fallback = True

    return weather_records, weather_location, used_fallback


def _latest_visit_date(community_filter=None, disease_filter=None):
    query = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None))
    if community_filter:
        query = query.filter(MedicalRecord.community == community_filter)
    if disease_filter:
        query = query.filter(MedicalRecord.disease_category == disease_filter)
    latest = query.with_entities(db.func.max(MedicalRecord.visit_time)).scalar()
    return latest.date() if latest else None


def _latest_weather_date(location):
    query = WeatherData.query
    if location:
        query = query.filter(WeatherData.location == location)
    latest = query.with_entities(db.func.max(WeatherData.date)).scalar()
    return latest


# ======================== 病例分析页共用 ========================

def _text_arg(name, max_length):
    return sanitize_input(request.values.get(name), max_length=max_length)


def _stratum_arg():
    stratum = _text_arg('stratum', 30) or 'all'
    return stratum if stratum in STRATUM_LABELS else 'all'


def _disease_options():
    rows = db.session.query(MedicalRecord.disease_category).filter(
        MedicalRecord.disease_category.isnot(None)
    ).distinct().order_by(MedicalRecord.disease_category).all()
    return [row[0] for row in rows]


def _resolve_case_date_range(community_filter, disease_filter, window_days):
    """解析病例分析的日期区间；未指定时自动定位到病例与天气都有数据的最近区间。"""
    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    auto_range = False
    if not start_raw and not end_raw:
        last_visit = _latest_visit_date(community_filter, disease_filter)
        default_city = _default_city()
        last_weather = _latest_weather_date(community_filter or default_city)
        if not last_weather and community_filter:
            last_weather = _latest_weather_date(default_city)
        candidates = [d for d in (last_visit, last_weather) if d]
        if candidates:
            end_date = min(candidates)
            start_date = end_date - timedelta(days=window_days)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=window_days)
    return start_date, end_date, auto_range


def _visit_query(start_date, end_date, community_filter=None, disease_filter=None):
    query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    )
    if community_filter:
        query = query.filter(MedicalRecord.community == community_filter)
    if disease_filter:
        query = query.filter(MedicalRecord.disease_category == disease_filter)
    return query


def _daily_visits_in_stratum(query, stratum):
    """按分层过滤病例并按本地日期计数，返回 (每日病例数, 纳入病例数)。"""
    daily = {}
    count = 0
    for record in query.with_entities(MedicalRecord.visit_time, MedicalRecord.age, MedicalRecord.gender).all():
        if not record_matches_stratum(record.age, record.gender, stratum):
            continue
        day = utc_to_local_date(record.visit_time)
        daily[day] = daily.get(day, 0) + 1
        count += 1
    return daily, count


def _case_data_notes(auto_range, community_filter, used_fallback, weather_source,
                     total_visits, weather_days, overlap_days, total_days):
    notes = []
    if auto_range:
        notes.append("已自动定位到最近有数据的时间区间")
    if community_filter and used_fallback:
        notes.append(f"社区无天气数据，已使用{weather_source}")
    if total_visits == 0:
        notes.append("所选区间无门诊记录")
    if weather_days == 0:
        notes.append(f"所选区间无{weather_source}天气数据")
    if total_visits > 0 and weather_days > 0 and overlap_days == 0:
        notes.append("病例与天气日期无重叠")
    if weather_days < total_days:
        notes.append(f"天气覆盖{weather_days}/{total_days}天")
    return notes


# ======================== 预警核验页共用 ========================

THRESHOLD_Q_OPTIONS = [0.75, 0.80, 0.85, 0.90, 0.95]


def _alert_filters():
    """预警核验页的筛选参数：地点、类型、等级、跟踪天数、最少样本天数、阈值分位。"""
    try:
        threshold_q = float(request.values.get('threshold_q', 0.90))
    except (TypeError, ValueError):
        threshold_q = 0.90
    if threshold_q not in THRESHOLD_Q_OPTIONS:
        threshold_q = min(THRESHOLD_Q_OPTIONS, key=lambda option: abs(option - threshold_q))
    return {
        'location': _text_arg('location', 100),
        'alert_type': _text_arg('alert_type', 60),
        'alert_level': _text_arg('alert_level', 30),
        'follow_days': safe_int(request.values.get('follow_days'), 3, minimum=1, maximum=7),
        'min_days': safe_int(request.values.get('min_days'), 7, minimum=3, maximum=45),
        'threshold_q': threshold_q,
    }


def _alert_record_coverage():
    """全库预警与病例的时间覆盖范围及其交集。"""
    def local_day(value):
        return utc_to_local_date(value) if value else None

    visits = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None))
    coverage = {
        'alert_min': local_day(WeatherAlert.query.with_entities(db.func.min(WeatherAlert.alert_date)).scalar()),
        'alert_max': local_day(WeatherAlert.query.with_entities(db.func.max(WeatherAlert.alert_date)).scalar()),
        'record_min': local_day(visits.with_entities(db.func.min(MedicalRecord.visit_time)).scalar()),
        'record_max': local_day(visits.with_entities(db.func.max(MedicalRecord.visit_time)).scalar()),
    }
    coverage['overlap_start'], coverage['overlap_end'], coverage['has_overlap'] = compute_date_overlap(
        coverage['alert_min'], coverage['alert_max'], coverage['record_min'], coverage['record_max']
    )
    return coverage


def _coverage_note(coverage):
    if all(coverage[key] for key in ('alert_min', 'alert_max', 'record_min', 'record_max')) \
            and not coverage['has_overlap']:
        return (
            f"预警时间范围 {coverage['alert_min']}~{coverage['alert_max']} 与病例时间范围 "
            f"{coverage['record_min']}~{coverage['record_max']} 无重叠，命中仅可视为不可核验"
        )
    return None


def _resolve_alert_date_range(auto_window_days, default_window_days):
    """解析预警核验的日期区间；未指定时以最近一条预警为终点，起止颠倒时自动交换。"""
    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    auto_range = False
    date_swapped = False
    if not start_raw and not end_raw:
        latest_alert_utc = WeatherAlert.query.with_entities(db.func.max(WeatherAlert.alert_date)).scalar()
        latest_alert_date = utc_to_local_date(latest_alert_utc) if latest_alert_utc else None
        if latest_alert_date:
            end_date = latest_alert_date
            start_date = end_date - timedelta(days=auto_window_days)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=default_window_days)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        date_swapped = True
    return start_date, end_date, auto_range, date_swapped


def _filtered_alerts(start_date, end_date, filters):
    """返回区间内的类型/等级选项，以及按筛选条件过滤、按时间倒序的预警。"""
    range_query = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= date_to_utc_start(start_date),
        WeatherAlert.alert_date <= date_to_utc_end(end_date)
    )

    def distinct_values(column):
        return [item[0] for item in range_query.with_entities(column).distinct().order_by(column).all() if item[0]]

    query = range_query
    if filters['location']:
        query = query.filter(WeatherAlert.location.contains(filters['location']))
    if filters['alert_type']:
        query = query.filter(WeatherAlert.alert_type == filters['alert_type'])
    if filters['alert_level']:
        query = query.filter(WeatherAlert.alert_level == filters['alert_level'])
    alert_rows = query.order_by(WeatherAlert.alert_date.desc()).all()
    return distinct_values(WeatherAlert.alert_type), distinct_values(WeatherAlert.alert_level), alert_rows


def _daily_visits_by_location(record_start, record_end):
    """按社区与全局统计每日病例数，返回 (病例行, 分社区每日病例, 全局每日病例)。"""
    records = _visit_query(record_start, record_end).with_entities(
        MedicalRecord.community,
        MedicalRecord.visit_time
    ).all()
    daily_visits = {}
    global_daily_visits = {}
    for record in records:
        day = utc_to_local_date(record.visit_time)
        if day is None:
            continue
        key = (record.community or '').strip() or '未知'
        day_map = daily_visits.setdefault(key, {})
        day_map[day] = day_map.get(day, 0) + 1
        global_daily_visits[day] = global_daily_visits.get(day, 0) + 1
    return records, daily_visits, global_daily_visits


def _visit_threshold(day_map, all_days, min_days, q_value):
    """病例超阈值判定线：观测天数不足时返回 None。返回 (阈值, 有病例天数)。"""
    values = [day_map.get(day, 0) for day in all_days]
    observed_days = sum(1 for value in values if value > 0)
    if observed_days < min_days:
        return None, observed_days
    return percentile(sorted(values), q_value), observed_days


def _threshold_profile(daily_visits, global_daily_visits, all_days, min_days, q_value):
    threshold_by_location = {}
    sample_days_by_location = {}
    for key, day_map in daily_visits.items():
        threshold_by_location[key], sample_days_by_location[key] = _visit_threshold(
            day_map, all_days, min_days, q_value
        )
    global_threshold, global_sample_days = _visit_threshold(global_daily_visits, all_days, min_days, q_value)
    return {
        'threshold_by_location': threshold_by_location,
        'sample_days_by_location': sample_days_by_location,
        'global_threshold': global_threshold,
        'global_sample_days': global_sample_days
    }


def _alert_baseline(location_key, daily_visits, global_daily_visits, profile):
    """预警所在社区有病例时用社区基线，否则回退全局基线。

    返回 (每日病例, 阈值, 有病例天数, 核验键, 基线名称, 是否全局)。
    """
    day_map = daily_visits.get(location_key)
    if day_map is None:
        return (global_daily_visits, profile['global_threshold'], profile['global_sample_days'],
                '__GLOBAL__', '全局基线', True)
    return (day_map, profile['threshold_by_location'].get(location_key),
            profile['sample_days_by_location'].get(location_key, 0), location_key, location_key, False)


def _baseline_for_key(key, daily_visits, global_daily_visits, profile):
    """按核验键取 (每日病例, 阈值, 名称)。"""
    if key == '__GLOBAL__':
        return global_daily_visits, profile['global_threshold'], '全局基线'
    return daily_visits.get(key, {}), profile['threshold_by_location'].get(key), key


def _alert_window(alert_day, day_map, follow_days, threshold, threshold_evaluable):
    """预警后 follow_days 天的病例轨迹，返回 (轨迹点, 峰值, 峰值日, 首次超阈值日)。"""
    window_points = []
    peak_visits = 0
    peak_day = None
    first_hit_day = None
    if alert_day is not None:
        for offset in range(follow_days + 1):
            day = alert_day + timedelta(days=offset)
            visits = day_map.get(day, 0)
            window_points.append({'day': day.strftime('%Y-%m-%d'), 'visits': visits})
            if visits > peak_visits:
                peak_visits = visits
                peak_day = day
            if threshold_evaluable and first_hit_day is None and visits >= threshold:
                first_hit_day = day
    return window_points, peak_visits, peak_day, first_hit_day


def _alert_row(alert, alert_day, location_key, threshold, threshold_evaluable, threshold_source,
               window, outcome, semantics):
    """两个预警核验页共用的单条预警展示字段。"""
    window_points, peak_visits, peak_day, first_hit_day = window
    severity, certainty, urgency = semantics
    lead_days = (first_hit_day - alert_day).days if (first_hit_day and alert_day) else None
    return {
        'id': alert.id,
        'alert_day': alert_day,
        'alert_time_text': alert.alert_date.strftime('%Y-%m-%d %H:%M') if alert.alert_date else '--',
        'location': location_key,
        'alert_type': alert.alert_type or '--',
        'alert_level': alert.alert_level or '--',
        'description': alert.description or '--',
        'threshold': threshold,
        'threshold_evaluable': threshold_evaluable,
        'threshold_source': threshold_source,
        'peak_visits': peak_visits,
        'peak_day_text': peak_day.strftime('%Y-%m-%d') if peak_day else '--',
        'first_hit_day_text': first_hit_day.strftime('%Y-%m-%d') if first_hit_day else '--',
        'lead_days': lead_days,
        'lead_hours': lead_days * 24 if lead_days is not None else None,
        'observed_ratio': (peak_visits / threshold) if (threshold_evaluable and threshold and threshold > 0) else None,
        'outcome': outcome,
        'outcome_label': OUTCOME_LABELS[outcome],
        'severity': severity,
        'severity_label': SEVERITY_LABELS.get(severity, severity),
        'certainty': certainty,
        'certainty_label': CERTAINTY_LABELS.get(certainty, certainty),
        'urgency': urgency,
        'urgency_label': URGENCY_LABELS.get(urgency, urgency),
        'window_points': window_points,
    }


@admin_route('/analysis/history', methods=['GET', 'POST'], endpoint='analysis_history')
def analysis_history():
    """历史数据回溯分析"""
    community_filter = _text_arg('community', 100)
    disease_filter = _text_arg('disease', 100)
    start_date, end_date, auto_range = _resolve_case_date_range(community_filter, disease_filter, 30)

    communities = Community.query.all()
    diseases = _disease_options()

    # 统计每日病例
    visits_by_date, _ = _daily_visits_in_stratum(
        _visit_query(start_date, end_date, community_filter, disease_filter), 'all'
    )

    # 天气数据（按日平均）
    weather_records, weather_location, used_fallback = _load_weather_records(
        start_date, end_date, community_filter
    )
    default_city = _default_city()
    weather_source = _weather_source_label(weather_location, default_city)

    weather_by_date = {}
    weather_counts = {}
    for w in weather_records:
        date_key = w.date
        if date_key not in weather_by_date:
            weather_by_date[date_key] = {
                'temperature': 0,
                'humidity': 0
            }
            weather_counts[date_key] = 0
        weather_by_date[date_key]['temperature'] += w.temperature or 0
        weather_by_date[date_key]['humidity'] += w.humidity or 0
        weather_counts[date_key] += 1

    for date_key, count in weather_counts.items():
        if count > 0:
            weather_by_date[date_key]['temperature'] /= count
            weather_by_date[date_key]['humidity'] /= count

    days = date_span(start_date, end_date)
    dates = [day.strftime('%Y-%m-%d') for day in days]
    visits = [visits_by_date.get(day, 0) for day in days]
    temperatures = [weather_by_date[day]['temperature'] if day in weather_by_date else None for day in days]
    humidities = [weather_by_date[day]['humidity'] if day in weather_by_date else None for day in days]

    # 相关性
    paired_temp = [(v, t) for v, t in zip(visits, temperatures) if t is not None]
    paired_hum = [(v, h) for v, h in zip(visits, humidities) if h is not None]
    temp_corr = pearson_corr([p[0] for p in paired_temp], [p[1] for p in paired_temp])
    hum_corr = pearson_corr([p[0] for p in paired_hum], [p[1] for p in paired_hum])
    temp_n = len(paired_temp)
    hum_n = len(paired_hum)

    total_days = (end_date - start_date).days + 1
    visit_days = len(visits_by_date)
    weather_days = len(weather_by_date)
    overlap_days = len(set(visits_by_date.keys()) & set(weather_by_date.keys()))
    total_visits = sum(visits)
    data_notes = _case_data_notes(auto_range, community_filter, used_fallback, weather_source,
                                  total_visits, weather_days, overlap_days, total_days)

    data_summary = {
        'total_days': total_days,
        'visit_days': visit_days,
        'total_visits': total_visits,
        'weather_days': weather_days,
        'overlap_days': overlap_days,
        'weather_source': weather_source
    }

    return render_template(
        'analysis_history.html',
        communities=communities,
        diseases=diseases,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        community_filter=community_filter,
        disease_filter=disease_filter,
        dates=dates,
        visits=visits,
        temperatures=temperatures,
        humidities=humidities,
        temp_corr=temp_corr,
        hum_corr=hum_corr,
        temp_n=temp_n,
        hum_n=hum_n,
        data_summary=data_summary,
        data_notes=data_notes,
        ui_version='HISTORY-WIREFRAME-2026-02-13',
        runtime_root=current_app.root_path
    )


@admin_route('/analysis/heatmap', methods=['GET', 'POST'], endpoint='analysis_heatmap')
def analysis_heatmap():
    """天气-疾病相关性热力图（RR + 滞后 + 不确定性）"""
    community_filter = _text_arg('community', 100)
    disease_filter = _text_arg('disease', 100)
    stratum = _stratum_arg()

    lag_window = safe_int(request.values.get('lag_window'), 7, minimum=0, maximum=21)
    if lag_window not in {0, 3, 7, 14, 21}:
        lag_window = 7

    binning = _text_arg('binning', 20) or 'fixed'
    if binning not in {'fixed', 'quantile'}:
        binning = 'fixed'

    min_days = safe_int(request.values.get('min_days'), 3, minimum=1, maximum=14)
    start_date, end_date, auto_range = _resolve_case_date_range(community_filter, disease_filter, 90)

    communities = Community.query.all()
    diseases = _disease_options()
    daily_counts, filtered_record_count = _daily_visits_in_stratum(
        _visit_query(start_date, end_date, community_filter, disease_filter), stratum
    )

    # 为了支持 lag exposure，天气查询窗口前移 lag_window 天。
    lag_start_date = start_date - timedelta(days=lag_window)
    weather_records, weather_location, used_fallback = _load_weather_records(
        lag_start_date, end_date, community_filter
    )
    default_city = _default_city()
    weather_source = _weather_source_label(weather_location, default_city)
    weather_by_date = build_daily_weather(weather_records)

    # 构建日级别分析样本（每一天一个 exposure + outcome）。
    analysis_points = []
    for day in date_span(start_date, end_date):
        exposure = lag_exposure_for_date(day, lag_window, weather_by_date)
        if exposure:
            analysis_points.append({
                'date': day,
                'visits': daily_counts.get(day, 0),
                'temperature': exposure['temperature'],
                'humidity': exposure['humidity'],
                'month': day.month,
                'weekday': day.weekday()
            })

    temp_values = [point['temperature'] for point in analysis_points]
    hum_values = [point['humidity'] for point in analysis_points]
    fixed_temp_bins = [-30, -10, 0, 10, 20, 30, 40, 55]
    fixed_hum_bins = [0, 20, 40, 60, 80, 100]
    if binning == 'quantile':
        temp_bins = build_quantile_bins(temp_values, len(fixed_temp_bins) - 1, fixed_temp_bins)
        humidity_bins = build_quantile_bins(hum_values, len(fixed_hum_bins) - 1, fixed_hum_bins)
    else:
        temp_bins = fixed_temp_bins
        humidity_bins = fixed_hum_bins

    # 以 month + weekday 做简化校正基线，减少季节/周内结构偏差。
    baseline_bucket = {}
    for point in analysis_points:
        key = (point['month'], point['weekday'])
        stats = baseline_bucket.setdefault(key, {'visits': 0, 'days': 0})
        stats['visits'] += point['visits']
        stats['days'] += 1

    valid_exposure_days = len(analysis_points)
    total_visits = sum(daily_counts.values())
    overall_baseline_rate = (total_visits / valid_exposure_days) if valid_exposure_days else 0.0
    baseline_rate = {}
    for key, stats in baseline_bucket.items():
        if stats['days'] > 0:
            baseline_rate[key] = stats['visits'] / stats['days']
        else:
            baseline_rate[key] = overall_baseline_rate

    # 统计每个温湿度格子的 observed / expected。
    matrix_raw = [
        [
            {'visits': 0, 'days': 0, 'expected': 0.0}
            for _ in range(len(humidity_bins) - 1)
        ]
        for _ in range(len(temp_bins) - 1)
    ]

    for point in analysis_points:
        temp_idx = find_bin(point['temperature'], temp_bins)
        hum_idx = find_bin(point['humidity'], humidity_bins)
        if temp_idx is None or hum_idx is None:
            continue
        cell = matrix_raw[temp_idx][hum_idx]
        cell['visits'] += point['visits']
        cell['days'] += 1
        key = (point['month'], point['weekday'])
        cell['expected'] += baseline_rate.get(key, overall_baseline_rate)

    temp_labels = [
        format_bucket_label(temp_bins[idx], temp_bins[idx + 1], '°C')
        for idx in range(len(temp_bins) - 1)
    ]
    hum_labels = [
        format_bucket_label(humidity_bins[idx], humidity_bins[idx + 1], '%')
        for idx in range(len(humidity_bins) - 1)
    ]

    certainty_counts = {'high': 0, 'medium': 0, 'low': 0, 'insufficient': 0}
    heatmap_rows = []
    top_risk_cells = []

    for temp_idx, row in enumerate(matrix_raw):
        row_cells = []
        for hum_idx, raw_cell in enumerate(row):
            visits = raw_cell['visits']
            days = raw_cell['days']
            expected = raw_cell['expected']
            rate = (visits / days) if days > 0 else None
            rr, ci_low, ci_high = rr_with_ci(visits, expected) if days > 0 else (None, None, None)
            significant = is_significant(rr, days, min_days, ci_low, ci_high)
            certainty = certainty_level(days, visits, ci_low, ci_high, min_days)
            certainty_counts[certainty] += 1
            action = action_level(rr, significant, certainty, days, min_days)

            cell = {
                'temp_idx': temp_idx,
                'hum_idx': hum_idx,
                'temp_label': temp_labels[temp_idx],
                'hum_label': hum_labels[hum_idx],
                'visits': visits,
                'days': days,
                'expected': expected,
                'rate': rate,
                'rr': rr,
                'ci_low': ci_low,
                'ci_high': ci_high,
                'significant': significant,
                'certainty': certainty,
                'action': action,
                'bg_color': heatmap_cell_color(rr, days, min_days)
            }
            row_cells.append(cell)
            if rr is not None and days >= min_days and rr >= 1.2:
                top_risk_cells.append(cell)
        heatmap_rows.append(row_cells)

    high_risk_cell_total = len(top_risk_cells)
    top_risk_cells = sorted(
        top_risk_cells,
        key=lambda cell: ((cell['rr'] or 0), cell['visits'], cell['days']),
        reverse=True
    )[:5]

    max_rr = 0.0
    max_visits = 0
    for row in heatmap_rows:
        for cell in row:
            if cell['rr'] is not None and cell['days'] >= min_days:
                max_rr = max(max_rr, cell['rr'])
            max_visits = max(max_visits, cell['visits'])

    total_days = (end_date - start_date).days + 1
    visit_days = len(daily_counts)
    weather_days = len([
        day for day in weather_by_date.keys()
        if start_date <= day <= end_date
    ])
    overlap_days = len(set(daily_counts.keys()) & set(
        day for day in weather_by_date.keys() if start_date <= day <= end_date
    ))
    missing_exposure_days = max(0, total_days - valid_exposure_days)

    binning_labels = {
        'fixed': '固定阈值分箱',
        'quantile': '分位数分箱'
    }

    data_notes = _case_data_notes(auto_range, community_filter, used_fallback, weather_source,
                                  total_visits, weather_days, overlap_days, total_days)
    if missing_exposure_days > 0:
        data_notes.append(f"滞后窗口为 {lag_window} 天，可用于建模的暴露样本为 {valid_exposure_days}/{total_days} 天")
    if valid_exposure_days == 0:
        data_notes.append("当前筛选下没有可计算的暴露-病例样本")

    data_summary = {
        'total_days': total_days,
        'visit_days': visit_days,
        'total_visits': total_visits,
        'weather_days': weather_days,
        'overlap_days': overlap_days,
        'weather_source': weather_source,
        'valid_exposure_days': valid_exposure_days,
        'missing_exposure_days': missing_exposure_days,
        'baseline_daily_rate': overall_baseline_rate,
        'max_rr': max_rr,
        'high_risk_cells': high_risk_cell_total,
        'filtered_record_count': filtered_record_count
    }

    return render_template(
        'analysis_heatmap.html',
        communities=communities,
        diseases=diseases,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        community_filter=community_filter,
        disease_filter=disease_filter,
        stratum=stratum,
        stratum_labels=STRATUM_LABELS,
        lag_window=lag_window,
        binning=binning,
        min_days=min_days,
        lag_options=[0, 3, 7, 14, 21],
        min_days_options=[1, 2, 3, 5, 7],
        binning_labels=binning_labels,
        temp_labels=temp_labels,
        hum_labels=hum_labels,
        heatmap_rows=heatmap_rows,
        certainty_counts=certainty_counts,
        top_risk_cells=top_risk_cells,
        max_visits=max_visits,
        data_summary=data_summary,
        data_notes=data_notes
    )


@admin_route('/analysis/lag', methods=['GET', 'POST'], endpoint='analysis_lag')
def analysis_lag():
    """滞后效应可视化（lag-response + cumulative + risk semantics）"""
    community_filter = _text_arg('community', 100)
    disease_filter = _text_arg('disease', 100)
    stratum = _stratum_arg()

    max_lag = safe_int(request.values.get('max_lag'), 14, minimum=7, maximum=21)
    if max_lag not in {7, 14, 21}:
        max_lag = 14
    min_days = safe_int(request.values.get('min_days'), 3, minimum=1, maximum=14)

    communities = Community.query.all()
    diseases = _disease_options()
    start_date, end_date, auto_range = _resolve_case_date_range(community_filter, disease_filter, 90)
    visits_by_date, filtered_record_count = _daily_visits_in_stratum(
        _visit_query(start_date, end_date, community_filter, disease_filter), stratum
    )

    lag_start_date = start_date - timedelta(days=max_lag)
    weather_records, weather_location, used_fallback = _load_weather_records(
        lag_start_date, end_date, community_filter
    )
    default_city = _default_city()
    weather_source = _weather_source_label(weather_location, default_city)
    weather_by_date = build_daily_weather(weather_records)

    analysis_days = date_span(start_date, end_date)

    baseline_bucket = {}
    total_visits = 0
    for day in analysis_days:
        visits = visits_by_date.get(day, 0)
        total_visits += visits
        key = (day.month, day.weekday())
        stats = baseline_bucket.setdefault(key, {'visits': 0, 'days': 0})
        stats['visits'] += visits
        stats['days'] += 1

    total_days = len(analysis_days)
    overall_baseline = (total_visits / total_days) if total_days else 0.0
    baseline_rate = {}
    for key, stats in baseline_bucket.items():
        baseline_rate[key] = (stats['visits'] / stats['days']) if stats['days'] else overall_baseline

    all_temps = sorted(
        row['temperature']
        for day, row in weather_by_date.items()
        if lag_start_date <= day <= end_date and row.get('temperature') is not None
    )
    heat_threshold = percentile(all_temps, 0.9) if all_temps else None
    cold_threshold = percentile(all_temps, 0.1) if all_temps else None

    lag_axis = list(range(0, max_lag + 1))
    lag_results = []
    for lag in lag_axis:
        x_vals = []
        y_vals = []
        heat_obs = 0
        heat_exp = 0.0
        heat_days = 0
        cold_obs = 0
        cold_exp = 0.0
        cold_days = 0

        for day in analysis_days:
            exposure_day = day - timedelta(days=lag)
            weather = weather_by_date.get(exposure_day)
            temp = weather.get('temperature') if weather else None
            if temp is None:
                continue

            visits = visits_by_date.get(day, 0)
            baseline = baseline_rate.get((day.month, day.weekday()), overall_baseline)
            x_vals.append(temp)
            y_vals.append(visits)

            if heat_threshold is not None and temp >= heat_threshold:
                heat_obs += visits
                heat_exp += baseline
                heat_days += 1
            if cold_threshold is not None and temp <= cold_threshold:
                cold_obs += visits
                cold_exp += baseline
                cold_days += 1

        corr, corr_low, corr_high = corr_with_ci(x_vals, y_vals)
        heat_rr, heat_ci_low, heat_ci_high = rr_with_ci(heat_obs, heat_exp) if heat_days else (None, None, None)
        cold_rr, cold_ci_low, cold_ci_high = rr_with_ci(cold_obs, cold_exp) if cold_days else (None, None, None)
        heat_sig = is_significant(heat_rr, heat_days, min_days, heat_ci_low, heat_ci_high)
        cold_sig = is_significant(cold_rr, cold_days, min_days, cold_ci_low, cold_ci_high)

        lag_results.append({
            'lag': lag,
            'n': len(x_vals),
            'corr': corr,
            'corr_low': corr_low,
            'corr_high': corr_high,
            'heat_rr': heat_rr,
            'heat_ci_low': heat_ci_low,
            'heat_ci_high': heat_ci_high,
            'heat_days': heat_days,
            'heat_significant': heat_sig,
            'cold_rr': cold_rr,
            'cold_ci_low': cold_ci_low,
            'cold_ci_high': cold_ci_high,
            'cold_days': cold_days,
            'cold_significant': cold_sig
        })

    cumulative_windows = [w for w in [0, 3, 7, 14, 21] if w <= max_lag]
    cumulative_results = []
    for window in cumulative_windows:
        day_samples = []
        exposure_values = []
        for day in analysis_days:
            temp_list = []
            ok = True
            for offset in range(window + 1):
                weather = weather_by_date.get(day - timedelta(days=offset))
                temp = weather.get('temperature') if weather else None
                if temp is None:
                    ok = False
                    break
                temp_list.append(temp)
            if not ok:
                continue

            avg_temp = sum(temp_list) / len(temp_list)
            visits = visits_by_date.get(day, 0)
            baseline = baseline_rate.get((day.month, day.weekday()), overall_baseline)
            day_samples.append((avg_temp, visits, baseline))
            exposure_values.append(avg_temp)

        if not day_samples:
            cumulative_results.append({
                'window': window,
                'sample_days': 0,
                'heat_rr': None,
                'heat_ci_low': None,
                'heat_ci_high': None,
                'cold_rr': None,
                'cold_ci_low': None,
                'cold_ci_high': None
            })
            continue

        sorted_values = sorted(exposure_values)
        w_heat_thr = percentile(sorted_values, 0.9)
        w_cold_thr = percentile(sorted_values, 0.1)

        heat_obs = 0
        heat_exp = 0.0
        heat_days = 0
        cold_obs = 0
        cold_exp = 0.0
        cold_days = 0
        for avg_temp, visits, baseline in day_samples:
            if w_heat_thr is not None and avg_temp >= w_heat_thr:
                heat_obs += visits
                heat_exp += baseline
                heat_days += 1
            if w_cold_thr is not None and avg_temp <= w_cold_thr:
                cold_obs += visits
                cold_exp += baseline
                cold_days += 1

        heat_rr, heat_ci_low, heat_ci_high = rr_with_ci(heat_obs, heat_exp) if heat_days else (None, None, None)
        cold_rr, cold_ci_low, cold_ci_high = rr_with_ci(cold_obs, cold_exp) if cold_days else (None, None, None)

        cumulative_results.append({
            'window': window,
            'sample_days': len(day_samples),
            'heat_rr': heat_rr,
            'heat_ci_low': heat_ci_low,
            'heat_ci_high': heat_ci_high,
            'cold_rr': cold_rr,
            'cold_ci_low': cold_ci_low,
            'cold_ci_high': cold_ci_high
        })

    temp_bins = [-30, -10, 0, 10, 20, 30, 40, 55]
    temp_labels = [format_bucket_label(temp_bins[i], temp_bins[i + 1], '°C') for i in range(len(temp_bins) - 1)]
    matrix_raw = [
        [{'visits': 0, 'days': 0, 'expected': 0.0} for _ in lag_axis]
        for _ in range(len(temp_bins) - 1)
    ]

    for lag_idx, lag in enumerate(lag_axis):
        for day in analysis_days:
            weather = weather_by_date.get(day - timedelta(days=lag))
            temp = weather.get('temperature') if weather else None
            if temp is None:
                continue
            bin_idx = find_bin(temp, temp_bins)
            if bin_idx is None:
                continue
            baseline = baseline_rate.get((day.month, day.weekday()), overall_baseline)
            cell = matrix_raw[bin_idx][lag_idx]
            cell['visits'] += visits_by_date.get(day, 0)
            cell['days'] += 1
            cell['expected'] += baseline

    lag_heatmap = []
    max_heatmap_rr = 0.0
    for bin_idx, row in enumerate(matrix_raw):
        cells = []
        for lag_idx, raw_cell in enumerate(row):
            visits = raw_cell['visits']
            days = raw_cell['days']
            rr, ci_low, ci_high = rr_with_ci(visits, raw_cell['expected']) if days > 0 else (None, None, None)
            significant = is_significant(rr, days, min_days, ci_low, ci_high)
            certainty = certainty_level(days, visits, ci_low, ci_high, min_days)
            action = action_level(rr, significant, certainty, days, min_days)
            max_heatmap_rr = max(max_heatmap_rr, rr or 0)
            cells.append({
                'lag': lag_axis[lag_idx],
                'temp_label': temp_labels[bin_idx],
                'days': days,
                'visits': visits,
                'rr': rr,
                'ci_low': ci_low,
                'ci_high': ci_high,
                'significant': significant,
                'certainty': certainty,
                'action': action,
                'bg_color': heatmap_cell_color(rr, days, min_days)
            })
        lag_heatmap.append(cells)

    heat_candidates = [item for item in lag_results if item['heat_rr'] is not None and item['heat_days'] >= min_days]
    cold_candidates = [item for item in lag_results if item['cold_rr'] is not None and item['cold_days'] >= min_days]
    peak_heat = max(heat_candidates, key=lambda item: item['heat_rr']) if heat_candidates else None
    peak_cold = max(cold_candidates, key=lambda item: item['cold_rr']) if cold_candidates else None

    peak_type = None
    peak_lag = None
    peak_rr = None
    peak_significant = False
    peak_days = 0
    if peak_heat and (not peak_cold or (peak_heat['heat_rr'] or 0) >= (peak_cold['cold_rr'] or 0)):
        peak_type = '热暴露'
        peak_lag = peak_heat['lag']
        peak_rr = peak_heat['heat_rr']
        peak_significant = peak_heat['heat_significant']
        peak_days = peak_heat['heat_days']
    elif peak_cold:
        peak_type = '冷暴露'
        peak_lag = peak_cold['lag']
        peak_rr = peak_cold['cold_rr']
        peak_significant = peak_cold['cold_significant']
        peak_days = peak_cold['cold_days']

    if peak_rr is None:
        severity = 'Minor'
    elif peak_rr >= 2.0:
        severity = 'Extreme'
    elif peak_rr >= 1.6:
        severity = 'Severe'
    elif peak_rr >= 1.3:
        severity = 'Moderate'
    else:
        severity = 'Minor'

    if peak_rr is None:
        certainty = 'Possible'
    elif peak_significant and peak_lag == 0:
        certainty = 'Observed'
    elif peak_significant:
        certainty = 'Likely'
    else:
        certainty = 'Possible'

    if peak_lag is None:
        urgency = 'Future'
    elif peak_lag <= 2:
        urgency = 'Immediate'
    elif peak_lag <= 5:
        urgency = 'Expected'
    else:
        urgency = 'Future'

    if severity in {'Severe', 'Extreme'} and urgency in {'Immediate', 'Expected'}:
        action_text = '立即行动'
    elif severity in {'Moderate', 'Severe'}:
        action_text = '准备干预'
    else:
        action_text = '持续观察'

    action_semantics = {
        'severity': severity,
        'certainty': certainty,
        'urgency': urgency,
        'action': action_text,
        'peak_type': peak_type,
        'peak_lag': peak_lag,
        'peak_rr': peak_rr
    }

    visit_days = len(visits_by_date)
    weather_days = len([
        day for day in analysis_days
        if weather_by_date.get(day) and weather_by_date.get(day).get('temperature') is not None
    ])
    overlap_days = len(set(visits_by_date.keys()) & set(
        day for day in analysis_days
        if weather_by_date.get(day) and weather_by_date.get(day).get('temperature') is not None
    ))

    data_notes = _case_data_notes(auto_range, community_filter, used_fallback, weather_source,
                                  total_visits, weather_days, overlap_days, total_days)
    if heat_threshold is not None and cold_threshold is not None:
        data_notes.append(f"温度分位阈值：冷暴露≤{cold_threshold:.1f}°C，热暴露≥{heat_threshold:.1f}°C")
    if filtered_record_count == 0:
        data_notes.append("当前分层条件下无病例记录")

    data_summary = {
        'total_days': total_days,
        'visit_days': visit_days,
        'total_visits': total_visits,
        'weather_days': weather_days,
        'overlap_days': overlap_days,
        'weather_source': weather_source,
        'filtered_record_count': filtered_record_count,
        'overall_baseline': overall_baseline,
        'max_lag': max_lag,
        'max_heatmap_rr': max_heatmap_rr,
        'peak_type': peak_type,
        'peak_lag': peak_lag,
        'peak_rr': peak_rr
    }

    return render_template(
        'analysis_lag.html',
        communities=communities,
        diseases=diseases,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        community_filter=community_filter,
        disease_filter=disease_filter,
        stratum=stratum,
        stratum_labels=STRATUM_LABELS,
        max_lag=max_lag,
        min_days=min_days,
        max_lag_options=[7, 14, 21],
        min_days_options=[1, 2, 3, 5, 7],
        lag_axis=lag_axis,
        temp_labels=temp_labels,
        lag_results=lag_results,
        lag_heatmap=lag_heatmap,
        cumulative_results=cumulative_results,
        action_semantics=action_semantics,
        data_summary=data_summary,
        data_notes=data_notes
    )


@admin_route('/analysis/community-compare', methods=['GET', 'POST'], endpoint='analysis_community_compare')
def analysis_community_compare():
    """社区对比分析（SIR + 漏斗图 + 不平等指标）"""
    disease_filter = _text_arg('disease', 100)
    stratum = _stratum_arg()

    min_days = safe_int(request.values.get('min_days'), 3, minimum=1, maximum=14)
    smoothing_alpha = safe_int(request.values.get('smoothing_alpha'), 5, minimum=0, maximum=30)
    top_n = safe_int(request.values.get('top_n'), 12, minimum=5, maximum=25)

    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    auto_range = False
    date_swapped = False

    if not start_raw and not end_raw:
        last_visit = _latest_visit_date(None, disease_filter)
        if last_visit:
            end_date = last_visit
            start_date = end_date - timedelta(days=90)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=90)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        date_swapped = True

    communities = Community.query.order_by(Community.name.asc()).all()
    community_map = {item.name: item for item in communities}
    diseases = _disease_options()

    records = _visit_query(start_date, end_date, disease_filter=disease_filter).with_entities(
        MedicalRecord.community,
        MedicalRecord.visit_time,
        MedicalRecord.age,
        MedicalRecord.gender
    ).all()

    unknown_community_label = '未标注社区'
    visits_by_community = {}
    visit_days_by_community = {}
    filtered_record_count = 0
    for row in records:
        if not record_matches_stratum(row.age, row.gender, stratum):
            continue
        community_name = (row.community or '').strip() or unknown_community_label
        day = utc_to_local_date(row.visit_time)
        visits_by_community[community_name] = visits_by_community.get(community_name, 0) + 1
        visit_days_by_community.setdefault(community_name, set()).add(day)
        filtered_record_count += 1

    community_names = sorted(set(community_map.keys()) | set(visits_by_community.keys()))
    total_days = max(1, (end_date - start_date).days + 1)

    population_known_count = 0
    total_population = 0
    total_person_days = 0.0
    total_observed_for_baseline = 0
    for name in community_names:
        meta = community_map.get(name)
        population = int(meta.population) if meta and meta.population and meta.population > 0 else 0
        if population <= 0:
            continue
        population_known_count += 1
        total_population += population
        person_days = population * total_days
        total_person_days += person_days
        total_observed_for_baseline += visits_by_community.get(name, 0)

    baseline_rate = (total_observed_for_baseline / total_person_days) if total_person_days > 0 else None

    stats = []
    funnel_outlier_95 = 0
    funnel_outlier_998 = 0
    for name in community_names:
        meta = community_map.get(name)
        observed = visits_by_community.get(name, 0)
        visit_days = len(visit_days_by_community.get(name, set()))
        population = int(meta.population) if meta and meta.population and meta.population > 0 else None
        person_days = (population * total_days) if population else None
        expected = (baseline_rate * person_days) if (baseline_rate is not None and person_days) else None
        sir, ci_low, ci_high = rr_with_ci(observed, expected) if expected else (None, None, None)
        smoothed_sir = None
        if expected:
            if smoothing_alpha > 0:
                smoothed_sir = (observed + smoothing_alpha) / (expected + smoothing_alpha)
            else:
                smoothed_sir = sir
        signal_rr = smoothed_sir if smoothed_sir is not None else sir
        significant = is_significant(signal_rr, visit_days, min_days, ci_low, ci_high)
        certainty = certainty_level(visit_days, observed, ci_low, ci_high, min_days)
        action = action_level(signal_rr, significant, certainty, visit_days, min_days)
        incidence_rate = ((observed / person_days) * 10000) if person_days else None
        excess_cases = (observed - expected) if expected is not None else None
        excess_rate = ((excess_cases / person_days) * 10000) if (person_days and excess_cases is not None) else None

        funnel_flag = 'insufficient'
        funnel_low_95 = None
        funnel_high_95 = None
        funnel_low_998 = None
        funnel_high_998 = None
        if expected and expected > 0 and signal_rr is not None:
            root_e = math.sqrt(expected)
            funnel_low_95 = math.exp(-1.96 / root_e)
            funnel_high_95 = math.exp(1.96 / root_e)
            funnel_low_998 = math.exp(-3.0 / root_e)
            funnel_high_998 = math.exp(3.0 / root_e)
            if signal_rr < funnel_low_998 or signal_rr > funnel_high_998:
                funnel_flag = 'outside_998'
                funnel_outlier_998 += 1
            elif signal_rr < funnel_low_95 or signal_rr > funnel_high_95:
                funnel_flag = 'outside_95'
                funnel_outlier_95 += 1
            else:
                funnel_flag = 'inside'

        stats.append({
            'name': name,
            'observed': observed,
            'visit_days': visit_days,
            'population': population,
            'person_days': person_days,
            'expected': expected,
            'sir': sir,
            'ci_low': ci_low,
            'ci_high': ci_high,
            'smoothed_sir': smoothed_sir,
            'incidence_rate': incidence_rate,
            'excess_cases': excess_cases,
            'excess_rate': excess_rate,
            'significant': significant,
            'certainty': certainty,
            'action': action,
            'funnel_flag': funnel_flag,
            'funnel_low_95': funnel_low_95,
            'funnel_high_95': funnel_high_95,
            'funnel_low_998': funnel_low_998,
            'funnel_high_998': funnel_high_998,
            'risk_level': meta.risk_level if meta and meta.risk_level else '未知',
            'vulnerability_index': meta.vulnerability_index if meta and meta.vulnerability_index is not None else None
        })

    stats = sorted(
        stats,
        key=lambda item: (
            item['smoothed_sir'] if item['smoothed_sir'] is not None else (
                item['sir'] if item['sir'] is not None else -1
            ),
            item['observed'],
            item['visit_days']
        ),
        reverse=True
    )
    for index, row in enumerate(stats, start=1):
        row['rank'] = index

    valid_rates = sorted(
        row['incidence_rate'] for row in stats
        if row['incidence_rate'] is not None and row['population']
    )
    risk_values = sorted(
        row['smoothed_sir'] if row['smoothed_sir'] is not None else row['sir']
        for row in stats
        if (row['smoothed_sir'] is not None or row['sir'] is not None)
    )
    p90_rate = percentile(valid_rates, 0.9) if valid_rates else None
    p10_rate = percentile(valid_rates, 0.1) if valid_rates else None
    p90_p10_ratio = (p90_rate / p10_rate) if (p90_rate is not None and p10_rate and p10_rate > 0) else None
    gini_rate = gini(valid_rates)
    max_risk = max(risk_values) if risk_values else None
    min_risk = min(risk_values) if risk_values else None
    risk_gap_ratio = (max_risk / min_risk) if (max_risk is not None and min_risk and min_risk > 0) else None

    chart_rows = [
        row for row in stats
        if row['smoothed_sir'] is not None or row['sir'] is not None
    ][:top_n]
    if not chart_rows:
        chart_rows = stats[:top_n]

    ranking_payload = {
        'labels': [row['name'] for row in chart_rows],
        'values': [
            row['smoothed_sir'] if row['smoothed_sir'] is not None else row['sir']
            for row in chart_rows
        ],
        'ci_low': [row['ci_low'] for row in chart_rows],
        'ci_high': [row['ci_high'] for row in chart_rows],
        'observed': [row['observed'] for row in chart_rows],
        'expected': [row['expected'] for row in chart_rows],
        'incidence_rate': [row['incidence_rate'] for row in chart_rows]
    }

    funnel_points = []
    max_expected = 0.0
    max_funnel_y = 1.0
    for row in stats:
        signal_rr = row['smoothed_sir'] if row['smoothed_sir'] is not None else row['sir']
        expected = row['expected']
        if expected is None or expected <= 0 or signal_rr is None:
            continue
        max_expected = max(max_expected, expected)
        max_funnel_y = max(max_funnel_y, signal_rr)
        funnel_points.append({
            'x': expected,
            'y': signal_rr,
            'name': row['name'],
            'observed': row['observed'],
            'expected': expected,
            'sir': row['sir'],
            'smoothed_sir': row['smoothed_sir'],
            'funnel_flag': row['funnel_flag']
        })

    if max_expected <= 0:
        max_expected = 1.0
    funnel_upper_95 = []
    funnel_lower_95 = []
    funnel_upper_998 = []
    funnel_lower_998 = []
    funnel_center = []
    for idx in range(1, 41):
        x_val = max(0.1, max_expected * idx / 40)
        root_e = math.sqrt(x_val)
        upper_95 = math.exp(1.96 / root_e)
        lower_95 = math.exp(-1.96 / root_e)
        upper_998 = math.exp(3.0 / root_e)
        lower_998 = math.exp(-3.0 / root_e)
        funnel_upper_95.append({'x': x_val, 'y': upper_95})
        funnel_lower_95.append({'x': x_val, 'y': lower_95})
        funnel_upper_998.append({'x': x_val, 'y': upper_998})
        funnel_lower_998.append({'x': x_val, 'y': lower_998})
        funnel_center.append({'x': x_val, 'y': 1.0})
        max_funnel_y = max(max_funnel_y, upper_998)

    ranked_rows = [
        row for row in stats
        if row['smoothed_sir'] is not None or row['sir'] is not None
    ]
    top_risk_communities = ranked_rows[:5]
    low_risk_communities = sorted(
        ranked_rows,
        key=lambda row: row['smoothed_sir'] if row['smoothed_sir'] is not None else row['sir']
    )[:5]

    total_visits = sum(row['observed'] for row in stats)
    data_notes = []
    if auto_range:
        data_notes.append("已自动定位到最近有数据的时间区间")
    if date_swapped:
        data_notes.append("开始日期晚于结束日期，系统已自动交换")
    if not communities and not visits_by_community:
        data_notes.append("暂无社区数据")
    if total_visits == 0:
        data_notes.append("当前筛选条件下无门诊记录")
    if filtered_record_count == 0 and stratum != 'all':
        data_notes.append("当前人群分层无有效样本")
    if baseline_rate is None:
        data_notes.append("缺少可用人口分母，无法计算标准化风险（SIR）")
    if stats and population_known_count < len(stats):
        missing_count = len(stats) - population_known_count
        data_notes.append(f"{missing_count} 个社区缺少人口，相关指标会显示为 --")

    funnel_flag_labels = {
        'outside_998': '超出99.8%控制限',
        'outside_95': '超出95%控制限',
        'inside': '控制限内',
        'insufficient': '样本不足'
    }

    return render_template(
        'analysis_community_compare.html',
        communities=communities,
        diseases=diseases,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        disease_filter=disease_filter,
        stratum=stratum,
        stratum_labels=STRATUM_LABELS,
        min_days=min_days,
        smoothing_alpha=smoothing_alpha,
        top_n=top_n,
        min_days_options=[1, 2, 3, 5, 7],
        smoothing_alpha_options=[0, 3, 5, 8, 12, 20],
        top_n_options=[8, 12, 15, 20, 25],
        stats=stats,
        top_risk_communities=top_risk_communities,
        low_risk_communities=low_risk_communities,
        ranking_payload=ranking_payload,
        funnel_points=funnel_points,
        funnel_upper_95=funnel_upper_95,
        funnel_lower_95=funnel_lower_95,
        funnel_upper_998=funnel_upper_998,
        funnel_lower_998=funnel_lower_998,
        funnel_center=funnel_center,
        funnel_y_max=max_funnel_y * 1.08,
        funnel_flag_labels=funnel_flag_labels,
        data_notes=data_notes,
        data_summary={
            'total_days': total_days,
            'total_visits': total_visits,
            'community_count': len(stats),
            'population_covered_count': population_known_count,
            'total_population': total_population,
            'baseline_rate_per_10k_pd': (baseline_rate * 10000) if baseline_rate is not None else None,
            'outlier_95': funnel_outlier_95 + funnel_outlier_998,
            'outlier_998': funnel_outlier_998,
            'p90_p10_ratio': p90_p10_ratio,
            'gini_rate': gini_rate,
            'risk_gap_ratio': risk_gap_ratio
        }
    )


@admin_route('/alerts/history', methods=['GET', 'POST'], endpoint='alerts_history')
def alerts_history():
    """预警历史记录（预警-实况核验）"""
    filters = _alert_filters()
    location_filter = filters['location']
    alert_type_filter = filters['alert_type']
    alert_level_filter = filters['alert_level']
    follow_days = filters['follow_days']
    min_days = filters['min_days']
    threshold_q = filters['threshold_q']
    outcome_filter = _text_arg('outcome', 20) or 'all'
    if outcome_filter not in {'all', 'hit', 'false_alarm', 'insufficient'}:
        outcome_filter = 'all'

    coverage = _alert_record_coverage()
    start_date, end_date, auto_range, date_swapped = _resolve_alert_date_range(60, 30)
    alert_type_options, alert_level_options, alert_rows = _filtered_alerts(start_date, end_date, filters)

    record_start = start_date - timedelta(days=follow_days)
    record_end = end_date + timedelta(days=follow_days)
    records, daily_visits, global_daily_visits = _daily_visits_by_location(record_start, record_end)
    profile = _threshold_profile(
        daily_visits, global_daily_visits, date_span(record_start, record_end), min_days, threshold_q
    )

    timeline_rows = []
    pre_rows = []
    for alert in alert_rows:
        alert_day = utc_to_local_date(alert.alert_date)
        location_key = (alert.location or '').strip() or '未知'
        location_day_map, threshold, observed_days, eval_key, eval_label, using_global = _alert_baseline(
            location_key, daily_visits, global_daily_visits, profile
        )
        threshold_evaluable = bool(
            threshold is not None and
            threshold > 0 and
            observed_days >= min_days
        )
        window = _alert_window(alert_day, location_day_map, follow_days, threshold, threshold_evaluable)
        outcome = 'insufficient'
        if threshold_evaluable:
            outcome = 'hit' if window[3] is not None else 'false_alarm'

        severity, certainty, urgency = alert_cap_semantics(
            alert.alert_level, alert.alert_type, alert.description
        )
        affected_communities = json_loads_safe(alert.affected_communities, [])
        if not isinstance(affected_communities, list):
            affected_communities = []
        disease_corr = json_loads_safe(alert.disease_correlation, {})
        if not isinstance(disease_corr, dict):
            disease_corr = {}

        row = _alert_row(alert, alert_day, location_key, threshold, threshold_evaluable, eval_label,
                         window, outcome, (severity, certainty, urgency))
        row.update({
            'alert_time': alert.alert_date,
            'action_text': action_from_alert_semantics(severity, certainty, urgency),
            'impact_bucket': impact_bucket_from_severity(severity),
            'likelihood_bucket': likelihood_bucket_from_certainty(certainty),
            'affected_communities_count': len(affected_communities),
            'disease_corr': disease_corr,
            'eval_key': eval_key,
            'using_global': using_global
        })
        pre_rows.append(row)
        timeline_rows.append({
            'date': alert_day.strftime('%Y-%m-%d') if alert_day else '--',
            'time': row['alert_time_text'],
            'kind': 'alert',
            'kind_label': '预警发布',
            'location': location_key,
            'title': f"{row['alert_type']} {row['alert_level']}".strip(),
            'detail': row['description']
        })

    if outcome_filter == 'all':
        rows = pre_rows
    else:
        rows = [row for row in pre_rows if row['outcome'] == outcome_filter]

    outcome_counts = {'hit': 0, 'false_alarm': 0, 'insufficient': 0}
    for row in rows:
        outcome_counts[row['outcome']] = outcome_counts.get(row['outcome'], 0) + 1

    evaluable_rows = [row for row in rows if row['threshold_evaluable']]
    total_alerts = len(rows)
    evaluable_count = len(evaluable_rows)
    hit_count = sum(1 for row in evaluable_rows if row['outcome'] == 'hit')
    false_alarm_count = sum(1 for row in evaluable_rows if row['outcome'] == 'false_alarm')
    hit_rate = (hit_count / evaluable_count) * 100 if evaluable_count else None
    far = (false_alarm_count / evaluable_count) * 100 if evaluable_count else None
    lead_hours_values = [row['lead_hours'] for row in evaluable_rows if row['lead_hours'] is not None]
    avg_lead_hours = (sum(lead_hours_values) / len(lead_hours_values)) if lead_hours_values else None

    key_set = sorted({row['eval_key'] for row in rows})
    alert_days_by_key = {}
    for row in rows:
        if row['alert_day'] is None:
            continue
        alert_days_by_key.setdefault(row['eval_key'], set()).add(row['alert_day'])

    events_by_day = {}
    event_rows = []
    matched_events = 0
    total_events = 0
    for key in key_set:
        day_map, threshold, key_label = _baseline_for_key(key, daily_visits, global_daily_visits, profile)
        if threshold is None or threshold <= 0:
            continue

        alert_days = sorted(alert_days_by_key.get(key, set()))
        cursor = start_date
        while cursor <= end_date:
            visits = day_map.get(cursor, 0)
            if visits >= threshold:
                total_events += 1
                matched = False
                for alert_day in alert_days:
                    delta_days = (cursor - alert_day).days
                    if 0 <= delta_days <= follow_days:
                        matched = True
                        break
                if matched:
                    matched_events += 1
                events_by_day[cursor] = events_by_day.get(cursor, 0) + 1
                event_rows.append({
                    'date': cursor,
                    'date_text': cursor.strftime('%Y-%m-%d'),
                    'location': key_label,
                    'visits': visits,
                    'threshold': threshold,
                    'matched': matched
                })
                timeline_rows.append({
                    'date': cursor.strftime('%Y-%m-%d'),
                    'time': f"{cursor.strftime('%Y-%m-%d')} 23:59",
                    'kind': 'observed',
                    'kind_label': '实况超阈值',
                    'location': key_label,
                    'title': f"病例 {visits} ≥ 阈值 {threshold:.2f}",
                    'detail': '与预警窗口匹配' if matched else '未在预警窗口内'
                })
            cursor += timedelta(days=1)

    miss_count = max(0, total_events - matched_events)
    pod = (matched_events / total_events) * 100 if total_events else None

    trend_labels = []
    trend_alerts = []
    trend_hits = []
    trend_events = []
    alerts_by_day = {}
    hits_by_day = {}
    for row in rows:
        day = row['alert_day']
        if day is None:
            continue
        alerts_by_day[day] = alerts_by_day.get(day, 0) + 1
        if row['outcome'] == 'hit':
            hits_by_day[day] = hits_by_day.get(day, 0) + 1

    cursor = start_date
    while cursor <= end_date:
        trend_labels.append(cursor.strftime('%m-%d'))
        trend_alerts.append(alerts_by_day.get(cursor, 0))
        trend_hits.append(hits_by_day.get(cursor, 0))
        trend_events.append(events_by_day.get(cursor, 0))
        cursor += timedelta(days=1)

    matrix_counts = {
        'high': {'high': 0, 'medium': 0, 'low': 0},
        'medium': {'high': 0, 'medium': 0, 'low': 0},
        'low': {'high': 0, 'medium': 0, 'low': 0}
    }
    for row in rows:
        impact_bucket = row['impact_bucket']
        likelihood_bucket = row['likelihood_bucket']
        matrix_counts[impact_bucket][likelihood_bucket] += 1

    timeline_rows = sorted(
        timeline_rows,
        key=lambda item: item['time'],
        reverse=True
    )[:120]

    event_rows = sorted(event_rows, key=lambda item: item['date'], reverse=True)[:40]
    row_detail_payload = {}
    for row in rows:
        row_detail_payload[str(row['id'])] = {
            'id': row['id'],
            'time': row['alert_time_text'],
            'location': row['location'],
            'alert_type': row['alert_type'],
            'alert_level': row['alert_level'],
            'description': row['description'],
            'outcome_label': row['outcome_label'],
            'threshold': row['threshold'],
            'threshold_source': row['threshold_source'],
            'peak_visits': row['peak_visits'],
            'peak_day_text': row['peak_day_text'],
            'first_hit_day_text': row['first_hit_day_text'],
            'lead_hours': row['lead_hours'],
            'observed_ratio': row['observed_ratio'],
            'severity': row['severity'],
            'severity_label': row['severity_label'],
            'certainty': row['certainty'],
            'certainty_label': row['certainty_label'],
            'urgency': row['urgency'],
            'urgency_label': row['urgency_label'],
            'action_text': row['action_text'],
            'window_points': row['window_points']
        }
    top_risk_alerts = sorted(
        evaluable_rows,
        key=lambda row: (
            row['observed_ratio'] if row['observed_ratio'] is not None else 0,
            row['peak_visits'],
            row['lead_days'] if row['lead_days'] is not None else -1
        ),
        reverse=True
    )[:5]

    data_notes = []
    if auto_range:
        data_notes.append("已自动定位到最近有预警记录的时间区间")
    if date_swapped:
        data_notes.append("开始日期晚于结束日期，系统已自动交换")
    if total_alerts == 0:
        data_notes.append("当前筛选条件下无预警记录")
    if records and not evaluable_count:
        data_notes.append("病例样本不足，当前预警均无法核验")
    if not records:
        data_notes.append("当前窗口无门诊记录，无法进行实况核验")
    global_fallback_count = sum(1 for row in rows if row['using_global'])
    if global_fallback_count > 0:
        data_notes.append(f"{global_fallback_count} 条预警无对应社区病例，使用全局基线核验")
    if _coverage_note(coverage):
        data_notes.append(_coverage_note(coverage))

    return render_template(
        'alerts_history.html',
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        location_filter=location_filter,
        alert_type_filter=alert_type_filter,
        alert_level_filter=alert_level_filter,
        outcome_filter=outcome_filter,
        follow_days=follow_days,
        min_days=min_days,
        threshold_q=threshold_q,
        threshold_q_options=THRESHOLD_Q_OPTIONS,
        follow_days_options=[1, 2, 3, 5, 7],
        min_days_options=[3, 5, 7, 14, 21],
        alert_type_options=alert_type_options,
        alert_level_options=alert_level_options,
        rows=rows,
        row_detail_payload=row_detail_payload,
        top_risk_alerts=top_risk_alerts,
        timeline_rows=timeline_rows,
        event_rows=event_rows,
        matrix_counts=matrix_counts,
        data_notes=data_notes,
        data_summary={
            'total_alerts': total_alerts,
            'evaluable_count': evaluable_count,
            'hit_count': hit_count,
            'false_alarm_count': false_alarm_count,
            'hit_rate': hit_rate,
            'far': far,
            'avg_lead_hours': avg_lead_hours,
            'total_events': total_events,
            'matched_events': matched_events,
            'miss_count': miss_count,
            'pod': pod,
            'ground_truth_overlap': coverage['has_overlap'],
            'overlap_start': coverage['overlap_start'],
            'overlap_end': coverage['overlap_end']
        },
        chart_payload={
            'outcome': {
                'labels': ['命中', '空报', '样本不足'],
                'values': [
                    outcome_counts['hit'],
                    outcome_counts['false_alarm'],
                    outcome_counts['insufficient']
                ]
            },
            'trend': {
                'labels': trend_labels,
                'alerts': trend_alerts,
                'hits': trend_hits,
                'events': trend_events
            }
        }
    )


@admin_route('/alerts/accuracy', methods=['GET', 'POST'], endpoint='alerts_accuracy')
def alerts_accuracy():
    """预警准确率统计（分类核验 + 可靠性 + 阈值敏感性）"""
    filters = _alert_filters()
    follow_days = filters['follow_days']
    min_days = filters['min_days']
    threshold_q = filters['threshold_q']

    coverage = _alert_record_coverage()
    start_date, end_date, auto_range, date_swapped = _resolve_alert_date_range(90, 90)
    alert_type_options, alert_level_options, alert_rows = _filtered_alerts(start_date, end_date, filters)

    record_start = start_date - timedelta(days=follow_days)
    record_end = end_date + timedelta(days=follow_days)
    records, daily_visits, global_daily_visits = _daily_visits_by_location(record_start, record_end)
    all_days = date_span(record_start, record_end)
    threshold_profiles = {
        q_value: _threshold_profile(daily_visits, global_daily_visits, all_days, min_days, q_value)
        for q_value in THRESHOLD_Q_OPTIONS
    }

    lead_bucket_labels = ['当天', '+24h', '+48h', '+72h', '>72h']

    def lead_bucket_for_hours(lead_hours):
        if lead_hours is None:
            return None
        if lead_hours <= 0:
            return '当天'
        if lead_hours <= 24:
            return '+24h'
        if lead_hours <= 48:
            return '+48h'
        if lead_hours <= 72:
            return '+72h'
        return '>72h'

    def evaluate_quantile(q_value, include_rows=False):
        profile = threshold_profiles[q_value]

        total_alerts = len(alert_rows)
        evaluable_alerts = 0
        hit_alerts = 0
        false_alarm_alerts = 0
        insufficient_alerts = 0
        global_fallback_count = 0
        lead_hours_values = []
        lead_bucket_counts = {label: 0 for label in lead_bucket_labels}

        alert_days_by_key = defaultdict(set)
        alerts_by_day = defaultdict(int)
        hit_alerts_by_day = defaultdict(int)
        certainty_pairs = []
        weekly_calibration_map = {}
        level_groups = {}
        type_groups = {}
        certainty_groups = {}
        rows = []

        for alert in alert_rows:
            alert_day = utc_to_local_date(alert.alert_date)
            location_key = (alert.location or '').strip() or '未知'
            location_day_map, threshold, observed_days, eval_key, threshold_source, using_global = _alert_baseline(
                location_key, daily_visits, global_daily_visits, profile
            )
            threshold_evaluable = bool(
                alert_day is not None and
                threshold is not None and
                threshold > 0 and
                observed_days >= min_days
            )
            window = _alert_window(alert_day, location_day_map, follow_days, threshold, threshold_evaluable)
            first_hit_day = window[3]
            lead_days = (first_hit_day - alert_day).days if (first_hit_day and alert_day) else None
            lead_hours = lead_days * 24 if lead_days is not None else None
            if threshold_evaluable:
                outcome = 'hit' if first_hit_day is not None else 'false_alarm'
            else:
                outcome = 'insufficient'

            severity, certainty, urgency = alert_cap_semantics(
                alert.alert_level, alert.alert_type, alert.description
            )
            probability = certainty_to_probability(certainty)

            if threshold_evaluable:
                evaluable_alerts += 1
                if using_global:
                    global_fallback_count += 1
                if alert_day is not None:
                    alerts_by_day[alert_day] += 1
                    alert_days_by_key[eval_key].add(alert_day)
                if outcome == 'hit':
                    hit_alerts += 1
                    if alert_day is not None:
                        hit_alerts_by_day[alert_day] += 1
                    if lead_hours is not None:
                        lead_hours_values.append(lead_hours)
                        lead_bucket = lead_bucket_for_hours(lead_hours)
                        if lead_bucket:
                            lead_bucket_counts[lead_bucket] += 1
                else:
                    false_alarm_alerts += 1

                certainty_pairs.append({
                    'probability': probability,
                    'observed': 1 if outcome == 'hit' else 0,
                    'certainty': certainty
                })
                if alert_day is not None:
                    iso = alert_day.isocalendar()
                    week_key = f"{iso.year}-W{iso.week:02d}"
                    week_item = weekly_calibration_map.setdefault(week_key, {
                        'year': iso.year,
                        'week': iso.week,
                        'pairs': []
                    })
                    week_item['pairs'].append({
                        'probability': probability,
                        'observed': 1 if outcome == 'hit' else 0
                    })

                for groups, group_key in ((level_groups, alert.alert_level or '--'),
                                          (type_groups, alert.alert_type or '--')):
                    group = groups.setdefault(group_key, {
                        'name': group_key, 'alerts': 0, 'hit': 0, 'false_alarm': 0, 'lead_sum': 0.0, 'lead_n': 0
                    })
                    group['alerts'] += 1
                    if outcome == 'hit':
                        group['hit'] += 1
                        if lead_hours is not None:
                            group['lead_sum'] += lead_hours
                            group['lead_n'] += 1
                    else:
                        group['false_alarm'] += 1

                certainty_group = certainty_groups.setdefault(certainty, {
                    'certainty': certainty,
                    'label': CERTAINTY_LABELS.get(certainty, certainty),
                    'probability': probability,
                    'alerts': 0,
                    'hit': 0,
                    'false_alarm': 0
                })
                certainty_group['alerts'] += 1
                if outcome == 'hit':
                    certainty_group['hit'] += 1
                else:
                    certainty_group['false_alarm'] += 1
            else:
                insufficient_alerts += 1

            if include_rows:
                row = _alert_row(alert, alert_day, location_key, threshold, threshold_evaluable, threshold_source,
                                 window, outcome, (severity, certainty, urgency))
                row.update({'probability': probability, 'using_global': using_global})
                rows.append(row)

        warned_days_by_key = {}
        for key, alert_days in alert_days_by_key.items():
            warned_set = set()
            for alert_day in alert_days:
                for offset in range(follow_days + 1):
                    day = alert_day + timedelta(days=offset)
                    if start_date <= day <= end_date:
                        warned_set.add(day)
            warned_days_by_key[key] = warned_set

        hit_count = 0
        false_alarm_count = 0
        miss_count = 0
        correct_negative_count = 0
        event_rows = []
        miss_rows = []
        events_by_day = defaultdict(int)
        warned_events_by_day = defaultdict(int)

        for key, warned_days in warned_days_by_key.items():
            day_map, threshold, key_label = _baseline_for_key(key, daily_visits, global_daily_visits, profile)
            if threshold is None or threshold <= 0:
                continue

            cursor = start_date
            while cursor <= end_date:
                visits = day_map.get(cursor, 0)
                observed = visits >= threshold
                warned = cursor in warned_days
                if warned and observed:
                    hit_count += 1
                elif warned and not observed:
                    false_alarm_count += 1
                elif (not warned) and observed:
                    miss_count += 1
                else:
                    correct_negative_count += 1

                if observed:
                    events_by_day[cursor] += 1
                    if warned:
                        warned_events_by_day[cursor] += 1
                    item = {
                        'date': cursor,
                        'date_text': cursor.strftime('%Y-%m-%d'),
                        'location': key_label,
                        'visits': visits,
                        'threshold': threshold,
                        'warned': warned,
                        'exceed_ratio': (visits / threshold) if threshold > 0 else None
                    }
                    event_rows.append(item)
                    if not warned:
                        miss_rows.append(item)
                cursor += timedelta(days=1)

        contingency = {
            'hit': hit_count,
            'false_alarm': false_alarm_count,
            'miss': miss_count,
            'correct_negative': correct_negative_count,
            'total': hit_count + false_alarm_count + miss_count + correct_negative_count
        }
        scores = compute_contingency_scores(
            hit_count=hit_count,
            false_alarm_count=false_alarm_count,
            miss_count=miss_count,
            correct_negative_count=correct_negative_count
        )

        alert_hit_rate = safe_ratio(hit_alerts, evaluable_alerts)
        alert_far = safe_ratio(false_alarm_alerts, evaluable_alerts)
        avg_lead_hours = (sum(lead_hours_values) / len(lead_hours_values)) if lead_hours_values else None

        reliability_rows = []
        reliability_specs = [
            (0.00, 0.40, '0.00-0.40'),
            (0.40, 0.60, '0.40-0.60'),
            (0.60, 0.80, '0.60-0.80'),
            (0.80, 1.01, '0.80-1.00')
        ]
        for left, right, label in reliability_specs:
            subset = [item for item in certainty_pairs if left <= item['probability'] < right]
            count = len(subset)
            avg_probability = None
            observed_rate = None
            if count > 0:
                avg_probability = sum(item['probability'] for item in subset) / count
                observed_rate = sum(item['observed'] for item in subset) / count
            reliability_rows.append({
                'label': label,
                'count': count,
                'avg_probability': avg_probability,
                'observed_rate': observed_rate
            })

        brier_score = None
        brier_skill = None
        climatology = None
        sharpness = None
        roc_auc = None
        if certainty_pairs:
            brier_score = sum(
                (item['probability'] - item['observed']) ** 2
                for item in certainty_pairs
            ) / len(certainty_pairs)
            climatology = sum(item['observed'] for item in certainty_pairs) / len(certainty_pairs)
            sharpness = sum(
                (item['probability'] - climatology) ** 2
                for item in certainty_pairs
            ) / len(certainty_pairs)
            brier_reference = sum(
                (climatology - item['observed']) ** 2
                for item in certainty_pairs
            ) / len(certainty_pairs)
            if brier_reference > 0:
                brier_skill = 1 - (brier_score / brier_reference)
            roc_auc = roc_auc_from_pairs(certainty_pairs)

        weekly_calibration_rows = []
        for week_key, week_item in sorted(weekly_calibration_map.items(), key=lambda kv: kv[0]):
            pairs = week_item['pairs']
            sample_count = len(pairs)
            if sample_count <= 0:
                continue
            week_prob_avg = sum(item['probability'] for item in pairs) / sample_count
            week_obs_rate = sum(item['observed'] for item in pairs) / sample_count
            week_brier = sum(
                (item['probability'] - item['observed']) ** 2
                for item in pairs
            ) / sample_count
            week_sharpness = sum(
                (item['probability'] - week_prob_avg) ** 2
                for item in pairs
            ) / sample_count
            week_auc = roc_auc_from_pairs(pairs)
            weekly_calibration_rows.append({
                'week_key': week_key,
                'sample_count': sample_count,
                'avg_probability': week_prob_avg,
                'observed_rate': week_obs_rate,
                'brier_score': week_brier,
                'sharpness': week_sharpness,
                'roc_auc': week_auc
            })

        def group_rows(groups):
            return sorted([
                {
                    'name': group['name'],
                    'alerts': group['alerts'],
                    'hit': group['hit'],
                    'false_alarm': group['false_alarm'],
                    'hit_rate': safe_ratio(group['hit'], group['alerts']),
                    'far': safe_ratio(group['false_alarm'], group['alerts']),
                    'avg_lead_hours': (group['lead_sum'] / group['lead_n']) if group['lead_n'] else None
                }
                for group in groups.values()
            ], key=lambda item: (item['alerts'], item['hit']), reverse=True)

        level_rows = group_rows(level_groups)
        type_rows = group_rows(type_groups)

        certainty_rows = []
        for group in certainty_groups.values():
            certainty_rows.append({
                'certainty': group['certainty'],
                'label': group['label'],
                'probability': group['probability'],
                'alerts': group['alerts'],
                'hit': group['hit'],
                'false_alarm': group['false_alarm'],
                'observed_rate': safe_ratio(group['hit'], group['alerts'])
            })
        certainty_rows = sorted(certainty_rows, key=lambda item: item['probability'], reverse=True)

        top_false_alerts = sorted(
            [row for row in rows if row['outcome'] == 'false_alarm'],
            key=lambda item: (
                item['peak_visits'],
                item['observed_ratio'] if item['observed_ratio'] is not None else 0,
                item['alert_time_text']
            ),
            reverse=True
        )[:8]
        miss_rows = sorted(
            miss_rows,
            key=lambda item: (
                item['exceed_ratio'] if item['exceed_ratio'] is not None else 0,
                item['date']
            ),
            reverse=True
        )[:12]
        event_rows = sorted(event_rows, key=lambda item: item['date'], reverse=True)[:60]

        trend = {
            'labels': [],
            'alerts': [],
            'hits': [],
            'events': [],
            'warned_events': []
        }
        if include_rows:
            cursor = start_date
            while cursor <= end_date:
                trend['labels'].append(cursor.strftime('%m-%d'))
                trend['alerts'].append(alerts_by_day.get(cursor, 0))
                trend['hits'].append(hit_alerts_by_day.get(cursor, 0))
                trend['events'].append(events_by_day.get(cursor, 0))
                trend['warned_events'].append(warned_events_by_day.get(cursor, 0))
                cursor += timedelta(days=1)

        lead_buckets = [
            {'label': label, 'count': lead_bucket_counts[label]}
            for label in lead_bucket_labels
        ]

        return {
            'total_alerts': total_alerts,
            'evaluable_alerts': evaluable_alerts,
            'insufficient_alerts': insufficient_alerts,
            'hit_alerts': hit_alerts,
            'false_alarm_alerts': false_alarm_alerts,
            'alert_hit_rate': alert_hit_rate,
            'alert_far': alert_far,
            'avg_lead_hours': avg_lead_hours,
            'global_fallback_count': global_fallback_count,
            'contingency': contingency,
            'scores': scores,
            'event_total': hit_count + miss_count,
            'event_matched': hit_count,
            'event_miss': miss_count,
            'rows': rows,
            'level_rows': level_rows,
            'type_rows': type_rows,
            'certainty_rows': certainty_rows,
            'lead_buckets': lead_buckets,
            'event_rows': event_rows,
            'miss_rows': miss_rows,
            'top_false_alerts': top_false_alerts,
            'reliability_rows': reliability_rows,
            'reliability_summary': {
                'brier_score': brier_score,
                'brier_skill': brier_skill,
                'climatology': climatology,
                'sample_count': len(certainty_pairs),
                'sharpness': sharpness,
                'roc_auc': roc_auc
            },
            'weekly_calibration_rows': weekly_calibration_rows,
            'trend': trend
        }

    evaluations = {}
    for q_value in THRESHOLD_Q_OPTIONS:
        evaluations[q_value] = evaluate_quantile(
            q_value,
            include_rows=(q_value == threshold_q)
        )
    selected = evaluations[threshold_q]

    def ratio_to_percent(value):
        return (value * 100.0) if value is not None else None

    sensitivity_rows = []
    for q_value in THRESHOLD_Q_OPTIONS:
        result = evaluations[q_value]
        sensitivity_rows.append({
            'quantile': q_value,
            'label': f"P{int(round(q_value * 100))}",
            'evaluable_alerts': result['evaluable_alerts'],
            'alert_hit_rate': result['alert_hit_rate'],
            'pod': result['scores']['pod'],
            'csi': result['scores']['csi'],
            'hss': result['scores']['hss']
        })

    reliability_points = []
    for item in selected['reliability_rows']:
        if item['avg_probability'] is None or item['observed_rate'] is None:
            continue
        reliability_points.append({
            'x': item['avg_probability'] * 100,
            'y': item['observed_rate'] * 100,
            'count': item['count'],
            'label': item['label']
        })

    chart_payload = {
        'reliability': {
            'points': reliability_points
        },
        'sensitivity': {
            'labels': [item['label'] for item in sensitivity_rows],
            'alert_hit_rate': [ratio_to_percent(item['alert_hit_rate']) for item in sensitivity_rows],
            'pod': [ratio_to_percent(item['pod']) for item in sensitivity_rows],
            'csi': [ratio_to_percent(item['csi']) for item in sensitivity_rows]
        },
        'lead': {
            'labels': [item['label'] for item in selected['lead_buckets']],
            'counts': [item['count'] for item in selected['lead_buckets']]
        },
        'trend': selected['trend']
        ,
        'weekly_calibration': {
            'labels': [item['week_key'] for item in selected['weekly_calibration_rows']],
            'brier': [item['brier_score'] for item in selected['weekly_calibration_rows']],
            'sharpness': [item['sharpness'] for item in selected['weekly_calibration_rows']],
            'roc_auc': [item['roc_auc'] for item in selected['weekly_calibration_rows']]
        }
    }

    data_summary = {
        'total_alerts': selected['total_alerts'],
        'evaluable_alerts': selected['evaluable_alerts'],
        'insufficient_alerts': selected['insufficient_alerts'],
        'hit_alerts': selected['hit_alerts'],
        'false_alarm_alerts': selected['false_alarm_alerts'],
        'alert_hit_rate': ratio_to_percent(selected['alert_hit_rate']),
        'alert_far': ratio_to_percent(selected['alert_far']),
        'avg_lead_hours': selected['avg_lead_hours'],
        'event_total': selected['event_total'],
        'event_matched': selected['event_matched'],
        'event_miss': selected['event_miss'],
        'pod': ratio_to_percent(selected['scores']['pod']),
        'far': ratio_to_percent(selected['scores']['far']),
        'csi': ratio_to_percent(selected['scores']['csi']),
        'accuracy': ratio_to_percent(selected['scores']['accuracy']),
        'bias': selected['scores']['bias'],
        'pofd': ratio_to_percent(selected['scores']['pofd']),
        'tss': selected['scores']['tss'],
        'f1': ratio_to_percent(selected['scores']['f1']),
        'ets': selected['scores']['ets'],
        'hss': selected['scores']['hss'],
        'brier_score': selected['reliability_summary']['brier_score'],
        'brier_skill': selected['reliability_summary']['brier_skill'],
        'sharpness': selected['reliability_summary']['sharpness'],
        'roc_auc': selected['reliability_summary']['roc_auc'],
        'climatology': ratio_to_percent(selected['reliability_summary']['climatology']),
        'reliability_n': selected['reliability_summary']['sample_count']
    }

    contingency = selected['contingency']
    data_notes = []
    if auto_range:
        data_notes.append("已自动定位到最近有预警记录的时间区间")
    if date_swapped:
        data_notes.append("开始日期晚于结束日期，系统已自动交换")
    if not alert_rows:
        data_notes.append("当前筛选条件下无预警记录")
    if not records:
        data_notes.append("当前窗口无门诊记录，无法进行实况核验")
    if records and selected['evaluable_alerts'] == 0 and alert_rows:
        data_notes.append("病例样本不足或阈值不可用，当前预警无法进入标准核验")
    if selected['global_fallback_count'] > 0:
        data_notes.append(f"{selected['global_fallback_count']} 条预警无对应社区病例，已回退到全局基线")
    if contingency['total'] == 0 and selected['evaluable_alerts'] > 0:
        data_notes.append("当前评估窗口未形成可用于日级混淆矩阵的样本网格")
    if _coverage_note(coverage):
        data_notes.append(_coverage_note(coverage))

    return render_template(
        'alerts_accuracy.html',
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        location_filter=filters['location'],
        alert_type_filter=filters['alert_type'],
        alert_level_filter=filters['alert_level'],
        follow_days=follow_days,
        min_days=min_days,
        threshold_q=threshold_q,
        follow_days_options=[1, 2, 3, 5, 7],
        min_days_options=[3, 5, 7, 14, 21],
        threshold_q_options=THRESHOLD_Q_OPTIONS,
        alert_type_options=alert_type_options,
        alert_level_options=alert_level_options,
        rows=selected['rows'],
        contingency=contingency,
        data_summary=data_summary,
        data_notes=data_notes,
        level_rows=selected['level_rows'],
        type_rows=selected['type_rows'],
        certainty_rows=selected['certainty_rows'],
        reliability_rows=selected['reliability_rows'],
        weekly_calibration_rows=selected['weekly_calibration_rows'],
        sensitivity_rows=sensitivity_rows,
        top_false_alerts=selected['top_false_alerts'],
        miss_rows=selected['miss_rows'],
        event_rows=selected['event_rows'],
        chart_payload=chart_payload
        ,
        overlap_meta={
            'has_overlap': coverage['has_overlap'],
            'overlap_start': coverage['overlap_start'],
            'overlap_end': coverage['overlap_end'],
            'alert_min': coverage['alert_min'],
            'alert_max': coverage['alert_max'],
            'record_min': coverage['record_min'],
            'record_max': coverage['record_max']
        }
    )


@admin_route('/reports', endpoint='reports_center')
def reports_center():
    """报告导出"""
    return render_template('reports.html')


@admin_route('/reports/export', methods=['POST'], endpoint='reports_export')
def reports_export():
    """导出周报/月报"""
    report_type = request.form.get('report_type', 'weekly')
    report_format = request.form.get('format', 'excel')

    log_audit(
        'reports_export',
        resource_type='reports',
        metadata={'type': report_type, 'format': report_format}
    )

    end_date = today_local()
    if report_type == 'monthly':
        start_date = end_date - timedelta(days=30)
        title = '月报'
    else:
        start_date = end_date - timedelta(days=7)
        title = '周报'

    summary = {
        'period': f"{start_date} ~ {end_date}",
        'total_visits': _visit_query(start_date, end_date).count(),
        'total_alerts': WeatherAlert.query.filter(
            WeatherAlert.alert_date >= date_to_utc_start(start_date),
            WeatherAlert.alert_date <= date_to_utc_end(end_date)
        ).count(),
        'total_assessments': HealthRiskAssessment.query.filter(
            HealthRiskAssessment.assessment_date >= date_to_utc_start(start_date),
            HealthRiskAssessment.assessment_date <= date_to_utc_end(end_date)
        ).count()
    }

    disease_stats = db.session.query(
        MedicalRecord.disease_category,
        db.func.count(MedicalRecord.id)
    ).filter(
        MedicalRecord.disease_category.isnot(None),
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    ).group_by(MedicalRecord.disease_category).order_by(
        db.func.count(MedicalRecord.id).desc()
    ).limit(10).all()

    weather_stats = db.session.query(
        db.func.avg(WeatherData.temperature),
        db.func.avg(WeatherData.humidity),
        db.func.avg(WeatherData.aqi)
    ).filter(
        WeatherData.date >= start_date,
        WeatherData.date <= end_date
    ).first()

    avg_temp = round(weather_stats[0], 2) if weather_stats and weather_stats[0] is not None else None
    avg_humidity = round(weather_stats[1], 2) if weather_stats and weather_stats[1] is not None else None
    avg_aqi = round(weather_stats[2], 2) if weather_stats and weather_stats[2] is not None else None

    if report_format == 'excel':
        import pandas as pd
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            pd.DataFrame([summary]).to_excel(writer, index=False, sheet_name='summary')
            pd.DataFrame(disease_stats, columns=['disease_category', 'count']).to_excel(
                writer, index=False, sheet_name='top_diseases'
            )
            pd.DataFrame([{
                'avg_temperature': avg_temp,
                'avg_humidity': avg_humidity,
                'avg_aqi': avg_aqi
            }]).to_excel(writer, index=False, sheet_name='weather')
        output.seek(0)
        filename = f"{title}_{end_date}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    if report_format == 'pdf':
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        try:
            pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
            pdf.setFont('STSong-Light', 12)
        except Exception as exc:
            logger.warning("PDF font registration failed: %s", exc)
        pdf.setTitle(f"{title}")
        pdf.drawString(50, 800, f"{title} - {summary['period']}")
        pdf.drawString(50, 780, f"总门诊量: {summary['total_visits']}")
        pdf.drawString(50, 760, f"预警次数: {summary['total_alerts']}")
        pdf.drawString(50, 740, f"评估次数: {summary['total_assessments']}")
        pdf.drawString(50, 720, f"平均温度: {avg_temp if avg_temp is not None else '--'}°C")
        pdf.drawString(50, 700, f"平均湿度: {avg_humidity if avg_humidity is not None else '--'}%")
        pdf.drawString(50, 680, f"平均AQI: {avg_aqi if avg_aqi is not None else '--'}")
        pdf.drawString(50, 650, "Top疾病:")
        y = 630
        for disease, count in disease_stats:
            pdf.drawString(70, y, f"{disease}: {count}")
            y -= 18
            if y < 80:
                pdf.showPage()
                y = 800
        pdf.save()
        buffer.seek(0)
        filename = f"{title}_{end_date}.pdf"
        return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')

    flash('不支持的导出格式', 'error')
    return redirect(url_for('analysis.reports_center'))


@bp.route('/annual-report', endpoint='annual_report')
@login_required
def annual_report():
    """年度健康报告"""
    if is_guest_user(current_user):
        flash('游客模式无法生成年度报告，请注册/登录正式账号', 'error')
        return redirect(url_for('user.user_dashboard'))

    end_date = today_local()
    start_date = end_date - timedelta(days=365)

    # 使用 UTC-aware 时间比较（assessment_date 是 UTC 时间戳）
    assessments = HealthRiskAssessment.query.filter(
        HealthRiskAssessment.user_id == current_user.id,
        HealthRiskAssessment.assessment_date >= date_to_utc_start(start_date),
        HealthRiskAssessment.assessment_date <= date_to_utc_end(end_date)
    ).all()

    diary_entries = HealthDiary.query.filter(
        HealthDiary.user_id == current_user.id,
        HealthDiary.entry_date >= start_date,
        HealthDiary.entry_date <= end_date
    ).all()

    risk_scores = [a.risk_score for a in assessments if a.risk_score is not None]
    avg_risk = round(sum(risk_scores) / len(risk_scores), 2) if risk_scores else None
    level_counts = {'低风险': 0, '中风险': 0, '高风险': 0}
    for a in assessments:
        level_counts[a.risk_level] = level_counts.get(a.risk_level, 0) + 1

    severity_counts = {}
    for entry in diary_entries:
        severity = entry.severity or '未填写'
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    return render_template(
        'annual_report.html',
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        total_assessments=len(assessments),
        avg_risk=avg_risk,
        level_counts=level_counts,
        diary_count=len(diary_entries),
        severity_counts=severity_counts
    )


@admin_route('/analysis/pilot', endpoint='pilot_dashboard')
def pilot_dashboard():
    """试点数据看板（管理员）"""
    days = request.args.get('days', default=30, type=int)
    days = max(1, min(days, 365))

    now = utcnow()
    start_ts = now - timedelta(days=days)
    start_7d = now - timedelta(days=7)
    start_30d = now - timedelta(days=30)

    pairs_total = Pair.query.filter_by(status='active').count()
    elders_total = Pair.query.filter(Pair.status == 'active', Pair.member_id.isnot(None)).count()

    # 活跃 caregiver：近N天有 usage_events
    active_7d = db.session.query(db.func.count(db.func.distinct(UsageEvent.user_id))).filter(
        UsageEvent.user_id.isnot(None),
        UsageEvent.created_at >= start_7d
    ).scalar() or 0
    active_30d = db.session.query(db.func.count(db.func.distinct(UsageEvent.user_id))).filter(
        UsageEvent.user_id.isnot(None),
        UsageEvent.created_at >= start_30d
    ).scalar() or 0

    # 推送投递与CTR
    sent = AlertDelivery.query.filter(AlertDelivery.sent_at >= start_ts, AlertDelivery.status == 'sent').count()
    failed = AlertDelivery.query.filter(AlertDelivery.sent_at >= start_ts, AlertDelivery.status == 'failed').count()
    clicked = AlertDelivery.query.filter(AlertDelivery.sent_at >= start_ts, AlertDelivery.clicked_at.isnot(None)).count()
    ctr = round(clicked / sent, 4) if sent else 0.0

    template_copy = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'template_copy'
    ).count()
    template_pairs = db.session.query(db.func.count(db.func.distinct(UsageEvent.pair_id))).filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'template_copy',
        UsageEvent.pair_id.isnot(None)
    ).scalar() or 0

    feedback_count = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'feedback_submitted'
    ).count()

    wxoa_land = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'wxoa_land'
    ).count()

    location_expr = db.func.coalesce(Pair.location_query, Pair.community_code).label('location')
    cnt_expr = db.func.count(Pair.id).label('cnt')
    location_rows = db.session.query(
        location_expr,
        cnt_expr
    ).filter(
        Pair.status == 'active'
    ).group_by(location_expr).order_by(cnt_expr.desc()).limit(20).all()
    location_coverage = [{'location': r[0] or '', 'count': int(r[1] or 0)} for r in location_rows]

    return render_template(
        'analysis_pilot.html',
        days=days,
        pairs_total=pairs_total,
        elders_total=elders_total,
        active_7d=active_7d,
        active_30d=active_30d,
        push_sent=sent,
        push_failed=failed,
        push_clicked=clicked,
        push_ctr=ctr,
        template_copy=template_copy,
        template_pairs=template_pairs,
        feedback_count=feedback_count,
        wxoa_land=wxoa_land,
        location_coverage=location_coverage,
    )


@admin_route('/analysis/pilot/export.csv', endpoint='pilot_export_csv')
def pilot_export_csv():
    """导出试点埋点（CSV）"""
    days = request.args.get('days', default=30, type=int)
    days = max(1, min(days, 365))
    start_ts = utcnow() - timedelta(days=days)

    events = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts
    ).order_by(UsageEvent.created_at.desc()).all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['created_at', 'event_type', 'user_id', 'pair_id', 'member_id', 'source', 'meta_json'])
    for e in events:
        writer.writerow([
            e.created_at.isoformat() if e.created_at else '',
            e.event_type or '',
            e.user_id or '',
            e.pair_id or '',
            e.member_id or '',
            e.source or '',
            e.meta_json or '',
        ])

    data = out.getvalue().encode('utf-8-sig')  # Excel-friendly
    return send_file(
        io.BytesIO(data),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'pilot_events_last_{days}d.csv',
    )


@admin_route('/analysis/model-quality', endpoint='model_quality')
def model_quality():
    """模型可靠性（护栏 + 回测报告）"""
    from pathlib import Path

    base_dir = Path(__file__).resolve().parents[1]
    report_path = base_dir / 'tmp' / 'backtest_report.json'

    report = None
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            report = None

    return render_template('analysis_model_quality.html', report=report, report_path=str(report_path))
