"""Conversation and utterance LLM costs at provider list prices.

Used by bluejay_run_to_csv (live export) and annotate_eval_costs (re-costing
existing eval CSVs). Every rate comes from voice-agent-harnesses/s2s-model-pricing.json,
which records the provider page and the date each rate was checked.

How a conversation is priced, in order of preference:

1. tokens      every generation in the trace carries a provider usage packet
               (gen_ai.usage.* on realtime_inference / realtime_metrics / agent_turn for
               livekit, `model` for the SDK harnesses, `chat {model}` for gpt-live-1
               backends, the voice.call root for the cascaded pair).
2. +backfill   a generation with no packet (socket closed before turn_complete, etc.) is
               priced from what the trace does record about it: measured speech seconds
               at the provider's published audio conversion, transcript / tool-call text at
               the provider's chars-per-token, and the context the previous priced
               generation was billed for. Counted in llm_cost_backfilled_turns.
3. csv_tokens  the exporter's per-conversation token columns, when the trace is gone.
4. reconstructed  no usage anywhere: every generation is rebuilt from the timed transcript
               and the pack's system prompt with the same published conversions.

Per-minute products (grok, the gpt-live-1 voice leg) are billed on their metered minutes;
grok adds xAI's per-text-input line for each tool result.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICING_PATH = ROOT / "voice-agent-harnesses" / "s2s-model-pricing.json"
INDUSTRIES = ROOT / "industries"
CACHE = ROOT / ".cache" / "eval_costs"
ENV_PATH = ROOT / ".env"
LABS_TRANSCRIPTS = Path(
    "/Users/farazsiddiqi/Desktop/bluejay/repos/bluejay-labs/scripts/.cache/transcripts"
)

HARNESS_MODELS = {
    "openai-realtime-2.1": "gpt-realtime-2.1",
    "openai-realtime-2.1-mini": "gpt-realtime-2.1-mini",
    "grok-voice": "grok-voice-latest",
    "aws-nova-sonic-2": "amazon.nova-2-sonic-v1:0",
    "gemini-flash-live-3.1": "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio": "gemini-2.5-flash-native-audio",
    "qwen-audio-realtime": "qwen-audio-3.0-realtime-plus",
    "livekit-cascaded": "gpt-4.1",
    "openai-gpt-live-1@sol-low": "gpt-5.6-sol",
    "openai-gpt-live-1@astra-medium": "gpt-6-astra",
    "gemini-3.8-live": "gemini-3.8-live",
    "gemini-3.8-live@extended": "gemini-3.8-live-extended-thinking",
}

MODEL_ALIASES = {
    # qwen-audio-3.0-realtime-plus has its own verified row since 2026-08-22; the old
    # proxy alias onto qwen3-omni-flash-realtime is gone.
    "grok-voice": "grok-voice-latest",
    "gemini-3.1-flash-live": "gemini-3.1-flash-live-preview",
}

# Gemini 3.1 Flash Live sends ONE usage packet per server turn that aggregates every model
# invocation in that turn (tool call + reply), while livekit opens an agent_turn span per
# invocation. A packet-less agent_turn followed by a priced one in the same agent stage is
# therefore already billed; only the stage's last turn is lost. Verified on the wire (see
# _note_gemini_undercount in the pricing table) and in the traces: the packet after a
# packet-less tool turn carries ~2x the prompt (+4,300 input_text tokens on healthcare).
# 2.5 native audio and 3.8 do NOT aggregate (+800 / +1,800 tokens after a packet-less tool
# turn, which is the tool exchange and the thinking that entered the context, not a second
# prompt), so their packet-less turns are priced individually.
AGGREGATED_USAGE_MODELS = {"gemini-3.1-flash-live-preview"}

USAGE_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.input_text_tokens",
    "gen_ai.usage.input_audio_tokens",
    "gen_ai.usage.output_text_tokens",
    "gen_ai.usage.output_audio_tokens",
    "gen_ai.usage.cached_tokens",
)
SPEECH_CHARS_PER_SECOND = 15.0  # ~150 wpm conversational speech, used only when no timing exists
DEFAULT_TEXT_CHARS_PER_TOKEN = 4.0
COST_COLUMNS = (
    "llm_cost_usd",
    "llm_cost_source",
    "llm_cost_per_hour_usd",
    "llm_cost_turns",
    "llm_cost_backfilled_turns",
    "llm_cost_detail",
    "utterance_costs_json",
)
TURN_RE = re.compile(r"^([A-Z][A-Z0-9 .'-]{0,60}):\s*(.*)$")


def harness_slug(harness: str) -> str:
    text = (harness or "").strip()
    if not text:
        return "openai-realtime-2.1"
    return text.replace("/", "-")


def env_value(name: str) -> str:
    found = (os.environ.get(name) or "").strip()
    if found:
        return found
    if not ENV_PATH.exists():
        return ""
    prefix = f"{name}="
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def api_base() -> str:
    return (env_value("BLUEJAY_API_URL") or "https://api.getbluejay.ai/v1").rstrip("/")


def fetch_json(url: str, method: str = "GET") -> object | None:
    key = env_value("BLUEJAY_API_KEY")
    if not key:
        return None
    errors = (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
        ValueError,
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
    )
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=b"{}" if method == "POST" else None,
            method=method,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except errors:
            if attempt == 2:
                return None
            time.sleep(0.4 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# pricing table


def load_pricing() -> dict:
    return json.loads(PRICING_PATH.read_text())


def normalize_model_id(model: str) -> str:
    text = (model or "").strip().lower()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"@\d{8}$", "", text)
    text = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", text)
    text = re.sub(r"-preview-\d{2}-\d{4}", "-preview", text)
    text = text.replace("-native-audio-preview", "-native-audio")
    return MODEL_ALIASES.get(text, text)


def rates_for(pricing: dict, model: str) -> tuple[dict | None, float | None]:
    key = normalize_model_id(model)
    token = (pricing.get("token_pricing") or {}).get(key)
    per_min = (pricing.get("per_minute_pricing") or {}).get(key)
    return token, per_min


def audio_rates_for(pricing: dict, model: str) -> dict:
    """provider-published audio-seconds-to-token / per-minute conversions for one model."""
    return dict((pricing.get("audio_rates") or {}).get(normalize_model_id(model)) or {})


def text_input_rate_for(pricing: dict, model: str) -> float | None:
    value = (pricing.get("per_text_input_pricing") or {}).get(normalize_model_id(model))
    return None if value is None else float(value)


def component_rates(pricing: dict) -> tuple[float | None, float | None]:
    components = pricing.get("component_pricing") or {}
    stt = (components.get("stt") or {}).get("usd_per_minute")
    tts = (components.get("tts") or {}).get("usd_per_1k_characters")
    return (None if stt is None else float(stt), None if tts is None else float(tts))


def as_int(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def as_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, value), 6)


def token_cost(usage: dict, rates: dict) -> float:
    """USD for one usage packet at the given per-1M lane rates.

    Handles packets that only report totals (billed at the audio lane when the model has
    one), and tokens the provider bills but attributes to neither lane (priced at the text
    rate, the lower one). Cached tokens sit inside the input lanes and are re-priced at the
    cached rate rather than added.
    """
    input_text = as_int(usage.get("gen_ai.usage.input_text_tokens"))
    input_audio = as_int(usage.get("gen_ai.usage.input_audio_tokens"))
    output_text = as_int(usage.get("gen_ai.usage.output_text_tokens"))
    output_audio = as_int(usage.get("gen_ai.usage.output_audio_tokens"))
    cached = as_int(usage.get("gen_ai.usage.cached_tokens")) or as_int(
        usage.get("gen_ai.usage.input_cached_tokens")
    )
    input_total = as_int(usage.get("gen_ai.usage.input_tokens"))
    output_total = as_int(usage.get("gen_ai.usage.output_tokens"))

    if not any((input_text, input_audio, output_text, output_audio)):
        if rates.get("inputAudio") is not None or rates.get("outputAudio") is not None:
            input_audio = input_total
            output_audio = output_total
            input_text = 0
            output_text = 0
        else:
            input_text = input_total
            output_text = output_total

    if any((input_text, input_audio)) and input_total > input_text + input_audio:
        input_text += input_total - input_text - input_audio
    if any((output_text, output_audio)) and output_total > output_text + output_audio:
        output_text += output_total - output_text - output_audio

    cached_text = min(cached, input_text) if input_text else (cached if not input_audio else 0)
    cached_audio = min(max(0, cached - cached_text), input_audio)
    uncached_text = max(0, input_text - cached_text)
    uncached_audio = max(0, input_audio - cached_audio)

    total = 0.0
    for count, lane in (
        (uncached_text, "inputText"),
        (uncached_audio, "inputAudio"),
        (cached_text, "cachedText"),
        (cached_audio, "cachedAudio"),
        (output_text, "outputText"),
        (output_audio, "outputAudio"),
    ):
        rate = rates.get(lane)
        if not count or rate is None:
            continue
        total += count * float(rate) / 1_000_000.0
    return total


# ---------------------------------------------------------------------------
# trace cache


def cache_path(kind: str, key: str) -> Path:
    folder = CACHE / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}.json"


def load_cache(kind: str, key: str) -> object | None:
    path = cache_path(kind, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def save_cache(kind: str, key: str, data: object) -> None:
    cache_path(kind, key).write_text(json.dumps(data))


def result_trace_ids(result_id: str, hinted: list[str] | None = None) -> list[str]:
    if hinted:
        return [str(item) for item in hinted if item]
    cached = load_cache("results", result_id)
    if isinstance(cached, dict) and "trace_ids" in cached:
        return [str(item) for item in cached["trace_ids"] if item]
    payload = fetch_json(f"{api_base()}/retrieve-simulation-result/{result_id}")
    detail = payload.get("simulation_result") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        detail = payload if isinstance(payload, dict) else {}
    ids = [str(item) for item in (detail.get("trace_ids") or []) if item]
    save_cache("results", result_id, {"trace_ids": ids})
    return ids


def span_rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    inner = payload.get("data")
    if isinstance(inner, dict) and inner.get("type") == "raw":
        inner = inner.get("data")
    results = (inner or {}).get("results") if isinstance(inner, dict) else payload.get("results")
    rows = results[0].get("rows") if isinstance(results, list) and results else []
    spans = []
    for row in rows or []:
        span = row.get("data") if isinstance(row, dict) else None
        if isinstance(span, dict):
            spans.append(span)
    return spans


# attribute prefixes that carry no billing signal (room / job / participant plumbing)
DROP_ATTR_PREFIXES = (
    "lk.job",
    "lk.sip",
    "lk.room",
    "lk.pii.room",
    "lk.pii.participant",
    "lk.participant",
    "lk.dispatch",
    "lk.agent_name",
    "lk.agent_label",
    "lk.track_sid",
    "lk.callback",
    "lk.close",
    "lk.shutdown",
    "room_id",
    "job_id",
    "gen_ai.conversation.id",
    "gen_ai.tool.description",
    "gen_ai.agent.name",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.stream",
    "gen_ai.output.type",
    "langfuse.",
)
TRACE_CACHE_KIND = "traces_v2"


def slim_span(span: dict) -> dict:
    """keeps timing, parentage, and every billing-relevant attribute of one raw span."""
    attrs = span.get("attributes") or {}
    keep = {key: value for key, value in attrs.items() if not key.startswith(DROP_ATTR_PREFIXES)}
    return {
        "name": span.get("name"),
        "span_id": span.get("span_id") or "",
        "parent_span_id": span.get("parent_span_id") or "",
        "timestamp": span.get("timestamp") or "",
        "duration_nano": as_int(span.get("duration_nano")),
        "attributes": keep,
    }


def trace_cached(trace_id: str) -> bool:
    return cache_path(TRACE_CACHE_KIND, trace_id).exists()


def load_spans(trace_id: str) -> list[dict]:
    """timed spans of one trace, fetched once and cached under .cache/eval_costs/traces_v2.

    The v1 cache (traces/) kept only names and usage attributes, which cannot price a
    turn that never received a usage packet; v2 keeps span timing and parent links so
    agent_speaking / user_speaking durations can be attributed to their agent_turn.
    """
    cached = load_cache(TRACE_CACHE_KIND, trace_id)
    if isinstance(cached, list):
        return cached
    payload = fetch_json(f"{api_base()}/traces/{trace_id}", method="POST")
    slim = [slim_span(span) for span in span_rows(payload)]
    if payload is not None:
        save_cache(TRACE_CACHE_KIND, trace_id, slim)
    return slim


def spans_for_result(
    result_id: str,
    *,
    trace_ids: list[str] | None = None,
    fetch: bool = True,
) -> list[dict]:
    if not fetch or not result_id:
        return []
    spans: list[dict] = []
    for trace_id in result_trace_ids(result_id, trace_ids):
        spans.extend(load_spans(trace_id))
    return spans


# ---------------------------------------------------------------------------
# span helpers


def usage_present(attrs: dict) -> bool:
    return any(as_int(attrs.get(key)) for key in USAGE_KEYS if key != "gen_ai.usage.cached_tokens")


def span_name(span: dict) -> str:
    return str(span.get("name") or "")


def span_attrs(span: dict) -> dict:
    return span.get("attributes") or {}


def span_start(span: dict) -> float | None:
    """epoch seconds of a span's start, from the ISO-8601 timestamp the trace API returns."""
    text = str(span.get("timestamp") or "").strip()
    if not text:
        return None
    text = text.rstrip("Z")
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6].ljust(6, '0')}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def span_seconds(span: dict) -> float:
    return max(0.0, as_int(span.get("duration_nano")) / 1e9)


