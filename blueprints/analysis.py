# -*- coding: utf-8 -*-
"""Analysis and report routes."""
import csv
import io
import json
import logging
import math
from collections import defaultdict
from datetime import timedelta

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
from services.miniprogram_metrics import load_miniprogram_metrics
from utils.parsers import parse_date
from utils.validators import sanitize_input

logger = logging.getLogger(__name__)

bp = Blueprint('analysis', __name__)


def _require_admin():
    if getattr(current_user, 'role', None) != 'admin':
        flash('权限不足', 'error')
        return False
    return True


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


def _safe_int(raw_value, default, minimum=None, maximum=None):
    """Parse int with optional clamp."""
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _normalize_gender(raw_gender):
    if raw_gender is None:
        return ''
    value = str(raw_gender).strip().lower()
    if value in {'male', 'm', 'man'}:
        return 'male'
    if value in {'female', 'f', 'woman'}:
        return 'female'
    if '男' in value and '女' not in value:
        return 'male'
    if '女' in value and '男' not in value:
        return 'female'
    return value


def _record_matches_stratum(age, gender, stratum):
    if stratum == 'all':
        return True
    if stratum == 'elderly':
        return age is not None and age >= 65
    if stratum == 'non_elderly':
        return age is not None and age < 65
    normalized = _normalize_gender(gender)
    if stratum == 'male':
        return normalized == 'male'
    if stratum == 'female':
        return normalized == 'female'
    return True


def _build_daily_weather(records):
    weather_by_date = {}
    for row in records:
        day = row.date
        bucket = weather_by_date.setdefault(day, {
            'temp_sum': 0.0,
            'temp_n': 0,
            'hum_sum': 0.0,
            'hum_n': 0
        })
        if row.temperature is not None:
            bucket['temp_sum'] += row.temperature
            bucket['temp_n'] += 1
        if row.humidity is not None:
            bucket['hum_sum'] += row.humidity
            bucket['hum_n'] += 1

    daily_avg = {}
    for day, values in weather_by_date.items():
        temp = values['temp_sum'] / values['temp_n'] if values['temp_n'] else None
        humidity = values['hum_sum'] / values['hum_n'] if values['hum_n'] else None
        daily_avg[day] = {'temperature': temp, 'humidity': humidity}
    return daily_avg


def _lag_exposure_for_date(target_date, lag_window, weather_by_date):
    temps = []
    humidities = []
    for offset in range(lag_window + 1):
        day = target_date - timedelta(days=offset)
        row = weather_by_date.get(day)
        if not row:
            return None
        temp = row.get('temperature')
        humidity = row.get('humidity')
        if temp is None or humidity is None:
            return None
        temps.append(temp)
        humidities.append(humidity)
    return {
        'temperature': sum(temps) / len(temps),
        'humidity': sum(humidities) / len(humidities)
    }


def _find_bin(value, bins):
    if value is None:
        return None
    for idx in range(len(bins) - 1):
        left = bins[idx]
        right = bins[idx + 1]
        if left <= value < right:
            return idx
    if value >= bins[-1]:
        return len(bins) - 2
    return None


def _format_bucket_label(left, right, unit):
    def fmt(num):
        if num is None:
            return '--'
        if abs(num - round(num)) < 0.05:
            return str(int(round(num)))
        return f"{num:.1f}"

    return f"{fmt(left)}~{fmt(right)}{unit}"


def _percentile(values, q):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return values[low]
    weight = pos - low
    return values[low] * (1 - weight) + values[high] * weight


def _gini(values):
    """Gini coefficient for non-negative values."""
    valid = sorted(
        float(value) for value in values
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    )
    count = len(valid)
    if count == 0:
        return None
    total = sum(valid)
    if total <= 0:
        return 0.0
    weighted_sum = 0.0
    for idx, value in enumerate(valid, start=1):
        weighted_sum += idx * value
    gini = (2 * weighted_sum) / (count * total) - (count + 1) / count
    return max(0.0, min(1.0, gini))


def _roc_auc_from_pairs(pairs):
    """基于概率-观测对计算二分类 ROC AUC（Mann-Whitney 近似）。"""
    valid = []
    for item in pairs or []:
        try:
            prob = float(item.get('probability'))
            obs = int(item.get('observed'))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0.0 <= prob <= 1.0):
            continue
        if obs not in (0, 1):
            continue
        valid.append((prob, obs))
    if len(valid) < 2:
        return None

    pos_scores = [score for score, obs in valid if obs == 1]
    neg_scores = [score for score, obs in valid if obs == 0]
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    if n_pos == 0 or n_neg == 0:
        return None

    wins = 0.0
    ties = 0.0
    for pos in pos_scores:
        for neg in neg_scores:
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                ties += 1.0
    auc = (wins + 0.5 * ties) / (n_pos * n_neg)
    return max(0.0, min(1.0, auc))


def _build_quantile_bins(values, bucket_count, fallback_bins):
    valid = sorted(
        value for value in values
        if isinstance(value, (int, float)) and math.isfinite(value)
    )
    if len(valid) < bucket_count * 3:
        return fallback_bins

    edges = []
    for idx in range(bucket_count + 1):
        edge = _percentile(valid, idx / bucket_count)
        if edge is None:
            return fallback_bins
        edges.append(round(edge, 2))

    normalized = [edges[0]]
    for edge in edges[1:]:
        if edge <= normalized[-1]:
            edge = round(normalized[-1] + 0.1, 2)
        normalized.append(edge)

    if len(normalized) != len(fallback_bins):
        return fallback_bins
    return normalized


def _rr_with_ci(observed, expected):
    if expected is None or expected <= 0:
        return None, None, None
    if observed <= 0:
        # Poisson 95% upper bound when observed=0 is approximately 3.0.
        return 0.0, 0.0, 3.0 / expected

    rr = observed / expected
    se = 1.0 / math.sqrt(observed)
    ci_low = math.exp(math.log(rr) - 1.96 * se)
    ci_high = math.exp(math.log(rr) + 1.96 * se)
    return rr, ci_low, ci_high


