# -*- coding: utf-8 -*-
"""分析页共用的纯统计函数（不依赖 Flask 与数据库）。"""
import json
import math
from datetime import timedelta

from core.analytics import pearson_corr

STRATUM_LABELS = {
    'all': '全人群',
    'elderly': '老年人(>=65)',
    'non_elderly': '非老年(<65)',
    'male': '男性',
    'female': '女性'
}
OUTCOME_LABELS = {
    'hit': '命中',
    'false_alarm': '空报',
    'insufficient': '样本不足'
}
SEVERITY_LABELS = {
    'Extreme': '极高',
    'Severe': '高',
    'Moderate': '中',
    'Minor': '低',
    'Unknown': '未知'
}
CERTAINTY_LABELS = {
    'Observed': '已发生',
    'Likely': '较可能',
    'Possible': '可能',
    'Unlikely': '较不可能',
    'Unknown': '未知'
}
URGENCY_LABELS = {
    'Immediate': '立即',
    'Expected': '预期',
    'Future': '后续',
    'Past': '已过',
    'Unknown': '未知'
}


def date_span(start_date, end_date):
    """闭区间内的逐日日期列表。"""
    days = []
    cursor = start_date
    while cursor <= end_date:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def is_significant(rr, days, min_days, ci_low, ci_high):
    """样本天数足够且 95% 置信区间不跨 1。"""
    return bool(
        rr is not None and
        days >= min_days and
        ci_low is not None and
        ci_high is not None and
        (ci_low > 1 or ci_high < 1)
    )


def safe_int(raw_value, default, minimum=None, maximum=None):
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


def normalize_gender(raw_gender):
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


def record_matches_stratum(age, gender, stratum):
    if stratum == 'all':
        return True
    if stratum == 'elderly':
        return age is not None and age >= 65
    if stratum == 'non_elderly':
        return age is not None and age < 65
    normalized = normalize_gender(gender)
    if stratum == 'male':
        return normalized == 'male'
    if stratum == 'female':
        return normalized == 'female'
    return True


def build_daily_weather(records):
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


def lag_exposure_for_date(target_date, lag_window, weather_by_date):
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


def find_bin(value, bins):
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


def format_bucket_label(left, right, unit):
    def fmt(num):
        if num is None:
            return '--'
        if abs(num - round(num)) < 0.05:
            return str(int(round(num)))
        return f"{num:.1f}"

    return f"{fmt(left)}~{fmt(right)}{unit}"


def percentile(values, q):
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


def gini(values):
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


def roc_auc_from_pairs(pairs):
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


def build_quantile_bins(values, bucket_count, fallback_bins):
    valid = sorted(
        value for value in values
        if isinstance(value, (int, float)) and math.isfinite(value)
    )
    if len(valid) < bucket_count * 3:
        return fallback_bins

    edges = []
    for idx in range(bucket_count + 1):
        edge = percentile(valid, idx / bucket_count)
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


def rr_with_ci(observed, expected):
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


def corr_with_ci(xs, ys):
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


def certainty_level(days, visits, ci_low, ci_high, min_days):
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


def action_level(rr, significant, certainty, days, min_days):
    if rr is None or days < min_days:
        return '样本不足'
    if rr >= 1.6 and significant and certainty == 'high':
        return '立即行动'
    if rr >= 1.3 and (significant or certainty in {'high', 'medium'}):
        return '准备干预'
    if rr <= 0.75 and significant:
        return '观察（低风险）'
    return '观察'


def heatmap_cell_color(rr, days, min_days):
    if rr is None or days < min_days:
        return 'rgba(148, 163, 184, 0.18)'
    capped = max(0.4, min(2.4, rr))
    if capped >= 1:
        alpha = 0.18 + 0.5 * ((capped - 1.0) / 1.4)
        return f"rgba(201, 72, 72, {alpha:.3f})"
    alpha = 0.18 + 0.5 * ((1.0 - capped) / 0.6)
    return f"rgba(52, 120, 189, {alpha:.3f})"


def json_loads_safe(raw_text, default):
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


def alert_cap_semantics(alert_level, alert_type, description):
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


def impact_bucket_from_severity(severity):
    if severity in {'Extreme', 'Severe'}:
        return 'high'
    if severity == 'Moderate':
        return 'medium'
    return 'low'


def likelihood_bucket_from_certainty(certainty):
    if certainty in {'Observed', 'Likely'}:
        return 'high'
    if certainty == 'Possible':
        return 'medium'
    return 'low'


def action_from_alert_semantics(severity, certainty, urgency):
    if severity in {'Extreme', 'Severe'} and urgency in {'Immediate', 'Expected'} and certainty in {'Observed', 'Likely'}:
        return '立即行动'
    if severity in {'Moderate', 'Severe'} and certainty in {'Possible', 'Likely', 'Observed'}:
        return '准备干预'
    if severity == 'Minor':
        return '加强观察'
    return '持续观察'


def safe_ratio(numerator, denominator):
    if denominator is None or denominator == 0:
        return None
    return numerator / denominator


def compute_contingency_scores(hit_count, false_alarm_count, miss_count, correct_negative_count):
    total = hit_count + false_alarm_count + miss_count + correct_negative_count
    pod = safe_ratio(hit_count, hit_count + miss_count)
    far = safe_ratio(false_alarm_count, hit_count + false_alarm_count)
    csi = safe_ratio(hit_count, hit_count + false_alarm_count + miss_count)
    accuracy = safe_ratio(hit_count + correct_negative_count, total)
    bias = safe_ratio(hit_count + false_alarm_count, hit_count + miss_count)
    pofd = safe_ratio(false_alarm_count, false_alarm_count + correct_negative_count)
    tss = (pod - pofd) if pod is not None and pofd is not None else None
    f1 = safe_ratio(2 * hit_count, 2 * hit_count + false_alarm_count + miss_count)

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


def certainty_to_probability(certainty):
    mapping = {
        'Observed': 0.95,
        'Likely': 0.80,
        'Possible': 0.60,
        'Unlikely': 0.35,
        'Unknown': 0.50
    }
    return mapping.get(certainty, 0.50)


def compute_date_overlap(start_a, end_a, start_b, end_b):
    if not all([start_a, end_a, start_b, end_b]):
        return None, None, False
    overlap_start = max(start_a, start_b)
    overlap_end = min(end_a, end_b)
    return overlap_start, overlap_end, overlap_start <= overlap_end
