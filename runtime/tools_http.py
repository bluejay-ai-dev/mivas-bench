"""HTTP that lives on the industry tool server but is not SQLite.

/bind   — platform webhook id map (provider_bind)
/snapshot — freeze/load final JSON next to the per-call DB files
"""

from __future__ import annotations

from pathlib import Path

import provider_bind
import snapshot


def mount(app, calls_dir: Path) -> None:
    provider_bind.mount(app)
    snapshot.mount(app, calls_dir)
