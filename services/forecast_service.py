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
from collections import defaultdict
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
    
    def quantile_mapping(self, forecast_temp, lead_day=1):
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
        if not self.qm_params:
            return forecast_temp, {'lower': forecast_temp - 2, 'upper': forecast_temp + 2}
        
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
        uncertainty = base_uncertainty * uncertainty_factor
        
        return corrected_temp, {
            'lower': corrected_temp - uncertainty,
            'upper': corrected_temp + uncertainty,
            'std': uncertainty / 1.96,  # 95%置信区间
            'lead_day': lead_day,
            'original_temp': forecast_temp,
            'bias_correction': lead_bias
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
                        if pd.notna(temp):
                            lag_profile.append(float(temp))
                            data_sources.append('observation')
                            continue
                except Exception:
                    pass
            
            # 尝试从预报获取
            if normalized_forecast and check_date_only in normalized_forecast:
                lag_profile.append(float(normalized_forecast[check_date_only]))
                data_sources.append('forecast')
                continue
            
            # 如果都没有，使用气候态平均值
            if self.qm_params and 'mean' in self.qm_params:
                lag_profile.append(float(self.qm_params['mean']))
                data_sources.append('climatology')
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
        
        # 点预测
        point_estimate = baseline * rr * dow_factor
        
        # 预测区间（基于Negative Binomial分布的不确定性）
        # 使用过度离散参数 theta
        theta = 2.0  # 可调整
        std_estimate = np.sqrt(point_estimate + point_estimate**2 / theta)
        
        lower_bound = max(0, point_estimate - 1.96 * std_estimate)
        upper_bound = point_estimate + 1.96 * std_estimate
        
        # 超阈值概率 P(Y >= P90)
        if self.visit_threshold_p90:
            # 使用正态近似
            z = (self.visit_threshold_p90 - point_estimate) / std_estimate if std_estimate > 0 else 0
            prob_high = 1 - stats.norm.cdf(z)
        else:
            prob_high = 0.1
        
        return {
            'point_estimate': round(point_estimate, 1),
            'lower_bound': round(lower_bound, 1),
            'upper_bound': round(upper_bound, 1),
            'p10': round(max(0, point_estimate - 1.28 * std_estimate), 1),
            'p50': round(point_estimate, 1),
            'p90': round(point_estimate + 1.28 * std_estimate, 1),
            'probability_exceed_p90': round(prob_high, 3),
            'probability_exceed_p75': round(min(1, prob_high * 1.5), 3),
            'rr': round(rr, 3),
            'baseline': round(baseline, 1),
            'temperature': temperature
        }
    
    def generate_7day_forecast(self, forecast_temps, start_date=None):
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
        
        # 转换预报温度格式为统一的 date -> temp 字典
        if isinstance(forecast_temps, list):
            forecast_temps_dict = {
                (start_date + timedelta(days=i)): float(temp) 
                for i, temp in enumerate(forecast_temps)
            }
        else:
            # 标准化键为 date 对象
            forecast_temps_dict = {}
            for k, v in forecast_temps.items():
                if hasattr(k, 'date') and callable(k.date):
                    key = k.date()
                elif isinstance(k, str):
                    key = pd.to_datetime(k).date()
                else:
                    key = k
                forecast_temps_dict[key] = float(v) if v is not None else 15.0
        
        forecasts = []
        total_expected_visits = 0
        high_risk_days = 0
        
        # 获取温度列表用于备选
        temp_values = list(forecast_temps_dict.values())
        
        for lead_day in range(1, 8):
            target_date = start_date + timedelta(days=lead_day - 1)
            
            # 获取预报温度
            if target_date in forecast_temps_dict:
                raw_temp = forecast_temps_dict[target_date]
            elif lead_day <= len(temp_values):
                raw_temp = temp_values[lead_day - 1]
            else:
                raw_temp = 15.0
            
            # 后处理校正
            corrected_temp, uncertainty = self.quantile_mapping(raw_temp, lead_day)
            
            # 获取滞后温度profile
            lag_temps, sources = self.get_lag_temperature_profile(
                target_date, 
                forecast_temps={d: t for d, t in forecast_temps_dict.items() if d < target_date}
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
            
            forecast = {
                'date': target_date.strftime('%Y-%m-%d'),
                'lead_day': lead_day,
                'day_of_week': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][dow],
                
                # 温度信息
                'temperature': {
                    'forecast': round(raw_temp, 1),
                    'corrected': round(corrected_temp, 1),
                    'uncertainty_lower': round(uncertainty['lower'], 1),
                    'uncertainty_upper': round(uncertainty['upper'], 1)
                },
                
                # 门诊量预测
                'visits': prediction,
                
                # 风险信息
                'risk_level': risk_level,
                'risk_color': risk_color,
                'probability_high_visits': round(prob_high * 100, 1),
                
                # 极端天气
                'extreme_events': extreme_events,
                
                # 置信度（随lead_day降低）
                'confidence': 'high' if lead_day <= 2 else 'medium' if lead_day <= 4 else 'low'
            }
            
            forecasts.append(forecast)
            total_expected_visits += prediction['point_estimate']
        
        # 生成建议
        recommendations = self._generate_forecast_recommendations(forecasts, high_risk_days)
        
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
            'generated_at': now_local().strftime('%Y-%m-%d %H:%M:%S')
        }
        
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

