"""天气提供者适配层：解析已保存的小时响应。

只登记不购买；不外发 HTTP；不把检索时刻写成签发时刻。
按冻结 schema 1.0 映射五字段，原字段/单位/格点/检索时间另存 provenance。
"""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlencode
from typing import Any

from .models import InputError, parse_time


# R04-A09：只登记。付费档未订阅，本模块不构造、不解析 customer-api。
REGISTERED_PROVIDERS = {
    "open_meteo_hosted_free": {
        "display_name": "Open-Meteo 免费托管 Forecast API",
        "endpoint": "https://api.open-meteo.com/v1/forecast",
        "ingest_json": True,
        "purchase_status": "not_purchased",
        "commercial_hosted": "forbidden_on_free_tier",
        "default_product": "forecast",
        "warning_product": False,
    },
    "open_meteo_hosted_paid": {
        "display_name": "Open-Meteo 付费 customer API",
        "endpoint": "https://customer-api.open-meteo.com",
        "ingest_json": False,
        "purchase_status": "not_purchased",
        "reject_code": "PROVIDER_NOT_PURCHASED",
        "default_product": "forecast",
        "warning_product": False,
    },
    "open_meteo_selfhost": {
        "display_name": "自建 Open-Meteo 服务",
        "endpoint": None,
        "ingest_json": True,
        "purchase_status": "not_purchased",
        "deployed": False,
        "default_product": "forecast",
        "warning_product": False,
    },
    "ecmwf_open_data": {
        "display_name": "ECMWF Open Data GRIB2",
        "endpoint": "https://data.ecmwf.int/forecasts/",
        "ingest_json": False,
        "purchase_status": "not_purchased",
        "reject_code": "PROVIDER_NOT_JSON_HOURLY",
        "default_product": "forecast",
        "warning_product": False,
    },
    "api_weather_gov": {
        "display_name": "NWS api.weather.gov",
        "endpoint": "https://api.weather.gov",
        "ingest_json": False,
        "purchase_status": "not_purchased",
        "reject_code": "PROVIDER_WRONG_GEOGRAPHY",
        "covers_duchang": False,
        "warning_product": True,
    },
    "nws_web_and_formula_pages": {
        "display_name": "NOAA/NWS 热指数公式页",
        "endpoint": None,
        "ingest_json": False,
        "purchase_status": "not_purchased",
        "reject_code": "PROVIDER_METHOD_PAGE_NOT_API",
        "warning_product": False,
    },
}

# schema 1.0 闭集；provenance 不得写入这些以外的键。
_SCHEMA_WEATHER_KEYS = (
    "source", "issued_at", "kind", "environment", "records",
)
_SCHEMA_RECORD_KEYS = (
    "start", "end", "temperature_c", "relative_humidity_pct", "wind_m_s",
    "shortwave_w_m2", "precipitation_mm", "daylight",
)

# Open-Meteo 文档：瞬时温湿风；降水与短波为前一小时合计/平均。默认风速单位 kmh。
SI_HOURLY_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "shortwave_radiation",
    "precipitation",
)
SI_UNITS = {
    "temperature_2m": (("c", "celsius"), ("f", "fahrenheit")),
    "relative_humidity_2m": (("%", "percent", "pct"), ()),
    "wind_speed_10m": (("m/s", "ms", "ms-1", "m/sec"), ("km/h", "kmh", "mph", "kn", "kt", "knot")),
    "shortwave_radiation": (("w/m2", "wm-2", "wm2"), ()),
    "precipitation": (("mm", "millimeter", "millimetre"), ("inch", "in")),
}
# 累计场取 time[i+1]；瞬时场取 time[i]。
ACCUM_VARS = frozenset({"shortwave_radiation", "precipitation"})
INSTANT_VARS = frozenset({"temperature_2m", "relative_humidity_2m", "wind_speed_10m"})
SCHEMA_FIELD = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "relative_humidity_pct",
    "wind_speed_10m": "wind_m_s",
    "shortwave_radiation": "shortwave_w_m2",
    "precipitation": "precipitation_mm",
}
REANALYSIS_PRODUCTS = frozenset({
    "era5", "era5-land", "era5t", "cerra", "reanalysis",
})
STITCHED_PRODUCTS = frozenset({
    "historical_forecast", "stitched_short_lead",
})
PAID_HOST_MARKERS = ("customer-api.open-meteo.com",)
FORECAST_NOT_ALERT = "not an official warning product"


