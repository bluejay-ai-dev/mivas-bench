"""Assert every report.py call site matches its own harness's signature.

Each harness owns its own report.py, so a signature change there has to be made in
that folder's callers too. A regex sweep missed a multi-line
`finish_tool_span(span, ..., name=..., parameters=...)` in vapi/adapters/chirp.py and
shipped a harness that died on its first server-side tool call — the runtime smoke
only exercised harness.run_tool, never the adapter path.

    python3 voice-agent-harnesses/check_call_sites.py
"""

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
bad = []

for report in sorted(ROOT.glob("*/report.py")):
    # keywords each function in *this* harness's report.py accepts (**kwargs takes any)
    sigs = {
        fn.name: {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
        for fn in ast.parse(report.read_text()).body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.args.kwarg
    }
    for py in report.parent.rglob("*.py"):
        if py == report or {".venv", "__pycache__"} & set(py.parts):
            continue
        for call in ast.walk(ast.parse(py.read_text())):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            accepted = sigs.get(call.func.id)
            if accepted is None:
                continue
            bad += [
                f"FAIL {py.relative_to(ROOT)}:{call.lineno} {call.func.id}(… {kw.arg}=…)"
                f" — not in {sorted(accepted)}"
                for kw in call.keywords
                if kw.arg and kw.arg not in accepted
            ]

for line in bad:
    print(line)
print(f"{'FAILED' if bad else 'ok'} — {len(bad)} bad call site(s)")
sys.exit(1 if bad else 0)
