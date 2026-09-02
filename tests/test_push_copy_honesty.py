# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wxoa_landing_does_not_promise_push_without_setup():
    text = (ROOT / 'templates' / 'wxoa_landing.html').read_text(encoding='utf-8')
    assert '当出现预警时收到推送' not in text
    assert '开通推送' in text or 'WxPusher' in text or '个人设置' in text


def test_trust_network_cta_does_not_claim_push_is_already_on():
    text = (ROOT / 'templates' / 'about_trust_network.html').read_text(encoding='utf-8')
    assert '去添加老人并开启推送' not in text


def test_health_assessment_explains_community_proxy_in_score():
    text = (ROOT / 'templates' / 'health_assessment.html').read_text(encoding='utf-8')
    assert '社区参考' in text
    assert '一般参考，请结合实际情况使用' not in text
