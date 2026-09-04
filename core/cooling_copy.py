# -*- coding: utf-8 -*-
"""Load cooling-page copy from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_COPY_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'cooling_page.json'
_BAND_KEYS = ('status', 'status_class', 'title', 'detail')


@lru_cache(maxsize=1)
def load_cooling_page_copy():
    payload = json.loads(_COPY_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('cooling_page.json must be an object')
    required = ('kicker', 'title', 'lead', 'footer', 'bands')
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f'cooling_page.json missing: {", ".join(missing)}')
    if not isinstance(payload['bands'], list):
        raise ValueError('cooling_page.json bands must be a list')
    bands = []
    for raw in payload['bands']:
        if not isinstance(raw, dict):
            raise ValueError('cooling_page.json bands must contain objects')
        missing_band = [key for key in _BAND_KEYS if not raw.get(key)]
        if missing_band:
            raise ValueError(f'cooling_page.json band missing: {", ".join(missing_band)}')
        band_min = raw.get('min')
        if band_min is not None:
            try:
                band_min = float(band_min)
            except (TypeError, ValueError) as exc:
                raise ValueError('cooling_page.json band min must be a number') from exc
        bands.append({
            'min': band_min,
            'status': raw['status'],
            'status_class': raw['status_class'],
            'title': raw['title'],
            'detail': raw['detail'],
        })
    bands.sort(key=lambda band: float('-inf') if band['min'] is None else band['min'], reverse=True)
    return {
        'kicker': payload['kicker'],
        'title': payload['title'],
        'lead': payload['lead'],
        'footer': payload['footer'],
        'bands': bands,
    }


def select_cooling_temperature_band(copy, outdoor_temp):
    """Pick the first matching temperature band; higher thresholds come first."""
    if outdoor_temp is None or not copy:
        return None
    try:
        temperature = float(outdoor_temp)
    except (TypeError, ValueError):
        return None
    for band in copy.get('bands') or []:
        band_min = band.get('min')
        if band_min is None or temperature >= band_min:
            return band
    return None
