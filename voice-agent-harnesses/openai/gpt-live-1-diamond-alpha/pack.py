"""Industry pack → GPT-Live v3 session shapes.

GPT-Live is two models behind one session (alpha guide, "How GPT-Live works"):

  * the **live** model speaks and decides when to delegate. Its prompt is
    ``session.instructions`` — immutable after ``session.start``, later guidance
    arrives via ``session.instructions.append`` (≤500 tokens each).
  * the **backend** Responses model owns tools and business rules. Its prompt and
    tools live under ``delegation.responses`` and can be swapped wholesale with
    ``session.update`` mid-call.

A MIVAS pack has one prompt per blueprint agent. Mapping:

  * backend instructions for stage S = S's system prompt verbatim, wrapped in
    OpenAI's recommended voice header/footer ("Backend prompt and setup").
    Tools = S's tools only (archive isolation), in Responses function format.
  * live instructions = the starting stage's prompt verbatim (or the pack's
    optional ``<stage>.live.md`` FEM prompt when it ships one) + OpenAI's
    prompting-guide delegation template, filled from the pack's own tool
    descriptions (no tool names). Nothing pack-specific is authored here.
  * a handoff = ``session.update`` to the target stage's backend + a short
    ``session.instructions.append`` role-change notice for the live model.

The harness adds exactly two things the pack cannot know: the wall clock (the
model has none) and the wire-level rule that the live model never calls tools.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

# --- OpenAI's own prompt scaffolding (alpha docs), verbatim -------------------

BACKEND_HEADER = """## Voice conversation context
You are helping an assistant in a live voice conversation. Transcripts
can contain mistakes, unfinished phrases, and later corrections. Use
the latest context and verified records. If a needed detail is still
unclear, ask for that detail instead of guessing.

## Task instructions
"""

BACKEND_FOOTER = """
## Return the result
Return a short, clear plain-text answer for the voice assistant.
Say what happened, whether the task is complete, and what comes next.
Use confirmed values. Do not invent a successful action.
Keep raw tool output, JSON, and Markdown out of the spoken summary."""

LIVE_TEMPLATE = """Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the user interrupts. Listen to what they say.

Delegation policy:
You do not call tools yourself; the backend does. Wherever the instructions above say to call or use a tool, delegate that request to the backend instead.
Backend tools:
{capabilities}

Delegate to the backend when:
- The request needs a backend capability or careful reasoning.
- A correction changes the work already requested.
- The instructions above say to call, use, or hand off to a tool.

Do not delegate to the backend when:
- You can answer from the conversation or a still-current result.
- You need a brief clarification to understand the request.

Delegate before giving an answer that depends on backend work.
Do not guess the result while waiting."""

HANDOFF_NOTICE = """Role change: the backend now handles the "{target}" stage of this call.
Backend tools:
{capabilities}
Continue the conversation from where it is. The caller already spoke with the previous stage; do not greet again or restart."""


def _first_sentence(text: str, limit: int = 160) -> str:
    head = text.strip().split("\n", 1)[0]
    for sep in (". ", "; "):
        if sep in head:
            head = head.split(sep, 1)[0] + "."
            break
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def today_line(day: date | None = None) -> str:
    d = day or date.today()
    return f"Today is {d:%A}, {d:%B} {d.day}, {d.year}."


def industry_path(name: str | Path) -> Path:
    path = Path(name)
    if path.is_dir():
        return path.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    return (REPO_ROOT / "industries" / name).resolve()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handoff_to: str | None = None
    session: bool = False

    @property
    def is_handoff(self) -> bool:
        return self.handoff_to is not None

    def responses_function(self) -> dict[str, Any]:
        params = dict(self.parameters or {"type": "object"})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": params,
        }


@dataclass(frozen=True)
class Stage:
    name: str
    prompt: str
    tools: tuple[Tool, ...]
    # Optional pack-authored live (FEM) prompt for this stage: system-prompts/<name>.live.md.
    # When present it replaces the verbatim stage prompt in the live instructions.
    live_prompt: str | None = None

    def tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    def capabilities(self) -> str:
        """One capability per tool, described in the pack's own words. No tool names:
        OpenAI's prompting guide and FEM/BEM skill keep names and schemas out of
        the live prompt; it only needs to know what the backend can do."""
        return "\n".join(f"- {_first_sentence(t.description)}" for t in self.tools) or "- (none)"

    def backend_instructions(self, clock: str) -> str:
        return f"{BACKEND_HEADER}{self.prompt.strip()}\n\n{clock}\n{BACKEND_FOOTER}"

    def responses_tools(self) -> list[dict[str, Any]]:
        return [t.responses_function() for t in self.tools]

    def handoff_notice(self) -> str:
        return HANDOFF_NOTICE.format(target=self.name, capabilities=self.capabilities())


@dataclass(frozen=True)
class Pack:
    industry: str
    start: str
    greeting: str
    stages: dict[str, Stage] = field(default_factory=dict)

    def live_instructions(self, clock: str) -> str:
        start = self.stages[self.start]
        template = LIVE_TEMPLATE.format(capabilities=start.capabilities())
        return f"{(start.live_prompt or start.prompt).strip()}\n\n{template}\n\n{clock}"

    def speak_first_prompt(self) -> str:
        text = (
            "The call has just connected and the caller has not spoken yet. "
            "Greet the caller now, then pause and listen."
        )
        if self.greeting:
            text += f"\n\nRequired welcome text: {self.greeting}"
        return text


def load_pack(industry: str | Path) -> Pack:
    industry_dir = industry_path(industry)
    blueprint = json.loads((industry_dir / "agent_blueprint.json").read_text())
    catalog = {
        t["name"]: t for t in json.loads((industry_dir / "tools.json").read_text())["tools"]
    }
    stages: dict[str, Stage] = {}
    for entry in blueprint["agents"]:
        tools: list[Tool] = []
        for ref in entry["tools"]:
            spec = catalog.get(ref["name"])
            if spec is None:
                raise KeyError(f"{industry_dir.name}: tool {ref['name']!r} missing from tools.json")
            tools.append(
                Tool(
                    name=spec["name"],
                    description=spec.get("description", spec["name"]),
                    parameters=spec.get("inputSchema") or {"type": "object"},
                    handoff_to=ref.get("handoff_to") if ref.get("handoff") else None,
                    session=bool(ref.get("session")),
                )
            )
        prompt_path = industry_dir / entry["system_prompt"]
        live_path = prompt_path.with_suffix(".live.md")
        stages[entry["name"]] = Stage(
            name=entry["name"],
            prompt=prompt_path.read_text(),
            tools=tuple(tools),
            live_prompt=live_path.read_text() if live_path.is_file() else None,
        )
    return Pack(
        industry=industry_dir.name,
        start=blueprint["agents"][0]["name"],
        greeting=str(blueprint.get("greeting") or "").strip(),
        stages=stages,
    )
