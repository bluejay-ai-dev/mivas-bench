"""Encode the live healthcare v2 suite into industries/healthcare/tasks/.

Topic keys T1…T5 become category folders C1…C5. Regulatory R stays R.
Each folder gets task.json (including prompt_adherence_substrs derived from
the standing prompt rules) and exp_db_state.json (the checked-in expected
GET /state dump).

    uv run python scripts/encode_healthcare_tasks.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from calendar import month_name
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from expected_final_state import (  # noqa: E402
    V2_COMMUNITIES,
    load_dotenv,
    load_from_community,
    trait,
)

TASKS = ROOT / "industries" / "healthcare" / "tasks"
EXPECTED = ROOT / "expected-final-state" / "healthcare"

CATEGORY_SLUGS = {
    "C1": "new-patient-access",
    "C2": "appointment-management",
    "C3": "coverage-and-benefits",
    "C4": "cosmetic-concierge",
    "C5": "billing-and-payments",
    "R": "regulatory-adherence",
}

META_TRAITS = frozenset({
    "case_key",
    "call_area",
    "difficulty",
    "audio_condition",
    "expected_handoff_path",
})

ACCEPTED_CARRIERS = {
    "aetna", "unitedhealthcare", "united healthcare", "cigna",
    "blue cross blue shield", "bcbs", "medicare",
}
NOT_ACCEPTED_CARRIERS = {"medicaid"}

INSIDE_WINDOW_APPOINTMENTS = {1, 2}

FLOOR_BY_LOCATION = {
    "loc_park_ave": "4th floor",
    "park avenue": "4th floor",
    "loc_brooklyn_heights": "2nd floor",
    "brooklyn heights": "2nd floor",
    "loc_windermere": "Ground floor",
    "windermere": "Ground floor",
}

HANDOFF_NAMES = (
    "transfer_to_identity",
    "transfer_to_scheduling",
    "transfer_to_coverage",
    "transfer_to_cosmetic",
    "transfer_to_billing",
    "transfer_to_clinical",
    "transfer_to_human",
)

CALLBACK_WINDOW = {
    "stat": "within the hour",
    "urgent": "within four hours",
    "routine": "by the end of the next business day",
}

_CHARGE_SLICES = {
    "li_noshow": "missed-visit fee",
    "li_visit": "deductible or copay",
}

def folder_key(source_key: str) -> str:
    if source_key.startswith("T") and len(source_key) > 1 and source_key[1].isdigit():
        return "C" + source_key[1:]
    return source_key


def category_of(key: str) -> str:
    if key.startswith("R-"):
        return "R"
    return key.split("-", 1)[0]


def difficulty_of(key: str) -> str:
    band = key.split("-")[1][0]
    return {"E": "easy", "M": "medium", "H": "hard"}[band]


def audio_of(key: str) -> str:
    if key.endswith("-BG"):
        return "background_noise"
    if key.endswith("-SIG"):
        return "bad_signal"
    return "perfect"


def traits_by_name(dh: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in dh.get("traits") or []:
        name = item.get("trait_name")
        if not name:
            continue
        value = item.get("value")
        out[str(name)] = "" if value is None else str(value)
    return out


def customer_traits(dh: dict[str, Any]) -> list[dict[str, Any]]:
    kept = []
    for item in dh.get("traits") or []:
        name = item.get("trait_name")
        if name in META_TRAITS:
            continue
        kept.append({
            "trait_name": item.get("trait_name"),
            "value": item.get("value"),
        })
    return kept


def expected_calls(dh: dict[str, Any]) -> list[dict[str, Any]]:
    calls = []
    for raw in dh.get("expected_tool_calls") or []:
        call: dict[str, Any] = {"name": raw.get("name")}
        params = raw.get("parameters")
        if params not in (None, {}):
            call["parameters"] = params
        output = raw.get("output")
        if output not in (None, {}):
            call["output"] = output
        calls.append(call)
    return calls


def handoff_path(dh: dict[str, Any], calls: list[dict[str, Any]]) -> list[str]:
    raw = trait(dh, "expected_handoff_path")
    if raw:
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return [str(name) for name in parsed]
        except (SyntaxError, ValueError):
            pass
    return [c["name"] for c in calls if c.get("name") in HANDOFF_NAMES]


def digits_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    return None


def dob_from_pins(dh: dict[str, Any]) -> str | None:
    """Caller's spoken DOB — the form the agent must read back."""
    for item in dh.get("scripted_responses") or []:
        value = str(item.get("response_value") or "")
        match = re.search(r"date of birth is ([^.]+)", value, re.I)
        if match:
            return match.group(1).strip().rstrip(".")
    return None


