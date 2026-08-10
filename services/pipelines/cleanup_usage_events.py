# -*- coding: utf-8 -*-
"""每日清理超过 30 天的 UsageEvent。"""

import argparse
import json
import logging
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from core.usage import (  # noqa: E402
    ALERT_DELIVERY_CLICK_RETENTION_DAYS,
    USAGE_EVENT_RETENTION_DAYS,
    clear_expired_alert_delivery_clicks,
    delete_expired_usage_events,
)

logger = logging.getLogger(__name__)


def _audit_result(result, click_result):
    """把清理结果整理为适合 systemd journal 留存的 JSON 字段。"""
    cutoff = result.get('cutoff')
    if hasattr(cutoff, 'isoformat'):
        cutoff = cutoff.isoformat()
    elif cutoff is not None:
        cutoff = str(cutoff)
    complete = bool(result.get('complete')) and bool(click_result.get('complete'))

    return {
        'status': 'success' if complete else 'partial',
        'retention_days': USAGE_EVENT_RETENTION_DAYS,
        'click_retention_days': ALERT_DELIVERY_CLICK_RETENTION_DAYS,
        'cutoff': cutoff,
        'deleted': int(result.get('deleted') or 0),
        'click_timestamps_cleared': int(click_result.get('cleared') or 0),
        'complete': complete,
    }


def cleanup_usage_events(*, batch_size=None, max_batches=None, app_instance=None):
    """在独立 Flask app context 中执行 UsageEvent 保留策略。"""
    cleanup_options = {}
    if batch_size is not None:
        cleanup_options['batch_size'] = batch_size
    if max_batches is not None:
        cleanup_options['max_batches'] = max_batches

    # 延迟创建 app，让配置或扩展初始化失败也进入 CLI 的非零退出路径。
    target_app = app_instance or create_app(register_blueprints=False)
    with target_app.app_context():
        result = delete_expired_usage_events(**cleanup_options)
        click_result = clear_expired_alert_delivery_clicks(**cleanup_options)
    return _audit_result(result, click_result)


def _build_parser():
    parser = argparse.ArgumentParser(
        description=(
            'Delete expired UsageEvent rows and clear AlertDelivery click timestamps '
            'after the 30-day retention window.'
        ),
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=None,
        help='Rows per delete batch; core retention limits still apply.',
    )
    parser.add_argument(
        '--max-batches',
        type=int,
        default=None,
        help='Maximum delete batches for this run; core retention limits still apply.',
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        result = cleanup_usage_events(
            batch_size=args.batch_size,
            max_batches=args.max_batches,
        )
    except Exception as exc:
        logger.exception('UsageEvent cleanup failed: %s', exc)
        error_result = {
            'status': 'error',
            'error_type': type(exc).__name__,
            'message': str(exc),
        }
        print(
            json.dumps(error_result, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    # 仍有过期记录时让 systemd 标记失败，避免清理积压静默发生。
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
