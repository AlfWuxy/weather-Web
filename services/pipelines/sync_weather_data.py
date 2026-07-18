# -*- coding: utf-8 -*-
"""Weather data sync pipeline (CSV backfill + daily API update)."""
import argparse
import math
from datetime import date, datetime
from pathlib import Path
import sys

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from core.constants import DEFAULT_CITY_LABEL  # noqa: E402
from core.db_models import DailyStatus, Pair, WeatherData  # noqa: E402
from core.extensions import db  # noqa: E402
from core.weather import get_consecutive_hot_days, is_qweather_online_weather  # noqa: E402
from core.time_utils import today_local  # noqa: E402
from services.community_daily_service import refresh_community_daily  # noqa: E402
from services.heat_action_service import HeatActionService  # noqa: E402
from services.user.owner_write_guard import OwnerInactiveError, owner_write_guard  # noqa: E402
from services.weather_service import WeatherService  # noqa: E402

app = create_app(register_blueprints=False)

CSV_RENAME_MAP = {
    '日期': 'date',
    '2米平均气温 (多源融合)(°C)': 'temperature',
    '2米最高气温 (多源融合)(°C)': 'temperature_max',
    '2米最低气温 (多源融合)(°C)': 'temperature_min',
    '2米平均相对湿度 (多源融合)(%)': 'humidity',
    '10米平均风速 (多源融合)(m/s)': 'wind_speed'
}

DEFAULT_CSV_PATH = ROOT_DIR / 'data' / 'raw' / '逐日数据.csv'
REQUIRED_ACTION_WEATHER_FIELDS = (
    'temperature',
    'temperature_max',
    'temperature_min',
    'humidity',
)


def _validate_action_weather(weather_data):
    """仅允许字段完整的真实和风天气写入风险与行动状态。"""
    if not isinstance(weather_data, dict) or not weather_data:
        return {
            'valid': False,
            'reason': 'weather_unavailable',
            'weather_source': 'unknown',
            'missing_fields': list(REQUIRED_ACTION_WEATHER_FIELDS),
        }

    source = str(
        weather_data.get('data_source') or weather_data.get('source') or 'unknown'
    ).strip()
    if weather_data.get('is_mock') or weather_data.get('is_demo'):
        return {
            'valid': False,
            'reason': 'mock_weather',
            'weather_source': source,
            'missing_fields': [],
        }
    if source != 'QWeather':
        return {
            'valid': False,
            'reason': 'untrusted_weather_source',
            'weather_source': source,
            'missing_fields': [],
        }

    missing_fields = []
    for field in REQUIRED_ACTION_WEATHER_FIELDS:
        try:
            value = float(weather_data.get(field))
        except (TypeError, ValueError):
            missing_fields.append(field)
            continue
        if not math.isfinite(value):
            missing_fields.append(field)

    if missing_fields:
        return {
            'valid': False,
            'reason': 'incomplete_weather',
            'weather_source': source,
            'missing_fields': missing_fields,
        }
    if not is_qweather_online_weather(weather_data):
        return {
            'valid': False,
            'reason': 'untrusted_weather_source',
            'weather_source': source,
            'missing_fields': [],
        }
    return {
        'valid': True,
        'reason': None,
        'weather_source': source,
        'missing_fields': [],
    }


def _normalize_location(location):
    default_city = app.config.get('DEFAULT_CITY', DEFAULT_CITY_LABEL) or DEFAULT_CITY_LABEL
    return location or default_city


def _parse_date(value):
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None
    return None


def backfill_weather_from_csv(csv_path=None, location=None, overwrite=False, start_date=None, end_date=None):
    csv_path = Path(csv_path) if csv_path else DEFAULT_CSV_PATH
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)
    missing_cols = [col for col in CSV_RENAME_MAP if col not in df.columns]
    if missing_cols:
        raise ValueError(f"CSV missing columns: {missing_cols}")

    df = df.rename(columns=CSV_RENAME_MAP)
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.date
    df = df[df['date'].notna()]

    if start_date:
        start_date = _parse_date(start_date)
        if start_date:
            df = df[df['date'] >= start_date]
    if end_date:
        end_date = _parse_date(end_date)
        if end_date:
            df = df[df['date'] <= end_date]

    df = df.drop_duplicates(subset=['date'])
    for col in ('temperature', 'temperature_max', 'temperature_min', 'humidity', 'wind_speed'):
        df[col] = pd.to_numeric(df[col], errors='coerce')

    with app.app_context():
        location = _normalize_location(location)
        dates = df['date'].tolist()
        existing = WeatherData.query.filter(
            WeatherData.location == location,
            WeatherData.date.in_(dates)
        ).all()
        existing_map = {item.date: item for item in existing}

        created = 0
        updated = 0
        skipped = 0

        for row in df.itertuples(index=False):
            record = existing_map.get(row.date)
            if record and not overwrite:
                skipped += 1
                continue
            if record is None:
                record = WeatherData(date=row.date, location=location)
                db.session.add(record)
                created += 1
            else:
                updated += 1

            record.temperature = row.temperature
            record.temperature_max = row.temperature_max
            record.temperature_min = row.temperature_min
            record.humidity = row.humidity
            record.wind_speed = row.wind_speed

        db.session.commit()
        return {
            'location': location,
            'rows': len(df),
            'created': created,
            'updated': updated,
            'skipped': skipped
        }


