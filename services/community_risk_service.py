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
from flask import current_app, has_app_context


class CommunityRiskService:
    """社区风险评估服务"""

    # 这些字段全部参与风险、就诊增量或行动优先级计算。
    # 任一字段缺失时整个社区关闭计算，不用代理值补齐。
    REQUIRED_RISK_PROFILE_FIELDS = (
        'population',
        'elderly_ratio',
        'chronic_disease_ratio',
        'green_space_ratio',
        'heat_island_index',
        'medical_accessibility',
        'baseline_visits',
    )
    PROFILE_FIELD_LABELS = {
        'population': '人口',
        'elderly_ratio': '老龄率',
        'chronic_disease_ratio': '慢病率',
        'green_space_ratio': '绿地率',
        'heat_island_index': '热岛指数',
        'medical_accessibility': '医疗可达性',
        'baseline_visits': '实测基线门诊量',
    }
    
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
    def _finite_number(value):
        """转换有限浮点数；空值、布尔值和非有限数均视为无效。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _profile_readiness(self, profile):
        """评估社区档案是否可参与风险计算。"""
        missing_fields = []
        invalid_fields = []

        for field_name in self.REQUIRED_RISK_PROFILE_FIELDS:
            raw_value = profile.get(field_name)
            if raw_value is None or raw_value == '':
                missing_fields.append(field_name)
                continue

            number = self._finite_number(raw_value)
            if number is None:
                invalid_fields.append(field_name)
                continue

            if field_name in {'population', 'baseline_visits'} and number <= 0:
                invalid_fields.append(field_name)
            elif field_name in {
                'elderly_ratio',
                'chronic_disease_ratio',
                'green_space_ratio',
                'heat_island_index',
                'medical_accessibility',
            } and not 0.0 <= number <= 1.0:
                invalid_fields.append(field_name)

        uses_proxy_values = bool(profile.get('uses_proxy_values'))
        ready = not missing_fields and not invalid_fields and not uses_proxy_values
        issue_labels = [
            self.PROFILE_FIELD_LABELS.get(field_name, field_name)
            for field_name in (*missing_fields, *invalid_fields)
        ]
        if uses_proxy_values:
            issue_labels.append('存在未核验代理值')

        if ready:
            message = '字段完整，可参与排名。'
            status = 'available'
        else:
            detail = '、'.join(issue_labels) if issue_labels else '关键字段未核验'
            message = f'数据不足，未参与排名：{detail}。'
            status = 'insufficient_vulnerability_data'

        return {
            'ready': ready,
            'status': status,
            'message': message,
            'missing_fields': missing_fields,
            'invalid_fields': invalid_fields,
            'uses_proxy_values': uses_proxy_values,
        }

    def _configured_coordinate(self, community_name):
        """只从运行时 GCJ-02 权威配置读取同名社区坐标。"""
        coords_map = {}
        if has_app_context():
            configured = current_app.config.get('COMMUNITY_COORDS_GCJ')
            if isinstance(configured, dict):
                coords_map = configured

        raw_coords = coords_map.get(community_name)
        if not isinstance(raw_coords, (list, tuple)) or len(raw_coords) != 2:
            return {
                'available': False,
                'longitude': None,
                'latitude': None,
                'source': None,
                'status': 'missing_in_config',
                'message': 'COMMUNITY_COORDS_GCJ 无同名有效坐标，未使用数据库坐标。',
            }

        longitude = self._finite_number(raw_coords[0])
        latitude = self._finite_number(raw_coords[1])
        if (
            longitude is None
            or latitude is None
            or not -180.0 <= longitude <= 180.0
            or not -90.0 <= latitude <= 90.0
        ):
            return {
                'available': False,
                'longitude': None,
                'latitude': None,
                'source': None,
                'status': 'invalid_config_coordinate',
                'message': 'COMMUNITY_COORDS_GCJ 同名坐标无效，未使用数据库坐标。',
            }

        return {
            'available': True,
            'longitude': longitude,
            'latitude': latitude,
            'source': 'config.COMMUNITY_COORDS_GCJ',
            'status': 'available',
            'message': '坐标来自 config.COMMUNITY_COORDS_GCJ 同名项。',
        }

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
            if row.get('ranking_eligible') is not True:
                row['hotspot_z'] = None
                row['hotspot_p'] = None
                row['hotspot_category'] = '数据不足'
            elif not row.get('historical_component_available'):
                row['hotspot_z'] = None
                row['hotspot_p'] = None
                row['hotspot_category'] = '数据不足'
            elif row.get('coordinate_available') is not True:
                row['hotspot_z'] = None
                row['hotspot_p'] = None
                row['hotspot_category'] = '无坐标'
            else:
                row['hotspot_z'] = None
                row['hotspot_p'] = None
                row['hotspot_category'] = '样本不足'

        valid_rows = [
            row for row in rows
            if row.get('ranking_eligible') is True
            and row.get('coordinate_available') is True
            and row.get('historical_component_available')
            and (row.get('smoothed_sir') is not None or row.get('sir') is not None)
            and row.get('longitude') is not None and row.get('latitude') is not None
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
        """加载社区档案数据。

        Flask 应用上下文中只信任 Community 表。空表或查询失败时保持空集，
        避免内置演示村落被误发布为正式排名。无应用上下文的纯离线运行保留演示档案。
        """
        self.community_profiles = {}

        if not has_app_context():
            self._setup_default_communities()
            self.community_profile_status = {
                'available': False,
                'code': 'offline_demo',
                'source': 'built_in_demo_profiles',
                'message': '当前为离线演示档案，代理字段不参与排名。',
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
                coordinate = self._configured_coordinate(comm.name)
                profile = {
                    'id': comm.id,
                    'name': comm.name,
                    'location': comm.location,
                    # 地图坐标只接受运行时 GCJ-02 同名配置。
                    # Community 表坐标可能是其他坐标系，不在此处静默兜底。
                    'latitude': coordinate['latitude'],
                    'longitude': coordinate['longitude'],
                    'coordinate_available': coordinate['available'],
                    'coordinate_source': coordinate['source'],
                    'coordinate_status': coordinate['status'],
                    'coordinate_message': coordinate['message'],
                    'population': comm.population,
                    'elderly_ratio': comm.elderly_ratio,
                    'chronic_disease_ratio': comm.chronic_disease_ratio,
                    'vulnerability_index': comm.vulnerability_index,
                    'risk_level': comm.risk_level,
                    # 现有 ORM 可能尚未定义下列字段。缺失时保持 None，
                    # 等待经核验的真实字段接入，不估算、不 seed。
                    'green_space_ratio': getattr(comm, 'green_space_ratio', None),
                    'heat_island_index': getattr(comm, 'heat_island_index', None),
                    'medical_accessibility': getattr(comm, 'medical_accessibility', None),
                    'baseline_visits': getattr(comm, 'baseline_visits', None),
                    'uses_proxy_values': False,
                }
                profile['profile_readiness'] = self._profile_readiness(profile)
                loaded_profiles[comm.name] = profile

            self.community_profiles = loaded_profiles
            eligible_count = sum(
                1 for profile in loaded_profiles.values()
                if profile['profile_readiness']['ready']
            )
            if eligible_count:
                status_code = 'available'
                status_message = (
                    f'已加载 {len(loaded_profiles)} 个社区档案，'
                    f'{eligible_count} 个字段完整并参与排名。'
                )
            else:
                status_code = 'insufficient_vulnerability_data'
                status_message = (
                    f'已加载 {len(loaded_profiles)} 个社区档案，'
                    '但均缺少关键脆弱性或实测基线字段，未参与排名。'
                )
            self.community_profile_status = {
                'available': eligible_count > 0,
                'code': status_code,
                'source': 'community_table',
                'message': status_message,
                'total_count': len(loaded_profiles),
                'eligible_count': eligible_count,
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
                'baseline_visits': self._estimate_baseline_visits(comm['population']),
                # 离线演示档案仅保留向后兼容，实际风险计算会因此标志关闭。
                'uses_proxy_values': True,
                'coordinate_available': False,
                'coordinate_source': None,
                'coordinate_status': 'offline_demo_coordinate',
                'coordinate_message': '离线演示坐标未经 COMMUNITY_COORDS_GCJ 运行时核验。',
            }
            self.community_profiles[comm['name']]['profile_readiness'] = self._profile_readiness(
                self.community_profiles[comm['name']]
            )
    
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
        readiness = self._profile_readiness(community_data)
        if not readiness['ready']:
            return {
                'vulnerability_index': None,
                'level': '数据不足',
                'color': 'secondary',
                'breakdown': {},
                'interpretation': readiness['message'],
                'ranking_eligible': False,
                'data_status': readiness['status'],
                'data_message': readiness['message'],
                'missing_fields': readiness['missing_fields'],
                'invalid_fields': readiness['invalid_fields'],
                'uses_proxy_values': readiness['uses_proxy_values'],
            }

        # 通过完整性门后只使用实际字段，不含默认值。
        elderly_ratio = float(community_data['elderly_ratio'])
        chronic_ratio = float(community_data['chronic_disease_ratio'])
        green_ratio = float(community_data['green_space_ratio'])
        heat_island = float(community_data['heat_island_index'])
        medical_access = float(community_data['medical_accessibility'])
        
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
            'ranking_eligible': True,
            'data_status': 'available',
            'data_message': '字段完整，可参与排名。',
            'missing_fields': [],
            'invalid_fields': [],
            'uses_proxy_values': False,
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
        if not vi_result.get('ranking_eligible'):
            return {
                'community': community_name,
                'ranking_eligible': False,
                'data_status': vi_result.get('data_status', 'insufficient_vulnerability_data'),
                'data_message': vi_result.get('data_message', '数据不足，未参与排名。'),
                'missing_fields': vi_result.get('missing_fields', []),
                'invalid_fields': vi_result.get('invalid_fields', []),
                'uses_proxy_values': vi_result.get('uses_proxy_values', False),
                'risk_score': None,
                'normalized_score': None,
                'risk_level': '数据不足',
                'color': 'secondary',
                'components': {
                    'weather_rr': None,
                    'vulnerability_index': None,
                    'baseline_rate': None,
                    'excess_risk_score': None,
                },
                'hazard_formula': None,
                'vi_details': vi_result,
                'population': profile.get('population'),
                'elderly_ratio': profile.get('elderly_ratio'),
                'chronic_disease_ratio': profile.get('chronic_disease_ratio'),
                'expected_excess_visits': None,
            }
        vi = vi_result['vulnerability_index']

        # 基线门诊量已通过完整性门，只读取实测值。
        baseline_rate = float(profile['baseline_visits'])
        
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
            'ranking_eligible': True,
            'data_status': 'available',
            'data_message': '字段完整，已参与排名。',
            'missing_fields': [],
            'invalid_fields': [],
            'uses_proxy_values': False,
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
            'population': profile['population'],
            'elderly_ratio': profile['elderly_ratio'],
            'chronic_disease_ratio': profile['chronic_disease_ratio'],
            'expected_excess_visits': round(excess_risk_score, 1)
        }

    @staticmethod
    def _exploratory_omitted_fields(evidence_metadata):
        """合并证据模块与完整画像门中统一省略的字段说明。"""
        omitted_by_field = {}
        for item in evidence_metadata.get('omitted_fields', []):
            if not isinstance(item, dict):
                continue
            field_name = item.get('field')
            if field_name:
                omitted_by_field[field_name] = dict(item)

        additional_fields = (
            (
                'green_space_ratio',
                'ESA 树木覆盖类别与社区总绿地率口径不同，本次只使用 tree_cover_pct。',
            ),
            (
                'heat_island_index',
                'NASA 历史夏季地表温度与社区热岛指数口径不同，本次只使用 q3_lst_c_mean。',
            ),
            (
                'medical_accessibility',
                '缺少同口径、可追溯的 16 社区共同医疗可达性来源。',
            ),
        )
        for field_name, reason in additional_fields:
            omitted_by_field.setdefault(field_name, {
                'field': field_name,
                'reason': reason,
            })

        field_order = (
            'population',
            'elderly_ratio',
            'chronic_disease_ratio',
            'green_space_ratio',
            'heat_island_index',
            'medical_accessibility',
            'baseline_visits',
            'medical_records',
            'vulnerability_index',
            'risk_level',
        )
        ordered = [
            omitted_by_field[field_name]
            for field_name in field_order
            if field_name in omitted_by_field
        ]
        ordered.extend(
            item for field_name, item in omitted_by_field.items()
            if field_name not in field_order
        )
        return ordered

    def _exploratory_community_names(self):
        """从项目规范坐标中取得可做公开空间筛查的社区名。"""
        if not has_app_context():
            return []

        from config import CITY_LOCATION_MAP

        configured_coords = current_app.config.get('COMMUNITY_COORDS_GCJ') or {}
        configured_names = list(configured_coords) if isinstance(configured_coords, dict) else []
        profile_names = list(self.community_profiles) if isinstance(self.community_profiles, dict) else []
        candidates = configured_names or profile_names
        return [name for name in candidates if name in CITY_LOCATION_MAP]

    def get_ranking_input_signature(self):
        """生成社区画像、坐标和冻结 GIS 证据共同输入指纹。"""
        if has_app_context():
            self._load_community_profiles()

        from config import CITY_LOCATION_MAP
        from services.community_vulnerability_evidence import get_evidence_bundle_sha256

        configured_coords = (
            current_app.config.get('COMMUNITY_COORDS_GCJ')
            if has_app_context()
            else {}
        ) or {}
        profile_fields = self.REQUIRED_RISK_PROFILE_FIELDS + (
            'vulnerability_index',
            'risk_level',
        )
        profiles = []
        for name in sorted(self.community_profiles):
            profile = self.community_profiles[name]
            profiles.append({
                'name': name,
                **{field: profile.get(field) for field in profile_fields},
            })

        exploratory_communities = self._exploratory_community_names()
        signature_payload = {
            'profiles': profiles,
            'exploratory_communities': exploratory_communities,
            'evidence_coordinates_wgs84': {
                name: CITY_LOCATION_MAP.get(name)
                for name in exploratory_communities
            },
            'display_coordinates': {
                str(name): configured_coords[name]
                for name in sorted(configured_coords)
            } if isinstance(configured_coords, dict) else {},
            'evidence_bundle_sha256': get_evidence_bundle_sha256(),
        }
        encoded = json.dumps(
            signature_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def generate_exploratory_geospatial_screening(
        self,
        *,
        target_date=None,
        window_days=30,
        disease_filter='',
    ):
        """在实时天气不可用时独立生成冻结公开 GIS 筛查。"""
        if has_app_context():
            self._load_community_profiles()

        try:
            from core.time_utils import today_local
        except Exception:
            today_local = lambda: datetime.now().date()  # noqa: E731

        if target_date is None:
            target_date = today_local()
        try:
            normalized_window_days = max(7, min(int(window_days or 30), 120))
        except (TypeError, ValueError):
            normalized_window_days = 30

        return self._build_exploratory_geospatial_screening_result(
            temperature=None,
            macro_rr=None,
            lag_temperatures=[],
            target_date=target_date,
            window_days=normalized_window_days,
            disease_filter=disease_filter,
        )

    def _build_exploratory_geospatial_screening_result(
        self,
        *,
        temperature,
        macro_rr,
        lag_temperatures,
        target_date,
        window_days,
        disease_filter,
    ):
        """在完整画像无人过门时，尝试构建公开 GIS 探索性筛查结果。"""
        community_names = self._exploratory_community_names()
        if not community_names:
            return None

        from services.community_vulnerability_evidence import (
            RANKING_MODE,
            build_exploratory_rankings,
        )

        evidence_result = build_exploratory_rankings(community_names)
        ranking_status = evidence_result.get('status')
        evidence_rankings = evidence_result.get('rankings')
        evidence_metadata = evidence_result.get('metadata') or {}
        weather_context_available = (
            self._finite_number(temperature) is not None
            and self._finite_number(macro_rr) is not None
        )
        if ranking_status not in {'available', 'partial'} or not evidence_rankings:
            reason = evidence_metadata.get('reason') or '冻结公开 GIS 证据当前不可用。'
            requested_count = int(
                evidence_metadata.get('requested_community_count')
                or len(community_names)
            )
            omitted_field_details = self._exploratory_omitted_fields(evidence_metadata)
            methodology = [
                f'探索性社区空间筛查暂不可用：{reason}',
                '当前没有使用旧代理值、哈希生成字段或数据库默认值恢复排名。',
                (
                    '正式健康风险、预计额外就诊、O/E、SIR、Gi*、Impact×Likelihood '
                    '与医疗资源建议继续保持关闭。'
                ),
            ]
            ranking_metadata = dict(evidence_metadata)
            ranking_metadata.update({
                'ranking_mode': RANKING_MODE,
                'status': 'unavailable',
                'methodology': methodology,
                'omitted_fields': omitted_field_details,
                'clinical_outputs_enabled': False,
                'weather_used_in_ranking': False,
                'weather_context_available': weather_context_available,
                'request_filters_used': False,
                'full_profile_ranked_count': 0,
            })
            return {
                'data_available': False,
                'data_status': {
                    'available': False,
                    'code': 'exploratory_geospatial_screening_unavailable',
                    'source': 'frozen_public_gis_bundle',
                    'message': reason,
                },
                'ranking_mode': RANKING_MODE,
                'ranking_status': 'unavailable',
                'ranking_metadata': ranking_metadata,
                'map_data': {
                    'type': 'FeatureCollection',
                    'features': [],
                    'unmapped_communities': list(community_names),
                },
                'rankings': [],
                'summary': {
                    'data_available': False,
                    'data_status': 'exploratory_geospatial_screening_unavailable',
                    'data_message': reason,
                    'ranking_mode': RANKING_MODE,
                    'ranking_status': 'unavailable',
                    'ranking_eligible_communities': 0,
                    'ranking_excluded_communities': requested_count,
                    'ranking_unique_cells': 0,
                    'total_communities': requested_count,
                    'ranked_communities': 0,
                    'unranked_communities': requested_count,
                    'missing_coordinate_count': 0,
                    'high_risk_count': 0,
                    'medium_risk_count': 0,
                    'low_risk_count': 0,
                    'total_expected_excess': None,
                    'analysis_date': str(target_date),
                    'window_days': None,
                    'disease_filter': '',
                    'requested_window_days': window_days,
                    'requested_disease_filter': disease_filter or '',
                    'request_filters_used': False,
                    'historical_component_available': False,
                    'risk_weights': {},
                    'screening_level_counts': {},
                    'used_evidence_fields': [],
                    'omitted_fields': [
                        item['field'] for item in omitted_field_details
                    ],
                },
                'macro_weather': {
                    'available': weather_context_available,
                    'temperature': temperature if weather_context_available else None,
                    'rr': round(float(macro_rr), 3) if weather_context_available else None,
                    'lag_temperatures_used': (
                        len(lag_temperatures) if weather_context_available and lag_temperatures else 0
                    ),
                    'used_in_ranking': False,
                    'role': 'context_only' if weather_context_available else 'unavailable',
                },
                'impact_likelihood_matrix': {
                    'data_available': False,
                    'impact_levels': [],
                    'likelihood_levels': [],
                    'counts': {},
                },
                'layers': {
                    'risk_index': [],
                    'vulnerability': [],
                    'uncertainty': [],
                    'hotspot': [],
                },
                'equity_stratification': {
                    'quartiles': [],
                    'priority_communities': [],
                },
                'methodology': methodology,
                'management_suggestions': [],
            }

        ranking_metadata = dict(evidence_metadata)
        omitted_field_details = self._exploratory_omitted_fields(evidence_metadata)
        omitted_field_names = [item['field'] for item in omitted_field_details]
        eligible_count = len(evidence_rankings)
        requested_count = int(
            ranking_metadata.get('requested_community_count')
            or len(self.community_profiles)
        )
        excluded_count = int(
            ranking_metadata.get('excluded_community_count')
            or max(requested_count - eligible_count, 0)
        )

        data_message = (
            f'{eligible_count} 个社区已按共同公开 GIS 证据完成探索性相对脆弱性筛查；'
            'ASPECT 65+ 占比、NASA 历史夏季地表温度与 ESA 低树木覆盖三主题等权计分。'
        )
        if excluded_count:
            data_message += f' {excluded_count} 个社区因空间证据不完整未进入筛查。'

        methodology_details = evidence_metadata.get('methodology')
        methodology = [
            data_message,
            (
                '筛查得分=(老年人口模型化比例分位+历史夏季地表温度分位+'
                '低树木覆盖分位)/3；各主题使用同一批证据完整社区作参照。'
            ),
            (
                '社区证据落格使用 CITY_LOCATION_MAP 的 WGS84 点与原生 MODIS Polygon '
                '包含判定；网页显示继续使用 config.COMMUNITY_COORDS_GCJ。'
            ),
            '相同未四舍五入得分使用 dense rank 并列名次；共享同一约 1 km 网格的社区保留为并列行。',
            'Q3 观测覆盖率是必需的证据质量字段，仅用于展示，不进入综合分。',
            (
                '人口、数据库老龄率、慢病率、社区绿地率、热岛指数、医疗可达性、'
                '实测门诊基线与病历记录统一省略，不进入本次筛查。'
            ),
            (
                '本模式只提供社区间探索性筛查顺序；临床健康风险、预计额外就诊、'
                'O/E、SIR、Gi*、Impact×Likelihood 与医疗资源建议均保持关闭。'
            ),
        ]
        ranking_metadata.update({
            'ranking_mode': RANKING_MODE,
            'status': ranking_status,
            'methodology_details': (
                dict(methodology_details)
                if isinstance(methodology_details, dict)
                else methodology_details
            ),
            'methodology': methodology,
            'omitted_fields': omitted_field_details,
            'clinical_outputs_enabled': False,
            'weather_used_in_ranking': False,
            'weather_context_available': weather_context_available,
            'request_filters_used': False,
            'full_profile_ranked_count': 0,
        })

        rankings = []
        for evidence_row in evidence_rankings:
            community = evidence_row['community']
            coordinate = self._configured_coordinate(community)
            row = dict(evidence_row)
            row.update({
                'ranking_mode': RANKING_MODE,
                'ranking_eligible': True,
                'data_status': 'exploratory_geospatial_screening',
                'data_message': '共同公开 GIS 证据完整，已参与探索性筛查。',
                'missing_fields': [],
                'invalid_fields': [],
                'omitted_fields': list(omitted_field_names),
                'omitted_field_details': [dict(item) for item in omitted_field_details],
                'uses_proxy_values': False,
                'uses_modelled_geospatial_values': True,
                'latitude': coordinate['latitude'],
                'longitude': coordinate['longitude'],
                'coordinate_available': coordinate['available'],
                'coordinate_source': coordinate['source'],
                'coordinate_status': coordinate['status'],
                'coordinate_message': coordinate['message'],
                'risk_score': None,
                'risk_level': None,
                'population': None,
                'elderly_ratio': None,
                'chronic_disease_ratio': None,
                'vulnerability_index': None,
                'expected_excess_visits': None,
                'relative_index': None,
                'percentile_rank': None,
                'risk_index': None,
                'weather_hazard_score': None,
                'historical_component_available': False,
                'burden_percentile': None,
                'uncertainty_penalty': None,
                'risk_weights': {},
                'risk_contributions': {},
                'hazard_formula': None,
                'heatrisk_level': None,
                'heatrisk_label': None,
                'heatrisk_color': None,
                'svi_percentile': None,
                'theme_scores': {},
                'observed_cases': None,
                'expected_cases': None,
                'sir': None,
                'ci_low': None,
                'ci_high': None,
                'smoothed_sir': None,
                'probability_exceed_baseline': None,
                'certainty': 'unavailable',
                'uncertainty_index': None,
                'hotspot_category': None,
                'hotspot_z': None,
                'hotspot_p': None,
                'impact_bucket': None,
                'likelihood_bucket': None,
                'matrix_score': None,
                'equity_stratum': None,
            })
            rankings.append(row)

        geojson_features = []
        for row in rankings:
            if row.get('coordinate_available') is not True:
                continue
            geojson_features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [row['longitude'], row['latitude']],
                },
                'properties': {
                    'name': row['community'],
                    'ranking_mode': RANKING_MODE,
                    'rank': row['rank'],
                    'rank_label': row['rank_label'],
                    'is_tied': row['is_tied'],
                    'tie_count': row['tie_count'],
                    'screening_score': row['screening_score'],
                    'screening_level': row['screening_level'],
                    'screening_label': row['screening_label'],
                    'screening_color': row['screening_color'],
                    'cell_id': row['cell_id'],
                    'raw_values': dict(row['raw_values']),
                },
            })

        q3_coverage_values = [
            float(row['raw_values']['q3_coverage_pct'])
            for row in rankings
        ]
        screening_level_counts = {}
        for row in rankings:
            level = row['screening_level']
            screening_level_counts[level] = screening_level_counts.get(level, 0) + 1

        summary = {
            'data_available': True,
            'data_status': 'exploratory_geospatial_screening',
            'data_message': data_message,
            'ranking_mode': RANKING_MODE,
            'ranking_status': ranking_status,
            'ranking_method_version': ranking_metadata.get('method_version'),
            'ranking_eligible_communities': eligible_count,
            'ranking_excluded_communities': excluded_count,
            'ranking_unique_cells': ranking_metadata.get('unique_cell_count', 0),
            'total_communities': requested_count,
            'ranked_communities': eligible_count,
            'unranked_communities': excluded_count,
            'missing_coordinate_count': sum(
                1 for row in rankings if row.get('coordinate_available') is not True
            ),
            'ranked_missing_coordinate_count': sum(
                1 for row in rankings if row.get('coordinate_available') is not True
            ),
            'high_risk_count': 0,
            'medium_risk_count': 0,
            'low_risk_count': 0,
            'total_expected_excess': None,
            'analysis_date': str(target_date),
            'window_days': None,
            'disease_filter': '',
            'requested_window_days': window_days,
            'requested_disease_filter': disease_filter or '',
            'request_filters_used': False,
            'matched_records': None,
            'total_records': None,
            'unmatched_records': None,
            'excluded_incomplete_profile_records': None,
            'historical_component_available': False,
            'risk_weights': {},
            'data_coverage_ratio': None,
            'evidence_coverage_ratio': round(eligible_count / requested_count, 4),
            'baseline_rate_per_person_day': None,
            'median_uncertainty_index': None,
            'heatrisk_counts': {},
            'hotspot_counts': {},
            'equity_priority_count': 0,
            'screening_level_counts': screening_level_counts,
            'q3_coverage_median_pct': round(float(np.median(q3_coverage_values)), 2),
            'used_evidence_fields': list(
                ranking_metadata.get('required_evidence_fields', [])
            ),
            'omitted_fields': list(omitted_field_names),
        }

        # 保留所有候选社区的可见性；证据不足的社区以灰色未排名行展示原因。
        for excluded in evidence_metadata.get('excluded_communities') or []:
            community = excluded.get('community')
            if not community:
                continue
            coordinate = self._configured_coordinate(community)
            rankings.append({
                'community': community,
                'ranking_mode': RANKING_MODE,
                'ranking_eligible': False,
                'data_status': 'exploratory_evidence_incomplete',
                'data_message': excluded.get('reason') or '共同空间证据不完整。',
                'reason_code': excluded.get('reason_code'),
                'missing_fields': [],
                'invalid_fields': list(excluded.get('invalid_fields') or []),
                'omitted_fields': list(omitted_field_names),
                'omitted_field_details': [dict(item) for item in omitted_field_details],
                'uses_proxy_values': False,
                'uses_modelled_geospatial_values': True,
                'latitude': coordinate['latitude'],
                'longitude': coordinate['longitude'],
                'coordinate_available': coordinate['available'],
                'coordinate_source': coordinate['source'],
                'coordinate_status': coordinate['status'],
                'coordinate_message': coordinate['message'],
                'rank': None,
                'rank_label': '未进入筛查',
                'is_tied': False,
                'tie_count': 0,
                'screening_score': None,
                'screening_level': None,
                'screening_label': '证据不足',
                'screening_color': '#94a3b8',
                'cell_id': excluded.get('cell_id'),
                'raw_values': {},
                'theme_percentiles': {},
                'risk_score': None,
                'risk_level': None,
                'population': None,
                'elderly_ratio': None,
                'chronic_disease_ratio': None,
                'vulnerability_index': None,
                'expected_excess_visits': None,
                'risk_index': None,
                'weather_hazard_score': None,
                'observed_cases': None,
                'expected_cases': None,
                'sir': None,
                'ci_low': None,
                'ci_high': None,
                'smoothed_sir': None,
                'probability_exceed_baseline': None,
                'uncertainty_index': None,
                'hotspot_category': None,
                'matrix_score': None,
            })

        return {
            'data_available': True,
            'data_status': {
                'available': True,
                'code': 'exploratory_geospatial_screening',
                'source': 'frozen_public_gis_bundle',
                'message': data_message,
            },
            'ranking_mode': RANKING_MODE,
            'ranking_status': ranking_status,
            'ranking_metadata': ranking_metadata,
            'map_data': {
                'type': 'FeatureCollection',
                'features': geojson_features,
                'unmapped_communities': [
                    row['community']
                    for row in rankings
                    if row.get('coordinate_available') is not True
                ],
            },
            'rankings': rankings,
            'summary': summary,
            'macro_weather': {
                'available': weather_context_available,
                'temperature': temperature if weather_context_available else None,
                'rr': round(float(macro_rr), 3) if weather_context_available else None,
                'lag_temperatures_used': (
                    len(lag_temperatures) if weather_context_available and lag_temperatures else 0
                ),
                'used_in_ranking': False,
                'role': 'context_only' if weather_context_available else 'unavailable',
            },
            'impact_likelihood_matrix': {
                'data_available': False,
                'impact_levels': [],
                'likelihood_levels': [],
                'counts': {},
            },
            'layers': {
                'screening_score': [
                    {'community': row['community'], 'value': row['screening_score']}
                    for row in rankings
                ],
                'risk_index': [],
                'vulnerability': [],
                'uncertainty': [],
                'hotspot': [],
            },
            'equity_stratification': {
                'quartiles': [],
                'priority_communities': [],
            },
            'methodology': methodology,
            'management_suggestions': [],
        }

    def _attach_optional_screening_weather_context(self, result, weather_data):
        """静态筛查建成后再尝试附加 DLNM 天气上下文。"""
        if not isinstance(result, dict) or not isinstance(weather_data, dict):
            return result

        temperature = self._finite_number(weather_data.get('temperature'))
        if temperature is None:
            return result
        lag_temperatures = self._extract_lag_temperatures(weather_data, temperature)

        try:
            from services.dlnm_risk_service import get_dlnm_service

            dlnm = get_dlnm_service()
            if lag_temperatures:
                macro_rr, _ = dlnm.calculate_rr(
                    temperature,
                    lag_temperatures=lag_temperatures,
                )
            else:
                macro_rr, _ = dlnm.calculate_rr(temperature)
            macro_rr = self._finite_number(macro_rr)
            if macro_rr is None:
                raise ValueError('DLNM 未返回有限 RR')
        except Exception as exc:
            if has_app_context():
                current_app.logger.warning(
                    '探索性 GIS 筛查已生成，DLNM 天气上下文附加失败: %s',
                    exc,
                )
            return result

        result['macro_weather'] = {
            'available': True,
            'temperature': temperature,
            'rr': round(macro_rr, 3),
            'lag_temperatures_used': len(lag_temperatures) if lag_temperatures else 0,
            'used_in_ranking': False,
            'role': 'context_only',
        }
        ranking_metadata = result.get('ranking_metadata')
        if isinstance(ranking_metadata, dict):
            ranking_metadata['weather_context_available'] = True
        return result

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
        # 社区表可在服务单例创建后继续增改。每次计算前刷新小型档案表，
        # 避免新社区病历被旧内存清单误判为“未匹配”。
        if has_app_context():
            self._load_community_profiles()

        try:
            from core.time_utils import today_local
        except Exception:
            today_local = lambda: datetime.now().date()  # noqa: E731

        try:
            window_days = max(7, min(int(window_days or 30), 120))
        except (TypeError, ValueError):
            window_days = 30
        if target_date is None:
            target_date = today_local()

        has_complete_profile = any(
            self._profile_readiness(profile)['ready']
            for profile in self.community_profiles.values()
        )
        if not has_complete_profile:
            # 先完成与天气、DLNM、病历独立的静态筛查，
            # 随后才尝试附加可失败的天气上下文。
            exploratory_result = self._build_exploratory_geospatial_screening_result(
                temperature=None,
                macro_rr=None,
                lag_temperatures=[],
                target_date=target_date,
                window_days=window_days,
                disease_filter=disease_filter,
            )
            if exploratory_result is not None:
                return self._attach_optional_screening_weather_context(
                    exploratory_result,
                    weather_data,
                )

        from services.dlnm_risk_service import get_dlnm_service

        dlnm = get_dlnm_service()

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
            coordinate = self._configured_coordinate(name)
            risk['latitude'] = coordinate['latitude']
            risk['longitude'] = coordinate['longitude']
            risk['coordinate_available'] = coordinate['available']
            risk['coordinate_source'] = coordinate['source']
            risk['coordinate_status'] = coordinate['status']
            risk['coordinate_message'] = coordinate['message']
            risk['green_space_ratio'] = profile.get('green_space_ratio')
            risk['heat_island_index'] = profile.get('heat_island_index')
            risk['medical_accessibility'] = profile.get('medical_accessibility')
            community_risks.append(risk)

        if not community_risks:
            exploratory_result = self._build_exploratory_geospatial_screening_result(
                temperature=temperature,
                macro_rr=macro_rr,
                lag_temperatures=lag_temperatures,
                target_date=target_date,
                window_days=window_days,
                disease_filter=disease_filter,
            )
            if exploratory_result is not None:
                return exploratory_result

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
                    'ranked_communities': 0,
                    'unranked_communities': 0,
                    'missing_coordinate_count': 0,
                    'total_expected_excess': None,
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
                    'data_available': False,
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

        eligible_risks = [
            item for item in community_risks if item.get('ranking_eligible') is True
        ]
        ineligible_risks = [
            item for item in community_risks if item.get('ranking_eligible') is not True
        ]

        # 完整画像无人通过时，公开 GIS 筛查使用独立字段、独立语义与独立输出。
        # 证据模块无法识别当前 Community 表时继续沿用下方失败关闭结果。
        if not eligible_risks:
            exploratory_result = self._build_exploratory_geospatial_screening_result(
                temperature=temperature,
                macro_rr=macro_rr,
                lag_temperatures=lag_temperatures,
                target_date=target_date,
                window_days=window_days,
                disease_filter=disease_filter,
            )
            if exploratory_result is not None:
                return exploratory_result

        # 3) 相对指数与分位只在字段完整的社区之间计算。
        risk_scores = np.array(
            [float(item['risk_score']) for item in eligible_risks],
            dtype=float,
        )
        mean_score = float(np.mean(risk_scores)) if risk_scores.size else 0.0
        raw_percentiles = self._percentile_map({
            item['community']: float(item['risk_score'])
            for item in eligible_risks
        })
        for item in eligible_risks:
            score = float(item['risk_score'])
            item['relative_index'] = round((score / mean_score * 100.0), 1) if mean_score > 0 else 100.0
            item['percentile_rank'] = round(raw_percentiles.get(item['community'], 0.0), 1)

        # 4) 历史病例窗口，做 SIR / CI / 不确定性
        medical_summary = self._collect_medical_counts(target_date, window_days, disease_filter=disease_filter)
        eligible_names = {item['community'] for item in eligible_risks}
        counts_by_community = {
            name: int(count)
            for name, count in medical_summary['counts_by_community'].items()
            if name in eligible_names
        }
        analysis_days = max(1, int(medical_summary['window_days']))

        total_population = sum(
            float(item['population'])
            for item in eligible_risks
        )
        matched_records = sum(counts_by_community.values())
        excluded_profile_records = max(
            int(medical_summary['matched_records']) - matched_records,
            0,
        )
        historical_component_available = matched_records > 0 and total_population > 0
        baseline_rate_per_person_day = None
        if historical_component_available:
            baseline_rate_per_person_day = matched_records / (total_population * analysis_days)

        expected_lookup = {}
        observed_lookup = {}
        expected_sum = 0.0
        observed_sum = 0
        for item in eligible_risks:
            community = item['community']
            observed = int(counts_by_community.get(community, 0)) if historical_component_available else None
            pop = float(item['population'])
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
        for item in eligible_risks:
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
        for item in eligible_risks:
            name = item['community']
            elderly = float(item['elderly_ratio'])
            chronic = float(item['chronic_disease_ratio'])
            heat_island = float(item['heat_island_index'])
            green_space = float(item['green_space_ratio'])
            medical_access = float(item['medical_accessibility'])
            sensitivity_raw[name] = 0.6 * elderly + 0.4 * chronic
            exposure_raw[name] = heat_island
            adaptive_gap_raw[name] = 0.5 * (1.0 - green_space) + 0.5 * (1.0 - medical_access)

        sensitivity_pct = self._percentile_map(sensitivity_raw)
        exposure_pct = self._percentile_map(exposure_raw)
        adaptive_gap_pct = self._percentile_map(adaptive_gap_raw)

        # 6) 风险综合：天气危险度 + 脆弱性 + 历史负担
        if historical_component_available:
            burden_values = {}
            for item in eligible_risks:
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

        for item in eligible_risks:
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

        # 缺失关键字段的社区保留在 API 中便于补数据，
        # 所有风险、排名、就诊增量和行动字段都保持空值。
        for item in ineligible_risks:
            item.update({
                'relative_index': None,
                'percentile_rank': None,
                'historical_component_available': False,
                'observed_cases': None,
                'expected_cases': None,
                'sir': None,
                'ci_low': None,
                'ci_high': None,
                'smoothed_sir': None,
                'probability_exceed_baseline': None,
                'certainty': 'unavailable',
                'uncertainty_index': None,
                'svi_percentile': None,
                'theme_scores': {},
                'burden_percentile': None,
                'weather_hazard_score': None,
                'uncertainty_penalty': None,
                'risk_weights': {},
                'risk_contributions': {},
                'risk_index': None,
                'heatrisk_level': None,
                'heatrisk_label': '数据不足',
                'heatrisk_color': '#94a3b8',
                'impact_bucket': None,
                'likelihood_bucket': None,
                'matrix_score': None,
                'equity_stratum': None,
            })

        # 7) 空间热点显著性（Gi*）
        self._compute_hotspot_stats(community_risks)

        # 只对通过数据门的社区排序；其余社区放在列表末尾并标记未排名。
        ranked_rows = sorted(
            eligible_risks,
            key=lambda row: float(row['risk_index']),
            reverse=True
        )
        for idx, row in enumerate(ranked_rows, start=1):
            row['rank'] = idx
        unranked_rows = sorted(ineligible_risks, key=lambda row: row['community'])
        for row in unranked_rows:
            row['rank'] = None
        rankings = ranked_rows + unranked_rows

        # GeoJSON
        geojson_features = []
        for row in ranked_rows:
            if row.get('coordinate_available') is not True:
                continue
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
            'features': geojson_features,
            'unmapped_communities': [
                row['community']
                for row in rankings
                if row.get('coordinate_available') is not True
            ],
        }

        management_suggestions = self._generate_management_suggestions(ranked_rows[:5], weather_data)

        heatrisk_counts = {str(level): 0 for level in range(5)}
        hotspot_counts = {
            '热点(99%)': 0,
            '热点(95%)': 0,
            '冷点(99%)': 0,
            '冷点(95%)': 0,
            '无显著': 0,
            '样本不足': 0,
            '无坐标': 0,
        }
        uncertainty_values = []
        for row in ranked_rows:
            level_key = str(int(row['heatrisk_level']))
            heatrisk_counts[level_key] = heatrisk_counts.get(level_key, 0) + 1
            hotspot_label = row.get('hotspot_category', '无显著')
            hotspot_counts[hotspot_label] = hotspot_counts.get(hotspot_label, 0) + 1
            if row.get('uncertainty_index') is not None:
                uncertainty_values.append(float(row['uncertainty_index']))
        median_uncertainty = float(np.median(uncertainty_values)) if uncertainty_values else None

        data_coverage_ratio = (
            matched_records / medical_summary['total_records']
            if medical_summary['total_records'] > 0 else None
        )

        layers = {
            'risk_index': [
                {'community': row['community'], 'value': row['risk_index']}
                for row in ranked_rows
            ],
            'vulnerability': [
                {'community': row['community'], 'value': row['svi_percentile']}
                for row in ranked_rows
            ],
            'uncertainty': [
                {'community': row['community'], 'value': row.get('uncertainty_index')}
                for row in ranked_rows
            ],
            'hotspot': [
                {'community': row['community'], 'category': row.get('hotspot_category', '数据不足')}
                for row in ranked_rows
            ]
        }

        # 8) 公平性分层（按 SVI-like 分位分层）
        strata_map = {'Q1': [], 'Q2': [], 'Q3': [], 'Q4': []}
        for row in ranked_rows:
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
        ] if ranked_rows else []
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
            row for row in ranked_rows
            if float(row.get('svi_percentile') or 0.0) >= 75.0
            and (
                float(row.get('risk_index') or 0.0) >= 60.0
                or int(row.get('heatrisk_level') or 0) >= 3
            )
        ]
        if not priority_candidates:
            priority_candidates = sorted(
                ranked_rows,
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

        if ranked_rows and unranked_rows:
            data_status_code = 'partial_vulnerability_data'
            data_message = (
                f'{len(ranked_rows)} 个社区已参与排名；'
                f'{len(unranked_rows)} 个社区数据不足，未参与排名。'
            )
        elif ranked_rows:
            data_status_code = 'available'
            data_message = f'{len(ranked_rows)} 个社区字段完整，已参与排名。'
        else:
            data_status_code = 'insufficient_vulnerability_data'
            data_message = (
                f'{len(unranked_rows)} 个社区均缺少关键脆弱性或实测基线字段，'
                '数据不足，未参与排名。'
            )

        missing_coordinate_count = sum(
            1 for row in rankings if row.get('coordinate_available') is not True
        )

        return {
            'data_available': bool(ranked_rows),
            'data_status': {
                'available': bool(ranked_rows),
                'code': data_status_code,
                'source': 'community_table',
                'message': data_message,
            },
            'map_data': map_data,
            'rankings': [
                {
                    'rank': row['rank'],
                    'community': row['community'],
                    'ranking_eligible': row.get('ranking_eligible') is True,
                    'data_status': row.get('data_status'),
                    'data_message': row.get('data_message'),
                    'missing_fields': row.get('missing_fields', []),
                    'invalid_fields': row.get('invalid_fields', []),
                    'uses_proxy_values': row.get('uses_proxy_values', False),
                    'latitude': row.get('latitude'),
                    'longitude': row.get('longitude'),
                    'coordinate_available': row.get('coordinate_available') is True,
                    'coordinate_source': row.get('coordinate_source'),
                    'coordinate_status': row.get('coordinate_status'),
                    'coordinate_message': row.get('coordinate_message'),
                    'risk_score': row.get('normalized_score'),
                    'risk_level': row['risk_level'],
                    'population': row['population'],
                    'elderly_ratio': row.get('elderly_ratio'),
                    'chronic_disease_ratio': row.get('chronic_disease_ratio'),
                    'vulnerability_index': row['components'].get('vulnerability_index'),
                    'expected_excess_visits': row.get('expected_excess_visits'),
                    'relative_index': row.get('relative_index'),
                    'percentile_rank': row.get('percentile_rank'),
                    'risk_index': row.get('risk_index'),
                    'weather_hazard_score': row.get('weather_hazard_score'),
                    'historical_component_available': row.get('historical_component_available', False),
                    'burden_percentile': row.get('burden_percentile'),
                    'uncertainty_penalty': row.get('uncertainty_penalty'),
                    'risk_weights': row.get('risk_weights', {}),
                    'risk_contributions': row.get('risk_contributions', {}),
                    'hazard_formula': row.get('hazard_formula'),
                    'heatrisk_level': row.get('heatrisk_level'),
                    'heatrisk_label': row.get('heatrisk_label', '数据不足'),
                    'heatrisk_color': row.get('heatrisk_color', '#94a3b8'),
                    'svi_percentile': row.get('svi_percentile'),
                    'theme_scores': row.get('theme_scores', {}),
                    'observed_cases': row.get('observed_cases'),
                    'expected_cases': row.get('expected_cases'),
                    'sir': row.get('sir'),
                    'ci_low': row.get('ci_low'),
                    'ci_high': row.get('ci_high'),
                    'smoothed_sir': row.get('smoothed_sir'),
                    'probability_exceed_baseline': row.get('probability_exceed_baseline'),
                    'certainty': row.get('certainty', 'unavailable'),
                    'uncertainty_index': row.get('uncertainty_index'),
                    'hotspot_category': row.get('hotspot_category', '数据不足'),
                    'hotspot_z': row.get('hotspot_z'),
                    'hotspot_p': row.get('hotspot_p'),
                    'impact_bucket': row.get('impact_bucket'),
                    'likelihood_bucket': row.get('likelihood_bucket'),
                    'matrix_score': row.get('matrix_score'),
                    'equity_stratum': row.get('equity_stratum')
                }
                for row in rankings
            ],
            'summary': {
                'data_available': bool(ranked_rows),
                'data_status': data_status_code,
                'data_message': data_message,
                'total_communities': len(rankings),
                'ranked_communities': len(ranked_rows),
                'unranked_communities': len(unranked_rows),
                'missing_coordinate_count': missing_coordinate_count,
                'ranked_missing_coordinate_count': sum(
                    1 for row in ranked_rows if row.get('coordinate_available') is not True
                ),
                'high_risk_count': sum(1 for row in ranked_rows if row['risk_level'] == '高风险'),
                'medium_risk_count': sum(1 for row in ranked_rows if row['risk_level'] == '中风险'),
                'low_risk_count': sum(1 for row in ranked_rows if row['risk_level'] == '低风险'),
                'total_expected_excess': (
                    sum(row['expected_excess_visits'] for row in ranked_rows)
                    if ranked_rows else None
                ),
                'analysis_date': str(target_date),
                'window_days': analysis_days,
                'disease_filter': disease_filter or '',
                'matched_records': matched_records,
                'total_records': medical_summary['total_records'],
                'unmatched_records': medical_summary['unmatched_records'],
                'excluded_incomplete_profile_records': excluded_profile_records,
                'historical_component_available': historical_component_available,
                'risk_weights': ranked_rows[0].get('risk_weights', {}) if ranked_rows else {},
                'data_coverage_ratio': round(data_coverage_ratio, 4) if data_coverage_ratio is not None else None,
                'baseline_rate_per_person_day': round(baseline_rate_per_person_day, 8) if baseline_rate_per_person_day is not None else None,
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
                'data_available': bool(ranked_rows),
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
                data_message,
                (
                    '完整性门：人口、老龄率、慢病率、绿地率、热岛指数、'
                    '医疗可达性与实测基线门诊量任一缺失，该社区即不计算、不排名、'
                    '不生成预计就诊或行动优先级。'
                ),
                (
                    '天气危险度上游公式：Excess=max(Weather RR-1, 0)×VI×BaselineVisits；'
                    'Hazard=clip((1-exp(-Excess/Efold))×100, 0, 100)。'
                    '仅对通过完整性门的社区计算。'
                    if ranked_rows else
                    '本次无社区通过完整性门，未执行风险与预计就诊计算。'
                ),
                (
                    '社区风险=天气危险度(45%)+SVI-like脆弱性(35%)+历史负担(20%)，'
                    '并对高不确定性样本执行惩罚。'
                    if historical_component_available else
                    (
                        '本次没有可匹配的历史病例分量；已过门社区使用天气危险度'
                        '(56.25%)+SVI-like脆弱性(43.75%)，不使用历史不确定性惩罚。'
                        if ranked_rows else
                        '由于无社区通过完整性门，本次不生成综合风险权重结果。'
                    )
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
                    (
                        '历史概率缺失时不生成 Impact×Likelihood 分值。'
                        if ranked_rows else
                        '数据完整性不足，本次不生成行动优先级。'
                    )
                ),
                '公平性分层按脆弱性分位(Q1-Q4)聚合，仅包含通过完整性门的社区。',
                (
                    '地图与空间热点只使用 config.COMMUNITY_COORDS_GCJ 同名有效坐标；'
                    '缺失时标记无坐标，不使用数据库坐标兜底。'
                ),
            ],
            'management_suggestions': management_suggestions
        }
    
    def _generate_management_suggestions(self, high_risk_communities, weather_data):
        """生成管控建议（医生端）"""
        suggestions = []

        # 没有通过数据完整性门的社区时，不发布“常规”或其他行动结论。
        if not high_risk_communities:
            return suggestions

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
                'population': profile.get('population'),
                'elderly_ratio': profile.get('elderly_ratio'),
                'chronic_disease_ratio': profile.get('chronic_disease_ratio'),
                'vulnerability_index': vi_result['vulnerability_index'],
                'vulnerability_level': vi_result['level'],
                'ranking_eligible': vi_result.get('ranking_eligible', False),
                'data_status': vi_result.get('data_status'),
                'data_message': vi_result.get('data_message'),
            })

        # 完整社区按 VI 排序，数据不足社区只按名称置于末尾。
        return sorted(
            communities,
            key=lambda item: (
                item.get('ranking_eligible') is not True,
                -(float(item['vulnerability_index']) if item['vulnerability_index'] is not None else 0.0),
                item['name'],
            ),
        )


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

