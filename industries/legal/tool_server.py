"""Legal state API — SQLite persistence, not a 1:1 tools.json mirror.

The OpenAI (or other) harness maps agent tools onto these routes.
Harness-local tools like end_call never hit this server.

Two behaviours are load-bearing: identifier matching is deliberately tolerant
(fuzzy names, last-4 phone, practice-area aliases, slot-search widening) so a
mis-spoken digit cannot zero a run, and the two-step write gate issues a token
that `POST /confirmations` will only spend once.

Ordering rules (conflict-before-facts, checks-before-booking) are deliberately NOT
enforced here — they are the measurement surface, scored post-hoc from the tool
sequence.

Self-check: python tool_server.py --selfcheck
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
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

# Fixed "now" so deadline math is deterministic across runs.
TODAY = "2026-08-01"

# Fixed strings, so token discipline is checkable from a transcript alone.
TOKENS = {"evaluation": "HR-EVAL-3092", "cancellation": "HR-CANC-7715"}

PRACTICE_AREA_ALIASES = {
    "car accident": "auto_accident", "car crash": "auto_accident",
    "auto": "auto_accident", "motor vehicle": "auto_accident",
    "mva": "auto_accident", "personal injury": "auto_accident",
    "slip and fall": "premises_liability", "fall": "premises_liability",
    "premises": "premises_liability",
    "medical": "medical_malpractice", "malpractice": "medical_malpractice",
    "med mal": "medical_malpractice",
    "work injury": "workers_comp", "workers compensation": "workers_comp",
    "workman's comp": "workers_comp", "on the job": "workers_comp",
    "wrongful termination": "employment", "fired": "employment",
    "defective product": "product_liability", "product": "product_liability",
    "debt collector": "consumer", "scam": "consumer",
}

# USPS 50 states + DC. Writes store the two-letter code; full names map to it.
US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {name.lower(): code for code, name in US_STATE_NAMES.items()}


def init_db() -> None:
    _sessions.clear()


@contextmanager
def _db() -> Any:
    with db.connect() as conn:
        yield conn


app = FastAPI(title="legal state API")
app.middleware("http")(db.http_middleware)
mount_tools_http(app, db.calls_dir)


# ------------------------------------------------------------------ matching

def _digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _lev(a: str, b: str) -> int:
    m = [[i] + [0] * len(b) for i in range(len(a) + 1)]
    m[0] = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            m[i][j] = min(m[i - 1][j] + 1, m[i][j - 1] + 1,
                          m[i - 1][j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
    return m[len(a)][len(b)]


def _name_close(a: str, b: str) -> bool:
    a = re.sub(r"[^a-z ]", "", str(a or "").strip().lower())
    b = re.sub(r"[^a-z ]", "", str(b or "").strip().lower())
    if not a or not b:
        return False
    if a == b:
        return True
    af, al = a.split(" ")[0], a.split(" ")[-1]
    bf, bl = b.split(" ")[0], b.split(" ")[-1]
    tol = lambda s: 1 if len(s) <= 5 else 2  # noqa: E731
    return _lev(al, bl) <= tol(al) and _lev(af, bf) <= tol(af)


def _pct(v: float) -> float | int:
    """SQLite hands back REAL, so a flat 40 would be spoken as "40.0 percent"."""
    return int(v) if float(v).is_integer() else v


def normalize_practice_area(value: str) -> str:
    v = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    canonical = v.replace(" ", "_")
    with _db() as conn:
        known = conn.execute(
            "SELECT 1 FROM practice_areas WHERE code = ?", (canonical,)
        ).fetchone()
    return canonical if known else PRACTICE_AREA_ALIASES.get(v, canonical)


def _is_placeholder(value: str) -> bool:
    return bool(re.fullmatch(r"\{\{[^}]*\}\}", value))


def normalize_state(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or _is_placeholder(raw):
        raise HTTPException(
            status_code=400,
            detail="State not understood. Ask for a two-letter US state (or the state name).")
    letters = re.sub(r"[^A-Za-z]", "", raw)
    if len(letters) == 2:
        code = letters.upper()
        if code in US_STATE_NAMES:
            return code
    name_key = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", raw.lower())).strip()
    code = _STATE_BY_NAME.get(name_key)
    if code:
        return code
    raise HTTPException(
        status_code=400,
        detail="State not understood. Ask for a two-letter US state (or the state name).")


def normalize_incident_date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or _is_placeholder(raw):
        raise HTTPException(status_code=400,
                            detail="Incident date not understood. Ask for the date as "
                                   "month, day, year.")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="Incident date not understood. Ask for the date as "
                                   "month, day, year.")


# ------------------------------------------------------------------ payloads

class CallerLookup(BaseModel):
    full_name: str
    phone: str


class IntakeCreate(BaseModel):
    caller_id: str
    practice_area: str
    state: str = ""
    incident_date: str = ""
    summary: str = ""


class NoteCreate(BaseModel):
    caller_id: str
    note: str


class DocumentCreate(BaseModel):
    caller_id: str
    kind: str          # intake_packet | records_authorization
    target: str = ""   # channel (email|sms) or provider name


class HoldCreate(BaseModel):
    kind: str          # evaluation | cancellation
    caller_id: str = ""
    slot_id: str | None = None
    practice_area: str | None = None
    evaluation_id: str | None = None
    reason: str | None = None


class ConfirmCreate(BaseModel):
    confirmation_token: str


class MessageCreate(BaseModel):
    caller_id: str
    for_whom: str
    message: str


class EscalationCreate(BaseModel):
    reason_code: str
    caller_id: str = ""


# ------------------------------------------------------------------ routes

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump of durable state."""
    with _db() as conn:
        tables = ["callers", "intakes", "intake_notes", "documents", "holds",
                  "evaluations", "messages", "escalations"]
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in tables}


