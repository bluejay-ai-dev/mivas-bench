"""Provider schema converters must not drop `items` off an array parameter.

Gemini Live rejects the entire session setup with
`properties[location_ids].items: missing field`, which surfaced as every
healthcare call returning NO_ANSWER with no tool ever fired. The other providers
accept the bare array and leave the model guessing what goes in it, which is the
same bug with a quieter failure mode.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ROOT / "voice-agent-harnesses"


def _load(provider: str):
    """Import voice-agent-harnesses/<provider>/harness.py under its own sys.path."""
    path = HARNESSES / provider / "harness.py"
    if not path.is_file():
        pytest.skip(f"{provider} harness not present")
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(f"{provider}_harness", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # provider SDK missing in this env
            pytest.skip(f"{provider} harness not importable: {exc}")
        return module
    finally:
        sys.path.remove(str(path.parent))


def _array_props() -> list[tuple[str, str, dict]]:
    out = []
    for tools_json in sorted((ROOT / "industries").glob("*/tools.json")):
        for tool in json.loads(tools_json.read_text())["tools"]:
            for name, prop in ((tool.get("inputSchema") or {}).get("properties") or {}).items():
                if isinstance(prop, dict) and prop.get("type") == "array":
                    out.append((tools_json.parent.name, f"{tool['name']}.{name}", prop))
    assert out, "no array parameters found — this test would prove nothing"
    return out


@pytest.mark.parametrize("provider", ["gemini", "vapi"])
def test_array_items_survive_conversion(provider: str) -> None:
    module = _load(provider)
    for industry, where, prop in _array_props():
        converted = module._prop(dict(prop))
        assert converted.get("type") == "array", (provider, industry, where)
        assert isinstance(converted.get("items"), dict), (provider, industry, where)
        assert converted["items"].get("type"), (provider, industry, where, converted)


@pytest.mark.parametrize("provider", ["gemini", "vapi"])
def test_bare_array_gets_an_item_type(provider: str) -> None:
    """A tools.json that forgets `items` still must not reach the provider bare."""
    module = _load(provider)
    converted = module._prop({"type": "array"})
    assert converted["items"]["type"] == "string", converted


def test_every_industry_array_declares_items() -> None:
    """The source schemas should be complete on their own, not repaired downstream."""
    for industry, where, prop in _array_props():
        assert isinstance(prop.get("items"), dict), f"{industry}: {where} has no items"
        assert prop["items"].get("type"), f"{industry}: {where} items has no type"
