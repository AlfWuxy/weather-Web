# -*- coding: utf-8 -*-
"""API routes."""
from flask import Blueprint, current_app, request
from flask_login import current_user, login_required

from core.extensions import limiter
from core.security import rate_limit_key, reject_guest
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


@bp.route('/api/v1/weather/nowcast', endpoint='api_v1_weather_nowcast')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_WEATHER', '120 per minute'), key_func=rate_limit_key)
def api_v1_weather_nowcast():
    """获取短临小时级降水时间轴（v1）"""
    return api_service._api_weather_nowcast()


@bp.route('/api/weather/nowcast', endpoint='api_weather_nowcast')
def api_weather_nowcast():
    """获取短临小时级降水时间轴（兼容）"""
    return api_v1_weather_nowcast()


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
@reject_guest  # 高成本：游客烧配额
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
@reject_guest  # 高成本：游客烧配额
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
@reject_guest  # 高成本：游客烧配额
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
@reject_guest  # 高成本：和风/算力
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_FORECAST', '60 per minute'), key_func=rate_limit_key)
def api_v1_forecast_7day():
    """获取未来7天健康预测（v1）"""
    return api_service._api_forecast_7day()


@bp.route('/api/forecast/7day', methods=['POST'], endpoint='api_forecast_7day')
def api_forecast_7day():
    """获取未来7天健康预测（兼容）"""
    return api_v1_forecast_7day()


@bp.route('/api/v1/forecast/daily', methods=['POST'], endpoint='api_v1_forecast_daily')
@login_required
@reject_guest  # 高成本：游客烧配额
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
@reject_guest  # 计算接口：正式用户
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
@reject_guest  # 高成本：游客烧配额
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CHRONIC', '60 per minute'), key_func=rate_limit_key)
def api_v1_chronic_individual():
    """个体慢病风险预测（v1）"""
    return api_service._api_chronic_individual()


@bp.route('/api/chronic/individual', methods=['POST'], endpoint='api_chronic_individual')
def api_chronic_individual():
    """个体慢病风险预测（兼容）"""
    return api_v1_chronic_individual()


@bp.route('/api/v1/chronic/population', methods=['POST'], endpoint='api_v1_chronic_population')
@login_required
@reject_guest  # 高成本：游客烧配额
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
@reject_guest  # 付费上游：硅基流动
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
@login_required
@reject_guest  # 高成本：多次和风 + 计算
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_FORECAST', '60 per minute'), key_func=rate_limit_key)
def api_v1_comprehensive_alert():
    """获取综合健康预警（v1）"""
    return api_service._api_comprehensive_alert()


@bp.route('/api/alert/comprehensive', methods=['POST'], endpoint='api_comprehensive_alert')
def api_comprehensive_alert():
    """获取综合健康预警（兼容）"""
    return api_v1_comprehensive_alert()


# ======================== Pilot 埋点API ========================

@bp.route('/api/v1/events', methods=['POST'], endpoint='api_v1_events')
@login_required
@reject_guest  # 写库埋点：与 login_required 写接口一致，拒游客污染 usage_events
def api_v1_events():
    """写入试点埋点事件（v1）"""
    return api_service._api_usage_event()


def _help_ok(data, status=200):
    from flask import g, jsonify
    return jsonify({'success': True, 'data': data, 'request_id': getattr(g, 'request_id', None)}), status


@bp.route('/api/v1/capabilities', endpoint='api_v1_capabilities')
def api_v1_capabilities():
    from services.help_request_service import capabilities
    return _help_ok(capabilities())


@bp.route('/api/v1/scripts', endpoint='api_v1_scripts')
def api_v1_scripts():
    from services.content_scripts import script_catalog
    return _help_ok(script_catalog())


@bp.route('/api/v1/help-requests', endpoint='api_v1_help_list')
@login_required
@reject_guest
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_MP_PENDING_USER', '360 per minute'), key_func=rate_limit_key)
def api_v1_help_list():
    from services.help_http import handle_domain_error
    from services.help_request_service import list_help_requests
    try:
        return _help_ok(list_help_requests(
            current_user,
            status=request.args.get('status') or 'open',
            cursor=request.args.get('cursor'),
            limit=request.args.get('limit') or 20,
        ))
    except Exception as exc:
        return handle_domain_error(exc)


