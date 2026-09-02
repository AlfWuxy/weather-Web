# -*- coding: utf-8 -*-
"""Load cooling-page copy from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_COPY_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'cooling_page.json'


@lru_cache(maxsize=1)
def load_cooling_page_copy():
    payload = json.loads(_COPY_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('cooling_page.json must be an object')
    required = ('kicker', 'title', 'lead')
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f'cooling_page.json missing: {", ".join(missing)}')
    return {key: payload[key] for key in required}
