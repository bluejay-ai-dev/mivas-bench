"""Healthcare state API — SQLite persistence plus a generic tool dispatch.

Harnesses call POST /tools/{tool_name} with {"arguments": {...}} for every
industry tool; the REST routes stay for evals and debugging (GET /state,
GET /health). Session tools (end_call) and handoff tools (transfer_to_*)
never hit this server.

Tools without a dedicated table are minimal deterministic implementations
backed by the seeded fixtures; their writes land in `tool_events` so GET
/state still shows them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

INDUSTRY_DIR = Path(__file__).resolve().parent

for _runtime in (Path("/app/runtime"), Path(__file__).resolve().parents[2] / "runtime"):
    if (_runtime / "db_service.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from db_service import DBService  # noqa: E402
from tools_http import mount as mount_tools_http  # noqa: E402

db = DBService.for_industry(INDUSTRY_DIR)


@contextmanager
def _db() -> Any:
    with db.connect() as conn:
        yield conn


app = FastAPI(title="healthcare state API")
app.middleware("http")(db.http_middleware)
mount_tools_http(app, db.calls_dir)

logger = logging.getLogger(__name__)


class AppointmentCreate(BaseModel):
    patient_id: str | None = None
    location_id: str
    provider_id: str
    appointment_type_code: str
    start: str
    end: str
    description: str = ""


class AppointmentUpdate(BaseModel):
    start: str | None = None
    end: str | None = None
    location_id: str | None = None
    provider_id: str | None = None
    status: str | None = Field(default=None, description="booked | cancelled | completed")
    description: str | None = None


class WaitlistCreate(BaseModel):
    patient_id: str | None = None
    appointment_type_code: str
    location_ids: list[str]
    earliest: str | None = None
    latest: str | None = None


def _appointment(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "location_id": row["location_id"],
        "provider_id": row["provider_id"],
        "appointment_type_code": row["appointment_type_code"],
        "start": row["start"],
        "end": row["end"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump of durable state."""
    with _db() as conn:
        patients = [dict(r) for r in conn.execute("SELECT * FROM patients ORDER BY id")]
        locations = [dict(r) for r in conn.execute("SELECT * FROM locations ORDER BY id")]
        providers = [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY id")]
        appointments = [
            _appointment(r)
            for r in conn.execute("SELECT * FROM appointments ORDER BY id")
        ]
        waitlist = []
        for r in conn.execute("SELECT * FROM waitlist ORDER BY id"):
            waitlist.append(
                {
                    "id": r["id"],
                    "patient_id": r["patient_id"],
                    "appointment_type_code": r["appointment_type_code"],
                    "location_ids": json.loads(r["location_ids"]),
                    "earliest": r["earliest"],
                    "latest": r["latest"],
                    "created_at": r["created_at"],
                }
            )
        tool_events = [
            {"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"]),
             "created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM tool_events ORDER BY id")
        ]
    return {
        "patients": patients,
        "locations": locations,
        "providers": providers,
        "appointments": appointments,
        "waitlist": waitlist,
        "tool_events": tool_events,
    }


@app.get("/locations")
def list_locations() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM locations ORDER BY id")]


@app.get("/providers")
def list_providers() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY id")]


@app.get("/patients")
def list_patients() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM patients ORDER BY id")]


