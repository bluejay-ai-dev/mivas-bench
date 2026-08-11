"""Assert every report.py call site matches its own harness's signature.

Each harness owns its own report.py, so a signature change there has to be made
in that folder's callers too. A regex sweep missed a multi-line
`finish_tool_span(span, ..., name=..., parameters=...)` in vapi/adapters/chirp.py
and shipped a harness that died on its first server-side tool call — the runtime
smoke only exercised harness.run_tool, never the adapter path.

    uv run python voice-agent-harnesses/check_call_sites.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
CHECKED = ("finish_tool_span", "tool_span", "start_speech_span", "record_past_tool_span")


def signatures(report: pathlib.Path) -> dict[str, set[str]]:
    """Keyword names each checked function in this harness's report.py accepts."""
    out: dict[str, set[str]] = {}
    for node in ast.parse(report.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in CHECKED:
            a = node.args
            out[node.name] = {x.arg for x in (*a.args, *a.posonlyargs, *a.kwonlyargs)}
    return out


def main() -> int:
    bad: list[str] = []
    for report in sorted(ROOT.glob("*/report.py")):
        harness = report.parent
        sigs = signatures(report)
        for py in harness.rglob("*.py"):
            if py == report or ".venv" in py.parts or "__pycache__" in py.parts:
                continue
            for call in ast.walk(ast.parse(py.read_text())):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                accepted = sigs.get(call.func.id)
                if accepted is None:
                    continue
                for kw in call.keywords:
                    if kw.arg and kw.arg not in accepted:
                        bad.append(
                            f"{py.relative_to(ROOT)}:{call.lineno} "
                            f"{call.func.id}(… {kw.arg}=…) — not in {sorted(accepted)}"
                        )
    for line in bad:
        print(f"FAIL {line}")
    print(f"{'FAILED' if bad else 'ok'} — {len(bad)} bad call site(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
