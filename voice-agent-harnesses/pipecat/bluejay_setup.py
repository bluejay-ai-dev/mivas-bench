"""Create/refresh the Bluejay side of the Pipecat Daily SIP workers.

Bluejay `connection_type=SIP` dials `DAILY_SIP_URI` (the static pinless
interconnect address from `pinless_setup.py`).

    export BLUEJAY_API_KEY=...
    export DAILY_SIP_URI=sip:...@daily-...pinless-sip...
    uv run python voice-agent-harnesses/pipecat/bluejay_setup.py
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
PINLESS = HARNESS_DIR / ".daily-pinless.json"
API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")

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


def sip_uri(runtime: str) -> str:
    industry = os.environ.get("INDUSTRY", "control-industry")
    runtime_slug = runtime.replace("/", "-").replace(".", "-").replace("_", "-").lower()
    key = f"pipecat-{runtime_slug}-{industry.replace('_', '-')}".lower()
    if PINLESS.is_file():
        by_slug = json.loads(PINLESS.read_text())
        if isinstance(by_slug, dict) and by_slug.get(key):
            return str(by_slug[key]).strip()
    explicit = os.environ.get("DAILY_SIP_URI") or os.environ.get("PIPECAT_SIP_URI")
    if explicit:
        return explicit.strip()
    sys.exit(f"DAILY_SIP_URI missing and {key!r} not in {PINLESS} (run pinless_setup.py)")


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
    industry = HARNESS_DIR.parents[1] / "industries" / "control-industry"
    prompt = (industry / "system-prompts" / "receptionist.md").read_text()

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    by_name = {a["name"]: a["id"] for a in call("/all-agents")}

    def checkpoint() -> None:
        STATE.write_text(json.dumps(state, indent=2) + "\n")

    for runtime, desc in RUNTIMES.items():
        uri = sip_uri(runtime)
        st = state.setdefault(runtime, {})
        name = f"control-industry:pipecat {runtime}"
        st.setdefault("agent_id", by_name.get(name))
        payload = {
            "name": name,
            "system_prompt": prompt,
            "goals": GOALS,
            "connection_type": "SIP",
            "mode": "VOICE",
            "type": "INBOUND",
            "external_agent_id": f"pipecat/{runtime}",
            "folder": "MIVAS",
            "sip_uri": uri,
        }
        if not st["agent_id"]:
            st["agent_id"] = call("/add-agent", payload)["agent_id"]
            checkpoint()
        else:
            call("/update-agent", {"agent_id": str(st["agent_id"]), "sip_uri": uri, "connection_type": "SIP"})

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

        print(runtime, st, uri)

    checkpoint()


if __name__ == "__main__":
    main()
