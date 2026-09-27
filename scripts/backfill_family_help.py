# -*- coding: utf-8 -*-
"""把既有 Pair / DailyStatus.help_flag 回填为家庭空间与求助工单。

支持 --dry-run。两次运行不重复创建家庭或未结工单。无法证明的结束时间标 legacy。
不构造虚假 ActionEvent。
"""
from __future__ import annotations

import argparse
import json
import sys
import secrets
from datetime import timezone

from core.app import create_app
from core.db_models import DailyStatus, HelpRequest, Pair
from core.extensions import db
from core.time_utils import utcnow
from services.family_access import ensure_space_for_pair
from services.help_request_service import OPEN_STATUSES


def _aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def run_backfill(*, dry_run=True, limit=None):
    stats = {
        'pairs_seen': 0,
        'spaces_created': 0,
        'open_help_created': 0,
        'legacy_help_created': 0,
        'skipped_existing_open': 0,
        'errors': [],
    }
    query = Pair.query.order_by(Pair.id.asc())
    if limit:
        query = query.limit(int(limit))
    pairs = query.all()
    for pair in pairs:
        stats['pairs_seen'] += 1
        try:
            existed_space = bool(pair.family_space_id)
            space = ensure_space_for_pair(pair, commit=False)
            if not existed_space:
                stats['spaces_created'] += 1
            open_row = HelpRequest.query.filter(
                HelpRequest.pair_id == pair.id,
                HelpRequest.status.in_(OPEN_STATUSES),
            ).first()
            if open_row:
                stats['skipped_existing_open'] += 1
                continue
            latest = (
                DailyStatus.query.filter_by(pair_id=pair.id, help_flag=True)
                .order_by(DailyStatus.status_date.desc())
                .first()
            )
            if not latest:
                continue
            now = utcnow()
            closed = latest.closed_at is not None
            ack = latest.help_acknowledged_at is not None
            if closed:
                status = 'resolved'
                legacy = 'daily_status_closed'
                stats['legacy_help_created'] += 1
            elif ack:
                status = 'acknowledged'
                legacy = 'daily_status_acked_unknown_times'
                stats['open_help_created'] += 1
            else:
                status = 'pending_ack'
                legacy = 'daily_status_help_flag'
                stats['open_help_created'] += 1
            created_at = _aware(latest.created_at) or now
            help_row = HelpRequest(
                public_id=secrets.token_hex(16),
                family_space_id=space.id,
                pair_id=pair.id,
                status=status,
                origin_channel='web',
                actor_role='elder',
                is_proxy=True,
                category='cannot_complete',
                version=1,
                acknowledged_at=_aware(latest.help_acknowledged_at) if ack else None,
                resolved_at=_aware(latest.closed_at) if closed else None,
                resolution_code='other' if closed else None,
                is_test=bool(getattr(pair, 'is_test', False)),
                legacy_source=legacy,
                created_at=created_at,
                updated_at=now,
            )
            db.session.add(help_row)
        except Exception as exc:
            stats['errors'].append({'pair_id': pair.id, 'error': type(exc).__name__})
    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description='回填家庭空间与未结求助')
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--commit', action='store_true', help='真正写入；默认只演练')
    parser.add_argument('--limit', type=int, default=None)
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        stats = run_backfill(dry_run=not args.commit, limit=args.limit)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if not stats['errors'] else 1


if __name__ == '__main__':
    sys.exit(main())
