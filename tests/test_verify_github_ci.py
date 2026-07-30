import importlib.util
import io
import json
from pathlib import Path
import stat
from urllib.error import HTTPError

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_github_ci.py"
COMMIT = "a" * 40
REPO = "AlfWuxy/weather-Web"
WORKFLOW = ".github/workflows/ci.yml"
BRANCH = "codex/miniprogram-v1.1.1-unified"
PROOF_JOB = "可发布提交证明"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_github_ci", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload):
        if isinstance(payload, bytes):
            self.payload = payload
        else:
            self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return self.payload


def _run(
    *,
    run_id=101,
    created_at="2026-07-31T01:00:00Z",
    status="completed",
    conclusion="success",
    path=None,
    repository=REPO,
    head_repository=REPO,
):
    return {
        "id": run_id,
        "run_attempt": 1,
        "path": path or f"{WORKFLOW}@refs/heads/{BRANCH}",
        "head_branch": BRANCH,
        "head_sha": COMMIT,
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "repository": {"full_name": repository},
        "head_repository": {"full_name": head_repository},
        "created_at": created_at,
        "updated_at": created_at,
        "html_url": f"https://github.com/example/actions/runs/{run_id}",
    }


def _job(*, conclusion="success", name=PROOF_JOB, run_id=101):
    return {
        "id": 201,
        "name": name,
        "run_id": run_id,
        "head_sha": COMMIT,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-07-31T01:00:01Z",
        "completed_at": "2026-07-31T01:02:00Z",
        "html_url": "https://github.com/example/actions/jobs/201",
    }


def _install_api(
    monkeypatch,
    module,
    *,
    branch_sha=COMMIT,
    runs=None,
    jobs=None,
):
    runs = [_run()] if runs is None else runs
    jobs = [_job()] if jobs is None else jobs
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        url = request.full_url
        if "/branches/" in url:
            return _Response({"commit": {"sha": branch_sha}})
        if "/actions/workflows/" in url:
            return _Response(
                {"total_count": len(runs), "workflow_runs": runs}
            )
        if "/actions/runs/" in url and "/jobs?" in url:
            return _Response({"total_count": len(jobs), "jobs": jobs})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    return requests


def _verify_online(module):
    return module.verify_online(
        repo=REPO,
        workflow=WORKFLOW,
        commit=COMMIT,
        branch=BRANCH,
        proof_job=PROOF_JOB,
        token="secret-token",
        timeout=10,
    )


def test_exact_commit_success_writes_private_token_free_receipt(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    requests = _install_api(monkeypatch, module)

    receipt = _verify_online(module)
    output = tmp_path / "ci-proof.json"
    module._write_receipt(output, receipt)
    decoded = json.loads(output.read_text(encoding="utf-8"))

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert decoded["commit_sha"] == COMMIT
    assert decoded["run"]["id"] == 101
    assert decoded["job"]["name"] == PROOF_JOB
    assert "secret-token" not in output.read_text(encoding="utf-8")
    assert all(request.headers["Authorization"] == "Bearer secret-token" for request, _ in requests)
    module.verify_receipt(
        decoded,
        repo=REPO,
        workflow=WORKFLOW,
        commit=COMMIT,
        branch=BRANCH,
        proof_job=PROOF_JOB,
    )


def test_branch_tip_mismatch_fails_before_workflow_lookup(monkeypatch):
    module = _load_module()
    requests = _install_api(monkeypatch, module, branch_sha="b" * 40)

    with pytest.raises(module.VerificationError, match="分支 tip"):
        _verify_online(module)

    assert len(requests) == 1


def test_latest_pending_run_cannot_be_hidden_by_older_success(monkeypatch):
    module = _load_module()
    runs = [
        _run(run_id=101, created_at="2026-07-31T01:00:00Z"),
        _run(
            run_id=102,
            created_at="2026-07-31T02:00:00Z",
            status="in_progress",
            conclusion=None,
        ),
    ]
    _install_api(monkeypatch, module, runs=runs)

    with pytest.raises(module.VerificationError, match="尚未完成"):
        _verify_online(module)


@pytest.mark.parametrize(
    "run",
    (
        _run(repository="other/repo"),
        _run(head_repository="other/repo"),
        _run(path=".github/workflows/other.yml"),
    ),
)
def test_wrong_repository_or_workflow_binding_is_rejected(monkeypatch, run):
    module = _load_module()
    _install_api(monkeypatch, module, runs=[run])

    with pytest.raises(module.VerificationError):
        _verify_online(module)


@pytest.mark.parametrize(
    "jobs",
    (
        [],
        [_job(conclusion="failure")],
        [_job(), _job()],
    ),
)
def test_missing_failed_or_duplicate_proof_job_is_rejected(monkeypatch, jobs):
    module = _load_module()
    _install_api(monkeypatch, module, jobs=jobs)

    with pytest.raises(module.VerificationError):
        _verify_online(module)


def test_rate_limit_and_invalid_json_fail_closed(monkeypatch):
    module = _load_module()

    def limited(_request, timeout):
        raise HTTPError(
            "https://api.github.com/example",
            403,
            "forbidden",
            {},
            io.BytesIO(b"{}"),
        )

    monkeypatch.setattr(module, "urlopen", limited)
    with pytest.raises(module.VerificationError, match="限流"):
        _verify_online(module)

    monkeypatch.setattr(
        module,
        "urlopen",
        lambda _request, timeout: _Response(b"not-json"),
    )
    with pytest.raises(module.VerificationError, match="无效 JSON"):
        _verify_online(module)


def test_offline_receipt_rejects_mode_and_binding_tamper(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    _install_api(monkeypatch, module)
    receipt = _verify_online(module)
    output = tmp_path / "ci-proof.json"
    module._write_receipt(output, receipt)

    output.chmod(0o644)
    with pytest.raises(module.VerificationError, match="0600"):
        module._read_receipt(output)

    output.chmod(0o600)
    decoded = module._read_receipt(output)
    decoded["job"]["head_sha"] = "b" * 40
    with pytest.raises(module.VerificationError, match="job 字段"):
        module.verify_receipt(
            decoded,
            repo=REPO,
            workflow=WORKFLOW,
            commit=COMMIT,
            branch=BRANCH,
            proof_job=PROOF_JOB,
        )
