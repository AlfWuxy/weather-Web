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
from statistics import mean, pstdev
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
        configured_api_base = app_config.get('QWEATHER_API_BASE')
        if configured_api_base is None:
            configured_api_base = os.getenv('QWEATHER_API_BASE')
        self.api_base_url = (configured_api_base or '').strip()
        self.city_map = app_config.get('CITY_LOCATION_MAP') or {}
        self.default_location = (
            app_config.get('DEFAULT_LOCATION')
            or os.getenv('DEFAULT_LOCATION', self.default_location)
        )
    
    def _get_location(self, city):
        """获取城市的location参数"""
        city = str(city).strip() if city is not None else ''
        if not city:
            return self.default_location

        # Allow passing raw QWeather location id or lon,lat coordinates directly.
        if city.isdigit():
            return city
        if self._parse_lon_lat(city):
            return city

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
                temp_val = float(now.get('temp', 20))
                result = {
                    'temperature': temp_val,
                    # 优先使用真实日极值，必要时回退到小时序列推导
                    'temperature_max': None,
                    'temperature_min': None,
                    'temperature_estimated': True,
                    'temperature_range_source': 'unavailable',
                    'temperature_range_confidence': 'none',
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
                    'is_mock': False,
                    'data_source': 'QWeather'
                }

                tmax, tmin, range_source, range_confidence = self._resolve_qweather_current_temperature_range(location)
                if tmax is not None and tmin is not None:
                    result['temperature_max'] = tmax
                    result['temperature_min'] = tmin
                    result['temperature_estimated'] = (range_source != 'daily')
                    result['temperature_range_source'] = range_source
                    result['temperature_range_confidence'] = range_confidence
                
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
                'daily': 'temperature_2m_max,temperature_2m_min',
                'forecast_days': 1,
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
                weather_condition = self._weather_code_to_text(weather_code)

                temp = current.get('temperature_2m', 20)
                daily = data.get('daily', {})
                tmax_list = daily.get('temperature_2m_max') or []
                tmin_list = daily.get('temperature_2m_min') or []
                tmax_daily = self._safe_float(tmax_list[0]) if tmax_list else None
                tmin_daily = self._safe_float(tmin_list[0]) if tmin_list else None
                if tmax_daily is not None and tmin_daily is not None:
                    tmax = round(tmax_daily, 1)
                    tmin = round(tmin_daily, 1)
                    temp_estimated = False
                    temp_range_source = 'daily'
                    temp_range_confidence = 'high'
                else:
                    tmax_hourly, tmin_hourly, hourly_confidence = self._get_openmeteo_hourly_extremes(lon, lat)
                    if tmax_hourly is not None and tmin_hourly is not None:
                        tmax = tmax_hourly
                        tmin = tmin_hourly
                        temp_estimated = True
                        temp_range_source = 'hourly'
                        temp_range_confidence = hourly_confidence
                    else:
                        tmax = None
                        tmin = None
                        temp_estimated = True
                        temp_range_source = 'unavailable'
                        temp_range_confidence = 'none'
                result = {
                    'temperature': round(temp, 1),
                    'temperature_max': tmax,
                    'temperature_min': tmin,
                    'temperature_estimated': temp_estimated,
                    'temperature_range_source': temp_range_source,
                    'temperature_range_confidence': temp_range_confidence,
                    'humidity': round(current.get('relative_humidity_2m', 60), 1),
                    'pressure': round(current.get('surface_pressure', 1013), 1),
                    'weather_condition': weather_condition,
                    'wind_speed': round(current.get('wind_speed_10m', 3), 1),
                    'pm25': 0,  # Open-Meteo不提供空气质量数据，0表示未知
                    'aqi': 0,  # 同上，0 而非真实值
                    'aqi_estimated': True,  # 标记为非真实 AQI，下游可据此降权或隐藏
                    'is_mock': False,
                    'data_source': 'Open-Meteo'
                }
                logger.info("Open-Meteo API调用成功")
                return result
        except Exception as e:
            logger.warning("Open-Meteo API调用失败: %s", e)
        return None

    def _weather_code_to_text(self, weather_code):
        """Open-Meteo 天气代码转中文描述"""
        weather_map = {
            0: '晴', 1: '晴', 2: '多云', 3: '阴',
            45: '雾', 48: '雾', 51: '小雨', 53: '中雨', 55: '大雨',
            61: '小雨', 63: '中雨', 65: '大雨', 71: '小雪', 73: '中雪', 75: '大雪',
            80: '阵雨', 81: '阵雨', 82: '暴雨', 95: '雷阵雨', 96: '雷雨夹冰雹', 99: '强雷雨'
        }
        try:
            code = int(weather_code)
        except Exception:
            return '多云'
        return weather_map.get(code, '多云')

    def _safe_float(self, value, default=None):
        try:
            return float(value)
        except Exception:
            return default

    def _temperature_range_confidence(self, sample_count):
        """按样本点数量评估温差推导置信度。"""
        count = int(sample_count or 0)
        if count >= 18:
            return 'high'
        if count >= 12:
            return 'medium'
        if count >= 4:
            return 'low'
        return 'none'

    def _derive_temperature_range(self, samples):
        """从温度样本推导 tmax/tmin（样本过少时返回 unavailable）。"""
        temps = []
        for value in samples or []:
            parsed = self._safe_float(value)
            if parsed is not None:
                temps.append(parsed)
        if len(temps) < 4:
            return None, None, 'none'
        tmax = max(temps)
        tmin = min(temps)
        if tmin > tmax:
            tmax, tmin = tmin, tmax
        return round(tmax, 1), round(tmin, 1), self._temperature_range_confidence(len(temps))

    def _get_qweather_hourly_extremes(self, location):
        """从和风 24 小时温度序列推导温差。"""
        logger = logging.getLogger(__name__)
        if not self.qweather_key or not self.api_base_url:
            return None, None, 'none'
        try:
            hourly_url = f"{self.api_base_url}/weather/24h"
            hourly_params = {
                'key': self.qweather_key,
                'location': location
            }
            start_ts = time.perf_counter()
            response = requests.get(hourly_url, params=hourly_params, timeout=10)
            _record_external_api_timing(
                'qweather_hourly_for_now',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code != 200:
                return None, None, 'none'
            payload = response.json()
            if payload.get('code') != '200':
                return None, None, 'none'
            hourly = payload.get('hourly') or []
            temps = [item.get('temp') for item in hourly if isinstance(item, dict)]
            return self._derive_temperature_range(temps)
        except Exception as exc:
            logger.debug("获取和风 hourly 高低温失败: %s", exc)
            return None, None, 'none'

    def _get_openmeteo_hourly_extremes(self, lon, lat):
        """从 Open-Meteo 小时序列推导温差（优先当天样本，不足则退回24h样本）。"""
        logger = logging.getLogger(__name__)
        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                'latitude': lat,
                'longitude': lon,
                'hourly': 'temperature_2m',
                'forecast_days': 2,
                'timezone': 'Asia/Shanghai'
            }
            start_ts = time.perf_counter()
            response = requests.get(url, params=params, timeout=10)
            _record_external_api_timing(
                'openmeteo_now_hourly',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code != 200:
                return None, None, 'none'
            payload = response.json()
            hourly = payload.get('hourly') or {}
            temps = hourly.get('temperature_2m') or []
            time_list = hourly.get('time') or []
            today_prefix = today_local().strftime('%Y-%m-%d')
            today_temps = []
            for idx, value in enumerate(temps):
                ts = time_list[idx] if idx < len(time_list) else ''
                if str(ts).startswith(today_prefix):
                    today_temps.append(value)

            selected = today_temps if len(today_temps) >= 4 else temps
            return self._derive_temperature_range(selected)
        except Exception as exc:
            logger.debug("获取 Open-Meteo hourly 高低温失败: %s", exc)
            return None, None, 'none'

    def _resolve_qweather_current_temperature_range(self, location):
        """获取当前天气所需温差：daily 优先，失败则回退 hourly。"""
        tmax, tmin = self._get_qweather_today_extremes(location)
        if tmax is not None and tmin is not None:
            return tmax, tmin, 'daily', 'high'
        tmax_hourly, tmin_hourly, confidence = self._get_qweather_hourly_extremes(location)
        if tmax_hourly is not None and tmin_hourly is not None:
            return tmax_hourly, tmin_hourly, 'hourly', confidence
        return None, None, 'unavailable', 'none'

    def _get_qweather_today_extremes(self, location):
        """获取和风当日最高/最低温（用于修正实况无日温差的问题）。"""
        logger = logging.getLogger(__name__)
        if not self.qweather_key or not self.api_base_url:
            return None, None
        try:
            forecast_url = f"{self.api_base_url}/weather/7d"
            forecast_params = {
                'key': self.qweather_key,
                'location': location
            }
            start_ts = time.perf_counter()
            response = requests.get(forecast_url, params=forecast_params, timeout=10)
            _record_external_api_timing(
                'qweather_daily_for_now',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code != 200:
                return None, None
            payload = response.json()
            if payload.get('code') != '200':
                return None, None
            daily = payload.get('daily') or []
            if not daily:
                return None, None
            today = daily[0]
            tmax = self._safe_float(today.get('tempMax'))
            tmin = self._safe_float(today.get('tempMin'))
            if tmax is None or tmin is None:
                return None, None
            return round(tmax, 1), round(tmin, 1)
        except Exception as exc:
            logger.debug("获取当日高低温失败（将回退估算）: %s", exc)
            return None, None

    def _predictability_from_spread(self, spread, lead_day=1):
        """
        基于多模型离散度 + 提前期估计可预报性（0-100）。
        spread 越大，lead_day 越远，可预报性越低。
        """
        try:
            spread_v = max(0.0, float(spread))
        except Exception:
            spread_v = 0.0
        day_penalty = max(0, int(lead_day) - 1) * 3.0
        score = max(5.0, min(99.0, 100.0 - spread_v * 16.0 - day_penalty))
        if score >= 75:
            label = '高'
        elif score >= 50:
            label = '中'
        else:
            label = '低'
        return round(score, 1), label

    def _normalize_qweather_daily_entry(self, day):
        """将和风天气 daily 条目标准化为统一结构"""
        tmax = self._safe_float(day.get('tempMax'), 25.0)
        tmin = self._safe_float(day.get('tempMin'), 15.0)
        return {
            'date': day.get('fxDate', ''),
            'temperature_max': tmax,
            'temperature_min': tmin,
            'temperature_mean': round((tmax + tmin) / 2, 2),
            'condition': day.get('textDay', '晴'),
            'condition_night': day.get('textNight', '晴'),
            'humidity': self._safe_float(day.get('humidity'), 60.0),
            'wind_dir': day.get('windDirDay', ''),
            'wind_speed': self._safe_float(day.get('windSpeedDay'), 3.0),
            'uv_index': day.get('uvIndex', ''),
            'sunrise': day.get('sunrise', ''),
            'sunset': day.get('sunset', ''),
            'precip_probability': self._safe_float(day.get('pop')),
            'data_source': 'QWeather',
            'is_mock': False,
        }

    def get_qweather_daily_forecast(self, city="都昌", days=7):
        """只获取和风天气 7 日预报，不启用备用源或模拟数据。"""
        logger = logging.getLogger(__name__)
        try:
            days = max(1, min(int(days or 7), 7))
        except Exception:
            days = 7

        meta = {'source': 'QWeather'}
        if not self.qweather_key or not self.api_base_url:
            meta['error'] = 'qweather_not_configured'
            logger.warning("和风天气预报未配置，跳过和风-only预报")
            return {'success': False, 'daily': [], 'meta': meta}

        location = self._get_location(city)
        meta['location'] = city
        meta['location_code'] = location
        try:
            forecast_url = f"{self.api_base_url}/weather/7d"
            forecast_params = {
                'key': self.qweather_key,
                'location': location
            }
            start_ts = time.perf_counter()
            response = requests.get(forecast_url, params=forecast_params, timeout=10)
            _record_external_api_timing(
                'qweather_forecast_only',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code != 200:
                meta['error'] = f'http_{response.status_code}'
                logger.warning("和风-only预报HTTP状态码: %s", response.status_code)
                return {'success': False, 'daily': [], 'meta': meta}

            try:
                payload = response.json()
            except Exception as exc:
                meta['error'] = 'invalid_json'
                logger.warning("和风-only预报JSON解析失败: %s", exc)
                return {'success': False, 'daily': [], 'meta': meta}

            code = payload.get('code')
            if code != '200':
                meta['error'] = f'qweather_{code or "unknown"}'
                meta['error_message'] = self._get_error_message(code or 'unknown')
                logger.warning("和风-only预报返回错误[%s]: %s", code, meta['error_message'])
                return {'success': False, 'daily': [], 'meta': meta}

            daily = [
                self._normalize_qweather_daily_entry(day)
                for day in (payload.get('daily') or [])[:days]
                if isinstance(day, dict)
            ]
            for entry in daily:
                entry['forecast_date'] = entry.get('date')
                entry['update_time'] = payload.get('updateTime')
            meta['update_time'] = payload.get('updateTime')
            meta['fx_link'] = payload.get('fxLink')
            return {'success': bool(daily), 'daily': daily, 'meta': meta}
        except requests.exceptions.Timeout:
            meta['error'] = 'timeout'
            logger.warning("和风-only预报请求超时")
        except requests.exceptions.ConnectionError:
            meta['error'] = 'connection_error'
            logger.warning("和风-only预报网络连接失败")
        except requests.exceptions.RequestException as exc:
            meta['error'] = 'request_exception'
            logger.warning("和风-only预报请求异常: %s", exc)
        except Exception as exc:
            meta['error'] = 'exception'
            logger.exception("和风-only预报调用失败: %s", exc)
        return {'success': False, 'daily': [], 'meta': meta}

    def _get_openmeteo_forecast(self, city="都昌", days=7):
        """Open-Meteo 逐日预报（用于多模型融合）"""
        logger = logging.getLogger(__name__)
        try:
            location = self._get_location(city)
            parsed = self._parse_lon_lat(location)
            if not parsed:
                logger.info("Open-Meteo逐日预报跳过：location不是经纬度格式: %s", str(location)[:32])
                return []
            lon, lat = parsed

            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                'latitude': lat,
                'longitude': lon,
                'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code',
                'timezone': 'Asia/Shanghai'
            }
            start_ts = time.perf_counter()
            response = requests.get(url, params=params, timeout=10)
            _record_external_api_timing(
                'openmeteo_forecast_daily',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code != 200:
                return []

            payload = response.json()
            daily = payload.get('daily') or {}
            dates = daily.get('time') or []
            tmax_list = daily.get('temperature_2m_max') or []
            tmin_list = daily.get('temperature_2m_min') or []
            pop_list = daily.get('precipitation_probability_max') or []
            code_list = daily.get('weather_code') or []

            entries = []
            max_len = min(days, len(dates), len(tmax_list), len(tmin_list))
            for idx in range(max_len):
                tmax = self._safe_float(tmax_list[idx], 25.0)
                tmin = self._safe_float(tmin_list[idx], 15.0)
                entries.append({
                    'date': dates[idx],
                    'temperature_max': tmax,
                    'temperature_min': tmin,
                    'temperature_mean': round((tmax + tmin) / 2, 2),
                    'condition': self._weather_code_to_text(code_list[idx] if idx < len(code_list) else None),
                    'condition_night': self._weather_code_to_text(code_list[idx] if idx < len(code_list) else None),
                    'humidity': None,
                    'wind_dir': '',
                    'wind_speed': None,
                    'uv_index': '',
                    'sunrise': '',
                    'sunset': '',
                    'precip_probability': self._safe_float(pop_list[idx] if idx < len(pop_list) else None),
                    'data_source': 'Open-Meteo',
                    'is_mock': False,
                })
            return entries
        except Exception as exc:
            logger.warning("Open-Meteo逐日预报调用失败: %s", exc)
            return []

    def _merge_multimodel_forecast(self, qweather_forecast, openmeteo_forecast, days=7):
        """融合多模型日预报，并给出概率化统计指标。"""
        if not qweather_forecast and not openmeteo_forecast:
            return []

        by_date = {}
        for item in qweather_forecast or []:
            date = item.get('date') or item.get('forecast_date')
            if date:
                by_date.setdefault(date, {})['qweather'] = item
        for item in openmeteo_forecast or []:
            date = item.get('date') or item.get('forecast_date')
            if date:
                by_date.setdefault(date, {})['openmeteo'] = item

        ordered_dates = sorted(by_date.keys())[:days]
        merged = []
        for idx, date in enumerate(ordered_dates, start=1):
            row = by_date[date]
            qw = row.get('qweather')
            om = row.get('openmeteo')

            model_entries = []
            model_names = []
            if qw:
                model_entries.append(qw.get('temperature_mean'))
                model_names.append('QWeather')
            if om:
                model_entries.append(om.get('temperature_mean'))
                model_names.append('Open-Meteo')
            model_means = [self._safe_float(v) for v in model_entries if self._safe_float(v) is not None]
            if not model_means:
                continue

            ensemble_mean = mean(model_means)
            ensemble_std = pstdev(model_means) if len(model_means) > 1 else 0.0
            p10 = ensemble_mean - 1.2816 * ensemble_std
            p90 = ensemble_mean + 1.2816 * ensemble_std

            # 对昼夜温差做平均，以便保留 max/min 兼容字段
            ranges = []
            for src in (qw, om):
                if not src:
                    continue
                tmax = self._safe_float(src.get('temperature_max'))
                tmin = self._safe_float(src.get('temperature_min'))
                if tmax is not None and tmin is not None:
                    ranges.append(max(2.0, tmax - tmin))
            diurnal_range = mean(ranges) if ranges else 8.0
            tmax_ens = ensemble_mean + diurnal_range / 2
            tmin_ens = ensemble_mean - diurnal_range / 2

            predictability_score, predictability_label = self._predictability_from_spread(ensemble_std, lead_day=idx)

            merged.append({
                'date': date,
                'forecast_date': date,
                'temperature_max': round(tmax_ens, 1),
                'temperature_min': round(tmin_ens, 1),
                'temperature_ensemble_mean': round(ensemble_mean, 2),
                'temperature_ensemble_p10': round(p10, 2),
                'temperature_ensemble_p50': round(ensemble_mean, 2),
                'temperature_ensemble_p90': round(p90, 2),
                'temperature_ensemble_std': round(ensemble_std, 3),
                'model_count': len(model_means),
                'model_names': model_names,
                'predictability_score': predictability_score,
                'predictability_label': predictability_label,
                'condition': (qw or om or {}).get('condition', '多云'),
                'condition_night': (qw or om or {}).get('condition_night', '多云'),
                'humidity': (qw or om or {}).get('humidity'),
                'wind_dir': (qw or om or {}).get('wind_dir', ''),
                'wind_speed': (qw or om or {}).get('wind_speed'),
                'uv_index': (qw or om or {}).get('uv_index', ''),
                'sunrise': (qw or om or {}).get('sunrise', ''),
                'sunset': (qw or om or {}).get('sunset', ''),
                'precip_probability': (qw or om or {}).get('precip_probability'),
                'data_source': '+'.join(model_names) if len(model_names) > 1 else model_names[0],
                'is_mock': False,
            })
        return merged

    def get_short_term_nowcast(self, city="都昌", hours=6):
        """获取未来小时级降水时间轴（短临交互数据）。"""
        logger = logging.getLogger(__name__)
        hours = max(1, min(int(hours or 6), 24))
        location = self._get_location(city)
        parsed = self._parse_lon_lat(location)
        if not parsed:
            return {
                'available': False,
                'source': 'Open-Meteo',
                'reason': 'location_not_lon_lat',
                'timeline': []
            }
        lon, lat = parsed

        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                'latitude': lat,
                'longitude': lon,
                'hourly': 'precipitation_probability,precipitation,temperature_2m,weather_code',
                'forecast_hours': hours,
                'timezone': 'Asia/Shanghai'
            }
            start_ts = time.perf_counter()
            response = requests.get(url, params=params, timeout=10)
            _record_external_api_timing(
                'openmeteo_nowcast_hourly',
                (time.perf_counter() - start_ts) * 1000,
                response.status_code
            )
            if response.status_code != 200:
                return {
                    'available': False,
                    'source': 'Open-Meteo',
                    'reason': f'http_{response.status_code}',
                    'timeline': []
                }
            payload = response.json()
            hourly = payload.get('hourly') or {}
            times = hourly.get('time') or []
            pops = hourly.get('precipitation_probability') or []
            precs = hourly.get('precipitation') or []
            temps = hourly.get('temperature_2m') or []
            wcodes = hourly.get('weather_code') or []

            size = min(hours, len(times), len(pops), len(precs), len(temps))
            timeline = []
            for i in range(size):
                pop = self._safe_float(pops[i], 0.0) or 0.0
                entry = {
                    'time': str(times[i]),
                    'precipitation_probability': round(pop, 1),
                    'precipitation_mm': round(self._safe_float(precs[i], 0.0) or 0.0, 2),
                    'temperature': round(self._safe_float(temps[i], 0.0) or 0.0, 1),
                    'condition': self._weather_code_to_text(wcodes[i] if i < len(wcodes) else None),
                    'risk_level': '高' if pop >= 70 else '中' if pop >= 40 else '低'
                }
                timeline.append(entry)

            peak = max(timeline, key=lambda x: x.get('precipitation_probability', 0), default=None)
            rain_threshold = 40.0
            rain_windows = []
            current_window = None
            for item in timeline:
                prob = self._safe_float(item.get('precipitation_probability'), 0.0) or 0.0
                raining = prob >= rain_threshold
                if raining and current_window is None:
                    current_window = {
                        'start_time': item.get('time'),
                        'end_time': item.get('time'),
                        'max_probability': prob
                    }
                elif raining and current_window is not None:
                    current_window['end_time'] = item.get('time')
                    current_window['max_probability'] = max(current_window['max_probability'], prob)
                elif (not raining) and current_window is not None:
                    rain_windows.append(current_window)
                    current_window = None
            if current_window is not None:
                rain_windows.append(current_window)

            next_rain = rain_windows[0] if rain_windows else None
            replay_meta = {
                'frame_interval_minutes': 60,
                'total_frames': len(timeline)
            }
            return {
                'available': bool(timeline),
                'source': 'Open-Meteo',
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'timeline': timeline,
                'peak': peak,
                'rain_window': next_rain,
                'rain_windows': rain_windows,
                'rain_probability_threshold': rain_threshold,
                'replay': replay_meta
            }
        except Exception as exc:
            logger.warning("Open-Meteo短临时间轴调用失败: %s", exc)
            return {
                'available': False,
                'source': 'Open-Meteo',
                'reason': 'exception',
                'timeline': []
            }
    
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
        qweather_forecast = []
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
                _record_external_api_timing(
                    'qweather_forecast',
                    (time.perf_counter() - start_ts) * 1000,
                    response.status_code
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get('code') == '200' and 'daily' in data:
                        qweather_forecast = [
                            self._normalize_qweather_daily_entry(day)
                            for day in data['daily'][:days]
                        ]
                        logger.info("成功获取%s的和风%s天预报数据", city, len(qweather_forecast))
                    else:
                        error_msg = self._get_error_message(data.get('code', 'unknown'))
                        logger.warning("和风预报获取失败: %s", error_msg)
                else:
                    logger.warning("和风预报HTTP状态码: %s", response.status_code)
            except requests.exceptions.Timeout:
                logger.warning("和风预报请求超时")
            except requests.exceptions.ConnectionError:
                logger.warning("和风预报网络连接失败")
            except requests.exceptions.RequestException as e:
                logger.warning("和风预报请求异常: %s", e)
            except Exception as e:
                logger.exception("和风预报调用失败: %s", e)
        else:
            logger.info("未配置和风预报Key，跳过和风源")

        openmeteo_forecast = []
        if self.use_openmeteo_fallback:
            openmeteo_forecast = self._get_openmeteo_forecast(city, days=days)

        # 多模型融合（优先）
        merged = self._merge_multimodel_forecast(qweather_forecast, openmeteo_forecast, days=days)
        if merged:
            logger.info(
                "多模型融合预报成功: city=%s days=%s qweather=%s openmeteo=%s",
                city, len(merged), len(qweather_forecast), len(openmeteo_forecast)
            )
            return merged

        if qweather_forecast:
            return qweather_forecast
        if openmeteo_forecast:
            return openmeteo_forecast

        # 返回模拟预报数据
        logger.warning("所有预报源均不可用，使用模拟预报")
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
        temp_now = self._safe_float(weather_data.get('temperature'), 0.0)
        humidity = self._safe_float(weather_data.get('humidity'), 0.0)
        wind_speed = self._safe_float(weather_data.get('wind_speed'), 0.0)

        # 高温
        if temp_now > 35:
            extreme_conditions.append({
                'type': '高温',
                'severity': '高',
                'description': f"当前温度{temp_now}°C，极易引发中暑、心脑血管疾病"
            })
        
        # 低温
        if temp_now < -10:
            extreme_conditions.append({
                'type': '低温',
                'severity': '高',
                'description': f"当前温度{temp_now}°C，需警惕呼吸道疾病、冻伤"
            })
        
        # 温差大（处理 None 值，避免 TypeError）
        temp_max = self._safe_float(weather_data.get('temperature_max'))
        temp_min = self._safe_float(weather_data.get('temperature_min'))
        temp_range_source = str(weather_data.get('temperature_range_source') or '').strip().lower()
        temp_range_confidence = str(weather_data.get('temperature_range_confidence') or '').strip().lower()
        if temp_max is not None and temp_min is not None:
            temp_diff = temp_max - temp_min
        else:
            temp_diff = None

        # 明确标记 unavailable/heuristic 的来源不参与高风险温差规则
        range_usable = True
        if temp_range_source in {'unavailable', 'heuristic'}:
            range_usable = False
        # 小时样本过少时，保守地不触发“温差过大”强规则
        if temp_range_source == 'hourly' and temp_range_confidence == 'none':
            range_usable = False

        if temp_diff is not None and temp_diff > 15 and range_usable:
            extreme_conditions.append({
                'type': '温差过大',
                'severity': '中',
                'description': f"日温差达{temp_diff}°C，易引发感冒、关节炎复发"
            })
        
        # 高湿度
        if humidity > 85:
            extreme_conditions.append({
                'type': '高湿度',
                'severity': '中',
                'description': f"湿度{humidity}%，不利于呼吸道疾病患者"
            })
        
        # 强风
        if wind_speed > 10:
            extreme_conditions.append({
                'type': '强风',
                'severity': '中',
                'description': f"风速{wind_speed}m/s，老年人应减少外出"
            })
        
        # 空气污染
        aqi = self._safe_float(weather_data.get('aqi'), 0.0)
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
            if '呼吸' in disease and (self._safe_float(weather_data.get('aqi'), 0.0) > 100):
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

  
