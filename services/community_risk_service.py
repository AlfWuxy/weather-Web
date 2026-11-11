# -*- coding: utf-8 -*-
"""
模块二：社区风险评估服务（改进版）

功能：
C1. 社区档案管理
C2. 脆弱性指数(VI)计算 - 使用可审计的线性指数
C3. 社区风险得分 & 地图生成
C4. Top N高风险社区清单
C5. 管控建议生成（医生端）

公式：
VI_c = 1 + a*老龄率 + b*慢病率 - d*绿地率 + ...

或使用回归模型：
log E[Y_{c,t}] = α + cb(Temp_t, lag) + s(time) + DOW + u_c + v_c · Heat_t
- u_c: 社区基线就诊水平差异
- v_c: 社区对高温（或寒冷）的额外敏感性（"天气脆弱性"）
"""
import hashlib
import math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os
from flask import current_app, has_app_context, has_request_context


_PUBLIC_HEALTH_STAT_FIELDS = (
    'population',
    'elderly_ratio',
    'chronic_disease_ratio',
    'vulnerability_index',
    'vulnerability_level',
    'vulnerability_details',
    'risk_level',
    'baseline_visits',
)

# 这些字段能单独或互相回推出村级就诊整数。
_CASE_COUNT_FIELDS = (
    'observed_cases',
    'expected_cases',
    'sir',
    'ci_low',
    'ci_high',
    'smoothed_sir',
    'probability_exceed_baseline',
)

# 风险地图排名行上的人口学/脆弱性精确值。小样本、公开或未授权时不返回。
_RANKING_DEMOGRAPHIC_FIELDS = (
    'population',
    'elderly_ratio',
    'chronic_disease_ratio',
    'vulnerability_index',
)

# Excess = max(WeatherRR-1, 0) × VI × BaselineVisits。这些分量留在抑制行里就能回推 VI。
_HAZARD_FORMULA_RECONSTRUCTABLE_FIELDS = (
    'weather_rr',
    'vi',
    'baseline_visits',
    'excess',
    'hazard',
    'efold',
)

_SUPPRESSED_HAZARD_FORMULA_NOTE = '精确脆弱性与超额风险分量已按权限或最小样本抑制'

# 公开排名把 normalized_score 放在 risk_score 上。它和 weather_hazard_score
# 都是 Excess 的可逆危险度；relative_index 与原始 RiskScore=RR×VI×Baseline 成比例。
# 这些字段配公开 macro RR 和固定 baseline 能还原 VI。排名与综合预警 top_risk 共用同一行。
_VI_RECONSTRUCTABLE_SCORE_FIELDS = (
    'expected_excess_visits',
    'normalized_score',
    'weather_hazard_score',
    'relative_index',
)

# risk_index/uncertainty_penalty - w_weather*weather_hazard_score - w_burden*burden_percentile
# 再除以 w_svi，可还原 svi_percentile。抑制行上这组输入必须同时失效。
_SUPPRESSED_DECOMPOSITION_FIELDS = (
    'risk_index',
    'risk_score',
    'weather_hazard_score',
    'burden_percentile',
    'uncertainty_penalty',
    'risk_weights',
    'relative_index',
    'expected_excess_visits',
)


def _community_min_sample_size():
    """最小公布样本。优先读 community_access，未提供时用应用配置。"""
    try:
        from core.community_access import community_min_sample_size
    except ImportError:
        community_min_sample_size = None
    if community_min_sample_size is not None:
        try:
            return int(community_min_sample_size())
        except Exception:
            pass
    default = 10
    if has_app_context():
        try:
            return int(current_app.config.get('COMMUNITY_MIN_SAMPLE_SIZE', default))
        except Exception:
            return default
    return default


def _can_view_community_health_stats(community_name):
    """当前请求能否查看该村健康统计。辅助未就绪时按患者社区范围等价判断。"""
    try:
        from core.community_access import can_view_community_health_stats
    except ImportError:
        can_view_community_health_stats = None
    if can_view_community_health_stats is not None:
        try:
            return bool(can_view_community_health_stats(community_name))
        except Exception:
            return False
    try:
        from core.community_access import patient_community_scope
        scope = patient_community_scope()
    except Exception:
        return False
    if scope is None:
        return True
    try:
        return bool(community_name) and community_name in scope
    except TypeError:
        return False


def _sample_below_minimum(raw_population, min_sample):
    if raw_population is None:
        return True
    try:
        return float(raw_population) < float(min_sample)
    except (TypeError, ValueError):
        return True


def _community_stats_min_n():
    """就诊计数的 k-匿名阈值。

    显式 COMMUNITY_STATS_MIN_N 优先，否则沿用社区统计最小样本。
    """
    raw = os.environ.get('COMMUNITY_STATS_MIN_N')
    if has_app_context():
        try:
            configured = current_app.config.get('COMMUNITY_STATS_MIN_N')
        except Exception:
            configured = None
        if configured is not None:
            raw = configured
    if raw is not None and str(raw).strip() != '':
        try:
            return max(int(str(raw).strip()), 1)
        except (TypeError, ValueError):
            pass
    return _community_min_sample_size()


def _case_count_sample_size(profile):
    """公开判断用原始人口。缺失的 raw_population 不能被代理人口 100 抬过阈值。"""
    if not isinstance(profile, dict):
        return None
    if 'raw_population' in profile:
        return profile.get('raw_population')
    return profile.get('population')


def _case_count_publication(community_name, sample_size):
    """决定一个村的就诊计数能否出现在风险地图结果里。

    小于 k 的样本一律不公布精确整数，包括已授权请求和预计算缓存。
    样本达到阈值时，管理员和已授权社区保留精确值；请求内的未授权查看者仍隐藏。
    无请求上下文的内部计算只对小样本失败关闭，避免把大样本计数从预计算里抹掉。
    """
    below = _sample_below_minimum(sample_size, _community_stats_min_n())
    if not has_request_context():
        authorized = None
    else:
        authorized = _can_view_community_health_stats(community_name)
    if below:
        return {'hide': True, 'suppressed': True, 'redacted': authorized is False}
    if authorized is False:
        return {'hide': True, 'suppressed': False, 'redacted': True}
    return {'hide': False, 'suppressed': False, 'redacted': False}


def _ranking_demographics_hidden(community_name, sample_size):
    """排名行是否隐藏人口学和脆弱性精确值。

    公开、未授权，或样本小于 k 时隐藏。
    无请求上下文只隐藏小样本，避免预计算把达标社区从授权缓存里抹掉。
    """
    if _sample_below_minimum(sample_size, _community_stats_min_n()):
        return True
    if not has_request_context():
        return False
    return not _can_view_community_health_stats(community_name)


def _clear_risk_contributions(container):
    """就地清空贡献表。地图要素与排名行共用同一字典时一起失效。"""
    if not isinstance(container, dict):
        return
    contributions = container.get('risk_contributions')
    if isinstance(contributions, dict):
        contributions.clear()
        return
    container['risk_contributions'] = {}


def _is_real_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sum_published_excess(rows):
    """只加仍公开的预计超额就诊。没有可公布的数时返回 None，避免用合计做减法。"""
    total = 0.0
    saw_number = False
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = row.get('expected_excess_visits')
        if not _is_real_number(value):
            continue
        total += float(value)
        saw_number = True
    if not saw_number:
        return None
    return total


def _null_suppressed_decomposition(container):
    """清掉能线性还原 svi_percentile 的风险分解。排名行与地图 properties 共用。"""
    if not isinstance(container, dict):
        return
    for field in _SUPPRESSED_DECOMPOSITION_FIELDS:
        container[field] = None


def _suppress_reconstructable_risk_fields(row):
    """抑制行去掉可回推 VI 的公式分量，以及能解出 SVI 分位的风险分解。

    expected_excess_visits、危险度（公开 risk_score / weather_hazard_score）
    和 relative_index 留在抑制行上时，配公开 macro RR 与固定 baseline 能还原 VI。
    risk_index、uncertainty_penalty、burden_percentile、risk_weights 再配危险度，
    可还原 svi_percentile。排名和综合预警的 top_risk 使用同一批行，所以一并置空。
    """
    formula = row.get('hazard_formula')
    if isinstance(formula, dict):
        formula['expression'] = _SUPPRESSED_HAZARD_FORMULA_NOTE
        for field in _HAZARD_FORMULA_RECONSTRUCTABLE_FIELDS:
            formula[field] = None
    row['theme_scores'] = None
    row['svi_percentile'] = None
    # (after_penalty - weather - burden) / risk_weights['svi'] 可还原 svi_percentile。
    # svi、before_penalty 单独也能还原。整表清空后这组方程缺项。
    _clear_risk_contributions(row)
    # Q1–Q4 是 SVI 的分位桶，能给风险指数回推结果卡在 25/50/75 边界上。
    row['equity_stratum'] = None
    # 分位名次是抑制前的风险排序键。留下它就能把公开名单重新排回风险顺序。
    row['percentile_rank'] = None
    for field in _VI_RECONSTRUCTABLE_SCORE_FIELDS:
        row[field] = None
    # 原始 RiskScore = RR×VI×Baseline。公开字段 risk_score 取的是 normalized_score。
    row['risk_score'] = None
    _null_suppressed_decomposition(row)
    components = row.get('components')
    if isinstance(components, dict):
        for field in ('vulnerability_index', 'excess_risk_score', 'baseline_rate'):
            components[field] = None


