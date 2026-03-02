#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量修复 datetime.utcnow() 的使用

替换策略：
1. 在文件顶部添加 from core.time_utils import utcnow（如果没有）
2. 将 datetime.utcnow() 替换为 utcnow().replace(tzinfo=None)
   - 使用 .replace(tzinfo=None) 保持向后兼容（数据库可能期望 naive datetime）
"""
import re
from pathlib import Path

# 需要修复的文件列表
FILES_TO_FIX = [
    'blueprints/user.py',
    'blueprints/public.py',
    'blueprints/analysis.py',
    'core/guest.py',
    'services/chronic_risk_service.py',
    'services/emergency_triage.py',
    'services/pipelines/sync_weather_cache.py',
]

def fix_file(filepath):
    """修复单个文件"""
    path = Path(filepath)
    if not path.exists():
        print(f"跳过不存在的文件: {filepath}")
        return False

    content = path.read_text(encoding='utf-8')
    original_content = content

    # 检查是否需要修复
    if 'datetime.utcnow()' not in content:
        print(f"无需修复: {filepath}")
        return False

    # 检查是否已经导入 utcnow
    has_utcnow_import = 'from core.time_utils import' in content and 'utcnow' in content

    if not has_utcnow_import:
        # 查找导入区域
        lines = content.split('\n')
        import_insert_line = -1

        for i, line in enumerate(lines):
            # 在第一个 from core 或 from flask 导入之后插入
            if line.startswith('from core.') or line.startswith('from flask'):
                import_insert_line = i + 1
            # 或在第一个函数定义之前
            elif line.startswith('def ') or line.startswith('class '):
                if import_insert_line == -1:
                    import_insert_line = i
                break

        if import_insert_line > 0:
            # 检查是否已有 time_utils 导入
            time_utils_import_line = -1
            for i, line in enumerate(lines):
                if 'from core.time_utils import' in line:
                    time_utils_import_line = i
                    break

            if time_utils_import_line >= 0:
                # 追加到现有导入
                lines[time_utils_import_line] = lines[time_utils_import_line].rstrip()
                if not lines[time_utils_import_line].endswith('utcnow'):
                    if ')' in lines[time_utils_import_line]:
                        lines[time_utils_import_line] = lines[time_utils_import_line].replace(')', ', utcnow)')
                    else:
                        lines[time_utils_import_line] += ', utcnow'
            else:
                # 插入新导入
                lines.insert(import_insert_line, 'from core.time_utils import utcnow')

            content = '\n'.join(lines)

    # 替换 datetime.utcnow() 为 utcnow().replace(tzinfo=None)
    # 保持 naive datetime 以兼容现有数据库字段
    content = re.sub(
        r'datetime\.utcnow\(\)',
        'utcnow().replace(tzinfo=None)',
        content
    )

    if content != original_content:
        path.write_text(content, encoding='utf-8')
        print(f"✅ 已修复: {filepath}")
        return True
    else:
        print(f"无变更: {filepath}")
        return False

def main():
    """主函数"""
    print("开始批量修复 datetime.utcnow()...\n")

    fixed_count = 0
    for filepath in FILES_TO_FIX:
        if fix_file(filepath):
            fixed_count += 1

    print(f"\n修复完成！共修复 {fixed_count} 个文件")

if __name__ == '__main__':
    main()
