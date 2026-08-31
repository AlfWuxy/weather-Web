# -*- coding: utf-8 -*-
"""首页轻量化与无障碍回归测试。"""
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIFESTYLE_DIR = PROJECT_ROOT / 'static' / 'illustrations' / 'lifestyle'
BRAND_DIR = PROJECT_ROOT / 'static' / 'brand'
LIFESTYLE_STEMS = (
    'home-hero-elder-window',
    'weather-action-water-phone',
    'family-care-video-call',
    'community-cooling-room',
)


def _relative_luminance(hex_color):
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first, second):
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_home_uses_responsive_avif_webp_with_jpeg_fallback(client):
    response = client.get('/')
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert body.count('type="image/avif"') == 6
    assert body.count('type="image/webp"') == 5
    assert 'rel="preload"' in body
    assert 'type="image/avif"' in body
    assert 'imagesrcset=' in body
    preload_tag = re.search(r'<link rel="preload"[^>]+>', body)
    assert preload_tag is not None
    assert ' href=' not in preload_tag.group(0)
    assert '(max-width: 680px) calc(100vw - 48px)' in body
    assert 'fetchpriority="high"' in body
    assert 'loading="eager"' in body
    assert 'class="yl-lifestyle-hero in-view"' in body
    assert 'data-home-critical-motion' in body
    assert body.count('fetchpriority="low"') == 4
    assert body.count('loading="lazy"') >= 4
    for stem in LIFESTYLE_STEMS:
        assert f'{stem}-640.avif' in body
        assert f'{stem}-1280.avif' in body
        assert f'{stem}-640.webp' in body
        assert f'{stem}-1280.webp' in body
        assert f'{stem}.jpg' in body


def test_home_omits_unused_global_payloads(client):
    response = client.get('/')
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert 'metricExplanationCatalog' not in body
    assert 'metric-explanations.js' not in body
    assert 'clipboard.js' not in body
    assert 'yilao-data-fx.css' not in body
    assert 'yilao-data-fx-extra.css' not in body
    assert 'yilao-data-fx.js' not in body
    assert 'yilao-data-fx-extra.js' not in body
    assert len(body.encode('utf-8')) < 45_000


def test_optimized_home_assets_remain_small_and_servable(client):
    avif_paths = []
    webp_paths = []
    for stem in LIFESTYLE_STEMS:
        avif_paths.extend((
            LIFESTYLE_DIR / f'{stem}-640.avif',
            LIFESTYLE_DIR / f'{stem}-1280.avif',
        ))
        webp_paths.extend((
            LIFESTYLE_DIR / f'{stem}-640.webp',
            LIFESTYLE_DIR / f'{stem}-1280.webp',
        ))

    brand_paths = (
        BRAND_DIR / 'yilao-avatar-64.webp',
        BRAND_DIR / 'yilao-favicon-64.png',
        BRAND_DIR / 'yilao-apple-touch-icon-180.png',
    )
    assert all(path.stat().st_size < 45_000 for path in avif_paths)
    assert sum(path.stat().st_size for path in avif_paths) < 180_000
    assert all(path.stat().st_size < 70_000 for path in webp_paths)
    assert sum(path.stat().st_size for path in webp_paths) < 300_000
    assert all(path.stat().st_size < 10_000 for path in brand_paths)

    for path in (*avif_paths, *webp_paths, *brand_paths):
        relative_path = path.relative_to(PROJECT_ROOT / 'static')
        response = client.get(f'/static/{relative_path.as_posix()}')
        assert response.status_code == 200
        expected_types = {
            '.avif': 'image/avif',
            '.webp': 'image/webp',
            '.png': 'image/png',
        }
        expected_type = expected_types[path.suffix]
        assert response.content_type.startswith(expected_type)


def test_home_defers_icon_font_without_changing_other_pages(client):
    home = client.get('/').get_data(as_text=True)
    transparency = client.get('/transparency').get_data(as_text=True)
    icon_url = 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css'
    motion_url = '/static/css/yilao-motion.css'

    assert icon_url in home
    assert 'media="print" onload="this.onload=null;this.media=\'all\'"' in home
    assert f'<noscript><link rel="stylesheet" href="{icon_url}"></noscript>' in home
    assert f'<link rel="stylesheet" href="{motion_url}' in home
    assert f'<noscript><link rel="stylesheet" href="{motion_url}' in home
    assert home.count('media="print" onload="this.onload=null;this.media=\'all\'"') == 2
    assert 'data-inline-menu-icon' in home
    assert icon_url in transparency
    assert motion_url in transparency
    assert 'media="print"' not in transparency
    assert 'data-inline-menu-icon' not in transparency
    assert 'class="bi bi-list"' in transparency


def test_authenticated_home_keeps_icon_font_synchronous(authenticated_client):
    body = authenticated_client.get('/').get_data(as_text=True)
    icon_url = 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css'
    icon_links = re.findall(r'<link rel="stylesheet"[^>]+bootstrap-icons[^>]+>', body)
    motion_links = re.findall(r'<link rel="stylesheet"[^>]+yilao-motion\.css[^>]+>', body)

    assert len(icon_links) == 1
    assert len(motion_links) == 1
    assert icon_url in icon_links[0]
    assert 'media="print"' not in icon_links[0]
    assert 'media="print"' not in motion_links[0]
    assert 'data-inline-menu-icon' not in body
    assert 'data-home-critical-motion' not in body
    assert 'class="yl-lifestyle-hero yl-fade-up"' in body
    assert 'class="bi bi-list"' in body


def test_base_avoids_legacy_loader_and_geometry_reflow_hooks():
    source = (PROJECT_ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')

    assert 'animations.css' not in source
    assert 'page-transitions.css' not in source
    assert 'pageLoader' not in source
    assert 'hideLoader' not in source
    assert 'visualViewport' not in source
    assert 'innerWidth' not in source
    assert '--app-vw' not in source
    assert '--app-vh' not in source
    assert '--app-scale' not in source
    assert "matchMedia('(max-width: 680px)')" in source
    assert "matchMedia('(max-width: 980px)')" in source


def test_story_step_number_meets_wcag_text_contrast():
    css = (PROJECT_ROOT / 'static' / 'css' / 'yilao.css').read_text(encoding='utf-8')
    rule = re.search(r'\.yl-story-copy span\s*\{(?P<body>[^}]*)\}', css)
    assert rule is not None
    color = re.search(r'color:\s*(#[0-9A-Fa-f]{6})', rule.group('body'))
    assert color is not None
    assert _contrast_ratio(color.group(1), '#FFFFFF') >= 4.5
