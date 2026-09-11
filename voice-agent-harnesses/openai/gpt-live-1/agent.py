"""gpt-live-1 harness entry: offline --check of pack → session shapes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live import MODEL, LiveSession  # noqa: E402
from pack import industry_path, load_pack, today_line  # noqa: E402


async def _noop_audio(_: bytes) -> None: ...
async def _noop_tool(_n: str, _a: dict) -> dict: return {"success": True}


def check(industry: str | Path) -> None:
    pack = load_pack(industry)
    live = LiveSession(pack, api_key="check", on_audio=_noop_audio, run_tool=_noop_tool)
    cfg = live.session_config()
    json.dumps(cfg)  # must be wire-serialisable
    assert cfg["model"] == MODEL
    assert cfg["audio"]["format"] == {"type": "audio/pcm", "rate": 16000}
    assert cfg["delegation"]["type"] == "responses"
    start = pack.stages[pack.start]
    names = {t["name"] for t in cfg["delegation"]["responses"]["tools"]}
    assert names == {t.name for t in start.tools}, "start stage tools must match blueprint"
    assert today_line() in cfg["instructions"] and today_line() in cfg["delegation"]["responses"]["instructions"]
    assert start.prompt.strip() in cfg["instructions"]
    for name, stage in pack.stages.items():
        if name != pack.start:
            assert stage.prompt.strip() not in cfg["instructions"], f"{name} prompt leaked into live prompt"
            assert len(stage.handoff_notice()) <= 2 * 1400, f"{name} handoff notice needs >2 appends"
    # The live model has a small context window; keep its prompt well inside 16k tokens
    # (~3 chars/token is the conservative bound).
    assert len(cfg["instructions"]) < 16_384 * 3, "live instructions too long for the live model"
    print(
        f"ok {pack.industry} × {MODEL} start={pack.start} stages={list(pack.stages)} "
        f"backend={live.backend_model} live_prompt_chars={len(cfg['instructions'])} "
        f"tool_server={os.environ.get('TOOL_SERVER_URL', 'http://127.0.0.1:8000')}"
    )


if __name__ == "__main__":
    industry = next((a for a in sys.argv[1:] if not a.startswith("-")), "control-industry")
    industry_dir = Path(os.environ.get("INDUSTRY_DIR") or industry_path(industry))
    if "--check" in sys.argv:
        check(industry_dir)
    else:
        raise SystemExit("run adapters/chirp.py for a live bridge; agent.py only supports --check")
