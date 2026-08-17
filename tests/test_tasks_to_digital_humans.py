"""MIVAS task.json → Bluejay digital-human conversion (no live API)."""

from __future__ import annotations

import importlib.util
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


def test_healthcare_emits_66_payloads() -> None:
    humans = _humans()
    assert len(humans) == 66
    keys = [conv.case_key_of(dh) for dh in humans]
    assert len(set(keys)) == 66
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


def test_audio_condition_mapping() -> None:
    by_audio = Counter()
    for dh in _humans():
        audio = conv.trait_value(dh, "audio_condition")
        by_audio[audio] += 1
        mapped = conv.audio_fields(audio)
        for field, want in mapped.items():
            assert dh[field] == want
    assert by_audio["perfect"] == 54
    assert by_audio["background_noise"] == 6
    assert by_audio["bad_signal"] == 6


def test_three_by_three_per_category() -> None:
    scored: dict[str, Counter] = {}
    for dh in _humans():
        if conv.trait_value(dh, "audio_condition") != "perfect":
            continue
        area = conv.trait_value(dh, "call_area") or ""
        difficulty = conv.trait_value(dh, "difficulty") or ""
        scored.setdefault(area, Counter())[difficulty] += 1
    assert len(scored) == 6
    for area, counts in scored.items():
        assert counts == Counter({"easy": 3, "medium": 3, "hard": 3}), area


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
    import json

    conv.main(["--industry", "healthcare", "--json"])
    body = json.loads(capsys.readouterr().out)
    humans = body["digital_humans"]
    assert len(humans) == 66
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
    kept = [call["name"] for call in enc.drop_unneeded_send_sms(wants_address)]
    assert kept == ["send_sms", "book_appointment"]

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
    import json

    enc = _encoder()
    keep: list[str] = []
    for path in sorted((ROOT / "industries" / "healthcare" / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        names = [call.get("name") for call in task.get("exp_tool_calls") or []]
        if "send_sms" not in names:
            continue
        keep.append(path.parent.name)
        assert enc.caller_requests_sms(task), path.parent.name
        for call in task["exp_tool_calls"]:
            assert enc.keep_send_sms_call(task, call, names)
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
    assert params.get("earliest") == "2026-08-24T00:00:00"
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
    import json

    tasks = ROOT / "industries" / "healthcare" / "tasks"

    c2h1 = json.loads((tasks / "C2-H1" / "task.json").read_text())
    wait = next(c for c in c2h1["exp_tool_calls"] if c["name"] == "join_waitlist")
    assert "latest" not in (wait.get("parameters") or {})
    assert "23:59:59" not in json.dumps(wait)

    c1h2 = json.loads((tasks / "C1-H2" / "task.json").read_text())
    loc = next(c for c in c1h2["exp_tool_calls"] if c["name"] == "list_locations")
    assert "zip" not in (loc.get("parameters") or {})

    for key in ("C4-M1", "C4-M2"):
        task = json.loads((tasks / key / "task.json").read_text())
        pins = task.get("scripted_responses") or []
        assert isinstance(pins, list) and pins, key
        loc = next(c for c in task["exp_tool_calls"] if c["name"] == "list_locations")
        assert (loc.get("parameters") or {}) in ({}, None) or "zip" not in loc.get("parameters", {})

    c4m2 = json.loads((tasks / "C4-M2" / "task.json").read_text())
    names = [c["name"] for c in c4m2["exp_tool_calls"]]
    assert "offer_financing" not in names
    assert "find_slots" in names
    pin_text = " ".join(p.get("response_value") or "" for p in c4m2["scripted_responses"])
    assert "calendar" in pin_text.lower() or "booked" in pin_text.lower()

    c5h3 = json.loads((tasks / "C5-H3" / "task.json").read_text())
    explains = [c for c in c5h3["exp_tool_calls"] if c["name"] == "explain_charge"]
    assert len(explains) == 2
    premature = [
        p for p in c5h3["scripted_responses"]
        if "I'll pay the full balance now" in (p.get("response_value") or "")
        and "any line" in (p.get("match_phrase") or "")
    ]
    assert not premature

    c2h3 = json.loads((tasks / "C2-H3" / "task.json").read_text())
    assert "join_waitlist" not in [c["name"] for c in c2h3["exp_tool_calls"]]
    assert (c2h3.get("exp_db_state") or {}).get("waitlist") == []
    sibling = json.loads((tasks / "C2-H3" / "exp_db_state.json").read_text())
    assert sibling.get("waitlist") == []

    c3m2 = json.loads((tasks / "C3-M2" / "task.json").read_text())
    captured = next(c for c in c3m2["exp_tool_calls"] if c["name"] == "capture_insurance_update")
    assert "group_number" not in (captured.get("parameters") or {})

