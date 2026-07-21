# -*- coding: utf-8 -*-
"""微信正式运行态全局日志隐私边界测试。"""

import json
import logging
from pathlib import Path
import re
import threading

import pytest

from core.logging_privacy import (
    _restore_logging_privacy_for_testing,
    formal_request_log_event,
    install_formal_logging_privacy,
)


def test_formal_runtime_refuses_sentry_even_if_app_config_was_preloaded(app):
    """应用配置层也要阻断绕过 dotenv 门禁的第三方异常上传。"""
    from core.config import _configure_sentry

    app.config.update({
        "WECHAT_FORMAL_RUNTIME": True,
        "SENTRY_DSN": "https://public@example.invalid/1",
        "SENTRY_TRACES_SAMPLE_RATE": 0.0,
        "SENTRY_SEND_PII": False,
    })

    with pytest.raises(RuntimeError, match="禁止启用 Sentry"):
        _configure_sentry(app, logging.getLogger("tests.sentry-guard"))


def test_request_reachable_dlnm_service_does_not_write_direct_stdout_or_traceback():
    """请求可达的模型服务必须统一进入正式态日志净化层。"""
    source = (
        Path(__file__).resolve().parents[1] / "services" / "dlnm_risk_service.py"
    ).read_text(encoding="utf-8")
    runtime_source = source.split("# 测试代码", 1)[0]

    assert "print(" not in runtime_source
    assert "traceback.print_exc" not in runtime_source


