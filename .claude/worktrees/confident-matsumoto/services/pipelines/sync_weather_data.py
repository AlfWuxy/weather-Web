# -*- coding: utf-8 -*-
"""Weather data sync pipeline (CSV backfill + daily API update)."""
import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]

from core.app import create_app
from core.constants import DEFAULT_CITY_LABEL  # noqa: E402
from core.db_models import CommunityDaily, DailyStatus, Pair, WeatherData  # noqa: E402
from core.extensions import db  # noqa: E402
from core.weather import get_consecutive_hot_days  # noqa: E402
from core.time_utils import today_local  # noqa: E402
from services.heat_action_service import HeatActionService  # noqa: E402
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
        if not weather_data:
            return {'location': location, 'date': target_date, 'updated': False}

        extreme = weather_service.identify_extreme_weather(weather_data)
        record = WeatherData.query.filter_by(date=target_date, location=location).first()
        if record and not overwrite:
            return {'location': location, 'date': target_date, 'updated': False}
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
        return {'location': location, 'date': target_date, 'updated': True}


def _map_heat_level(level):
    return {
        'low': '低风险',
        'medium': '中风险',
        'high': '高风险',
        'extreme': '极高'
    }.get(level, '低风险')


def _outreach_summary(total_people, confirmed_count, help_count, escalation_count):
    if total_people <= 0:
        return '暂无可用行动数据。'
    pending = total_people - confirmed_count
    if escalation_count > 0:
        return f'已有{escalation_count}个家庭进入升级链，优先安排社区跟进。'
    if help_count > 0:
        return f'已有{help_count}个家庭发出求助，请尽快联系。'
    if pending > 0:
        return f'仍有{pending}个家庭未确认，建议分批提醒。'
    return '全部家庭已完成确认，继续关注高温变化。'


def sync_action_daily(target_date=None, community_code=None, overwrite=False):
    """Sync daily heat action status for all active pairs."""
    target_date = _parse_date(target_date) or today_local()
    with app.app_context():
        query = Pair.query.filter_by(status='active')
        if community_code:
            query = query.filter_by(community_code=community_code)
        pairs = query.all()
        if not pairs:
            return {'date': target_date, 'updated': 0, 'communities': 0}

        heat_service = HeatActionService()
        weather_service = WeatherService()

        communities = {}
        for pair in pairs:
            communities.setdefault(pair.community_code, []).append(pair)

        updated = 0
        for code, members in communities.items():
            weather_data = weather_service.get_current_weather(code)
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

            for pair in members:
                status = DailyStatus.query.filter_by(
                    pair_id=pair.id,
                    status_date=target_date
                ).first()
                if status and not overwrite:
                    if status.risk_level:
                        continue
                if status is None:
                    status = DailyStatus(
                        pair_id=pair.id,
                        status_date=target_date,
                        community_code=pair.community_code
                    )
                    db.session.add(status)
                status.risk_level = risk_level
                updated += 1

        db.session.commit()

        for code in communities.keys():
            total_people = Pair.query.filter_by(status='active', community_code=code).count()
            statuses = DailyStatus.query.filter_by(
                community_code=code,
                status_date=target_date
            ).all()
            confirmed_count = sum(1 for s in statuses if s.confirmed_at)
            help_count = sum(1 for s in statuses if s.help_flag)
            escalation_count = sum(1 for s in statuses if s.relay_stage in ('community', 'emergency'))
            risk_dist = {'低风险': 0, '中风险': 0, '高风险': 0, '极高': 0}
            for status in statuses:
                if status.risk_level in risk_dist:
                    risk_dist[status.risk_level] += 1

            summary = _outreach_summary(total_people, confirmed_count, help_count, escalation_count)
            confirm_rate = (confirmed_count / total_people) if total_people else 0
            escalation_rate = (escalation_count / total_people) if total_people else 0

            record = CommunityDaily.query.filter_by(
                community_code=code,
                date=target_date
            ).first()
            if record is None:
                record = CommunityDaily(
                    community_code=code,
                    date=target_date
                )
                db.session.add(record)
            record.total_people = total_people
            record.confirm_rate = round(confirm_rate, 4)
            record.escalation_rate = round(escalation_rate, 4)
            record.risk_distribution = json.dumps(risk_dist, ensure_ascii=False)
            record.outreach_summary = summary

        db.session.commit()
        return {'date': target_date, 'updated': updated, 'communities': len(communities)}


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
