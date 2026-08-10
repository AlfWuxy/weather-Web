#!/usr/bin/env python3
"""验证 GitHub Actions 对精确发布提交的通过证明。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 512 * 1024
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
BRANCH_PATTERN = re.compile(r"^(?:main|codex/[A-Za-z0-9._/-]+)$")
ALLOWED_WORKFLOWS = {
    ".github/workflows/ci.yml",
    ".github/workflows/cloudflare-edge.yml",
    ".github/workflows/miniprogram.yml",
}


class VerificationError(RuntimeError):
    """表示发布证明无法安全确认。"""


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{label} 缺失或格式异常")
    return value.strip()


def _validate_inputs(
    *,
    repo: str,
    workflow: str,
    commit: str,
    branch: str,
    proof_job: str,
) -> None:
    if not REPOSITORY_PATTERN.fullmatch(repo):
        raise VerificationError("GitHub 仓库格式异常")
    if workflow not in ALLOWED_WORKFLOWS:
        raise VerificationError("GitHub workflow 不在发布允许清单中")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise VerificationError("发布 commit 必须是 40 位小写十六进制 SHA")
    if not BRANCH_PATTERN.fullmatch(branch) or ".." in branch:
        raise VerificationError("发布分支只能是 main 或安全的 codex/* 分支")
    if not proof_job.strip() or len(proof_job) > 100:
        raise VerificationError("发布证明任务名格式异常")


def _api_json(
    path: str,
    *,
    token: str | None,
    timeout: float,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "case-weather-release-verifier",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{API_ROOT}{path}", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {403, 429}:
            raise VerificationError(
                "GitHub API 请求被拒绝或达到限流"
            ) from None
        raise VerificationError(
            f"GitHub API 返回 HTTP {exc.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise VerificationError("GitHub API 连接失败或超时") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise VerificationError("GitHub API 响应超过安全上限")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError("GitHub API 返回无效 JSON") from None
    if not isinstance(decoded, dict):
        raise VerificationError("GitHub API 返回结构异常")
    return decoded


def _repository_name(payload: Any, label: str) -> str:
    if not isinstance(payload, dict):
        raise VerificationError(f"{label} 缺失")
    return _require_text(payload.get("full_name"), f"{label}.full_name")


def _parse_github_time(value: Any, label: str) -> datetime:
    raw = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise VerificationError(f"{label} 时间格式异常") from None
    if parsed.tzinfo is None:
        raise VerificationError(f"{label} 缺少时区")
    return parsed.astimezone(timezone.utc)


def _select_latest_run(
    runs: list[Any],
    *,
    repo: str,
    workflow: str,
    commit: str,
    branch: str,
) -> dict[str, Any]:
    expected_path_with_ref = f"{workflow}@refs/heads/{branch}"
    matching: list[dict[str, Any]] = []
    for raw in runs:
        if not isinstance(raw, dict):
            raise VerificationError("workflow run 结构异常")
        if (
            raw.get("head_sha") == commit
            and raw.get("head_branch") == branch
            and raw.get("event") == "push"
        ):
            matching.append(raw)
    if not matching:
        raise VerificationError("找不到该提交的 push workflow run")

    def sort_key(run: dict[str, Any]) -> tuple[datetime, int, int]:
        created_at = _parse_github_time(
            run.get("created_at"),
            "workflow run created_at",
        )
        run_id = run.get("id")
        attempt = run.get("run_attempt")
        if not isinstance(run_id, int) or run_id <= 0:
            raise VerificationError("workflow run id 异常")
        if not isinstance(attempt, int) or attempt <= 0:
            raise VerificationError("workflow run attempt 异常")
        return created_at, run_id, attempt

    latest = max(matching, key=sort_key)
    if _repository_name(latest.get("repository"), "run repository") != repo:
        raise VerificationError("workflow run 仓库不匹配")
    if _repository_name(
        latest.get("head_repository"),
        "run head_repository",
    ) != repo:
        raise VerificationError("workflow run 来源仓库不匹配")
    if latest.get("path") not in {workflow, expected_path_with_ref}:
        raise VerificationError("workflow run 路径或 ref 不匹配")
    if latest.get("status") != "completed":
        raise VerificationError("最新 workflow run 尚未完成")
    if latest.get("conclusion") != "success":
        raise VerificationError("最新 workflow run 未成功")
    return latest


def _select_proof_job(
    jobs: list[Any],
    *,
    proof_job: str,
    run_id: int,
    commit: str,
) -> dict[str, Any]:
    matches = [
        job
        for job in jobs
        if isinstance(job, dict) and job.get("name") == proof_job
    ]
    if len(matches) != 1:
        raise VerificationError("稳定发布证明任务必须恰好出现一次")
    job = matches[0]
    if job.get("run_id") != run_id:
        raise VerificationError("发布证明任务 run_id 不匹配")
    if job.get("head_sha") != commit:
        raise VerificationError("发布证明任务 commit 不匹配")
    if job.get("status") != "completed":
        raise VerificationError("发布证明任务尚未完成")
    if job.get("conclusion") != "success":
        raise VerificationError("发布证明任务未成功")
    job_id = job.get("id")
    if not isinstance(job_id, int) or job_id <= 0:
        raise VerificationError("发布证明任务 id 异常")
    return job


def _run_receipt(run: dict[str, Any], *, repo: str) -> dict[str, Any]:
    return {
        "id": run["id"],
        "attempt": run["run_attempt"],
        "path": run["path"],
        "head_branch": run["head_branch"],
        "head_sha": run["head_sha"],
        "event": run["event"],
        "status": run["status"],
        "conclusion": run["conclusion"],
        "repository": repo,
        "head_repository": repo,
        "created_at": _require_text(
            run.get("created_at"),
            "run created_at",
        ),
        "updated_at": _require_text(
            run.get("updated_at"),
            "run updated_at",
        ),
        "html_url": _require_text(run.get("html_url"), "run html_url"),
    }


def _job_receipt(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "name": job["name"],
        "run_id": job["run_id"],
        "head_sha": job["head_sha"],
        "status": job["status"],
        "conclusion": job["conclusion"],
        "started_at": _require_text(
            job.get("started_at"),
            "job started_at",
        ),
        "completed_at": _require_text(
            job.get("completed_at"),
            "job completed_at",
        ),
        "html_url": _require_text(job.get("html_url"), "job html_url"),
    }


def verify_online(
    *,
    repo: str,
    workflow: str,
    commit: str,
    branch: str,
    proof_job: str,
    token: str | None,
    timeout: float,
) -> dict[str, Any]:
    _validate_inputs(
        repo=repo,
        workflow=workflow,
        commit=commit,
        branch=branch,
        proof_job=proof_job,
    )
    branch_path = quote(branch, safe="")
    branch_payload = _api_json(
        f"/repos/{repo}/branches/{branch_path}",
        token=token,
        timeout=timeout,
    )
    branch_commit = branch_payload.get("commit")
    if not isinstance(branch_commit, dict) or branch_commit.get("sha") != commit:
        raise VerificationError(
            "GitHub 发布分支 tip 已变化或与本次 commit 不一致"
        )

    query = urlencode(
        {
            "branch": branch,
            "event": "push",
            "head_sha": commit,
            "per_page": "100",
        }
    )
    workflow_id = quote(workflow, safe="")
    runs_payload = _api_json(
        f"/repos/{repo}/actions/workflows/{workflow_id}/runs?{query}",
        token=token,
        timeout=timeout,
    )
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise VerificationError("workflow runs 响应缺少列表")
    total_count = runs_payload.get("total_count")
    if not isinstance(total_count, int) or total_count < 0:
        raise VerificationError("workflow runs total_count 异常")
    if total_count > 100:
        raise VerificationError("该提交 workflow runs 超过单页安全上限")
    run = _select_latest_run(
        runs,
        repo=repo,
        workflow=workflow,
        commit=commit,
        branch=branch,
    )

    jobs_payload = _api_json(
        (
            f"/repos/{repo}/actions/runs/{run['id']}/attempts/"
            f"{run['run_attempt']}/jobs?per_page=100"
        ),
        token=token,
        timeout=timeout,
    )
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise VerificationError("workflow jobs 响应缺少列表")
    jobs_total = jobs_payload.get("total_count")
    if not isinstance(jobs_total, int) or jobs_total < 0:
        raise VerificationError("workflow jobs total_count 异常")
    if jobs_total > 100:
        raise VerificationError("workflow jobs 超过单页安全上限")
    proof = _select_proof_job(
        jobs,
        proof_job=proof_job,
        run_id=run["id"],
        commit=commit,
    )
    return {
        "schema_version": 1,
        "kind": "github-actions-ci-proof",
        "repository": repo,
        "workflow": workflow,
        "branch": branch,
        "commit_sha": commit,
        "event": "push",
        "proof_job": proof_job,
        "verified_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "run": _run_receipt(run, repo=repo),
        "job": _job_receipt(proof),
    }


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    if path.is_symlink():
        raise VerificationError("CI 收据目标不得为符号链接")
    parent = path.parent
    if not parent.is_dir():
        raise VerificationError("CI 收据父目录不存在")
    payload = (
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_receipt(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise VerificationError("CI 收据不得为符号链接")
    try:
        metadata = path.stat()
    except OSError:
        raise VerificationError("CI 收据不存在或不可读") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise VerificationError("CI 收据必须是普通文件")
    if metadata.st_size <= 0 or metadata.st_size > MAX_RECEIPT_BYTES:
        raise VerificationError("CI 收据大小异常")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise VerificationError("CI 收据权限必须精确为 0600")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError("CI 收据 JSON 无效") from None
    if not isinstance(decoded, dict):
        raise VerificationError("CI 收据结构异常")
    return decoded


def verify_receipt(
    receipt: dict[str, Any],
    *,
    repo: str,
    workflow: str,
    commit: str,
    branch: str,
    proof_job: str,
) -> None:
    _validate_inputs(
        repo=repo,
        workflow=workflow,
        commit=commit,
        branch=branch,
        proof_job=proof_job,
    )
    expected = {
        "schema_version": 1,
        "kind": "github-actions-ci-proof",
        "repository": repo,
        "workflow": workflow,
        "branch": branch,
        "commit_sha": commit,
        "event": "push",
        "proof_job": proof_job,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise VerificationError(f"CI 收据字段不匹配: {key}")
    _parse_github_time(receipt.get("verified_at"), "receipt verified_at")
    run = receipt.get("run")
    job = receipt.get("job")
    if not isinstance(run, dict) or not isinstance(job, dict):
        raise VerificationError("CI 收据缺少 run 或 job")
    expected_path_with_ref = f"{workflow}@refs/heads/{branch}"
    if run.get("path") not in {workflow, expected_path_with_ref}:
        raise VerificationError("CI 收据 workflow 路径或 ref 不匹配")
    run_id = run.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise VerificationError("CI 收据 run id 异常")
    if not isinstance(run.get("attempt"), int) or run["attempt"] <= 0:
        raise VerificationError("CI 收据 run attempt 异常")
    run_expectations = {
        "head_branch": branch,
        "head_sha": commit,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": repo,
        "head_repository": repo,
    }
    for key, value in run_expectations.items():
        if run.get(key) != value:
            raise VerificationError(f"CI 收据 run 字段不匹配: {key}")
    _parse_github_time(run.get("created_at"), "receipt run created_at")
    _parse_github_time(run.get("updated_at"), "receipt run updated_at")
    _require_text(run.get("html_url"), "receipt run html_url")
    job_expectations = {
        "name": proof_job,
        "run_id": run_id,
        "head_sha": commit,
        "status": "completed",
        "conclusion": "success",
    }
    for key, value in job_expectations.items():
        if job.get(key) != value:
            raise VerificationError(f"CI 收据 job 字段不匹配: {key}")
    if not isinstance(job.get("id"), int) or job["id"] <= 0:
        raise VerificationError("CI 收据 job id 异常")
    _parse_github_time(job.get("started_at"), "receipt job started_at")
    _parse_github_time(job.get("completed_at"), "receipt job completed_at")
    _require_text(job.get("html_url"), "receipt job html_url")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证 GitHub Actions 精确提交发布证明",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_binding_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--repo", required=True)
        target.add_argument("--workflow", required=True)
        target.add_argument("--commit", required=True)
        target.add_argument("--branch", required=True)
        target.add_argument("--proof-job", required=True)

    online = subparsers.add_parser("verify-online")
    add_binding_arguments(online)
    online.add_argument("--output", type=Path, required=True)
    online.add_argument("--token-env", default="GITHUB_TOKEN")
    online.add_argument("--timeout", type=float, default=15.0)

    offline = subparsers.add_parser("verify-receipt")
    add_binding_arguments(offline)
    offline.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-online":
            if not 1 <= args.timeout <= 30:
                raise VerificationError("GitHub API timeout 必须为 1 至 30 秒")
            token = os.environ.get(args.token_env, "").strip() or None
            receipt = verify_online(
                repo=args.repo,
                workflow=args.workflow,
                commit=args.commit,
                branch=args.branch,
                proof_job=args.proof_job,
                token=token,
                timeout=args.timeout,
            )
            _write_receipt(args.output, receipt)
            print(
                f"GitHub CI 发布证明通过: {args.workflow} "
                f"{args.commit[:12]}"
            )
            return 0
        receipt = _read_receipt(args.receipt)
        verify_receipt(
            receipt,
            repo=args.repo,
            workflow=args.workflow,
            commit=args.commit,
            branch=args.branch,
            proof_job=args.proof_job,
        )
        print(
            f"GitHub CI 收据绑定通过: {args.workflow} "
            f"{args.commit[:12]}"
        )
        return 0
    except VerificationError as exc:
        print(f"GitHub CI 发布证明失败: {exc}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
