from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import types
import venv

import pytest

from scripts import release_runtime_smoke as smoke


COMMIT = "a" * 40
LOCK_SHA = smoke.EXPECTED_REQUIREMENTS_LOCK_SHA256
PYTHON_MINOR = smoke.EXPECTED_PYTHON_MINOR


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "app"
    repo.mkdir()
    source_lock = Path(__file__).resolve().parents[1] / "requirements.lock"
    (repo / "requirements.lock").write_bytes(source_lock.read_bytes())
    (repo / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (repo / "migrations").mkdir()
    return repo


def _make_python(tmp_path: Path) -> Path:
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake executable")
    python.chmod(0o700)
    return python


def _fake_python_identity(python: Path) -> dict[str, str]:
    return {
        "executable": os.fspath(python),
        "executable_realpath": os.fspath(python.resolve()),
        "minor": PYTHON_MINOR,
        "prefix_realpath": os.fspath(python.parent.parent.resolve()),
        "version": f"{PYTHON_MINOR}.9",
    }


def _patch_success_probes(
    monkeypatch: pytest.MonkeyPatch,
    python: Path,
) -> None:
    monkeypatch.setattr(
        smoke,
        "_runtime_identity",
        lambda expected: _fake_python_identity(python),
    )
    monkeypatch.setattr(smoke, "_run_pip_check", lambda: None)
    monkeypatch.setattr(
        smoke,
        "_collect_packages",
        lambda locked: {
            "flask": locked["flask"],
            "sqlalchemy": locked["sqlalchemy"],
            "alembic": locked["alembic"],
            "gunicorn": locked["gunicorn"],
        },
    )
    monkeypatch.setattr(smoke, "_collect_alembic_heads", lambda root: ["0028_head"])
    monkeypatch.setattr(smoke, "_normalized_ru_maxrss_kib", lambda: 43210)
    monkeypatch.setattr(smoke, "_utc_now_text", lambda: "2026-07-31T12:00:00Z")


def _create_valid_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    repo = _make_repo(tmp_path)
    python = _make_python(tmp_path)
    output_dir = tmp_path / "runtime"
    output_dir.mkdir(mode=0o700)
    receipt = output_dir / "runtime-smoke.json"
    _patch_success_probes(monkeypatch, python)
    smoke.create_receipt(
        repo_root=repo,
        expected_commit=COMMIT,
        expected_python=python,
        expected_python_minor=PYTHON_MINOR,
        expected_lock_sha=LOCK_SHA,
        output=receipt,
    )
    return repo, python, receipt


def test_create_receipt_is_private_bounded_and_contains_only_fixed_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SECRET_KEY", "do-not-leak-this-secret")
    monkeypatch.setenv("QWEATHER_JWT_PRIVATE_KEY", "do-not-leak-this-key")

    _, _, receipt = _create_valid_receipt(tmp_path, monkeypatch)

    metadata = receipt.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_size < smoke.MAX_RECEIPT_BYTES
    text = receipt.read_text(encoding="utf-8")
    assert "do-not-leak-this-secret" not in text
    assert "do-not-leak-this-key" not in text
    assert "SECRET_KEY" not in text
    assert "QWEATHER" not in text

    payload = json.loads(text)
    assert payload["schema_version"] == 1
    assert payload["receipt_type"] == smoke.RECEIPT_TYPE
    assert payload["status"] == "passed"
    assert payload["commit_sha"] == COMMIT
    assert payload["python"]["minor"] == "3.11"
    assert payload["requirements"] == {
        "lock_sha256": LOCK_SHA,
        "pip_check": "passed",
    }
    assert payload["alembic"]["heads"] == ["0028_head"]
    assert payload["resource"]["ru_maxrss_kib"] == 43210
    assert set(payload["packages"]) == set(smoke.PACKAGE_IMPORTS)
    assert all(payload["checks"].values())


def test_create_receipt_refuses_to_replace_existing_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _make_repo(tmp_path)
    python = _make_python(tmp_path)
    output = tmp_path / "existing.json"
    output.write_text("old proof", encoding="utf-8")
    output.chmod(0o600)
    _patch_success_probes(monkeypatch, python)

    with pytest.raises(smoke.SmokeError, match="拒绝覆盖"):
        smoke.create_receipt(
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
            output=output,
        )

    assert output.read_text(encoding="utf-8") == "old proof"


def test_create_receipt_writes_nothing_when_a_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _make_repo(tmp_path)
    python = _make_python(tmp_path)
    output = tmp_path / "failed.json"
    _patch_success_probes(monkeypatch, python)

    def fail_pip_check() -> None:
        raise smoke.SmokeError("pip check 未通过。")

    monkeypatch.setattr(smoke, "_run_pip_check", fail_pip_check)

    with pytest.raises(smoke.SmokeError, match="pip check"):
        smoke.create_receipt(
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
            output=output,
        )

    assert not output.exists()


def test_pip_check_uses_only_offline_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SECRET_KEY", "must-not-reach-child")
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    smoke._run_pip_check()

    assert observed["command"] == [
        smoke.sys.executable,
        "-I",
        "-m",
        "pip",
        "check",
    ]
    child_env = observed["env"]
    assert child_env["PIP_NO_INDEX"] == "1"
    assert child_env["PIP_CONFIG_FILE"] == os.devnull
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert "SECRET_KEY" not in child_env


def test_create_receipt_rejects_caller_attempt_to_relax_fixed_baseline(
    tmp_path: Path,
):
    repo = _make_repo(tmp_path)
    python = _make_python(tmp_path)

    with pytest.raises(smoke.SmokeError, match="Python minor"):
        smoke.create_receipt(
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor="3.12",
            expected_lock_sha=LOCK_SHA,
            output=tmp_path / "python.json",
        )
    with pytest.raises(smoke.SmokeError, match="固定基线"):
        smoke.create_receipt(
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha="b" * 64,
            output=tmp_path / "lock.json",
        )


def test_runtime_identity_binds_executable_realpath_prefix_and_minor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    python = _make_python(tmp_path)
    monkeypatch.setattr(smoke.sys, "executable", os.fspath(python))
    monkeypatch.setattr(smoke.sys, "prefix", os.fspath(python.parent.parent))
    monkeypatch.setattr(
        smoke.sys,
        "version_info",
        types.SimpleNamespace(major=3, minor=11),
    )
    monkeypatch.setattr(smoke.platform, "python_version", lambda: "3.11.9")

    identity = smoke._runtime_identity(python)

    assert identity == _fake_python_identity(python)

    other_python = _make_python(tmp_path / "other")
    with pytest.raises(smoke.SmokeError, match="候选虚拟环境"):
        smoke._runtime_identity(other_python)


@pytest.mark.skipif(
    f"{sys.version_info.major}.{sys.version_info.minor}" != PYTHON_MINOR,
    reason="只在正式 Python 3.11 job 验证真实 venv 路径语义",
)
def test_runtime_identity_with_real_python311_venv(tmp_path: Path):
    runtime_venv = tmp_path / "runtime-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime_venv)
    python = runtime_venv / "bin" / "python"
    repo_root = Path(__file__).resolve().parents[1]
    source = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from scripts import release_runtime_smoke as smoke\n"
        "identity = smoke._runtime_identity(Path(sys.argv[2]))\n"
        "assert identity['minor'] == '3.11'\n"
        "assert identity['executable'] == sys.argv[2]\n"
    )

    completed = subprocess.run(
        [os.fspath(python), "-c", source, os.fspath(repo_root), os.fspath(python)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )


def test_verify_receipt_is_offline_and_does_not_import_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo, python, receipt = _create_valid_receipt(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline verification must not import or spawn")

    monkeypatch.setattr(smoke.importlib, "import_module", forbidden)
    monkeypatch.setattr(smoke.subprocess, "run", forbidden)

    validated = smoke.verify_receipt(
        receipt=receipt,
        repo_root=repo,
        expected_commit=COMMIT,
        expected_python=python,
        expected_python_minor=PYTHON_MINOR,
        expected_lock_sha=LOCK_SHA,
    )

    assert validated["commit_sha"] == COMMIT


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commit_sha", "b" * 40, "commit"),
        ("status", "failed", "状态"),
    ],
)
def test_verify_receipt_rejects_tampered_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
):
    repo, python, receipt = _create_valid_receipt(tmp_path, monkeypatch)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload[field] = value
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt.chmod(0o600)

    with pytest.raises(smoke.SmokeError, match=message):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )


