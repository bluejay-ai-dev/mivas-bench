"""Industry tool dispatch: POST {TOOL_SERVER_URL}/tools/{name} with the call id header.

Handoffs and ``end_call`` never reach the tool server (see live.py). Every other
blueprint tool, including human-transfer session tools, is a dumb-pipe POST and
the server's JSON envelope is returned to the backend model verbatim.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import call_session, headers as call_headers, set_call_id  # noqa: E402,F401

TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


async def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TOOL_SERVER_URL}/tools/{name}",
            json={"arguments": arguments},
            headers=call_headers(),
        )
    try:
        body = resp.json()
    except ValueError:
        body = {"success": False, "error": f"tool server {resp.status_code}: {resp.text[:200]}"}
    return body if isinstance(body, dict) else {"result": body}
