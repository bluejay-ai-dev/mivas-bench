"""Freeze a call's DB at hangup: local JSON, then S3.

Evals read ``s3://$MIVAS_SNAPSHOT_BUCKET/$prefix/{slug}/{id}.final.json``
(and the sibling ``.db``). They do not GET the public hostname — ALB would
pick a random replica. Replica death after the PUT is fine.

``MIVAS_SNAPSHOT_BUCKET`` unset (local ``--check``): skip S3, keep the file.
PUT failures print and never raise.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _calls_dir() -> Path:
    raw = os.environ.get("MIVAS_DB_PATH", "/data/industry.db").strip() or "/data/industry.db"
    return Path(raw).expanduser().resolve().parent / "calls"


def snapshot_key(call_id: str, suffix: str) -> str:
    prefix = (os.environ.get("MIVAS_SNAPSHOT_PREFIX", "mivas").strip() or "mivas").strip("/")
    slug = (os.environ.get("MIVAS_SLUG") or "local").strip() or "local"
    return f"{prefix}/{slug}/{call_id}{suffix}"


def _put_s3(key: str, body: bytes, content_type: str) -> None:
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    if not bucket:
        return
    import boto3

    region = (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-west-1"
    )
    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


def preflight() -> None:
    """Boot check: a configured bucket the pod cannot write to loses every call.

    Hard-fails on a missing boto3 (packaging bug, deterministic). Only warns on
    credentials, which can resolve a moment after the container starts.
    """
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    if not bucket:
        print("snapshot: MIVAS_SNAPSHOT_BUCKET unset — local .final.json only", flush=True)
        return
    import boto3  # ModuleNotFoundError here is fatal on purpose

    region = (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-west-1"
    )
    if boto3.Session(region_name=region).get_credentials() is None:
        print(
            f"snapshot: NO AWS CREDENTIALS — every PUT to s3://{bucket} will fail",
            flush=True,
        )
        return
    print(f"snapshot: preflight ok bucket={bucket} region={region}", flush=True)


def capture_final(call_id: str) -> dict[str, Any] | None:
    """GET /state for this id, write local JSON, PUT JSON + sqlite to S3."""
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

    calls = _calls_dir()
    calls.mkdir(parents=True, exist_ok=True)
    json_path = calls / f"{cid}.final.json"
    json_bytes = json.dumps(state).encode()
    try:
        json_path.write_bytes(json_bytes)
    except OSError as e:
        print(f"snapshot: write {json_path} failed sim={cid} err={e}", flush=True)

    db_path = calls / f"{cid}.db"
    db_bytes = db_path.read_bytes() if db_path.is_file() else None

    try:
        _put_s3(snapshot_key(cid, ".final.json"), json_bytes, "application/json")
        if db_bytes is not None:
            _put_s3(snapshot_key(cid, ".db"), db_bytes, "application/vnd.sqlite3")
    except Exception as e:
        bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
        print(
            f"snapshot: S3 PUT FAILED sim={cid} "
            f"s3://{bucket}/{snapshot_key(cid, '.final.json')} "
            f"err={type(e).__name__}: {e}",
            flush=True,
        )
        return state
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    if bucket:
        print(f"snapshot: s3://{bucket}/{snapshot_key(cid, '.final.json')}", flush=True)
    else:
        print(f"snapshot: final sim={cid}", flush=True)
    return state


def mount(app, calls_dir: Path) -> None:
    """GET/POST /snapshot on the tool server. Files sit next to {id}.db."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from db_service import CallIdError, normalise_call_id

    root = Path(calls_dir)

    def _final_path(call_id: str) -> Path:
        safe = normalise_call_id(call_id)
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{safe}.final.json"

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
            Route("/snapshot", save_snapshot, methods=["POST"]),
            Route("/snapshot/{call_id}", get_snapshot, methods=["GET"]),
        ]
    )