def list_registered_providers() -> list[dict]:
    """只读登记表。不创建账号、不打开 checkout。"""
    rows = []
    for provider_id, meta in REGISTERED_PROVIDERS.items():
        row = {"provider_id": provider_id}
        row.update(meta)
        rows.append(row)
    return rows


def build_open_meteo_forecast_url(
    latitude: float,
    longitude: float,
    *,
    timezone: str = "Asia/Shanghai",
    forecast_days: int = 3,
    model: str = "ecmwf_ifs025",
    host: str = "https://api.open-meteo.com",
) -> str:
    """构造免费档 SI 单位 URL。不发请求，不带 apikey，不指向付费主机。"""
    if not isinstance(host, str) or any(mark in host for mark in PAID_HOST_MARKERS):
        raise InputError("build_open_meteo_forecast_url: PROVIDER_NOT_PURCHASED 不构造付费主机 URL")
    if "apikey" in host.lower():
        raise InputError("build_open_meteo_forecast_url: 不得把 API key 写入 URL")
    query = urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(SI_HOURLY_VARS + ("is_day",)),
        "forecast_days": forecast_days,
        "timezone": timezone,
        "temperature_unit": "celsius",
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "models": model,
    })
    return f"{host.rstrip('/')}/v1/forecast?{query}"


def fetch_provider_response(*_args, **_kwargs) -> dict:
    """关闭网络抓取。本轮只用已保存响应，不外发、不购买。"""
    return _result(
        ok=False,
        errors=[{
            "code": "NETWORK_FETCH_DISABLED",
            "path": "fetch",
            "message": "本适配层只解析已保存响应；不外发 HTTP、不购买付费档、不带 API key。",
        }],
        provenance={
            "retrieved_at": None,
            "purchase_status": "not_purchased",
            "network": "disabled",
        },
    )


