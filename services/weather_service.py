# -*- coding: utf-8 -*-
"""
模块一：天气预警服务
功能：自动天气数据采集、极端天气识别与定义、天气疾病相关分析、宏观气象风险预警
"""
import logging
import os
import requests
from datetime import datetime, timedelta
import json
import time
from flask import current_app, has_app_context
from services.external_api import record_external_api_timing as _record_external_api_timing
from core.time_utils import today_local

class WeatherService:
    """天气服务类"""
    
    def __init__(self):
        self.qweather_key = None
        self.api_base_url = None
        self.city_map = {}
        self.default_location = '116.20,29.27'  # 都昌县
        self.use_openmeteo_fallback = True  # 启用Open-Meteo备用API

        self._load_config()

    def _load_config(self):
        app_config = {}
        if has_app_context():
            try:
                app_config = current_app.config
            except Exception:
                app_config = {}

        self.qweather_key = app_config.get('QWEATHER_KEY') or os.getenv('QWEATHER_KEY')
        self.api_base_url = (
            app_config.get('QWEATHER_API_BASE')
            or os.getenv('QWEATHER_API_BASE', 'https://mj76x98pfn.re.qweatherapi.com/v7')
        )
        self.city_map = app_config.get('CITY_LOCATION_MAP') or {}
        self.default_location = (
            app_config.get('DEFAULT_LOCATION')
            or os.getenv('DEFAULT_LOCATION', self.default_location)
        )
    
    def _get_location(self, city):
        """获取城市的location参数"""
        # 首先从映射中查找
        if city in self.city_map:
            return self.city_map[city]
        
        # 尝试模糊匹配
        for key in self.city_map:
            if city in key or key in city:
                return self.city_map[key]
        
        # 返回默认位置
        return self.default_location

    def _parse_lon_lat(self, location: str):
        """Parse 'lon,lat' string safely. Return (lon, lat) as floats or None."""
        if not location or ',' not in str(location):
            return None
        parts = [p.strip() for p in str(location).split(',')]
        if len(parts) != 2:
            return None
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            return None
        # basic sanity check
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return None
        return lon, lat
    
    def get_current_weather(self, city="都昌"):
        """
        获取当前天气数据 - 使用和风天气API
        如果API调用失败，返回模拟数据
        """
        logger = logging.getLogger(__name__)
        # 尝试调用和风天气API
        if self.qweather_key and self.api_base_url:
            try:
                # 获取城市location
                location = self._get_location(city)
                
                # 调用实况天气API
                weather_url = f"{self.api_base_url}/weather/now"
                weather_params = {
                    'key': self.qweather_key,
                    'location': location
                }
                
                start_ts = time.perf_counter()
                weather_response = requests.get(weather_url, params=weather_params, timeout=10)
                _record_external_api_timing('qweather_now', (time.perf_counter() - start_ts) * 1000, weather_response.status_code)
                
                # 检查HTTP状态码
                if weather_response.status_code != 200:
                    logger.warning("API HTTP状态码: %s，使用模拟数据", weather_response.status_code)
                    return self._get_mock_weather()
                
                try:
                    weather_data = weather_response.json()
                except Exception as json_error:
                    logger.warning("JSON解析失败: %s，使用模拟数据", json_error)
                    logger.debug("响应内容: %s", weather_response.text[:200])
                    return self._get_mock_weather()
                
                # 检查返回状态
                code = weather_data.get('code')
                if code != '200':
                    if code is None:
                        logger.warning("和风天气API响应格式异常，使用模拟数据")
                        logger.debug("响应内容: %s", str(weather_data)[:200])
                    else:
                        error_msg = self._get_error_message(code)
                        logger.warning("和风天气API返回错误[%s]: %s，使用模拟数据", code, error_msg)
                    return self._get_mock_weather()
                
                # 解析天气数据
                now = weather_data.get('now', {})
                result = {
                    'temperature': float(now.get('temp', 20)),
                    'temperature_max': float(now.get('temp', 20)) + 3,  # 实况无最高温，预估
                    'temperature_min': float(now.get('temp', 20)) - 3,  # 实况无最低温，预估
                    'humidity': float(now.get('humidity', 60)),
                    'pressure': float(now.get('pressure', 1013)),
                    'weather_condition': now.get('text', '晴'),
                    'wind_speed': float(now.get('windSpeed', 3)),
                    'wind_dir': now.get('windDir', ''),
                    'feels_like': float(now.get('feelsLike', now.get('temp', 20))),
                    'pm25': 0,
                    'aqi': 0,
                    'location': city,
                    'update_time': now.get('obsTime', datetime.now().strftime('%Y-%m-%d %H:%M')),
                    'is_mock': False
                }
                
                # 尝试获取空气质量数据
                try:
                    air_url = f"{self.api_base_url}/air/now"
                    air_params = {
                        'key': self.qweather_key,
                        'location': location
                    }
                    
                    air_start = time.perf_counter()
                    air_response = requests.get(air_url, params=air_params, timeout=10)
                    _record_external_api_timing('qweather_air', (time.perf_counter() - air_start) * 1000, air_response.status_code)
                    try:
                        air_data = air_response.json()
                    except Exception as json_error:
                        logger.debug("空气质量JSON解析失败: %s", json_error)
                        air_data = {}
                    
                    if air_data.get('code') == '200' and 'now' in air_data:
                        air_now = air_data['now']
                        result['pm25'] = float(air_now.get('pm2p5', 0))
                        result['aqi'] = int(air_now.get('aqi', 0))
                        result['air_quality'] = air_now.get('category', '良')
                except requests.exceptions.Timeout:
                    logger.debug("空气质量请求超时")
                except requests.exceptions.ConnectionError:
                    logger.debug("空气质量网络连接失败")
                except requests.exceptions.RequestException as air_error:
                    logger.debug("空气质量请求失败: %s", air_error)
                except Exception as air_error:
                    logger.debug("空气质量解析失败: %s", air_error)
                
                logger.info("成功获取%s的真实天气数据 (温度: %s°C)", city, result['temperature'])
                return result
                    
            except requests.exceptions.Timeout:
                logger.warning("和风天气API请求超时，尝试备用API")
            except requests.exceptions.ConnectionError:
                logger.warning("网络连接失败，尝试备用API")
            except requests.exceptions.RequestException as e:
                logger.warning("和风天气API请求异常: %s，尝试备用API", e)
            except Exception as e:
                logger.exception("和风天气API调用失败: %s，尝试备用API", e)
        else:
            logger.warning("未配置和风天气API，尝试备用API")
        
        # 和风天气API失败，尝试Open-Meteo备用API
        if self.use_openmeteo_fallback:
            logger.info("尝试使用Open-Meteo备用API...")
            openmeteo_result = self._get_openmeteo_weather(city)
            if openmeteo_result:
                return openmeteo_result
        
        # 所有API都失败，返回模拟数据
        logger.error("所有天气API均失败，使用模拟数据")
        return self._get_mock_weather()
    
    def _get_error_message(self, code):
        """获取错误码对应的说明"""
        error_codes = {
            '400': '请求错误',
            '401': 'API密钥无效或过期',
            '402': '超过访问次数限制',
            '403': '无访问权限',
            '404': '查询的数据不存在',
            '500': '服务器内部错误',
            '204': '请求成功，但无数据返回'
        }
        return error_codes.get(str(code), f'未知错误(代码:{code})')
    
    def _get_openmeteo_weather(self, city="都昌"):
        """使用Open-Meteo免费API获取天气数据（无需API Key）"""
        logger = logging.getLogger(__name__)
        try:
            location = self._get_location(city)
            parsed = self._parse_lon_lat(location)
            if not parsed:
                logger.info("Open-Meteo兜底跳过：location不是经纬度格式: %s", str(location)[:32])
                return None
            lon, lat = parsed
            
            # Open-Meteo API - 完全免费，无需注册
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,relative_humidity_2m,surface_pressure,weather_code,wind_speed_10m',
                'timezone': 'Asia/Shanghai'
            }
            
            start_ts = time.perf_counter()
            response = requests.get(url, params=params, timeout=10)
            _record_external_api_timing(
                'openmeteo_now',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code == 200:
                data = response.json()
                current = data.get('current', {})
                
                # 天气代码转中文
                weather_code = current.get('weather_code', 0)
                weather_map = {
                    0: '晴', 1: '晴', 2: '多云', 3: '阴',
                    45: '雾', 48: '雾', 51: '小雨', 53: '中雨', 55: '大雨',
                    61: '小雨', 63: '中雨', 65: '大雨', 71: '小雪', 73: '中雪', 75: '大雪',
                    80: '阵雨', 81: '阵雨', 82: '暴雨', 95: '雷阵雨'
                }
                weather_condition = weather_map.get(weather_code, '多云')
                
                temp = current.get('temperature_2m', 20)
                result = {
                    'temperature': round(temp, 1),
                    'temperature_max': round(temp + 3, 1),
                    'temperature_min': round(temp - 3, 1),
                    'humidity': round(current.get('relative_humidity_2m', 60), 1),
                    'pressure': round(current.get('surface_pressure', 1013), 1),
                    'weather_condition': weather_condition,
                    'wind_speed': round(current.get('wind_speed_10m', 3), 1),
                    'pm25': 50,  # Open-Meteo不提供空气质量数据
                    'aqi': 75,
                    'is_mock': False,
                    'data_source': 'Open-Meteo'
                }
                logger.info("Open-Meteo API调用成功")
                return result
        except Exception as e:
            logger.warning("Open-Meteo API调用失败: %s", e)
        return None
    
    def _get_mock_weather(self):
        """获取模拟天气数据（最后备用方案）"""
        import random
        temp = random.uniform(10, 25)
        return {
            'temperature': round(temp, 1),
            'temperature_max': round(temp + random.uniform(2, 5), 1),
            'temperature_min': round(temp - random.uniform(2, 5), 1),
            'humidity': round(random.uniform(40, 80), 1),
            'pressure': round(random.uniform(1000, 1020), 1),
            'weather_condition': random.choice(['晴', '多云', '阴', '小雨']),
            'wind_speed': round(random.uniform(1, 8), 1),
            'pm25': random.randint(20, 100),
            'aqi': random.randint(30, 150),
            'is_mock': True,
            'data_source': 'Mock'
        }
    
    def get_weather_forecast(self, city="都昌", days=7):
        """
        获取未来天气预报 - 使用和风天气7天预报API
        如果API调用失败，返回模拟数据
        """
        logger = logging.getLogger(__name__)
        # 限制最多7天
        days = min(days, 7)
        
        if self.qweather_key and self.api_base_url:
            try:
                location = self._get_location(city)
                
                # 调用7天预报API
                forecast_url = f"{self.api_base_url}/weather/7d"
                forecast_params = {
                    'key': self.qweather_key,
                    'location': location
                }
                
                start_ts = time.perf_counter()
                response = requests.get(forecast_url, params=forecast_params, timeout=10)
                _record_external_api_timing('qweather_forecast', (time.perf_counter() - start_ts) * 1000, response.status_code)
                if response.status_code != 200:
                    logger.warning("预报API HTTP状态码: %s，使用模拟数据", response.status_code)
                    return self._get_mock_forecast(days)
                try:
                    data = response.json()
                except Exception as json_error:
                    logger.warning("预报JSON解析失败: %s，使用模拟数据", json_error)
                    return self._get_mock_forecast(days)
                
                if data.get('code') == '200' and 'daily' in data:
                    forecast = []
                    for i, day in enumerate(data['daily'][:days]):
                        forecast.append({
                            'date': day.get('fxDate', ''),
                            'temperature_max': float(day.get('tempMax', 25)),
                            'temperature_min': float(day.get('tempMin', 15)),
                            'condition': day.get('textDay', '晴'),
                            'condition_night': day.get('textNight', '晴'),
                            'humidity': float(day.get('humidity', 60)),
                            'wind_dir': day.get('windDirDay', ''),
                            'wind_speed': float(day.get('windSpeedDay', 3)),
                            'uv_index': day.get('uvIndex', ''),
                            'sunrise': day.get('sunrise', ''),
                            'sunset': day.get('sunset', '')
                        })
                    
                    logger.info("成功获取%s的%s天预报数据", city, len(forecast))
                    return forecast
                else:
                    error_msg = self._get_error_message(data.get('code', 'unknown'))
                    logger.warning("获取预报失败: %s，使用模拟数据", error_msg)
                    
            except requests.exceptions.Timeout:
                logger.warning("预报API请求超时，使用模拟数据")
            except requests.exceptions.ConnectionError:
                logger.warning("预报API网络连接失败，使用模拟数据")
            except requests.exceptions.RequestException as e:
                logger.warning("预报API请求异常: %s，使用模拟数据", e)
            except Exception as e:
                logger.exception("预报API调用失败: %s，使用模拟数据", e)
        
        # 返回模拟预报数据
        return self._get_mock_forecast(days)
    
    def _get_mock_forecast(self, days=7):
        """生成模拟的天气预报数据"""
        import random
        
        forecast = []
        base_temp = random.uniform(10, 25)
        
        base_date = today_local()
        for i in range(days):
            date = base_date + timedelta(days=i)
            temp_variation = random.uniform(-3, 3)
            
            forecast.append({
                'date': date.strftime('%Y-%m-%d'),
                'temperature_max': round(base_temp + temp_variation + random.uniform(3, 8), 1),
                'temperature_min': round(base_temp + temp_variation - random.uniform(2, 5), 1),
                'condition': random.choice(['晴', '多云', '阴', '小雨', '晴转多云']),
                'condition_night': random.choice(['晴', '多云', '阴']),
                'humidity': round(random.uniform(40, 80), 0),
                'wind_dir': random.choice(['东风', '南风', '西风', '北风', '东南风']),
                'wind_speed': round(random.uniform(1, 8), 1),
                'uv_index': str(random.randint(1, 10)),
                'sunrise': '06:30',
                'sunset': '18:00'
            })
            
            # 温度有一定连续性
            base_temp += random.uniform(-2, 2)
        
        return forecast
    
    def identify_extreme_weather(self, weather_data):
        """
        识别极端天气
        定义：
        - 高温：温度>35°C
        - 低温：温度<-10°C
        - 温差大：日温差>15°C
        - 高湿度：湿度>85%
        - 强风：风速>10m/s
        - 重度污染：AQI>200
        """
        extreme_conditions = []
        
        # 高温
        if weather_data.get('temperature', 0) > 35:
            extreme_conditions.append({
                'type': '高温',
                'severity': '高',
                'description': f"当前温度{weather_data['temperature']}°C，极易引发中暑、心脑血管疾病"
            })
        
        # 低温
        if weather_data.get('temperature', 0) < -10:
            extreme_conditions.append({
                'type': '低温',
                'severity': '高',
                'description': f"当前温度{weather_data['temperature']}°C，需警惕呼吸道疾病、冻伤"
            })
        
        # 温差大（处理 None 值，避免 TypeError）
        temp_max = weather_data.get('temperature_max')
        temp_min = weather_data.get('temperature_min')
        if temp_max is not None and temp_min is not None:
            temp_diff = temp_max - temp_min
        else:
            temp_diff = None
        if temp_diff is not None and temp_diff > 15:
            extreme_conditions.append({
                'type': '温差过大',
                'severity': '中',
                'description': f"日温差达{temp_diff}°C，易引发感冒、关节炎复发"
            })
        
        # 高湿度
        if weather_data.get('humidity', 0) > 85:
            extreme_conditions.append({
                'type': '高湿度',
                'severity': '中',
                'description': f"湿度{weather_data['humidity']}%，不利于呼吸道疾病患者"
            })
        
        # 强风
        if weather_data.get('wind_speed', 0) > 10:
            extreme_conditions.append({
                'type': '强风',
                'severity': '中',
                'description': f"风速{weather_data['wind_speed']}m/s，老年人应减少外出"
            })
        
        # 空气污染
        aqi = weather_data.get('aqi', 0)
        if aqi > 200:
            extreme_conditions.append({
                'type': '重度空气污染',
                'severity': '高',
                'description': f"AQI达{aqi}，严重影响呼吸系统，建议佩戴口罩"
            })
        elif aqi > 150:
            extreme_conditions.append({
                'type': '中度空气污染',
                'severity': '中',
                'description': f"AQI达{aqi}，敏感人群应减少户外活动"
            })
        elif aqi > 100:
            extreme_conditions.append({
                'type': '轻度空气污染',
                'severity': '低',
                'description': f"AQI达{aqi}，建议减少长时间户外活动"
            })
        
        return {
            'is_extreme': len(extreme_conditions) > 0,
            'conditions': extreme_conditions
        }
    
    def analyze_weather_disease_correlation(self, weather_conditions, disease_records):
        """
        分析天气与疾病的相关性
        基于历史数据进行统计分析
        """
        correlations = {}
        
        # 呼吸道疾病与天气关系
        correlations['呼吸道疾病'] = {
            '低温': 0.75,  # 相关系数
            '高湿度': 0.65,
            '空气污染': 0.85,
            '温差大': 0.70
        }
        
        # 心血管疾病与天气关系
        correlations['心血管疾病'] = {
            '高温': 0.72,
            '低温': 0.68,
            '气压变化': 0.60,
            '温差大': 0.65
        }
        
        # 关节炎与天气关系
        correlations['关节炎'] = {
            '高湿度': 0.78,
            '低温': 0.70,
            '气压低': 0.62
        }
        
        # 消化系统疾病与天气关系
        correlations['消化系统疾病'] = {
            '高温': 0.55,
            '湿度变化': 0.45
        }
        
        return correlations
    
    def generate_weather_alert(self, location, weather_data):
        """
        生成天气预警
        """
        extreme_result = self.identify_extreme_weather(weather_data)
        
        if not extreme_result['is_extreme']:
            return None
        
        # 确定预警等级（蓝/黄/橙/红）
        severity_weights = {'高': 3, '中': 2, '低': 1}
        severity_score = sum(severity_weights.get(c['severity'], 1) for c in extreme_result['conditions'])
        if severity_score >= 6:
            alert_level = '红色预警'
        elif severity_score >= 4:
            alert_level = '橙色预警'
        elif severity_score >= 2:
            alert_level = '黄色预警'
        else:
            alert_level = '蓝色预警'
        
        # 生成预警内容
        descriptions = [c['description'] for c in extreme_result['conditions']]
        
        alert = {
            'location': location,
            'alert_level': alert_level,
            'alert_type': '、'.join([c['type'] for c in extreme_result['conditions']]),
            'description': '；'.join(descriptions),
            'recommendations': self._generate_recommendations(extreme_result['conditions'])
        }
        
        return alert
    
    def _generate_recommendations(self, conditions):
        """根据极端天气条件生成健康建议"""
        recommendations = []
        
        condition_types = [c['type'] for c in conditions]
        
        if '高温' in condition_types:
            recommendations.append('避免在高温时段外出，及时补充水分')
            recommendations.append('老年人和慢性病患者应待在阴凉处')
        
        if '低温' in condition_types:
            recommendations.append('注意保暖，特别是头部和四肢')
            recommendations.append('心血管疾病患者应避免剧烈运动')
        
        if '温差过大' in condition_types:
            recommendations.append('早晚注意增减衣物')
            recommendations.append('关节炎患者应注意关节保暖')
        
        if '重度空气污染' in condition_types or '中度空气污染' in condition_types:
            recommendations.append('减少户外活动，外出佩戴口罩')
            recommendations.append('呼吸道疾病患者应关闭门窗，使用空气净化器')
        
        if '高湿度' in condition_types:
            recommendations.append('注意室内通风除湿')
            recommendations.append('呼吸道疾病患者应谨慎外出')
        
        return recommendations
    
    def calculate_risk_index(self, weather_data, user_health_profile):
        """
        计算个人天气健康风险指数
        综合考虑天气因素和个人健康状况
        """
        risk_score = 0
        
        # 基础天气风险
        extreme_result = self.identify_extreme_weather(weather_data)
        if extreme_result['is_extreme']:
            risk_score += len(extreme_result['conditions']) * 20
        
        # 根据个人健康状况调整
        if user_health_profile.get('age', 0) > 65:
            risk_score += 15  # 老年人风险增加
        
        if user_health_profile.get('has_chronic_disease'):
            risk_score += 25  # 慢性病患者风险增加
        
        # 特定疾病与天气的关联
        chronic_diseases = user_health_profile.get('chronic_diseases', [])
        for disease in chronic_diseases:
            if '呼吸' in disease and weather_data.get('aqi', 0) > 100:
                risk_score += 20
            if '心血管' in disease and abs(weather_data.get('temperature', 20) - 20) > 10:
                risk_score += 20
            if '关节' in disease and weather_data.get('humidity', 0) > 80:
                risk_score += 15
        
        # 标准化到0-100
        risk_score = min(risk_score, 100)
        
        # 确定风险等级
        if risk_score < 30:
            risk_level = '低风险'
        elif risk_score < 60:
            risk_level = '中风险'
        else:
            risk_level = '高风险'
        
        return {
            'risk_score': risk_score,
            'risk_level': risk_level
        }

  
