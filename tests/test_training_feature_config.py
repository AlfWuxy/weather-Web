# -*- coding: utf-8 -*-
"""训练脚本特征配置与运行制品清单回归测试。"""

import hashlib
import json
import os
from pathlib import Path
import stat

import pytest
import sklearn

from services.pipelines import feature_config_writer


ROOT = Path(__file__).resolve().parents[1]
TRAINING_SCRIPTS = (
    "train_optimized_model.py",
    "train_binary_model.py",
    "train_xgboost_model.py",
    "train_multiclass_model.py",
    "train_real_model.py",
)
ARTIFACT_CONTENT = {
    "disease_predictor.pkl": b"new-disease-model",
    "scaler.pkl": b"new-scaler",
    "label_encoder.pkl": b"new-label-encoder",
}


def _write_artifacts(directory: Path) -> None:
    for filename, content in ARTIFACT_CONTENT.items():
        (directory / filename).write_bytes(content)


def _expected_files() -> dict[str, dict[str, str | int]]:
    return {
        filename: {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        for filename, content in ARTIFACT_CONTENT.items()
    }


def test_write_feature_config_builds_complete_exact_runtime_artifacts(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)
    new_training_config = {
        "feature_cols": ["年龄数值", "月份"],
        "accuracy": 0.91,
    }

    feature_config_writer.write_feature_config(
        config_path,
        new_training_config,
    )

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written == {
        **new_training_config,
        "runtime_artifacts": {
            "expected_sklearn_version": sklearn.__version__,
            "files": _expected_files(),
        },
    }
    assert set(written["runtime_artifacts"]) == {
        "expected_sklearn_version",
        "files",
    }
    assert set(written["runtime_artifacts"]["files"]) == set(
        feature_config_writer.ARTIFACT_NAMES
    )
    assert all(
        stat.S_IMODE((tmp_path / filename).stat().st_mode) == 0o600
        for filename in feature_config_writer.ARTIFACT_NAMES
    )


@pytest.mark.parametrize(
    "existing_runtime",
    (
        {"files": {}},
        {
            "expected_sklearn_version": "0.0.0",
            "files": [],
        },
        {
            "expected_sklearn_version": "1.7.2",
            "files": {
                "disease_predictor.pkl": {
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                },
            },
            "extra_semantics": "旧清单不得继承",
        },
    ),
)
def test_write_feature_config_replaces_old_or_malformed_runtime_artifacts(
    tmp_path: Path,
    existing_runtime: object,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)
    config_path.write_text(
        json.dumps(
            {
                "feature_cols": ["旧特征"],
                "runtime_artifacts": existing_runtime,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    feature_config_writer.write_feature_config(
        config_path,
        {
            "feature_cols": ["新特征"],
            "runtime_artifacts": {
                "files": {"伪造": "调用方内容也不得继承"},
            },
        },
    )

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["runtime_artifacts"] == {
        "expected_sklearn_version": sklearn.__version__,
        "files": _expected_files(),
    }
    assert written["feature_cols"] == ["新特征"]


@pytest.mark.parametrize(
    ("broken_name", "broken_kind"),
    (
        ("label_encoder.pkl", "missing"),
        ("scaler.pkl", "directory"),
        ("disease_predictor.pkl", "empty"),
    ),
)
def test_write_feature_config_marks_incomplete_artifacts_stale(
    tmp_path: Path,
    broken_name: str,
    broken_kind: str,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)
    broken_path = tmp_path / broken_name
    broken_path.unlink()
    if broken_kind == "directory":
        broken_path.mkdir()
    elif broken_kind == "empty":
        broken_path.touch()

    old_digest = "a" * 64
    config_path.write_text(
        json.dumps(
            {
                "feature_cols": ["旧特征"],
                "runtime_artifacts": {
                    "expected_sklearn_version": sklearn.__version__,
                    "files": {
                        name: {
                            "sha256": old_digest,
                            "size_bytes": 1,
                        }
                        for name in feature_config_writer.ARTIFACT_NAMES
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    feature_config_writer.write_feature_config(
        config_path,
        {"feature_cols": ["新特征"]},
    )

    written = json.loads(config_path.read_text(encoding="utf-8"))
    runtime = written["runtime_artifacts"]
    assert runtime["status"] == "stale"
    assert runtime["reason"]
    assert runtime["expected_sklearn_version"] == sklearn.__version__
    assert runtime["files"] == {}
    assert old_digest not in config_path.read_text(encoding="utf-8")
    assert set(runtime) != {"expected_sklearn_version", "files"}


def test_write_feature_config_marks_symbolic_link_stale(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)
    target = tmp_path / "outside.pkl"
    target.write_bytes(b"outside")
    artifact = tmp_path / "scaler.pkl"
    artifact.unlink()
    artifact.symlink_to(target)

    feature_config_writer.write_feature_config(
        config_path,
        {"feature_cols": ["年龄数值"]},
    )

    runtime = json.loads(
        config_path.read_text(encoding="utf-8")
    )["runtime_artifacts"]
    assert runtime["status"] == "stale"
    assert runtime["files"] == {}


def test_write_feature_config_marks_hard_link_stale_without_chmod(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)
    artifact = tmp_path / "scaler.pkl"
    artifact.chmod(0o644)
    os.link(artifact, tmp_path / "scaler-copy.pkl")

    feature_config_writer.write_feature_config(
        config_path,
        {"feature_cols": ["年龄数值"]},
    )

    runtime = json.loads(
        config_path.read_text(encoding="utf-8")
    )["runtime_artifacts"]
    assert runtime["status"] == "stale"
    assert runtime["reason"] == "artifact_metadata_invalid"
    assert runtime["files"] == {}
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o644


def test_write_feature_config_marks_unexpected_digest_error_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)

    def fail_read(descriptor: int, size: int) -> bytes:
        raise RuntimeError("模拟摘要过程异常")

    monkeypatch.setattr(feature_config_writer.os, "read", fail_read)

    feature_config_writer.write_feature_config(
        config_path,
        {"feature_cols": ["年龄数值"]},
    )

    runtime = json.loads(
        config_path.read_text(encoding="utf-8")
    )["runtime_artifacts"]
    assert runtime == {
        "status": "stale",
        "reason": "artifact_digest_error",
        "expected_sklearn_version": sklearn.__version__,
        "files": {},
    }


def test_write_feature_config_keeps_original_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "feature_config.json"
    _write_artifacts(tmp_path)
    original_content = (
        '{"feature_cols":["旧特征"],'
        '"runtime_artifacts":{"files":{"model":{"size_bytes":1}}}}'
    )
    config_path.write_text(original_content, encoding="utf-8")

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr(feature_config_writer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="模拟原子替换失败"):
        feature_config_writer.write_feature_config(
            config_path,
            {"feature_cols": ["新特征"]},
        )

    assert config_path.read_text(encoding="utf-8") == original_content
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [config_path.name, *ARTIFACT_CONTENT]
    )


@pytest.mark.parametrize("script_name", TRAINING_SCRIPTS)
def test_training_scripts_write_manifest_after_all_three_artifacts(
    script_name: str,
) -> None:
    script = ROOT / "services" / "pipelines" / script_name
    content = script.read_text(encoding="utf-8")
    writer_position = content.index("write_feature_config(")

    assert content.count("write_feature_config(") == 1
    for filename in feature_config_writer.ARTIFACT_NAMES:
        dump_position = content.index(
            f"MODELS_DIR / '{filename}'",
        )
        assert dump_position < writer_position
    assert "with open(MODELS_DIR / 'feature_config.json'" not in content
