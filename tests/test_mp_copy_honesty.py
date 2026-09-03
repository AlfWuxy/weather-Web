# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mp_alerts_wxml_does_not_promise_threshold_temps_when_weather_missing():
    text = (ROOT / 'miniprogram' / 'pages' / 'alerts' / 'index.wxml').read_text(encoding='utf-8')
    assert 'weather.weather_available' in text
    assert '天气更新中' in text
    assert '仍会展示温度阈值信息供参考' not in text


def test_mp_alerts_missing_pair_is_not_shown_as_empty_warnings():
    wxml = (ROOT / 'miniprogram' / 'pages' / 'alerts' / 'index.wxml').read_text(encoding='utf-8')
    js = (ROOT / 'miniprogram' / 'pages' / 'alerts' / 'index.js').read_text(encoding='utf-8')
    assert 'missingPairId' in js
    assert '缺少照护对象' in wxml
    assert 'missingPairId' in wxml


def test_mp_bind_token_explains_missing_api_base():
    text = (ROOT / 'miniprogram' / 'pages' / 'bind-token' / 'index.js').read_text(encoding='utf-8')
    assert 'miniapp_api_base_missing' in text
    assert '未配置 API 地址' in text


def test_mp_elders_does_not_imply_thresholds_always_apply():
    text = (ROOT / 'miniprogram' / 'pages' / 'elders' / 'index.wxml').read_text(encoding='utf-8')
    assert '天气可用时才套用温度阈值' in text
    assert '官方预警 + 阈值规则' not in text


def test_mp_elders_uses_web_no_alert_copy_not_normal():
    wxml = (ROOT / 'miniprogram' / 'pages' / 'elders' / 'index.wxml').read_text(encoding='utf-8')
    js = (ROOT / 'miniprogram' / 'pages' / 'elders' / 'index.js').read_text(encoding='utf-8')
    assert '暂无预警' in wxml
    assert '>正常<' not in wxml
    assert '监测对象' not in wxml
    assert '照护对象' in wxml
    assert 'Math.round' in js


def test_mp_template_missing_pair_is_not_blank_temps():
    wxml = (ROOT / 'miniprogram' / 'pages' / 'template' / 'index.wxml').read_text(encoding='utf-8')
    js = (ROOT / 'miniprogram' / 'pages' / 'template' / 'index.js').read_text(encoding='utf-8')
    assert 'missingPairId' in js
    assert '缺少照护对象' in wxml
    assert 'missingPairId' in wxml
    assert '可信节点' not in wxml
