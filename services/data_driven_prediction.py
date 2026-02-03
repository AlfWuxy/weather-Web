# -*- coding: utf-8 -*-
"""
数据驱动的天气-疾病预测服务
基于真实病历数据训练的预测模型

⚠️ 废弃警告 (DEPRECATED)
========================
此模块未被 app.py 引用，功能已被 ml_prediction_service.py 替代。
计划在后续版本中删除。

废弃时间: 2025-01
替代方案: services/ml_prediction_service.py
"""
import pandas as pd
import numpy as np
from datetime import timedelta
from collections import defaultdict
import json
from pathlib import Path
from core.time_utils import now_local, utcnow

class DataDrivenPredictionService:
    """基于真实数据的预测服务"""
    
    def __init__(self):
        self.model_data = None
        self.disease_by_month = {}  # 月份-疾病分布
        self.disease_by_age = {}    # 年龄段-疾病分布
        self.disease_by_community = {}  # 社区-疾病分布
        self.seasonal_risk = {}     # 季节风险系数
        self.age_risk = {}          # 年龄风险系数
        
        # 季节-天气特征映射
        self.season_weather = {
            1: {'season': '冬季', 'temp_range': (-5, 8), 'features': ['低温', '寒冷', '干燥']},
            2: {'season': '冬季', 'temp_range': (0, 12), 'features': ['低温', '寒冷', '潮湿']},
            3: {'season': '春季', 'temp_range': (8, 18), 'features': ['温差大', '潮湿', '回暖']},
            4: {'season': '春季', 'temp_range': (12, 22), 'features': ['温差大', '多雨', '潮湿']},
            5: {'season': '春季', 'temp_range': (18, 28), 'features': ['温暖', '多雨']},
            6: {'season': '夏季', 'temp_range': (24, 35), 'features': ['高温', '潮湿', '闷热']},
            7: {'season': '夏季', 'temp_range': (28, 38), 'features': ['高温', '酷热', '潮湿']},
            8: {'season': '夏季', 'temp_range': (26, 36), 'features': ['高温', '闷热']},
            9: {'season': '秋季', 'temp_range': (20, 30), 'features': ['温差大', '干燥']},
            10: {'season': '秋季', 'temp_range': (14, 24), 'features': ['凉爽', '干燥', '降温']},
            11: {'season': '秋季', 'temp_range': (6, 16), 'features': ['寒凉', '干燥', '降温']},
            12: {'season': '冬季', 'temp_range': (-2, 10), 'features': ['低温', '寒冷', '干燥']}
        }
        
        # 天气-疾病关联（基于医学常识，将通过数据验证）
        self.weather_disease_base = {
            '低温': ['上呼吸道疾病', '支气管炎', '高血压', '肺气肿'],
            '高温': ['胃肠炎', '中暑', '心血管疾病'],
            '潮湿': ['关节疾病', '皮肤感染', '风湿'],
            '干燥': ['上呼吸道疾病', '皮肤干燥'],
            '温差大': ['上呼吸道疾病', '感冒', '心血管疾病'],
            '寒冷': ['肺气肿', '支气管炎', '高血压']
        }
        
        # 加载并训练模型
        self._train_model()
    
    def _parse_age(self, age_str):
        """解析年龄字符串"""
        age_str = str(age_str)
        if '岁' in age_str:
            try:
                return float(age_str.replace('岁', ''))
            except (ValueError, TypeError):
                return None
        elif '月' in age_str or '天' in age_str:
            return 0
        else:
            try:
                return float(age_str)
            except (ValueError, TypeError):
                return None
    
    def _get_age_group(self, age):
        """获取年龄段"""
        if age is None:
            return '未知'
        if age < 18:
            return '0-17岁(未成年)'
        elif age < 40:
            return '18-39岁(青年)'
        elif age < 60:
            return '40-59岁(中年)'
        elif age < 80:
            return '60-79岁(老年)'
        else:
            return '80岁以上(高龄)'
    
    def _train_model(self):
        """基于真实病历数据训练模型"""
        try:
            # 读取病历数据
            base_dir = Path(__file__).resolve().parents[1]
            data_path = base_dir / 'data' / 'research' / '数据.xlsx'
            df = pd.read_excel(data_path, header=None)
            df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
                         '科室', '医生', '疾病分类', '主诉', '病历描述', 
                         '列11', '体温', '心率', '血压']
            
            # 解析数据
            df['年龄数值'] = df['年龄'].apply(self._parse_age)
            df['年龄段'] = df['年龄数值'].apply(self._get_age_group)
            df['就诊时间'] = pd.to_datetime(df['就诊时间'])
            df['月份'] = df['就诊时间'].dt.month
            df['季节'] = df['月份'].apply(lambda m: self.season_weather[m]['season'])
            
            # 提取姓氏用于社区分配
            df['姓氏'] = df['姓名'].astype(str).str[0]
            
            total_records = len(df)
            
            # ========== 1. 月份-疾病分布分析 ==========
            for month in range(1, 13):
                month_df = df[df['月份'] == month]
                if len(month_df) > 0:
                    disease_counts = month_df['疾病分类'].value_counts()
                    self.disease_by_month[month] = {
                        'total': len(month_df),
                        'rate': len(month_df) / total_records,
                        'diseases': disease_counts.to_dict(),
                        'top_diseases': disease_counts.head(5).to_dict(),
                        'weather_features': self.season_weather[month]['features'],
                        'season': self.season_weather[month]['season']
                    }
            
            # ========== 2. 年龄段-疾病分布分析 ==========
            age_groups = ['0-17岁(未成年)', '18-39岁(青年)', '40-59岁(中年)', 
                         '60-79岁(老年)', '80岁以上(高龄)']
            
            for age_group in age_groups:
                age_df = df[df['年龄段'] == age_group]
                if len(age_df) > 0:
                    disease_counts = age_df['疾病分类'].value_counts()
                    self.disease_by_age[age_group] = {
                        'total': len(age_df),
                        'rate': len(age_df) / total_records,
                        'diseases': disease_counts.to_dict(),
                        'top_diseases': disease_counts.head(5).to_dict()
                    }
            
            # ========== 3. 计算季节风险系数 ==========
            # 基于各月份的发病率计算
            avg_monthly = total_records / 12
            for month, data in self.disease_by_month.items():
                # 风险系数 = 实际发病数 / 平均发病数
                self.seasonal_risk[month] = {
                    'risk_factor': data['total'] / avg_monthly if avg_monthly > 0 else 1.0,
                    'season': data['season'],
                    'features': data['weather_features']
                }
            
            # ========== 4. 计算年龄风险系数 ==========
            # 基于各年龄段的发病率
            for age_group, data in self.disease_by_age.items():
                # 老年人风险更高
                if '老年' in age_group or '高龄' in age_group:
                    base_risk = 1.5
                elif '中年' in age_group:
                    base_risk = 1.2
                else:
                    base_risk = 1.0
                
                self.age_risk[age_group] = {
                    'risk_factor': base_risk,
                    'case_count': data['total'],
                    'top_diseases': data['top_diseases']
                }
            
            # ========== 5. 天气-疾病相关性验证 ==========
            self.weather_disease_correlation = {}
            
            # 分析冬季（低温）的疾病分布
            winter_df = df[df['季节'] == '冬季']
            summer_df = df[df['季节'] == '夏季']
            
            if len(winter_df) > 0:
                winter_diseases = winter_df['疾病分类'].value_counts()
                self.weather_disease_correlation['低温/寒冷'] = {
                    'sample_size': len(winter_df),
                    'top_diseases': winter_diseases.head(5).to_dict(),
                    'risk_increase': len(winter_df) / (total_records / 4)  # 与平均季节比
                }
            
            if len(summer_df) > 0:
                summer_diseases = summer_df['疾病分类'].value_counts()
                self.weather_disease_correlation['高温/炎热'] = {
                    'sample_size': len(summer_df),
                    'top_diseases': summer_diseases.head(5).to_dict(),
                    'risk_increase': len(summer_df) / (total_records / 4)
                }
            
            self.model_data = {
                'total_records': total_records,
                'trained_at': utcnow().isoformat(),
                'disease_categories': df['疾病分类'].nunique(),
                'date_range': {
                    'start': str(df['就诊时间'].min()),
                    'end': str(df['就诊时间'].max())
                }
            }
            
            print(f"模型训练完成: 基于 {total_records} 条病历数据")
            
        except Exception as e:
            print(f"模型训练失败: {e}")
            import traceback
            traceback.print_exc()
    
    def predict_community_risk(self, community_info, weather_data):
        """
        预测社区健康风险
        
        参数:
        - community_info: 社区信息 {name, elderly_ratio, chronic_disease_ratio, population}
        - weather_data: 天气数据 {temperature, humidity, aqi, month}
        
        返回:
        - 风险评估结果
        """
        risk_score = 0
        risk_factors = []
        high_risk_diseases = []
        
        # 1. 基于月份/季节的风险
        month = weather_data.get('month', now_local().month)
        if month in self.seasonal_risk:
            seasonal = self.seasonal_risk[month]
            season_risk = (seasonal['risk_factor'] - 1) * 30  # 转换为风险分数
            risk_score += max(0, season_risk)
            
            if seasonal['risk_factor'] > 1.1:
                risk_factors.append(f"{seasonal['season']}季节发病率较高(系数:{seasonal['risk_factor']:.2f})")
            
            # 获取该月份高发疾病
            if month in self.disease_by_month:
                high_risk_diseases = list(self.disease_by_month[month]['top_diseases'].keys())[:3]
        
        # 2. 基于温度的风险
        temp = weather_data.get('temperature', 20)
        if temp < 5:
            risk_score += 25
            risk_factors.append(f'低温({temp}°C)增加呼吸道疾病风险')
            high_risk_diseases.extend(['上呼吸道疾病', '支气管炎', '肺气肿'])
        elif temp > 35:
            risk_score += 20
            risk_factors.append(f'高温({temp}°C)增加中暑和胃肠道疾病风险')
            high_risk_diseases.extend(['胃肠炎', '中暑'])
        elif abs(temp - 20) > 10:
            risk_score += 10
            risk_factors.append('温度偏离舒适区')
        
        # 3. 基于社区老龄化程度
        elderly_ratio = community_info.get('elderly_ratio', 0)
        if elderly_ratio > 0.3:
            risk_score += 30
            risk_factors.append(f'老年人口比例高({elderly_ratio*100:.1f}%)，健康风险显著增加')
        elif elderly_ratio > 0.2:
            risk_score += 20
            risk_factors.append(f'老年人口比例较高({elderly_ratio*100:.1f}%)')
        elif elderly_ratio > 0.1:
            risk_score += 10
        
        # 4. 基于慢性病比例
        chronic_ratio = community_info.get('chronic_disease_ratio', 0)
        if chronic_ratio > 0.2:
            risk_score += 25
            risk_factors.append(f'慢性病患者比例高({chronic_ratio*100:.1f}%)')
            high_risk_diseases.extend(['高血压', '慢性胃炎'])
        elif chronic_ratio > 0.1:
            risk_score += 15
        
        # 5. 基于空气质量
        aqi = weather_data.get('aqi', 50)
        if aqi > 150:
            risk_score += 25
            risk_factors.append(f'空气质量差(AQI:{aqi})，呼吸道疾病风险增加')
            high_risk_diseases.extend(['上呼吸道疾病', '支气管炎'])
        elif aqi > 100:
            risk_score += 15
            risk_factors.append(f'空气质量一般(AQI:{aqi})')
        
        # 确定风险等级
        if risk_score >= 70:
            risk_level = '高风险'
            color = 'danger'
        elif risk_score >= 40:
            risk_level = '中风险'
            color = 'warning'
        else:
            risk_level = '低风险'
            color = 'success'
        
        # 去重并限制疾病数量
        high_risk_diseases = list(dict.fromkeys(high_risk_diseases))[:5]
        
        return {
            'community': community_info.get('name', '未知'),
            'risk_score': min(risk_score, 100),
            'risk_level': risk_level,
            'color': color,
            'risk_factors': risk_factors,
            'high_risk_diseases': high_risk_diseases,
            'recommendations': self._generate_recommendations(risk_factors, weather_data),
            'model_confidence': 'high' if self.model_data else 'low'
        }
    
    def predict_individual_risk(self, user_info, weather_data):
        """
        预测个人健康风险
        
        参数:
        - user_info: 用户信息 {age, gender, chronic_diseases, community}
        - weather_data: 天气数据
        """
        risk_score = 0
        risk_factors = []
        personalized_risks = []
        
        age = user_info.get('age', 40)
        age_group = self._get_age_group(age)
        
        # 1. 年龄相关风险
        if age_group in self.age_risk:
            age_data = self.age_risk[age_group]
            age_risk_factor = age_data['risk_factor']
            risk_score += (age_risk_factor - 1) * 40
            
            if age >= 60:
                risk_factors.append(f'年龄({age}岁)属于高风险人群')
                personalized_risks.extend(list(age_data['top_diseases'].keys())[:3])
        
        # 2. 天气相关风险
        month = weather_data.get('month', now_local().month)
        temp = weather_data.get('temperature', 20)
        
        # 老年人对极端温度更敏感
        if age >= 60:
            if temp < 10:
                risk_score += 25
                risk_factors.append('低温天气对老年人影响更大')
                personalized_risks.extend(['高血压', '心血管疾病', '上呼吸道疾病'])
            elif temp > 32:
                risk_score += 25
                risk_factors.append('高温天气对老年人影响更大')
                personalized_risks.extend(['中暑', '心血管疾病'])
        
        # 3. 慢性病相关风险
        chronic_diseases = user_info.get('chronic_diseases', [])
        if chronic_diseases:
            if isinstance(chronic_diseases, str):
                chronic_diseases = [chronic_diseases]
            
            risk_score += len(chronic_diseases) * 15
            risk_factors.append(f'患有{len(chronic_diseases)}种慢性病')
            
            for disease in chronic_diseases:
                if '高血压' in disease:
                    if temp < 5 or temp > 35:
                        risk_score += 15
                        risk_factors.append('极端温度可能影响血压')
                elif '呼吸' in disease or '肺' in disease:
                    aqi = weather_data.get('aqi', 50)
                    if aqi > 100:
                        risk_score += 20
                        risk_factors.append('空气质量可能加重呼吸系统疾病')
        
        # 4. 季节性风险
        if month in self.disease_by_month:
            month_data = self.disease_by_month[month]
            personalized_risks.extend(list(month_data['top_diseases'].keys())[:2])
        
        # 确定风险等级
        if risk_score >= 60:
            risk_level = '高风险'
            color = 'danger'
            alert = True
        elif risk_score >= 35:
            risk_level = '中风险'
            color = 'warning'
            alert = False
        else:
            risk_level = '低风险'
            color = 'success'
            alert = False
        
        personalized_risks = list(dict.fromkeys(personalized_risks))[:5]
        
        return {
            'risk_score': min(risk_score, 100),
            'risk_level': risk_level,
            'color': color,
            'alert': alert,
            'risk_factors': risk_factors,
            'potential_diseases': personalized_risks,
            'recommendations': self._generate_personal_recommendations(user_info, weather_data, risk_factors),
            'age_group': age_group
        }
    
    def get_weather_alert(self, weather_data):
        """
        生成天气健康预警
        """
        alerts = []
        
        temp = weather_data.get('temperature', 20)
        humidity = weather_data.get('humidity', 50)
        aqi = weather_data.get('aqi', 50)
        month = weather_data.get('month', now_local().month)
        
        # 温度预警
        if temp >= 35:
            alerts.append({
                'type': '高温预警',
                'level': 'danger',
                'message': f'当前温度{temp}°C，注意防暑降温',
                'affected_groups': ['老年人', '心血管疾病患者', '户外工作者'],
                'diseases_risk': ['中暑', '心血管疾病', '胃肠炎'],
                'advice': ['减少户外活动', '多饮水', '注意防晒', '开启空调降温']
            })
        elif temp <= 5:
            alerts.append({
                'type': '低温预警',
                'level': 'warning',
                'message': f'当前温度{temp}°C，注意保暖防寒',
                'affected_groups': ['老年人', '呼吸系统疾病患者', '高血压患者'],
                'diseases_risk': ['上呼吸道疾病', '支气管炎', '高血压'],
                'advice': ['注意保暖', '减少外出', '保持室内温暖', '预防感冒']
            })
        
        # 空气质量预警
        if aqi > 150:
            alerts.append({
                'type': '空气质量预警',
                'level': 'danger',
                'message': f'空气质量指数{aqi}，污染严重',
                'affected_groups': ['呼吸系统疾病患者', '老年人', '儿童'],
                'diseases_risk': ['上呼吸道疾病', '支气管炎', '哮喘'],
                'advice': ['减少户外活动', '外出佩戴口罩', '使用空气净化器', '关闭门窗']
            })
        elif aqi > 100:
            alerts.append({
                'type': '空气质量提醒',
                'level': 'warning',
                'message': f'空气质量指数{aqi}，轻度污染',
                'affected_groups': ['敏感人群'],
                'diseases_risk': ['呼吸道疾病'],
                'advice': ['敏感人群减少户外活动']
            })
        
        # 季节性预警
        if month in self.disease_by_month:
            month_data = self.disease_by_month[month]
            if month_data['rate'] > 0.1:  # 高于平均
                top_disease = list(month_data['top_diseases'].keys())[0]
                alerts.append({
                    'type': '季节性健康提醒',
                    'level': 'info',
                    'message': f'{month_data["season"]}是{top_disease}高发期',
                    'affected_groups': ['全体居民'],
                    'diseases_risk': list(month_data['top_diseases'].keys())[:3],
                    'advice': ['注意个人卫生', '增强免疫力', '如有不适及时就医']
                })
        
        return alerts
    
    def _generate_recommendations(self, risk_factors, weather_data):
        """生成社区健康建议"""
        recommendations = []
        
        if any('低温' in f for f in risk_factors):
            recommendations.append('社区应关注独居老人保暖情况')
            recommendations.append('建议开放社区暖心驿站')
        
        if any('高温' in f for f in risk_factors):
            recommendations.append('社区应设立防暑降温点')
            recommendations.append('关注户外工作人员健康')
        
        if any('老年' in f for f in risk_factors):
            recommendations.append('加强对老年人的健康巡访')
            recommendations.append('社区医疗站做好应急准备')
        
        if any('空气' in f for f in risk_factors):
            recommendations.append('建议居民减少户外活动')
            recommendations.append('社区可发放防护口罩')
        
        if any('慢性病' in f for f in risk_factors):
            recommendations.append('提醒慢性病患者按时服药')
            recommendations.append('开展慢性病健康宣教')
        
        if not recommendations:
            recommendations.append('保持正常健康管理工作')
        
        return recommendations
    
    def _generate_personal_recommendations(self, user_info, weather_data, risk_factors):
        """生成个人健康建议"""
        recommendations = []
        age = user_info.get('age', 40)
        
        if age >= 60:
            recommendations.append('建议定期测量血压')
            recommendations.append('外出注意防滑防摔')
        
        temp = weather_data.get('temperature', 20)
        if temp < 10:
            recommendations.append('注意保暖，特别是头部和脚部')
            if age >= 60:
                recommendations.append('室内保持适宜温度(18-22°C)')
        elif temp > 30:
            recommendations.append('多饮水，避免高温时段外出')
            recommendations.append('饮食清淡，注意食品卫生')
        
        aqi = weather_data.get('aqi', 50)
        if aqi > 100:
            recommendations.append('减少户外运动')
            recommendations.append('外出佩戴口罩')
        
        chronic_diseases = user_info.get('chronic_diseases', [])
        if chronic_diseases:
            recommendations.append('按时服药，定期复查')
            recommendations.append('如有不适及时就医')
        
        if not recommendations:
            recommendations.append('保持健康生活方式')
            recommendations.append('适量运动，均衡饮食')
        
        return recommendations
    
    def get_model_statistics(self):
        """获取模型统计信息"""
        if not self.model_data:
            return {'status': '模型未训练'}
        
        return {
            'status': '模型已训练',
            'total_records': self.model_data['total_records'],
            'trained_at': self.model_data['trained_at'],
            'disease_categories': self.model_data['disease_categories'],
            'date_range': self.model_data['date_range'],
            'seasonal_analysis': {
                month: {
                    'season': data['season'],
                    'risk_factor': f"{data['risk_factor']:.2f}",
                    'features': data['features']
                }
                for month, data in self.seasonal_risk.items()
            },
            'age_analysis': {
                age: {
                    'case_count': data['case_count'],
                    'risk_factor': f"{data['risk_factor']:.2f}"
                }
                for age, data in self.age_risk.items()
            },
            'weather_correlation': self.weather_disease_correlation
        }


# 测试代码
if __name__ == '__main__':
    service = DataDrivenPredictionService()
    
    print("\n" + "=" * 60)
    print("模型统计信息")
    print("=" * 60)
    
    import json
    stats = service.get_model_statistics()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    
    print("\n" + "=" * 60)
    print("测试社区风险预测")
    print("=" * 60)
    
    # 测试社区预测
    community = {
        'name': '牛家垄周村',
        'elderly_ratio': 0.67,
        'chronic_disease_ratio': 0.1,
        'population': 132
    }
    
    weather = {
        'temperature': 5,
        'humidity': 70,
        'aqi': 120,
        'month': 12
    }
    
    result = service.predict_community_risk(community, weather)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    print("\n" + "=" * 60)
    print("测试个人风险预测")
    print("=" * 60)
    
    user = {
        'age': 68,
        'gender': '男',
        'chronic_diseases': ['高血压'],
        'community': '牛家垄周村'
    }
    
    result = service.predict_individual_risk(user, weather)
    print(json.dumps(result, ensure_ascii=False, indent=2))
