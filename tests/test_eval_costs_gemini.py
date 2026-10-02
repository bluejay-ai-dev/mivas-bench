"""eval_costs: usage packets, list-price backfill of packet-less turns, per-product legs."""
import importlib.util, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_costs", ROOT / "scripts" / "eval_costs.py")
ec = importlib.util.module_from_spec(spec); sys.modules["eval_costs"] = ec; spec.loader.exec_module(ec)

RATES = {"inputText": 0.75, "inputAudio": 3.0, "cachedText": None, "cachedAudio": None, "outputText": 4.5, "outputAudio": 12.0}
PRICING = ec.load_pricing()
T0 = "2026-09-26T08:00:00.000000000Z"


def _at(seconds: float) -> str:
    return f"2026-09-26T08:00:{seconds:09.6f}000Z"


def _usage(it, ia, ot, oa, itot=None, otot=None, model="gemini-3.8-live"):
    return {"gen_ai.usage.input_text_tokens": it, "gen_ai.usage.input_audio_tokens": ia,
            "gen_ai.usage.output_text_tokens": ot, "gen_ai.usage.output_audio_tokens": oa,
            "gen_ai.usage.input_tokens": itot if itot is not None else it + ia,
            "gen_ai.usage.output_tokens": otot if otot is not None else ot + oa,
            "gen_ai.request.model": model}


def _span(name, span_id, parent="", start=0.0, seconds=1.0, **attrs):
    return {"name": name, "span_id": span_id, "parent_span_id": parent, "timestamp": _at(start),
            "duration_nano": int(seconds * 1e9), "attributes": attrs}


def _gemini_trace(*, last_turn_priced: bool):
    """three agent_turns on the livekit 1.8 layout; the farewell turn may lack its packet."""
    spans = [
        _span("agent_turn", "t1", start=0, seconds=5, **{"lk.pii.response.text": "Hello, how can I help?", "lk.pii.response.function_calls": "[]"}),
        _span("realtime_inference", "i1", "t1", start=0, **_usage(4000, 100, 0, 120)),
        _span("agent_speaking", "s1", "t1", start=1, seconds=4.0),
        _span("user_speaking", "u1", start=6, seconds=3.0),
        _span("agent_turn", "t2", start=10, seconds=4, **{"lk.pii.response.text": "", "lk.pii.response.function_calls": '[{"name": "check_plan", "arguments": "{\\"carrier\\": \\"Aetna\\"}"}]'}),
        _span("realtime_inference", "i2", "t2", start=10),
        _span("realtime_metrics", "m2", "i2", start=11, **_usage(4200, 96, 20, 0)),  # usage landed late, on the metrics child
        _span("function_tool", "f2", "t2", start=12, **{"lk.pii.function_tool.output": '{"ok": true, "accepted": true}'}),
        _span("user_speaking", "u2", start=15, seconds=6.0),
        _span("agent_turn", "t3", start=22, seconds=4, **{"lk.pii.response.text": "Goodbye!", "lk.pii.response.function_calls": "[]"}),
        _span("realtime_inference", "i3", "t3", start=22, **(_usage(4300, 192, 0, 60) if last_turn_priced else {"gen_ai.request.model": "gemini-3.8-live"})),
        _span("agent_speaking", "s3", "t3", start=23, seconds=3.0),
    ]
    return spans


def test_metrics_span_usage_is_counted_once() -> None:
    gens = ec.generations_from_spans(_gemini_trace(last_turn_priced=True), "gemini-3.8-live", PRICING)
    assert [g["gen_ai.usage.input_text_tokens"] for g in gens] == [4000, 4200, 4300]
    assert not any(g["_backfilled"] for g in gens)


def test_packetless_turn_is_priced_from_measured_audio_and_context() -> None:
    gens = ec.generations_from_spans(_gemini_trace(last_turn_priced=False), "gemini-3.8-live", PRICING)
    assert len(gens) == 3
    farewell = gens[-1]
    assert farewell["_backfilled"]
    # context = previous packet's text lane + what the tool turn added (call json + result), 4 chars/token
    added = len('{"name": "check_plan", "arguments": "{\\"carrier\\": \\"Aetna\\"}"}') + len('{"ok": true, "accepted": true}')
    assert farewell["gen_ai.usage.input_text_tokens"] == 4200 + round(added / 4)
    # 6 s of new caller audio at $0.005/min and 3 s of reply audio at $0.018/min, Google's per-minute prices
    assert abs(farewell["_extra_usd"] - (6 / 60 * 0.005 + 3 / 60 * 0.018)) < 1e-9
    rates, _ = ec.rates_for(PRICING, "gemini-3.8-live")
    assert abs(farewell["_cost"] - (farewell["gen_ai.usage.input_text_tokens"] * rates["inputText"] / 1e6 + farewell["_extra_usd"])) < 1e-9


def test_gemini_31_tool_turn_is_covered_by_the_next_packet() -> None:
    # 3.1 aggregates the tool invocation into the next server-turn packet: no double billing
    spans = [
        _span("agent_turn", "t1", start=0, seconds=3, **_usage(4000, 100, 0, 120, model="gemini-3.1-flash-live-preview"), **{"lk.pii.response.text": "Hi"}),
        _span("agent_turn", "t2", start=5, seconds=2, **{"lk.pii.response.function_calls": '[{"name": "lookup"}]'}),
        _span("agent_turn", "t3", start=8, seconds=3, **_usage(8500, 200, 0, 100, model="gemini-3.1-flash-live-preview"), **{"lk.pii.response.text": "Found it"}),
    ]
    gens = ec.generations_from_spans(spans, "gemini-3.1-flash-live-preview", PRICING)
    assert [g["gen_ai.usage.input_text_tokens"] for g in gens] == [4000, 8500]