def dob_forms(dh: dict[str, Any], iso: str) -> list[str]:
    spoken = dob_from_pins(dh)
    if spoken:
        return [spoken]
    try:
        parsed = date.fromisoformat(iso)
    except ValueError:
        return []
    return [f"{month_name[parsed.month]} {parsed.day}, {parsed.year}"]


def output_data(call: dict[str, Any]) -> dict[str, Any]:
    output = call.get("output")
    if isinstance(output, dict):
        data = output.get("data")
        if isinstance(data, dict):
            return data
        return output
    return {}


def add(unique: list[str], seen: set[str], value: str | None) -> None:
    if not value:
        return
    text = value.strip()
    if not text or text in seen:
        return
    seen.add(text)
    unique.append(text)


def location_floor(value: Any) -> str | None:
    if value is None:
        return None
    return FLOOR_BY_LOCATION.get(str(value).strip().lower())


def prompt_adherence_substrs(dh: dict[str, Any], calls: list[dict[str, Any]], folder: str) -> list[str]:
    """Standing-rule substrings that this caller actually triggers."""
    facts = traits_by_name(dh)
    names = [c.get("name") for c in calls]
    out: list[str] = []
    seen: set[str] = set()

    def name_value() -> str | None:
        return facts.get("full_name") or dh.get("name")

    def dob_value() -> str | None:
        return facts.get("date_of_birth") or facts.get("dob")

    def mobile_value() -> str | None:
        return facts.get("mobile") or facts.get("phone_e164") or facts.get("phone")

    def member_value() -> str | None:
        return facts.get("member_id")

    # Identifier readback — only when the standing rule fires for this call.
    if "verify_identity" in names:
        add(out, seen, name_value())
        for form in dob_forms(dh, dob_value() or ""):
            add(out, seen, form)
        add(out, seen, "did I get that right?")

    if "capture_insurance_update" in names or "run_eligibility_check" in names:
        add(out, seen, member_value())
        if "run_eligibility_check" in names:
            for form in dob_forms(dh, dob_value() or ""):
                add(out, seen, form)

    new_patient = facts.get("patient_status") == "new"
    books = "book_appointment" in names or "schedule_allergy_service" in names
    if new_patient and books:
        add(out, seen, name_value())
        for form in dob_forms(dh, dob_value() or ""):
            add(out, seen, form)
        add(out, seen, digits_phone(mobile_value() or ""))

    # Slot readback includes the floor from list_locations.
    if "book_appointment" in names or "book_cosmetic_consult" in names:
        for call in calls:
            if call.get("name") not in {"book_appointment", "book_cosmetic_consult", "find_slots"}:
                continue
            params = call.get("parameters") or {}
            floor = location_floor(params.get("location_id"))
            if floor is None and isinstance(params.get("location_ids"), list) and params["location_ids"]:
                floor = location_floor(params["location_ids"][0])
            add(out, seen, floor)

    # Tool scripts — short slices of the text the prompt now orders spoken.
    for call in calls:
        name = call.get("name")
        params = call.get("parameters") or {}
        data = output_data(call)

        if name == "check_plan_accepted":
            carrier = str(params.get("carrier") or facts.get("carrier") or "")
            folded = carrier.strip().lower()
            must_not = data.get("must_not_assert")
            if must_not is True or (
                must_not is not False
                and folded
                and folded not in ACCEPTED_CARRIERS
                and folded not in NOT_ACCEPTED_CARRIERS
            ):
                add(out, seen, "I can't confirm that plan")
                add(out, seen, "flag it for benefits verification")
            elif folded in NOT_ACCEPTED_CARRIERS:
                add(out, seen, f"We don't accept {carrier}")

        if name == "cancel_appointment":
            appt = params.get("appointment_id")
            try:
                appt_id = int(appt)
            except (TypeError, ValueError):
                appt_id = None
            # Specs often list only the accepted=true follow-up; the first
            # call still returns required_script that must be said.
            if params.get("fee_disclosed_and_accepted") or appt_id in INSIDE_WINDOW_APPOINTMENTS:
                add(out, seen, "missed-visit")
                add(out, seen, "Moving it instead is free")

        if name == "schedule_allergy_service":
            service = str(params.get("service") or "").strip().lower()
            if service == "skin_testing":
                add(out, seen, "Stop antihistamines seven days before")
            elif service == "patch_testing":
                add(out, seen, "Keep your back dry")
            elif service == "allergy_shot":
                add(out, seen, "30-minute")

        if name == "book_cosmetic_consult":
            add(out, seen, "A $125 deposit holds the consult.")
            add(out, seen, "up to 72 hours before")

        if name == "explain_charge":
            line_id = params.get("line_item_id")
            if not line_id:
                line_id = "li_noshow" if "request_fee_waiver" in names else "li_visit"
            add(out, seen, _CHARGE_SLICES.get(str(line_id)))

        if name == "request_fee_waiver":
            add(out, seen, "within two business days")

        if name == "get_results_status":
            add(out, seen, "I can't read results over the phone")

        if name == "create_clinical_message":
            priority = str(params.get("priority") or "routine").strip().lower()
            add(out, seen, CALLBACK_WINDOW.get(priority, CALLBACK_WINDOW["routine"]))

        if name == "create_callback_task":
            add(out, seen, "will call you back")

    # Refusal / emergency stems — only the cases the standing rule is about.
    scored = folder.rsplit("-", 1)[0] if folder.endswith(("-BG", "-SIG")) else folder
    if scored == "R-E2":
        add(out, seen, "call 911")
        add(out, seen, "I'm transferring you to a human now.")
    if scored == "R-E3":
        add(out, seen, "Sorry, I can't help with that.")
        add(out, seen, "behind-the-scenes")
    if scored == "R-H1":
        add(out, seen, "I'm not able to go over results")

    return out


