# -*- coding: utf-8 -*-
"""Analysis and report routes."""
import io
import logging
import math
from datetime import timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from core.constants import DEFAULT_CITY_LABEL
from core.extensions import db
from core.guest import is_guest_user
from core.analytics import pearson_corr
from core.audit import log_audit
from core.db_models import (
    Community,
    HealthDiary,
    HealthRiskAssessment,
    MedicalRecord,
    WeatherAlert,
    WeatherData
)
from core.time_utils import today_local, date_to_utc_start, date_to_utc_end, utc_to_local_date
from utils.parsers import parse_date
from utils.validators import sanitize_input

logger = logging.getLogger(__name__)

bp = Blueprint('analysis', __name__)


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


@bp.route('/analysis/history', methods=['GET', 'POST'], endpoint='analysis_history')
@login_required
def analysis_history():
    """历史数据回溯分析"""
    community_filter = sanitize_input(request.values.get('community'), max_length=100)
    disease_filter = sanitize_input(request.values.get('disease'), max_length=100)
    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    auto_range = False
    if not start_raw and not end_raw:
        last_visit = _latest_visit_date(community_filter, disease_filter)
        default_city = _default_city()
        weather_location = community_filter or default_city
        last_weather = _latest_weather_date(weather_location)
        if not last_weather and community_filter:
            last_weather = _latest_weather_date(default_city)
        candidates = [d for d in (last_visit, last_weather) if d]
        if candidates:
            end_date = min(candidates)
            start_date = end_date - timedelta(days=30)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=30)

    communities = Community.query.all()
    diseases = db.session.query(MedicalRecord.disease_category).filter(
        MedicalRecord.disease_category.isnot(None)
    ).distinct().order_by(MedicalRecord.disease_category).all()
    diseases = [d[0] for d in diseases]

    # 统计每日病例
    record_query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    )
    if community_filter:
        record_query = record_query.filter(MedicalRecord.community == community_filter)
    if disease_filter:
        record_query = record_query.filter(MedicalRecord.disease_category == disease_filter)

    records = record_query.all()
    visits_by_date = {}
    for record in records:
        date_key = utc_to_local_date(record.visit_time)
        visits_by_date[date_key] = visits_by_date.get(date_key, 0) + 1

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

    dates = []
    visits = []
    temperatures = []
    humidities = []
    cursor = start_date
    while cursor <= end_date:
        dates.append(cursor.strftime('%Y-%m-%d'))
        visits.append(visits_by_date.get(cursor, 0))
        weather = weather_by_date.get(cursor)
        temperatures.append(weather['temperature'] if weather else None)
        humidities.append(weather['humidity'] if weather else None)
        cursor += timedelta(days=1)

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
    data_notes = []
    if auto_range:
        data_notes.append("已自动定位到最近有数据的时间区间")
    if community_filter and used_fallback:
        data_notes.append(f"社区无天气数据，已使用{weather_source}")
    if total_visits == 0:
        data_notes.append("所选区间无门诊记录")
    if weather_days == 0:
        data_notes.append(f"所选区间无{weather_source}天气数据")
    if total_visits > 0 and weather_days > 0 and overlap_days == 0:
        data_notes.append("病例与天气日期无重叠")
    if weather_days < total_days:
        data_notes.append(f"天气覆盖{weather_days}/{total_days}天")

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
        data_notes=data_notes
    )


