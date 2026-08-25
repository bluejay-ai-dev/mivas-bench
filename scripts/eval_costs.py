"""Conversation and utterance LLM costs from the S2S price table.

Used by bluejay_run_to_csv (live export).
Token models use gen_ai.usage.* on model / agent_turn spans. Grok is
$0.08 per audio minute. Missing usage falls back to 25 audio tokens/s.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICING_PATH = ROOT / "voice-agent-harnesses" / "s2s-model-pricing.json"
CACHE = ROOT / ".cache" / "eval_costs"
ENV_PATH = ROOT / ".env"

HARNESS_MODELS = {
    "openai-realtime-2.1": "gpt-realtime-2.1",
    "openai-realtime-2.1-mini": "gpt-realtime-2.1-mini",
    "grok-voice": "grok-voice-latest",
    "aws-nova-sonic-2": "amazon.nova-2-sonic-v1:0",
    "gemini-flash-live-3.1": "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio": "gemini-2.5-flash-native-audio",
    "qwen-audio-realtime": "qwen-audio-3.0-realtime-plus",
    "livekit-cascaded": "gpt-4.1",
}

MODEL_ALIASES = {
    "qwen-audio-3.0-realtime-plus": "qwen3-omni-flash-realtime",
    "grok-voice": "grok-voice-latest",
    "gemini-3.1-flash-live": "gemini-3.1-flash-live-preview",
}

GENERATION_NAMES = {"model", "agent_turn"}
AUDIO_TOKENS_PER_SEC = 25.0
TEXT_TOKENS_PER_SEC = 10.0
# Deepgram Flux list and ElevenLabs Flash v2.5 (~750 chars/min of speech at $0.075/1k).
FLUX_STT_PER_MINUTE = 0.0077
ELEVEN_FLASH_PER_SPOKEN_MINUTE = 0.056
COST_COLUMNS = (
    "llm_cost_usd",
    "llm_cost_source",
    "llm_cost_per_hour_usd",
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
    input_text = as_int(usage.get("gen_ai.usage.input_text_tokens"))
    input_audio = as_int(usage.get("gen_ai.usage.input_audio_tokens"))
    output_text = as_int(usage.get("gen_ai.usage.output_text_tokens"))
    output_audio = as_int(usage.get("gen_ai.usage.output_audio_tokens"))
    cached = as_int(usage.get("gen_ai.usage.cached_tokens"))
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


def load_spans(trace_id: str) -> list[dict]:
    cached = load_cache("traces", trace_id)
    if isinstance(cached, list):
        return cached
    payload = fetch_json(f"{api_base()}/traces/{trace_id}", method="POST")
    spans = span_rows(payload)
    slim = []
    for span in spans:
        attrs = span.get("attributes") or {}
        keep = {
            key: attrs[key]
            for key in attrs
            if key.startswith("gen_ai.usage")
            or key in {"gen_ai.request.model", "gen_ai.response.model", "mivas.transcript"}
        }
        slim.append({"name": span.get("name"), "attributes": keep})
    save_cache("traces", trace_id, slim)
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


def usage_present(attrs: dict) -> bool:
    return any(
        as_int(attrs.get(key))
        for key in (
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.input_text_tokens",
            "gen_ai.usage.input_audio_tokens",
            "gen_ai.usage.output_text_tokens",
            "gen_ai.usage.output_audio_tokens",
        )
    )


def deltalize(usages: list[dict]) -> list[dict]:
    keys = [
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.input_text_tokens",
        "gen_ai.usage.input_audio_tokens",
        "gen_ai.usage.output_text_tokens",
        "gen_ai.usage.output_audio_tokens",
        "gen_ai.usage.cached_tokens",
    ]
    if len(usages) < 2:
        return usages
    for key in keys:
        values = [as_int(item.get(key)) for item in usages]
        if any(values) and values == sorted(values):
            prev = 0
            for item, value in zip(usages, values):
                item[key] = max(0, value - prev)
                prev = value
    return usages


def generations_from_spans(spans: list[dict], default_model: str) -> list[dict]:
    picked = [span for span in spans if (span.get("name") or "") in GENERATION_NAMES]
    if not picked:
        picked = [span for span in spans if (span.get("name") or "") == "realtime_session"]
    usages = []
    for span in picked:
        attrs = dict(span.get("attributes") or {})
        if not usage_present(attrs) and (span.get("name") or "") != "model":
            continue
        attrs["_model"] = (
            attrs.get("gen_ai.request.model")
            or attrs.get("gen_ai.response.model")
            or default_model
        )
        attrs["_transcript"] = str(attrs.get("mivas.transcript") or "").strip()
        usages.append(attrs)
    names = {span.get("name") for span in picked}
    if names == {"agent_turn"}:
        usages = deltalize(usages)
    return [item for item in usages if usage_present(item) or item.get("_transcript")]


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


def load_turns(row: dict, transcript_lines: list[str] | None = None) -> list[dict]:
    if transcript_lines:
        return parse_plain_transcript("\n".join(transcript_lines))
    result_id = str(row.get("result_id") or "").strip()
    labs_cache = (
        Path("/Users/farazsiddiqi/Desktop/bluejay/repos/bluejay-labs/scripts/.cache/transcripts")
        / f"{result_id}.json"
    )
    if result_id and labs_cache.exists():
        try:
            turns = parse_timed_transcript(json.loads(labs_cache.read_text()))
            if turns:
                return turns
        except json.JSONDecodeError:
            pass
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
        turn["cost_usd"] = money(total * weight / weight_sum)


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


def speak_fraction(row: dict) -> float:
    speak = as_float(row.get("builtin_agent_speak_percentage"))
    if speak is not None and speak > 1:
        speak = speak / 100.0
    if speak is None:
        speak = 0.4
    return min(max(speak, 0.0), 1.0)


def estimate_native_audio(row: dict, rates: dict) -> float:
    duration = as_float(row.get("duration_s")) or 0.0
    speak = speak_fraction(row)
    return token_cost(
        {
            "gen_ai.usage.input_audio_tokens": duration * (1.0 - speak) * AUDIO_TOKENS_PER_SEC,
            "gen_ai.usage.output_audio_tokens": duration * speak * AUDIO_TOKENS_PER_SEC,
        },
        rates,
    )


def estimate_text_llm(row: dict, rates: dict) -> float:
    duration = as_float(row.get("duration_s")) or 0.0
    speak = speak_fraction(row)
    return token_cost(
        {
            "gen_ai.usage.input_text_tokens": duration * (1.0 - speak) * TEXT_TOKENS_PER_SEC,
            "gen_ai.usage.output_text_tokens": duration * speak * TEXT_TOKENS_PER_SEC,
        },
        rates,
    )


def cascaded_media_cost(row: dict) -> float:
    duration = as_float(row.get("duration_s")) or 0.0
    speak = speak_fraction(row)
    return duration / 60.0 * FLUX_STT_PER_MINUTE + duration * speak / 60.0 * ELEVEN_FLASH_PER_SPOKEN_MINUTE


def estimate_from_rates(row: dict, rates: dict, slug: str) -> float:
    if rates.get("inputAudio") is not None or rates.get("outputAudio") is not None:
        return estimate_native_audio(row, rates)
    total = estimate_text_llm(row, rates)
    if slug == "livekit-cascaded":
        total += cascaded_media_cost(row)
    return total


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
    slug = harness_slug(harness)
    pricing = pricing or load_pricing()
    default_model = HARNESS_MODELS.get(slug) or slug
    token_rates, per_min = rates_for(pricing, default_model)
    duration = as_float(row.get("duration_s")) or 0.0
    turns = load_turns(row, transcript_lines)
    if spans is None:
        spans = spans_for_result(str(row.get("result_id") or ""), trace_ids=trace_ids, fetch=fetch)

    source = "none"
    total = 0.0
    if per_min is not None:
        total = duration / 60.0 * float(per_min)
        source = "per_minute"
        allocate_by_duration(turns, total)
    else:
        priced = []
        for gen in generations_from_spans(spans, default_model):
            model = str(gen.get("_model") or default_model)
            rates, _ = rates_for(pricing, model)
            rates = rates or token_rates
            if not rates:
                continue
            cost = token_cost(gen, rates)
            if cost <= 0:
                continue
            gen["_cost"] = cost
            priced.append(gen)
            total += cost
        if priced:
            source = "tokens"
            attach_generation_costs(turns, priced, 0.0)
        elif token_rates:
            total = estimate_from_rates(row, token_rates, slug)
            source = "estimated"
            allocate_by_duration(turns, total)

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
        "llm_cost_usd": "" if source == "none" or money(total) is None else str(money(total)),
        "llm_cost_source": "" if source == "none" else source,
        "llm_cost_per_hour_usd": "" if hourly is None else str(money(hourly)),
        "utterance_costs_json": (
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) if payload else ""
        ),
    }