def test_verify_receipt_rejects_wrong_mode_and_duplicate_json_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo, python, receipt = _create_valid_receipt(tmp_path, monkeypatch)
    receipt.chmod(0o640)

    with pytest.raises(smoke.SmokeError, match="0600"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )

    receipt.write_text('{"status":"passed","status":"passed"}', encoding="utf-8")
    receipt.chmod(0o600)
    with pytest.raises(smoke.SmokeError, match="重复字段"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )


def test_verify_receipt_requires_current_owner_and_single_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo, python, receipt = _create_valid_receipt(tmp_path, monkeypatch)
    extra_link = receipt.with_name("extra-link.json")
    os.link(receipt, extra_link)

    with pytest.raises(smoke.SmokeError, match="硬链接"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )

    extra_link.unlink()
    monkeypatch.setattr(smoke.os, "geteuid", lambda: receipt.stat().st_uid + 1)
    with pytest.raises(smoke.SmokeError, match="当前激活用户"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )


def test_verify_receipt_rejects_lock_or_python_path_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo, python, receipt = _create_valid_receipt(tmp_path, monkeypatch)
    other_python = _make_python(tmp_path / "other")

    with pytest.raises(smoke.SmokeError, match="Python 路径"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=other_python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )

    (repo / "requirements.lock").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(smoke.SmokeError, match="requirements.lock"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )


