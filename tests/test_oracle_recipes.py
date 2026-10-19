"""Every task's solution/oracle.json must be current and must reach its own exp_db_state."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "verifiers" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recipes = _load("oracle_recipes")
check = _load("oracle_check")


@pytest.mark.parametrize("industry", recipes.INDUSTRIES)
def test_oracle_files_match_generator(industry: str) -> None:
    overrides = recipes.load_overrides()
    stale = []
    for task_id, task in recipes.tasks(industry):
        path = recipes.oracle_path(industry, task_id)
        want = recipes.render(recipes.recipe(industry, task_id, task, overrides))
        if not path.is_file() or path.read_text() != want:
            stale.append(task_id)
    assert not stale, f"{industry}: stale or missing oracle.json, rerun verifiers/oracle_recipes.py: {stale}"


@pytest.mark.parametrize("industry", recipes.INDUSTRIES)
def test_oracle_reaches_expected_state(industry: str) -> None:
    results = check.check_industry(industry)
    assert len(results) == len(recipes.tasks(industry))
    failed = [f"{r['task']}: " + "; ".join(r["reasons"]) for r in results if not r["ok"]]
    assert not failed, f"{industry}: {len(failed)} task(s) whose oracle does not reach exp_db_state\n" + "\n".join(failed)


def test_override_selectors() -> None:
    calls = [{"name": "a", "arguments": {}}, {"name": "b", "arguments": {}}, {"name": "a", "arguments": {}}]
    recipes.apply_overrides(calls, {
        "args": {"a#2": {"x": 1}},
        "expect_error": {"b": "E"},
        "insert": [{"before": "b", "name": "c", "arguments": {"y": 2}}],
    })
    assert [c["name"] for c in calls] == ["a", "c", "b", "a"]
    assert calls[3]["arguments"] == {"x": 1} and calls[0]["arguments"] == {}
    assert calls[2]["expect_error"] == "E"
    with pytest.raises(KeyError):
        recipes.apply_overrides(calls, {"args": {"zzz": {}}})
