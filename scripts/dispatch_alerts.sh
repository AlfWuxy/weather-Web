#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -z "${VENV_PY:-}" ]; then
  if [ -n "${DEPLOY_VENV_DIR:-}" ]; then
    case "$DEPLOY_VENV_DIR" in
      /*) ;;
      *)
        echo "错误：DEPLOY_VENV_DIR 必须是绝对路径：$DEPLOY_VENV_DIR" >&2
        exit 70
        ;;
    esac

    CANDIDATE="${DEPLOY_VENV_DIR%/}/bin/python"
    if [ -f "$CANDIDATE" ] && [ -x "$CANDIDATE" ]; then
      VENV_PY="$CANDIDATE"
    fi
  fi

  if [ -z "${VENV_PY:-}" ]; then
    # 优先使用当前仓库自己的虚拟环境；正式 release 的虚拟环境位于 app 同级，作为最后候选。
    for CANDIDATE in \
      "$ROOT_DIR/.venv2/bin/python" \
      "$ROOT_DIR/venv/bin/python" \
      "$ROOT_DIR/.venv/bin/python" \
      "$ROOT_DIR/../venv/bin/python"; do
      if [ -f "$CANDIDATE" ] && [ -x "$CANDIDATE" ]; then
        VENV_PY="$CANDIDATE"
        break
      fi
    done
  fi

  if [ -z "${VENV_PY:-}" ]; then
    echo "错误：未找到可执行的绝对 Python 路径，请设置 VENV_PY 或 DEPLOY_VENV_DIR。" >&2
    exit 70
  fi
fi

# 禁止使用 PATH 中的裸 python，避免 systemd 与交互式 shell 解析到不同解释器。
case "$VENV_PY" in
  /*) ;;
  *)
    echo "错误：VENV_PY 必须是绝对路径：$VENV_PY" >&2
    exit 70
    ;;
esac

if [ ! -f "$VENV_PY" ] || [ ! -x "$VENV_PY" ]; then
  echo "错误：VENV_PY 不是可执行文件：$VENV_PY" >&2
  exit 70
fi

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 锁路径只由应用正式配置读取，wrapper 不再生成临时替代路径。
exec "$VENV_PY" -m services.pipelines.dispatch_alerts "$@"
