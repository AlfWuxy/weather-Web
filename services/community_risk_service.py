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
import math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os
from flask import current_app, has_app_context


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
        valid_rows = [
            row for row in rows
            if row.get('longitude') is not None and row.get('latitude') is not None
        ]
        if len(valid_rows) < 3:
            for row in rows:
                row['hotspot_z'] = 0.0
                row['hotspot_p'] = 1.0
                row['hotspot_category'] = '样本不足'
            return

        coords = [(row['longitude'], row['latitude']) for row in valid_rows]
        values = np.array([
            float(row.get('smoothed_sir') or row.get('sir') or 1.0)
            for row in valid_rows
        ], dtype=float)

        mean_x = float(np.mean(values))
        std_x = float(np.std(values))
        if std_x <= 1e-9:
            for row in rows:
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
        """NWS HeatRisk 风格 0-4 档位。"""
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

    def _collect_medical_counts(self, end_date, window_days, disease_filter=''):
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
        """加载社区档案数据"""
        # 从数据库或配置加载社区信息
        # 这里使用示例数据，实际应从Community表加载
        self.community_profiles = {}
        
        try:
            # 延迟导入避免循环导入问题
            # 只在Flask应用上下文中才能访问数据库
            from flask import current_app
            if current_app:
                from core.db_models import Community
                communities = Community.query.all()
                
                for comm in communities:
                    population = comm.population or 100
                    self.community_profiles[comm.name] = {
                        'id': comm.id,
                        'name': comm.name,
                        'location': comm.location,
                        'latitude': comm.latitude or 29.35,  # 默认都昌县坐标
                        'longitude': comm.longitude or 116.37,
                        'population': population,
                        'elderly_ratio': comm.elderly_ratio or 0.2,
                        'chronic_disease_ratio': comm.chronic_disease_ratio or 0.15,
                        'vulnerability_index': comm.vulnerability_index,
                        'risk_level': comm.risk_level,
                        
                        # 可扩展字段
                        'green_space_ratio': 0.1,  # 默认值，后续可更新
                        'heat_island_index': 0.5,  # 默认值
                        'medical_accessibility': 0.6,  # 默认值
                        'baseline_visits': self._estimate_baseline_visits(population)
                    }
                
                if self.community_profiles:
                    print(f"✅ 加载 {len(self.community_profiles)} 个社区档案")
                    return
            
            # 如果没有应用上下文或没有数据，使用默认配置
            self._setup_default_communities()
            
        except RuntimeError:
            # 没有应用上下文时使用默认配置
            print("⚠️ 无Flask应用上下文，使用默认社区配置")
            self._setup_default_communities()
        except Exception as e:
            print(f"⚠️ 社区数据加载失败: {e}，使用默认配置")
            self._setup_default_communities()
    
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
            if coords and len(coords) == 2:
                longitude, latitude = coords[0], coords[1]
            else:
                latitude = 29.35 + np.random.uniform(-0.05, 0.05)
                longitude = 116.37 + np.random.uniform(-0.05, 0.05)
            self.community_profiles[comm['name']] = {
                'id': i + 1,
                'name': comm['name'],
                'location': f"江西省九江市都昌县{comm['name']}",
                'latitude': latitude,
                'longitude': longitude,
                'population': comm['population'],
                'elderly_ratio': comm['elderly_ratio'],
                'chronic_disease_ratio': comm['chronic_disease_ratio'],
                'green_space_ratio': np.random.uniform(0.05, 0.25),
                'heat_island_index': np.random.uniform(0.3, 0.7),
                'medical_accessibility': np.random.uniform(0.4, 0.8),
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
            'vi_details': vi_result,
            'population': profile.get('population', 0),
            'elderly_ratio': profile.get('elderly_ratio', 0),
            'expected_excess_visits': round(excess_risk_score, 1)
        }
    
    def generate_community_risk_map(self, weather_data, target_date=None, window_days=30, disease_filter=''):
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
            risk = self.calculate_community_risk_score(name, macro_rr, target_date)
            risk['latitude'] = profile.get('latitude', 29.35)
            risk['longitude'] = profile.get('longitude', 116.37)
            risk['green_space_ratio'] = profile.get('green_space_ratio', 0.1)
            risk['heat_island_index'] = profile.get('heat_island_index', 0.5)
            risk['medical_accessibility'] = profile.get('medical_accessibility', 0.6)
            community_risks.append(risk)

        if not community_risks:
            return {
                'map_data': {'type': 'FeatureCollection', 'features': []},
                'rankings': [],
                'summary': {
                    'total_communities': 0,
                    'high_risk_count': 0,
                    'medium_risk_count': 0,
                    'low_risk_count': 0,
                    'total_expected_excess': 0
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
                'methodology': []
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
        medical_summary = self._collect_medical_counts(target_date, window_days, disease_filter=disease_filter)
        counts_by_community = medical_summary['counts_by_community']
        analysis_days = max(1, int(medical_summary['window_days']))

        total_population = sum(
            max(float(item.get('population') or 0.0), 0.0)
            for item in community_risks
        )
        matched_records = int(medical_summary['matched_records'])
        baseline_rate_per_person_day = None
        if total_population > 0 and matched_records > 0:
            baseline_rate_per_person_day = matched_records / (total_population * analysis_days)
        else:
            baseline_samples = []
            for item in community_risks:
                pop = float(item.get('population') or 0.0)
                base = float(item['components'].get('baseline_rate') or 0.0)
                if pop > 0:
                    baseline_samples.append(base / pop)
            baseline_rate_per_person_day = float(np.mean(baseline_samples)) if baseline_samples else 0.0

        expected_lookup = {}
        observed_lookup = {}
        expected_sum = 0.0
        observed_sum = 0
        for item in community_risks:
            community = item['community']
            observed = int(counts_by_community.get(community, 0))
            pop = max(float(item.get('population') or 0.0), 0.0)
            expected = (baseline_rate_per_person_day * pop * analysis_days) if pop > 0 else None
            expected_lookup[community] = expected
            observed_lookup[community] = observed
            if expected is not None:
                expected_sum += expected
            observed_sum += observed

        global_sir = (observed_sum / expected_sum) if expected_sum > 0 else 1.0
        for item in community_risks:
            community = item['community']
            observed = observed_lookup.get(community, 0)
            expected = expected_lookup.get(community)
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
        burden_pct = self._percentile_map({
            item['community']: float(item.get('smoothed_sir') or item.get('sir') or 1.0)
            for item in community_risks
        })

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
            burden = burden_pct.get(name, 50.0)
            uncertainty_penalty = 0.93 if float(item.get('uncertainty_index') or 0.0) >= 70 else 1.0
            risk_index = (0.45 * hazard_pct + 0.35 * svi_percentile + 0.20 * burden) * uncertainty_penalty
            risk_index = float(np.clip(risk_index, 0.0, 100.0))

            heatrisk_level, heatrisk_label, heatrisk_color = self._heatrisk_level_from_index(risk_index)

            impact_score = min(
                100.0,
                risk_index * 0.75 + float(item.get('expected_excess_visits') or 0.0) * 6.0
            )
            likelihood_score = float(item.get('probability_exceed_baseline') or 0.0) * 100.0
            if item.get('certainty') == 'high':
                likelihood_score += 10.0
            elif item.get('certainty') == 'low':
                likelihood_score -= 10.0
            likelihood_score = float(np.clip(likelihood_score, 0.0, 100.0))

            impact_bucket = self._to_four_level_bucket(impact_score)
            likelihood_bucket = self._to_four_level_bucket(likelihood_score)
            matrix_counts[impact_bucket][likelihood_bucket] += 1
            matrix_score = impact_rank[impact_bucket] * likelihood_rank[likelihood_bucket]

            item['svi_percentile'] = round(svi_percentile, 1)
            item['theme_scores'] = {
                'sensitivity': round(sensitivity_pct.get(name, 0.0), 1),
                'exposure': round(exposure_pct.get(name, 0.0), 1),
                'adaptive_gap': round(adaptive_gap_pct.get(name, 0.0), 1)
            }
            item['burden_percentile'] = round(float(burden), 1)
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
                    'heatrisk_level': row.get('heatrisk_level', 0),
                    'uncertainty_index': row.get('uncertainty_index', 0.0),
                    'hotspot_category': row.get('hotspot_category', '无显著')
                }
            }
            geojson_features.append(feature)

        map_data = {
            'type': 'FeatureCollection',
            'features': geojson_features
        }

        management_suggestions = self._generate_management_suggestions(rankings[:5], weather_data)

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
            uncertainty_values.append(float(row.get('uncertainty_index') or 0.0))
        median_uncertainty = float(np.median(uncertainty_values)) if uncertainty_values else 0.0

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
                {'community': row['community'], 'value': row.get('uncertainty_index', 0.0)}
                for row in rankings
            ],
            'hotspot': [
                {'community': row['community'], 'category': row.get('hotspot_category', '无显著')}
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
                avg_uncertainty = float(np.mean([float(r.get('uncertainty_index') or 0.0) for r in rows]))
                high_heatrisk = sum(1 for r in rows if int(r.get('heatrisk_level') or 0) >= 3)
            else:
                avg_risk_index = 0.0
                avg_uncertainty = 0.0
                high_heatrisk = 0
            quartile_rows.append({
                'stratum': key,
                'label': label,
                'count': len(rows),
                'avg_risk_index': round(avg_risk_index, 1),
                'avg_uncertainty': round(avg_uncertainty, 1),
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

        return {
            'map_data': map_data,
            'rankings': [
                {
                    'rank': row['rank'],
                    'community': row['community'],
                    'latitude': row.get('latitude'),
                    'longitude': row.get('longitude'),
                    'risk_score': row['normalized_score'],
                    'risk_level': row['risk_level'],
                    'population': row['population'],
                    'elderly_ratio': row.get('elderly_ratio'),
                    'chronic_disease_ratio': row.get('chronic_disease_ratio'),
                    'vulnerability_index': row['components'].get('vulnerability_index'),
                    'expected_excess_visits': row['expected_excess_visits'],
                    'relative_index': row.get('relative_index', 100.0),
                    'percentile_rank': row.get('percentile_rank', 0.0),
                    'risk_index': row.get('risk_index', row.get('normalized_score', 0.0)),
                    'heatrisk_level': row.get('heatrisk_level', 0),
                    'heatrisk_label': row.get('heatrisk_label', '最小'),
                    'heatrisk_color': row.get('heatrisk_color', '#16a34a'),
                    'svi_percentile': row.get('svi_percentile', 0.0),
                    'theme_scores': row.get('theme_scores', {}),
                    'observed_cases': row.get('observed_cases', 0),
                    'expected_cases': row.get('expected_cases'),
                    'sir': row.get('sir'),
                    'ci_low': row.get('ci_low'),
                    'ci_high': row.get('ci_high'),
                    'smoothed_sir': row.get('smoothed_sir'),
                    'probability_exceed_baseline': row.get('probability_exceed_baseline', 0.5),
                    'certainty': row.get('certainty', 'low'),
                    'uncertainty_index': row.get('uncertainty_index', 100.0),
                    'hotspot_category': row.get('hotspot_category', '无显著'),
                    'hotspot_z': row.get('hotspot_z', 0.0),
                    'hotspot_p': row.get('hotspot_p', 1.0),
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
                'total_expected_excess': sum(row['expected_excess_visits'] for row in rankings),
                'analysis_date': str(target_date),
                'window_days': analysis_days,
                'disease_filter': disease_filter or '',
                'matched_records': medical_summary['matched_records'],
                'total_records': medical_summary['total_records'],
                'unmatched_records': medical_summary['unmatched_records'],
                'data_coverage_ratio': round(data_coverage_ratio, 4) if data_coverage_ratio is not None else None,
                'baseline_rate_per_person_day': round(baseline_rate_per_person_day, 8) if baseline_rate_per_person_day is not None else None,
                'median_uncertainty_index': round(median_uncertainty, 1),
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
                '社区风险=天气危险度(45%)+SVI-like脆弱性(35%)+历史负担(20%)，并对高不确定性样本执行惩罚。',
                '历史负担采用 SIR + 95%CI，并使用经验贝叶斯平滑抑制小样本波动。',
                '不确定性同时展示 CI 宽度与 P(RR>1) 概率，避免仅给单点值。',
                '空间热点采用 Getis-Ord Gi* 思路给出显著性分级（95%/99%）。',
                '行动优先级使用 Impact×Likelihood 四级矩阵（1-16分）支持自动决策。',
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
        
        # 巡访建议
        for comm in high_risk_communities[:3]:
            if comm.get('elderly_ratio', 0) > 0.4:
                suggestions.append({
                    'category': '健康巡访',
                    'priority': 'high',
                    'advice': f'{comm["community"]} 老龄化程度高({comm["elderly_ratio"]*100:.0f}%)，建议加强独居老人巡访',
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
        
        # 门诊准备
        total_excess = sum(c.get('expected_excess_visits', 0) for c in high_risk_communities)
        if total_excess > 10:
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
    
    def get_community_profile(self, community_name):
        """获取社区档案"""
        if community_name in self.community_profiles:
            profile = self.community_profiles[community_name].copy()
            vi_result = self.calculate_vulnerability_index(profile)
            profile['vulnerability_details'] = vi_result
            return profile
        return None
    
    def get_all_communities(self):
        """获取所有社区列表"""
        communities = []
        for name, profile in self.community_profiles.items():
            vi_result = self.calculate_vulnerability_index(profile)
            communities.append({
                'name': name,
                'population': profile.get('population', 0),
                'elderly_ratio': profile.get('elderly_ratio', 0),
                'chronic_disease_ratio': profile.get('chronic_disease_ratio', 0),
                'vulnerability_index': vi_result['vulnerability_index'],
                'vulnerability_level': vi_result['level']
            })
        
        # 按VI排序
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

