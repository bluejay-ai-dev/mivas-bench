"""Blueprint → NVIDIA Nemotron cascaded services (ASR → LLM → TTS) + Flows nodes.

Stack mirrors NVIDIA-AI-Blueprints/nemotron-voice-agent (cloud NIM profile):
  NvidiaSTTService (Nemotron ASR Streaming)
  NvidiaLLMService (nemotron-3-nano-30b-a3b)
  NvidiaTTSService (Magpie Multilingual)

Multi-agent: Pipecat Flows — one NodeConfig per blueprint agent, each with its
own prompt and tool set. Handoff tools (`handoff: true`) return
`(result, next_node)` and FlowManager swaps context + advertised tools.
Industry tools map onto the industry state API; session tools (`end_call`) hang up.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

# stdlib, not loguru: tests/ import this module in a venv without pipecat.
log = logging.getLogger(__name__)

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import begin_session, end_session, headers as tool_headers, set_call_id  # noqa: E402

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1] if len(HARNESS_DIR.parents) > 1 else HARNESS_DIR

RUNTIME = "nemotron"
MODEL = os.environ.get(
    "NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-nano-30b-a3b"
)
LLM_BASE_URL = os.environ.get(
    "NEMOTRON_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
ASR_SERVER = os.environ.get("NEMOTRON_ASR_SERVER", "grpc.nvcf.nvidia.com:443")
ASR_FUNCTION_ID = os.environ.get(
    "NEMOTRON_ASR_FUNCTION_ID", "bb0837de-8c7b-481f-9ec8-ef5663e9c1fa"
)
ASR_MODEL = os.environ.get("NEMOTRON_ASR_MODEL", "nemotron-asr-streaming")
TTS_SERVER = os.environ.get("NEMOTRON_TTS_SERVER", "grpc.nvcf.nvidia.com:443")
TTS_VOICE = os.environ.get(
    "NEMOTRON_TTS_VOICE", "Magpie-Multilingual.EN-US.Aria"
)
SAMPLE_RATE = int(os.environ.get("NEMOTRON_SAMPLE_RATE", "16000"))
# SynthesizeOnline has no deadline in Pipecat 1.7; NVCF "failed to establish
# link to worker" then sits until the channel default (tens of seconds) and
# the greeting never plays (run 231249, TTFU 61–87s). Fail fast and retry.

# Verbatim from control-industry receptionist prompt; Flows kicks greeting via LLMRun.
GREETING = "Welcome to Bluejay's Repair Services!"

_IO_INSTALLED = False
_MAGPIE_LOCK = threading.Lock()
_MAGPIE_CONFIG = None


def io_workers() -> int:
    raw = os.environ.get("NEMOTRON_IO_WORKERS", "24").strip() or "24"
    n = int(raw)
    if n < 8:
        raise ValueError(f"NEMOTRON_IO_WORKERS must be >= 8, got {n}")
    return n


def install_io_executor() -> None:
    """Widen the loop default executor. 1-CPU pods get min(32, cpu+4)=5 threads,
    which starved two of six Magpie start()s (run 230706)."""
    global _IO_INSTALLED
    if _IO_INSTALLED:
        return
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=io_workers(),
            thread_name_prefix="nvidia-io",
        )
    )
    _IO_INSTALLED = True


def attach_magpie(tts) -> None:
    """Per-call gRPC channel, process-wide synthesis config.

    GetRivaSynthesisConfig is the only blocking network RPC in TTS start(); six
    at once hung behind live SynthesizeOnline streams (run 230708), so it is
    fetched once under the lock and reused. The channel must NOT be shared:
    Pipecat's _close_client() closes auth.channel, so with one shared client the
    first caller to hang up killed TTS for the other five with "Cannot invoke
    RPC: Channel closed!" (run 230716). grpc.secure_channel is lazy, so a
    per-call client costs no I/O here.
    """
    global _MAGPIE_CONFIG
    tts._initialize_client()
    with _MAGPIE_LOCK:
        if _MAGPIE_CONFIG is None:
            _MAGPIE_CONFIG = tts._create_synthesis_config()
    tts._config = _MAGPIE_CONFIG
    tts._load_zero_shot_audio_prompt()


def warm_magpie() -> None:
    """Pay GetRivaSynthesisConfig at process start, while the pod is idle."""
    tts = build_tts()
    try:
        attach_magpie(tts)
    finally:
        tts._close_client()


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    for base in (HARNESS_DIR / "industries", REPO_ROOT / "industries"):
        if (base / name).is_dir():
            return (base / name).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


def load_blueprint(industry_dir: str | Path) -> dict[str, Any]:
    industry_dir = industry_path(industry_dir)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = {
        t["name"]: t
        for t in json.loads((industry_dir / "tools.json").read_text())["tools"]
    }
    agents = {
        entry["name"]: {
            "name": entry["name"],
            "instructions": (industry_dir / entry["system_prompt"]).read_text(),
            "tools": entry["tools"],
        }
        for entry in blueprint["agents"]
    }
    return {
        "industry_dir": industry_dir,
        "start": blueprint["agents"][0]["name"],
        "agents": agents,
        "catalog": catalog,
        # Pack opener for speak-first DHs. Healthcare reception.md assumes this
        # already played; control-industry has none and the LLM greets itself.
        "greeting": (blueprint.get("greeting") or "").strip(),
    }


def agent_order(bp: dict[str, Any]) -> list[str]:
    return [bp["start"]] + [n for n in bp["agents"] if n != bp["start"]]


def instructions(bp: dict[str, Any], agent: str) -> str:
    return bp["agents"][agent]["instructions"]


def tool_names(bp: dict[str, Any], agent: str) -> list[str]:
    return [t["name"] for t in bp["agents"][agent]["tools"] if t["name"] in bp["catalog"]]


def handoff_target(bp: dict[str, Any], agent: str, tool: str) -> str | None:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool and t.get("handoff"):
            target = t.get("handoff_to")
            return target if target in bp["agents"] else None
    return None


def is_session_tool(bp: dict[str, Any], agent: str, tool: str) -> bool:
    for t in bp["agents"][agent]["tools"]:
        if t["name"] == tool:
            return bool(t.get("session"))
    return False


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")


def use_ssl() -> bool:
    """TLS for ASR/TTS gRPC. Cloud NVCF is TLS; a local/LAN NIM is plaintext."""
    raw = os.environ.get("NEMOTRON_USE_SSL", "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes", "on")
    return "nvcf.nvidia.com" in ASR_SERVER or ASR_SERVER.endswith(":443")


def nvidia_api_key() -> str:
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if key:
        return key
    if use_ssl():
        raise SystemExit("need NVIDIA_API_KEY")
    return ""


def asr_model_function_map() -> dict[str, str]:
    """NVCF needs function_id. A local NIM only wants the model name."""
    out: dict[str, str] = {"model_name": ASR_MODEL}
    explicit = os.environ.get("NEMOTRON_ASR_FUNCTION_ID")
    if explicit is not None and not explicit.strip():
        return out
    if not use_ssl():
        if explicit and explicit.strip():
            out["function_id"] = explicit.strip()
        return out
    out["function_id"] = ASR_FUNCTION_ID
    return out


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run a blueprint tool. Returns (result, should_end_call)."""
    target = handoff_target(bp, state["agent"], name)
    if target:
        state["agent"] = target
        return {"success": True, "role": target}, False

    if name == "end_call" or is_session_tool(bp, state["agent"], name):
        return {"success": True}, True

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        return resp.json(), False


