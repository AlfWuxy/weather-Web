# -*- coding: utf-8 -*-
"""Load caregiver reminder scripts from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_SCRIPTS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'caregiver_tip_scripts.json'


@lru_cache(maxsize=1)
def load_caregiver_tip_scripts():
    payload = json.loads(_SCRIPTS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('caregiver_tip_scripts.json must be an object')
    return payload


def format_caregiver_script(
    *,
    kind,
    address='你',
    location='',
    tmax=None,
    tmin=None,
    action_link='',
    short_code='',
    extra_lines=None,
):
    tips = load_caregiver_tip_scripts()
    lines = []
    if kind == 'weather_unavailable':
        block = tips['weather_unavailable']
        lines.append(block['title'])
        lines.append(block['lead'])
    else:
        block = tips.get(kind) or tips['daily']
        lines.append(block['title'])
        lead = block['lead'].format(address=address)
        if kind == 'cold' and tmin:
            lead += block.get('temp_clause', '').format(tmin=tmin)
            if not lead.endswith('。'):
                lead += '。'
        elif kind == 'heat' and tmax:
            lead += block.get('temp_clause', '').format(tmax=tmax)
            if not lead.endswith('。'):
                lead += '。'
        elif kind in ('cold', 'heat') and not lead.endswith('。'):
            lead += '。'
        lines.append(lead)
        advice = block.get('advice')
        if advice:
            lines.append(advice)

    if location:
        lines.append(tips['location_line'].format(location=location))
    if extra_lines:
        lines.extend([line for line in extra_lines if line])
    if kind != 'weather_unavailable':
        lines.append(tips['disclaimer'])
    lines.append(tips['action_line'].format(action_link=action_link, short_code=short_code))
    return '\n'.join([line for line in lines if line])
