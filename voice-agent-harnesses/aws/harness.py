"""Blueprint → Amazon Nova 2 Sonic (Bedrock bidirectional stream).

Industry tools map onto the industry state API (`TOOL_SERVER_URL`).
Handoff tools (`handoff: true`) open a new Sonic stream for the target agent
(promptStart tools are immutable). Session tools (`session: true`) hang up.

Nova requires an open USER audio content stream for the whole prompt. When
the caller is quiet the bridge feeds silent PCM so the stream does not idle
out — same constraint as hosted VoiceChat duplex, without a speech-shaped
kick. Speak-first is an interactive USER text block after that audio stream
is live (pack owns greeting text).

Industry-agnostic: pack owns prompts/tool policy. No harness greeting strings.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as _dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

for _root in (Path("/app"), *Path(__file__).resolve().parents):
    _runtime = _root / "runtime"
    if (_runtime / "call_id.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from call_id import headers as tool_headers, log_ws_accept, set_call_id  # noqa: E402

_BOOKING_CONFIRM_RE = re.compile(
    r"(?:booking\s+confirmed|appointment\s+(?:is\s+)?scheduled|"
    r"you(?:'| a)re\s+(?:all\s+)?set(?:\s+for)?|confirmed\s+for|"
    r"appointment\s+has\s+been\s+confirmed|"
    r"(?:i(?:'|'?ll| will| have)\s+)?(?:get\s+that\s+)?booked\s+for|"
    r"got\s+that\s+booked)",
    re.IGNORECASE,
)
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_DATE_MONTH_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:[,\s]+(\d{4}))?\b",
    re.IGNORECASE,
)
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_NEXT_WEEKDAY_RE = re.compile(
    r"\bnext\s+(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE
)

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1] if len(HARNESS_DIR.parents) > 1 else HARNESS_DIR

RUNTIME = "nova-sonic-2"
MODEL = os.environ.get("NOVA_SONIC_MODEL", "amazon.nova-2-sonic-v1:0")
REGION = os.environ.get("NOVA_SONIC_REGION", "us-east-1")
VOICE = os.environ.get("NOVA_SONIC_VOICE", "matthew")
INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
TOOL_SERVER_URL = os.environ.get("TOOL_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
END_CALL_CLOSE_DELAY_S = float(os.environ.get("MIVAS_END_CALL_CLOSE_DELAY_S", "2.5"))

# 512 frames @ 16 kHz ≈ 32 ms — AWS sample / eval-harness silent-audio cadence.
SILENCE_FRAMES = 512
SILENCE_BYTES = SILENCE_FRAMES * 2
SILENCE_CHUNK_S = float(os.environ.get("NOVA_SONIC_SILENCE_CHUNK_S", "0.03"))
SILENCE_PCM = b"\x00" * SILENCE_BYTES
# Interactive USER text that triggers speak-first. Silence alone yields usage
# events only; the model will not open. Pack still owns the greeting words.
SPEAK_FIRST_TEXT = os.environ.get("NOVA_SONIC_SPEAK_FIRST_TEXT", ".")


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
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
    }


def tool_server_url() -> str:
    return os.environ.get("TOOL_SERVER_URL", TOOL_SERVER_URL).rstrip("/")


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


def handoff_role(result: dict[str, Any], bp: dict[str, Any]) -> str | None:
    role = result.get("role")
    return role if isinstance(role, str) and role in bp["agents"] else None


def today_context_line(today: _dt.date | None = None) -> str:
    d = today or _dt.date.today()
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_today_context(instructions: str, today: _dt.date | None = None) -> str:
    line = today_context_line(today)
    text = (instructions or "").rstrip()
    if line in text:
        return text
    return f"{text}\n\n{line}"


def extract_appointment_date(text: str, *, default_year: int | None = None) -> str | None:
    if not text:
        return None
    year_default = int(default_year or _dt.date.today().year)
    hits: list[tuple[int, int, str]] = []

    for m in _DATE_NUMERIC_RE.finditer(text):
        mm, dd, yyyy = m.group(1).split("/")
        hits.append((50, m.end(), f"{int(mm):02d}/{int(dd):02d}/{int(yyyy)}"))
    for m in _DATE_MONTH_RE.finditer(text):
        month = _MONTHS.index(m.group(1).lower()) + 1
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else year_default
        hits.append((50, m.end(), f"{month:02d}/{day:02d}/{year}"))
    for m in _NEXT_WEEKDAY_RE.finditer(text):
        target = _WEEKDAYS.index(m.group(1).lower())
        today = _dt.date.today()
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        day = today + _dt.timedelta(days=delta)
        hits.append((45, m.end(), day.strftime("%m/%d/%Y")))

    if not hits:
        return None
    best = max(h[0] for h in hits)
    cands = [h for h in hits if h[0] == best]
    cands.sort(key=lambda x: x[1])
    return cands[-1][2]


def infer_schedule_appointment(text: str) -> dict[str, Any] | None:
    if not text or not _BOOKING_CONFIRM_RE.search(text):
        return None
    m = _BOOKING_CONFIRM_RE.search(text)
    assert m is not None
    window = text[max(0, m.start() - 40) : min(len(text), m.end() + 100)]
    date = extract_appointment_date(window) or extract_appointment_date(text)
    if not date:
        return None
    return {"date": date}


def _event_id() -> str:
    return str(uuid.uuid4())


def _tool_spec(spec: dict) -> dict[str, Any]:
    raw = dict(spec.get("inputSchema") or {"type": "object"})
    raw.pop("additionalProperties", None)
    props = raw.get("properties")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(props) if isinstance(props, dict) else {},
    }
    if raw.get("required"):
        schema["required"] = list(raw["required"])
    return {
        "toolSpec": {
            "name": spec["name"],
            "description": spec.get("description", spec["name"]),
            "inputSchema": {"json": json.dumps(schema)},
        }
    }


def tools_for_agent(bp: dict[str, Any], agent: str) -> list[dict[str, Any]]:
    return [_tool_spec(bp["catalog"][name]) for name in tool_names(bp, agent)]


def advertised_tools(industry_dir: str | Path, agent: str | None = None) -> list[str]:
    bp = load_blueprint(industry_dir)
    name = agent or bp["start"]
    return [t["toolSpec"]["name"] for t in tools_for_agent(bp, name)]


def build_agents(industry_dir: str | Path) -> tuple[str, list[str]]:
    bp = load_blueprint(industry_dir)
    return bp["start"], list(bp["agents"])


def session_start_event() -> dict[str, Any]:
    return {
        "event": {
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": 1024,
                    "topP": 0.9,
                    "temperature": 0.7,
                },
                "turnDetectionConfiguration": {"endpointingSensitivity": "HIGH"},
            }
        }
    }


def prompt_start_event(
    prompt_name: str,
    tools: list[dict[str, Any]],
    *,
    voice: str = VOICE,
    output_rate: int = OUTPUT_RATE,
) -> dict[str, Any]:
    prompt: dict[str, Any] = {
        "promptName": prompt_name,
        "textOutputConfiguration": {"mediaType": "text/plain"},
        "audioOutputConfiguration": {
            "mediaType": "audio/lpcm",
            "sampleRateHertz": output_rate,
            "sampleSizeBits": 16,
            "channelCount": 1,
            "voiceId": voice,
            "encoding": "base64",
            "audioType": "SPEECH",
        },
    }
    if tools:
        prompt["toolUseOutputConfiguration"] = {"mediaType": "application/json"}
        prompt["toolConfiguration"] = {"tools": tools}
    return {"event": {"promptStart": prompt}}


def content_start_text(
    prompt_name: str,
    content_name: str,
    role: str,
    *,
    interactive: bool,
) -> dict[str, Any]:
    return {
        "event": {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "type": "TEXT",
                "interactive": interactive,
                "role": role,
                "textInputConfiguration": {"mediaType": "text/plain"},
            }
        }
    }


def text_input_event(prompt_name: str, content_name: str, text: str) -> dict[str, Any]:
    return {
        "event": {
            "textInput": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": text,
            }
        }
    }


def content_start_audio(prompt_name: str, content_name: str) -> dict[str, Any]:
    return {
        "event": {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "type": "AUDIO",
                "interactive": True,
                "role": "USER",
                "audioInputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": INPUT_RATE,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "audioType": "SPEECH",
                    "encoding": "base64",
                },
            }
        }
    }


def audio_input_event(prompt_name: str, content_name: str, pcm: bytes) -> dict[str, Any]:
    return {
        "event": {
            "audioInput": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": base64.b64encode(pcm).decode("ascii"),
            }
        }
    }


def content_end_event(prompt_name: str, content_name: str) -> dict[str, Any]:
    return {"event": {"contentEnd": {"promptName": prompt_name, "contentName": content_name}}}


def content_start_tool(
    prompt_name: str, content_name: str, tool_use_id: str
) -> dict[str, Any]:
    return {
        "event": {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "interactive": False,
                "type": "TOOL",
                "role": "TOOL",
                "toolResultInputConfiguration": {
                    "toolUseId": tool_use_id,
                    "type": "TEXT",
                    "textInputConfiguration": {"mediaType": "text/plain"},
                },
            }
        }
    }


def tool_result_event(prompt_name: str, content_name: str, content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        content = json.dumps(content)
    return {
        "event": {
            "toolResult": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": content,
            }
        }
    }


def prompt_end_event(prompt_name: str) -> dict[str, Any]:
    return {"event": {"promptEnd": {"promptName": prompt_name}}}


def session_end_event() -> dict[str, Any]:
    return {"event": {"sessionEnd": {}}}


def handoff_seed_text(*, user_said: str = "", prior_agent_said: str = "") -> str:
    """Industry-agnostic mid-call notice for a cold Sonic stream.

    Lead with the caller's last line so the target treats it as the live
    request, then a short continue-don't-greet instruction.
    """
    user = " ".join((user_said or "").split()).strip()[:500]
    prior = " ".join((prior_agent_said or "").split()).strip()[:280]
    parts: list[str] = []
    if user:
        parts.append(user)
    else:
        parts.append("Please continue helping me with what I just asked.")
    if prior:
        parts.append(f"(The previous agent said: {prior})")
    parts.append(
        "SYSTEM: Mid-call handoff. Continue from here — do not greet, "
        "welcome, or re-ask information already given."
    )
    return "\n".join(parts)


def ensure_aws_credentials() -> None:
    """Copy boto3's default chain into env for the experimental Bedrock SDK."""
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return
    try:
        import boto3

        creds = boto3.Session().get_credentials()
        if creds is None:
            return
        frozen = creds.get_frozen_credentials()
        os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            os.environ["AWS_SESSION_TOKEN"] = frozen.token
    except Exception:
        pass