def test_unattributed_tokens_are_priced_at_the_text_rate() -> None:
    lanes_only = ec.token_cost(_usage(1000, 100, 0, 50), RATES)
    with_leftover = ec.token_cost(_usage(1000, 100, 0, 50, itot=1110, otot=60), RATES)
    assert round(with_leftover - lanes_only, 9) == round((10 * 0.75 + 10 * 4.5) / 1_000_000, 9)


def test_qwen_totals_are_split_into_caller_audio_and_text() -> None:
    turns = [
        {"role": "agent", "text": "Hello", "t": 0.0, "tEnd": 2.0},
        {"role": "caller", "text": "Hi there", "t": 3.0, "tEnd": 7.0},
        {"role": "agent", "text": "Sure", "t": 8.0, "tEnd": 9.0},
    ]
    spans = [
        _span("model", "m1", start=0, **{"gen_ai.usage.input_tokens": 4000, "gen_ai.usage.output_tokens": 25, "mivas.transcript": "Hello"}),
        _span("model", "m2", start=8, **{"gen_ai.usage.input_tokens": 4100, "gen_ai.usage.output_tokens": 12, "mivas.transcript": "Sure"}),
    ]
    gens = ec.sdk_generations(spans, "qwen-audio-3.0-realtime-plus", PRICING, turns)
    # 4 s of caller audio heard before the second reply = 50 tokens at Alibaba's 12.5 tok/s
    assert gens[0]["gen_ai.usage.input_audio_tokens"] == 0
    assert gens[1]["gen_ai.usage.input_audio_tokens"] == 50
    assert gens[1]["gen_ai.usage.input_text_tokens"] == 4050
    assert gens[1]["gen_ai.usage.output_audio_tokens"] == 12


def test_grok_bills_minutes_plus_one_text_input_per_tool_result() -> None:
    spans = [_span("execute_tool lookup", "e1"), _span("execute_tool transfer", "e2")]
    out = ec.cost_conversation({"result_id": "1", "duration_s": "120"}, "grok/voice", PRICING, spans=spans)
    assert out["llm_cost_source"] == "per_minute+text_inputs"
    assert abs(float(out["llm_cost_usd"]) - (2 * 0.08 + 2 * 0.004)) < 1e-9


def test_gpt_live_without_delegation_bills_only_the_voice_seconds() -> None:
    spans = [_span("voice.call", "v1", **{"mivas.live.usage_seconds": 90, "gen_ai.usage.input_tokens": 0}),
             _span("agent.speech", "a1", "v1", **{"mivas.transcript": "Hello"})]
    row = {"result_id": "1", "duration_s": "100", "transcript": "AGENT: Hello\nCALLER: Bye"}
    out = ec.cost_conversation(row, "openai/gpt-live-1@sol-low", PRICING, spans=spans)
    assert out["llm_cost_source"] == "per_minute"
    assert abs(float(out["llm_cost_usd"]) - 90 / 60 * 0.05) < 1e-9


def test_cascaded_prices_root_tokens_plus_stt_and_tts_legs() -> None:
    spans = [_span("voice.call", "v1", **{
        "gen_ai.usage.input_tokens": 30000, "gen_ai.usage.input_tokens_text": 30000, "gen_ai.usage.cached_tokens": 20000,
        "gen_ai.usage.output_tokens": 200, "gen_ai.usage.output_tokens_text": 200,
        "mivas.stt.audio_duration_s": 60, "mivas.tts.characters": 1000})]
    out = ec.cost_conversation({"result_id": "1", "duration_s": "60"}, "livekit/cascaded", PRICING, spans=spans)
    assert out["llm_cost_source"] == "tokens+stt_tts"
    expected = (10000 * 2.0 + 20000 * 0.5 + 200 * 8.0) / 1e6 + 0.0077 + 0.04
    assert abs(float(out["llm_cost_usd"]) - expected) < 1e-9


def test_reconstruction_uses_openai_published_audio_rates_when_no_usage_exists() -> None:
    turns = [
        {"role": "agent", "text": "Hello there", "t": 0.0, "tEnd": 2.0},
        {"role": "caller", "text": "Hi", "t": 3.0, "tEnd": 4.0},
        {"role": "agent", "text": "Bye", "t": 5.0, "tEnd": 6.0},
    ]
    rates, _ = ec.rates_for(PRICING, "gpt-realtime-2.1")
    gens = ec.reconstructed_generations({"industry": ""}, turns, "gpt-realtime-2.1", rates, ec.audio_rates_for(PRICING, "gpt-realtime-2.1"), family="realtime")
    assert gens[0]["gen_ai.usage.output_audio_tokens"] == 40   # 2 s at 20 tok/s
    assert gens[1]["gen_ai.usage.input_audio_tokens"] == 50    # 40 replayed reply tokens + 1 s caller audio at 10 tok/s
    assert gens[1]["gen_ai.usage.cached_tokens"] == gens[0]["gen_ai.usage.input_tokens"] + gens[0]["gen_ai.usage.output_tokens"]


def test_cost_columns_include_turn_counts_and_detail() -> None:
    out = ec.cost_conversation({"result_id": "1", "duration_s": "30"}, "gemini/3.8-live", PRICING, spans=_gemini_trace(last_turn_priced=False))
    assert out["llm_cost_source"] == "tokens+backfill"
    assert out["llm_cost_turns"] == "3" and out["llm_cost_backfilled_turns"] == "1"
    assert json.loads(out["llm_cost_detail"])["backfilled_turns"] == 1
