"""Register a Pipecat runtime as a LiveKit Cloud agent worker.

Bluejay `connection_type=LIVEKIT` dispatches by `livekit_agent_name`. This process
accepts those jobs, joins the assigned room over LiveKitTransport, and runs the
existing `bot.run_bot` pipeline. The industry tool server stays in-process
(`TOOL_SERVER_URL=http://127.0.0.1:8000`).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from livekit.agents import AgentServer, JobContext, JobExecutorType, cli  # noqa: E402
from livekit.api import AccessToken, VideoGrants  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport  # noqa: E402

import bot  # noqa: E402
import harness  # noqa: E402
import report  # noqa: E402

logger = logging.getLogger("mivas.pipecat.livekit")

AGENT_NAMES = {
    "cascaded": "mivas-pipecat-cascaded",
    "openai-realtime-2.1": "mivas-pipecat-openai-realtime",
    "gemini-flash-live-3.1": "mivas-pipecat-gemini-live",
}


def sim_result_id_from_job_metadata(raw: Any) -> str | None:
    """Bluejay puts X-Simulation-Result-Id on the LiveKit job metadata JSON."""
    if not raw:
        return None
    meta = raw if isinstance(raw, dict) else None
    if meta is None:
        try:
            meta = json.loads(str(raw))
        except Exception:
            logger.warning("job.metadata is not valid JSON: %s", raw)
            return None
    if not isinstance(meta, dict):
        return None
    for key in (
        "X-Simulation-Result-Id",
        "x-simulation-result-id",
        "simulation_result_id",
        "simulationResultId",
    ):
        val = meta.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _job_room_name(ctx: JobContext) -> str:
    job = ctx.job
    room = getattr(job, "room", None)
    name = getattr(room, "name", None) if room is not None else None
    if name:
        return str(name)
    return str(getattr(ctx.room, "name", "") or "")


def _job_url_and_token(ctx: JobContext, identity: str, room_name: str) -> tuple[str, str]:
    """Prefer the assigned job token so LiveKit Cloud counts this worker as joined.

    Do not also call ``ctx.connect()`` — LiveKitTransport opens its own RTC
    session. A second join with the same identity kicks the first participant.
    """
    info = getattr(ctx, "_info", None)
    job_url = str(getattr(info, "url", "") or "").strip()
    job_token = str(getattr(info, "token", "") or "").strip()
    if job_url and job_token:
        return job_url, job_token
    url = os.environ.get("LIVEKIT_URL", "").strip()
    key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    if not url or not key or not secret:
        raise SystemExit("LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are required")
    token = (
        AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return url, token


def main() -> None:
    runtime = os.environ.get("HARNESS_RUNTIME") or harness.DEFAULT_RUNTIME
    if runtime not in AGENT_NAMES:
        raise SystemExit(f"unknown pipecat runtime {runtime!r}")
    agent_name = AGENT_NAMES[runtime]
    url = os.environ.get("LIVEKIT_URL", "").strip()
    if not url:
        raise SystemExit("LIVEKIT_URL is required")

    async def entrypoint(ctx: JobContext) -> None:
        room_name = _job_room_name(ctx)
        if not room_name:
            raise RuntimeError("LiveKit job has no room name")
        sim_id = sim_result_id_from_job_metadata(getattr(ctx.job, "metadata", None))
        job_url, job_token = _job_url_and_token(ctx, agent_name, room_name)
        logger.info(
            "pipecat livekit job runtime=%s room=%s sim=%s",
            runtime,
            room_name,
            sim_id,
        )
        transport = LiveKitTransport(
            url=job_url,
            token=job_token,
            room_name=room_name,
            params=LiveKitParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
            ),
        )
        async with report.traced_run(
            f"mivas-{bot.INDUSTRY}-{runtime}",
            simulation_result_id=sim_id,
            model=harness.RUNTIMES[runtime],
        ):
            await bot.run_bot(transport, runtime)

    server = AgentServer(job_executor_type=JobExecutorType.THREAD)
    server.rtc_session(agent_name=agent_name)(entrypoint)
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)
    sys.argv = [sys.argv[0], "start"]
    cli.run_app(server)


if __name__ == "__main__":
    main()
