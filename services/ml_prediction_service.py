# -*- coding: utf-8 -*-
"""
机器学习预测服务 - 支持多分类疾病预测
包含多种天气因素：温度、湿度、体感温度、风速、降水量、日照等
"""
import joblib
import logging
import numpy as np
import os
import threading
from core.time_utils import now_local

logger = logging.getLogger(__name__)

GENERIC_ERROR_MESSAGE = '服务暂时不可用，请稍后再试'
_ml_service_instance = None
_ml_service_lock = threading.Lock()


def get_ml_service():
    """获取 ML 预测服务单例，避免重复加载模型。"""
    global _ml_service_instance
    if _ml_service_instance is None:
        with _ml_service_lock:
            if _ml_service_instance is None:
                _ml_service_instance = MLPredictionService()
    return _ml_service_instance

class MLPredictionService:
    """基于机器学习模型的多分类预测服务"""
    
    def __init__(self):
        self.model = None
        self.scaler = None
        self.label_encoder = None
        self.model_loaded = False
        self.model_info = {}
        
        # 天气特征默认值（用于缺失值填充）
        self.weather_defaults = {
            'tmean': 15.0,
            'tmin': 10.0,
            'tmax': 20.0,
            'feels_like': 14.0,
            'humidity': 70.0,
            'wind_speed': 2.5,
            'precipitation': 0.0,
            'sunshine_hours': 20000.0
        }
        
        # 疾病-天气关联矩阵（用于风险调整）
        self.disease_weather_sensitivity = {
            '上呼吸道疾病': {'low_temp': 1.5, 'high_humidity': 1.2, 'low_humidity': 1.3},
            '支气管炎': {'low_temp': 1.4, 'high_humidity': 1.3},
            '肺气肿': {'low_temp': 1.6, 'low_humidity': 1.2},
            '高血压': {'low_temp': 1.4, 'high_temp': 1.3, 'temp_change': 1.5},
            '胃肠炎': {'high_temp': 1.4, 'high_humidity': 1.3},
            '慢性胃炎': {'high_temp': 1.2, 'stress': 1.3},
            '心血管疾病': {'low_temp': 1.5, 'high_temp': 1.4, 'temp_change': 1.6}
        }
        
        # 加载模型
        self._load_model()
    
    def _load_model(self):
        """加载训练好的模型"""
        try:
            base_path = os.path.dirname(os.path.dirname(__file__))
            models_path = os.path.join(base_path, 'models')
            
            self.model = joblib.load(os.path.join(models_path, 'disease_predictor.pkl'))
            self.scaler = joblib.load(os.path.join(models_path, 'scaler.pkl'))
            self.label_encoder = joblib.load(os.path.join(models_path, 'label_encoder.pkl'))
            
            # 加载配置
            import json
            with open(os.path.join(models_path, 'feature_config.json'), 'r', encoding='utf-8') as f:
                self.model_info = json.load(f)
            
            self.model_loaded = True
            print("✅ ML模型加载成功！")
            print(f"   模型类型: {self.model_info.get('model_name', 'Unknown')}")
            print(f"   分类类型: {self.model_info.get('model_type', 'unknown')}")
            print(f"   准确率: {self.model_info.get('accuracy', 0)*100:.2f}%")
            print(f"   疾病类别数: {len(self.model_info.get('classes', []))}")
            
        except Exception as e:
            print(f"⚠️ ML模型加载失败: {e}")
            self.model_loaded = False
    
    def _get_season(self, month):
        """获取季节"""
        if month in [12, 1, 2]:
            return 0  # 冬季
        elif month in [3, 4, 5]:
            return 1  # 春季
        elif month in [6, 7, 8]:
            return 2  # 夏季
        else:
            return 3  # 秋季
    
    def _get_season_name(self, month):
        """获取季节名称"""
        if month in [12, 1, 2]:
            return '冬季'
        elif month in [3, 4, 5]:
            return '春季'
        elif month in [6, 7, 8]:
            return '夏季'
        else:
            return '秋季'
    
    def _get_age_group(self, age):
        """获取年龄段编码"""
        if age is None:
            return 2
        if age < 18:
            return 0
        elif age < 40:
            return 1
        elif age < 60:
            return 2
        elif age < 80:
            return 3
        else:
            return 4
    
    def _get_age_group_name(self, age):
        """获取年龄段名称"""
        if age < 18:
            return '未成年人(0-17岁)'
        elif age < 40:
            return '青年人(18-39岁)'
        elif age < 60:
            return '中年人(40-59岁)'
        elif age < 80:
            return '老年人(60-79岁)'
        else:
            return '高龄老人(80岁以上)'
    
    def _calculate_feels_like(self, temp, humidity, wind_speed):
        """计算体感温度"""
        # 简化的体感温度计算
        if temp <= 10:
            # 风寒指数 (适用于低温)
            feels_like = 13.12 + 0.6215 * temp - 11.37 * (wind_speed ** 0.16) + 0.3965 * temp * (wind_speed ** 0.16)
        elif temp >= 27:
            # 热指数 (适用于高温)
            feels_like = temp + 0.33 * (humidity / 100.0 * 6.105 * np.exp(17.27 * temp / (237.7 + temp))) - 4.0
        else:
            feels_like = temp
        return feels_like
    
    def predict_disease_risk(self, user_info, weather_info=None):
        """
        预测个人疾病风险（多分类）
        
        参数:
        - user_info: 用户信息 {age, gender}
        - weather_info: 天气信息 {
            temperature/tmean, tmin, tmax, feels_like,
            humidity, wind_speed, precipitation, sunshine_hours,
            aqi, month
          }
        
        返回:
        - 多分类预测结果
        """
        if not self.model_loaded:
            return {
                'success': False,
                'error': '模型未加载',
                'predictions': []
            }
        
        try:
            # 提取用户信息
            age = user_info.get('age', 40)
            gender = user_info.get('gender', '男')
            
            # 时间特征
            now = now_local()
            month = int(weather_info.get('month', now.month)) if weather_info else now.month
            weekday = now.weekday()
            hour = now.hour
            
            # 计算派生特征
            season = self._get_season(month)
            age_group = self._get_age_group(age)
            gender_code = 1 if gender in ['男', '男性'] else 0
            
            # 提取天气特征
            if weather_info:
                # 温度 - 支持多种参数名
                tmean = weather_info.get('tmean', weather_info.get('temperature', self.weather_defaults['tmean']))
                tmin = weather_info.get('tmin', weather_info.get('temperature_min', tmean - 5))
                tmax = weather_info.get('tmax', weather_info.get('temperature_max', tmean + 5))
                
                # 湿度
                humidity = weather_info.get('humidity', self.weather_defaults['humidity'])
                
                # 风速
                wind_speed = weather_info.get('wind_speed', self.weather_defaults['wind_speed'])
                
                # 体感温度 - 如果没提供则计算
                feels_like = weather_info.get('feels_like')
                if feels_like is None:
                    feels_like = self._calculate_feels_like(tmean, humidity, wind_speed)
                
                # 降水量
                precipitation = weather_info.get('precipitation', self.weather_defaults['precipitation'])
                
                # 日照时数
                sunshine_hours = weather_info.get('sunshine_hours', self.weather_defaults['sunshine_hours'])
            else:
                tmean = self.weather_defaults['tmean']
                tmin = self.weather_defaults['tmin']
                tmax = self.weather_defaults['tmax']
                feels_like = self.weather_defaults['feels_like']
                humidity = self.weather_defaults['humidity']
                wind_speed = self.weather_defaults['wind_speed']
                precipitation = self.weather_defaults['precipitation']
                sunshine_hours = self.weather_defaults['sunshine_hours']
            
            # 检查模型特征列
            feature_cols = self.model_info.get('feature_cols', [])
            
            # 根据模型配置构建特征向量
            if 'tmean' in feature_cols:
                # 新模型（包含天气特征）
                features = np.array([[
                    age, gender_code, month, season, age_group, weekday, hour,
                    tmean, tmin, tmax, feels_like, humidity,
                    wind_speed, precipitation, sunshine_hours
                ]])
            else:
                # 旧模型（仅基本特征）
                features = np.array([[age, gender_code, month, season, age_group, weekday, hour]])
            
            # 标准化
            features_scaled = self.scaler.transform(features)
            
            # 预测概率
            probabilities = self.model.predict_proba(features_scaled)[0]
            
            # 获取所有疾病的预测概率
            predictions = []
            for idx, prob in enumerate(probabilities):
                disease_name = self.label_encoder.classes_[idx]
                
                # 应用天气敏感度调整
                adjusted_prob = self._adjust_probability_by_weather(
                    disease_name, prob, weather_info
                )
                
                predictions.append({
                    'disease': disease_name,
                    'probability': float(adjusted_prob),
                    'percentage': f"{adjusted_prob*100:.1f}%",
                    'original_probability': float(prob)
                })
            
            # 按概率排序
            predictions.sort(key=lambda x: x['probability'], reverse=True)
            
            # 计算综合风险分数
            risk_score = self._calculate_risk_score(age, predictions, weather_info)
            
            # 风险等级
            if risk_score >= 70:
                risk_level = '高风险'
                risk_color = 'danger'
            elif risk_score >= 40:
                risk_level = '中风险'
                risk_color = 'warning'
            else:
                risk_level = '低风险'
                risk_color = 'success'
            
            # 分析风险因素
            risk_factors = self._analyze_risk_factors(age, weather_info, predictions)
            
            # 生成建议
            recommendations = self._generate_recommendations(
                age, gender, predictions[:3], weather_info
            )
            
            # 天气影响分析
            weather_impact = self._analyze_weather_impact(weather_info)

            # 可解释输出（复用慢病规则）
            explain = None
            triggered_rules = []
            rule_version = None
            try:
                from services.chronic_risk_service import ChronicRiskService
                chronic_service = ChronicRiskService()
                top_disease = predictions[0]['disease'] if predictions else ''
                disease_type = 'general'
                if any(k in top_disease for k in ['心', '血压', '冠心', '心力']):
                    disease_type = 'cardiovascular'
                elif any(k in top_disease for k in ['呼吸', '肺', '支气管', '哮喘']):
                    disease_type = 'respiratory'
                elif any(k in top_disease for k in ['胃', '肠', '消化']):
                    disease_type = 'digestive'
                rr_proxy = 1.0 + (min(max(risk_score, 0), 100) / 100.0) * 0.8
                explain_context = {
                    'age': age,
                    'temperature': tmean,
                    'rr': rr_proxy,
                    'disease_type': disease_type,
                    'chronic_diseases': [],
                    'has_chronic_disease': False,
                    'disease_count': 0,
                    'aqi': weather_info.get('aqi', 50) if weather_info else 50,
                    'hot_night': False,
                    'hot_night_temp': weather_info.get('tmin', 22) if weather_info else 22,
                    'heat_wave_days': weather_info.get('heat_wave_days', 0) if weather_info else 0,
                    'cold_wave_days': weather_info.get('cold_wave_days', 0) if weather_info else 0
                }
                explain, triggered_rules = chronic_service.build_explain(explain_context, recommendations)
                rule_version = chronic_service.rules_version
            except Exception:
                explain = None
            
            return {
                'success': True,
                'user_profile': {
                    'age': age,
                    'gender': gender,
                    'age_group': self._get_age_group_name(age)
                },
                'predictions': predictions[:10],  # 返回前10个预测
                'top_prediction': predictions[0] if predictions else None,
                'risk_score': risk_score,
                'risk_level': risk_level,
                'risk_color': risk_color,
                'risk_factors': risk_factors,
                'weather_impact': weather_impact,
                'recommendations': recommendations,
                'explain': explain,
                'rule_version': rule_version,
                'triggered_rules': triggered_rules,
                'model_info': {
                    'accuracy': f"{self.model_info.get('accuracy', 0)*100:.1f}%",
                    'model_type': self.model_info.get('model_name', 'RandomForest'),
                    'classification_type': self.model_info.get('model_type', 'multiclass'),
                    'total_classes': len(self.label_encoder.classes_)
                },
                'weather_conditions': {
                    'temperature': tmean,
                    'feels_like': feels_like,
                    'humidity': humidity,
                    'wind_speed': wind_speed,
                    'precipitation': precipitation,
                    'season': self._get_season_name(month)
                }
            }
            
        except Exception as exc:
            logger.exception("ML疾病风险预测失败")
            return {
                'success': False,
                'error': GENERIC_ERROR_MESSAGE,
                'predictions': []
            }
    
    def _adjust_probability_by_weather(self, disease, prob, weather_info):
        """根据天气条件调整疾病概率"""
        if not weather_info or disease not in self.disease_weather_sensitivity:
            return prob
        
        sensitivity = self.disease_weather_sensitivity[disease]
        adjustment = 1.0
        
        temp = weather_info.get('tmean', weather_info.get('temperature', 15))
        humidity = weather_info.get('humidity', 70)
        
        # 低温调整
        if 'low_temp' in sensitivity and temp < 10:
            adjustment *= sensitivity['low_temp'] * (1 + (10 - temp) / 20)
        
        # 高温调整
        if 'high_temp' in sensitivity and temp > 30:
            adjustment *= sensitivity['high_temp'] * (1 + (temp - 30) / 20)
        
        # 高湿度调整
        if 'high_humidity' in sensitivity and humidity > 80:
            adjustment *= sensitivity['high_humidity']
        
        # 低湿度调整
        if 'low_humidity' in sensitivity and humidity < 40:
            adjustment *= sensitivity['low_humidity']
        
        # 限制调整幅度
        adjusted = min(prob * adjustment, 0.95)
        return adjusted
    
    def _calculate_risk_score(self, age, predictions, weather_info):
        """计算综合风险分数"""
        risk_score = 0
        
        # 基于年龄的风险
        if age >= 70:
            risk_score += 25
        elif age >= 60:
            risk_score += 18
        elif age >= 50:
            risk_score += 10
        elif age < 18:
            risk_score += 8
        
        # 基于疾病概率的风险
        if predictions:
            top_prob = predictions[0]['probability']
            risk_score += top_prob * 35
            
            # 如果多个疾病概率都较高
            high_prob_count = sum(1 for p in predictions[:5] if p['probability'] > 0.15)
            if high_prob_count >= 3:
                risk_score += 10
        
        # 基于天气的风险
        if weather_info:
            temp = weather_info.get('tmean') or weather_info.get('temperature') or 20
            humidity = weather_info.get('humidity') or 70
            aqi = weather_info.get('aqi') or 50
            wind_speed = weather_info.get('wind_speed') or 2.5
            
            # 确保数值类型
            try:
                temp = float(temp)
                humidity = float(humidity)
                aqi = float(aqi)
                wind_speed = float(wind_speed)
            except (TypeError, ValueError):
                temp, humidity, aqi, wind_speed = 20, 70, 50, 2.5
            
            # 极端温度
            if temp < 0 or temp > 38:
                risk_score += 15
            elif temp < 5 or temp > 35:
                risk_score += 10
            elif temp < 10 or temp > 32:
                risk_score += 5
            
            # 极端湿度
            if humidity > 90 or humidity < 30:
                risk_score += 8
            elif humidity > 85 or humidity < 40:
                risk_score += 4
            
            # 空气质量
            if aqi > 150:
                risk_score += 15
            elif aqi > 100:
                risk_score += 8
            elif aqi > 75:
                risk_score += 4
            
            # 强风
            if wind_speed > 10:
                risk_score += 5
        
        return min(risk_score, 100)
    
    def _analyze_risk_factors(self, age, weather_info, predictions):
        """分析风险因素"""
        factors = []
        
        # 年龄因素
        if age >= 65:
            factors.append(f'年龄({age}岁)属于高风险人群，免疫力相对较低')
        elif age < 10:
            factors.append(f'年龄({age}岁)为儿童，免疫系统发育中')
        
        if weather_info:
            temp = weather_info.get('tmean') or weather_info.get('temperature') or 20
            humidity = weather_info.get('humidity') or 70
            aqi = weather_info.get('aqi') or 50
            wind_speed = weather_info.get('wind_speed') or 2.5
            feels_like = weather_info.get('feels_like')
            if feels_like is None:
                feels_like = temp  # 默认使用实际温度
            
            # 确保数值类型
            try:
                temp = float(temp)
                humidity = float(humidity)
                aqi = float(aqi)
                wind_speed = float(wind_speed)
                feels_like = float(feels_like)
            except (TypeError, ValueError):
                temp, humidity, aqi, wind_speed, feels_like = 20, 70, 50, 2.5, 20
            
            # 温度因素
            if temp < 5:
                factors.append(f'低温天气({temp}°C)增加呼吸道和心血管疾病风险')
            elif temp > 35:
                factors.append(f'高温天气({temp}°C)增加中暑和胃肠道疾病风险')
            elif temp < 10:
                factors.append(f'气温偏低({temp}°C)，注意保暖防寒')
            elif temp > 32:
                factors.append(f'气温偏高({temp}°C)，注意防暑降温')
            
            # 体感温度
            if feels_like is not None:
                if feels_like < temp - 5:
                    factors.append(f'体感温度({feels_like:.1f}°C)明显低于实际温度，风寒效应显著')
                elif feels_like > temp + 5:
                    factors.append(f'体感温度({feels_like:.1f}°C)明显高于实际温度，闷热感强')
            
            # 湿度因素
            if humidity > 85:
                factors.append(f'湿度过高({humidity:.0f}%)，易引发关节炎和皮肤问题')
            elif humidity < 40:
                factors.append(f'湿度过低({humidity:.0f}%)，呼吸道黏膜易干燥')
            
            # 空气质量
            if aqi > 150:
                factors.append(f'空气质量差(AQI:{aqi})，呼吸系统疾病风险显著增加')
            elif aqi > 100:
                factors.append(f'空气质量一般(AQI:{aqi})，敏感人群需注意')
            
            # 风速
            if wind_speed > 8:
                factors.append(f'大风天气({wind_speed:.1f}m/s)，体感温度降低，注意防风')
        
        # 疾病概率因素
        if predictions and predictions[0]['probability'] > 0.5:
            factors.append(f'当前条件下{predictions[0]["disease"]}风险较高({predictions[0]["percentage"]})')
        
        return factors
    
    def _analyze_weather_impact(self, weather_info):
        """分析天气对健康的影响"""
        if not weather_info:
            return {'level': '未知', 'description': '无天气数据'}
        
        impact_score = 0
        impacts = []
        
        temp = weather_info.get('tmean') or weather_info.get('temperature') or 20
        humidity = weather_info.get('humidity') or 70
        aqi = weather_info.get('aqi') or 50
        
        # 确保数值类型
        try:
            temp = float(temp)
            humidity = float(humidity)
            aqi = float(aqi)
        except (TypeError, ValueError):
            temp, humidity, aqi = 20, 70, 50
        
        # 温度影响
        if temp < 5 or temp > 35:
            impact_score += 3
            impacts.append('极端温度')
        elif temp < 10 or temp > 32:
            impact_score += 2
            impacts.append('温度偏离舒适区')
        elif 15 <= temp <= 25:
            impacts.append('温度适宜')
        
        # 湿度影响
        if humidity > 85 or humidity < 35:
            impact_score += 2
            impacts.append('湿度不适')
        elif 50 <= humidity <= 70:
            impacts.append('湿度适宜')
        
        # 空气质量影响
        if aqi > 150:
            impact_score += 3
            impacts.append('空气污染严重')
        elif aqi > 100:
            impact_score += 2
            impacts.append('空气轻度污染')
        elif aqi <= 50:
            impacts.append('空气质量优')
        
        # 综合影响等级
        if impact_score >= 5:
            level = '严重影响'
            color = 'danger'
        elif impact_score >= 3:
            level = '中等影响'
            color = 'warning'
        elif impact_score >= 1:
            level = '轻微影响'
            color = 'info'
        else:
            level = '影响较小'
            color = 'success'
        
        return {
            'level': level,
            'color': color,
            'score': impact_score,
            'factors': impacts,
            'description': '、'.join(impacts) if impacts else '天气条件良好'
        }
    
    def _generate_recommendations(self, age, gender, top_predictions, weather_info):
        """生成健康建议"""
        recommendations = []
        
        # 基于年龄的建议
        if age >= 65:
            recommendations.append({
                'category': '老年健康',
                'advice': '建议定期测量血压和血糖，外出注意防滑防摔，随身携带常用药物',
                'priority': 'high'
            })
        elif age < 10:
            recommendations.append({
                'category': '儿童健康',
                'advice': '注意营养均衡，保证充足睡眠，避免接触传染源',
                'priority': 'medium'
            })
        
        if weather_info:
            temp = weather_info.get('tmean', weather_info.get('temperature', 20))
            humidity = weather_info.get('humidity', 70)
            aqi = weather_info.get('aqi', 50)
            wind_speed = weather_info.get('wind_speed', 2.5)
            
            # 温度相关建议
            if temp < 5:
                recommendations.append({
                    'category': '低温防护',
                    'advice': '天气寒冷，注意添衣保暖，特别是头部、颈部和脚部。室内保持适宜温度(18-22°C)',
                    'priority': 'high'
                })
            elif temp < 10:
                recommendations.append({
                    'category': '防寒提醒',
                    'advice': '气温较低，早晚温差大，注意保暖防寒，预防感冒',
                    'priority': 'medium'
                })
            elif temp > 35:
                recommendations.append({
                    'category': '高温防护',
                    'advice': '天气炎热，多饮水，避免10-14点高温时段外出，注意防暑降温',
                    'priority': 'high'
                })
            elif temp > 30:
                recommendations.append({
                    'category': '防暑提醒',
                    'advice': '气温较高，适当增加饮水量，饮食清淡，注意食品卫生',
                    'priority': 'medium'
                })
            
            # 湿度相关建议
            if humidity > 85:
                recommendations.append({
                    'category': '高湿度提醒',
                    'advice': '空气湿度较大，注意室内通风除湿，衣物及时晾晒',
                    'priority': 'low'
                })
            elif humidity < 40:
                recommendations.append({
                    'category': '干燥提醒',
                    'advice': '空气干燥，多饮水，可使用加湿器，注意皮肤保湿',
                    'priority': 'low'
                })
            
            # 空气质量建议
            if aqi > 150:
                recommendations.append({
                    'category': '空气质量警告',
                    'advice': '空气质量差，建议减少户外活动，外出务必佩戴口罩，使用空气净化器',
                    'priority': 'high'
                })
            elif aqi > 100:
                recommendations.append({
                    'category': '空气质量提醒',
                    'advice': '空气质量一般，敏感人群减少户外活动，外出建议佩戴口罩',
                    'priority': 'medium'
                })
            
            # 大风建议
            if wind_speed > 8:
                recommendations.append({
                    'category': '大风提醒',
                    'advice': '风力较大，外出注意防风，避免在高大建筑物附近停留',
                    'priority': 'medium'
                })
        
        # 基于预测疾病的建议
        for pred in top_predictions:
            disease = pred['disease']
            if '呼吸' in disease or '支气管' in disease or '肺' in disease:
                recommendations.append({
                    'category': '呼吸系统',
                    'advice': f'当前{disease}风险较高，注意保暖防寒，保持室内通风，避免接触烟尘',
                    'priority': 'high' if pred['probability'] > 0.3 else 'medium'
                })
            elif '胃' in disease or '肠' in disease or '消化' in disease:
                recommendations.append({
                    'category': '消化系统',
                    'advice': f'当前{disease}风险较高，饮食规律清淡，避免生冷辛辣，注意食品卫生',
                    'priority': 'high' if pred['probability'] > 0.3 else 'medium'
                })
            elif '高血压' in disease or '心血管' in disease:
                recommendations.append({
                    'category': '心血管系统',
                    'advice': f'当前{disease}风险较高，避免剧烈运动，保持情绪稳定，按时服药',
                    'priority': 'high' if pred['probability'] > 0.3 else 'medium'
                })
        
        # 通用建议
        recommendations.append({
            'category': '日常健康',
            'advice': '保持规律作息，适量运动，均衡饮食，如有不适及时就医',
            'priority': 'low'
        })
        
        # 按优先级排序
        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        recommendations.sort(key=lambda x: priority_order.get(x.get('priority', 'low'), 2))
        
        return recommendations
    
    def predict_community_risk(self, community_info, weather_info):
        """
        预测社区健康风险（多分类版本）
        """
        if not self.model_loaded:
            return {
                'success': False,
                'error': '模型未加载'
            }
        
        try:
            community_name = community_info.get('name', '未知社区')
            elderly_ratio = community_info.get('elderly_ratio', 0)
            population = community_info.get('population', 0)
            
            # 模拟不同年龄段人群的预测
            age_groups = [
                {'age': 8, 'ratio': 0.08, 'name': '儿童'},
                {'age': 25, 'ratio': 0.15, 'name': '青年'},
                {'age': 45, 'ratio': 0.22, 'name': '中年'},
                {'age': 65, 'ratio': 0.35, 'name': '老年'},
                {'age': 80, 'ratio': 0.20, 'name': '高龄'}
            ]
            
            # 根据社区老龄化率调整
            if elderly_ratio > 0.4:
                age_groups[3]['ratio'] = 0.40
                age_groups[4]['ratio'] = 0.25
                age_groups[1]['ratio'] = 0.10
            elif elderly_ratio > 0.3:
                age_groups[3]['ratio'] = 0.38
                age_groups[4]['ratio'] = 0.22
            
            # 聚合预测结果
            disease_risk = {}
            total_risk_score = 0
            all_factors = []
            
            for group in age_groups:
                result = self.predict_disease_risk(
                    {'age': group['age'], 'gender': '男'},
                    weather_info
                )
                
                if result['success']:
                    for pred in result['predictions'][:5]:
                        disease = pred['disease']
                        if disease not in disease_risk:
                            disease_risk[disease] = 0
                        disease_risk[disease] += pred['probability'] * group['ratio']
                    
                    total_risk_score += result['risk_score'] * group['ratio']
                    
                    # 收集风险因素
                    for factor in result.get('risk_factors', []):
                        if group['name'] in ['老年', '高龄'] and factor not in all_factors:
                            all_factors.append(factor)
            
            # 排序疾病风险
            sorted_risks = sorted(disease_risk.items(), key=lambda x: x[1], reverse=True)
            
            # 风险等级
            if total_risk_score >= 60:
                risk_level = '高风险'
                risk_color = 'danger'
            elif total_risk_score >= 40:
                risk_level = '中风险'
                risk_color = 'warning'
            else:
                risk_level = '低风险'
                risk_color = 'success'
            
            # 高风险人群分析
            high_risk_groups = []
            if elderly_ratio > 0.3:
                high_risk_groups.append('老年人群体')
            
            if weather_info:
                temp = weather_info.get('tmean', weather_info.get('temperature', 20))
                aqi = weather_info.get('aqi', 50)
                
                if temp < 10:
                    high_risk_groups.extend(['心血管疾病患者', '呼吸系统疾病患者'])
                if temp > 32:
                    high_risk_groups.extend(['心血管疾病患者', '户外工作者'])
                if aqi > 100:
                    high_risk_groups.append('呼吸系统疾病患者')
            
            return {
                'success': True,
                'community': community_name,
                'risk_score': round(total_risk_score, 1),
                'risk_level': risk_level,
                'risk_color': risk_color,
                'disease_risks': [
                    {'disease': d, 'risk': round(r * 100, 1), 'percentage': f'{r*100:.1f}%'} 
                    for d, r in sorted_risks[:8]
                ],
                'high_risk_groups': list(set(high_risk_groups)),
                'risk_factors': all_factors[:5],
                'recommendations': self._generate_community_recommendations(
                    elderly_ratio, weather_info, sorted_risks
                ),
                'model_accuracy': f"{self.model_info.get('accuracy', 0)*100:.1f}%"
            }
            
        except Exception as exc:
            logger.exception("ML社区风险预测失败")
            return {
                'success': False,
                'error': GENERIC_ERROR_MESSAGE
            }
    
    def _generate_community_recommendations(self, elderly_ratio, weather_info, disease_risks):
        """生成社区健康建议"""
        recommendations = []
        
        if elderly_ratio > 0.3:
            recommendations.append('加强对独居老人的健康巡访')
            recommendations.append('社区卫生站做好应急药品储备')
        
        if weather_info:
            temp = weather_info.get('tmean', weather_info.get('temperature', 20))
            aqi = weather_info.get('aqi', 50)
            humidity = weather_info.get('humidity', 70)
            
            if temp < 5:
                recommendations.append('开放社区暖心驿站')
                recommendations.append('提醒居民注意防寒保暖')
            elif temp < 10:
                recommendations.append('关注独居老人保暖情况')
            
            if temp > 35:
                recommendations.append('设立防暑降温点')
                recommendations.append('关注独居老人防暑情况')
            elif temp > 32:
                recommendations.append('提醒居民多饮水避暑')
            
            if aqi > 150:
                recommendations.append('发布空气质量红色预警')
                recommendations.append('建议居民减少户外活动')
            elif aqi > 100:
                recommendations.append('发布空气质量提醒')
                recommendations.append('建议敏感人群减少外出')
            
            if humidity > 85:
                recommendations.append('提醒居民注意室内通风除湿')
        
        # 基于疾病风险
        if disease_risks:
            top_diseases = [d[0] for d in disease_risks[:3]]
            if any('呼吸' in d or '支气管' in d for d in top_diseases):
                recommendations.append('开展呼吸道疾病预防宣教')
            if any('胃' in d or '肠' in d for d in top_diseases):
                recommendations.append('加强食品卫生宣传')
        
        if not recommendations:
            recommendations.append('保持常规健康管理工作')
        
        return recommendations
    
    def get_model_status(self):
        """获取模型状态"""
        return {
            'model_loaded': self.model_loaded,  # 添加明确的 model_loaded 字段
            'loaded': self.model_loaded,  # 保持向后兼容
            'model_name': self.model_info.get('model_name', 'Unknown'),
            'model_type': self.model_info.get('model_type', 'unknown'),
            'accuracy': self.model_info.get('accuracy', 0),
            'f1_score': self.model_info.get('f1_score', 0),
            'classes': self.model_info.get('classes', []),
            'feature_cols': self.model_info.get('feature_cols', []),
            'weather_features': self.model_info.get('weather_features', []),
            'description': self.model_info.get('description', '')
        }


