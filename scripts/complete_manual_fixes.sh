#!/bin/bash
# 完成手动修复步骤的辅助脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

echo "============================================================"
echo "安全修复 - 手动步骤辅助脚本"
echo "============================================================"

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

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
    if grep -q "^SECRET_KEY=your-secret-key" .env; then
        echo -e "${RED}❌ SECRET_KEY 使用示例值${NC}"
        read -p "是否生成新的 SECRET_KEY? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            NEW_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
            # 使用临时文件替换
            sed "s/^SECRET_KEY=.*/SECRET_KEY=$NEW_KEY/" .env > .env.tmp
            mv .env.tmp .env
            echo -e "${GREEN}✅ SECRET_KEY 已生成${NC}"
        fi
    elif grep -q "^SECRET_KEY=" .env; then
        echo -e "${GREEN}✅ SECRET_KEY 已配置${NC}"
    else
        echo -e "${YELLOW}⚠️  SECRET_KEY 未找到${NC}"
        read -p "是否生成 SECRET_KEY? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            NEW_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
            echo "SECRET_KEY=$NEW_KEY" >> .env
            echo -e "${GREEN}✅ SECRET_KEY 已添加${NC}"
        fi
    fi
else
    echo -e "${RED}❌ .env 文件不存在，跳过检查${NC}"
fi

# 3. 检查 PAIR_TOKEN_PEPPER
echo -e "\n${YELLOW}[3/5] 检查 PAIR_TOKEN_PEPPER...${NC}"
if [ -f .env ]; then
    if grep -q "^PAIR_TOKEN_PEPPER=your-pair-token-pepper" .env; then
        echo -e "${RED}❌ PAIR_TOKEN_PEPPER 使用示例值${NC}"
        read -p "是否生成新的 PAIR_TOKEN_PEPPER? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            NEW_PEPPER=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
            sed "s/^PAIR_TOKEN_PEPPER=.*/PAIR_TOKEN_PEPPER=$NEW_PEPPER/" .env > .env.tmp
            mv .env.tmp .env
            echo -e "${GREEN}✅ PAIR_TOKEN_PEPPER 已生成${NC}"
        fi
    elif grep -q "^PAIR_TOKEN_PEPPER=" .env; then
        echo -e "${GREEN}✅ PAIR_TOKEN_PEPPER 已配置${NC}"
    else
        echo -e "${YELLOW}⚠️  PAIR_TOKEN_PEPPER 未找到${NC}"
        read -p "是否生成 PAIR_TOKEN_PEPPER? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            NEW_PEPPER=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
            echo "PAIR_TOKEN_PEPPER=$NEW_PEPPER" >> .env
            echo -e "${GREEN}✅ PAIR_TOKEN_PEPPER 已添加${NC}"
        fi
    fi
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
echo -e "  2. 运行完整测试: ${YELLOW}pytest tests/ -v${NC}"
echo -e "  3. 审查异常处理: ${YELLOW}grep -rn 'except Exception' blueprints/ services/${NC}"
echo -e "  4. 阅读详细报告: ${YELLOW}docs/reports/SECURITY_FIXES_2025.md${NC}"

exit 0
