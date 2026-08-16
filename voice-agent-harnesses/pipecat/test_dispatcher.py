"""Dispatcher unit tests — no Daily, no cluster."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import dispatcher  # noqa: E402


class _Resp:
    def __init__(self, code: int, content: bytes = b'{"ok":true}') -> None:
        self.status_code = code
        self.content = content
        self.headers = {"content-type": "application/json"}


def test_worker_url_default(monkeypatch) -> None:
    monkeypatch.delenv("PIPECAT_WORKER_URL_TEMPLATE", raising=False)
    assert dispatcher.worker_url("pipecat-cascaded-healthcare") == (
        "http://mivas-pipecat-cascaded-healthcare:8000/tools/dialin"
    )
    monkeypatch.setenv("PIPECAT_WORKER_URL_TEMPLATE", "https://{slug}.example.com/tools/dialin")
    assert dispatcher.worker_url("pipecat-cascaded-healthcare") == (
        "https://pipecat-cascaded-healthcare.example.com/tools/dialin"
    )


def test_probe_does_not_forward(monkeypatch) -> None:
    monkeypatch.setenv("PIPECAT_WORKER_URL_TEMPLATE", "http://worker/tools/dialin")
    monkeypatch.setenv("PIPECAT_DISPATCH_RETRIES", "1")
    calls: list[object] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None):
            calls.append((url, json))
            return _Resp(202)

    monkeypatch.setattr(dispatcher.httpx, "AsyncClient", _Client)
    client = TestClient(dispatcher.build_app())
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/dialin/not ok").status_code == 400
    probe = client.post("/dialin/pipecat-cascaded-healthcare", json={"test": "test"})
    assert probe.status_code == 200
    assert calls == []


def test_forwards_and_retries_busy(monkeypatch) -> None:
    monkeypatch.setenv("PIPECAT_WORKER_URL_TEMPLATE", "https://{slug}.example.com/tools/dialin")
    monkeypatch.setenv("PIPECAT_DISPATCH_RETRIES", "3")
    monkeypatch.setenv("PIPECAT_DISPATCH_RETRY_S", "0")
    codes = [409, 409, 202]

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None):
            assert url == "https://pipecat-cascaded-healthcare.example.com/tools/dialin"
            assert json["callId"] == "abc"
            return _Resp(codes.pop(0))

    monkeypatch.setattr(dispatcher.httpx, "AsyncClient", _Client)
    client = TestClient(dispatcher.build_app())
    r = client.post(
        "/dialin/pipecat-cascaded-healthcare",
        json={"callId": "abc", "callDomain": "dom", "From": "+1555"},
    )
    assert r.status_code == 202
    assert codes == []


def test_all_busy_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("PIPECAT_WORKER_URL_TEMPLATE", "http://mivas-{slug}:8000/tools/dialin")
    monkeypatch.setenv("PIPECAT_DISPATCH_RETRIES", "2")
    monkeypatch.setenv("PIPECAT_DISPATCH_RETRY_S", "0")

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None):
            return _Resp(409)

    monkeypatch.setattr(dispatcher.httpx, "AsyncClient", _Client)
    client = TestClient(dispatcher.build_app())
    r = client.post(
        "/dialin/pipecat-cascaded-healthcare",
        json={"callId": "abc"},
    )
    assert r.status_code == 503


def test_pinless_merge_keeps_other_slugs(monkeypatch) -> None:
    import pinless_setup

    monkeypatch.setenv("MIVAS_BASE_DOMAIN", "benchmarks.example.com")
    merged = pinless_setup.merge_entries(
        [
            {
                "name_prefix": "keep-me",
                "room_creation_api": "https://old.example/dialin/keep-me",
            },
            {
                "name_prefix": "pipecat-cascaded-healthcare",
                "room_creation_api": "https://old.example/dialin/stale",
            },
        ],
        ["pipecat-cascaded-healthcare"],
    )
    by_prefix = {e["name_prefix"]: e["room_creation_api"] for e in merged}
    assert by_prefix["keep-me"] == "https://old.example/dialin/keep-me"
    assert by_prefix["pipecat-cascaded-healthcare"] == (
        "https://pipecat-dialin.benchmarks.example.com/dialin/pipecat-cascaded-healthcare"
    )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
