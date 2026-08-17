"""NVIDIA Nemotron hosted function-calling wire format.

Local NIM jinja injects <AVAILABLE_TOOLS>/<TOOLCALL> when session.tools is set.
Hosted NVCF (VoiceChat Realtime and integrate.api.nvidia.com) does not, so both
runtimes append this trained protocol from the same catalog decls.

Cascaded nano often writes a <TOOLCALL> block into the text stream instead of
OpenAI-style tool_calls. Magpie then speaks the XML / markdown. Parse the block
out of TTS and turn it into a real function call.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# Official Nemotron VoiceChat / Nemotron Nano v2 function-calling protocol.
FC_PROTOCOL = (
    "\n\nCall a tool ONLY when the user's request matches one of the tools listed "
    "in <AVAILABLE_TOOLS> below. For every other request, do not call any tool - "
    "just answer from your knowledge. Never invent or call a tool name that is not "
    "literally in <AVAILABLE_TOOLS>.\n"
    "If a tool has required parameters, emit the <TOOLCALL> with those parameters "
    "BEFORE you speak any confirmation to the caller. Do not claim a booking or "
    "handoff succeeded unless you have emitted the matching <TOOLCALL>.\n"
    "<AVAILABLE_TOOLS>{tools}</AVAILABLE_TOOLS>\n\n"
    "If you decide to call any tool(s), use the following format:\n"
    '<TOOLCALL>[{{"name": "tool_name1", "arguments": {{"param": "value"}}}}]</TOOLCALL>\n\n'
    "The user will execute tool-calls and return responses from tool(s) in this format:\n"
    "<TOOL_RESPONSE>[{{tool_response1}}]</TOOL_RESPONSE>\n"
)
TOOLCALL_RE = re.compile(r"<TOOLCALL>(.*?)</TOOLCALL>", re.DOTALL | re.IGNORECASE)
_OPEN_RE = re.compile(r"<TOOLCALL\b", re.IGNORECASE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_STAGE_DIR_RE = re.compile(
    r"\((?:looks up|copying|pause|holds|retriev|checking)[^)]*\)",
    re.IGNORECASE,
)
_PARTIAL_OPEN = "<toolcall>"


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def available_tools_json(tools: list[dict[str, Any]]) -> str:
    slim = [
        {
            "name": t["name"],
            "description": t.get("description", t["name"]),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }
        for t in tools
    ]
    return _ascii(json.dumps(slim, separators=(",", ":")))


def append_fc_protocol(instructions: str, tools: list[dict[str, Any]]) -> str:
    """Append the NVIDIA FC protocol for this node's tools. Pack text is unchanged."""
    if not tools:
        return instructions
    return instructions + FC_PROTOCOL.format(tools=available_tools_json(tools))


def parse_toolcalls(text: str) -> list[dict[str, Any]]:
    """Parse native <TOOLCALL>[...]</TOOLCALL> blocks from model text."""
    out: list[dict[str, Any]] = []
    if not text:
        return out
    for match in TOOLCALL_RE.finditer(text):
        raw = (match.group(1) or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            args = item.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"raw": args}
            out.append({"name": str(item["name"]), "arguments": args or {}})
    return out


# Magpie Triton: "multichar start character but not an end character"
# (Gloria 727614, 71s punctuation stall). Zero-width / curly punctuation.
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None
)


