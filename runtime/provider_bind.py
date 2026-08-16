"""Provider call id → simulation result id, for platform webhooks.

Vapi/Retell/Bland/Cartesia tool webhooks do not inherit the CHIRP connection.
The adapter publishes the mapping here; a later webhook on this process looks
it up. In-memory is enough: tools run in-pod with the adapter.
"""

from __future__ import annotations

import threading

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_store: dict[str, str] = {}
_lock = threading.Lock()


async def _bind(request: Request) -> JSONResponse:
    data = await request.json()
    pid = str((data or {}).get("provider_call_id") or "").strip()
    sid = str((data or {}).get("sim_id") or "").strip()
    if not pid or not sid:
        return JSONResponse(
            {"detail": "provider_call_id and sim_id required"}, status_code=400
        )
    with _lock:
        _store[pid] = sid
    return JSONResponse({"ok": "true", "provider_call_id": pid, "sim_id": sid})


async def _lookup(request: Request) -> JSONResponse:
    pid = str(request.path_params.get("provider_call_id") or "").strip()
    with _lock:
        found = _store.get(pid)
    if not found:
        return JSONResponse({"detail": "unknown provider call id"}, status_code=404)
    return JSONResponse({"provider_call_id": pid, "sim_id": found})


def mount(app) -> None:
    app.router.routes.extend(
        [
            Route("/bind", _bind, methods=["POST"]),
            Route("/bind/{provider_call_id}", _lookup, methods=["GET"]),
        ]
    )