def span_end(span: dict) -> float | None:
    start = span_start(span)
    return None if start is None else start + span_seconds(span)


def sorted_by_start(spans: list[dict]) -> list[dict]:
    return sorted(spans, key=lambda span: span_start(span) or 0.0)


def children_index(spans: list[dict]) -> dict[str, list[dict]]:
    children: dict[str, list[dict]] = {}
    for span in spans:
        children.setdefault(str(span.get("parent_span_id") or ""), []).append(span)
    return children


def descendants(span: dict, children: dict[str, list[dict]], names: set[str]) -> list[dict]:
    span_id = str(span.get("span_id") or "")
    if not span_id:
        return []
    found: list[dict] = []
    seen = {span_id}
    stack = list(children.get(span_id, []))
    while stack:
        child = stack.pop()
        child_id = str(child.get("span_id") or "")
        if child_id in seen:
            continue
        if span_name(child) in names:
            found.append(child)
        if child_id:
            seen.add(child_id)
            stack.extend(children.get(child_id, []))
    return found


def usage_of(attrs: dict) -> dict:
    return {key: as_int(attrs.get(key)) for key in USAGE_KEYS}


def text_tokens(chars: int, chars_per_token: float) -> int:
    return int(round(chars / chars_per_token)) if chars > 0 else 0


