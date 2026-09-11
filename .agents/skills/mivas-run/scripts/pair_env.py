#!/usr/bin/env python3
"""Print the environment a MIVAS pair's container needs, in the shape your platform wants.

The image already bakes HARNESS, INDUSTRY and the ports. This prints what you must supply
at run time: the provider key for that harness, the Bluejay key, CHIRP auth, the snapshot
store, and the slug the snapshot key is built from.

    uv run python .agents/skills/mivas-run/scripts/pair_env.py --harness openai/realtime-2.1 --industry healthcare
    ... --format railway    # one `railway variables --set` command
    ... --format docker     # a `docker run` line
    ... --redact            # same list with values masked, safe to paste into chat

Values come from the repo-root .env. Missing required ones are reported, not invented.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from preflight import PROVIDER_KEYS, load_dotenv  # noqa: E402  (one source of truth per family)

# Always sent: Bluejay ingest + CHIRP auth. CHIRP_USER/PASS must match the Bluejay agent.
ALWAYS = ("BLUEJAY_API_KEY", "CHIRP_USER", "CHIRP_PASS")
# Sent when set locally; snapshot storage and Bluejay endpoint overrides.
OPTIONAL = (
    "MIVAS_SNAPSHOT_BUCKET", "MIVAS_SNAPSHOT_PREFIX", "AWS_ENDPOINT_URL_S3",
    "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "BLUEJAY_API_URL", "BLUEJAY_OTLP_ENDPOINT",
)
DEFAULTS = {"CHIRP_USER": "mivas", "CHIRP_PASS": "mivas"}


def slug(harness: str, industry: str) -> str:
    return f"{harness.replace('/', '-')}-{industry}".replace("_", "-").replace(".", "-").lower()


def pair_env(harness: str, industry: str) -> tuple[dict[str, str], list[str]]:
    family = harness.split("/", 1)[0]
    env: dict[str, str] = {
        # the image bakes these per tag; re-sending them makes a mismatched tag fail loudly
        "HARNESS": harness,
        "INDUSTRY": industry,
        # snapshot keys are s3://bucket/prefix/<slug>/<result id>.final.json
        "MIVAS_SLUG": slug(harness, industry),
    }
    missing: list[str] = []
    for alternatives in PROVIDER_KEYS.get(family, []):
        chosen = next((k for k in alternatives if os.environ.get(k, "").strip()), None)
        if chosen:
            env[chosen] = os.environ[chosen].strip()
        else:
            missing.append(" | ".join(alternatives))
    if family not in PROVIDER_KEYS:
        missing.append(f"(unknown family {family}: add its keys by hand)")
    for key in ALWAYS:
        value = os.environ.get(key, "").strip() or DEFAULTS.get(key, "")
        if value:
            env[key] = value
        else:
            missing.append(key)
    for key in OPTIONAL:
        value = os.environ.get(key, "").strip()
        if value:
            env[key] = value
    return env, missing


def render(env: dict[str, str], fmt: str, redact: bool) -> str:
    shown = {k: ("***" if redact else v) for k, v in env.items()}
    if fmt == "railway":
        sets = " ".join(f"--set {shlex.quote(f'{k}={v}')}" for k, v in shown.items())
        return f"railway variables {sets}"
    if fmt == "docker":
        flags = " ".join(f"-e {shlex.quote(f'{k}={v}')}" for k, v in shown.items())
        return f"docker run --rm -p 8765:8765 {flags} mivas-bench:{env['MIVAS_SLUG']}"
    return "\n".join(f"{k}={v}" for k, v in shown.items())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--harness", default=os.environ.get("HARNESS", "openai/realtime-2.1"))
    p.add_argument("--industry", default=os.environ.get("INDUSTRY", "control-industry"))
    p.add_argument("--format", choices=("env", "railway", "docker"), default="env")
    p.add_argument("--redact", action="store_true", help="mask values (safe to share)")
    a = p.parse_args(argv)
    load_dotenv()
    env, missing = pair_env(a.harness, a.industry)
    print(render(env, a.format, a.redact))
    if missing:
        print(f"\nmissing from .env: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
