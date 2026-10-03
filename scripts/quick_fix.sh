#!/bin/bash
# 补齐本地安全配置，不打印密钥或改变已有有效密钥。
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"
# 原子发布目录通过链接复用持久化状态，已有目标权限由部署流程管理。
if [ -L instance ]; then
    [ -d instance ] || { echo 'instance 链接目标不是有效目录' >&2; exit 1; }
else
    mkdir -p instance
    chmod 700 instance
fi
python3 "$SCRIPT_DIR/secure_environment.py" defaults .env
echo '安全配置已补齐，环境文件权限为0600；已有密钥保持不变。'
