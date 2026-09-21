"""田间仪器实测的导入与归一：绑定仪器、校准、地点和时刻。

本模块只做导入、归一和边界拒绝，不做人体安全、农艺适宜或准确度判断。
导入通过只表示“元数据齐全且单位可辨识”，不等于读数准确、可代表整块田、
可跨地块使用，也不等于可以派工。

两条硬边界：

1. 仪器、校准、地点、时刻缺一不可，且必须是可辨识的声明值；占位词、
   缺时区时刻、未绑定地块或坐标不符的记录不得当作该地块实测。
2. 实测只描述已经发生的时段。未来作业区间的排程必须使用预报；本模块
   另提供快速失败辅助，权威判定仍在 ``environment.assess_interval``
   （``MEASURED_WEATHER_IS_NOT_FORECAST``），本模块不重复实现该规则。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .models import InputError, is_placeholder_source, parse_time


class MeasurementError(InputError):
    """导入失败；消息含字段路径，``code`` 供测试与调用方分支。"""

    def __init__(self, message: str, *, path: str | None = None, code: str | None = None) -> None:
        self.path = path
        self.code = code
        text = message if path is None else f"{path}: {message}"
        if code is not None:
            text = f"{text} [{code}]"
        super().__init__(text)


# 输出记录字段到唯一接受的 SI 单位别名。别名之外的写法（含 km/h、mph、°F）
# 一律拒绝：Open-Meteo 默认 wind_speed_unit=km/h，若不显式改单位会把风速
# 放大约 3.6 倍（见 R04-ISSUE-01）。
SI_UNITS: dict[str, frozenset[str]] = {
    "temperature_c": frozenset({"degc", "c", "celsius", "摄氏度"}),
    "relative_humidity_pct": frozenset({"percent", "%", "pct", "百分比"}),
    "wind_m_s": frozenset({"m_s", "m/s", "mps", "m·s-1", "ms-1", "米每秒"}),
    "shortwave_w_m2": frozenset({"w_m2", "w/m2", "w·m-2", "wm-2"}),
    "precipitation_mm": frozenset({"mm", "毫米"}),
    "wbgt_c": frozenset({"degc", "c", "celsius", "摄氏度"}),
}
# 常见非 SI 输入，仅用于给出可操作的拒绝理由，不构成任何换算。
NON_SI_HINTS = {
    "km/h": "Open-Meteo 默认风速单位；导入前须改为 m/s 并重新取值",
    "kmh": "Open-Meteo 默认风速单位；导入前须改为 m/s 并重新取值",
    "kph": "非 SI 风速单位；须改为 m/s",
    "mph": "非 SI 风速单位；须改为 m/s",
    "f": "华氏度；须改为摄氏度并说明换算",
    "degf": "华氏度；须改为摄氏度并说明换算",
    "fahrenheit": "华氏度；须改为摄氏度并说明换算",
}
# 只有这些仪器种类可以申报 WBGT。普通温湿探头不能冒充 WBGT 仪（R04-A05/A07）。
WBGT_INSTRUMENT_KINDS = frozenset({"wbgt_meter", "agro_station"})
INSTRUMENT_KINDS = frozenset({
    "wbgt_meter", "black_globe", "thermo_hygro", "anemometer", "pyranometer",
    "rain_gauge", "agro_station",
})
SITES = frozenset({"sun_work", "shade_rest", "greenhouse_work"})
_BOUNDS = {
    "temperature_c": (-100.0, 100.0),
    "relative_humidity_pct": (0.0, 100.0),
    "wind_m_s": (0.0, 120.0),
    "shortwave_w_m2": (0.0, 2000.0),
    "precipitation_mm": (0.0, 2000.0),
    "wbgt_c": (-100.0, 100.0),
}
_MEASUREMENT_KEYS = frozenset({
    "id", "observed_at", "interval_minutes", "environment", "site", "source",
    "instrument", "calibration", "location", "units", "values", "daylight",
})
_INSTRUMENT_KEYS = frozenset({"id", "kind", "model", "serial"})
_CALIBRATION_KEYS = frozenset({"calibrated_at", "valid_until", "method", "certificate", "source"})
_LOCATION_KEYS = frozenset({"binding", "plot_id", "latitude", "longitude", "height_m", "site"})


def _fail(path: str, code: str, message: str) -> None:
    raise MeasurementError(message, path=path, code=code)


def _mapping(value: Any, path: str, allowed: frozenset[str], code: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, code, "必须为对象")
    for key in value:
        if key not in allowed:
            _fail(f"{path}.{key}", "FIELD_UNKNOWN", "未知字段")
    return value


def _declared(value: Any, path: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, code, "必须为非空字符串")
    if is_placeholder_source(value):
        _fail(path, code, f"占位或缺省声明不得作为仪器元数据：{value!r}")
    return value


def _instant(value: Any, path: str, code: str) -> datetime:
    try:
        return parse_time(value)
    except InputError:
        _fail(path, code, "必须为含时区的 ISO 8601 时刻，不猜测本地时区")
    raise AssertionError("unreachable")


def _finite(value: Any, path: str, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, code, "必须为有限数值，缺失不得补零")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        _fail(path, code, "必须为有限数值")
    return number


def _unit_of(field: str, units: dict, path: str) -> None:
    raw = units.get(field)
    if raw is None:
        _fail(f"{path}.units.{field}", "MEASUREMENT_UNIT_MISSING",
              f"{field} 必须显式声明 SI 单位，缺省不能按惯例推断")
    if not isinstance(raw, str):
        _fail(f"{path}.units.{field}", "MEASUREMENT_UNIT_INVALID", "单位必须为字符串")
    normalized = raw.strip().casefold().replace(" ", "")
    if normalized in SI_UNITS[field]:
        return
    hint = NON_SI_HINTS.get(normalized)
    _fail(f"{path}.units.{field}", "UNIT_NOT_SI",
          f"{field} 只接受 SI 单位 {' / '.join(sorted(SI_UNITS[field]))}，得到 {raw!r}"
          + (f"：{hint}" if hint else ""))


def _instrument(raw: dict, path: str) -> tuple[dict, str]:
    instrument = _mapping(raw.get("instrument"), f"{path}.instrument", _INSTRUMENT_KEYS,
                          "INSTRUMENT_MISSING")
    instrument_id = _declared(instrument.get("id"), f"{path}.instrument.id", "INSTRUMENT_ID_UNKNOWN")
    kind = _declared(instrument.get("kind"), f"{path}.instrument.kind", "INSTRUMENT_KIND_UNKNOWN")
    if kind not in INSTRUMENT_KINDS:
        _fail(f"{path}.instrument.kind", "INSTRUMENT_KIND_UNKNOWN",
              f"仪器种类须为 {' / '.join(sorted(INSTRUMENT_KINDS))}")
    for key in ("model", "serial"):
        if key in instrument and instrument[key] is not None:
            _declared(instrument[key], f"{path}.instrument.{key}", "INSTRUMENT_FIELD_UNKNOWN")
    return dict(instrument), kind


def _calibration(raw: dict, path: str, observed: datetime) -> dict:
    calibration = _mapping(raw.get("calibration"), f"{path}.calibration", _CALIBRATION_KEYS,
                           "CALIBRATION_MISSING")
    calibrated_at = _instant(calibration.get("calibrated_at"), f"{path}.calibration.calibrated_at",
                             "CALIBRATION_TIME_UNKNOWN")
    valid_until = _instant(calibration.get("valid_until"), f"{path}.calibration.valid_until",
                           "CALIBRATION_TIME_UNKNOWN")
    _declared(calibration.get("method"), f"{path}.calibration.method", "CALIBRATION_METHOD_UNKNOWN")
    _declared(calibration.get("source"), f"{path}.calibration.source", "CALIBRATION_SOURCE_UNKNOWN")
    if "certificate" in calibration and calibration["certificate"] is not None:
        _declared(calibration["certificate"], f"{path}.calibration.certificate",
                  "CALIBRATION_FIELD_UNKNOWN")
    if valid_until <= calibrated_at:
        _fail(f"{path}.calibration.valid_until", "CALIBRATION_INTERVAL_INVALID",
              "校准有效期必须晚于校准时刻")
    if calibrated_at > observed:
        _fail(f"{path}.calibration.calibrated_at", "CALIBRATION_NOT_YET_VALID",
              "观测时刻早于校准时刻，不能让未完成的校准回填读数")
    if valid_until < observed:
        _fail(f"{path}.calibration.valid_until", "CALIBRATION_EXPIRED",
              "观测时刻已超出校准有效期，须先重新校准或换用有效仪器")
    return dict(calibration)


def _location(raw: dict, path: str, environment: str, site: str | None) -> dict:
    location = _mapping(raw.get("location"), f"{path}.location", _LOCATION_KEYS, "LOCATION_MISSING")
    binding = location.get("binding")
    if binding not in {"plot", "coordinates", "unbound"}:
        _fail(f"{path}.location.binding", "LOCATION_BINDING_UNKNOWN",
              "binding 须为 plot / coordinates / unbound 之一；缺省不是已绑定")
    height = _finite(location.get("height_m"), f"{path}.location.height_m", "LOCATION_HEIGHT_UNKNOWN")
    if not 0.0 <= height <= 100.0:
        _fail(f"{path}.location.height_m", "LOCATION_HEIGHT_UNKNOWN", "测量高度须在 0—100 m")
    result = {"binding": binding, "height_m": height}
    if binding == "plot":
        _declared(location.get("plot_id"), f"{path}.location.plot_id", "LOCATION_PLOT_UNKNOWN")
        result["plot_id"] = location["plot_id"]
    elif binding == "coordinates":
        latitude = _finite(location.get("latitude"), f"{path}.location.latitude",
                           "LOCATION_COORDINATE_UNKNOWN")
        longitude = _finite(location.get("longitude"), f"{path}.location.longitude",
                            "LOCATION_COORDINATE_UNKNOWN")
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            _fail(f"{path}.location", "LOCATION_COORDINATE_UNKNOWN", "坐标超出合法范围")
        result.update(latitude=latitude, longitude=longitude)
    if site is not None:
        if site == "greenhouse_work" and environment != "greenhouse":
            _fail(f"{path}.site", "SITE_ENVIRONMENT_MISMATCH", "棚内作业点须对应 greenhouse 环境")
        if site in {"sun_work", "shade_rest"} and environment != "outdoor":
            _fail(f"{path}.site", "SITE_ENVIRONMENT_MISMATCH", "露天作业点/遮阴点须对应 outdoor 环境")
        result["site"] = site
    return result


def _values(raw: dict, path: str, units: Any, kind: str) -> dict:
    if not isinstance(units, dict):
        _fail(f"{path}.units", "MEASUREMENT_UNIT_MISSING",
              "必须逐字段声明单位；缺省不能按来源惯例推断")
    values = raw.get("values")
    if not isinstance(values, dict) or not values:
        _fail(f"{path}.values", "MEASUREMENT_EMPTY", "至少有温度或 WBGT 读数，缺失不能当作零")
    normalized: dict[str, float] = {}
    for field, value in values.items():
        if field not in SI_UNITS:
            _fail(f"{path}.values.{field}", "MEASUREMENT_FIELD_UNKNOWN", "不是可导入的观测字段")
        number = _finite(value, f"{path}.values.{field}", "MEASUREMENT_VALUE_UNKNOWN")
        low, high = _BOUNDS[field]
        if not low <= number <= high:
            _fail(f"{path}.values.{field}", "MEASUREMENT_VALUE_UNKNOWN", "读数越出物理边界")
        _unit_of(field, units, path)
        normalized[field] = number
    if not ({"temperature_c", "wbgt_c"} & set(normalized)):
        _fail(f"{path}.values", "MEASUREMENT_EMPTY", "缺少温度与 WBGT，无法描述该时段环境")
    if "wbgt_c" in normalized and kind not in WBGT_INSTRUMENT_KINDS:
        _fail(f"{path}.values.wbgt_c", "WBGT_INSTRUMENT_MISMATCH",
              f"{kind} 不能输出 WBGT；须为 {' / '.join(sorted(WBGT_INSTRUMENT_KINDS))}，"
              "或改交原始温湿风辐射")
    extra = set(units) - set(SI_UNITS)
    if extra:
        _fail(f"{path}.units.{sorted(extra)[0]}", "MEASUREMENT_FIELD_UNKNOWN", "声明了未知字段的单位")
    return normalized


def import_measurement(raw: dict) -> dict:
    """把一个仪器观测导入为归一记录。

    返回字典含 ``record``（可直接作为 schema 1.0 的 ``measured`` 记录）、
    ``schedulable`` 与 ``hold_codes``。``binding="unbound"`` 的记录会被
    标记为不可用于排程，而不是被当成已绑定地块。
    """
    path = "measurement"
    raw = _mapping(raw, path, _MEASUREMENT_KEYS, "MEASUREMENT_MISSING")
    measurement_id = _declared(raw.get("id"), f"{path}.id", "MEASUREMENT_ID_UNKNOWN")
    observed = _instant(raw.get("observed_at"), f"{path}.observed_at", "OBSERVED_AT_UNKNOWN")
    environment = raw.get("environment")
    if environment not in {"outdoor", "greenhouse"}:
        _fail(f"{path}.environment", "ENVIRONMENT_UNKNOWN",
              "实测必须声明 outdoor 或 greenhouse；缺省不推断")
    site = raw.get("site")
    if site is not None and site not in SITES:
        _fail(f"{path}.site", "SITE_UNKNOWN", f"site 须为 {' / '.join(sorted(SITES))}")
    source = _declared(raw.get("source"), f"{path}.source", "MEASUREMENT_SOURCE_UNKNOWN")
    if raw.get("daylight") is not True and raw.get("daylight") is not False:
        _fail(f"{path}.daylight", "MEASUREMENT_DAYLIGHT_UNKNOWN",
              "该时段是否有日光必须显式给出，不能靠时刻推断")
    interval = raw.get("interval_minutes", 15)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440:
        _fail(f"{path}.interval_minutes", "INTERVAL_UNKNOWN", "interval_minutes 须为 1—1440 的整数")

    instrument, kind = _instrument(raw, path)
    calibration = _calibration(raw, path, observed)
    location = _location(raw, path, environment, site)
    values = _values(raw, path, raw.get("units"), kind)

    records: list[dict] = []
    warnings = [
        "导入通过只表示元数据齐全、单位可辨识；不表示读数准确或可代表整块田。",
        "本记录不包含危险天气声明；预警完整性仍由调用方提供的 alert_feed 决定。",
    ]
    hold_codes: list[str] = []
    schedulable = True
    if location["binding"] == "unbound":
        schedulable = False
        hold_codes.append("LOCATION_UNBOUND")
        warnings.append("location.binding=unbound：未绑定地块的仪器读数保持 HOLD，不进入排程。")
    else:
        end = observed + timedelta(minutes=interval)
        record: dict[str, Any] = {
            "start": observed.isoformat(),
            "end": end.isoformat(),
            "daylight": bool(raw["daylight"]),
        }
        record.update(values)
        records.append(record)
        warnings.append("实测记录只覆盖已经发生的时段；未来作业区间必须使用预报。")

    return {
        "id": measurement_id,
        "observed_at": observed.isoformat(),
        "environment": environment,
        "site": site,
        "source": source,
        "instrument": instrument,
        "calibration": calibration,
        "location": location,
        "values": values,
        "records": records,
        "schedulable": schedulable,
        "hold_codes": hold_codes,
        "warnings": warnings,
    }


def refuse_future_measured_use(start: datetime | str, end: datetime | str, now: datetime | str) -> None:
    """导入侧快速失败：实测区间不得伸入当前时刻之后。

    权威判定仍在 ``environment.assess_interval``；此辅助只让调用方在写入
    请求前就拿到可操作错误，不改变 ``kind``，也不把实测升级成预报。
    """
    start_dt = start if isinstance(start, datetime) else _instant(start, "start", "INTERVAL_UNKNOWN")
    end_dt = end if isinstance(end, datetime) else _instant(end, "end", "INTERVAL_UNKNOWN")
    now_dt = now if isinstance(now, datetime) else _instant(now, "now", "INTERVAL_UNKNOWN")
    if end_dt > now_dt:
        raise MeasurementError(
            f"区间末端 {end_dt.isoformat()} 晚于当前时刻 {now_dt.isoformat()}；"
            "实测不能代替未来预报",
            path="interval", code="MEASURED_WEATHER_IS_NOT_FORECAST")


def to_weather_block(plot: dict, measurements: list[dict], *, source: str, issued_at: str,
                     wbgt_method: str | None = None, tolerance_deg: float = 0.02) -> dict:
    """把导入记录合并为一个 plot 的 ``measured`` 天气块（schema 1.0 合法字段）。

    拒绝未绑定、环境不符或坐标与地块不符的记录；不生成 ``hazards`` 或
    ``alert_feed``，因此非演示模式的预警完整性仍由调用方补齐。
    返回的记录为深拷贝，调用方后续写入不改变传入的导入结果。
    """
    if not isinstance(plot, dict) or not plot.get("id"):
        _fail("plot.id", "PLOT_UNKNOWN", "必须给出唯一地块")
    plot_id = plot["id"]
    plot_environment = plot.get("environment")
    if plot_environment not in {"outdoor", "greenhouse"}:
        _fail("plot.environment", "ENVIRONMENT_UNKNOWN", "地块须声明 outdoor 或 greenhouse")
    records: list[dict] = []
    for index, measurement in enumerate(measurements):
        if not isinstance(measurement, dict) or "location" not in measurement:
            _fail(f"measurements[{index}]", "MEASUREMENT_MISSING", "须为 import_measurement 的输出")
        if not measurement.get("schedulable", False):
            _fail(f"measurements[{index}]", "LOCATION_UNBOUND",
                  "未绑定地块的读数保持 HOLD，不能合并进地块天气块")
        location = measurement["location"]
        if location["binding"] == "plot" and location.get("plot_id") != plot_id:
            _fail(f"measurements[{index}].location.plot_id", "LOCATION_MISMATCH_TO_PLOT",
                  f"仪器绑定地块 {location.get('plot_id')!r} 与目标地块 {plot_id!r} 不符")
        if location["binding"] == "coordinates":
            latitude, longitude = plot.get("latitude"), plot.get("longitude")
            if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
                _fail("plot", "PLOT_COORDINATE_UNKNOWN",
                      "地块坐标未知，无法核对仪器坐标，不能假定同一地点")
            if (abs(location["latitude"] - latitude) > tolerance_deg
                    or abs(location["longitude"] - longitude) > tolerance_deg):
                _fail(f"measurements[{index}].location", "LOCATION_MISMATCH_TO_PLOT",
                      "仪器坐标超出与地块的允许偏差；须核实是否同一地点或声明未绑定")
        if measurement["environment"] != plot_environment:
            _fail(f"measurements[{index}].environment", "ENVIRONMENT_MISMATCH_TO_PLOT",
                  "仪器环境与地块露天/棚内不一致")
        records.extend(deepcopy(measurement.get("records") or []))
    if not records:
        _fail("measurements", "MEASUREMENT_EMPTY", "没有可用于该地块的实测记录")
    records.sort(key=lambda record: parse_time(record["start"]))
    for previous, current in zip(records, records[1:]):
        if parse_time(current["start"]) < parse_time(previous["end"]):
            _fail("measurements", "MEASUREMENT_OVERLAP", "实测区间重叠，无法唯一积分")
    block: dict[str, Any] = {
        "source": _declared(source, "source", "MEASUREMENT_SOURCE_UNKNOWN"),
        "issued_at": _instant(issued_at, "issued_at", "ISSUED_AT_UNKNOWN").isoformat(),
        "kind": "measured",
        "environment": plot_environment,
        "records": records,
    }
    if any("wbgt_c" in record for record in records):
        block["wbgt_method"] = _declared(wbgt_method, "wbgt_method", "WBGT_METHOD_REQUIRED")
    return block
