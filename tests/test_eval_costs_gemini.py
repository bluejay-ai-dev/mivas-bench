"""Gemini Live usage accounting in eval_costs: metrics-span usage and unattributed lanes."""
import importlib.util, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_costs", ROOT / "scripts" / "eval_costs.py")
ec = importlib.util.module_from_spec(spec); sys.modules["eval_costs"] = ec; spec.loader.exec_module(ec)

RATES = {"inputText": 0.75, "inputAudio": 3.0, "cachedText": None, "cachedAudio": None, "outputText": 4.5, "outputAudio": 12.0}


def _usage(it, ia, ot, oa, itot=None, otot=None):
    return {"gen_ai.usage.input_text_tokens": it, "gen_ai.usage.input_audio_tokens": ia,
            "gen_ai.usage.output_text_tokens": ot, "gen_ai.usage.output_audio_tokens": oa,
            "gen_ai.usage.input_tokens": itot if itot is not None else it + ia,
            "gen_ai.usage.output_tokens": otot if otot is not None else ot + oa,
            "gen_ai.request.model": "gemini-3.8-live"}


def test_metrics_span_usage_is_counted_once() -> None:
    spans = [
        {"name": "agent_turn", "attributes": {}},
        {"name": "realtime_inference", "attributes": _usage(1000, 100, 0, 50)},
        {"name": "realtime_inference", "attributes": {}},  # usage arrived late ...
        {"name": "realtime_metrics", "attributes": _usage(1200, 120, 0, 60)},  # ... on the child metrics span
    ]
    gens = ec.generations_from_spans(spans, "gemini-3.8-live")
    assert [g["gen_ai.usage.input_text_tokens"] for g in gens] == [1000, 1200]


def test_unattributed_tokens_are_priced_at_the_text_rate() -> None:
    lanes_only = ec.token_cost(_usage(1000, 100, 0, 50), RATES)
    with_leftover = ec.token_cost(_usage(1000, 100, 0, 50, itot=1110, otot=60), RATES)
    assert round(with_leftover - lanes_only, 9) == round((10 * 0.75 + 10 * 4.5) / 1_000_000, 9)
