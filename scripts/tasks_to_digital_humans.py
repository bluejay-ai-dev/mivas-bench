"""Convert MIVAS task.json files into Bluejay digital-human payloads.

Source of truth is always `industries/<industry>/tasks/*/task.json`.
Bluejay-only fields (voice, occurrence_mode, audio, meta traits) are added
here and never written back onto the task.

    uv run python scripts/tasks_to_digital_humans.py --industry healthcare
    uv run python scripts/tasks_to_digital_humans.py --industry healthcare --json
    uv run python scripts/tasks_to_digital_humans.py --industry healthcare --push
    uv run python scripts/tasks_to_digital_humans.py --industry healthcare --simulation-id 30606
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_ROOT = ROOT / "industries"
DEFAULT_API = "https://api.getbluejay.ai/v1"

DEFAULT_AGENTS = {
    "healthcare": 32161,  # mivas healthcare · openai realtime-2.1 (not the k8s twin)
    "legal": 34170,
}

ESCALATION_SILENCE_TIMEOUT_S = 30

CREATIVITY = 0
BATCH_SIZE = 10
PACE_S = 0.25

CASE_KEY_RE = re.compile(r"^[A-Z]+\d*-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLONE_SUFFIXES = ("-BG", "-SIG")

DATE_TRAIT_NAMES = frozenset({
    "dob", "date_of_birth", "date_of_service", "appointment_date",
})
NUMBER_TRAIT_NAMES = frozenset({
    "balance_cents", "age", "fee_cents",
})

AUDIO = {
    "perfect": {"background_noise": "none", "background_noise_volume": 0.0, "audio_quality": "high"},
    "background_noise": {
        "background_noise": "traffic",
        "background_noise_volume": 0.8,
        "audio_quality": "high",
    },
    "bad_signal": {
        "background_noise": "none",
        "background_noise_volume": 0.0,
        "audio_quality": "medium",
    },
}

NO_TOOLS_CRITERIA = (
    "Success requires the standing spoken rule for this case to have been said, "
    "with no extra write."
)


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def voices() -> list[tuple[str, str]]:
    """Reuse the English catalog from scripts/healthcare_digital_humans.py."""
    path = ROOT / "scripts" / "healthcare_digital_humans.py"
    spec = importlib.util.spec_from_file_location("healthcare_dh_voices", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.VOICES)


def api_url() -> str:
    return os.environ.get("BLUEJAY_API_URL", DEFAULT_API).rstrip("/")


def _api_key() -> str:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        raise SystemExit("need BLUEJAY_API_KEY")
    return key


def _req(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    not_found_ok: bool = False,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    key = _api_key()
    req = urllib.request.Request(
        f"{api_url()}/{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "X-API-Key": key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if not_found_ok and e.code == 404:
            return {}
        body = e.read()[:600].decode(errors="replace")
        raise SystemExit(f"{method} {path} → {e.code} {body}") from e


def load_tasks(industry: str) -> list[dict[str, Any]]:
    tasks_dir = INDUSTRY_ROOT / industry / "tasks"
    if not tasks_dir.is_dir():
        raise SystemExit(f"no tasks directory: {tasks_dir}")
    rows: list[dict[str, Any]] = []
    for folder in sorted(p for p in tasks_dir.iterdir() if p.is_dir()):
        path = folder / "task.json"
        if not path.is_file():
            continue
        task = json.loads(path.read_text())
        if not isinstance(task, dict):
            raise SystemExit(f"{path} is not an object")
        rows.append({"case_key": folder.name, "path": path, "task": task})
    if not rows:
        raise SystemExit(f"no task.json files under {tasks_dir}")
    return rows


def source_case_key(case_key: str) -> str:
    for suffix in CLONE_SUFFIXES:
        if case_key.endswith(suffix):
            return case_key[: -len(suffix)]
    return case_key


def infer_trait_data_type(name: str, value: Any) -> str:
    if name in DATE_TRAIT_NAMES:
        return "DATE"
    if isinstance(value, bool):
        return "BOOLEAN"
    if name in NUMBER_TRAIT_NAMES:
        return "NUMBER"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "NUMBER"
    if isinstance(value, str) and ISO_DATE.fullmatch(value) and "date" in name:
        return "DATE"
    return "STRING"


def _trait(name: str, value: Any, data_type: str | None = None) -> dict[str, Any]:
    return {
        "trait_name": name,
        "trait_data_type": data_type or infer_trait_data_type(name, value),
        "value": value,
        "is_sip_header": False,
    }


def handoff_path_string(path: Any) -> str:
    names = [str(item) for item in (path or [])]
    return "[" + ", ".join(repr(name) for name in names) + "]"


def success_criteria(tool_calls: Any) -> str:
    names = [str(call.get("name")) for call in (tool_calls or []) if call.get("name")]
    if not names:
        return NO_TOOLS_CRITERIA
    if len(names) == 1:
        return f"Success requires {names[0]} to have been called."
    if len(names) == 2:
        return f"Success requires {names[0]} and {names[1]} to have been called."
    return f"Success requires {', '.join(names[:-1])}, and {names[-1]} to have been called."


def scripted_responses(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {
            "match_type": item.get("match_type"),
            "match_phrase": item.get("match_phrase"),
            "response_type": item.get("response_type"),
            "response_value": item.get("response_value"),
            "occurrence_mode": item.get("occurrence_mode") or "always",
        }
        if item.get("occurrence_n") is not None:
            row["occurrence_n"] = item["occurrence_n"]
        if item.get("silence_duration") is not None:
            row["silence_duration"] = item["silence_duration"]
        out.append(row)
    return out


def expected_tool_calls(raw: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        call: dict[str, Any] = {"name": item["name"]}
        if item.get("parameters") not in (None, {}):
            call["parameters"] = item["parameters"]
        if item.get("output") not in (None, {}):
            call["output"] = item["output"]
        calls.append(call)
    return calls


def person_name(task: dict[str, Any]) -> str:
    name = str(task.get("customer_name") or "").strip()
    if name:
        return name
    for item in task.get("traits") or []:
        if item.get("trait_name") == "full_name" and item.get("value"):
            return str(item["value"]).strip()
    raise SystemExit("task is missing customer_name")


def audio_fields(condition: str, industry: str = "") -> dict[str, Any]:
    mapped = AUDIO.get(condition)
    if mapped is None:
        raise SystemExit(f"unknown audio_condition: {condition}")
    out = dict(mapped)
    # Legal identity pins are 10-digit callbacks; 0.8 traffic buried them in ASR.
    if industry == "legal" and condition == "background_noise":
        out["background_noise_volume"] = 0.1
    return out


def assign_voices(rows: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    catalog = voices()
    scored = sorted(
        (row["case_key"] for row in rows if source_case_key(row["case_key"]) == row["case_key"]),
    )
    assigned: dict[str, tuple[str, str]] = {}
    for i, key in enumerate(scored):
        assigned[key] = catalog[i % len(catalog)]
    for row in rows:
        key = row["case_key"]
        if key not in assigned:
            source = source_case_key(key)
            if source not in assigned:
                raise SystemExit(f"clone {key} has no source voice ({source})")
            assigned[key] = assigned[source]
    return assigned


def creativity_of(task: dict[str, Any]) -> float:
    """Bluejay DH temperature. task.json `behaviors.creativity` wins; else 0."""
    behaviors = task.get("behaviors")
    if isinstance(behaviors, dict) and behaviors.get("creativity") is not None:
        return float(behaviors["creativity"])
    return float(CREATIVITY)


def task_to_digital_human(
    task: dict[str, Any],
    case_key: str,
    accent: str,
    gender: str,
    industry: str,
) -> dict[str, Any]:
    meta = task.get("metadata") or {}
    category_slug = str(meta.get("category_slug") or "")
    difficulty = str(meta.get("difficulty") or "")
    audio_condition = str(meta.get("audio_condition") or "perfect")
    industry_tag = f"mivas_{industry.replace('-', '_')}"

    traits = [
        _trait(item["trait_name"], item.get("value"))
        for item in (task.get("traits") or [])
        if item.get("trait_name")
    ]
    traits.extend([
        _trait("case_key", case_key, "STRING"),
        _trait("call_area", category_slug, "STRING"),
        _trait("difficulty", difficulty, "STRING"),
        _trait("audio_condition", audio_condition, "STRING"),
        _trait("expected_handoff_path", handoff_path_string(task.get("exp_handoff_path")), "STRING"),
    ])

    dh: dict[str, Any] = {
        "name": person_name(task),
        "test_name": task["task_name"],
        "intent": task.get("intent") or "",
        "success_criteria": success_criteria(task.get("exp_tool_calls")),
        "expected_tool_calls": expected_tool_calls(task.get("exp_tool_calls")),
        "traits": traits,
        "tags": [industry_tag, category_slug, audio_condition],
        "speaks_first_config": {"speaks_first": False},
        "creativity": creativity_of(task),
        "language": "en",
        "accent": accent,
        "gender": gender,
        "fluency": "native",
        "voice_speed": "normal",
        "verbosity": "medium",
        "interruptions": {"type": "none"},
        "allow_end_call_tool": True,
        "allow_silence_tool": True,
        "allow_dtmf_tool": False,
        "num_runs": 1,
        **audio_fields(audio_condition, industry),
    }
    pins = scripted_responses(task.get("scripted_responses"))
    if pins:
        dh["scripted_responses"] = pins
    if meta.get("escalation"):
        dh["silence_timeout"] = ESCALATION_SILENCE_TIMEOUT_S
    return dh


def build(industry: str) -> list[dict[str, Any]]:
    rows = load_tasks(industry)
    voice_by_key = assign_voices(rows)
    humans: list[dict[str, Any]] = []
    for row in rows:
        accent, gender = voice_by_key[row["case_key"]]
        humans.append(task_to_digital_human(row["task"], row["case_key"], accent, gender, industry))
    return humans


def trait_value(dh: dict[str, Any], name: str) -> str | None:
    for item in dh.get("traits") or []:
        if item.get("trait_name") == name:
            value = item.get("value")
            return None if value is None else str(value)
    return None


def case_key_of(dh: dict[str, Any]) -> str:
    keyed = trait_value(dh, "case_key")
    if keyed:
        return keyed
    test_name = str(dh.get("test_name") or "")
    if ":" in test_name:
        prefix = test_name.split(":", 1)[0].strip()
        if CASE_KEY_RE.match(prefix):
            return prefix
    raise ValueError("digital human has no case_key")


def check(humans: list[dict[str, Any]], industry: str) -> None:
    if industry in ("healthcare", "legal") and len(humans) != 66:
        raise SystemExit(f"expected 66 {industry} digital humans, got {len(humans)}")
    keys = [case_key_of(dh) for dh in humans]
    if len(keys) != len(set(keys)):
        raise SystemExit("duplicate case_key values")
    for dh in humans:
        key = case_key_of(dh)
        name = dh.get("name") or ""
        if name.startswith(key) or CASE_KEY_RE.match(str(name).split()[0] if name else ""):
            raise SystemExit(f"{key}: name must be person-only, got {name!r}")
        if dh.get("test_name", "").split(":", 1)[0].strip() != key:
            raise SystemExit(f"{key}: test_name must start with the case key")
        if "exp_db_state" in dh:
            raise SystemExit(f"{key}: verifier fields must not be copied onto the DH")
        for pin in dh.get("scripted_responses") or []:
            if pin.get("occurrence_mode") != "always":
                raise SystemExit(f"{key}: scripted_responses need occurrence_mode=always")
        audio = trait_value(dh, "audio_condition")
        mapped = audio_fields(audio or "", industry)
        for field, want in mapped.items():
            if dh.get(field) != want:
                raise SystemExit(f"{key}: {field}={dh.get(field)!r} want {want!r}")
        clone_source = source_case_key(key)
        if clone_source != key:
            source = next(h for h in humans if case_key_of(h) == clone_source)
            if (dh.get("accent"), dh.get("gender")) != (source.get("accent"), source.get("gender")):
                raise SystemExit(f"{key}: clone voice must match {clone_source}")


def create_payloads(
    humans: list[dict[str, Any]],
    simulation_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Unwrapped create items: {digital_human, simulation_ids}."""
    out = []
    for dh in humans:
        item: dict[str, Any] = {"digital_human": dh}
        if simulation_ids:
            item["simulation_ids"] = simulation_ids
        out.append(item)
    return out


