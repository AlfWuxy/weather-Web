#!/usr/bin/env python3
"""
修复 blueprints 目录中所有 Python 文件的 url_for 调用
"""
import re
from pathlib import Path

# 端点映射
ENDPOINT_MAPPING = {
    # public 蓝图
    'index': 'public.index',
    'login': 'public.login',
    'logout': 'public.logout',
    'register': 'public.register',
    'guest_login': 'public.guest_login',
    
    # admin 蓝图
    'admin_dashboard': 'admin.admin_dashboard',
    'admin_users': 'admin.admin_users',
    'admin_add_user': 'admin.admin_add_user',
    'admin_edit_user': 'admin.admin_edit_user',
    'admin_delete_user': 'admin.admin_delete_user',
    'admin_communities': 'admin.admin_communities',
    'admin_add_community': 'admin.admin_add_community',
    'admin_edit_community': 'admin.admin_edit_community',
    'admin_records': 'admin.admin_records',
    'admin_statistics': 'admin.admin_statistics',
    'admin_sync_community_coordinates': 'admin.admin_sync_community_coordinates',
    
    # user 蓝图
    'user_dashboard': 'user.user_dashboard',
    'profile': 'user.profile',
    'elder_dashboard': 'user.elder_dashboard',
    'update_location': 'user.update_location',
    
    # health 蓝图
    'health_assessment': 'health.health_assessment',
    'health_diary': 'health.health_diary',
    'medication_reminders': 'health.medication_reminders',
    'medication_reminder_delete': 'health.medication_reminder_delete',
    'family_members': 'health.family_members',
    'family_member_edit': 'health.family_member_edit',
    'family_member_detail': 'health.family_member_detail',
    'family_member_delete': 'health.family_member_delete',
    'family_member_toggle_alert': 'health.family_member_toggle_alert',
    'chronic_risk': 'health.chronic_risk',
    'community_risk': 'health.community_risk',
    
    # analysis 蓝图
    'analysis_history': 'analysis.analysis_history',
    'analysis_lag': 'analysis.analysis_lag',
    'analysis_heatmap': 'analysis.analysis_heatmap',
    'analysis_community_compare': 'analysis.analysis_community_compare',
    'alerts_history': 'analysis.alerts_history',
    'alerts_accuracy': 'analysis.alerts_accuracy',
    'reports': 'analysis.reports',
    'reports_center': 'analysis.reports_center',
    'reports_export': 'analysis.reports_export',
    'annual_report': 'analysis.annual_report',
    
    # tools 蓝图
    'forecast_7day': 'tools.forecast_7day',
    'ml_prediction': 'tools.ml_prediction',
    'ai_question': 'tools.ai_question',
    'ai_qa': 'tools.ai_qa',
}

def fix_url_for_in_file(file_path):
    """修复 Python 文件中的 url_for 调用"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    changes_made = []
    
    # 匹配 url_for('endpoint' 或 url_for("endpoint"
    pattern = r"url_for\(['\"]([^'\"]+)['\"]"
    
    def replace_endpoint(match):
        full_match = match.group(0)
        endpoint = match.group(1)
        
        # 如果已经包含蓝图前缀或是 static，不修改
        if '.' in endpoint or endpoint == 'static':
            return full_match
        
        # 查找映射
        if endpoint in ENDPOINT_MAPPING:
            new_endpoint = ENDPOINT_MAPPING[endpoint]
            quote = "'" if "'" in full_match else '"'
            changes_made.append(f"  {endpoint} -> {new_endpoint}")
            return f"url_for({quote}{new_endpoint}{quote}"
        else:
            print(f"  ⚠️  未找到映射: {endpoint} in {file_path.name}")
            return full_match
    
    content = re.sub(pattern, replace_endpoint, content)
    
    if content != original_content:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ {file_path.name}")
        for change in changes_made:
            print(change)
        return True
    return False

def main():
    base_dir = Path(__file__).resolve().parents[2]
    blueprints_dir = base_dir / 'blueprints'
    fixed_count = 0
    
    print("开始修复 blueprints Python 文件...\n")
    
    for py_file in sorted(blueprints_dir.glob('*.py')):
        if fix_url_for_in_file(py_file):
            fixed_count += 1
            print()
    
    print(f"\n修复完成！共修改 {fixed_count} 个文件")

if __name__ == '__main__':
    main()
