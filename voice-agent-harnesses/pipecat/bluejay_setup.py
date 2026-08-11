"""Create/refresh the Bluejay side of the Pipecat proof runs (agent, sim, DH).

Re-run it whenever the cloudflared URL changes: the deployed bot reaches the
industry tool server through `pipecat_agent_configuration.tool_server_url`, so a
new tunnel only needs an `update-agent`, never a redeploy.

    export BLUEJAY_API_KEY=...
    export TOOL_SERVER_URL=https://<tunnel>.trycloudflare.com
    uv run python voice-agent-harnesses/pipecat/bluejay_setup.py

Ids are cached in `.bluejay-ids.json` next to this file (gitignored).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
STATE = HARNESS_DIR / ".bluejay-ids.json"
API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
PIPECAT_AGENT = os.environ.get("PIPECAT_AGENT_NAME", "mivas-control")

RUNTIMES = {
    "cascaded": "Deepgram Flux flux-general-en + gpt-4.1 + ElevenLabs eleven_flash_v2_5",
    "openai-realtime-2.1": "OpenAI Realtime gpt-realtime-2.1",
    "gemini-flash-live-3.1": "Gemini gemini-3.1-flash-live-preview",
}
GOALS = [
    "Hand off to scheduler when the caller wants an appointment",
    "Schedule a concrete repair appointment date",
    "End the call when done",
]
INTENT = (
    "Call Bluejay's Repair Services and schedule a repair appointment for next "
    "Tuesday afternoon."
)
SUCCESS = "Appointment is scheduled for a concrete date."


def call(path: str, data: dict | None = None) -> dict:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        sys.exit("BLUEJAY_API_KEY is required")
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code} {e.read().decode()[:600]}") from e


def main() -> None:
    tool_url = os.environ.get("TOOL_SERVER_URL")
    if not tool_url:
        sys.exit("TOOL_SERVER_URL must be a PUBLIC url — the bot runs in Pipecat Cloud")
    industry = HARNESS_DIR.parents[1] / "industries" / "control-industry"
    prompt = (industry / "system-prompts" / "receptionist.md").read_text()

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    by_name = {a["name"]: a["id"] for a in call("/all-agents")}

    def checkpoint() -> None:
        STATE.write_text(json.dumps(state, indent=2) + "\n")

    for runtime, desc in RUNTIMES.items():
        st = state.setdefault(runtime, {})
        name = f"control-industry:pipecat {runtime}"
        st.setdefault("agent_id", by_name.get(name))
        if not st["agent_id"]:
            st["agent_id"] = call("/add-agent", {
                "name": name,
                "system_prompt": prompt,
                "goals": GOALS,
                "connection_type": "PIPECAT",
                "mode": "VOICE",
                "type": "INBOUND",
                "external_agent_id": f"pipecat/{runtime}",
                "folder": "MIVAS",
                "pipecat_agent_name": PIPECAT_AGENT,
                "pipecat_agent_configuration": {"runtime": runtime},
            })["agent_id"]
            checkpoint()

        if not st.get("simulation_id"):
            st["simulation_id"] = call("/create-simulation", {
                "agent_id": str(st["agent_id"]),
                "name": f"MIVAS control — pipecat {runtime}",
                "description": desc,
                "max_concurrent": 1,
                "max_call_duration": 180,
                "max_call_duration_units": "seconds",
            })["simulation_id"]
            checkpoint()

        # the config is the bot's only channel: runtime, tool server, and the
        # simulation id it resolves the live simulation_result_id from
        cfg = {
            "runtime": runtime,
            "tool_server_url": tool_url.rstrip("/"),
            "simulation_id": st["simulation_id"],
        }
        call("/update-agent", {
            "agent_id": str(st["agent_id"]),
            "pipecat_agent_name": PIPECAT_AGENT,
            "pipecat_agent_configuration": cfg,
        })

        if not st.get("digital_human_id"):
            call("/create-digital-humans", {
                "simulation_ids": [st["simulation_id"]],
                "digital_humans": [{
                    "name": f"pipecat {runtime} scheduler caller",
                    "test_name": f"pipecat {runtime} — schedule next Tuesday afternoon",
                    "intent": INTENT,
                    "success_criteria": SUCCESS,
                    "expected_tool_calls": [
                        {"name": "handoff_to_scheduler"},
                        {"name": "schedule_appointment"},
                    ],
                    "speaks_first_config": {"speaks_first": False},
                    "language": "en",
                    "accent": "american",
                }],
            })
            dhs = call(f"/digital-humans-by-simulation/{st['simulation_id']}")
            st["digital_human_id"] = (dhs.get("digital_humans") or dhs)[0]["id"]
            checkpoint()

        print(runtime, st, cfg["tool_server_url"])

    checkpoint()


if __name__ == "__main__":
    main()
