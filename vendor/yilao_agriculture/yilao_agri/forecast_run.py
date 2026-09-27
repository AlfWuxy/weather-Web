"""单次模式运行的来源与时间绑定；一致性校验不认证外部来源身份。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit

SOURCE = "Open-Meteo / ECMWF IFS 0.25° (single run)"
MODEL = "ecmwf_ifs025"
SCHEMA = "open-meteo-single-run-1"
MAX_RAW_BYTES = 1_000_000
ERROR = "FORECAST_RUN_SNAPSHOT_INVALID：单次运行天气证据缺失、不一致或时间不可用。"
VARIABLES = frozenset({"temperature_2m", "relative_humidity_2m", "wind_speed_10m", "shortwave_radiation", "precipitation", "is_day"})
RECORD_FIELDS = frozenset({"start", "end", "temperature_c", "relative_humidity_pct", "wind_m_s", "shortwave_w_m2", "precipitation_mm", "daylight"})
FIELDS = frozenset({"schema", "provider", "product", "model", "run", "initialised_at", "archive_created_at", "request_url", "metadata_url",
    "queried_at", "retrieved_at", "metadata_queried_at", "metadata_retrieved_at", "raw_response_text", "raw_response_sha256", "raw_metadata_text", "raw_metadata_sha256"})


def _fail():
    raise ValueError(ERROR)


def _instant(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            _fail()
        return moment.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(ERROR) from None


def _number(value, low, high):
    if type(value) not in (int, float):
        _fail()
    try:
        if not math.isfinite(value) or not low <= value <= high:
            _fail()
    except OverflowError:
        _fail()
    return float(value)


def _text(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW_BYTES:
        _fail()
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError(ERROR) from None


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            _fail()
        return result

    try:
        if not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= MAX_RAW_BYTES:
            _fail()
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: _fail(), parse_float=finite_float)
        if not isinstance(result, dict):
            _fail()
        return result
    except (TypeError, ValueError, RecursionError, UnicodeError, OverflowError):
        raise ValueError(ERROR) from None


def _url(value, host, path):
    try:
        if (not isinstance(value, str) or len(value) > 4096 or "#" in value
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
            _fail()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != host or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.path != path):
            _fail()
        return parsed
    except (TypeError, ValueError):
        raise ValueError(ERROR) from None


def _request(value):
    parsed = _url(value, "single-runs-api.open-meteo.com", "/v1/forecast")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        required = {"latitude", "longitude", "models", "run", "hourly", "wind_speed_unit", "timezone"}
        if len(pairs) != len(query) or not required <= set(query) or set(query) - required - {"temperature_unit", "precipitation_unit"}:
            _fail()
        if (query["models"] != MODEL or query["timezone"] != "GMT" or query["wind_speed_unit"] != "ms"
                or query.get("temperature_unit", "celsius") != "celsius" or query.get("precipitation_unit", "mm") != "mm"):
            _fail()
        hourly = query["hourly"].split(",")
        if len(hourly) != len(VARIABLES) or set(hourly) != VARIABLES:
            _fail()
        # 接口 run 为 UTC 初始化时刻；请求中必须显式指定，不能使用 latest/Best Match。
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T(?:00|06|12|18):00", query["run"]) is None:
            _fail()
        run = query["run"]
        if not run.endswith("Z") and not run.endswith("+00:00"):
            run += "+00:00"
        initialised = _instant(run)
        lat = _number(float(query["latitude"]), -90, 90)
        lon = _number(float(query["longitude"]), -180, 180)
        return initialised, lat, lon
    except (TypeError, ValueError, OverflowError):
        raise ValueError(ERROR) from None


@lru_cache(maxsize=16)
def _parse_bundle(response_text, metadata_text, request_url, metadata_url):
    """只缓存不可变原文的纯解析；决策时间与调用方天气内容每次重新核对。"""
    initialised, lat, lon = _request(request_url)
    path = initialised.strftime(f"/data_run/{MODEL}/%Y/%m/%d/%H00Z/meta.json")
    meta_url = _url(metadata_url, "openmeteo.s3.amazonaws.com", path)
    if meta_url.query or "?" in metadata_url:
        _fail()
    payload, metadata = _json(response_text), _json(metadata_text)
    created = _instant(metadata.get("created_at"))
    if _instant(metadata.get("reference_time")) != initialised or created < initialised:
        _fail()
    valid_times = metadata.get("valid_times")
    if not isinstance(valid_times, list) or not 2 <= len(valid_times) <= 1000:
        _fail()
    valid = [_instant(t) for t in valid_times]
    if valid[0] != initialised or any(b <= a for a, b in zip(valid, valid[1:])):
        _fail()
    if type(metadata.get("temporal_resolution_seconds")) is not int or metadata["temporal_resolution_seconds"] <= 0:
        _fail()
    resolution = metadata["temporal_resolution_seconds"]
    if any((b - a).total_seconds() != resolution for a, b in zip(valid, valid[1:])):
        # IFS 00/12Z 的公开时序为前144小时每3小时，之后至360小时每6小时。
        # 元数据仍声明基础3小时间隔；只接纳这条明确时序，不把任意缺口当作变步长。
        if resolution != 10800 or initialised.hour not in {0, 12}:
            _fail()
        for start, end in zip(valid, valid[1:]):
            lead = (start - initialised).total_seconds()
            expected = 10800 if lead < 144 * 3600 else 21600
            if (end - start).total_seconds() != expected or end - initialised > timedelta(hours=360):
                _fail()
    variables = metadata.get("variables")
    if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
        _fail()
    # 风速和日光可能由提供方从分量/天文计算派生，不假装 metadata 必须列出派生名。
    if not {"temperature_2m", "relative_humidity_2m", "shortwave_radiation", "precipitation"} <= set(variables):
        _fail()
    if "wind_speed_10m" not in variables and not {"wind_u_component_10m", "wind_v_component_10m"} <= set(variables):
        _fail()
    grid_lat = _number(payload.get("latitude"), -90, 90)
    grid_lon = _number(payload.get("longitude"), -180, 180)
    if (type(payload.get("utc_offset_seconds")) not in (int, float) or payload["utc_offset_seconds"] != 0
            or payload.get("timezone") not in {"GMT", "UTC"}):
        _fail()
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or set(hourly) != VARIABLES | {"time"}:
        _fail()
    units = payload.get("hourly_units")
    if not isinstance(units, dict) or units.get("time") != "iso8601" or units.get("is_day") != "":
        _fail()
    times = hourly.get("time")
    if not isinstance(times, list) or not 2 <= len(times) <= 1000:
        _fail()
    # GMT 响应的无偏移小时串仅在明确 utc_offset_seconds=0 后解释为 UTC。
    moments = []
    for value in times:
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:00(?::00)?(?:Z|\+00:00)?", value) is None:
            _fail()
        moments.append(_instant(value if value.endswith(("Z", "+00:00")) else value + "+00:00"))
    if moments[0] != initialised or any(b - a != timedelta(hours=1) for a, b in zip(moments, moments[1:])):
        _fail()
    limits = {"temperature_2m": (-100, 100), "relative_humidity_2m": (0, 100),
              "wind_speed_10m": (0, 1000), "shortwave_radiation": (0, 100000), "precipitation": (0, 100000), "is_day": (0, 1)}
    for key, bounds in limits.items():
        values = hourly.get(key)
        if not isinstance(values, list) or len(values) != len(times):
            _fail()
        for value in values:
            if value is None and key != "is_day":
                continue  # 真实缺测只形成缺口，不能补零，也不能偷偷删掉原文。
            _number(value, *bounds)
            if key == "is_day" and value not in (0, 1):
                _fail()
    # 延迟导入以免 models -> forecast_run -> weather -> models 形成顶层循环。
    from .weather import ingest_provider_response
    # 本次调用只复用逐小时映射；临时 parser provenance 不输出。
    # 真正的实际取得时刻仅来自快照四个显式时点，不取此处映射参数。
    parsed = ingest_provider_response("open_meteo_hosted_free", payload, retrieved_at=created.isoformat(),
        requested_location={"latitude": lat, "longitude": lon}, issued_at=None,
        model_id=MODEL, product="forecast", request_url=request_url)
    if (not parsed["ok"] or any(e["code"] != "WEATHER_ISSUED_TIME_UNKNOWN" for e in parsed["errors"])
            or not parsed["aligned_records"]):
        _fail()
    records = parsed["aligned_records"]
    if any(set(row) != RECORD_FIELDS or _instant(row["end"]) > valid[-1] for row in records):
        _fail()
    # 只声明提供方返回的模式代表坐标；不是站点/地块实测，也未取得单元几何。
    # 不虚构 grid_cell 所要求的 cell_id 或公里分辨率；既有距离策略仍单独核对。
    return initialised, created, records, {"kind": "point", "latitude": grid_lat, "longitude": grid_lon,
        "environment": "outdoor", "source": SOURCE}


def make_forecast_run_snapshot(raw_response: bytes, raw_metadata: bytes, *, request_url, metadata_url,
                               queried_at, retrieved_at, metadata_queried_at, metadata_retrieved_at) -> dict:
    """绑定实际取得的原文；created_at 仅表示归档生成，不能称为 API 可用时刻。"""
    response_text, metadata_text = _text(raw_response), _text(raw_metadata)
    try:
        initialised, created, _, _ = _parse_bundle(response_text, metadata_text, request_url, metadata_url)
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError):
        raise ValueError(ERROR) from None
    snapshot = {"schema": SCHEMA, "provider": "open_meteo", "product": "single-run", "model": MODEL,
        "run": initialised.isoformat(), "initialised_at": initialised.isoformat(), "archive_created_at": created.isoformat(),
        "request_url": request_url, "metadata_url": metadata_url,
        "queried_at": _instant(queried_at).isoformat(), "retrieved_at": _instant(retrieved_at).isoformat(),
        "metadata_queried_at": _instant(metadata_queried_at).isoformat(), "metadata_retrieved_at": _instant(metadata_retrieved_at).isoformat(),
        "raw_response_text": response_text, "raw_response_sha256": sha256(raw_response).hexdigest(),
        "raw_metadata_text": metadata_text, "raw_metadata_sha256": sha256(raw_metadata).hexdigest()}
    _validated(snapshot, max(_instant(retrieved_at), _instant(metadata_retrieved_at)))
    return snapshot


def _validated(snapshot, decision_at):
    try:
        if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
            _fail()
        if (snapshot["schema"] != SCHEMA or snapshot["provider"] != "open_meteo"
                or snapshot["product"] != "single-run" or snapshot["model"] != MODEL):
            _fail()
        for name in ("response", "metadata"):
            text = snapshot[f"raw_{name}_text"]
            if (not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= MAX_RAW_BYTES
                    or sha256(text.encode("utf-8")).hexdigest() != snapshot[f"raw_{name}_sha256"]):
                _fail()
        initialised, created, records, grid = _parse_bundle(snapshot["raw_response_text"], snapshot["raw_metadata_text"], snapshot["request_url"], snapshot["metadata_url"])
        if (_instant(snapshot["run"]) != initialised or _instant(snapshot["initialised_at"]) != initialised
                or _instant(snapshot["archive_created_at"]) != created):
            _fail()
        decision = _instant(decision_at)
        for query_key, retrieve_key in (("queried_at", "retrieved_at"), ("metadata_queried_at", "metadata_retrieved_at")):
            queried, retrieved = _instant(snapshot[query_key]), _instant(snapshot[retrieve_key])
            if not queried <= retrieved <= decision or not created <= retrieved:
                _fail()
        return initialised, records, grid
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ValueError(ERROR) from None


def derive_run_weather(snapshot) -> dict:
    """仅由快照重建基础天气；不添加预警结论、不改变原始实际取得时刻。"""
    try:
        _, records, grid = _validated(snapshot, max(_instant(snapshot["retrieved_at"]), _instant(snapshot["metadata_retrieved_at"])))
        return {"source": SOURCE, "issued_at": None, "kind": "forecast", "environment": "outdoor",
            "records": deepcopy(records), "grid": deepcopy(grid), "forecast_run_snapshot": deepcopy(snapshot)}
    except (TypeError, KeyError, ValueError):
        raise ValueError(ERROR) from None


def validate_run_snapshot(weather, decision_at: datetime) -> datetime:
    """返回初始化时刻作为保守年龄/跨度起点；每次检查当前决策及天气投影。"""
    try:
        if (not isinstance(weather, dict) or "issued_at" not in weather or weather["issued_at"] is not None
                or weather.get("source") != SOURCE or weather.get("kind") != "forecast" or weather.get("environment") != "outdoor"):
            _fail()
        initialised, records, grid = _validated(weather.get("forecast_run_snapshot"), decision_at)
        actual = weather.get("records")
        if not isinstance(actual, list) or len(actual) != len(records):
            _fail()
        for row, expected in zip(actual, records):
            if not isinstance(row, dict) or not RECORD_FIELDS <= set(row) or set(row) - RECORD_FIELDS - {"hazards", "hazard_item_ids"}:
                _fail()
            if json.dumps({key: row[key] for key in RECORD_FIELDS}, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
                _fail()
        if json.dumps(weather.get("grid"), sort_keys=True, allow_nan=False) != json.dumps(grid, sort_keys=True, allow_nan=False):
            _fail()
        if "retrieved_at" in weather and _instant(weather["retrieved_at"]) != _instant(weather["forecast_run_snapshot"]["retrieved_at"]):
            _fail()
        return initialised
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError):
        raise ValueError(ERROR) from None


def validate_run_location(snapshot, plot) -> None:
    """请求点必须就是本次地块；返回网格点另交给既有网格距离策略核对。"""
    try:
        _, latitude, longitude = _request(snapshot["request_url"])
        if latitude != _number(plot.get("latitude"), -90, 90) or longitude != _number(plot.get("longitude"), -180, 180):
            _fail()
    except (TypeError, AttributeError, KeyError, ValueError):
        raise ValueError(ERROR) from None
