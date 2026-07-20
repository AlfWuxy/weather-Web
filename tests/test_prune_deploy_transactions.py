# -*- coding: utf-8 -*-
"""部署事务隐私保留期测试。"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat

import pytest

from scripts.prune_deploy_transactions import prune_deploy_transactions


def _age(path: Path, now: datetime, days: int):
    timestamp = (now - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_prune_only_removes_expired_directories_and_preserves_current(tmp_path):
    state_dir = tmp_path / "case-weather"
    root = state_dir / "backups" / "deploy-transactions"
    old = root / "old-success"
    current = root / "current-release"
    fresh = root / "fresh-success"
    for directory in (old, current, fresh):
        directory.mkdir(parents=True)
        (directory / "database-before.db").write_text("private", encoding="utf-8")
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    _age(old, now, 31)
    _age(current, now, 31)
    _age(fresh, now, 29)

    result = prune_deploy_transactions(
        state_dir,
        now=now,
        preserve_names=[current.name],
    )

    assert result["removed"] == ["old-success"]
    assert not old.exists()
    assert current.exists()
    assert fresh.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_prune_preserves_old_failure_backup_until_recovery_is_confirmed(tmp_path):
    state_dir = tmp_path / "case-weather"
    failed = state_dir / "backups" / "deploy-transactions" / "failed-release"
    failed.mkdir(parents=True)
    (failed / "ROLLBACK_REQUIRED.txt").write_text("secret path and details", encoding="utf-8")
    (failed / "environment-before.env").write_text("SECRET_KEY=value", encoding="utf-8")
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    _age(failed, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["failed-release"]
    assert failed.exists()
    assert not (state_dir / "backups" / "deploy-retention-alerts").exists()

    (failed / "RECOVERY_CONFIRMED").write_text("confirmed", encoding="utf-8")
    _age(failed, now, 31)
    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == ["failed-release"]
    alert = state_dir / "backups" / "deploy-retention-alerts" / "failed-release.txt"
    assert alert.exists()
    assert "ROLLBACK_REQUIRED.txt" in alert.read_text(encoding="utf-8")
    assert "SECRET_KEY" not in alert.read_text(encoding="utf-8")
    assert stat.S_IMODE(alert.stat().st_mode) == 0o600


def test_prune_preserves_interrupted_activation_for_manual_recovery(tmp_path):
    state_dir = tmp_path / "case-weather"
    interrupted = state_dir / "backups" / "deploy-transactions" / "interrupted-release"
    interrupted.mkdir(parents=True)
    (interrupted / "ACTIVATION_STARTED").write_text("private release path", encoding="utf-8")
    (interrupted / "environment-before.env").write_text("SECRET_KEY=value", encoding="utf-8")
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    _age(interrupted, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["interrupted-release"]
    assert interrupted.exists()


def test_prune_never_follows_symlink(tmp_path):
    state_dir = tmp_path / "case-weather"
    root = state_dir / "backups" / "deploy-transactions"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    root.mkdir(parents=True)
    (root / "linked").symlink_to(outside, target_is_directory=True)

    result = prune_deploy_transactions(
        state_dir,
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
        retention_days=1,
    )

    assert result["skipped_symlinks"] == ["linked"]
    assert (outside / "keep.txt").exists()


@pytest.mark.parametrize("unsafe", (Path("/"),))
def test_prune_rejects_broad_state_directory(unsafe):
    with pytest.raises(ValueError):
        prune_deploy_transactions(unsafe)