def _unwrap_sim(body: dict[str, Any]) -> dict[str, Any]:
    sim = body.get("simulation") or body.get("data") or body
    return sim if isinstance(sim, dict) else body


def _sim_id(body: dict[str, Any]) -> int:
    sim = _unwrap_sim(body)
    value = sim.get("id") or sim.get("simulation_id") or body.get("id")
    if value is None:
        raise SystemExit(f"create-simulation returned no id: {list(body)[:8]}")
    return int(value)


def _duration_seconds(sim: dict[str, Any]) -> int | None:
    settings = sim.get("settings") if isinstance(sim.get("settings"), dict) else {}
    raw = sim.get("max_call_duration")
    if raw is None:
        raw = settings.get("max_call_duration")
    if raw is None:
        return None
    units = str(
        sim.get("max_call_duration_units")
        or settings.get("max_call_duration_units")
        or "seconds"
    ).lower()
    value = int(raw)
    if units.startswith("min"):
        return value * 60
    return value


def create_simulation(
    industry: str,
    agent_id: int,
    name: str | None,
    n_humans: int = 66,
) -> dict[str, Any]:
    title = name or f"mivas {industry} · prompt-adherence 66-case review"
    created = _req("POST", "create-simulation", {
        "agent_id": str(agent_id),
        "name": title,
        "max_concurrent": n_humans,
        "max_call_duration": 8,
        "max_call_duration_units": "minutes",
        "runs_per_digital_human": 1,
        "hangup_on_transfer": False,
    })
    sim_id = _sim_id(created)
    fetched = _unwrap_sim(_req("GET", f"simulation/{sim_id}"))
    seconds = _duration_seconds(fetched)
    if seconds == 28800:
        _req("PUT", f"simulation/{sim_id}", {
            "max_call_duration": 8,
            "max_call_duration_units": "minutes",
        })
        fetched = _unwrap_sim(_req("GET", f"simulation/{sim_id}"))
        seconds = _duration_seconds(fetched)
    if seconds != 480:
        print(f"warning: max_call_duration stored as {seconds}s (want 480)", flush=True)
    fetched["id"] = sim_id
    return fetched