def new_generation(model: str, transcript: str = "") -> dict:
    return {
        "_model": model,
        "_transcript": transcript.strip(),
        "_backfilled": False,
        "_extra_usd": 0.0,
        "_start": None,
        "_end": None,
    }


# ---------------------------------------------------------------------------
# livekit layout (Gemini families)


def livekit_turns(spans: list[dict]) -> list[dict]:
    """one record per agent_turn: usage packet (if any), speech seconds, text, timing."""
    children = children_index(spans)
    turns = sorted_by_start([span for span in spans if span_name(span) == "agent_turn"])
    user_speech = sorted_by_start([span for span in spans if span_name(span) == "user_speaking"])
    stage_exits = sorted(
        start for start in (span_start(span) for span in spans if span_name(span) == "on_exit")
        if start is not None
    )
    records: list[dict] = []
    for index, turn in enumerate(turns):
        attrs = span_attrs(turn)
        usage_attrs = None
        if usage_present(attrs):
            usage_attrs = attrs
        else:
            for candidate in descendants(turn, children, {"realtime_inference", "realtime_metrics"}):
                if usage_present(span_attrs(candidate)):
                    usage_attrs = span_attrs(candidate)
                    break
        start = span_start(turn)
        next_start = span_start(turns[index + 1]) if index + 1 < len(turns) else None
        prev_start = span_start(turns[index - 1]) if index else None
        speaking = descendants(turn, children, {"agent_speaking"})
        if not speaking and start is not None:
            speaking = [
                span for span in spans
                if span_name(span) == "agent_speaking"
                and (span_start(span) or 0.0) >= start
                and (next_start is None or (span_start(span) or 0.0) < next_start)
            ]
        user_seconds = 0.0
        for speech in user_speech:
            at = span_start(speech)
            if at is None or start is None:
                continue
            lower = prev_start if prev_start is not None else float("-inf")
            if lower <= at < start:
                user_seconds += span_seconds(speech)
        model = ""
        for candidate in descendants(turn, children, {"realtime_inference", "realtime_metrics"}):
            model = str(span_attrs(candidate).get("gen_ai.request.model") or "")
            if model:
                break
        tool_output_chars = sum(
            len(str(span_attrs(tool).get("lk.pii.function_tool.output") or ""))
            for tool in descendants(turn, children, {"function_tool"})
        )
        records.append(
            {
                "span": turn,
                "start": start,
                "end": span_end(turn),
                "usage": usage_attrs,
                "model": model,
                "speak_s": sum(span_seconds(span) for span in speaking),
                "user_s": user_seconds,
                "text": str(attrs.get("lk.pii.response.text") or "").strip(),
                "tool_chars": len(str(attrs.get("lk.pii.response.function_calls") or "").strip("[] ")),
                "tool_output_chars": tool_output_chars,
                "stage_ends_after": any(
                    start is not None and exit_at >= start and (next_start is None or exit_at < next_start)
                    for exit_at in stage_exits
                ),
                "last": index == len(turns) - 1,
            }
        )
    return records


