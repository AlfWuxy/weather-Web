#!/bin/bash
# 完成手动修复步骤的辅助脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

trim_whitespace() {
    local value="${1:-}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

normalize_env_value() {
    local value
    local remainder
    value="$(trim_whitespace "${1:-}")"
    case "$value" in
        \"*)
            value="${value#\"}"
            [[ "$value" == *\"* ]] || return 1
            remainder="${value#*\"}"
            value="${value%%\"*}"
            ;;
        \'*)
            value="${value#\'}"
            [[ "$value" == *\'* ]] || return 1
            remainder="${value#*\'}"
            value="${value%%\'*}"
            ;;
        *)
            value="${value%%#*}"
            remainder=""
            ;;
    esac
    remainder="$(trim_whitespace "$remainder")"
    if [ -n "$remainder" ] && [[ "$remainder" != \#* ]]; then
        return 1
    fi
    trim_whitespace "$value"
}

read_env_value() {
    local target="$1"
    local env_file="$2"
    local line key value found=1
    while IFS= read -r line || [ -n "$line" ]; do
        line="$(trim_whitespace "$line")"
        case "$line" in ''|\#*) continue ;; esac
        [[ "$line" == *=* ]] || continue
        key="$(trim_whitespace "${line%%=*}")"
        [ "$key" = "$target" ] || continue
        if ! value="$(normalize_env_value "${line#*=}")"; then
            value=""
        fi
        found=0
    done < "$env_file"
    [ "$found" -eq 0 ] || return 1
    printf '%s' "$value"
}

is_placeholder_secret() {
    local value lowered
    if ! value="$(normalize_env_value "${1:-}")"; then
        return 0
    fi
    lowered="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
    case "$lowered" in
        ''|your-*|your_*|change-me*|change_me*|changeme*|example*|placeholder*|replace-me*) return 0 ;;
        *) return 1 ;;
    esac
}

write_env_value() {
    local key="$1"
    local value="$2"
    local env_file="$3"
    local tmp_file="${env_file}.tmp"
    awk -v wanted="$key" -v replacement="$key=$value" '
        BEGIN { updated = 0 }
        {
            line = $0
            trimmed = line
            sub(/^[[:space:]]*/, "", trimmed)
            if (trimmed !~ /^#/ && index(trimmed, "=") > 0) {
                candidate = substr(trimmed, 1, index(trimmed, "=") - 1)
                gsub(/[[:space:]]/, "", candidate)
                if (candidate == wanted) {
                    if (!updated) {
                        print replacement
                        updated = 1
                    }
                    # 后续重复键直接丢弃，确保最终配置只有一个有效值。
                    next
                }
            }
            print line
        }
        END { if (!updated) print replacement }
    ' "$env_file" > "$tmp_file"
    # 新文件固定为仅当前用户可读写，避免密钥轮换时放宽原有权限。
    chmod 600 "$tmp_file"
    mv "$tmp_file" "$env_file"
}

check_or_generate_secret() {
    local key="$1"
    local value=""
    local prompt_text=""
    local generated=""
    local reply=""

    if value="$(read_env_value "$key" .env)"; then
        if ! is_placeholder_secret "$value"; then
            echo -e "${GREEN}✅ $key 已配置${NC}"
            return 0
        fi
        echo -e "${RED}❌ $key 为空或使用示例值${NC}"
        prompt_text="是否生成新的 $key? (y/n) "
    else
        echo -e "${YELLOW}⚠️  $key 未找到${NC}"
        prompt_text="是否生成 $key? (y/n) "
    fi

    read -p "$prompt_text" -n 1 -r reply
    echo
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        generated="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        write_env_value "$key" "$generated" .env
        echo -e "${GREEN}✅ $key 已生成${NC}"
    fi
}

# 被测试脚本 source 时只暴露辅助函数，不进入交互流程。
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

cd "$ROOT_DIR"

echo "============================================================"
echo "安全修复 - 手动步骤辅助脚本"
echo "============================================================"

# 1. 检查 .env 文件
echo -e "\n${YELLOW}[1/5] 检查 .env 文件...${NC}"
if [ ! -f .env ]; then
    echo -e "${RED}❌ .env 文件不存在${NC}"
    if [ -f .env.backup ]; then
        read -p "是否从 .env.backup 恢复? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            cp .env.backup .env
            echo -e "${GREEN}✅ 已从 .env.backup 恢复${NC}"
        fi
    elif [ -f .env.example ]; then
        read -p "是否从 .env.example 创建新的? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            cp .env.example .env
            echo -e "${YELLOW}⚠️  已创建 .env，请手动编辑填入真实密钥${NC}"
        fi
    fi
else
    echo -e "${GREEN}✅ .env 文件存在${NC}"
fi

# 2. 检查 SECRET_KEY
echo -e "\n${YELLOW}[2/5] 检查 SECRET_KEY...${NC}"
if [ -f .env ]; then
    check_or_generate_secret "SECRET_KEY"
else
    echo -e "${RED}❌ .env 文件不存在，跳过检查${NC}"
fi

# 3. 检查 PAIR_TOKEN_PEPPER
echo -e "\n${YELLOW}[3/5] 检查 PAIR_TOKEN_PEPPER...${NC}"
if [ -f .env ]; then
    check_or_generate_secret "PAIR_TOKEN_PEPPER"
else
    echo -e "${RED}❌ .env 文件不存在，跳过检查${NC}"
fi

# 4. 可选：配置速率限制
echo -e "\n${YELLOW}[4/5] 配置速率限制（可选）...${NC}"
if [ -f .env ]; then
    if ! grep -q "^RATE_LIMIT_LOGIN=" .env; then
        read -p "是否配置更严格的登录限流 (5 per 5 minutes)? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "RATE_LIMIT_LOGIN=5 per 5 minutes" >> .env
            echo -e "${GREEN}✅ RATE_LIMIT_LOGIN 已配置${NC}"
        fi
    else
        echo -e "${GREEN}✅ RATE_LIMIT_LOGIN 已存在${NC}"
    fi

    if ! grep -q "^RATE_LIMIT_AI=" .env; then
        read -p "是否配置 AI 限流 (20 per minute)? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "RATE_LIMIT_AI=20 per minute" >> .env
            echo -e "${GREEN}✅ RATE_LIMIT_AI 已配置${NC}"
        fi
    else
        echo -e "${GREEN}✅ RATE_LIMIT_AI 已存在${NC}"
    fi
else
    echo -e "${RED}❌ .env 文件不存在，跳过配置${NC}"
fi

# 5. 运行验证测试
echo -e "\n${YELLOW}[5/5] 运行验证测试...${NC}"
read -p "是否运行 scripts/test_fixes.py 验证修复? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f scripts/test_fixes.py ]; then
        python3 scripts/test_fixes.py
    else
        echo -e "${RED}❌ scripts/test_fixes.py 不存在${NC}"
    fi
fi

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}手动步骤辅助脚本执行完成！${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "\n后续步骤:"
echo -e "  1. 审查 .env 文件，确保所有密钥已正确配置"
echo -e "  2. 运行默认自动测试: ${YELLOW}pytest tests/ -v${NC}"
echo -e "  3. 审查异常处理: ${YELLOW}grep -rn 'except Exception' blueprints/ services/${NC}"
echo -e "  4. 历史安全修复报告已归档，不再保留在产品仓库中${NC}"

exit 0