def _corr_with_ci(xs, ys):
    """Pearson correlation with Fisher-z 95% CI."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None, None, None

    corr = pearson_corr(xs, ys)
    if corr is None or not math.isfinite(corr):
        return None, None, None

    bounded = max(-0.999999, min(0.999999, corr))
    if n <= 3:
        return bounded, None, None

    z = 0.5 * math.log((1 + bounded) / (1 - bounded))
    se = 1.0 / math.sqrt(max(1, n - 3))
    z_low = z - 1.96 * se
    z_high = z + 1.96 * se
    corr_low = math.tanh(z_low)
    corr_high = math.tanh(z_high)
    return bounded, corr_low, corr_high


def _certainty_level(days, visits, ci_low, ci_high, min_days):
    if days < min_days:
        return 'insufficient'
    if ci_low is None or ci_high is None:
        return 'low'
    width = ci_high - ci_low
    if days >= max(10, min_days + 6) and visits >= 12 and width <= 1.2:
        return 'high'
    if days >= max(5, min_days + 2) and visits >= 4 and width <= 2.0:
        return 'medium'
    return 'low'


def _action_level(rr, significant, certainty, days, min_days):
    if rr is None or days < min_days:
        return '样本不足'
    if rr >= 1.6 and significant and certainty == 'high':
        return '立即行动'
    if rr >= 1.3 and (significant or certainty in {'high', 'medium'}):
        return '准备干预'
    if rr <= 0.75 and significant:
        return '观察（低风险）'
    return '观察'


def _heatmap_cell_color(rr, days, min_days):
    if rr is None or days < min_days:
        return 'rgba(148, 163, 184, 0.18)'
    capped = max(0.4, min(2.4, rr))
    if capped >= 1:
        alpha = 0.18 + 0.5 * ((capped - 1.0) / 1.4)
        return f"rgba(201, 72, 72, {alpha:.3f})"
    alpha = 0.18 + 0.5 * ((1.0 - capped) / 0.6)
    return f"rgba(52, 120, 189, {alpha:.3f})"


def _json_loads_safe(raw_text, default):
    if raw_text is None:
        return default
    if isinstance(raw_text, (dict, list)):
        return raw_text
    text = str(raw_text).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _alert_cap_semantics(alert_level, alert_type, description):
    level_text = str(alert_level or '')
    type_text = str(alert_type or '')
    desc_text = str(description or '')
    merged_cn = f"{level_text}{type_text}{desc_text}"
    merged_low = merged_cn.lower()

    if any(token in merged_cn for token in ['红', '极端', '特别严重']) or 'extreme' in merged_low:
        severity = 'Extreme'
    elif any(token in merged_cn for token in ['橙', '严重']) or 'severe' in merged_low:
        severity = 'Severe'
    elif any(token in merged_cn for token in ['黄', '中度']) or 'moderate' in merged_low:
        severity = 'Moderate'
    elif any(token in merged_cn for token in ['蓝', '阈值', '提醒']) or 'minor' in merged_low:
        severity = 'Minor'
    else:
        severity = 'Unknown'

    if any(token in merged_cn for token in ['已发生', '正在', '实况']) or 'observed' in merged_low:
        certainty = 'Observed'
    elif any(token in merged_cn for token in ['预计', '将', '可能出现']) or 'likely' in merged_low:
        certainty = 'Likely'
    elif 'possible' in merged_low or '可能' in merged_cn:
        certainty = 'Possible'
    elif 'unlikely' in merged_low or '不太可能' in merged_cn:
        certainty = 'Unlikely'
    else:
        certainty = 'Possible' if severity != 'Unknown' else 'Unknown'

    if severity in {'Extreme', 'Severe'}:
        urgency = 'Immediate'
    elif certainty in {'Observed', 'Likely'}:
        urgency = 'Expected'
    elif severity == 'Unknown':
        urgency = 'Future'
    else:
        urgency = 'Future'

    return severity, certainty, urgency


def _impact_bucket_from_severity(severity):
    if severity in {'Extreme', 'Severe'}:
        return 'high'
    if severity == 'Moderate':
        return 'medium'
    return 'low'


def _likelihood_bucket_from_certainty(certainty):
    if certainty in {'Observed', 'Likely'}:
        return 'high'
    if certainty == 'Possible':
        return 'medium'
    return 'low'


def _action_from_alert_semantics(severity, certainty, urgency):
    if severity in {'Extreme', 'Severe'} and urgency in {'Immediate', 'Expected'} and certainty in {'Observed', 'Likely'}:
        return '立即行动'
    if severity in {'Moderate', 'Severe'} and certainty in {'Possible', 'Likely', 'Observed'}:
        return '准备干预'
    if severity == 'Minor':
        return '加强观察'
    return '持续观察'


def _safe_ratio(numerator, denominator):
    if denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _compute_contingency_scores(hit_count, false_alarm_count, miss_count, correct_negative_count):
    total = hit_count + false_alarm_count + miss_count + correct_negative_count
    pod = _safe_ratio(hit_count, hit_count + miss_count)
    far = _safe_ratio(false_alarm_count, hit_count + false_alarm_count)
    csi = _safe_ratio(hit_count, hit_count + false_alarm_count + miss_count)
    accuracy = _safe_ratio(hit_count + correct_negative_count, total)
    bias = _safe_ratio(hit_count + false_alarm_count, hit_count + miss_count)
    pofd = _safe_ratio(false_alarm_count, false_alarm_count + correct_negative_count)
    tss = (pod - pofd) if pod is not None and pofd is not None else None
    f1 = _safe_ratio(2 * hit_count, 2 * hit_count + false_alarm_count + miss_count)

    random_hit = None
    ets = None
    if total > 0:
        random_hit = ((hit_count + false_alarm_count) * (hit_count + miss_count)) / total
        denominator = hit_count + false_alarm_count + miss_count - random_hit
        if denominator > 0:
            ets = (hit_count - random_hit) / denominator

    hss_denominator = (
        (hit_count + miss_count) * (miss_count + correct_negative_count) +
        (hit_count + false_alarm_count) * (false_alarm_count + correct_negative_count)
    )
    hss = None
    if hss_denominator > 0:
        hss = (2 * (hit_count * correct_negative_count - false_alarm_count * miss_count)) / hss_denominator

    return {
        'pod': pod,
        'far': far,
        'csi': csi,
        'accuracy': accuracy,
        'bias': bias,
        'pofd': pofd,
        'tss': tss,
        'f1': f1,
        'ets': ets,
        'hss': hss,
        'random_hit': random_hit
    }


def _certainty_to_probability(certainty):
    mapping = {
        'Observed': 0.95,
        'Likely': 0.80,
        'Possible': 0.60,
        'Unlikely': 0.35,
        'Unknown': 0.50
    }
    return mapping.get(certainty, 0.50)


def _compute_date_overlap(start_a, end_a, start_b, end_b):
    if not all([start_a, end_a, start_b, end_b]):
        return None, None, False
    overlap_start = max(start_a, start_b)
    overlap_end = min(end_a, end_b)
    return overlap_start, overlap_end, overlap_start <= overlap_end


@bp.route('/analysis/history', methods=['GET', 'POST'], endpoint='analysis_history')
@login_required
def analysis_history():
    """历史数据回溯分析"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
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
        data_notes=data_notes,
        ui_version='HISTORY-WIREFRAME-2026-02-13',
        runtime_root=current_app.root_path
    )


