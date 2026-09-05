# -*- coding: utf-8 -*-
"""单一话术来源。网页直接调用，小程序经 /scripts 拉取。"""
from __future__ import annotations

import hashlib
import json

DEFAULT_SCRIPT_VERSION = 'v2_gist_why'
MESSENGER_ROLES = frozenset({
    'child', 'grandchild', 'spouse', 'neighbor', 'village_cadre', 'village_doctor', 'self',
})
CHANNELS = frozenset({'wechat_text', 'wechat_voice', 'phone_call', 'in_person', 'wxpusher'})
SCENARIOS = frozenset({'heat', 'cold', 'normal'})

SCRIPT_VERSIONS = {
    'v1_official': {
        'heat': '【高温预警】今天最高 {tmax} 度，{window} 高温。建议：1）中午避免外出和田间劳动；2）多喝水；3）到阴凉或有空调处休息；4）不适及时联系家人。',
        'cold': '【低温提醒】今天最低 {tmin} 度。建议：1）少出门，注意保暖防滑；2）室内保暖；3）不适及时联系家人。',
        'normal': '【日常提醒】天气有变化，注意劳逸结合，出门记得带水或外套。不适及时联系家人。',
    },
    'v2_gist_why': {
        'heat': '{elder_call}，今天就记一件事：{window} 别下地、别出门。为什么——今天 {tmax} 度，这个时候太阳最毒，中午出汗多容易头晕摔倒。水放在手边，多喝几口。',
        'cold': '{elder_call}，今天就记一件事：少出门、穿暖和。为什么——最低 {tmin} 度，地面容易滑，着凉后更不舒服。',
        'normal': '{elder_call}，今天天气有变化，出门慢一点，水或外套带上。这是行动提醒，不提供医疗建议。',
    },
    'v3_kin_time': {
        'heat': '{elder_call}，我是{messenger_self}。今天 {tmax} 度，{window} 这段你就在家歇着，别去地里。空调开 28 度就行，一天电费不到一块钱，别省。我 {callback_time} 再给你打电话。',
        'cold': '{elder_call}，我是{messenger_self}。今天最低 {tmin} 度，你就在家歇着，出门把衣裳穿好。我 {callback_time} 再给你打电话。',
        'normal': '{elder_call}，我是{messenger_self}。今天天气有变化，出门慢一点。我 {callback_time} 再给你打电话。',
    },
}


def script_catalog():
    payload = {
        'default': DEFAULT_SCRIPT_VERSION,
        'versions': SCRIPT_VERSIONS,
        'messenger_roles': sorted(MESSENGER_ROLES),
        'channels': sorted(CHANNELS),
        'scenarios': sorted(SCENARIOS),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    payload['version_hash'] = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    payload['schema_version'] = '2026-09-06.scripts-v1'
    return payload


def render_script(version, scenario, **placeholders):
    version = version or DEFAULT_SCRIPT_VERSION
    scenario = scenario if scenario in SCENARIOS else 'normal'
    templates = SCRIPT_VERSIONS.get(version) or SCRIPT_VERSIONS[DEFAULT_SCRIPT_VERSION]
    text = templates.get(scenario) or templates['normal']
    defaults = {
        'elder_call': '家里',
        'tmax': '--',
        'tmin': '--',
        'window': '中午前后',
        'messenger_self': '家里人',
        'callback_time': '傍晚',
        'action_1': '少出门',
        'action_2': '多喝水',
    }
    defaults.update({k: v for k, v in placeholders.items() if v not in (None, '')})
    try:
        return text.format(**defaults)
    except KeyError:
        return text
