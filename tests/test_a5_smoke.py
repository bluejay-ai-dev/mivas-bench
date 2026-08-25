"""A5 smoke: live tool-server processes, overlapping calls, distinct SQLite files.

Proves Part A end-to-end at HTTP level (replicas = 1): two concurrent calls on
the same pod get two files; GET /state for an unused id is seed; each dump is
seed plus that call's writes only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from call_id import set_call_id  # noqa: E402

INDUSTRIES = ("control-industry", "healthcare", "legal")


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=1) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _start_isolated(industry: str, port: int, data_dir: Path) -> subprocess.Popen[bytes]:
    industry_dir = ROOT / "industries" / industry
    env = os.environ.copy()
    env["TOOL_SERVER_PORT"] = str(port)
    env["TOOL_SERVER_URL"] = f"http://127.0.0.1:{port}"
    env["MIVAS_DB_PATH"] = str(data_dir / "runtime.db")
    env["MIVAS_DB_SHARED"] = "0"
    env["INDUSTRY_DIR"] = str(industry_dir)
    env["PYTHONPATH"] = str(RUNTIME) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [sys.executable, str(industry_dir / "tool_server.py")],
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = env["TOOL_SERVER_URL"]
    for _ in range(80):
        if _health_ok(url):
            return proc
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(f"{industry} tool server exited: {err[-500:]}")
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"{industry} tool server failed health on {url}")


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _post(url: str, name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    resp = httpx.post(
        f"{url}/tools/{name}",
        json={"arguments": args},
        headers={"X-Mivas-Call-Id": call_id},
        timeout=15.0,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _state(url: str, call_id: str) -> dict[str, Any]:
    resp = httpx.get(f"{url}/state", params={"call_id": call_id}, timeout=15.0)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _db_files(data_dir: Path) -> list[Path]:
    calls = data_dir / "calls"
    if not calls.is_dir():
        return []
    return sorted(p for p in calls.glob("*.db") if p.is_file())


def _sqlite_tables(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    finally:
        conn.close()


def test_overlapping_calls_use_distinct_sqlite_files(tmp_path: Path) -> None:
    """Two overlapping HTTP calls on one process → two files, no cross-leak."""
    reports: list[dict[str, Any]] = []
    for i, industry in enumerate(INDUSTRIES):
        data_dir = tmp_path / industry
        data_dir.mkdir()
        port = 18100 + i
        url = f"http://127.0.0.1:{port}"
        proc = _start_isolated(industry, port, data_dir)
        try:
            seed = _state(url, "seed_probe")
            barrier = threading.Barrier(2)

            def write_a() -> dict[str, Any]:
                barrier.wait(timeout=10)
                return _write(industry, url, "675")

            def write_b() -> dict[str, Any]:
                barrier.wait(timeout=10)
                return _write(industry, url, "676")

            with ThreadPoolExecutor(max_workers=2) as pool:
                fa = pool.submit(write_a)
                fb = pool.submit(write_b)
                out_a, out_b = fa.result(timeout=20), fb.result(timeout=20)

            final_a = _state(url, "675")
            final_b = _state(url, "676")
            unused = _state(url, "677")
            files = _db_files(data_dir)
            names = {p.name for p in files}
            assert {"675.db", "676.db", "677.db", "seed_probe.db"} <= names, names
            inodes = {p.stat().st_ino for p in files if p.name in {"675.db", "676.db"}}
            assert len(inodes) == 2, "675 and 676 must be distinct inodes"
            assert final_a != final_b
            assert _canonical(unused) == _canonical(seed)
            _assert_industry_isolation(industry, seed, final_a, final_b, unused)
            miss = httpx.get(f"{url}/state", timeout=10.0)
            assert miss.status_code == 400
            reports.append(
                {
                    "industry": industry,
                    "files": sorted(names),
                    "files_detail": [
                        {
                            "name": p.name,
                            "ino": p.stat().st_ino,
                            "bytes": p.stat().st_size,
                        }
                        for p in files
                    ],
                    "inodes": len(inodes),
                    "a": _summary(industry, final_a),
                    "b": _summary(industry, final_b),
                    "unused": _summary(industry, unused),
                    "seed": _summary(industry, seed),
                    "write_a": out_a,
                    "write_b": out_b,
                    "table_counts": {p.name: _sqlite_tables(p) for p in files},
                }
            )
        finally:
            _stop(proc)
    payload = json.dumps(reports, indent=2, default=str)
    (tmp_path / "a5_report.json").write_text(payload)
    Path("/tmp/mivas-a5-report.json").write_text(payload)


def _write(industry: str, url: str, call_id: str) -> dict[str, Any]:
    if industry == "control-industry":
        date = "08/15/2026" if call_id == "675" else "08/16/2026"
        return _post(url, "schedule_appointment", {"date": date}, call_id)
    if industry == "healthcare":
        if call_id == "675":
            ident = _post(
                url,
                "verify_identity",
                {"full_name": "Alice Romano", "dob": "1995-09-08"},
                call_id,
            )
            cancel = _post(
                url,
                "cancel_appointment",
                {"appointment_id": "3", "cancellation_reason_code": "patient_request"},
                call_id,
            )
            return {"verify": ident, "cancel": cancel}
        return _post(
            url,
            "verify_identity",
            {"full_name": "Jordan Lee", "dob": "1990-04-12"},
            call_id,
        )
    if industry == "legal":
        if call_id == "675":
            _post(
                url,
                "lookup_caller",
                {"full_name": "Dana Whitfield", "phone": "5105550142"},
                call_id,
            )
            return _post(
                url,
                "take_message",
                {"for_whom": "reception", "message": "please call back A"},
                call_id,
            )
        _post(
            url,
            "lookup_caller",
            {"full_name": "Marcus Oyelaran", "phone": "4155550188"},
            call_id,
        )
        return _post(
            url,
            "take_message",
            {"for_whom": "reception", "message": "please call back B"},
            call_id,
        )
    raise AssertionError(industry)


def _canonical(value: Any) -> Any:
    """Drop wall-clock created_at so two seed copies compare equal."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items() if k != "created_at"}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _assert_industry_isolation(
    industry: str,
    seed: dict[str, Any],
    a: dict[str, Any],
    b: dict[str, Any],
    unused: dict[str, Any],
) -> None:
    assert _canonical(unused) == _canonical(seed)
    if industry == "control-industry":
        assert [r["date"] for r in a["appointments"]] == ["08/15/2026"]
        assert [r["date"] for r in b["appointments"]] == ["08/16/2026"]
        assert unused["appointments"] == []
        return
    if industry == "healthcare":
        a_status = {r["id"]: r["status"] for r in a["appointments"]}
        b_status = {r["id"]: r["status"] for r in b["appointments"]}
        seed_status = {r["id"]: r["status"] for r in seed["appointments"]}
        assert a_status[3] == "cancelled"
        assert b_status[3] == "booked"
        assert seed_status[3] == "booked"
        return
    if industry == "legal":
        a_msgs = [r.get("message") for r in a.get("messages") or []]
        b_msgs = [r.get("message") for r in b.get("messages") or []]
        assert a_msgs == ["please call back A"]
        assert b_msgs == ["please call back B"]
        assert (unused.get("messages") or []) == []
        return
    assert a != seed
    assert b != a