class _EntryCapture(logging.Handler):
    """在 Handler 入口直接保存 LogRecord，避免格式化掩盖泄漏。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def handle(self, record):
        self.records.append(record)
        return True


@pytest.fixture
def formal_logging_privacy():
    _restore_logging_privacy_for_testing()
    assert install_formal_logging_privacy(True) is True
    assert install_formal_logging_privacy(True) is True
    yield
    _restore_logging_privacy_for_testing()


@pytest.fixture
def capture_logger():
    configured = []

    def configure(name):
        target = logging.getLogger(name) if name else logging.getLogger()
        snapshot = (target, list(target.handlers), target.level, target.propagate, target.disabled)
        handler = _EntryCapture()
        target.handlers = [handler]
        target.setLevel(logging.DEBUG)
        target.propagate = False
        target.disabled = False
        configured.append(snapshot)
        return target, handler

    yield configure

    for target, handlers, level, propagate, disabled in reversed(configured):
        target.handlers = handlers
        target.setLevel(level)
        target.propagate = propagate
        target.disabled = disabled


def _record_surface(record):
    return "\n".join((record.getMessage(), repr(record.__dict__)))


def test_formal_runtime_removes_message_args_exception_and_extra_before_handler(
    formal_logging_privacy,
    capture_logger,
):
    """未来新增 logger 也不能把正文、网络信息、SQL 或异常参数送入 handler。"""
    sentinels = {
        "body": "BODY_SENTINEL_6A31",
        "query": "QUERY_SENTINEL_031C",
        "ip": "198.51.100.194",
        "user_agent": "USER_AGENT_SENTINEL_A550",
        "header": "HEADER_SENTINEL_D8B2",
        "sql": "SQL_PARAMS_SENTINEL_85ED",
        "exception": "EXCEPTION_SENTINEL_557D",
    }
    logger, capture = capture_logger("tests.future_logger")

    current_thread = threading.current_thread()
    original_thread_name = current_thread.name
    try:
        current_thread.name = sentinels["header"]
        try:
            raise RuntimeError(sentinels["exception"])
        except RuntimeError:
            logger.error(
                "body=%s query=%s",
                sentinels["body"],
                sentinels["query"],
                exc_info=True,
                stack_info=True,
                extra={
                    "remote_addr": sentinels["ip"],
                    "user_agent": sentinels["user_agent"],
                    "request_headers": {"X-Probe": sentinels["header"]},
                    "sql_params": (sentinels["sql"],),
                },
            )
    finally:
        current_thread.name = original_thread_name

    assert len(capture.records) == 1
    record = capture.records[0]
    payload = json.loads(record.getMessage())
    assert payload == {
        "event": "python_log",
        "logger": "tests.future_logger",
        "level": "ERROR",
        "module": "test_logging_privacy",
        "function": (
            "test_formal_runtime_removes_message_args_exception_and_extra_before_handler"
        ),
        "line": payload["line"],
    }
    assert isinstance(payload["line"], int) and payload["line"] > 0
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert record.threadName == "thread"
    assert record.processName == "process"
    surface = _record_surface(record)
    for sentinel in sentinels.values():
        assert sentinel not in surface


@pytest.mark.parametrize("logger_name", [None, "gunicorn.error", "gunicorn.access"])
def test_formal_runtime_covers_root_and_gunicorn_loggers(
    formal_logging_privacy,
    capture_logger,
    logger_name,
):
    """同一进程内 root 与 Gunicorn logger 都经过全局净化。"""
    sentinel = "GUNICORN_ACCESS_SENTINEL_4EAC"
    logger, capture = capture_logger(logger_name)
    logger.warning(
        "request=%s",
        sentinel,
        extra={"h": "203.0.113.77", "a": sentinel, "r": f"/?q={sentinel}"},
    )

    assert len(capture.records) == 1
    record = capture.records[0]
    assert sentinel not in _record_surface(record)
    payload = json.loads(record.getMessage())
    assert payload["event"] == "python_log"
    assert payload["logger"] == (logger_name or "root")


def test_formal_request_event_keeps_only_validated_fixed_fields(
    formal_logging_privacy,
    capture_logger,
):
    """专用请求事件保留排障字段，并裁掉未声明或不合法的值。"""
    sentinels = {
        "token": "ACTION_TOKEN_SENTINEL_998A",
        "query": "QUERY_SENTINEL_C2D7",
        "body": "BODY_SENTINEL_0CB4",
        "ip": "192.0.2.155",
        "user_agent": "USER_AGENT_SENTINEL_87AA",
        "header": "HEADER_SENTINEL_E930",
        "sql": "SQL_SENTINEL_AA18",
    }
    logger, capture = capture_logger("core.hooks")
    event = formal_request_log_event({
            "request_id": "0123456789abcdef",
            "user_id": 17,
            "user_role": "user",
            "method": "GET",
            "path": f"/e/{sentinels['token']}/checkin?probe={sentinels['query']}",
            "endpoint": "public.action_check",
            "status": 200,
            "duration_ms": 12.345,
            "external_api": [
                {"service": "qweather_now", "elapsed_ms": 8.765, "status": 200},
                {"service": sentinels["body"], "elapsed_ms": 1, "status": 200},
            ],
            "request_body": sentinels["body"],
            "remote_addr": sentinels["ip"],
            "user_agent": sentinels["user_agent"],
            "headers": {"X-Probe": sentinels["header"]},
            "sql_params": sentinels["sql"],
        })
    # 调用方即使保留引用并追加字段，LogRecord 创建时仍会再次校验。
    event.payload["late_body"] = sentinels["body"]
    logger.info(
        event,
        extra={"future_extra": sentinels["header"]},
    )

    assert len(capture.records) == 1
    record = capture.records[0]
    payload = json.loads(record.getMessage())
    assert payload["event"] == "http_request"
    assert payload["logger"] == "core.hooks"
    assert payload["level"] == "INFO"
    assert payload["module"] == "test_logging_privacy"
    assert payload["function"] == "test_formal_request_event_keeps_only_validated_fixed_fields"
    assert payload["request_id"] == "0123456789abcdef"
    assert payload["path"] == "/e/<token>/checkin"
    assert payload["endpoint"] == "public.action_check"
    assert payload["duration_ms"] == 12.35
    assert payload["external_api"] == [
        {"service": "qweather_now", "elapsed_ms": 8.77, "status": 200}
    ]
    assert set(payload) == {
        "event",
        "logger",
        "level",
        "module",
        "function",
        "line",
        "request_id",
        "user_id",
        "user_role",
        "method",
        "path",
        "endpoint",
        "status",
        "duration_ms",
        "external_api",
    }
    surface = _record_surface(record)
    for sentinel in sentinels.values():
        assert sentinel not in surface


def test_formal_http_request_does_not_log_external_request_content(
    app,
    client,
    formal_logging_privacy,
    capture_logger,
):
    """真实 Flask 请求中的外部头、正文、查询、IP 与 UA 不进入正式请求日志。"""
    sentinels = {
        "external_request_id": "EXTERNAL_REQUEST_ID_SENTINEL_B409",
        "body": "REQUEST_BODY_SENTINEL_BCB7",
        "query": "QUERY_SENTINEL_358E",
        "ip": "198.51.100.231",
        "user_agent": "USER_AGENT_SENTINEL_278F",
        "header": "EXTERNAL_HEADER_SENTINEL_124A",
    }
    app.config["WECHAT_FORMAL_RUNTIME"] = True
    app.config["FEATURE_STRUCTURED_LOGS"] = True
    _logger, capture = capture_logger("core.hooks")

    response = client.post(
        f"/mp/api/v1/nonexistent?probe={sentinels['query']}",
        json={"notes": sentinels["body"]},
        headers={
            "X-Request-Id": sentinels["external_request_id"],
            "User-Agent": sentinels["user_agent"],
            "X-Debug-Probe": sentinels["header"],
        },
        environ_overrides={"REMOTE_ADDR": sentinels["ip"]},
    )

    assert response.status_code == 404
    assert len(capture.records) == 1
    record = capture.records[0]
    payload = json.loads(record.getMessage())
    assert payload["event"] == "http_request"
    assert re.fullmatch(r"[0-9a-f]{16}", payload["request_id"])
    assert response.headers["X-Request-Id"] == payload["request_id"]
    assert payload["path"] == "/mp/api/v1/nonexistent"
    surface = _record_surface(record)
    for sentinel in sentinels.values():
        assert sentinel not in surface


def test_nonformal_runtime_preserves_standard_logging_behavior(capture_logger):
    """未启用正式微信运行态时，既有 logging 行为保持不变。"""
    _restore_logging_privacy_for_testing()
    assert install_formal_logging_privacy(False) is False
    logger, capture = capture_logger("tests.web_only_logger")
    logger.info("ordinary=%s", "WEB_ONLY_SENTINEL_1B8C")

    assert len(capture.records) == 1
    record = capture.records[0]
    assert record.msg == "ordinary=%s"
    assert record.args == ("WEB_ONLY_SENTINEL_1B8C",)
    assert record.getMessage() == "ordinary=WEB_ONLY_SENTINEL_1B8C"