def turn_context_chars(record: dict) -> int:
    """characters a turn adds to the model context: its reply text, tool calls and results."""
    return len(record["text"]) + record["tool_chars"] + record["tool_output_chars"]


def input_text_of(record: dict) -> int:
    usage = usage_of(record["usage"])
    if usage["gen_ai.usage.input_text_tokens"]:
        return usage["gen_ai.usage.input_text_tokens"]
    return max(0, usage["gen_ai.usage.input_tokens"] - usage["gen_ai.usage.input_audio_tokens"])


def backfill_livekit_turn(
    record: dict,
    reference: dict | None,
    previous: dict | None,
    rates: dict,
    audio_rates: dict,
) -> dict:
    """prices one packet-less livekit generation from what the trace measured about it.

    Output audio: agent_speaking seconds at the provider's per-minute audio price (or its
    tokens-per-second at the output rate). Output text: the tool-call JSON the turn
    produced, at chars-per-token (thinking tokens of a packet-less turn are not
    recoverable from the trace and are not guessed). Input: the text context the
    neighbouring priced turn was billed for (Gemini re-sends the whole context every turn)
    plus what the previous turn added to it, and the new caller audio seconds at the
    published input rate.
    """
    chars_per_token = float(audio_rates.get("textCharsPerToken") or DEFAULT_TEXT_CHARS_PER_TOKEN)
    gen = new_generation(record["model"], record["text"])
    gen["_backfilled"] = True
    gen["_start"], gen["_end"] = record["start"], record["end"]

    input_text = input_text_of(reference) if reference else 0
    if previous is not None:
        input_text += text_tokens(turn_context_chars(previous), chars_per_token)
    gen["gen_ai.usage.input_text_tokens"] = input_text

    input_audio_tokens = 0
    if record["user_s"] > 0:
        if audio_rates.get("inputUsdPerMinute") is not None:
            gen["_extra_usd"] += record["user_s"] / 60.0 * float(audio_rates["inputUsdPerMinute"])
        elif audio_rates.get("inputTokensPerSecond"):
            input_audio_tokens = int(round(record["user_s"] * float(audio_rates["inputTokensPerSecond"])))
    gen["gen_ai.usage.input_audio_tokens"] = input_audio_tokens

    output_audio_tokens = 0
    if record["speak_s"] > 0:
        if audio_rates.get("outputUsdPerMinute") is not None:
            gen["_extra_usd"] += record["speak_s"] / 60.0 * float(audio_rates["outputUsdPerMinute"])
        elif audio_rates.get("outputTokensPerSecond"):
            output_audio_tokens = int(round(record["speak_s"] * float(audio_rates["outputTokensPerSecond"])))
    gen["gen_ai.usage.output_audio_tokens"] = output_audio_tokens
    gen["gen_ai.usage.output_text_tokens"] = text_tokens(record["tool_chars"], chars_per_token)
    gen["gen_ai.usage.input_tokens"] = input_text + input_audio_tokens
    gen["gen_ai.usage.output_tokens"] = output_audio_tokens + gen["gen_ai.usage.output_text_tokens"]
    gen["_cost"] = token_cost(gen, rates) + gen["_extra_usd"]
    return gen


def livekit_generations(spans: list[dict], default_model: str, pricing: dict) -> list[dict]:
    """priced generations for a livekit (Gemini) trace, backfilling packet-less turns."""
    records = livekit_turns(spans)
    if not records:
        return []
    model_key = normalize_model_id(default_model)
    aggregated = model_key in AGGREGATED_USAGE_MODELS
    generations: list[dict] = []
    for index, record in enumerate(records):
        model = record["model"] or default_model
        rates, _ = rates_for(pricing, model)
        rates = rates or rates_for(pricing, default_model)[0]
        if not rates:
            continue
        if record["usage"] is not None:
            gen = new_generation(model, record["text"])
            gen.update(usage_of(record["usage"]))
            gen["_start"], gen["_end"] = record["start"], record["end"]
            gen["_cost"] = token_cost(gen, rates)
            generations.append(gen)
            continue
        if aggregated and not record["last"] and not record["stage_ends_after"]:
            later_priced = any(item["usage"] is not None for item in records[index + 1:])
            if later_priced:
                continue  # billed inside the next packet of this stage
        priced_before = [item for item in records[:index] if item["usage"] is not None]
        priced_after = [item for item in records[index + 1:] if item["usage"] is not None]
        reference = priced_before[-1] if priced_before else (priced_after[0] if priced_after else None)
        if reference is None:
            continue  # no packet anywhere: cost_conversation falls back to reconstruction
        # the previous turn's additions to the context only matter when the reference
        # packet precedes this turn; a later packet already contains them
        previous = records[index - 1] if index and priced_before else None
        generations.append(
            backfill_livekit_turn(record, reference, previous, rates, audio_rates_for(pricing, model))
        )
    return generations


# ---------------------------------------------------------------------------
# SDK layout (`model` spans: OpenAI Realtime, Nova, Qwen, Grok tracers)


