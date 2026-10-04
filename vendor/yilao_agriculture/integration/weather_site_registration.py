"""天气通账户工作台显式挂载器；由原站工厂调用，默认关闭。"""
from datetime import datetime, timezone
from hashlib import sha256
from importlib import import_module
from pathlib import Path
import sys


def principal_for_site_user(user):
    """仅使用服务器用户模型；保留创建时刻以拒绝删除后重用ID。"""
    from integration.proposed_weather_web.agriculture_bridge import Principal
    if user is None or getattr(user, "deleted_at", None) is not None:
        return None
    # 原站密码戳验证会话；ID加不可编辑的创建时刻区分SQLite删除后重用ID的账户。
    if getattr(user, "is_authenticated", False) is not True:
        return None
    uid = getattr(user, "id", None)
    is_guest = (getattr(user, "is_guest", False) is True
                or getattr(user, "role", None) == "guest"
                or str(uid).startswith("guest:"))
    if is_guest:
        return Principal("guest:rejected", True, True)
    if type(uid) is not int or uid <= 0:
        return None
    created = getattr(user, "created_at", None)
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    generation = sha256(created.astimezone(timezone.utc).isoformat(timespec="microseconds").encode()).hexdigest()[:24]
    return Principal(f"weather-web:user:{uid}:{generation}", True, False)


def make_site_alert_fetcher(app):
    """复用原站认证与预算；不放宽HTTP请求上下文的网络门禁。"""
    def alert_fetcher(plot, archive_dir):
        # 只允许受控后台进程取数；网页入队既不生成令牌，也不预占额度。
        from flask import has_request_context
        if has_request_context():
            raise RuntimeError("官方预警必须由后台工作进程采集")
        from services.qweather_auth import (
            get_qweather_request_headers, is_qweather_configured, invalidate_qweather_token)
        from services.qweather_budget import reserve_qweather_request
        from yilao_agri.official_alert_fetch import fetch_qweather_alerts

        api_base = app.config.get("QWEATHER_API_BASE", "")
        return fetch_qweather_alerts(
            plot, archive_dir, api_base=api_base,
            configured=is_qweather_configured(app.config),
            auth_headers_provider=lambda: get_qweather_request_headers(config=app.config, api_base=api_base),
            budget_reserver=lambda: reserve_qweather_request("weatheralert_v1_current"),
            invalidate_auth=invalidate_qweather_token)

    return alert_fetcher


def register_agriculture_workbench(app, *, embed_template=None):
    """原站在注册蓝图时调用；不自动建库、迁移、加载凭据或开启功能。"""
    app.config.setdefault("YILAO_AGRICULTURE_WORKBENCH_ENABLED", False)
    if app.config["YILAO_AGRICULTURE_WORKBENCH_ENABLED"] is not True:
        return None

    def path_config(key):
        value = app.config.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f"{key}需要显式绝对路径")
        path = Path(value)
        if path != path.resolve():
            raise ValueError(f"{key}不能包含符号链接或路径跳转")
        return path

    source = path_config("YILAO_AGRICULTURE_SOURCE_ROOT")
    database = path_config("YILAO_AGRICULTURE_ACCOUNT_DB")
    archive = path_config("YILAO_AGRICULTURE_ARCHIVE_ROOT")
    for relative in ("yilao_agri/community_service.py", "integration/community_workbench.py", "web/index.html",
                     "data/community_catalog.json", "examples/duchang_demo.json"):
        if not (source / relative).is_file():
            raise ValueError("宜老农业源码交付不完整")
    if not database.is_file():
        raise ValueError("账户库须由维护者明确初始化后才可开启")
    # 交付包保留原目录相对关系。显式检查命名冲突，不能悄悄调用站点里同名的另一个模块。
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in {"integration", "yilao_agri"}:
            continue
        filename = getattr(module, "__file__", None)
        paths = list(getattr(module, "__path__", []))
        if (filename and not Path(filename).resolve().is_relative_to(source)) or any(
                not Path(path).resolve().is_relative_to(source) for path in paths):
            raise ValueError("源码模块命名冲突，请在独立进程配置正确依赖路径")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    repository_module = import_module("integration.account_repository")
    workbench_module = import_module("integration.community_workbench")
    principal_module = import_module("integration.proposed_weather_web.agriculture_bridge")
    for module in (repository_module, workbench_module, principal_module):
        if not Path(module.__file__).resolve().is_relative_to(source):
            raise ValueError("实际加载的宜老农业源码与指定目录不一致")

    from flask import redirect, url_for
    from flask_login import current_user
    from core.security import generate_csrf_token, validate_csrf

    def principal_provider():
        return principal_for_site_user(current_user)

    page_renderer = None
    if embed_template is not None:
        if embed_template != "agriculture.html":
            raise ValueError("只允许固定的农业站内模板")
        render_module = import_module("integration.weather_site_page")
        page_renderer = lambda context, source: render_module.render_weather_site_page(
            context, source, template=embed_template)

    def login_page(principal):
        # next由服务器固定，不转送任意查询字符串，也不创建共享访客资料。
        return redirect(url_for("public.login", next="/agriculture/"))


    from integration.weather_jobs import WeatherJobRepository, weather_job_path
    repository = repository_module.AccountRepository(database)
    jobs = WeatherJobRepository(weather_job_path(database))
    bp = workbench_module.create_workbench_blueprint(
        repository=repository, principal_provider=principal_provider,
        csrf_token_provider=generate_csrf_token, csrf_validator=validate_csrf,
        archive_root=archive, site_origin=app.config.get("YILAO_AGRICULTURE_SITE_ORIGIN"), enabled=True,
        page_renderer=page_renderer, unauthenticated_page=login_page if embed_template else None,
        weather_jobs=jobs)
    app.register_blueprint(bp)
    app.extensions["yilao_agriculture_account_repository"] = repository
    app.extensions["yilao_agriculture_weather_jobs"] = jobs
    return bp
