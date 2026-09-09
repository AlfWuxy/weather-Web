# -*- coding: utf-8 -*-
"""
天气预报后处理与7天健康预测服务

功能：
B1. 天气预报输入（CMA/和风天气API）
B2. 预报后处理（Quantile Mapping / EMOS）
B3. Lag拼接（过去7天观测 + 未来预报）
B4. 健康预测（点预测 + 区间 + 概率预警）
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
            self.visit_threshold_p90 = 15
            self.visit_mean = 10
            self.visit_std = 5
            self.max_observed_daily_visits = 30

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
            'temp': 15.0,
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
            return base

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

        base['temp'] = self._safe_float(temp, 15.0)
        base['temp_min'] = tmin
        base['temp_max'] = tmax
        base['temperature_p10'] = p10
        base['temperature_p50'] = p50
        base['temperature_p90'] = p90
        base['humidity'] = self._safe_float(entry.get('humidity'))
        base['aqi'] = self._safe_float(entry.get('aqi'))
        base['pm25'] = self._safe_float(entry.get('pm25'))
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
        base['predictability_score'] = self._safe_float(entry.get('predictability_score'), None)
        base['source'] = str(entry.get('data_source') or '')
        return base

    def _composite_exposure_risk(
        self,
        temperature,
        temp_min,
        humidity,
        pm25=None,
        aqi=None,
        *,
        temp_min_fallback=None,
        pm25_origin=None,
        aqi_origin=None,
    ):
        """
        复合暴露风险：热 + PM2.5 + 湿度 + 热夜（学术实践增强版简化实现）。
        输出 0-100 及分项贡献。
        """
        temp_input = self._safe_float(temperature, None)
        temp_imputed = temp_input is None
        temp = 20.0 if temp_imputed else temp_input

        tmin_input = self._safe_float(temp_min, None)
        temp_min_imputed = tmin_input is None
        if temp_min_imputed:
            fallback_value = self._safe_float(temp_min_fallback, None)
            if fallback_value is None:
                tmin = temp - 4.0
                temp_min_source = 'temperature_minus_4'
            else:
                tmin = fallback_value
                temp_min_source = 'temperature_uncertainty_lower'
        else:
            tmin = tmin_input
            temp_min_source = 'direct'

        humidity_input = self._safe_float(humidity, None)
        humidity_imputed = humidity_input is None
        hum = 60.0 if humidity_imputed else humidity_input

        pm = self._safe_float(pm25, None)
        aqi_used = None
        aqi_imputed = False
        if pm is None:
            aqi_input = self._safe_float(aqi, None)
            aqi_imputed = aqi_input is None
            aqi_v = 50.0 if aqi_imputed else aqi_input
            # AQI 到 PM2.5 的保守近似（用于无PM预报时）
            pm = max(5.0, min(220.0, aqi_v * 0.65))
            if aqi_imputed:
                pm25_source = 'default_aqi_50'
                pm25_detail_source = 'default_aqi_50'
            elif aqi_origin == 'current_weather_context':
                pm25_source = 'current_observation_aqi_proxy'
                pm25_detail_source = 'current_weather_context'
            else:
                pm25_source = 'aqi_proxy'
                pm25_detail_source = 'day_aqi_input'
            aqi_used = aqi_v
        elif pm25_origin == 'current_weather_context':
            # 未来日没有污染物预报时复用当前实况，必须与未来日直接预报区分。
            pm25_source = 'current_observation_reuse'
            pm25_detail_source = 'current_weather_context'
        else:
            pm25_source = 'direct'
            pm25_detail_source = pm25_origin or 'forecast_input'

        heat_score = float(np.clip((temp - 28.0) * 6.0, 0.0, 100.0))
        pollution_score = float(np.clip((pm - 35.0) * 1.8, 0.0, 100.0))
        humidity_score = float(np.clip((hum - 70.0) * 2.4, 0.0, 100.0))
        hot_night_score = 100.0 if tmin >= 26 else 72.0 if tmin >= 24 else 45.0 if tmin >= 22 else 8.0

        synergy_bonus = 0.0
        if heat_score >= 45 and pollution_score >= 40:
            synergy_bonus += 8.0
        if heat_score >= 45 and humidity_score >= 40:
            synergy_bonus += 6.0
        if hot_night_score >= 70 and pollution_score >= 35:
            synergy_bonus += 4.0

        pre_clip_score = (
            0.34 * heat_score
            + 0.28 * pollution_score
            + 0.18 * humidity_score
            + 0.20 * hot_night_score
            + synergy_bonus
        )
        final_score = float(np.clip(pre_clip_score, 0.0, 100.0))
        if final_score >= 70:
            level = '高'
        elif final_score >= 45:
            level = '中'
        else:
            level = '低'

        return {
            # score 保留为兼容字段，final_score 明确表示经过 0-100 限幅后的结果。
            'score': round(final_score, 1),
            'pre_clip_score': round(pre_clip_score, 1),
            'final_score': round(final_score, 1),
            'synergy_bonus': round(synergy_bonus, 1),
            'level': level,
            'components': {
                'heat': round(heat_score, 1),
                'pm25': round(pollution_score, 1),
                'humidity': round(humidity_score, 1),
                'hot_night': round(hot_night_score, 1)
            },
            'hot_night': bool(tmin >= 22),
            # pm25_proxy 保留旧接口语义；来源请以 pm25_source 为准。
            'pm25_proxy': round(pm, 1),
            'pm25_source': pm25_source,
            'inputs': {
                'temperature': {
                    'used_value': round(temp, 1),
                    'imputed': temp_imputed,
                    'source': 'default_20' if temp_imputed else 'corrected_forecast',
                },
                'temp_min': {
                    'used_value': round(tmin, 1),
                    'imputed': temp_min_imputed,
                    'source': temp_min_source,
                },
                'humidity': {
                    'used_value': round(hum, 1),
                    'imputed': humidity_imputed,
                    'source': 'default_60' if humidity_imputed else 'forecast_input',
                },
                'pm25': {
                    'used_value': round(pm, 1),
                    'imputed': pm25_source != 'direct',
                    'source': pm25_source,
                    'detail_source': pm25_detail_source,
                    'aqi_used': round(aqi_used, 1) if aqi_used is not None else None,
                    'aqi_imputed': aqi_imputed,
                },
            },
        }

    def _cap_semantics_for_forecast(self, prob_high_percent, composite_level):
        """将日级概率映射为 CAP 风格语义。"""
        prob = self._safe_float(prob_high_percent, 0.0) or 0.0
        if prob >= 65 or composite_level == '高':
            severity = 'severe'
        elif prob >= 35:
            severity = 'moderate'
        else:
            severity = 'minor'

        if prob >= 60:
            certainty = 'likely'
        elif prob >= 30:
            certainty = 'possible'
        else:
            certainty = 'unlikely'

        if severity == 'severe' and certainty == 'likely':
            urgency = 'immediate'
        elif severity in ('severe', 'moderate'):
            urgency = 'expected'
        else:
            urgency = 'future'

        return {
            'severity': severity,
            'certainty': certainty,
            'urgency': urgency
        }

    def _build_role_action_cards(self, forecasts, summary):
        """按角色输出行动卡：居民 / 村医 / 社区干部。"""
        high_days = [row for row in forecasts if (self._safe_float(row.get('probability_high_visits'), 0.0) or 0.0) >= 50]
        composite_high_days = [row for row in forecasts if (row.get('composite_exposure') or {}).get('level') == '高']
        scenario = summary.get('scenario_totals') or {}
        baseline_total = self._safe_float(scenario.get('baseline_total'), 0.0) or 0.0
        worst_total = self._safe_float(scenario.get('worst_case_total'), baseline_total) or baseline_total
        extra = max(0.0, worst_total - baseline_total)

        resident_cards = [
            {
                'priority': 'high' if high_days else 'medium',
                'title': '居民日常行动',
                'action': '根据预警概率调整外出时段，优先早晚活动，午后减少户外暴露。'
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
                'priority': 'high' if high_days else 'medium',
                'title': '村医排班准备',
                'action': f'未来7天最坏情景较基线多约 {round(extra, 1)} 人次，建议提前安排门急诊与随访。'
            }
        ]
        if any((row.get('cap_semantics') or {}).get('urgency') == 'immediate' for row in forecasts):
            doctor_cards.append({
                'priority': 'high',
                'title': '高危人群追踪',
                'action': '对老年慢病与近期不适人群进行电话回访，必要时上门复核。'
            })

        community_cards = [
            {
                'priority': 'high' if len(high_days) >= 2 else 'medium',
                'title': '社区资源调度',
                'action': '根据高风险日分布，动态调整避暑点开放时段和宣传频次。'
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

    def _calculate_predictability(self, lead_day, model_spread=None, model_count=1, external_score=None):
        """
        计算可预报性分数（0-100）并分级。
        - 模型离散度越大，分数越低
        - 提前期越长，分数越低
        - 模型成员数越多，分数略有提升（信息增益）
        """
        spread = max(0.0, float(model_spread)) if model_spread is not None else 0.0
        lead_penalty = max(0, int(lead_day) - 1) * 3.0
        model_bonus = min(8.0, max(0, int(model_count) - 1) * 2.0)
        if external_score is not None:
            branch = 'external'
            raw_score = float(external_score)
            score = max(0.0, min(100.0, raw_score))
        else:
            branch = 'derived'
            raw_score = 100.0 - spread * 16.0 - lead_penalty + model_bonus
            score = max(5.0, min(99.0, raw_score))

        if score >= 75:
            label = '高'
        elif score >= 50:
            label = '中'
        else:
            label = '低'
        return {
            'score': round(score, 1),
            'label': label,
            'branch': branch,
            'raw_score': round(raw_score, 1),
            'inputs': {
                'external_score': round(float(external_score), 1) if external_score is not None else None,
                'lead_day': int(lead_day),
                'model_spread': round(spread, 3),
                'model_count': max(1, int(model_count)),
                # 外部分支不会应用下面两个调整，保留输入便于解释分支差异。
                'lead_penalty': round(lead_penalty, 1) if branch == 'derived' else None,
                'model_bonus': round(model_bonus, 1) if branch == 'derived' else None,
            },
        }

    def _build_impact_likelihood_matrix(self, forecasts):
        """
        影响×可能性矩阵（类似英气象部门 impact-likelihood 风格）。
        返回 3x3 计数，供前端可视化。
        """
        matrix = {
            'high': {'high': 0, 'medium': 0, 'low': 0},
            'medium': {'high': 0, 'medium': 0, 'low': 0},
            'low': {'high': 0, 'medium': 0, 'low': 0}
        }
        for item in forecasts or []:
            visits = (item.get('visits') or {})
            point_estimate = self._safe_float(visits.get('point_estimate'), 0.0) or 0.0
            baseline = self._safe_float(visits.get('baseline'), self.visit_mean or 1.0) or 1.0
            ratio = point_estimate / baseline if baseline > 0 else 1.0

            if ratio >= 1.4:
                impact = 'high'
            elif ratio >= 1.1:
                impact = 'medium'
            else:
                impact = 'low'

            prob = self._safe_float(item.get('probability_high_visits'), 0.0) or 0.0
            if prob >= 50:
                likelihood = 'high'
            elif prob >= 20:
                likelihood = 'medium'
            else:
                likelihood = 'low'

            matrix[impact][likelihood] += 1

        return {
            'impact_levels': ['low', 'medium', 'high'],
            'likelihood_levels': ['low', 'medium', 'high'],
            'cells': matrix
        }

    def quantile_mapping(self, forecast_temp, lead_day=1, model_spread=None):
        """
        Quantile Mapping后处理
        
        将预报温度校正到"像观测"的分布
        
        参数:
        - forecast_temp: 预报温度
        - lead_day: 预报提前天数（1-7）
        
        返回:
        - corrected_temp: 校正后的温度
        - uncertainty: 不确定性范围
        """
        forecast_temp = self._safe_float(forecast_temp)
        if forecast_temp is None:
            raise ValueError("forecast temperature must be finite")

        if not self.qm_params:
            spread = self._safe_float(model_spread, 0.0) or 0.0
            width = 2.0 + min(3.0, spread * 0.6)
            return forecast_temp, {
                'lower': forecast_temp - width,
                'upper': forecast_temp + width,
                'std': width / 1.96,
                'lead_day': lead_day,
                'original_temp': forecast_temp,
                'bias_correction': 0.0,
                'model_spread': spread
            }
        
        # 计算预报温度在分布中的分位数
        forecast_percentile = stats.percentileofscore(
            self.qm_params['temp_values'], 
            forecast_temp
        )
        
        # 应用偏差校正（根据lead_day增加不确定性）
        lead_bias = 0.5 * (lead_day - 1)  # 预报越远偏差越大
        corrected_temp = forecast_temp - lead_bias
        
        # 确保在历史范围内
        corrected_temp = max(self.qm_params['min'] - 5, 
                            min(self.qm_params['max'] + 5, corrected_temp))
        
        # 计算不确定性范围（随lead_day增加）
        base_uncertainty = 1.5
        uncertainty_factor = 1 + 0.3 * (lead_day - 1)
        model_spread_v = self._safe_float(model_spread, 0.0) or 0.0
        spread_uncertainty = min(4.0, model_spread_v * 0.6)
        uncertainty = base_uncertainty * uncertainty_factor + spread_uncertainty
        
        return corrected_temp, {
            'lower': corrected_temp - uncertainty,
            'upper': corrected_temp + uncertainty,
            'std': uncertainty / 1.96,  # 95%置信区间
            'lead_day': lead_day,
            'original_temp': forecast_temp,
            'bias_correction': lead_bias,
            'model_spread': model_spread_v
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
            
            # 如果都没有，使用气候态平均值
            if self.qm_params and 'mean' in self.qm_params:
                climatology_temp = self._safe_float(self.qm_params['mean'])
                if climatology_temp is not None:
                    lag_profile.append(climatology_temp)
                    data_sources.append('climatology')
                else:
                    lag_profile.append(15.0)
                    data_sources.append('default')
            else:
                lag_profile.append(15.0)  # 默认值
                data_sources.append('default')
        
        return lag_profile, data_sources
    
    def predict_daily_visits(self, temperature, lag_temps=None, month=None, dow=None):
        """
        预测日门诊量
        
        参数:
        - temperature: 当天温度
        - lag_temps: 过去7天温度
        - month: 月份
        - dow: 星期几（0-6）
        
        返回:
        - point_estimate: 点预测
        - interval: 预测区间
        - probability_high: 超阈值概率
        """
        from services.dlnm_risk_service import get_dlnm_service
        
        dlnm = get_dlnm_service()
        
        # 获取相对风险
        rr, breakdown = dlnm.calculate_rr(temperature, lag_temps)
        
        # 基础门诊量（考虑季节性）
        if month and month in dlnm.seasonal_baseline:
            baseline = dlnm.seasonal_baseline[month]['avg_visits']
        else:
            baseline = self.visit_mean
        
        # 星期效应
        dow_factor = 1.0
        if dow is not None:
            # 周末门诊量通常较低
            if dow in [5, 6]:
                dow_factor = 0.7
            elif dow == 0:  # 周一略高
                dow_factor = 1.1
        
        # 点预测。保留限幅前数值，概率计算始终使用这一原始均值。
        point_estimate = baseline * rr * dow_factor
        raw_point_estimate = float(point_estimate)
        
        # 预测区间（基于Negative Binomial分布的不确定性）
        # 使用过度离散参数 theta
        theta = 2.0  # 可调整
        std_estimate = np.sqrt(point_estimate + point_estimate**2 / theta)
        
        lower_bound = max(0, point_estimate - 1.96 * std_estimate)
        upper_bound = point_estimate + 1.96 * std_estimate
        
        # 超阈值概率 P(Y >= P90)
        visit_threshold_p90 = self._safe_float(self.visit_threshold_p90, None)
        if visit_threshold_p90 is not None and visit_threshold_p90 > 0:
            # 使用正态近似
            z = (visit_threshold_p90 - raw_point_estimate) / std_estimate if std_estimate > 0 else 0
            prob_high = 1 - stats.norm.cdf(z)
            probability_method = 'normal_approximation'
        else:
            prob_high = 0.1
            probability_method = 'fallback_0.1'
        
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
            'probability_exceed_p90': round(prob_high, 3),
            'probability_exceed_p75': round(min(1, prob_high * 1.5), 3),
            'rr': round(rr, 3),
            'baseline': round(baseline, 1),
            'dow_factor': round(dow_factor, 3),
            'raw_point_estimate': round(raw_point_estimate, 4),
            'visit_threshold_p90': round(visit_threshold_p90, 4) if visit_threshold_p90 is not None else None,
            'std_estimate': round(float(std_estimate), 4),
            'probability_method': probability_method,
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
        high_risk_days = 0
        predictability_scores = []
        model_sources = set()
        total_worst_case_visits = 0.0
        total_optimistic_visits = 0.0
        composite_scores = []
        composite_high_days = 0

        # 获取温度列表用于备选
        temp_values = [entry.get('temp', 15.0) for entry in forecast_temps_dict.values()]
        context = context or {}
        context_aqi = self._safe_float(context.get('aqi'))
        context_pm25 = self._safe_float(context.get('pm25'))
        
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
            raw_temp = selected_entry.get('temp', 15.0)
            model_spread = selected_entry.get('model_spread')
            model_count = selected_entry.get('model_count', 1)
            model_names = selected_entry.get('model_names', []) or []
            humidity = selected_entry.get('humidity')
            temp_min = selected_entry.get('temp_min')
            pm25 = selected_entry.get('pm25')
            pm25_origin = 'forecast_input' if pm25 is not None else None
            aqi = selected_entry.get('aqi')
            aqi_origin = 'forecast_input' if aqi is not None else None
            if pm25 is None:
                pm25 = context_pm25
                if pm25 is not None:
                    pm25_origin = 'current_weather_context'
            if pm25 is None and aqi is None and context_aqi is not None:
                aqi = context_aqi
                aqi_origin = 'current_weather_context'
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
                d: (e.get('temp', 15.0) if isinstance(e, dict) else float(e))
                for d, e in forecast_temps_dict.items()
                if d < target_date
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
            
            # 确定风险等级
            prob_high = prediction['probability_exceed_p90']
            if prob_high > 0.5:
                risk_level = '红色预警'
                risk_color = 'danger'
            elif prob_high > 0.3:
                risk_level = '橙色预警'
                risk_color = 'warning'
            elif prob_high > 0.15:
                risk_level = '黄色提醒'
                risk_color = 'info'
            else:
                risk_level = '正常'
                risk_color = 'success'
            
            if prob_high > 0.3:
                high_risk_days += 1
            
            # 识别极端天气
            from services.dlnm_risk_service import get_dlnm_service
            dlnm = get_dlnm_service()
            extreme_events = dlnm.identify_extreme_weather_events(corrected_temp)

            predictability = self._calculate_predictability(
                lead_day=lead_day,
                model_spread=model_spread,
                model_count=model_count,
                external_score=selected_entry.get('predictability_score')
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
            )
            composite_scores.append(self._safe_float(composite_exposure.get('score'), 0.0) or 0.0)
            if composite_exposure.get('level') == '高':
                composite_high_days += 1

            cap_semantics = self._cap_semantics_for_forecast(
                prob_high_percent=prob_high * 100.0,
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
                    'input_spread': round(uncertainty.get('model_spread', 0.0), 3),
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
                'probability_high_visits': round(prob_high * 100, 1),
                
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
        
        # 生成建议
        recommendations = self._generate_forecast_recommendations(forecasts, high_risk_days)
        avg_predictability = round(sum(predictability_scores) / len(predictability_scores), 1) if predictability_scores else None
        low_predictability_days = sum(1 for s in predictability_scores if s < 50)
        
        summary = {
            'forecast_period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': (start_date + timedelta(days=6)).strftime('%Y-%m-%d')
            },
            'total_expected_visits': round(total_expected_visits, 0),
            'high_risk_days': high_risk_days,
            'average_daily_visits': round(total_expected_visits / 7, 1),
            'overall_risk': 'high' if high_risk_days >= 3 else 'medium' if high_risk_days >= 1 else 'low',
            'recommendations': recommendations,
            'scenario_totals': {
                'optimistic_total': round(total_optimistic_visits, 1),
                'baseline_total': round(total_expected_visits, 1),
                'worst_case_total': round(total_worst_case_visits, 1),
                'worst_case_extra': round(max(0.0, total_worst_case_visits - total_expected_visits), 1)
            },
            'probability_products': {
                'days_prob_exceed_p90_ge50': int(sum(1 for f in forecasts if (self._safe_float(f.get('probability_high_visits'), 0.0) or 0.0) >= 50.0)),
                'days_prob_exceed_p90_ge30': int(sum(1 for f in forecasts if (self._safe_float(f.get('probability_high_visits'), 0.0) or 0.0) >= 30.0)),
                'days_prob_exceed_p75_ge50': int(sum(1 for f in forecasts if (self._safe_float((f.get('visits') or {}).get('probability_exceed_p75'), 0.0) or 0.0) * 100.0 >= 50.0))
            },
            'predictability': {
                'average_score': avg_predictability,
                'low_predictability_days': low_predictability_days
            },
            'composite_exposure': {
                'average_score': round(float(np.mean(composite_scores)) if composite_scores else 0.0, 1),
                'high_risk_days': composite_high_days
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
        
        # 分析高风险天数
        if high_risk_days >= 3:
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
                        'category': '极端天气',
                        'advice': f"{day['date']}: {event['description']}"
                    })
        
        # 温度趋势分析
        temps = [f['temperature']['corrected'] for f in forecasts]
        if max(temps) - min(temps) > 10:
            recommendations.append({
                'priority': 'medium',
                'category': '温差预警',
                'advice': f'未来一周温差较大({min(temps):.0f}°C ~ {max(temps):.0f}°C)，注意防范温度骤变影响'
            })
        
        # 周末高峰预警
        weekend_high = [f for f in forecasts if f['day_of_week'] in ['周六', '周日'] and f['risk_level'] in ['红色预警', '橙色预警']]
        if weekend_high:
            recommendations.append({
                'priority': 'medium',
                'category': '周末安排',
                'advice': '周末预计有就诊高峰，建议安排值班人员'
            })
        
        if not recommendations:
            recommendations.append({
                'priority': 'low',
                'category': '常规管理',
                'advice': '未来一周天气和就诊量预计正常，保持常规医疗资源配置'
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