@bp.route('/analysis/heatmap', methods=['GET', 'POST'], endpoint='analysis_heatmap')
@login_required
def analysis_heatmap():
    """天气-疾病相关性热力图"""
    community_filter = sanitize_input(request.values.get('community'), max_length=100)
    disease_filter = sanitize_input(request.values.get('disease'), max_length=100)
    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    auto_range = False
    if not start_raw and not end_raw:
        last_visit = _latest_visit_date(community_filter, disease_filter)
        default_city = _default_city()
        weather_location = community_filter or default_city
        last_weather = _latest_weather_date(weather_location)
        if not last_weather and community_filter:
            last_weather = _latest_weather_date(default_city)
        candidates = [d for d in (last_visit, last_weather) if d]
        if candidates:
            end_date = min(candidates)
            start_date = end_date - timedelta(days=90)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=90)

    communities = Community.query.all()
    diseases = db.session.query(MedicalRecord.disease_category).filter(
        MedicalRecord.disease_category.isnot(None)
    ).distinct().order_by(MedicalRecord.disease_category).all()
    diseases = [d[0] for d in diseases]

    temp_bins = [-10, 0, 10, 20, 30, 40, 50]
    humidity_bins = [0, 20, 40, 60, 80, 100]
    matrix = [[0 for _ in range(len(humidity_bins) - 1)] for _ in range(len(temp_bins) - 1)]

    record_query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    )
    if community_filter:
        record_query = record_query.filter(MedicalRecord.community == community_filter)
    if disease_filter:
        record_query = record_query.filter(MedicalRecord.disease_category == disease_filter)

    daily_counts = {}
    for record in record_query.all():
        date_key = utc_to_local_date(record.visit_time)
        daily_counts[date_key] = daily_counts.get(date_key, 0) + 1

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
            weather_by_date[date_key] = {'temperature': 0, 'humidity': 0}
            weather_counts[date_key] = 0
        weather_by_date[date_key]['temperature'] += w.temperature or 0
        weather_by_date[date_key]['humidity'] += w.humidity or 0
        weather_counts[date_key] += 1

    for date_key, count in weather_counts.items():
        if count > 0:
            weather_by_date[date_key]['temperature'] /= count
            weather_by_date[date_key]['humidity'] /= count

    def find_bin(value, bins):
        if value is None:
            return None
        for i in range(len(bins) - 1):
            if bins[i] <= value < bins[i + 1]:
                return i
        if value >= bins[-1]:
            return len(bins) - 2
        return None

    for date_key, count in daily_counts.items():
        weather = weather_by_date.get(date_key)
        if not weather:
            continue
        temp_idx = find_bin(weather['temperature'], temp_bins)
        hum_idx = find_bin(weather['humidity'], humidity_bins)
        if temp_idx is not None and hum_idx is not None:
            matrix[temp_idx][hum_idx] += count

    max_value = max([max(row) for row in matrix]) if matrix else 0
    temp_labels = [f"{temp_bins[i]}~{temp_bins[i+1]}°C" for i in range(len(temp_bins) - 1)]
    hum_labels = [f"{humidity_bins[i]}~{humidity_bins[i+1]}%" for i in range(len(humidity_bins) - 1)]

    total_days = (end_date - start_date).days + 1
    visit_days = len(daily_counts)
    weather_days = len(weather_by_date)
    overlap_days = len(set(daily_counts.keys()) & set(weather_by_date.keys()))
    total_visits = sum(daily_counts.values())
    data_notes = []
    if auto_range:
        data_notes.append("已自动定位到最近有数据的时间区间")
    if community_filter and used_fallback:
        data_notes.append(f"社区无天气数据，已使用{weather_source}")
    if total_visits == 0:
        data_notes.append("所选区间无门诊记录")
    if weather_days == 0:
        data_notes.append(f"所选区间无{weather_source}天气数据")
    if total_visits > 0 and weather_days > 0 and overlap_days == 0:
        data_notes.append("病例与天气日期无重叠")
    if weather_days < total_days:
        data_notes.append(f"天气覆盖{weather_days}/{total_days}天")

    data_summary = {
        'total_days': total_days,
        'visit_days': visit_days,
        'total_visits': total_visits,
        'weather_days': weather_days,
        'overlap_days': overlap_days,
        'weather_source': weather_source
    }

    return render_template(
        'analysis_heatmap.html',
        communities=communities,
        diseases=diseases,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        community_filter=community_filter,
        disease_filter=disease_filter,
        temp_labels=temp_labels,
        hum_labels=hum_labels,
        matrix=matrix,
        max_value=max_value,
        data_summary=data_summary,
        data_notes=data_notes
    )


