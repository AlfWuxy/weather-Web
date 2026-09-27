# -*- coding: utf-8 -*-
"""
模块二：健康风险评估服务

功能：
1) 社区脆弱性指数计算（供管理后台使用）
2) 个人健康风险评估（学术实践增强版）：
   - 多路径融合（DLNM + 规则暴露 + 社区脆弱性）
   - 概率化输出（低/中/高风险概率 + 不确定性区间）
   - 事件语义（severity / certainty / urgency）
   - Impact × Likelihood 矩阵化表达
"""
import math
from datetime import timedelta
from statistics import pstdev

from core.db_models import Community, MedicalRecord
from core.time_utils import utcnow
from utils.parsers import safe_json_loads


class HealthRiskService:
    """健康风险评估服务类"""

    def __init__(self):
        self.model_version = 'health-risk-fusion-2.0'

    # ---------------------------
    # 社区脆弱性（兼容旧接口）
    # ---------------------------
    def calculate_community_vulnerability_index(self, community_data):
        """
        计算社区脆弱性指数（0-100）。
        """
        elderly_ratio = self._clamp(self._to_float(community_data.get('elderly_ratio'), 0.0), 0.0, 1.0)
        chronic_ratio = self._clamp(self._to_float(community_data.get('chronic_disease_ratio'), 0.0), 0.0, 1.0)
        medical_accessibility = self._clamp(self._to_float(community_data.get('medical_accessibility'), 50.0), 0.0, 100.0)
        env_quality = self._clamp(self._to_float(community_data.get('env_quality_score'), 50.0), 0.0, 100.0)

        aging_score = elderly_ratio * 100.0 * 0.30
        disease_score = chronic_ratio * 100.0 * 0.35
        medical_score = (100.0 - medical_accessibility) * 0.20
        env_score = (100.0 - env_quality) * 0.15

        vulnerability_index = self._clamp(aging_score + disease_score + medical_score + env_score, 0.0, 100.0)
        risk_level = self._bucket_three(vulnerability_index, low=30.0, high=60.0, labels=('低', '中', '高'))

        return {
            'vulnerability_index': round(vulnerability_index, 2),
            'risk_level': risk_level,
            'level': risk_level,
            'breakdown': {
                'aging_score': round(aging_score, 2),
                'disease_score': round(disease_score, 2),
                'medical_score': round(medical_score, 2),
                'env_score': round(env_score, 2)
            }
        }

    # ---------------------------
    # 个人风险评估（主入口）
    # ---------------------------
    def assess_personal_weather_health_risk(self, user_profile, weather_data, screening=None):
        """
        学术实践增强版个人风险评估。

        返回字段兼容旧逻辑：
        - risk_score / risk_level / recommendations / disease_risks
        同时增加：
        - risk_probabilities / risk_interval / cap_semantics / impact_likelihood
        - model_paths / component_scores / community_context / methodology
        """
        from services.chronic_risk_service import get_chronic_service
        from services.dlnm_risk_service import get_dlnm_service
        from services.weather_service import WeatherService

        profile = self._normalize_user_profile(user_profile)
        weather = self._normalize_weather_data(weather_data)
        screening_data = self._normalize_screening(screening or {})

        # 路径 A：DLNM 温度风险 + 个人易感性
        dlnm = get_dlnm_service()
        lag_temps = self._extract_lag_temperatures(weather_data, weather['temperature'])
        base_rr, rr_breakdown = dlnm.calculate_rr(
            weather['temperature'],
            lag_temperatures=lag_temps,
            disease_type='general',
            age=profile['age']
        )
        temp_score = self._clamp((float(base_rr) - 1.0) / 1.2 * 100.0, 0.0, 100.0)
        personal_susceptibility = self._calc_personal_susceptibility_score(profile)
        model_a_score = self._clamp(
            0.50 * temp_score + 0.28 * personal_susceptibility + 0.22 * self._aqi_score(weather['aqi']),
            0.0,
            100.0
        )

        # 路径 B：规则暴露（空气/湿度/极端天气）+ 即时筛查
        weather_service = WeatherService()
        extreme = weather_service.identify_extreme_weather(weather_data)
        extreme_score = self._clamp(len(extreme.get('conditions', [])) * 25.0, 0.0, 100.0)
        humidity_score = self._humidity_score(weather['humidity'])
        aqi_score = self._aqi_score(weather['aqi'])
        screening_score = self._screening_score(screening_data)
        model_b_score = self._clamp(
            0.30 * extreme_score + 0.30 * aqi_score + 0.15 * humidity_score + 0.25 * screening_score,
            0.0,
            100.0
        )

        # 路径 C：社区脆弱性 + 社区近期负担 + 个体基础敏感性
        community_context = self._build_community_context(profile.get('community'))
        community_score = self._clamp(
            0.65 * community_context['vulnerability_index'] + 0.35 * community_context['burden_score'],
            0.0,
            100.0
        )
        model_c_score = self._clamp(
            0.40 * community_score + 0.35 * personal_susceptibility + 0.25 * temp_score,
            0.0,
            100.0
        )

        # 慢病专项（用于病种风险与解释）
        chronic_service = get_chronic_service()
        chronic_result = chronic_service.predict_individual_risk(
            {
                'age': profile['age'],
                'gender': profile['gender'],
                'chronic_diseases': profile['chronic_diseases']
            },
            weather_data
        )
        chronic_overall_score = self._to_float(
            (chronic_result.get('overall_risk') or {}).get('score'),
            30.0
        )

        # 模型融合 + 不确定性
        path_fused_score = (
            model_a_score * 0.45
            + model_b_score * 0.30
            + model_c_score * 0.25
        )
        model_paths = [
            {
                'name': 'DLNM个体模型',
                'score': round(model_a_score, 1),
                'path_weight': 0.45,
                'weight': 0.85 * 0.45,
                'contribution': round(model_a_score * 0.85 * 0.45, 2),
            },
            {
                'name': '规则暴露模型',
                'score': round(model_b_score, 1),
                'path_weight': 0.30,
                'weight': 0.85 * 0.30,
                'contribution': round(model_b_score * 0.85 * 0.30, 2),
            },
            {
                'name': '社区脆弱性模型',
                'score': round(model_c_score, 1),
                'path_weight': 0.25,
                'weight': 0.85 * 0.25,
                'contribution': round(model_c_score * 0.85 * 0.25, 2),
            },
            {
                'name': '慢病专项模型',
                'score': round(chronic_overall_score, 1),
                'path_weight': 1.0,
                'weight': 0.15,
                'contribution': round(chronic_overall_score * 0.15, 2),
            },
        ]
        fused_score = 0.85 * path_fused_score + 0.15 * chronic_overall_score
        fused_score = self._clamp(fused_score, 0.0, 100.0)

        spread = pstdev([model_a_score, model_b_score, model_c_score]) if len(model_paths) > 1 else 0.0
        spread = max(spread, 4.0)
        interval_half = self._clamp(spread * 1.45, 6.0, 22.0)
        score_low = self._clamp(fused_score - interval_half, 0.0, 100.0)
        score_high = self._clamp(fused_score + interval_half, 0.0, 100.0)
        uncertainty_label = self._bucket_three(
            spread, low=6.0, high=11.0, labels=('低', '中', '高')
        )

        probability = self._risk_probabilities(fused_score, sigma=max(6.0, spread + 5.0))
        high_risk_probability = probability['high']

        cap_semantics = self._cap_semantics(
            score=fused_score,
            high_probability=high_risk_probability,
            uncertainty_label=uncertainty_label
        )
        impact_input_score = 0.7 * fused_score + 0.3 * screening_score
        likelihood_input_score = high_risk_probability * 100.0
        impact_likelihood = self._impact_likelihood_bucket(
            impact_score=impact_input_score,
            likelihood_score=likelihood_input_score,
            certainty=cap_semantics['certainty']
        )

        component_scores = {
            '温度风险': round(temp_score, 1),
            '空气质量风险': round(aqi_score, 1),
            '湿度风险': round(humidity_score, 1),
            '极端天气暴露': round(extreme_score, 1),
            '个体易感性': round(personal_susceptibility, 1),
            '即时健康筛查': round(screening_score, 1),
            '社区脆弱性': round(community_context['vulnerability_index'], 1),
            '社区病例负担': round(community_context['burden_score'], 1),
        }

        explain = chronic_result.get('explain') or {'reasons': [], 'actions': [], 'escalation': []}
        explain['reasons'] = self._merge_unique(
            explain.get('reasons', []),
            self._top_component_reasons(component_scores)
        )[:4]
        explain['actions'] = self._merge_unique(
            explain.get('actions', []),
            self._matrix_actions(impact_likelihood)
        )[:6]

        recommendations = self._compose_recommendations(
            chronic_result.get('recommendations', []),
            cap_semantics,
            impact_likelihood,
            weather,
            profile,
            screening_data
        )

        risk_level = self._bucket_three(
            fused_score,
            low=40.0,
            high=70.0,
            labels=('低风险', '中风险', '高风险')
        )

        return {
            'risk_score': round(fused_score, 1),
            'risk_level': risk_level,
            'risk_interval': {
                'low': round(score_low, 1),
                'high': round(score_high, 1),
                'spread': round(spread, 2),
                'label': uncertainty_label
            },
            'risk_probabilities': {
                'low': round(probability['low'], 4),
                'medium': round(probability['medium'], 4),
                'high': round(probability['high'], 4)
            },
            'high_risk_probability': round(high_risk_probability, 4),
            'cap_semantics': cap_semantics,
            'impact_likelihood': impact_likelihood,
            'model_paths': model_paths,
            'fusion_breakdown': {
                'path_fused_score': round(path_fused_score, 2),
                'chronic_overall_score': round(chronic_overall_score, 2),
                'final_score': round(fused_score, 1),
                'contribution_total': round(sum(path['contribution'] for path in model_paths), 2),
            },
            'component_scores': component_scores,
            'community_context': community_context,
            'screening': screening_data,
            'weather': weather,
            'disease_risks': chronic_result.get('disease_risks', {}),
            'recommendations': recommendations,
            'explain': explain,
            'rule_version': chronic_result.get('rule_version'),
            'triggered_rules': chronic_result.get('triggered_rules', []),
            'methodology': [
                '路径A: DLNM温度风险 + 年龄/慢病敏感性',
                '路径B: 规则暴露(空气质量/湿度/极端天气) + 即时筛查',
                '路径C: 社区脆弱性指数 + 30天病例负担',
                '融合分数 = 0.45*A + 0.30*B + 0.25*C，并与慢病专项结果做轻量校准',
                '概率化输出基于分数分布计算低/中/高风险概率，并给出区间与不确定性等级',
                '行动优先级采用 Impact × Likelihood 四级矩阵（1-16分）'
            ],
            'model_version': self.model_version,
            'rr_breakdown': rr_breakdown
        }

    # ---------------------------
    # 兼容旧接口（已废弃）
    # ---------------------------
    def assess_user_risk(self, user_id):
        """旧接口：保留兼容。"""
        return None

    def generate_community_risk_map_data(self):
        """旧接口：保留兼容。"""
        return []

    # ---------------------------
    # 内部工具函数
    # ---------------------------
    def _normalize_user_profile(self, user_profile):
        diseases = user_profile.get('chronic_diseases', [])
        if isinstance(diseases, str):
            parsed = safe_json_loads(diseases, [])
            if isinstance(parsed, list):
                diseases = parsed
            elif diseases:
                diseases = [diseases]
            else:
                diseases = []
        if diseases is None:
            diseases = []
        if not isinstance(diseases, list):
            diseases = [str(diseases)]

        cleaned = [str(item).strip() for item in diseases if str(item).strip()]
        has_chronic = bool(user_profile.get('has_chronic_disease')) or bool(cleaned)

        return {
            'age': self._to_int(user_profile.get('age'), 45, min_value=0, max_value=110),
            'gender': str(user_profile.get('gender') or '未知'),
            'community': str(user_profile.get('community') or '').strip(),
            'has_chronic_disease': has_chronic,
            'chronic_diseases': cleaned
        }

    def _normalize_weather_data(self, weather_data):
        return {
            'temperature': self._to_float(weather_data.get('temperature'), 20.0),
            'humidity': self._clamp(self._to_float(weather_data.get('humidity'), 60.0), 0.0, 100.0),
            'aqi': self._clamp(self._to_float(weather_data.get('aqi'), 50.0), 0.0, 500.0),
            'pressure': self._to_float(weather_data.get('pressure'), 1013.0),
            'wind_speed': self._to_float(weather_data.get('wind_speed'), 3.0),
            'weather_condition': str(weather_data.get('weather_condition') or '')
        }

    def _normalize_screening(self, screening):
        mapping = {
            'outdoor_exposure': ({'low', 'medium', 'high'}, 'medium'),
            'symptom_level': ({'none', 'mild', 'moderate', 'severe'}, 'none'),
            'hydration': ({'good', 'normal', 'poor'}, 'normal'),
            'medication_adherence': ({'good', 'partial', 'poor'}, 'good'),
            'sleep_quality': ({'good', 'fair', 'poor'}, 'good')
        }
        data = {}
        for key, pair in mapping.items():
            allowed, default = pair
            value = str(screening.get(key) or '').strip().lower()
            data[key] = value if value in allowed else default
        return data

    def _screening_score(self, screening):
        outdoor_map = {'low': 15.0, 'medium': 35.0, 'high': 60.0}
        symptom_map = {'none': 0.0, 'mild': 35.0, 'moderate': 70.0, 'severe': 95.0}
        hydration_map = {'good': 0.0, 'normal': 15.0, 'poor': 42.0}
        adherence_map = {'good': 0.0, 'partial': 22.0, 'poor': 45.0}
        sleep_map = {'good': 0.0, 'fair': 15.0, 'poor': 35.0}
        score = (
            0.30 * symptom_map.get(screening['symptom_level'], 0.0)
            + 0.25 * outdoor_map.get(screening['outdoor_exposure'], 15.0)
            + 0.15 * hydration_map.get(screening['hydration'], 0.0)
            + 0.20 * adherence_map.get(screening['medication_adherence'], 0.0)
            + 0.10 * sleep_map.get(screening['sleep_quality'], 0.0)
        )
        return self._clamp(score, 0.0, 100.0)

    def _calc_personal_susceptibility_score(self, profile):
        age = profile['age']
        if age >= 80:
            age_score = 95.0
        elif age >= 70:
            age_score = 78.0
        elif age >= 60:
            age_score = 62.0
        elif age >= 45:
            age_score = 45.0
        else:
            age_score = 28.0

        disease_count = len(profile['chronic_diseases'])
        chronic_score = 12.0 if disease_count == 0 else self._clamp(45.0 + disease_count * 14.0, 45.0, 98.0)

        gender = profile['gender']
        male_cardio_bonus = 8.0 if gender in {'男', '男性'} and age >= 55 else 0.0
        female_elder_bonus = 5.0 if gender in {'女', '女性'} and age >= 70 else 0.0

        return self._clamp(
            0.55 * age_score + 0.40 * chronic_score + male_cardio_bonus + female_elder_bonus,
            0.0,
            100.0
        )

    def _extract_lag_temperatures(self, weather_data, current_temp):
        keys = ('lag_temperatures', 'temperature_lags', 'temperature_history', 'historical_temperatures')
        for key in keys:
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
            if abs(lag_temps[0] - float(current_temp)) > 0.01:
                lag_temps.insert(0, float(current_temp))
            return lag_temps
        return None

    def _build_community_context(self, community_name):
        default_vi = 45.0
        default_burden_score = 30.0
        if not community_name:
            return {
                'community': '未设置',
                'population': None,
                'elderly_ratio': None,
                'chronic_disease_ratio': None,
                'vulnerability_index': default_vi,
                'vulnerability_level': '中',
                'cases_30d': None,
                'burden_per_1000': None,
                'burden_score': default_burden_score,
                'source': 'user_profile_missing',
                'source_label': '个人资料未设置社区',
                'vulnerability_source': 'default_proxy',
                'vulnerability_source_label': '默认中性 VI 代理值',
                'population_source': 'missing',
                'burden_source': 'unavailable_no_community',
                'population_available': False,
                'burden_available': False,
                'imputed': True,
                'imputed_fields': ['vulnerability_index', 'burden_score'],
                'warnings': [
                    '个人资料未设置社区，社区 VI 使用 45 分中性代理值。',
                    '社区名缺失，30 日门诊记录与每千人负担无法计算，模型使用 30 分中性负担代理值。'
                ]
            }

        profile_data = None
        try:
            from services.community_risk_service import get_community_service
            profile_data = get_community_service().get_community_profile(community_name)
        except Exception:
            profile_data = None

        community_row = Community.query.filter_by(name=community_name).first()
        population = None
        elderly_ratio = None
        chronic_ratio = None
        vulnerability_index = default_vi
        vulnerability_level = '中'
        source = 'unmatched_community'
        source_label = '未匹配社区表或内置档案'
        population_source = 'missing'
        vulnerability_source = 'default_proxy'
        vulnerability_source_label = '默认中性 VI 代理值'
        imputed_fields = []
        warnings = []

        if community_row:
            source = 'community_table'
            source_label = '社区表实时记录'
            raw_population = int(self._to_float(community_row.population, 0.0))
            if raw_population > 0:
                population = raw_population
                population_source = 'community_table'
            else:
                imputed_fields.append('population')
                warnings.append('社区表缺少有效人口，每千人负担无法计算。')
            if community_row.elderly_ratio is not None:
                elderly_ratio = self._clamp(self._to_float(community_row.elderly_ratio), 0.0, 1.0)
            if community_row.chronic_disease_ratio is not None:
                chronic_ratio = self._clamp(self._to_float(community_row.chronic_disease_ratio), 0.0, 1.0)
            if community_row.vulnerability_index is not None:
                vulnerability_index = self._clamp(self._to_float(community_row.vulnerability_index), 0.0, 100.0)
                vulnerability_source = 'community_table'
                vulnerability_source_label = '社区表 VI'
                if community_row.risk_level:
                    vulnerability_level = str(community_row.risk_level)
            else:
                imputed_fields.append('vulnerability_index')
                warnings.append('社区表缺少 VI，当前使用 45 分中性代理值。')
        elif profile_data:
            source = 'bundled_profile'
            source_label = '内置社区档案'
            raw_population = int(self._to_float(profile_data.get('population'), 0.0))
            if raw_population > 0:
                population = raw_population
                population_source = 'bundled_profile'
            if profile_data.get('elderly_ratio') is not None:
                elderly_ratio = self._clamp(self._to_float(profile_data.get('elderly_ratio')), 0.0, 1.0)
            if profile_data.get('chronic_disease_ratio') is not None:
                chronic_ratio = self._clamp(self._to_float(profile_data.get('chronic_disease_ratio')), 0.0, 1.0)
            imputed_fields.append('vulnerability_index')
            warnings.append('未匹配社区表 VI，当前使用 45 分中性代理值。')
        else:
            imputed_fields.extend(['population', 'vulnerability_index'])
            warnings.extend([
                '未匹配社区表或内置档案，社区 VI 使用 45 分中性代理值。',
                '人口数缺失，每千人负担无法计算。'
            ])

        burden = self._community_recent_burden(community_name, population, window_days=30)
        cases_30d = burden['cases']
        burden_per_1000 = burden['per_1000']
        burden_available = burden['available']
        if burden_available:
            burden_score = self._clamp(burden_per_1000 * 8.0, 0.0, 100.0)
            burden_source = 'medical_records_30d_per_1000'
        else:
            burden_score = default_burden_score
            burden_source = burden['reason']
            imputed_fields.append('burden_score')
            if burden['reason'] == 'unavailable_query_failed':
                warnings.append('30 日门诊记录查询失败，模型使用 30 分中性负担代理值。')
            else:
                warnings.append('因人口数缺失，30 日每千人负担无法计算，模型使用 30 分中性负担代理值。')

        # 保持字段顺序稳定，方便页面与导出结果审计。
        imputed_fields = list(dict.fromkeys(imputed_fields))
        warnings = list(dict.fromkeys(warnings))

        return {
            'community': community_name,
            'population': population,
            'elderly_ratio': round(elderly_ratio, 4) if elderly_ratio is not None else None,
            'chronic_disease_ratio': round(chronic_ratio, 4) if chronic_ratio is not None else None,
            'vulnerability_index': round(vulnerability_index, 1),
            'vulnerability_level': vulnerability_level,
            'cases_30d': int(cases_30d) if cases_30d is not None else None,
            'burden_per_1000': round(burden_per_1000, 3) if burden_per_1000 is not None else None,
            'burden_score': round(burden_score, 1),
            'source': source,
            'source_label': source_label,
            'vulnerability_source': vulnerability_source,
            'vulnerability_source_label': vulnerability_source_label,
            'population_source': population_source,
            'burden_source': burden_source,
            'population_available': population is not None and population > 0,
            'burden_available': burden_available,
            'imputed': bool(imputed_fields),
            'imputed_fields': imputed_fields,
            'warnings': warnings
        }

    def _community_recent_burden(self, community_name, population, window_days=30):
        try:
            start_time = utcnow() - timedelta(days=max(7, int(window_days or 30)))
            query = MedicalRecord.query.filter(MedicalRecord.community == community_name)
            query = query.filter(MedicalRecord.visit_time >= start_time)
            cases = int(query.count())
        except Exception:
            return {
                'cases': None,
                'per_1000': None,
                'available': False,
                'reason': 'unavailable_query_failed'
            }

        pop = max(int(population or 0), 0)
        if pop <= 0:
            return {
                'cases': cases,
                'per_1000': None,
                'available': False,
                'reason': 'unavailable_missing_population'
            }
        return {
            'cases': cases,
            'per_1000': cases * 1000.0 / pop,
            'available': True,
            'reason': 'medical_records_30d_per_1000'
        }

    def _aqi_score(self, aqi):
        aqi = self._clamp(self._to_float(aqi, 50.0), 0.0, 500.0)
        if aqi <= 50:
            return 8.0
        if aqi <= 100:
            return 24.0
        if aqi <= 150:
            return 48.0
        if aqi <= 200:
            return 72.0
        if aqi <= 300:
            return 88.0
        return 96.0

    def _humidity_score(self, humidity):
        humidity = self._clamp(self._to_float(humidity, 60.0), 0.0, 100.0)
        if humidity < 35:
            return self._clamp((35.0 - humidity) * 2.4, 0.0, 100.0)
        if humidity > 75:
            return self._clamp((humidity - 75.0) * 2.6, 0.0, 100.0)
        return 12.0

    def _risk_probabilities(self, mean_score, sigma):
        sigma = max(self._to_float(sigma, 10.0), 1.0)

        def cdf(x):
            z = (x - mean_score) / (sigma * math.sqrt(2.0))
            return 0.5 * (1.0 + math.erf(z))

        p_low = self._clamp(cdf(40.0), 0.0, 1.0)
        p_high = self._clamp(1.0 - cdf(70.0), 0.0, 1.0)
        p_medium = self._clamp(1.0 - p_low - p_high, 0.0, 1.0)

        total = p_low + p_medium + p_high
        if total <= 0:
            return {'low': 0.33, 'medium': 0.34, 'high': 0.33}
        return {
            'low': p_low / total,
            'medium': p_medium / total,
            'high': p_high / total
        }

    def _cap_semantics(self, score, high_probability, uncertainty_label):
        if score >= 85:
            severity = 'extreme'
        elif score >= 70:
            severity = 'severe'
        elif score >= 50:
            severity = 'moderate'
        else:
            severity = 'minor'

        if high_probability >= 0.7:
            certainty = 'likely'
        elif high_probability >= 0.45:
            certainty = 'possible'
        else:
            certainty = 'unlikely'

        if severity in ('extreme', 'severe') and certainty == 'likely':
            urgency = 'immediate'
        elif severity in ('severe', 'moderate'):
            urgency = 'expected'
        else:
            urgency = 'future'

        if uncertainty_label == '高' and certainty == 'likely':
            certainty = 'possible'

        return {
            'severity': severity,
            'certainty': certainty,
            'urgency': urgency
        }

    def _impact_likelihood_bucket(self, impact_score, likelihood_score, certainty):
        raw_likelihood = self._clamp(self._to_float(likelihood_score, 0.0), 0.0, 100.0)
        likelihood = raw_likelihood
        certainty_adjustment = 0.0
        if certainty == 'likely':
            certainty_adjustment = 8.0
        elif certainty == 'unlikely':
            certainty_adjustment = -8.0
        likelihood = self._clamp(likelihood + certainty_adjustment, 0.0, 100.0)

        impact_bucket = self._to_four_bucket(impact_score)
        likelihood_bucket = self._to_four_bucket(likelihood)
        rank = {'low': 1, 'medium': 2, 'high': 3, 'very_high': 4}
        return {
            'impact_bucket': impact_bucket,
            'likelihood_bucket': likelihood_bucket,
            'matrix_score': rank[impact_bucket] * rank[likelihood_bucket],
            'impact_score': round(self._clamp(self._to_float(impact_score), 0.0, 100.0), 1),
            'likelihood_raw_score': round(raw_likelihood, 1),
            'certainty_adjustment': round(certainty_adjustment, 1),
            'likelihood_score': round(likelihood, 1),
        }

    def _to_four_bucket(self, score):
        score = self._clamp(self._to_float(score, 0.0), 0.0, 100.0)
        if score >= 75:
            return 'very_high'
        if score >= 50:
            return 'high'
        if score >= 25:
            return 'medium'
        return 'low'

    def _compose_recommendations(
        self,
        chronic_recommendations,
        cap_semantics,
        impact_likelihood,
        weather,
        profile,
        screening
    ):
        result = []
        seen = set()

        def add_item(category, advice, priority='medium'):
            if not advice:
                return
            key = f'{category}:{advice}'
            if key in seen:
                return
            seen.add(key)
            result.append({
                'category': category,
                'priority': priority,
                'advice': advice
            })

        for item in chronic_recommendations or []:
            if isinstance(item, dict):
                add_item(
                    item.get('category', '慢病管理'),
                    item.get('advice', ''),
                    item.get('priority', 'medium')
                )

        if weather['temperature'] >= 32:
            add_item('高温防护', '减少午后外出，优先在阴凉或空调环境活动，主动补水。', 'high')
        elif weather['temperature'] <= 5:
            add_item('低温防护', '外出注意分层保暖，尤其关注头颈和四肢。', 'high')

        if weather['aqi'] >= 150:
            add_item('空气质量', '空气质量偏差，尽量减少户外运动，必要时佩戴防护口罩。', 'high')

        if screening.get('symptom_level') in ('moderate', 'severe'):
            add_item('症状管理', '已出现明显不适，请减少活动并监测症状变化。', 'high')

        if screening.get('medication_adherence') in ('partial', 'poor') and profile.get('has_chronic_disease'):
            add_item('用药依从', '请按医嘱规律服药，不要自行停药或减量。', 'high')

        if cap_semantics['urgency'] == 'immediate':
            add_item('紧急分流', '若出现胸痛、呼吸困难、意识模糊等症状，请立即就医。', 'urgent')
        elif impact_likelihood['matrix_score'] >= 12:
            add_item('行动升级', '当前风险已进入高优先级，请联系家属或村医协助观察。', 'high')

        if not result:
            add_item('日常管理', '保持规律作息、均衡饮食与适量活动，留意天气变化。', 'low')

        priority_order = {'urgent': 0, 'high': 1, 'medium': 2, 'low': 3}
        result.sort(key=lambda item: priority_order.get(item.get('priority', 'medium'), 9))
        return result[:8]

    def _top_component_reasons(self, component_scores):
        labels = {
            '温度风险': '温度暴露已偏离舒适区',
            '空气质量风险': '空气质量因素导致风险上升',
            '湿度风险': '湿度处于不利区间',
            '极端天气暴露': '存在极端天气触发因素',
            '个体易感性': '年龄/慢病使个体对天气更敏感',
            '即时健康筛查': '即时状态筛查提示风险上升',
            '社区脆弱性': '所在社区脆弱性较高',
            '社区病例负担': '近期社区病例负担偏高'
        }
        sorted_items = sorted(component_scores.items(), key=lambda x: float(x[1]), reverse=True)
        reasons = []
        for name, score in sorted_items[:4]:
            if float(score) < 40:
                continue
            reason = labels.get(name)
            if reason:
                reasons.append(reason)
        return reasons

    def _matrix_actions(self, impact_likelihood):
        impact = impact_likelihood.get('impact_bucket')
        likelihood = impact_likelihood.get('likelihood_bucket')
        if impact in ('high', 'very_high') and likelihood in ('high', 'very_high'):
            return ['建议立即执行高优先级防护并启动家庭/社区协同监测。']
        if impact in ('high', 'very_high'):
            return ['建议提前准备药物、补水和降温/保暖资源。']
        if likelihood in ('high', 'very_high'):
            return ['建议今天增加自我监测频次，必要时减少户外活动。']
        return ['保持常规防护，关注后续天气与健康提示更新。']

    @staticmethod
    def _merge_unique(base_items, extra_items):
        result = []
        seen = set()
        for item in (base_items or []) + (extra_items or []):
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _to_int(value, default=0, min_value=None, max_value=None):
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = int(default)
        if min_value is not None:
            result = max(int(min_value), result)
        if max_value is not None:
            result = min(int(max_value), result)
        return result

    @staticmethod
    def _clamp(value, lower, upper):
        return max(float(lower), min(float(upper), float(value)))

    @staticmethod
    def _bucket_three(score, low, high, labels):
        score = float(score)
        if score < low:
            return labels[0]
        if score < high:
            return labels[1]
        return labels[2]