def ingest_provider_response(
    provider_id: str,
    payload: Any,
    *,
    retrieved_at: str,
    requested_location: dict | None = None,
    plot_id: str = "plot",
    environment: str = "outdoor",
    issued_at: str | None = None,
    product: str | None = None,
    model_id: str | None = None,
    request_url: str | None = None,
) -> dict:
    """读取提供者 JSON，保存原字段/单位/位置/检索时间，并尝试映射 schema 1.0。

    不修改 payload。缺签发时刻或日光时关闭 schema_weather，不编造。
    """
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    original = payload if isinstance(payload, dict) else None

    def reject(code: str, path: str, message: str) -> None:
        item = {"code": code, "path": path, "message": message}
        if item not in errors:
            errors.append(item)

    def warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    registry = REGISTERED_PROVIDERS.get(provider_id)
    if registry is None:
        reject("PROVIDER_UNKNOWN", "provider_id", f"未登记的提供者：{provider_id}。")
        return _result(ok=False, errors=errors, warnings=warnings)

    if request_url and any(mark in request_url for mark in PAID_HOST_MARKERS):
        reject("PROVIDER_NOT_PURCHASED", "request_url",
               "customer-api.open-meteo.com 未购买，拒绝解析付费档响应。")
        return _result(ok=False, errors=errors, warnings=warnings, provenance={
            "provider_id": provider_id,
            "purchase_status": "not_purchased",
            "request_url_host_rejected": True,
        })
    if request_url and "apikey=" in request_url.lower():
        reject("API_KEY_FORBIDDEN", "request_url", "不得携带 API key。")
        return _result(ok=False, errors=errors, warnings=warnings)

    if not registry.get("ingest_json"):
        code = registry.get("reject_code") or "PROVIDER_NOT_INGESTIBLE"
        reject(code, "provider_id", f"{registry.get('display_name')} 不作为本适配层小时 JSON 源。")
        return _result(ok=False, errors=errors, warnings=warnings, provenance={
            "provider_id": provider_id,
            "purchase_status": registry.get("purchase_status", "not_purchased"),
            "retrieved_at": retrieved_at,
        })

    if original is None:
        reject("PAYLOAD_NOT_OBJECT", "payload", "提供者响应必须为 JSON 对象。")
        return _result(ok=False, errors=errors, warnings=warnings)

    try:
        retrieved = parse_time(retrieved_at)
        retrieved_stamp = retrieved.isoformat()
    except InputError:
        reject("RETRIEVED_AT_INVALID", "retrieved_at", "检索时间必须是含时区的 ISO 8601 字符串。")
        retrieved_stamp = None

    product_name = product or registry.get("default_product") or "forecast"
    product_key = str(product_name).strip().lower()

    provenance = {
        "provider_id": provider_id,
        "purchase_status": registry.get("purchase_status", "not_purchased"),
        "network": "not_used",
        "retrieved_at": retrieved_stamp,
        "requested_location": _location(requested_location) if requested_location is not None else None,
        "returned_location": {
            "latitude": original.get("latitude"),
            "longitude": original.get("longitude"),
            "elevation": original.get("elevation"),
        },
        "timezone": original.get("timezone"),
        "timezone_abbreviation": original.get("timezone_abbreviation"),
        "utc_offset_seconds": original.get("utc_offset_seconds"),
        "generationtime_ms": original.get("generationtime_ms"),
        "hourly_units": deepcopy(original.get("hourly_units")),
        "original_hourly_fields": (
            sorted(original["hourly"].keys())
            if isinstance(original.get("hourly"), dict) else []
        ),
        "original_hourly": deepcopy(original.get("hourly")),
        "product": product_name,
        "model_id": model_id,
        "warning_product": False,
        "mapping_rule": "R-ALIGN-VALID-TIME: 温湿风=time[i]；短波/降水=time[i+1]；区间 [time[i], time[i+1])。",
        "height_metadata": {
            "temperature_height_m": 2,
            "humidity_height_m": 2,
            "wind_height_m": 10,
            "wind_is_gust": False,
            "radiation_is_ghi_mean": True,
            "precipitation_is_preceding_hour_sum": True,
        },
    }
    if registry.get("deployed") is False:
        warn("open_meteo_selfhost 已登记未部署；本次只解析调用方提供的 JSON。")

    if retrieved_stamp is None:
        return _result(ok=False, errors=errors, warnings=warnings, provenance=provenance)

    issued_stamp = None
    if issued_at is not None:
        try:
            issued_stamp = parse_time(issued_at).isoformat()
        except InputError:
            reject("WEATHER_ISSUED_TIME_UNKNOWN", "issued_at",
                   "签发时刻必须是含时区的 ISO 8601；不得用 generationtime_ms 或检索时刻冒充。")
        else:
            if issued_stamp == retrieved_stamp:
                warn("issued_at 与 retrieved_at 相同；适配层未自动抄检索时刻，请确认来自模式元数据而非抓取时钟。")
    else:
        reject("WEATHER_ISSUED_TIME_UNKNOWN", "issued_at",
               "Open-Meteo 小时 JSON 无 issued_at；不得用 retrieved_at 或 generationtime_ms 填签发时刻。")
    if original.get("generationtime_ms") is not None:
        warn("generationtime_ms 是服务器生成耗时，不是预报签发时刻。")

    hourly = original.get("hourly")
    units = original.get("hourly_units")
    if not isinstance(hourly, dict):
        reject("HOURLY_MISSING", "hourly", "缺少 hourly 对象。")
        return _result(ok=False, errors=errors, warnings=warnings, provenance=provenance)
    if not isinstance(units, dict):
        reject("UNITS_MISSING", "hourly_units", "缺少 hourly_units，不能假定 SI。")
        units = {}

    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        reject("TIME_MISSING", "hourly.time", "缺少 hourly.time。")
        return _result(ok=False, errors=errors, warnings=warnings, provenance=provenance)

    offset_s = original.get("utc_offset_seconds")
    # 零偏移是合法 UTC；必须区分有效的 0 与数值校验失败的 None。
    if offset_s is not None and _finite_number(offset_s) is None:
        reject("UTC_OFFSET_INVALID", "utc_offset_seconds", "utc_offset_seconds 必须为有限数值。")
        offset_s = None

    unit_ok = True
    for var, (allowed, forbidden) in SI_UNITS.items():
        got = units.get(var)
        if got is None or (isinstance(got, str) and not got.strip()):
            reject("UNIT_UNKNOWN", f"hourly_units.{var}",
                   f"{var} 未给出单位，不能假定 SI。")
            unit_ok = False
            continue
        token = _norm_unit(got)
        if any(token == _norm_unit(item) or _norm_unit(item) in token for item in forbidden):
            reject("UNIT_NOT_SI", f"hourly_units.{var}",
                   f"{var} 单位为 {got!r}，拒绝静默换算。需要 SI。")
            unit_ok = False
        elif not any(token == _norm_unit(item) or _norm_unit(item) in token for item in allowed):
            reject("UNIT_NOT_SI", f"hourly_units.{var}",
                   f"{var} 单位为 {got!r}，不是本适配层接受的 SI 单位。")
            unit_ok = False

    for var in SI_HOURLY_VARS:
        values = hourly.get(var)
        if not isinstance(values, list) or len(values) != len(times):
            reject("FIELD_LENGTH_MISMATCH", f"hourly.{var}",
                   f"{var} 缺失或长度与 time 不一致。")

    if "weather_code" in hourly:
        warn("hourly.weather_code 是 WMO 天气代码，不是官方预警，不得写入 records[].hazards。")
    if "et0_fao_evapotranspiration" in hourly:
        warn("et0_fao_evapotranspiration 不得填入硬约束五字段。")
    if "wind_gusts_10m" in hourly:
        warn("阵风不得写入 wind_m_s；wind_m_s 只接受 10 m 平均风速。")

    kind = None
    if product_key in REANALYSIS_PRODUCTS:
        reject("KIND_ERA5_NOT_FORECAST", "product",
               "再分析不得写成 kind=forecast。")
    elif product_key in STITCHED_PRODUCTS:
        reject("KIND_STITCHED_NOT_FORECAST", "product",
               "拼接短时效不是当时完整预报批次，不得写成 kind=forecast。")
    elif product_key == "forecast":
        kind = "forecast"
    else:
        reject("KIND_UNKNOWN", "product", f"不支持的产品类型：{product_name}。")

    if environment not in {"outdoor", "greenhouse"}:
        reject("PLOT_ENVIRONMENT_UNKNOWN", "environment", "地块环境须为 outdoor 或 greenhouse。")
        environment = None  # type: ignore[assignment]

    aligned: list[dict] = []
    omitted: list[dict] = []
    daylight_complete = True
    for i in range(len(times) - 1):
        start = _aware_stamp(times[i], offset_s, reject, f"hourly.time[{i}]")
        end = _aware_stamp(times[i + 1], offset_s, reject, f"hourly.time[{i + 1}]")
        if start is None or end is None:
            omitted.append({"index": i, "reason": "TIME_INVALID"})
            continue
        try:
            if parse_time(end) <= parse_time(start):
                reject("WEATHER_RECORD_INTERVAL_INVALID", f"hourly.time[{i}]",
                       "记录区间必须具有正时长。")
                omitted.append({"index": i, "start": start, "end": end, "reason": "NONPOSITIVE_INTERVAL"})
                continue
        except InputError:
            omitted.append({"index": i, "reason": "TIME_INVALID"})
            continue

        mapped: dict[str, Any] = {"start": start, "end": end}
        null_fields: list[str] = []
        invalid_fields: list[str] = []
        for var in SI_HOURLY_VARS:
            values = hourly.get(var)
            if not isinstance(values, list) or len(values) != len(times):
                null_fields.append(var)
                continue
            src_index = i + 1 if var in ACCUM_VARS else i
            raw = values[src_index]
            if raw is None:
                null_fields.append(var)
                continue
            number = _finite_number(raw)
            if number is None:
                invalid_fields.append(var)
                continue
            mapped[SCHEMA_FIELD[var]] = number
        if null_fields or invalid_fields:
            reason = "NULL_NOT_ZERO" if null_fields else "WEATHER_VALUE_UNKNOWN"
            omitted.append({
                "index": i, "start": start, "end": end, "reason": reason,
                "null_fields": null_fields, "invalid_fields": invalid_fields,
            })
            continue

        is_day_values = hourly.get("is_day")
        if isinstance(is_day_values, list) and len(is_day_values) == len(times):
            flag = _is_day(is_day_values[i])
            if flag is None:
                daylight_complete = False
                omitted.append({
                    "index": i, "start": start, "end": end,
                    "reason": "DAYLIGHT_UNKNOWN", "null_fields": ["is_day"],
                })
                continue
            mapped["daylight"] = flag
        else:
            daylight_complete = False

        aligned.append(mapped)

    provenance["omitted_intervals"] = omitted
    provenance["n_source_timestamps"] = len(times)
    provenance["n_aligned_intervals"] = len(aligned)
    provenance["n_omitted_intervals"] = len(omitted)

    if not unit_ok:
        return _result(
            ok=False, errors=errors, warnings=warnings, provenance=provenance,
            aligned_records=[], omitted=omitted,
        )

    if not aligned:
        reject("NO_USABLE_INTERVAL", "hourly",
               "没有五字段均有限的半开区间；缺测不得改写成 0。")

    schema_weather = None
    if (
        aligned
        and unit_ok
        and kind == "forecast"
        and issued_stamp
        and environment in {"outdoor", "greenhouse"}
        and daylight_complete
        and all("daylight" in row for row in aligned)
        and not any(item["code"] in {
            "KIND_ERA5_NOT_FORECAST", "KIND_STITCHED_NOT_FORECAST", "KIND_UNKNOWN",
            "WEATHER_ISSUED_TIME_UNKNOWN",
        } for item in errors)
    ):
        source = _source_text(provider_id, model_id)
        records = []
        for row in aligned:
            record = {key: row[key] for key in _SCHEMA_RECORD_KEYS if key in row}
            records.append(record)
        schema_weather = {
            "source": source,
            "issued_at": issued_stamp,
            "kind": "forecast",
            "environment": environment,
            "records": records,
        }
        warn("schema_weather 省略 hazards；小时预报不是预警源，不能写成已核对无预警。")
    else:
        if aligned and not daylight_complete:
            reject("DAYLIGHT_UNKNOWN", "hourly.is_day",
                   "payload 无 is_day 时不发明 daylight，schema_weather 关闭。")
        if aligned and kind != "forecast":
            warn("已保存原字段，但产品类型不允许写入 kind=forecast 的 schema_weather。")

    ok = bool(aligned) and unit_ok and retrieved_stamp is not None
    return _result(
        ok=ok,
        errors=errors,
        warnings=warnings,
        provenance=provenance,
        aligned_records=aligned,
        omitted=omitted,
        schema_weather=schema_weather,
        plot_id=plot_id,
    )


