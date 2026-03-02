# -*- coding: utf-8 -*-
"""
Shared helpers for external API usage.
"""
def record_external_api_timing(name, elapsed_ms, status_code=None):
    try:
        from flask import has_request_context, g
        if not has_request_context():
            return
        timings = getattr(g, 'external_api_timings', [])
        timings.append({
            'service': name,
            'elapsed_ms': round(elapsed_ms, 2),
            'status': status_code
        })
        g.external_api_timings = timings
    except Exception:
        return
