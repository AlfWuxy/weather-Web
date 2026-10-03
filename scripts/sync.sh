#!/bin/bash
# 快速同步必须保持已有HTTPS、非root服务与回环监听边界。
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
source "$SCRIPT_DIR/deployment_security.sh"
initialize_deploy_security
# 在上传前检查已有运行边界，旧的不安全部署不能借同步入口继续运行。
verify_deployment_boundary
upload_files
remote_exec "python3 '$PROJECT_DIR/scripts/deployment_security.py' secure-tree '$PROJECT_DIR' '$APP_USER'"
remote_exec "systemctl restart case-weather && systemctl is-active --quiet case-weather"
verify_deployment_boundary
printf '同步完成，HTTPS与端口隔离检查通过：%s\n' "$PUBLIC_BASE_URL"
