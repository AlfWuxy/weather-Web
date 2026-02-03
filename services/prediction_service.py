# -*- coding: utf-8 -*-
"""
疾病预测服务 - 基于历史数据预测未来患病趋势

⚠️ 废弃警告 (DEPRECATED)
========================
此模块未被 app.py 引用，功能已被其他服务替代。
计划在后续版本中删除。如有依赖请迁移到其他服务。

废弃时间: 2025-01
"""
from datetime import datetime, timedelta
from collections import defaultdict
import math

class PredictionService:
    """疾病预测服务"""
    
    def __init__(self):
        pass
    
    def predict_future_cases(self, historical_data, months_ahead=3):
        """
        基于历史数据预测未来月份的患病情况
        使用简单移动平均和季节性因子
        
        参数:
        - historical_data: 历史月度病例数据 {年月: 数量}
        - months_ahead: 预测未来几个月
        
        返回:
        - 预测结果列表
        """
        if not historical_data or len(historical_data) < 3:
            return []
        
        # 计算移动平均（最近3个月）
        sorted_data = sorted(historical_data.items(), key=lambda x: x[0])
        recent_3_months = sorted_data[-3:]
        avg_cases = sum(item[1] for item in recent_3_months) / 3
        
        # 计算趋势（简单线性趋势）
        if len(sorted_data) >= 6:
            recent_6 = sorted_data[-6:]
            first_3_avg = sum(item[1] for item in recent_6[:3]) / 3
            last_3_avg = sum(item[1] for item in recent_6[3:]) / 3
            trend = (last_3_avg - first_3_avg) / 3  # 每月变化
        else:
            trend = 0
        
        # 计算季节性因子
        monthly_pattern = self._calculate_seasonal_pattern(sorted_data)
        
        # 生成预测
        predictions = []
        last_month = sorted_data[-1][0]
        last_year, last_month_num = map(int, last_month.split('-'))
        
        for i in range(1, months_ahead + 1):
            # 计算下一个月份
            next_month_num = last_month_num + i
            next_year = last_year
            
            while next_month_num > 12:
                next_month_num -= 12
                next_year += 1
            
            next_month_str = f"{next_year}-{next_month_num:02d}"
            
            # 预测值 = 平均值 + 趋势 × 月数 + 季节性调整
            seasonal_factor = monthly_pattern.get(next_month_num, 1.0)
            predicted_value = (avg_cases + trend * i) * seasonal_factor
            predicted_value = max(0, int(predicted_value))  # 不能为负
            
            predictions.append({
                'month': next_month_str,
                'predicted_cases': predicted_value,
                'confidence': 'high' if i <= 2 else 'medium' if i <= 4 else 'low'
            })
        
        return predictions
    
    def _calculate_seasonal_pattern(self, historical_data):
        """计算季节性模式"""
        monthly_avg = defaultdict(list)
        
        for month_str, count in historical_data:
            month_num = int(month_str.split('-')[1])
            monthly_avg[month_num].append(count)
        
        # 计算每个月的平均值
        overall_avg = sum(count for _, count in historical_data) / len(historical_data)
        
        seasonal_factors = {}
        for month, values in monthly_avg.items():
            month_avg = sum(values) / len(values)
            seasonal_factors[month] = month_avg / overall_avg if overall_avg > 0 else 1.0
        
        return seasonal_factors
    
    def predict_disease_outbreak_risk(self, weather_forecast, community_data, historical_disease_data):
        """
        预测疾病爆发风险
        
        参数:
        - weather_forecast: 未来天气预报
        - community_data: 社区信息
        - historical_disease_data: 历史疾病数据
        
        返回:
        - 风险评估和预警
        """
        risk_factors = []
        risk_score = 0
        
        # 分析天气风险
        if weather_forecast.get('temperature_max', 0) > 35:
            risk_factors.append('预计高温天气，中暑和心血管疾病风险增加')
            risk_score += 30
        
        if weather_forecast.get('aqi', 0) > 150:
            risk_factors.append('预计空气质量差，呼吸道疾病风险显著增加')
            risk_score += 40
        
        # 分析社区脆弱性
        if community_data.get('elderly_ratio', 0) > 0.2:
            risk_factors.append('社区老年人比例较高，需特别关注')
            risk_score += 20
        
        if community_data.get('chronic_disease_ratio', 0) > 0.25:
            risk_factors.append('社区慢性病患者较多，健康风险增加')
            risk_score += 20
        
        # 确定风险等级
        if risk_score > 70:
            risk_level = '高风险'
            color = 'danger'
        elif risk_score > 40:
            risk_level = '中风险'
            color = 'warning'
        else:
            risk_level = '低风险'
            color = 'success'
        
        return {
            'risk_score': risk_score,
            'risk_level': risk_level,
            'color': color,
            'risk_factors': risk_factors,
            'recommendations': self._generate_outbreak_recommendations(risk_factors)
        }
    
    def _generate_outbreak_recommendations(self, risk_factors):
        """生成预防建议"""
        recommendations = []
        
        if any('高温' in factor for factor in risk_factors):
            recommendations.append('社区应设立防暑降温点')
            recommendations.append('提醒老年人避免高温时段外出')
        
        if any('空气' in factor for factor in risk_factors):
            recommendations.append('建议社区居民减少户外活动')
            recommendations.append('呼吸道疾病患者应备好药物')
        
        if any('老年人' in factor for factor in risk_factors):
            recommendations.append('加强对老年人的健康监测')
            recommendations.append('社区医疗站应做好应急准备')
        
        if any('慢性病' in factor for factor in risk_factors):
            recommendations.append('提醒慢性病患者按时服药')
            recommendations.append('建议进行健康宣教活动')
        
        return recommendations