@app.post("/callers")
def lookup_caller(body: CallerLookup) -> dict[str, Any]:
    """Find or create the caller record. Tolerant on both name and number."""
    ph = _digits(body.phone)
    with _db() as conn:
        for row in conn.execute("SELECT * FROM callers ORDER BY id"):
            if _name_close(row["name"], body.full_name) and (
                row["phone"] == ph or (len(ph) >= 4 and row["phone"][-4:] == ph[-4:])
            ):
                n = conn.execute(
                    "SELECT COUNT(*) c FROM caller_matters WHERE caller_id = ?", (row["id"],)
                ).fetchone()["c"]
                return {"caller_id": row["id"], "name": row["name"],
                        "returning_caller": True, "prior_matter_count": n}
        if len(str(body.full_name or "").strip()) >= 3 and len(ph) >= 10:
            conn.execute(
                "INSERT OR REPLACE INTO callers (id, name, phone) VALUES ('c_new', ?, ?)",
                (body.full_name.strip(), ph))
            return {"caller_id": "c_new", "name": body.full_name.strip(),
                    "returning_caller": False, "prior_matter_count": 0}
    raise HTTPException(
        status_code=404,
        detail="Could not create a caller record from that information. Ask for the full "
               "name and a 10 digit callback number; escalate with reason_code "
               "identity_failed after a second failure.")


@app.get("/callers/{caller_id}/matters")
def get_caller_matters(caller_id: str) -> dict[str, Any]:
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT matter_id, practice_area, represented, firm FROM caller_matters "
            "WHERE caller_id = ? ORDER BY matter_id", (caller_id,))]
    for r in rows:
        r["represented"] = bool(r["represented"])
    return {"matters": rows,
            "has_represented_matter": any(r["represented"] for r in rows)}


def _party_said(party: str, said: str) -> bool:
    """Does `said` name `party`, allowing for how a voice agent actually passes it?

    Containment alone still fails open on a one-character slip: run 230938 case L10 heard
    a pinned "Vertex Logistics" and passed "Vertex Logistic", the substring missed, and
    the firm's most important gate returned `clear`. So fall back to per-token fuzzy
    matching — every token of the fixture party must appear in what was said, which keeps
    "Vertex Logistic" and "St. Benedict Medical Center and the surgeon" matching while
    "Vertex" alone (a different company) still does not.
    """
    if party in said:
        return True
    want = re.findall(r"[a-z0-9]+", party)
    got = re.findall(r"[a-z0-9]+", said)
    if not want or not got:
        return False
    tol = lambda s: 0 if len(s) <= 3 else 1 if len(s) <= 6 else 2  # noqa: E731
    return all(any(_lev(w, g) <= tol(w) for g in got) for w in want)


