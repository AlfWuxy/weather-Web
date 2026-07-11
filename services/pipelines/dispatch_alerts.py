# -*- coding: utf-8 -*-
"""Cron-friendly entrypoint: dispatch pilot alerts."""

import argparse
import logging
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from services.push.dispatch import dispatch_alerts  # noqa: E402

logger = logging.getLogger(__name__)

app = create_app(register_blueprints=False)


def main():
    parser = argparse.ArgumentParser(description="Dispatch pilot alerts (WxPusher).")
    parser.add_argument("--dedupe-hours", type=int, default=6, help="Dedupe window in hours (default: 6)")
    args = parser.parse_args()

    with app.app_context():
        result = dispatch_alerts(dedupe_hours=args.dedupe_hours)
        print(f"dispatch_alerts: {result}")


if __name__ == "__main__":
    main()
