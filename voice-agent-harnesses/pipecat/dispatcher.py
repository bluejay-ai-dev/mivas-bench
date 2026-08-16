"""Daily pinless SIP webhook → the Pipecat worker with the fewest active calls.

Daily (the SIP fabric) POSTs here when someone dials the static pinless URI.
This process does not run the bot. It resolves the headless worker Service
(`mivas-{slug}-pods`) to pod IPs, reads each replica's inflight count, and
POSTs `/dialin` on :8080 to the least-loaded one. A ClusterIP retry cannot
do this: the dial-in HTTP request ends in milliseconds while the call stays
on the pod. On 409/502/503 the next-lowest replica is tried. Do not hairpin
through the public hostname from inside the cluster.

    POST /dialin/{slug}   Daily room_creation_api
    GET  /health
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("mivas.pipecat.dispatcher")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RETRY_CODES = {409, 502, 503}
_DIALIN_PATH = "/dialin"
_HEALTH_PATH = "/health"


def _retries() -> int:
    return int(os.environ.get("PIPECAT_DISPATCH_RETRIES", "8"))


def _retry_s() -> float:
    return float(os.environ.get("PIPECAT_DISPATCH_RETRY_S", "0.15"))


def _worker_port() -> int:
    return int(os.environ.get("PIPECAT_WORKER_PORT", "8080"))


def worker_url(slug: str) -> str:
    template = os.environ.get(
        "PIPECAT_WORKER_URL_TEMPLATE",
        "http://mivas-{slug}:8080/dialin",
    )
    return template.replace("{slug}", slug)


def pods_host(slug: str) -> str:
    template = os.environ.get("PIPECAT_WORKER_PODS_TEMPLATE", "mivas-{slug}-pods")
    if not template.strip():
        return ""
    return template.replace("{slug}", slug)


def _http_host(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def pod_url(ip: str, path: str) -> str:
    return f"http://{_http_host(ip)}:{_worker_port()}{path}"


def pick_pod(
    healths: dict[str, tuple[int, int]],
    reserved: dict[str, int],
    tried: set[str],
) -> str | None:
    """Lowest inflight+reserved that is still under cap; else the least-loaded."""
    under: list[tuple[int, str]] = []
    overflow: list[tuple[int, str]] = []
    for ip, (inflight, cap) in healths.items():
        if ip in tried:
            continue
        effective = inflight + reserved.get(ip, 0)
        row = (effective, ip)
        if effective < cap:
            under.append(row)
        else:
            overflow.append(row)
    pool = under or overflow
    if not pool:
        return None
    pool.sort(key=lambda row: (row[0], row[1]))
    return pool[0][1]


async def resolve_pod_ips(host: str) -> list[str]:
    if not host:
        return []
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, _worker_port(), socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return []
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def _as_response(last: httpx.Response) -> Response:
    return Response(
        content=last.content,
        status_code=last.status_code,
        media_type=last.headers.get("content-type", "application/json"),
    )


def build_app() -> FastAPI:
    app = FastAPI(title="mivas pipecat dispatcher")
    reserved: dict[str, int] = {}
    pick_lock = asyncio.Lock()

    async def probe_healths(
        client: httpx.AsyncClient, ips: list[str]
    ) -> dict[str, tuple[int, int]]:
        async def one(ip: str) -> tuple[str, tuple[int, int] | None]:
            try:
                resp = await client.get(pod_url(ip, _HEALTH_PATH), timeout=2.0)
                data = resp.json()
                return ip, (
                    int(data.get("inflight", 0)),
                    max(1, int(data.get("max_inflight", 1))),
                )
            except (httpx.HTTPError, TypeError, ValueError, KeyError):
                return ip, None

        out: dict[str, tuple[int, int]] = {}
        for ip, score in await asyncio.gather(*[one(ip) for ip in ips]):
            if score is not None:
                out[ip] = score
        return out

    async def dispatch_pods(
        client: httpx.AsyncClient, slug: str, body: dict
    ) -> httpx.Response | None:
        ips = await resolve_pod_ips(pods_host(slug))
        if not ips:
            return None
        healths = await probe_healths(client, ips)
        if not healths:
            healths = {ip: (0, 10**9) for ip in ips}
        tried: set[str] = set()
        last: httpx.Response | None = None
        while True:
            async with pick_lock:
                ip = pick_pod(healths, reserved, tried)
                if ip is None:
                    return last
                reserved[ip] = reserved.get(ip, 0) + 1
                tried.add(ip)
            try:
                last = await client.post(pod_url(ip, _DIALIN_PATH), json=body)
            except httpx.HTTPError as e:
                logger.warning("worker %s: %s", ip, e)
                last = None
            finally:
                async with pick_lock:
                    reserved[ip] = max(0, reserved.get(ip, 0) - 1)
            if last is not None and last.status_code not in _RETRY_CODES:
                logger.info(
                    "worker slug=%s ip=%s status=%s inflight=%s",
                    slug,
                    ip,
                    last.status_code,
                    healths.get(ip, (None, None))[0],
                )
                return last
            logger.info(
                "worker slug=%s ip=%s status=%s; trying next",
                slug,
                ip,
                last.status_code if last is not None else "error",
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/dialin/{slug}")
    async def dialin(slug: str, request: Request) -> Response:
        if not _SLUG.fullmatch(slug):
            raise HTTPException(status_code=400, detail="invalid slug")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        # Daily's config probe is not a call. Do not occupy a worker.
        if body.get("test") == "test" or not body.get("callId"):
            return JSONResponse({"ok": True, "probe": True})

        last: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(max(1, _retries())):
                last = await dispatch_pods(client, slug, body)
                if last is None:
                    url = worker_url(slug)
                    try:
                        last = await client.post(url, json=body)
                    except httpx.HTTPError as e:
                        logger.warning("worker %s attempt %d: %s", url, attempt + 1, e)
                        await asyncio.sleep(_retry_s())
                        continue
                if last.status_code not in _RETRY_CODES:
                    return _as_response(last)
                logger.info(
                    "worker slug=%s status=%s attempt=%d",
                    slug,
                    last.status_code,
                    attempt + 1,
                )
                await asyncio.sleep(_retry_s())
        raise HTTPException(status_code=503, detail="no open worker")

    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PIPECAT_DISPATCHER_PORT", "8000")),
    )
