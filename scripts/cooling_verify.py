# -*- coding: utf-8 -*-
"""电话/现场核验 CLI：写库 + 追加 docs/data/cooling_verification_ledger.csv。

不写姓名、电话号码或任何老人信息。封闭码见 docs/data/cooling_verification_protocol.md。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LEDGER_PATH = ROOT_DIR / 'docs' / 'data' / 'cooling_verification_ledger.csv'
LEDGER_HEADER = [
    'verified_at',
    'resource_id',
    'resource_type',
    'township',
    'method',
    'open_during_alert',
    'alert_open_note_code',
    'ac',
    'water',
    'seats',
    'toilet',
    'step_free',
    'shade',
    'transport_need',
    'result_code',
    'notes_code',
]
RESULT_CODES = frozenset({'verified', 'unverified', 'closed', 'relocated'})
NOTE_CODES = frozenset({
    'ok',
    'no_answer',
    'wrong_number',
    'hours_changed',
    'relocated',
    'refused',
})


def _tri_bool(value):
    if value is None or value == '':
        return None
    normalized = str(value).strip().lower()
    if normalized in ('yes', 'true', '1'):
        return True
    if normalized in ('no', 'false', '0'):
        return False
    if normalized in ('unknown', 'null', 'none'):
        return None
    raise ValueError(f'expected yes/no/unknown, got {value}')


def append_ledger_row(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = (not path.exists()) or path.stat().st_size == 0
    with path.open('a', encoding='utf-8', newline='') as handle:
        import csv
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(LEDGER_HEADER)
        writer.writerow([row.get(column, '') for column in LEDGER_HEADER])


def run_verify(
    resource_id,
    method,
    open_during_alert=None,
    alert_note=None,
    amenities=None,
    transport=None,
    result='verified',
    note='ok',
    by_role='student',
):
    from core.db_models import CoolingResource
    from core.extensions import db
    from services.cooling_service import (
        amenity_csv_value,
        parse_amenities,
        record_verification,
        township_for_resource,
    )

    result_code = (result or 'verified').strip()
    notes_code = (note or 'ok').strip()
    if result_code not in RESULT_CODES:
        raise ValueError('invalid result_code')
    if notes_code not in NOTE_CODES:
        raise ValueError('invalid notes_code')

    resource = db.session.get(CoolingResource, resource_id)
    if resource is None:
        raise ValueError(f'resource {resource_id} not found')

    amenities = amenities or {}
    record_verification(
        resource,
        method,
        open_during_alert,
        alert_note,
        amenities,
        transport,
        by_role,
    )
    stored = parse_amenities(resource)
    append_ledger_row(LEDGER_PATH, {
        'verified_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'resource_id': resource.id,
        'resource_type': resource.resource_type or '',
        'township': township_for_resource(resource),
        'method': resource.verify_method or method or '',
        'open_during_alert': resource.open_during_alert or '',
        'alert_open_note_code': resource.alert_open_note_code or '',
        'ac': amenity_csv_value(stored.get('ac')),
        'water': amenity_csv_value(stored.get('water')),
        'seats': amenity_csv_value(stored.get('seats')),
        'toilet': amenity_csv_value(stored.get('toilet')),
        'step_free': amenity_csv_value(stored.get('step_free')),
        'shade': amenity_csv_value(stored.get('shade')),
        'transport_need': resource.transport_need or '',
        'result_code': result_code,
        'notes_code': notes_code,
    })
    return resource


def build_parser():
    parser = argparse.ArgumentParser(description='记录避暑资源核验（写库 + 台账）')
    parser.add_argument('--id', type=int, required=True)
    parser.add_argument('--method', required=True, choices=['phone', 'onsite', 'official_doc'])
    parser.add_argument('--open', default='unknown')
    parser.add_argument('--alert-note', default='')
    parser.add_argument('--ac', default='unknown')
    parser.add_argument('--water', default='unknown')
    parser.add_argument('--seats', default='unknown')
    parser.add_argument('--toilet', default='unknown')
    parser.add_argument('--step-free', default='unknown')
    parser.add_argument('--shade', default='unknown')
    parser.add_argument('--transport', default='unknown')
    parser.add_argument('--result', default='verified')
    parser.add_argument('--note', default='ok')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    db_url = (os.environ.get('DATABASE_URL') or os.environ.get('DATABASE_URI') or '').strip()
    if db_url:
        os.environ['DATABASE_URI'] = db_url

    from core.app import create_app

    app = create_app(register_blueprints=False)
    amenities = {
        'ac': _tri_bool(args.ac),
        'water': _tri_bool(args.water),
        'seats': _tri_bool(args.seats),
        'toilet': _tri_bool(args.toilet),
        'step_free': _tri_bool(getattr(args, 'step_free')),
        'shade': _tri_bool(args.shade),
    }
    with app.app_context():
        resource = run_verify(
            args.id,
            args.method,
            open_during_alert=args.open,
            alert_note=args.alert_note or None,
            amenities=amenities,
            transport=args.transport,
            result=args.result,
            note=args.note,
        )
        print(
            f'verified resource_id={resource.id} status={resource.verify_status} '
            f'method={resource.verify_method}'
        )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