def sync_daily_weather(target_date=None, location=None, overwrite=True):
    target_date = _parse_date(target_date) or today_local()
    with app.app_context():
        location = _normalize_location(location)
        weather_service = WeatherService()
        weather_data = weather_service.get_current_weather(location)
        validation = _validate_action_weather(weather_data)
        if not validation['valid']:
            return {
                'location': location,
                'date': target_date,
                'updated': False,
                'skipped': True,
                'reason': validation['reason'],
                'weather_source': validation['weather_source'],
                'missing_fields': validation['missing_fields'],
            }

        extreme = weather_service.identify_extreme_weather(weather_data)
        record = WeatherData.query.filter_by(date=target_date, location=location).first()
        if record and not overwrite:
            return {
                'location': location,
                'date': target_date,
                'updated': False,
                'skipped': True,
                'reason': 'existing_record',
                'weather_source': validation['weather_source'],
                'missing_fields': [],
            }
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
        record.is_extreme = bool(extreme.get('is_extreme'))
        record.extreme_type = '、'.join([c['type'] for c in extreme.get('conditions', [])]) if extreme.get('is_extreme') else None

        db.session.commit()
        return {
            'location': location,
            'date': target_date,
            'updated': True,
            'skipped': False,
            'reason': None,
            'weather_source': validation['weather_source'],
            'missing_fields': [],
        }


def _map_heat_level(level):
    return {
        'low': '低风险',
        'medium': '中风险',
        'high': '高风险',
        'extreme': '极高'
    }.get(level, '低风险')


