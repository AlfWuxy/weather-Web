#!/usr/bin/env python3
"""清理历史埋点副本；默认预览，--apply 才写入，业务地址表不受影响。"""
import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.analytics_privacy import sanitize_analytics_meta
from core.db_models import UsageEvent


def scrub_usage_metadata(session, *, apply=False, batch_size=500):
    """分批只改元数据列；输出数量，不输出任何原始地址或记录内容。"""
    last_id = 0
    changed = 0
    while True:
        rows = session.query(UsageEvent).filter(UsageEvent.id > last_id).order_by(
            UsageEvent.id).limit(batch_size).all()
        if not rows:
            break
        for row in rows:
            last_id = row.id
            try:
                # 历史超长/深层 JSON 无需重新展开后再丢弃。
                value = json.loads(row.meta_json) if row.meta_json and len(row.meta_json) <= 16384 else None
            except (ValueError, TypeError, RecursionError):
                value = None
            clean = sanitize_analytics_meta(row.event_type, value)
            encoded = json.dumps(clean, ensure_ascii=False) if clean else None
            if encoded != row.meta_json:
                changed += 1
                if apply:
                    row.meta_json = encoded
        if apply:
            session.commit()
        session.expire_all()
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='写入历史埋点清理结果')
    args = parser.parse_args()
    from core.app import create_app
    from core.extensions import db
    with create_app().app_context():
        changed = scrub_usage_metadata(db.session, apply=args.apply)
    print(json.dumps({'applied': args.apply, 'changed_events': changed}))


if __name__ == '__main__':
    main()
