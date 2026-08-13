# -*- coding: utf-8 -*-
"""站内 HTML 与 API 的统一 HTTP 错误响应。"""
from html import escape
import math
import time

from flask import current_app, g, jsonify, make_response, render_template, request
from flask_limiter.errors import RateLimitExceeded

from core.extensions import limiter


_ERROR_COPY = {
    403: ('没有访问权限', '此页面仅向具备相应身份的账号开放。'),
    404: ('页面没有找到', '链接可能已经变化，也可能暂时不可用。'),
    429: ('操作太频繁', '为保护账号安全，请等待倒计时结束后再试。'),
    500: ('页面暂时不可用', '我们已经记录问题，请稍后再试。'),
}

_ERROR_CODES = {
    403: 'forbidden',
    404: 'not_found',
    429: 'rate_limit_exceeded',
    500: 'internal_server_error',
}


def _request_id():
    return str(getattr(g, 'request_id', '') or '')


def _wants_json_error():
    path = request.path or ''
    if path.startswith(('/api/', '/mp/api/')) or request.is_json:
        return True
    best = request.accept_mimetypes.best_match(
        ('text/html', 'application/json')
    )
    return bool(
        best == 'application/json'
        and request.accept_mimetypes['application/json']
        > request.accept_mimetypes['text/html']
    )


def _retry_after_seconds():
    try:
        current_limit = limiter.current_limit
        reset_at = getattr(current_limit, 'reset_at', None)
        if reset_at is not None:
            return max(1, math.ceil(float(reset_at) - time.time()))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    try:
        return max(
            1,
            int(current_app.config.get('LOGIN_LOCKOUT_SECONDS', 300)),
        )
    except (TypeError, ValueError):
        return 300


def _database_safe_internal_error_html(title, message, request_id):
    """生成不运行模板上下文、也不读取登录主体的 500 应急页。"""
    safe_title = escape(str(title), quote=True)
    safe_message = escape(str(message), quote=True)
    safe_request_id = escape(str(request_id), quote=True)
    request_id_html = (
        f'<p class="request-id">请求编号：{safe_request_id}</p>'
        if safe_request_id
        else ''
    )
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex, nofollow, noarchive">
    <title>{safe_title} · 宜老天气通</title>
    <style>
        body {{ margin: 0; background: #f5f7fb; color: #24324a; font-family: system-ui, sans-serif; }}
        main {{ box-sizing: border-box; max-width: 720px; margin: 10vh auto; padding: 24px; }}
        section {{ border-radius: 18px; background: #fff; padding: 40px 24px; text-align: center; box-shadow: 0 12px 34px rgba(35, 54, 88, .09); }}
        .status {{ color: #2367d1; font-size: 2.5rem; font-weight: 750; }}
        h1 {{ margin: 8px 0 12px; font-size: 1.55rem; }}
        p {{ line-height: 1.7; }}
        a {{ display: inline-block; margin-top: 12px; border-radius: 10px; background: #2367d1; color: #fff; padding: 10px 18px; text-decoration: none; }}
        .request-id {{ margin-top: 24px; color: #68758a; font-size: .875rem; overflow-wrap: anywhere; }}
    </style>
</head>
<body>
    <main>
        <section>
            <div class="status">500</div>
            <h1>{safe_title}</h1>
            <p>{safe_message}</p>
            <a href="/">返回首页</a>
            {request_id_html}
        </section>
    </main>
</body>
</html>'''


def _build_error_response(status_code, *, retry_after=None):
    title, message = _ERROR_COPY[status_code]
    request_id = _request_id()
    if _wants_json_error():
        payload = {
            'success': False,
            'error': _ERROR_CODES[status_code],
            'message': message,
            'request_id': request_id,
        }
        if retry_after is not None:
            payload['retry_after_seconds'] = retry_after
        response = make_response(jsonify(payload), status_code)
    elif status_code == 500:
        # 数据库故障后不能渲染继承 base.html 的模板：base 与上下文处理器会读取
        # current_user/location，可能在处理原始 500 时再次访问同一个故障数据库。
        response = make_response(
            _database_safe_internal_error_html(title, message, request_id),
            status_code,
        )
    else:
        response = make_response(
            render_template(
                'http_error.html',
                status_code=status_code,
                error_title=title,
                error_message=message,
                request_id=request_id,
                retry_after_seconds=retry_after,
            ),
            status_code,
        )

    response.headers['Cache-Control'] = 'no-store, private, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Request-ID'] = request_id
    if retry_after is not None:
        response.headers['Retry-After'] = str(retry_after)
    return response


def register_http_error_handlers(app):
    """注册中文页面和保持结构化的 API 错误响应。"""

    @app.errorhandler(RateLimitExceeded)
    def handle_rate_limit(_error):
        return _build_error_response(
            429,
            retry_after=_retry_after_seconds(),
        )

    @app.errorhandler(403)
    def handle_forbidden(_error):
        return _build_error_response(403)

    @app.errorhandler(404)
    def handle_not_found(_error):
        return _build_error_response(404)

    @app.errorhandler(500)
    def handle_internal_error(error):
        # 复用响应日志已有的“不要解析身份”哨兵，阻止 after_request 再读 ORM 用户。
        g.formal_web_gate_blocked = True
        app.logger.error(
            '请求处理失败 request_id=%s',
            _request_id(),
            exc_info=(type(error), error, error.__traceback__),
        )
        return _build_error_response(500)