@bp.route('/api/v1/help-requests', methods=['POST'], endpoint='api_v1_help_create')
@login_required
@reject_guest
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_HELP', '10 per hour'), key_func=rate_limit_key)
def api_v1_help_create():
    from core.db_models import Pair
    from core.extensions import db
    from services.family_access import can_access_pair
    from services.help_http import error_payload, handle_domain_error, json_body
    from services.help_request_service import create_help_request
    from services.notification_outbox import process_outbox_batch
    try:
        payload = json_body()
        pair = Pair.query.filter_by(id=int(payload.get('pair_id') or 0), status='active').first()
        if not pair or not can_access_pair(current_user, pair, 'create_help'):
            return error_payload('not_found', '对象不存在或无权访问。', 404)
        body, _created = create_help_request(
            current_user,
            pair,
            category=payload.get('category') or 'cannot_complete',
            origin_channel='web',
            idempotency_key=payload.get('idempotency_key'),
            is_proxy=True,
            actor_role='elder_proxy',
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _help_ok(body)
    except Exception as exc:
        from core.extensions import db
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route('/api/v1/help-requests/<public_id>', endpoint='api_v1_help_detail')
@login_required
@reject_guest
def api_v1_help_detail(public_id):
    from services.help_http import handle_domain_error
    from services.help_request_service import get_help_request
    try:
        return _help_ok(get_help_request(current_user, public_id))
    except Exception as exc:
        return handle_domain_error(exc)


@bp.route('/api/v1/help-requests/<public_id>/ack', methods=['POST'], endpoint='api_v1_help_ack')
@login_required
@reject_guest
def api_v1_help_ack(public_id):
    from core.extensions import db
    from services.help_http import handle_domain_error, json_body
    from services.help_request_service import ack_help_request
    from services.notification_outbox import process_outbox_batch
    try:
        payload = json_body() if request.get_json(silent=True) is not None else {}
        body = ack_help_request(
            current_user,
            public_id,
            expected_version=payload.get('expected_version'),
            idempotency_key=payload.get('idempotency_key'),
            origin_channel='web',
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _help_ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route('/api/v1/help-requests/<public_id>/start', methods=['POST'], endpoint='api_v1_help_start')
@login_required
@reject_guest
def api_v1_help_start(public_id):
    from core.extensions import db
    from services.help_http import handle_domain_error, json_body
    from services.help_request_service import start_help_request
    try:
        payload = json_body() if request.get_json(silent=True) is not None else {}
        return _help_ok(start_help_request(
            current_user,
            public_id,
            expected_version=payload.get('expected_version'),
            idempotency_key=payload.get('idempotency_key'),
            origin_channel='web',
            commit=True,
        ))
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route('/api/v1/help-requests/<public_id>/resolve', methods=['POST'], endpoint='api_v1_help_resolve')
@login_required
@reject_guest
def api_v1_help_resolve(public_id):
    from core.extensions import db
    from services.help_http import handle_domain_error, json_body
    from services.help_request_service import resolve_help_request
    from services.notification_outbox import process_outbox_batch
    try:
        payload = json_body() if request.get_json(silent=True) is not None else {}
        body = resolve_help_request(
            current_user,
            public_id,
            expected_version=payload.get('expected_version'),
            resolution_code=payload.get('resolution_code') or 'reached_elder',
            idempotency_key=payload.get('idempotency_key'),
            origin_channel='web',
            commit=True,
        )
        process_outbox_batch(limit=10)
        return _help_ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route('/api/v1/help-requests/<public_id>/cancel', methods=['POST'], endpoint='api_v1_help_cancel')
@login_required
@reject_guest
def api_v1_help_cancel(public_id):
    from core.extensions import db
    from services.help_http import handle_domain_error, json_body
    from services.help_request_service import cancel_help_request
    try:
        payload = json_body() if request.get_json(silent=True) is not None else {}
        body = cancel_help_request(
            current_user,
            public_id,
            expected_version=payload.get('expected_version'),
            reason_code=payload.get('cancel_reason') or payload.get('reason_code') or 'other',
            idempotency_key=payload.get('idempotency_key'),
            origin_channel='web',
            commit=True,
        )
        return _help_ok(body)
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route('/api/v1/family-invites', methods=['POST'], endpoint='api_v1_family_invite_create')
@login_required
@reject_guest
def api_v1_family_invite_create():
    from core.db_models import Pair
    from core.extensions import db
    from services.family_access import create_invite
    from services.help_http import error_payload, handle_domain_error, json_body
    try:
        payload = json_body()
        pair = Pair.query.filter_by(id=int(payload.get('pair_id') or 0), status='active').first()
        if not pair:
            return error_payload('not_found', '对象不存在或无权访问。', 404)
        invite, plain = create_invite(
            current_user,
            pair,
            payload.get('role') or 'caregiver',
            ttl_hours=payload.get('ttl_hours') or 72,
            max_uses=payload.get('max_uses') or 1,
        )
        db.session.commit()
        return _help_ok({
            'invite_id': invite.id,
            'role': invite.role,
            'expires_at': invite.expires_at.isoformat() if invite.expires_at else None,
            'code': plain,
        })
    except Exception as exc:
        from core.extensions import db
        db.session.rollback()
        return handle_domain_error(exc)


@bp.route('/api/v1/family-invites/<code>', endpoint='api_v1_family_invite_preview')
@login_required
def api_v1_family_invite_preview(code):
    from services.family_access import preview_invite
    from services.help_http import handle_domain_error
    try:
        return _help_ok(preview_invite(code))
    except Exception as exc:
        return handle_domain_error(exc)


@bp.route('/api/v1/family-invites/<code>/accept', methods=['POST'], endpoint='api_v1_family_invite_accept')
@login_required
@reject_guest
def api_v1_family_invite_accept(code):
    from core.extensions import db
    from services.family_access import consume_invite
    from services.help_http import handle_domain_error
    try:
        membership, invite = consume_invite(current_user, code)
        db.session.commit()
        return _help_ok({
            'role': membership.role,
            'family_space_id': invite.family_space_id,
        })
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)
