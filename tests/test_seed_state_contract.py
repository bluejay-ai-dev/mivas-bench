"""Contract: a task that expects a write must expect that write's tables to change.

An exp_db_state that equals the seed while exp_tool_calls contains a write means the
replay that produced the state never performed the write. This file is that
consistency rule as a test, so no such task can be exported again.

For every expected tool call that is a write, the expected state must differ from the
seed on at least one table the tool writes. Writes are derived from the industry's
tool_server.py: a DISPATCH entry counts as writing a table when an INSERT or UPDATE on
it is reached on every non-raising path of the handler or anything it calls (a write
inside an if/for/try is conditional and may legitimately leave the table at seed, for
example legal lookup_caller on a returning caller). Seed state is GET /state of a fresh
tool server with no calls replayed, through verifiers/expected_final_state.py.

    uv run pytest tests/test_seed_state_contract.py
    uv run python tests/test_seed_state_contract.py            # audit industries/
    uv run python tests/test_seed_state_contract.py HF_CLONE   # audit an export's tasks.jsonl
    MIVAS_FROZEN_DATASET=HF_CLONE uv run pytest tests/test_seed_state_contract.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "verifiers"))
import expected_final_state as efs  # noqa: E402

INDUSTRIES = ("customer-support", "healthcare", "legal")
_SQL_WRITE = re.compile(r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE)\s+(\w+)", re.I)
_BRANCH = (ast.If, ast.IfExp, ast.BoolOp, ast.For, ast.While, ast.Match)

# Expected writes the tool is meant to refuse, so the table stays at seed by design.
# task.json carries no error marker on these calls; add `output: {"ok": false, ...}`
# to the call there (the generic exemption below) to retire an entry.
REFUSED_WRITES: dict[str, dict[str, set[str]]] = {
    "customer-support": {
        "R-H3": {"create_return_label"},   # HAZMAT_NO_LABEL on a swollen power bank
        "T5-M2": {"quote_price_match"},    # unqualified retailer
        "T5-M3": {"quote_price_match"},    # competitor offer out of stock
        "T5-M4": {"quote_price_match"},    # competitor price not lower
    },
}

# Customer-support rows of the 2026-08-24 export that expected a write on a seed state.
FROZEN_GROUP_1 = {
    "T1-H1", "T1-H2", "T1-H3", "T1-M2", "T2-H1", "T2-H2", "T2-H4", "T3-H1", "T3-H4",
    "T4-H1", "T4-H2", "T4-H3", "T4-H4", "T5-H1", "T5-H3", "T5-H4",
}


def _unconditional(fn: ast.FunctionDef):
    """Nodes of fn reached on every non-raising path: no branch, loop or try ancestor."""
    todo = list(fn.body)
    while todo:
        node = todo.pop()
        yield node
        if isinstance(node, (ast.Try, ast.TryStar)):
            todo.extend(node.body + node.finalbody)  # attempted on every path
        elif not isinstance(node, _BRANCH):
            todo.extend(ast.iter_child_nodes(node))


@lru_cache(maxsize=None)
def write_tables(industry: str) -> dict[str, frozenset[str]]:
    """DISPATCH tool name -> tables it always INSERTs or UPDATEs, from tool_server.py."""
    tree = ast.parse((efs.INDUSTRY_ROOT / industry / "tool_server.py").read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    direct: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for name, fn in funcs.items():
        tables, called = set(), set()
        for node in _unconditional(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                tables.update(t.lower() for t in _SQL_WRITE.findall(node.value))
            elif isinstance(node, ast.Name) and node.id in funcs and node.id != name:
                called.add(node.id)
        direct[name], calls[name] = tables, called

    def reach(roots: set[str]) -> set[str]:
        seen, out, todo = set(), set(), list(roots)
        while todo:
            f = todo.pop()
            if f not in seen:
                seen.add(f)
                out |= direct[f]
                todo += calls[f]
        return out

    dispatch = next(
        n.value for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DISPATCH" for t in n.targets))
    assert isinstance(dispatch, ast.Dict), f"{industry}: DISPATCH is not a dict literal"
    out: dict[str, frozenset[str]] = {}
    for key, value in zip(dispatch.keys, dispatch.values):
        roots = {n.id for n in ast.walk(value) if isinstance(n, ast.Name) and n.id in funcs}
        tables = reach(roots)
        if tables:
            out[key.value] = frozenset(tables)
    return out


@lru_cache(maxsize=None)
def seed_state(industry: str) -> dict:
    """GET /state after replaying zero calls on a fresh call id."""
    with efs.load_tool_server(industry) as module, TestClient(module.app) as client:
        resp = client.get("/state", headers={"X-Mivas-Call-Id": "seed-contract"})
    assert resp.status_code == 200, resp.text
    return efs.canonical_state(resp.json())


def _expects_error(call: dict) -> bool:
    out = call.get("output") if isinstance(call.get("output"), dict) else {}
    return (call.get("ok") is False or out.get("ok") is False
            or bool(call.get("error_code") or out.get("error_code")))


def violations(industry: str, tasks: dict[str, dict]) -> list[str]:
    """One line per expected write whose tables all equal the seed in exp_db_state."""
    writes, seed = write_tables(industry), seed_state(industry)
    found = []
    for task_id, task in tasks.items():
        state = efs.canonical_state(task["exp_db_state"])
        refused = REFUSED_WRITES.get(industry, {}).get(task_id, set())
        for call in task.get("exp_tool_calls") or []:
            name = call["name"]
            tables = [t for t in sorted(writes.get(name, ())) if t in seed]
            if not tables or name in refused or _expects_error(call):
                continue
            if all(state.get(t) == seed[t] for t in tables):
                found.append(f"{task_id}: expects {name} but {tables} equal the seed")
    return found


def runtime_tasks(industry: str) -> dict[str, dict]:
    folder = efs.INDUSTRY_ROOT / industry / "tasks"
    return {d.name: json.loads((d / "task.json").read_text())
            for d in sorted(folder.iterdir()) if (d / "task.json").is_file()}


def jsonl_tasks(path: Path) -> dict[str, dict]:
    rows = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {row["task_id"]: row for row in rows}


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_write_tools_are_derived_from_tool_server(industry: str) -> None:
    writes = write_tables(industry)
    known_write, known_read = {
        "customer-support": (("confirm_delivery_change", "orders"), "get_order"),
        "healthcare": (("book_appointment", "appointments"), "get_patient_summary"),
        "legal": (("record_intake", "intakes"), "get_caller_matters"),
    }[industry]
    assert known_write[1] in writes[known_write[0]]
    assert known_read not in writes
    if industry == "legal":
        assert "lookup_caller" not in writes, "its INSERT only runs for a new caller"


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_expected_writes_change_expected_state(industry: str) -> None:
    assert violations(industry, runtime_tasks(industry)) == []


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_seed_state_with_an_expected_write_is_flagged(industry: str) -> None:
    """The 2026-08-24 defect in one row: an expected write, a seed exp_db_state."""
    tasks, writes = runtime_tasks(industry), write_tables(industry)
    task_id = next(
        t for t, task in tasks.items()
        if t not in REFUSED_WRITES.get(industry, {})
        and any(c["name"] in writes and not _expects_error(c) for c in task["exp_tool_calls"]))
    broken = dict(tasks[task_id], exp_db_state=seed_state(industry))
    assert violations(industry, {task_id: broken})


FROZEN = os.environ.get("MIVAS_FROZEN_DATASET")


@pytest.mark.skipif(not FROZEN, reason="set MIVAS_FROZEN_DATASET to a clone of "
                    "huggingface.co/datasets/bluejay-labs/mivas-bench at 2eb37a90")
@pytest.mark.parametrize("industry", INDUSTRIES)
def test_frozen_2026_08_24_rows(industry: str) -> None:
    found = violations(industry, jsonl_tasks(Path(FROZEN) / industry / "tasks.jsonl"))
    flagged = {line.split(":", 1)[0] for line in found}
    if industry == "customer-support":
        assert FROZEN_GROUP_1 <= flagged, sorted(FROZEN_GROUP_1 - flagged)
    else:
        assert found == []


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    total = 0
    for ind in INDUSTRIES:
        found = violations(ind, jsonl_tasks(source / ind / "tasks.jsonl") if source
                           else runtime_tasks(ind))
        total += len(found)
        print(f"{ind}: {len(found)} violation(s)")
        print("".join(f"  {line}\n" for line in found), end="")
    raise SystemExit(1 if total else 0)
