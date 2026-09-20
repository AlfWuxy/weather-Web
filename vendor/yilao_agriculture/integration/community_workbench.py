"""同一社区服务的私有 Flask 工作台；身份、CSRF、存储和来源均由站点显式绑定。"""
from __future__ import annotations

from hashlib import sha256
from html import escape
import hmac
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .account_repository import AccountRepository, AccountRepositoryError, _subject
from .community_account_store import AccountCommunityStore
from yilao_agri.community_service import CommunityService, ROOT, estimates, readiness
from yilao_agri.community_store import CommunityError, MODES, json_text
from yilao_agri.engine import PlanInterrupted
from yilao_agri.models import InputError
from yilao_agri.collection_identity import account_collection_key

MAX_BODY = 4_000_000
FORBIDDEN = {"owner", "owner_subject", "subject", "user_id", "account_id", "principal",
             "authenticated", "guest", "db_path", "archive_dir", "now"}
GET_QUERIES = {
    "health": set(), "catalog": set(), "field-template": set(),
    "state": {"mode"}, "estimates": {"mode"}, "readiness": {"mode"},
    "export": {"mode"}, "field-collection": {"mode"}, "predictions/review": {"mode"},
    "rate-review": {"mode", "task_id", "worker_id"},
    "predictions/preview": {"mode", "task_id", "worker_id", "quantity_value", "quantity_unit"},
}
POST_ACTIONS = {"plan": "plan", "weather/refresh": "refresh_weather", "weather/import": "import_weather",
                "feedback": "feedback", "rate-review/adopt": "adopt_rate_review",
                "predictions/freeze": "freeze_prediction", "import": "import_bundle"}


def _fail(message, code="INVALID_INPUT", status=400):
    raise CommunityError(message, code=code, status=status)


def _prefix(value):
    if not isinstance(value, str) or not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", value):
        raise ValueError("工作台前缀须为明确的站内绝对路径，不能含尾斜杠")
    return value


def _origin(value):
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
        raise ValueError("来源格式错误")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("来源须为scheme和host，不带路径")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("账户工作台在非本机环境须使用HTTPS")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复JSON字段")
        result[key] = value
    return result


