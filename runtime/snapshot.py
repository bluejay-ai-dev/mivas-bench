"""Freeze a call's GET /state dump on the tools replica at teardown.

Evals load ``GET /snapshot/{id}`` (Ingress → tools ClusterIP). They do not
need to know which harness pod owned the WebSocket. Replica death after this
write is fine: the JSON lives next to that call's SQLite file.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def capture_final(call_id: str) -> dict[str, Any] | None:
    """GET /state for this id, then POST /snapshot so evals can read it back."""
    cid = str(call_id or "").strip()
    if not cid:
        return None
    base = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    if not base:
        return None
    q = urllib.parse.urlencode({"call_id": cid})
    try:
        with urllib.request.urlopen(f"{base}/state?{q}", timeout=5) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"snapshot: GET /state failed sim={cid} err={e}", flush=True)
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"snapshot: GET /state not json sim={cid} err={e}", flush=True)
        return None
    if not isinstance(state, dict):
        print(f"snapshot: GET /state not an object sim={cid}", flush=True)
        return None
    body = json.dumps({"call_id": cid, "state": state}).encode()
    req = urllib.request.Request(
        f"{base}/snapshot",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"snapshot: POST /snapshot failed sim={cid} err={e}", flush=True)
        return None
    print(f"snapshot: final sim={cid}", flush=True)
    return state