def _public_ranking_order_key(row):
    """抑制行只按社区名排。仍公开的行按风险指数，同分再按名称。"""
    name = str(row.get('community') or '')
    if row.get('suppressed'):
        return (1, name)
    risk = row.get('risk_index')
    if not _is_real_number(risk):
        risk = 0.0
    return (0, -float(risk), name)


def _order_public_risk_lists(rankings, map_data, layers):
    """有抑制行时，公开顺序不能再沿用综合风险名次。"""
    if not rankings or not any(row.get('suppressed') for row in rankings):
        return
    rankings.sort(key=_public_ranking_order_key)
    for idx, row in enumerate(rankings, start=1):
        row['rank'] = idx
    order = {row.get('community'): idx for idx, row in enumerate(rankings)}

    features = (map_data or {}).get('features')
    if isinstance(features, list):
        features.sort(
            key=lambda feature: order.get(
                (feature.get('properties') or {}).get('name'),
                len(order),
            )
        )
    for entries in (layers or {}).values():
        if not isinstance(entries, list):
            continue
        entries.sort(
            key=lambda entry: order.get(
                entry.get('community') if isinstance(entry, dict) else None,
                len(order),
            )
        )


def _scrub_suppressed_quartile_risk(quartile_rows, strata_map):
    """分位均值不能单独留下被抑制社区的 risk_index，也不能用全体均值做减法。"""
    for quartile in quartile_rows or []:
        members = (strata_map or {}).get(quartile.get('stratum'), [])
        if not any(isinstance(row, dict) and row.get('suppressed') for row in members):
            continue
        visible_scores = [
            float(row['risk_index'])
            for row in members
            if isinstance(row, dict)
            and not row.get('suppressed')
            and _is_real_number(row.get('risk_index'))
        ]
        quartile['avg_risk_index'] = (
            round(float(np.mean(visible_scores)), 1) if visible_scores else None
        )


def _republish_visible_relative_index(rankings, hidden_names):
    """相对指数只在仍公开的 RiskScore 上重算，避免用全体均值减出被抑制的 VI。"""
    hidden_names = set(hidden_names or [])
    visible = [
        row for row in rankings
        if row.get('community') not in hidden_names and _is_real_number(row.get('risk_score'))
    ]
    if not visible:
        return
    scores = np.array([float(row['risk_score']) for row in visible], dtype=float)
    mean_score = float(np.mean(scores)) if scores.size else 0.0
    for row, score in zip(visible, scores):
        row['relative_index'] = (
            round((float(score) / mean_score * 100.0), 1) if mean_score > 0 else 100.0
        )


def _public_vulnerability_sort_key(item):
    """只比较裁剪后的脆弱性指数。缺失值并列，避免用原名次泄漏。"""
    vi = item.get('vulnerability_index')
    if vi is None:
        return (1, 0.0)
    try:
        return (0, -float(vi))
    except (TypeError, ValueError):
        return (1, 0.0)


