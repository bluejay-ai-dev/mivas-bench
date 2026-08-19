"""MIVAS task.json → Bluejay digital-human conversion (no live API)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "tasks_to_digital_humans", ROOT / "scripts" / "tasks_to_digital_humans.py"
)
assert _SPEC is not None and _SPEC.loader is not None
conv = importlib.util.module_from_spec(_SPEC)
sys.modules["tasks_to_digital_humans"] = conv
_SPEC.loader.exec_module(conv)

CASE_KEY_PREFIX = conv.CASE_KEY_RE


def _humans() -> list[dict]:
    return conv.build("healthcare")


def test_healthcare_emits_72_payloads() -> None:
    humans = _humans()
    assert len(humans) == 72
    keys = [conv.case_key_of(dh) for dh in humans]
    assert len(set(keys)) == 72
    conv.check(humans, "healthcare")


def test_names_are_person_only_and_test_name_from_task() -> None:
    for dh in _humans():
        key = conv.case_key_of(dh)
        first = str(dh["name"]).split()[0]
        assert not CASE_KEY_PREFIX.match(first), dh["name"]
        assert not str(dh["name"]).startswith(key)
        assert dh["test_name"].startswith(f"{key}:")


def test_case_key_trait_and_no_verifier_fields() -> None:
    for dh in _humans():
        assert conv.trait_value(dh, "case_key") == conv.case_key_of(dh)
        assert "exp_db_state" not in dh
        assert conv.trait_value(dh, "call_area")
        assert conv.trait_value(dh, "difficulty")
        assert conv.trait_value(dh, "audio_condition")
        assert conv.trait_value(dh, "expected_handoff_path") is not None


def test_scripted_responses_always_or_omitted() -> None:
    saw_pins = False
    for dh in _humans():
        pins = dh.get("scripted_responses")
        if not pins:
            assert "scripted_responses" not in dh or pins == []
            continue
        saw_pins = True
        for pin in pins:
            assert pin["occurrence_mode"] == "always"
    assert saw_pins


def test_clones_share_source_easy_voice() -> None:
    by_key = {conv.case_key_of(dh): dh for dh in _humans()}
    clones = [k for k in by_key if conv.source_case_key(k) != k]
    assert len(clones) == 12
    for key in clones:
        source = by_key[conv.source_case_key(key)]
        clone = by_key[key]
        assert (clone["accent"], clone["gender"]) == (source["accent"], source["gender"])
        assert clone["name"] == source["name"]


def test_clones_match_source_semantics_except_audio_metadata() -> None:
    tasks_dir = ROOT / "industries" / "healthcare" / "tasks"
    for clone_path in sorted(tasks_dir.glob("*-BG/task.json")) + sorted(
        tasks_dir.glob("*-SIG/task.json")
    ):
        clone_key = clone_path.parent.name
        source_key = conv.source_case_key(clone_key)
        clone = json.loads(clone_path.read_text())
        source = json.loads((tasks_dir / source_key / "task.json").read_text())

        assert {
            item["trait_name"]: item.get("value") for item in clone["traits"]
        } == {
            item["trait_name"]: item.get("value") for item in source["traits"]
        }, clone_key
        for field in (
            "intent",
            "exp_handoff_path",
            "exp_tool_calls",
            "behaviors",
            "scripted_responses",
            "customer_name",
            "customer_available_tools",
            "exp_db_state",
        ):
            assert clone.get(field) == source.get(field), f"{clone_key}: {field}"
        for field in ("category", "category_slug", "difficulty"):
            assert clone["metadata"][field] == source["metadata"][field], clone_key


def test_audio_condition_mapping() -> None:
    by_audio = Counter()
    for dh in _humans():
        audio = conv.trait_value(dh, "audio_condition")
        by_audio[audio] += 1
        mapped = conv.audio_fields(audio)
        for field, want in mapped.items():
            assert dh[field] == want
    assert by_audio["perfect"] == 60
    assert by_audio["background_noise"] == 6
    assert by_audio["bad_signal"] == 6


def test_two_four_four_per_category() -> None:
    scored: dict[str, Counter] = {}
    for dh in _humans():
        if conv.trait_value(dh, "audio_condition") != "perfect":
            continue
        area = conv.trait_value(dh, "call_area") or ""
        difficulty = conv.trait_value(dh, "difficulty") or ""
        scored.setdefault(area, Counter())[difficulty] += 1
    assert len(scored) == 6
    for area, counts in scored.items():
        assert counts == Counter({"easy": 2, "medium": 4, "hard": 4}), area


def test_date_of_birth_is_date_type() -> None:
    found = False
    for dh in _humans():
        for item in dh["traits"]:
            if item["trait_name"] == "date_of_birth":
                found = True
                assert item["trait_data_type"] == "DATE"
    assert found


def test_prior_test_name_is_mild_and_unique() -> None:
    title = "R-E1: Ask for a person"
    assert conv.prior_test_name(title, set()) == f"{title} (prior)"
    taken = {f"{title} (prior)".casefold()}
    assert conv.prior_test_name(title, taken) == f"{title} (prior 2)"
    taken.add(f"{title} (prior 2)".casefold())
    assert conv.prior_test_name(title, taken) == f"{title} (prior 3)"


def test_success_criteria_from_tools() -> None:
    assert conv.success_criteria([]) == conv.NO_TOOLS_CRITERIA
    assert conv.success_criteria([{"name": "check_plan_accepted"}]) == (
        "Success requires check_plan_accepted to have been called."
    )
    assert conv.success_criteria([
        {"name": "transfer_to_coverage"},
        {"name": "check_plan_accepted"},
    ]) == "Success requires transfer_to_coverage and check_plan_accepted to have been called."
    three = conv.success_criteria([
        {"name": "A"}, {"name": "B"}, {"name": "C"},
    ])
    assert three == "Success requires A, B, and C to have been called."
    assert three.startswith("Success requires")


def test_healthcare_creativity_is_zero() -> None:
    for dh in _humans():
        assert dh["creativity"] == 0, conv.case_key_of(dh)


def test_claim_updates_include_expected_tool_calls() -> None:
    want = _humans()[0]
    live = [{
        "id": 99,
        "test_name": want["test_name"],
        "traits": want["traits"],
        "expected_tool_calls": [],
    }]
    updates = conv.claim_updates([want], live)
    assert len(updates) == 1
    patch = updates[0]["update"]
    assert patch["expected_tool_calls"] == want["expected_tool_calls"]
    assert patch["intent"] == want["intent"]
    assert patch["success_criteria"] == want["success_criteria"]
    assert patch["test_name"] == want["test_name"]
    assert patch["traits"] == want["traits"]
    assert "scripted_responses" in patch
    assert patch["creativity"] == 0
    pinless = {**want}
    pinless.pop("scripted_responses", None)
    cleared = conv.claim_updates([pinless], live)
    assert cleared[0]["update"]["scripted_responses"] == []


def test_api_url_reads_env_after_import(monkeypatch) -> None:
    monkeypatch.setenv("BLUEJAY_API_URL", "https://example.test/v1/")
    assert conv.api_url() == "https://example.test/v1"


def test_json_emits_raw_digital_humans(capsys) -> None:
    conv.main(["--industry", "healthcare", "--json"])
    body = json.loads(capsys.readouterr().out)
    humans = body["digital_humans"]
    assert len(humans) == 72
    assert "digital_human" not in humans[0]
    assert "expected_tool_calls" in humans[0]
    assert "test_name" in humans[0]


def test_encoder_slugs_stay_inside_enums() -> None:
    spec = importlib.util.spec_from_file_location(
        "encode_healthcare_tasks", ROOT / "scripts" / "encode_healthcare_tasks.py"
    )
    assert spec is not None and spec.loader is not None
    enc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(enc)
    assert enc.slug_carrier("Humana") == "other"
    assert enc.slug_location("unknown clinic") is None
    assert enc.slug_cosmetic("laser") is None
    assert enc.slug_cosmetic("thread lift") is None
    assert enc.slug_cosmetic("botox") == "botox"
    assert enc.slug_location("Park Avenue") == "loc_park_ave"
    quoted = enc.complete_call(
        {"name": "quote_cosmetic_service", "parameters": {"service": "laser"}},
        {"intent": "quote laser", "customer_name": "Maria Alvarez", "traits": []},
        "C4-E1",
        [],
    )
    assert (quoted.get("parameters") or {}).get("service") != "laser"


def _encoder():
    spec = importlib.util.spec_from_file_location(
        "encode_healthcare_tasks", ROOT / "scripts" / "encode_healthcare_tasks.py"
    )
    assert spec is not None and spec.loader is not None
    enc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(enc)
    return enc


def test_encoder_drops_courtesy_send_sms() -> None:
    enc = _encoder()
    courtesy = {
        "intent": "Take the first time they offer, confirm it, then thank them and end the call.",
        "scripted_responses": [],
        "exp_tool_calls": [
            {"name": "book_appointment"},
            {
                "name": "send_sms",
                "parameters": {"template_id": "appointment_confirmation"},
            },
        ],
        "exp_db_state": {
            "tool_events": [
                {"kind": "sms", "payload": {"template_id": "appointment_confirmation"}},
            ],
        },
    }
    dropped = enc.drop_unneeded_send_sms(courtesy)
    assert [call["name"] for call in dropped] == ["book_appointment"]
    state = enc.drop_courtesy_sms_events({**courtesy, "exp_tool_calls": dropped})
    assert state["tool_events"] == []

    wants_confirmation = {
        "intent": (
            "After they confirm a time is on the calendar, ask them to text you "
            "an appointment confirmation."
        ),
        "scripted_responses": [
            {"response_value": "Yes, text me the appointment confirmation."},
        ],
        "exp_tool_calls": [
            {"name": "send_sms", "parameters": {"template_id": "appointment_confirmation"}},
            {"name": "book_appointment"},
        ],
    }
    kept = [call["name"] for call in enc.drop_unneeded_send_sms(wants_confirmation)]
    assert kept == ["send_sms", "book_appointment"]

    wants_address = {
        "intent": "Ask them to text you the address so you have it.",
        "scripted_responses": [
            {"response_value": "Yes, please text me the address."},
        ],
        "exp_tool_calls": [
            {"name": "send_sms", "parameters": {"template_id": "directions"}},
            {"name": "book_appointment"},
        ],
    }
    assert [call["name"] for call in enc.drop_unneeded_send_sms(wants_address)] == [
        "book_appointment",
    ]

    forbidden = {
        "intent": "Do not ask them to text you an appointment confirmation.",
        "scripted_responses": [],
        "exp_tool_calls": [
            {"name": "send_sms", "parameters": {"template_id": "appointment_confirmation"}},
            {"name": "book_appointment"},
        ],
    }
    assert [call["name"] for call in enc.drop_unneeded_send_sms(forbidden)] == [
        "book_appointment",
    ]

    deposit = {
        "intent": "Ask them to text you the deposit link so you can pay it.",
        "scripted_responses": [],
        "exp_tool_calls": [
            {"name": "send_payment_link"},
            {"name": "send_sms", "parameters": {"template_id": "cosmetic_deposit"}},
        ],
    }
    assert [call["name"] for call in enc.drop_unneeded_send_sms(deposit)] == [
        "send_payment_link",
    ]

    faq = {
        "intent": (
            "Ask when they send appointment confirmation texts — how many days "
            "before the visit they start."
        ),
        "scripted_responses": [],
        "exp_tool_calls": [
            {"name": "send_sms", "parameters": {"template_id": "appointment_confirmation"}},
        ],
    }
    assert enc.drop_unneeded_send_sms(faq) == []


def test_encoder_stamps_creativity_zero() -> None:
    enc = _encoder()
    assert enc.behaviors({}) == {"creativity": 0}
    assert enc.behaviors({"behaviors": {"creativity": 0.15}}) == {"creativity": 0}
    assert enc.behaviors({"creativity": 0.15, "behaviors": {}}) == {"creativity": 0}


def test_healthcare_send_sms_only_when_caller_wants_text() -> None:
    enc = _encoder()
    yes = {"C1-M1", "C2-M1"}
    keep: list[str] = []
    for path in sorted((ROOT / "industries" / "healthcare" / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        names = [call.get("name") for call in task.get("exp_tool_calls") or []]
        has_sms = "send_sms" in names
        wants = enc.caller_requests_sms(task)
        parent = path.parent.name
        is_yes = parent in yes
        assert has_sms == wants == is_yes, parent
        pins = [p.get("response_value") for p in task.get("scripted_responses") or []]
        intent = task.get("intent") or ""
        if is_yes:
            keep.append(parent)
            assert "Yes, text me the appointment confirmation." in pins, parent
            assert "No thanks, I don't need a confirmation text." not in pins, parent
            assert "No thanks, I don't need a text." not in pins, parent
            for call in task["exp_tool_calls"]:
                assert enc.keep_send_sms_call(task, call, names)
            sms = next(c for c in task["exp_tool_calls"] if c["name"] == "send_sms")
            assert (sms.get("parameters") or {}).get("template_id") == "appointment_confirmation"
        else:
            assert "Yes, text me the appointment confirmation." not in pins, parent
            assert "Do not ask them to text you an appointment confirmation." in intent, parent
            assert "No thanks, I don't need a confirmation text." in pins, parent
    assert set(keep) == yes
def test_encoder_omits_waitlist_latest_and_location_zip() -> None:
    enc = _encoder()
    waitlisted = enc.complete_call(
        {"name": "join_waitlist", "parameters": {}},
        {
            "intent": "cancel then waitlist from August twenty-fourth through September thirtieth",
            "customer_name": "Jordan Lee",
            "traits": [
                {"trait_name": "full_name", "value": "Jordan Lee"},
                {"trait_name": "preferred_office", "value": "Park Avenue"},
            ],
        },
        "C2-H1",
        [],
    )
    params = waitlisted.get("parameters") or {}
    assert "latest" not in params
    assert params.get("earliest") == "2026-08-24"
    replayed = enc.replay_arguments(waitlisted)
    assert replayed["parameters"]["latest"] == enc.JOIN_WAITLIST_REPLAY_LATEST

    listed = enc.complete_call(
        {"name": "list_locations", "parameters": {"zip": "10016"}},
        {
            "intent": "Ask for the Park Avenue street, floor, and suite. Do not volunteer a ZIP.",
            "customer_name": "Owen Castellanos",
            "traits": [{"trait_name": "zip", "value": "10016"}],
        },
        "C1-H2",
        [],
    )
    assert (listed.get("parameters") or {}).get("zip") is None

    financed = enc.complete_call(
        {"name": "offer_financing", "parameters": {"amount_cents": 60000}},
        {"intent": "spread the cost out", "customer_name": "Lorraine Hobbs", "traits": []},
        "C4-M2",
        [],
    )
    assert "amount_cents" not in (financed.get("parameters") or {})


def test_healthcare_leftover_holes_are_closed() -> None:
    tasks = ROOT / "industries" / "healthcare" / "tasks"

    def load(key: str) -> dict:
        return json.loads((tasks / key / "task.json").read_text())

    def pin_blob(task: dict) -> str:
        return " ".join(
            f"{pin.get('match_phrase', '')} {pin.get('response_value', '')}"
            for pin in task.get("scripted_responses") or []
        ).lower()

    c2h1 = load("C2-H1")
    wait = next(c for c in c2h1["exp_tool_calls"] if c["name"] == "join_waitlist")
    assert "latest" not in (wait.get("parameters") or {})
    assert (wait.get("parameters") or {}).get("earliest") == "2026-08-24"
    assert "T" not in str((wait.get("parameters") or {}).get("earliest"))
    assert "23:59" not in json.dumps(wait)

    c1h2 = load("C1-H2")
    loc = next(c for c in c1h2["exp_tool_calls"] if c["name"] == "list_locations")
    assert "zip" not in (loc.get("parameters") or {})
    opening = c1h2["intent"].split("Stay until", 1)[0].lower()
    assert "weekday" in opening and "train" in opening
    for requirement in ("train", "street address", "floor", "suite", "calendar"):
        assert requirement in pin_blob(c1h2)
    spoken = (c1h2["intent"] + " " + pin_blob(c1h2)).lower()
    assert "search_practice_kb" not in spoken
    assert "verifier" not in spoken

    for key in ("C4-M1", "C4-M2"):
        task = load(key)
        assert task.get("scripted_responses"), key
        loc = next(c for c in task["exp_tool_calls"] if c["name"] == "list_locations")
        assert "zip" not in (loc.get("parameters") or {})

    c4m2 = load("C4-M2")
    names = [c["name"] for c in c4m2["exp_tool_calls"]]
    assert "offer_financing" not in names
    assert "find_slots" in names
    assert "calendar" in pin_blob(c4m2) or "booked" in pin_blob(c4m2)

    c5h3 = load("C5-H3")
    assert len([c for c in c5h3["exp_tool_calls"] if c["name"] == "explain_charge"]) == 2
    assert not any(
        "I'll pay the full balance now" in (p.get("response_value") or "")
        and "any line" in (p.get("match_phrase") or "")
        for p in c5h3["scripted_responses"]
    )
    assert "34786" in pin_blob(c5h3), "C5-H3"
    assert "407-555-0155" in pin_blob(c5h3), "C5-H3"

    c2h3 = load("C2-H3")
    assert "join_waitlist" not in [c["name"] for c in c2h3["exp_tool_calls"]]
    assert (c2h3.get("exp_db_state") or {}).get("waitlist") == []
    assert json.loads((tasks / "C2-H3" / "exp_db_state.json").read_text()).get("waitlist") == []

    captured = next(c for c in load("C3-M2")["exp_tool_calls"] if c["name"] == "capture_insurance_update")
    assert "group_number" not in (captured.get("parameters") or {})

    assert "w123456789" in pin_blob(load("C3-M1")), "C3-M1"
    assert "11201" in pin_blob(load("C5-M3")), "C5-M3"
    assert "transfer is not the appointment" in pin_blob(load("C1-H1")), "C1-H1"
    for key in ("C4-E1", "C4-E1-BG", "C4-E1-SIG"):
        pins = [pin.get("response_value") for pin in load(key).get("scripted_responses") or []]
        assert "No thanks, I don't need a text." not in pins, key
        assert "No thanks, I don't need a confirmation text." in pins, key

    c4h2 = load("C4-H2")
    c4h2_names = [c["name"] for c in c4h2["exp_tool_calls"]]
    assert "transfer_to_identity" in c4h2["exp_handoff_path"]
    assert "verify_identity" in c4h2_names
    assert "identify_patient" in c4h2_names

    rh1 = load("R-H1")
    message = next(c for c in rh1["exp_tool_calls"] if c["name"] == "create_clinical_message")
    assert message["parameters"] == {"category": "results_followup"}

    rh2 = load("R-H2")
    rh2_blob = rh2["intent"].lower() + " " + pin_blob(rh2)
    assert next(c for c in rh2["exp_tool_calls"] if c["name"] == "request_rx_refill").get("parameters", {}).get("medication_name") == "accutane", "R-H2"
    assert "do not reschedule the august twentieth acne check" in rh2_blob, "R-H2"
    assert "weekday morning" in rh2_blob, "R-H2"
    assert "reschedule_appointment" not in [c["name"] for c in rh2["exp_tool_calls"]], "R-H2"

    rh3_names = [c["name"] for c in load("R-H3")["exp_tool_calls"]]
    assert rh3_names.index("request_rx_refill") < rh3_names.index("create_clinical_message")
    rh3_message = next(c for c in load("R-H3")["exp_tool_calls"] if c["name"] == "create_clinical_message")
    assert rh3_message["parameters"] == {"category": "rx_question"}

    c4m1 = load("C4-M1")
    assert "microneedling" in pin_blob(c4m1), "C4-M1"
    assert not any("before a time has been confirmed" in (p.get("match_phrase") or "") for p in c4m1["scripted_responses"]), "C4-M1"

    c5h1 = load("C5-H1")
    assert "212-555-0133" in pin_blob(c5h1), "C5-H1"
    c5h1_pins = [pin.get("response_value") for pin in c5h1.get("scripted_responses") or []]
    assert "No thanks, I don't need a text." not in c5h1_pins, "C5-H1"
    assert "No thanks, I don't need a confirmation text." in c5h1_pins, "C5-H1"
    assert "Yes, please text the payment link to 212-555-0133." in c5h1_pins, "C5-H1"
    c5h2 = load("C5-H2")
    greeting = next(p for p in c5h2["scripted_responses"] if "greets you" in (p.get("match_phrase") or ""))
    assert "move" not in greeting["response_value"].lower(), "C5-H2"
    assert "no payment or dispute" in pin_blob(c5h2), "C5-H2"

    for key in ("C1-M1", "C2-M1"):
        task = load(key)
        names = [c["name"] for c in task["exp_tool_calls"]]
        assert "send_sms" in names, key
        assert "yes, text me the appointment confirmation." in pin_blob(task), key
        assert "ask them to text you an appointment confirmation" in task["intent"], key

    for path in tasks.glob("*/task.json"):
        phrases = [p["match_phrase"] for p in json.loads(path.read_text()).get("scripted_responses") or []]
        assert len(phrases) == len(set(phrases)), path.parent.name

    c1h2 = load("C1-H2")
    c1h2_names = [call["name"] for call in c1h2["exp_tool_calls"]]
    assert "search_practice_kb" not in c1h2_names
    assert "list_locations" in c1h2_names
    c1m2_find = next(call for call in load("C1-M2")["exp_tool_calls"] if call["name"] == "find_slots")
    assert (c1m2_find.get("parameters") or {}).get("location_ids") == ["loc_brooklyn_heights"]

    by_key = {conv.case_key_of(dh): dh for dh in _humans()}
    for case_key in ("C1-M3", "C2-H2"):
        allergy = next(c for c in by_key[case_key]["expected_tool_calls"] if c["name"] == "schedule_allergy_service")
        params = allergy.get("parameters") or {}
        assert "window_start" not in params
        assert "window_end" not in params

    c2h2 = load("C2-H2")
    c2h2_blob = c2h2["intent"].lower() + " " + pin_blob(c2h2)
    assert "first available" in c2h2_blob
    assert "not yes" in c2h2_blob
    assert "no callback" in c2h2_blob
    assert "allergy shot scheduled" in c2h2_blob
    assert [c["name"] for c in c2h2["exp_tool_calls"]].count("schedule_allergy_service") == 1

    c3m3 = load("C3-M3")
    c3m3_blob = c3m3["intent"].lower() + " " + pin_blob(c3m3)
    assert "no callback" in c3m3_blob
    assert "searched openings" in c3m3_blob
    assert "windermere" in c3m3_blob
    assert [c["name"] for c in c3m3["exp_tool_calls"]] == [
        "transfer_to_coverage",
        "check_plan_accepted",
        "transfer_to_scheduling",
        "classify_visit_request",
        "find_slots",
        "book_appointment",
    ]

    c2h3_blob = c2h3["intent"].lower() + " " + pin_blob(c2h3)
    assert "do not transfer me to a person" in c2h3_blob
    assert "i accept the missed-visit fee" in c2h3_blob
    assert "talk to a person" in c2h3_blob
    assert "friday is cancelled" in c2h3_blob
    assert "do not open with the friday cancel" in c2h3["intent"].lower()
    greeting = next(p for p in c2h3["scripted_responses"] if "greets you" in (p.get("match_phrase") or ""))
    assert "total balance" in greeting["response_value"].lower()
    assert "cancel" not in greeting["response_value"].lower()
    assert [c["name"] for c in c2h3["exp_tool_calls"]].count("cancel_appointment") == 2
    assert "transfer_to_human" not in [c["name"] for c in c2h3["exp_tool_calls"]]

    c1e3 = load("C1-E3")
    c1e3_blob = c1e3["intent"].lower() + " " + pin_blob(c1e3)
    assert "do not have one yet" in c1e3_blob
    assert "i don't want to give a zip" in c1e3_blob
    assert "no callback" in c1e3_blob
    assert [c["name"] for c in c1e3["exp_tool_calls"]] == [
        "transfer_to_coverage",
        "check_plan_accepted",
    ]
    medicaid = next(c for c in c1e3["exp_tool_calls"] if c["name"] == "check_plan_accepted")
    assert medicaid["parameters"] == {"carrier": "medicaid", "location_id": "loc_park_ave"}
    assert medicaid["output"]["data"]["accepted"] is False

    c4h1 = load("C4-H1")
    c4h1_blob = c4h1["intent"].lower() + " " + pin_blob(c4h1)
    assert "cosmetic team" in c4h1_blob
    assert "quote botox" in c4h1_blob
    assert "don't want a person" in c4h1_blob or "do not want a person" in c4h1_blob
    names = [c["name"] for c in c4h1["exp_tool_calls"]]
    assert names == [
        "transfer_to_identity",
        "identify_patient",
        "transfer_to_cosmetic",
        "quote_cosmetic_service",
        "quote_cosmetic_service",
        "list_locations",
        "find_slots",
        "book_cosmetic_consult",
        "offer_financing",
        "send_payment_link",
    ]
    assert "transfer_to_human" not in names
    assert names.count("quote_cosmetic_service") == 2
    rh2_dh = by_key["R-H2"]
    book = next(c for c in rh2_dh["expected_tool_calls"] if c["name"] == "book_appointment")
    assert book["parameters"]["slot_id"] == "slot_loc_park_ave_1"


def test_healthcare_hard_tool_counts_span_7_to_12() -> None:
    """Hard band is 7–12 including handoffs. Spread the band; do not require a flat histogram."""
    hist: Counter = Counter()
    below: list[str] = []
    above: list[str] = []
    for cat in ("C1", "C2", "C3", "C4", "C5", "R"):
        for n in range(1, 5):
            key = f"{cat}-H{n}"
            task = json.loads((ROOT / "industries" / "healthcare" / "tasks" / key / "task.json").read_text())
            count = len(task.get("exp_tool_calls") or [])
            names = [call.get("name") for call in task.get("exp_tool_calls") or []]
            assert "send_sms" not in names, key
            hist[count] += 1
            if count < 7:
                below.append(f"{key}={count}")
            if count > 12:
                above.append(f"{key}={count}")
    assert not below, f"hard tasks below 7: {below}"
    assert not above, f"hard tasks above 12: {above}"
    assert sum(hist[n] for n in range(7, 13)) == 24
    assert hist[11] + hist[12] >= 4
    assert len([n for n in range(7, 13) if hist[n]]) >= 4


def test_healthcare_expected_params_match_schema() -> None:
    tools = {
        spec["name"]: spec.get("inputSchema") or {}
        for spec in json.loads(
            (ROOT / "industries" / "healthcare" / "tools.json").read_text()
        )["tools"]
    }
    for path in sorted((ROOT / "industries" / "healthcare" / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        for call in task.get("exp_tool_calls") or []:
            schema = tools.get(call["name"])
            if not schema:
                continue
            params = call.get("parameters") or {}
            props = schema.get("properties") or {}
            extra = set(params) - set(props)
            assert not extra, f"{path.parent.name} {call['name']} extra {sorted(extra)}"
            for key, value in params.items():
                prop = props[key]
                items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
                if prop.get("enum") is not None:
                    assert value in prop["enum"], f"{path.parent.name} {call['name']}.{key}={value!r}"
                if items.get("enum") is not None:
                    for item in value:
                        assert item in items["enum"], f"{path.parent.name} {call['name']}.{key}={item!r}"
                pattern = prop.get("pattern")
                if pattern and isinstance(value, str):
                    assert re.fullmatch(pattern, value), (
                        f"{path.parent.name} {call['name']}.{key}={value!r} vs {pattern}"
                    )


