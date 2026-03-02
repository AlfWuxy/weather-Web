# -*- coding: utf-8 -*-
"""
模块二：健康风险评估服务
功能：社区健康档案管理、社区脆弱性指数计算、空间风险与地图生成
"""
import json
from datetime import datetime
import math

class HealthRiskService:
    """健康风险评估服务类"""
    
    def __init__(self):
        pass
    
    def calculate_community_vulnerability_index(self, community_data):
        """
        计算社区脆弱性指数
        考虑因素：
        1. 人口老龄化程度
        2. 慢性病患病率
        3. 医疗资源可达性
        4. 社区环境质量
        """
        # 老龄化指数 (权重: 0.3)
        elderly_ratio = community_data.get('elderly_ratio', 0)
        aging_score = min(elderly_ratio * 100, 100) * 0.3
        
        # 慢性病患病率 (权重: 0.35)
        chronic_disease_ratio = community_data.get('chronic_disease_ratio', 0)
        disease_score = min(chronic_disease_ratio * 100, 100) * 0.35
        
        # 医疗资源可达性 (权重: 0.2) - 值越大越好，需要反转
        medical_accessibility = community_data.get('medical_accessibility', 50)
        medical_score = (100 - medical_accessibility) * 0.2
        
        # 环境质量 (权重: 0.15) - 基于空气质量等
        env_quality = community_data.get('env_quality_score', 50)
        env_score = (100 - env_quality) * 0.15
        
        # 综合脆弱性指数 (0-100)
        vulnerability_index = aging_score + disease_score + medical_score + env_score
        
        # 确定风险等级
        if vulnerability_index < 30:
            risk_level = '低'
        elif vulnerability_index < 60:
            risk_level = '中'
        else:
            risk_level = '高'
        
        return {
            'vulnerability_index': round(vulnerability_index, 2),
            'risk_level': risk_level,
            'breakdown': {
                'aging_score': round(aging_score, 2),
                'disease_score': round(disease_score, 2),
                'medical_score': round(medical_score, 2),
                'env_score': round(env_score, 2)
            }
        }
    
    def assess_user_risk(self, user_id):
        """
        评估用户健康风险
        注意：此方法已废弃，请直接在 app.py 中调用相关服务
        """
        return None
    
    def _analyze_disease_risks(self, user_profile, weather_data, community_risk):
        """分析各类疾病风险"""
        risks = {}
        
        # 呼吸道疾病风险
        respiratory_risk = 30  # 基础风险
        if weather_data.get('aqi', 0) > 100:
            respiratory_risk += 30
        if weather_data.get('temperature', 20) < 10:
            respiratory_risk += 20
        if '呼吸' in str(user_profile.get('chronic_diseases', [])):
            respiratory_risk += 20
        risks['呼吸道疾病'] = min(respiratory_risk, 100)
        
        # 心血管疾病风险
        cardiovascular_risk = 25
        temp = weather_data.get('temperature', 20)
        if temp > 35 or temp < 0:
            cardiovascular_risk += 35
        if user_profile.get('age', 0) > 60:
            cardiovascular_risk += 20
        if '心血管' in str(user_profile.get('chronic_diseases', [])) or '高血压' in str(user_profile.get('chronic_diseases', [])):
            cardiovascular_risk += 20
        risks['心血管疾病'] = min(cardiovascular_risk, 100)
        
        # 关节疾病风险
        joint_risk = 20
        if weather_data.get('humidity', 0) > 80:
            joint_risk += 25
        if weather_data.get('temperature', 20) < 15:
            joint_risk += 15
        if '关节' in str(user_profile.get('chronic_diseases', [])):
            joint_risk += 30
        risks['关节疾病'] = min(joint_risk, 100)
        
        # 消化系统疾病风险
        digestive_risk = 15
        if weather_data.get('temperature', 20) > 30:
            digestive_risk += 20
        risks['消化系统疾病'] = min(digestive_risk, 100)
        
        return risks
    
    def _generate_health_recommendations(self, user_profile, weather_data, disease_risks):
        """生成健康建议"""
        recommendations = []
        
        # 基于年龄的建议
        if user_profile.get('age', 0) > 65:
            recommendations.append({
                'category': '老年人健康',
                'advice': '建议减少户外活动时间，注意保暖或防暑'
            })
        
        # 基于天气的建议
        aqi = weather_data.get('aqi', 0)
        if aqi > 150:
            recommendations.append({
                'category': '空气质量',
                'advice': '空气质量较差，建议佩戴口罩，减少户外活动'
            })
        
        temp = weather_data.get('temperature', 20)
        if temp > 35:
            recommendations.append({
                'category': '高温预防',
                'advice': '高温天气，注意防暑降温，多饮水，避免中暑'
            })
        elif temp < 5:
            recommendations.append({
                'category': '低温预防',
                'advice': '低温天气，注意保暖，预防感冒和冻伤'
            })
        
        # 基于疾病风险的建议
        for disease, risk in disease_risks.items():
            if risk > 60:
                if disease == '呼吸道疾病':
                    recommendations.append({
                        'category': '呼吸系统健康',
                        'advice': '呼吸道疾病风险较高，建议减少外出，注意室内通风'
                    })
                elif disease == '心血管疾病':
                    recommendations.append({
                        'category': '心血管健康',
                        'advice': '心血管疾病风险较高，避免剧烈运动，按时服药'
                    })
                elif disease == '关节疾病':
                    recommendations.append({
                        'category': '关节健康',
                        'advice': '关节疾病风险较高，注意关节保暖，适当活动'
                    })
        
        # 基于慢性病的建议
        chronic_diseases = user_profile.get('chronic_diseases', [])
        if chronic_diseases:
            recommendations.append({
                'category': '慢性病管理',
                'advice': '定期监测健康指标，按时服药，如有不适及时就医'
            })
        
        return recommendations
    
    def _get_risk_level(self, risk_score):
        """根据风险评分确定风险等级"""
        if risk_score < 30:
            return '低风险'
        elif risk_score < 60:
            return '中风险'
        else:
            return '高风险'
    
    def generate_community_risk_map_data(self):
        """生成社区风险地图数据
        注意：此方法已废弃，请直接在 app.py 中调用
        """
        return []