def sdk_generations(spans: list[dict], default_model: str, pricing: dict, turns: list[dict]) -> list[dict]:
    """priced generations from `model` spans.

    Nova streams usage as many small packets (not one per response) so its spans are
    summed as-is. Qwen reports only totals: the input is split into the caller audio
    accumulated so far (12.5 tokens/s, re-sent every turn per Alibaba's billing rules)
    and text for the rest, output is audio.
    """
    model_spans = sorted_by_start([span for span in spans if span_name(span) == "model"])
    if not model_spans:
        return []
    model_key = normalize_model_id(default_model)
    audio_rates = audio_rates_for(pricing, default_model)
    input_tps = float(audio_rates.get("inputTokensPerSecond") or 0.0)
    caller_turns = [turn for turn in turns if turn.get("role") == "caller"]
    agent_turns = [turn for turn in turns if turn.get("role") == "agent"]
    generations: list[dict] = []
    response_index = 0
    for span in model_spans:
        attrs = span_attrs(span)
        if not usage_present(attrs):
            continue
        model = str(attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model") or default_model)
        rates, _ = rates_for(pricing, model)
        rates = rates or rates_for(pricing, default_model)[0]
        if not rates:
            continue
        gen = new_generation(model, str(attrs.get("mivas.transcript") or ""))
        gen.update(usage_of(attrs))
        gen["_start"], gen["_end"] = span_start(span), span_end(span)
        lanes_missing = not any(
            gen[key] for key in (
                "gen_ai.usage.input_text_tokens",
                "gen_ai.usage.input_audio_tokens",
                "gen_ai.usage.output_text_tokens",
                "gen_ai.usage.output_audio_tokens",
            )
        )
        if lanes_missing and input_tps and model_key.startswith("qwen"):
            # caller audio accumulated before this response (matched by response order)
            agent_at = agent_turns[response_index]["t"] if response_index < len(agent_turns) and agent_turns[response_index].get("t") is not None else None
            heard = sum(
                duration_of(turn) for turn in caller_turns
                if agent_at is None or (turn.get("t") is not None and turn["t"] < agent_at)
            )
            audio = min(gen["gen_ai.usage.input_tokens"], int(round(max(heard, 0.0) * input_tps)))
            gen["gen_ai.usage.input_audio_tokens"] = audio
            gen["gen_ai.usage.input_text_tokens"] = gen["gen_ai.usage.input_tokens"] - audio
            gen["gen_ai.usage.output_audio_tokens"] = gen["gen_ai.usage.output_tokens"]
        response_index += 1
        gen["_cost"] = token_cost(gen, rates)
        generations.append(gen)
    return generations


# ---------------------------------------------------------------------------
# gpt-live-1 (voice.call root + `chat {model}` backend spans) and cascaded (voice.call root)


def chat_generations(spans: list[dict], default_model: str, pricing: dict) -> list[dict]:
    generations: list[dict] = []
    for span in sorted_by_start([span for span in spans if span_name(span).startswith("chat ")]):
        attrs = span_attrs(span)
        if not usage_present(attrs):
            continue
        model = str(attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model") or default_model)
        rates, _ = rates_for(pricing, model)
        rates = rates or rates_for(pricing, default_model)[0]
        if not rates:
            continue
        gen = new_generation(model)
        gen.update(usage_of(attrs))
        gen["_start"], gen["_end"] = span_start(span), span_end(span)
        gen["_cost"] = token_cost(gen, rates)
        generations.append(gen)
    return generations


def root_span(spans: list[dict], name: str) -> dict | None:
    for span in spans:
        if span_name(span) == name:
            return span
    return None


def cascaded_llm_generation(spans: list[dict], default_model: str, pricing: dict) -> dict | None:
    """the gpt-4.1 leg of the cascaded pair, aggregated by the harness onto voice.call."""
    root = root_span(spans, "voice.call")
    if root is None or not usage_present(span_attrs(root)):
        return None
    attrs = span_attrs(root)
    model = str(attrs.get("gen_ai.request.model") or default_model)
    rates, _ = rates_for(pricing, model)
    rates = rates or rates_for(pricing, default_model)[0]
    if not rates:
        return None
    gen = new_generation(model)
    gen["gen_ai.usage.input_tokens"] = as_int(attrs.get("gen_ai.usage.input_tokens"))
    gen["gen_ai.usage.input_text_tokens"] = as_int(
        attrs.get("gen_ai.usage.input_tokens_text") or attrs.get("gen_ai.usage.input_tokens")
    )
    gen["gen_ai.usage.output_tokens"] = as_int(attrs.get("gen_ai.usage.output_tokens"))
    gen["gen_ai.usage.output_text_tokens"] = as_int(
        attrs.get("gen_ai.usage.output_tokens_text") or attrs.get("gen_ai.usage.output_tokens")
    )
    gen["gen_ai.usage.cached_tokens"] = as_int(attrs.get("gen_ai.usage.cached_tokens"))
    gen["_cost"] = token_cost(gen, rates)
    return gen


def cascaded_component_costs(row: dict, spans: list[dict], turns: list[dict], pricing: dict) -> dict[str, float]:
    """Deepgram Flux minutes and ElevenLabs characters, from the voice.call attrs, else the
    exporter's columns, else the call duration and the agent transcript."""
    stt_rate, tts_rate = component_rates(pricing)
    root = root_span(spans, "voice.call")
    attrs = span_attrs(root) if root else {}
    stt_seconds = as_float(attrs.get("mivas.stt.audio_duration_s"))
    if stt_seconds is None:
        stt_seconds = as_float(row.get("stt_audio_duration_s"))
    if stt_seconds is None:
        stt_seconds = as_float(row.get("duration_s")) or 0.0
    tts_chars = as_float(attrs.get("mivas.tts.characters"))
    if tts_chars is None:
        tts_chars = as_float(row.get("tts_characters"))
    if tts_chars is None:
        tts_chars = float(sum(len(turn.get("text") or "") for turn in turns if turn.get("role") == "agent"))
    out: dict[str, float] = {}
    if stt_rate is not None:
        out["stt_usd"] = stt_seconds / 60.0 * stt_rate
    if tts_rate is not None:
        out["tts_usd"] = tts_chars / 1000.0 * tts_rate
    return out


# ---------------------------------------------------------------------------
# fallbacks when the trace carries no usage at all


def csv_token_generation(row: dict, model: str, rates: dict) -> dict | None:
    """one generation from the exporter's per-conversation token columns."""
    gen = new_generation(model)
    gen["gen_ai.usage.input_text_tokens"] = as_int(row.get("input_text_tokens"))
    gen["gen_ai.usage.input_audio_tokens"] = as_int(row.get("input_audio_tokens"))
    gen["gen_ai.usage.output_text_tokens"] = as_int(row.get("output_text_tokens"))
    gen["gen_ai.usage.output_audio_tokens"] = as_int(row.get("output_audio_tokens"))
    gen["gen_ai.usage.cached_tokens"] = as_int(row.get("cached_tokens"))
    gen["gen_ai.usage.input_tokens"] = gen["gen_ai.usage.input_text_tokens"] + gen["gen_ai.usage.input_audio_tokens"]
    gen["gen_ai.usage.output_tokens"] = gen["gen_ai.usage.output_text_tokens"] + gen["gen_ai.usage.output_audio_tokens"]
    if not usage_present(gen):
        return None
    gen["_cost"] = token_cost(gen, rates)
    return gen


def prompt_chars(industry: str) -> int:
    """characters of the pack's entry-agent system prompt plus its tool schemas."""
    folder = INDUSTRIES / (industry or "")
    blueprint = folder / "agent_blueprint.json"
    if not industry or not blueprint.exists():
        return 0
    try:
        data = json.loads(blueprint.read_text())
    except json.JSONDecodeError:
        return 0
    agents = data.get("agents") or []
    entry = agents[0] if agents else {}
    total = len(str(data.get("greeting") or ""))
    prompt_file = folder / str(entry.get("system_prompt") or "")
    if prompt_file.is_file():
        total += len(prompt_file.read_text())
    tools = entry.get("tools") or []
    schema_file = folder / "tools.json"
    if schema_file.is_file():
        try:
            schemas = json.loads(schema_file.read_text())
        except json.JSONDecodeError:
            schemas = []
        if isinstance(schemas, dict):
            schemas = schemas.get("tools") or []
        wanted = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
        for schema in schemas if isinstance(schemas, list) else []:
            if not wanted or str((schema or {}).get("name")) in wanted:
                total += len(json.dumps(schema))
    else:
        total += len(json.dumps(tools))
    return total


def agent_seconds(turn: dict) -> float:
    measured = duration_of(turn)
    if measured > 0:
        return measured
    return len(turn.get("text") or "") / SPEECH_CHARS_PER_SECOND


def reconstructed_generations(
    row: dict,
    turns: list[dict],
    model: str,
    rates: dict,
    audio_rates: dict,
    *,
    family: str,
) -> list[dict]:
    """rebuilds every generation of a conversation from the timed transcript.

    family "realtime": OpenAI-style context — caller audio (10 tok/s) and prior replies
    (20 tok/s audio + transcript text) are re-sent every turn, everything before the
    newest caller message at the cached rate.
    family "qwen": Alibaba's rules — caller audio re-sent every turn at 12.5 tok/s,
    replies billed once as audio, instructions and reply text as text, no cache lane.
    family "gemini": text context (prompt + reply transcripts) re-sent every turn, new
    caller audio and reply audio at Google's per-minute audio prices.
    family "text": a text LLM (gpt-live-1 backend, cascaded gpt-4.1) — prompt plus the
    whole transcript so far, prior context at the cached rate.
    """
    chars_per_token = float(audio_rates.get("textCharsPerToken") or DEFAULT_TEXT_CHARS_PER_TOKEN)
    input_tps = float(audio_rates.get("inputTokensPerSecond") or 0.0)
    output_tps = float(audio_rates.get("outputTokensPerSecond") or 0.0)
    prompt_tokens = text_tokens(prompt_chars(str(row.get("industry") or "")), chars_per_token)
    generations: list[dict] = []
    context_text = prompt_tokens
    context_audio = 0
    context_audio_seconds = 0.0
    previous_total = 0
    pending_caller_seconds = 0.0
    pending_caller_chars = 0
    for turn in turns:
        if turn.get("role") != "agent":
            pending_caller_seconds += agent_seconds(turn)
            pending_caller_chars += len(turn.get("text") or "")
            continue
        gen = new_generation(model, str(turn.get("text") or ""))
        gen["_backfilled"] = True
        gen["_start"], gen["_end"] = turn.get("t"), turn.get("tEnd")
        speak = agent_seconds(turn)
        reply_chars = len(turn.get("text") or "")
        if family == "text":
            input_text = context_text + text_tokens(pending_caller_chars, chars_per_token)
            gen["gen_ai.usage.input_text_tokens"] = input_text
            gen["gen_ai.usage.cached_tokens"] = min(previous_total, input_text)
            gen["gen_ai.usage.output_text_tokens"] = text_tokens(reply_chars, chars_per_token)
            context_text = input_text + gen["gen_ai.usage.output_text_tokens"]
            previous_total = context_text
        elif family == "gemini":
            gen["gen_ai.usage.input_text_tokens"] = context_text
            if audio_rates.get("inputUsdPerMinute") is not None:
                gen["_extra_usd"] += pending_caller_seconds / 60.0 * float(audio_rates["inputUsdPerMinute"])
            else:
                gen["gen_ai.usage.input_audio_tokens"] = int(round(pending_caller_seconds * input_tps))
            if audio_rates.get("outputUsdPerMinute") is not None:
                gen["_extra_usd"] += speak / 60.0 * float(audio_rates["outputUsdPerMinute"])
            else:
                gen["gen_ai.usage.output_audio_tokens"] = int(round(speak * output_tps))
            context_text += text_tokens(reply_chars, chars_per_token)
        elif family == "qwen":
            context_audio_seconds += pending_caller_seconds
            gen["gen_ai.usage.input_text_tokens"] = context_text
            gen["gen_ai.usage.input_audio_tokens"] = int(round(context_audio_seconds * input_tps))
            gen["gen_ai.usage.output_audio_tokens"] = int(round(max(speak, 1.0) * output_tps))
            gen["gen_ai.usage.output_text_tokens"] = text_tokens(reply_chars, chars_per_token)
            context_text += gen["gen_ai.usage.output_text_tokens"]
        else:  # realtime
            new_audio = int(round(pending_caller_seconds * input_tps))
            input_text = context_text
            input_audio = context_audio + new_audio
            gen["gen_ai.usage.input_text_tokens"] = input_text
            gen["gen_ai.usage.input_audio_tokens"] = input_audio
            gen["gen_ai.usage.cached_tokens"] = min(previous_total, input_text + input_audio)
            gen["gen_ai.usage.output_audio_tokens"] = int(round(speak * output_tps))
            gen["gen_ai.usage.output_text_tokens"] = text_tokens(reply_chars, chars_per_token)
            previous_total = input_text + input_audio + gen["gen_ai.usage.output_audio_tokens"] + gen["gen_ai.usage.output_text_tokens"]
            context_text = input_text + gen["gen_ai.usage.output_text_tokens"]
            context_audio = input_audio + gen["gen_ai.usage.output_audio_tokens"]
        gen["gen_ai.usage.input_tokens"] = gen.get("gen_ai.usage.input_text_tokens", 0) + gen.get("gen_ai.usage.input_audio_tokens", 0)
        gen["gen_ai.usage.output_tokens"] = gen.get("gen_ai.usage.output_text_tokens", 0) + gen.get("gen_ai.usage.output_audio_tokens", 0)
        gen["_cost"] = token_cost(gen, rates) + gen["_extra_usd"]
        pending_caller_seconds = 0.0
        pending_caller_chars = 0
        generations.append(gen)
    return generations


def reconstruction_family(slug: str) -> str:
    if slug.startswith("gemini"):
        return "gemini"
    if slug.startswith("qwen"):
        return "qwen"
    if slug.startswith("openai-realtime"):
        return "realtime"
    return "text"


# ---------------------------------------------------------------------------
# transcripts


def parse_plain_transcript(value: object) -> list[dict]:
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    turns: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        matched = TURN_RE.match(line)
        if matched:
            speaker = matched.group(1).strip()
            body = matched.group(2).strip()
            is_agent = speaker.upper() == "AGENT"
            turns.append({"role": "agent" if is_agent else "caller", "text": body})
        elif turns:
            turns[-1]["text"] = f"{turns[-1]['text']} {line}".strip()
    return [turn for turn in turns if turn.get("text")]


def parse_timed_transcript(data: object) -> list[dict]:
    items = data if isinstance(data, list) else (
        (data or {}).get("transcript") or (data or {}).get("messages") or []
        if isinstance(data, dict)
        else []
    )
    turns: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("utterance") or item.get("content") or item.get("text") or "").strip()
        if not text:
            continue
        speaker = str(item.get("speaker") or item.get("role") or "").strip()
        is_agent = speaker.upper() in {"AGENT", "ASSISTANT"}
        turn = {"role": "agent" if is_agent else "caller", "text": text}
        try:
            start = item.get("start_offset_ms")
            end = item.get("end_offset_ms")
            if start not in (None, ""):
                turn["t"] = round(float(start) / 1000.0, 3)
            if end not in (None, ""):
                turn["tEnd"] = round(float(end) / 1000.0, 3)
        except (TypeError, ValueError):
            pass
        turns.append(turn)
    return turns


def fetch_timed_transcript(row: dict) -> object | None:
    """timed transcript JSON: the labs cache, then the run's transcript_url, cached locally."""
    result_id = str(row.get("result_id") or "").strip()
    if not result_id:
        return None
    labs = LABS_TRANSCRIPTS / f"{result_id}.json"
    if labs.exists():
        try:
            return json.loads(labs.read_text())
        except json.JSONDecodeError:
            pass
    cached = load_cache("transcripts", result_id)
    if cached is not None:
        return cached
    url = str(row.get("transcript_url") or "").strip()
    if not url.startswith("http"):
        return None
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, http.client.HTTPException):
        return None
    save_cache("transcripts", result_id, data)
    return data