class CommunityRiskService:
    """社区风险评估服务"""
    
    def __init__(self):
        # 风险分数归一化参数（使用“超额风险”避免全量顶格）
        self.excess_score_efold = self._read_float_env(
            'COMMUNITY_RISK_EXCESS_EFOLD',
            default=10.0,
            min_value=0.1
        )
        self.baseline_visit_rate = self._read_float_env(
            'COMMUNITY_BASELINE_VISIT_RATE',
            default=0.03,
            min_value=0.001
        )
        self.min_baseline_visits = self._read_float_env(
            'COMMUNITY_MIN_BASELINE_VISITS',
            default=1.0,
            min_value=0.1
        )
        self.max_baseline_visits = self._read_float_env(
            'COMMUNITY_MAX_BASELINE_VISITS',
            default=20.0,
            min_value=1.0
        )
        self.risk_level_thresholds = {
            'high': 75,
            'medium': 45
        }
        # VI权重参数（可审计、可调整）
        self.vi_weights = {
            'elderly_ratio': 1.5,      # 老龄率权重
            'chronic_disease_ratio': 1.8,  # 慢病率权重
            'green_space_ratio': -0.8,  # 绿地率权重（负向）
            'heat_island_index': 0.5,   # 热岛效应权重
            'medical_accessibility': -0.3  # 医疗可达性权重（负向）
        }
        
        # 社区敏感性参数（v_c）
        self.community_sensitivity = {}
        
        # 加载社区数据
        self._load_community_profiles()

    def _read_float_env(self, key, default, min_value=None):
        """读取浮点型环境变量并做基础范围保护。"""
        raw = os.getenv(key)
        if raw is None:
            value = float(default)
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = float(default)
        if min_value is not None:
            value = max(float(min_value), value)
        return value

    @staticmethod
    def _stable_unit_value(community_name, field_name):
        """按社区标识生成稳定的 0-1 数值，避免进程重启后代理值漂移。"""
        seed = f'{community_name or "未命名社区"}:{field_name}'.encode('utf-8')
        digest = hashlib.sha256(seed).digest()
        return int.from_bytes(digest[:8], byteorder='big') / float((1 << 64) - 1)

    def _stable_proxy_profile(self, community_name):
        """生成窄范围、中性且可复现的社区代理字段。

        这些值只用于真实字段缺失时维持页面与计算链可用，不能视为实测数据。
        各范围围绕现有中性默认值轻微变化，既保留社区区分，也避免代理值主导风险排序。
        """
        def around(field_name, center, half_width, digits=4):
            unit = self._stable_unit_value(community_name, field_name)
            return round(center + (unit - 0.5) * half_width * 2.0, digits)

        return {
            'latitude': around('latitude', 29.35, 0.035, digits=6),
            'longitude': around('longitude', 116.37, 0.035, digits=6),
            'green_space_ratio': around('green_space_ratio', 0.10, 0.02),
            'heat_island_index': around('heat_island_index', 0.50, 0.05),
            'medical_accessibility': around('medical_accessibility', 0.60, 0.05),
        }

    def _estimate_baseline_visits(self, population):
        """按人口估算社区日基线门诊，替代固定常数。"""
        try:
            pop = float(population) if population is not None else 100.0
        except (TypeError, ValueError):
            pop = 100.0
        pop = max(10.0, pop)
        estimated = pop * self.baseline_visit_rate
        return float(np.clip(estimated, self.min_baseline_visits, self.max_baseline_visits))

    def _normalize_excess_risk(self, excess_risk_score):
        """把超额风险映射到0-100，避免线性缩放导致快速打满。"""
        try:
            excess = float(excess_risk_score)
        except (TypeError, ValueError):
            excess = 0.0
        if excess <= 0:
            return 0.0
        normalized = (1.0 - np.exp(-excess / self.excess_score_efold)) * 100.0
        return float(np.clip(normalized, 0.0, 100.0))

    def _extract_lag_temperatures(self, weather_data, current_temperature):
        """从输入中提取滞后温度序列，优先使用显式lag字段。"""
        candidate_keys = (
            'lag_temperatures',
            'temperature_lags',
            'temperature_history',
            'historical_temperatures'
        )
        for key in candidate_keys:
            values = weather_data.get(key)
            if not isinstance(values, (list, tuple)) or not values:
                continue
            lag_temps = []
            for value in values:
                try:
                    lag_temps.append(float(value))
                except (TypeError, ValueError):
                    continue
            if not lag_temps:
                continue
            if abs(lag_temps[0] - current_temperature) > 0.01:
                lag_temps.insert(0, current_temperature)
            return lag_temps
        return None

    def _percentile_map(self, values_by_key):
        """计算每个键对应值的分位（0-100，含并列修正）。"""
        keys = list(values_by_key.keys())
        if not keys:
            return {}
        values = np.array([float(values_by_key[key]) for key in keys], dtype=float)
        result = {}
        for key, value in values_by_key.items():
            value = float(value)
            less_count = np.sum(values < value)
            equal_count = np.sum(values == value)
            percentile = (less_count + 0.5 * equal_count) / values.size * 100
            result[key] = float(np.clip(percentile, 0.0, 100.0))
        return result

    def _rr_with_ci(self, observed, expected):
        """Poisson近似 RR 与 95%CI。"""
        if expected is None or expected <= 0:
            return None, None, None

        observed = max(int(observed or 0), 0)
        expected = float(expected)
        if observed == 0:
            return 0.0, 0.0, 3.0 / expected

        rr = observed / expected
        se = 1.0 / math.sqrt(observed)
        ci_low = math.exp(math.log(max(rr, 1e-9)) - 1.96 * se)
        ci_high = math.exp(math.log(max(rr, 1e-9)) + 1.96 * se)
        return rr, ci_low, ci_high

    def _probability_rr_above_one(self, rr, observed):
        """近似计算 P(RR>1)，用于概率化表达。"""
        if rr is None:
            return 0.5
        observed = max(int(observed or 0), 0)
        if observed == 0:
            return 0.05
        rr = max(float(rr), 1e-9)
        se = 1.0 / math.sqrt(observed)
        z = math.log(rr) / se
        # 标准正态CDF
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    def _haversine_distance_m(self, coord_a, coord_b):
        """两点球面距离（米）。"""
        lon1, lat1 = coord_a
        lon2, lat2 = coord_b
        lon1 = math.radians(float(lon1))
        lat1 = math.radians(float(lat1))
        lon2 = math.radians(float(lon2))
        lat2 = math.radians(float(lat2))
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        aa = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return 2 * 6371000 * math.asin(math.sqrt(aa))

    def _compute_hotspot_stats(self, rows):
        """基于 Getis-Ord Gi* 思路给出热点显著性分类。"""
        for row in rows:
            if row.get('historical_component_available'):
                row['hotspot_z'] = None
                row['hotspot_p'] = None
                row['hotspot_category'] = '样本不足'
            else:
                row['hotspot_z'] = None
                row['hotspot_p'] = None
                row['hotspot_category'] = '数据不足'

        valid_rows = [
            row for row in rows
            if row.get('historical_component_available')
            and (row.get('smoothed_sir') is not None or row.get('sir') is not None)
            if row.get('longitude') is not None and row.get('latitude') is not None
        ]
        if len(valid_rows) < 3:
            return

        coords = [(row['longitude'], row['latitude']) for row in valid_rows]
        values = []
        for row in valid_rows:
            value = row.get('smoothed_sir')
            if value is None:
                value = row.get('sir')
            values.append(float(value))
        values = np.array(values, dtype=float)

        mean_x = float(np.mean(values))
        std_x = float(np.std(values))
        if std_x <= 1e-9:
            for row in valid_rows:
                row['hotspot_z'] = 0.0
                row['hotspot_p'] = 1.0
                row['hotspot_category'] = '无显著'
            return

        nearest_distances = []
        for i, coord in enumerate(coords):
            dists = []
            for j, target in enumerate(coords):
                if i == j:
                    continue
                dists.append(self._haversine_distance_m(coord, target))
            if dists:
                nearest_distances.append(min(dists))
        if nearest_distances:
            neighbor_radius = float(np.median(nearest_distances) * 1.6)
        else:
            neighbor_radius = 1200.0
        neighbor_radius = max(300.0, min(neighbor_radius, 5000.0))

        n = len(valid_rows)
        for i, row in enumerate(valid_rows):
            weights = []
            for j, target in enumerate(coords):
                dist = self._haversine_distance_m(coords[i], target)
                weight = 1.0 if dist <= neighbor_radius else 0.0
                if i == j:
                    weight = 1.0
                weights.append(weight)

            if sum(weights) <= 1.0:
                # 防止孤立点导致 Gi* 不稳定：至少连接最近邻。
                nearest_j = None
                nearest_dist = None
                for j, target in enumerate(coords):
                    if i == j:
                        continue
                    dist = self._haversine_distance_m(coords[i], target)
                    if nearest_dist is None or dist < nearest_dist:
                        nearest_dist = dist
                        nearest_j = j
                if nearest_j is not None:
                    weights[nearest_j] = 1.0

            sum_w = float(sum(weights))
            sum_w2 = float(sum(weight * weight for weight in weights))
            denom_term = (n * sum_w2 - sum_w ** 2) / max(n - 1, 1)
            denom = std_x * math.sqrt(max(denom_term, 0.0))

            numerator = float(np.dot(weights, values) - mean_x * sum_w)
            z_score = numerator / denom if denom > 1e-9 else 0.0
            p_value = math.erfc(abs(z_score) / math.sqrt(2.0))

            if z_score >= 2.58:
                category = '热点(99%)'
            elif z_score >= 1.96:
                category = '热点(95%)'
            elif z_score <= -2.58:
                category = '冷点(99%)'
            elif z_score <= -1.96:
                category = '冷点(95%)'
            else:
                category = '无显著'

            row['hotspot_z'] = round(z_score, 3)
            row['hotspot_p'] = round(p_value, 4)
            row['hotspot_category'] = category

    def _to_four_level_bucket(self, score_value):
        """把0-100连续分数映射到 low/medium/high/very_high。"""
        score_value = float(np.clip(score_value, 0.0, 100.0))
        if score_value >= 75:
            return 'very_high'
        if score_value >= 55:
            return 'high'
        if score_value >= 35:
            return 'medium'
        return 'low'

    def _heatrisk_level_from_index(self, risk_index):
        """把项目综合风险指数映射为 0-4 档位。"""
        risk_index = float(np.clip(risk_index, 0.0, 100.0))
        if risk_index >= 80:
            level = 4
            label = '极高'
            color = '#7f1d1d'
        elif risk_index >= 60:
            level = 3
            label = '高'
            color = '#dc2626'
        elif risk_index >= 40:
            level = 2
            label = '中等'
            color = '#f59e0b'
        elif risk_index >= 20:
            level = 1
            label = '轻微'
            color = '#84cc16'
        else:
            level = 0
            label = '最小'
            color = '#16a34a'
        return level, label, color

    def _collect_medical_counts(self, end_date, window_days, disease_filter='', community_scope=None):
        """拉取窗口期社区病例计数，用于 SIR 与不确定性估计。"""
        window_days = max(1, int(window_days))
        start_date = end_date - timedelta(days=window_days - 1)
        summary = {
            'start_date': start_date,
            'end_date': end_date,
            'window_days': window_days,
            'counts_by_community': {},
            'matched_records': 0,
            'total_records': 0,
            'unmatched_records': 0
        }
        if not has_app_context():
            return summary

        try:
            from core.db_models import MedicalRecord
            from core.time_utils import date_to_utc_start, date_to_utc_end
        except Exception:
            return summary

        query = MedicalRecord.query.filter(
            MedicalRecord.visit_time.isnot(None),
            MedicalRecord.visit_time >= date_to_utc_start(start_date),
            MedicalRecord.visit_time <= date_to_utc_end(end_date)
        )
        if disease_filter:
            query = query.filter(MedicalRecord.disease_category == disease_filter)

        if community_scope is not None:
            query = query.filter(MedicalRecord.community.in_(community_scope))
        rows = query.with_entities(MedicalRecord.community).all()
        counts = {}
        total_records = 0
        matched_records = 0
        unmatched_records = 0

        for row in rows:
            total_records += 1
            community = (row.community or '').strip()
            if not community or community not in self.community_profiles:
                unmatched_records += 1
                continue
            counts[community] = counts.get(community, 0) + 1
            matched_records += 1

        summary['counts_by_community'] = counts
        summary['matched_records'] = matched_records
        summary['total_records'] = total_records
        summary['unmatched_records'] = unmatched_records
        return summary
    
    def _load_community_profiles(self):
        """加载社区档案数据。

        Flask 应用上下文中只信任 Community 表。空表或查询失败时保持空集，
        避免内置演示村落被误发布为正式排名。无应用上下文的纯离线运行保留演示档案。
        """
        self.community_profiles = {}

        if not has_app_context():
            self._setup_default_communities()
            self.community_profile_status = {
                'available': True,
                'code': 'offline_demo',
                'source': 'built_in_demo_profiles',
                'message': '当前为无 Flask 应用上下文的离线演示档案。',
            }
            return

        try:
            from core.db_models import Community

            communities = Community.query.all()
            if not communities:
                self.community_profile_status = {
                    'available': False,
                    'code': 'community_table_empty',
                    'source': 'community_table',
                    'message': 'Community 表暂无社区档案，本次不生成社区风险排名。',
                }
                return

            # 先组装局部结果。任一档案读取异常时整批保持为空，避免发布半套排名。
            loaded_profiles = {}
            for comm in communities:
                population = comm.population or 100
                proxy = self._stable_proxy_profile(comm.name)
                loaded_profiles[comm.name] = {
                    'id': comm.id,
                    'name': comm.name,
                    'location': comm.location,
                    'latitude': comm.latitude if comm.latitude is not None else proxy['latitude'],
                    'longitude': comm.longitude if comm.longitude is not None else proxy['longitude'],
                    'population': population,
                    'elderly_ratio': comm.elderly_ratio or 0.2,
                    'chronic_disease_ratio': comm.chronic_disease_ratio or 0.15,
                    'vulnerability_index': comm.vulnerability_index,
                    'risk_level': comm.risk_level,
                    # 模型仍使用上面的代理填充值；公开小样本判断只看数据库原列。
                    'raw_population': comm.population,
                    'raw_elderly_ratio': comm.elderly_ratio,
                    'raw_chronic_disease_ratio': comm.chronic_disease_ratio,
                    'raw_vulnerability_index': comm.vulnerability_index,
                    'raw_risk_level': comm.risk_level,

                    # 数据库尚无这些实测字段，暂用稳定中性代理；接入真实数据后应直接替换。
                    'green_space_ratio': proxy['green_space_ratio'],
                    'heat_island_index': proxy['heat_island_index'],
                    'medical_accessibility': proxy['medical_accessibility'],
                    'baseline_visits': self._estimate_baseline_visits(population)
                }

            self.community_profiles = loaded_profiles
            self.community_profile_status = {
                'available': True,
                'code': 'available',
                'source': 'community_table',
                'message': f'已从 Community 表加载 {len(loaded_profiles)} 个社区档案。',
            }
        except Exception:
            self.community_profiles = {}
            self.community_profile_status = {
                'available': False,
                'code': 'community_query_failed',
                'source': 'community_table',
                'message': 'Community 表当前无法读取，本次不生成社区风险排名。',
            }
            current_app.logger.exception('Community 表查询或档案加载失败')
    
    def _setup_default_communities(self):
        """设置默认社区配置"""
        coords_map = {}
        if has_app_context():
            try:
                coords_map = current_app.config.get('COMMUNITY_COORDS_GCJ') or {}
            except Exception:
                coords_map = {}

        default_communities = [
            {'name': '牛家垄周村', 'population': 132, 'elderly_ratio': 0.67, 'chronic_disease_ratio': 0.1},
            {'name': '岭背徐村', 'population': 89, 'elderly_ratio': 0.45, 'chronic_disease_ratio': 0.12},
            {'name': '徐家湾', 'population': 156, 'elderly_ratio': 0.38, 'chronic_disease_ratio': 0.15},
            {'name': '徐家咀', 'population': 98, 'elderly_ratio': 0.52, 'chronic_disease_ratio': 0.18},
            {'name': '竹峦徐村', 'population': 112, 'elderly_ratio': 0.41, 'chronic_disease_ratio': 0.11},
            {'name': '樟树湾徐村', 'population': 78, 'elderly_ratio': 0.55, 'chronic_disease_ratio': 0.14},
            {'name': '谭家新村', 'population': 145, 'elderly_ratio': 0.35, 'chronic_disease_ratio': 0.09},
            {'name': '新屋汪家', 'population': 92, 'elderly_ratio': 0.48, 'chronic_disease_ratio': 0.16},
        ]
        
        for i, comm in enumerate(default_communities):
            coords = coords_map.get(comm['name']) if coords_map else None
            proxy = self._stable_proxy_profile(comm['name'])
            if coords and len(coords) == 2:
                longitude, latitude = coords[0], coords[1]
            else:
                # 坐标缺失时使用按社区标识固定的都昌县附近坐标，避免每次启动位置变化。
                latitude = proxy['latitude']
                longitude = proxy['longitude']
            self.community_profiles[comm['name']] = {
                'id': i + 1,
                'name': comm['name'],
                'location': f"江西省九江市都昌县{comm['name']}",
                'latitude': latitude,
                'longitude': longitude,
                'population': comm['population'],
                'elderly_ratio': comm['elderly_ratio'],
                'chronic_disease_ratio': comm['chronic_disease_ratio'],
                # 以下字段为稳定中性代理，不能替代社区实测绿地、热岛和医疗可达性数据。
                'green_space_ratio': proxy['green_space_ratio'],
                'heat_island_index': proxy['heat_island_index'],
                'medical_accessibility': proxy['medical_accessibility'],
                'baseline_visits': self._estimate_baseline_visits(comm['population'])
            }
    
    def calculate_vulnerability_index(self, community_data):
        """
        计算社区脆弱性指数 (Vulnerability Index)
        
        公式: VI_c = 1 + a*老龄率 + b*慢病率 - d*绿地率 + e*热岛指数 - f*医疗可达性
        
        参数:
        - community_data: 社区数据字典
        
        返回:
        - vi: 脆弱性指数（>1表示比平均更脆弱）
        - breakdown: 各因子贡献分解
        """
        # 获取各因子值
        elderly_ratio = community_data.get('elderly_ratio', 0.2)
        chronic_ratio = community_data.get('chronic_disease_ratio', 0.15)
        green_ratio = community_data.get('green_space_ratio', 0.1)
        heat_island = community_data.get('heat_island_index', 0.5)
        medical_access = community_data.get('medical_accessibility', 0.5)
        
        # 计算各因子贡献
        breakdown = {
            'elderly_contribution': self.vi_weights['elderly_ratio'] * elderly_ratio,
            'chronic_contribution': self.vi_weights['chronic_disease_ratio'] * chronic_ratio,
            'green_contribution': self.vi_weights['green_space_ratio'] * green_ratio,
            'heat_island_contribution': self.vi_weights['heat_island_index'] * heat_island,
            'medical_contribution': self.vi_weights['medical_accessibility'] * medical_access
        }
        
        # 计算VI
        vi = 1.0
        for contribution in breakdown.values():
            vi += contribution
        
        # 确保VI >= 0.5
        vi = max(0.5, vi)
        
        # 确定脆弱性等级
        if vi >= 1.5:
            level = '高脆弱性'
            color = 'danger'
        elif vi >= 1.2:
            level = '中脆弱性'
            color = 'warning'
        else:
            level = '低脆弱性'
            color = 'success'
        
        return {
            'vulnerability_index': round(vi, 3),
            'level': level,
            'color': color,
            'breakdown': breakdown,
            'interpretation': f'该社区脆弱性指数为{vi:.2f}，{level}。'
                            f'主要因素：老龄率贡献{breakdown["elderly_contribution"]:.2f}，'
                            f'慢病率贡献{breakdown["chronic_contribution"]:.2f}'
        }
    
    def calculate_community_risk_score(self, community_name, weather_rr, target_date=None):
        """
        计算社区风险得分
        
        公式: RiskScore_c(t) = MacroRR(t) × VI_c × BaselineRate_c
        
        参数:
        - community_name: 社区名称
        - weather_rr: 宏观天气相对风险
        - target_date: 目标日期
        
        返回:
        - risk_score: 风险得分
        - details: 详细信息
        """
        if community_name not in self.community_profiles:
            return {'error': f'社区 {community_name} 未找到'}
        
        profile = self.community_profiles[community_name]
        
        # 计算VI
        vi_result = self.calculate_vulnerability_index(profile)
        vi = vi_result['vulnerability_index']
        
        # 获取基线门诊率
        baseline_rate = profile.get('baseline_visits', 5)
        
        # 标准化输入RR，避免非数值污染
        try:
            weather_rr = float(weather_rr)
        except (TypeError, ValueError):
            weather_rr = 1.0
        weather_rr = max(0.01, weather_rr)

        # 计算风险得分（总量）与超额风险（天气导致增量）
        risk_score = weather_rr * vi * baseline_rate
        excess_risk_score = max(weather_rr - 1.0, 0.0) * vi * baseline_rate

        # 标准化到0-100（超额风险映射，保留跨天可比性）
        normalized_score = self._normalize_excess_risk(excess_risk_score)

        # 保留本次计算使用的原始浮点值，供 API 使用者独立回算。
        # 页面展示时可以四舍五入，计算对象本身不做提前截断。
        hazard_formula = {
            'expression': (
                'Excess=max(WeatherRR-1,0)×VI×BaselineVisits; '
                'Hazard=clip((1-exp(-Excess/Efold))×100,0,100)'
            ),
            'weather_rr': weather_rr,
            'vi': vi,
            'baseline_visits': baseline_rate,
            'excess': excess_risk_score,
            'efold': self.excess_score_efold,
            'hazard': normalized_score,
        }
        
        # 确定风险等级
        if normalized_score >= self.risk_level_thresholds['high']:
            risk_level = '高风险'
            color = 'danger'
        elif normalized_score >= self.risk_level_thresholds['medium']:
            risk_level = '中风险'
            color = 'warning'
        else:
            risk_level = '低风险'
            color = 'success'
        
        return {
            'community': community_name,
            'risk_score': round(risk_score, 2),
            'normalized_score': round(normalized_score, 1),
            'risk_level': risk_level,
            'color': color,
            'components': {
                'weather_rr': round(weather_rr, 3),
                'vulnerability_index': vi,
                'baseline_rate': baseline_rate,
                'excess_risk_score': round(excess_risk_score, 2)
            },
            'hazard_formula': hazard_formula,
            'vi_details': vi_result,
            'population': profile.get('population', 0),
            'elderly_ratio': profile.get('elderly_ratio', 0),
            'expected_excess_visits': round(excess_risk_score, 1)
        }
    
    def generate_community_risk_map(self, weather_data, target_date=None, window_days=30, disease_filter='', community_scope=None):
        """
        生成社区风险地图数据（学术增强版）。

        主要输出：
        1) 天气驱动风险（DLNM RR）
        2) 历史负担校准（SIR + 95%CI + 经验贝叶斯平滑）
        3) 不确定性（CI宽度 + RR>1概率）
        4) 空间热点显著性（Gi* 近似）
        5) 0-4等级风险与 Impact×Likelihood 矩阵
        """
        from services.dlnm_risk_service import get_dlnm_service

        # 社区表可在服务单例创建后继续增改。每次计算前刷新小型档案表，
        # 避免新社区病历被旧内存清单误判为“未匹配”。
        if has_app_context():
            self._load_community_profiles()

        try:
            from core.time_utils import today_local
        except Exception:
            today_local = lambda: datetime.now().date()  # noqa: E731

        dlnm = get_dlnm_service()
        window_days = max(7, min(int(window_days or 30), 120))
        if target_date is None:
            target_date = today_local()

        # 1) 天气宏观风险（DLNM）
        try:
            temperature = float(weather_data.get('temperature', 20))
        except (TypeError, ValueError):
            temperature = 20.0
        lag_temperatures = self._extract_lag_temperatures(weather_data, temperature)
        if lag_temperatures:
            macro_rr, _ = dlnm.calculate_rr(temperature, lag_temperatures=lag_temperatures)
        else:
            macro_rr, _ = dlnm.calculate_rr(temperature)

        # 2) 计算天气驱动风险底图
        community_risks = []
        for name, profile in self.community_profiles.items():
            if community_scope is not None and name not in community_scope:
                continue
            risk = self.calculate_community_risk_score(name, macro_rr, target_date)
            risk['latitude'] = profile.get('latitude', 29.35)
            risk['longitude'] = profile.get('longitude', 116.37)
            risk['green_space_ratio'] = profile.get('green_space_ratio', 0.1)
            risk['heat_island_index'] = profile.get('heat_island_index', 0.5)
            risk['medical_accessibility'] = profile.get('medical_accessibility', 0.6)
            community_risks.append(risk)

        if not community_risks:
            profile_status = getattr(self, 'community_profile_status', {
                'available': False,
                'code': 'community_profiles_unavailable',
                'source': 'community_table',
                'message': '社区档案当前不可用，本次不生成社区风险排名。',
            })
            return {
                'data_available': False,
                'data_status': profile_status,
                'map_data': {'type': 'FeatureCollection', 'features': []},
                'rankings': [],
                'summary': {
                    'data_available': False,
                    'data_status': profile_status.get('code', 'community_profiles_unavailable'),
                    'data_message': profile_status.get('message'),
                    'community_profile_source': profile_status.get('source'),
                    'total_communities': 0,
                    'high_risk_count': 0,
                    'medium_risk_count': 0,
                    'low_risk_count': 0,
                    'total_expected_excess': 0,
                    'historical_component_available': False,
                    'risk_weights': {},
                    'baseline_rate_per_person_day': None,
                    'median_uncertainty_index': None,
                },
                'macro_weather': {
                    'temperature': temperature,
                    'rr': round(macro_rr, 3),
                    'lag_temperatures_used': len(lag_temperatures) if lag_temperatures else 0
                },
                'impact_likelihood_matrix': {
                    'impact_levels': ['low', 'medium', 'high', 'very_high'],
                    'likelihood_levels': ['low', 'medium', 'high', 'very_high'],
                    'counts': {}
                },
                'layers': {
                    'risk_index': [],
                    'vulnerability': [],
                    'uncertainty': [],
                    'hotspot': []
                },
                'management_suggestions': [],
                'methodology': [profile_status.get('message')]
            }

        # 3) 相对指数与分位（原有字段兼容）
        risk_scores = np.array([float(item.get('risk_score', 0.0)) for item in community_risks], dtype=float)
        mean_score = float(np.mean(risk_scores)) if risk_scores.size else 0.0
        raw_percentiles = self._percentile_map({
            item['community']: float(item.get('risk_score', 0.0))
            for item in community_risks
        })
        for item in community_risks:
            score = float(item.get('risk_score', 0.0))
            item['relative_index'] = round((score / mean_score * 100.0), 1) if mean_score > 0 else 100.0
            item['percentile_rank'] = round(raw_percentiles.get(item['community'], 0.0), 1)

        # 4) 历史病例窗口，做 SIR / CI / 不确定性
        medical_summary = self._collect_medical_counts(target_date, window_days, disease_filter=disease_filter, community_scope=community_scope)
        counts_by_community = medical_summary['counts_by_community']
        analysis_days = max(1, int(medical_summary['window_days']))

        total_population = sum(
            max(float(item.get('population') or 0.0), 0.0)
            for item in community_risks
        )
        matched_records = int(medical_summary['matched_records'])
        historical_component_available = matched_records > 0 and total_population > 0
        baseline_rate_per_person_day = None
        if historical_component_available:
            baseline_rate_per_person_day = matched_records / (total_population * analysis_days)

        expected_lookup = {}
        observed_lookup = {}
        expected_sum = 0.0
        observed_sum = 0
        for item in community_risks:
            community = item['community']
            observed = int(counts_by_community.get(community, 0)) if historical_component_available else None
            pop = max(float(item.get('population') or 0.0), 0.0)
            expected = (
                baseline_rate_per_person_day * pop * analysis_days
                if historical_component_available and pop > 0 else None
            )
            expected_lookup[community] = expected
            observed_lookup[community] = observed
            if expected is not None:
                expected_sum += expected
            if observed is not None:
                observed_sum += observed

        global_sir = (
            observed_sum / expected_sum
            if historical_component_available and expected_sum > 0 else None
        )
        for item in community_risks:
            community = item['community']
            observed = observed_lookup.get(community)
            expected = expected_lookup.get(community)

            if not historical_component_available:
                item['historical_component_available'] = False
                item['observed_cases'] = None
                item['expected_cases'] = None
                item['sir'] = None
                item['ci_low'] = None
                item['ci_high'] = None
                item['smoothed_sir'] = None
                item['probability_exceed_baseline'] = None
                item['certainty'] = 'unavailable'
                item['uncertainty_index'] = None
                continue

            rr, ci_low, ci_high = self._rr_with_ci(observed, expected)
            prob_above_one = self._probability_rr_above_one(rr, observed)

            # 经验贝叶斯平滑，减少小样本社区波动
            prior_strength = 8.0
            if rr is None:
                smoothed_sir = None
            else:
                shrink_w = (expected / (expected + prior_strength)) if expected else 0.0
                smoothed_sir = shrink_w * rr + (1.0 - shrink_w) * global_sir

            certainty = 'high'
            if expected is None or expected < 3:
                certainty = 'low'
            elif expected < 8:
                certainty = 'medium'

            ci_width = (ci_high - ci_low) if (ci_low is not None and ci_high is not None) else 3.0
            uncertainty_index = min(100.0, ci_width * 30.0 + (20.0 if certainty == 'low' else 8.0 if certainty == 'medium' else 0.0))

            item['historical_component_available'] = True
            item['observed_cases'] = observed
            item['expected_cases'] = round(expected, 3) if expected is not None else None
            item['sir'] = round(rr, 3) if rr is not None else None
            item['ci_low'] = round(ci_low, 3) if ci_low is not None else None
            item['ci_high'] = round(ci_high, 3) if ci_high is not None else None
            item['smoothed_sir'] = round(smoothed_sir, 3) if smoothed_sir is not None else None
            item['probability_exceed_baseline'] = round(float(np.clip(prob_above_one, 0.0, 1.0)), 4)
            item['certainty'] = certainty
            item['uncertainty_index'] = round(float(np.clip(uncertainty_index, 0.0, 100.0)), 1)

        # 5) SVI-like 多主题脆弱性（灵感来自 CDC SVI）
        sensitivity_raw = {}
        exposure_raw = {}
        adaptive_gap_raw = {}
        for item in community_risks:
            name = item['community']
            elderly = float(item.get('elderly_ratio') or 0.0)
            chronic = float(item.get('chronic_disease_ratio') or 0.0)
            heat_island = float(item.get('heat_island_index') or 0.0)
            green_space = float(item.get('green_space_ratio') or 0.0)
            medical_access = float(item.get('medical_accessibility') or 0.0)
            sensitivity_raw[name] = 0.6 * elderly + 0.4 * chronic
            exposure_raw[name] = heat_island
            adaptive_gap_raw[name] = 0.5 * (1.0 - green_space) + 0.5 * (1.0 - medical_access)

        sensitivity_pct = self._percentile_map(sensitivity_raw)
        exposure_pct = self._percentile_map(exposure_raw)
        adaptive_gap_pct = self._percentile_map(adaptive_gap_raw)

        # 6) 风险综合：天气危险度 + 脆弱性 + 历史负担
        if historical_component_available:
            burden_values = {}
            for item in community_risks:
                burden_value = item.get('smoothed_sir')
                if burden_value is None:
                    burden_value = item.get('sir')
                if burden_value is not None:
                    burden_values[item['community']] = float(burden_value)
            burden_pct = self._percentile_map(burden_values)
        else:
            burden_pct = {}

        matrix_impact_levels = ['low', 'medium', 'high', 'very_high']
        matrix_likelihood_levels = ['low', 'medium', 'high', 'very_high']
        matrix_counts = {
            impact: {likelihood: 0 for likelihood in matrix_likelihood_levels}
            for impact in matrix_impact_levels
        }
        impact_rank = {name: idx + 1 for idx, name in enumerate(matrix_impact_levels)}
        likelihood_rank = {name: idx + 1 for idx, name in enumerate(matrix_likelihood_levels)}

        for item in community_risks:
            name = item['community']
            svi_percentile = (
                0.40 * sensitivity_pct.get(name, 0.0)
                + 0.25 * exposure_pct.get(name, 0.0)
                + 0.35 * adaptive_gap_pct.get(name, 0.0)
            )
            hazard_pct = float(item.get('normalized_score') or 0.0)
            if historical_component_available:
                burden = burden_pct.get(name, 50.0)
                risk_weights = {'weather': 0.45, 'svi': 0.35, 'burden': 0.20}
                uncertainty_penalty = (
                    0.93 if float(item.get('uncertainty_index') or 0.0) >= 70 else 1.0
                )
            else:
                # 历史分量缺失时，把可用权重 0.45/0.35 重新归一化到总和 1。
                burden = None
                risk_weights = {'weather': 0.5625, 'svi': 0.4375, 'burden': 0.0}
                uncertainty_penalty = 1.0

            weather_contribution = risk_weights['weather'] * hazard_pct
            svi_contribution = risk_weights['svi'] * svi_percentile
            burden_contribution = risk_weights['burden'] * (burden or 0.0)
            pre_penalty_total = weather_contribution + svi_contribution + burden_contribution
            risk_index = pre_penalty_total * uncertainty_penalty
            risk_index = float(np.clip(risk_index, 0.0, 100.0))

            heatrisk_level, heatrisk_label, heatrisk_color = self._heatrisk_level_from_index(risk_index)

            impact_score = min(
                100.0,
                risk_index * 0.75 + float(item.get('expected_excess_visits') or 0.0) * 6.0
            )
            impact_bucket = self._to_four_level_bucket(impact_score)
            if historical_component_available:
                likelihood_score = float(item['probability_exceed_baseline']) * 100.0
                if item.get('certainty') == 'high':
                    likelihood_score += 10.0
                elif item.get('certainty') == 'low':
                    likelihood_score -= 10.0
                likelihood_score = float(np.clip(likelihood_score, 0.0, 100.0))
                likelihood_bucket = self._to_four_level_bucket(likelihood_score)
                matrix_counts[impact_bucket][likelihood_bucket] += 1
                matrix_score = impact_rank[impact_bucket] * likelihood_rank[likelihood_bucket]
            else:
                likelihood_bucket = None
                matrix_score = None

            item['svi_percentile'] = round(svi_percentile, 1)
            item['theme_scores'] = {
                'sensitivity': round(sensitivity_pct.get(name, 0.0), 1),
                'exposure': round(exposure_pct.get(name, 0.0), 1),
                'adaptive_gap': round(adaptive_gap_pct.get(name, 0.0), 1)
            }
            item['historical_component_available'] = historical_component_available
            item['burden_percentile'] = round(float(burden), 1) if burden is not None else None
            item['weather_hazard_score'] = round(hazard_pct, 1)
            item['uncertainty_penalty'] = uncertainty_penalty
            item['risk_weights'] = risk_weights
            item['risk_contributions'] = {
                'weather': round(weather_contribution, 2),
                'svi': round(svi_contribution, 2),
                'burden': round(burden_contribution, 2),
                'before_penalty': round(pre_penalty_total, 2),
                'after_penalty': round(risk_index, 2),
            }
            item['risk_index'] = round(risk_index, 1)
            item['heatrisk_level'] = heatrisk_level
            item['heatrisk_label'] = heatrisk_label
            item['heatrisk_color'] = heatrisk_color
            item['impact_bucket'] = impact_bucket
            item['likelihood_bucket'] = likelihood_bucket
            item['matrix_score'] = matrix_score

        # 7) 空间热点显著性（Gi*）
        self._compute_hotspot_stats(community_risks)

        # 按综合风险排序
        rankings = sorted(
            community_risks,
            key=lambda row: float(row.get('risk_index') or row.get('normalized_score') or 0.0),
            reverse=True
        )
        for idx, row in enumerate(rankings, start=1):
            row['rank'] = idx

        # GeoJSON
        geojson_features = []
        for row in rankings:
            feature = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [row['longitude'], row['latitude']]
                },
                'properties': {
                    'name': row['community'],
                    'risk_score': row['normalized_score'],
                    'risk_level': row['risk_level'],
                    'color': row['color'],
                    'population': row['population'],
                    'elderly_ratio': row['elderly_ratio'],
                    'vi': row['components']['vulnerability_index'],
                    'relative_index': row.get('relative_index', 100.0),
                    'percentile_rank': row.get('percentile_rank', 0.0),
                    'risk_index': row.get('risk_index', row.get('normalized_score', 0.0)),
                    'weather_hazard_score': row.get('weather_hazard_score', row.get('normalized_score', 0.0)),
                    'historical_component_available': row.get('historical_component_available', False),
                    'burden_percentile': row.get('burden_percentile'),
                    'uncertainty_penalty': row.get('uncertainty_penalty', 1.0),
                    'risk_weights': row.get('risk_weights', {}),
                    'risk_contributions': row.get('risk_contributions', {}),
                    'hazard_formula': row.get('hazard_formula', {}),
                    'heatrisk_level': row.get('heatrisk_level', 0),
                    'uncertainty_index': row.get('uncertainty_index'),
                    'hotspot_category': row.get('hotspot_category', '数据不足')
                }
            }
            geojson_features.append(feature)

        map_data = {
            'type': 'FeatureCollection',
            'features': geojson_features
        }

        heatrisk_counts = {str(level): 0 for level in range(5)}
        hotspot_counts = {
            '热点(99%)': 0,
            '热点(95%)': 0,
            '冷点(99%)': 0,
            '冷点(95%)': 0,
            '无显著': 0,
            '样本不足': 0
        }
        uncertainty_values = []
        for row in rankings:
            level_key = str(int(row.get('heatrisk_level', 0)))
            heatrisk_counts[level_key] = heatrisk_counts.get(level_key, 0) + 1
            hotspot_label = row.get('hotspot_category', '无显著')
            hotspot_counts[hotspot_label] = hotspot_counts.get(hotspot_label, 0) + 1
            if row.get('uncertainty_index') is not None:
                uncertainty_values.append(float(row['uncertainty_index']))
        median_uncertainty = float(np.median(uncertainty_values)) if uncertainty_values else None

        data_coverage_ratio = (
            medical_summary['matched_records'] / medical_summary['total_records']
            if medical_summary['total_records'] > 0 else None
        )

        layers = {
            'risk_index': [
                {'community': row['community'], 'value': row.get('risk_index', 0.0)}
                for row in rankings
            ],
            'vulnerability': [
                {'community': row['community'], 'value': row.get('svi_percentile', 0.0)}
                for row in rankings
            ],
            'uncertainty': [
                {'community': row['community'], 'value': row.get('uncertainty_index')}
                for row in rankings
            ],
            'hotspot': [
                {'community': row['community'], 'category': row.get('hotspot_category', '数据不足')}
                for row in rankings
            ]
        }

        # 8) 公平性分层（按 SVI-like 分位分层）
        strata_map = {'Q1': [], 'Q2': [], 'Q3': [], 'Q4': []}
        for row in rankings:
            svi = float(row.get('svi_percentile') or 0.0)
            if svi >= 75:
                stratum = 'Q4'
            elif svi >= 50:
                stratum = 'Q3'
            elif svi >= 25:
                stratum = 'Q2'
            else:
                stratum = 'Q1'
            row['equity_stratum'] = stratum
            strata_map[stratum].append(row)

        quartile_defs = [
            ('Q4', '最高脆弱'),
            ('Q3', '较高脆弱'),
            ('Q2', '中等脆弱'),
            ('Q1', '较低脆弱')
        ]
        quartile_rows = []
        for key, label in quartile_defs:
            rows = strata_map.get(key, [])
            if rows:
                avg_risk_index = float(np.mean([float(r.get('risk_index') or 0.0) for r in rows]))
                uncertainty_items = [
                    float(r['uncertainty_index'])
                    for r in rows if r.get('uncertainty_index') is not None
                ]
                avg_uncertainty = (
                    float(np.mean(uncertainty_items)) if uncertainty_items else None
                )
                high_heatrisk = sum(1 for r in rows if int(r.get('heatrisk_level') or 0) >= 3)
            else:
                avg_risk_index = 0.0
                avg_uncertainty = None
                high_heatrisk = 0
            quartile_rows.append({
                'stratum': key,
                'label': label,
                'count': len(rows),
                'avg_risk_index': round(avg_risk_index, 1),
                'avg_uncertainty': round(avg_uncertainty, 1) if avg_uncertainty is not None else None,
                'high_heatrisk_count': high_heatrisk
            })

        priority_candidates = [
            row for row in rankings
            if float(row.get('svi_percentile') or 0.0) >= 75.0
            and (
                float(row.get('risk_index') or 0.0) >= 60.0
                or int(row.get('heatrisk_level') or 0) >= 3
            )
        ]
        if not priority_candidates:
            priority_candidates = sorted(
                rankings,
                key=lambda row: (
                    float(row.get('svi_percentile') or 0.0) * 0.55
                    + float(row.get('risk_index') or 0.0) * 0.45
                ),
                reverse=True
            )[:5]

        priority_rows = []
        for row in priority_candidates[:8]:
            if int(row.get('heatrisk_level') or 0) >= 3:
                action = '优先安排巡访与高风险人群随访，必要时增加临时接诊能力。'
            elif float(row.get('uncertainty_index') or 0.0) >= 70:
                action = '优先补全数据与病例核验，避免高脆弱社区因样本不足低估风险。'
            else:
                action = '优先开展健康宣教与分层干预，提前准备防暑/防寒资源。'
            priority_rows.append({
                'community': row.get('community'),
                'equity_stratum': row.get('equity_stratum', 'Q4'),
                'svi_percentile': round(float(row.get('svi_percentile') or 0.0), 1),
                'risk_index': round(float(row.get('risk_index') or 0.0), 1),
                'heatrisk_level': int(row.get('heatrisk_level') or 0),
                'uncertainty_index': round(float(row.get('uncertainty_index') or 0.0), 1),
                'recommended_action': action
            })
        equity_priority_count = len(priority_rows)
        model_risk_weights = {}
        if rankings:
            raw_weights = rankings[0].get('risk_weights')
            if isinstance(raw_weights, dict):
                model_risk_weights = dict(raw_weights)
        case_counts_hidden = self._apply_case_count_k_anonymity(rankings)
        hidden_names = self._apply_ranking_demographic_suppression(rankings, map_data)
        self._redact_suppressed_vulnerability_outputs(layers, priority_rows, hidden_names)
        _scrub_suppressed_quartile_risk(quartile_rows, strata_map)
        _order_public_risk_lists(rankings, map_data, layers)
        management_suggestions = self._generate_management_suggestions(
            rankings[:5],
            weather_data,
        )

        return {
            'map_data': map_data,
            'rankings': [
                {
                    'rank': row['rank'],
                    'community': row['community'],
                    'latitude': row.get('latitude'),
                    'longitude': row.get('longitude'),
                    'risk_score': None if row.get('suppressed') else row.get('normalized_score'),
                    'risk_level': row['risk_level'],
                    'population': row.get('population'),
                    'elderly_ratio': row.get('elderly_ratio'),
                    'chronic_disease_ratio': row.get('chronic_disease_ratio'),
                    'vulnerability_index': row.get('vulnerability_index'),
                    'expected_excess_visits': (
                        None if row.get('suppressed') else row.get('expected_excess_visits')
                    ),
                    'relative_index': (
                        None if row.get('suppressed') else row.get('relative_index', 100.0)
                    ),
                    'percentile_rank': None if row.get('suppressed') else row.get('percentile_rank', 0.0),
                    'risk_index': (
                        None if row.get('suppressed')
                        else row.get('risk_index', row.get('normalized_score', 0.0))
                    ),
                    'weather_hazard_score': (
                        None if row.get('suppressed')
                        else row.get('weather_hazard_score', row.get('normalized_score', 0.0))
                    ),
                    'historical_component_available': row.get('historical_component_available', False),
                    'burden_percentile': None if row.get('suppressed') else row.get('burden_percentile'),
                    'uncertainty_penalty': (
                        None if row.get('suppressed') else row.get('uncertainty_penalty', 1.0)
                    ),
                    'risk_weights': (
                        None if row.get('suppressed') else (row.get('risk_weights') or {})
                    ),
                    'risk_contributions': row.get('risk_contributions', {}),
                    'hazard_formula': row.get('hazard_formula', {}),
                    'heatrisk_level': row.get('heatrisk_level', 0),
                    'heatrisk_label': row.get('heatrisk_label', '最小'),
                    'heatrisk_color': row.get('heatrisk_color', '#16a34a'),
                    'svi_percentile': row.get('svi_percentile', 0.0),
                    'theme_scores': row.get('theme_scores', {}),
                    'observed_cases': row.get('observed_cases'),
                    'expected_cases': row.get('expected_cases'),
                    'sir': row.get('sir'),
                    'ci_low': row.get('ci_low'),
                    'ci_high': row.get('ci_high'),
                    'smoothed_sir': row.get('smoothed_sir'),
                    'probability_exceed_baseline': row.get('probability_exceed_baseline'),
                    'suppressed': bool(row.get('suppressed')),
                    'stats_suppressed': bool(row.get('stats_suppressed')),
                    'stats_redacted': bool(row.get('stats_redacted')),
                    'certainty': row.get('certainty', 'unavailable'),
                    'uncertainty_index': row.get('uncertainty_index'),
                    'hotspot_category': row.get('hotspot_category', '数据不足'),
                    'hotspot_z': row.get('hotspot_z'),
                    'hotspot_p': row.get('hotspot_p'),
                    'impact_bucket': row.get('impact_bucket', 'low'),
                    'likelihood_bucket': row.get('likelihood_bucket', 'low'),
                    'matrix_score': row.get('matrix_score', 1),
                    'equity_stratum': row.get('equity_stratum', 'Q1')
                }
                for row in rankings
            ],
            'summary': {
                'total_communities': len(rankings),
                'high_risk_count': sum(1 for row in rankings if row['risk_level'] == '高风险'),
                'medium_risk_count': sum(1 for row in rankings if row['risk_level'] == '中风险'),
                'low_risk_count': sum(1 for row in rankings if row['risk_level'] == '低风险'),
                'total_expected_excess': _sum_published_excess(rankings),
                'analysis_date': str(target_date),
                'window_days': analysis_days,
                'disease_filter': disease_filter or '',
                'matched_records': None if case_counts_hidden else medical_summary['matched_records'],
                'total_records': None if case_counts_hidden else medical_summary['total_records'],
                'unmatched_records': None if case_counts_hidden else medical_summary['unmatched_records'],
                'suppressed': any(bool(row.get('suppressed')) for row in rankings),
                'historical_component_available': historical_component_available,
                'risk_weights': model_risk_weights,
                'data_coverage_ratio': None if case_counts_hidden else (
                    round(data_coverage_ratio, 4) if data_coverage_ratio is not None else None
                ),
                'baseline_rate_per_person_day': None if case_counts_hidden else (
                    round(baseline_rate_per_person_day, 8) if baseline_rate_per_person_day is not None else None
                ),
                'median_uncertainty_index': round(median_uncertainty, 1) if median_uncertainty is not None else None,
                'heatrisk_counts': heatrisk_counts,
                'hotspot_counts': hotspot_counts,
                'equity_priority_count': equity_priority_count
            },
            'macro_weather': {
                'temperature': temperature,
                'rr': round(macro_rr, 3),
                'lag_temperatures_used': len(lag_temperatures) if lag_temperatures else 0
            },
            'impact_likelihood_matrix': {
                'impact_levels': matrix_impact_levels,
                'likelihood_levels': matrix_likelihood_levels,
                'counts': matrix_counts
            },
            'layers': layers,
            'equity_stratification': {
                'quartiles': quartile_rows,
                'priority_communities': priority_rows
            },
            'methodology': [
                (
                    '天气危险度上游公式：Excess=max(Weather RR-1, 0)×VI×BaselineVisits；'
                    'Hazard=clip((1-exp(-Excess/Efold))×100, 0, 100)。'
                    '每个社区的本次输入与结果在地图弹窗中展示。'
                ),
                (
                    '社区风险=天气危险度(45%)+SVI-like脆弱性(35%)+历史负担(20%)，'
                    '并对高不确定性样本执行惩罚。'
                    if historical_component_available else
                    '本次没有可匹配的历史病例分量；社区风险使用天气危险度(56.25%)+'
                    'SVI-like脆弱性(43.75%)，不使用历史不确定性惩罚。'
                ),
                (
                    '历史负担采用门诊记录 O/E + 95%CI，并使用经验贝叶斯平滑抑制小样本波动。'
                    if historical_component_available else
                    '门诊记录 O/E、95%CI、平滑 O/E、P(O/E>1)、历史负担与不确定性保持为空，等待可匹配病例数据。'
                ),
                '有历史分量时同时展示 CI 宽度与 P(O/E>1) 概率，避免仅给单点值。',
                (
                    '空间热点采用 Getis-Ord Gi* 思路给出显著性分级（95%/99%）。'
                    if historical_component_available else
                    '历史病例分量缺失时不计算空间热点显著性，相关字段显示数据不足。'
                ),
                (
                    '行动优先级使用 Impact×Likelihood 四级矩阵（1-16分）支持人工分流、核查与行动排序。'
                    if historical_component_available else
                    '历史概率缺失时不生成 Impact×Likelihood 分值；综合风险仅用于人工分流、核查与行动排序。'
                ),
                '公平性分层按脆弱性分位(Q1-Q4)聚合，优先识别“高脆弱+高风险”社区。'
            ],
            'management_suggestions': management_suggestions
        }
    
    def _generate_management_suggestions(self, high_risk_communities, weather_data):
        """生成管控建议（医生端）"""
        suggestions = []
        
        temp = weather_data.get('temperature', 20)
        
        # 资源调度建议
        if len(high_risk_communities) >= 3:
            suggestions.append({
                'category': '资源调配',
                'priority': 'high',
                'advice': f'建议向 {high_risk_communities[0]["community"]}、{high_risk_communities[1]["community"]} 等高风险社区增派医疗资源',
                'target_communities': [c['community'] for c in high_risk_communities[:3]]
            })
        
        # 巡访建议。比率缺失表示该行已抑制，不把精确老龄率、慢病率或 VI 写进文本。
        for comm in high_risk_communities[:3]:
            elderly_ratio = comm.get('elderly_ratio')
            if isinstance(elderly_ratio, bool) or not isinstance(elderly_ratio, (int, float)):
                continue
            if elderly_ratio > 0.4:
                suggestions.append({
                    'category': '健康巡访',
                    'priority': 'high',
                    'advice': f'{comm["community"]} 老龄化程度高({elderly_ratio*100:.0f}%)，建议加强独居老人巡访',
                    'target_communities': [comm['community']]
                })
        
        # 温度相关建议
        if temp > 32:
            suggestions.append({
                'category': '防暑措施',
                'priority': 'high',
                'advice': '高温天气，建议在高风险社区开放避暑点、发放防暑物资',
                'target_communities': [c['community'] for c in high_risk_communities]
            })
        elif temp < 5:
            suggestions.append({
                'category': '防寒措施',
                'priority': 'high',
                'advice': '低温天气，建议检查高风险社区供暖情况、关注独居老人',
                'target_communities': [c['community'] for c in high_risk_communities]
            })
        
        # 门诊准备。抑制行的超额人次已置空，不能把 None 加进来，也不能写回建议文本。
        total_excess = _sum_published_excess(high_risk_communities)
        if total_excess is not None and total_excess > 10:
            suggestions.append({
                'category': '门诊准备',
                'priority': 'medium',
                'advice': f'预计高风险社区额外增加约 {total_excess:.0f} 人次就诊，建议门诊做好准备',
                'target_communities': [c['community'] for c in high_risk_communities]
            })
        
        if not suggestions:
            suggestions.append({
                'category': '常规管理',
                'priority': 'low',
                'advice': '各社区风险处于正常水平，保持常规健康管理工作',
                'target_communities': []
            })
        
        return suggestions
    
    def update_community_sensitivity(self, community_name, heat_sensitivity=None, cold_sensitivity=None):
        """
        更新社区天气敏感性参数 (v_c)
        
        这是模型中的关键参数，表示社区对高温/寒冷的额外敏感性
        """
        if community_name not in self.community_sensitivity:
            self.community_sensitivity[community_name] = {
                'heat_sensitivity': 1.0,
                'cold_sensitivity': 1.0
            }
        
        if heat_sensitivity is not None:
            self.community_sensitivity[community_name]['heat_sensitivity'] = heat_sensitivity
        
        if cold_sensitivity is not None:
            self.community_sensitivity[community_name]['cold_sensitivity'] = cold_sensitivity
    
    def _apply_case_count_k_anonymity(self, rankings):
        """裁剪即将返回的村级就诊计数。风险指数已算完，这里只改公开字段。

        只要有一个村的计数被抑制，其它村的 O/E、平滑 SIR 也会通过全局合计把那个整数算回来，
        所以这些衍生字段一并清空。允许公布的村只保留 observed_cases 本身。
        """
        decisions = []
        for row in rankings:
            name = row.get('community')
            profile = self.community_profiles.get(name) if name else None
            decisions.append(_case_count_publication(name, _case_count_sample_size(profile)))
        any_hidden = any(decision['hide'] for decision in decisions)
        for row, decision in zip(rankings, decisions):
            row['suppressed'] = decision['suppressed']
            row['stats_suppressed'] = decision['suppressed']
            row['stats_redacted'] = decision['redacted']
            if not decision['hide'] and not any_hidden:
                continue
            saved_observed = row.get('observed_cases')
            for field in _CASE_COUNT_FIELDS:
                row[field] = None
            if not decision['hide']:
                row['observed_cases'] = saved_observed
        return any_hidden

    def _apply_ranking_demographic_suppression(self, rankings, map_data=None):
        """按公开权限和样本阈值裁剪排名行上的人口学/脆弱性字段。

        隐藏时置空并标记 suppressed，同时清掉可回推 VI 的公式分量、
        超额就诊、危险度、相对指数、风险分解，以及能反推 SVI 分位的贡献表和分位桶。
        这些行会进入排名和综合预警 top_risk。授权且样本达到 k 时保留精确值；
        若同时有抑制行，相对指数只按仍公开的 RiskScore 重算。
        公开顺序不再使用被抑制社区的风险指数，只按社区名稳定排列。
        """
        profiles = self.community_profiles if isinstance(self.community_profiles, dict) else {}
        hidden_names = set()
        for row in rankings:
            name = row.get('community')
            profile = profiles.get(name) if name else None
            if _ranking_demographics_hidden(name, _case_count_sample_size(profile)):
                for field in _RANKING_DEMOGRAPHIC_FIELDS:
                    row[field] = None
                row['suppressed'] = True
                _suppress_reconstructable_risk_fields(row)
                if name:
                    hidden_names.add(name)
                continue
            self._publish_ranking_demographics(row, profile)

        if hidden_names:
            _republish_visible_relative_index(rankings, hidden_names)

        visible_relative = {
            row.get('community'): row.get('relative_index')
            for row in rankings
            if row.get('community') not in hidden_names
        }
        for feature in (map_data or {}).get('features') or []:
            props = feature.get('properties') or {}
            name = props.get('name')
            if name in hidden_names:
                props['population'] = None
                props['elderly_ratio'] = None
                props['vi'] = None
                props['percentile_rank'] = None
                _null_suppressed_decomposition(props)
                _clear_risk_contributions(props)
                formula = props.get('hazard_formula')
                if isinstance(formula, dict):
                    formula['expression'] = _SUPPRESSED_HAZARD_FORMULA_NOTE
                    for field in _HAZARD_FORMULA_RECONSTRUCTABLE_FIELDS:
                        formula[field] = None
                continue
            if name in visible_relative:
                props['relative_index'] = visible_relative[name]
        return hidden_names

    @staticmethod
    def _redact_suppressed_vulnerability_outputs(layers, priority_rows, hidden_names):
        """抑制社区在图层和公平性清单里不再带 SVI 精确值或分位桶。"""
        hidden_names = set(hidden_names or [])
        if not hidden_names:
            return
        for entry in (layers or {}).get('vulnerability') or []:
            if entry.get('community') in hidden_names:
                entry['value'] = None
        for entry in (layers or {}).get('risk_index') or []:
            if entry.get('community') in hidden_names:
                entry['value'] = None
        for priority in priority_rows or []:
            if priority.get('community') in hidden_names:
                priority['svi_percentile'] = None
                priority['equity_stratum'] = None
                priority['risk_index'] = None

    @staticmethod
    def _publish_ranking_demographics(row, profile):
        """授权且样本达标时，把档案里的精确人口学写回排名行。"""
        profile = profile or {}
        if 'raw_chronic_disease_ratio' in profile:
            row['chronic_disease_ratio'] = profile.get('raw_chronic_disease_ratio')
        elif profile.get('chronic_disease_ratio') is not None:
            row['chronic_disease_ratio'] = profile.get('chronic_disease_ratio')
        components = row.get('components') if isinstance(row.get('components'), dict) else {}
        row['vulnerability_index'] = components.get('vulnerability_index')

    def sanitize_community_public_stats(self, profile_dict):
        """按当前请求权限和原始样本裁剪公开医疗统计。

        无权查看时标记 stats_redacted；已授权但 raw_population 缺失或低于最小样本时
        标记 stats_suppressed。两种情况都清空医疗衍生字段，并移除 raw_* 内部列。
        """
        profile = dict(profile_dict or {})
        community_name = profile.get('name') or profile.get('community')
        if not _can_view_community_health_stats(community_name):
            redacted = True
            suppressed = False
            hide = True
        else:
            redacted = False
            suppressed = _sample_below_minimum(
                profile.get('raw_population'),
                _community_min_sample_size(),
            )
            hide = suppressed
        if hide:
            for field in _PUBLIC_HEALTH_STAT_FIELDS:
                profile[field] = None
        profile['stats_redacted'] = redacted
        profile['stats_suppressed'] = suppressed
        for key in [key for key in profile if key.startswith('raw_')]:
            del profile[key]
        return profile

    def get_community_profile(self, community_name, respect_access=False):
        """获取社区档案。

        默认 respect_access=False，无参调用保持离线算法测试的原返回。
        公开接口传入 True 时才按权限与最小样本裁剪。
        """
        if community_name in self.community_profiles:
            profile = self.community_profiles[community_name].copy()
            vi_result = self.calculate_vulnerability_index(profile)
            profile['vulnerability_details'] = vi_result
            if respect_access:
                return self.sanitize_community_public_stats(profile)
            return profile
        return None
    
    def get_all_communities(self, respect_access=False):
        """获取所有社区列表。respect_access=True 时只按裁剪后的脆弱性指数排序。"""
        communities = []
        for name, profile in self.community_profiles.items():
            vi_result = self.calculate_vulnerability_index(profile)
            item = {
                'name': name,
                'population': profile.get('population', 0),
                'elderly_ratio': profile.get('elderly_ratio', 0),
                'chronic_disease_ratio': profile.get('chronic_disease_ratio', 0),
                'vulnerability_index': vi_result['vulnerability_index'],
                'vulnerability_level': vi_result['level']
            }
            if respect_access:
                item['raw_population'] = profile.get('raw_population')
                item['vulnerability_details'] = vi_result
                item['risk_level'] = profile.get('risk_level')
                item['baseline_visits'] = profile.get('baseline_visits')
            communities.append(item)

        if respect_access:
            sanitized = [self.sanitize_community_public_stats(item) for item in communities]
            return sorted(sanitized, key=_public_vulnerability_sort_key)
        return sorted(communities, key=lambda x: x['vulnerability_index'], reverse=True)


