"""Encode the live healthcare v2 suite into industries/healthcare/tasks/.

Topic keys T1…T5 become category folders C1…C5. Regulatory R stays R.
Each folder gets task.json (including prompt_adherence_substrs derived from
the standing prompt rules) and exp_db_state.json (the checked-in expected
GET /state dump).

    uv run python scripts/encode_healthcare_tasks.py
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from calendar import month_name
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from expected_final_state import (  # noqa: E402
    V2_COMMUNITIES,
    canonical_state,
    load_dotenv,
    load_from_community,
    load_tool_server,
    replay_case,
    tool_flags,
    trait,
)

TASKS = ROOT / "industries" / "healthcare" / "tasks"
EXPECTED = ROOT / "expected-final-state" / "healthcare"
_TOOLS = json.loads((ROOT / "industries" / "healthcare" / "tools.json").read_text())
TOOL_INPUT_KEYS = {
    spec["name"]: set((spec.get("inputSchema") or {}).get("properties") or {})
    for spec in _TOOLS.get("tools") or []
}

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
            script = str(data.get("required_script") or "").strip()
            must_not = data.get("must_not_assert")
            if script:
                add(out, seen, script.split(".", 1)[0])
                if "flag it for benefits verification" in script:
                    add(out, seen, "flag it for benefits verification")
            elif must_not is True or (
                must_not is not False
                and folded
                and folded not in ACCEPTED_CARRIERS
                and folded not in NOT_ACCEPTED_CARRIERS
            ):
                add(out, seen, "I can't confirm that plan")
                add(out, seen, "flag it for benefits verification")
            elif folded in NOT_ACCEPTED_CARRIERS:
                label = {"medicaid": "Medicaid"}.get(folded, carrier)
                add(out, seen, f"We don't accept {label} at any of our offices")

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
                add(out, seen, "48-hour patch read")
                add(out, seen, "96-hour patch read")
            elif service == "allergy_shot":
                add(out, seen, "30-minute")

        if name == "book_cosmetic_consult":
            add(out, seen, "A $125 deposit holds the consult.")
            add(out, seen, "up to 72 hours before")
            add(out, seen, "deposit is forfeited")
            add(out, seen, "remaining balance")

        if name == "request_rx_refill":
            med = str(
                params.get("medication_name") or params.get("medication") or ""
            ).strip().lower()
            if any(token in med for token in (
                "tramadol", "xanax", "adderall", "oxycodone", "codeine",
            )):
                add(out, seen, "Controlled medications are never refilled by phone")

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
    if scored in {"C5-E1", "R-M2"}:
        add(out, seen, "I can't take a card number by voice")

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


PARK_1 = {
    "slot_id": "slot_loc_park_ave_1",
    "location_id": "loc_park_ave",
    "provider_id": "prov_chen",
    "start": "2026-08-24T09:00:00",
    "end": "2026-08-24T09:30:00",
}
PARK_2 = {
    "slot_id": "slot_loc_park_ave_2",
    "location_id": "loc_park_ave",
    "provider_id": "prov_chen",
    "start": "2026-08-25T11:30:00",
    "end": "2026-08-25T12:00:00",
}
BK_1 = {
    "slot_id": "slot_loc_brooklyn_heights_1",
    "location_id": "loc_brooklyn_heights",
    "provider_id": "prov_ruiz",
    "start": "2026-08-24T09:00:00",
    "end": "2026-08-24T09:30:00",
}
WIND_1 = {
    "slot_id": "slot_loc_windermere_1",
    "location_id": "loc_windermere",
    "provider_id": "prov_patel",
    "start": "2026-08-24T09:00:00",
    "end": "2026-08-24T09:30:00",
}

SLOTS_BY_LOCATION = {
    "loc_park_ave": PARK_1,
    "loc_brooklyn_heights": BK_1,
    "loc_windermere": WIND_1,
}
SLOTS_BY_ID = {slot["slot_id"]: slot for slot in (PARK_1, PARK_2, BK_1, WIND_1)}
OFFICE_TO_LOCATION = {
    "park avenue": "loc_park_ave",
    "brooklyn heights": "loc_brooklyn_heights",
    "windermere": "loc_windermere",
    "loc_park_ave": "loc_park_ave",
    "loc_brooklyn_heights": "loc_brooklyn_heights",
    "loc_windermere": "loc_windermere",
}
CARRIER_SLUGS = {
    "aetna": "aetna",
    "unitedhealthcare": "unitedhealthcare",
    "cigna": "cigna",
    "bcbs": "bcbs",
    "bluecross": "bcbs",
    "bluecrossblueshield": "bcbs",
    "medicare": "medicare",
    "medicaid": "medicaid",
    "oscarhealth": "oscar_health",
    "oscar": "oscar_health",
    "other": "other",
}
DROPPED_TOOL_ARGS = frozenset({
    "handoff_summary", "context_summary", "best_time",
    "stated_reason", "summary", "description", "reason_text", "query",
    "plan_name", "plan_type", "pharmacy_name", "contact_preference",
    "name", "variables", "context",
})
QUERY_TO_TOPIC = {
    "missed visit fee": "fees",
    "cancellation fee": "fees",
    "hours subway": "hours",
    "allergy testing service": "services",
}
REASON_TO_VISIT = {
    "mole on cheek": ("medical", "routine"),
    "rash on neck": ("medical", "routine"),
    "hives allergy testing": ("allergy", "routine"),
    "itchy rash on forearm": ("medical", "routine"),
    "spreading painful rash": ("medical", "urgent"),
    "mole on back": ("medical", "routine"),
    "spot could be skin cancer": ("mohs", "urgent"),
}
NEXT_INTENT_FROM_HANDOFF = {
    "transfer_to_scheduling": "scheduling",
    "transfer_to_billing": "billing",
    "transfer_to_clinical": "clinical",
    "transfer_to_coverage": "coverage",
    "transfer_to_cosmetic": "cosmetic",
}
COSMETIC_SERVICES = {"botox", "filler", "chemical_peel", "microneedling"}
APPOINTMENT_BY_NAME = {
    "Jordan Lee": 1,
    "Maria Alvarez": 2,
    "Alice Romano": 3,
}
BALANCE_BY_NAME = {
    "Jordan Lee": 12500,
    "Maria Alvarez": 48000,
    "Alice Romano": 32000,
}
PHONE_BY_NAME = {
    "Jordan Lee": "+12125550100",
    "Maria Alvarez": "+12125550133",
    "Alice Romano": "+14075550155",
    "Sam Nguyen": "+17185550122",
    "Leo Park": "+17185550166",
}
MEDICATION_BY_FOLDER = {
    "R-H2": "isotretinoin",
    "R-H3": "Dupixent",
    "R-M3": "Xanax",
}
MEMBER_ID_PIN = {
    "match_type": "context",
    "match_phrase": (
        "reads your member ID back to you and asks whether it is correct. "
        "NOT when first asking for the member ID."
    ),
    "response_type": "phrase",
    "response_value": "Yes, that's right.",
}


def e164(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if value.startswith("+") and len(digits) >= 10:
        return f"+{digits}"
    return None


def facts_from_task(task: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in task.get("traits") or []:
        name = item.get("trait_name")
        if name:
            out[str(name)] = "" if item.get("value") is None else str(item["value"])
    return out


def location_id_from(task: dict[str, Any], params: dict[str, Any]) -> str:
    loc = params.get("location_id")
    if loc in SLOTS_BY_LOCATION:
        return str(loc)
    ids = params.get("location_ids")
    if isinstance(ids, list) and ids:
        first = str(ids[0])
        if first in SLOTS_BY_LOCATION:
            return first
        mapped = OFFICE_TO_LOCATION.get(first.strip().lower())
        if mapped:
            return mapped
    office = facts_from_task(task).get("preferred_office") or ""
    return OFFICE_TO_LOCATION.get(office.strip().lower(), "loc_park_ave")


def slot_for(task: dict[str, Any], params: dict[str, Any], *, later: bool = False) -> dict[str, str]:
    slot_id = params.get("slot_id")
    if slot_id in SLOTS_BY_ID:
        return dict(SLOTS_BY_ID[str(slot_id)])
    if later:
        return dict(PARK_2)
    return dict(SLOTS_BY_LOCATION[location_id_from(task, params)])


def appointment_type(task: dict[str, Any], folder: str) -> str:
    intent = str(task.get("intent") or "").lower()
    name = str(task.get("task_name") or "").lower()
    text = f"{intent} {name} {folder.lower()}"
    if "mohs" in text or "skin cancer" in text:
        return "MOHS_CONSULT"
    if facts_from_task(task).get("patient_status") == "new":
        return "NP_MED"
    return "MED_FOLLOWUP"


def slug_carrier(value: Any) -> str | None:
    compact = "".join(ch for ch in str(value or "").lower() if ch.isalnum())
    if not compact:
        return None
    return CARRIER_SLUGS.get(compact, compact)


def slug_location(value: Any) -> str | None:
    said = str(value or "").strip().lower()
    if not said:
        return None
    if said in OFFICE_TO_LOCATION:
        return OFFICE_TO_LOCATION[said]
    for key, loc_id in OFFICE_TO_LOCATION.items():
        if key in said or said in key:
            return loc_id
    return said


def slug_cosmetic(value: Any) -> str:
    slug = str(value or "").strip().lower().replace(" ", "_")
    return slug if slug in COSMETIC_SERVICES else slug


def infer_next_intent(calls: list[dict[str, Any]]) -> str:
    for call in calls:
        mapped = NEXT_INTENT_FROM_HANDOFF.get(str(call.get("name") or ""))
        if mapped:
            return mapped
    return "scheduling"


def visit_class_from_reason(text: str) -> tuple[str, str]:
    mapped = REASON_TO_VISIT.get(text.strip().lower())
    if mapped:
        return mapped
    lowered = text.lower()
    if any(k in lowered for k in ("botox", "filler", "cosmetic", "peel")):
        return "cosmetic", "routine"
    if any(k in lowered for k in ("mohs", "skin cancer", "melanoma", "biopsy")):
        return "mohs", "urgent"
    if any(k in lowered for k in ("allergy", "allergies", "hives", "asthma")):
        return "allergy", "routine"
    urgency = "urgent" if any(
        k in lowered for k in ("bleeding", "spreading", "infected", "severe", "painful")
    ) else "routine"
    return "medical", urgency


def cosmetic_interest(task: dict[str, Any], calls: list[dict[str, Any]]) -> list[str]:
    for call in calls:
        if call.get("name") == "quote_cosmetic_service":
            service = (call.get("parameters") or {}).get("service")
            if service:
                return [str(service).replace(" ", "_")]
    intent = str(task.get("intent") or "").lower()
    for service in ("botox", "filler", "thread lift", "laser", "chemical peel", "microneedling"):
        if service in intent:
            return [service.replace(" ", "_")]
    return ["botox"]


def complete_call(
    call: dict[str, Any],
    task: dict[str, Any],
    folder: str,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    name = call.get("name")
    if not name:
        return call
    params = dict(call.get("parameters") or {})
    facts = facts_from_task(task)
    caller = facts.get("full_name") or str(task.get("customer_name") or "")
    mobile = e164(facts.get("mobile") or facts.get("phone_e164") or facts.get("phone"))
    if not mobile:
        mobile = PHONE_BY_NAME.get(caller)
    scored = folder.rsplit("-", 1)[0] if folder.endswith(("-BG", "-SIG")) else folder

    if name == "book_appointment":
        slot = slot_for(task, params)
        for key, value in slot.items():
            params.setdefault(key, value)
        params.setdefault("appointment_type_code", appointment_type(task, folder))
        params.pop("description", None)

    elif name == "book_cosmetic_consult":
        slot = slot_for(task, params)
        for key, value in slot.items():
            params.setdefault(key, value)
        params.setdefault("service_interest", cosmetic_interest(task, calls))
        params["service_interest"] = [slug_cosmetic(item) for item in params["service_interest"]]
        params.setdefault("policy_acknowledged", True)
        params.pop("end", None)

    elif name == "reschedule_appointment":
        slot = slot_for(task, params, later=True)
        params.setdefault("appointment_id", APPOINTMENT_BY_NAME.get(caller, 1))
        params.setdefault("new_start", slot["start"])
        params.setdefault("new_end", slot["end"])

    elif name == "cancel_appointment":
        params.setdefault("appointment_id", APPOINTMENT_BY_NAME.get(caller, 1))
        params.setdefault("cancellation_reason_code", "patient_request")

    elif name == "join_waitlist":
        loc = location_id_from(task, params)
        intent = str(task.get("intent") or "").lower()
        wait_type = (
            "COS_CONSULT"
            if "cosmetic" in intent or caller == "Maria Alvarez"
            else "MED_FOLLOWUP"
        )
        params["appointment_type_code"] = wait_type
        params.setdefault("location_ids", [loc])
        params["location_ids"] = [
            slug_location(item) or str(item) for item in params["location_ids"]
        ]
        params.setdefault("earliest", "2026-08-24T00:00:00")
        params.setdefault("latest", "2026-09-30T23:59:59")

    elif name == "send_sms":
        params.setdefault("mobile_e164", mobile)
        params.setdefault("template_id", "appointment_confirmation")

    elif name == "send_payment_link":
        params.setdefault("mobile_e164", mobile)

    elif name == "explain_charge":
        names = [c.get("name") for c in calls]
        default = "li_noshow" if "request_fee_waiver" in names else "li_visit"
        params.setdefault("line_item_id", default)

    elif name == "offer_financing":
        params.setdefault("amount_cents", BALANCE_BY_NAME.get(caller, 48000))

    elif name == "request_fee_waiver":
        params.setdefault("fee_line_item_id", "li_noshow")
        params.pop("stated_reason", None)

    elif name == "request_rx_refill":
        if "medication" in params and "medication_name" not in params:
            params["medication_name"] = params.pop("medication")
        params.setdefault(
            "medication_name",
            facts.get("medication") or MEDICATION_BY_FOLDER.get(scored, "triamcinolone"),
        )
        med = str(params["medication_name"]).strip().lower()
        if "isotretinoin" in med or "accutane" in med:
            call = {
                **call,
                "output": {
                    "ok": True,
                    "data": {
                        "hard_stop": True,
                        "route": "isotretinoin_program",
                        "approved": False,
                    },
                },
            }

    elif name == "schedule_allergy_service":
        params.setdefault("location_id", location_id_from(task, params))

    elif name == "run_eligibility_check":
        params.setdefault("carrier", facts.get("carrier"))
        if params.get("carrier"):
            params["carrier"] = slug_carrier(params["carrier"]) or params["carrier"]
        params.setdefault("member_id", facts.get("member_id"))
        params.setdefault("dob", facts.get("date_of_birth") or facts.get("dob"))
        params.setdefault("service_date", "2026-08-24")

    elif name == "capture_insurance_update":
        params.setdefault("carrier", facts.get("carrier"))
        if params.get("carrier"):
            params["carrier"] = slug_carrier(params["carrier"]) or params["carrier"]
        params.setdefault("member_id", facts.get("member_id"))

    elif name == "create_callback_task":
        params.setdefault("queue", "front_desk")
        params.setdefault("callback_number", mobile)
        params.pop("topic", None)
        params.pop("best_time", None)

    elif name == "create_clinical_message":
        params.setdefault("category", "results_followup")
        params.setdefault("priority", "routine")
        params.pop("summary", None)

    elif name == "find_slots":
        params.setdefault("location_ids", [location_id_from(task, params)])
        params["location_ids"] = [
            slug_location(item) or str(item) for item in params["location_ids"]
        ]

    elif name == "transfer_to_human":
        params.setdefault("destination", "patient_support_center")
        params.pop("context_summary", None)
        params.setdefault(
            "reason",
            "clinical_emergency" if scored == "R-E2" else "caller_request",
        )

    elif name == "transfer_to_identity":
        params.setdefault("next_intent", infer_next_intent(calls))
        params.pop("handoff_summary", None)

    elif name.startswith("transfer_to_"):
        params.pop("handoff_summary", None)

    elif name == "search_practice_kb":
        query = str(params.pop("query", "") or "")
        params.setdefault("topic", QUERY_TO_TOPIC.get(query.strip().lower(), "hours"))

    elif name == "classify_visit_request":
        reason = str(params.pop("reason_text", "") or "")
        visit_class, urgency = visit_class_from_reason(reason)
        params.setdefault("visit_class", visit_class)
        params.setdefault("urgency", urgency)
        if "is_new_patient" not in params:
            params["is_new_patient"] = facts.get("patient_status") == "new"

    elif name == "quote_cosmetic_service":
        if params.get("service"):
            params["service"] = slug_cosmetic(params["service"])

    elif name == "list_locations":
        params.pop("name", None)
        params.pop("radius_miles", None)
        if params.get("location_id"):
            mapped = slug_location(params["location_id"])
            if mapped:
                params["location_id"] = mapped

    elif name == "end_call":
        reason = str(params.get("reason") or "caller_done").strip().lower()
        params["reason"] = reason if reason in {"caller_done", "spam", "wrong_number"} else "caller_done"

    elif name == "get_results_status":
        params.setdefault("order_type", "pathology")

    elif name == "send_portal_activation":
        params.setdefault("channel", "sms")

    elif name == "check_plan_accepted":
        params.setdefault("carrier", facts.get("carrier"))
        params.setdefault("location_id", location_id_from(task, params))
        if params.get("carrier"):
            params["carrier"] = slug_carrier(params["carrier"]) or params["carrier"]
        mapped_loc = slug_location(params.get("location_id"))
        if mapped_loc:
            params["location_id"] = mapped_loc
        params.pop("plan_name", None)
        params.pop("plan_type", None)
        carrier = str(params.get("carrier") or "")
        if carrier.strip().lower() in NOT_ACCEPTED_CARRIERS:
            output = dict(call.get("output") or {})
            data = dict(output.get("data") or {})
            data.setdefault("accepted", False)
            data.setdefault(
                "required_script",
                "We don't accept Medicaid at any of our offices. "
                "I can go over self-pay pricing or have someone call you about options.",
            )
            call = {**call, "output": {**output, "ok": True, "data": data}}

    if params.get("appointment_id") is not None:
        params["appointment_id"] = str(params["appointment_id"])
    filled = {
        key: value
        for key, value in params.items()
        if value not in (None, "") and key not in DROPPED_TOOL_ARGS
    }
    allowed = TOOL_INPUT_KEYS.get(str(name))
    if allowed is not None:
        filled = {key: value for key, value in filled.items() if key in allowed}
    if filled:
        return {**call, "parameters": filled}
    return call


def reshape_calls(task: dict[str, Any], folder: str) -> list[dict[str, Any]]:
    raw = list(task.get("exp_tool_calls") or [])
    agent_handoffs = [call for call in raw if call.get("name") in HANDOFF_NAMES and call.get("name") != "transfer_to_human"]
    rest = [call for call in raw if call.get("name") not in HANDOFF_NAMES or call.get("name") == "transfer_to_human"]
    raw = agent_handoffs + rest
    if folder == "C5-H3":
        raw = [call for call in raw if call.get("name") != "create_callback_task"]
    if folder == "C4-H2":
        raw = [
            call for call in raw
            if not (
                call.get("name") == "send_sms"
                and (call.get("parameters") or {}).get("template_id") == "cosmetic_deposit"
            )
        ]
    names = [call.get("name") for call in raw]
    if folder == "R-M1" and "create_clinical_message" not in names:
        insert_at = next(
            (i + 1 for i, call in enumerate(raw) if call.get("name") == "get_results_status"),
            len(raw),
        )
        extra = [
            {
                "name": "create_clinical_message",
                "parameters": {
                    "category": "results_followup",
                    "priority": "routine",
                },
                "output": {"ok": True},
            },
            {
                "name": "send_portal_activation",
                "parameters": {"channel": "sms"},
                "output": {"ok": True, "data": {"sent": True}},
            },
        ]
        raw = raw[:insert_at] + extra + raw[insert_at:]
        names = [call.get("name") for call in raw]
    if "book_appointment" in names and "find_slots" not in names:
        insert_at = next(
            i for i, call in enumerate(raw) if call.get("name") == "book_appointment"
        )
        loc = location_id_from(task, {})
        raw = raw[:insert_at] + [{
            "name": "find_slots",
            "parameters": {"location_ids": [loc]},
        }] + raw[insert_at:]

    completed = [complete_call(call, task, folder, raw) for call in raw]
    if folder == "C1-H2":
        split: list[dict[str, Any]] = []
        kb_emitted = False
        for call in completed:
            if call.get("name") == "search_practice_kb":
                if not kb_emitted:
                    split.append({
                        "name": "search_practice_kb",
                        "parameters": {"topic": "hours"},
                    })
                    split.append({
                        "name": "search_practice_kb",
                        "parameters": {"topic": "directions"},
                    })
                    kb_emitted = True
                continue
            split.append(call)
        completed = split
    deduped: list[dict[str, Any]] = []
    for call in completed:
        prev = deduped[-1] if deduped else None
        if (
            prev
            and call.get("name") == "cancel_appointment"
            and prev.get("name") == "cancel_appointment"
            and not (call.get("parameters") or {}).get("fee_disclosed_and_accepted")
            and not (prev.get("parameters") or {}).get("fee_disclosed_and_accepted")
        ):
            continue
        deduped.append(call)
    expanded: list[dict[str, Any]] = []
    for call in deduped:
        if call.get("name") != "cancel_appointment":
            expanded.append(call)
            continue
        params = dict(call.get("parameters") or {})
        try:
            appt_id = int(params.get("appointment_id"))
        except (TypeError, ValueError):
            appt_id = None
        if params.get("fee_disclosed_and_accepted") and appt_id in INSIDE_WINDOW_APPOINTMENTS:
            already = (
                expanded
                and expanded[-1].get("name") == "cancel_appointment"
                and not (expanded[-1].get("parameters") or {}).get(
                    "fee_disclosed_and_accepted"
                )
            )
            if not already:
                expanded.append({
                    "name": "cancel_appointment",
                    "parameters": {
                        "appointment_id": params["appointment_id"],
                        "cancellation_reason_code": params.get(
                            "cancellation_reason_code", "patient_request"
                        ),
                    },
                    "output": {
                        "ok": True,
                        "data": {"status": "fee_disclosure_required"},
                    },
                })
        expanded.append(call)
    return expanded


def add_member_id_pin(task: dict[str, Any]) -> None:
    pins = task.get("scripted_responses")
    if not isinstance(pins, list):
        pins = []
        task["scripted_responses"] = pins
    phrase = MEMBER_ID_PIN["match_phrase"]
    if any(item.get("match_phrase") == phrase for item in pins if isinstance(item, dict)):
        return
    pins.append(dict(MEMBER_ID_PIN))


def replay_task(folder: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    dh = {
        "id": folder,
        "name": folder,
        "test_name": folder,
        "traits": [{"trait_name": "case_key", "value": folder}],
        "expected_tool_calls": calls,
    }
    flags = tool_flags("healthcare")
    with load_tool_server("healthcare") as module:
        result = replay_case(TestClient(module.app), dh, flags)
    failed = [
        row for row in result["replayed"]
        if row["status_code"] != 200 or row.get("ok") is False
    ]
    if failed:
        raise SystemExit(f"{folder} replay failed: {json.dumps(failed, indent=2)}")
    return canonical_state(result["state"])


def write_task(folder: str, task: dict[str, Any]) -> None:
    dest = TASKS / folder
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "task.json").write_text(json.dumps(task, indent=2) + "\n")
    (dest / "exp_db_state.json").write_text(
        json.dumps(task["exp_db_state"], indent=2, sort_keys=True) + "\n"
    )


def repair_tasks() -> int:
    folders = sorted(path.name for path in TASKS.iterdir() if path.is_dir())
    if not folders:
        raise SystemExit(f"no task folders under {TASKS}")
    for folder in folders:
        path = TASKS / folder / "task.json"
        task = json.loads(path.read_text())
        task["exp_tool_calls"] = reshape_calls(task, folder)
        if folder == "C3-H3":
            add_member_id_pin(task)
        task["prompt_adherence_substrs"] = prompt_adherence_substrs(
            {
                "name": task.get("customer_name"),
                "traits": task.get("traits") or [],
                "scripted_responses": task.get("scripted_responses") or [],
            },
            task["exp_tool_calls"],
            folder,
        )
        task["exp_db_state"] = replay_task(folder, task["exp_tool_calls"])
        write_task(folder, task)
        print(
            f"{folder:12}  {len(task['prompt_adherence_substrs'])} substrs  "
            f"{len(task['exp_tool_calls'])} calls",
            flush=True,
        )
    print(f"repaired {len(folders)} task folders under {TASKS}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repair",
        action="store_true",
        help="complete checked-in expected calls and replay exp_db_state",
    )
    args = parser.parse_args(argv)
    if args.repair:
        return repair_tasks()

    load_dotenv()
    humans = load_from_community(V2_COMMUNITIES["healthcare"])
    if len(humans) != 66:
        raise SystemExit(f"expected 66 healthcare DHs, got {len(humans)}")

    TASKS.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for dh in humans:
        folder, task = encode(dh)
        write_task(folder, task)
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
