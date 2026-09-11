#!/usr/bin/env python3
"""Score one Bluejay get_simulation_result JSON against the MIVAS smoke rubric.

Stdlib only. Reads a result payload (MCP envelope or the simulation_result object)
and optional transcript JSON. Prints one JSON object per call and exits 0 iff every
file PASSes all six checks.

    python scripts/score_rubric.py result.json [result.json ...]
    python scripts/score_rubric.py --self-test

Do not eyeball time_to_first_agent_utterance — this script is the source of truth
for check 6 (opening silence).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.request import urlopen

TTFA_MAX_MS = 3000
PUNCT_MAX_MS = 25000
GAP_MAX_MS = 8000
CLIP_EPS = 0.001
NUDGE_RE = re.compile(
    r"are you still there|haven't heard from you",
    re.IGNORECASE,
)
HANGUP_GAP_OK = frozenset({"transfer_to_human", "end_call"})
MAX_LENGTH_RE = re.compile(r"max call length reached", re.IGNORECASE)
CHECKS = (
    "utterances",
    "tool_calls",
    "traces",
    "dead_air",
    "audio_continuity",
    "opening_silence",
)


def _unwrap(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SystemExit("result JSON must be an object")
    if isinstance(payload.get("simulation_result"), dict):
        inner = payload["simulation_result"]
        if isinstance(inner.get("simulation_result"), dict):
            return inner["simulation_result"]
        return inner
    if isinstance(payload.get("data"), dict) and isinstance(
        payload["data"].get("simulation_result"), dict
    ):
        return payload["data"]["simulation_result"]
    return payload


def _metric(result: dict[str, Any], name: str) -> Any:
    for row in result.get("metrics") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row.get("value")
    return None


def _num(value: Any) -> float | None:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _turns(result: dict[str, Any]) -> int:
    evals = result.get("evaluations") or []
    if evals and isinstance(evals[0], dict) and evals[0].get("num_turns") is not None:
        try:
            return int(evals[0]["num_turns"])
        except (TypeError, ValueError):
            pass
    n = _metric(result, "num_turns")
    return int(n) if n is not None else 0


def _has_actual(result: dict[str, Any]) -> bool:
    for row in result.get("tool_calls") or []:
        if isinstance(row, dict) and row.get("actual"):
            return True
    return False


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _fetch_transcript(url: str) -> list[dict[str, Any]] | None:
    try:
        with urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if isinstance(data, dict):
        for key in ("transcript", "utterances", "turns"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [t for t in rows if isinstance(t, dict)]
    return None


def _speaker(turn: dict[str, Any]) -> str:
    raw = (
        turn.get("speaker")
        or turn.get("role")
        or turn.get("type")
        or ""
    )
    return str(raw).strip().lower()


def _is_agent(turn: dict[str, Any]) -> bool:
    sp = _speaker(turn)
    return sp in {"agent", "assistant", "bot", "ai"}


def _is_user(turn: dict[str, Any]) -> bool:
    sp = _speaker(turn)
    return sp in {"user", "customer", "human", "caller"}


def _text(turn: dict[str, Any]) -> str:
    return str(turn.get("text") or turn.get("content") or turn.get("transcript") or "")


def _offset(turn: dict[str, Any], key: str) -> float | None:
    return _num(turn.get(key) or turn.get(key.replace("_ms", "")))


def _nudge_turns(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in transcript if _is_user(t) and NUDGE_RE.search(_text(t))]


def _first_agent_start_ms(transcript: list[dict[str, Any]]) -> float | None:
    for turn in transcript:
        if _is_agent(turn):
            return _offset(turn, "start_offset_ms") or _offset(turn, "start_ts") or 0.0
    return None


def _user_agent_gaps(
    transcript: list[dict[str, Any]],
    *,
    hangup_after_ms: float | None,
) -> list[float]:
    gaps: list[float] = []
    for i, turn in enumerate(transcript):
        if not _is_user(turn):
            continue
        end = _offset(turn, "end_offset_ms")
        if end is None:
            continue
        nxt = next((t for t in transcript[i + 1 :] if _is_agent(t)), None)
        if nxt is None:
            continue
        start = _offset(nxt, "start_offset_ms")
        if start is None:
            continue
        if MAX_LENGTH_RE.search(_text(nxt)):
            continue
        if hangup_after_ms is not None and end >= hangup_after_ms:
            continue
        gaps.append(start - end)
    return gaps


def _tools_after_nudge(
    result: dict[str, Any], transcript: list[dict[str, Any]]
) -> bool:
    nudges = _nudge_turns(transcript)
    if not nudges:
        return False
    earliest = min(
        (_offset(t, "start_offset_ms") or 0.0) for t in nudges
    )
    for row in result.get("tool_calls") or []:
        if not isinstance(row, dict):
            continue
        for actual in row.get("actual") or []:
            if not isinstance(actual, dict):
                continue
            start = _num(actual.get("start_offset_ms"))
            if start is not None and start >= earliest:
                return True
    return False


def _hangup_after_ms(result: dict[str, Any]) -> float | None:
    earliest: float | None = None
    for row in result.get("tool_calls") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "") not in HANGUP_GAP_OK:
            continue
        for actual in row.get("actual") or []:
            if not isinstance(actual, dict):
                continue
            start = _num(actual.get("start_offset_ms"))
            if start is None:
                continue
            earliest = start if earliest is None else min(earliest, start)
    return earliest


def score_call(
    result: dict[str, Any],
    *,
    transcript: list[dict[str, Any]] | None = None,
    in_pod_tools: bool = False,
) -> dict[str, Any]:
    result = _unwrap(result)
    checks: dict[str, dict[str, Any]] = {}

    turns = _turns(result)
    url = result.get("transcript_url") or ""
    checks["utterances"] = {
        "pass": turns > 0 and bool(url),
        "num_turns": turns,
        "transcript_url": bool(url),
    }

    has_actual = _has_actual(result)
    checks["tool_calls"] = {
        "pass": has_actual or in_pod_tools,
        "has_actual": has_actual,
        "in_pod_tools": in_pod_tools,
    }

    traces = [t for t in (result.get("trace_ids") or []) if t]
    checks["traces"] = {"pass": bool(traces), "trace_ids": traces}

    punct = _num(_metric(result, "max_punctuation_latency"))
    dead = {
        "max_punctuation_latency": punct,
        "nudge": False,
        "max_user_agent_gap_ms": None,
        "tool_after_nudge": False,
    }
    dead_ok = punct is not None and punct <= PUNCT_MAX_MS
    if transcript is not None:
        dead["nudge"] = bool(_nudge_turns(transcript))
        gaps = _user_agent_gaps(transcript, hangup_after_ms=_hangup_after_ms(result))
        dead["max_user_agent_gap_ms"] = max(gaps) if gaps else 0.0
        dead["tool_after_nudge"] = _tools_after_nudge(result, transcript)
        dead_ok = (
            dead_ok
            and not dead["nudge"]
            and dead["max_user_agent_gap_ms"] <= GAP_MAX_MS
            and not dead["tool_after_nudge"]
        )
    dead["pass"] = dead_ok
    checks["dead_air"] = dead

    dropouts = _num(_metric(result, "agent_audio_dropouts"))
    clipping = _num(_metric(result, "agent_audio_clipping"))
    clip_ok = clipping is not None and clipping < CLIP_EPS
    drop_ok = dropouts is not None and dropouts == 0
    checks["audio_continuity"] = {
        "pass": drop_ok and clip_ok,
        "agent_audio_dropouts": dropouts,
        "agent_audio_clipping": clipping,
    }

    ttfa = _num(_metric(result, "time_to_first_agent_utterance"))
    first_agent = _first_agent_start_ms(transcript) if transcript is not None else None
    opening_ok = ttfa is not None and ttfa <= TTFA_MAX_MS
    if first_agent is not None:
        opening_ok = opening_ok and first_agent <= TTFA_MAX_MS
    checks["opening_silence"] = {
        "pass": opening_ok,
        "time_to_first_agent_utterance": ttfa,
        "first_agent_start_offset_ms": first_agent,
        "max_ms": TTFA_MAX_MS,
    }

    passed = all(checks[name]["pass"] for name in CHECKS)
    return {
        "id": result.get("id"),
        "digital_human_id": result.get("digital_human_id"),
        "pass": passed,
        "checks": checks,
    }


def _print_score(score: dict[str, Any]) -> None:
    flags = " ".join(
        f"{name}={'PASS' if score['checks'][name]['pass'] else 'FAIL'}"
        for name in CHECKS
    )
    ttfa = score["checks"]["opening_silence"].get("time_to_first_agent_utterance")
    print(
        f"{'PASS' if score['pass'] else 'FAIL'} id={score.get('id')} "
        f"dh={score.get('digital_human_id')} ttfa_ms={ttfa} {flags}",
        flush=True,
    )
    print(json.dumps(score, indent=2), flush=True)


def _self_test() -> int:
    base = {
        "id": 1,
        "digital_human_id": 1,
        "transcript_url": "https://example.invalid/t.json",
        "trace_ids": ["abc"],
        "evaluations": [{"num_turns": 8}],
        "tool_calls": [{"name": "identify_patient", "actual": [{"start_offset_ms": 1000}]}],
        "metrics": [
            {"name": "max_punctuation_latency", "value": 12000},
            {"name": "agent_audio_dropouts", "value": 0},
            {"name": "agent_audio_clipping", "value": 0},
            {"name": "time_to_first_agent_utterance", "value": 80},
        ],
    }
    greeting = [
        {"speaker": "agent", "text": "Thanks for calling", "start_offset_ms": 80, "end_offset_ms": 4000},
        {"speaker": "user", "text": "hi", "start_offset_ms": 5000, "end_offset_ms": 6000},
        {"speaker": "agent", "text": "sure", "start_offset_ms": 7500, "end_offset_ms": 9000},
    ]
    ok = score_call(base, transcript=greeting)
    assert ok["pass"], ok

    silent = json.loads(json.dumps(base))
    silent["metrics"][3]["value"] = 18105
    bad = score_call(silent, transcript=[
        {"speaker": "agent", "text": "hi", "start_offset_ms": 18105, "end_offset_ms": 20000},
    ])
    assert not bad["pass"]
    assert not bad["checks"]["opening_silence"]["pass"]

    edge = json.loads(json.dumps(base))
    edge["metrics"][3]["value"] = TTFA_MAX_MS
    assert score_call(edge, transcript=[
        {"speaker": "agent", "text": "hi", "start_offset_ms": TTFA_MAX_MS, "end_offset_ms": TTFA_MAX_MS + 1000},
    ])["checks"]["opening_silence"]["pass"]

    over = json.loads(json.dumps(base))
    over["metrics"][3]["value"] = TTFA_MAX_MS + 1
    assert not score_call(over)["checks"]["opening_silence"]["pass"]

    missing = json.loads(json.dumps(base))
    missing["metrics"] = [m for m in missing["metrics"] if m["name"] != "time_to_first_agent_utterance"]
    assert not score_call(missing)["checks"]["opening_silence"]["pass"]

    envelope = {"simulation_result": base}
    assert score_call(envelope, transcript=greeting)["pass"]

    print("self-test ok", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", nargs="*", type=Path, help="get_simulation_result JSON files")
    p.add_argument("--transcript", type=Path, help="optional transcript JSON (applies to every file)")
    p.add_argument("--in-pod-tools", action="store_true", help="treat in-pod tool_post as check 2 pass")
    p.add_argument("--fetch-transcript", action="store_true", help="HTTP GET transcript_url when local file omitted")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.results:
        p.error("need result JSON files (or --self-test)")
    transcript = None
    if args.transcript:
        raw = _load_json(args.transcript)
        if isinstance(raw, list):
            transcript = [t for t in raw if isinstance(t, dict)]
        elif isinstance(raw, dict):
            for key in ("transcript", "utterances", "turns"):
                rows = raw.get(key)
                if isinstance(rows, list):
                    transcript = [t for t in rows if isinstance(t, dict)]
                    break
    failed = 0
    for path in args.results:
        result = _unwrap(_load_json(path))
        turns = transcript
        if turns is None and args.fetch_transcript and result.get("transcript_url"):
            turns = _fetch_transcript(str(result["transcript_url"]))
        score = score_call(result, transcript=turns, in_pod_tools=args.in_pod_tools)
        _print_score(score)
        if not score["pass"]:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
