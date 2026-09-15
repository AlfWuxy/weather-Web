#!/usr/bin/env python3
"""本地训练：python scripts/pilot_train.py snapshot.zip --output candidate.json。"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.data_workbench.count_model import ModelValidationError, canonical_bytes, train_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验冻结数据包并在本地训练机构就诊计数模型")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="机构计数模型候选")
    args = parser.parse_args(argv)
    try:
        bundle = train_snapshot(args.snapshot, name=args.name)
        # 仅写新文件，防止覆盖已经用于评估或上传的候选版本。
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(bundle) + b"\n")
    except (ModelValidationError, OSError, ValueError) as exc:
        print(f"训练未完成：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "family": bundle["model"]["family"],
                      "exploratory": bundle["exploratory"], "periods": bundle["periods"],
                      "evaluation_kind": bundle["evaluation_kind"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
