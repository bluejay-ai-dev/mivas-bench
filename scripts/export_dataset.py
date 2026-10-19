"""Export the public MIVAS dataset (Hugging Face bluejay-labs/mivas-bench) from industries/.

The one export path. Source of truth is industries/<industry>/: tasks/*/task.json,
system-prompts/*.md, tools.json, agent_blueprint.json and agent_blueprint.mmd.
Nothing under industries/ is read for anything else or written to.

    uv run python scripts/export_dataset.py                 # rewrite dataset/
    uv run python scripts/export_dataset.py --out /tmp/hf   # write elsewhere
    uv run python scripts/export_dataset.py --check         # CI: exit 1 if dataset/ is stale

Row shape matches the 2026-08-24 export, plus `mivas_bench_commit` (git HEAD at export
time, or --commit). --check regenerates with the commit recorded in dataset/README.md so
only content drift fails; agent_blueprint.png and CHANGELOG.md are not generated and are
left alone.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_ROOT = ROOT / "industries"
DEFAULT_OUT = ROOT / "dataset"
TEMPLATE = Path(__file__).with_name("dataset_card_template.md")
INDUSTRIES = ("customer-support", "healthcare", "legal")
COPIED = ("agent_blueprint.json", "agent_blueprint.mmd", "tools.json")
ROW_KEYS = (
    "industry", "task_id", "task_name", "customer_name", "intent",
    "category", "category_slug", "difficulty", "audio_condition",
    "traits", "exp_handoff_path", "exp_tool_calls", "scripted_responses",
    "behaviors", "customer_available_tools", "exp_db_state", "mivas_bench_commit",
)
META_KEYS = ("category", "category_slug", "difficulty", "audio_condition")
UNMANAGED = {"CHANGELOG.md", ".gitattributes"}


def head_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def task_row(industry: str, task_dir: Path, commit: str) -> dict:
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    meta = task.get("metadata") or {}
    missing = [k for k in META_KEYS if not meta.get(k)]
    if missing:
        raise SystemExit(f"{task_dir}: metadata is missing {missing}")
    if not str(task.get("task_name", "")).startswith(f"{task_dir.name}:"):
        raise SystemExit(f"{task_dir}: task_name does not start with {task_dir.name!r}")
    fixed = {"industry": industry, "task_id": task_dir.name, "mivas_bench_commit": commit,
             **{k: meta[k] for k in META_KEYS}}
    try:
        return {k: fixed[k] if k in fixed else task[k] for k in ROW_KEYS}
    except KeyError as e:
        raise SystemExit(f"{task_dir}: task.json has no {e.args[0]!r}") from None


def export_industry(industry: str, out: Path, commit: str) -> list[dict]:
    src = INDUSTRY_ROOT / industry
    dest = out / industry
    dest.mkdir(parents=True, exist_ok=True)
    task_dirs = sorted((d for d in (src / "tasks").iterdir() if (d / "task.json").is_file()),
                       key=lambda d: d.name)
    rows = [task_row(industry, d, commit) for d in task_dirs]
    if not rows:
        raise SystemExit(f"{src / 'tasks'}: no task.json found")
    (dest / "tasks.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    for name in COPIED:
        shutil.copyfile(src / name, dest / name)
    prompts = dest / "system-prompts"
    shutil.rmtree(prompts, ignore_errors=True)
    prompts.mkdir()
    for p in sorted((src / "system-prompts").glob("*.md")):
        shutil.copyfile(p, prompts / p.name)
    return rows


def category_table(rows: list[dict]) -> str:
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["category"], r["category_slug"])
        counts[key] = counts.get(key, 0) + 1
    lines = ["| Category | Slug | Cases |", "|---|---|---|"]
    lines += [f"| {cat} | `{slug}` | {n} |" for (cat, slug), n in sorted(counts.items())]
    return "\n".join(lines)


def write_card(out: Path, commit: str, rows_by_industry: dict[str, list[dict]]) -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{commit}}", commit)
    text = text.replace("{{total}}", str(sum(len(r) for r in rows_by_industry.values())))
    for industry, rows in rows_by_industry.items():
        text = text.replace(f"{{{{count:{industry}}}}}", str(len(rows)))
        text = text.replace(f"{{{{categories:{industry}}}}}", category_table(rows))
    if "{{" in text:
        raise SystemExit(f"{TEMPLATE}: unfilled placeholder near {text[text.index('{{'):][:40]!r}")
    (out / "README.md").write_text(text, encoding="utf-8")


def export(out: Path, commit: str) -> None:
    rows = {ind: export_industry(ind, out, commit) for ind in INDUSTRIES}
    write_card(out, commit, rows)
    print(f"exported {sum(len(r) for r in rows.values())} rows to {out} at {commit[:12]}")


def recorded_commit(out: Path) -> str | None:
    readme = out / "README.md"
    if not readme.is_file():
        return None
    for line in readme.read_text(encoding="utf-8").splitlines():
        if line.startswith("mivas_bench_commit:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name not in UNMANAGED and p.suffix != ".png"}


def check(out: Path) -> int:
    commit = recorded_commit(out) or head_commit()
    with tempfile.TemporaryDirectory(prefix="mivas-export-") as tmp:
        export(Path(tmp), commit)
        want = _files(Path(tmp))
    have = _files(out) if out.is_dir() else {}
    stale = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
    if stale:
        print(f"{out} does not match industries/ ({len(stale)} files):")
        print("\n".join(f"  {k}" for k in stale))
        print("run `uv run python scripts/export_dataset.py` and commit dataset/")
        return 1
    print(f"{out} matches industries/ ({len(want)} files)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--commit", help="mivas-bench commit to stamp (default: git HEAD)")
    parser.add_argument("--check", action="store_true",
                        help="regenerate into a temp dir and fail if --out differs")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.out)
    export(args.out, args.commit or head_commit())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
