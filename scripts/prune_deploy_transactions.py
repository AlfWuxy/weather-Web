#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理超过保留期的部署事务副本，并保留去敏故障提示。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


ATTENTION_MARKERS = ("ROLLBACK_REQUIRED.txt", "POST_COMMIT_ATTENTION.txt")
TERMINAL_MARKERS = ATTENTION_MARKERS + ("COMMITTED", "ROLLED_BACK")
RECOVERY_CONFIRMED_MARKER = "RECOVERY_CONFIRMED"


def prune_deploy_transactions(
    state_dir: Path,
    *,
    now: datetime | None = None,
    retention_days: int = 30,
    preserve_names=(),
):
    """只处理 state/backups/deploy-transactions 的直接子目录。"""
    state_dir = Path(state_dir).expanduser().resolve()
    if not state_dir.is_absolute() or state_dir == Path("/"):
        raise ValueError("state_dir 必须是安全的专用绝对目录")
    retention_days = int(retention_days)
    if not 1 <= retention_days <= 365:
        raise ValueError("retention_days 必须在 1 至 365 之间")

    transaction_root = state_dir / "backups" / "deploy-transactions"
    alert_root = state_dir / "backups" / "deploy-retention-alerts"
    transaction_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(transaction_root, 0o700)
    preserve = {str(name) for name in preserve_names if str(name)}
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff_timestamp = (reference - timedelta(days=retention_days)).timestamp()
    removed = []
    alerts = []
    skipped_symlinks = []
    preserved_unresolved = []

    for entry in sorted(transaction_root.iterdir(), key=lambda item: item.name):
        if entry.name in preserve:
            continue
        if entry.is_symlink():
            skipped_symlinks.append(entry.name)
            continue
        if not entry.is_dir() or entry.stat().st_mtime >= cutoff_timestamp:
            continue

        marker_names = [name for name in ATTENTION_MARKERS if (entry / name).is_file()]
        unfinished = (entry / "ACTIVATION_STARTED").is_file() and not any(
            (entry / name).is_file() for name in TERMINAL_MARKERS
        )
        attention_unresolved = marker_names and not (
            entry / RECOVERY_CONFIRMED_MARKER
        ).is_file()
        if unfinished or attention_unresolved:
            preserved_unresolved.append(entry.name)
            continue
        if marker_names:
            alert_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(alert_root, 0o700)
            alert_path = alert_root / f"{entry.name}.txt"
            alert_path.write_text(
                "部署事务敏感副本已按保留期清理。\n"
                f"事务: {entry.name}\n"
                f"原故障标记: {', '.join(marker_names)}\n",
                encoding="utf-8",
            )
            os.chmod(alert_path, 0o600)
            alerts.append(alert_path.name)

        # entry 是受控根目录的直接、非符号链接子目录。
        shutil.rmtree(entry)
        removed.append(entry.name)

    return {
        "retention_days": retention_days,
        "removed": removed,
        "attention_alerts": alerts,
        "skipped_symlinks": skipped_symlinks,
        "preserved_unresolved": preserved_unresolved,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prune private deploy transaction backups.")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--preserve-name", action="append", default=[])
    args = parser.parse_args(argv)
    result = prune_deploy_transactions(
        args.state_dir,
        retention_days=args.retention_days,
        preserve_names=args.preserve_name,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