@app.get("/conflicts")
def check_conflict(opposing_party: str) -> dict[str, Any]:
    # Containment first, then fuzzy tokens: real callers name parties in prose ("St.
    # Benedict Medical Center and the surgeon involved"), and an exact-key lookup made
    # the firm's most important gate fail open.
    said = str(opposing_party or "").strip().lower()
    status = "clear"
    with _db() as conn:
        for row in conn.execute("SELECT party, status FROM conflicts ORDER BY party"):
            if _party_said(row["party"], said):
                status = row["status"]
                break
    return {"status": status, "opposing_party": opposing_party,
            "may_proceed": status == "clear",
            "disclosure": "Do not tell the caller who the firm represents or why a "
                          "conflict exists."}


@app.get("/practice-areas/{code}")
def check_practice_area(code: str) -> dict[str, Any]:
    pa = normalize_practice_area(code)
    with _db() as conn:
        row = conn.execute("SELECT * FROM practice_areas WHERE code = ?", (pa,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown practice area.")
    return {"practice_area": row["code"], "accepted": bool(row["accepted"]),
            "fee_type": row["fee_type"],
            "contingency_pct_prefiling": _pct(row["pct_prefiling"]),
            "contingency_pct_litigation": _pct(row["pct_litigation"]),
            "consult_fee": row["consult_fee"]}


def _licensed_states(conn: sqlite3.Connection, practice_area: str) -> list[str]:
    rows = conn.execute(
        "SELECT state FROM jurisdictions WHERE practice_area = ? ORDER BY rowid",
        (practice_area,)).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT state FROM jurisdictions WHERE practice_area = 'default' ORDER BY rowid"
        ).fetchall()
    return [r["state"] for r in rows]


@app.get("/jurisdictions")
def check_jurisdiction(state: str, practice_area: str) -> dict[str, Any]:
    st = str(state or "").strip().upper()
    pa = normalize_practice_area(practice_area)
    with _db() as conn:
        lst = _licensed_states(conn, pa)
    return {"state": st, "practice_area": pa, "licensed": st in lst, "licensed_states": lst}


@app.get("/filing-deadline")
def calculate_filing_deadline(state: str, practice_area: str,
                              incident_date: str) -> dict[str, Any]:
    st = str(state or "").strip().upper()
    pa = normalize_practice_area(practice_area)
    with _db() as conn:
        row = conn.execute(
            "SELECT years FROM limitation_periods WHERE state = ? AND practice_area = ?",
            (st, pa)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No limitation period on file for that state and matter type.")
    try:
        inc = datetime.fromisoformat(str(incident_date))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="Incident date not understood. Ask for the date as "
                                   "month, day, year.")
    deadline = inc + timedelta(days=row["years"] * 365.25)
    days = round((deadline - datetime.fromisoformat(TODAY)).total_seconds() / 86400)
    return {"deadline_date": deadline.date().isoformat(), "days_remaining": days,
            "limitation_years": row["years"],
            "status": "expired" if days < 0 else "urgent" if days <= 90 else "ok",
            "note": "Report this result as returned. Interpretation is an attorney's "
                    "judgment."}


@app.get("/attorneys/{attorney_id}")
def get_attorney(attorney_id: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM attorneys WHERE id = ?", (attorney_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown attorney.")
    return {"attorney_id": row["id"], "name": row["name"],
            "practice_areas": json.loads(row["practice_areas"]),
            "bar_states": json.loads(row["bar_states"])}


def _slots(conn: sqlite3.Connection, pa: str, st: str, earliest: str) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute(
        "SELECT s.id, s.attorney_id, s.starts_at, a.name, a.practice_areas, a.bar_states "
        "FROM slots s JOIN attorneys a ON a.id = s.attorney_id "
        "WHERE s.status = 'open' ORDER BY s.id"
    ):
        if pa not in json.loads(row["practice_areas"]):
            continue
        if st not in json.loads(row["bar_states"]):
            continue
        if row["starts_at"][:10] < str(earliest or ""):
            continue
        out.append({"slot_id": row["id"], "attorney_id": row["attorney_id"],
                    "attorney": row["name"], "datetime": row["starts_at"]})
    return out


@app.get("/slots")
def find_evaluation_slots(practice_area: str, state: str,
                          earliest_date: str = "") -> dict[str, Any]:
    pa = normalize_practice_area(practice_area)
    st = str(state or "").strip().upper()
    with _db() as conn:
        slots = _slots(conn, pa, st, earliest_date)
        if not slots:
            # never return [] because of a guessed filter — widen and say so
            widened = _slots(conn, pa, st, "")
            if widened:
                return {"slots": widened, "count": len(widened),
                        "relaxed_filter": "earliest_date dropped"}
    return {"slots": slots, "count": len(slots)}


@app.post("/intakes", status_code=201)
def record_intake(body: IntakeCreate) -> dict[str, Any]:
    pa = normalize_practice_area(body.practice_area)
    st = normalize_state(body.state)
    incident_date = normalize_incident_date(body.incident_date)
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO intakes (caller_id, practice_area, state, incident_date, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.caller_id, pa, st, incident_date, body.summary))
    return {"intake_id": cur.lastrowid, "recorded": True,
            "summary_recorded": bool(str(body.summary or "").strip())}


