# -*- coding: utf-8 -*-
"""Sync minute-level weather cache for all locations."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import stat
import threading
import uuid
from datetime import datetime
from pathlib import Path
import sys

from flask import current_app

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from core.constants import DEFAULT_CITY_LABEL  # noqa: E402
from core.db_models import ForecastCache, WeatherCache, WeatherData  # noqa: E402
from core.extensions import db  # noqa: E402
from core.weather import normalize_location_name  # noqa: E402
from core.time_utils import today_local, utcnow  # noqa: E402
from services.miniprogram_service import (  # noqa: E402
    CANONICAL_LOCATION_NAME,
    latest_snapshot_record,
    qweather_runtime_configured,
    refresh_snapshot_from_cycle,
    snapshot_payload,
)
from services.weather_service import WeatherService  # noqa: E402
from services.qweather_budget import get_qweather_redis_client  # noqa: E402

logger = logging.getLogger(__name__)
app = create_app(register_blueprints=False)
NOWCAST_CACHE_LOCATION = f"nowcast:{CANONICAL_LOCATION_NAME}"
NOWCAST_CACHE_HOURS = 24
SYNC_LEASE_SECONDS = 1800
SYNC_LEASE_KEY = "weather:sync:cycle:v1"
FORMAL_SMOKE_USED_PREFIX = "weather:sync:formal-smoke:v1"
_HOST_LOCK = threading.Lock()


class WeatherSyncBusy(RuntimeError):
    """同步周期已被同机进程或分布式租约占用。"""


def _sync_lock_path():
    configured = str(os.getenv("WEATHER_SYNC_LOCK_PATH", "")).strip()
    if configured:
        return Path(configured)
    dispatch_lock = str(current_app.config.get("DISPATCH_LOCK_PATH") or "").strip()
    if dispatch_lock and Path(dispatch_lock).is_absolute():
        # 正式 systemd 已允许写共享 run 目录，timer 与手工命令据此稳定共锁。
        return Path(dispatch_lock).parent / "case-weather-sync.lock"
    return Path(app.instance_path) / "case-weather-sync.lock"


@contextmanager
def _host_cycle_lock():
    """同机非阻塞锁，手工命令和 timer 共用同一个入口。"""
    if not _HOST_LOCK.acquire(blocking=False):
        raise WeatherSyncBusy("host_thread_lock_busy")
    descriptor = None
    try:
        lock_path = _sync_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise WeatherSyncBusy("host_lock_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WeatherSyncBusy("host_process_lock_busy") from exc
        yield
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        _HOST_LOCK.release()


def _distributed_cycle_lease_required():
    if not qweather_runtime_configured():
        return False
    # 单元测试默认不依赖 Redis；需要验证 lease 时可显式打开。
    if current_app.config.get("TESTING") or current_app.config.get("DEBUG"):
        return bool(current_app.config.get("QWEATHER_ENFORCE_SYNC_LEASE_IN_TESTS", False))
    return True


def _formal_smoke_credentials():
    names = (
        "CASE_WEATHER_FORMAL_SMOKE_TOKEN",
        "CASE_WEATHER_FORMAL_SMOKE_BINDING",
        "CASE_WEATHER_FORMAL_SMOKE_TICKET",
        "CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN",
    )
    values = [str(os.getenv(name, "")).strip() for name in names]
    if not any(values):
        return None
    if not all(values):
        raise WeatherSyncBusy("formal_smoke_credentials_incomplete")
    return values


def _consume_formal_smoke_ticket(
    client,
    token,
    binding,
    ticket_value,
    lease_token,
):
    """消费 root 签发的一次性票据；消费成功后即使上游失败也禁止重试。"""
    ticket_path = Path(ticket_value)
    if not ticket_path.is_absolute():
        raise WeatherSyncBusy("formal_smoke_ticket_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(ticket_path, flags)
        ticket_stat = os.fstat(descriptor)
        if not stat.S_ISREG(ticket_stat.st_mode) or ticket_stat.st_nlink != 1:
            raise WeatherSyncBusy("formal_smoke_ticket_invalid")
        if not (current_app.config.get("TESTING") or current_app.config.get("DEBUG")):
            if ticket_stat.st_uid != 0 or stat.S_IMODE(ticket_stat.st_mode) != 0o640:
                raise WeatherSyncBusy("formal_smoke_ticket_permissions_invalid")
        raw = os.read(descriptor, 4096).decode("ascii")
        if os.read(descriptor, 1):
            raise WeatherSyncBusy("formal_smoke_ticket_invalid")
    except (OSError, UnicodeError) as exc:
        raise WeatherSyncBusy("formal_smoke_ticket_invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    fields = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    lease_token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
    if (
        not hmac.compare_digest(fields.get("binding", ""), binding)
        or not hmac.compare_digest(fields.get("token_sha256", ""), token_hash)
        or not hmac.compare_digest(
            fields.get("lease_token_sha256", ""),
            lease_token_hash,
        )
    ):
        raise WeatherSyncBusy("formal_smoke_ticket_binding_invalid")

    used_key = f"{FORMAL_SMOKE_USED_PREFIX}:{binding}:{token_hash}"
    try:
        consumed = client.set(used_key, "used", nx=True, ex=7 * 86400)
    except Exception as exc:
        raise WeatherSyncBusy("formal_smoke_receipt_lease_unavailable") from exc
    if not consumed:
        raise WeatherSyncBusy("formal_smoke_ticket_already_used")
    try:
        ticket_path.unlink()
        parent_fd = os.open(ticket_path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise WeatherSyncBusy("formal_smoke_ticket_consume_failed") from exc


def reserve_formal_cycle_lease(lease_token):
    """在 started receipt 形成前预占正式烟测的跨主机周期租约。"""
    lease_token = str(lease_token or "").strip()
    if len(lease_token) != 64 or any(
        character not in "0123456789abcdef" for character in lease_token
    ):
        raise WeatherSyncBusy("formal_smoke_lease_token_invalid")
    with app.app_context():
        client = get_qweather_redis_client()
        if client is None:
            raise WeatherSyncBusy("redis_lease_unavailable")
        try:
            acquired = client.set(
                SYNC_LEASE_KEY,
                lease_token,
                nx=True,
                ex=SYNC_LEASE_SECONDS,
            )
        except Exception as exc:
            raise WeatherSyncBusy("redis_lease_unavailable") from exc
        if not acquired:
            raise WeatherSyncBusy("redis_lease_busy")
    return True


def _acquire_distributed_cycle_lease():
    """预占完整 30 分钟周期；即使本轮失败也保留租约，防止自动重试烧额度。"""
    formal_credentials = _formal_smoke_credentials()
    if formal_credentials is None and not _distributed_cycle_lease_required():
        return True
    client = get_qweather_redis_client()
    if client is None:
        raise WeatherSyncBusy("redis_lease_unavailable")
    if formal_credentials is not None:
        token, binding, ticket_value, lease_token = formal_credentials
        try:
            stored_lease = str(client.get(SYNC_LEASE_KEY) or "")
        except Exception as exc:
            raise WeatherSyncBusy("redis_lease_unavailable") from exc
        if not hmac.compare_digest(stored_lease, lease_token):
            raise WeatherSyncBusy("formal_smoke_lease_invalid")
        _consume_formal_smoke_ticket(
            client,
            token,
            binding,
            ticket_value,
            lease_token,
        )
        return True
    try:
        acquired = client.set(
            SYNC_LEASE_KEY,
            uuid.uuid4().hex,
            nx=True,
            ex=SYNC_LEASE_SECONDS,
        )
    except Exception as exc:
        raise WeatherSyncBusy("redis_lease_unavailable") from exc
    if not acquired:
        raise WeatherSyncBusy("redis_lease_busy")
    return True


def _dedupe_locations(locations):
    seen = set()
    for loc in locations:
        if not loc:
            continue
        normalized = normalize_location_name(loc)
        if normalized in seen:
            continue
        seen.add(normalized)
        yield normalized


def _upsert_cache(location, weather_data, fetched_at):
    payload = json.dumps(weather_data, ensure_ascii=False)
    cache = WeatherCache.query.filter_by(location=location).first()
    if cache:
        cache.payload = payload
        cache.fetched_at = fetched_at
        cache.is_mock = bool(weather_data.get('is_mock'))
        return cache
    cache = WeatherCache(
        location=location,
        fetched_at=fetched_at,
        payload=payload,
        is_mock=bool(weather_data.get('is_mock'))
    )
    db.session.add(cache)
    return cache


def _upsert_daily(location, weather_data, target_date):
    record = WeatherData.query.filter_by(date=target_date, location=location).first()
    if record is None:
        record = WeatherData(date=target_date, location=location)
        db.session.add(record)

    record.temperature = weather_data.get('temperature')
    record.temperature_max = weather_data.get('temperature_max')
    record.temperature_min = weather_data.get('temperature_min')
    record.humidity = weather_data.get('humidity')
    record.pressure = weather_data.get('pressure')
    record.weather_condition = weather_data.get('weather_condition')
    record.wind_speed = weather_data.get('wind_speed')
    record.pm25 = weather_data.get('pm25')
    record.aqi = weather_data.get('aqi')
    return record


def _valid_nowcast(nowcast):
    """只缓存来源明确、时间连续且数值可信的小时预报。"""
    if not isinstance(nowcast, dict):
        return False
    if (
        not nowcast.get('available')
        or nowcast.get('source') != 'Open-Meteo'
        or nowcast.get('is_mock')
        or nowcast.get('is_demo')
    ):
        return False
    timeline = nowcast.get('timeline')
    if not isinstance(timeline, list) or not 1 <= len(timeline) <= NOWCAST_CACHE_HOURS:
        return False

    parsed_times = []
    for item in timeline:
        if not isinstance(item, dict):
            return False
        try:
            item_time = datetime.fromisoformat(str(item.get('time') or ''))
            probability = float(item.get('precipitation_probability'))
            precipitation = float(item.get('precipitation_mm'))
            temperature = float(item.get('temperature'))
        except (TypeError, ValueError, OverflowError):
            return False
        if item_time.tzinfo is not None:
            return False
        if (
            not math.isfinite(probability)
            or not 0 <= probability <= 100
            or not math.isfinite(precipitation)
            or not 0 <= precipitation <= 500
            or not math.isfinite(temperature)
            or not -100 <= temperature <= 100
        ):
            return False
        expected_risk = '高' if probability >= 70 else '中' if probability >= 40 else '低'
        if item.get('risk_level') != expected_risk:
            return False
        if not str(item.get('condition') or '').strip():
            return False
        parsed_times.append(item_time)

    return parsed_times == sorted(parsed_times) and len(set(parsed_times)) == len(parsed_times)


def _upsert_nowcast(nowcast, fetched_at):
    """复用 ForecastCache 保存后台周期取得的小时预报。"""
    if not _valid_nowcast(nowcast):
        return False
    record = ForecastCache.query.filter_by(
        location=NOWCAST_CACHE_LOCATION,
        days=NOWCAST_CACHE_HOURS,
    ).order_by(ForecastCache.fetched_at.desc(), ForecastCache.id.desc()).first()
    payload = json.dumps(nowcast, ensure_ascii=False)
    if record is None:
        record = ForecastCache(
            location=NOWCAST_CACHE_LOCATION,
            days=NOWCAST_CACHE_HOURS,
            fetched_at=fetched_at,
            payload=payload,
            is_mock=False,
        )
        db.session.add(record)
    else:
        record.fetched_at = fetched_at
        record.payload = payload
        record.is_mock = False
    return True


def _resolve_locations(locations):
    if locations:
        return locations
    env_locations = os.getenv('WEATHER_SYNC_LOCATIONS', '').strip()
    if env_locations:
        return [item.strip() for item in env_locations.split(',') if item.strip()]
    # 后台默认只预热都昌县。村庄与页面选项共享县级缓存，禁止把 UI 选项批量当成 API 任务。
    return [DEFAULT_CITY_LABEL]


def _trusted_complete_snapshot(payload):
    """只有三项官方来源都成功时，本周期才可触发下游预警派发。"""
    source_status = (payload or {}).get('source_status') or {}
    weather_status = source_status.get('weather') or {}
    forecast_status = source_status.get('forecast') or {}
    warning_status = source_status.get('warnings') or {}
    return bool(
        str(weather_status.get('provider') or '').strip().casefold() == 'qweather'
        and weather_status.get('available')
        and not weather_status.get('is_mock')
        and forecast_status.get('available')
        and {
            str(provider).strip().casefold()
            for provider in (forecast_status.get('providers') or [])
        } == {'qweather'}
        and warning_status.get('available')
        and str(warning_status.get('status') or '').strip().casefold()
        in {'ok', 'success'}
    )


def _sync_weather_cache_locked(locations=None, update_daily=True, include_nowcast=True):
    """同步正式天气快照；受控发布烟测可关闭不参与门禁的短时 nowcast。"""
    with app.app_context():
        # 小程序与 Web 共用唯一都昌县周期。即使旧命令传入多个地点，也不会形成 API fan-out。
        requested_locations = list(_dedupe_locations(_resolve_locations(locations)))
        locations = [CANONICAL_LOCATION_NAME]
        weather_service = WeatherService() if qweather_runtime_configured() else None
        formal_smoke = _formal_smoke_credentials() is not None
        previous_snapshot = latest_snapshot_record()
        previous_snapshot_id = previous_snapshot.snapshot_id if previous_snapshot else None
        fetched_at = None
        target_date = today_local()
        updated = 0
        nowcast_updated = 0
        nowcast = {}

        if weather_service is not None:
            try:
                weather_data = weather_service.get_current_weather(
                    CANONICAL_LOCATION_NAME,
                    include_enrichment=False,
                    allow_fallback=not formal_smoke,
                )
                fetched_at = utcnow()
            except Exception as exc:
                logger.exception("Weather sync failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
                db.session.rollback()
                weather_data = {}
                fetched_at = utcnow()
            if include_nowcast:
                try:
                    nowcast = weather_service.get_short_term_nowcast(
                        CANONICAL_LOCATION_NAME,
                        hours=NOWCAST_CACHE_HOURS,
                    )
                except Exception as exc:
                    logger.warning("Nowcast sync failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
                    nowcast = {}
        else:
            # QWeather 未配置时由快照层只读旧缓存并继承原始 fetched_at。
            # 这里不回写 WeatherCache，避免旧数据被洗成刚抓取。
            weather_data = {}

        weather_is_mock = bool(
            isinstance(weather_data, dict)
            and (weather_data.get('is_mock') or weather_data.get('is_demo'))
        )
        try:
            if weather_data and not weather_is_mock:
                fetched_at = fetched_at or utcnow()
                _upsert_cache(CANONICAL_LOCATION_NAME, weather_data, fetched_at)
                if update_daily:
                    _upsert_daily(CANONICAL_LOCATION_NAME, weather_data, target_date)
                updated = 1
            if _upsert_nowcast(nowcast, fetched_at or utcnow()):
                nowcast_updated = 1
            if updated or nowcast_updated:
                db.session.commit()
        except Exception as exc:
            logger.exception("Weather cache upsert failed for %s: %s", CANONICAL_LOCATION_NAME, exc)
            db.session.rollback()
            updated = 0
            nowcast_updated = 0

        if not weather_data or weather_is_mock:
            logger.warning(
                "No trustworthy persisted weather data available for %s",
                CANONICAL_LOCATION_NAME,
            )

        snapshot = refresh_snapshot_from_cycle(
            {} if weather_is_mock else weather_data,
            weather_service=weather_service,
            fetched_at=fetched_at,
            current_fetched_at=fetched_at if weather_data and not weather_is_mock else None,
            force_refresh_sources=weather_service is not None,
        )
        persisted = snapshot_payload(snapshot)
        trusted_complete_cycle = _trusted_complete_snapshot(persisted)
        snapshot_ready = bool(
            updated == 1
            and persisted.get('snapshot_id')
            and persisted.get('snapshot_id') != previous_snapshot_id
            and persisted.get('available')
            and not persisted.get('stale', True)
            and not (persisted.get('current') or {}).get('is_mock', False)
            and trusted_complete_cycle
        )
        return {
            'locations': 1,
            'requested_locations': len(requested_locations),
            'canonical_location': CANONICAL_LOCATION_NAME,
            'updated': updated,
            'nowcast_updated': nowcast_updated,
            'update_daily': update_daily,
            'snapshot_id': snapshot.snapshot_id if snapshot else None,
            'snapshot_ready': snapshot_ready,
            'snapshot_degraded': not trusted_complete_cycle,
            'snapshot_stale': bool(persisted.get('stale', True)),
            'status': 'completed',
        }


def sync_weather_cache(locations=None, update_daily=True, include_nowcast=True):
    """唯一受管天气周期：先拿同机锁与 30 分钟 Redis lease，再允许访问上游。"""
    with app.app_context():
        try:
            with _host_cycle_lock():
                _acquire_distributed_cycle_lease()
                return _sync_weather_cache_locked(
                    locations=locations,
                    update_daily=update_daily,
                    include_nowcast=include_nowcast,
                )
        except WeatherSyncBusy as exc:
            logger.warning("Weather sync skipped before upstream access: %s", exc)
            return {
                'locations': 1,
                'requested_locations': 0,
                'canonical_location': CANONICAL_LOCATION_NAME,
                'updated': 0,
                'nowcast_updated': 0,
                'update_daily': update_daily,
                'snapshot_id': None,
                'snapshot_ready': False,
                'snapshot_degraded': True,
                'snapshot_stale': True,
                'status': 'busy',
                'reason': str(exc),
            }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sync minute-level weather cache.')
    parser.add_argument('--location', action='append', dest='locations', help='Location label override')
    parser.add_argument('--no-daily', action='store_true', help='Skip WeatherData daily upsert')
    parser.add_argument(
        '--skip-nowcast',
        action='store_true',
        help='正式发布受控烟测跳过短时预报',
    )
    parser.add_argument(
        '--reserve-formal-lease-only',
        action='store_true',
        help='只预占正式烟测的 30 分钟跨主机租约，不访问天气上游',
    )
    args = parser.parse_args(argv)

    if args.reserve_formal_lease_only:
        if args.locations or args.no_daily or args.skip_nowcast:
            parser.error('--reserve-formal-lease-only 不能与同步参数混用')
        try:
            reserve_formal_cycle_lease(
                os.getenv('CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN', ''),
            )
        except WeatherSyncBusy as exc:
            logger.warning("Formal weather smoke lease unavailable: %s", exc)
            return 75
        print('Formal weather smoke lease reserved.')
        return 0

    result = sync_weather_cache(
        locations=args.locations,
        update_daily=not args.no_daily,
        include_nowcast=not args.skip_nowcast,
    )
    print(f"Cache sync: {result}")
    # systemd 只会在新鲜快照形成后通过 OnSuccess 触发推送。
    if result.get('status') == 'busy':
        return 75
    return 0 if result.get('snapshot_ready') else 2


if __name__ == '__main__':
    raise SystemExit(main())