def attach_schema_weather(request: dict, plot_id: str, result: dict) -> dict:
    """把闭集 schema_weather 写入请求副本。不写入 provenance，不发明预警。"""
    block = result.get("schema_weather")
    if not isinstance(block, dict):
        raise InputError(f"weather.{plot_id}: SCHEMA_WEATHER_UNAVAILABLE 不能把未完成的适配结果写入请求")
    extra = [key for key in block if key not in _SCHEMA_WEATHER_KEYS]
    if extra:
        raise InputError(f"weather.{plot_id}: 未知字段 {extra[0]}")
    out = deepcopy(request)
    weather = dict(out.get("weather") or {})
    weather[plot_id] = deepcopy(block)
    out["weather"] = weather
    return out


def _result(
    *,
    ok: bool,
    errors: list | None = None,
    warnings: list | None = None,
    provenance: dict | None = None,
    aligned_records: list | None = None,
    omitted: list | None = None,
    schema_weather: dict | None = None,
    plot_id: str | None = None,
) -> dict:
    return {
        "ok": ok,
        "plot_id": plot_id,
        "schema_weather": schema_weather,
        "aligned_records": aligned_records or [],
        "omitted": omitted or [],
        "provenance": provenance or {},
        "errors": errors or [],
        "warnings": warnings or [
            "适配通过只表示已保存原响应并完成 SI 映射，不是田间准确率或个人健康安全。",
        ],
        "meaning": "保存原字段、单位、返回格点与检索时间；不购买、不外发、不把空 hazards 写成无预警。",
    }


