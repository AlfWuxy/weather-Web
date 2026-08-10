import hashlib
import json
import os
from pathlib import Path
import grp
import pwd
import stat
import subprocess
import sys

import pytest

from scripts import model_artifact


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "model_artifact.py"
ARTIFACT_NAMES = (
    "disease_predictor.pkl",
    "scaler.pkl",
    "label_encoder.pkl",
)
COMMIT = "a" * 40


def _write_private(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def _fixture_files() -> dict[str, bytes]:
    return {
        "disease_predictor.pkl": b"synthetic predictor\n",
        "scaler.pkl": b"synthetic scaler\n",
        "label_encoder.pkl": b"synthetic labels\n",
    }


def _write_source_and_manifest(
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
) -> tuple[Path, Path, dict]:
    contents = files or _fixture_files()
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    specs = {}
    for filename in ARTIFACT_NAMES:
        content = contents[filename]
        _write_private(source / filename, content)
        specs[filename] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    manifest = {
        "feature_cols": ["年龄数值"],
        "runtime_artifacts": {
            "expected_sklearn_version": "1.7.2",
            "files": specs,
        },
    }
    manifest_path = tmp_path / "feature_config.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return source, manifest_path, manifest


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _snapshot(
    *,
    source: Path,
    manifest: Path,
    output: Path,
    receipt: Path,
    commit: str = COMMIT,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "snapshot",
        "--source-dir",
        str(source),
        "--manifest",
        str(manifest),
        "--output-dir",
        str(output),
        "--receipt",
        str(receipt),
        "--commit",
        commit,
    )


def test_snapshot_and_verify_create_private_exact_artifacts(tmp_path: Path) -> None:
    source, manifest_path, manifest = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "snapshot-receipt.json"

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )

    assert completed.returncode == 0, completed.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert {path.name for path in output.iterdir()} == set(ARTIFACT_NAMES)
    for filename in ARTIFACT_NAMES:
        target = output / filename
        assert target.read_bytes() == (source / filename).read_bytes()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.stat().st_nlink == 1
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "receipt_type": "yilao-model-artifact-snapshot",
        "status": "passed",
        "commit": COMMIT,
        "expected_sklearn_version": "1.7.2",
        "files": manifest["runtime_artifacts"]["files"],
    }

    verified = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--receipt",
        str(receipt),
        "--commit",
        COMMIT,
    )
    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        ("relative_source", "source-dir 必须使用绝对路径"),
        ("source_symlink", "source-dir 必须是非符号链接目录"),
        ("wrong_mode", "源文件元数据不符合要求"),
        ("hard_link", "源文件元数据不符合要求"),
        ("wrong_commit", "commit 必须是 40 位"),
    ],
)
def test_snapshot_rejects_unsafe_source_or_commit(
    tmp_path: Path,
    mutation: str,
    expected_fragment: str,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    commit = COMMIT

    if mutation == "relative_source":
        source = Path("relative-model-source")
    elif mutation == "source_symlink":
        link = tmp_path / "source-link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    elif mutation == "wrong_mode":
        (source / "scaler.pkl").chmod(0o644)
    elif mutation == "hard_link":
        os.link(
            source / "label_encoder.pkl",
            tmp_path / "second-label-link.pkl",
        )
    elif mutation == "wrong_commit":
        commit = "A" * 40

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
        commit=commit,
    )

    assert completed.returncode != 0
    assert expected_fragment in completed.stderr
    assert not output.exists()
    assert not receipt.exists()


def test_snapshot_rejects_symlinked_source_file_without_leaking_content(
    tmp_path: Path,
) -> None:
    secret = b"do-not-print-this-secret-model-content"
    source, manifest_path, manifest = _write_source_and_manifest(tmp_path)
    external = tmp_path / "external.pkl"
    _write_private(external, secret)
    (source / "scaler.pkl").unlink()
    (source / "scaler.pkl").symlink_to(external)
    manifest["runtime_artifacts"]["files"]["scaler.pkl"] = {
        "sha256": hashlib.sha256(secret).hexdigest(),
        "size_bytes": len(secret),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=tmp_path / "snapshot",
        receipt=tmp_path / "receipt.json",
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert secret.decode() not in combined
    assert str(external) not in combined


@pytest.mark.parametrize("entry_kind", ("regular", "symlink"))
def test_snapshot_rejects_extra_pickle_entry_before_copy_without_leaking(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    secret = b"extra-private-model-content-never-log"
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    extra = source / "unapproved-extra.pkl"
    external = tmp_path / "private-external-model.bin"
    if entry_kind == "regular":
        _write_private(extra, secret)
    else:
        _write_private(external, secret)
        extra.symlink_to(external)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )

    assert completed.returncode != 0
    assert "pkl 条目集合不符合要求" in completed.stderr
    assert not output.exists()
    assert not receipt.exists()
    combined = completed.stdout + completed.stderr
    assert secret.decode() not in combined
    assert str(external) not in combined


def test_snapshot_allows_non_pickle_metadata_entries(tmp_path: Path) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    (source / "README.txt").write_text("metadata", encoding="utf-8")
    (source / "latest-metadata").symlink_to(source / "README.txt")
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )

    assert completed.returncode == 0, completed.stderr
    assert {path.name for path in output.iterdir()} == set(ARTIFACT_NAMES)
    assert receipt.is_file()


