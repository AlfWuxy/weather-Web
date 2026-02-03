# -*- coding: utf-8 -*-
"""API routes."""
from flask import Blueprint, current_app
from flask_login import login_required

from core.extensions import limiter
from core.security import rate_limit_key
from services import api_service

bp = Blueprint('api', __name__)


@bp.before_request
def _api_csrf_protect():
    return api_service._api_csrf_protect()


# ======================== 天气/社区基础API ========================

@bp.route('/api/v1/weather/current', endpoint='api_v1_current_weather')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_WEATHER', '120 per minute'), key_func=rate_limit_key)
def api_v1_current_weather():
    """获取当前天气（v1）"""
    return api_service._api_current_weather()


@bp.route('/api/weather/current', endpoint='api_current_weather')
def api_current_weather():
    """获取当前天气（兼容）"""
    return api_v1_current_weather()


@bp.route('/api/v1/community/risk-map', endpoint='api_v1_community_risk_map')
def api_v1_community_risk_map():
    """获取社区风险地图数据（v1）"""
    return api_service._api_community_risk_map()


@bp.route('/api/community/risk-map', endpoint='api_community_risk_map')
def api_community_risk_map():
    """获取社区风险地图数据（兼容）"""
    return api_v1_community_risk_map()


@bp.route('/api/v1/statistics/disease-weather', endpoint='api_v1_disease_weather_stats')
def api_v1_disease_weather_stats():
    """疾病与天气相关性统计（v1）"""
    return api_service._api_disease_weather_stats()


@bp.route('/api/statistics/disease-weather', endpoint='api_disease_weather_stats')
def api_disease_weather_stats():
    """疾病与天气相关性统计（兼容）"""
    return api_v1_disease_weather_stats()


# ======================== ML预测API ========================

@bp.route('/api/v1/ml/predict', methods=['POST'], endpoint='api_v1_ml_predict')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ML', '60 per minute'), key_func=rate_limit_key)
def api_v1_ml_predict():
    """使用机器学习模型进行疾病风险预测（v1）"""
    return api_service._api_ml_predict()


@bp.route('/api/ml/predict', methods=['POST'], endpoint='api_ml_predict')
def api_ml_predict():
    """使用机器学习模型进行疾病风险预测（兼容）"""
    return api_v1_ml_predict()


@bp.route('/api/v1/ml/predict-community', methods=['POST'], endpoint='api_v1_ml_predict_community')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ML', '60 per minute'), key_func=rate_limit_key)
def api_v1_ml_predict_community():
    """使用机器学习模型进行社区风险预测（v1）"""
    return api_service._api_ml_predict_community()


@bp.route('/api/ml/predict-community', methods=['POST'], endpoint='api_ml_predict_community')
def api_ml_predict_community():
    """使用机器学习模型进行社区风险预测（兼容）"""
    return api_v1_ml_predict_community()


@bp.route('/api/v1/ml/status', endpoint='api_v1_ml_status')
def api_v1_ml_status():
    """获取ML模型状态（v1）"""
    return api_service._api_ml_status()


@bp.route('/api/ml/status', endpoint='api_ml_status')
def api_ml_status():
    """获取ML模型状态（兼容）"""
    return api_v1_ml_status()


# ======================== DLNM风险预测API ========================

@bp.route('/api/v1/dlnm/risk', methods=['POST'], endpoint='api_v1_dlnm_risk')
@login_required
def api_v1_dlnm_risk():
    """DLNM风险函数计算（v1）"""
    return api_service._api_dlnm_risk()


@bp.route('/api/dlnm/risk', methods=['POST'], endpoint='api_dlnm_risk')
def api_dlnm_risk():
    """DLNM风险函数计算（兼容）"""
    return api_v1_dlnm_risk()


@bp.route('/api/v1/dlnm/summary', endpoint='api_v1_dlnm_summary')
def api_v1_dlnm_summary():
    """获取DLNM模型摘要（v1）"""
    return api_service._api_dlnm_summary()


@bp.route('/api/dlnm/summary', endpoint='api_dlnm_summary')
def api_dlnm_summary():
    """获取DLNM模型摘要（兼容）"""
    return api_v1_dlnm_summary()


# ======================== 7天预测API ========================

@bp.route('/api/v1/forecast/7day', methods=['POST'], endpoint='api_v1_forecast_7day')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_FORECAST', '60 per minute'), key_func=rate_limit_key)
def api_v1_forecast_7day():
    """获取未来7天健康预测（v1）"""
    return api_service._api_forecast_7day()


