"""Daily pinless SIP webhook → an idle Pipecat worker on this cluster.

Daily (the SIP fabric) POSTs here when someone dials the static pinless URI.
This process does not run the bot. It forwards the payload to the in-cluster
worker Service (`http://mivas-{slug}:8000/tools/dialin`) and retries on 409/502/503
so a free replica can take the call. Do not hairpin through the public hostname
from inside the cluster.

    POST /dialin/{slug}   Daily room_creation_api
    GET  /health
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("mivas.pipecat.dispatcher")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RETRIES = int(os.environ.get("PIPECAT_DISPATCH_RETRIES", "8"))
_RETRY_S = float(os.environ.get("PIPECAT_DISPATCH_RETRY_S", "0.15"))
_RETRY_CODES = {409, 502, 503}


def worker_url(slug: str) -> str:
    template = os.environ.get(
        "PIPECAT_WORKER_URL_TEMPLATE",
        "http://mivas-{slug}:8000/tools/dialin",
    )
    return template.replace("{slug}", slug)


def build_app() -> FastAPI:
    app = FastAPI(title="mivas pipecat dispatcher")

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

        url = worker_url(slug)
        last: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(max(1, _RETRIES)):
                try:
                    last = await client.post(url, json=body)
                except httpx.HTTPError as e:
                    logger.warning("worker %s attempt %d: %s", url, attempt + 1, e)
                    await asyncio.sleep(_RETRY_S)
                    continue
                if last.status_code not in _RETRY_CODES:
                    return Response(
                        content=last.content,
                        status_code=last.status_code,
                        media_type=last.headers.get("content-type", "application/json"),
                    )
                logger.info(
                    "worker slug=%s status=%s attempt=%d",
                    slug,
                    last.status_code,
                    attempt + 1,
                )
                await asyncio.sleep(_RETRY_S)
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
