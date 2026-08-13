"""Bluejay simulation result id → X-Mivas-Call-Id for industry tool POSTs.

CHIRP / LiveKit job metadata supplies X-Simulation-Result-Id. Platform
webhooks are a separate HTTP request and must look the id up from the
provider call id (Vapi call.id, Retell call_id, …).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextvars import ContextVar
from typing import Any

HEADER = "X-Mivas-Call-Id"
CALL_ID: ContextVar[str] = ContextVar("mivas_call_id", default="")

_lock = threading.Lock()
_provider_sim: dict[str, str] = {}
_sessions: dict[str, str] = {}


def set_call_id(sim_id: str | None) -> str:
    """Pin this task to a conversation id. Mints `call_{uuid}` if Bluejay omitted it."""
    resolved = str(sim_id).strip() if sim_id is not None else ""
    if not resolved:
        resolved = f"call_{uuid.uuid4().hex[:12]}"
        print(
            f"call_id: Bluejay omitted X-Simulation-Result-Id; minted {resolved}",
            flush=True,
        )
    CALL_ID.set(resolved)
    return resolved


def current() -> str:
    return CALL_ID.get()


def pod_name() -> str:
    """Kubernetes sets HOSTNAME to the pod name."""
    return os.environ.get("HOSTNAME") or "local"


def log_ws_accept(sim_id: str | None) -> None:
    print(f"call_id: ws_accept sim={sim_id or '-'} pod={pod_name()}", flush=True)


def log_tool_post(sim_id: str, *, path: str = "") -> None:
    extra = f" path={path}" if path else ""
    print(f"call_id: tool_post sim={sim_id} pod={pod_name()}{extra}", flush=True)


def headers(call_id: str | None = None) -> dict[str, str]:
    """Always returns X-Mivas-Call-Id; never sends an empty header."""
    cid = (call_id or CALL_ID.get() or sole_session() or "").strip()
    if not cid:
        cid = set_call_id(None)
    return {HEADER: cid}


def begin_session(sim_id: str | None, *, session_key: str | None = None) -> str:
    """Register an in-flight call so a webhook with no ContextVar can find it."""
    resolved = set_call_id(sim_id)
    key = session_key or resolved
    with _lock:
        _sessions[str(key)] = resolved
    return resolved


def end_session(session_key: str) -> None:
    with _lock:
        sim = _sessions.pop(str(session_key), None)
    if sim:
        try:
            from snapshot import capture_final

            capture_final(sim)
        except Exception as e:
            print(f"snapshot: capture failed sim={sim} err={e}", flush=True)


def sole_session() -> str | None:
    """If exactly one in-flight session exists, return its Bluejay id."""
    with _lock:
        ids = set(_sessions.values())
    if len(ids) == 1:
        return next(iter(ids))
    return None


def _tools_cluster_url() -> str:
    """In-cluster tools Service base URL, or empty when tools are local."""
    url = os.environ.get("TOOL_SERVER_URL", "").strip().rstrip("/")
    if not url:
        return ""
    host = url.split("://", 1)[-1]
    if host.startswith("127.0.0.1") or host.startswith("localhost"):
        return ""
    return url


def _publish_bind(provider_call_id: str, sim_id: str) -> None:
    base = _tools_cluster_url()
    if not base:
        return
    body = json.dumps({"provider_call_id": provider_call_id, "sim_id": sim_id}).encode()
    req = urllib.request.Request(
        f"{base}/bind",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"call_id: bind publish failed provider={provider_call_id} err={e}", flush=True)


def _lookup_bind(provider_call_id: str) -> str | None:
    base = _tools_cluster_url()
    if not base:
        return None
    req = urllib.request.Request(
        f"{base}/bind/{urllib.parse.quote(provider_call_id, safe='')}"
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    found = str(data.get("sim_id") or "").strip()
    return found or None


def bind_provider(provider_call_id: str | None, sim_id: str | None = None) -> str:
    """Map a provider conversation id (Vapi/Retell/…) to the Bluejay sim id."""
    resolved = (str(sim_id).strip() if sim_id is not None else "") or current()
    if not resolved:
        resolved = set_call_id(None)
    CALL_ID.set(resolved)
    if provider_call_id:
        with _lock:
            _provider_sim[str(provider_call_id)] = resolved
        _publish_bind(str(provider_call_id), resolved)
    return resolved


def unbind_provider(provider_call_id: str | None) -> None:
    if not provider_call_id:
        return
    with _lock:
        _provider_sim.pop(str(provider_call_id), None)


def for_provider(provider_call_id: str | None) -> str:
    """Resolve a webhook's provider call id to the Bluejay sim id; set CALL_ID."""
    if provider_call_id:
        with _lock:
            found = _provider_sim.get(str(provider_call_id))
        if found:
            CALL_ID.set(found)
            return found
        remote = _lookup_bind(str(provider_call_id))
        if remote:
            with _lock:
                _provider_sim[str(provider_call_id)] = remote
            CALL_ID.set(remote)
            return remote
    fallback = sole_session()
    if fallback:
        if provider_call_id:
            print(
                f"call_id: unknown provider call {provider_call_id}; "
                f"using sole in-flight session {fallback}",
                flush=True,
            )
        CALL_ID.set(fallback)
        return fallback
    print(
        f"call_id: no bound provider/session for {provider_call_id!r}; minting",
        flush=True,
    )
    return set_call_id(None)


def provider_id_from_payload(payload: Any) -> str | None:
    """Best-effort extract of a provider conversation id from a webhook body."""
    if not isinstance(payload, dict):
        return None
    call = payload.get("call")
    if isinstance(call, dict):
        for key in ("id", "call_id", "callId"):
            val = call.get(key)
            if val not in (None, ""):
                return str(val)
    for key in ("call_id", "callId", "conversation_id", "inbound_id", "cid"):
        val = payload.get(key)
        if val not in (None, "") and not isinstance(val, dict):
            return str(val)
    return None


def provider_id_from_request(
    payload: Any = None,
    *,
    query: Any = None,
    headers: Any = None,
) -> str | None:
    """Provider id from JSON body, query string, or common webhook headers."""
    found = provider_id_from_payload(payload)
    if found:
        return found
    if query is not None:
        getter = query.get if hasattr(query, "get") else lambda _k: None
        for key in ("call_id", "callId", "inbound_id", "cid"):
            val = getter(key)
            if val not in (None, ""):
                return str(val)
    if headers is not None:
        getter = headers.get if hasattr(headers, "get") else lambda _k: None
        for key in (
            "x-call-id",
            "x-bland-call-id",
            "x-cartesia-call-id",
            "x-vapi-call-id",
        ):
            val = getter(key)
            if val not in (None, ""):
                return str(val)
    return None


def reset() -> None:
    """Clear maps and the ContextVar. Tests only."""
    CALL_ID.set("")
    with _lock:
        _provider_sim.clear()
        _sessions.clear()
