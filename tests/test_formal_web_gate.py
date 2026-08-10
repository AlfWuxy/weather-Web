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
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = False
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
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = False
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


def test_formal_web_registration_keeps_minimal_account_creation(
    app,
    client,
    db_session,
):
    """正式态保留待验证手机号保存，并继续执行 CSRF 与输入校验。"""
    from core.db_models import User

    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = False
    csrf = "formal-register-csrf"
    with client.session_transaction() as flask_session:
        flask_session["_csrf_token"] = csrf

    response = client.post(
        "/register",
        data={
            "username": "formal_link_user",
            "password": "formal-test-password",
            "phone": "13800138000",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert "/login" in response.headers["Location"]
    user = User.query.filter_by(username="formal_link_user").one()
    assert user.phone_normalized == "+8613800138000"
    assert user.phone_verified_at is None


def test_formal_web_gate_preserves_public_aggregate_and_admin_inventory(app):
    """公开天气、社区、GIS 与管理员研究入口继续可达。"""
    allowed = {
        "public.index",
        "public.register",
        "public.account_link",
        "public.account_link_phone",
        "public.account_link_code",
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


def test_dual_runtime_forecast_uses_route_auth_and_renders_normally(
    app,
    authenticated_client,
):
    """双端开关开启后，预报页不得再被中央门禁送到行动页。"""
    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = True

    response = authenticated_client.get("/forecast-7day", follow_redirects=False)

    assert response.status_code == 200
    assert response.request.path == "/forecast-7day"
    assert 'id="forecastChart"' in response.get_data(as_text=True)


def test_dual_runtime_private_route_still_requires_login(app, client):
    """双端开关移除禁用层后，中央最小登录门禁仍继续生效。"""
    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = True

    response = client.get("/forecast-7day", follow_redirects=False)

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["Location"]
    assert "next=" in response.headers["Location"]
    assert not response.headers["Location"].endswith("/action")


def test_dual_runtime_central_gate_blocks_future_unprotected_private_view(
    app,
    client,
):
    """未来敏感端点即使漏写路由登录装饰器，也不能匿名访问。"""
    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = True
    endpoint = "tools.forecast_7day"
    original_view = app.view_functions[endpoint]
    app.view_functions[endpoint] = lambda: ("unsafe future view", 200)
    try:
        response = client.get("/forecast-7day", follow_redirects=False)
    finally:
        app.view_functions[endpoint] = original_view

    assert response.status_code in (302, 303)
    assert "/login" in response.headers["Location"]
    assert response.headers["Cache-Control"] == (
        "no-store, private, max-age=0"
    )
    assert b"unsafe future view" not in response.data


def test_dual_runtime_private_api_keeps_anonymous_login_guard(
    app,
    client,
    monkeypatch,
):
    """双端态匿名 API 仍由原登录门禁拒绝。"""
    from services import api_service

    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = True
    called = []

    def fake_predict():
        called.append(True)
        return {"success": True}, 200

    monkeypatch.setattr(api_service, "_api_ml_predict", fake_predict)

    anonymous_csrf = "dual-api-anonymous-csrf"
    with client.session_transaction() as session_record:
        session_record["_csrf_token"] = anonymous_csrf
    anonymous = client.post(
        "/api/v1/ml/predict",
        json={"age": 70},
        headers={"X-CSRF-Token": anonymous_csrf},
        follow_redirects=False,
    )
    assert anonymous.status_code == 401
    assert anonymous.get_json() == {
        "success": False,
        "error": "authentication_required",
        "message": "请先登录后使用此功能。",
    }
    assert anonymous.headers["Cache-Control"] == (
        "no-store, private, max-age=0"
    )
    assert called == []


def test_dual_runtime_authenticated_private_api_reaches_service(
    app,
    authenticated_client,
    monkeypatch,
):
    """双端态已登录 API 通过 CSRF 后可以进入原服务层。"""
    from services import api_service

    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["WEB_PRIVATE_FEATURES_ENABLED"] = True
    called = []

    def fake_predict():
        called.append(True)
        return {"success": True}, 200

    monkeypatch.setattr(api_service, "_api_ml_predict", fake_predict)

    authenticated = authenticated_client.post(
        "/api/v1/ml/predict",
        json={"age": 70},
        headers={"X-CSRF-Token": "test-csrf-token"},
        follow_redirects=False,
    )
    assert authenticated.status_code == 200
    assert authenticated.get_json() == {"success": True}
    assert called == [True]
