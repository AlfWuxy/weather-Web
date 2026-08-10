#!/bin/bash
# 兼容旧命令：所有同步统一走不可变发布与自动回滚流程。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "sync.sh 已切换为安全发布兼容入口。"
exec "$SCRIPT_DIR/deploy.sh" "$@"
