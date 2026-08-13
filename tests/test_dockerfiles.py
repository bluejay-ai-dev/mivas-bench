"""CodeBuild + Dockerfile cache contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_dockerfiles_install_deps_before_source() -> None:
    files = sorted((ROOT / "voice-agent-harnesses").glob("*/*/Dockerfile"))
    assert len(files) == 21
    for path in files:
        text = path.read_text()
        assert text.startswith("# syntax=docker/dockerfile:1"), path
        assert "--mount=type=cache,target=/root/.cache/uv" in text, path
        assert "uv pip install --system --no-cache" not in text, path
        req = text.index("/tmp/harness-requirements.txt")
        src = text.index("COPY runtime/")
        assert req < src, path


def test_batch_id_is_codebuild_safe() -> None:
    from codebuild.fleet import _batch_id

    ident = _batch_id("openai/realtime-2.1", "healthcare")
    assert ident[0].isalpha()
    assert "/" not in ident and "." not in ident
    assert ident == "openai_realtime_2_1_healthcare"


def test_batch_role_can_start_builds_on_any_resource() -> None:
    import inspect

    from codebuild import fleet

    src = inspect.getsource(fleet._ensure_roles)
    assert '"codebuild:StartBuild"' in src
    assert '"Resource": "*"' in src
