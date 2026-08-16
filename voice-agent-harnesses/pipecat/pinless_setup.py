"""Configure Daily pinless SIP URIs that webhook the k8s dispatcher.

Daily POSTs `properties.pinless_dialin` as a full replace. This script GETs the
current domain config first and overlays only the slugs you pass, so other
prefixes keep their URIs.

    export DAILY_API_KEY=...
    export MIVAS_BASE_DOMAIN=benchmarks.getbluejay.ai
    uv run python voice-agent-harnesses/pipecat/pinless_setup.py \\
        pipecat-cascaded-healthcare pipecat-openai-realtime-2-1-healthcare

Prints each slug's static sip_uri for Bluejay `connection_type=SIP`.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1").rstrip("/")
STATE = Path(__file__).resolve().parent / ".daily-pinless.json"


def api(method: str, path: str, data: dict | None = None) -> dict:
    key = os.environ.get("DAILY_API_KEY", "").strip()
    if not key:
        sys.exit("DAILY_API_KEY is required")
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} → {e.code} {e.read().decode()[:800]}") from e


def dispatcher_url(slug: str) -> str:
    base = os.environ.get("MIVAS_BASE_DOMAIN", "").strip().lower().strip(".")
    if not base:
        sys.exit("MIVAS_BASE_DOMAIN is required")
    prefix = os.environ.get("PIPECAT_DISPATCHER_URL", "").strip().rstrip("/")
    if prefix:
        return f"{prefix}/dialin/{slug}"
    return f"https://pipecat-dialin.{base}/dialin/{slug}"


def _pinless_entries(payload: dict) -> list[dict]:
    cfg = payload.get("config") or payload
    raw = cfg.get("pinless_dialin") if isinstance(cfg, dict) else None
    return list(raw) if isinstance(raw, list) else []


def merge_entries(existing: list[dict], slugs: list[str]) -> list[dict]:
    by_prefix: dict[str, dict] = {}
    for entry in existing:
        prefix = str(entry.get("name_prefix") or "").strip()
        api_url = str(entry.get("room_creation_api") or "").strip()
        if prefix and api_url:
            by_prefix[prefix] = {
                "name_prefix": prefix,
                "room_creation_api": api_url,
            }
    for slug in slugs:
        by_prefix[slug] = {
            "name_prefix": slug,
            "room_creation_api": dispatcher_url(slug),
        }
    return list(by_prefix.values())


def main() -> None:
    slugs = [s.strip() for s in sys.argv[1:] if s.strip()]
    if not slugs:
        sys.exit("usage: pinless_setup.py <slug> [<slug> ...]")
    entries = merge_entries(_pinless_entries(api("GET", "/")), slugs)
    result = api("POST", "/", {"properties": {"pinless_dialin": entries}})
    pinless = _pinless_entries(result)
    state: dict[str, str] = {}
    for entry in pinless:
        uri = str(entry.get("sip_uri") or "").strip()
        prefix = str(entry.get("name_prefix") or "").strip()
        if uri and not uri.startswith("sip:"):
            uri = f"sip:{uri}"
        if prefix and uri:
            state[prefix] = uri
            print(prefix, uri)
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    print("wrote", STATE)


if __name__ == "__main__":
    main()