def speakable_text(text: str) -> str:
    """Strip tool XML, markdown, and Magpie-breaking unicode before TTS."""
    if not text:
        return ""
    text = TOOLCALL_RE.sub("", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _STAGE_DIR_RE.sub("", text)
    text = text.translate(_INVISIBLE)
    text = (
        text.replace("\u2014", ", ")
        .replace("\u2013", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    text = text.replace("*", " ")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def drain_toolcall_text(
    buf: str, *, flush: bool = False
) -> tuple[str, str, list[dict[str, Any]]]:
    """Split streamed LLM text into speakable prefix, held remainder, parsed calls.

    Holds an unclosed <TOOLCALL so Magpie never speaks the XML. On flush, try to
    parse a truncated block and drop leftover tags.
    """
    calls: list[dict[str, Any]] = []
    speech: list[str] = []
    i = 0
    for match in TOOLCALL_RE.finditer(buf):
        speech.append(buf[i : match.start()])
        calls.extend(parse_toolcalls(match.group(0)))
        i = match.end()
    rest = buf[i:]
    open_m = _OPEN_RE.search(rest)
    if open_m:
        speech.append(rest[: open_m.start()])
        held = rest[open_m.start() :]
        if flush:
            inner = re.sub(r"^<TOOLCALL\s*>?", "", held, flags=re.IGNORECASE)
            inner = re.sub(r"</TOOLCALL>\s*$", "", inner, flags=re.IGNORECASE).strip()
            if inner:
                calls.extend(parse_toolcalls(f"<TOOLCALL>{inner}</TOOLCALL>"))
            return "".join(speech), "", calls
        return "".join(speech), held, calls
    if not flush:
        lower = rest.lower()
        for n in range(1, len(_PARTIAL_OPEN) + 1):
            if lower.endswith(_PARTIAL_OPEN[:n]):
                return "".join(speech) + rest[:-n], rest[-n:], calls
    return "".join(speech) + rest, "", calls


def advertised_tool_names(functions: Any) -> set[str]:
    """Names Pipecat has registered on the LLM this turn (dict or list)."""
    names: set[str] = set()
    if isinstance(functions, dict):
        names.update(str(k) for k in functions if k)
        items = functions.values()
    else:
        items = functions or []
    for item in items:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
        else:
            n = getattr(item, "name", None)
            if isinstance(n, str) and n:
                names.add(n)
    return names


def last_user_text(messages: list[Any]) -> str:
    """Plain text of the latest user turn in an LLMContext message list."""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            return " ".join(p for p in parts if p).strip()
    return ""


# User-utterance hints → advertised handoff tools. Used only when nano speaks
# instead of emitting a function call (cancel 727533, copay 727501).
_TRANSFER_HINTS: list[tuple[str, tuple[str, ...]]] = [
    (
        "transfer_to_coverage",
        ("copay", "insurance", "eligibility", "member id", "aetna", "referral"),
    ),
    (
        "transfer_to_cosmetic",
        ("botox", "filler", "laser", "peel", "cosmetic"),
    ),
    (
        "transfer_to_billing",
        ("bill", "balance", "charge", "payment", "invoice"),
    ),
    (
        "transfer_to_clinical",
        ("refill", "results", "nurse", "prescription"),
    ),
    (
        "transfer_to_scheduling",
        (
            "cancel",
            "reschedule",
            "book",
            "appointment",
            "earliest",
            "slot",
            "follow-up",
            "follow up",
        ),
    ),
    (
        "handoff_to_scheduler",
        ("book", "schedule", "appointment", "slot"),
    ),
]


def infer_transfer_tool(
    user_text: str, advertised: set[str]
) -> dict[str, Any] | None:
    """If the caller clearly asked for a handoff the node owns, return that call.

    Existing-patient cancel/reschedule prefers transfer_to_identity when that
    tool is advertised (pack: identity first, next_intent=scheduling).
    """
    blob = (user_text or "").lower()
    if not blob or not advertised:
        return None
    if any(w in blob for w in ("cancel", "reschedule")):
        if "transfer_to_identity" in advertised:
            return {
                "name": "transfer_to_identity",
                "arguments": {"next_intent": "scheduling"},
            }
        if "transfer_to_scheduling" in advertised:
            return {"name": "transfer_to_scheduling", "arguments": {}}
    if any(
        w in blob
        for w in ("bill", "balance", "charge", "payment", "invoice", "owe")
    ):
        if "transfer_to_identity" in advertised:
            return {
                "name": "transfer_to_identity",
                "arguments": {"next_intent": "billing"},
            }
        if "transfer_to_billing" in advertised:
            return {"name": "transfer_to_billing", "arguments": {}}
    scored: list[tuple[int, str]] = []
    for name, needles in _TRANSFER_HINTS:
        if name not in advertised:
            continue
        hits = sum(1 for n in needles if n in blob)
        if hits:
            scored.append((hits, name))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    name = scored[0][1]
    args: dict[str, Any] = {}
    if name == "transfer_to_identity":
        args["next_intent"] = "scheduling"
    return {"name": name, "arguments": args}
