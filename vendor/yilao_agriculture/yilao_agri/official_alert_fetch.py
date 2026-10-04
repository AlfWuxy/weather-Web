"""按显式配置采集一次和风预警；认证、预算、请求和时钟均可注入。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .alert_http_body import decode_alert_body
from .qweather_alert_snapshot import MAX_RAW_BYTES, build_qweather_alert_feed

ALERT_RESULT_MESSAGES = {
    "ALERT_QUERY_OK": "已取得并私有留存官方预警查询结果。",
    "ALERT_NOT_CONFIGURED": "官方预警查询尚未配置，状态保持未知。",
    "ALERT_CONFIG_INVALID": "官方预警查询地址配置不符合要求，状态保持未知。",
    "ALERT_AUTH_UNAVAILABLE": "官方预警认证暂不可用，状态保持未知。",
    "ALERT_BUDGET_UNAVAILABLE": "官方预警查询额度未获确认，状态保持未知。",
    "ALERT_CLOCK_INVALID": "官方预警查询时钟无效，状态保持未知。",
    "ALERT_REQUEST_FAILED": "官方预警请求未完成，状态保持未知。",
    "ALERT_HTTP_ERROR": "官方预警服务未返回成功响应，状态保持未知。",
    "ALERT_AUTH_REJECTED": "官方预警认证被拒绝，状态保持未知。",
    "ALERT_REDIRECT_BLOCKED": "官方预警请求发生地址变更，已停止读取。",
    "ALERT_RESPONSE_INVALID": "官方预警响应不完整或无法核验，状态保持未知。",
    "ALERT_ARCHIVE_FAILED": "官方预警结果未能安全留存，状态保持未知。",
    "ALERT_CALLBACK_FAILED": "官方预警采集暂不可用，状态保持未知。",
    "ALERT_CALLBACK_INVALID": "官方预警采集结果无法核验，状态保持未知。",
}
ARCHIVE_SUBDIR = "qweather-alerts"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 在urllib构造下一次请求之前拒绝，不向新地址转送认证头。
        return None


class _Failure(Exception):
    def __init__(self, code, coverage="query_incomplete"):
        self.code, self.coverage = code, coverage
        super().__init__(code)


def _coordinate(value, limit):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and -limit <= value <= limit
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("官方预警查询需要范围内的有限地块坐标")
    return float(value)


def _url(api_base, latitude, longitude):
    if (not isinstance(api_base, str) or len(api_base) > 2048 or any(c in api_base for c in "?#")
            or any(ord(c) <= 32 or ord(c) == 127 for c in api_base)):
        raise ValueError("官方预警地址配置无效")
    parsed = urlsplit(api_base)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 443} or parsed.path not in {"/v7", "/v7/"}
            or re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+qweatherapi\.com", parsed.hostname or "") is None):
        raise ValueError("官方预警地址配置无效")
    # 使用浮点数的无损十进制表达，不按行政区、格点或固定小数位改变地块。
    lat, lon = (format(Decimal(str(value)), "f") for value in (latitude, longitude))
    return f"https://{parsed.netloc}/weatheralert/v1/current/{lat}/{lon}"


def _auth(provider):
    headers = provider()
    if not isinstance(headers, dict) or len(headers) != 1:
        raise ValueError("官方预警认证头无效")
    key, value = next(iter(headers.items()))
    if (not isinstance(key, str) or not isinstance(value, str) or not 1 <= len(value) <= 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in value if c != " ")
            or value != value.strip()):
        raise ValueError("官方预警认证头无效")
    if key.lower() == "authorization":
        matched = re.fullmatch(r"Bearer ([^\s]+)", value, re.IGNORECASE)
        if matched:
            return {"Authorization": "Bearer " + matched[1]}, matched[1]
    elif key.lower() == "x-qw-api-key" and " " not in value:
        return {"X-QW-Api-Key": value}, value
    raise ValueError("官方预警认证头无效")


def _instant(clock):
    value = clock()
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("官方预警查询时钟无效")
    return moment


def _status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _unknown(latitude, longitude, code, *, queried_at=None, coverage="not_queried", http_status=None):
    return {"feed": {"source": "QWeather official weather alerts", "protocol": "official_equivalent",
                     "issued_at": None, "queried_at": queried_at, "http_status": http_status,
                     "coverage_status": coverage,
                     "query": {"type": "point", "latitude": latitude, "longitude": longitude, "snapshot_complete": False},
                     "items": []}, "code": code, "message": ALERT_RESULT_MESSAGES[code]}


def _invalidate(callback):
    if callable(callback):
        try:
            callback()
        except Exception:
            pass


def _secret_echoed(raw, credential):
    # 完整原文不能一边删改凭据一边声称原文一致；服务若回显认证，整份拒绝。
    pending = [json.loads(raw)]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and (value == credential or value == "Bearer " + credential
                                       or (len(credential) >= 8 and credential in value)):
            return True
    return False


def _open_directory(name, parent_fd, *, private):
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise OSError("归档目录不属于当前服务用户")
        if private:
            os.fchmod(fd, 0o700)
        return fd
    except Exception:
        os.close(fd)
        raise


def _existing_matches(directory_fd, filename, content):
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size != len(content)):
            raise OSError("归档目标不是独立私有常规文件")
        chunks, remaining = [], len(content) + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != content:
            raise OSError("同名归档内容冲突")
    finally:
        os.close(fd)


def _archive(feed, archive_dir):
    content = json.dumps(feed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    filename = sha256(content).hexdigest() + ".json"
    root = Path(archive_dir).absolute()
    # 既有父目录由调用方配置；解析其真实位置后以目录fd锚定，根目录和子目录禁止符号链接。
    parent = root.parent.resolve(strict=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_fd = directory_fd = None
    temporary = None
    try:
        root_fd = _open_directory(root.name, parent_fd, private=False)
        directory_fd = _open_directory(ARCHIVE_SUBDIR, root_fd, private=True)
        temporary = ".pending-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("归档写入未完成")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        except FileExistsError:
            _existing_matches(directory_fd, filename, content)
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
    finally:
        if temporary is not None and directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        for fd in (directory_fd, root_fd, parent_fd):
            if fd is not None:
                os.close(fd)


def fetch_qweather_alerts(plot, archive_dir, *, api_base=None, auth_headers_provider=None,
                         budget_reserver=None, configured=False, opener=None, clock=None,
                         invalidate_auth=None) -> dict:
    """仅在配置、认证与预算通过后请求一次；未能私有留存时不返回可用的无预警结论。"""
    if not isinstance(plot, dict):
        raise ValueError("官方预警查询需要有效地块坐标")
    latitude, longitude = _coordinate(plot.get("latitude"), 90), _coordinate(plot.get("longitude"), 180)
    if configured is not True:
        return _unknown(latitude, longitude, "ALERT_NOT_CONFIGURED")
    try:
        request_url = _url(api_base, latitude, longitude)
    except Exception:
        return _unknown(latitude, longitude, "ALERT_CONFIG_INVALID")
    try:
        auth_headers, credential = _auth(auth_headers_provider)
    except Exception:
        return _unknown(latitude, longitude, "ALERT_AUTH_UNAVAILABLE")
    try:
        allowed = budget_reserver()
    except Exception:
        allowed = False
    if allowed is not True:
        return _unknown(latitude, longitude, "ALERT_BUDGET_UNAVAILABLE")
    # 配置和认证不进入URL、日志或返回对象；压缩体与解压后内容均须有界。
    request = urllib.request.Request(request_url, headers={**auth_headers, "Accept": "application/json", "Accept-Encoding": "gzip, identity"}, method="GET")
    try:
        transport = opener if opener is not None else urllib.request.build_opener(_NoRedirect()).open
        timer = clock if clock is not None else lambda: datetime.now(timezone.utc)
        queried = _instant(timer)
    except Exception:
        return _unknown(latitude, longitude, "ALERT_CLOCK_INVALID")
    queried_text = queried.isoformat()
    response = None
    http_status = None
    failure = None
    feed = None
    try:
        try:
            response = transport(request, timeout=12)
        except urllib.error.HTTPError as error:
            response = error
            http_status = _status(error.code)
            if http_status == 401:
                _invalidate(invalidate_auth)
            code = "ALERT_AUTH_REJECTED" if http_status == 401 else "ALERT_REDIRECT_BLOCKED" if http_status and 300 <= http_status < 400 else "ALERT_HTTP_ERROR"
            raise _Failure(code, "feed_unavailable") from None
        except Exception:
            raise _Failure("ALERT_REQUEST_FAILED", "feed_unavailable") from None
        http_status = _status(getattr(response, "status", None))
        if http_status is None:
            http_status = _status(response.getcode())
        if response.geturl() != request_url:
            raise _Failure("ALERT_REDIRECT_BLOCKED")
        if http_status != 200:
            if http_status == 401:
                _invalidate(invalidate_auth)
            code = "ALERT_AUTH_REJECTED" if http_status == 401 else "ALERT_REDIRECT_BLOCKED" if http_status and 300 <= http_status < 400 else "ALERT_HTTP_ERROR"
            raise _Failure(code, "feed_unavailable")
        encoding = response.headers.get("Content-Encoding", "identity")
        if hasattr(response.headers, "get_all"):
            # 真实 HTTP 头可重复；不能只取首个值而忽略叠加的编码。
            encodings = response.headers.get_all("Content-Encoding", [])
            if len(encodings) > 1:
                raise _Failure("ALERT_RESPONSE_INVALID")
        try:
            raw = response.read(MAX_RAW_BYTES + 1)
        except Exception:
            raise _Failure("ALERT_REQUEST_FAILED", "feed_unavailable") from None
        if not isinstance(raw, bytes) or len(raw) > MAX_RAW_BYTES:
            raise _Failure("ALERT_RESPONSE_INVALID")
        try:
            retrieved = _instant(timer)
        except Exception:
            raise _Failure("ALERT_CLOCK_INVALID") from None
        text = decode_alert_body(raw, encoding, max_bytes=MAX_RAW_BYTES)
        feed = build_qweather_alert_feed(text, request_url=request_url, latitude=latitude, longitude=longitude,
                    queried_at=queried, retrieved_at=retrieved, http_status=http_status, now=retrieved)
        if _secret_echoed(text, credential):
            raise _Failure("ALERT_RESPONSE_INVALID")
    except _Failure as error:
        failure = error
    except Exception:
        failure = _Failure("ALERT_RESPONSE_INVALID")
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                failure = _Failure("ALERT_RESPONSE_INVALID")
    if failure is not None:
        return _unknown(latitude, longitude, failure.code, queried_at=queried_text,
                        coverage=failure.coverage, http_status=http_status)
    try:
        _archive(feed, archive_dir)
    except Exception:
        return _unknown(latitude, longitude, "ALERT_ARCHIVE_FAILED", queried_at=queried_text,
                        coverage="query_incomplete", http_status=http_status)
    return {"feed": feed, "code": "ALERT_QUERY_OK", "message": ALERT_RESULT_MESSAGES["ALERT_QUERY_OK"]}
