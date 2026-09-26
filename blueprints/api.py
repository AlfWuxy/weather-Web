# -*- coding: utf-8 -*-
"""API routes.

每个接口在表里声明一行，统一注册两个地址：
- `/api/v1/<path>`，endpoint `api_v1_<name>`，挂登录校验与限流；
- `/api/<path>`（兼容旧地址），endpoint `api_<name>`，转调 v1 视图，因此同样受登录与限流约束。

视图函数名与改造前逐字一致（Flask-Limiter 以函数全名区分限流桶），
既有客户端、url_for 与限流计数都不受影响。
"""
from flask import Blueprint, current_app
from flask_login import login_required

from core.extensions import limiter
from core.security import rate_limit_key
from services import api_service

bp = Blueprint('api', __name__)


@bp.before_request
def _api_csrf_protect():
    return api_service._api_csrf_protect()


# 限流配置: (配置项, 默认值)
WEATHER = ('RATE_LIMIT_WEATHER', '120 per minute')
ML = ('RATE_LIMIT_ML', '60 per minute')
FORECAST = ('RATE_LIMIT_FORECAST', '60 per minute')
CHRONIC = ('RATE_LIMIT_CHRONIC', '60 per minute')
AI = ('RATE_LIMIT_AI', '30 per hour')

POST = ('POST',)

# (endpoint 名, 路径, 处理函数, HTTP 方法, 需要登录, 限流, 保留兼容地址)
API_ROUTES = [
    # 天气/社区基础
    ('current_weather', 'weather/current', api_service._api_current_weather, None, False, WEATHER, True),
    ('weather_nowcast', 'weather/nowcast', api_service._api_weather_nowcast, None, False, WEATHER, True),
    ('community_risk_map', 'community/risk-map', api_service._api_community_risk_map, None, False, None, True),
    ('disease_weather_stats', 'statistics/disease-weather', api_service._api_disease_weather_stats,
     None, False, None, True),
    # ML 预测
    ('ml_predict', 'ml/predict', api_service._api_ml_predict, POST, True, ML, True),
    ('ml_predict_community', 'ml/predict-community', api_service._api_ml_predict_community, POST, True, ML, True),
    ('ml_status', 'ml/status', api_service._api_ml_status, None, False, None, True),
    # DLNM 风险函数
    ('dlnm_risk', 'dlnm/risk', api_service._api_dlnm_risk, POST, True, None, True),
    ('dlnm_summary', 'dlnm/summary', api_service._api_dlnm_summary, None, False, None, True),
    # 7 天与单日预测
    ('forecast_7day', 'forecast/7day', api_service._api_forecast_7day, POST, True, FORECAST, True),
    ('forecast_daily', 'forecast/daily', api_service._api_forecast_daily, POST, True, FORECAST, True),
    # 社区风险地图与脆弱性
    ('community_risk_map_v2', 'community/risk-map-v2', api_service._api_community_risk_map_v2,
     POST, True, None, True),
    ('community_vulnerability', 'community/vulnerability/<community_name>',
     api_service._api_community_vulnerability, None, False, None, True),
    ('community_list', 'community/list', api_service._api_community_list, None, False, None, True),
    # 慢病风险
    ('chronic_individual', 'chronic/individual', api_service._api_chronic_individual, POST, True, CHRONIC, True),
    ('chronic_population', 'chronic/population', api_service._api_chronic_population, POST, True, None, True),
    # AI 问答
    ('ai_ask', 'ai/ask', api_service._api_ai_ask, POST, True, AI, True),
    ('chronic_rules_version', 'chronic/rules-version', api_service._api_chronic_rules_version,
     None, False, None, True),
    # 综合预警
    ('comprehensive_alert', 'alert/comprehensive', api_service._api_comprehensive_alert,
     POST, True, FORECAST, True),
    # 试点埋点（只有 v1 地址）
    ('events', 'events', api_service._api_usage_event, POST, True, None, False),
]


def _named(func, name):
    func.__name__ = name
    func.__qualname__ = name
    return func


def _register(name, path, handler, methods, needs_login, limit, with_compat):
    v1_name = f'api_v1_{name}'
    view = _named(lambda **kwargs: handler(**kwargs), v1_name)
    if limit:
        config_key, default = limit
        view = limiter.limit(
            lambda: current_app.config.get(config_key, default), key_func=rate_limit_key
        )(view)
    if needs_login:
        view = login_required(view)
    bp.add_url_rule(f'/api/v1/{path}', endpoint=v1_name, view_func=view, methods=methods)

    if with_compat:
        compat_name = f'api_{name}'
        compat = _named(lambda **kwargs: view(**kwargs), compat_name)
        bp.add_url_rule(f'/api/{path}', endpoint=compat_name, view_func=compat, methods=methods)


for _route in API_ROUTES:
    _register(*_route)