@bp.route('/analysis/heatmap', methods=['GET', 'POST'], endpoint='analysis_heatmap')
@login_required
def analysis_heatmap():
    """天气-疾病相关性热力图（RR + 滞后 + 不确定性）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
    community_filter = sanitize_input(request.values.get('community'), max_length=100)
    disease_filter = sanitize_input(request.values.get('disease'), max_length=100)
    stratum = sanitize_input(request.values.get('stratum'), max_length=30) or 'all'
    if stratum not in {'all', 'elderly', 'non_elderly', 'male', 'female'}:
        stratum = 'all'

    lag_window = _safe_int(request.values.get('lag_window'), 7, minimum=0, maximum=21)
    if lag_window not in {0, 3, 7, 14, 21}:
        lag_window = 7

    binning = sanitize_input(request.values.get('binning'), max_length=20) or 'fixed'
    if binning not in {'fixed', 'quantile'}:
        binning = 'fixed'

    min_days = _safe_int(request.values.get('min_days'), 3, minimum=1, maximum=14)

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
    filtered_record_count = 0
    record_rows = record_query.with_entities(
        MedicalRecord.visit_time,
        MedicalRecord.age,
        MedicalRecord.gender
    ).all()
    for record in record_rows:
        if not _record_matches_stratum(record.age, record.gender, stratum):
            continue
        date_key = utc_to_local_date(record.visit_time)
        daily_counts[date_key] = daily_counts.get(date_key, 0) + 1
        filtered_record_count += 1

    # 为了支持 lag exposure，天气查询窗口前移 lag_window 天。
    lag_start_date = start_date - timedelta(days=lag_window)
    weather_records, weather_location, used_fallback = _load_weather_records(
        lag_start_date, end_date, community_filter
    )
    default_city = _default_city()
    weather_source = _weather_source_label(weather_location, default_city)
    weather_by_date = _build_daily_weather(weather_records)

    # 构建日级别分析样本（每一天一个 exposure + outcome）。
    analysis_points = []
    cursor = start_date
    while cursor <= end_date:
        exposure = _lag_exposure_for_date(cursor, lag_window, weather_by_date)
        if exposure:
            analysis_points.append({
                'date': cursor,
                'visits': daily_counts.get(cursor, 0),
                'temperature': exposure['temperature'],
                'humidity': exposure['humidity'],
                'month': cursor.month,
                'weekday': cursor.weekday()
            })
        cursor += timedelta(days=1)

    temp_values = [point['temperature'] for point in analysis_points]
    hum_values = [point['humidity'] for point in analysis_points]
    fixed_temp_bins = [-30, -10, 0, 10, 20, 30, 40, 55]
    fixed_hum_bins = [0, 20, 40, 60, 80, 100]
    if binning == 'quantile':
        temp_bins = _build_quantile_bins(temp_values, len(fixed_temp_bins) - 1, fixed_temp_bins)
        humidity_bins = _build_quantile_bins(hum_values, len(fixed_hum_bins) - 1, fixed_hum_bins)
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
        temp_idx = _find_bin(point['temperature'], temp_bins)
        hum_idx = _find_bin(point['humidity'], humidity_bins)
        if temp_idx is None or hum_idx is None:
            continue
        cell = matrix_raw[temp_idx][hum_idx]
        cell['visits'] += point['visits']
        cell['days'] += 1
        key = (point['month'], point['weekday'])
        cell['expected'] += baseline_rate.get(key, overall_baseline_rate)

    temp_labels = [
        _format_bucket_label(temp_bins[idx], temp_bins[idx + 1], '°C')
        for idx in range(len(temp_bins) - 1)
    ]
    hum_labels = [
        _format_bucket_label(humidity_bins[idx], humidity_bins[idx + 1], '%')
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
            rr, ci_low, ci_high = _rr_with_ci(visits, expected) if days > 0 else (None, None, None)
            significant = bool(
                rr is not None and
                days >= min_days and
                ci_low is not None and
                ci_high is not None and
                (ci_low > 1 or ci_high < 1)
            )
            certainty = _certainty_level(days, visits, ci_low, ci_high, min_days)
            certainty_counts[certainty] += 1
            action = _action_level(rr, significant, certainty, days, min_days)

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
                'bg_color': _heatmap_cell_color(rr, days, min_days)
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

    stratum_labels = {
        'all': '全人群',
        'elderly': '老年人(>=65)',
        'non_elderly': '非老年(<65)',
        'male': '男性',
        'female': '女性'
    }
    binning_labels = {
        'fixed': '固定阈值分箱',
        'quantile': '分位数分箱'
    }

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
        stratum_labels=stratum_labels,
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


@bp.route('/analysis/lag', methods=['GET', 'POST'], endpoint='analysis_lag')
@login_required
def analysis_lag():
    """滞后效应可视化（lag-response + cumulative + risk semantics）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
    community_filter = sanitize_input(request.values.get('community'), max_length=100)
    disease_filter = sanitize_input(request.values.get('disease'), max_length=100)
    stratum = sanitize_input(request.values.get('stratum'), max_length=30) or 'all'
    if stratum not in {'all', 'elderly', 'non_elderly', 'male', 'female'}:
        stratum = 'all'

    max_lag = _safe_int(request.values.get('max_lag'), 14, minimum=7, maximum=21)
    if max_lag not in {7, 14, 21}:
        max_lag = 14
    min_days = _safe_int(request.values.get('min_days'), 3, minimum=1, maximum=14)

    communities = Community.query.all()
    diseases = db.session.query(MedicalRecord.disease_category).filter(
        MedicalRecord.disease_category.isnot(None)
    ).distinct().order_by(MedicalRecord.disease_category).all()
    diseases = [d[0] for d in diseases]

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

    record_query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    )
    if community_filter:
        record_query = record_query.filter(MedicalRecord.community == community_filter)
    if disease_filter:
        record_query = record_query.filter(MedicalRecord.disease_category == disease_filter)

    visits_by_date = {}
    filtered_record_count = 0
    for record in record_query.with_entities(MedicalRecord.visit_time, MedicalRecord.age, MedicalRecord.gender).all():
        if not _record_matches_stratum(record.age, record.gender, stratum):
            continue
        date_key = utc_to_local_date(record.visit_time)
        visits_by_date[date_key] = visits_by_date.get(date_key, 0) + 1
        filtered_record_count += 1

    lag_start_date = start_date - timedelta(days=max_lag)
    weather_records, weather_location, used_fallback = _load_weather_records(
        lag_start_date, end_date, community_filter
    )
    default_city = _default_city()
    weather_source = _weather_source_label(weather_location, default_city)
    weather_by_date = _build_daily_weather(weather_records)

    analysis_days = []
    cursor = start_date
    while cursor <= end_date:
        analysis_days.append(cursor)
        cursor += timedelta(days=1)

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
    heat_threshold = _percentile(all_temps, 0.9) if all_temps else None
    cold_threshold = _percentile(all_temps, 0.1) if all_temps else None

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

        corr, corr_low, corr_high = _corr_with_ci(x_vals, y_vals)
        heat_rr, heat_ci_low, heat_ci_high = _rr_with_ci(heat_obs, heat_exp) if heat_days else (None, None, None)
        cold_rr, cold_ci_low, cold_ci_high = _rr_with_ci(cold_obs, cold_exp) if cold_days else (None, None, None)
        heat_sig = bool(
            heat_rr is not None and heat_days >= min_days and
            heat_ci_low is not None and heat_ci_high is not None and
            (heat_ci_low > 1 or heat_ci_high < 1)
        )
        cold_sig = bool(
            cold_rr is not None and cold_days >= min_days and
            cold_ci_low is not None and cold_ci_high is not None and
            (cold_ci_low > 1 or cold_ci_high < 1)
        )

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
        w_heat_thr = _percentile(sorted_values, 0.9)
        w_cold_thr = _percentile(sorted_values, 0.1)

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

        heat_rr, heat_ci_low, heat_ci_high = _rr_with_ci(heat_obs, heat_exp) if heat_days else (None, None, None)
        cold_rr, cold_ci_low, cold_ci_high = _rr_with_ci(cold_obs, cold_exp) if cold_days else (None, None, None)

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
    temp_labels = [_format_bucket_label(temp_bins[i], temp_bins[i + 1], '°C') for i in range(len(temp_bins) - 1)]
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
            bin_idx = _find_bin(temp, temp_bins)
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
            rr, ci_low, ci_high = _rr_with_ci(visits, raw_cell['expected']) if days > 0 else (None, None, None)
            significant = bool(
                rr is not None and days >= min_days and
                ci_low is not None and ci_high is not None and
                (ci_low > 1 or ci_high < 1)
            )
            certainty = _certainty_level(days, visits, ci_low, ci_high, min_days)
            action = _action_level(rr, significant, certainty, days, min_days)
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
                'bg_color': _heatmap_cell_color(rr, days, min_days)
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

    stratum_labels = {
        'all': '全人群',
        'elderly': '老年人(>=65)',
        'non_elderly': '非老年(<65)',
        'male': '男性',
        'female': '女性'
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
        stratum_labels=stratum_labels,
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


@bp.route('/analysis/community-compare', methods=['GET', 'POST'], endpoint='analysis_community_compare')
@login_required
def analysis_community_compare():
    """社区对比分析（SIR + 漏斗图 + 不平等指标）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
    disease_filter = sanitize_input(request.values.get('disease'), max_length=100)
    stratum = sanitize_input(request.values.get('stratum'), max_length=30) or 'all'
    if stratum not in {'all', 'elderly', 'non_elderly', 'male', 'female'}:
        stratum = 'all'

    min_days = _safe_int(request.values.get('min_days'), 3, minimum=1, maximum=14)
    smoothing_alpha = _safe_int(request.values.get('smoothing_alpha'), 5, minimum=0, maximum=30)
    top_n = _safe_int(request.values.get('top_n'), 12, minimum=5, maximum=25)

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
    diseases = db.session.query(MedicalRecord.disease_category).filter(
        MedicalRecord.disease_category.isnot(None)
    ).distinct().order_by(MedicalRecord.disease_category).all()
    diseases = [d[0] for d in diseases]

    record_query = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(start_date),
        MedicalRecord.visit_time <= date_to_utc_end(end_date)
    )
    if disease_filter:
        record_query = record_query.filter(MedicalRecord.disease_category == disease_filter)

    records = record_query.with_entities(
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
        if not _record_matches_stratum(row.age, row.gender, stratum):
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
        sir, ci_low, ci_high = _rr_with_ci(observed, expected) if expected else (None, None, None)
        smoothed_sir = None
        if expected:
            if smoothing_alpha > 0:
                smoothed_sir = (observed + smoothing_alpha) / (expected + smoothing_alpha)
            else:
                smoothed_sir = sir
        signal_rr = smoothed_sir if smoothed_sir is not None else sir
        significant = bool(
            signal_rr is not None and
            visit_days >= min_days and
            ci_low is not None and
            ci_high is not None and
            (ci_low > 1 or ci_high < 1)
        )
        certainty = _certainty_level(visit_days, observed, ci_low, ci_high, min_days)
        action = _action_level(signal_rr, significant, certainty, visit_days, min_days)
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
    p90_rate = _percentile(valid_rates, 0.9) if valid_rates else None
    p10_rate = _percentile(valid_rates, 0.1) if valid_rates else None
    p90_p10_ratio = (p90_rate / p10_rate) if (p90_rate is not None and p10_rate and p10_rate > 0) else None
    gini_rate = _gini(valid_rates)
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

    stratum_labels = {
        'all': '全人群',
        'elderly': '老年人(>=65)',
        'non_elderly': '非老年(<65)',
        'male': '男性',
        'female': '女性'
    }
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
        stratum_labels=stratum_labels,
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


@bp.route('/alerts/history', methods=['GET', 'POST'], endpoint='alerts_history')
@login_required
def alerts_history():
    """预警历史记录（预警-实况核验）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
    location_filter = sanitize_input(request.values.get('location'), max_length=100)
    alert_type_filter = sanitize_input(request.values.get('alert_type'), max_length=60)
    alert_level_filter = sanitize_input(request.values.get('alert_level'), max_length=30)
    outcome_filter = sanitize_input(request.values.get('outcome'), max_length=20) or 'all'
    if outcome_filter not in {'all', 'hit', 'false_alarm', 'insufficient'}:
        outcome_filter = 'all'

    follow_days = _safe_int(request.values.get('follow_days'), 3, minimum=1, maximum=7)
    min_days = _safe_int(request.values.get('min_days'), 7, minimum=3, maximum=45)
    threshold_q_options = [0.75, 0.80, 0.85, 0.90, 0.95]
    try:
        threshold_q = float(request.values.get('threshold_q', 0.90))
    except (TypeError, ValueError):
        threshold_q = 0.90
    if threshold_q not in threshold_q_options:
        threshold_q = min(threshold_q_options, key=lambda option: abs(option - threshold_q))

    alert_min_utc = WeatherAlert.query.with_entities(db.func.min(WeatherAlert.alert_date)).scalar()
    alert_max_utc = WeatherAlert.query.with_entities(db.func.max(WeatherAlert.alert_date)).scalar()
    record_min_utc = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None)).with_entities(db.func.min(MedicalRecord.visit_time)).scalar()
    record_max_utc = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None)).with_entities(db.func.max(MedicalRecord.visit_time)).scalar()
    alert_min_date = utc_to_local_date(alert_min_utc) if alert_min_utc else None
    alert_max_date = utc_to_local_date(alert_max_utc) if alert_max_utc else None
    record_min_date = utc_to_local_date(record_min_utc) if record_min_utc else None
    record_max_date = utc_to_local_date(record_max_utc) if record_max_utc else None
    overlap_start_all, overlap_end_all, overlap_exists_all = _compute_date_overlap(
        alert_min_date, alert_max_date, record_min_date, record_max_date
    )

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
            start_date = end_date - timedelta(days=60)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=30)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        date_swapped = True

    range_query = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= date_to_utc_start(start_date),
        WeatherAlert.alert_date <= date_to_utc_end(end_date)
    )
    alert_type_options = [item[0] for item in range_query.with_entities(WeatherAlert.alert_type).distinct().order_by(WeatherAlert.alert_type).all() if item[0]]
    alert_level_options = [item[0] for item in range_query.with_entities(WeatherAlert.alert_level).distinct().order_by(WeatherAlert.alert_level).all() if item[0]]

    query = range_query
    if location_filter:
        query = query.filter(WeatherAlert.location.contains(location_filter))
    if alert_type_filter:
        query = query.filter(WeatherAlert.alert_type == alert_type_filter)
    if alert_level_filter:
        query = query.filter(WeatherAlert.alert_level == alert_level_filter)

    alert_rows = query.order_by(WeatherAlert.alert_date.desc()).all()

    record_start = start_date - timedelta(days=follow_days)
    record_end = end_date + timedelta(days=follow_days)
    records = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(record_start),
        MedicalRecord.visit_time <= date_to_utc_end(record_end)
    ).with_entities(
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

    all_days = []
    cursor = record_start
    while cursor <= record_end:
        all_days.append(cursor)
        cursor += timedelta(days=1)

    def build_threshold(day_map):
        values = [day_map.get(day, 0) for day in all_days]
        observed_days = sum(1 for value in values if value > 0)
        if observed_days < min_days:
            return None, observed_days
        sorted_values = sorted(values)
        threshold = _percentile(sorted_values, threshold_q)
        return threshold, observed_days

    threshold_by_location = {}
    sample_days_by_location = {}
    for key, day_map in daily_visits.items():
        threshold, observed_days = build_threshold(day_map)
        threshold_by_location[key] = threshold
        sample_days_by_location[key] = observed_days

    global_threshold, global_sample_days = build_threshold(global_daily_visits)

    outcome_labels = {
        'hit': '命中',
        'false_alarm': '空报',
        'insufficient': '样本不足'
    }
    severity_labels = {
        'Extreme': '极高',
        'Severe': '高',
        'Moderate': '中',
        'Minor': '低',
        'Unknown': '未知'
    }
    certainty_labels = {
        'Observed': '已发生',
        'Likely': '较可能',
        'Possible': '可能',
        'Unlikely': '较不可能',
        'Unknown': '未知'
    }
    urgency_labels = {
        'Immediate': '立即',
        'Expected': '预期',
        'Future': '后续',
        'Past': '已过',
        'Unknown': '未知'
    }

    timeline_rows = []
    pre_rows = []
    for alert in alert_rows:
        alert_day = utc_to_local_date(alert.alert_date)
        location_key = (alert.location or '').strip() or '未知'
        location_day_map = daily_visits.get(location_key)
        using_global = False
        if location_day_map is None:
            location_day_map = global_daily_visits
            threshold = global_threshold
            observed_days = global_sample_days
            eval_key = '__GLOBAL__'
            eval_label = '全局基线'
            using_global = True
        else:
            threshold = threshold_by_location.get(location_key)
            observed_days = sample_days_by_location.get(location_key, 0)
            eval_key = location_key
            eval_label = location_key

        threshold_evaluable = bool(
            threshold is not None and
            threshold > 0 and
            observed_days >= min_days
        )

        window_points = []
        peak_visits = 0
        peak_day = None
        first_hit_day = None
        if alert_day:
            for offset in range(follow_days + 1):
                day = alert_day + timedelta(days=offset)
                visits = location_day_map.get(day, 0)
                window_points.append({'day': day.strftime('%Y-%m-%d'), 'visits': visits})
                if visits > peak_visits:
                    peak_visits = visits
                    peak_day = day
                if threshold_evaluable and first_hit_day is None and visits >= threshold:
                    first_hit_day = day

        hit = first_hit_day is not None
        lead_days = (first_hit_day - alert_day).days if (first_hit_day and alert_day) else None
        lead_hours = lead_days * 24 if lead_days is not None else None
        observed_ratio = (peak_visits / threshold) if (threshold_evaluable and threshold and threshold > 0) else None
        outcome = 'insufficient'
        if threshold_evaluable:
            outcome = 'hit' if hit else 'false_alarm'

        severity, certainty, urgency = _alert_cap_semantics(
            alert.alert_level, alert.alert_type, alert.description
        )
        action_text = _action_from_alert_semantics(severity, certainty, urgency)
        impact_bucket = _impact_bucket_from_severity(severity)
        likelihood_bucket = _likelihood_bucket_from_certainty(certainty)

        affected_communities = _json_loads_safe(alert.affected_communities, [])
        if not isinstance(affected_communities, list):
            affected_communities = []
        disease_corr = _json_loads_safe(alert.disease_correlation, {})
        if not isinstance(disease_corr, dict):
            disease_corr = {}

        row = {
            'id': alert.id,
            'alert_day': alert_day,
            'alert_time': alert.alert_date,
            'alert_time_text': alert.alert_date.strftime('%Y-%m-%d %H:%M') if alert.alert_date else '--',
            'location': location_key,
            'alert_type': alert.alert_type or '--',
            'alert_level': alert.alert_level or '--',
            'description': alert.description or '--',
            'threshold': threshold,
            'threshold_evaluable': threshold_evaluable,
            'threshold_source': eval_label,
            'peak_visits': peak_visits,
            'peak_day_text': peak_day.strftime('%Y-%m-%d') if peak_day else '--',
            'first_hit_day_text': first_hit_day.strftime('%Y-%m-%d') if first_hit_day else '--',
            'lead_days': lead_days,
            'lead_hours': lead_hours,
            'observed_ratio': observed_ratio,
            'outcome': outcome,
            'outcome_label': outcome_labels[outcome],
            'severity': severity,
            'severity_label': severity_labels.get(severity, severity),
            'certainty': certainty,
            'certainty_label': certainty_labels.get(certainty, certainty),
            'urgency': urgency,
            'urgency_label': urgency_labels.get(urgency, urgency),
            'action_text': action_text,
            'impact_bucket': impact_bucket,
            'likelihood_bucket': likelihood_bucket,
            'window_points': window_points,
            'affected_communities_count': len(affected_communities),
            'disease_corr': disease_corr,
            'eval_key': eval_key,
            'using_global': using_global
        }
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
        if key == '__GLOBAL__':
            day_map = global_daily_visits
            threshold = global_threshold
            key_label = '全局基线'
        else:
            day_map = daily_visits.get(key, {})
            threshold = threshold_by_location.get(key)
            key_label = key
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
    if alert_min_date and alert_max_date and record_min_date and record_max_date and not overlap_exists_all:
        data_notes.append(
            f"预警时间范围 {alert_min_date}~{alert_max_date} 与病例时间范围 {record_min_date}~{record_max_date} 无重叠，命中仅可视为不可核验"
        )

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
        threshold_q_options=threshold_q_options,
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
            'ground_truth_overlap': overlap_exists_all,
            'overlap_start': overlap_start_all,
            'overlap_end': overlap_end_all
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


@bp.route('/alerts/accuracy', methods=['GET', 'POST'], endpoint='alerts_accuracy')
@login_required
def alerts_accuracy():
    """预警准确率统计（分类核验 + 可靠性 + 阈值敏感性）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
    location_filter = sanitize_input(request.values.get('location'), max_length=100)
    alert_type_filter = sanitize_input(request.values.get('alert_type'), max_length=60)
    alert_level_filter = sanitize_input(request.values.get('alert_level'), max_length=30)

    follow_days = _safe_int(request.values.get('follow_days'), 3, minimum=1, maximum=7)
    min_days = _safe_int(request.values.get('min_days'), 7, minimum=3, maximum=45)
    threshold_q_options = [0.75, 0.80, 0.85, 0.90, 0.95]
    try:
        threshold_q = float(request.values.get('threshold_q', 0.90))
    except (TypeError, ValueError):
        threshold_q = 0.90
    if threshold_q not in threshold_q_options:
        threshold_q = min(threshold_q_options, key=lambda option: abs(option - threshold_q))

    alert_min_utc = WeatherAlert.query.with_entities(db.func.min(WeatherAlert.alert_date)).scalar()
    alert_max_utc = WeatherAlert.query.with_entities(db.func.max(WeatherAlert.alert_date)).scalar()
    record_min_utc = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None)).with_entities(db.func.min(MedicalRecord.visit_time)).scalar()
    record_max_utc = MedicalRecord.query.filter(MedicalRecord.visit_time.isnot(None)).with_entities(db.func.max(MedicalRecord.visit_time)).scalar()
    alert_min_date = utc_to_local_date(alert_min_utc) if alert_min_utc else None
    alert_max_date = utc_to_local_date(alert_max_utc) if alert_max_utc else None
    record_min_date = utc_to_local_date(record_min_utc) if record_min_utc else None
    record_max_date = utc_to_local_date(record_max_utc) if record_max_utc else None
    overlap_start_all, overlap_end_all, overlap_exists_all = _compute_date_overlap(
        alert_min_date, alert_max_date, record_min_date, record_max_date
    )

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
            start_date = end_date - timedelta(days=90)
            auto_range = True
    if not end_date:
        end_date = today_local()
    if not start_date:
        start_date = end_date - timedelta(days=90)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        date_swapped = True

    range_query = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= date_to_utc_start(start_date),
        WeatherAlert.alert_date <= date_to_utc_end(end_date)
    )
    alert_type_options = [
        item[0] for item in
        range_query.with_entities(WeatherAlert.alert_type).distinct().order_by(WeatherAlert.alert_type).all()
        if item[0]
    ]
    alert_level_options = [
        item[0] for item in
        range_query.with_entities(WeatherAlert.alert_level).distinct().order_by(WeatherAlert.alert_level).all()
        if item[0]
    ]

    query = range_query
    if location_filter:
        query = query.filter(WeatherAlert.location.contains(location_filter))
    if alert_type_filter:
        query = query.filter(WeatherAlert.alert_type == alert_type_filter)
    if alert_level_filter:
        query = query.filter(WeatherAlert.alert_level == alert_level_filter)
    alert_rows = query.order_by(WeatherAlert.alert_date.desc()).all()

    record_start = start_date - timedelta(days=follow_days)
    record_end = end_date + timedelta(days=follow_days)
    records = MedicalRecord.query.filter(
        MedicalRecord.visit_time.isnot(None),
        MedicalRecord.visit_time >= date_to_utc_start(record_start),
        MedicalRecord.visit_time <= date_to_utc_end(record_end)
    ).with_entities(
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

    all_days = []
    cursor = record_start
    while cursor <= record_end:
        all_days.append(cursor)
        cursor += timedelta(days=1)

    def build_threshold(day_map, q_value):
        values = [day_map.get(day, 0) for day in all_days]
        observed_days = sum(1 for value in values if value > 0)
        if observed_days < min_days:
            return None, observed_days
        threshold = _percentile(sorted(values), q_value)
        return threshold, observed_days

    threshold_profiles = {}
    for q_value in threshold_q_options:
        threshold_by_location = {}
        sample_days_by_location = {}
        for key, day_map in daily_visits.items():
            threshold, observed_days = build_threshold(day_map, q_value)
            threshold_by_location[key] = threshold
            sample_days_by_location[key] = observed_days
        global_threshold, global_sample_days = build_threshold(global_daily_visits, q_value)
        threshold_profiles[q_value] = {
            'threshold_by_location': threshold_by_location,
            'sample_days_by_location': sample_days_by_location,
            'global_threshold': global_threshold,
            'global_sample_days': global_sample_days
        }

    outcome_labels = {
        'hit': '命中',
        'false_alarm': '空报',
        'insufficient': '样本不足'
    }
    certainty_labels = {
        'Observed': '已发生',
        'Likely': '较可能',
        'Possible': '可能',
        'Unlikely': '较不可能',
        'Unknown': '未知'
    }
    severity_labels = {
        'Extreme': '极高',
        'Severe': '高',
        'Moderate': '中',
        'Minor': '低',
        'Unknown': '未知'
    }
    urgency_labels = {
        'Immediate': '立即',
        'Expected': '预期',
        'Future': '后续',
        'Past': '已过',
        'Unknown': '未知'
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
        threshold_by_location = profile['threshold_by_location']
        sample_days_by_location = profile['sample_days_by_location']
        global_threshold = profile['global_threshold']
        global_sample_days = profile['global_sample_days']

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
            location_day_map = daily_visits.get(location_key)
            using_global = False
            if location_day_map is None:
                location_day_map = global_daily_visits
                threshold = global_threshold
                observed_days = global_sample_days
                eval_key = '__GLOBAL__'
                threshold_source = '全局基线'
                using_global = True
            else:
                threshold = threshold_by_location.get(location_key)
                observed_days = sample_days_by_location.get(location_key, 0)
                eval_key = location_key
                threshold_source = location_key

            threshold_evaluable = bool(
                alert_day is not None and
                threshold is not None and
                threshold > 0 and
                observed_days >= min_days
            )

            window_points = []
            peak_visits = 0
            peak_day = None
            first_hit_day = None
            if alert_day is not None:
                for offset in range(follow_days + 1):
                    day = alert_day + timedelta(days=offset)
                    visits = location_day_map.get(day, 0)
                    window_points.append({
                        'day': day.strftime('%Y-%m-%d'),
                        'visits': visits
                    })
                    if visits > peak_visits:
                        peak_visits = visits
                        peak_day = day
                    if threshold_evaluable and first_hit_day is None and visits >= threshold:
                        first_hit_day = day

            hit = first_hit_day is not None
            lead_days = (first_hit_day - alert_day).days if (first_hit_day and alert_day) else None
            lead_hours = lead_days * 24 if lead_days is not None else None
            observed_ratio = (peak_visits / threshold) if (threshold_evaluable and threshold and threshold > 0) else None
            if threshold_evaluable:
                outcome = 'hit' if hit else 'false_alarm'
            else:
                outcome = 'insufficient'

            severity, certainty, urgency = _alert_cap_semantics(
                alert.alert_level, alert.alert_type, alert.description
            )
            probability = _certainty_to_probability(certainty)

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

                level_key = alert.alert_level or '--'
                level_group = level_groups.setdefault(level_key, {
                    'name': level_key,
                    'alerts': 0,
                    'hit': 0,
                    'false_alarm': 0,
                    'lead_sum': 0.0,
                    'lead_n': 0
                })
                level_group['alerts'] += 1
                if outcome == 'hit':
                    level_group['hit'] += 1
                    if lead_hours is not None:
                        level_group['lead_sum'] += lead_hours
                        level_group['lead_n'] += 1
                else:
                    level_group['false_alarm'] += 1

                type_key = alert.alert_type or '--'
                type_group = type_groups.setdefault(type_key, {
                    'name': type_key,
                    'alerts': 0,
                    'hit': 0,
                    'false_alarm': 0,
                    'lead_sum': 0.0,
                    'lead_n': 0
                })
                type_group['alerts'] += 1
                if outcome == 'hit':
                    type_group['hit'] += 1
                    if lead_hours is not None:
                        type_group['lead_sum'] += lead_hours
                        type_group['lead_n'] += 1
                else:
                    type_group['false_alarm'] += 1

                certainty_group = certainty_groups.setdefault(certainty, {
                    'certainty': certainty,
                    'label': certainty_labels.get(certainty, certainty),
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
                rows.append({
                    'id': alert.id,
                    'alert_time_text': alert.alert_date.strftime('%Y-%m-%d %H:%M') if alert.alert_date else '--',
                    'alert_day': alert_day,
                    'location': location_key,
                    'alert_type': alert.alert_type or '--',
                    'alert_level': alert.alert_level or '--',
                    'description': alert.description or '--',
                    'threshold': threshold,
                    'threshold_source': threshold_source,
                    'threshold_evaluable': threshold_evaluable,
                    'peak_visits': peak_visits,
                    'peak_day_text': peak_day.strftime('%Y-%m-%d') if peak_day else '--',
                    'first_hit_day_text': first_hit_day.strftime('%Y-%m-%d') if first_hit_day else '--',
                    'lead_hours': lead_hours,
                    'observed_ratio': observed_ratio,
                    'outcome': outcome,
                    'outcome_label': outcome_labels[outcome],
                    'severity': severity,
                    'severity_label': severity_labels.get(severity, severity),
                    'certainty': certainty,
                    'certainty_label': certainty_labels.get(certainty, certainty),
                    'urgency': urgency,
                    'urgency_label': urgency_labels.get(urgency, urgency),
                    'probability': probability,
                    'using_global': using_global,
                    'window_points': window_points
                })

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
            if key == '__GLOBAL__':
                day_map = global_daily_visits
                threshold = global_threshold
                key_label = '全局基线'
            else:
                day_map = daily_visits.get(key, {})
                threshold = threshold_by_location.get(key)
                key_label = key
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
        scores = _compute_contingency_scores(
            hit_count=hit_count,
            false_alarm_count=false_alarm_count,
            miss_count=miss_count,
            correct_negative_count=correct_negative_count
        )

        alert_hit_rate = _safe_ratio(hit_alerts, evaluable_alerts)
        alert_far = _safe_ratio(false_alarm_alerts, evaluable_alerts)
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
            roc_auc = _roc_auc_from_pairs(certainty_pairs)

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
            week_auc = _roc_auc_from_pairs(pairs)
            weekly_calibration_rows.append({
                'week_key': week_key,
                'sample_count': sample_count,
                'avg_probability': week_prob_avg,
                'observed_rate': week_obs_rate,
                'brier_score': week_brier,
                'sharpness': week_sharpness,
                'roc_auc': week_auc
            })

        level_rows = []
        for group in level_groups.values():
            level_rows.append({
                'name': group['name'],
                'alerts': group['alerts'],
                'hit': group['hit'],
                'false_alarm': group['false_alarm'],
                'hit_rate': _safe_ratio(group['hit'], group['alerts']),
                'far': _safe_ratio(group['false_alarm'], group['alerts']),
                'avg_lead_hours': (group['lead_sum'] / group['lead_n']) if group['lead_n'] else None
            })
        level_rows = sorted(level_rows, key=lambda item: (item['alerts'], item['hit']), reverse=True)

        type_rows = []
        for group in type_groups.values():
            type_rows.append({
                'name': group['name'],
                'alerts': group['alerts'],
                'hit': group['hit'],
                'false_alarm': group['false_alarm'],
                'hit_rate': _safe_ratio(group['hit'], group['alerts']),
                'far': _safe_ratio(group['false_alarm'], group['alerts']),
                'avg_lead_hours': (group['lead_sum'] / group['lead_n']) if group['lead_n'] else None
            })
        type_rows = sorted(type_rows, key=lambda item: (item['alerts'], item['hit']), reverse=True)

        certainty_rows = []
        for group in certainty_groups.values():
            certainty_rows.append({
                'certainty': group['certainty'],
                'label': group['label'],
                'probability': group['probability'],
                'alerts': group['alerts'],
                'hit': group['hit'],
                'false_alarm': group['false_alarm'],
                'observed_rate': _safe_ratio(group['hit'], group['alerts'])
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
    for q_value in threshold_q_options:
        evaluations[q_value] = evaluate_quantile(
            q_value,
            include_rows=(q_value == threshold_q)
        )
    selected = evaluations[threshold_q]

    def ratio_to_percent(value):
        return (value * 100.0) if value is not None else None

    sensitivity_rows = []
    for q_value in threshold_q_options:
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
    if alert_min_date and alert_max_date and record_min_date and record_max_date and not overlap_exists_all:
        data_notes.append(
            f"预警时间范围 {alert_min_date}~{alert_max_date} 与病例时间范围 {record_min_date}~{record_max_date} 无重叠，命中仅可视为不可核验"
        )

    return render_template(
        'alerts_accuracy.html',
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        location_filter=location_filter,
        alert_type_filter=alert_type_filter,
        alert_level_filter=alert_level_filter,
        follow_days=follow_days,
        min_days=min_days,
        threshold_q=threshold_q,
        follow_days_options=[1, 2, 3, 5, 7],
        min_days_options=[3, 5, 7, 14, 21],
        threshold_q_options=threshold_q_options,
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
            'has_overlap': overlap_exists_all,
            'overlap_start': overlap_start_all,
            'overlap_end': overlap_end_all,
            'alert_min': alert_min_date,
            'alert_max': alert_max_date,
            'record_min': record_min_date,
            'record_max': record_max_date
        }
    )


@bp.route('/reports', endpoint='reports_center')
@login_required
def reports_center():
    """报告导出"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
    return render_template('reports.html')


@bp.route('/reports/export', methods=['POST'], endpoint='reports_export')
@login_required
def reports_export():
    """导出周报/月报"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))
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


@bp.route('/analysis/pilot', endpoint='pilot_dashboard')
@login_required
def pilot_dashboard():
    """试点数据看板（管理员）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))

    days = request.args.get('days', default=30, type=int)
    # 账号级产品事件只保留 30 天，报表不得把短期数据包装成 90 天口径。
    days = max(1, min(days, 30))

    now = utcnow()
    start_ts = now - timedelta(days=days)
    start_7d = now - timedelta(days=7)
    start_30d = now - timedelta(days=30)
    excluded_test_user_ids = {
        int(value)
        for value in str(current_app.config.get('ANALYTICS_TEST_USER_IDS', '')).replace(',', ' ').split()
        if value.isdigit()
    }
    miniprogram_metrics = load_miniprogram_metrics(
        start_ts,
        as_of=now,
        excluded_user_ids=excluded_test_user_ids,
    )

    pair_query = Pair.query.filter(Pair.status == 'active')
    if excluded_test_user_ids:
        pair_query = pair_query.filter(db.or_(
            Pair.caregiver_id.is_(None),
            Pair.caregiver_id.notin_(excluded_test_user_ids),
        ))
    pairs_total = pair_query.count()
    elders_total = pair_query.filter(Pair.member_id.isnot(None)).count()

    # 活跃 caregiver：近N天有 usage_events
    active_7d_query = db.session.query(db.func.count(db.func.distinct(UsageEvent.user_id))).filter(
        UsageEvent.user_id.isnot(None),
        UsageEvent.created_at >= start_7d
    )
    active_30d_query = db.session.query(db.func.count(db.func.distinct(UsageEvent.user_id))).filter(
        UsageEvent.user_id.isnot(None),
        UsageEvent.created_at >= start_30d
    )
    if excluded_test_user_ids:
        active_7d_query = active_7d_query.filter(
            UsageEvent.user_id.notin_(excluded_test_user_ids)
        )
        active_30d_query = active_30d_query.filter(
            UsageEvent.user_id.notin_(excluded_test_user_ids)
        )
    active_7d = active_7d_query.scalar() or 0
    active_30d = active_30d_query.scalar() or 0

    # CTR 只比较确认 sent 的投递与点击，uncertain 点击单独展示并等待复核。
    delivery_query = AlertDelivery.query.filter(AlertDelivery.sent_at >= start_ts)
    if excluded_test_user_ids:
        delivery_query = delivery_query.filter(
            AlertDelivery.user_id.notin_(excluded_test_user_ids)
        )
    sent = delivery_query.filter(AlertDelivery.status == 'sent').count()
    failed = delivery_query.filter(AlertDelivery.status == 'failed').count()
    uncertain = delivery_query.filter(AlertDelivery.status == 'uncertain').count()
    sending = delivery_query.filter(AlertDelivery.status == 'sending').count()
    retry_ready = delivery_query.filter(AlertDelivery.status == 'retry_ready').count()
    clicked = delivery_query.filter(
        AlertDelivery.status == 'sent',
        AlertDelivery.clicked_at.isnot(None),
    ).count()
    uncertain_clicked = delivery_query.filter(
        AlertDelivery.status == 'uncertain',
        AlertDelivery.clicked_at.isnot(None),
    ).count()
    ctr = round(clicked / sent, 4) if sent else 0.0
    review_deliveries = delivery_query.filter(
        AlertDelivery.status.in_(('sending', 'failed', 'uncertain', 'retry_ready'))
    ).order_by(
        AlertDelivery.sent_at.desc(),
        AlertDelivery.id.desc(),
    ).limit(50).all()
    review_required_count = sum(
        delivery.status in {'sending', 'uncertain'}
        or (delivery.status == 'failed' and not delivery.review_action)
        for delivery in review_deliveries
    )

    usage_filter = []
    if excluded_test_user_ids:
        usage_filter.append(db.or_(
            UsageEvent.user_id.is_(None),
            UsageEvent.user_id.notin_(excluded_test_user_ids),
        ))
    template_copy = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'template_copy',
        *usage_filter,
    ).count()
    template_users = db.session.query(db.func.count(db.func.distinct(UsageEvent.user_id))).filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'template_copy',
        UsageEvent.user_id.isnot(None),
        *usage_filter,
    ).scalar() or 0

    feedback_count = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'feedback_submitted',
        *usage_filter,
    ).count()

    wxoa_land = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts,
        UsageEvent.event_type == 'wxoa_land',
        *usage_filter,
    ).count()

    # 地区卡片只展示社区编码的聚合结果，避免把自由输入地址带进运营看板。
    location_min_count = max(
        3,
        int(current_app.config.get('ANALYTICS_MIN_LOCATION_COUNT', 3) or 3),
    )
    location_expr = Pair.community_code.label('location')
    cnt_expr = db.func.count(Pair.id).label('cnt')
    location_query = db.session.query(
        location_expr,
        cnt_expr
    ).filter(
        Pair.status == 'active'
    )
    if excluded_test_user_ids:
        location_query = location_query.filter(
            Pair.caregiver_id.notin_(excluded_test_user_ids)
        )
    location_rows = location_query.group_by(location_expr).having(
        cnt_expr >= location_min_count
    ).order_by(cnt_expr.desc(), location_expr.asc()).limit(20).all()
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
        push_uncertain=uncertain,
        push_sending=sending,
        push_retry_ready=retry_ready,
        push_clicked=clicked,
        push_uncertain_clicked=uncertain_clicked,
        push_ctr=ctr,
        review_deliveries=review_deliveries,
        review_required_count=review_required_count,
        template_copy=template_copy,
        template_users=template_users,
        feedback_count=feedback_count,
        wxoa_land=wxoa_land,
        location_coverage=location_coverage,
        location_min_count=location_min_count,
        miniprogram_metrics=miniprogram_metrics,
    )


