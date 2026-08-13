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
        print(f"snapshot: S3 put failed sim={cid} err={e}", flush=True)
        return state
    bucket = os.environ.get("MIVAS_SNAPSHOT_BUCKET", "").strip()
    if bucket:
        print(f"snapshot: s3://{bucket}/{snapshot_key(cid, '.final.json')}", flush=True)
    else:
        print(f"snapshot: final sim={cid}", flush=True)
    return state
