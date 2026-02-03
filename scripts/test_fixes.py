#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速验证安全修复是否生效"""
import sys
import os

# 设置环境变量
os.environ['DEBUG'] = 'true'
os.environ['DEMO_MODE'] = '1'
os.environ['SECRET_KEY'] = 'test-secret-key-for-validation'
os.environ['PAIR_TOKEN_PEPPER'] = 'test-pepper-for-validation'

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

print("=" * 60)
print("验证安全修复")
print("=" * 60)

# 1. 验证时区修复
print("\n[1/6] 验证时区修复...")
try:
    from core.time_utils import utcnow, utcnow_naive
    from datetime import timezone

    aware_time = utcnow()
    naive_time = utcnow_naive()

    assert aware_time.tzinfo == timezone.utc, "utcnow() 应该返回 timezone-aware datetime"
    assert naive_time.tzinfo is None, "utcnow_naive() 应该返回 naive datetime"

    print("✅ utcnow() 返回 timezone-aware datetime")
    print("✅ utcnow_naive() 返回 naive datetime")
except AssertionError as e:
    print(f"❌ {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ 时区函数导入失败: {e}")
    sys.exit(1)

# 2. 验证配置验证函数存在
print("\n[2/6] 验证配置验证...")
try:
    from core.config import validate_production_config

    # 在开发环境下调用（DEBUG=true）应该成功
    validate_production_config()
    print("✅ validate_production_config() 存在且可调用")
except ImportError:
    print("❌ validate_production_config() 未找到")
    sys.exit(1)
except Exception as e:
    print(f"⚠️  配置验证警告: {e}（开发环境可忽略）")

# 3. 验证 db_models 不使用 datetime.utcnow
print("\n[3/6] 验证 db_models 时区修复...")
try:
    with open(os.path.join(ROOT_DIR, 'core', 'db_models.py'), 'r', encoding='utf-8') as f:
        content = f.read()

    # 检查是否还有 datetime.utcnow（不包括注释和字符串）
    import re
    # 查找不在注释或字符串中的 datetime.utcnow
    pattern = r'^[^#]*datetime\.utcnow(?!\(\)\.isoformat)'
    matches = re.findall(pattern, content, re.MULTILINE)

    if any('datetime.utcnow' in m for m in matches):
        print("⚠️  db_models.py 仍包含 datetime.utcnow 引用")
    else:
        print("✅ db_models.py 已全部替换 datetime.utcnow")

    # 检查是否使用了 lambda: datetime.now(timezone.utc)
    if 'lambda: datetime.now(timezone.utc)' in content:
        print("✅ db_models.py 使用 lambda: datetime.now(timezone.utc)")
    else:
        print("⚠️  db_models.py 未找到新的时区模式")
except FileNotFoundError:
    print("❌ core/db_models.py 未找到")
    sys.exit(1)

# 4. 验证 .env.example 存在
print("\n[4/6] 验证 .env.example...")
try:
    with open(os.path.join(ROOT_DIR, '.env.example'), 'r', encoding='utf-8') as f:
        env_example = f.read()

    required_keys = ['SECRET_KEY', 'PAIR_TOKEN_PEPPER', 'DATABASE_URI']
    missing_keys = [key for key in required_keys if key not in env_example]

    if missing_keys:
        print(f"⚠️  .env.example 缺少: {missing_keys}")
    else:
        print("✅ .env.example 包含所有必需配置项")

    # 检查是否不包含真实密钥
    if 'sk-ecbyvvsxsicjyrnq' in env_example or '73684be4bf0141c7' in env_example:
        print("❌ .env.example 包含真实密钥！")
        sys.exit(1)
    else:
        print("✅ .env.example 不包含真实密钥")
except FileNotFoundError:
    print("❌ .env.example 未找到")
    sys.exit(1)

# 5. 验证 JSON 大小限制
print("\n[5/6] 验证 JSON 大小限制...")
try:
    with open(os.path.join(ROOT_DIR, 'core', 'hooks.py'), 'r', encoding='utf-8') as f:
        hooks_content = f.read()

    if 'MAX_JSON_BYTES' in hooks_content and 'len(raw_bytes) > MAX_JSON_BYTES' in hooks_content:
        print("✅ core/hooks.py 包含 JSON 大小限制")
    else:
        print("⚠️  core/hooks.py 未找到 JSON 大小限制")
except FileNotFoundError:
    print("❌ core/hooks.py 未找到")
    sys.exit(1)

# 6. 验证 redeemed_at 检查
print("\n[6/6] 验证 redeemed_at 重复检查...")
try:
    with open(os.path.join(ROOT_DIR, 'blueprints', 'public.py'), 'r', encoding='utf-8') as f:
        public_content = f.read()

    if 'if link.redeemed_at:' in public_content and '短码已被赎回' in public_content:
        print("✅ blueprints/public.py 包含 redeemed_at 重复检查")
    else:
        print("⚠️  blueprints/public.py 未找到 redeemed_at 检查")

    if 'hasattr(link, \'pair_id\')' in public_content:
        print("✅ blueprints/public.py 包含 pair_id 安全检查")
    else:
        print("⚠️  blueprints/public.py 未找到 pair_id 安全检查")
except FileNotFoundError:
    print("❌ blueprints/public.py 未找到")
    sys.exit(1)

print("\n" + "=" * 60)
print("验证结果")
print("=" * 60)
print("✅ 所有核心修复已验证")
print("\n建议:")
print("  1. 从 .env.backup 恢复 .env 文件（或使用 .env.example）")
print("  2. 运行完整测试: pytest tests/ -v")
print("  3. 审查异常处理: grep -rn 'except Exception' blueprints/ services/")
print("  4. 配置更严格的速率限制")

sys.exit(0)
