"""Encode legal_task_spec rows into industries/legal/tasks/*/task.json.

Each folder gets one task.json with inline exp_db_state: full hangup GET /state
after replaying expected tools on a fresh seeded DB (same pattern as healthcare).
Audio clones ({category}-E1-BG / -SIG) share the base E1 contract.

    uv run python scripts/encode_legal_tasks.py
    uv run python scripts/encode_legal_tasks.py --repair
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

from expected_final_state import (  # noqa: E402
    canonical_state,
    is_harness_native,
    load_tool_server,
    tool_flags,
)
from legal_task_spec import (  # noqa: E402
    CATEGORY_SLUGS,
    all_cases,
    band_for,
    category_of,
)

TASKS = ROOT / "industries" / "legal" / "tasks"

CLONE_SOURCES = tuple(f"C{i}-E1" for i in range(1, 6)) + ("R-E1",)
CLONE_SUFFIXES = {"-BG": "background_noise", "-SIG": "bad_signal"}


def phone_digits(raw: str) -> str:
    return re.sub(r"\D", "", raw)


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
    if "phone" not in names:
        traits.append({"trait_name": "phone", "value": row["phone"]})
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
    if row.get("booking"):
        meta["booking"] = True

    expected_calls = copy.deepcopy(row.get("tools") or [])
    for call in expected_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        params = call.get("parameters")
        if not isinstance(params, dict):
            continue
        # Fairness: reason/token fields are system-internal confirmations that
        # do not materially change customer-visible success.
        if name == "escalate_to_human":
            params.pop("reason_code", None)
        if name in {"confirm_evaluation", "confirm_cancellation"}:
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


# Prose fields omitted from exp_tool_calls; replay only needs a non-empty value.
_REPLAY_PROSE: dict[str, dict[str, str]] = {
    "take_message": {"message": "Callback requested."},
    "add_intake_note": {"note": "Intake note."},
    "record_intake": {"summary": "Intake summary."},
}


def enrich_replay_call(
    call: dict[str, Any],
    row: dict[str, Any],
    *,
    evaluation_id: str | None,
) -> dict[str, Any]:
    out = copy.deepcopy(call)
    name = out.get("name")
    params = dict(out.get("parameters") or {})
    if name == "lookup_caller":
        params.setdefault("full_name", row["name"])
        params.setdefault("phone", phone_digits(row["phone"]))
    if name == "find_evaluation_slots":
        params.setdefault("earliest_date", "")
    for key, default in (_REPLAY_PROSE.get(name) or {}).items():
        if params.get(key) in (None, ""):
            params[key] = default
    if name == "hold_cancellation":
        raw_id = params.get("evaluation_id")
        if evaluation_id and (not raw_id or raw_id in ("eval-1", "")):
            params["evaluation_id"] = evaluation_id
    if params:
        out["parameters"] = params
    return out


def replay_sequence(row: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any]:
    flags = tool_flags("legal")
    case_key = row["key"]
    cid = f"exp-{re.sub(r'[^A-Za-z0-9_-]', '-', case_key)[:60].strip('-') or 'case'}"
    headers = {"X-Mivas-Call-Id": cid}
    evaluation_id: str | None = None
    last_slot_id: str | None = None
    prior: dict[str, Any] | None = None
    replayed: list[dict[str, Any]] = []

    with load_tool_server("legal") as module:
        client = TestClient(module.app)
        for raw in calls:
            call = enrich_replay_call(raw, row, evaluation_id=evaluation_id)
            name = str(call.get("name") or "").strip()
            if not name or is_harness_native(name, flags):
                continue
            args = dict(call.get("parameters") or {})
            prior_data = prior.get("data") if prior and isinstance(prior.get("data"), dict) else {}
            if name == "hold_evaluation" and not args.get("slot_id"):
                slots = prior_data.get("slots") if isinstance(prior_data.get("slots"), list) else []
                first = slots[0] if slots and isinstance(slots[0], dict) else {}
                slot_id = first.get("slot_id") or last_slot_id
                if slot_id:
                    args["slot_id"] = slot_id
            if name == "hold_cancellation":
                raw_id = args.get("evaluation_id")
                if evaluation_id and (not raw_id or raw_id in ("eval-1", "")):
                    args["evaluation_id"] = evaluation_id
            if prior_data:
                for key in ("confirmation_token",):
                    if args.get(key) in (None, "") and prior_data.get(key):
                        args[key] = prior_data[key]
            resp = client.post(f"/tools/{name}", json={"arguments": args}, headers=headers)
            body: dict[str, Any]
            try:
                parsed = resp.json()
                body = parsed if isinstance(parsed, dict) else {"raw": parsed}
            except ValueError:
                body = {"raw": resp.text}
            replayed.append({
                "name": name,
                "status_code": resp.status_code,
                "ok": body.get("ok", body.get("success")),
            })
            if resp.status_code != 200 or body.get("ok") is False:
                raise SystemExit(
                    f"{case_key} replay {name} → {resp.status_code} "
                    f"{json.dumps(body)[:400]}"
                )
            prior = body
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            if name == "find_evaluation_slots":
                slots = data.get("slots") if isinstance(data.get("slots"), list) else []
                if slots and isinstance(slots[0], dict) and slots[0].get("slot_id"):
                    last_slot_id = str(slots[0]["slot_id"])
            if name == "confirm_evaluation" and data.get("evaluation_id"):
                evaluation_id = str(data["evaluation_id"])

        state_resp = client.get("/state", headers=headers)
        if state_resp.status_code != 200:
            raise SystemExit(f"{case_key}: GET /state → {state_resp.status_code}")
        full = state_resp.json()
        if not isinstance(full, dict):
            raise SystemExit(f"{case_key}: GET /state was not an object")

    return canonical_state(full)


def replay_calls(row: dict[str, Any], task: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prefix = list(row.get("replay_prefix") or [])
    scored = list((task or {}).get("exp_tool_calls") or row.get("tools") or [])
    return prefix + scored


def replay_row(row: dict[str, Any], task: dict[str, Any] | None = None) -> dict[str, Any]:
    return replay_sequence(row, replay_calls(row, task))


def all_keys() -> list[str]:
    keys = [row["key"] for row in all_cases()]
    for src in CLONE_SOURCES:
        keys.append(f"{src}-BG")
        keys.append(f"{src}-SIG")
    return keys


def encode_all(repair_only: bool = False) -> int:
    by_key = spec_by_key()
    TASKS.mkdir(parents=True, exist_ok=True)
    written = 0
    for case_key in all_keys():
        src = source_key(case_key)
        row = by_key[src]
        path = TASKS / case_key / "task.json"
        if repair_only and path.is_file():
            task = json.loads(path.read_text())
        else:
            task = row_to_task(row, case_key)
        task["exp_db_state"] = replay_row(row, task if repair_only else None)
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
    args = parser.parse_args(argv)
    return encode_all(repair_only=args.repair)


if __name__ == "__main__":
    raise SystemExit(main())