@bp.route('/analysis/lag', methods=['GET', 'POST'], endpoint='analysis_lag')
@login_required
def analysis_lag():
    """滞后效应可视化"""
    community_filter = sanitize_input(request.values.get('community'), max_length=100)
    communities = Community.query.all()
    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    auto_range = False
    if not start_raw and not end_raw:
        last_visit = _latest_visit_date(community_filter, None)
        default_city = _default_city()
        weather_location = community_filter or default_city
        last_weather = _latest_weather_date(weather_location)
        if not last_weather and community_filter:
            last_weather = _latest_weather_date(default_city)
        candidates = [d for d in (last_visit, last_weather) if d]
        if candidates:
            end_date = min(candidates)
            start_date = end_date - timedelta(days=90)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=90)

    record_query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    )
    if community_filter:
        record_query = record_query.filter(MedicalRecord.community == community_filter)

    visits_by_date = {}
    for record in record_query.all():
        date_key = utc_to_local_date(record.visit_time)
        visits_by_date[date_key] = visits_by_date.get(date_key, 0) + 1

    weather_records, weather_location, used_fallback = _load_weather_records(
        start_date, end_date, community_filter
    )
    default_city = _default_city()
    weather_source = _weather_source_label(weather_location, default_city)

    temp_by_date = {}
    temp_count = {}
    for w in weather_records:
        date_key = w.date
        if date_key not in temp_by_date:
            temp_by_date[date_key] = 0
            temp_count[date_key] = 0
        temp_by_date[date_key] += w.temperature or 0
        temp_count[date_key] += 1
    for date_key, count in temp_count.items():
        if count > 0:
            temp_by_date[date_key] /= count

    lag_results = []
    for lag in range(1, 8):
        x_vals = []
        y_vals = []
        for date_key, visits in visits_by_date.items():
            temp_date = date_key - timedelta(days=lag)
            temp = temp_by_date.get(temp_date)
            if temp is not None:
                x_vals.append(temp)
                y_vals.append(visits)
        n = len(x_vals)
        corr = pearson_corr(x_vals, y_vals) if n >= 2 else None
        lag_results.append({'lag': lag, 'corr': corr, 'n': n})

    total_days = (end_date - start_date).days + 1
    visit_days = len(visits_by_date)
    weather_days = len(temp_by_date)
    overlap_days = len(set(visits_by_date.keys()) & set(temp_by_date.keys()))
    total_visits = sum(visits_by_date.values())
    data_notes = []
    if auto_range:
        data_notes.append("已自动定位到最近有数据的时间区间")
    if community_filter and used_fallback:
        data_notes.append(f"社区无天气数据，已使用{weather_source}")
    if total_visits == 0:
        data_notes.append("所选区间无门诊记录")
    if weather_days == 0:
        data_notes.append(f"所选区间无{weather_source}天气数据")
    if total_visits > 0 and weather_days > 0 and overlap_days == 0:
        data_notes.append("病例与天气日期无重叠")
    if weather_days < total_days:
        data_notes.append(f"天气覆盖{weather_days}/{total_days}天")

    data_summary = {
        'total_days': total_days,
        'visit_days': visit_days,
        'total_visits': total_visits,
        'weather_days': weather_days,
        'overlap_days': overlap_days,
        'weather_source': weather_source
    }

    return render_template(
        'analysis_lag.html',
        communities=communities,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        community_filter=community_filter,
        lag_results=lag_results,
        data_summary=data_summary,
        data_notes=data_notes
    )


@bp.route('/analysis/community-compare', methods=['GET', 'POST'], endpoint='analysis_community_compare')
@login_required
def analysis_community_compare():
    """社区对比分析"""
    start_raw = request.values.get('start_date')
    end_raw = request.values.get('end_date')
    start_date = parse_date(start_raw)
    end_date = parse_date(end_raw)
    if not start_raw and not end_raw:
        last_visit = _latest_visit_date(None, None)
        if last_visit:
            end_date = last_visit
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=90)

    communities = Community.query.all()
    stats = []
    for comm in communities:
        count = MedicalRecord.query.filter(
            MedicalRecord.community == comm.name,
            MedicalRecord.visit_time.isnot(None),
            MedicalRecord.visit_time >= date_to_utc_start(start_date),
            MedicalRecord.visit_time <= date_to_utc_end(end_date)
        ).count()
        stats.append({
            'name': comm.name,
            'visits': count,
            'population': comm.population or 0,
            'risk_level': comm.risk_level or '未知',
            'vulnerability_index': comm.vulnerability_index or 0
        })

    total_visits = sum(item['visits'] for item in stats)
    data_notes = []
    if not communities:
        data_notes.append("暂无社区数据")
    if total_visits == 0:
        data_notes.append("所选区间无门诊记录")

    return render_template(
        'analysis_community_compare.html',
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        stats=stats,
        labels=[s['name'] for s in stats],
        values=[s['visits'] for s in stats],
        data_notes=data_notes,
        data_summary={
            'total_visits': total_visits,
            'community_count': len(communities)
        }
    )