async def run_tool(
    name: str,
    args: dict[str, Any],
    bp: dict[str, Any],
    state: dict[str, Any],
    *,
    call_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    from report import finish_tool_span, tool_span

    parent = state.get("_otel_root")
    log.info("tool_post name=%s sim=%s", name, call_id or "")
    with tool_span(name, args, call_id=call_id, parent=parent) as span:
        try:
            result, stop = await _execute_tool(name, args, bp, state)
            ok = bool(result.get("success"))
        except Exception as e:  # noqa: BLE001 — dead tool must not kill the call
            result, stop, ok = (
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                False,
                False,
            )
        finish_tool_span(
            span, result, ok=ok, name=name, parameters=args
        )
        return result, stop


def _spec(bp: dict[str, Any], name: str) -> tuple[str, dict, list]:
    spec = bp["catalog"][name]
    raw = spec.get("inputSchema") or {}
    return (
        spec.get("description", name),
        dict(raw.get("properties") or {}),
        list(raw.get("required") or []),
    )


def flows_node(
    bp: dict[str, Any],
    agent: str,
    handler,
    *,
    respond_immediately: bool = True,
):
    """One agent as a Pipecat Flows node: its own prompt, its own functions."""
    import functools

    from nvidia_fc import append_fc_protocol
    from pipecat.flows import FlowsFunctionSchema, NodeConfig
    from pipecat.flows.types import ContextStrategy, ContextStrategyConfig

    decls = [
        {
            "name": name,
            "description": d,
            "parameters": {"type": "object", "properties": p, "required": r},
        }
        for name in tool_names(bp, agent)
        for d, p, r in [_spec(bp, name)]
    ]
    return NodeConfig(
        name=agent,
        task_messages=[
            {
                "role": "system",
                "content": append_fc_protocol(instructions(bp, agent), decls),
            }
        ],
        functions=[
            FlowsFunctionSchema(
                name=name,
                description=d,
                properties=p,
                required=r,
                handler=functools.partial(handler, name),
            )
            for name in tool_names(bp, agent)
            for d, p, r in [_spec(bp, name)]
        ],
        context_strategy=ContextStrategyConfig(strategy=ContextStrategy.RESET),
        respond_immediately=respond_immediately,
    )


def build_stt():
    from pipecat.services.nvidia.stt import NvidiaSTTService

    class NonblockingNvidiaSTTService(NvidiaSTTService):
        """TLS + gRPC Auth() must not run on the uvicorn loop (6-way run 230683)."""

        async def start(self, frame):
            await asyncio.to_thread(self._initialize_client)
            real_init = self._initialize_client
            self._initialize_client = lambda: None  # noqa: E731
            try:
                await super().start(frame)
            finally:
                self._initialize_client = real_init

    return NonblockingNvidiaSTTService(
        api_key=nvidia_api_key(),
        server=ASR_SERVER,
        use_ssl=use_ssl(),
        sample_rate=SAMPLE_RATE,
        model_function_map=asr_model_function_map(),
    )


def llm_retries() -> int:
    raw = os.environ.get("NEMOTRON_LLM_RETRIES", "3").strip() or "3"
    n = int(raw)
    if n < 0:
        raise ValueError(f"NEMOTRON_LLM_RETRIES must be >= 0, got {n}")
    return n


def magpie_rpc_timeout() -> float:
    raw = os.environ.get("NEMOTRON_MAGPIE_RPC_TIMEOUT", "8").strip() or "8"
    n = float(raw)
    if n <= 0:
        raise ValueError(f"NEMOTRON_MAGPIE_RPC_TIMEOUT must be > 0, got {n}")
    return n


def is_capacity_error(exc: BaseException) -> bool:
    """NVCF admission control refusing the request, not a defect in our call.

    Run 2026-08-14, 8 incidents across 3 pods: an SSE error body about 250ms
    after the POST, "ResourceExhausted: Worker local total request limit
    reached (32/32)", also seen as (33/32) and (50/32). The counter passed 50
    while at most 18 of our own completions could be in flight, so the ceiling
    belongs to a shared NVCF worker rather than to our key or our replicas.

    Matched on the message, not on a status: NVIDIA documents neither the error
    nor a code for it, and third-party reports have it arriving as 503, as 429
    and, as here, inside the SSE body of an otherwise healthy stream. A bare
    503 with no capacity wording is a real failure and must not be replayed.
    """
    from openai import RateLimitError

    text = str(exc).lower()
    if "worker local total request limit" in text or "resourceexhausted" in text:
        return True
    # Magpie SynthesizeOnline, same NVCF admission: the RPC dies before any
    # audio, so the pipeline ErrorFrame silences the turn (run 231225).
    if "failed to establish link to worker" in text:
        return True
    return isinstance(exc, RateLimitError)


def is_magpie_capacity_error(exc: BaseException) -> bool:
    """Admission refusal or a hung SynthesizeOnline before the first frame.

    Pipecat's Magpie client sets no RPC deadline. NVCF worker-link failures
    surface as DEADLINE_EXCEEDED after tens of seconds (run 231249). We cap
    the RPC at magpie_rpc_timeout(); that abort looks like a generic
    deadline and must still retry, but only when no audio has arrived.
    """
    if is_capacity_error(exc):
        return True
    text = str(exc).lower()
    return "deadline_exceeded" in text or "deadline exceeded" in text


def is_magpie_bad_text_error(exc: BaseException) -> bool:
    """Triton tokenizer abort on leftover markdown / zero-width chars."""
    return "multichar start character" in str(exc).lower()


async def stream_with_retry(open_stream, *, reset=None):
    """Yield a completion stream, re-opening it while NVCF refuses for capacity.

    Pipecat turns any completion exception into a non-fatal ErrorFrame, so a
    refusal drops the turn: the agent goes silent and the call is scored as a
    model failure. The refusal is fail-fast and carries no tokens, so
    re-issuing the same request is safe, nothing reached TTS and no tool call
    was parsed. Once a chunk has been yielded the request is no longer
    replayable, and a mid-stream failure propagates instead, otherwise a retry
    would duplicate text or a function_call.

    `reset` re-arms the caller's per-response state, which the abandoned
    attempt consumed.
    """
    delay = 0.25
    for attempt in range(llm_retries() + 1):
        if reset is not None:
            reset()
        started = False
        try:
            stream = await open_stream()
            async with contextlib.aclosing(stream):
                async for chunk in stream:
                    started = True
                    yield chunk
            return
        except Exception as e:  # noqa: BLE001, re-raised unless retryable
            if started or not is_capacity_error(e):
                raise
            if attempt == llm_retries():
                log.error(
                    "NVCF refused capacity on all %d attempts, turn lost "
                    "(upstream limit, not the model): %s",
                    attempt + 1,
                    e,
                )
                raise
            wait = delay * (0.5 + random.random())
            log.warning(
                "NVCF refused capacity on attempt %d, retrying in %.2fs: %s",
                attempt + 1,
                wait,
                e,
            )
            delay = min(delay * 2, 2.0)
        await asyncio.sleep(wait)


def build_llm():
    """Text LLM for Flows. Prompt/tools are per-node, not on the service."""
    from pipecat.services.nvidia.llm import NvidiaLLMService, NvidiaLLMSettings

    class RetryingNvidiaLLMService(NvidiaLLMService):
        """NvidiaLLMService that survives an NVCF capacity refusal.

        In every logged incident the next attempt 0.3s to 2.9s later
        succeeded, one turn needing two.

        Hosted nano often writes NVIDIA <TOOLCALL> into the text stream
        instead of OpenAI tool_calls (copay 727501). Parse those before
        Magpie speaks the XML.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._toolcall_buf = ""
            self._parsed_toolcalls: list[dict] = []
            self._ran_fc = False

        async def run_function_calls(self, function_calls):
            if function_calls:
                self._ran_fc = True
            return await super().run_function_calls(function_calls)

        async def get_chat_completions(self, context):
            parent = super().get_chat_completions
            return stream_with_retry(
                lambda: parent(context), reset=self._reset_response_state
            )

        async def _push_llm_text(self, text: str):
            from nvidia_fc import drain_toolcall_text, speakable_text

            self._toolcall_buf += text or ""
            speech, rest, calls = drain_toolcall_text(self._toolcall_buf)
            self._toolcall_buf = rest
            self._parsed_toolcalls.extend(calls)
            spoken = speakable_text(speech)
            if spoken:
                await super()._push_llm_text(spoken)

        async def _process_context(self, context):
            import uuid

            from nvidia_fc import (
                advertised_tool_names,
                drain_toolcall_text,
                infer_transfer_tool,
                last_user_text,
                speakable_text,
            )
            from pipecat.frames.frames import FunctionCallFromLLM

            self._toolcall_buf = ""
            self._parsed_toolcalls = []
            self._ran_fc = False
            await super()._process_context(context)
            if self._toolcall_buf:
                speech, rest, calls = drain_toolcall_text(
                    self._toolcall_buf, flush=True
                )
                self._toolcall_buf = rest
                self._parsed_toolcalls.extend(calls)
                spoken = speakable_text(speech)
                if spoken:
                    await super()._push_llm_text(spoken)
            pending = list(self._parsed_toolcalls)
            if not pending and not self._ran_fc:
                advertised = advertised_tool_names(self._functions)
                user = last_user_text(context.get_messages())
                inferred = infer_transfer_tool(user, advertised)
                if inferred:
                    log.info(
                        "inferred handoff from user text: %s", inferred["name"]
                    )
                    pending.append(inferred)
                else:
                    log.info(
                        "no inferred handoff advertised=%s user=%r",
                        sorted(advertised),
                        user[:160],
                    )
            if not pending:
                return
            names = [c["name"] for c in pending]
            if self._parsed_toolcalls:
                log.info("parsed <TOOLCALL> from nano text: %s", names)
            await self.run_function_calls(
                [
                    FunctionCallFromLLM(
                        context=context,
                        tool_call_id=f"nvcf-{uuid.uuid4()}",
                        function_name=c["name"],
                        arguments=c.get("arguments") or {},
                    )
                    for c in pending
                ]
            )

    # Match nemotron-voice-agent cloud catalog: thinking off for lowest latency.
    settings = NvidiaLLMSettings(model=MODEL)
    settings.extra = {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "repetition_penalty": 1.05,
        }
    }
    return RetryingNvidiaLLMService(
        api_key=nvidia_api_key(),
        base_url=LLM_BASE_URL,
        settings=settings,
    )


def build_tts():
    from pipecat.services.nvidia.tts import NvidiaTTSService

    class NonblockingNvidiaTTSService(NvidiaTTSService):
        """Off-loop Magpie client + config; Event when start() can take TTSSpeakFrame."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ready = asyncio.Event()

        def _blocking_start(self) -> None:
            attach_magpie(self)

        async def start(self, frame):
            await super(NvidiaTTSService, self).start(frame)
            await asyncio.to_thread(self._blocking_start)
            self.ready.set()

        # No semaphore around run_tts. It existed to stop six Magpie start()
        # handshakes racing live streams; attach_magpie() now caches the only
        # blocking RPC, so the handshake is free. Delaying synthesis is not:
        # Pipecat cleans up the TTS context when the LLM turn ends, and any
        # sentence still waiting for a slot is truncated or dropped (run 230744,
        # "...date of birth? This").

        def _synthesis_handler(self, state):
            """Retry SynthesizeOnline while NVCF refuses a Magpie worker.

            Run 231225: DEADLINE_EXCEEDED "failed to establish link to worker"
            on the first RPC, before any audio. Pipecat turns that into an
            ErrorFrame and the agent goes silent ("I haven't heard from you").
            The request_generator has not been consumed yet, so the text queue
            is still full and a new call is safe. Once a response has arrived
            the stream is no longer replayable.
            """
            event_loop = self.get_event_loop()
            delay = 0.25
            started = False
            last_exc: BaseException | None = None
            for attempt in range(llm_retries() + 1):
                if state.stop_event.is_set():
                    break
                base_req = self._build_base_request()

                def request_generator(req=base_req):
                    from nvidia_fc import speakable_text

                    while True:
                        if state.stop_event.is_set():
                            break
                        text = state.text_queue.get()
                        if text is None or state.stop_event.is_set():
                            break
                        req.text = speakable_text(text)
                        if not req.text:
                            continue
                        yield req

                try:
                    call = self._service.stub.SynthesizeOnline(
                        request_generator(),
                        metadata=self._service.auth.get_auth_metadata(),
                        timeout=magpie_rpc_timeout(),
                    )
                    state.rpc_call = call
                    for resp in call:
                        started = True
                        if state.stop_event.is_set():
                            break
                        asyncio.run_coroutine_threadsafe(
                            state.response_queue.put(resp), event_loop
                        )
                    last_exc = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    if started:
                        break
                    if is_magpie_bad_text_error(e):
                        log.warning(
                            "Magpie rejected stitched text; dropping: %s", e
                        )
                        last_exc = None
                        break
                    if not is_magpie_capacity_error(e):
                        break
                    if attempt == llm_retries():
                        log.error(
                            "Magpie NVCF refused a worker on all %d attempts: %s",
                            attempt + 1,
                            e,
                        )
                        break
                    wait = delay * (0.5 + random.random())
                    log.warning(
                        "Magpie NVCF refused a worker on attempt %d, "
                        "retrying in %.2fs: %s",
                        attempt + 1,
                        wait,
                        e,
                    )
                    delay = min(delay * 2, 2.0)
                    time.sleep(wait)
                finally:
                    state.rpc_call = None
            if last_exc is not None and not state.stop_event.is_set():
                log.error("gRPC synthesis stream error: %s", last_exc)
                asyncio.run_coroutine_threadsafe(
                    state.response_queue.put(last_exc), event_loop
                )
            asyncio.run_coroutine_threadsafe(
                state.response_queue.put(None), event_loop
            )

        async def _run_tts_per_sentence(self, text: str, context_id: str):
            """Pipecat 1.7 default: one SynthesizeOnline per sentence.

            Run 231249 never hit ``_synthesis_handler`` (stitched mode). A
            refused Magpie worker then blocked the greeting for the channel
            deadline. Retry that RPC with ``magpie_rpc_timeout`` so the
            agent speaks before the DH asks if anyone is there.
            """
            from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame

            if not self.audio_context_available(context_id):
                await self.create_audio_context(context_id)
                await self.start_ttfb_metrics()
                yield TTSStartedFrame(context_id=context_id)

            from nvidia_fc import speakable_text

            text = speakable_text(text)
            chunks = [
                chunk
                for chunk in self._split_text_into_chunks(text)
                if any(c.isalnum() for c in chunk)
            ]
            if not chunks:
                return

            await self.start_tts_usage_metrics(text)

            response_queue: asyncio.Queue = asyncio.Queue()
            event_loop = self.get_event_loop()
            stop_event = threading.Event()

            def run_grpc():
                try:
                    for chunk in chunks:
                        if stop_event.is_set():
                            break
                        delay = 0.25
                        started = False
                        for attempt in range(llm_retries() + 1):
                            if stop_event.is_set():
                                break
                            base_req = self._build_base_request()

                            def request_gen(req=base_req, spoken=chunk):
                                req.text = spoken
                                yield req

                            try:
                                call = self._service.stub.SynthesizeOnline(
                                    request_gen(),
                                    metadata=self._service.auth.get_auth_metadata(),
                                    timeout=magpie_rpc_timeout(),
                                )
                                self._per_sentence_rpc_call = call
                                try:
                                    for resp in call:
                                        if stop_event.is_set():
                                            break
                                        started = True
                                        asyncio.run_coroutine_threadsafe(
                                            response_queue.put(resp),
                                            event_loop,
                                        )
                                finally:
                                    if self._per_sentence_rpc_call is call:
                                        self._per_sentence_rpc_call = None
                                break
                            except Exception as e:  # noqa: BLE001
                                if started:
                                    raise
                                if is_magpie_bad_text_error(e):
                                    log.warning(
                                        "Magpie rejected chunk %r; skipping: %s",
                                        chunk[:80],
                                        e,
                                    )
                                    break
                                if not is_magpie_capacity_error(e):
                                    raise
                                if attempt == llm_retries():
                                    log.error(
                                        "Magpie NVCF refused a worker on all "
                                        "%d per-sentence attempts: %s",
                                        attempt + 1,
                                        e,
                                    )
                                    raise
                                wait = delay * (0.5 + random.random())
                                log.warning(
                                    "Magpie NVCF refused a worker on "
                                    "per-sentence attempt %d, retrying in "
                                    "%.2fs: %s",
                                    attempt + 1,
                                    wait,
                                    e,
                                )
                                delay = min(delay * 2, 2.0)
                                time.sleep(wait)
                except Exception as e:  # noqa: BLE001
                    if not stop_event.is_set():
                        asyncio.run_coroutine_threadsafe(
                            response_queue.put(e), event_loop
                        )
                finally:
                    self._per_sentence_rpc_call = None
                    asyncio.run_coroutine_threadsafe(
                        response_queue.put(None), event_loop
                    )

            grpc_task = self.create_task(
                asyncio.to_thread(run_grpc), name="nvidia-tts-per-sentence"
            )
            try:
                while True:
                    item = await response_queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    await self.stop_ttfb_metrics()
                    yield TTSAudioRawFrame(
                        audio=item.audio,
                        sample_rate=self.sample_rate,
                        num_channels=1,
                        context_id=context_id,
                    )
            finally:
                stop_event.set()
                self._cancel_per_sentence_call()
                await self.cancel_task(grpc_task)

    return NonblockingNvidiaTTSService(
        api_key=nvidia_api_key(),
        server=TTS_SERVER,
        use_ssl=use_ssl(),
        voice_id=TTS_VOICE,
        sample_rate=SAMPLE_RATE,
    )


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


def _check_capacity_retry() -> None:
    """NVCF capacity refusals are replayed before the first chunk, never after."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    def opener(*attempts):
        """Each attempt is a list of chunks; an Exception in it is raised there."""
        pending = list(attempts)

        async def open_stream():
            async def gen(items):
                for item in items:
                    if isinstance(item, Exception):
                        raise item
                    yield item

            return gen(pending.pop(0))

        return open_stream

    def drain(*attempts):
        async def go():
            return [c async for c in stream_with_retry(opener(*attempts))]

        return asyncio.run(go())

    refused = RuntimeError(
        "ResourceExhausted: Worker local total request limit reached (32/32)"
    )
    magpie = RuntimeError(
        'DEADLINE_EXCEEDED:reason:"failed to establish link to worker"'
    )
    assert is_capacity_error(refused)
    assert is_capacity_error(magpie)
    assert is_magpie_capacity_error(RuntimeError("StatusCode.DEADLINE_EXCEEDED"))
    assert not is_capacity_error(RuntimeError("StatusCode.DEADLINE_EXCEEDED"))
    assert not is_capacity_error(RuntimeError("invalid request"))
    assert magpie_rpc_timeout() > 0

    def assert_backoff(waits: list[float]) -> None:
        """Each wait is its doubling base jittered by 0.5x to 1.5x."""
        for i, wait in enumerate(waits):
            base = min(0.25 * 2**i, 2.0)
            assert base * 0.5 <= wait <= base * 1.5, f"wait {i} off base {base}: {waits}"
        assert any(w != min(0.25 * 2**i, 2.0) for i, w in enumerate(waits)), waits

    asyncio.sleep = fake_sleep
    os.environ["NEMOTRON_LLM_RETRIES"] = "3"
    try:
        # Two refusals then a real completion: the turn survives.
        assert drain([refused], [refused], ["hi", " there"]) == ["hi", " there"]
        assert len(sleeps) == 2, sleeps
        assert_backoff(sleeps)

        # The budget is finite and the final refusal still reaches the caller.
        # Three waits also pin the doubling: a fixed delay fails the third band.
        sleeps.clear()
        try:
            drain(*([[refused]] * 4))
            raise AssertionError("exhausted retries must re-raise")
        except RuntimeError as e:
            assert "ResourceExhausted" in str(e), e
        assert len(sleeps) == 3, sleeps
        assert_backoff(sleeps)

        # A refusal after the first chunk is not replayable: retrying there
        # would duplicate spoken text or a function_call.
        sleeps.clear()
        try:
            drain(["hi", refused], ["never reached"])
            raise AssertionError("mid-stream failure must not retry")
        except RuntimeError:
            pass
        assert sleeps == [], sleeps

        # Anything that is not a capacity refusal fails fast.
        try:
            drain([RuntimeError("boom")], ["never reached"])
            raise AssertionError("non-capacity error must not retry")
        except RuntimeError as e:
            assert str(e) == "boom", e
        assert sleeps == [], sleeps
    finally:
        asyncio.sleep = real_sleep
        os.environ.pop("NEMOTRON_LLM_RETRIES", None)


def demo() -> None:
    """Self-check: blueprint, per-agent tool split and handoff, no network."""
    import asyncio

    bp = load_blueprint("control-industry")
    assert bp["start"] == "receptionist", bp["start"]
    assert agent_order(bp) == ["receptionist", "scheduler"], agent_order(bp)
    assert tool_names(bp, "receptionist") == ["handoff_to_scheduler", "end_call"]
    assert tool_names(bp, "scheduler") == ["schedule_appointment", "end_call"]
    assert "schedule_appointment" not in tool_names(bp, "receptionist")
    assert handoff_target(bp, "receptionist", "handoff_to_scheduler") == "scheduler"
    assert f'say: "{GREETING}"' in instructions(bp, "receptionist"), GREETING

    state = {"agent": bp["start"]}
    res, stop = asyncio.run(run_tool("handoff_to_scheduler", {}, bp, state))
    assert res == {"success": True, "role": "scheduler"} and not stop, res
    assert state["agent"] == "scheduler"

    res, stop = asyncio.run(run_tool("end_call", {"reason": "done"}, bp, state))
    assert res == {"success": True} and stop

    os.environ["TOOL_SERVER_URL"] = "http://127.0.0.1:1"
    res, stop = asyncio.run(
        run_tool("schedule_appointment", {"date": "08/18/2026"}, bp, state)
    )
    assert res["success"] is False and "error" in res, res

    _check_capacity_retry()

    from nvidia_fc import drain_toolcall_text, parse_toolcalls, speakable_text

    blob = '<TOOLCALL>[{"name": "handoff_to_scheduler", "arguments": {}}]</TOOLCALL>'
    assert parse_toolcalls(blob) == [
        {"name": "handoff_to_scheduler", "arguments": {}}
    ]
    speech, held, calls = drain_toolcall_text("Sure. " + blob)
    assert "Sure." in speech and not held and calls[0]["name"] == "handoff_to_scheduler"
    speech, held, calls = drain_toolcall_text("wait <TOOLCALL>[")
    assert speech.strip() == "wait" and held.startswith("<TOOLCALL")
    assert speakable_text("copay is **$50**") == "copay is $50"
    mashed = "Sure—\u200btocancel"
    clean = speakable_text(mashed)
    assert "\u200b" not in clean and "\u2014" not in clean
    assert "Sure" in clean
    assert is_magpie_bad_text_error(
        RuntimeError(
            "Encountered a multichar start character but not an end character."
        )
    )

    from nvidia_fc import infer_transfer_tool

    cancel = "Hi, I need to cancel my follow-up appointment tomorrow."
    got = infer_transfer_tool(
        cancel, {"transfer_to_identity", "transfer_to_scheduling"}
    )
    assert got and got["name"] == "transfer_to_identity", got
    assert got["arguments"]["next_intent"] == "scheduling"
    copay = infer_transfer_tool(
        "What's my Aetna copay at Park Avenue?",
        {"transfer_to_coverage", "transfer_to_scheduling"},
    )
    assert copay and copay["name"] == "transfer_to_coverage", copay
    alice = (
        "Hi, I'm calling about my balance. Could you give me a detailed "
        "breakdown of each charge?"
    )
    # Reception advertises identity, not transfer_to_billing (Alice 727608).
    bill = infer_transfer_tool(
        alice,
        {"transfer_to_identity", "transfer_to_scheduling", "transfer_to_coverage"},
    )
    assert bill and bill["name"] == "transfer_to_identity", bill
    assert bill["arguments"]["next_intent"] == "billing"
    assert infer_transfer_tool("hello", {"transfer_to_scheduling"}) is None

    print("harness self-check ok")


if __name__ == "__main__":
    demo()
