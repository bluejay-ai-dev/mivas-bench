"""Tool-span success must accept {"ok": true} (tool servers) and {"success": true}
(transfer_*/end_call). Source-level tripwire: importing the harnesses pulls heavy
runtime deps, so assert the ok-derivation line directly."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ["grok", "aws", "qwen", "nvidia"]


@pytest.mark.parametrize("family", HARNESSES)
def test_tool_span_ok_accepts_both_keys(family):
    src = (ROOT / "voice-agent-harnesses" / family / "harness.py").read_text()
    assert 'result.get("ok") or result.get("success")' in src, (
        f"{family}/harness.py span-status must accept both 'ok' and 'success' "
        "response keys; success-only marks every tool-server call as ERROR"
    )
    assert 'ok = bool(result.get("success"))' not in src