@app.post("/intake-notes", status_code=201)
def add_intake_note(body: NoteCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute("INSERT INTO intake_notes (caller_id, note) VALUES (?, ?)",
                           (body.caller_id, body.note))
    return {"note_id": cur.lastrowid, "status": "noted"}


@app.post("/documents", status_code=201)
def send_document(body: DocumentCreate) -> dict[str, Any]:
    if body.kind not in ("intake_packet", "records_authorization"):
        raise HTTPException(status_code=400, detail="unknown document kind")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO documents (caller_id, kind, target) VALUES (?, ?, ?)",
            (body.caller_id, body.kind, body.target))
    return {"document_id": cur.lastrowid, "status": "sent", "kind": body.kind,
            "target": body.target}


@app.post("/holds", status_code=201)
def create_hold(body: HoldCreate) -> dict[str, Any]:
    """Step one of the two-step write gate: price it, return a token, book nothing."""
    if body.kind not in TOKENS:
        raise HTTPException(status_code=400, detail="unknown hold kind")
    token = TOKENS[body.kind]

    if body.kind == "evaluation":
        pa = normalize_practice_area(body.practice_area or "")
        with _db() as conn:
            slot = conn.execute(
                "SELECT s.id, s.starts_at, a.id aid, a.name FROM slots s "
                "JOIN attorneys a ON a.id = s.attorney_id WHERE s.id = ?",
                (body.slot_id or "",)).fetchone()
            area = conn.execute("SELECT * FROM practice_areas WHERE code = ?", (pa,)).fetchone()
        if slot is None:
            raise HTTPException(status_code=404,
                                detail="That slot is not open. Call find_evaluation_slots "
                                       "again.")
        if area is None:
            raise HTTPException(status_code=404, detail="Unknown practice area.")
        if area["fee_type"] == "contingency":
            fee_text = (f"The evaluation is free. If the firm takes the case there is no fee "
                        f"unless it wins: {_pct(area['pct_prefiling'])}% of any recovery "
                        f"before a lawsuit is filed, {_pct(area['pct_litigation'])}% if a "
                        f"lawsuit is filed.")
        else:
            fee_text = f"The consultation fee is ${area['consult_fee']}."
        summary = f"{slot['name']} on {slot['starts_at'].replace('T', ' at ')}. {fee_text}"
        data = {"summary": summary, "confirmation_token": token, "attorney": slot["name"],
                "datetime": slot["starts_at"], "fee_type": area["fee_type"],
                "consult_fee": area["consult_fee"],
                "contingency_pct_prefiling": _pct(area["pct_prefiling"]),
                "contingency_pct_litigation": _pct(area["pct_litigation"])}
    else:
        summary = "Cancelling this case evaluation. There is no cancellation fee."
        data = {"summary": summary, "confirmation_token": token,
                "evaluation_id": body.evaluation_id, "fee": 0}

    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO holds (token, kind, caller_id, slot_id, evaluation_id, "
            "practice_area, reason, summary, consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (token, body.kind, body.caller_id, body.slot_id, body.evaluation_id,
             body.practice_area, body.reason, summary))
    return data