# 单例实例
_community_service = None

def get_community_service():
    """获取社区风险服务单例"""
    global _community_service
    if _community_service is None:
        _community_service = CommunityRiskService()
    return _community_service


# 测试代码
if __name__ == '__main__':
    print("=" * 60)
    print("社区风险评估服务测试")
    print("=" * 60)
    
    service = CommunityRiskService()
    
    print("\n所有社区列表:")
    communities = service.get_all_communities()
    for comm in communities:
        print(f"  {comm['name']}: VI={comm['vulnerability_index']:.2f}, "
              f"老龄率={comm['elderly_ratio']*100:.0f}%, "
              f"级别={comm['vulnerability_level']}")
    
    print("\n社区风险地图生成测试:")
    weather = {'temperature': 35, 'humidity': 80, 'aqi': 120}
    result = service.generate_community_risk_map(weather)
    
    print(f"\n风险摘要:")
    print(f"  高风险社区: {result['summary']['high_risk_count']} 个")
    print(f"  中风险社区: {result['summary']['medium_risk_count']} 个")
    print(f"  低风险社区: {result['summary']['low_risk_count']} 个")
    
    print("\n风险排名Top 3:")
    for r in result['rankings'][:3]:
        print(f"  {r['rank']}. {r['community']}: "
              f"风险分数={r['risk_score']}, {r['risk_level']}")
    
    print("\n管控建议:")
    for s in result['management_suggestions']:
        print(f"  [{s['priority']}] {s['category']}: {s['advice']}")

