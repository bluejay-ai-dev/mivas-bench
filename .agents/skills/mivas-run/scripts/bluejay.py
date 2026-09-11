#!/usr/bin/env python3
"""Drive Bluejay from the consumer side for one MIVAS harness × industry pair.

Everything goes through the public REST API with your own BLUEJAY_API_KEY
(X-API-Key). No Bluejay internals, no MCP.

    uv run python .agents/skills/mivas-run/scripts/bluejay.py ensure-agent --harness openai/realtime-2.1 --industry control-industry --url wss://HOST
    uv run python .agents/skills/mivas-run/scripts/bluejay.py smoke        --harness openai/realtime-2.1 --industry control-industry --url wss://HOST
    uv run python .agents/skills/mivas-run/scripts/bluejay.py full         --harness openai/realtime-2.1 --industry healthcare --runs 5
    uv run python .agents/skills/mivas-run/scripts/bluejay.py poll  --run RUN_ID
    uv run python .agents/skills/mivas-run/scripts/bluejay.py results --run RUN_ID --out verify-out/smoke/RUN_ID
    uv run python .agents/skills/mivas-run/scripts/bluejay.py score verify-out/smoke/RUN_ID

`smoke` = ensure agent → dedicated smoke simulation → smoke digital humans →
queue → poll → dump results → rubric. Exit 0 only when every call passes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
for extra in (HERE, ROOT / "scripts", ROOT / "runtime"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import score_rubric  # noqa: E402

DEFAULT_API = "https://api.getbluejay.ai/v1"
# tools are extracted after the harness posts trace ids, so these are unfinished
NOT_FINAL = {"RUNNING", "IN_PROGRESS", "QUEUED", "PENDING", "EVALUATING", "CONVERSATION_ENDED"}
NO_CONVERSATION = {"NO_ANSWER", "NO_CONNECTION", "CANCELLED", "SYSTEM_ERROR", "ERROR"}


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def api_url() -> str:
    return os.environ.get("BLUEJAY_API_URL", DEFAULT_API).rstrip("/")


def app_url() -> str:
    return os.environ.get("BLUEJAY_APP_URL", "https://app.getbluejay.ai").rstrip("/")


def _key() -> str:
    key = os.environ.get("BLUEJAY_API_KEY", "").strip()
    if not key:
        raise SystemExit("BLUEJAY_API_KEY is not set (create one under API Keys in the Bluejay app)")
    return key


def req(method: str, path: str, payload: dict[str, Any] | None = None, *, not_found_ok: bool = False) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(
        f"{api_url()}/{path}", data=data, method=method,
        headers={"X-API-Key": _key(), "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if not_found_ok and e.code == 404:
                return {}
            body = e.read()[:600].decode(errors="replace")
            if e.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"{method} {path} → {e.code} {body}") from e
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"{method} {path} → {e}") from e
    raise SystemExit(f"{method} {path} → gave up")


def slug(harness: str, industry: str) -> str:
    return f"{harness.replace('/', '-')}-{industry}".replace("_", "-").replace(".", "-").lower()


def default_url(harness: str, industry: str) -> str | None:
    base = os.environ.get("MIVAS_BASE_DOMAIN", "").strip().lower().strip(".")
    return f"wss://{slug(harness, industry)}.{base}" if base else None


# ---------------------------------------------------------------- agent

def _unwrap(body: dict[str, Any], *keys: str) -> dict[str, Any]:
    for k in keys:
        inner = body.get(k)
        if isinstance(inner, dict):
            return inner
    return body


def _id(obj: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def get_agent_by_external_id(external_id: str) -> dict[str, Any] | None:
    body = req("GET", f"agent-by-external-id/{urllib.parse.quote(external_id, safe='')}", not_found_ok=True)
    if not body:
        return None
    agent = _unwrap(body, "agent", "data")
    return agent if _id(agent, "id", "agent_id") is not None else None


def ensure_agent(harness: str, industry: str, url: str, user: str, password: str, name: str | None) -> int:
    external_id = f"mivas:{slug(harness, industry)}"
    ws = {
        "connection_type": "WEBSOCKET",
        "mode": "VOICE",
        "websocket_url": url,
        "websocket_username": user,
        "websocket_password": password,
    }
    existing = get_agent_by_external_id(external_id)
    if existing:
        agent_id = _id(existing, "id", "agent_id")
        req("POST", "update-agent", {"agent_id": str(agent_id), **ws})
        print(f"agent {agent_id} ({external_id}) → {url}", flush=True)
        return int(agent_id)  # type: ignore[arg-type]
    title = name or f"MIVAS {harness} × {industry}"
    req("POST", "add-agent", {
        "name": title,
        # Prompts and tools live in the industry pack on the harness; Bluejay only dials.
        "system_prompt": f"MIVAS harness {harness} running the {industry} pack. Prompts live in the pack.",
        "knowledge_base": "Not used. The harness owns prompts, tools and state.",
        "goals": ["Complete the caller's request using the industry's agents and tools."],
        "type": "INBOUND",
        "external_agent_id": external_id,
        **ws,
    })
    created = get_agent_by_external_id(external_id)
    if not created:
        raise SystemExit("add-agent succeeded but agent-by-external-id cannot find it; check the Bluejay app")
    agent_id = _id(created, "id", "agent_id")
    print(f"agent {agent_id} created ({external_id}) → {url}", flush=True)
    return int(agent_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------- simulation

def _sim(body: dict[str, Any]) -> dict[str, Any]:
    return _unwrap(body, "simulation", "data")


def _duration_seconds(sim: dict[str, Any]) -> int | None:
    settings = sim.get("settings") or {}
    raw = sim.get("max_call_duration") or settings.get("max_call_duration")
    if raw is None:
        return None
    units = str(sim.get("max_call_duration_units") or settings.get("max_call_duration_units") or "seconds").lower()
    return int(raw) * 60 if units.startswith("min") else int(raw)


def create_simulation(agent_id: int, name: str, *, minutes: int, max_concurrent: int, runs: int = 1) -> int:
    payload = {
        "agent_id": str(agent_id),
        "name": name,
        "max_concurrent": max_concurrent,
        "max_call_duration": minutes,
        "max_call_duration_units": "minutes",
        "runs_per_digital_human": runs,
        "hangup_on_transfer": False,
    }
    created = req("POST", "create-simulation", payload)
    sim_id = _id(created, "simulation_id", "id") or _id(_sim(created), "id")
    if sim_id is None:
        raise SystemExit(f"create-simulation returned no id: {json.dumps(created)[:300]}")
    # the API has stored minutes as seconds on create; re-PUT until it reads back right
    for _ in range(2):
        fetched = _sim(req("GET", f"simulation/{sim_id}"))
        if _duration_seconds(fetched) == minutes * 60:
            break
        req("PUT", f"simulation/{sim_id}", {"max_call_duration": minutes, "max_call_duration_units": "minutes"})
    else:
        print(f"warning: simulation {sim_id} max_call_duration stored as {_duration_seconds(fetched)}s", flush=True)
    print(f"simulation {sim_id}: {name} ({minutes} min cap, max_concurrent={max_concurrent})", flush=True)
    return int(sim_id)


def update_simulation(sim_id: int, **fields: Any) -> None:
    req("PUT", f"simulation/{sim_id}", fields)


# ---------------------------------------------------------------- digital humans

CONTROL_BOOKER = {
    "name": "Riley Booker",
    "test_name": "MIVAS smoke · control-industry · repair booking",
    "intent": (
        "You are calling Bluejay's Repair Services to schedule a repair appointment for next "
        "Tuesday afternoon. Wait for the receptionist to greet you, then say you need to book a "
        "repair. When asked for a date, say next Tuesday afternoon; if they ask for a specific "
        "date, give the calendar date of next Tuesday. Once the appointment is confirmed, thank "
        "them and end the call. Do not ask for anything else."
    ),
    "success_criteria": "Success requires handoff_to_scheduler and schedule_appointment to have been called.",
    "expected_tool_calls": [{"name": "handoff_to_scheduler"}, {"name": "schedule_appointment"}],
    "tags": ["mivas_smoke", "mivas_control_industry"],
    "speaks_first_config": {"speaks_first": False},
    "creativity": 0,
    "language": "en",
    "accent": "american",
    "gender": "female",
    "fluency": "native",
    "voice_speed": "normal",
    "verbosity": "medium",
    "interruptions": {"type": "none"},
    "allow_end_call_tool": True,
    "allow_silence_tool": True,
    "allow_dtmf_tool": False,
    "num_runs": 1,
    "background_noise": "none",
    "background_noise_volume": 0.0,
    "audio_quality": "high",
}


def smoke_humans(industry: str, n: int) -> list[dict[str, Any]]:
    """n digital humans for a smoke: the control booker, or n easy/perfect tasks of the pack."""
    import tasks_to_digital_humans as t2d  # repo script; source of truth for DH shape

    if industry == "control-industry" or not (ROOT / "industries" / industry / "tasks").is_dir():
        dh = dict(CONTROL_BOOKER)
        dh["intent"] = t2d.with_scenario_clock(dh["intent"], industry)
        return [dh]  # one DH, repeated by runs_per_digital_human
    humans = t2d.build(industry)

    def trait(h: dict[str, Any], name: str) -> str:
        return str(t2d.trait_value(h, name) or "")

    # one easy, clean-audio task per call area, so a 3-call smoke crosses three specialists
    picked: list[dict[str, Any]] = []
    seen_areas: set[str] = set()
    for h in humans:
        if trait(h, "difficulty") != "easy" or trait(h, "audio_condition") != "perfect":
            continue
        area = trait(h, "call_area")
        if area in seen_areas:
            continue
        seen_areas.add(area)
        picked.append(h)
        if len(picked) == n:
            break
    if len(picked) < n:
        picked += [h for h in humans if h not in picked][: n - len(picked)]
    for h in picked:
        h["test_name"] = f"MIVAS smoke · {h['test_name']}"
        h["tags"] = list(h.get("tags") or []) + ["mivas_smoke"]
    return picked


def find_dh_by_test_name(title: str) -> dict[str, Any] | None:
    body = req("GET", f"digital-human-by-test-name/{urllib.parse.quote(title, safe='')}", not_found_ok=True)
    dh = _unwrap(body, "digital_human", "data") if body else {}
    if isinstance(dh, dict) and dh.get("digital_human"):
        dh = dh["digital_human"]
    return dh if isinstance(dh, dict) and dh.get("id") else None


def ensure_humans(humans: list[dict[str, Any]], sim_id: int) -> list[int]:
    ids: list[int] = []
    to_create: list[dict[str, Any]] = []
    for dh in humans:
        live = find_dh_by_test_name(dh["test_name"])
        if live:
            sims = {int(s) for s in (live.get("simulation_ids") or []) if str(s).isdigit()}
            sims.add(sim_id)
            req("PUT", f"update-digital-human/{live['id']}", {"simulation_ids": sorted(sims)})
            ids.append(int(live["id"]))
        else:
            to_create.append(dh)
    if to_create:
        resp = req("POST", "create-digital-humans", {"simulation_ids": [sim_id], "digital_humans": to_create})
        rows = resp.get("digital_humans") or resp.get("created") or resp.get("created_digital_humans") or []
        if not rows and isinstance(resp.get("data"), list):
            rows = resp["data"]
        errs = resp.get("errors") or []
        if errs:
            raise SystemExit(f"create-digital-humans errors: {json.dumps(errs[:3])[:600]}")
        for row in rows:
            row = row.get("digital_human", row) if isinstance(row, dict) else row
            if isinstance(row, dict) and row.get("id"):
                ids.append(int(row["id"]))
    if len(ids) != len(humans):
        raise SystemExit(f"expected {len(humans)} digital humans on simulation {sim_id}, have {len(ids)}")
    print(f"digital humans on simulation {sim_id}: {ids}", flush=True)
    return ids


def humans_on_simulation(sim_id: int) -> list[int]:
    body = req("GET", f"digital-humans-by-simulation/{sim_id}")
    rows = body.get("digital_humans") or body.get("data") or []
    return [int(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]


# ---------------------------------------------------------------- runs

def queue(sim_id: int, dh_ids: list[int], runs: int) -> int:
    body = req("POST", "queue-simulation-run", {
        "simulation_id": str(sim_id),
        "digital_human_ids": [str(i) for i in dh_ids],
        "runs_per_digital_human": runs,
    })
    run_id = _id(body, "simulation_run_id", "run_id", "id")
    if run_id is None:
        raise SystemExit(f"queue-simulation-run returned no run id: {json.dumps(body)[:300]}")
    print(f"queued run {run_id}: {len(dh_ids)} digital humans × {runs}", flush=True)
    return int(run_id)


def run_results(run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body = req("GET", f"retrieve-simulation-results/{run_id}")
    run = body.get("simulation_run") or {}
    results = body.get("simulation_results") or body.get("results") or []
    return run, results


def poll(run_id: int, *, interval: int, timeout_min: int, sim_id: int | None = None) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_min * 60
    while True:
        run, results = run_results(run_id)
        counts: dict[str, int] = {}
        for r in results:
            counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
        run_status = str(run.get("status") or "?")
        settled = bool(results) and all(str(r.get("status")) not in NOT_FINAL for r in results)
        print(f"run {run_id} {run_status}: {counts}", flush=True)
        if settled and run_status.upper() not in NOT_FINAL:
            return results
        if time.monotonic() > deadline:
            raise SystemExit(f"run {run_id} still not final after {timeout_min} min")
        time.sleep(interval)


def dump_results(run_id: int, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    _, results = run_results(run_id)
    paths: list[Path] = []
    for r in results:
        rid = r.get("id")
        if rid is None:
            continue
        full = req("GET", f"retrieve-simulation-result/{rid}")
        path = out / f"{rid}.json"
        path.write_text(json.dumps(full, indent=2))
        paths.append(path)
    print(f"wrote {len(paths)} results → {out}", flush=True)
    return paths


def score_dir(out: Path, *, in_pod_tools: bool = False) -> bool:
    paths = sorted(out.glob("*.json"))
    if not paths:
        raise SystemExit(f"no result JSON in {out}")
    all_ok = True
    print(f"{'result':>8} {'dh':>7} {'status':<20} {'goal':<5} rubric")
    for path in paths:
        result = score_rubric._unwrap(json.loads(path.read_text()))
        status = str(result.get("status") or "?")
        if status in NO_CONVERSATION:
            print(f"{result.get('id')!s:>8} {result.get('digital_human_id')!s:>7} {status:<20} {'-':<5} FAIL (no conversation)")
            all_ok = False
            continue
        transcript = score_rubric._fetch_transcript(str(result["transcript_url"])) if result.get("transcript_url") else None
        score = score_rubric.score_call(result, transcript=transcript, in_pod_tools=in_pod_tools)
        flags = " ".join(f"{k}={'ok' if score['checks'][k]['pass'] else 'FAIL'}" for k in score_rubric.CHECKS)
        goal = result.get("goal_success")
        goal_s = "-" if goal is None else ("yes" if goal else "no")
        print(f"{result.get('id')!s:>8} {result.get('digital_human_id')!s:>7} {status:<20} {goal_s:<5} {'PASS' if score['pass'] else 'FAIL'} {flags}")
        all_ok = all_ok and score["pass"]
    print("goal_success is informational only; the rubric is the gate (see BLUEJAY.md).", flush=True)
    return all_ok


# ---------------------------------------------------------------- commands

def cmd_ensure_agent(a: argparse.Namespace) -> int:
    url = a.url or default_url(a.harness, a.industry)
    if not url:
        raise SystemExit("pass --url wss://… or set MIVAS_BASE_DOMAIN")
    ensure_agent(a.harness, a.industry, url, a.user, a.password, a.name)
    return 0


def cmd_smoke(a: argparse.Namespace) -> int:
    url = a.url or default_url(a.harness, a.industry)
    if not url:
        raise SystemExit("pass --url wss://… or set MIVAS_BASE_DOMAIN")
    if url.startswith("ws://") or "localhost" in url or "127.0.0.1" in url:
        raise SystemExit("Bluejay dials from the internet: use a public wss:// URL (ingress or a tunnel)")
    agent_id = a.agent_id or ensure_agent(a.harness, a.industry, url, a.user, a.password, None)
    pair = slug(a.harness, a.industry)
    sim_id = create_simulation(
        agent_id, f"MIVAS smoke · {pair}", minutes=a.minutes, max_concurrent=a.max_concurrent,
    )
    humans = smoke_humans(a.industry, a.calls)
    dh_ids = ensure_humans(humans, sim_id)
    runs = a.calls if len(dh_ids) == 1 else 1
    run_id = queue(sim_id, dh_ids, runs)
    print(f"{app_url()}/simulations/{sim_id}/runs/{run_id}", flush=True)
    poll(run_id, interval=a.interval, timeout_min=a.timeout_min)
    out = Path(a.out) if a.out else ROOT / "verify-out" / "smoke" / pair / str(run_id)
    dump_results(run_id, out)
    ok = score_dir(out, in_pod_tools=a.in_pod_tools)
    summary = {"harness": a.harness, "industry": a.industry, "slug": pair, "agent_id": agent_id,
               "simulation_id": sim_id, "run_id": run_id, "results_dir": str(out), "pass": ok,
               "run_url": f"{app_url()}/simulations/{sim_id}/runs/{run_id}"}
    (out / "smoke.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    print("SMOKE PASS" if ok else "SMOKE FAIL — see HARNESS.md → fix loop", flush=True)
    return 0 if ok else 1


def cmd_queue(a: argparse.Namespace) -> int:
    dh_ids = [int(x) for x in a.dh_ids] if a.dh_ids else humans_on_simulation(a.sim)
    if not dh_ids:
        raise SystemExit(f"simulation {a.sim} has no digital humans")
    run_id = queue(a.sim, dh_ids, a.runs)
    print(f"{app_url()}/simulations/{a.sim}/runs/{run_id}", flush=True)
    return 0


def cmd_poll(a: argparse.Namespace) -> int:
    poll(a.run, interval=a.interval, timeout_min=a.timeout_min)
    return 0


def cmd_results(a: argparse.Namespace) -> int:
    dump_results(a.run, Path(a.out))
    return 0


def cmd_score(a: argparse.Namespace) -> int:
    return 0 if score_dir(Path(a.dir), in_pod_tools=a.in_pod_tools) else 1


def cmd_full(a: argparse.Namespace) -> int:
    """Whole industry: pack tasks → digital humans on a fresh simulation → queue k runs each."""
    if not (ROOT / "industries" / a.industry / "tasks").is_dir():
        raise SystemExit(f"{a.industry} has no scored task suite (control-industry is smoke-only)")
    url = a.url or default_url(a.harness, a.industry)
    agent_id = a.agent_id
    if agent_id is None:
        if not url:
            raise SystemExit("pass --agent-id, or --url / MIVAS_BASE_DOMAIN so the agent can be ensured")
        agent_id = ensure_agent(a.harness, a.industry, url, a.user, a.password, None)
    pair = slug(a.harness, a.industry)
    cmd = [sys.executable, str(ROOT / "scripts" / "tasks_to_digital_humans.py"),
           "--industry", a.industry, "--push", "--agent-id", str(agent_id),
           "--name", f"MIVAS {a.industry} · {a.harness} · k={a.runs}"]
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        return proc.returncode
    sim_id = None
    for line in proc.stdout.splitlines():
        if line.startswith("simulation ") and " on agent " in line:
            sim_id = int(line.split()[1])
    if sim_id is None:
        raise SystemExit("could not read the simulation id from tasks_to_digital_humans output")
    update_simulation(sim_id, max_concurrent=a.max_concurrent, runs_per_digital_human=a.runs)
    dh_ids = humans_on_simulation(sim_id)
    run_id = queue(sim_id, dh_ids, a.runs)
    run_url = f"{app_url()}/simulations/{sim_id}/runs/{run_id}"
    print(run_url, flush=True)
    print(
        "\nwhen the run is final:\n"
        f"  uv run python verifiers/verify_run.py {run_id}\n"
        f"  uv run python verifiers/verify_task_run.py {run_id} --industry {a.industry} --harness {a.harness}\n"
        f"  uv run python scripts/bluejay_run_to_csv.py {run_id} --industry {a.industry} --harness {a.harness}\n",
        flush=True,
    )
    if a.no_wait:
        print(f"resume with: bluejay.py poll --run {run_id}", flush=True)
        return 0
    poll(run_id, interval=a.interval, timeout_min=a.timeout_min)
    print(f"run {run_id} final → {run_url}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def pair_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--harness", default=os.environ.get("HARNESS", "openai/realtime-2.1"))
        sp.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
        sp.add_argument("--url", help="public wss:// Bluejay should dial (default: wss://{slug}.$MIVAS_BASE_DOMAIN)")
        sp.add_argument("--user", default=os.environ.get("CHIRP_USER", "mivas"))
        sp.add_argument("--password", default=os.environ.get("CHIRP_PASS", "mivas"))

    def poll_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--interval", type=int, default=30, help="seconds between polls")
        sp.add_argument("--timeout-min", type=int, default=240)

    sp = sub.add_parser("ensure-agent", help="create or repoint the Bluejay WEBSOCKET agent for this pair")
    pair_args(sp); sp.add_argument("--name"); sp.set_defaults(fn=cmd_ensure_agent)

    sp = sub.add_parser("smoke", help="3 smoke calls on Bluejay, scored by the rubric")
    pair_args(sp); poll_args(sp)
    sp.add_argument("--calls", type=int, default=3)
    sp.add_argument("--max-concurrent", type=int, default=1)
    sp.add_argument("--minutes", type=int, default=4, help="max call duration")
    sp.add_argument("--agent-id", type=int)
    sp.add_argument("--out")
    sp.add_argument("--in-pod-tools", action="store_true", help="count in-pod tool_post logs as the tool check")
    sp.set_defaults(fn=cmd_smoke)

    sp = sub.add_parser("queue", help="queue a run on an existing simulation")
    poll_args(sp)
    sp.add_argument("--sim", type=int, required=True)
    sp.add_argument("--dh-ids", nargs="*", help="default: every digital human on the simulation")
    sp.add_argument("--runs", type=int, default=1)
    sp.set_defaults(fn=cmd_queue)

    sp = sub.add_parser("poll", help="wait for a run to be final")
    poll_args(sp); sp.add_argument("--run", type=int, required=True); sp.set_defaults(fn=cmd_poll)

    sp = sub.add_parser("results", help="dump every result of a run as JSON")
    sp.add_argument("--run", type=int, required=True); sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_results)

    sp = sub.add_parser("score", help="rubric over a results directory")
    sp.add_argument("dir"); sp.add_argument("--in-pod-tools", action="store_true"); sp.set_defaults(fn=cmd_score)

    sp = sub.add_parser("full", help="run the whole industry task suite (k runs per task)")
    pair_args(sp); poll_args(sp)
    sp.add_argument("--agent-id", type=int)
    sp.add_argument("--runs", type=int, default=5, help="k conversations per task (Pass^k)")
    sp.add_argument("--max-concurrent", type=int, default=3, help="≤ MIVAS_REPLICAS × in-process socket limit")
    sp.add_argument("--no-wait", action="store_true")
    sp.set_defaults(fn=cmd_full)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
