"""HTTP that lives on the industry tool server but is not SQLite.

/snapshot — freeze/load final JSON next to the per-call DB files
"""

from __future__ import annotations

from pathlib import Path

import snapshot


def mount(app, calls_dir: Path) -> None:
    snapshot.mount(app, calls_dir)