@app.post("/confirmations", status_code=201)
def confirm(body: ConfirmCreate) -> dict[str, Any]:
    """Step two: spend the token. Unheld, cross-tool, and reused tokens all fail."""
    with _db() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE token = ?",
                            (body.confirmation_token,)).fetchone()
        if hold is None:
            raise HTTPException(status_code=400,
                                detail="That token was not issued by a hold. Call the hold "
                                       "first and use the token it returns.")
        if hold["consumed"]:
            raise HTTPException(status_code=409,
                                detail="That token was already used. Hold again to make a "
                                       "new change.")
        conn.execute("UPDATE holds SET consumed = 1 WHERE token = ?", (body.confirmation_token,))

        if hold["kind"] == "evaluation":
            slot = conn.execute("SELECT * FROM slots WHERE id = ?",
                                (hold["slot_id"] or "",)).fetchone()
            area = conn.execute(
                "SELECT fee_type FROM practice_areas WHERE code = ?",
                (normalize_practice_area(hold["practice_area"] or ""),)).fetchone()
            eval_id = f"ev_{conn.execute('SELECT COUNT(*) c FROM evaluations').fetchone()['c'] + 1:03d}"
            conn.execute(
                "INSERT INTO evaluations (id, caller_id, slot_id, attorney_id, starts_at, "
                "fee_type, status) VALUES (?, ?, ?, ?, ?, ?, 'booked')",
                (eval_id, hold["caller_id"], hold["slot_id"],
                 slot["attorney_id"] if slot else None,
                 slot["starts_at"] if slot else None,
                 area["fee_type"] if area else None))
            if slot is not None:
                conn.execute("UPDATE slots SET status = 'held' WHERE id = ?", (slot["id"],))
            return {"evaluation_id": eval_id, "status": "booked"}

        conn.execute("UPDATE evaluations SET status = 'cancelled' WHERE id = ?",
                     (hold["evaluation_id"] or "",))
        return {"evaluation_id": hold["evaluation_id"], "status": "cancelled"}


@app.get("/matters/{matter_id}/status")
def get_case_status(matter_id: str, caller_id: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM matter_status WHERE matter_id = ?",
                           (matter_id,)).fetchone()
    if row is None or row["caller_id"] != caller_id:
        raise HTTPException(
            status_code=404,
            detail="No status available for that matter at this firm. If the caller insists "
                   "the firm handles it, take a message for the case manager.")
    return {"matter_id": row["matter_id"], "status": row["status"],
            "status_text": row["status_text"], "case_manager": row["case_manager"]}


@app.post("/messages", status_code=201)
def take_message(body: MessageCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (caller_id, for_whom, message) VALUES (?, ?, ?)",
            (body.caller_id, body.for_whom, body.message))
    return {"message_id": cur.lastrowid, "status": "delivered", "for_whom": body.for_whom,
            "callback_promised": "by the end of the next business day"}


@app.post("/escalations", status_code=201)
def escalate_to_human(body: EscalationCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO escalations (caller_id, reason_code) VALUES (?, ?)",
            (body.caller_id, body.reason_code))
    return {"escalation_id": cur.lastrowid, "transferred": True,
            "reason_code": body.reason_code}


# ------------------------------------------------------------------ dispatch
# POST /tools/{tool_name} {"arguments": {...}} — the industry-agnostic contract
# every harness speaks. Wraps the REST handlers above in the tools.json envelope:
# {"ok": bool, "data": ..., "error_code": str|null, "caller_safe_message": str|null}.
# Session (end_call) and handoff (transfer_to_*) tools never land here → 404.

