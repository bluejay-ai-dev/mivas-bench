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
        assert "prompt_adherence_substrs" not in dh
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
