# -*- coding: utf-8 -*-
"""Cron-friendly entrypoint: dispatch pilot alerts."""

import argparse
import fcntl
import logging
import os
from pathlib import Path
import sys
import tempfile

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from services.push.dispatch import dispatch_alerts  # noqa: E402

logger = logging.getLogger(__name__)

app = create_app(register_blueprints=False)


def _acquire_dispatch_lock():
    """跨进程占用唯一调度锁，避免手工命令与 systemd 同时外呼。"""
    state_dir = Path(os.getenv("DEPLOY_STATE_DIR") or tempfile.gettempdir())
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "case-weather-dispatch.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dispatch pilot alerts (WxPusher).")
    parser.add_argument("--dedupe-hours", type=int, default=6, help="Dedupe window in hours (default: 6)")
    args = parser.parse_args(argv)

    lock_handle = _acquire_dispatch_lock()
    if lock_handle is None:
        logger.error("已有另一个预警投递任务正在运行，本轮未发送")
        return 75
    try:
        with app.app_context():
            result = dispatch_alerts(dedupe_hours=args.dedupe_hours)
            print(f"dispatch_alerts: {result}")
    finally:
        lock_handle.close()
    if result.get("status") == "snapshot_unavailable":
        return 3
    return 2 if int(result.get("failed") or 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
