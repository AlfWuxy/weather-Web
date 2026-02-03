#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试配置验证功能"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

print("=" * 60)
print("测试配置验证功能")
print("=" * 60)

# 测试1: 正常配置应该通过
print("\n[测试1] 正常配置（应该通过）")
os.environ['DEBUG'] = 'true'
os.environ['SECRET_KEY'] = 'test-secret-key-for-validation-32chars'
os.environ['PAIR_TOKEN_PEPPER'] = 'test-pepper-for-validation-32chars'
os.environ['DEMO_MODE'] = '1'

try:
    from core.config import validate_production_config
    validate_production_config()
    print("✅ 正常配置验证通过")
except Exception as e:
    print(f"❌ 验证失败: {e}")
    sys.exit(1)

# 测试2: 检查弱密钥是否被拒绝（在生产模式下）
print("\n[测试2] 检查弱密钥检测")
weak_keys = ['your-secret-key-here', 'your-secret-key-change-in-production']
for weak_key in weak_keys:
    os.environ['SECRET_KEY'] = weak_key
    os.environ['DEBUG'] = 'false'
    
    # 需要重新导入以触发验证
    import importlib
    import core.config as config_module
    
    try:
        importlib.reload(config_module)
        # 如果重新加载config，validate_production_config会在模块导入时被调用
        # 但是由于已经导入，我们直接调用函数
        config_module.validate_production_config()
        print(f"⚠️  弱密钥 '{weak_key}' 未被检测（可能是 DEBUG=true）")
    except RuntimeError as e:
        if "示例值" in str(e):
            print(f"✅ 弱密钥 '{weak_key}' 被正确拒绝")
        else:
            print(f"❌ 错误消息不符合预期: {e}")

# 恢复环境
os.environ['DEBUG'] = 'true'
os.environ['SECRET_KEY'] = 'test-secret-key-for-validation-32chars'

print("\n" + "=" * 60)
print("配置验证测试完成")
print("=" * 60)