async def _execute_tool(
    name: str, args: dict[str, Any], bp: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    target = handoff_target(bp, state["agent"], name)
    if target:
        state["agent"] = target
        return {"success": True, "role": target}, False

    if name == "end_call":
        return {"success": True}, True

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{tool_server_url()}/tools/{name}",
            json={"arguments": args},
            headers=tool_headers(),
        )
        result = resp.json()
        if is_session_tool(bp, state["agent"], name):
            return result, True
        return result, False


async def handle_function_call(
    name: str,
    arguments: str | dict,
    call_id: str,
    bp: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    else:
        args = dict(arguments or {})
    return await run_tool(name, args, bp, state, call_id=call_id)


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
    with tool_span(name, args, call_id=call_id, parent=parent) as span:
        try:
            result, stop = await _execute_tool(name, args, bp, state)
            # tool servers answer {"ok": true}; only transfer_*/end_call use "success"
            ok = bool(result.get("ok") or result.get("success"))
        except Exception as e:  # noqa: BLE001 — dead tool must not kill the call
            result, stop, ok = (
                {"success": False, "error": f"{type(e).__name__}: {e}"},
                False,
                False,
            )
        finish_tool_span(span, result, ok=ok)
        return result, stop


def parse_tool_arguments(tool_use: dict[str, Any]) -> dict[str, Any]:
    raw = tool_use.get("content", tool_use.get("arguments", "{}"))
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _interrupted(text: str) -> bool:
    if not text:
        return False
    if '{ "interrupted" : true }' in text or '{"interrupted":true}' in text.replace(" ", ""):
        return True
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("interrupted") is True


class SonicSession:
    """One Bedrock bidirectional stream for one blueprint agent."""

    def __init__(
        self,
        *,
        agent: str,
        bp: dict[str, Any],
        model: str = MODEL,
        region: str = REGION,
        voice: str = VOICE,
        generation: int = 0,
    ) -> None:
        self.agent = agent
        self.bp = bp
        self.model = model
        self.region = region
        self.voice = voice
        self.generation = generation
        self.prompt_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.is_active = False
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._last_user_audio = 0.0
        self._stream = None
        self._client = None
        self._recv_task: asyncio.Task[None] | None = None
        self._keep_task: asyncio.Task[None] | None = None
        self._role: str | None = None
        self._content_type: str | None = None
        self._final_text = True
        self._assistant_text: list[str] = []

    def _stamp(self, ev: dict[str, Any]) -> dict[str, Any]:
        ev["generation"] = self.generation
        return ev

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._stream is None or not self.is_active:
            return
        from aws_sdk_bedrock_runtime.models import (
            BidirectionalInputPayloadPart,
            InvokeModelWithBidirectionalStreamInputChunk,
        )

        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=json.dumps(payload).encode("utf-8"))
        )
        async with self._send_lock:
            if self._stream is None or not self.is_active:
                return
            await self._stream.input_stream.send(chunk)

    async def send_pcm(self, pcm: bytes) -> None:
        if not pcm or not self.is_active:
            return
        self._last_user_audio = time.monotonic()
        await self._send(audio_input_event(self.prompt_name, self.audio_content_name, pcm))

    async def send_user_text(self, text: str, *, interactive: bool = True) -> None:
        cid = str(uuid.uuid4())
        await self._send(
            content_start_text(self.prompt_name, cid, "USER", interactive=interactive)
        )
        await self._send(text_input_event(self.prompt_name, cid, text))
        await self._send(content_end_event(self.prompt_name, cid))

    async def nudge_speak_first(self) -> None:
        await self.send_user_text(SPEAK_FIRST_TEXT, interactive=True)

    async def seed_handoff(self, *, user_said: str = "", prior_agent_said: str = "") -> None:
        await self.send_user_text(
            handoff_seed_text(user_said=user_said, prior_agent_said=prior_agent_said),
            interactive=True,
        )

    async def send_tool_result(self, tool_use_id: str, result: dict[str, Any]) -> None:
        cid = str(uuid.uuid4())
        await self._send(content_start_tool(self.prompt_name, cid, tool_use_id))
        await self._send(tool_result_event(self.prompt_name, cid, result))
        await self._send(content_end_event(self.prompt_name, cid))

    async def get_event(self, timeout: float = 0.25) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._events.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"type": "_timeout", "generation": self.generation}

    async def _keepalive(self) -> None:
        while self.is_active:
            await asyncio.sleep(SILENCE_CHUNK_S)
            if not self.is_active:
                return
            if time.monotonic() - self._last_user_audio < SILENCE_CHUNK_S:
                continue
            try:
                await self._send(
                    audio_input_event(
                        self.prompt_name, self.audio_content_name, SILENCE_PCM
                    )
                )
            except Exception:
                return

    async def _receive(self) -> None:
        assert self._stream is not None
        try:
            while self.is_active:
                try:
                    output = await self._stream.await_output()
                    result = await output[1].receive()
                except StopAsyncIteration:
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    msg = str(e).lower()
                    if "timed out" in msg or "timeout" in msg:
                        continue
                    print(f"nova recv {type(e).__name__}: {e}", flush=True)
                    if any(
                        s in msg
                        for s in ("closed", "broken pipe", "validationexception", "gone")
                    ):
                        await self._events.put(
                            self._stamp({"type": "error", "error": f"{type(e).__name__}: {e}"})
                        )
                        break
                    continue
                raw = getattr(getattr(result, "value", None), "bytes_", None)
                if not raw:
                    continue
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                event = data.get("event") or {}
                await self._dispatch(event)
        finally:
            self.is_active = False
            await self._events.put(None)

    async def _dispatch(self, event: dict[str, Any]) -> None:
        if "contentStart" in event:
            start = event["contentStart"]
            self._role = start.get("role")
            self._content_type = start.get("type")
            extra = start.get("additionalModelFields")
            self._final_text = True
            if extra:
                try:
                    fields = json.loads(extra) if isinstance(extra, str) else extra
                    self._final_text = fields.get("generationStage") != "SPECULATIVE"
                except json.JSONDecodeError:
                    pass
            if self._role == "ASSISTANT" and str(self._content_type or "").upper() != "AUDIO":
                self._assistant_text = []
            return

        if "textOutput" in event:
            block = event["textOutput"]
            text = str(block.get("content") or "")
            role = str(block.get("role") or self._role or "")
            if _interrupted(text):
                await self._events.put(self._stamp({"type": "interrupted"}))
                return
            if role == "ASSISTANT" and self._final_text and text:
                self._assistant_text.append(text)
            await self._events.put(
                self._stamp({"type": "text", "role": role, "content": text})
            )
            return

        if "audioOutput" in event:
            b64 = event["audioOutput"].get("content") or ""
            if not b64:
                return
            await self._events.put(
                self._stamp({"type": "audio", "pcm": base64.b64decode(b64)})
            )
            return

        if "toolUse" in event:
            tool = event["toolUse"]
            await self._events.put(
                self._stamp(
                    {
                        "type": "tool_use",
                        "name": tool.get("toolName") or "",
                        "id": tool.get("toolUseId") or _event_id(),
                        "arguments": parse_tool_arguments(tool),
                    }
                )
            )
            return

        if "contentEnd" in event:
            end = event["contentEnd"]
            ctype = str(end.get("type") or self._content_type or "").upper()
            role = str(end.get("role") or self._role or "").upper()
            if ctype == "AUDIO" and role == "ASSISTANT":
                transcript = "".join(self._assistant_text).strip()
                await self._events.put(
                    self._stamp({"type": "turn_end", "transcript": transcript})
                )
            return

        if "usageEvent" in event:
            await self._events.put(
                self._stamp({"type": "usage", "usage": event["usageEvent"]})
            )

    async def start(self, *, speak_first: bool = False) -> None:
        from aws_sdk_bedrock_runtime.client import (
            BedrockRuntimeClient,
            InvokeModelWithBidirectionalStreamOperationInput,
        )
        from aws_sdk_bedrock_runtime.config import Config
        from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

        ensure_aws_credentials()
        cfg: dict[str, Any] = {
            "endpoint_uri": f"https://bedrock-runtime.{self.region}.amazonaws.com",
            "region": self.region,
            "aws_credentials_identity_resolver": EnvironmentCredentialsResolver(),
        }
        key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
        if key and secret:
            cfg["aws_access_key_id"] = key
            cfg["aws_secret_access_key"] = secret
            token = os.environ.get("AWS_SESSION_TOKEN", "").strip()
            if token:
                cfg["aws_session_token"] = token
        self._client = BedrockRuntimeClient(config=Config(**cfg))
        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model)
        )
        self.is_active = True
        self._recv_task = asyncio.create_task(self._receive())

        tools = tools_for_agent(self.bp, self.agent)
        sys_id = str(uuid.uuid4())
        instructions = with_today_context(self.bp["agents"][self.agent]["instructions"])
        for payload in (
            session_start_event(),
            prompt_start_event(self.prompt_name, tools, voice=self.voice),
            content_start_text(self.prompt_name, sys_id, "SYSTEM", interactive=False),
            text_input_event(self.prompt_name, sys_id, instructions),
            content_end_event(self.prompt_name, sys_id),
            content_start_audio(self.prompt_name, self.audio_content_name),
        ):
            await self._send(payload)

        for _ in range(2):
            await self._send(
                audio_input_event(self.prompt_name, self.audio_content_name, SILENCE_PCM)
            )

        self._last_user_audio = time.monotonic()
        self._keep_task = asyncio.create_task(self._keepalive())
        if speak_first:
            await self.nudge_speak_first()

    async def close(self) -> None:
        if not self.is_active and self._stream is None:
            return
        keep = self._keep_task
        self._keep_task = None
        if keep is not None:
            keep.cancel()
            await asyncio.gather(keep, return_exceptions=True)
        try:
            await self._send(content_end_event(self.prompt_name, self.audio_content_name))
            await self._send(prompt_end_event(self.prompt_name))
            await self._send(session_end_event())
        except Exception:
            pass
        self.is_active = False
        recv = self._recv_task
        self._recv_task = None
        if recv is not None:
            recv.cancel()
            await asyncio.gather(recv, return_exceptions=True)
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await self._stream.input_stream.close()
            self._stream = None
        await self._events.put(None)


