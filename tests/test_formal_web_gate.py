# -*- coding: utf-8 -*-
"""正式微信运行态的 Web 私密入口中央门禁回归测试。"""

from sqlalchemy import event

from core.hooks import (
    FORMAL_WEB_ALLOWED_ANALYSIS_ENDPOINTS,
    FORMAL_WEB_ALLOWED_API_ENDPOINTS,
    FORMAL_WEB_ALLOWED_USER_ENDPOINTS,
    _formal_web_gate_kind,
)


def test_formal_web_gate_inventory_defaults_sensitive_blueprints_to_closed(app):
    """新增敏感端点必须默认进入门禁，公开端点只能显式放行。"""
    endpoints = {
        rule.endpoint
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static"
    }

    for endpoint in endpoints:
        blueprint = endpoint.partition(".")[0]
        gate_kind = _formal_web_gate_kind(endpoint)
        if blueprint in {"health", "tools"}:
            assert gate_kind == "html", endpoint
        elif blueprint == "user":
            expected = None if endpoint in FORMAL_WEB_ALLOWED_USER_ENDPOINTS else "html"
            assert gate_kind == expected, endpoint
        elif blueprint == "analysis":
            expected = (
                None
                if endpoint in FORMAL_WEB_ALLOWED_ANALYSIS_ENDPOINTS
                else "html"
            )
            assert gate_kind == expected, endpoint
        elif blueprint == "api":
            expected = None if endpoint in FORMAL_WEB_ALLOWED_API_ENDPOINTS else "json"
            assert gate_kind == expected, endpoint

    expected_private = {
        "public.register",
        "health.family_members",
        "health.health_diary",
        "health.medication_reminders",
        "user.user_dashboard",
        "user.pair_management",
        "user.caregiver_dashboard",
        "user.health_assessment",
        "user.profile",
        "user.update_location",
        "tools.ml_prediction",
        "tools.forecast_7day",
        "tools.chronic_risk",
        "tools.ai_qa",
        "analysis.annual_report",
        "api.api_v1_ml_predict",
        "api.api_ml_predict",
        "api.api_v1_dlnm_risk",
        "api.api_dlnm_risk",
        "api.api_v1_chronic_individual",
        "api.api_chronic_individual",
        "api.api_v1_ai_ask",
        "api.api_ai_ask",
        "api.api_v1_forecast_7day",
        "api.api_forecast_7day",
        "api.api_v1_forecast_daily",
        "api.api_forecast_daily",
        "api.api_v1_comprehensive_alert",
        "api.api_comprehensive_alert",
    }
    assert expected_private <= endpoints
    assert all(_formal_web_gate_kind(endpoint) for endpoint in expected_private)


def test_formal_web_html_gate_runs_before_login_loader_and_database(
    app,
    client,
    db_session,
):
    """正式态 HTML 门禁不得先加载用户或查询健康数据。"""
    from core.db_models import User
    from core.extensions import db

    user = User(username="formal-web-gate-user", role="user")
    user.set_password("safe-test-password")
    db_session.add(user)
    db_session.commit()

    with client.session_transaction() as session_record:
        session_record["_user_id"] = str(user.id)
        session_record["_fresh"] = True

    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["FEATURE_STRUCTURED_LOGS"] = True
    statements = []

    def record_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record_statement)
    try:
        response = client.get("/family-members", follow_redirects=False)
    finally:
        event.remove(db.engine, "before_cursor_execute", record_statement)

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/action")
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert statements == []


def test_formal_web_json_gate_returns_fixed_error_before_csrf_or_service(
    app,
    client,
    monkeypatch,
):
    """个体健康 JSON API 在 CSRF、认证和业务服务前固定拒绝。"""
    from services import api_service

    app.config["WECHAT_FORMAL_RUNTIME"] = True
    monkeypatch.setattr(
        api_service,
        "_api_ml_predict",
        lambda: (_ for _ in ()).throw(AssertionError("不得调用个体健康服务")),
    )

    response = client.post("/api/v1/ml/predict", json={"age": 70})

    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "error": "wechat_formal_web_private_disabled",
        "message": "正式版本请在微信小程序中使用此私密功能。",
    }
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"


def test_formal_web_registration_is_blocked_before_csrf(app, client):
    """正式态注册 POST 即使没有 CSRF，也应先进入固定停用说明。"""
    app.config["WECHAT_FORMAL_RUNTIME"] = True

    response = client.post(
        "/register",
        data={"username": "should-not-register"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/action")
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"


def test_formal_web_gate_preserves_public_aggregate_and_admin_inventory(app):
    """公开天气、社区、GIS 与管理员研究入口继续可达。"""
    allowed = {
        "public.index",
        "public.action_check",
        "user.community_dashboard",
        "user.community_risk",
        "user.heat_exposure_gis",
        "analysis.reports_center",
        "analysis.pilot_dashboard",
        "api.api_v1_current_weather",
        "api.api_v1_community_list",
        "api.api_v1_chronic_population",
    }
    assert all(_formal_web_gate_kind(endpoint) is None for endpoint in allowed)


def test_web_only_runtime_preserves_legacy_registration(app, client, db_session):
    """显式 Web-only 运行态继续保留旧 Web 行为。"""
    app.config["WECHAT_FORMAL_RUNTIME"] = False

    response = client.get("/register", follow_redirects=False)

    assert response.status_code == 200