def test_snapshot_rejects_directory_change_during_pickle_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    source_inode = source.stat().st_ino
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    original_listdir = os.listdir
    changed = False

    def mutate_during_list(directory_fd):
        nonlocal changed
        names = original_listdir(directory_fd)
        if (
            not changed
            and isinstance(directory_fd, int)
            and os.fstat(directory_fd).st_ino == source_inode
        ):
            changed = True
            _write_private(source / "raced-extra.pkl", b"race")
        return names

    monkeypatch.setattr(model_artifact.os, "listdir", mutate_during_list)

    with pytest.raises(
        model_artifact.ModelArtifactError,
        match="列出期间发生变化",
    ):
        model_artifact.snapshot(
            source_dir=source,
            manifest_path=manifest_path,
            output_dir=output,
            receipt_path=receipt,
            commit=COMMIT,
        )

    assert changed is True
    assert not output.exists()
    assert not receipt.exists()


def test_snapshot_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("当前平台不支持 FIFO")
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    fifo = source / "scaler.pkl"
    fifo.unlink()
    os.mkfifo(fifo, mode=0o600)

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=tmp_path / "snapshot",
        receipt=tmp_path / "receipt.json",
    )

    assert completed.returncode != 0
    assert "元数据不符合要求" in completed.stderr
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize(
    "mutate_manifest",
    [
        lambda runtime: runtime.update({"extra": True}),
        lambda runtime: runtime.update({"expected_sklearn_version": "1.6.1"}),
        lambda runtime: runtime["files"].pop("scaler.pkl"),
        lambda runtime: runtime["files"].update(
            {
                "unexpected.pkl": {
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                }
            }
        ),
        lambda runtime: runtime["files"]["scaler.pkl"].update({"extra": True}),
    ],
)
def test_manifest_runtime_artifacts_schema_is_exact(
    tmp_path: Path,
    mutate_manifest,
) -> None:
    source, manifest_path, manifest = _write_source_and_manifest(tmp_path)
    mutate_manifest(manifest["runtime_artifacts"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=tmp_path / "snapshot",
        receipt=tmp_path / "receipt.json",
    )

    assert completed.returncode != 0
    assert not (tmp_path / "snapshot").exists()


def test_manifest_rejects_artifact_size_over_64_mib(tmp_path: Path) -> None:
    source, manifest_path, manifest = _write_source_and_manifest(tmp_path)
    manifest["runtime_artifacts"]["files"]["disease_predictor.pkl"]["size_bytes"] = (
        64 * 1024 * 1024 + 1
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )

    assert completed.returncode != 0
    assert "大小格式异常" in completed.stderr
    assert not output.exists()
    assert not receipt.exists()


def test_hash_mismatch_fails_closed_and_removes_partial_output(
    tmp_path: Path,
) -> None:
    secret = b"private-mismatch-content-never-log"
    source, manifest_path, manifest = _write_source_and_manifest(
        tmp_path,
        files={
            "disease_predictor.pkl": secret,
            "scaler.pkl": b"scaler",
            "label_encoder.pkl": b"labels",
        },
    )
    manifest["runtime_artifacts"]["files"]["disease_predictor.pkl"]["sha256"] = (
        "0" * 64
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"

    completed = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )

    assert completed.returncode != 0
    assert not output.exists()
    assert not receipt.exists()
    assert secret.decode() not in completed.stderr


def test_snapshot_rejects_source_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    target = source / "disease_predictor.pkl"
    target_inode = target.stat().st_ino
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    original_read = os.read
    changed = False

    def mutate_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, size)
        if (
            chunk
            and not changed
            and os.fstat(descriptor).st_ino == target_inode
        ):
            changed = True
            target.write_bytes(b"x" * len(chunk))
            target.chmod(0o600)
        return chunk

    monkeypatch.setattr(model_artifact.os, "read", mutate_after_first_read)

    with pytest.raises(
        model_artifact.ModelArtifactError,
        match="读取期间发生变化",
    ):
        model_artifact.snapshot(
            source_dir=source,
            manifest_path=manifest_path,
            output_dir=output,
            receipt_path=receipt,
            commit=COMMIT,
        )

    assert changed is True
    assert not output.exists()
    assert not receipt.exists()


