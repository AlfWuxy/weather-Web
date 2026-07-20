# -*- coding: utf-8 -*-
"""轻量微信发布冻结器的状态收敛与安全门禁测试。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from scripts import finalize_wechat_release as finalizer
from scripts import wechat_release_contract as contract


ROOT = Path(__file__).resolve().parents[1]
FORM_NAME = ".env.wechat-release"
EFFECTIVE_DATE = "2026-08-01"
PRIVACY_VERSION = "privacy-2026.08.01"
PRIVATE_SENTINEL = "PRIVATE_VALUE_MUST_NOT_LEAK"


def test_commit_rule_matches_current_sha1_repository():
    assert finalizer.COMMIT_RE.fullmatch("a" * 40)
    assert finalizer.COMMIT_RE.fullmatch("a" * 64) is None


def _git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _only_marker(pattern, contents: dict[str, bytes], label: str) -> str:
    values = {
        match
        for path, content in contents.items()
        if path != contract.CONFIG_PATH
        for match in pattern.findall(content.decode("utf-8"))
    }
    assert len(values) == 1, f"正式 HEAD 的{label} marker 不一致"
    return values.pop()


def _restore_head_candidate(contents: dict[str, bytes]) -> dict[str, bytes]:
    final_states = [contract.has_final_marker(contents[path]) for path in contract.CONTENT_PATHS[:-1]]
    if not any(final_states):
        return contents
    assert all(final_states), "HEAD 发布材料不能混合候选与正式状态"
    fields = contract.PublicReleaseFields(
        name=_only_marker(contract.NAME_RE, contents, "平台名称"),
        service_name=_only_marker(contract.SERVICE_NAME_RE, contents, "服务名称"),
        effective_date=_only_marker(contract.DATE_RE, contents, "生效日期"),
        privacy_version=_only_marker(contract.PRIVACY_RE, contents, "隐私版本"),
        release_version=contract.EXPECTED_RELEASE_VERSION,
    )
    contract.verify_final(contents, fields)
    return contract.restore_candidate(contents, fields)


@lru_cache(maxsize=1)
def _candidate_fixture() -> dict[str, bytes]:
    head = {path: _git(ROOT, "show", f"HEAD:{path}") for path in contract.CONTENT_PATHS}
    return _restore_head_candidate(head)


def _candidate(path: str) -> bytes:
    return _candidate_fixture()[path]


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in contract.CONTENT_PATHS:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_candidate(relative))
        target.chmod(0o644)
    (repo / ".gitignore").write_text(".env.*\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release@example.invalid")
    _git(repo, "add", ".gitignore", *contract.CONTENT_PATHS)
    _git(repo, "commit", "-q", "-m", "候选基线")
    return repo


def _form_lines(**overrides: str) -> list[str]:
    values = {
        "WECHAT_MINIPROGRAM_NAME": contract.EXPECTED_PLATFORM_NAME,
        "WECHAT_SERVICE_NAME": contract.EXPECTED_SERVICE_NAME,
        "WECHAT_EFFECTIVE_DATE": EFFECTIVE_DATE,
        "WX_MINIPROGRAM_PRIVACY_VERSION": PRIVACY_VERSION,
        "WECHAT_RELEASE_VERSION": contract.EXPECTED_RELEASE_VERSION,
        "WECHAT_FORM_READY": "0",
        "WECHAT_CATEGORY_CONFIRMED": "0",
        "WECHAT_TARGET_COMMIT_SHA": "",
        "WECHAT_PRIVACY_DOC_SHA256": "",
        "WECHAT_AGREEMENT_DOC_SHA256": "",
        "WECHAT_LISTING_COPY_SHA256": "",
        "WECHAT_PRIVACY_PAGE_SHA256": "",
        "WECHAT_AGREEMENT_PAGE_SHA256": "",
        "WECHAT_HEALTH_CONSENT_PAGE_SHA256": "",
        "WECHAT_OPERATOR_NAME": PRIVATE_SENTINEL,
        "WECHAT_CONTACT_EMAIL": f"{PRIVATE_SENTINEL}@example.invalid",
        "WX_MINIPROGRAM_SECRET": PRIVATE_SENTINEL,
        "QWEATHER_EXPECTED_KID": PRIVATE_SENTINEL,
    }
    values.update(overrides)
    return ["# 私密发布确认单", *(f"{key}={value}" for key, value in values.items())]


def _write_form(repo: Path, **overrides: str) -> Path:
    path = repo / FORM_NAME
    path.write_text("\n".join(_form_lines(**overrides)) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _fields() -> contract.PublicReleaseFields:
    return contract.PublicReleaseFields(
        name=contract.EXPECTED_PLATFORM_NAME,
        service_name=contract.EXPECTED_SERVICE_NAME,
        effective_date=EFFECTIVE_DATE,
        privacy_version=PRIVACY_VERSION,
        release_version=contract.EXPECTED_RELEASE_VERSION,
    )


def _candidate_map(repo: Path) -> dict[str, bytes]:
    return {path: (repo / path).read_bytes() for path in contract.CONTENT_PATHS}


def _expected(repo: Path) -> dict[str, bytes]:
    return contract.render_final(_candidate_map(repo), _fields())


def _snapshot(repo: Path) -> dict[str, bytes]:
    return {path: (repo / path).read_bytes() for path in contract.CONTENT_PATHS}


def _parse_env(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def _finalize_commit(repo: Path, form: Path) -> str:
    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is True
    _git(repo, "add", *contract.CONTENT_PATHS)
    _git(repo, "commit", "-q", "-m", "正式冻结")
    return _git(repo, "rev-parse", "HEAD").decode("ascii").strip()


def test_candidate_fixture_recovers_reviewed_baseline_from_final_head():
    candidate = dict(_candidate_fixture())
    final = contract.render_final(candidate, _fields())
    assert _restore_head_candidate(final) == candidate


def test_privacy_page_final_contact_copy_is_current_and_reversible():
    candidate = dict(_candidate_fixture())
    final = contract.render_final(candidate, _fields())
    privacy = final[contract.PRIVACY_PAGE_PATH].decode("utf-8")
    assert "以微信公众平台隐私保护指引展示的认证信息为准" in privacy
    assert "会在正式提交前同步" not in privacy
    assert contract.restore_candidate(final, _fields()) == candidate


def test_finalized_privacy_materials_preserve_exact_data_lifecycle_disclosures():
    candidate = dict(_candidate_fixture())
    final = contract.render_final(candidate, _fields())

    for path in (contract.PRIVACY_DOC_PATH, contract.PRIVACY_PAGE_PATH):
        text = final[path].decode("utf-8")
        assert re.search(
            r"wechat_login_success.*direct.*family_share.*内部账号 ID.*事件时间.*30 天",
            text,
            re.S,
        )
        assert re.search(
            r"内部账号 ID.*随机内部账号名.*随机密码.*哈希.*一般隐私同意版本.*最近登录时间.*保存至账号注销",
            text,
            re.S,
        )
        assert re.search(
            r"会话 token.*不可逆哈希.*创建时间.*到期时间.*最近使用时间.*撤销时间.*7 天.*30 天",
            text,
            re.S,
        )
        assert re.search(
            r"老人码.*短码.*不可逆哈希.*短码到期时间.*关系状态.*(?:保存至账号注销|随账号保存至注销)",
            text,
            re.S,
        )
        assert re.search(r"行动确认.*日期.*完成状态.*实际所选完成项", text, re.S)
        assert re.search(
            r"停止管理家人.*软停用.*不会删除家人档案.*历史记录",
            text,
            re.S,
        )
        assert re.search(
            r"瞬时读取客户端 IP.*明文 IP 不写入产品分析事件或应用审计表",
            text,
            re.S,
        )
        assert re.search(
            r"个性化提醒.*系统剪贴板.*可能包含家人称呼.*不读取系统剪贴板",
            text,
            re.S,
        )

    privacy_page = final[contract.PRIVACY_PAGE_PATH].decode("utf-8")
    assert not re.search(r"safe-note[^>]*>[^<]*昵称", privacy_page)
    health_consent = final[contract.HEALTH_CONSENT_PAGE_PATH].decode("utf-8")
    assert "当前只有用药记录支持逐条删除" in health_consent
    assert "主动删除相关记录" not in health_consent
    assert contract.restore_candidate(final, _fields()) == candidate


def test_dual_name_contract_has_exact_markers_visible_relation_and_roundtrip():
    candidate = dict(_candidate_fixture())
    assert {
        path: hashlib.sha256(content).hexdigest()
        for path, content in candidate.items()
    } == contract.CANDIDATE_SHA256

    fields = _fields()
    final = contract.render_final(candidate, fields)
    marker_total = 0
    for path in contract.CONTENT_PATHS[:-1]:
        text = final[path].decode("utf-8")
        visible = contract._visible(text)
        assert contract.NAME_RE.findall(text) == [contract.EXPECTED_PLATFORM_NAME]
        assert contract.SERVICE_NAME_RE.findall(text) == [contract.EXPECTED_SERVICE_NAME]
        assert visible.count(fields.visible_brand_relation) == 1
        marker_total += text.count("<!-- WECHAT_")
    assert marker_total == 26

    restored = contract.restore_candidate(final, fields)
    assert restored == candidate
    assert contract.render_final(restored, fields) == final


def test_swapped_platform_and_service_names_fail_closed():
    fields = contract.PublicReleaseFields(
        name=contract.EXPECTED_SERVICE_NAME,
        service_name=contract.EXPECTED_PLATFORM_NAME,
        effective_date=EFFECTIVE_DATE,
        privacy_version=PRIVACY_VERSION,
        release_version=contract.EXPECTED_RELEASE_VERSION,
    )
    with pytest.raises(contract.ReleaseContractError, match="小程序名称"):
        contract.validate_public_fields(
            fields,
            form_ready="0",
            category_confirmed="0",
        )


@pytest.mark.parametrize("drift", ("missing_service", "duplicate_service", "swapped_names"))
def test_verify_final_rejects_dual_name_marker_drift(drift):
    fields = _fields()
    final = contract.render_final(dict(_candidate_fixture()), fields)
    target = contract.LISTING_COPY_PATH
    text = final[target].decode("utf-8")
    platform_marker = f"<!-- WECHAT_MINIPROGRAM_NAME: {fields.name} -->"
    service_marker = f"<!-- WECHAT_SERVICE_NAME: {fields.service_name} -->"
    if drift == "missing_service":
        text = text.replace(f"{service_marker}\n", "", 1)
    elif drift == "duplicate_service":
        text = text.replace(service_marker, f"{service_marker}\n{service_marker}", 1)
    else:
        text = text.replace(
            f"{platform_marker}\n{service_marker}",
            (
                f"<!-- WECHAT_MINIPROGRAM_NAME: {fields.service_name} -->\n"
                f"<!-- WECHAT_SERVICE_NAME: {fields.name} -->"
            ),
            1,
        )
    final[target] = text.encode("utf-8")

    with pytest.raises(contract.ReleaseContractError, match="名称 marker"):
        contract.verify_final(final, fields)


def test_verify_final_rejects_hidden_marker_only_brand_relation():
    fields = _fields()
    final = contract.render_final(dict(_candidate_fixture()), fields)
    target = contract.PRIVACY_PAGE_PATH
    text = final[target].decode("utf-8").replace(
        fields.visible_brand_relation,
        f"{fields.name} · {fields.service_name}",
        1,
    )
    final[target] = text.encode("utf-8")

    with pytest.raises(contract.ReleaseContractError, match="可见双名称关系"):
        contract.verify_final(final, fields)


def test_finalize_candidate_matches_contract_and_is_idempotent(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    expected = _expected(repo)

    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is True
    assert _snapshot(repo) == expected
    combined = "".join(expected[path].decode("utf-8") for path in contract.CONTENT_PATHS[:-1])
    assert combined.count("候选") == 0
    assert combined.count("<!-- WECHAT_") == 26
    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is False
    assert _snapshot(repo) == expected


@pytest.mark.parametrize("completed", range(8))
def test_finalize_known_partial_states_converge(tmp_path, completed):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    expected = _expected(repo)
    for path in contract.CONTENT_PATHS[:completed]:
        (repo / path).write_bytes(expected[path])

    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is (completed < 7)
    assert _snapshot(repo) == expected


def test_finalize_resumes_interrupted_dual_name_write(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    expected = _expected(repo)
    for path in contract.CONTENT_PATHS[:3]:
        (repo / path).write_bytes(expected[path])

    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is True
    final = _snapshot(repo)
    assert final == expected
    for path in contract.CONTENT_PATHS[:-1]:
        text = final[path].decode("utf-8")
        assert contract.SERVICE_NAME_RE.findall(text) == [contract.EXPECTED_SERVICE_NAME]


@pytest.mark.parametrize("target", contract.CONTENT_PATHS)
def test_finalize_unknown_target_bytes_block_before_other_writes(tmp_path, target):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    changed = repo / target
    changed.write_bytes(changed.read_bytes() + b"\nUNKNOWN_STATE\n")
    before = _snapshot(repo)

    with pytest.raises(finalizer.ReleaseFinalizeError, match="未知字节"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    assert _snapshot(repo) == before


@pytest.mark.parametrize("target", contract.CONTENT_PATHS)
def test_finalize_committed_candidate_sha_drift_blocks(tmp_path, target):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    changed = repo / target
    changed.write_bytes(changed.read_bytes() + b"\n")
    _git(repo, "add", target)
    _git(repo, "commit", "-q", "-m", "未复核候选漂移")
    before = _snapshot(repo)

    with pytest.raises(finalizer.ReleaseFinalizeError, match="未知|局部冻结"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    assert _snapshot(repo) == before


def test_finalize_marker_preserving_final_head_drift_blocks(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    _finalize_commit(repo, form)
    target = repo / contract.LISTING_COPY_PATH
    target.write_bytes(target.read_bytes() + b"\n")
    _git(repo, "add", contract.LISTING_COPY_PATH)
    _git(repo, "commit", "-q", "-m", "未复核正式漂移")

    with pytest.raises(finalizer.ReleaseFinalizeError, match="未知|局部冻结"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)


def test_finalize_staged_state_hidden_by_candidate_worktree_blocks(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    target = contract.PRIVACY_DOC_PATH
    expected = _expected(repo)[target]
    (repo / target).write_bytes(expected)
    _git(repo, "add", target)
    (repo / target).write_bytes(_candidate(target))
    before = _snapshot(repo)

    with pytest.raises(finalizer.ReleaseFinalizeError, match="暂存区"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    assert _snapshot(repo) == before


@pytest.mark.parametrize("kind", ("tracked", "untracked"))
def test_finalize_unrelated_worktree_change_blocks(tmp_path, kind):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    if kind == "tracked":
        (repo / ".gitignore").write_text(".env.*\n# changed\n", encoding="utf-8")
    else:
        (repo / "untracked.txt").write_text("change", encoding="utf-8")
    before = _snapshot(repo)

    with pytest.raises(finalizer.ReleaseFinalizeError, match="以外"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    assert _snapshot(repo) == before


@pytest.mark.parametrize(
    "overrides",
    (
        {"WECHAT_MINIPROGRAM_NAME": "其他名称"},
        {"WECHAT_SERVICE_NAME": "其他服务"},
        {
            "WECHAT_MINIPROGRAM_NAME": contract.EXPECTED_SERVICE_NAME,
            "WECHAT_SERVICE_NAME": contract.EXPECTED_PLATFORM_NAME,
        },
        {"WECHAT_EFFECTIVE_DATE": "2026-02-30"},
        {"WX_MINIPROGRAM_PRIVACY_VERSION": "bad version"},
        {"WECHAT_RELEASE_VERSION": "1.0.1"},
        {"WECHAT_FORM_READY": "1"},
        {"WECHAT_CATEGORY_CONFIRMED": "1"},
    ),
)
def test_finalize_invalid_public_form_fields_block_without_writes(tmp_path, overrides):
    repo = _init_repo(tmp_path)
    form = _write_form(repo, **overrides)
    before = _snapshot(repo)

    with pytest.raises(finalizer.ReleaseFinalizeError, match="公开字段"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    assert _snapshot(repo) == before


def test_finalize_accepts_one_dotenv_quote_layer(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(
        repo,
        WECHAT_MINIPROGRAM_NAME=f'"{contract.EXPECTED_PLATFORM_NAME}"',
        WECHAT_SERVICE_NAME=f"'{contract.EXPECTED_SERVICE_NAME}'",
        WECHAT_EFFECTIVE_DATE=f"'{EFFECTIVE_DATE}'",
        WX_MINIPROGRAM_PRIVACY_VERSION=f'"{PRIVACY_VERSION}"',
        WECHAT_RELEASE_VERSION="'1.0.0'",
        WECHAT_FORM_READY="'0'",
        WECHAT_CATEGORY_CONFIRMED='"0"',
    )
    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is True


@pytest.mark.parametrize("problem", ("duplicate", "mode", "symlink", "not_ignored", "orphan"))
def test_finalize_private_form_safety_gates(tmp_path, problem):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    if problem == "duplicate":
        with form.open("a", encoding="utf-8") as target:
            target.write(f"WECHAT_EFFECTIVE_DATE={EFFECTIVE_DATE}\n")
    elif problem == "mode":
        form.chmod(0o644)
    elif problem == "symlink":
        real = repo / ".env.real"
        os.replace(form, real)
        form.symlink_to(real)
    elif problem == "not_ignored":
        (repo / ".gitignore").write_text("other\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "不再忽略")
    else:
        (repo / f"{FORM_NAME}.tmp.orphan").write_text(PRIVATE_SENTINEL, encoding="utf-8")

    with pytest.raises(finalizer.ReleaseFinalizeError):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)


def test_finalize_requires_service_name_form_field_without_writes(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    lines = [
        line
        for line in form.read_text(encoding="utf-8").splitlines()
        if not line.startswith("WECHAT_SERVICE_NAME=")
    ]
    form.write_text("\n".join(lines) + "\n", encoding="utf-8")
    form.chmod(0o600)
    before = _snapshot(repo)

    with pytest.raises(finalizer.ReleaseFinalizeError, match="缺少必填字段"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    assert _snapshot(repo) == before


@pytest.mark.parametrize("kind", ("content", "form"))
def test_finalize_safe_crash_temp_residue_is_cleaned_and_converges(tmp_path, kind):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    if kind == "content":
        target = repo / contract.PRIVACY_PAGE_PATH
        temporary = target.parent / f".{target.name}.finalize.crash"
    else:
        temporary = repo / f"{FORM_NAME}.tmp.crash"
    temporary.write_bytes(b"interrupted")
    temporary.chmod(0o600)

    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is True
    assert not temporary.exists()


def test_finalize_partial_or_unknown_head_blocks(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    expected = _expected(repo)
    (repo / contract.PRIVACY_DOC_PATH).write_bytes(expected[contract.PRIVACY_DOC_PATH])
    _git(repo, "add", contract.PRIVACY_DOC_PATH)
    _git(repo, "commit", "-q", "-m", "错误提交局部正式内容")

    with pytest.raises(finalizer.ReleaseFinalizeError, match="局部冻结"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)


def test_finalize_final_head_clean_is_idempotent(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    _finalize_commit(repo, form)
    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is False


@pytest.mark.parametrize("change", ("form", "head"))
def test_finalize_detects_cooperative_context_change_and_can_resume(tmp_path, monkeypatch, change):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    real_write = finalizer._atomic_write
    calls = 0

    def write_then_change(path, content):
        nonlocal calls
        real_write(path, content)
        calls += 1
        if calls == 1:
            if change == "form":
                form.write_text(form.read_text(encoding="utf-8").replace(PRIVATE_SENTINEL, "NEW_PRIVATE"), encoding="utf-8")
                form.chmod(0o600)
            else:
                _git(repo, "commit", "--allow-empty", "-q", "-m", "并发 HEAD")

    monkeypatch.setattr(finalizer, "_atomic_write", write_then_change)
    with pytest.raises(finalizer.ReleaseFinalizeError, match="发生变化"):
        finalizer.finalize_content(repo_root=repo, wechat_form=form)
    monkeypatch.setattr(finalizer, "_atomic_write", real_write)
    assert finalizer.finalize_content(repo_root=repo, wechat_form=form) is True


def test_cli_never_echoes_private_form_values(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    form = _write_form(repo, WECHAT_FORM_READY="1")
    assert finalizer.main(["finalize-content", "--repo-root", str(repo), "--wechat-form", str(form)]) == 2
    output = capsys.readouterr()
    assert PRIVATE_SENTINEL not in output.out + output.err


def test_record_freeze_updates_exact_fields_from_head_and_preserves_private_bytes(tmp_path):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    head = _finalize_commit(repo, form)
    before = form.read_bytes()

    assert finalizer.record_freeze(repo_root=repo, wechat_form=form) is True
    values = _parse_env(form)
    assert values["WECHAT_TARGET_COMMIT_SHA"] == head
    for key, path in contract.RELEASE_ARTIFACTS:
        blob = _git(repo, "cat-file", "blob", f"{head}:{path}")
        assert values[key] == hashlib.sha256(blob).hexdigest()
    assert values["WECHAT_OPERATOR_NAME"] == PRIVATE_SENTINEL
    assert values["WX_MINIPROGRAM_SECRET"] == PRIVATE_SENTINEL
    assert form.read_bytes().startswith("# 私密发布确认单\n".encode("utf-8"))
    assert stat.S_IMODE(form.stat().st_mode) == 0o600
    assert finalizer.record_freeze(repo_root=repo, wechat_form=form) is False
    assert len(form.read_bytes()) >= len(before)


@pytest.mark.parametrize("state", ("candidate", "tracked", "untracked"))
def test_record_freeze_requires_clean_final_head(tmp_path, state):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    if state != "candidate":
        _finalize_commit(repo, form)
        if state == "tracked":
            config = repo / contract.CONFIG_PATH
            config.write_bytes(config.read_bytes() + b"\n")
        else:
            (repo / "extra.txt").write_text("x", encoding="utf-8")
    original = form.read_bytes()

    with pytest.raises(finalizer.ReleaseFinalizeError):
        finalizer.record_freeze(repo_root=repo, wechat_form=form)
    assert form.read_bytes() == original


@pytest.mark.parametrize("problem", ("missing", "duplicate"))
def test_record_freeze_requires_eight_unique_existing_fields(tmp_path, problem):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    _finalize_commit(repo, form)
    lines = form.read_text(encoding="utf-8").splitlines()
    key = "WECHAT_TARGET_COMMIT_SHA"
    if problem == "missing":
        lines = [line for line in lines if not line.startswith(f"{key}=")]
    else:
        lines.append(f"{key}=")
    form.write_text("\n".join(lines) + "\n", encoding="utf-8")
    form.chmod(0o600)
    original = form.read_bytes()

    with pytest.raises(finalizer.ReleaseFinalizeError, match="冻结失败"):
        finalizer.record_freeze(repo_root=repo, wechat_form=form)
    assert form.read_bytes() == original


def test_record_freeze_detects_form_change_without_losing_new_private_value(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    _finalize_commit(repo, form)
    real_update = finalizer.env_updater.update_env_values

    def change_then_update(path, updates, **kwargs):
        path.write_text(path.read_text(encoding="utf-8").replace(PRIVATE_SENTINEL, "CONCURRENT_PRIVATE"), encoding="utf-8")
        path.chmod(0o600)
        return real_update(path, updates, **kwargs)

    monkeypatch.setattr(finalizer.env_updater, "update_env_values", change_then_update)
    with pytest.raises(finalizer.ReleaseFinalizeError):
        finalizer.record_freeze(repo_root=repo, wechat_form=form)
    assert "CONCURRENT_PRIVATE" in form.read_text(encoding="utf-8")


def test_record_freeze_head_change_after_replace_returns_safe_error(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    form = _write_form(repo)
    target_head = _finalize_commit(repo, form)
    real_update = finalizer.env_updater.update_env_values

    def update_then_commit(path, updates, **kwargs):
        changed = real_update(path, updates, **kwargs)
        _git(repo, "commit", "--allow-empty", "-q", "-m", "替换后 HEAD 变化")
        return changed

    monkeypatch.setattr(finalizer.env_updater, "update_env_values", update_then_commit)
    with pytest.raises(finalizer.ReleaseFinalizeError, match="表单替换后"):
        finalizer.record_freeze(repo_root=repo, wechat_form=form)
    assert _parse_env(form)["WECHAT_TARGET_COMMIT_SHA"] == target_head
