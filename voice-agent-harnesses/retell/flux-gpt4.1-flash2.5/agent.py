"""Retell harness — gpt-4.1 + ElevenLabs Flash v2.5, native states/edges multi-agent.

There is no local text loop: Retell drives the whole call platform-side and the
only way in is the LiveKit web call, so `--check` pushes the blueprint to Retell
(states, edges, tool URLs) and prints the ids. Real runs go through
`adapters/chirp.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import TOOL_SERVER_URL, ensure_agent, industry_path, load_blueprint  # noqa: E402

MODEL = "retell-gpt4.1-flash2.5"


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR", str(industry_path(industry))))
    public_url = os.environ.get("PUBLIC_URL", "https://example.invalid")
    bp = load_blueprint(industry_dir)
    ids = ensure_agent(industry_dir, public_url)
    print(
        f"ok {industry_dir.name} × {MODEL} agent={ids['agent_id']} llm={ids['llm_id']} "
        f"states={list(bp['agents'])} tools→{public_url}/tool/* tool_server={TOOL_SERVER_URL}"
    )
