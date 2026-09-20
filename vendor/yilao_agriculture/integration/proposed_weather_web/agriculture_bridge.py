"""可单独挂载的私有只读农业JSON入口；默认关闭，身份、存储、复核均显式注入。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Callable
from urllib.parse import urlsplit

ROUTE = "/api/v1/agriculture/plan-preview"
SCHEMA = "yilao-weather-web-display-1"
ENABLE_KEY = "YILAO_AGRICULTURE_BRIDGE_ENABLED"
BINDINGS_KEY = "yilao_agriculture_bridge_bindings"
MAX_RESPONSE_BYTES = 1_000_000
LABELS = {"draft": "按已确认资料生成的安排草稿", "demonstration": "虚构演示安排",
          "blocked": "资料或核查未通过，暂不展示安排", "expired": "安排已过期，请重新核对"}
TASK_LABELS = {
    "看地与种植确认", "种苗物料准备", "挑粪及农资搬运", "翻耕碎土作畦", "开沟与修田埂", "基肥施入",
    "育苗育秧与苗床管理", "播种", "定植与插秧", "查苗间苗补苗", "浇水与灌溉操作", "排水与沟渠清理",
    "追肥施入", "拔草锄草与中耕", "铺盖抑草材料", "搭架绑蔓", "植株整理", "人工授粉", "疏果护果与套袋",
    "病虫异常巡查", "已确认的物理防治", "农药配制与喷施", "分批摘叶摘果", "割收与整株收获", "拔收挖收",
    "农机收获服务", "装筐与收获物搬运", "分拣清洗与手工初处理", "脱粒机械服务", "晾晒翻晒与入库",
    "清田与下茬准备", "极端天气前后检查", "农活任务", "施肥", "农资搬运",
}
UNITS = {"sqm": "平方米", "mu": "亩", "kg": "公斤", "trip": "趟", "plant": "株", "m": "米", "m3": "立方米"}
PHASES = {"setup", "outbound", "work", "rest", "wait", "cleanup", "return", "buffer"}
REASONS = {
    "MODE_MISMATCH", "PLAN_EXPIRED", "PLAN_NOT_CURRENT", "STATE_REVISION_MISMATCH", "PLAN_NOT_LATEST_SAVED",
    "PLAN_REQUEST_MISSING", "REQUEST_MODE_MISMATCH", "PLAN_STATUS_UNKNOWN", "PLAN_CREATED_IN_FUTURE",
    "PLAN_EXPIRED_OR_CHANGED_VALIDITY", "STATE_INPUT_HASH_MISMATCH", "STORED_VERIFICATION_NOT_PASSED",
    "RECOMPUTED_VERIFICATION_FAILED", "CURRENT_READINESS_INCOMPLETE", "WEATHER_ORIGIN_UNVERIFIED",
    "WEATHER_STALE", "WEATHER_ISSUANCE_UNKNOWN", "OFFICIAL_ALERT_STATUS_UNKNOWN", "OFFICIAL_ALERT_STALE",
    "OFFICIAL_ALERT_TIME_UNKNOWN", "PLANNED_START_PASSED_REPLAN_REQUIRED", "UNIT_NOT_SUPPORTED_FOR_DISPLAY",
    "PHASE_NOT_SUPPORTED_FOR_DISPLAY", "MALFORMED_OR_UNVERIFIABLE_INPUT",
}


@dataclass(frozen=True)
class Principal:
    """只能由服务端认证层构造；subject是稳定账户ID，不能来自请求查询参数。"""
    subject: str
    authenticated: bool
    guest: bool


@dataclass(frozen=True)
class OwnerPlanSnapshot:
    """读取结果再次绑定所有者，防止存储实现误返回另一用户或共享全局资料。"""
    owner_subject: str
    state: dict
    plan: dict


@dataclass(frozen=True)
class BridgeBindings:
    get_principal: Callable[[dict], Principal | None]
    load_owned_snapshot: Callable[[str], OwnerPlanSnapshot | None]
    adapt_plan: Callable[[dict, dict], dict]


def _time(value):
    if not isinstance(value, str):
        raise ValueError("时间格式不符")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间缺少时区")
    return result.isoformat()


def _number(value, *, integer=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("数字格式不符")
    if value < 0 or (positive and value <= 0) or (integer and value != int(value)):
        raise ValueError("数字范围不符")
    return int(value) if integer else value


def _public_label(value):
    return value if isinstance(value, str) and value in TASK_LABELS else "农活任务"


def _origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("来源格式不符")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _same_origin(environ):
    # 只使用WSGI服务器已确认的scheme/host；不自行信任X-Forwarded-*头。
    origin = environ.get("HTTP_ORIGIN")
    if origin is None:
        return True
    try:
        host = environ.get("HTTP_HOST")
        if not host:
            host = environ["SERVER_NAME"] + ":" + environ["SERVER_PORT"]
        return _origin(origin) == _origin(environ["wsgi.url_scheme"] + "://" + host)
    except (ValueError, TypeError, KeyError):
        return False


def project_display(payload):
    """再次逐字段投影；即使适配器意外多返姓名/健康/位置，也不随JSON透传。"""
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise ValueError("适配契约不符")
    status, mode = payload.get("status"), payload.get("mode")
    if status not in LABELS or mode not in {"real", "demonstration"}:
        raise ValueError("状态不符")
    displayable = payload.get("displayable")
    if type(displayable) is not bool or payload.get("audience") != "authorized_owner_view":
        raise ValueError("受众不符")
    verification = payload.get("validation")
    if not isinstance(verification, dict) or any(type(verification.get(k)) is not bool for k in ("state_match", "engine", "independent")):
        raise ValueError("缺复核")
    if displayable and (not all(verification[k] for k in ("state_match", "engine", "independent"))
                        or status != ("demonstration" if mode == "demonstration" else "draft")):
        raise ValueError("可展示声明与复核状态不一致")
    if not displayable and status not in {"blocked", "expired"}:
        raise ValueError("阻断状态不符")
    alert_state = payload.get("alert_state")
    if alert_state not in {"unknown", "queried_clear", "active_alerts", "demonstration"}:
        raise ValueError("预警状态不符")
    if displayable and mode == "real" and alert_state not in {"queried_clear", "active_alerts"}:
        raise ValueError("真实安排不能使用未知或演示预警")
    result = {"schema_version": SCHEMA, "exported_at": _time(payload.get("exported_at")),
              "status": status, "mode": mode, "displayable": displayable, "label": LABELS[status],
              "audience": "authorized_owner_view", "alert_state": alert_state,
              "validation": {k: verification[k] for k in ("state_match", "engine", "independent")},
              "reason_codes": [code if isinstance(code, str) and code in REASONS else "REVIEW_REQUIRED"
                               for code in payload.get("reason_codes", [])][:50],
              "sessions": [], "tasks": [], "contains_names_health_or_location": False,
              "meaning": "安排量不等于实际完成量；此结果不是个人健康安全许可"}
    if not displayable:
        return result
    result.update(valid_until=_time(payload.get("valid_until")), plan_created_at=_time(payload.get("plan_created_at")),
                  timezone="Asia/Shanghai")
    if datetime.fromisoformat(result["valid_until"]) <= datetime.now(timezone.utc):
        result.update(status="expired", displayable=False, label=LABELS["expired"], reason_codes=["PLAN_EXPIRED"])
        return result
    sessions, tasks = payload.get("sessions"), payload.get("tasks")
    if not isinstance(sessions, list) or len(sessions) > 640 or not isinstance(tasks, list) or len(tasks) > 20:
        raise ValueError("超出显示数量")
    for session in sessions:
        unit = session["unit"]
        if unit not in UNITS or not isinstance(session.get("phases"), list) or len(session["phases"]) > 1000:
            raise ValueError("分段或单位不符")
        phases = []
        for phase in session["phases"]:
            if phase["kind"] not in PHASES:
                raise ValueError("阶段不符")
            phases.append({"kind": phase["kind"], "start": _time(phase["start"]), "end": _time(phase["end"])})
        result["sessions"].append({"task_number": _number(session["task_number"], integer=True, positive=True),
                                   "participant_number": _number(session["participant_number"], integer=True, positive=True),
                                   "task_label": _public_label(session.get("task_label")), "unit": unit, "unit_label": UNITS[unit],
                                   "quantity": _number(session["quantity"], positive=True),
                                   "start": _time(session["start"]), "end": _time(session["end"]), "phases": phases})
    for task in tasks:
        if task["unit"] not in UNITS:
            raise ValueError("单位不符")
        result["tasks"].append({"task_number": _number(task["task_number"], integer=True, positive=True),
                                "task_label": _public_label(task.get("task_label")), "unit": task["unit"],
                                **{k: _number(task[k]) for k in ("requested_quantity", "scheduled_quantity", "unarranged_quantity")},
                                "meaning": "已排进时间表的量；不是实际完成量"})
    return result


def create_wsgi_app(bindings: BridgeBindings | None = None, *, enabled=False):
    """创建无副作用WSGI应用；即使有依赖，也必须显式enabled=True才开放读取。"""
    def application(environ, start_response):
        status, body = _serve(environ, bindings, enabled=enabled)
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            status, encoded = 503, b'{"error":{"code":"DISPLAY_TOO_LARGE"}}'
        descriptions = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                        405: "Method Not Allowed", 503: "Service Unavailable"}
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(encoded))),
                   ("Cache-Control", "private, no-store, max-age=0"), ("Pragma", "no-cache"),
                   ("Vary", "Cookie, Authorization"), ("X-Content-Type-Options", "nosniff"),
                   ("Cross-Origin-Resource-Policy", "same-origin"), ("Content-Security-Policy", "default-src 'none'"),
                   ("Referrer-Policy", "no-referrer")]
        if status == 405:
            headers.append(("Allow", "GET, HEAD"))
        start_response(f"{status} {descriptions[status]}", headers)
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else encoded]
    return application


def _serve(environ, bindings, *, enabled):
    def fail(status, code):
        return status, {"error": {"code": code}}
    if environ.get("PATH_INFO") != ROUTE:
        return fail(404, "NOT_FOUND")
    if environ.get("REQUEST_METHOD") not in {"GET", "HEAD"}:
        return fail(405, "READ_ONLY_ENDPOINT")
    if enabled is not True:
        return fail(503, "AGRICULTURE_BRIDGE_DISABLED")
    if (not isinstance(bindings, BridgeBindings)
            or any(not callable(getattr(bindings, k)) for k in ("get_principal", "load_owned_snapshot", "adapt_plan"))):
        return fail(503, "TRUSTED_BINDINGS_REQUIRED")
    # 不接受客户端挑选账户、指定now或上传state来绕过服务端绑定。
    if environ.get("QUERY_STRING") or environ.get("CONTENT_LENGTH") not in (None, "", "0"):
        return fail(400, "NO_CLIENT_STATE_OR_IDENTITY_PARAMETERS")
    if environ.get("HTTP_SEC_FETCH_SITE") == "cross-site" or not _same_origin(environ):
        return fail(403, "SAME_SITE_REQUIRED")
    try:
        principal = bindings.get_principal(environ)
        if not isinstance(principal, Principal) or principal.authenticated is not True:
            return fail(401, "AUTHENTICATION_REQUIRED")
        if (principal.guest is not False or not isinstance(principal.subject, str) or not principal.subject.strip()
                or len(principal.subject) > 200 or principal.subject.startswith("guest:")):
            return fail(403, "REAL_ACCOUNT_REQUIRED")
        snapshot = bindings.load_owned_snapshot(principal.subject)
        if snapshot is None:
            return fail(404, "NO_OWNED_AGRICULTURE_PLAN")
        if not isinstance(snapshot, OwnerPlanSnapshot) or snapshot.owner_subject != principal.subject:
            return fail(403, "OWNER_BINDING_MISMATCH")
        if not isinstance(snapshot.state, dict) or not isinstance(snapshot.plan, dict):
            return fail(503, "OWNED_SNAPSHOT_INVALID")
        return 200, project_display(bindings.adapt_plan(snapshot.state, snapshot.plan))
    except Exception:
        # 对外和日志均不抄录底层异常，避免泄露私有路径、姓名和资料。
        return fail(503, "AGRICULTURE_PREVIEW_UNAVAILABLE")


def create_blueprint():
    """原站Flask薄挂载层；创建时不读取current_user、不加载任何全局SQLite。"""
    from flask import Blueprint, current_app, request
    from werkzeug.wrappers import Response

    blueprint = Blueprint("agriculture_private", __name__)

    # 写方法仅进入固定405 JSON，不调用身份、存储或适配器。
    @blueprint.route(ROUTE, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                     provide_automatic_options=False)
    def plan_preview():
        app = create_wsgi_app(current_app.extensions.get(BINDINGS_KEY),
                              enabled=current_app.config.get(ENABLE_KEY) is True)
        return Response.from_app(app, request.environ)

    return blueprint
