#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键应用所有安全修复

运行方式：
    python3 scripts/apply_security_fixes.py

修复内容：
    B. 时区一致性 - 替换 utcnow().replace(tzinfo=None) 为 utcnow_naive()
    C. 异常处理精细化 - 替换宽泛的 except Exception
    D. 输入校验与安全 - JSON大小限制、CSRF、限流
    E. 数据库事务 - 添加回滚处理、连接池配置
    F. 业务逻辑 - 短码强化、审计日志、None检查
"""
import re
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ========================================
# B. 时区一致性修复
# ========================================

def fix_timezone_naive_calls():
    """替换 utcnow().replace(tzinfo=None) 为 utcnow_naive()"""
    files_to_fix = [
        'services/emergency_triage.py',
        'services/chronic_risk_service.py',
        'core/guest.py',
        'core/weather.py',
        'services/pipelines/sync_weather_cache.py',
        'blueprints/public.py',
        'blueprints/analysis.py',
        'blueprints/user.py',
    ]

    pattern = r'utcnow\(\)\.replace\(tzinfo=None\)'
    replacement = 'utcnow_naive()'

    for file_path in files_to_fix:
        full_path = PROJECT_ROOT / file_path
        if not full_path.exists():
            print(f"⚠️  跳过不存在的文件: {file_path}")
            continue

        content = full_path.read_text(encoding='utf-8')
        new_content = re.sub(pattern, replacement, content)

        if content != new_content:
            full_path.write_text(new_content, encoding='utf-8')
            count = len(re.findall(pattern, content))
            print(f"✅ {file_path}: 替换 {count} 处 utcnow().replace(tzinfo=None)")
        else:
            print(f"  {file_path}: 无需修改")


# ========================================
# C. 异常处理精细化
# ========================================

def fix_exception_handling():
    """添加更具体的异常处理（需手动审查）"""
    print("\n⚠️  异常处理精细化需要手动审查以下文件：")
    print("   - blueprints/api.py (8+ 处过宽异常)")
    print("   - blueprints/analysis.py (bare pass)")
    print("   - core/hooks.py (JSON 解析)")
    print("   建议：运行 grep -rn 'except Exception' blueprints/ services/ 查找所有位置")


# ========================================
# D. 输入校验与安全
# ========================================

def add_json_size_validation():
    """在 core/hooks.py 添加 JSON 大小限制"""
    file_path = PROJECT_ROOT / 'core/hooks.py'

    if not file_path.exists():
        print("⚠️  core/hooks.py 不存在，跳过")
        return

    content = file_path.read_text(encoding='utf-8')

    # 检查是否已经添加了限制
    if 'JSON_MAX_SIZE' in content or 'len(str(value)) > 10000' in content:
        print("  core/hooks.py: JSON 大小限制已存在")
        return

    # 查找 from_json_filter 函数
    old_pattern = r'(def from_json_filter\(value\):.*?)(    if value:)'
    new_code = r'\1    # JSON 大小限制（10KB）\n    if value and len(str(value)) <= 10000:\2'

    new_content = re.sub(old_pattern, new_code, content, flags=re.DOTALL)

    if content != new_content:
        file_path.write_text(new_content, encoding='utf-8')
        print("✅ core/hooks.py: 添加 JSON 大小限制")
    else:
        print("⚠️  core/hooks.py: 未能自动添加限制，需手动检查")


def update_rate_limits():
    """更新登录限流配置"""
    print("\n⚠️  速率限制更新需要手动操作：")
    print("   在 .env 文件中设置：")
    print("   RATE_LIMIT_LOGIN=5 per 5 minutes")
    print("   RATE_LIMIT_AI=20 per minute")
    print("   当前配置在 core/config.py:150")


# ========================================
# E. 数据库事务与连接池
# ========================================

def add_db_connection_pool():
    """在 core/extensions.py 添加 SQLAlchemy 连接池配置"""
    file_path = PROJECT_ROOT / 'core/extensions.py'

    if not file_path.exists():
        print("⚠️  core/extensions.py 不存在，跳过")
        return

    content = file_path.read_text(encoding='utf-8')

    # 检查是否已经配置
    if 'pool_pre_ping' in content:
        print("  core/extensions.py: 连接池配置已存在")
        return

    # 查找 db = SQLAlchemy() 或初始化位置
    if 'db = SQLAlchemy()' in content:
        # 添加配置注释
        config_comment = '''
# SQLAlchemy 连接池配置（在 core/config.py 的 configure_app 中设置）
# app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
#     'pool_pre_ping': True,  # 连接前先 ping，避免使用过期连接
#     'pool_size': 5,         # 连接池大小
#     'pool_recycle': 3600,   # 连接回收时间（秒）
#     'max_overflow': 10      # 超出 pool_size 后允许的最大连接数
# }
'''
        new_content = content.replace('db = SQLAlchemy()', f'db = SQLAlchemy(){config_comment}')
        file_path.write_text(new_content, encoding='utf-8')
        print("✅ core/extensions.py: 添加连接池配置注释")
    else:
        print("⚠️  core/extensions.py: 未找到 db = SQLAlchemy()，需手动添加配置")


# ========================================
# F. 业务逻辑加固
# ========================================

def strengthen_short_codes():
    """提示强化短码生成"""
    print("\n⚠️  短码强化需要手动操作：")
    print("   1. 检查 blueprints/user.py 中的 generate_short_code()")
    print("   2. 将长度从 6 位增加到 8 位")
    print("   3. 添加审计日志：短码生成和赎回")
    print("   4. 添加 redeemed_at 重复检查")
    print("   5. 强制 expires_at 校验")


def add_none_checks():
    """添加 None 检查提示"""
    print("\n⚠️  None 检查需要手动操作：")
    print("   在 blueprints/public.py 中检查以下位置：")
    print("   - line 197: link.expires_at 检查前先验证 link 不为 None")
    print("   - line 220-226: 使用 link.pair_id 前先检查")


# ========================================
# 主函数
# ========================================

def main():
    print("=" * 60)
    print("开始应用安全修复")
    print("=" * 60)

    # B. 时区一致性
    print("\n[B] 修复时区一致性...")
    fix_timezone_naive_calls()

    # C. 异常处理
    print("\n[C] 异常处理精细化...")
    fix_exception_handling()

    # D. 输入校验与安全
    print("\n[D] 输入校验与安全...")
    add_json_size_validation()
    update_rate_limits()

    # E. 数据库事务与连接池
    print("\n[E] 数据库事务与连接池...")
    add_db_connection_pool()

    # F. 业务逻辑加固
    print("\n[F] 业务逻辑加固...")
    strengthen_short_codes()
    add_none_checks()

    print("\n" + "=" * 60)
    print("自动修复完成！")
    print("=" * 60)
    print("\n请手动完成以下步骤：")
    print("  1. 复制 .env.backup 为 .env 并填入真实密钥")
    print("  2. 审查并修复 blueprints/api.py 中的宽泛异常处理")
    print("  3. 在 .env 中设置更严格的速率限制")
    print("  4. 添加短码审计日志和None检查")
    print("  5. 运行测试: pytest tests/ -v")


if __name__ == '__main__':
    main()