def push_digital_humans(humans: list[dict[str, Any]], simulation_id: int) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    for i in range(0, len(humans), BATCH_SIZE):
        batch = humans[i : i + BATCH_SIZE]
        # unwrapped DH objects + top-level simulation_ids (wrapping each DH
        # as {digital_human: ...} 422s on create)
        resp = _req("POST", "create-digital-humans", {
            "simulation_ids": [simulation_id],
            "digital_humans": batch,
        })
        rows = (
            resp.get("digital_humans")
            or resp.get("created")
            or resp.get("created_digital_humans")
            or []
        )
        if not rows and isinstance(resp.get("data"), list):
            rows = resp["data"]
        created.extend(row.get("digital_human", row) if isinstance(row, dict) else row for row in rows)
        errs = resp.get("errors") or []
        if errs:
            raise SystemExit(f"create-digital-humans errors: {json.dumps(errs[:3])[:600]}")
        print(f"  posted {min(i + BATCH_SIZE, len(humans))}/{len(humans)}", flush=True)
        time.sleep(PACE_S)
    return created


def list_simulation_humans(simulation_id: int) -> list[dict[str, Any]]:
    body = _req("GET", f"digital-humans-by-simulation/{simulation_id}")
    return body.get("digital_humans") or body.get("data") or []


