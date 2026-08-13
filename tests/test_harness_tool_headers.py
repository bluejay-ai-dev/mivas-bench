"""Every harness POST to the industry tool server carries X-Mivas-Call-Id."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from collections.abc import Coroutine
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
HARNESSES = ROOT / "voice-agent-harnesses"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from call_id import HEADER, bind_provider, for_provider, reset, set_call_id  # noqa: E402

FAMILIES = (
    "openai",
    "gemini",
    "vapi",
    "retell",
    "bland",
    "cartesia",
    "elevenlabs",
    "twilio",
    "grok",
    "nvidia",
    "assemblyai",
    "deepgram",
    "livekit",
    "pipecat",
)


@pytest.fixture(autouse=True)
def _clean_call_id() -> None:
    reset()
    yield
    reset()


class _FakeResp:
    def json(self) -> dict[str, Any]:
        return {"ok": True, "success": True}


class _RecordingClient:
    posts: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResp:
        _RecordingClient.posts.append({"url": url, "json": json, "headers": headers or {}})
        return _FakeResp()


@contextmanager
def _family_harness(family: str) -> Iterator[Any]:
    family_dir = HARNESSES / family
    saved = {name: sys.modules[name] for name in ("harness", "report") if name in sys.modules}
    sys.modules.pop("harness", None)
    sys.modules.pop("report", None)
    sys.path.insert(0, str(family_dir))
    sys.path.insert(0, str(RUNTIME))
    try:
        spec = importlib.util.spec_from_file_location(
            f"mivas_{family}_harness", family_dir / "harness.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if str(family_dir) in sys.path:
            sys.path.remove(str(family_dir))
        sys.modules.pop("harness", None)
        sys.modules.pop("report", None)
        sys.modules.pop(f"mivas_{family}_harness", None)
        sys.modules.update(saved)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def test_no_harness_posts_appointments_for_industry_tools() -> None:
    offenders: list[str] = []
    for path in HARNESSES.rglob("*.py"):
        if path.name == "report.py":
            continue
        text = path.read_text()
        if "/appointments" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            src = ast.get_source_segment(text, node) or ""
            if "/appointments" in src and (
                "tool_server" in src or "TOOL_SERVER" in src or "tool_server_url" in src
            ):
                offenders.append(f"{path.relative_to(ROOT)}: {src.splitlines()[0][:80]}")
    assert offenders == []


def test_every_tool_server_post_sets_call_id_header() -> None:
    offenders: list[str] = []
    for path in HARNESSES.rglob("*.py"):
        if path.name == "report.py":
            continue
        text = path.read_text()
        if "/tools/" not in text or ".post(" not in text:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if ".post(" not in line:
                continue
            window = "\n".join(lines[max(0, i - 4) : i + 12])
            if "/tools/" not in window:
                continue
            if "TOOL_SERVER" not in window and "tool_server" not in window:
                continue
            if HEADER not in window and "tool_headers(" not in window and "headers(" not in window:
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{i + 1}: {line.strip()[:100]}")
    assert offenders == []


def test_platform_webhooks_bind_bluejay_id() -> None:
    for family in ("vapi", "retell", "bland", "cartesia"):
        text = (HARNESSES / family / "adapters" / "chirp.py").read_text()
        assert "for_provider" in text, family
        assert "begin_session" in text, family
        assert "bind_provider" in text, family


def test_cartesia_run_tool_keeps_webhook_bind() -> None:
    """The Line webhook pins CALL_ID; run_tool must not for_provider(None) and mint."""
    _RecordingClient.posts = []
    try:
        with _family_harness("cartesia") as harness:
            bind_provider("sid_abc", "675")
            for_provider("sid_abc")
            with patch("httpx.AsyncClient", _RecordingClient):
                _run(harness.run_tool("schedule_appointment", {"date": "08/15/2026"}))
    except ModuleNotFoundError as e:
        pytest.skip(str(e))
    _assert_posted_header("675")


def test_cartesia_line_tools_carry_provider_call_id() -> None:
    """Line POSTs must include Cartesia call_id so a random replica can hit the bind store."""
    text = (HARNESSES / "cartesia" / "line_agent" / "main.py").read_text()
    assert "call_request" in text
    assert "?call_id=" in text
    assert "X-Call-Id" in text or "X-Cartesia-Call-Id" in text
    harness = (HARNESSES / "cartesia" / "harness.py").read_text()
    assert "src_digest" in harness
    chirp = (HARNESSES / "cartesia" / "adapters" / "chirp.py").read_text()
    assert "_record_line_tools" in chirp
    assert "emit_span=False" in chirp
    assert '"stream_id": resolved' in chirp


def _assert_posted_header(expected: str) -> None:
    assert _RecordingClient.posts, "expected a tool-server POST"
    last = _RecordingClient.posts[-1]
    assert "/tools/" in last["url"], last["url"]
    assert "/appointments" not in last["url"], last["url"]
    assert last["headers"].get(HEADER) == expected, last["headers"]


@pytest.mark.parametrize("family", FAMILIES)
def test_family_dispatch_sends_bluejay_header(family: str) -> None:
    _RecordingClient.posts = []
    try:
        with _family_harness(family) as harness:
            _dispatch_family(family, harness)
    except ModuleNotFoundError as e:
        pytest.skip(str(e))
    _assert_posted_header("675")


def test_vapi_two_calls_do_not_cross_headers() -> None:
    _RecordingClient.posts = []
    try:
        with _family_harness("vapi") as harness:
            bind_provider("vapi-aaa", "675")
            bind_provider("vapi-bbb", "676")
            with patch("httpx.AsyncClient", _RecordingClient):
                _run(
                    harness._execute_tool(
                        "schedule_appointment",
                        {"date": "08/15/2026"},
                        for_provider("vapi-aaa"),
                    )
                )
                _run(
                    harness._execute_tool(
                        "schedule_appointment",
                        {"date": "08/16/2026"},
                        for_provider("vapi-bbb"),
                    )
                )
    except ModuleNotFoundError as e:
        pytest.skip(str(e))
    assert [p["headers"].get(HEADER) for p in _RecordingClient.posts] == ["675", "676"]


def _dispatch_family(family: str, harness: Any) -> None:
    set_call_id("675")
    with patch("httpx.AsyncClient", _RecordingClient):
        if family == "openai":
            _run(harness.dispatch_industry_tool("schedule_appointment", {"date": "08/15/2026"}))
            return
        if family == "gemini":
            _run(harness._dispatch("schedule_appointment", {"date": "08/15/2026"}))
            return
        if family == "vapi":
            bind_provider("vapi-aaa", "675")
            _run(
                harness._execute_tool(
                    "schedule_appointment", {"date": "08/15/2026"}, for_provider("vapi-aaa")
                )
            )
            return
        if family in {"retell", "bland", "cartesia", "elevenlabs"}:
            _run(harness._execute_tool("schedule_appointment", {"date": "08/15/2026"}))
            return
        if family == "twilio":
            _run(harness.dispatch_industry_tool("schedule_appointment", {"date": "08/15/2026"}))
            return
        if family in {"assemblyai", "deepgram"}:
            _run(harness._dispatch("schedule_appointment", {"date": "08/15/2026"}))
            return
        if family == "livekit":
            _run(harness._execute("schedule_appointment", {"date": "08/15/2026"}, local=False))
            return
        if family == "pipecat":
            bp = {
                "agents": {
                    "receptionist": {
                        "tools": [{"name": "schedule_appointment"}],
                    }
                },
                "catalog": {"schedule_appointment": {}},
            }
            _run(
                harness._execute_tool(
                    "schedule_appointment",
                    {"date": "08/15/2026"},
                    bp,
                    {"agent": "receptionist"},
                )
            )
            return
        if family in {"grok", "nvidia"}:
            bp = {
                "agents": {
                    "scheduler": {
                        "tools": [{"name": "schedule_appointment"}],
                    }
                },
                "catalog": {"schedule_appointment": {}},
            }
            _run(
                harness._execute_tool(
                    "schedule_appointment",
                    {"date": "08/15/2026"},
                    bp,
                    {"agent": "scheduler"},
                )
            )
            return
        raise AssertionError(f"no dispatcher for {family}")