async def open_session(
    agent: str,
    bp: dict[str, Any],
    *,
    model: str = MODEL,
    generation: int = 0,
    speak_first: bool = False,
) -> SonicSession:
    session = SonicSession(
        agent=agent,
        bp=bp,
        model=model,
        region=REGION,
        voice=VOICE,
        generation=generation,
    )
    await session.start(speak_first=speak_first)
    return session


def demo() -> None:
    bp = load_blueprint("control-industry")
    start = bp["start"]
    start_tools = advertised_tools("control-industry", start)
    assert tool_names(bp, start) == start_tools
    all_names = {n for a in bp["agents"] for n in tool_names(bp, a)}
    today_line = today_context_line()
    for agent in bp["agents"]:
        names = tool_names(bp, agent)
        specs = tools_for_agent(bp, agent)
        pack = bp["agents"][agent]["instructions"]
        instructions = with_today_context(pack)
        assert instructions.startswith(pack.rstrip())
        assert today_line in instructions
        assert [t["toolSpec"]["name"] for t in specs] == names
        leaked = all_names - set(names)
        for n in leaked:
            assert n not in [t["toolSpec"]["name"] for t in specs], f"{agent} leaked {n}"
        prompt = prompt_start_event("p", specs)
        advertised = [
            t["toolSpec"]["name"]
            for t in prompt["event"]["promptStart"]["toolConfiguration"]["tools"]
        ]
        assert advertised == names
        assert "you MUST call" not in instructions.lower()

    audio = content_start_audio("p", "a")
    assert audio["event"]["contentStart"]["interactive"] is True
    assert audio["event"]["contentStart"]["audioInputConfiguration"]["sampleRateHertz"] == INPUT_RATE
    assert len(SILENCE_PCM) == SILENCE_BYTES

    nudge = content_start_text("p", "t", "USER", interactive=True)
    assert nudge["event"]["contentStart"]["interactive"] is True
    assert SPEAK_FIRST_TEXT

    nxt = extract_appointment_date("next Tuesday afternoon")
    assert nxt and nxt.endswith(f"/{_dt.date.today().year}")
    assert infer_schedule_appointment("Booking confirmed for March 18.") == {
        "date": f"03/18/{_dt.date.today().year}"
    }
    seed = handoff_seed_text(
        user_said="I'd like next Tuesday afternoon.",
        prior_agent_said="One moment while I transfer you.",
    )
    assert "Mid-call handoff" in seed
    assert "next Tuesday" in seed
    assert seed.index("next Tuesday") < seed.index("Mid-call handoff")
    if len(bp["agents"]) > 1 and all_names - set(start_tools):
        assert set(start_tools) != all_names
    print(
        f"aws self-check ok start={start} tools={start_tools} "
        f"agents={list(bp['agents'])} model={MODEL} region={REGION}"
    )


if __name__ == "__main__":
    demo()
