# -*- coding: utf-8 -*-
"""WxPusher client (pilot channel).

API docs vary by deployment; we use the common send endpoint:
POST {WXPUSHER_API_BASE}/send/message
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

import requests
from flask import current_app, has_app_context

from services.external_api import record_external_api_timing as _record_external_api_timing

logger = logging.getLogger(__name__)
WXPUSHER_OFFICIAL_API_BASE = "https://wxpusher.zjiecode.com/api"
WXPUSHER_APP_TOKEN_PATTERN = re.compile(r"^AT_[A-Za-z0-9_-]{16,197}$")


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
    api_base = (_cfg("WXPUSHER_API_BASE") or "").strip() or WXPUSHER_OFFICIAL_API_BASE
    if not app_token:
        return {"ok": False, "error": "missing WXPUSHER_APP_TOKEN"}
    if not WXPUSHER_APP_TOKEN_PATTERN.fullmatch(app_token):
        return {"ok": False, "error": "invalid WXPUSHER_APP_TOKEN"}
    if api_base != WXPUSHER_OFFICIAL_API_BASE:
        # 凭证只允许发送到固定官方 origin，阻断错误配置导致的密钥外泄。
        return {"ok": False, "error": "invalid WXPUSHER_API_BASE"}

    endpoint = f"{api_base}/send/message"
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

    if not isinstance(data, dict):
        return {"ok": False, "error": "wxpusher invalid response", "raw": data}

    # 顶层成功只表示请求已处理，单 UID 的内层结果仍可能失败。
    code = data.get("code")
    if code in (1000, "1000"):
        items = data.get("data")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return {"ok": False, "error": "wxpusher empty delivery result", "raw": data}
        item = items[0]
        if str(item.get("uid") or "").strip() != uid:
            return {"ok": False, "error": "wxpusher uid mismatch", "raw": data}
        item_code = item.get("code")
        item_status = str(item.get("status") or "").strip().lower()
        item_ok = item_code in (1000, "1000") or item_status in {"success", "succeeded", "ok"}
        if not item_ok:
            item_error = (
                item.get("msg")
                or item.get("message")
                or f"wxpusher item code={item_code} status={item_status or 'unknown'}"
            )
            return {"ok": False, "error": str(item_error), "raw": data}
        msg_id = str(item.get("messageId") or item.get("msgId") or "") or None
        return {"ok": True, "msg_id": msg_id, "raw": data}

    return {"ok": False, "error": data.get("msg") or f"wxpusher code={code}", "raw": data}
