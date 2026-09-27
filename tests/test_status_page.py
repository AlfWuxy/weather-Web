# -*- coding: utf-8 -*-
"""PRD-02 /status page, boundary copy, and anonymous nav contracts."""
from __future__ import annotations

import re
from unittest.mock import Mock

from core.status_content import (
    ENGLISH_SUMMARY,
    FORBIDDEN_TERMS,
    GITHUB_URL,
    NO_GO,
    ONE_LINE_ZH,
    PROTOTYPE,
    STAGES,
    UNVERIFIED,
    VERIFIED,
    get_version,
)


def _scrub_allowed_boundary_copy(html):
    """Remove required boundary sentences that legitimately mention forbidden stems."""
    cleaned = html
    for line in ENGLISH_SUMMARY:
        cleaned = cleaned.replace(line, '')
    return cleaned.replace(ONE_LINE_ZH, '')


def _assert_no_forbidden_terms(html):
    scrubbed = _scrub_allowed_boundary_copy(html)
    for term in FORBIDDEN_TERMS:
        assert term not in scrubbed, f'forbidden term {term!r} found in HTML'


def test_status_content_uses_allowed_stages_only():
    for bucket in (VERIFIED, PROTOTYPE, UNVERIFIED, NO_GO):
        for item in bucket:
            assert set(item) == {'label', 'note', 'stage'}
            assert item['stage'] in STAGES
            assert item['stage'] in {'basic', 'feasibility'}


def test_status_page_returns_200_with_english_lines_and_one_liner(client):
    response = client.get('/status')
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert 'name="robots" content="index,follow"' in body
    for line in ENGLISH_SUMMARY:
        assert line in body
    assert ONE_LINE_ZH in body
    assert '仍是原型，未被采用（still a prototype, not adopted）' in body
    assert 'VERIFIED' in body
    assert 'PROTOTYPE' in body
    assert 'UNVERIFIED' in body
    assert 'NO-GO' in body
    assert GITHUB_URL in body
    assert '/transparency' in body
    assert '/transparency#privacy' in body
    _assert_no_forbidden_terms(body)


def test_home_html_contains_boundary_line_and_no_forbidden_terms(client):
    response = client.get('/')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ONE_LINE_ZH in body
    _assert_no_forbidden_terms(body)


def test_anonymous_nav_has_four_primary_items_plus_research_dropdown(client):
    body = client.get('/').get_data(as_text=True)
    desktop = body.split('class="app-desktop-nav', 1)[1].split('id="appNavDrawer"', 1)[0]
    primaries = re.findall(r'data-nav-primary="([^"]+)"', desktop)
    assert primaries == ['today', 'action', 'cooling', 'status']
    assert '研究与方法（探索性）' in desktop
    assert 'data-nav-more-trigger="desktop"' in desktop
    assert 'id="research-methods"' in desktop
    assert 'data-nav-key="care"' not in desktop
    assert 'href="/risk"' in desktop
    assert 'href="/action"' in desktop
    assert 'href="/cooling"' in desktop
    assert 'href="/status"' in desktop


def test_get_version_falls_back_to_dev_when_git_unavailable(monkeypatch):
    import core.status_content as status_content

    def boom(*args, **kwargs):
        raise FileNotFoundError('git')

    monkeypatch.setattr(status_content.subprocess, 'run', boom)
    assert status_content.get_version() == 'dev'

    failed = Mock(returncode=1, stdout='', stderr='fatal')
    monkeypatch.setattr(status_content.subprocess, 'run', lambda *args, **kwargs: failed)
    assert status_content.get_version() == 'dev'