def _summary(industry: str, state: dict[str, Any]) -> Any:
    if industry == "control-industry":
        return [r.get("date") for r in state.get("appointments") or []]
    if industry == "healthcare":
        return {
            "appt_status": {r["id"]: r["status"] for r in state.get("appointments") or []},
            "tool_events": len(state.get("tool_events") or []),
        }
    if industry == "legal":
        return {
            "messages": [
                {k: r.get(k) for k in ("id", "for_whom", "message", "caller_id")}
                for r in (state.get("messages") or state.get("voicemails") or [])
            ],
            "escalations": len(state.get("escalations") or []),
            "tool_events": len(state.get("tool_events") or []),
        }
    keys = sorted(state)
    return {k: (len(state[k]) if isinstance(state[k], list) else type(state[k]).__name__) for k in keys}


def test_harness_dispatch_hits_per_call_files(tmp_path: Path) -> None:
    """OpenAI / Grok dispatch against a live control-industry server."""
    data_dir = tmp_path / "control"
    data_dir.mkdir()
    port = 18200
    url = f"http://127.0.0.1:{port}"
    proc = _start_isolated("control-industry", port, data_dir)
    os.environ["TOOL_SERVER_URL"] = url
    try:
        seed = _state(url, "unused")
        _dispatch_openai(url, "675", "08/15/2026")
        _dispatch_grok(url, "676", "08/16/2026")
        a, b = _state(url, "675"), _state(url, "676")
        unused = _state(url, "unused")
        assert [r["date"] for r in a["appointments"]] == ["08/15/2026"]
        assert [r["date"] for r in b["appointments"]] == ["08/16/2026"]
        assert unused == seed
        names = {p.name for p in _db_files(data_dir)}
        assert {"675.db", "676.db", "unused.db"} <= names
        inodes = {p.stat().st_ino for p in _db_files(data_dir) if p.stem in {"675", "676"}}
        assert len(inodes) == 2
    finally:
        _stop(proc)


def _dispatch_openai(url: str, call_id: str, date: str) -> None:
    import importlib.util

    family = ROOT / "voice-agent-harnesses" / "openai"
    # unique module name + scoped sys.path: a bare `import harness` would cache
    # openai's harness as sys.modules["harness"] and poison later family tests
    saved = {n: sys.modules.pop(n) for n in ("harness", "report") if n in sys.modules}
    sys.path.insert(0, str(family))
    try:
        spec = importlib.util.spec_from_file_location(
            "mivas_openai_harness", family / "harness.py"
        )
        assert spec is not None and spec.loader is not None
        openai_harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(openai_harness)
    finally:
        sys.path.remove(str(family))
        sys.modules.pop("harness", None)
        sys.modules.pop("report", None)
        sys.modules.update(saved)

    openai_harness.TOOL_SERVER_URL = url
    set_call_id(call_id)
    import asyncio

    result = asyncio.run(
        openai_harness.dispatch_industry_tool("schedule_appointment", {"date": date})
    )
    assert result.get("success") is True, result


def _dispatch_grok(url: str, call_id: str, date: str) -> None:
    import asyncio
    import importlib.util

    family = ROOT / "voice-agent-harnesses" / "grok"
    spec = importlib.util.spec_from_file_location("mivas_grok_harness", family / "harness.py")
    assert spec is not None and spec.loader is not None
    grok = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grok)
    set_call_id(call_id)
    bp = {
        "agents": {"scheduler": {"tools": [{"name": "schedule_appointment"}]}},
        "catalog": {"schedule_appointment": {}},
    }
    result, stop = asyncio.run(
        grok._execute_tool("schedule_appointment", {"date": date}, bp, {"agent": "scheduler"})
    )
    assert stop is False
    assert result.get("success") is True, result