def unwrap_digital_human(body: dict[str, Any]) -> dict[str, Any] | None:
    dh = body.get("digital_human") or body.get("data") or body
    if isinstance(dh, dict) and dh.get("digital_human"):
        dh = dh["digital_human"]
    if isinstance(dh, dict) and dh.get("id"):
        return dh
    return None


def get_by_test_name(title: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(title, safe="")
    body = _req("GET", f"digital-human-by-test-name/{encoded}", not_found_ok=True)
    return unwrap_digital_human(body)


def hydrate_human(dh: dict[str, Any]) -> dict[str, Any]:
    if dh.get("traits") is not None and "test_name" in dh:
        return dh
    hid = dh.get("id")
    if hid is None:
        return dh
    full = unwrap_digital_human(_req("GET", f"digital-human/{hid}"))
    return full or dh


def prior_test_name(title: str, taken_lower: set[str]) -> str:
    """Mild rename so this suite can take the original title."""
    candidate = f"{title} (prior)"
    n = 2
    while candidate.casefold() in taken_lower:
        candidate = f"{title} (prior {n})"
        n += 1
    return candidate


def vacate_test_name(title: str, keep_ids: set[int]) -> str | None:
    """If another org DH holds `title`, rename it to `{title} (prior)`."""
    holder = get_by_test_name(title)
    if holder is None or int(holder["id"]) in keep_ids:
        return None
    taken = {title.casefold()}
    renamed = prior_test_name(title, taken)
    while True:
        clash = get_by_test_name(renamed)
        if clash is None or int(clash["id"]) == int(holder["id"]):
            break
        taken.add(renamed.casefold())
        renamed = prior_test_name(title, taken)
    _req("PUT", "update-digital-humans", {
        "updates": [{
            "digital_human_id": int(holder["id"]),
            "update": {"test_name": renamed},
        }],
    })
    print(f"  vacated {title!r} from dh {holder['id']} → {renamed!r}", flush=True)
    time.sleep(0.15)
    return renamed


CLAIM_FIELDS = (
    "name",
    "test_name",
    "intent",
    "success_criteria",
    "expected_tool_calls",
    "traits",
    "scripted_responses",
    "creativity",
    "tags",
    "background_noise",
    "background_noise_volume",
    "audio_quality",
)


def claim_patch(want: dict[str, Any]) -> dict[str, Any]:
    patch = {field: want[field] for field in CLAIM_FIELDS if field in want}
    patch["scripted_responses"] = want.get("scripted_responses") or []
    return patch


def _norm_traits(traits: Any) -> list[dict[str, Any]]:
    rows = traits if isinstance(traits, list) else []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        out.append({
            "trait_name": item.get("trait_name"),
            "trait_data_type": item.get("trait_data_type"),
            "value": item.get("value"),
            "is_sip_header": item.get("is_sip_header"),
        })
    return sorted(out, key=lambda row: str(row.get("trait_name") or ""))


def _norm_claim_field(field: str, value: Any) -> Any:
    if field == "expected_tool_calls":
        return expected_tool_calls(value)
    if field == "scripted_responses":
        rows = scripted_responses(value)
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("match_phrase") or ""),
                str(row.get("response_value") or ""),
            ),
        )
    if field == "traits":
        return _norm_traits(value)
    return value


