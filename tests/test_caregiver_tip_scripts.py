# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_miniprogram_elder_copy_does_not_invent_web_family_or_tip_personalization():
    elders = (ROOT / 'miniprogram' / 'pages' / 'elders' / 'index.wxml').read_text(encoding='utf-8')
    edit = (ROOT / 'miniprogram' / 'pages' / 'elder-edit' / 'index.wxml').read_text(encoding='utf-8')

    assert '网页端添加' not in elders
    assert '照护工作台' in elders
    assert '提醒话术' not in edit
    web = (ROOT / 'data' / 'content' / 'caregiver_tip_scripts.json').read_text(encoding='utf-8')
    mp = (ROOT / 'miniprogram' / 'content' / 'caregiver_tip_scripts.json').read_text(encoding='utf-8')
    assert web == mp


def test_caregiver_script_builder_covers_heat_cold_and_waiting():
    from core.caregiver_scripts import format_caregiver_script

    heat = format_caregiver_script(
        kind='heat',
        address='妈',
        location='都昌',
        tmax='36',
        action_link='https://example.test/a',
        short_code='12345678',
    )
    assert '【高温行动提醒】' in heat
    assert '最高约 36°C' in heat
    assert '都昌' in heat

    cold = format_caregiver_script(
        kind='cold',
        address='爸',
        location='都昌',
        tmin='2',
        action_link='https://example.test/a',
        short_code='12345678',
    )
    assert '【寒潮行动提醒】' in cold
    assert '最低约 2°C' in cold

    waiting = format_caregiver_script(
        kind='weather_unavailable',
        address='妈',
        location='都昌',
        action_link='https://example.test/a',
        short_code='12345678',
    )
    assert '【天气更新中】' in waiting
    assert '风险等级暂不显示' in waiting


def test_web_helpers_use_shared_caregiver_scripts():
    from types import SimpleNamespace

    from services.user._helpers import _build_caregiver_message
    from services.user.caregiver_service import _build_weather_waiting_message

    pair = SimpleNamespace(short_code='12345678', location_query='都昌', community_code='都昌')
    waiting = _build_weather_waiting_message(pair, 'https://example.test/a')
    assert '【天气更新中】' in waiting
    assert '风险等级暂不显示' in waiting

    heat = _build_caregiver_message(
        pair,
        alert_kind='heat',
        weather_data={'temperature_max': 36, 'temperature_min': 26},
        member=None,
        action_link='https://example.test/a',
    )
    assert '【高温行动提醒】' in heat
    assert '最高约 36°C' in heat


def test_mp_template_requires_shared_caregiver_scripts():
    text = (ROOT / 'miniprogram' / 'pages' / 'template' / 'index.js').read_text(encoding='utf-8')
    assert "require('../../content/caregiver_tip_scripts.json')" in text