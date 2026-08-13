"""The ONE writer for the live legal digital humans.

Rebuilds every PUT body from `legal_digital_humans.py`, pushes them with
PUT /v1/update-digital-humans, and dumps what it sent verbatim to
`docs/legal/dh_final.json` — the reviewable record of what the live digital humans hold.
Idempotent. There must be no second writer: hand-editing a digital human in the UI or
with a one-off script makes the spec a lie.

Matching is by the case key that starts each persona's name (L01, L02, …).

    uv run python scripts/apply_legal_digital_humans.py --dry-run       # diff only
    uv run python scripts/apply_legal_digital_humans.py                 # push all 60
    uv run python scripts/apply_legal_digital_humans.py --only L12 L44  # push a subset
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
SIMULATION_IDS = [30425]

# fields this script owns; anything not listed keeps whatever the server holds
OWNED = [
    "intent", "success_criteria", "expected_tool_calls", "traits", "tags",
    "scripted_responses", "speaks_first_config", "creativity", "language", "accent",
    "gender", "fluency", "voice_speed", "verbosity", "audio_quality",
    "background_noise", "background_noise_volume", "interruptions",
    "allow_dtmf_tool", "allow_end_call_tool", "allow_silence_tool", "num_runs",
    "test_name",
]


def _key() -> str:
    k = os.environ.get("BLUEJAY_API_KEY")
    if not k:
        raise SystemExit("need BLUEJAY_API_KEY")
    return k


def _req(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}/{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {_key()}", "X-API-Key": _key(),
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} → {e.code} {e.read()[:400].decode(errors='replace')}")


def spec() -> list[dict]:
    path = ROOT / "scripts" / "legal_digital_humans.py"
    s = importlib.util.spec_from_file_location("legal_dh_spec", path)
    module = importlib.util.module_from_spec(s)
    sys.modules["legal_dh_spec"] = module
    assert s.loader is not None
    s.loader.exec_module(module)
    module._check(module.build())  # never push a spec that fails its own invariants
    return module.build()


def live() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for dh in _req("GET", f"digital-humans-by-simulation/{SIMULATION_IDS[0]}").get(
        "digital_humans", []
    ):
        name = dh.get("name") or ""
        if name:
            out[name.split()[0]] = dh
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", nargs="*", help="case keys, e.g. L12 L44")
    args = p.parse_args()

    by_key = {d["digital_human"]["name"].split()[0]: d["digital_human"] for d in spec()}
    existing = live()
    missing = sorted(set(by_key) - set(existing))
    if missing:
        raise SystemExit(f"not live, create them first: {missing}")

    keys = args.only or sorted(by_key)
    updates, record = [], {}
    for k in keys:
        want = by_key[k]
        body = {f: want[f] for f in OWNED if f in want}
        body["simulation_ids"] = SIMULATION_IDS
        updates.append({"digital_human_id": existing[k]["id"], "update": body})
        record[k] = {"digital_human_id": existing[k]["id"], "update": body}

    out = ROOT / "docs" / "legal" / "dh_final.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(f"{len(updates)} digital humans → {out.relative_to(ROOT)}")
    print(f"  with scripted pins: {sum(1 for v in record.values() if v['update'].get('scripted_responses'))}")

    if args.dry_run:
        print("dry run — nothing pushed")
        return 0

    pushed = 0
    for i in range(0, len(updates), 30):  # ~150 rapid calls start 401ing; batch and pace
        batch = updates[i : i + 30]
        resp = _req("PUT", "update-digital-humans", {"updates": batch})
        got = resp.get("updated") or resp.get("updated_ids") or []
        pushed += len(got)
        for err in resp.get("errors") or []:
            # a Postgres deadlock hits one row in a big batch; retry it alone
            one = next((u for u in batch
                        if u["digital_human_id"] == err.get("digital_human_id")), None)
            if one is None:
                print("  error:", json.dumps(err)[:300])
                continue
            time.sleep(1.0)
            retry = _req("PUT", "update-digital-humans", {"updates": [one]})
            pushed += len(retry.get("updated") or [])
        time.sleep(0.3)
    print(f"pushed {pushed}/{len(updates)}")
    return 0 if pushed == len(updates) else 1


if __name__ == "__main__":
    sys.exit(main())