def claim_updates(
    humans: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Patch live DHs with the generated contract, matched by case_key."""
    generated = {}
    for dh in humans:
        try:
            generated[case_key_of(dh)] = dh
        except ValueError:
            continue
    updates = []
    for dh in live:
        try:
            key = case_key_of(dh)
        except ValueError:
            test_name = str(dh.get("test_name") or "")
            key = test_name.split(":", 1)[0].strip() if ":" in test_name else ""
        want = generated.get(key)
        if not want or dh.get("id") is None:
            continue
        updates.append({"digital_human_id": int(dh["id"]), "update": claim_patch(want)})
    return updates


def verify_against_live(humans: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[str]:
    """Return human-readable mismatches between generated DHs and live sim DHs."""
    generated: dict[str, dict[str, Any]] = {}
    for dh in humans:
        try:
            generated[case_key_of(dh)] = dh
        except ValueError:
            continue
    errors: list[str] = []
    seen: set[str] = set()
    for raw in live:
        dh = hydrate_human(raw)
        try:
            key = case_key_of(dh)
        except ValueError:
            test_name = str(dh.get("test_name") or "")
            key = test_name.split(":", 1)[0].strip() if ":" in test_name else "?"
        seen.add(key)
        want = generated.get(key)
        if not want:
            errors.append(f"{key}: live DH {dh.get('id')} not in generated pack")
            continue
        for field, expected in claim_patch(want).items():
            got = dh.get(field)
            if _norm_claim_field(field, got) != _norm_claim_field(field, expected):
                errors.append(f"{key}: {field}={got!r} want {expected!r}")
    missing = sorted(set(generated) - seen)
    for key in missing:
        errors.append(f"{key}: generated DH missing from simulation")
    return errors


def claim_test_names(humans: list[dict[str, Any]], live: list[dict[str, Any]]) -> int:
    """Give this suite the MIVAS titles. Older holders get a mild (prior) suffix."""
    want = {case_key_of(dh): dh["test_name"] for dh in humans}
    hydrated = [hydrate_human(dh) for dh in live]
    keep_ids = {int(dh["id"]) for dh in hydrated if dh.get("id") is not None}
    vacated = 0
    for title in want.values():
        if vacate_test_name(title, keep_ids):
            vacated += 1
    updates = claim_updates(humans, hydrated)
    claimed = 0
    for i in range(0, len(updates), 20):
        resp = _req("PUT", "update-digital-humans", {"updates": updates[i : i + 20]})
        claimed += len(resp.get("updated") or resp.get("updated_ids") or [])
        for err in resp.get("errors") or []:
            print(f"  test_name claim skipped: {err.get('detail') or err}", flush=True)
        time.sleep(0.2)
    if vacated:
        print(f"renamed {vacated} older digital humans to free titles", flush=True)
    return claimed


def default_agent_id(industry: str) -> int:
    env = (
        os.environ.get("BLUEJAY_HEALTHCARE_AGENT_ID")
        if industry == "healthcare"
        else None
    ) or os.environ.get("MIVAS_AGENT_ID")
    if env:
        return int(env)
    return DEFAULT_AGENTS.get(industry, DEFAULT_AGENTS["healthcare"])


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--industry", required=True, help="Industry slug (e.g. healthcare)")
    parser.add_argument("--json", action="store_true", help="Print create payloads as JSON")
    parser.add_argument("--push", action="store_true",
                        help="Create an unrun Bluejay simulation and POST the digital humans")
    parser.add_argument(
        "--simulation-id",
        type=int,
        help="Claim MIVAS test_name titles on an existing simulation (renames older holders)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="With --simulation-id: check live DHs match generated pack; do not push",
    )
    parser.add_argument("--agent-id", type=int, help="Bluejay agent id (healthcare default: 32161)")
    parser.add_argument("--name", help="Simulation name")
    args = parser.parse_args(argv)

    humans = build(args.industry)
    check(humans, args.industry)

    if args.simulation_id and not args.push:
        live = list_simulation_humans(args.simulation_id)
        if args.verify_only:
            mismatches = verify_against_live(humans, live)
            if mismatches:
                for line in mismatches[:20]:
                    print(line, flush=True)
                if len(mismatches) > 20:
                    print(f"... and {len(mismatches) - 20} more", flush=True)
                raise SystemExit(f"{len(mismatches)} live DH mismatches")
            print(f"verified {len(humans)} digital humans on simulation {args.simulation_id}")
            return 0
        patched = claim_test_names(humans, live)
        live = list_simulation_humans(args.simulation_id)
        mismatches = verify_against_live(humans, live)
        if mismatches:
            for line in mismatches[:20]:
                print(line, flush=True)
            if len(mismatches) > 20:
                print(f"... and {len(mismatches) - 20} more", flush=True)
            raise SystemExit(
                f"claimed {patched} DHs but {len(mismatches)} fields still mismatched — not safe to run"
            )
        print(f"claimed test_name on {patched} digital humans for simulation {args.simulation_id}")
        print(f"verified {len(humans)} digital humans")
        print(f"https://app.getbluejay.ai/simulations/{args.simulation_id}")
        return 0

    if args.push:
        agent_id = args.agent_id or default_agent_id(args.industry)
        sim = create_simulation(args.industry, agent_id, args.name, n_humans=len(humans))
        sim_id = int(sim["id"])
        print(f"simulation {sim_id} on agent {agent_id}", flush=True)
        for title in (dh["test_name"] for dh in humans):
            vacate_test_name(title, keep_ids=set())
        push_digital_humans(humans, sim_id)
        live = list_simulation_humans(sim_id)
        patched = claim_test_names(humans, live)
        if patched:
            print(f"claimed test_name on {patched} digital humans", flush=True)
            live = list_simulation_humans(sim_id)
        url = f"https://app.getbluejay.ai/simulations/{sim_id}"
        print(f"{len(live)} digital humans · not queued")
        print(url)
        if len(live) != len(humans):
            raise SystemExit(f"expected {len(humans)} live DHs, got {len(live)}")
        return 0

    if args.json:
        json.dump({"digital_humans": humans}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"{len(humans)} digital humans from industries/{args.industry}/tasks")
    for dh in humans:
        key = case_key_of(dh)
        audio = trait_value(dh, "audio_condition")
        print(f"  {key:<12} {dh['name']:<24} {dh['accent']}/{dh['gender']:<6} {audio}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
