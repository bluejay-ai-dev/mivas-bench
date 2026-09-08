#!/usr/bin/env python3
"""Post-run audio eval: transcript, diarization, latency stats per result.

Rewritten 2026-09-08 (the original was untracked and lost in cleanup).
Recordings are stereo — one speaker per channel — so diarization comes from
per-channel Deepgram transcription instead of the old pyannote pass.

    uv run python scripts/transcribe_diarize_runs.py \
        --run 300109:grok-voice --out verify-out/audio_eval --workers 16

Writes verify-out/audio_eval/{result_id}.json (skips existing files).
Needs BLUEJAY_API_KEY and DEEPGRAM_API_KEY; recordings are read from
s3://bluejay-simulation-recordings/<result_id>/audio_recording.mp3 via boto3.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3

ROOT = Path(__file__).resolve().parents[1]
RECORDING_BUCKET = "bluejay-simulation-recordings"
BLUEJAY_API = "https://api.getbluejay.ai/v1"
DEEPGRAM_URL = (
    "https://api.deepgram.com/v1/listen"
    "?model=nova-2&multichannel=true&punctuate=true&utterances=true&smart_format=true"
)
# Statuses whose calls produced audio worth scoring.
AUDIO_STATUSES = {"COMPLETED", "CONVERSATION_ENDED"}
AGENT_MARKERS = ("ai assistant", "halverson", "straus", "kestrel", "how can i help")


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


def bluejay_get(path: str) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{BLUEJAY_API}/{path}",
        headers={"Authorization": f"Bearer {os.environ['BLUEJAY_API_KEY']}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def deepgram(audio: bytes) -> dict[str, Any]:
    req = urllib.request.Request(
        DEEPGRAM_URL,
        data=audio,
        headers={
            "Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}",
            "Content-Type": "audio/mpeg",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def channel_utterances(dg: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    by_channel: dict[int, list[dict[str, Any]]] = {}
    for u in dg.get("results", {}).get("utterances", []) or []:
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        by_channel.setdefault(int(u.get("channel", 0)), []).append(
            {"start": round(float(u["start"]), 2), "end": round(float(u["end"]), 2), "text": text}
        )
    for turns in by_channel.values():
        turns.sort(key=lambda t: t["start"])
    return by_channel


def agent_channel(by_channel: dict[int, list[dict[str, Any]]]) -> int:
    """The agent greets with brand + AI disclosure; fall back to who speaks first."""
    for ch, turns in by_channel.items():
        head = " ".join(t["text"].lower() for t in turns[:3])
        if any(m in head for m in AGENT_MARKERS):
            return ch
    firsts = {ch: turns[0]["start"] for ch, turns in by_channel.items() if turns}
    return min(firsts, key=firsts.get) if firsts else 0


def merge_turns(turns: list[dict[str, Any]], gap: float = 1.0) -> list[dict[str, Any]]:
    """Collapse same-speaker utterances separated by short pauses into one turn."""
    merged: list[dict[str, Any]] = []
    for t in turns:
        if merged and t["start"] - merged[-1]["end"] <= gap:
            merged[-1]["end"] = max(merged[-1]["end"], t["end"])
        else:
            merged.append({"start": t["start"], "end": t["end"]})
    return merged


def latencies(own: list[dict[str, Any]], other: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Response gap: each of `own`'s turns that follows an `other` turn end."""
    out: list[dict[str, Any]] = []
    for turn in own:
        prev_ends = [o["end"] for o in other if o["end"] <= turn["start"]]
        if not prev_ends:
            continue
        prev_end = max(prev_ends)
        # Only a response if nothing of our own sat between the two.
        if any(prev_end < s["end"] <= turn["start"] for s in own if s is not turn):
            continue
        out.append({"at": turn["start"], "latency_ms": int(round((turn["start"] - prev_end) * 1000))})
    return out


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = sorted(r["latency_ms"] for r in rows)
    if not vals:
        return {"count": 0, "avg_ms": None, "p50_ms": None, "p90_ms": None,
                "max_ms": None, "median_abs_dev_ms": None}
    med = statistics.median(vals)
    return {
        "count": len(vals),
        "avg_ms": int(round(sum(vals) / len(vals))),
        "p50_ms": int(round(med)),
        "p90_ms": int(round(vals[min(len(vals) - 1, int(0.9 * len(vals)))])),
        "max_ms": int(max(vals)),
        "median_abs_dev_ms": int(round(statistics.median(abs(v - med) for v in vals))),
    }


