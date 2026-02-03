# -*- coding: utf-8 -*-
"""
紧急分流模块（关键词匹配）
"""
from datetime import datetime
import os
import re


DEFAULT_KEYWORDS = [
    '胸痛', '胸闷', '呼吸困难', '气喘不上来', '意识模糊', '说话不清', '口齿不清',
    '严重头晕', '持续头晕', '单侧无力', '一侧无力', '肢体无力', '面部歪斜',
    '突然昏倒', '昏迷', '抽搐', '剧烈腹痛', '大出血', '呕血'
]


from core.time_utils import utcnow
def triage_symptoms(text, keywords=None):
    """基于关键词的紧急分流判断"""
    if not text or not isinstance(text, str):
        return {
            'is_emergency': False,
            'actions': [],
            'matched_keywords': [],
            'triage_time': utcnow().isoformat(),
            'disclaimer': '系统提示仅供参考，不能替代医生诊断。'
        }

    keyword_list = keywords
    if keyword_list is None:
        env_keywords = os.getenv('EMERGENCY_TRIAGE_KEYWORDS')
        if env_keywords:
            keyword_list = [k.strip() for k in re.split(r'[，,;；\\s]+', env_keywords) if k.strip()]
        else:
            keyword_list = DEFAULT_KEYWORDS
    matched = []
    for kw in keyword_list:
        if kw and kw in text:
            matched.append(kw)

    is_emergency = bool(matched)
    actions = []
    if is_emergency:
        actions = [
            '如出现胸痛、呼吸困难或意识不清，请立即就医或拨打120。',
            '建议尽快联系家属或村医协助处理。'
        ]

    return {
        'is_emergency': is_emergency,
        'actions': actions,
        'matched_keywords': matched[:5],
        'triage_time': utcnow().isoformat(),
        'disclaimer': '系统提示仅供参考，不能替代医生诊断。'
    }