def test_verify_receipt_rejects_package_versions_not_matching_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo, python, receipt = _create_valid_receipt(tmp_path, monkeypatch)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["packages"]["flask"] = "999.0.0"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt.chmod(0o600)

    with pytest.raises(smoke.SmokeError, match="关键依赖版本"):
        smoke.verify_receipt(
            receipt=receipt,
            repo_root=repo,
            expected_commit=COMMIT,
            expected_python=python,
            expected_python_minor=PYTHON_MINOR,
            expected_lock_sha=LOCK_SHA,
        )


def test_alembic_requires_exactly_one_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = _make_repo(tmp_path)

    class FakeConfig:
        def __init__(self, path: str):
            self.path = path

        def set_main_option(self, key: str, value: str) -> None:
            self.option = (key, value)

    class FakeScriptDirectory:
        @classmethod
        def from_config(cls, config):
            return cls()

        def get_heads(self):
            return ["head_one", "head_two"]

    alembic_module = types.ModuleType("alembic")
    alembic_module.__path__ = []
    config_module = types.ModuleType("alembic.config")
    config_module.Config = FakeConfig
    script_module = types.ModuleType("alembic.script")
    script_module.ScriptDirectory = FakeScriptDirectory
    alembic_module.config = config_module
    alembic_module.script = script_module
    monkeypatch.setitem(sys.modules, "alembic", alembic_module)
    monkeypatch.setitem(sys.modules, "alembic.config", config_module)
    monkeypatch.setitem(sys.modules, "alembic.script", script_module)

    with pytest.raises(smoke.SmokeError, match="精确存在一个"):
        smoke._collect_alembic_heads(repo)


def test_ru_maxrss_is_normalized_to_kib(monkeypatch: pytest.MonkeyPatch):
    class Usage:
        def __init__(self, value: int):
            self.ru_maxrss = value

    monkeypatch.setattr(smoke.resource, "getrusage", lambda target: Usage(2 * 1024))
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    assert smoke._normalized_ru_maxrss_kib() == 2

    monkeypatch.setattr(smoke.sys, "platform", "linux")
    assert smoke._normalized_ru_maxrss_kib() == 2 * 1024


def test_cli_help_exposes_run_and_offline_verification():
    parser = smoke.build_parser()
    help_text = parser.format_help()

    assert "run" in help_text
    assert "verify-receipt" in help_text