def interruptions(agent: list[dict[str, Any]], user: list[dict[str, Any]]) -> dict[str, Any]:
    """A speaker starting >0.3s inside the other's active turn is an interruption."""
    def overlaps(starter: list[dict[str, Any]], active: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hits = []
        for s in starter:
            for a in active:
                if a["start"] + 0.3 <= s["start"] < a["end"] - 0.3:
                    hits.append({"at": s["start"], "into": a["start"]})
                    break
        return hits

    by_user = overlaps(user, agent)
    by_agent = overlaps(agent, user)
    return {
        "customer_interruption_count": len(by_user),
        "customer_interruption_details": by_user,
        "agent_interruption_count": len(by_agent),
        "agent_interruption_details": by_agent,
    }


def process_result(res: dict[str, Any], run_id: str, label: str, out_dir: Path, s3) -> str:
    rid = res["id"]
    dest = out_dir / f"{rid}.json"
    if dest.is_file():
        return f"{rid} skip"
    obj = s3.get_object(Bucket=RECORDING_BUCKET, Key=f"{rid}/audio_recording.mp3")
    dg = deepgram(obj["Body"].read())
    by_channel = channel_utterances(dg)
    ach = agent_channel(by_channel)
    agent_uts = by_channel.get(ach, [])
    user_uts = [t for ch, ts in by_channel.items() if ch != ach for t in ts]
    user_uts.sort(key=lambda t: t["start"])

    transcript = sorted(
        [{"speaker": "AGENT", **t} for t in agent_uts]
        + [{"speaker": "USER", **t} for t in user_uts],
        key=lambda t: t["start"],
    )
    transcript = [
        {"speaker": t["speaker"], "start": t["start"], "end": t["end"], "text": t["text"]}
        for t in transcript
    ]
    agent_turns = merge_turns(agent_uts)
    user_turns = merge_turns(user_uts)
    diarization = sorted(
        [{"speaker": "AGENT", **t} for t in agent_turns]
        + [{"speaker": "USER", **t} for t in user_turns],
        key=lambda t: t["start"],
    )
    a_lat = latencies(agent_turns, user_turns)
    u_lat = latencies(user_turns, agent_turns)
    package = {
        "result_id": rid,
        "run_id": int(run_id),
        "model": label,
        "digital_human_id": res.get("digital_human_id"),
        "status": res.get("status"),
        "duration_s": res.get("duration"),
        "transcript": transcript,
        "diarization": diarization,
        "agent_latencies": a_lat,
        "customer_latencies": u_lat,
        "agent_latency_stats": stats(a_lat),
        "customer_latency_stats": stats(u_lat),
        "interruptions": interruptions(agent_turns, user_turns),
    }
    dest.write_text(json.dumps(package))
    return f"{rid} ok"


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="append", required=True, metavar="RUN_ID:LABEL")
    ap.add_argument("--out", default=str(ROOT / "verify-out" / "audio_eval"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    s3 = boto3.client("s3")

    jobs: list[tuple[dict[str, Any], str, str]] = []
    for spec in args.run:
        run_id, _, label = spec.partition(":")
        listing = bluejay_get(f"retrieve-simulation-results/{run_id}")
        for res in listing.get("simulation_results") or []:
            if res.get("status") in AUDIO_STATUSES:
                jobs.append((res, run_id, label or "model"))

    done = errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_result, res, run_id, label, out_dir, s3): res["id"]
            for res, run_id, label in jobs
        }
        for fut in as_completed(futures):
            rid = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 — per-result isolation
                errors += 1
                print(f"{rid} ERROR {e}", flush=True)
            done += 1
            if done % 25 == 0 or done == len(jobs):
                print(f"progress {done}/{len(jobs)} (errors {errors})", flush=True)
    print(f"done: {len(jobs)}, errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
