"""post_trace_ids must settle, POST once, and never wait for COMPLETED or relink.

    uv run python voice-agent-harnesses/openai/test_post_trace_ids.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "{}") -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse()

    async def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        self.gets.append(url)
        return _FakeResponse()

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def _case_posts_once_after_settle() -> None:
    sleeps: list[float] = []
    client = _FakeClient()

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    report._api_key = lambda: "test-key"  # type: ignore[method-assign]
    report._api_url = lambda: "https://api.example.test/v1"  # type: ignore[method-assign]
    report.httpx.AsyncClient = lambda timeout=20: client  # type: ignore[method-assign]
    report.asyncio.sleep = _sleep  # type: ignore[method-assign]

    await report.post_trace_ids("737917", "abc123")

    assert sleeps == [10.0], sleeps
    assert client.gets == [], f"must not poll status before POST: {client.gets}"
    assert len(client.posts) == 1, client.posts
    assert client.posts[0]["url"].endswith("/update-simulation-result")
    assert client.posts[0]["json"] == {
        "simulation_result_id": "737917",
        "trace_ids": ["abc123"],
    }


async def _case_skips_without_key() -> None:
    client = _FakeClient()
    report._api_key = lambda: None  # type: ignore[method-assign]
    report.httpx.AsyncClient = lambda timeout=20: client  # type: ignore[method-assign]
    await report.post_trace_ids("737917", "abc123")
    assert client.posts == []


def main() -> None:
    asyncio.run(_case_posts_once_after_settle())
    asyncio.run(_case_skips_without_key())
    print("ok")


if __name__ == "__main__":
    main()