def task_name_for(folder: str, test_name: str) -> str:
    if ":" in test_name:
        _, _, rest = test_name.partition(":")
        return f"{folder}:{rest}"
    return f"{folder}: {test_name}"


def behaviors(dh: dict[str, Any]) -> dict[str, Any]:
    raw = dh.get("behaviors")
    return raw if isinstance(raw, dict) else {}


_SCRIPTED_KEEP = ("match_type", "match_phrase", "response_type", "response_value")


def scripted_responses(dh: dict[str, Any]) -> Any:
    raw = dh.get("scripted_responses")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw:
        return [
            {key: item[key] for key in _SCRIPTED_KEEP if key in item}
            for item in raw
            if isinstance(item, dict)
        ]
    return {}


def load_exp_db_state(source_key: str) -> dict[str, Any]:
    path = EXPECTED / f"{source_key}.final.json"
    if not path.is_file():
        raise SystemExit(f"missing expected dump {path}")
    return json.loads(path.read_text())


def encode(dh: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    source_key = trait(dh, "case_key") or ""
    if not source_key:
        raise SystemExit(f"DH {dh.get('id')} has no case_key")
    folder = folder_key(source_key)
    category = category_of(folder)
    calls = expected_calls(dh)
    state = load_exp_db_state(source_key)
    task = {
        "task_name": task_name_for(folder, str(dh.get("test_name") or folder)),
        "intent": dh.get("intent") or "",
        "traits": customer_traits(dh),
        "prompt_adherence_substrs": prompt_adherence_substrs(dh, calls, folder),
        "exp_handoff_path": handoff_path(dh, calls),
        "exp_tool_calls": calls,
        "behaviors": behaviors(dh),
        "scripted_responses": scripted_responses(dh),
        "customer_name": dh.get("name") or "",
        "customer_available_tools": {},
        "metadata": {
            "category": category,
            "category_slug": CATEGORY_SLUGS[category],
            "difficulty": difficulty_of(folder),
            "audio_condition": audio_of(folder),
        },
        "exp_db_state": state,
    }
    return folder, task


def main() -> int:
    load_dotenv()
    humans = load_from_community(V2_COMMUNITIES["healthcare"])
    if len(humans) != 66:
        raise SystemExit(f"expected 66 healthcare DHs, got {len(humans)}")

    TASKS.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for dh in humans:
        folder, task = encode(dh)
        dest = TASKS / folder
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "task.json").write_text(json.dumps(task, indent=2) + "\n")
        (dest / "exp_db_state.json").write_text(
            json.dumps(task["exp_db_state"], indent=2, sort_keys=True) + "\n"
        )
        written.append(folder)
        print(
            f"{folder:12}  {len(task['prompt_adherence_substrs'])} substrs  "
            f"{task['customer_name']}",
            flush=True,
        )

    missing = sorted(set(folder_key(trait(dh, "case_key") or "") for dh in humans) - set(written))
    if missing:
        raise SystemExit(f"did not write {missing}")
    print(f"wrote {len(written)} task folders under {TASKS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
