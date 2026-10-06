# -*- coding: utf-8 -*-
"""
天气预报后处理与7天健康预测服务

功能：
B1. 天气预报输入（CMA/和风天气API）
B2. 原始预报保留与未校准情景区间
B3. Lag拼接（过去7天观测 + 未来预报）
B4. 健康预测（探索性点预测；概率与模型预警待校准）
B5. 回测评估
"""
import pandas as pd
import numpy as np
from datetime import timedelta
from scipy import stats
import json
from pathlib import Path
import os
import logging
from core.time_utils import today_local, now_local
from services.missing_policy import input_state, weighted_known_risk, risk_floor


class ForecastService:
    """天气预报后处理与健康预测服务"""
    
    def __init__(self):
        self.weather_history = None  # 历史天气观测
        self.forecast_history = None  # 历史预报数据（用于后处理校准）
        self.qm_params = {}  # Quantile Mapping参数
        self.emos_params = {}  # EMOS参数
        self.visit_threshold_p90 = None  # 门诊量P90阈值
        self.max_observed_daily_visits = None  # 历史最大日门诊量（用于护栏）
        
        # 加载历史数据
        self._load_historical_data()
        self._calculate_thresholds()
    
    def _load_historical_data(self):
        """加载历史天气观测数据"""
        try:
            base_dir = Path(__file__).resolve().parents[1]
            weather_path = base_dir / 'data' / 'raw' / '逐日数据.csv'
            df = pd.read_csv(weather_path, encoding='utf-8')
            
            # 查找日期和温度列
            date_col = None
            temp_cols = {}
            
            for col in df.columns:
                if '日期' in col:
                    date_col = col
                if '2米平均气温' in col and '多源融合' in col:
                    temp_cols['tmean'] = col
                if '2米最低气温' in col and '多源融合' in col:
                    temp_cols['tmin'] = col
                if '2米最高气温' in col and '多源融合' in col:
                    temp_cols['tmax'] = col
                if '平均相对湿度' in col and '多源融合' in col:
                    temp_cols['humidity'] = col
                if '降雨量' in col and '多源融合' in col:
                    temp_cols['precipitation'] = col
                if '平均风速' in col and '多源融合' in col:
                    temp_cols['wind_speed'] = col
            
            # 重命名列
            rename_map = {date_col: 'date'} if date_col else {}
            rename_map.update({v: k for k, v in temp_cols.items()})
            
            df = df.rename(columns=rename_map)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
            
            # 转换数值列
            for col in ['tmean', 'tmin', 'tmax', 'humidity', 'precipitation', 'wind_speed']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            
            self.weather_history = df
            print(f"✅ 历史天气数据加载成功: {len(df)} 天")
            
            # 计算QM参数
            self._calculate_qm_params()
            
        except Exception as e:
            print(f"⚠️ 历史天气数据加载失败: {e}")
            self.weather_history = pd.DataFrame()
    
    def _calculate_qm_params(self):
        """计算Quantile Mapping参数"""
        if self.weather_history.empty or 'tmean' not in self.weather_history.columns:
            return
        
        temps = self.weather_history['tmean'].dropna()
        
        # 计算历史温度的分位数分布
        self.qm_params = {
            'percentiles': np.arange(0, 101, 5),  # 0%, 5%, 10%, ..., 100%
            'temp_values': np.percentile(temps, np.arange(0, 101, 5)),
            'mean': temps.mean(),
            'std': temps.std(),
            'min': temps.min(),
            'max': temps.max()
        }
    
    def _calculate_thresholds(self):
        """计算门诊量阈值"""
        try:
            base_dir = Path(__file__).resolve().parents[1]
            logger = logging.getLogger(__name__)
            env_path = os.getenv('MEDICAL_DATA_PATH')
            data_path = Path(env_path) if env_path else (base_dir / 'data' / 'research' / '数据.xlsx')
            if not data_path.exists():
                raise FileNotFoundError(f"medical data not found: {data_path}")

            # Minimize PII: only load columns needed for thresholds (就诊时间 -> date -> daily count)
            # 0-based indexes based on your schema: 5=就诊时间
            df = pd.read_excel(data_path, header=None, usecols=[5])
            df.columns = ['就诊时间']
            
            df['就诊时间'] = pd.to_datetime(df['就诊时间'])
            df['date'] = df['就诊时间'].dt.date
            
            daily_visits = df.groupby('date').size()
            try:
                self.max_observed_daily_visits = int(daily_visits.max()) if len(daily_visits) else None
            except Exception:
                self.max_observed_daily_visits = None
            
            self.visit_threshold_p90 = daily_visits.quantile(0.90)
            self.visit_threshold_p75 = daily_visits.quantile(0.75)
            self.visit_mean = daily_visits.mean()
            self.visit_std = daily_visits.std()
            
            logger.info(
                "Visit thresholds calculated (P90=%s mean=%s)",
                round(self.visit_threshold_p90, 2),
                round(self.visit_mean, 2)
            )
            
        except Exception as e:
            logging.getLogger(__name__).warning("Visit thresholds calculation failed: %s", e)
            self.visit_threshold_p90 = None
            self.visit_threshold_p75 = None
            self.visit_mean = None
            self.visit_std = None
            self.max_observed_daily_visits = None

    def _safe_float(self, value, default=None):
        try:
            parsed = float(value)
        except Exception:
            return default
        return parsed if np.isfinite(parsed) else default

    def _normalize_forecast_entry(self, entry):
        """
        将输入预报条目标准化：
        - 兼容 float/int
        - 兼容含 ensemble 字段的 dict
        """
        base = {
            'temp': None,
            'temp_min': None,
            'temp_max': None,
            'temperature_p10': None,
            'temperature_p50': None,
            'temperature_p90': None,
            'humidity': None,
            'aqi': None,
            'pm25': None,
            'precip_probability': None,
            'model_spread': None,
            'model_count': 1,
            'model_names': [],
            'predictability_score': None,
            'source': ''
        }
        if isinstance(entry, (int, float)):
            parsed_temp = self._safe_float(entry)
            if parsed_temp is None:
                raise ValueError("forecast temperature must be finite")
            base['temp'] = parsed_temp
            return base
        if not isinstance(entry, dict):
            raise ValueError("forecast entry must contain a finite temperature")

        p10 = self._safe_float(entry.get('temperature_ensemble_p10'))
        p50 = None
        for raw_value in (
            entry.get('temperature_ensemble_p50'),
            entry.get('temperature_ensemble_mean'),
            entry.get('temperature_mean'),
            entry.get('temperature'),
        ):
            parsed = self._safe_float(raw_value)
            if parsed is not None:
                p50 = parsed
                break
        p90 = self._safe_float(entry.get('temperature_ensemble_p90'))

        temp = p50
        tmax = self._safe_float(entry.get('temperature_max'))
        tmin = self._safe_float(entry.get('temperature_min'))
        if temp is None:
            if tmax is not None and tmin is not None:
                temp = (tmax + tmin) / 2.0

        if temp is None:
            raise ValueError('forecast temperature unavailable')
        base['temp'] = temp
        base['temp_min'] = tmin
        base['temp_max'] = tmax
        base['temperature_p10'] = p10
        base['temperature_p50'] = p50
        base['temperature_p90'] = p90
        base['humidity'] = self._safe_float(entry.get('humidity'))
        base['aqi'] = self._safe_float(entry.get('aqi'))
        base['pm25'] = self._safe_float(entry.get('pm25'))
        for key in ('aqi_standard', 'primary_pollutant', 'aqi_averaging_hours'):
            base[key] = entry.get(key)
        base['precip_probability'] = self._safe_float(
            entry.get('precip_probability', entry.get('precipitation_probability'))
        )

        base['model_spread'] = self._safe_float(
            entry.get('temperature_ensemble_std', entry.get('model_spread')),
            None
        )
        model_names = entry.get('model_names') or entry.get('models') or []
        if isinstance(model_names, str):
            model_names = [m.strip() for m in model_names.split(',') if m.strip()]
        if not isinstance(model_names, list):
            model_names = []
        base['model_names'] = model_names
        if entry.get('model_count') is not None:
            base['model_count'] = int(self._safe_float(entry.get('model_count'), len(model_names) or 1))
        else:
            base['model_count'] = max(1, len(model_names))
        base['predictability_score'] = entry.get('predictability_score')
        base['predictability_source'] = entry.get('predictability_source')
        base['predictability_method'] = entry.get('predictability_method')
        base['source'] = str(entry.get('data_source') or '')
        return base

    def _composite_exposure_risk(
        self, temperature, temp_min, humidity, pm25=None, aqi=None, *,
        temp_min_fallback=None, pm25_origin=None, aqi_origin=None,
        aqi_standard=None, primary_pollutant=None, aqi_averaging_hours=None,
    ):
        """仅对有依据的暴露计算分值，并保留未知分项与插补来源。"""
        temp = self._safe_float(temperature)
        tmin = self._safe_float(temp_min)
        hum = self._safe_float(humidity)
        pm = self._safe_float(pm25)
        if hum is not None and not 0 <= hum <= 100:
            hum = None
        if pm is not None and pm < 0:
            pm = None
        pm_source = 'direct' if pm is not None else 'unknown'
        # 今天的实况不能冒充未来日预报。
        if pm25_origin == 'current_weather_context':
            pm = None
            pm_source = 'unknown'
        aqi_value = self._safe_float(aqi)
        inverse_method = None
        # 旧标准只允许有明确版本和日均口径的数据；不猜测当前 cn-mee 的标准版本。
        if (pm is None and aqi_origin != 'current_weather_context'
                and aqi_standard == 'HJ633-2012' and aqi_averaging_hours == 24
                and str(primary_pollutant).lower() in {'pm2.5', 'pm2p5', 'pm25'}
                and aqi_value is not None and 0 <= aqi_value <= 500):
            pm = float(np.interp(aqi_value, [0, 50, 100, 150, 200, 300, 400, 500],
                                 [0, 35, 75, 115, 150, 250, 350, 500]))
            pm_source = 'hj633_2012_inverse'
            inverse_method = 'HJ633-2012_PM2.5_24h_piecewise_inverse'

        components = {
            'heat': float(np.clip((temp - 28) * 6, 0, 100)) if temp is not None else None,
            'pm25': float(np.clip((pm - 35) * 1.8, 0, 100)) if pm is not None else None,
            'humidity': float(np.clip((hum - 70) * 2.4, 0, 100)) if hum is not None else None,
            'hot_night': (100.0 if tmin >= 26 else 72.0 if tmin >= 24 else 45.0 if tmin >= 22 else 8.0) if tmin is not None else None,
        }
        weights = {'heat': .34, 'pm25': .28, 'humidity': .18, 'hot_night': .20}
        known = weighted_known_risk(components, weights)
        synergy = 0.0
        heat, pollution, humid, night = (components[k] for k in weights)
        if heat is not None and pollution is not None and heat >= 45 and pollution >= 40:
            synergy += 8
        if heat is not None and humid is not None and heat >= 45 and humid >= 40:
            synergy += 6
        if night is not None and pollution is not None and night >= 70 and pollution >= 35:
            synergy += 4
        score = known['score']
        # 高温筛查必须有温度证据；其他分项不能单独给出总体低风险。
        missing_required_inputs = ['temperature'] if temp is None else []
        pre_clip = score + synergy if score is not None and not missing_required_inputs else None
        if inverse_method:
            # 补入较低污染分项不能稀释已知天气风险。
            baseline = self._composite_exposure_risk(temperature, temp_min, humidity)['score']
            pre_clip = risk_floor(pre_clip, baseline, status='imputed')
        final = float(np.clip(pre_clip, 0, 100)) if pre_clip is not None else None
        inputs = {
            'temperature': input_state(temp, source='corrected_forecast'),
            'temp_min': input_state(tmin, source='forecast_input'),
            'humidity': input_state(hum, source='forecast_input'),
            'pm25': input_state(pm, source=pm_source, method=inverse_method,
                                reason='no_valid_daily_pollutant_forecast' if pm is None else None),
        }
        inputs['pm25'].update({'detail_source': pm25_origin or 'forecast_input',
                              'aqi_used': aqi_value if inverse_method else None, 'aqi_imputed': False})
        rounded = lambda value: round(value, 1) if value is not None else None
        return {
            'score': rounded(final), 'pre_clip_score': rounded(pre_clip), 'final_score': rounded(final),
            'synergy_bonus': synergy,
            'threshold_semantics': 'action_communication_interface',
            'warning_calibrated': False,
            'level': '未知' if final is None else '高' if final >= 70 else '中' if final >= 45 else '低',
            'status': known['status'] if final is not None else 'unknown', 'unknown_components': known['unknown_components'],
            'effective_weights': known['effective_weights'],
            'missing_required_inputs': missing_required_inputs,
            'components': {k: rounded(v) for k, v in components.items()},
            'hot_night': tmin >= 22 if tmin is not None else None,
            'pm25_proxy': rounded(pm), 'pm25_source': pm_source, 'inputs': inputs,
        }

    def _cap_semantics_for_forecast(self, prob_high_percent, composite_level):
        """未校准前不把模型分数映射成 CAP 预警语义。"""
        del prob_high_percent, composite_level
        return {
            'severity': 'unknown',
            'certainty': 'unknown',
            'urgency': 'unknown',
            'status': 'disabled_uncalibrated',
        }

    def _build_role_action_cards(self, forecasts, summary):
        """按角色输出行动卡：居民 / 村医 / 社区干部。"""
        composite_high_days = [row for row in forecasts if (row.get('composite_exposure') or {}).get('level') == '高']

        resident_cards = [
            {
                'priority': 'medium',
                'title': '居民日常行动',
                'action': '结合官方天气预警和逐日天气安排外出；复合暴露关注分只作防护沟通参考。'
            }
        ]
        if composite_high_days:
            resident_cards.append({
                'priority': 'high',
                'title': '复合暴露防护',
                'action': '出现“高温+污染/湿度”叠加风险，建议补水、降温并减少高强度活动。'
            })

        doctor_cards = [
            {
                'priority': 'medium',
                'title': '村医排班准备',
                'action': '门诊负担模型尚未完成时间切分校准，不据此自动排班；请结合官方预警与现场需求人工准备。'
            }
        ]

        community_cards = [
            {
                'priority': 'medium',
                'title': '社区资源调度',
                'action': '根据官方天气预警、现场容量和居民反馈调整避暑点开放与宣传频次。'
            },
            {
                'priority': 'medium',
                'title': '公众信息发布',
                'action': '同步发布“开始降雨时间/结束时间”和分时段行动建议，减少信息摩擦。'
            }
        ]

        return {
            'resident': resident_cards,
            'doctor': doctor_cards,
            'community': community_cards
        }

    def _calculate_predictability(
        self, lead_day, model_spread=None, model_count=1, external_score=None, *,
        external_source=None, external_method=None,
    ):
        """区分上游评分与本地启发式；任何分数均不宣称实测准确率。"""
        reported_spread = self._safe_float(model_spread)
        count = max(1, int(self._safe_float(model_count, 1)))
        spread = reported_spread if reported_spread is not None and reported_spread >= 0 and count >= 2 else None
        lead = max(1, int(lead_day))
        penalty = (lead - 1) * 3.0
        external = self._safe_float(external_score) if not isinstance(external_score, bool) else None
        external_valid = external is not None and 0 <= external <= 100
        external_status = 'valid' if external_valid else 'missing' if external_score is None else 'invalid'
        # 本服务上游产生的同一启发式不冒充第三方提供的独立评分。
        local_upstream = (external_source == 'local_weather_service'
                          and external_method in {'lead_only', 'ensemble_spread_heuristic'})
        if external_valid and not local_upstream:
            branch, raw, score = 'external', external, external
            source = str(external_source or 'upstream_unspecified')
            method = str(external_method or 'upstream_method_unspecified')
            note = '采用上游参考分；校准方法未验证，非实测准确率'
            if spread is None:
                note += '；无多模型信息，无法用离散度交叉核对'
        elif spread is None:
            branch, raw = 'lead_only', 60.0 - penalty
            score = max(5.0, min(60.0, raw))
            source, method = 'local_forecast_service', 'lead_only'
            note = '无多模型信息；仅按提前期的启发式评分，非实测准确率'
        else:
            branch, raw = 'derived', 100.0 - spread * 16 - penalty
            score = max(5.0, min(99.0, raw))
            source, method = 'local_forecast_service', 'ensemble_spread_heuristic'
            note = '多模型离散度启发式评分，非实测准确率'
        if external_status == 'invalid':
            note += '；上游分数无效，已回退本地评分'
        return {
            'score': round(score, 1), 'label': '高' if score >= 75 else '中' if score >= 50 else '低',
            'branch': branch, 'raw_score': round(raw, 1), 'validated': False,
            'source': source, 'method': method, 'calibration_status': 'unvalidated',
            'multimodel_information_available': spread is not None,
            'limitations': ['未验证与实际预报误差的关系', '不代表健康模型准确率'],
            'note': note,
            'inputs': {'external_score': external, 'external_score_status': external_status,
                       'external_score_rejection': 'expected_finite_score_0_100' if external_status == 'invalid' else None,
                       'external_source': external_source, 'external_method': external_method,
                       'lead_day': lead, 'model_spread': spread, 'reported_model_spread': reported_spread,
                       'model_count': count, 'lead_penalty': penalty if branch != 'external' else None,
                       'model_bonus': None},
        }

    def _build_impact_likelihood_matrix(self, forecasts):
        """Likelihood 没有校准概率时，矩阵必须显式不可用。"""
        del forecasts
        return {
            'available': False,
            'status': 'disabled_uncalibrated_probability',
            'impact_levels': [],
            'likelihood_levels': [],
            'cells': None,
        }

    def quantile_mapping(self, forecast_temp, lead_day=1, model_spread=None):
        """兼容旧调用名；没有配对回测校准参数时保留原预报值。"""
        forecast_temp = self._safe_float(forecast_temp)
        if forecast_temp is None:
            raise ValueError("forecast temperature must be finite")
        spread = self._safe_float(model_spread)
        if spread is not None and spread < 0:
            spread = None
        # 区间仅供情景探索，不声称为经过覆盖率验证的置信区间。
        width = 2.0 * (1 + .3 * max(0, int(lead_day) - 1)) + min(4.0, (spread or 0) * .6)
        return forecast_temp, {
            'lower': forecast_temp - width, 'upper': forecast_temp + width,
            'std': width / 1.96, 'lead_day': lead_day, 'original_temp': forecast_temp,
            'bias_correction': 0.0, 'model_spread': spread,
            'calibration_status': 'unvalidated', 'interval_method': 'heuristic_scenario',
        }

    def get_lag_temperature_profile(self, target_date, forecast_temps=None):
        """
        获取目标日期的滞后温度profile
        
        拼接：过去7天真实观测 + 目标日期预报温度
        
        参数:
        - target_date: 目标预测日期
        - forecast_temps: 预报温度字典 {date: temp}
        
        返回:
        - lag_profile: 滞后温度列表 [lag0, lag1, ..., lag7]
        - data_sources: 数据来源标记
        """
        target_date = pd.to_datetime(target_date)
        lag_profile = []
        data_sources = []
        
        # 标准化 forecast_temps 的键为 date 对象
        normalized_forecast = {}
        if forecast_temps:
            for k, v in forecast_temps.items():
                if hasattr(k, 'date'):
                    # datetime 对象
                    normalized_forecast[k.date() if callable(k.date) else k] = v
                elif isinstance(k, str):
                    # 字符串日期
                    normalized_forecast[pd.to_datetime(k).date()] = v
                else:
                    # 已经是 date 对象
                    normalized_forecast[k] = v
        
        for lag in range(8):  # lag 0 到 7
            check_date = target_date - timedelta(days=lag)
            check_date_only = check_date.date() if hasattr(check_date, 'date') else check_date
            
            # 尝试从历史观测获取
            if self.weather_history is not None and not self.weather_history.empty:
                try:
                    obs = self.weather_history[
                        self.weather_history['date'].dt.date == check_date_only
                    ]
                    if not obs.empty and 'tmean' in obs.columns:
                        temp = obs['tmean'].iloc[0]
                        parsed_temp = self._safe_float(temp)
                        if parsed_temp is not None:
                            lag_profile.append(parsed_temp)
                            data_sources.append('observation')
                            continue
                except Exception:
                    pass
            
            # 尝试从预报获取
            if normalized_forecast and check_date_only in normalized_forecast:
                parsed_temp = self._safe_float(normalized_forecast[check_date_only])
                if parsed_temp is not None:
                    lag_profile.append(parsed_temp)
                    data_sources.append('forecast')
                    continue
            
            # 不能用气候态或固定15℃替代缺失实况并降低风险。
            lag_profile.append(None)
            data_sources.append('unknown')
        
        return lag_profile, data_sources
    
    @staticmethod
    def _unknown_visit_prediction(missing_inputs):
        """缺少研究输入时门诊量与概率保持未知，独立的暴露评分仍可使用。"""
        keys = ('point_estimate', 'lower_bound', 'upper_bound', 'p10', 'p50', 'p90',
                'probability_exceed_p90', 'probability_exceed_p75', 'rr', 'baseline',
                'dow_factor', 'raw_point_estimate', 'visit_threshold_p90', 'std_estimate')
        return {**dict.fromkeys(keys), 'status': 'unknown', 'missing_inputs': missing_inputs,
                'probability_method': 'unavailable', 'guardrail_applied': False, 'guardrail_cap': None}

    def predict_daily_visits(self, temperature, lag_temps=None, month=None, dow=None):
        """
        预测日门诊量
        
        参数:
        - temperature: 当天温度
        - lag_temps: 过去7天温度
        - month: 月份
        - dow: 星期几（0-6）
        
        返回:
        - point_estimate: 探索性点预测
        - interval: 固定离散参数下的探索性范围
        - probability_high: 未校准，固定返回 None
        """
        missing = []
        if self._safe_float(temperature) is None:
            missing.append('temperature')
        if not lag_temps or any(self._safe_float(value) is None for value in lag_temps):
            missing.append('lag_temperatures')
        threshold = self._safe_float(self.visit_threshold_p90)
        if threshold is None or threshold <= 0:
            missing.append('historical_visit_threshold')
        if missing:
            return self._unknown_visit_prediction(missing)
        from services.dlnm_risk_service import get_dlnm_service
        
        dlnm = get_dlnm_service()
        
        # 获取相对风险
        rr, breakdown = dlnm.calculate_rr(temperature, lag_temps)
        
        # 基础门诊量（考虑季节性）
        if month and month in dlnm.seasonal_baseline:
            baseline = dlnm.seasonal_baseline[month]['avg_visits']
        else:
            baseline = self.visit_mean
        
        baseline = self._safe_float(baseline)
        if baseline is None or baseline <= 0:
            return self._unknown_visit_prediction(['historical_visit_baseline'])

        # 星期效应
        dow_factor = 1.0
        if dow is not None:
            # 周末门诊量通常较低
            if dow in [5, 6]:
                dow_factor = 0.7
            elif dow == 0:  # 周一略高
                dow_factor = 1.1
        
        # 点预测保留为研究参考，不用于触发模型预警。
        point_estimate = baseline * rr * dow_factor
        raw_point_estimate = float(point_estimate)
        
        # 预测区间（基于Negative Binomial分布的不确定性）
        # 使用过度离散参数 theta
        theta = 2.0  # 可调整
        std_estimate = np.sqrt(point_estimate + point_estimate**2 / theta)
        
        lower_bound = max(0, point_estimate - 1.96 * std_estimate)
        upper_bound = point_estimate + 1.96 * std_estimate
        
        # 历史阈值保留用于审计；概率没有时间切分校准，线上不计算。
        visit_threshold_p90 = self._safe_float(self.visit_threshold_p90, None)
        probability_method = 'disabled_uncalibrated'
        
        # --- Safety guardrail: clamp implausible outliers (pilot reliability) ---
        max_cap = None
        try:
            if self.max_observed_daily_visits is not None:
                max_cap = float(self.max_observed_daily_visits) * 2.0
        except Exception:
            max_cap = None

        def _clamp(value):
            if value is None:
                return None
            try:
                v = float(value)
            except Exception:
                return value
            if v < 0:
                v = 0.0
            if max_cap is not None and v > max_cap:
                v = max_cap
            return v

        point_estimate = _clamp(raw_point_estimate)
        lower_bound = _clamp(lower_bound)
        upper_bound = _clamp(upper_bound)
        guardrail_applied = bool(
            point_estimate is not None
            and abs(float(point_estimate) - raw_point_estimate) > 1e-9
        )

        p10 = _clamp(max(0, (point_estimate or 0) - 1.28 * std_estimate))
        p50 = _clamp(point_estimate)
        p90 = _clamp((point_estimate or 0) + 1.28 * std_estimate)

        return {
            'point_estimate': round(point_estimate, 1) if point_estimate is not None else None,
            'lower_bound': round(lower_bound, 1) if lower_bound is not None else None,
            'upper_bound': round(upper_bound, 1) if upper_bound is not None else None,
            'p10': round(p10, 1) if p10 is not None else None,
            'p50': round(p50, 1) if p50 is not None else None,
            'p90': round(p90, 1) if p90 is not None else None,
            'probability_exceed_p90': None,
            'probability_exceed_p75': None,
            'rr': round(rr, 3),
            'baseline': round(baseline, 1),
            'dow_factor': round(dow_factor, 3),
            'raw_point_estimate': round(raw_point_estimate, 4),
            'visit_threshold_p90': round(visit_threshold_p90, 4) if visit_threshold_p90 is not None else None,
            'std_estimate': round(float(std_estimate), 4),
            'probability_method': probability_method,
            'probability_calibrated': False,
            'prediction_status': 'exploratory_point_estimate',
            'interval_status': 'exploratory_fixed_theta_not_calibrated',
            'guardrail_cap': round(max_cap, 1) if max_cap is not None else None,
            'guardrail_applied': guardrail_applied,
            'temperature': temperature
        }
    
    def generate_7day_forecast(self, forecast_temps, start_date=None, context=None):
        """
        生成未来7天健康预测
        
        参数:
        - forecast_temps: 未来7天预报温度列表或字典
        - start_date: 起始日期（默认明天）
        
        返回:
        - forecasts: 7天预测结果列表
        - summary: 汇总信息
        """
        if start_date is None:
            start_date = today_local() + timedelta(days=1)
        else:
            start_date = pd.to_datetime(start_date).date()
        
        # 转换预报温度格式为统一的 date -> entry 字典
        if isinstance(forecast_temps, list):
            forecast_temps_dict = {
                (start_date + timedelta(days=i)): self._normalize_forecast_entry(temp)
                for i, temp in enumerate(forecast_temps)
            }
        elif isinstance(forecast_temps, dict):
            # 标准化键为 date 对象，值统一转 entry
            forecast_temps_dict = {}
            for k, v in forecast_temps.items():
                if hasattr(k, 'date') and callable(k.date):
                    key = k.date()
                elif isinstance(k, str):
                    key = pd.to_datetime(k).date()
                else:
                    key = k
                forecast_temps_dict[key] = self._normalize_forecast_entry(v)
        else:
            raise ValueError("forecast_temps must be a list or dict")
        
        forecasts = []
        total_expected_visits = 0
        predictability_scores = []
        model_sources = set()
        total_worst_case_visits = 0.0
        total_optimistic_visits = 0.0
        composite_scores = []
        composite_high_days = 0

        # 获取温度列表用于备选
        temp_values = [entry.get('temp') for entry in forecast_temps_dict.values()]
        context = context or {}
        
        for lead_day in range(1, 8):
            target_date = start_date + timedelta(days=lead_day - 1)
            
            # 获取预报输入条目
            selected_entry = None
            if target_date in forecast_temps_dict:
                selected_entry = forecast_temps_dict[target_date]
            elif lead_day <= len(temp_values):
                selected_entry = self._normalize_forecast_entry(temp_values[lead_day - 1])
            else:
                raise ValueError(f"insufficient forecast data for day {lead_day}")
            raw_temp = selected_entry.get('temp')
            model_spread = selected_entry.get('model_spread')
            model_count = selected_entry.get('model_count', 1)
            model_names = selected_entry.get('model_names', []) or []
            humidity = selected_entry.get('humidity')
            temp_min = selected_entry.get('temp_min')
            pm25 = selected_entry.get('pm25')
            pm25_origin = 'forecast_input' if pm25 is not None else None
            aqi = selected_entry.get('aqi')
            aqi_origin = 'forecast_input' if aqi is not None else None
            if selected_entry.get('source'):
                model_sources.add(selected_entry.get('source'))
            
            # 后处理校正
            corrected_temp, uncertainty = self.quantile_mapping(
                raw_temp,
                lead_day,
                model_spread=model_spread
            )
            
            # 获取滞后温度profile
            past_temp_map = {
                d: (e.get('temp') if isinstance(e, dict) else float(e))
                for d, e in forecast_temps_dict.items()
                if d <= target_date
            }
            lag_temps, sources = self.get_lag_temperature_profile(
                target_date, 
                forecast_temps=past_temp_map
            )
            
            # 预测门诊量
            month = target_date.month
            dow = target_date.weekday()
            
            prediction = self.predict_daily_visits(
                corrected_temp, 
                lag_temps, 
                month=month, 
                dow=dow
            )
            
            # 固定 theta + 正态近似未经时间切分校准，停用红橙黄模型预警。
            risk_level = None
            risk_color = 'secondary'
            
            # 识别极端天气
            from services.dlnm_risk_service import get_dlnm_service
            dlnm = get_dlnm_service()
            extreme_events = dlnm.identify_extreme_weather_events(corrected_temp)

            predictability = self._calculate_predictability(
                lead_day=lead_day,
                model_spread=model_spread,
                model_count=model_count,
                external_score=selected_entry.get('predictability_score'),
                external_source=selected_entry.get('predictability_source'),
                external_method=selected_entry.get('predictability_method'),
            )
            predictability_scores.append(predictability['score'])
            confidence = 'high' if predictability['score'] >= 75 else 'medium' if predictability['score'] >= 50 else 'low'

            # 复合暴露风险（热 + PM2.5 + 湿度 + 热夜）
            composite_exposure = self._composite_exposure_risk(
                corrected_temp,
                temp_min=temp_min,
                humidity=humidity,
                pm25=pm25,
                aqi=aqi,
                temp_min_fallback=uncertainty.get('lower'),
                pm25_origin=pm25_origin,
                aqi_origin=aqi_origin,
                aqi_standard=selected_entry.get('aqi_standard'),
                primary_pollutant=selected_entry.get('primary_pollutant'),
                aqi_averaging_hours=selected_entry.get('aqi_averaging_hours'),
            )
            composite_scores.append(self._safe_float(composite_exposure.get('score'), 0.0) or 0.0)
            if composite_exposure.get('level') == '高':
                composite_high_days += 1

            cap_semantics = self._cap_semantics_for_forecast(
                prob_high_percent=None,
                composite_level=composite_exposure.get('level')
            )

            forecast = {
                'date': target_date.strftime('%Y-%m-%d'),
                'lead_day': lead_day,
                'day_of_week': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][dow],
                
                # 温度信息
                'temperature': {
                    'forecast': round(raw_temp, 1),
                    'corrected': round(corrected_temp, 1),
                    'uncertainty_lower': round(uncertainty['lower'], 1),
                    'uncertainty_upper': round(uncertainty['upper'], 1),
                    'input_spread': round(model_spread, 3) if model_spread is not None else None,
                    'calibration_status': uncertainty.get('calibration_status', 'unvalidated'),
                    'interval_method': uncertainty.get('interval_method', 'heuristic_scenario'),
                    'p10': round(selected_entry.get('temperature_p10'), 1) if selected_entry.get('temperature_p10') is not None else None,
                    'p50': round(selected_entry.get('temperature_p50'), 1) if selected_entry.get('temperature_p50') is not None else round(corrected_temp, 1),
                    'p90': round(selected_entry.get('temperature_p90'), 1) if selected_entry.get('temperature_p90') is not None else None,
                    'humidity': round(humidity, 1) if humidity is not None else None
                },
                
                # 门诊量预测
                'visits': prediction,
                'scenarios': {
                    'optimistic': prediction.get('p10'),
                    'baseline': prediction.get('p50', prediction.get('point_estimate')),
                    'worst_case': prediction.get('p90')
                },
                
                # 风险信息
                'risk_level': risk_level,
                'risk_color': risk_color,
                'probability_high_visits': None,
                'model_warning_status': 'disabled_uncalibrated',
                'lag_input_states': [input_state(value, source=source) for value, source in zip(lag_temps, sources)],
                
                # 极端天气
                'extreme_events': extreme_events,

                # 模型融合与可预报性
                'model_fusion': {
                    'model_count': int(model_count) if model_count else 1,
                    'model_names': model_names
                },
                'predictability': predictability,

                # 置信度
                'confidence': confidence,
                'cap_semantics': cap_semantics,
                'composite_exposure': composite_exposure
            }
            
            forecasts.append(forecast)
            total_expected_visits += self._safe_float(prediction.get('point_estimate'), 0.0) or 0.0
            total_optimistic_visits += self._safe_float(prediction.get('p10'), 0.0) or 0.0
            total_worst_case_visits += self._safe_float(prediction.get('p90'), 0.0) or 0.0
        
        unknown_health_days = sum(1 for row in forecasts if row['visits'].get('point_estimate') is None)
        visits_complete = unknown_health_days == 0
        # 生成建议
        recommendations = self._generate_forecast_recommendations(forecasts, None)
        avg_predictability = round(sum(predictability_scores) / len(predictability_scores), 1) if predictability_scores else None
        low_predictability_days = sum(1 for s in predictability_scores if s < 50)
        
        summary = {
            'forecast_period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': (start_date + timedelta(days=6)).strftime('%Y-%m-%d')
            },
            'total_expected_visits': round(total_expected_visits, 0) if visits_complete else None,
            'high_risk_days': None,
            'unknown_health_days': unknown_health_days,
            'average_daily_visits': round(total_expected_visits / 7, 1) if visits_complete else None,
            'visit_projection_status': 'exploratory_uncalibrated' if visits_complete else 'unknown',
            'overall_risk': 'unavailable',
            'model_warning_status': 'disabled_uncalibrated',
            'recommendations': recommendations,
            'scenario_totals': {
                'status': 'exploratory_fixed_theta_not_calibrated',
                'optimistic_total': round(total_optimistic_visits, 1) if visits_complete else None,
                'baseline_total': round(total_expected_visits, 1) if visits_complete else None,
                'worst_case_total': round(total_worst_case_visits, 1) if visits_complete else None,
                'worst_case_extra': round(max(0.0, total_worst_case_visits - total_expected_visits), 1) if visits_complete else None
            },
            'probability_products': {
                'available': False,
                'status': 'disabled_uncalibrated',
                'days_prob_exceed_p90_ge50': None,
                'days_prob_exceed_p90_ge30': None,
                'days_prob_exceed_p75_ge50': None,
            },
            'predictability': {
                'average_score': avg_predictability,
                'low_predictability_days': low_predictability_days
            },
            'composite_exposure': {
                'average_score': round(float(np.mean(composite_scores)) if composite_scores else 0.0, 1),
                'high_attention_days': composite_high_days,
                'threshold_semantics': 'action_communication_interface',
            },
            'impact_likelihood_matrix': self._build_impact_likelihood_matrix(forecasts),
            'model_sources': sorted(model_sources),
            'generated_at': now_local().strftime('%Y-%m-%d %H:%M:%S')
        }
        summary['role_action_cards'] = self._build_role_action_cards(forecasts, summary)
        
        return forecasts, summary
    
    def _generate_forecast_recommendations(self, forecasts, high_risk_days):
        """生成预测建议"""
        recommendations = []
        
        # 只有经过校准的模型高风险天数才能触发资源配置建议。
        if high_risk_days is not None and high_risk_days >= 3:
            recommendations.append({
                'priority': 'high',
                'category': '资源调配',
                'advice': f'未来一周有{high_risk_days}天门诊量预计较高，建议提前增派医护人员'
            })
        
        # 分析极端天气
        extreme_days = [f for f in forecasts if f['extreme_events']]
        if extreme_days:
            for day in extreme_days:
                for event in day['extreme_events']:
                    recommendations.append({
                        'priority': 'high' if event['severity'] == 'extreme' else 'medium',
                        'category': '天气事件',
                        'advice': f"{day['date']}: {event['description']}（模型阈值行动提示，非官方预警）"
                    })
        
        # 温度趋势分析
        temps = [f['temperature']['corrected'] for f in forecasts]
        if max(temps) - min(temps) > 10:
            recommendations.append({
                'priority': 'medium',
                'category': '温差提醒',
                'advice': f'未来一周温差较大({min(temps):.0f}°C ~ {max(temps):.0f}°C)，注意防范温度骤变影响'
            })
        
        if not recommendations:
            recommendations.append({
                'priority': 'low',
                'category': '常规管理',
                'advice': '本页模型预警未启用，请继续关注官方天气预警与逐日天气变化'
            })
        
        return recommendations
    
    def calculate_forecast_accuracy(self, forecast_date, actual_visits):
        """
        回测：计算预报准确性
        
        参数:
        - forecast_date: 预报日期
        - actual_visits: 实际门诊量
        
        返回:
        - metrics: 评估指标
        """
        # 这里可以存储历史预报与实际值的对比
        # 计算MAE, RMSE, Brier Score等
        
        metrics = {
            'mae': None,  # Mean Absolute Error
            'rmse': None,  # Root Mean Square Error
            'brier_score': None,  # 概率预报校准度
            'reliability': None  # 可靠性
        }
        
        return metrics
    
    def get_service_status(self):
        """获取服务状态"""
        return {
            'weather_history_loaded': self.weather_history is not None and not self.weather_history.empty,
            'weather_history_days': len(self.weather_history) if self.weather_history is not None else 0,
            'qm_params_calculated': bool(self.qm_params),
            'visit_threshold_p90': self.visit_threshold_p90,
            'visit_mean': self.visit_mean
        }


# 单例实例
_forecast_service = None

def get_forecast_service():
    """获取预报服务单例"""
    global _forecast_service
    if _forecast_service is None:
        _forecast_service = ForecastService()
    return _forecast_service


# 测试代码
if __name__ == '__main__':
    print("=" * 60)
    print("天气预报后处理与健康预测服务测试")
    print("=" * 60)
    
    service = ForecastService()
    
    print("\n服务状态:")
    print(json.dumps(service.get_service_status(), ensure_ascii=False, indent=2))
    
    print("\n7天预测测试:")
    # 模拟未来7天温度预报
    forecast_temps = [15, 18, 22, 25, 20, 16, 12]
    
    forecasts, summary = service.generate_7day_forecast(forecast_temps)
    
    print("\n预测摘要:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    
    print("\n每日预测:")
    for f in forecasts:
        print(f"  {f['date']} ({f['day_of_week']}): "
              f"温度{f['temperature']['corrected']}°C, "
              f"预计门诊{f['visits']['point_estimate']}人次, "
              f"超阈值概率{f['probability_high_visits']}%, "
              f"{f['risk_level']}")

