"""和风官方预警响应的纯快照转换；内容哈希不是来源认证，不执行网络请求。"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit

SCHEMA = "qweather-alert-query-1"
PROVIDER = "qweather"
PRODUCT = "weatheralert/v1/current"
MAX_RAW_BYTES = 1_000_000
SNAPSHOT_FIELDS = frozenset({"schema", "provider", "product", "request_url", "latitude", "longitude",
                             "queried_at", "retrieved_at", "http_status", "raw_response_text", "raw_response_sha256"})
FEED_FIELDS = frozenset({"source", "protocol", "queried_at", "issued_at", "http_status", "query",
                         "coverage_status", "items", "attributions", "query_snapshot"})


def _fail(message):
    raise ValueError(message)


def _text(value):
    if not isinstance(value, str) or not value.strip():
        _fail("和风预警必需文本字段缺失或无效")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("和风预警文本字段不是有效 Unicode 文本") from None
    return value


def _instant(value, *, raw=False):
    try:
        if isinstance(value, str):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif not raw and isinstance(value, datetime):
            moment = value
        else:
            _fail("和风预警时间须为含时区的有效时刻")
        if moment.tzinfo is None or moment.utcoffset() is None:
            _fail("和风预警时间须为含时区的有效时刻")
        return moment
    except (ValueError, TypeError, OverflowError):
        raise ValueError("和风预警时间须为含时区的有效时刻") from None


def _coordinate(value, limit):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and -limit <= value <= limit
    except OverflowError:
        valid = False
    if not valid:
        _fail("和风预警坐标须为范围内的有限数字")
    return float(value)


def _request_url(value, latitude, longitude):
    if (not isinstance(value, str) or len(value) > 4096 or "#" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        _fail("和风预警请求地址格式无效")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443}
                or re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+qweatherapi\.com", host) is None):
            _fail("和风预警请求须使用无凭据的官方 HTTPS 主机与标准端口")
        number = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
        path = re.fullmatch(r"/weatheralert/v1/current/(" + number + ")/(" + number + ")", parsed.path)
        if path is None or float(path[1]) != latitude or float(path[2]) != longitude:
            _fail("和风预警请求路径坐标与地块坐标不一致")
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if len(parameters) != len({key for key, _ in parameters}):
            _fail("和风预警请求参数不得重复")
        for key, value in parameters:
            if key == "lang" and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?", value):
                continue
            if key == "localTime" and value in {"true", "false"}:
                continue
            _fail("和风预警请求仅允许有效的语言与当地时间参数，不得包含认证信息")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("和风预警请求地址、坐标或查询参数不符合官方查询契约") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("和风预警原响应含重复 JSON 字段")
        result[key] = value
    return result


def _no_constant(value):
    _fail("和风预警原响应不能含非有限 JSON 数字")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        _fail("和风预警原响应不能含溢出的 JSON 数字")
    return number


def _payload(raw_response):
    if not isinstance(raw_response, str):
        _fail("和风预警必须保留原始响应文本")
    try:
        encoded = raw_response.encode("utf-8")
        if len(encoded) > MAX_RAW_BYTES:
            _fail("和风预警原响应超过大小限制")
        payload = json.loads(raw_response, object_pairs_hook=_unique_object, parse_constant=_no_constant,
                             parse_float=_finite_float)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("和风预警原响应不是大小合规且字段唯一的有效 JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict) or not isinstance(payload.get("alerts"), list):
        _fail("和风预警原响应缺少完整元数据或预警列表")
    metadata, alerts = payload["metadata"], payload["alerts"]
    _text(metadata.get("tag"))
    if type(metadata.get("zeroResult")) is not bool or metadata["zeroResult"] != (len(alerts) == 0):
        _fail("和风预警无结果标记与预警列表不一致")
    # 真实成功空查询可省略归因数组；不改写原文，也不放行显式坏值或有消息的缺项。
    attributions = metadata.get("attributions", [] if metadata["zeroResult"] else None)
    if not isinstance(attributions, list):
        _fail("和风预警缺少归因列表")
    for attribution in attributions:
        _text(attribution)
    return payload, sha256(encoded).hexdigest()


def _active_items(alerts, retrieved):
    rows, references, issued_times = {}, {}, {}
    for row in alerts:
        required = {"id", "issuedTime", "messageType", "eventType", "effectiveTime", "onsetTime", "expireTime"}
        if not isinstance(row, dict) or not required <= row.keys():
            _fail("和风预警条目缺少生命周期必需字段")
        ident = _text(row["id"])
        if ident in rows:
            _fail("和风预警条目编号不得重复")
        message, event = row["messageType"], row["eventType"]
        if not isinstance(message, dict) or not isinstance(event, dict):
            _fail("和风预警消息类型或事件类型无效")
        code, supersedes = message.get("code"), message.get("supersedes")
        if not isinstance(code, str) or code not in {"alert", "update", "cancel"} or not isinstance(supersedes, list):
            _fail("和风预警消息类型或替换关系无效")
        for reference in supersedes:
            _text(reference)
        if len(supersedes) != len(set(supersedes)) or ident in supersedes:
            _fail("和风预警替换关系不得自指或重复")
        if (code == "alert" and supersedes) or (code in {"update", "cancel"} and not supersedes):
            _fail("和风预警初始、更新或取消消息的替换关系不完整")
        _text(event.get("name"))
        _text(event.get("code"))
        issued = _instant(row["issuedTime"], raw=True)
        _instant(row["effectiveTime"], raw=True)
        if row["onsetTime"] is not None:
            _instant(row["onsetTime"], raw=True)
        expires = _instant(row["expireTime"], raw=True)
        if issued > retrieved:
            _fail("和风预警签发时刻不能晚于响应取得时刻")
        # 生效、发生、签发没有统一先后关系；未来生效也保持阻挡。
        # 使用服务给出的截止时刻；不自己给取消消息补造一小时有效期。
        # 即使条目被替换或取消，返回的条目已过期仍拒绝整份异常响应。
        if expires <= retrieved:
            _fail("和风当前预警响应包含取得时已经到期的事件")
        rows[ident], references[ident] = row, supersedes
        issued_times[ident] = issued
    # 旧编号通常不再返回，缺席引用合法；只对本响应中的节点迭代查环。
    indegrees = dict.fromkeys(rows, 0)
    for ident, targets in references.items():
        for target in targets:
            if target in indegrees:
                if issued_times[target] > issued_times[ident]:
                    _fail("和风预警替换消息早于其所替换消息签发")
                indegrees[target] += 1
    pending = [ident for ident, degree in indegrees.items() if degree == 0]
    visited = 0
    while pending:
        ident = pending.pop()
        visited += 1
        for target in references[ident]:
            if target in indegrees:
                indegrees[target] -= 1
                if indegrees[target] == 0:
                    pending.append(target)
    if visited != len(rows):
        _fail("和风预警替换关系存在循环")
    replaced = {target for targets in references.values() for target in targets}
    items = []
    for ident, row in rows.items():
        if ident in replaced or row["messageType"]["code"] == "cancel":
            continue
        item = {"id": ident, "sent": row["issuedTime"], "hazard_tag": "official_weather_alert",
                "event": row["eventType"]["name"], "effective": row["effectiveTime"], "expires": row["expireTime"]}
        if row["onsetTime"] is not None:
            item["onset"] = row["onsetTime"]
        items.append(item)
    return items


def build_qweather_alert_feed(raw_response: str, *, request_url, latitude, longitude,
                              queried_at, retrieved_at, http_status, now) -> dict:
    """完整重建当前查询结果；过期/未知/错误不得转换成查询无预警。"""
    latitude, longitude = _coordinate(latitude, 90), _coordinate(longitude, 180)
    _request_url(request_url, latitude, longitude)
    if type(http_status) is not int or http_status != 200:
        _fail("和风预警仅接受 HTTP 200 的完整响应")
    query, retrieved, moment = [_instant(value) for value in (queried_at, retrieved_at, now)]
    if not query <= retrieved <= moment:
        _fail("和风预警查询、取得与当前时刻的顺序无效")
    payload, digest = _payload(raw_response)
    items = _active_items(payload["alerts"], retrieved)
    snapshot = {"schema": SCHEMA, "provider": PROVIDER, "product": PRODUCT, "request_url": request_url,
                "latitude": latitude, "longitude": longitude, "queried_at": query.isoformat(),
                "retrieved_at": retrieved.isoformat(), "http_status": http_status,
                "raw_response_text": raw_response, "raw_response_sha256": digest}
    return {"source": "QWeather official weather alerts", "protocol": "official_equivalent",
            "queried_at": query.isoformat(), "issued_at": None, "http_status": 200,
            "query": {"type": "point", "latitude": latitude, "longitude": longitude, "snapshot_complete": True},
            "coverage_status": "active_alerts" if items else "queried_clear", "items": items,
            "attributions": list(payload["metadata"].get("attributions", [])), "query_snapshot": snapshot}


def validate_qweather_snapshot(feed, now) -> None:
    """从原文重建后逐字段严格比较；新鲜度由共享模型检查，此处不随时间删预警。"""
    if not isinstance(feed, dict) or set(feed) != FEED_FIELDS:
        _fail("和风预警快照字段不完整或含额外覆盖字段")
    snapshot = feed.get("query_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS:
        _fail("和风预警查询快照字段不完整或含额外覆盖字段")
    try:
        rebuilt = build_qweather_alert_feed(snapshot["raw_response_text"], request_url=snapshot["request_url"],
                    latitude=snapshot["latitude"], longitude=snapshot["longitude"], queried_at=snapshot["queried_at"],
                    retrieved_at=snapshot["retrieved_at"], http_status=snapshot["http_status"], now=now)
        # JSON严格区分bool与数字，避免True等于1等Python宽松比较绕过。
        options = {"sort_keys": True, "ensure_ascii": False, "allow_nan": False, "separators": (",", ":")}
        if json.dumps(feed, **options) != json.dumps(rebuilt, **options):
            _fail("和风预警原文、摘要或派生字段不一致")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("和风预警快照校验失败，不能据此确认无预警") from None