def load_turns(row: dict, transcript_lines: list[str] | None = None) -> list[dict]:
    if transcript_lines:
        return parse_plain_transcript("\n".join(transcript_lines))
    data = fetch_timed_transcript(row)
    if data is not None:
        turns = parse_timed_transcript(data)
        if turns:
            return turns
    return parse_plain_transcript(row.get("transcript"))


def duration_of(turn: dict) -> float:
    start = turn.get("t")
    end = turn.get("tEnd")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
        return float(end) - float(start)
    return 0.0


def allocate_by_duration(turns: list[dict], total: float) -> None:
    agents = [turn for turn in turns if turn.get("role") == "agent"]
    weights = [duration_of(turn) or 1.0 for turn in agents]
    weight_sum = sum(weights) or 1.0
    for turn, weight in zip(agents, weights):
        turn["cost_usd"] = money((turn.get("cost_usd") or 0) + total * weight / weight_sum)


def attach_generation_costs(turns: list[dict], generations: list[dict], leftover: float) -> None:
    agents = [turn for turn in turns if turn.get("role") == "agent"]
    used: set[int] = set()
    for gen in generations:
        text = re.sub(r"\s+", " ", str(gen.get("_transcript") or "")).strip().lower()
        cost = float(gen.get("_cost") or 0)
        if not text or cost <= 0:
            leftover += cost
            continue
        match = None
        for index, turn in enumerate(agents):
            if index in used:
                continue
            body = re.sub(r"\s+", " ", str(turn.get("text") or "")).strip().lower()
            if text == body or text in body or body in text:
                match = index
                break
        if match is None:
            leftover += cost
            continue
        used.add(match)
        agents[match]["cost_usd"] = money((agents[match].get("cost_usd") or 0) + cost)
    if leftover > 0:
        unused = [turn for index, turn in enumerate(agents) if index not in used]
        targets = unused or agents
        if targets:
            share = leftover / len(targets)
            for turn in targets:
                turn["cost_usd"] = money((turn.get("cost_usd") or 0) + share)


