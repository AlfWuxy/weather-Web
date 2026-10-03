"""埋点只保留事件所需的汇总信息，不复制地址、坐标或自由文本。"""

_FIELDS = frozenset({'name', 'age', 'gender', 'relationship', 'chronic_diseases',
                     'wxpusher_uid', 'push_enabled', 'community'})
_ENUMS = {
    'via': frozenset({'mp_api', 'family_members', 'family_member_new', 'family_member_edit'}),
    'channel': frozenset({'wxpusher', 'wxoa'}),
    'relay_stage': frozenset({'none', 'caregiver', 'backup', 'community', 'emergency'}),
    'alert_type': frozenset({'heat', 'cold', 'rain', 'wind', 'warning', 'threshold'}),
    'alert_kind': frozenset({'heat', 'cold', ''}),
    'kind': frozenset({'reminded', 'acted'}),
}
_SCHEMA = {
    'elder_profile_created': {'via': 'enum'},
    'elder_profile_updated': {'via': 'enum', 'updated_fields': 'fields'},
    'settings_updated': {'fields': 'fields'},
    'feedback_submitted': {'optin': 'bool', 'difficulty_len': 'count',
                           'has_note': 'bool', 'caregiver_actions_count': 'count', 'kind': 'enum'},
    'template_copy': {'alert_kind': 'enum'},
    'checkin_confirmed': {'actions_done_count': 'count'},
    'help_flagged': {'relay_stage': 'enum'},
    'push_click': {'channel': 'enum', 'alert_id': 'count'},
    'push_sent': {'channel': 'enum', 'alert_id': 'count', 'alert_type': 'enum'},
    'push_failed': {'channel': 'enum', 'alert_id': 'count', 'alert_type': 'enum'},
}


def sanitize_analytics_meta(event_type, meta):
    """未知事件仍可计数，但不接收未知元数据；字段类型也必须匹配。"""
    if not isinstance(meta, dict):
        return None
    clean = {}
    for key, kind in _SCHEMA.get(event_type, {}).items():
        value = meta.get(key)
        if kind == 'bool' and type(value) is bool:
            clean[key] = value
        elif kind == 'count' and type(value) is int and 0 <= value <= 2147483647:
            clean[key] = value
        elif kind == 'enum' and isinstance(value, str) and value in _ENUMS[key]:
            clean[key] = value
        elif kind == 'fields' and isinstance(value, list):
            clean[key] = sorted({item for item in value[:20]
                                 if isinstance(item, str) and item in _FIELDS})
    return clean or None
