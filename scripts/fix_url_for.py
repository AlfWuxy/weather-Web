#!/usr/bin/env python3
"""
修复模板中的 url_for 调用，添加蓝图前缀
"""
import os
import re
from pathlib import Path

# 定义端点到蓝图的映射关系
ENDPOINT_MAPPING = {
    # public 蓝图
    'index': 'public.index',
    'login': 'public.login',
    'logout': 'public.logout',
    'register': 'public.register',
    
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
    
    # public 蓝图（补充）
    'guest_login': 'public.guest_login',
}

def fix_url_for_in_file(file_path):
    """修复文件中的 url_for 调用"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    changes_made = []
    
    # 查找所有 url_for 调用
    pattern = r"url_for\(['\"]([^'\"]+)['\"]"
    
    def replace_endpoint(match):
        endpoint = match.group(1)
        # 如果已经包含蓝图前缀或是 static，不修改
        if '.' in endpoint or endpoint == 'static':
            return match.group(0)
        
        # 查找映射
        if endpoint in ENDPOINT_MAPPING:
            new_endpoint = ENDPOINT_MAPPING[endpoint]
            changes_made.append(f"  {endpoint} -> {new_endpoint}")
            return f"url_for('{new_endpoint}'"
        else:
            print(f"  ⚠️  未找到映射: {endpoint}")
            return match.group(0)
    
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
    templates_dir = base_dir / 'templates'
    fixed_count = 0
    
    print("开始修复模板文件...\n")
    
    for html_file in sorted(templates_dir.glob('*.html')):
        if fix_url_for_in_file(html_file):
            fixed_count += 1
            print()
    
    print(f"\n修复完成！共修改 {fixed_count} 个文件")

if __name__ == '__main__':
    main()