@bp.route('/api/forecast/7day', methods=['POST'], endpoint='api_forecast_7day')
def api_forecast_7day():
    """获取未来7天健康预测（兼容）"""
    return api_v1_forecast_7day()


@bp.route('/api/v1/forecast/daily', methods=['POST'], endpoint='api_v1_forecast_daily')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_FORECAST', '60 per minute'), key_func=rate_limit_key)
def api_v1_forecast_daily():
    """获取单日门诊预测（v1）"""
    return api_service._api_forecast_daily()


@bp.route('/api/forecast/daily', methods=['POST'], endpoint='api_forecast_daily')
def api_forecast_daily():
    """获取单日门诊预测（兼容）"""
    return api_v1_forecast_daily()


# ======================== 社区风险地图API ========================

@bp.route('/api/v1/community/risk-map-v2', methods=['POST'], endpoint='api_v1_community_risk_map_v2')
@login_required
def api_v1_community_risk_map_v2():
    """获取社区风险地图数据（改进版v1）"""
    return api_service._api_community_risk_map_v2()


@bp.route('/api/community/risk-map-v2', methods=['POST'], endpoint='api_community_risk_map_v2')
def api_community_risk_map_v2():
    """获取社区风险地图数据（改进版兼容）"""
    return api_v1_community_risk_map_v2()


@bp.route('/api/v1/community/vulnerability/<community_name>', endpoint='api_v1_community_vulnerability')
def api_v1_community_vulnerability(community_name):
    """获取单个社区脆弱性指数（v1）"""
    return api_service._api_community_vulnerability(community_name)


@bp.route('/api/community/vulnerability/<community_name>', endpoint='api_community_vulnerability')
def api_community_vulnerability(community_name):
    """获取单个社区脆弱性指数（兼容）"""
    return api_v1_community_vulnerability(community_name)


@bp.route('/api/v1/community/list', endpoint='api_v1_community_list')
def api_v1_community_list():
    """获取所有社区列表及脆弱性（v1）"""
    return api_service._api_community_list()


@bp.route('/api/community/list', endpoint='api_community_list')
def api_community_list():
    """获取所有社区列表及脆弱性（兼容）"""
    return api_v1_community_list()


# ======================== 慢病风险预测API ========================

@bp.route('/api/v1/chronic/individual', methods=['POST'], endpoint='api_v1_chronic_individual')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CHRONIC', '60 per minute'), key_func=rate_limit_key)
def api_v1_chronic_individual():
    """个体慢病风险预测（v1）"""
    return api_service._api_chronic_individual()


@bp.route('/api/chronic/individual', methods=['POST'], endpoint='api_chronic_individual')
def api_chronic_individual():
    """个体慢病风险预测（兼容）"""
    return api_v1_chronic_individual()


@bp.route('/api/v1/chronic/population', methods=['POST'], endpoint='api_v1_chronic_population')
def api_v1_chronic_population():
    """人群分层慢病风险预测（v1）"""
    return api_service._api_chronic_population()


@bp.route('/api/chronic/population', methods=['POST'], endpoint='api_chronic_population')
def api_chronic_population():
    """人群分层慢病风险预测（兼容）"""
    return api_v1_chronic_population()


# ======================== AI问答API ========================

@bp.route('/api/v1/ai/ask', methods=['POST'], endpoint='api_v1_ai_ask')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_AI', '30 per hour'), key_func=rate_limit_key)
def api_v1_ai_ask():
    """AI问答接口（v1）"""
    return api_service._api_ai_ask()


@bp.route('/api/ai/ask', methods=['POST'], endpoint='api_ai_ask')
def api_ai_ask():
    """AI问答接口（兼容）"""
    return api_v1_ai_ask()


@bp.route('/api/v1/chronic/rules-version', endpoint='api_v1_chronic_rules_version')
def api_v1_chronic_rules_version():
    """获取慢病规则库版本（v1）"""
    return api_service._api_chronic_rules_version()


@bp.route('/api/chronic/rules-version', endpoint='api_chronic_rules_version')
def api_chronic_rules_version():
    """获取慢病规则库版本（兼容）"""
    return api_v1_chronic_rules_version()


# ======================== 综合预警API ========================

@bp.route('/api/v1/alert/comprehensive', methods=['POST'], endpoint='api_v1_comprehensive_alert')
def api_v1_comprehensive_alert():
    """获取综合健康预警（v1）"""
    return api_service._api_comprehensive_alert()


@bp.route('/api/alert/comprehensive', methods=['POST'], endpoint='api_comprehensive_alert')
def api_comprehensive_alert():
    """获取综合健康预警（兼容）"""
    return api_v1_comprehensive_alert()