def _norm_unit(value: Any) -> str:
    text = str(value).strip().lower().replace(" ", "").replace("²", "2").replace("°", "")
    return text


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _is_day(value: Any) -> bool | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value == 1:
        return True
    if value == 0:
        return False
    return None


def _offset_suffix(seconds: float) -> str:
    total = int(seconds)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def _stamp_has_offset(text: str) -> bool:
    if text.endswith("Z") or text.endswith("z"):
        return True
    if len(text) >= 6 and text[-3] == ":" and text[-6] in "+-":
        return True
    return False


def _aware_stamp(stamp: Any, offset_s: Any, reject, path: str) -> str | None:
    if not isinstance(stamp, str) or not stamp.strip():
        reject("TIME_INVALID", path, "时间戳必须为字符串。")
        return None
    candidate = stamp
    if not _stamp_has_offset(stamp):
        if offset_s is None:
            reject("UTC_OFFSET_MISSING", "utc_offset_seconds",
                   "hourly.time 无时区时必须提供 utc_offset_seconds，不能猜测。")
            return None
        candidate = stamp + _offset_suffix(offset_s)
    try:
        return parse_time(candidate).isoformat()
    except InputError:
        reject("TIME_INVALID", path, "无法解析为含时区时间。")
        return None


def _location(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        "latitude": value.get("latitude"),
        "longitude": value.get("longitude"),
    }


def _source_text(provider_id: str, model_id: str | None) -> str:
    model = model_id or "unspecified-model"
    return (
        f"Open-Meteo {model} hourly ({provider_id}); "
        f"CC BY 4.0; {FORECAST_NOT_ALERT}"
    )