@bp.route('/analysis/pilot/deliveries/<int:delivery_id>/review', methods=['POST'], endpoint='pilot_review_delivery')
@login_required
def pilot_review_delivery(delivery_id):
    """由管理员确认不明确投递，所有动作保留在投递记录和审计日志中。"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))

    action = str(request.form.get('action') or '').strip()
    if action not in {'confirm_sent', 'confirm_failed', 'allow_retry'}:
        flash('不支持的投递复核动作', 'error')
        return redirect(url_for('analysis.pilot_dashboard', days=30))

    delivery = db.session.get(AlertDelivery, delivery_id)
    if delivery is None:
        flash('投递记录不存在', 'error')
        return redirect(url_for('analysis.pilot_dashboard', days=30))
    if action == 'allow_retry' and delivery.status not in {'failed', 'uncertain'}:
        flash('当前状态不允许重新发送', 'error')
        return redirect(url_for('analysis.pilot_dashboard', days=30))

    previous_status = delivery.status
    reviewed_at = utcnow()
    if action == 'confirm_sent':
        delivery.status = 'sent'
        delivery.error = None
        delivery.sent_at = delivery.sent_at or reviewed_at
        message = '已确认送达'
    elif action == 'confirm_failed':
        delivery.status = 'failed'
        delivery.error = '管理员已确认本次未送达；未授权自动重试'
        message = '已确认未送达'
    else:
        delivery.status = 'retry_ready'
        delivery.error = '管理员已确认本次未送达；允许下一轮重新发送一次'
        message = '已允许下一轮重新发送'
    delivery.reviewed_at = reviewed_at
    delivery.reviewed_by_user_id = current_user.id
    delivery.review_action = action
    log_audit(
        'pilot_delivery_review',
        resource_type='alert_delivery',
        resource_id=delivery.id,
        metadata={
            'action': action,
            'previous_status': previous_status,
            'next_status': delivery.status,
        },
    )
    db.session.commit()
    flash(message, 'success')
    days = max(1, min(request.form.get('days', default=30, type=int), 30))
    return redirect(url_for('analysis.pilot_dashboard', days=days))


@bp.route('/analysis/pilot/export.csv', endpoint='pilot_export_csv')
@login_required
def pilot_export_csv():
    """按本地日期、事件类型和来源导出匿名聚合 CSV。"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))

    days = request.args.get('days', default=30, type=int)
    days = max(1, min(days, 30))
    start_ts = utcnow() - timedelta(days=days)
    excluded_test_user_ids = {
        int(value)
        for value in str(
            current_app.config.get('ANALYTICS_TEST_USER_IDS', '')
        ).replace(',', ' ').split()
        if value.isdigit()
    }

    event_query = UsageEvent.query.filter(
        UsageEvent.created_at >= start_ts
    )
    if excluded_test_user_ids:
        event_query = event_query.filter(
            db.or_(
                UsageEvent.user_id.is_(None),
                UsageEvent.user_id.notin_(excluded_test_user_ids),
            )
        )

    buckets = defaultdict(int)
    events = event_query.order_by(
        UsageEvent.created_at.asc(),
        UsageEvent.id.asc(),
    ).all()
    for event in events:
        local_date = utc_to_local_date(event.created_at)
        if local_date is None:
            continue
        buckets[(
            local_date.isoformat(),
            event.event_type or '',
            event.source or 'unknown',
        )] += 1

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['local_date', 'event_type', 'source', 'event_count'])
    for (local_date, event_type, source), count in sorted(buckets.items()):
        writer.writerow([local_date, event_type, source, count])

    data = out.getvalue().encode('utf-8-sig')  # Excel-friendly
    return send_file(
        io.BytesIO(data),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'pilot_events_last_{days}d.csv',
    )


@bp.route('/analysis/model-quality', endpoint='model_quality')
@login_required
def model_quality():
    """模型可靠性（护栏 + 回测报告）"""
    if not _require_admin():
        return redirect(url_for('user.user_dashboard'))

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
