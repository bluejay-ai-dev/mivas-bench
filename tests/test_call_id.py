"""call_id helper in isolation — no harness imported."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from call_id import (  # noqa: E402
    CALL_ID,
    HEADER,
    begin_session,
    bind_provider,
    end_session,
    for_provider,
    headers,
    log_tool_post,
    log_ws_accept,
    pod_name,
    provider_id_from_payload,
    provider_id_from_request,
    reset,
    set_call_id,
    sole_session,
)


@pytest.fixture(autouse=True)
def _clean_call_id(monkeypatch: pytest.MonkeyPatch) -> None:
    reset()
    monkeypatch.setattr("snapshot.capture_final", lambda _cid: None)
    yield
    reset()


def test_set_call_id_uses_bluejay_id() -> None:
    assert set_call_id("675") == "675"
    assert CALL_ID.get() == "675"
    assert headers() == {HEADER: "675"}


def test_set_call_id_mints_when_missing() -> None:
    minted = set_call_id(None)
    assert minted.startswith("call_")
    assert len(minted) > 5
    assert headers()[HEADER] == minted
    assert set_call_id("") != ""
    assert set_call_id("  ") != "  "


def test_headers_never_empty() -> None:
    reset()
    got = headers()
    assert HEADER in got
    assert got[HEADER]
    assert not got[HEADER].isspace()


def test_headers_explicit_id_wins() -> None:
    set_call_id("675")
    assert headers("676") == {HEADER: "676"}
    assert CALL_ID.get() == "675"


def test_bind_and_for_provider_roundtrip() -> None:
    bind_provider("vapi-aaa", "675")
    bind_provider("vapi-bbb", "676")
    assert for_provider("vapi-aaa") == "675"
    assert CALL_ID.get() == "675"
    assert for_provider("vapi-bbb") == "676"
    assert CALL_ID.get() == "676"


def test_two_provider_calls_do_not_clobber() -> None:
    bind_provider("a", "675")
    bind_provider("b", "676")
    assert for_provider("a") == "675"
    assert for_provider("b") == "676"
    assert for_provider("a") == "675"


def test_sole_session_used_when_provider_unknown() -> None:
    begin_session("675", session_key="ws-1")
    assert sole_session() == "675"
    assert for_provider("mystery") == "675"


def test_two_sessions_block_sole_fallback() -> None:
    begin_session("675", session_key="ws-1")
    begin_session("676", session_key="ws-2")
    assert sole_session() is None
    minted = for_provider("mystery")
    assert minted not in {"675", "676"}
    assert minted.startswith("call_")


def test_end_session_restores_sole(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr("snapshot.capture_final", lambda cid: seen.append(cid))
    begin_session("675", session_key="ws-1")
    begin_session("676", session_key="ws-2")
    end_session("ws-2")
    assert sole_session() == "675"
    assert seen == ["676"]


def test_provider_id_from_payload() -> None:
    assert provider_id_from_payload({"call": {"id": "vapi-1"}}) == "vapi-1"
    assert provider_id_from_payload({"call": {"call_id": "retell-1"}}) == "retell-1"
    assert provider_id_from_payload({"call_id": "x"}) == "x"
    assert provider_id_from_payload({"date": "08/15/2026"}) is None
    assert provider_id_from_payload(None) is None


def test_provider_id_from_request() -> None:
    assert provider_id_from_request({"call_id": "body"}) == "body"
    assert provider_id_from_request({}, query={"call_id": "q"}) == "q"
    assert provider_id_from_request({}, headers={"x-call-id": "hdr"}) == "hdr"
    assert provider_id_from_request({"date": "08/15/2026"}) is None


def test_pinning_logs_include_pod(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOSTNAME", "mivas-openai-aaa")
    assert pod_name() == "mivas-openai-aaa"
    log_ws_accept("675")
    log_tool_post("675", path="/tools/schedule_appointment")
    out = capsys.readouterr().out
    assert "call_id: ws_accept sim=675 pod=mivas-openai-aaa" in out
    assert "call_id: tool_post sim=675 pod=mivas-openai-aaa path=/tools/schedule_appointment" in out
