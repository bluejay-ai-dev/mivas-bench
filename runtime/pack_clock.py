"""Industry pack calendar for harness instruction injection.

Industry tool servers pin TODAY so slots, cancel windows, and deadlines stay
deterministic. Harnesses must tell the model that date. Using the wall clock
makes August pack slots look expired once the real calendar moves on.

Resolution order:
1. MIVAS_TODAY=wall — use date.today()
2. MIVAS_TODAY=YYYY-MM-DD — use that date
3. TODAY = "..." in the industry tool_server.py (date part only)
4. date.today() if the pack has no pin (control-industry)

Do not import industry tool_server modules — they start FastAPI.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

_TODAY_RE = re.compile(r'^TODAY\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _repo_roots() -> list[Path]:
    roots: list[Path] = []
    if Path("/app/industries").is_dir():
        roots.append(Path("/app"))
    here = Path(__file__).resolve().parents[1]
    if (here / "industries").is_dir():
        roots.append(here)
    return roots


def resolve_industry_dir(industry_dir: str | Path | None = None) -> Path | None:
    """Resolve a pack directory from a path, industry name, or env."""
    if industry_dir is not None:
        path = Path(industry_dir)
        if path.is_dir():
            return path.resolve()
        for root in _repo_roots():
            candidate = root / "industries" / str(industry_dir)
            if candidate.is_dir():
                return candidate.resolve()
    env_dir = os.environ.get("INDUSTRY_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    name = os.environ.get("INDUSTRY", "").strip()
    if name:
        for root in _repo_roots():
            candidate = root / "industries" / name
            if candidate.is_dir():
                return candidate.resolve()
    return None


def read_pack_today(industry_dir: str | Path | None) -> date | None:
    """Parse TODAY from tool_server.py. Healthcare pins a datetime; use the date."""
    resolved = resolve_industry_dir(industry_dir)
    if resolved is None:
        return None
    path = resolved / "tool_server.py"
    if not path.is_file():
        return None
    match = _TODAY_RE.search(path.read_text())
    if match is None:
        return None
    raw = match.group(1).split("T", 1)[0]
    return date.fromisoformat(raw)


def _wall_today() -> date:
    return date.today()


def pack_today(industry_dir: str | Path | None = None) -> date:
    """The date the model should treat as today for this pack."""
    override = os.environ.get("MIVAS_TODAY", "").strip()
    if override == "wall":
        return _wall_today()
    if override:
        return date.fromisoformat(override.split("T", 1)[0])
    pinned = read_pack_today(industry_dir)
    return pinned if pinned is not None else _wall_today()


def today_clock_line(
    industry_dir: str | Path | None = None,
    *,
    today: date | None = None,
) -> str:
    d = today if today is not None else pack_today(industry_dir)
    return f"Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}."


def with_pack_clock(
    instructions: str,
    industry_dir: str | Path | None = None,
    *,
    today: date | None = None,
) -> str:
    line = today_clock_line(industry_dir, today=today)
    text = (instructions or "").rstrip()
    if line in text:
        return text
    return f"{text}\n\n{line}"