# ---------------------------------------------------------------------------
# conversation


def generations_from_spans(spans: list[dict], default_model: str, pricing: dict | None = None, turns: list[dict] | None = None) -> list[dict]:
    """priced generations for any trace layout (dispatches on the span names present)."""
    pricing = pricing or load_pricing()
    names = {span_name(span).split(" ")[0] for span in spans}
    if "agent_turn" in names:
        return livekit_generations(spans, default_model, pricing)
    if "chat" in names:
        return chat_generations(spans, default_model, pricing)
    if "model" in names:
        return sdk_generations(spans, default_model, pricing, turns or [])
    if "voice.call" in names:
        gen = cascaded_llm_generation(spans, default_model, pricing)
        return [gen] if gen else []
    return []


def live_voice_seconds(row: dict, spans: list[dict]) -> float:
    """gpt-live-1 bills the API-reported session seconds; the harness stamps them on voice.call."""
    root = root_span(spans, "voice.call")
    reported = as_float(span_attrs(root).get("mivas.live.usage_seconds")) if root else None
    if reported is not None and reported > 0:
        return reported
    return as_float(row.get("duration_s")) or 0.0


def cost_conversation(
    row: dict,
    harness: str,
    pricing: dict | None = None,
    spans: list[dict] | None = None,
    *,
    fetch: bool = False,
    transcript_lines: list[str] | None = None,
    trace_ids: list[str] | None = None,
) -> dict[str, str]:
    """prices one conversation; returns the COST_COLUMNS as strings ready for the CSV.

    llm_cost_source is one of tokens | tokens+backfill | csv_tokens | reconstructed |
    per_minute (grok / gpt-live voice-only), joined with +per_minute, +text_inputs or
    +stt_tts for the extra legs. llm_cost_detail is a small JSON with the legs and the
    token lanes so the number can be audited without the trace.
    """
    slug = harness_slug(harness)
    pricing = pricing or load_pricing()
    default_model = HARNESS_MODELS.get(slug) or slug
    token_rates, per_min = rates_for(pricing, default_model)
    audio_rates = audio_rates_for(pricing, default_model)
    duration = as_float(row.get("duration_s")) or 0.0
    turns = load_turns(row, transcript_lines)
    if spans is None:
        spans = spans_for_result(str(row.get("result_id") or ""), trace_ids=trace_ids, fetch=fetch)

    sources: list[str] = []
    detail: dict[str, object] = {}
    total = 0.0
    generations: list[dict] = []

    if per_min is not None:
        voice = duration / 60.0 * float(per_min)
        total += voice
        detail["voice_usd"] = money(voice)
        sources.append("per_minute")
        allocate_by_duration(turns, voice)
        text_rate = text_input_rate_for(pricing, default_model)
        tool_calls = sum(1 for span in spans if span_name(span).startswith("execute_tool"))
        if text_rate is not None and tool_calls:
            extra = tool_calls * text_rate
            total += extra
            detail["text_inputs"] = tool_calls
            detail["text_inputs_usd"] = money(extra)
            sources.append("text_inputs")
            allocate_by_duration(turns, extra)
    elif token_rates:
        generations = generations_from_spans(spans, default_model, pricing, turns)
        generations = [gen for gen in generations if float(gen.get("_cost") or 0) > 0 or gen.get("_backfilled")]
        if generations:
            backfilled = sum(1 for gen in generations if gen.get("_backfilled"))
            sources.append("tokens+backfill" if backfilled else "tokens")
        elif slug.startswith("openai-gpt-live-1") and root_span(spans, "voice.call") is not None:
            # the trace is complete and has no `chat` span: the voice model never delegated
            # to the backend, so there are no backend tokens to price (voice.call reports 0)
            pass
        else:
            gen = csv_token_generation(row, default_model, token_rates)
            if gen is not None:
                generations = [gen]
                sources.append("csv_tokens")
            elif turns:
                generations = reconstructed_generations(
                    row, turns, default_model, token_rates, audio_rates, family=reconstruction_family(slug)
                )
                if generations:
                    sources.append("reconstructed")
        for gen in generations:
            total += float(gen.get("_cost") or 0)
        if generations:
            attach_generation_costs(turns, generations, 0.0)
            detail["turns"] = len(generations)
            detail["backfilled_turns"] = sum(1 for gen in generations if gen.get("_backfilled"))
            detail["backfilled_usd"] = money(sum(float(gen.get("_cost") or 0) for gen in generations if gen.get("_backfilled")))
            lanes = {}
            for key in USAGE_KEYS:
                lanes[key.rsplit(".", 1)[-1]] = sum(as_int(gen.get(key)) for gen in generations)
            detail["tokens"] = lanes

    if slug == "livekit-cascaded" and token_rates:
        legs = cascaded_component_costs(row, spans, turns, pricing)
        if legs:
            extra = sum(legs.values())
            total += extra
            detail.update({key: money(value) for key, value in legs.items()})
            sources.append("stt_tts")
            allocate_by_duration(turns, extra)

    if slug.startswith("openai-gpt-live-1"):
        live_rate = (pricing.get("per_minute_pricing") or {}).get("gpt-live-1")
        seconds = live_voice_seconds(row, spans)
        if live_rate is not None and seconds:
            voice = seconds / 60.0 * float(live_rate)
            total += voice
            detail["voice_seconds"] = seconds
            detail["voice_usd"] = money(voice)
            sources.append("per_minute")
            allocate_by_duration(turns, voice)

    source = "+".join(dict.fromkeys(sources))
    hourly = (total / duration * 3600.0) if duration and total else None
    payload = []
    for turn in turns:
        item = {
            "role": turn.get("role"),
            "text": turn.get("text"),
            "cost_usd": money(turn.get("cost_usd")) if turn.get("role") == "agent" else None,
        }
        if turn.get("t") is not None:
            item["t"] = turn["t"]
        if turn.get("tEnd") is not None:
            item["tEnd"] = turn["tEnd"]
        payload.append(item)
    return {
        "llm_cost_usd": "" if not source or money(total) is None else str(money(total)),
        "llm_cost_source": source,
        "llm_cost_per_hour_usd": "" if hourly is None else str(money(hourly)),
        "llm_cost_turns": str(len(generations)) if generations else "",
        "llm_cost_backfilled_turns": (
            str(sum(1 for gen in generations if gen.get("_backfilled"))) if generations else ""
        ),
        "llm_cost_detail": json.dumps(detail, separators=(",", ":")) if detail else "",
        "utterance_costs_json": (
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) if payload else ""
        ),
    }
