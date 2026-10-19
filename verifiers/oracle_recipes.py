"""Generate a replayable oracle recipe for every MIVAS task.

`exp_tool_calls` lists only the arguments the grader constrains. The oracle is the
full ordered call list a correct agent makes, with every argument filled, transfers
included, and `expect_error` on calls that are supposed to fail. It is derived from
each task's own traits, `exp_tool_calls`, `exp_db_state`, and the tool schema, plus
per-task overrides in `verifiers/oracle_overrides.json`. The graded expectations in
task.json are never edited.

    uv run python verifiers/oracle_recipes.py            # write every solution/oracle.json
    uv run python verifiers/oracle_recipes.py --check    # exit 1 if any file on disk is stale
    uv run python verifiers/oracle_recipes.py --industry legal --task C5-E2 --print

Override entry, keyed "<industry>/<task>"; selectors are "tool" or "tool#n" (n-th
occurrence, 1-based):

    {"args": {"quote_price_match": {"sku": "SKU-AUD-7720"}},
     "expect_error": {"verify_identity#1": "IDENTITY_MISMATCH"},
     "insert": [{"before": "hold_evaluation", "name": "record_intake", "arguments": {...}}]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_ROOT = ROOT / "industries"
OVERRIDES_PATH = Path(__file__).resolve().parent / "oracle_overrides.json"
INDUSTRIES = ("customer-support", "healthcare", "legal")

# Trait name per industry that fills a tool argument of a different name.
TRAIT_ALIASES: dict[str, dict[str, str]] = {
    "healthcare": {"dob": "date_of_birth", "mobile_e164": "mobile",
                   "callback_number": "mobile"},
    "customer-support": {},
    "legal": {},
}

# Legal tools that need a pinned caller before they can write.
LEGAL_NEEDS_CALLER = frozenset({
    "get_caller_matters", "take_message", "record_intake", "add_intake_note",
    "send_intake_packet", "request_records_authorization", "hold_evaluation",
    "hold_cancellation", "get_case_status",
})

# Short prose defaults for required free-text arguments the task leaves open.
PROSE_DEFAULTS: dict[str, str] = {
    "message": "Please call back.",
    "note": "Intake note.",
    "summary": "Intake summary.",
    "handoff_summary": "Caller handed off.",
    "issue": "Device issue.",
    "reason": "caller_request",
}


def tasks(industry: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted((INDUSTRY_ROOT / industry / "tasks").glob("*/task.json")):
        out.append((path.parent.name, json.loads(path.read_text())))
    return out


def tool_schemas(industry: str) -> dict[str, dict[str, Any]]:
    data = json.loads((INDUSTRY_ROOT / industry / "tools.json").read_text())
    return {t["name"]: (t.get("inputSchema") or {}) for t in data.get("tools") or []}


def traits_of(task: dict[str, Any]) -> dict[str, Any]:
    return {t["trait_name"]: t.get("value") for t in task.get("traits") or []}


def pick(value: Any) -> Any:
    """Collapse an any_of constraint to its first concrete value."""
    if isinstance(value, dict) and isinstance(value.get("any_of"), list) and value["any_of"]:
        return pick(value["any_of"][0])
    return value


def _first(rows: Any, **where: Any) -> dict[str, Any]:
    for row in rows or []:
        if isinstance(row, dict) and all(pick(row.get(k)) == v for k, v in where.items()):
            return row
    return {}


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _split_name(full: Any) -> tuple[str, str]:
    parts = str(full or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def base_calls(task: dict[str, Any]) -> list[dict[str, Any]]:
    calls = []
    for raw in task.get("exp_tool_calls") or []:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        params = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
        calls.append({"name": name, "arguments": {k: pick(v) for k, v in params.items()}})
    return calls


def fill_required(calls: list[dict[str, Any]], schemas: dict[str, Any],
                  traits: dict[str, Any], industry: str) -> None:
    """Required schema args that are missing: trait of the same (or aliased) name,
    then a prose default. Optional args are left to the per-industry rules."""
    aliases = TRAIT_ALIASES.get(industry, {})
    for call in calls:
        schema = schemas.get(call["name"]) or {}
        args = call["arguments"]
        for key in schema.get("required") or []:
            if args.get(key) not in (None, ""):
                continue
            trait = traits.get(key, traits.get(aliases.get(key, "")))
            if trait not in (None, ""):
                args[key] = trait
            elif key in PROSE_DEFAULTS:
                args[key] = PROSE_DEFAULTS[key]


def legal_rules(calls: list[dict[str, Any]], task: dict[str, Any]) -> None:
    tr = traits_of(task)
    st = task.get("exp_db_state") or {}
    names = [c["name"] for c in calls]
    esc = (st.get("escalations") or [{}])[0]
    pinned_write = bool(LEGAL_NEEDS_CALLER & set(names)) or bool(esc.get("caller_id"))
    if "lookup_caller" not in names and pinned_write:
        calls.insert(0, {"name": "lookup_caller",
                         "arguments": {"full_name": tr.get("full_name"), "phone": tr.get("phone")}})
    ev = (st.get("evaluations") or [{}])[0]
    holds = st.get("holds") or []
    msgs = st.get("messages") or []
    docs = st.get("documents") or []
    for c in calls:
        p, n = c["arguments"], c["name"]
        if n == "lookup_caller":
            p.setdefault("full_name", tr.get("full_name"))
            p.setdefault("phone", tr.get("phone"))
        elif n == "find_evaluation_slots":
            p.setdefault("practice_area", tr.get("practice_area"))
            p.setdefault("state", tr.get("state"))
            p.setdefault("earliest_date", "")
        elif n == "hold_evaluation":
            p.setdefault("practice_area", tr.get("practice_area"))
            if not p.get("slot_id"):
                slot = ev.get("slot_id") or _first(holds, kind="evaluation").get("slot_id")
                if slot:
                    p["slot_id"] = pick(slot)
        elif n == "hold_cancellation":
            p.setdefault("evaluation_id",
                         ev.get("id") or _first(holds, kind="cancellation").get("evaluation_id"))
            p.setdefault("reason", "caller_request")
        elif n == "take_message":
            p.setdefault("for_whom", pick((msgs[0].get("for_whom") if msgs else None) or "attorney"))
        elif n == "send_intake_packet":
            p.setdefault("channel", pick(_first(docs, kind="intake_packet").get("target") or "email"))
        elif n == "request_records_authorization":
            p.setdefault("provider", pick(_first(docs, kind="records_authorization").get("target") or ""))
        elif n == "get_case_status":
            p.setdefault("matter_id", tr.get("matter_id"))
        elif n == "record_intake":
            p.setdefault("practice_area", tr.get("practice_area"))
            p.setdefault("state", tr.get("state", ""))
            p.setdefault("incident_date", tr.get("incident_date", ""))
        elif n == "escalate_to_human":
            p.setdefault("reason_code", pick(esc.get("reason_code")) or "caller_request")
        elif n in ("check_jurisdiction", "calculate_filing_deadline"):
            p.setdefault("state", tr.get("state"))
            p.setdefault("practice_area", tr.get("practice_area"))
            if n == "calculate_filing_deadline":
                p.setdefault("incident_date", tr.get("incident_date"))
        elif n == "check_conflict":
            p.setdefault("opposing_party", tr.get("opposing_party"))
        elif n == "check_practice_area":
            p.setdefault("practice_area", tr.get("practice_area"))
        elif n.startswith("transfer_to_"):
            p.setdefault("handoff_summary", task.get("task_name", "Handoff."))
            if n == "transfer_to_intake":
                p.setdefault("contact_details_only", False)


SCAM_TOOLS = frozenset({
    "check_subscription_charge", "check_outbound_contact", "report_scam_contact",
})
ORDER_TOOLS = frozenset({
    "get_order", "quote_delivery_change", "cancel_order", "quote_price_match",
    "check_return_eligibility", "quote_return", "check_coverage", "book_service_appointment",
})


def cs_rules(calls: list[dict[str, Any]], task: dict[str, Any]) -> None:
    tr = traits_of(task)
    st = task.get("exp_db_state") or {}
    for c in calls:
        p, n = c["arguments"], c["name"]
        if n == "identify_customer":
            p.setdefault("full_name", tr.get("full_name"))
            p.setdefault("phone", tr.get("phone"))
        elif n == "verify_identity":
            p.setdefault("postal_code", tr.get("postal_code"))
            p.setdefault("card_last4", tr.get("card_last4"))
        elif n in ORDER_TOOLS and not p.get("order_number") and tr.get("order_number"):
            p["order_number"] = tr["order_number"]
        if n == "create_return_label" and not p.get("rma_number") and tr.get("rma_number"):
            p["rma_number"] = tr["rma_number"]
        if n == "get_refund_status" and not p.get("rma_number") and tr.get("rma_number"):
            p["rma_number"] = tr["rma_number"]
        if n == "escalate_to_human":
            p.setdefault("reason_code", pick((st.get("escalations") or [{}])[0].get("reason_code")))
        if n in SCAM_TOOLS and not (p.get("phone") or p.get("email")) and tr.get("phone"):
            # the report row is matched on whichever contact the agent supplied
            p["phone"] = tr["phone"]


def hc_rules(calls: list[dict[str, Any]], task: dict[str, Any]) -> None:
    tr = traits_of(task)
    for c in calls:
        p, n = c["arguments"], c["name"]
        if n == "identify_patient":
            first, last = _split_name(tr.get("full_name"))
            p.setdefault("first_name", first)
            p.setdefault("last_name", last)
            if tr.get("date_of_birth"):
                p.setdefault("dob", tr["date_of_birth"])
        elif n == "verify_identity":
            p.setdefault("full_name", tr.get("full_name"))
            p.setdefault("dob", tr.get("date_of_birth"))
        elif n in ("send_sms", "send_payment_link") and not p.get("mobile_e164") and tr.get("mobile"):
            p["mobile_e164"] = "+1" + _digits(tr["mobile"])[-10:]
        elif n == "create_callback_task" and not p.get("callback_number") and tr.get("mobile"):
            p["callback_number"] = "+1" + _digits(tr["mobile"])[-10:]
        elif n == "end_call":
            p.setdefault("reason", "caller_done")


RULES = {"legal": legal_rules, "customer-support": cs_rules, "healthcare": hc_rules}


def load_overrides() -> dict[str, Any]:
    if not OVERRIDES_PATH.is_file():
        return {}
    return json.loads(OVERRIDES_PATH.read_text())


def _select(calls: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    name, _, nth = selector.partition("#")
    hits = [c for c in calls if c["name"] == name]
    if not hits:
        raise KeyError(f"override selector {selector!r}: no such call")
    index = int(nth) - 1 if nth else 0
    if index >= len(hits):
        raise KeyError(f"override selector {selector!r}: only {len(hits)} occurrence(s)")
    return hits[index]


def apply_overrides(calls: list[dict[str, Any]], override: dict[str, Any]) -> None:
    for item in override.get("insert") or []:
        new = {"name": item["name"], "arguments": dict(item.get("arguments") or {})}
        if item.get("expect_error"):
            new["expect_error"] = item["expect_error"]
        if "before" in item:
            calls.insert(calls.index(_select(calls, item["before"])), new)
        elif "after" in item:
            calls.insert(calls.index(_select(calls, item["after"])) + 1, new)
        else:
            calls.append(new)
    for selector, args in (override.get("args") or {}).items():
        _select(calls, selector)["arguments"].update(args)
    for selector, code in (override.get("expect_error") or {}).items():
        _select(calls, selector)["expect_error"] = code


def recipe(industry: str, task_id: str, task: dict[str, Any],
           overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = load_overrides() if overrides is None else overrides
    calls = base_calls(task)
    RULES[industry](calls, task)
    fill_required(calls, tool_schemas(industry), traits_of(task), industry)
    apply_overrides(calls, overrides.get(f"{industry}/{task_id}") or {})
    return {
        "task": task_id,
        "industry": industry,
        "generated_by": "verifiers/oracle_recipes.py",
        "calls": calls,
    }


def oracle_path(industry: str, task_id: str) -> Path:
    return INDUSTRY_ROOT / industry / "tasks" / task_id / "solution" / "oracle.json"


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--industry", action="append", dest="industries", choices=INDUSTRIES)
    parser.add_argument("--task", help="Only this task id")
    parser.add_argument("--check", action="store_true",
                        help="Do not write; exit 1 if any oracle.json differs from the generator")
    parser.add_argument("--print", action="store_true", dest="show", help="Print recipes to stdout")
    args = parser.parse_args(argv)
    overrides = load_overrides()
    stale: list[str] = []
    for industry in args.industries or INDUSTRIES:
        for task_id, task in tasks(industry):
            if args.task and task_id != args.task:
                continue
            doc = recipe(industry, task_id, task, overrides)
            text = render(doc)
            path = oracle_path(industry, task_id)
            if args.show:
                print(text)
            if args.check:
                if not path.is_file() or path.read_text() != text:
                    stale.append(f"{industry}/{task_id}")
            elif not args.show:
                path.parent.mkdir(exist_ok=True)
                path.write_text(text)
    if stale:
        print("stale oracle.json (rerun verifiers/oracle_recipes.py):\n  " + "\n  ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
