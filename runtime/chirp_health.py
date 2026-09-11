"""Plain HTTP on the CHIRP port: GET /health → 200, anything else non-WebSocket → 404.

Platforms that can only probe the port the WebSocket listens on (Baseten's
docker_server readiness/liveness) need this; Kubernetes keeps probing the
in-pod tool server on :8000. Pass as ``serve(..., process_request=process_request)``
(websockets>=14 asyncio server).
"""

from __future__ import annotations

from http import HTTPStatus

HEALTH_PATHS = frozenset({"/health", "/healthz"})


def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path.split("?", 1)[0].rstrip("/") or "/"
    if path in HEALTH_PATHS:
        return connection.respond(HTTPStatus.OK, '{"status":"ok"}\n')
    return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
