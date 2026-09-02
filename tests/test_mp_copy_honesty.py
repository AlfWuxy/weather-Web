# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mp_alerts_wxml_does_not_promise_threshold_temps_when_weather_missing():
    text = (ROOT / 'miniprogram' / 'pages' / 'alerts' / 'index.wxml').read_text(encoding='utf-8')
    assert 'weather.weather_available' in text
    assert '天气更新中' in text
    assert '仍会展示温度阈值信息供参考' not in text


def test_mp_bind_token_explains_missing_api_base():
    text = (ROOT / 'miniprogram' / 'pages' / 'bind-token' / 'index.js').read_text(encoding='utf-8')
    assert 'miniapp_api_base_missing' in text
    assert '未配置 API 地址' in text
