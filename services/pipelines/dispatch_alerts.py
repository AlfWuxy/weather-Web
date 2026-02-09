# -*- coding: utf-8 -*-
"""Cron-friendly entrypoint: dispatch pilot alerts."""

import argparse
import logging

from core.app import create_app
from services.push.dispatch import dispatch_alerts

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