def sync_action_daily(target_date=None, community_code=None, overwrite=False):
    """同步 active Pair 的每日行动状态，每轮最多读取一次天气接口。"""
    target_date = _parse_date(target_date) or today_local()
    with app.app_context():
        query = db.select(
            Pair.id,
            Pair.caregiver_id,
            Pair.community_code,
        ).where(Pair.status == 'active')
        if community_code:
            query = query.where(Pair.community_code == community_code)
        candidates = db.session.execute(
            query.order_by(Pair.caregiver_id, Pair.id)
        ).all()
        if not candidates:
            return {
                'date': target_date,
                'updated': 0,
                'communities': 0,
                'processed_communities': 0,
                'skipped': True,
                'reason': 'no_active_pairs',
                'skipped_communities': {},
            }

        initial_communities = sorted({row.community_code for row in candidates})
        # 都昌单城试点使用同一份实时天气。接口读取发生在 owner 锁外，避免慢调用占锁。
        weather_location = _normalize_location(community_code)
        weather_data = WeatherService().get_current_weather(weather_location)
        validation = _validate_action_weather(weather_data)
        if not validation['valid']:
            skipped_communities = {
                code: {
                    'reason': validation['reason'],
                    'weather_source': validation['weather_source'],
                    'missing_fields': validation['missing_fields'],
                }
                for code in initial_communities
            }
            return {
                'date': target_date,
                'updated': 0,
                'communities': len(initial_communities),
                'processed_communities': 0,
                'skipped': True,
                'reason': 'weather_unavailable_for_all_communities',
                'skipped_communities': skipped_communities,
            }

        heat_service = HeatActionService()
        risk_by_community = {}
        for code in initial_communities:
            consecutive_hot_days = get_consecutive_hot_days(
                code,
                target_date=target_date,
                today_max=weather_data.get('temperature_max')
            )
            heat_result = heat_service.calculate_heat_risk(
                weather_data,
                consecutive_hot_days=consecutive_hot_days
            )
            risk_level = _map_heat_level(heat_result['risk_level'])
            risk_by_community[code] = risk_level

            weather_record = WeatherData.query.filter_by(date=target_date, location=code).first()
            if weather_record is None:
                weather_record = WeatherData(date=target_date, location=code)
                db.session.add(weather_record)
            weather_record.temperature = weather_data.get('temperature')
            weather_record.temperature_max = weather_data.get('temperature_max')
            weather_record.temperature_min = weather_data.get('temperature_min')
            weather_record.humidity = weather_data.get('humidity')
            weather_record.pressure = weather_data.get('pressure')
            weather_record.weather_condition = weather_data.get('weather_condition')
            weather_record.wind_speed = weather_data.get('wind_speed')
            weather_record.pm25 = weather_data.get('pm25')
            weather_record.aqi = weather_data.get('aqi')
            weather_record.is_extreme = bool(weather_data.get('is_extreme'))
            weather_record.extreme_type = weather_data.get('extreme_type')
        # 公共天气先独立提交，后续私密状态始终在 owner 守卫内写入。
        db.session.commit()

        candidate_ids_by_owner = {}
        for row in candidates:
            candidate_ids_by_owner.setdefault(int(row.caregiver_id), []).append(int(row.id))

        updated = 0
        processed_communities = set()
        for owner_user_id in sorted(candidate_ids_by_owner):
            try:
                with owner_write_guard(owner_user_id):
                    pair_ids = candidate_ids_by_owner[owner_user_id]
                    pairs = db.session.execute(
                        db.select(Pair)
                        .where(
                            Pair.id.in_(pair_ids),
                            Pair.caregiver_id == owner_user_id,
                            Pair.status == 'active',
                        )
                        .order_by(Pair.id)
                        .execution_options(populate_existing=True)
                    ).scalars().all()
                    for pair in pairs:
                        risk_level = risk_by_community.get(pair.community_code)
                        if risk_level is None:
                            # 位置在预计算后发生变化，留待下一轮，避免在锁内读取天气。
                            continue
                        status = DailyStatus.query.filter_by(
                            pair_id=pair.id,
                            status_date=target_date,
                        ).first()
                        if status and not overwrite and status.risk_level:
                            processed_communities.add(pair.community_code)
                            continue
                        if status is None:
                            status = DailyStatus(
                                pair_id=pair.id,
                                status_date=target_date,
                                community_code=pair.community_code,
                            )
                            db.session.add(status)
                        status.risk_level = risk_level
                        updated += 1
                        processed_communities.add(pair.community_code)
                    db.session.commit()
            except OwnerInactiveError:
                # 注销先完成时，本轮不再为该账号创建任何私密记录。
                continue

        # commit=False 会把同键投影锁保持到下方最终提交；固定顺序可避免并发批任务交叉持锁。
        for code in sorted(processed_communities):
            # 定时同步只复用本地状态投影，不增加天气接口调用。
            refresh_community_daily(code, target_date, commit=False)

        db.session.commit()
        return {
            'date': target_date,
            'updated': updated,
            'communities': len(initial_communities),
            'processed_communities': len(processed_communities),
            'skipped': False,
            'reason': None,
            'skipped_communities': {},
        }


def main():
    parser = argparse.ArgumentParser(description='Sync weather data.')
    parser.add_argument('--csv', dest='csv_path', help='CSV path for backfill')
    parser.add_argument('--backfill', action='store_true', help='Import historical CSV data')
    parser.add_argument('--daily', action='store_true', help='Sync daily weather from API')
    parser.add_argument('--action-daily', action='store_true', help='Sync daily heat action status')
    parser.add_argument('--date', dest='date_value', help='Target date (YYYY-MM-DD) for daily sync')
    parser.add_argument('--location', dest='location', help='Location label override')
    parser.add_argument('--community', dest='community', help='Community code override')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing rows')
    parser.add_argument('--start-date', dest='start_date', help='Start date (YYYY-MM-DD) for CSV import')
    parser.add_argument('--end-date', dest='end_date', help='End date (YYYY-MM-DD) for CSV import')
    args = parser.parse_args()

    if args.backfill:
        result = backfill_weather_from_csv(
            csv_path=args.csv_path,
            location=args.location,
            overwrite=args.overwrite,
            start_date=args.start_date,
            end_date=args.end_date
        )
        print(f"CSV backfill: {result}")

    if args.daily:
        result = sync_daily_weather(
            target_date=args.date_value,
            location=args.location,
            overwrite=args.overwrite
        )
        print(f"Daily sync: {result}")

    if args.action_daily:
        result = sync_action_daily(
            target_date=args.date_value,
            community_code=args.community,
            overwrite=args.overwrite
        )
        print(f"Action daily sync: {result}")

    if not args.backfill and not args.daily and not args.action_daily:
        parser.print_help()


if __name__ == '__main__':
    main()