def test_snapshot_requires_new_output_and_receipt(tmp_path: Path) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    output.mkdir()
    receipt = tmp_path / "receipt.json"

    existing_output = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )
    assert existing_output.returncode != 0
    assert output.is_dir()

    output.rmdir()
    receipt.write_text("keep", encoding="utf-8")
    existing_receipt = _snapshot(
        source=source,
        manifest=manifest_path,
        output=output,
        receipt=receipt,
    )
    assert existing_receipt.returncode != 0
    assert receipt.read_text(encoding="utf-8") == "keep"
    assert not output.exists()


def test_verify_rejects_extra_pickle_and_required_symlink(tmp_path: Path) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )

    _write_private(output / "extra.pkl", b"extra")
    extra = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
    )
    assert extra.returncode != 0

    (output / "extra.pkl").unlink()
    (output / "scaler.pkl").unlink()
    (output / "scaler.pkl").symlink_to(source / "scaler.pkl")
    linked = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
    )
    assert linked.returncode != 0


@pytest.mark.parametrize("mutation", ("world_readable", "hard_link"))
def test_verify_rejects_non_private_or_hard_linked_artifact(
    tmp_path: Path,
    mutation: str,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )
    if mutation == "world_readable":
        (output / "disease_predictor.pkl").chmod(0o644)
    else:
        os.link(
            output / "disease_predictor.pkl",
            tmp_path / "second-predictor-link.pkl",
        )

    completed = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
    )

    assert completed.returncode != 0
    assert "元数据不符合要求" in completed.stderr


def test_verify_supports_explicit_runtime_owner_group_and_modes(
    tmp_path: Path,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )
    output.chmod(0o750)
    for filename in ARTIFACT_NAMES:
        (output / filename).chmod(0o640)

    default_contract = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
    )
    assert default_contract.returncode != 0

    explicit_contract = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--receipt",
        str(receipt),
        "--commit",
        COMMIT,
        "--expected-owner",
        pwd.getpwuid(os.geteuid()).pw_name,
        "--expected-group",
        grp.getgrgid(output.stat().st_gid).gr_name,
        "--expected-file-mode",
        "0640",
        "--expected-dir-mode",
        "0750",
    )
    assert explicit_contract.returncode == 0, explicit_contract.stderr

    numeric_contract = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--expected-owner",
        str(os.geteuid()),
        "--expected-group",
        str(output.stat().st_gid),
        "--expected-file-mode",
        "0640",
        "--expected-dir-mode",
        "0750",
    )
    assert numeric_contract.returncode == 0, numeric_contract.stderr


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--expected-owner", "missing-model-owner-for-test"),
        ("--expected-group", "missing-model-group-for-test"),
        ("--expected-file-mode", "640"),
        ("--expected-dir-mode", "0o750"),
    ],
)
def test_verify_rejects_unresolvable_identity_or_invalid_mode(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )

    completed = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        flag,
        value,
    )

    assert completed.returncode != 0


def test_verify_allows_non_pickle_metadata_but_checks_receipt_consistency(
    tmp_path: Path,
) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )
    (output / "README.txt").write_text("metadata", encoding="utf-8")

    without_receipt = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
    )
    assert without_receipt.returncode == 0, without_receipt.stderr

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["files"]["scaler.pkl"]["sha256"] = "f" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt.chmod(0o600)
    with_receipt = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--receipt",
        str(receipt),
        "--commit",
        COMMIT,
    )
    assert with_receipt.returncode != 0
    assert "receipt 与 manifest 不一致" in with_receipt.stderr


def test_verify_receipt_requires_and_binds_commit(tmp_path: Path) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )

    missing_commit = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--receipt",
        str(receipt),
    )
    assert missing_commit.returncode != 0
    assert "必须同时提供 commit" in missing_commit.stderr

    stale_commit = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--receipt",
        str(receipt),
        "--commit",
        "b" * 40,
    )
    assert stale_commit.returncode != 0
    assert "receipt 与本轮 commit 不一致" in stale_commit.stderr


def test_verify_rejects_boolean_receipt_schema_version(tmp_path: Path) -> None:
    source, manifest_path, _ = _write_source_and_manifest(tmp_path)
    output = tmp_path / "snapshot"
    receipt = tmp_path / "receipt.json"
    assert (
        _snapshot(
            source=source,
            manifest=manifest_path,
            output=output,
            receipt=receipt,
        ).returncode
        == 0
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt.chmod(0o600)

    completed = _run(
        "verify",
        "--artifact-dir",
        str(output),
        "--manifest",
        str(manifest_path),
        "--receipt",
        str(receipt),
        "--commit",
        COMMIT,
    )

    assert completed.returncode != 0
    assert "固定字段不符合要求" in completed.stderr
