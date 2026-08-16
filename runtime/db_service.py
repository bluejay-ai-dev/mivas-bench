"""Per-call SQLite files for industry tool servers.

Handlers keep `with db.connect() as conn: conn.execute(...)`. This module is
the only place that chooses a file: first touch of a call id copies schema.sql
then seed.sql into `{data_dir}/calls/{id}.db`; later touches reuse it.

Reuse is what a real call wants (many HTTP requests, one call id, one evolving
DB). A repeatable check wants the opposite, so it says so at the call site:
`with db.scope("selfcheck", fresh=True):` rebuilds the fixture from schema+seed
before yielding.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

_CALL_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_call_id: ContextVar[str] = ContextVar("mivas_call_id", default="")


class CallIdError(ValueError):
    """Empty, missing, or path-escaping conversation id."""


class DBService:
    def __init__(
        self,
        schema_path: Path,
        seed_path: Path,
        data_dir: Path,
        *,
        shared: bool = False,
    ) -> None:
        self.schema_path = Path(schema_path)
        self.seed_path = Path(seed_path)
        self.data_dir = Path(data_dir)
        self.calls_dir = self.data_dir / "calls"
        self.shared = shared
        self.shared_path = self.data_dir / "runtime.db"
        self._create_lock = threading.Lock()

    @classmethod
    def for_industry(
        cls,
        industry_dir: Path | str,
        *,
        shared: bool | None = None,
    ) -> DBService:
        """Wire schema/seed from an industry pack and data_dir from MIVAS_DB_PATH."""
        industry_dir = Path(industry_dir)
        db_dir = industry_dir / "db"
        env_path = os.environ.get("MIVAS_DB_PATH", "").strip()
        data_dir = Path(env_path).parent if env_path else db_dir
        if shared is None:
            shared = os.environ.get("MIVAS_DB_SHARED", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
        return cls(
            schema_path=db_dir / "schema.sql",
            seed_path=db_dir / "seed.sql",
            data_dir=data_dir,
            shared=shared,
        )


    def ensure(self, call_id: str) -> Path:
        safe = _normalise_call_id(call_id)
        path = self.calls_dir / f"{safe}.db"
        if path.exists():
            return path
        with self._create_lock:
            if path.exists():
                return path
            self.calls_dir.mkdir(parents=True, exist_ok=True)
            _write_fixture_db(path, self.schema_path, self.seed_path)
        return path

    def _recreate(self, path: Path) -> Path:
        """Replace path with a pristine fixture DB, atomically."""
        with self._create_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # pid-unique so concurrent processes never share a half-built file;
            # threads are serialised by _create_lock.
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            _unlink_db(tmp)
            _write_fixture_db(tmp, self.schema_path, self.seed_path)
            # Drop the old -wal/-shm first: a stale journal beside a new main
            # file is the one combination sqlite would try to replay.
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            os.replace(tmp, path)
        return path

    @contextmanager
    def scope(self, call_id: str, *, fresh: bool = False) -> Iterator[str]:
        """Bind call_id for this context. fresh=True rebuilds its fixture DB first."""
        safe = _normalise_call_id(call_id)
        if fresh:
            self._recreate(self.calls_dir / f"{safe}.db")
        token = _call_id.set(safe)
        try:
            yield safe
        finally:
            _call_id.reset(token)

    def _active_path(self, call_id: str | None) -> Path:
        cid = (call_id if call_id is not None else _call_id.get()) or ""
        if cid:
            return self.ensure(cid)
        if self.shared:
            return self._ensure_shared()
        raise CallIdError("missing call id")

    def _ensure_shared(self) -> Path:
        path = self.shared_path
        if path.exists():
            return path
        with self._create_lock:
            if path.exists():
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_fixture_db(path, self.schema_path, self.seed_path)
        return path

    @contextmanager
    def connect(self, call_id: str | None = None) -> Iterator[sqlite3.Connection]:
        path = self._active_path(call_id)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def http_middleware(self, request, call_next):
        """Scope X-Mivas-Call-Id (or ?call_id=) for the request. /health is global."""
        from starlette.responses import JSONResponse

        if request.url.path.rstrip("/") == "/health":
            return await call_next(request)
        path = request.url.path.rstrip("/") or "/"
        if path == "/bind" or path.startswith("/bind/"):
            return await call_next(request)
        if path == "/tools/_claim":
            return await call_next(request)
        if path == "/tools/dialin":
            return await call_next(request)
        if path == "/snapshot" or path.startswith("/snapshot/"):
            return await call_next(request)
        raw = request.headers.get("x-mivas-call-id") or request.query_params.get("call_id")
        if not raw:
            if self.shared:
                return await call_next(request)
            return JSONResponse({"detail": "missing X-Mivas-Call-Id"}, status_code=400)
        try:
            with self.scope(raw):
                if request.method == "POST" and request.url.path.startswith("/tools/"):
                    from call_id import log_tool_post

                    log_tool_post(self.current_call_id(), path=request.url.path)
                return await call_next(request)
        except CallIdError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)

    def current_call_id(self) -> str:
        return _call_id.get()

    def mount_cluster_routes(self, app) -> None:
        """Provider-call-id → Bluejay sim id (in-process; same pod as CHIRP)."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        store: dict[str, str] = {}
        lock = threading.Lock()

        async def bind(request: Request) -> JSONResponse:
            data = await request.json()
            pid = str((data or {}).get("provider_call_id") or "").strip()
            sid = str((data or {}).get("sim_id") or "").strip()
            if not pid or not sid:
                return JSONResponse(
                    {"detail": "provider_call_id and sim_id required"}, status_code=400
                )
            with lock:
                store[pid] = sid
            return JSONResponse(
                {"ok": "true", "provider_call_id": pid, "sim_id": sid}
            )

        async def lookup(request: Request) -> JSONResponse:
            pid = str(request.path_params.get("provider_call_id") or "").strip()
            with lock:
                found = store.get(pid)
            if not found:
                return JSONResponse({"detail": "unknown provider call id"}, status_code=404)
            return JSONResponse({"provider_call_id": pid, "sim_id": found})

        claims: dict[str, str] = {}

        async def claim(request: Request) -> JSONResponse:
            """One owner per sim_id. Pipecat Cloud has no per-run result id."""
            data = await request.json()
            sid = str((data or {}).get("sim_id") or "").strip()
            owner = str((data or {}).get("owner") or "").strip()
            if not sid or not owner:
                return JSONResponse(
                    {"detail": "sim_id and owner required"}, status_code=400
                )
            with lock:
                held = claims.get(sid)
                if held and held != owner:
                    return JSONResponse(
                        {"ok": False, "sim_id": sid, "owner": held}, status_code=409
                    )
                claims[sid] = owner
            return JSONResponse({"ok": True, "sim_id": sid, "owner": owner})

        async def dialin_proxy(request: Request) -> JSONResponse:
            """Forward Daily SIP payload to the in-pod Pipecat dialin server."""
            import httpx

            upstream = os.environ.get("PIPECAT_DIALIN_UPSTREAM", "").strip()
            if not upstream:
                return JSONResponse({"detail": "dialin disabled"}, status_code=404)
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(upstream, json=payload)
            except httpx.HTTPError as e:
                return JSONResponse({"detail": str(e)}, status_code=502)
            try:
                data = resp.json()
            except Exception:
                data = {"ok": False, "body": resp.text[:500]}
            return JSONResponse(data, status_code=resp.status_code)

        def _final_path(call_id: str) -> Path:
            safe = _normalise_call_id(call_id)
            self.calls_dir.mkdir(parents=True, exist_ok=True)
            return self.calls_dir / f"{safe}.final.json"

        async def save_snapshot(request: Request) -> JSONResponse:
            try:
                data = await request.json()
            except Exception:
                data = None
            cid = str((data or {}).get("call_id") or "").strip()
            state = (data or {}).get("state")
            if not cid or not isinstance(state, dict):
                return JSONResponse(
                    {"detail": "call_id and state object required"}, status_code=400
                )
            try:
                path = _final_path(cid)
            except CallIdError as e:
                return JSONResponse({"detail": str(e)}, status_code=400)
            path.write_text(json.dumps(state), encoding="utf-8")
            return JSONResponse({"ok": True, "call_id": cid})

        async def get_snapshot(request: Request) -> JSONResponse:
            cid = str(request.path_params.get("call_id") or "").strip()
            try:
                path = _final_path(cid)
            except CallIdError as e:
                return JSONResponse({"detail": str(e)}, status_code=400)
            if not path.is_file():
                return JSONResponse({"detail": "no snapshot"}, status_code=404)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return JSONResponse({"detail": "corrupt snapshot"}, status_code=500)
            return JSONResponse(payload)

        app.router.routes.extend(
            [
                Route("/bind", bind, methods=["POST"]),
                Route("/bind/{provider_call_id}", lookup, methods=["GET"]),
                Route("/tools/_claim", claim, methods=["POST"]),
                Route("/tools/dialin", dialin_proxy, methods=["POST"]),
                Route("/snapshot", save_snapshot, methods=["POST"]),
                Route("/snapshot/{call_id}", get_snapshot, methods=["GET"]),
            ]
        )




def _normalise_call_id(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not _CALL_ID_OK.fullmatch(value):
        raise CallIdError(f"invalid call id {raw!r}")
    return value


def _unlink_db(path: Path) -> None:
    """Remove a sqlite file and its -wal/-shm sidecars."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        p.unlink(missing_ok=True)


def _write_fixture_db(path: Path, schema_path: Path, seed_path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema_path.read_text())
        seed = seed_path.read_text().strip()
        if seed:
            conn.executescript(seed)
        conn.commit()
    finally:
        conn.close()
