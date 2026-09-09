# -*- coding: utf-8 -*-
"""Static copy for the public /status boundary page.

Content is Python constants so tests can assert exact wording. It is not
loaded from the database.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]

ENGLISH_SUMMARY = [
    'Prototype heat-warning tool for older adults in Duchang County, Jiangxi, relayed through family members. Not a clinical or emergency service.',
    'Currently at feasibility stage: the elder action chain, messenger-script test, and cooling-site verification are being built and tested with volunteers.',
    'No claims about users, adoption, or health outcomes. Everything verified, prototyped, or untested is listed below. Source: GitHub link at the bottom.',
]

ONE_LINE_ZH = (
    '宜老天气通是一个原型：把高温、寒潮预警翻译成老人今天能做的少数几件事，'
    '由家属转述，老人用三个按钮回应。仍是原型，未被采用（still a prototype, not adopted）。'
)

STAGES = ('basic', 'feasibility', 'efficacy', 'effectiveness', 'impact')

_PROTOTYPE_NOTE = '合成用户 / 志愿者可用性测试；无真实高温期数据'

VERIFIED = [
    {
        'label': '官方预警与天气来源',
        'note': '和风 / Open-Meteo，含 fail-closed。',
        'stage': 'basic',
    },
    {
        'label': '1 km 热暴露 GIS 的数据口径',
        'note': 'MODIS LST、65+ 人口比例与复核门槛。',
        'stage': 'basic',
    },
    {
        'label': '卫生室就诊—气象关联的离线分析边界',
        'note': '仅离线去标识汇总，不外推到个体。',
        'stage': 'basic',
    },
]

PROTOTYPE = [
    {
        'label': '老人三按钮行动链',
        'note': _PROTOTYPE_NOTE,
        'stage': 'feasibility',
    },
    {
        'label': '家属核验 / 求助接收 / 结案',
        'note': _PROTOTYPE_NOTE,
        'stage': 'feasibility',
    },
    {
        'label': '避暑资源核验字段与反馈',
        'note': _PROTOTYPE_NOTE,
        'stage': 'feasibility',
    },
    {
        'label': '话术版本记录与 teach-back',
        'note': _PROTOTYPE_NOTE,
        'stage': 'feasibility',
    },
    {
        'label': '小程序家属端',
        'note': _PROTOTYPE_NOTE,
        'stage': 'feasibility',
    },
]

UNVERIFIED = [
    {
        'label': '老人是否因预警而行动',
        'note': '尚未在真实高温期收集证据。',
        'stage': 'feasibility',
    },
    {
        'label': '家属转述是否提高理解',
        'note': '尚未在真实高温期收集证据。',
        'stage': 'feasibility',
    },
    {
        'label': '任何健康结局',
        'note': '本原型不测量、不声称健康结局。',
        'stage': 'feasibility',
    },
    {
        'label': '资源点在高温警报期的真实开放情况',
        'note': '台账字段已设计，现场开放状态未经高温期核验。',
        'stage': 'feasibility',
    },
]

NO_GO = [
    {
        'label': '个体疾病预测',
        'note': '不在本原型范围内。',
        'stage': 'basic',
    },
    {
        'label': 'AI 自动健康建议',
        'note': '不在本原型范围内。',
        'stage': 'basic',
    },
    {
        'label': '医疗判断',
        'note': '不在本原型范围内。',
        'stage': 'basic',
    },
    {
        'label': '宣称用户数、覆盖村数、部署数',
        'note': '不在本原型范围内。',
        'stage': 'basic',
    },
]

DATA_SOURCES = [
    '天气 / 预警来源：和风、Open-Meteo（fail-closed）。',
    '健康数据仅离线去标识汇总。',
    '行动链事件为匿名 pair 级别，不含姓名、电话、自由文本。',
    '流量统计 2026-08-12 至 08-16 含自动化测试，不代表使用。',
]

FORBIDDEN_TERMS = [
    'Cornell',
    'users',
    'deployed',
    'adopted',
    'launched',
    '已保护',
    '已覆盖',
    '41.4k',
    'stars',
]

GITHUB_URL = 'https://github.com/AlfWuxy/weather-Web'


def get_version():
    """Return `git describe --tags --always`, or `dev` if git is unavailable."""
    try:
        completed = subprocess.run(
            ['git', 'describe', '--tags', '--always'],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 'dev'
    version = (completed.stdout or '').strip()
    if completed.returncode != 0 or not version:
        return 'dev'
    return version


VERSION = get_version()
BUILD_DATE = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