def create_workbench_blueprint(*, repository, principal_provider, csrf_token_provider, csrf_validator,
                               archive_root, site_origin, enabled=False,
                               api_prefix="/api/v1/agriculture/workbench", page_prefix="/agriculture",
                               page_renderer=None, unauthenticated_page=None, alert_fetcher=None):
    """注册不创建数据库、归档、迁移或账户；仅服务端回调可提供身份与CSRF。"""
    from flask import Blueprint, Response, current_app, g, redirect, request, send_file

    if not isinstance(repository, AccountRepository):
        raise ValueError("需要显式账户仓库")
    if not all(callable(callback) for callback in (principal_provider, csrf_token_provider, csrf_validator)):
        raise ValueError("需要完整的服务器身份与CSRF回调")
    if any(callback is not None and not callable(callback) for callback in (page_renderer, unauthenticated_page, alert_fetcher)):
        raise ValueError("页面渲染、登录引导与预警采集必须由服务器回调提供")
    if type(enabled) is not bool or not isinstance(archive_root, (str, Path)) or not str(archive_root):
        raise ValueError("须明确开关与私有天气归档目录")
    archive = Path(archive_root).expanduser().absolute()
    if archive != archive.resolve():
        raise ValueError("归档目录不能含符号链接或路径跳转")
    expected_origin = _origin(site_origin)
    api_prefix, page_prefix = _prefix(api_prefix), _prefix(page_prefix)
    if api_prefix == page_prefix or page_prefix.startswith(api_prefix + "/") or api_prefix.startswith(page_prefix + "/"):
        raise ValueError("工作台页面和接口前缀须分离")
    web_root = (ROOT / "web").resolve()
    bp = Blueprint("yilao_community_workbench", __name__)

    def body_limit():
        configured = current_app.config.get("MAX_CONTENT_LENGTH")
        return min(MAX_BODY, configured) if type(configured) is int and configured > 0 else MAX_BODY

    def response(data, status=200, headers=None):
        return Response(json_text(data), status=status, content_type="application/json; charset=utf-8", headers=headers)

    def mode(value):
        if not isinstance(value, str) or value not in MODES:
            _fail("真实与演示模式无效", "MODE_CONFLICT")
        return value

    def page_binding(subject, token):
        secret = current_app.secret_key
        if isinstance(secret, str): secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or not secret:
            _fail("站点会话密钥尚未配置", "SESSION_BINDING_NOT_CONFIGURED", 503)
        if not isinstance(token, str) or not token or len(token) > 512:
            _fail("页面验证尚未配置", "CSRF_NOT_CONFIGURED", 503)
        message = json_text(["yilao-workbench-account-context-v1", subject, token]).encode("utf-8")
        return hmac.new(secret, message, sha256).hexdigest()

    @bp.before_request
    def protect():
        if not enabled:
            _fail("工作台尚未启用", "NOT_FOUND", 404)
        try:
            current_origin = _origin(request.scheme + "://" + request.host)
            supplied = request.headers.get("Origin")
            if current_origin != expected_origin:
                _fail("访问来源与配置站点不符", "HOST_REJECTED", 403)
            if supplied is not None and _origin(supplied) != expected_origin:
                _fail("不接受其它网页的请求", "ORIGIN_REJECTED", 403)
        except ValueError:
            _fail("访问来源无法确认", "ORIGIN_REJECTED", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            _fail("不接受跨站请求", "ORIGIN_REJECTED", 403)
        # 一次请求固定一次认证结果，不接受请求体、查询参数或共享默认账户。
        principal = principal_provider()
        if (unauthenticated_page is not None and request.method in {"GET", "HEAD"}
                and request.path in {page_prefix, page_prefix + "/", page_prefix + "/index.html"}
                and (principal is None or principal.authenticated is not True or principal.guest is True)):
            return unauthenticated_page(principal)
        subject = _subject(principal)
        g.yilao_subject = subject
        if any(key in FORBIDDEN for key in request.args):
            _fail("账户由当前登录会话决定", "IDENTITY_OVERRIDE_REJECTED")
        owner_archive = archive / sha256(subject.encode("utf-8")).hexdigest()
        if archive != archive.resolve() or owner_archive != owner_archive.resolve():
            _fail("私有归档路径无法确认", "ARCHIVE_PATH_INVALID", 503)
        g.yilao_service = CommunityService(store=AccountCommunityStore(repository, principal),
                                         archive_dir=owner_archive, alert_fetcher=alert_fetcher)
        g.yilao_archive = owner_archive
        if request.path.startswith(api_prefix + "/"):
            # 原站登录切换可能保留CSRF；页面上下文另外绑定账户，旧页不能误读写新账户。
            binding = request.headers.get("X-Yilao-Account-Context")
            expected = page_binding(subject, csrf_token_provider())
            if not isinstance(binding, str) or not re.fullmatch(r"[a-f0-9]{64}", binding) or not hmac.compare_digest(binding, expected):
                _fail("登录账户或页面会话已变化，请刷新页面后继续", "ACCOUNT_CONTEXT_CHANGED", 409)
        if request.method not in {"GET", "HEAD"}:
            if request.method not in {"POST", "PUT"}:
                _fail("不支持此请求方法", "METHOD_NOT_ALLOWED", 405)
            token = request.headers.get("X-CSRF-Token")
            if not isinstance(token, str) or not token or len(token) > 512 or csrf_validator(token) is not True:
                _fail("页面验证已失效，请刷新后重试", "CSRF_REJECTED", 403)
            if request.mimetype != "application/json":
                _fail("保存资料须为JSON请求", "CONTENT_TYPE", 415)
            if request.content_length is None or not 0 < request.content_length <= body_limit():
                _fail("请求为空或超过本站资料大小限制", "BODY_SIZE", 413)

    @bp.after_request
    def private_headers(result):
        result.headers["Cache-Control"] = "private, no-store"
        result.vary.add("Cookie")
        result.vary.add("Authorization")
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        result.headers.setdefault("Content-Security-Policy", ("default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'"))
        return result

    @bp.errorhandler(CommunityError)
    def community_error(exc):
        return response({"error": {"code": exc.code, "message": str(exc), "field": exc.field}}, exc.status)

    @bp.errorhandler(AccountRepositoryError)
    def repository_error(exc):
        return response({"error": {"code": exc.code, "message": str(exc), "field": None}}, exc.status)

    @bp.errorhandler(InputError)
    def input_error(exc):
        return response({"error": {"code": "PLAN_INPUT_INVALID", "message": str(exc), "field": getattr(exc, "path", None)}}, 422)

    @bp.errorhandler(PlanInterrupted)
    def interrupted(exc):
        return response({"error": {"code": "SEARCH_TIME_LIMIT", "message": "本次计算达到时间上限，未保存不完整安排", "field": None}}, 422)

    @bp.errorhandler(Exception)
    def unexpected(exc):
        # 不把健康资料、原始请求或服务器路径放入响应或日志。
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return response({"error": {"code": "HTTP_ERROR", "message": "本次请求未完成", "field": None}}, exc.code)
        return response({"error": {"code": "INTERNAL_ERROR", "message": "本次处理未完成，已保存资料保留", "field": None}}, 500)

    @bp.get(page_prefix)
    def canonical_page():
        return redirect(page_prefix + "/", code=308)

    @bp.get(page_prefix + "/", defaults={"asset": "index.html"})
    @bp.get(page_prefix + "/<path:asset>")
    def page(asset):
        if request.args:
            _fail("页面入口不接受资料参数")
        file = (web_root / asset).resolve()
        if (not file.is_relative_to(web_root) or not file.is_file()
                or file.suffix not in {".html", ".css", ".js", ".svg", ".png", ".ico"}
                or (file.suffix == ".html" and file.name != "index.html")):
            _fail("文件不存在", "NOT_FOUND", 404)
        if file == web_root / "index.html":
            token = csrf_token_provider()
            if not isinstance(token, str) or not token or len(token) > 512:
                _fail("页面验证尚未配置", "CSRF_NOT_CONFIGURED", 503)
            body = file.read_text(encoding="utf-8")
            if page_renderer is not None:
                return page_renderer({"api_prefix": api_prefix, "page_prefix": page_prefix,
                    "csrf_token": token, "account_context": page_binding(g.yilao_subject, token),
                    "body_limit": body_limit()}, body)
            for name, value in (("yilao-api-base", api_prefix), ("yilao-storage-scope", "account"), ("csrf-token", token),
                                ("yilao-account-context", page_binding(g.yilao_subject, token)),
                                ("yilao-body-limit", str(body_limit()))):
                pattern = r'<meta name="' + re.escape(name) + r'" content="[^"]*"\s*/?>'
                body, count = re.subn(pattern, lambda _: f'<meta name="{name}" content="{escape(value, quote=True)}">', body)
                if count != 1:
                    _fail("工作台页面与接口配置不匹配", "UI_CONFIGURATION_INVALID", 503)
            return Response(body, content_type="text/html; charset=utf-8")
        return send_file(file, conditional=False, etag=False, max_age=0)

    def query_values(endpoint):
        allowed = GET_QUERIES.get(endpoint)
        if allowed is None:
            _fail("没有此接口", "NOT_FOUND", 404)
        if set(request.args) - allowed or any(len(values) != 1 for _, values in request.args.lists()):
            _fail("查询参数重复或不属于此接口")
        data = request.args.to_dict()
        data["mode"] = mode(data.get("mode", "real"))
        return data

    @bp.route(api_prefix + "/<path:endpoint>", methods=["GET", "PUT", "POST"])
    def api(endpoint):
        service = g.yilao_service
        if request.method in {"GET", "HEAD"}:
            data = query_values(endpoint)
            selected = data["mode"]
            if endpoint == "health":
                return response({"ok": True, "version": "0.2.0", "scope": "authenticated-account-workbench", "field_validated": False})
            if endpoint == "catalog":
                from yilao_agri.community_catalog import get_community_catalog
                return response(get_community_catalog())
            if endpoint == "field-template":
                return Response((ROOT / "examples/field_records_template.csv").read_bytes(),
                    content_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="field_records_template.csv"'})
            if endpoint == "state": return response(service.get_state(selected))
            if endpoint == "estimates": return response(estimates(service.get_state(selected)))
            if endpoint == "readiness": return response(readiness(service.get_state(selected)))
            if endpoint == "rate-review": return response(service.rate_review(data))
            if endpoint == "predictions/review": return response(service.prediction_review(selected))
            if endpoint == "predictions/preview":
                if "quantity_value" in data or "quantity_unit" in data:
                    if not {"quantity_value", "quantity_unit"} <= set(data):
                        _fail("预计完成量需要数字和单位")
                    try: value = float(data.pop("quantity_value"))
                    except ValueError: _fail("预计完成量须为数字")
                    data["quantity"] = {"value": value, "unit": data.pop("quantity_unit")}
                return response(service.prediction_preview(data))
            if endpoint == "export":
                return response(service.export(selected), headers={"Content-Disposition": f'attachment; filename="yilao-{selected}.json"'})
            if endpoint == "field-collection":
                key=account_collection_key(current_app.secret_key,g.yilao_subject)
                return response(service.field_collection(selected,export_secret=key),
                    headers={"Content-Disposition": 'attachment; filename="yilao-field-collection-draft.json"'})
        if request.args:
            _fail("保存操作的参数须完整放在JSON中")
        try:
            data = json.loads(request.get_data(cache=False), object_pairs_hook=_unique_object,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError, RecursionError):
            _fail("JSON资料无法读取")
        if not isinstance(data, dict): _fail("请求须为JSON对象")
        if set(data) & FORBIDDEN: _fail("账户由当前登录会话决定", "IDENTITY_OVERRIDE_REJECTED")
        if "mode" in data: mode(data["mode"])
        if request.method == "PUT" and endpoint == "state": return response(service.save(data))
        if request.method == "POST":
            if endpoint == "demo":
                if set(data) - {"mode"} or data.get("mode", "demonstration") != "demonstration":
                    _fail("演示重置只用于当前账户的演示区", "MODE_CONFLICT")
                return response(service.demo())
            if endpoint in POST_ACTIONS:
                if endpoint == "weather/refresh":
                    # 只有明确刷新天气时才创建归档；每账户独立且其他系统用户不可读。
                    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
                    g.yilao_archive.mkdir(exist_ok=True, mode=0o700)
                    archive.chmod(0o700)
                    g.yilao_archive.chmod(0o700)
                return response(getattr(service, POST_ACTIONS[endpoint])(data))
        _fail("没有此接口", "NOT_FOUND", 404)

    return bp
