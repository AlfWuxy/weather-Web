from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MINIPROGRAM_WORKFLOW = ROOT / ".github" / "workflows" / "miniprogram.yml"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_SHA = "ece7cb06caefa5fff74198d8649806c4678c61a1"
SETUP_NODE_SHA = "820762786026740c76f36085b0efc47a31fe5020"
LOCKED_INSTALL = (
    "python -m pip install --index-url https://pypi.org/simple "
    "--require-hashes --only-binary=:all: -r requirements.lock"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _push_block(workflow: str) -> str:
    """提取 push 到 permissions 之间的触发配置。"""
    return workflow.split("  push:\n", 1)[1].split("\npermissions:", 1)[0]


def test_ci_push_covers_release_branches() -> None:
    workflow = _read(CI_WORKFLOW)
    push = _push_block(workflow)

    assert "      - main\n" in push
    assert '      - "codex/**"\n' in push


def test_ci_uses_pinned_actions_and_locked_dependencies() -> None:
    workflow = _read(CI_WORKFLOW)
    test_job = workflow.split("  test:\n", 1)[1].split("\n  activation:\n", 1)[0]
    activation_job = workflow.split("  activation:\n", 1)[1].split(
        "\n  web-js:\n", 1
    )[0]
    web_js_job = workflow.split("  web-js:\n", 1)[1].split(
        "\n  release-proof:\n", 1
    )[0]

    for job in (test_job, activation_job, web_js_job):
        assert f"uses: actions/checkout@{CHECKOUT_SHA}" in job
    for job in (test_job, activation_job):
        assert f"uses: actions/setup-python@{SETUP_PYTHON_SHA}" in job
        assert LOCKED_INSTALL in job
    assert f"uses: actions/setup-node@{SETUP_NODE_SHA}" in web_js_job
    assert "node --test tests/*.test.js tests/js/*.test.mjs" in web_js_job
    assert "@v6" not in workflow
    assert "@v7" not in workflow
    assert "pip install --upgrade pip" not in workflow
    assert "pip install -r requirements.txt" not in workflow


def test_ci_covers_python_311_312_and_activation_uses_311() -> None:
    workflow = _read(CI_WORKFLOW)
    test_job = workflow.split("  test:\n", 1)[1].split("\n  activation:\n", 1)[0]
    activation_job = workflow.split("  activation:\n", 1)[1].split(
        "\n  web-js:\n", 1
    )[0]

    assert 'python-version: ["3.11", "3.12"]' in test_job
    assert "python-version: ${{ matrix.python-version }}" in test_job
    assert 'python-version: "3.11"' in activation_job
    assert "matrix.python-version" not in activation_job


def test_ci_release_proof_is_push_only_and_depends_on_all_test_groups() -> None:
    workflow = _read(CI_WORKFLOW)
    proof = workflow.split("  release-proof:\n", 1)[1]

    assert "name: 可发布提交证明" in proof
    assert "      - test\n" in proof
    assert "      - activation\n" in proof
    assert "      - web-js\n" in proof
    assert "github.event_name == 'push'" in proof
    assert "needs.test.result == 'success'" in proof
    assert "needs.activation.result == 'success'" in proof
    assert "needs.web-js.result == 'success'" in proof


def test_miniprogram_push_has_no_path_filter_and_covers_release_branches() -> None:
    workflow = _read(MINIPROGRAM_WORKFLOW)
    push = _push_block(workflow)

    assert "      - main\n" in push
    assert '      - "codex/**"\n' in push
    assert "paths:" not in push


def test_miniprogram_keeps_pr_paths_and_has_stable_proof_name() -> None:
    workflow = _read(MINIPROGRAM_WORKFLOW)
    pull_request = workflow.split("  pull_request:\n", 1)[1].split(
        "\n  push:\n", 1
    )[0]

    assert "    paths:\n" in pull_request
    assert '      - "miniprogram/**"\n' in pull_request
    assert "name: 小程序可发布提交证明" in workflow


def test_miniprogram_actions_are_pinned() -> None:
    workflow = _read(MINIPROGRAM_WORKFLOW)

    assert f"uses: actions/checkout@{CHECKOUT_SHA}" in workflow
    assert f"uses: actions/setup-node@{SETUP_NODE_SHA}" in workflow
    assert "@v7" not in workflow