@app.get("/patients/{patient_id}")
def get_patient(patient_id: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    return dict(row)


@app.get("/appointments")
def list_appointments() -> list[dict[str, Any]]:
    with _db() as conn:
        return [
            _appointment(r)
            for r in conn.execute("SELECT * FROM appointments ORDER BY id")
        ]


@app.post("/appointments", status_code=201)
def create_appointment(body: AppointmentCreate) -> dict[str, Any]:
    with _db() as conn:
        if body.location_id and conn.execute(
            "SELECT 1 FROM locations WHERE id = ?", (body.location_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=400, detail="unknown location_id")
        if body.provider_id and conn.execute(
            "SELECT 1 FROM providers WHERE id = ?", (body.provider_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=400, detail="unknown provider_id")
        cur = conn.execute(
            """
            INSERT INTO appointments (
              patient_id, location_id, provider_id, appointment_type_code,
              start, end, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.patient_id,
                body.location_id,
                body.provider_id,
                body.appointment_type_code,
                body.start,
                body.end,
                body.description,
            ),
        )
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="insert failed")
    return _appointment(row)


@app.patch("/appointments/{appointment_id}")
def update_appointment(appointment_id: int, body: AppointmentUpdate) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [appointment_id]
    with _db() as conn:
        cur = conn.execute(
            f"UPDATE appointments SET {cols} WHERE id = ?", vals
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="appointment not found")
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
    assert row is not None
    return _appointment(row)


@app.post("/waitlist", status_code=201)
def join_waitlist(body: WaitlistCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO waitlist (
              patient_id, appointment_type_code, location_ids, earliest, latest
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                body.patient_id,
                body.appointment_type_code,
                json.dumps(body.location_ids),
                body.earliest,
                body.latest,
            ),
        )
        row = conn.execute(
            "SELECT * FROM waitlist WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="insert failed")
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "appointment_type_code": row["appointment_type_code"],
        "location_ids": json.loads(row["location_ids"]),
        "earliest": row["earliest"],
        "latest": row["latest"],
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------------ dispatch
# POST /tools/{tool_name} {"arguments": {...}} — the industry-agnostic contract
# every harness speaks. Wraps everything in the tools.json envelope:
# {"ok": bool, "data": ..., "error_code": str|null, "patient_safe_message": str|null}.
# Session (end_call) and handoff (transfer_to_*) tools never land here → 404.

# Fixed "now" so cancellation-window math is deterministic across runs. The
# seeded MED_FOLLOWUP on 2026-08-20T10:00 is inside the 24 h medical window.
TODAY = "2026-08-19T12:00:00"

# Identity pin per call id (empty key = the shared/no-header session).
_sessions: dict[str, dict[str, Any]] = {}


def _session_state() -> dict[str, Any]:
    return _sessions.setdefault(db.current_call_id() or "", {})


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _event(kind: str, payload: dict[str, Any]) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO tool_events (kind, payload) VALUES (?, ?)",
            (kind, json.dumps(payload)),
        )
        event_id = int(cur.lastrowid or 0)
    return event_id


def _patient_row() -> sqlite3.Row:
    pid = _session_state().get("patient_id")
    if not pid or not _session_state().get("verified"):
        raise ToolError("NOT_VERIFIED", "Verify the caller's identity first.")
    with _db() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise ToolError("NOT_VERIFIED", "Verify the caller's identity first.")
    return row


def _resolve_location(value: str) -> sqlite3.Row:
    """Accept a real id OR whatever the caller called the office."""
    said = str(value or "").strip().lower()
    with _db() as conn:
        for row in conn.execute("SELECT * FROM locations ORDER BY id"):
            if said == row["id"].lower() or said in row["name"].lower() or row["name"].lower() in said:
                return row
    raise ToolError("UNKNOWN_LOCATION", f"No office matches {value!r}.")


def _iso_plus_minutes(start: str, minutes: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(start) + timedelta(minutes=minutes)).isoformat()


# --- identity ----------------------------------------------------------------


def _d_resolve_inbound_context(a: dict[str, Any]) -> dict[str, Any]:
    ani = str(a.get("caller_ani") or "").strip()
    with _db() as conn:
        patient = conn.execute(
            "SELECT * FROM patients WHERE phone_e164 = ?", (ani,)
        ).fetchone() if ani else None
        office = None
        if patient and patient["home_office_id"]:
            office = conn.execute(
                "SELECT * FROM locations WHERE id = ?", (patient["home_office_id"],)
            ).fetchone()
        if office is None:
            office = conn.execute("SELECT * FROM locations ORDER BY id LIMIT 1").fetchone()
    return {
        "office_id": office["id"],
        "office_name": office["name"],
        "brand_greeting": "Thank you for calling Straus Dermatology",
        "known_caller": patient is not None,
    }


_SPANISH = {"hola", "gracias", "buenos", "buenas", "cita", "necesito", "español",
            "espanol", "habla", "quiero", "por favor"}


def _d_detect_language(a: dict[str, Any]) -> dict[str, Any]:
    words = set(str(a["utterance"]).lower().split())
    lang = "es" if words & _SPANISH else "en"
    _session_state()["language"] = lang
    return {"language": lang, "note": "Switch to this language and stay switched."}


def _d_identify_patient(a: dict[str, Any]) -> dict[str, Any]:
    first = str(a.get("first_name") or "").strip().lower()
    last = str(a.get("last_name") or "").strip().lower()
    dob = str(a.get("dob") or "").strip()
    zip_ = str(a.get("zip") or "").strip()
    candidates = []
    with _db() as conn:
        for row in conn.execute("SELECT * FROM patients ORDER BY id"):
            if first and row["first_name"].lower() != first:
                continue
            if last and row["last_name"].lower() != last:
                continue
            if dob and row["dob"] != dob:
                continue
            if zip_ and row["zip"] != zip_:
                continue
            candidates.append(
                {
                    "patient_id": row["id"],
                    "first_name": row["first_name"],
                    "last_initial": row["last_name"][:1],
                    "dob_year": row["dob"][:4],
                }
            )
    if not (first or last or dob or zip_):
        candidates = []
    return {"candidates": candidates, "count": len(candidates)}


def _d_verify_identity(a: dict[str, Any]) -> dict[str, Any]:
    if _session_state().get("verify_failures", 0) >= 3:
        raise ToolError(
            "IDENTITY_LOCKED",
            "Verification is locked after three failures. Offer the front desk or a callback.",
        )
    name = str(a["full_name"]).strip().lower().split()
    dob = str(a["dob"]).strip()
    second = str(a.get("second_factor") or "").strip()
    with _db() as conn:
        for row in conn.execute("SELECT * FROM patients ORDER BY id"):
            full = f"{row['first_name']} {row['last_name']}".lower().split()
            if name and name[0] == full[0] and name[-1] == full[-1] and row["dob"] == dob:
                if second and second != row["zip"] and second != (row["phone_e164"] or "")[-4:]:
                    break
                _session_state().update(patient_id=row["id"], verified=True, verify_failures=0)
                return {"verified": True, "patient_id": row["id"]}
    _session_state()["verify_failures"] = _session_state().get("verify_failures", 0) + 1
    raise ToolError(
        "NO_MATCH",
        "That name and date of birth don't match a record. Re-confirm the spelling "
        "and date of birth; after a third failure offer the front desk.",
    )


def _d_get_patient_summary(a: dict[str, Any]) -> dict[str, Any]:
    row = _patient_row()
    with _db() as conn:
        upcoming = [
            _appointment(r)
            for r in conn.execute(
                "SELECT * FROM appointments WHERE patient_id = ? AND status = 'booked' "
                "ORDER BY start",
                (row["id"],),
            )
        ]
        office = conn.execute(
            "SELECT name FROM locations WHERE id = ?", (row["home_office_id"],)
        ).fetchone()
    member = row["member_id"] or ""
    return {
        "patient_id": row["id"],
        "name": f"{row['first_name']} {row['last_name']}",
        "language": row["language"],
        "home_office": office["name"] if office else None,
        "upcoming_appointments": upcoming,
        "balance_cents": row["balance_cents"],
        "insurance": {
            "carrier": row["carrier"],
            "plan_name": row["plan_name"],
            "member_id_last4": member[-4:] if member else None,
        },
        "portal_active": False,
        "open_orders": [],
        "clinical_flags": [],
    }


# --- scheduling ---------------------------------------------------------------

_VISIT_TYPES = [
    ({"botox", "filler", "juvederm", "laser", "cosmetic", "microneedling", "peel"},
     {"appointment_type_code": "COS_CONSULT", "visit_class": "cosmetic",
      "required_credential": "MD", "duration_min": 30, "urgency": "routine",
      "constraints": ["cosmetic offices only", "deposit policy applies"]}),
    ({"mohs", "skin cancer", "melanoma", "biopsy"},
     {"appointment_type_code": "MOHS_CONSULT", "visit_class": "medical",
      "required_credential": "MD", "duration_min": 45, "urgency": "urgent",
      "constraints": ["must be booked with an MD, never a PA"]}),
    ({"allergy", "allergies", "asthma", "hives", "shot", "immunotherapy"},
     {"appointment_type_code": "ALLERGY_EVAL", "visit_class": "allergy",
      "required_credential": "MD", "duration_min": 40, "urgency": "routine",
      "constraints": ["allergy services carry prep instructions"]}),
]


def _d_classify_visit_request(a: dict[str, Any]) -> dict[str, Any]:
    text = str(a["reason_text"]).lower()
    for keywords, spec in _VISIT_TYPES:
        if any(k in text for k in keywords):
            return dict(spec)
    urgent = any(k in text for k in ("bleeding", "spreading", "infected", "severe", "painful"))
    new = bool(a.get("is_new_patient"))
    return {
        "appointment_type_code": "NP_MED" if new else "MED_FOLLOWUP",
        "visit_class": "medical",
        "required_credential": "MD_OR_PA",
        "duration_min": 30 if new else 20,
        "urgency": "urgent" if urgent else "routine",
        "constraints": [],
    }


def _d_list_locations(a: dict[str, Any]) -> dict[str, Any]:
    """Offices, best match first. Either `zip` or `name` will do.

    reception.md promises this tool turns "whatever they called the office" into a real
    location, but `zip` used to be the only way in — so an existing patient who names
    their office ("Brooklyn Heights") gave the agent nothing to call it with, and the
    agent stalled asking for a ZIP it did not need.
    """
    zip_ = str(a.get("zip") or "").strip()
    name = str(a.get("name") or a.get("location") or "").strip().lower()
    with _db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM locations ORDER BY id")]
    def rank(r: dict[str, Any]) -> tuple[int, int]:
        by_name = 0 if name and (name in r["name"].lower() or r["name"].lower() in name) else 1
        by_zip = 0 if zip_ and r["zip"] == zip_ else 1
        return (by_name, by_zip)
    rows.sort(key=rank)
    for r in rows:
        r["offers_cosmetic"] = bool(r["offers_cosmetic"])
    return {"locations": rows}


_SLOT_TIMES = ("2026-08-24T09:00:00", "2026-08-25T11:30:00", "2026-08-26T14:00:00")


def _d_find_slots(a: dict[str, Any]) -> dict[str, Any]:
    """Deterministic open slots per requested office (no slot inventory table —
    book_appointment carries the full location/provider/start anyway)."""
    location_ids = [str(x) for x in (a.get("location_ids") or [])]
    window_start = str(a.get("window_start") or "")
    window_end = str(a.get("window_end") or "")
    max_results = int(a.get("max_results") or 6)
    slots = []
    with _db() as conn:
        for loc_id in location_ids:
            loc = conn.execute("SELECT * FROM locations WHERE id = ?", (loc_id,)).fetchone()
            if loc is None:
                try:
                    loc = _resolve_location(loc_id)
                except ToolError:
                    continue
            prov = conn.execute(
                "SELECT * FROM providers WHERE location_id = ? ORDER BY id LIMIT 1",
                (loc["id"],),
            ).fetchone()
            if prov is None:
                continue
            for i, start in enumerate(_SLOT_TIMES):
                if window_start and start < window_start:
                    continue
                if window_end and start > window_end:
                    continue
                slots.append(
                    {
                        "slot_id": f"slot_{loc['id']}_{i + 1}",
                        "location_id": loc["id"],
                        "location": loc["name"],
                        "provider_id": prov["id"],
                        "provider": f"{prov['name']}, {prov['credentials']}",
                        "start": start,
                        "end": _iso_plus_minutes(start, 30),
                    }
                )
    return {"slots": slots[:max_results], "count": min(len(slots), max_results)}


def _resolve_slot(slot_id: str) -> dict[str, Any]:
    """Recompute the exact same deterministic slot _d_find_slots would have
    offered for this slot_id, so book_appointment can bind the booking to
    whatever was actually presented to the caller instead of trusting
    arbitrary location/provider/start/end supplied alongside the id."""
    with _db() as conn:
        for loc in conn.execute("SELECT * FROM locations ORDER BY id"):
            prov = conn.execute(
                "SELECT * FROM providers WHERE location_id = ? ORDER BY id LIMIT 1",
                (loc["id"],),
            ).fetchone()
            if prov is None:
                continue
            for i, start in enumerate(_SLOT_TIMES):
                if slot_id == f"slot_{loc['id']}_{i + 1}":
                    return {
                        "location_id": loc["id"],
                        "provider_id": prov["id"],
                        "start": start,
                        "end": _iso_plus_minutes(start, 30),
                    }
    raise ToolError(
        "UNKNOWN_SLOT", f"No open slot matches {slot_id!r}. Call find_slots again."
    )


def _d_book_appointment(a: dict[str, Any]) -> dict[str, Any]:
    slot = _resolve_slot(str(a["slot_id"]))
    if (
        str(a["location_id"]) != slot["location_id"]
        or str(a["provider_id"]) != slot["provider_id"]
        or str(a["start"]) != slot["start"]
        or str(a["end"]) != slot["end"]
    ):
        raise ToolError(
            "SLOT_MISMATCH",
            "That slot no longer matches what was offered — call find_slots again "
            "and read back the new time before booking.",
        )
    created = create_appointment(
        AppointmentCreate(
            patient_id=_session_state().get("patient_id"),
            location_id=slot["location_id"],
            provider_id=slot["provider_id"],
            appointment_type_code=a["appointment_type_code"],
            start=slot["start"],
            end=slot["end"],
            description=a.get("description", ""),
        )
    )
    return {"appointment": created, "status": "booked"}


def _owned_appointment(appointment_id: int) -> sqlite3.Row:
    """An appointment row, scoped to the verified caller — prevents an id from
    reaching another patient's booking without identity verification."""
    patient = _patient_row()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ? AND patient_id = ?",
            (appointment_id, patient["id"]),
        ).fetchone()
    if row is None:
        raise ToolError("NOT_FOUND", "No appointment with that id for this patient.")
    return row


def _d_reschedule_appointment(a: dict[str, Any]) -> dict[str, Any]:
    appt_id = int(a["appointment_id"])
    _owned_appointment(appt_id)
    updated = update_appointment(
        appt_id,
        AppointmentUpdate(start=a["new_start"], end=a["new_end"], status="booked"),
    )
    return {"appointment": updated, "status": "rescheduled", "fee_cents": 0,
            "note": "Rescheduling never costs anything — say so."}


def _d_cancel_appointment(a: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime

    appt_id = int(a["appointment_id"])
    patient = _patient_row()
    row = _owned_appointment(appt_id)
    if row["status"] == "cancelled":
        return {"status": "cancelled", "fee_charged_cents": 0,
                "note": "Already cancelled — no fee charged again."}
    cosmetic = row["appointment_type_code"].startswith("COS")
    window_h, fee_cents = (72, 12500) if cosmetic else (24, 5000)
    hours_out = (datetime.fromisoformat(row["start"]) - datetime.fromisoformat(TODAY)).total_seconds() / 3600
    inside_window = hours_out < window_h
    if inside_window and not a.get("fee_disclosed_and_accepted"):
        fee = fee_cents // 100
        return {
            "status": "fee_disclosure_required",
            "fee_cents": fee_cents,
            "required_script": (
                f"Because this visit is within {window_h} hours, a ${fee} missed-visit "
                "fee applies if you cancel. Moving it instead is free — would you like "
                "a different time, or should I still cancel?"
            ),
        }
    update_appointment(appt_id, AppointmentUpdate(status="cancelled"))
    fee_charged_cents = 0
    if inside_window:
        fee_charged_cents = fee_cents
        with _db() as conn:
            conn.execute(
                "UPDATE patients SET balance_cents = balance_cents + ? WHERE id = ?",
                (fee_cents, patient["id"]),
            )
        _event(
            "cancellation_fee",
            {"patient_id": patient["id"], "appointment_id": appt_id,
             "fee_cents": fee_cents,
             "cancellation_reason_code": a["cancellation_reason_code"]},
        )
    return {
        "status": "cancelled",
        "fee_charged_cents": fee_charged_cents,
        "cancellation_reason_code": a["cancellation_reason_code"],
        "note": "Offer to rebook or join the waitlist.",
    }


def _d_join_waitlist(a: dict[str, Any]) -> dict[str, Any]:
    entry = join_waitlist(
        WaitlistCreate(
            patient_id=_session_state().get("patient_id"),
            appointment_type_code=a["appointment_type_code"],
            location_ids=[str(x) for x in a["location_ids"]],
            earliest=a.get("earliest"),
            latest=a.get("latest"),
        )
    )
    return {"waitlist_entry": entry, "status": "added"}


_ALLERGY_SERVICES = {
    "skin_testing": {"prep": "Stop antihistamines seven days before the visit.",
                     "observation_min": 0, "linked_visits": []},
    "patch_testing": {"prep": "Keep your back dry and skip topical steroids on it for a week.",
                      "observation_min": 0,
                      "linked_visits": ["48-hour patch read", "96-hour patch read"]},
    "food_challenge": {"prep": "Come fasting and plan on a four-hour visit.",
                       "observation_min": 240, "linked_visits": []},
    "allergy_shot": {"prep": "", "observation_min": 30, "linked_visits": []},
    "drops_pickup": {"prep": "", "observation_min": 0, "linked_visits": []},
    "asthma_eval": {"prep": "Hold your rescue inhaler for six hours if you safely can.",
                    "observation_min": 0, "linked_visits": []},
    "immunotherapy_buildup": {"prep": "", "observation_min": 30,
                              "linked_visits": ["weekly buildup visits"]},
}


def _d_schedule_allergy_service(a: dict[str, Any]) -> dict[str, Any]:
    service = str(a["service"]).strip().lower()
    spec = _ALLERGY_SERVICES.get(service)
    if spec is None:
        raise ToolError("UNKNOWN_SERVICE", f"No allergy service named {a['service']!r}.")
    loc = _resolve_location(a["location_id"])
    with _db() as conn:
        prov = conn.execute(
            "SELECT * FROM providers WHERE location_id = ? ORDER BY id LIMIT 1", (loc["id"],)
        ).fetchone()
    start = str(a.get("window_start") or _SLOT_TIMES[0])
    created = create_appointment(
        AppointmentCreate(
            patient_id=_session_state().get("patient_id"),
            location_id=loc["id"],
            provider_id=prov["id"],
            appointment_type_code=f"ALLERGY_{service.upper()}",
            start=start,
            end=_iso_plus_minutes(start, 30 + spec["observation_min"]),
            description=f"Allergy service: {service}",
        )
    )
    return {
        "appointment": created,
        "prep_instructions": spec["prep"],
        "observation_minutes_after": spec["observation_min"],
        "linked_return_visits": spec["linked_visits"],
        "note": "Say the prep, the observation time, and the linked visits out loud.",
    }


# --- coverage / billing --------------------------------------------------------

_ACCEPTED_CARRIERS = {"aetna", "unitedhealthcare", "united healthcare", "cigna",
                      "blue cross blue shield", "bcbs", "medicare"}
_NOT_ACCEPTED_CARRIERS = {"medicaid"}


_UNCONFIRMED_SCRIPT = (
    "I can't confirm that plan over the phone. The insurance team will "
    "verify it before your visit — I can set up a callback or you can "
    "text our insurance line."
)


def _d_check_plan_accepted(a: dict[str, Any]) -> dict[str, Any]:
    carrier = str(a["carrier"]).strip().lower()
    loc = _resolve_location(a["location_id"])
    provider_id = a.get("provider_id")
    if provider_id:
        with _db() as conn:
            prov = conn.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        if prov is None or prov["location_id"] != loc["id"]:
            # Unknown provider/location combo — can't validate the plan against
            # a provider we can't place, so don't assert coverage either way.
            return {
                "accepted": None,
                "must_not_assert": True,
                "carrier": a["carrier"],
                "location": loc["name"],
                "required_script": _UNCONFIRMED_SCRIPT,
            }
    with _db() as conn:
        others = [r["name"] for r in conn.execute(
            "SELECT name FROM locations WHERE id != ? ORDER BY id", (loc["id"],))]
    if carrier in _NOT_ACCEPTED_CARRIERS:
        # acceptance is carrier-level in this fixture, so no office takes it. Handing back
        # sibling offices as "alternatives" invited a false "try Brooklyn Heights instead".
        return {"accepted": False, "must_not_assert": False,
                "carrier": a["carrier"], "location": loc["name"],
                "alternative_locations": [],
                "notes": (f"{a['carrier']} is not accepted at any Straus office. Say so "
                          "plainly and offer self-pay pricing or a callback — do not send "
                          "them to another office."),
                "required_script": (f"We don't accept {a['carrier']} at any of our offices. "
                                    "I can go over self-pay pricing or have someone call "
                                    "you about options.")}
    if carrier in _ACCEPTED_CARRIERS:
        # We only have a carrier-level acceptance list, no real plan/provider
        # coverage matrix — a specific plan_name/plan_type can't be validated,
        # so don't assert accepted for those dimensions.
        if a.get("plan_name") or a.get("plan_type"):
            return {
                "accepted": None,
                "must_not_assert": True,
                "carrier": a["carrier"],
                "location": loc["name"],
                "required_script": _UNCONFIRMED_SCRIPT,
            }
        return {"accepted": True, "must_not_assert": False,
                "carrier": a["carrier"], "location": loc["name"]}
    return {
        "accepted": None,
        "must_not_assert": True,
        "carrier": a["carrier"],
        "location": loc["name"],
        "required_script": _UNCONFIRMED_SCRIPT,
    }


def _d_run_eligibility_check(a: dict[str, Any]) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE member_id = ? AND dob = ?",
            (str(a["member_id"]).strip(), str(a["dob"]).strip()),
        ).fetchone()
    if row is None:
        raise ToolError(
            "PAYER_UNAVAILABLE",
            "The payer didn't return eligibility. Say you couldn't get the number — "
            "never guess at a copay.",
        )
    submitted_carrier = "".join(str(a["carrier"]).strip().lower().split())
    on_file_carrier = "".join(str(row["carrier"] or "").strip().lower().split())
    if not on_file_carrier or submitted_carrier != on_file_carrier:
        raise ToolError(
            "PAYER_UNAVAILABLE",
            "The payer didn't return eligibility for that carrier. Say you "
            "couldn't get the number — never guess at a copay.",
        )
    return {"copay_cents": 3000, "deductible_remaining_cents": 25000,
            "coinsurance_pct": 20, "plan_active": True, "service_date": a["service_date"]}


def _d_capture_insurance_update(a: dict[str, Any]) -> dict[str, Any]:
    row = _patient_row()
    with _db() as conn:
        conn.execute(
            "UPDATE patients SET carrier = ?, member_id = ? WHERE id = ?",
            (a["carrier"], a["member_id"], row["id"]),
        )
    # group_number has no dedicated column — it must never overwrite plan_name;
    # keep it (and the rest of the update) in the durable event stream instead.
    _event("insurance_update", {"patient_id": row["id"], **a})
    return {"updated": True, "card_upload_link_sent": True,
            "note": "A secure link for card photos was texted."}


def _line_items(balance_cents: int) -> list[dict[str, Any]]:
    items = []
    remainder = balance_cents
    if balance_cents >= 5000:
        items.append({"line_item_id": "li_noshow", "description": "Missed-visit fee",
                      "amount_cents": 5000})
        remainder -= 5000
    if remainder > 0:
        items.append({"line_item_id": "li_visit", "description": "Office visit balance",
                      "amount_cents": remainder})
    return items


def _d_get_account_balance(a: dict[str, Any]) -> dict[str, Any]:
    row = _patient_row()
    return {"balance_cents": row["balance_cents"], "line_items": _line_items(row["balance_cents"])}


_CHARGE_SCRIPTS = {
    "li_noshow": ("That fifty dollar charge is the missed-visit fee from a visit that "
                  "was cancelled inside the notice window. If you believe it shouldn't "
                  "apply, I can open a review with the billing team."),
    "li_visit": ("That charge is the portion of your visit your insurance applied to "
                 "your deductible or copay after the claim processed."),
}


def _d_explain_charge(a: dict[str, Any]) -> dict[str, Any]:
    script = _CHARGE_SCRIPTS.get(str(a["line_item_id"]))
    if script is None:
        raise ToolError("UNKNOWN_LINE_ITEM", "No approved explanation for that line item — "
                                             "open a billing callback instead.")
    return {"line_item_id": a["line_item_id"], "approved_script": script}


def _d_send_payment_link(a: dict[str, Any]) -> dict[str, Any]:
    _event("payment_link", dict(a))
    return {"sent": True, "channel": "sms", "mobile_e164": a["mobile_e164"],
            "amount_cents": a.get("amount_cents")}


def _d_offer_financing(a: dict[str, Any]) -> dict[str, Any]:
    amount = int(a["amount_cents"])
    eligible = amount >= 25000
    return {"eligible": eligible, "provider": "CareCredit",
            "note": None if eligible else "CareCredit is offered for balances over $250."}


def _d_request_fee_waiver(a: dict[str, Any]) -> dict[str, Any]:
    _event("fee_waiver_request", dict(a))
    return {"review_opened": True, "sla": "two business days",
            "spoken_commitment": ("The billing team will review that fee and get back "
                                  "to you within two business days.")}


# --- cosmetic -------------------------------------------------------------------

_COSMETIC_QUOTES = {
    "botox": {"low_cents": 30000, "high_cents": 60000},
    "filler": {"low_cents": 60000, "high_cents": 120000},
    "chemical_peel": {"low_cents": 20000, "high_cents": 40000},
    "microneedling": {"low_cents": 35000, "high_cents": 70000},
}

_COSMETIC_POLICY_LINES = [
    "A $125 deposit holds the consult.",
    "You can move or cancel it free of charge up to 72 hours before.",
    "Inside 72 hours the deposit is forfeited.",
    "Any remaining balance for treatment is due at the visit.",
]


def _d_quote_cosmetic_service(a: dict[str, Any]) -> dict[str, Any]:
    service = str(a["service"]).strip().lower().replace(" ", "_")
    rng = _COSMETIC_QUOTES.get(service)
    if rng:
        return {"service": a["service"], "price_range": rng,
                "note": "You may say this range and must add that the consult settles the number."}
    return {"service": a["service"], "price_range": None,
            "note": "Pricing depends on the treatment plan — never invent or estimate a number."}


def _d_book_cosmetic_consult(a: dict[str, Any]) -> dict[str, Any]:
    if not a.get("policy_acknowledged"):
        return {"status": "policy_disclosure_required",
                "policy_lines": _COSMETIC_POLICY_LINES,
                "note": "Say all four lines, get a real yes, then call again with true."}
    loc = _resolve_location(a["location_id"])
    if not loc["offers_cosmetic"]:
        raise ToolError("COSMETIC_NOT_OFFERED", f"{loc['name']} does not do cosmetic work.")
    start = a["start"]
    created = create_appointment(
        AppointmentCreate(
            patient_id=_session_state().get("patient_id"),
            location_id=loc["id"],
            provider_id=a["provider_id"],
            appointment_type_code="COS_CONSULT",
            start=start,
            end=_iso_plus_minutes(start, 30),
            description="Cosmetic consult: " + ", ".join(a.get("service_interest") or []),
        )
    )
    return {"appointment": created, "status": "booked", "deposit_cents": 12500}


# --- clinical -------------------------------------------------------------------

_RX_HARD_STOPS = (
    ({"isotretinoin", "accutane"}, "isotretinoin_program",
     "Isotretinoin refills require the monthly program visit — offer to book it."),
    ({"tramadol", "xanax", "adderall", "oxycodone", "codeine"}, "controlled_substance",
     "Controlled medications are never refilled by phone — the prescriber must see them."),
    ({"dupixent", "humira", "skyrizi", "biologic"}, "biologic_coordinator",
     "Biologics route to the biologic coordinator, who will call back."),
)


def _d_request_rx_refill(a: dict[str, Any]) -> dict[str, Any]:
    patient = _patient_row()
    med = str(a["medication_name"]).strip().lower()
    for keywords, route, note in _RX_HARD_STOPS:
        if any(k in med for k in keywords):
            _event("rx_refill_request", {"patient_id": patient["id"], **a, "route": route})
            return {"route": route, "hard_stop": True, "approved": False, "note": note}
    _event("rx_refill_request", {"patient_id": patient["id"], **a, "route": "routed_to_provider"})
    return {"route": "routed_to_provider", "hard_stop": False, "approved": False,
            "pharmacy_needed": not a.get("pharmacy_name"),
            "note": "The request is with the clinical team; this never approves a refill."}


def _d_get_results_status(a: dict[str, Any]) -> dict[str, Any]:
    order = str(a.get("order_type") or "test")
    return {
        "status": "resulted_pending_review",
        "approved_script": (
            f"Your {order} results are in and are with the clinical team for review. "
            "A clinician will reach out — I can't read results over the phone, but I "
            "can send the team a message."
        ),
    }


# clinical.md tells the agent to "say the callback window out loud" off this tool, so the
# tool has to supply one — without it the agent either invents a window or (correctly)
# refuses to state one and fails a criterion it could never satisfy.
_CLINICAL_CALLBACK_WINDOW = {
    "stat": "within the hour",
    "urgent": "within four hours",
    "routine": "by the end of the next business day",
}


def _d_create_clinical_message(a: dict[str, Any]) -> dict[str, Any]:
    patient = _patient_row()
    event_id = _event("clinical_message", {"patient_id": patient["id"], **a})
    priority = str(a["priority"]).strip().lower()
    window = _CLINICAL_CALLBACK_WINDOW.get(priority, _CLINICAL_CALLBACK_WINDOW["routine"])
    return {"message_id": f"cm_{event_id}", "queued": True,
            "priority": a["priority"], "category": a["category"],
            "callback_window": window,
            "spoken_commitment": f"Someone from the clinical team will call you back {window}."}


# --- practice / plumbing ----------------------------------------------------------

_KB = [
    ({"hour", "open", "close"}, "hours",
     "Offices are open 8am to 5pm Monday through Friday, and Park Avenue is open "
     "9am to 1pm on Saturdays."),
    ({"park", "parking", "direction", "subway", "train"}, "directions",
     "Park Avenue is at 36th and Park, a block from the 6 train; there is a garage "
     "next door. Brooklyn Heights is two blocks from Borough Hall."),
    ({"portal", "login", "password"}, "portal",
     "The patient portal is portal.strausderm.example; activation links arrive by "
     "text and expire after 72 hours."),
    ({"fee", "cancel", "cancellation", "no-show", "missed"}, "fees",
     "Cancellations need 24 hours notice for medical visits and 72 for cosmetic; the "
     "missed-visit fee is $50 medical and $125 cosmetic."),
    ({"treat", "service", "condition"}, "services",
     "Straus treats medical, surgical, and cosmetic dermatology plus allergy testing "
     "and immunotherapy."),
]


def _d_search_practice_kb(a: dict[str, Any]) -> dict[str, Any]:
    """Substring match, most-specific bucket first.

    Exact-token intersection made the KB unable to answer its own hours question: the
    office is named "Park Avenue", so "Park Avenue office hours closing time" hit the
    `directions` bucket on "park" and never reached `hours` (which keys on "hour"/"open"/
    "close" and does not stem). Buckets are now tried in _KB order — narrower topics
    ahead of directions — and matched on substrings so "hours"/"closing" both land.
    """
    query = str(a["query"]).lower().replace("?", " ")
    for keywords, source, answer in _KB:
        if any(k in query for k in keywords):
            return {"source": source, "answer": answer}
    return {"source": None, "answer": None,
            "note": "No grounded source — do not make one up."}


_SMS_TEMPLATES = {"appointment_confirmation", "portal_activation", "payment_link",
                  "insurance_card_upload", "directions", "cosmetic_deposit"}


def _d_send_sms(a: dict[str, Any]) -> dict[str, Any]:
    if a["template_id"] not in _SMS_TEMPLATES:
        raise ToolError("UNKNOWN_TEMPLATE",
                        f"template_id must be one of {sorted(_SMS_TEMPLATES)}.")
    _event("sms", dict(a))
    return {"sent": True, "template_id": a["template_id"], "mobile_e164": a["mobile_e164"]}


def _d_send_portal_activation(a: dict[str, Any]) -> dict[str, Any]:
    channel = str(a.get("channel") or "sms")
    _event("portal_activation", {"channel": channel})
    return {"sent": True, "channel": channel, "expires": "72 hours"}


_CALLBACK_QUEUES = {"billing", "clinical", "front_desk", "cosmetic", "records"}


def _d_create_callback_task(a: dict[str, Any]) -> dict[str, Any]:
    if a["queue"] not in _CALLBACK_QUEUES:
        raise ToolError("UNKNOWN_QUEUE", f"queue must be one of {sorted(_CALLBACK_QUEUES)}.")
    sla_hours = {"stat": 1, "urgent": 4}.get(str(a.get("priority") or ""), 24)
    event_id = _event("callback_task", dict(a))
    sla_text = "one hour" if sla_hours == 1 else f"{sla_hours} hours"
    return {
        "task_id": f"cb_{event_id}",
        "queue": a["queue"],
        "sla_hours": sla_hours,
        "spoken_commitment": (
            f"Someone from the {a['queue'].replace('_', ' ')} team will call you back at "
            f"{a['callback_number']} within {sla_text}."
        ),
    }


_TRANSFER_DESTINATIONS = {"patient_support_center", "billing_team", "location_front_desk",
                          "cosmetic_coordinator", "clinical_triage", "records", "on_call"}


def _d_authenticate_for_transfer(a: dict[str, Any]) -> dict[str, Any]:
    return {"authenticated": bool(_session_state().get("verified")),
            "patient_id": _session_state().get("patient_id"),
            "transfer_packet": dict(a)}


def _d_transfer_call(a: dict[str, Any]) -> dict[str, Any]:
    dest = str(a.get("destination") or "patient_support_center")
    if dest not in _TRANSFER_DESTINATIONS:
        raise ToolError("UNKNOWN_DESTINATION",
                        f"destination must be one of {sorted(_TRANSFER_DESTINATIONS)}.")
    _event("transfer", {"destination": dest})
    return {"transferred": True, "destination": dest}


def _d_transfer_to_human(a: dict[str, Any]) -> dict[str, Any]:
    dest = str(a["destination"])
    if dest not in _TRANSFER_DESTINATIONS:
        raise ToolError("UNKNOWN_DESTINATION",
                        f"destination must be one of {sorted(_TRANSFER_DESTINATIONS)}.")
    _event("transfer", dict(a))
    return {"transferred": True, "destination": dest}


def _d_log_call_disposition(a: dict[str, Any]) -> dict[str, Any]:
    _event("call_disposition", dict(a))
    return {"logged": True}


DISPATCH = {
    "resolve_inbound_context": _d_resolve_inbound_context,
    "detect_language": _d_detect_language,
    "identify_patient": _d_identify_patient,
    "verify_identity": _d_verify_identity,
    "get_patient_summary": _d_get_patient_summary,
    "list_locations": _d_list_locations,
    "classify_visit_request": _d_classify_visit_request,
    "find_slots": _d_find_slots,
    "book_appointment": _d_book_appointment,
    "reschedule_appointment": _d_reschedule_appointment,
    "cancel_appointment": _d_cancel_appointment,
    "join_waitlist": _d_join_waitlist,
    "schedule_allergy_service": _d_schedule_allergy_service,
    "check_plan_accepted": _d_check_plan_accepted,
    "run_eligibility_check": _d_run_eligibility_check,
    "capture_insurance_update": _d_capture_insurance_update,
    "get_account_balance": _d_get_account_balance,
    "explain_charge": _d_explain_charge,
    "send_payment_link": _d_send_payment_link,
    "offer_financing": _d_offer_financing,
    "request_fee_waiver": _d_request_fee_waiver,
    "quote_cosmetic_service": _d_quote_cosmetic_service,
    "book_cosmetic_consult": _d_book_cosmetic_consult,
    "request_rx_refill": _d_request_rx_refill,
    "get_results_status": _d_get_results_status,
    "create_clinical_message": _d_create_clinical_message,
    "search_practice_kb": _d_search_practice_kb,
    "send_sms": _d_send_sms,
    "send_portal_activation": _d_send_portal_activation,
    "create_callback_task": _d_create_callback_task,
    "authenticate_for_transfer": _d_authenticate_for_transfer,
    "transfer_call": _d_transfer_call,
    "log_call_disposition": _d_log_call_disposition,
    "transfer_to_human": _d_transfer_to_human,
}


class ToolCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.post("/tools/{tool_name}")
def dispatch_tool(tool_name: str, body: ToolCall) -> dict[str, Any]:
    handler = DISPATCH.get(tool_name)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown tool {tool_name!r} — session and handoff tools are "
            "harness-native and industry tools must be listed in DISPATCH",
        )
    try:
        data = handler(dict(body.arguments or {}))
        return {"ok": True, "data": data, "error_code": None, "patient_safe_message": None}
    except ToolError as e:
        return {"ok": False, "data": None, "error_code": e.code,
                "patient_safe_message": e.message}
    except HTTPException as e:
        return {"ok": False, "data": None, "error_code": f"HTTP_{e.status_code}",
                "patient_safe_message": str(e.detail)}
    except Exception:  # soft-fail: a broken tool must not 500 into the call
        # Diagnostic details (exception type/message, tracebacks, DB errors)
        # stay server-side; callers only ever hear a fixed safe message.
        logger.exception("unhandled error dispatching tool %r", tool_name)
        return {"ok": False, "data": None, "error_code": "INVALID_ARGUMENTS",
                "patient_safe_message": "Something went wrong handling that request. "
                "Please try again or ask for a callback."}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