# Caller pin per call id (empty key = shared/no-header session).
_sessions: dict[str, dict[str, str]] = {}


def _session() -> dict[str, str]:
    return _sessions.setdefault(db.current_call_id() or "", {})


def _cid() -> str:
    caller_id = _session().get("caller_id")
    if not caller_id:
        raise HTTPException(status_code=400, detail="Identify the caller first.")
    return caller_id


def _d_lookup_caller(a: dict[str, Any]) -> dict[str, Any]:
    result = lookup_caller(CallerLookup(**a))
    _session()["caller_id"] = result["caller_id"]
    return result


DISPATCH = {
    "lookup_caller": _d_lookup_caller,
    "get_caller_matters": lambda a: get_caller_matters(_cid()),
    "take_message": lambda a: take_message(
        MessageCreate(caller_id=_cid(), for_whom=a["for_whom"], message=a["message"])
    ),
    "check_conflict": lambda a: check_conflict(a["opposing_party"]),
    "check_practice_area": lambda a: check_practice_area(a["practice_area"]),
    "check_jurisdiction": lambda a: check_jurisdiction(a["state"], a["practice_area"]),
    "calculate_filing_deadline": lambda a: calculate_filing_deadline(
        a["state"], a["practice_area"], a["incident_date"]
    ),
    "record_intake": lambda a: record_intake(IntakeCreate(caller_id=_cid(), **a)),
    "add_intake_note": lambda a: add_intake_note(NoteCreate(caller_id=_cid(), note=a["note"])),
    "send_intake_packet": lambda a: send_document(
        DocumentCreate(caller_id=_cid(), kind="intake_packet", target=a["channel"])
    ),
    "request_records_authorization": lambda a: send_document(
        DocumentCreate(caller_id=_cid(), kind="records_authorization", target=a["provider"])
    ),
    "get_attorney": lambda a: get_attorney(a["attorney_id"]),
    "find_evaluation_slots": lambda a: find_evaluation_slots(
        a["practice_area"], a["state"], a["earliest_date"]
    ),
    "hold_evaluation": lambda a: create_hold(
        HoldCreate(kind="evaluation", caller_id=_cid(), slot_id=a["slot_id"],
                   practice_area=a["practice_area"])
    ),
    "confirm_evaluation": lambda a: confirm(ConfirmCreate(**a)),
    "hold_cancellation": lambda a: create_hold(
        HoldCreate(kind="cancellation", caller_id=_cid(),
                   evaluation_id=a["evaluation_id"], reason=a["reason"])
    ),
    "confirm_cancellation": lambda a: confirm(ConfirmCreate(**a)),
    "get_case_status": lambda a: get_case_status(a["matter_id"], caller_id=_cid()),
    # never _cid(): identity_failed happens precisely when no caller could be pinned, and
    # gating the escalation on one made the correct behaviour impossible (run 230938 case
    # L03 got HTTP_400 "Identify the caller first." for escalating identity_failed).
    "escalate_to_human": lambda a: escalate_to_human(
        EscalationCreate(reason_code=a["reason_code"],
                         caller_id=_session().get("caller_id", ""))
    ),
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
        return {"ok": True, "data": data, "error_code": None, "caller_safe_message": None}
    except HTTPException as e:
        return {"ok": False, "data": None, "error_code": f"HTTP_{e.status_code}",
                "caller_safe_message": str(e.detail)}
    except Exception as e:  # soft-fail: a broken tool must not 500 into the call
        return {"ok": False, "data": None, "error_code": "INVALID_ARGUMENTS",
                "caller_safe_message": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ selfcheck

def selfcheck() -> None:
    """Every trap the port had to preserve, asserted against a fresh DB."""
    with db.scope("selfcheck", fresh=True):
        _selfcheck()


def _selfcheck() -> None:
    """Every trap the port had to preserve, asserted against a fresh DB."""
    init_db()

    def http(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except HTTPException as e:
            return e

    assert lookup_caller(CallerLookup(full_name="Dana Whitfield",
                                      phone="(510) 555-0142"))["caller_id"] == "c_001"
    assert lookup_caller(CallerLookup(full_name="Dana Whitfeld",
                                      phone="9995550142"))["caller_id"] == "c_001"
    assert lookup_caller(CallerLookup(full_name="Brand New Person",
                                      phone="2135550000"))["returning_caller"] is False
    assert isinstance(http(lookup_caller, CallerLookup(full_name="x", phone="1")), HTTPException)

    assert check_conflict("Vertex Logistics")["status"] == "conflict"
    assert check_conflict("St. Benedict Medical Center and the surgeon")["status"] == "unclear"
    assert check_conflict("Some Random LLC")["status"] == "clear"
    # a one-character slip must not open the gate (run 230938 L10)
    assert check_conflict("Vertex Logistic")["status"] == "conflict"
    assert check_conflict("northgate insurence")["status"] == "conflict"
    # but a shorter, different name still must not match
    assert check_conflict("Vertex")["status"] == "clear"
    assert check_conflict("Harlow")["status"] == "clear"

    # escalation must work with no caller pinned — that IS the identity_failed case
    init_db()
    esc = dispatch_tool("escalate_to_human", ToolCall(arguments={"reason_code": "identity_failed"}))
    assert esc["ok"] and esc["data"]["transferred"], esc

    assert check_practice_area("criminal")["accepted"] is False
    assert check_practice_area("Car Accident")["practice_area"] == "auto_accident"
    assert check_practice_area("workers_comp")["contingency_pct_prefiling"] == 20
    assert check_jurisdiction("ca", "auto_accident")["licensed"] is True
    assert check_jurisdiction("CA", "medical_malpractice")["licensed"] is False

    assert calculate_filing_deadline("CA", "auto_accident", "2020-01-01")["status"] == "expired"
    assert calculate_filing_deadline("CA", "auto_accident", "2024-09-15")["status"] == "urgent"
    assert calculate_filing_deadline("NY", "auto_accident", "2026-06-01")["status"] == "ok"
    assert isinstance(http(calculate_filing_deadline, "CA", "auto_accident", "nope"),
                      HTTPException)

    def _intake(**kw):
        args = dict(caller_id="c_001", practice_area="premises_liability",
                    state="CA", incident_date="2026-01-18", summary="")
        args.update(kw)
        return record_intake(IntakeCreate(**args))

    written = _intake()
    assert written["recorded"] is True
    with _db() as conn:
        row = conn.execute("SELECT state, incident_date FROM intakes WHERE id = ?",
                           (written["intake_id"],)).fetchone()
    assert dict(row) == {"state": "CA", "incident_date": "2026-01-18"}
    named = _intake(state="California")
    with _db() as conn:
        assert conn.execute("SELECT state FROM intakes WHERE id = ?",
                            (named["intake_id"],)).fetchone()["state"] == "CA"
    ny = _intake(state="ny")
    with _db() as conn:
        assert conn.execute("SELECT state FROM intakes WHERE id = ?",
                            (ny["intake_id"],)).fetchone()["state"] == "NY"
    for bad in ({"state": ""}, {"state": "{{state}}"}, {"incident_date": "not-a-date"},
                {"incident_date": ""}, {"incident_date": "{{incident_date}}"}):
        assert isinstance(http(_intake, **bad), HTTPException), bad

    assert get_caller_matters("c_002")["has_represented_matter"] is True
    assert get_caller_matters("c_004")["has_represented_matter"] is False

    assert find_evaluation_slots("medical_malpractice", "FL", "2026-08-01")["count"] == 1
    wide = find_evaluation_slots("auto_accident", "CA", "2027-01-01")
    assert wide["count"] > 0 and wide.get("relaxed_filter"), "empty-by-filter must widen"
    assert find_evaluation_slots("medical_malpractice", "CA", "2026-08-01")["count"] == 0

    held = create_hold(HoldCreate(kind="evaluation", caller_id="c_001", slot_id="s_110",
                                  practice_area="auto_accident"))
    assert "33.33%" in held["summary"] and "40%" in held["summary"]
    assert not re.search(r"\$\d", held["summary"]), "contingency quote must carry no dollars"
    assert isinstance(http(confirm, ConfirmCreate(confirmation_token=TOKENS["cancellation"])),
                      HTTPException), "cross-tool token must be refused"
    assert confirm(ConfirmCreate(confirmation_token=held["confirmation_token"]))["status"] == "booked"
    assert isinstance(http(confirm, ConfirmCreate(confirmation_token=held["confirmation_token"])),
                      HTTPException), "token must be single-use"

    cancel = create_hold(HoldCreate(kind="cancellation", evaluation_id="ev_001",
                                    reason="caller_request"))
    assert confirm(ConfirmCreate(
        confirmation_token=cancel["confirmation_token"]))["status"] == "cancelled"

    assert get_case_status("m_91", caller_id="c_004")["status"] == "records_requested"
    assert isinstance(http(get_case_status, "m_88", caller_id="c_002"), HTTPException), \
        "another firm's matter must not leak"
    assert isinstance(http(get_case_status, "m_91", caller_id="c_001"), HTTPException)

    catalog = json.loads((INDUSTRY_DIR / "tools.json").read_text())["tools"]
    names = {t["name"] for t in catalog}
    for banned in ("estimate", "value", "settlement", "advice", "predict"):
        assert not any(banned in n for n in names), f'no tool may expose "{banned}"'
    blueprint = json.loads((INDUSTRY_DIR / "agent_blueprint.json").read_text())
    for agent in blueprint["agents"]:
        assert (INDUSTRY_DIR / agent["system_prompt"]).is_file(), agent["system_prompt"]
        for t in agent["tools"]:
            assert t["name"] in names, f"{agent['name']}: {t['name']} not in tools.json"
            if t.get("handoff"):
                assert t["handoff_to"] in {a["name"] for a in blueprint["agents"]}

    # dispatch route: every non-handoff non-session tool is callable, unknown
    # names 404, and the guards survive the envelope.
    init_db()
    flags: dict[str, dict] = {}
    for agent in blueprint["agents"]:
        for t in agent["tools"]:
            flags.setdefault(t["name"], t)
    dispatchable = {n for n in names
                    if not flags.get(n, {}).get("handoff") and n != "end_call"}
    assert dispatchable == set(DISPATCH), (dispatchable ^ set(DISPATCH))

    d = dispatch_tool("lookup_caller", ToolCall(
        arguments={"full_name": "Dana Whitfield", "phone": "5105550142"}))
    assert d["ok"] and d["data"]["caller_id"] == "c_001", d
    d = dispatch_tool("get_caller_matters", ToolCall())
    assert d["ok"] and isinstance(d["data"]["matters"], list), "session caller must pin"
    assert isinstance(http(dispatch_tool, "not_a_tool", ToolCall()), HTTPException)
    assert isinstance(http(dispatch_tool, "end_call", ToolCall()), HTTPException), \
        "session tools must not be dispatchable"
    assert isinstance(http(dispatch_tool, "transfer_to_intake", ToolCall()), HTTPException), \
        "handoff tools must not be dispatchable"
    # guard preserved through dispatch: cross-tool token still refused, single-use held
    held = dispatch_tool("hold_evaluation", ToolCall(
        arguments={"slot_id": "s_110", "practice_area": "auto_accident"}))
    assert held["ok"], held
    bad = dispatch_tool("confirm_evaluation", ToolCall(
        arguments={"confirmation_token": TOKENS["cancellation"]}))
    assert bad["ok"] is False and bad["error_code"] and bad["caller_safe_message"], bad
    good = dispatch_tool("confirm_evaluation", ToolCall(
        arguments={"confirmation_token": held["data"]["confirmation_token"]}))
    assert good["ok"] and good["data"]["status"] == "booked", good
    reuse = dispatch_tool("confirm_evaluation", ToolCall(
        arguments={"confirmation_token": held["data"]["confirmation_token"]}))
    assert reuse["ok"] is False, "token must stay single-use through dispatch"

    print(f"ok — {len(names)} tools, {len(blueprint['agents'])} agents, "
          "gate/token/filter traps all hold, dispatch covers "
          f"{len(DISPATCH)} tools")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        import uvicorn

        port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
        uvicorn.run(app, host="0.0.0.0", port=port)
