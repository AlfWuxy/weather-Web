"""取得明确单次模型运行；起报、归档生成、实际取得各保留原义。"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlencode

from .community_store import CommunityError, finite, json_text
from .forecast_run import make_forecast_run_snapshot, derive_run_weather, validate_run_snapshot

MODEL = "ecmwf_ifs025"
DISCOVERY_URL = f"https://api.open-meteo.com/data/{MODEL}/static/meta.json"
HOURLY = "temperature_2m,relative_humidity_2m,wind_speed_10m,shortwave_radiation,precipitation,is_day"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # 在建立下一次连接前拒绝重定向，不把事后检查当成访问控制。
        raise CommunityError("天气来源发生重定向，已停止读取", code="WEATHER_REDIRECT")


def _read_json(url, opener, clock, maximum):
    request = urllib.request.Request(url, headers={
        "User-Agent": "YilaoAgriculture/0.2 educational-community-pilot",
        "Accept": "application/json", "Accept-Encoding": "identity"})
    queried = clock().astimezone(timezone.utc).isoformat()
    try:
        with opener(request, timeout=18) as response:
            if response.geturl() != url:
                raise CommunityError("天气返回地址与固定请求不一致", code="WEATHER_REDIRECT")
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise CommunityError("天气返回数据过大", code="WEATHER_RESPONSE_TOO_LARGE")
            payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, CommunityError):
            raise
        raise CommunityError("天气更新失败，已有资料仍保留；请稍后重试", code="WEATHER_UNAVAILABLE", status=502) from exc
    retrieved = clock().astimezone(timezone.utc).isoformat()
    if not isinstance(payload, dict) or payload.get("error"):
        raise CommunityError("天气来源未返回有效资料", code="WEATHER_INVALID", status=502)
    return raw, payload, queried, retrieved


def fetch_weather(plot, archive_dir, *, opener=None, clock=None):
    lat = finite(plot.get("latitude"), "latitude", -90, 90)
    lon = finite(plot.get("longitude"), "longitude", -180, 180)
    clock = clock or (lambda: datetime.now(timezone.utc))
    # 三次请求均为固定 HTTPS 来源：发现运行、同运行归档元数据、明确运行预报。
    opener = opener or urllib.request.build_opener(_NoRedirect()).open
    discovery_raw, discovery, discovery_query, discovery_received = _read_json(
        DISCOVERY_URL, opener, clock, 262_144)
    stamp = discovery.get("last_run_initialisation_time")
    try:
        if isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
            raise ValueError("天气来源未给出明确的模型运行")
        run = datetime.fromtimestamp(stamp, timezone.utc)
        if run.hour not in {0, 6, 12, 18} or any((run.minute, run.second, run.microsecond)) or run > datetime.fromisoformat(discovery_received):
            raise ValueError("运行时点异常")
    except (ValueError, OverflowError, OSError) as exc:
        raise CommunityError("模型运行时间无效，已停止更新", code="WEATHER_RUN_UNKNOWN", status=502) from exc
    metadata_url = f"https://openmeteo.s3.amazonaws.com/data_run/{MODEL}/{run:%Y/%m/%d/%H%MZ}/meta.json"
    metadata_raw, _, metadata_query, metadata_received = _read_json(metadata_url, opener, clock, 262_144)
    url = "https://single-runs-api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": lat, "longitude": lon, "models": MODEL, "run": run.strftime("%Y-%m-%dT%H:%M"),
        "hourly": HOURLY, "wind_speed_unit": "ms", "timezone": "GMT"})
    raw, payload, queried, retrieved = _read_json(url, opener, clock, 2_000_000)
    try:
        snapshot = make_forecast_run_snapshot(raw, metadata_raw, request_url=url, metadata_url=metadata_url,
            queried_at=queried, retrieved_at=retrieved,
            metadata_queried_at=metadata_query, metadata_retrieved_at=metadata_received)
        weather = derive_run_weather(snapshot)
        validate_run_snapshot(weather, clock())
    except (ValueError, TypeError, KeyError) as exc:
        raise CommunityError("同一运行的天气与来源证据未能对应，未替换已有资料", code="WEATHER_RUN_INVALID", status=502) from exc
    # 以整份取得记录作内容寻址；重复读取同一运行也不会改写旧快照的时点。
    archive = Path(archive_dir)
    archive.mkdir(parents=True, exist_ok=True)
    record = {"forecast_run_snapshot": snapshot, "discovery": {
        "request_url": DISCOVERY_URL, "queried_at": discovery_query, "retrieved_at": discovery_received,
        "raw_utf8": discovery_raw.decode("utf-8"), "raw_sha256": sha256(discovery_raw).hexdigest()},
        "claim": "public_forecast_source_receipt_not_field_validation", "n_real": 0}
    serialized = json_text(record)
    (archive / (sha256(serialized.encode()).hexdigest() + ".json")).write_text(serialized, encoding="utf-8")
    warnings = ["这是户外格点天气预报，仍需另行核对官方预警。",
                "已绑定单次模型运行；起报时间用于计算资料年龄，实际取得时间不代表预报发布时间。",
                "天气格点不代表田间实测；安排还需现场条件与个人活动限制。"]
    if plot.get("environment") == "greenhouse":
        warnings.append("所选地块为棚内；户外预报不能作为棚内暴露条件，需另行提供棚内依据。")
    weather.update(retrieved_at=retrieved, issue_time_status="explicit_run_initialisation_not_issuance",
        location={"latitude": lat, "longitude": lon,
                  "grid_latitude": payload.get("latitude"), "grid_longitude": payload.get("longitude")},
        alert_status="not_queried", warnings=warnings,
        provenance={"request_url": url, "raw_sha256": sha256(raw).hexdigest(),
                    "attribution_url": "https://open-meteo.com/", "licence": "CC-BY-4.0",
                    "use": "non-commercial community pilot", "timing_basis": "explicit_single_run"})
    return weather
