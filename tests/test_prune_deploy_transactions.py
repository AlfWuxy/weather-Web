# -*- coding: utf-8 -*-
"""部署事务隐私保留期测试。"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat

import pytest

import scripts.prune_deploy_transactions as prune_module
from scripts.prune_deploy_transactions import prune_deploy_transactions


def _age(path: Path, now: datetime, days: int):
    timestamp = (now - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _release_path(state_dir: Path, name: str = "candidate-release") -> Path:
    release = Path(f"{state_dir}-deploy") / "releases" / name
    release.mkdir(parents=True)
    return release


def _confirmation_payload(release: Path, *, confirmed_at="2026-07-18T00:00:00Z"):
    return (
        f"confirmed_at={confirmed_at}\n"
        f"confirmed_before_release={release}\n"
    )


def _write_valid_confirmation(transaction: Path, release: Path) -> Path:
    marker = transaction / "RECOVERY_CONFIRMED"
    marker.write_text(_confirmation_payload(release), encoding="utf-8")
    marker.chmod(0o600)
    return marker


def _write_terminal(
    transaction: Path,
    name: str,
    payload: str = "success\n",
    *,
    mode: int = 0o600,
) -> Path:
    marker = transaction / name
    marker.write_text(payload, encoding="utf-8")
    marker.chmod(mode)
    return marker


def _old_transaction(state_dir: Path, name: str, now: datetime) -> Path:
    transaction = state_dir / "backups" / "deploy-transactions" / name
    transaction.mkdir(parents=True)
    _age(transaction, now, 31)
    return transaction


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

    release = _release_path(state_dir)
    _write_valid_confirmation(failed, release)
    _age(failed, now, 31)
    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == ["failed-release"]
    alert = state_dir / "backups" / "deploy-retention-alerts" / "failed-release.txt"
    assert alert.exists()
    assert "ROLLBACK_REQUIRED.txt" in alert.read_text(encoding="utf-8")
    assert "SECRET_KEY" not in alert.read_text(encoding="utf-8")
    assert stat.S_IMODE(alert.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "invalid_kind",
    (
        "partial",
        "wrong-mode",
        "symlink",
        "path-escape",
        "relative-path",
        "invalid-time",
        "duplicate-key",
        "invalid-utf8",
    ),
)
def test_prune_preserves_failure_when_recovery_confirmation_is_invalid(
    tmp_path,
    invalid_kind,
):
    state_dir = tmp_path / "case-weather"
    failed = state_dir / "backups" / "deploy-transactions" / invalid_kind
    failed.mkdir(parents=True)
    (failed / "ACTIVATION_STARTED").write_text("old-release\n", encoding="utf-8")
    (failed / "ROLLBACK_REQUIRED.txt").write_text("private", encoding="utf-8")
    release = _release_path(state_dir, invalid_kind)
    marker = failed / "RECOVERY_CONFIRMED"

    if invalid_kind == "partial":
        marker.write_text("confirmed_at=2026-07-18T00:00:00Z\n", encoding="utf-8")
    elif invalid_kind == "wrong-mode":
        marker.write_text(_confirmation_payload(release), encoding="utf-8")
        marker.chmod(0o644)
    elif invalid_kind == "symlink":
        target = tmp_path / "outside-confirmation"
        target.write_text(_confirmation_payload(release), encoding="utf-8")
        target.chmod(0o600)
        marker.symlink_to(target)
    elif invalid_kind == "path-escape":
        outside = tmp_path / "outside-release"
        outside.mkdir()
        marker.write_text(_confirmation_payload(outside), encoding="utf-8")
    elif invalid_kind == "relative-path":
        marker.write_text(
            "confirmed_at=2026-07-18T00:00:00Z\n"
            "confirmed_before_release=releases/candidate\n",
            encoding="utf-8",
        )
    elif invalid_kind == "invalid-time":
        marker.write_text(
            _confirmation_payload(release, confirmed_at="2026-07-18"),
            encoding="utf-8",
        )
    elif invalid_kind == "duplicate-key":
        marker.write_text(
            _confirmation_payload(release)
            + f"confirmed_before_release={release}\n",
            encoding="utf-8",
        )
    else:
        marker.write_bytes(b"confirmed_at=\xff\n")
    if invalid_kind not in {"wrong-mode", "symlink"}:
        marker.chmod(0o600)

    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _age(failed, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == [invalid_kind]
    assert failed.exists()
    assert not (state_dir / "backups" / "deploy-retention-alerts").exists()


def test_prune_preserves_failure_when_release_root_cannot_be_verified(tmp_path):
    state_dir = tmp_path / "case-weather"
    failed = state_dir / "backups" / "deploy-transactions" / "missing-release-root"
    failed.mkdir(parents=True)
    (failed / "POST_COMMIT_ATTENTION.txt").write_text("private", encoding="utf-8")
    missing_release = tmp_path / "missing-deploy" / "releases" / "candidate"
    _write_valid_confirmation(failed, missing_release)
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _age(failed, now, 31)

    result = prune_deploy_transactions(
        state_dir,
        release_root=tmp_path / "missing-deploy",
        now=now,
    )

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["missing-release-root"]
    assert failed.exists()


def test_prune_accepts_explicit_release_root_for_valid_confirmation(tmp_path):
    state_dir = tmp_path / "case-weather"
    failed = state_dir / "backups" / "deploy-transactions" / "custom-release-root"
    failed.mkdir(parents=True)
    (failed / "ROLLBACK_REQUIRED.txt").write_text("private", encoding="utf-8")
    release_root = tmp_path / "custom-deploy-root"
    release = release_root / "releases" / "candidate"
    release.mkdir(parents=True)
    _write_valid_confirmation(failed, release)
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _age(failed, now, 31)

    result = prune_deploy_transactions(
        state_dir,
        release_root=release_root,
        now=now,
    )

    assert result["removed"] == ["custom-release-root"]
    assert not failed.exists()


def test_prune_preserves_transaction_with_symlink_control_marker(tmp_path):
    state_dir = tmp_path / "case-weather"
    transaction = state_dir / "backups" / "deploy-transactions" / "linked-terminal"
    transaction.mkdir(parents=True)
    outside = tmp_path / "outside-committed"
    outside.write_text("done\n", encoding="utf-8")
    (transaction / "COMMITTED").symlink_to(outside)
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    _age(transaction, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["linked-terminal"]
    assert transaction.exists()
    assert outside.read_text(encoding="utf-8") == "done\n"


@pytest.mark.parametrize(
    "marker_name",
    (
        "qweather-key-transition.json",
        "formal-smoke-lease.journal",
        "FORWARD_ONLY_REQUIRED",
        "PUBLIC_START_ATTEMPTED",
    ),
)
def test_prune_preserves_each_unfinished_activation_artifact(tmp_path, marker_name):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, marker_name, now)
    marker = transaction / marker_name
    marker.write_text("private activation state\n", encoding="utf-8")
    marker.chmod(0o600)
    _age(transaction, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == [marker_name]
    assert transaction.exists()


def test_prune_preserves_invalid_confirmation_even_with_valid_terminal(tmp_path):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, "invalid-confirmed-commit", now)
    _write_terminal(transaction, "COMMITTED")
    confirmation = transaction / "RECOVERY_CONFIRMED"
    confirmation.write_text(
        "confirmed_at=2026-07-18T00:00:00Z\n",
        encoding="utf-8",
    )
    confirmation.chmod(0o600)
    _age(transaction, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["invalid-confirmed-commit"]
    assert transaction.exists()


@pytest.mark.parametrize(
    ("invalid_kind", "marker_name", "payload", "mode"),
    (
        ("wrong-mode", "COMMITTED", "success\n", 0o644),
        ("wrong-commit-payload", "COMMITTED", "done\n", 0o600),
        ("wrong-rollback-payload", "ROLLED_BACK", "done\n", 0o600),
    ),
)
def test_prune_preserves_invalid_terminal_marker(
    tmp_path,
    invalid_kind,
    marker_name,
    payload,
    mode,
):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, invalid_kind, now)
    _write_terminal(transaction, marker_name, payload, mode=mode)
    _age(transaction, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == [invalid_kind]
    assert transaction.exists()


def test_prune_preserves_conflicting_terminal_markers(tmp_path):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, "conflicting-terminals", now)
    _write_terminal(transaction, "COMMITTED")
    _write_terminal(transaction, "ROLLED_BACK")
    _age(transaction, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["conflicting-terminals"]
    assert transaction.exists()


@pytest.mark.parametrize(
    ("marker_name", "payload"),
    (
        ("COMMITTED", "success\n"),
        ("ROLLED_BACK", "success\n"),
        ("ROLLED_BACK", "pre-mutation\n"),
    ),
)
def test_prune_removes_transaction_with_one_valid_terminal(
    tmp_path,
    marker_name,
    payload,
):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    name = f"valid-{marker_name.lower()}-{payload.strip()}"
    transaction = _old_transaction(state_dir, name, now)
    (transaction / "ACTIVATION_STARTED").write_text("private\n", encoding="utf-8")
    _write_terminal(transaction, marker_name, payload)
    _age(transaction, now, 31)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == [name]
    assert result["preserved_unresolved"] == []
    assert not transaction.exists()


def test_prune_rejects_symlinked_transaction_root_without_deleting_target(tmp_path):
    state_dir = tmp_path / "case-weather"
    backups = state_dir / "backups"
    backups.mkdir(parents=True)
    outside_root = tmp_path / "outside-transactions"
    outside_transaction = outside_root / "must-survive"
    outside_transaction.mkdir(parents=True)
    (outside_transaction / "private.txt").write_text("keep", encoding="utf-8")
    (backups / "deploy-transactions").symlink_to(
        outside_root,
        target_is_directory=True,
    )

    with pytest.raises(ValueError):
        prune_deploy_transactions(
            state_dir,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )

    assert (outside_transaction / "private.txt").read_text(encoding="utf-8") == "keep"


def test_prune_rejects_symlinked_alert_root_before_any_deletion(tmp_path):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, "old-safe-transaction", now)
    (transaction / "private.txt").write_text("keep", encoding="utf-8")
    outside_alerts = tmp_path / "outside-alerts"
    outside_alerts.mkdir()
    (outside_alerts / "must-survive.txt").write_text("keep", encoding="utf-8")
    (state_dir / "backups" / "deploy-retention-alerts").symlink_to(
        outside_alerts,
        target_is_directory=True,
    )

    with pytest.raises(ValueError):
        prune_deploy_transactions(state_dir, now=now)

    assert transaction.exists()
    assert (outside_alerts / "must-survive.txt").read_text(encoding="utf-8") == "keep"


def test_prune_never_overwrites_symlinked_alert_file(tmp_path):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, "confirmed-attention", now)
    (transaction / "POST_COMMIT_ATTENTION.txt").write_text(
        "private",
        encoding="utf-8",
    )
    release = _release_path(state_dir)
    _write_valid_confirmation(transaction, release)
    _age(transaction, now, 31)
    alert_root = state_dir / "backups" / "deploy-retention-alerts"
    alert_root.mkdir()
    victim = tmp_path / "outside-victim.txt"
    victim.write_text("must stay unchanged", encoding="utf-8")
    (alert_root / "confirmed-attention.txt").symlink_to(victim)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["confirmed-attention"]
    assert transaction.exists()
    assert victim.read_text(encoding="utf-8") == "must stay unchanged"


def test_prune_durably_records_new_alert_directory_before_deletion(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, "confirmed-durable-alert", now)
    (transaction / "POST_COMMIT_ATTENTION.txt").write_text(
        "private",
        encoding="utf-8",
    )
    release = _release_path(state_dir)
    _write_valid_confirmation(transaction, release)
    _age(transaction, now, 31)
    backups = state_dir / "backups"
    backups_identity = (backups.stat().st_dev, backups.stat().st_ino)
    events = []
    real_fsync = prune_module.os.fsync
    real_rmtree = prune_module.shutil.rmtree

    def record_fsync(descriptor):
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == backups_identity:
            events.append("backups-fsync")
        return real_fsync(descriptor)

    def record_rmtree(*args, **kwargs):
        events.append("rmtree")
        return real_rmtree(*args, **kwargs)

    record_rmtree.avoids_symlink_attacks = real_rmtree.avoids_symlink_attacks
    monkeypatch.setattr(prune_module.os, "fsync", record_fsync)
    monkeypatch.setattr(prune_module.shutil, "rmtree", record_rmtree)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == ["confirmed-durable-alert"]
    assert events.count("backups-fsync") == 1
    assert events.index("backups-fsync") < events.index("rmtree")


def test_prune_preserves_transaction_name_with_line_break(tmp_path):
    state_dir = tmp_path / "case-weather"
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    transaction = _old_transaction(state_dir, "line\nbreak", now)

    result = prune_deploy_transactions(state_dir, now=now)

    assert result["removed"] == []
    assert result["preserved_unresolved"] == ["line\nbreak"]
    assert transaction.exists()


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


@pytest.mark.parametrize("unsafe", (Path("/"), Path("relative-state")))
def test_prune_rejects_broad_state_directory(unsafe):
    with pytest.raises(ValueError):
        prune_deploy_transactions(unsafe)


def test_prune_rejects_symlinked_state_directory(tmp_path):
    actual_state = tmp_path / "actual-state"
    transaction = actual_state / "backups" / "deploy-transactions" / "keep"
    transaction.mkdir(parents=True)
    (transaction / "private.txt").write_text("keep", encoding="utf-8")
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(actual_state, target_is_directory=True)

    with pytest.raises(ValueError):
        prune_deploy_transactions(linked_state)

    assert (transaction / "private.txt").read_text(encoding="utf-8") == "keep"
