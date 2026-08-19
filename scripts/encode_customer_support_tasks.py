"""Encode customer_support_task_spec rows into industries/customer-support/tasks.

Each folder gets one task.json with inline exp_db_state: hangup GET /state after
replaying expected tools on a fresh seeded DB. Audio clones share the E1 contract.

    uv run python scripts/encode_customer_support_tasks.py
    uv run python scripts/encode_customer_support_tasks.py --repair
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from customer_support_task_spec import (  # noqa: E402
    CATEGORY_SLUGS,
    all_cases,
    band_for,
    category_of,
    validate_cases,
)
from expected_final_state import (  # noqa: E402
    canonical_state,
    is_harness_native,
    load_tool_server,
    tool_flags,
)

TASKS = ROOT / "industries" / "customer-support" / "tasks"

CLONE_SOURCES = tuple(f"T{i}-E1" for i in range(1, 6)) + ("R-E1",)
CLONE_SUFFIXES = {"-BG": "background_noise", "-SIG": "bad_signal"}

# quote/confirm pairs: token is issued by the quote and must not be guessed.
TOKEN_CONFIRMS = frozenset({
    "confirm_delivery_change",
    "confirm_price_match",
    "confirm_return",
    "confirm_membership_upgrade",
    "confirm_membership_cancellation",
})


def difficulty_of(key: str) -> str:
    letter = key.split("-")[1][0]
    return {"E": "easy", "M": "medium", "H": "hard"}[letter]


def audio_of(key: str) -> str:
    for suffix, condition in CLONE_SUFFIXES.items():
        if key.endswith(suffix):
            return condition
    return "perfect"


def source_key(key: str) -> str:
    for suffix in CLONE_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def spec_by_key() -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in all_cases()}


def customer_traits(row: dict[str, Any]) -> list[dict[str, Any]]:
    traits = [dict(item) for item in (row.get("traits") or [])]
    names = {item.get("trait_name") for item in traits}
    if "full_name" not in names:
        traits.insert(0, {"trait_name": "full_name", "value": row["name"]})
    return traits


def row_to_task(row: dict[str, Any], case_key: str) -> dict[str, Any]:
    cat = category_of(case_key)
    meta: dict[str, Any] = {
        "category": cat,
        "category_slug": CATEGORY_SLUGS[cat],
        "difficulty": difficulty_of(case_key),
        "audio_condition": audio_of(case_key),
    }
    if row.get("escalation"):
        meta["escalation"] = True

    expected_calls = copy.deepcopy(row.get("tools") or [])
    for call in expected_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        params = call.get("parameters")
        if not isinstance(params, dict):
            continue
        if name in TOKEN_CONFIRMS:
            params.pop("confirmation_token", None)
        if not params:
            call.pop("parameters", None)

    return {
        "task_name": f"{case_key}: {row['title']}",
        "customer_name": row["name"],
        "intent": row["intent"],
        "traits": customer_traits(row),
        "exp_handoff_path": list(row.get("handoffs") or []),
        "exp_tool_calls": expected_calls,
        "behaviors": {"creativity": 0},
        "scripted_responses": copy.deepcopy(row.get("pins") or []),
        "customer_available_tools": {},
        "metadata": meta,
    }


def replay_sequence(row: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any]:
    flags = tool_flags("customer-support")
    case_key = row["key"]
    cid = f"exp-{re.sub(r'[^A-Za-z0-9_-]', '-', case_key)[:60].strip('-') or 'case'}"
    headers = {"X-Mivas-Call-Id": cid}
    prior: dict[str, Any] | None = None
    last_ok: dict[str, Any] | None = None

    with load_tool_server("customer-support") as module:
        client = TestClient(module.app)
        for raw in calls:
            call = copy.deepcopy(raw)
            name = str(call.get("name") or "").strip()
            if not name or is_harness_native(name, flags):
                continue
            args = dict(call.get("parameters") or {})
            prior_data = (
                last_ok.get("data")
                if last_ok and isinstance(last_ok.get("data"), dict)
                else {}
            )
            if name in TOKEN_CONFIRMS and not args.get("confirmation_token"):
                token = prior_data.get("confirmation_token")
                if token:
                    args["confirmation_token"] = token
            if name == "create_return_label" and not args.get("rma_number"):
                rma = prior_data.get("rma_number")
                if rma:
                    args["rma_number"] = rma
            resp = client.post(f"/tools/{name}", json={"arguments": args}, headers=headers)
            try:
                parsed = resp.json()
                body = parsed if isinstance(parsed, dict) else {"raw": parsed}
            except ValueError:
                body = {"raw": resp.text}
            if resp.status_code != 200:
                raise SystemExit(
                    f"{case_key} replay {name} → {resp.status_code} "
                    f"{json.dumps(body)[:400]}"
                )
            prior = body
            if body.get("ok") is True:
                last_ok = body
            # refusals (ok:false) are valid expected outcomes; keep going

        state_resp = client.get("/state", headers=headers)
        if state_resp.status_code != 200:
            raise SystemExit(f"{case_key}: GET /state → {state_resp.status_code}")
        full = state_resp.json()
        if not isinstance(full, dict):
            raise SystemExit(f"{case_key}: GET /state was not an object")

    _ = prior
    return canonical_state(full)


def replay_calls(row: dict[str, Any], task: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return list((task or {}).get("exp_tool_calls") or row.get("tools") or [])


def all_keys() -> list[str]:
    keys = [row["key"] for row in all_cases()]
    for src in CLONE_SOURCES:
        keys.append(f"{src}-BG")
        keys.append(f"{src}-SIG")
    return keys


def encode_all(repair_only: bool = False, keys: list[str] | None = None) -> int:
    rows = all_cases()
    validate_cases(rows)
    by_key = {row["key"]: row for row in rows}
    TASKS.mkdir(parents=True, exist_ok=True)
    wanted = set(keys) if keys else None
    written = 0
    for case_key in all_keys():
        if wanted is not None and case_key not in wanted:
            continue
        src = source_key(case_key)
        row = by_key[src]
        path = TASKS / case_key / "task.json"
        if repair_only and path.is_file():
            task = json.loads(path.read_text())
        else:
            task = row_to_task(row, case_key)
        task["exp_db_state"] = replay_sequence(row, replay_calls(row, task if repair_only else None))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task, indent=2) + "\n")
        written += 1
        n_tools = len(task.get("exp_tool_calls") or [])
        print(f"{case_key:14}  {n_tools} tools  {row['name']}", flush=True)
    print(f"wrote {written} task folders under {TASKS}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repair",
        action="store_true",
        help="re-read task.json and replay exp_db_state from exp_tool_calls",
    )
    parser.add_argument(
        "--keys",
        nargs="+",
        help="encode only these case keys (default: the full matrix)",
    )
    args = parser.parse_args(argv)
    return encode_all(repair_only=args.repair, keys=args.keys)


if __name__ == "__main__":
    raise SystemExit(main())