# 测试
if __name__ == '__main__':
    service = MLPredictionService()

    print("\n" + "=" * 60)
    print("测试多分类ML预测服务")
    print("=" * 60)

    # 测试个人预测（包含完整天气因素）
    result = service.predict_disease_risk(
        {'age': 70, 'gender': '男'},
        {
            'temperature': 5,
            'tmean': 5,
            'tmin': 0,
            'tmax': 10,
            'humidity': 85,
            'wind_speed': 5.5,
            'precipitation': 2.0,
            'aqi': 120,
            'month': 1
        }
    )

    print("\n个人预测结果摘要:")
    print(f"  success: {bool(result.get('success'))}")
    print(f"  risk_level: {result.get('risk_level', '--')}")
    print(f"  risk_score: {result.get('risk_score', '--')}")

    # 测试社区预测
    result = service.predict_community_risk(
        {'name': '牛家垄周村', 'elderly_ratio': 0.67, 'population': 132},
        {'temperature': 5, 'humidity': 85, 'aqi': 120, 'month': 1}
    )

    print("\n社区预测结果摘要:")
    print(f"  success: {bool(result.get('success'))}")
    print(f"  risk_level: {result.get('risk_level', '--')}")
    print(f"  risk_score: {result.get('risk_score', '--')}")
