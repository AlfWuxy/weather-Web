# -*- coding: utf-8 -*-
"""WxPusher client (pilot channel).

API docs vary by deployment; we use the common send endpoint:
POST {WXPUSHER_API_BASE}/send/message
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests
from flask import current_app, has_app_context

from services.external_api import record_external_api_timing as _record_external_api_timing

logger = logging.getLogger(__name__)


def _cfg(key: str, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def send(uid: str, title: str, content: str, url: Optional[str] = None) -> Dict[str, Any]:
    """Send a message to a single WxPusher UID.

    Returns:
      {ok: bool, msg_id?: str, error?: str, raw?: object}
    """
    uid = (str(uid).strip() if uid is not None else "")
    if not uid:
        return {"ok": False, "error": "missing uid"}

    app_token = (_cfg("WXPUSHER_APP_TOKEN") or "").strip()
    api_base = (_cfg("WXPUSHER_API_BASE") or "").strip() or "https://wxpusher.zjiecode.com/api"
    if not app_token:
        return {"ok": False, "error": "missing WXPUSHER_APP_TOKEN"}

    endpoint = f"{api_base.rstrip('/')}/send/message"
    payload = {
        "appToken": app_token,
        "content": content or "",
        "summary": (title or "")[:80],
        "contentType": 1,  # text
        "uids": [uid],
    }
    if url:
        payload["url"] = url

    start_ts = time.perf_counter()
    try:
        resp = requests.post(endpoint, json=payload, timeout=10)
        _record_external_api_timing("wxpusher_send", (time.perf_counter() - start_ts) * 1000, resp.status_code)
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.info("WxPusher send failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    # Typical success: {"code":1000,"msg":"处理成功","data":[{"uid":"...","status":"success","code":1000,"msg":"..."}]}
    code = data.get("code")
    if code in (1000, "1000"):
        msg_id = None
        try:
            items = data.get("data") or []
            if isinstance(items, list) and items:
                msg_id = str(items[0].get("messageId") or items[0].get("msgId") or "") or None
        except Exception:
            msg_id = None
        return {"ok": True, "msg_id": msg_id, "raw": data}

    return {"ok": False, "error": data.get("msg") or f"wxpusher code={code}", "raw": data}