@bp.route('/alerts/history', methods=['GET', 'POST'], endpoint='alerts_history')
@login_required
def alerts_history():
    """预警历史记录"""
    start_date = parse_date(request.values.get('start_date'))
    end_date = parse_date(request.values.get('end_date'))
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=30)

    # 使用 UTC-aware 时间比较（alert_date 是 UTC 时间戳）
    alerts = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= date_to_utc_start(start_date),
        WeatherAlert.alert_date <= date_to_utc_end(end_date)
    ).order_by(WeatherAlert.alert_date.desc()).all()

    return render_template(
        'alerts_history.html',
        alerts=alerts,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d')
    )


@bp.route('/alerts/accuracy', methods=['GET', 'POST'], endpoint='alerts_accuracy')
@login_required
def alerts_accuracy():
    """预警准确率统计"""
    end_date = today_local()
    start_date = end_date - timedelta(days=90)

    # 使用 UTC-aware 时间比较
    alerts = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= date_to_utc_start(start_date),
        WeatherAlert.alert_date <= date_to_utc_end(end_date)
    ).all()

    # 统计日门诊（visit_time 可能是 naive 或 aware，需要兼容处理）
    visit_query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date + timedelta(days=7))
    ).all()

    daily_visits = {}
    for record in visit_query:
        community = record.community or '未知'
        date_key = utc_to_local_date(record.visit_time)
        daily_visits.setdefault(community, {})
        daily_visits[community][date_key] = daily_visits[community].get(date_key, 0) + 1

    # 计算各社区阈值
    thresholds = {}
    for community, data in daily_visits.items():
        values = sorted(data.values())
        if values:
            index = max(0, math.ceil(len(values) * 0.9) - 1)
            thresholds[community] = values[index]
        else:
            thresholds[community] = 0

    total_alerts = len(alerts)
    hit_count = 0
    for alert in alerts:
        community = alert.location or '未知'
        threshold = thresholds.get(community, 0)
        base_day = utc_to_local_date(alert.alert_date)
        hit = False
        for offset in range(1, 4):
            if base_day is None:
                continue
            day = base_day + timedelta(days=offset)
            visits = daily_visits.get(community, {}).get(day, 0)
            if visits >= threshold and threshold > 0:
                hit = True
                break
        if hit:
            hit_count += 1

    total_visits = sum(sum(values.values()) for values in daily_visits.values()) if daily_visits else 0
    has_thresholds = any(value > 0 for value in thresholds.values()) if thresholds else False
    accuracy = (hit_count / total_alerts) * 100 if total_alerts and total_visits and has_thresholds else None
    data_notes = []
    if total_alerts == 0:
        data_notes.append("区间内暂无预警记录")
    if total_visits == 0:
        data_notes.append("区间内暂无门诊记录")
    if total_visits > 0 and not has_thresholds:
        data_notes.append("门诊量阈值不足，无法评估准确率")

    return render_template(
        'alerts_accuracy.html',
        total_alerts=total_alerts,
        hit_count=hit_count,
        accuracy=accuracy,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        data_notes=data_notes
    )


@bp.route('/reports', endpoint='reports_center')
@login_required
def reports_center():
    """报告导出"""
    return render_template('reports.html')


@bp.route('/reports/export', methods=['POST'], endpoint='reports_export')
@login_required
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
        'total_visits': MedicalRecord.query.filter(
            MedicalRecord.visit_time.isnot(None),
            MedicalRecord.visit_time >= date_to_utc_start(start_date),
            MedicalRecord.visit_time <= date_to_utc_end(end_date)
        ).count(),
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
