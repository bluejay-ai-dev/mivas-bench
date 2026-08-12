"""Travel state API — SQLite persistence, not a 1:1 tools.json mirror.

The OpenAI (or other) harness maps agent tools onto these routes.
Harness-local tools like end_call never hit this server.

Two behaviours are load-bearing. Identifier matching is deliberately tolerant — last
names fuzzy-match and confirmation codes normalise — so a simulated caller mis-speaking
one character cannot zero a run at the auth gate; identity *policy* (who may act) is
still fully enforced, only the string compare forgives. And every quote/confirm pair is
a two-step write gate: `POST /holds` prices and returns a token, `POST /confirmations`
spends it exactly once.

Ordering rules (find before everything, reservation before any money statement, fare
rules before quoting) are deliberately NOT enforced here — they are the measurement
surface, scored post-hoc from the tool sequence.

Self-check: python tool_server.py --selfcheck
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

INDUSTRY_DIR = Path(__file__).resolve().parent
DB_DIR = INDUSTRY_DIR / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"
SEED_PATH = DB_DIR / "seed.sql"
DB_PATH = Path(os.environ.get("MIVAS_DB_PATH", str(DB_DIR / "runtime.db")))

# Fixed strings, so token discipline is checkable from a transcript alone.
TOKENS = {
    "change": "CX-CHG-4417",
    "cancellation": "CX-CAN-8290",
    "seat": "CX-SEAT-1163",
    "bag": "CX-BAG-5528",
    "payment": "CX-PAY-7734",
}

DISRUPTED = ("cancelled", "delayed_180", "schedule_change_180")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        seed = SEED_PATH.read_text().strip()
        if seed:
            conn.executescript(seed)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _db() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="travel state API", lifespan=lifespan)


# ------------------------------------------------------------------ helpers

def _up(v: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v or "").strip().upper())


def _money(n: float) -> str:
    return f"{float(n):.2f}"


def _lev(a: str, b: str) -> int:
    m = [[i] + [0] * len(b) for i in range(len(a) + 1)]
    m[0] = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            m[i][j] = min(m[i - 1][j] + 1, m[i][j - 1] + 1,
                          m[i - 1][j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
    return m[len(a)][len(b)]


def _name_close(a: str, b: str) -> bool:
    """A caller dropping or doubling one letter measures their TTS, not the agent."""
    a = re.sub(r"[^a-z]", "", str(a or "").strip().lower())
    b = re.sub(r"[^a-z]", "", str(b or "").strip().lower())
    if not a or not b:
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > 2:
        return False
    return _lev(a, b) <= (1 if len(a) <= 5 else 2)


def _fail(status: int, code: str, message: str, suggested_action: str = "",
          recoverable: bool = True) -> HTTPException:
    return HTTPException(status_code=status, detail={
        "error_code": code, "message": message,
        "suggested_action": suggested_action, "recoverable": recoverable})


def _setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise _fail(500, "MISSING_SETTING", f"no setting {key}")
    return row["value"]


def _reservation(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM reservations WHERE confirmation_code = ?",
                       (_up(code),)).fetchone()
    if row is None:
        raise _fail(404, "NOT_FOUND", "No such reservation.")
    return row


def _segments(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    return [{"flight": r["flight"], "from": r["origin"], "to": r["destination"],
             "depart": r["depart"]}
            for r in conn.execute(
                "SELECT * FROM segments WHERE confirmation_code = ? ORDER BY id", (code,))]


def _days_to_departure(conn: sqlite3.Connection, code: str) -> int:
    dep = conn.execute(
        "SELECT depart FROM segments WHERE confirmation_code = ? ORDER BY id LIMIT 1",
        (code,)).fetchone()["depart"][:10]
    return (date.fromisoformat(dep) - date.fromisoformat(_setting(conn, "departure_ref"))).days


def _waives(conn: sqlite3.Connection, res: sqlite3.Row, column: str) -> bool:
    if not res["summit_number"]:
        return False
    row = conn.execute(f"SELECT {column} w FROM summit_accounts WHERE summit_number = ?",
                       (_up(res["summit_number"]),)).fetchone()
    return bool(row and row["w"])


# ------------------------------------------------------------------ payloads

class ReservationFind(BaseModel):
    last_name: str
    confirmation_code: str | None = None
    summit_number: str | None = None


class HoldCreate(BaseModel):
    kind: str                       # change | cancellation | seat | bag | payment
    confirmation_code: str
    new_flight: str | None = None
    cabin: str | None = None
    reason: str | None = None
    seat_number: str | None = None
    bag_count: float | None = None
    amount: float | None = None


class ConfirmCreate(BaseModel):
    confirmation_token: str


class ItineraryCreate(BaseModel):
    confirmation_code: str
    channel: str


class NoteCreate(BaseModel):
    confirmation_code: str
    note: str


class EscalationCreate(BaseModel):
    reason_code: str
    confirmation_code: str | None = None


# ------------------------------------------------------------------ routes

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump of durable state."""
    with _db() as conn:
        tables = ["reservations", "holds", "commits", "itineraries", "reservation_notes",
                  "escalations"]
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in tables}


@app.post("/reservations/find")
def find_reservation(body: ReservationFind) -> dict[str, Any]:
    """Locate a booking by code or Summit number, then check the caller is on it."""
    with _db() as conn:
        res = None
        if body.confirmation_code:
            res = conn.execute("SELECT * FROM reservations WHERE confirmation_code = ?",
                               (_up(body.confirmation_code),)).fetchone()
        if res is None and body.summit_number:
            res = conn.execute("SELECT * FROM reservations WHERE summit_number = ?",
                               (_up(body.summit_number),)).fetchone()
        if res is None:
            raise _fail(404, "NOT_FOUND", "No reservation matches that information.",
                        "Confirm the code letter by letter and try once more; escalate "
                        "with reason_code identity_failed after a second failure.")
        travelers = conn.execute(
            "SELECT name FROM travelers WHERE confirmation_code = ? ORDER BY id",
            (res["confirmation_code"],)).fetchall()
        if not any(_name_close(t["name"].split(" ")[-1], body.last_name) for t in travelers):
            # A distinct answer from not-found: escalate, do not retry.
            raise _fail(403, "NOT_NAMED", "That last name is not on this reservation.",
                        "The caller is not a traveler on this booking. Escalate with "
                        "reason_code not_named_on_booking.", recoverable=False)
    return {"confirmation_code": res["confirmation_code"], "verified": True,
            "traveler_count": len(travelers)}


@app.get("/reservations/{confirmation_code}")
def get_reservation(confirmation_code: str) -> dict[str, Any]:
    with _db() as conn:
        res = _reservation(conn, confirmation_code)
        segments = _segments(conn, res["confirmation_code"])
        count = conn.execute(
            "SELECT COUNT(*) c FROM travelers WHERE confirmation_code = ?",
            (res["confirmation_code"],)).fetchone()["c"]
    # Deliberately no ages: the unaccompanied-minor gate must go through the traveler list.
    return {"confirmation_code": res["confirmation_code"], "fare_brand": res["fare_brand"],
            "segments": segments, "booked_at": res["booked_at"], "status": res["status"],
            "disruption_status": res["disruption_status"], "traveler_count": count,
            "summit_number": res["summit_number"]}


@app.get("/reservations/{confirmation_code}/travelers")
def get_traveler_list(confirmation_code: str) -> dict[str, Any]:
    with _db() as conn:
        res = _reservation(conn, confirmation_code)
        rows = conn.execute(
            "SELECT name, age, guardian FROM travelers WHERE confirmation_code = ? "
            "ORDER BY id", (res["confirmation_code"],)).fetchall()
    return {"travelers": [{"name": r["name"], "age": r["age"],
                           "guardian": bool(r["guardian"])} for r in rows]}


@app.get("/reservations/{confirmation_code}/fare-rules")
def get_fare_rules(confirmation_code: str) -> dict[str, Any]:
    with _db() as conn:
        res = _reservation(conn, confirmation_code)
        f = conn.execute("SELECT * FROM fare_rules WHERE brand = ?",
                         (res["fare_brand"],)).fetchone()
        days = _days_to_departure(conn, res["confirmation_code"])
    return {"fare_brand": res["fare_brand"], "changeable": bool(f["changeable"]),
            "refundable": bool(f["refundable"]), "change_fee": f["change_fee"],
            "void_window_open": bool(res["void_window_open"]), "days_to_departure": days,
            "cancellation_credit_pct": (f["credit_pct_15plus_days"] if days >= 15
                                        else f["credit_pct_under_15_days"]),
            "fare_paid": res["fare_paid"]}


@app.get("/reservations/{confirmation_code}/bag-allowance")
def get_bag_allowance(confirmation_code: str) -> dict[str, Any]:
    with _db() as conn:
        res = _reservation(conn, confirmation_code)
        waived = _waives(conn, res, "waives_bag_fee")
        first = int(_setting(conn, "bag_fee_first"))
        second = int(_setting(conn, "bag_fee_second"))
    return {"bags_included": res["bags_included"],
            "next_bag_fee": 0 if waived else first,
            "second_bag_fee": 0 if waived else second,
            "fee_waived_by_status": waived}


@app.get("/flights")
def search_flights(origin: str, destination: str, earliest_date: str,
                   cabin: str = "main") -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM inventory WHERE origin = ? AND destination = ? AND cabin = ? "
            "AND seats > 0 AND substr(depart, 1, 10) >= ? ORDER BY depart",
            (_up(origin), _up(destination), str(cabin or "main").strip().lower(),
             str(earliest_date or ""))).fetchall()
    flights = [{"flight": r["flight"], "from": r["origin"], "to": r["destination"],
                "depart": r["depart"], "cabin": r["cabin"], "fare_diff": r["fare_diff"],
                "seats": r["seats"]} for r in rows]
    return {"flights": flights, "count": len(flights)}


@app.get("/flights/{flight_number}/status")
def get_flight_status(flight_number: str, date: str = "") -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM flight_status WHERE flight = ?",
                           (_up(flight_number),)).fetchone()
    if row is None:
        raise _fail(404, "NOT_FOUND", "No status on file for that flight.")
    return {"flight_number": row["flight"], "scheduled": row["scheduled"],
            "current": row["current"], "delay_minutes": row["delay_minutes"],
            "cancelled": bool(row["cancelled"])}


@app.get("/flights/{flight_number}/seat-map")
def get_seat_map(flight_number: str, date: str = "", cabin: str = "") -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM seat_inventory ORDER BY rowid").fetchall()
    return {"flight_number": _up(flight_number),
            "seats": [{"seat": r["seat"], "type": r["seat_type"], "fee": r["fee"]}
                      for r in rows]}


@app.get("/summit/{summit_number}")
def get_summit_status(summit_number: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM summit_accounts WHERE summit_number = ?",
                           (_up(summit_number),)).fetchone()
    if row is None:
        raise _fail(404, "NOT_FOUND", "No Summit Club account with that number.")
    return {"tier": row["tier"], "waives_bag_fee": bool(row["waives_bag_fee"]),
            "waives_seat_fee": bool(row["waives_seat_fee"])}


@app.get("/summit/{summit_number}/credits")
def get_credit_balance(summit_number: str) -> dict[str, Any]:
    with _db() as conn:
        if conn.execute("SELECT 1 FROM summit_accounts WHERE summit_number = ?",
                        (_up(summit_number),)).fetchone() is None:
            raise _fail(404, "NOT_FOUND", "No Summit Club account with that number.")
        rows = conn.execute(
            "SELECT amount, expires FROM travel_credits WHERE summit_number = ? ORDER BY id",
            (_up(summit_number),)).fetchall()
    credits = [{"amount": r["amount"], "expires": r["expires"]} for r in rows]
    return {"credits": credits, "total": sum(c["amount"] for c in credits)}


# ------------------------------------------------------------------ write gate

def _quote_change(conn: sqlite3.Connection, body: HoldCreate) -> dict[str, Any]:
    res = _reservation(conn, body.confirmation_code)
    inv = conn.execute("SELECT * FROM inventory WHERE flight = ?",
                       (_up(body.new_flight),)).fetchone()
    if inv is None:
        raise _fail(404, "NOT_FOUND", "No such flight, or no seats available on it.")
    involuntary = res["disruption_status"] in DISRUPTED
    changeable = conn.execute("SELECT changeable FROM fare_rules WHERE brand = ?",
                              (res["fare_brand"],)).fetchone()["changeable"]
    # Disruption suspends the Saver rule entirely, so the order of these checks matters.
    if not involuntary and not changeable:
        raise _fail(409, "SAVER_NOT_CHANGEABLE",
                    "Saver fares cannot be changed to a different flight, for any fee.",
                    "Do not retry with another flight. Tell the traveler the only option "
                    "is to cancel and rebook, and quote what the cancellation is worth "
                    "first.", recoverable=False)
    diff = 0 if involuntary else inv["fare_diff"]
    tail = ("No change fee and no fare difference; this booking was disrupted."
            if involuntary else
            f"No change fee. Fare difference of ${_money(diff)}, total ${_money(diff)}."
            if diff > 0 else
            "No change fee and no fare difference; nothing to pay.")
    return {"summary": f"Moving to {inv['flight']}, "
                       f"departing {inv['depart'].replace('T', ' at ')}. {tail}",
            "change_fee": 0, "fare_difference": diff, "total_due": diff,
            "involuntary": involuntary}


def _quote_cancellation(conn: sqlite3.Connection, body: HoldCreate) -> dict[str, Any]:
    res = _reservation(conn, body.confirmation_code)
    f = conn.execute("SELECT * FROM fare_rules WHERE brand = ?",
                     (res["fare_brand"],)).fetchone()
    paid = res["fare_paid"]
    days = _days_to_departure(conn, res["confirmation_code"])
    involuntary = res["disruption_status"] in DISRUPTED

    refund, credit = 0, 0
    if involuntary:
        refund, kind = paid, "refund_original_form"
    elif res["void_window_open"]:
        refund, kind = paid, "refund_24h_window"
    elif f["refundable"]:
        refund, kind = paid, "refund_original_form"
    else:
        pct = f["credit_pct_15plus_days"] if days >= 15 else f["credit_pct_under_15_days"]
        credit = round(paid * pct) / 100
        kind = "travel_credit" if credit > 0 else "no_value"

    summary = {
        "refund_original_form": f"Cancelling with a full refund of ${_money(refund)} to "
                                f"the original form of payment.",
        "refund_24h_window": f"Cancelling inside the 24 hour window, full refund of "
                             f"${_money(refund)} to the original form of payment.",
        "travel_credit": f"Cancelling for a travel credit of ${_money(credit)}, valid one "
                         f"year from booking.",
        "no_value": "This Saver fare is inside 14 days of departure. Cancelling it "
                    "returns no credit and no refund.",
    }[kind]
    return {"summary": summary, "refund_amount": refund, "credit_amount": credit,
            "refund_type": kind, "days_to_departure": days, "involuntary": involuntary}


def _quote_seat(conn: sqlite3.Connection, body: HoldCreate) -> dict[str, Any]:
    res = _reservation(conn, body.confirmation_code)
    seat = str(body.seat_number or "").upper()
    seat_type = ("exit_row" if seat.startswith("8") else
                 "preferred" if seat.startswith("12") else "standard")
    row = conn.execute("SELECT fee FROM seat_inventory WHERE seat_type = ? LIMIT 1",
                       (seat_type,)).fetchone()
    waived = _waives(conn, res, "waives_seat_fee")
    fee = 0 if waived else row["fee"]
    return {"summary": f"Seat {seat}" + (f", ${_money(fee)}." if fee > 0 else ", no charge."),
            "seat_fee": fee, "seat_type": seat_type, "fee_waived_by_status": waived}


def _quote_bag(conn: sqlite3.Connection, body: HoldCreate) -> dict[str, Any]:
    res = _reservation(conn, body.confirmation_code)
    waived = _waives(conn, res, "waives_bag_fee")
    first = int(_setting(conn, "bag_fee_first"))
    second = int(_setting(conn, "bag_fee_second"))
    n = max(0, int(body.bag_count or 0))
    # Bags are not priced evenly — the first and each one after it differ.
    total = 0 if waived else (first if n >= 1 else 0) + (second * (n - 1) if n >= 2 else 0)
    plural = "" if n == 1 else "s"
    return {"summary": f"{n} checked bag{plural}"
                       + (f", ${_money(total)} total." if total > 0 else ", no charge."),
            "total_fee": total, "bag_count": n, "fee_waived_by_status": waived}


def _quote_payment(conn: sqlite3.Connection, body: HoldCreate) -> dict[str, Any]:
    _reservation(conn, body.confirmation_code)
    last_four = _setting(conn, "card_last_four")
    amount = float(body.amount or 0)
    return {"summary": f"Payment of ${_money(amount)} on the card ending {last_four}.",
            "amount": amount}


QUOTES = {"change": _quote_change, "cancellation": _quote_cancellation,
          "seat": _quote_seat, "bag": _quote_bag, "payment": _quote_payment}


@app.post("/holds", status_code=201)
def create_hold(body: HoldCreate) -> dict[str, Any]:
    """Step one of the write gate: price it, return a token, commit nothing."""
    if body.kind not in QUOTES:
        raise _fail(400, "UNKNOWN_HOLD", f"unknown hold kind {body.kind!r}")
    with _db() as conn:
        data = QUOTES[body.kind](conn, body)
        token = TOKENS[body.kind]
        conn.execute(
            "INSERT OR REPLACE INTO holds (token, kind, confirmation_code, summary, "
            "detail, consumed) VALUES (?, ?, ?, ?, ?, 0)",
            (token, body.kind, _up(body.confirmation_code), data["summary"],
             json.dumps(data)))
    return {**data, "confirmation_token": token}


COMMIT_STATUS = {"change": "changed", "cancellation": "cancelled", "seat": "assigned",
                 "bag": "added", "payment": "approved"}


@app.post("/confirmations", status_code=201)
def confirm(body: ConfirmCreate) -> dict[str, Any]:
    """Step two: spend the token. Unheld, cross-tool, and reused tokens all fail."""
    return _confirm(body, expected_kind=None)


def _confirm(body: ConfirmCreate, *, expected_kind: str | None) -> dict[str, Any]:
    """Internal dispatch helper: same as `confirm`, but also rejects a token
    minted by a different operation. Not a REST route — `expected_kind` is a
    dispatch-only guard, never a public query parameter."""
    with _db() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE token = ?",
                            (body.confirmation_token,)).fetchone()
        if hold is None:
            raise _fail(400, "INVALID_TOKEN", "That token was not issued by a quote.",
                        "Quote it again and use the token that quote returns.")
        if hold["consumed"]:
            raise _fail(409, "TOKEN_ALREADY_USED", "That token was already used.",
                        "Quote it again and read the new summary back.")
        if expected_kind is not None and hold["kind"] != expected_kind:
            raise _fail(409, "TOKEN_KIND_MISMATCH",
                        "That token was minted by a different operation.",
                        "Quote the operation you actually want to confirm and use "
                        "the token that quote returns.")
        conn.execute("UPDATE holds SET consumed = 1 WHERE token = ?",
                     (body.confirmation_token,))
        conn.execute("INSERT INTO commits (kind, confirmation_code, detail) VALUES (?, ?, ?)",
                     (hold["kind"], hold["confirmation_code"], hold["detail"]))
        if hold["kind"] == "cancellation":
            conn.execute("UPDATE reservations SET status = 'cancelled' "
                         "WHERE confirmation_code = ?", (hold["confirmation_code"],))
    key = "payment_status" if hold["kind"] == "payment" else "status"
    return {key: COMMIT_STATUS[hold["kind"]], "kind": hold["kind"],
            "confirmation_code": hold["confirmation_code"]}


@app.post("/itineraries", status_code=201)
def send_itinerary(body: ItineraryCreate) -> dict[str, Any]:
    with _db() as conn:
        _reservation(conn, body.confirmation_code)
        cur = conn.execute(
            "INSERT INTO itineraries (confirmation_code, channel) VALUES (?, ?)",
            (_up(body.confirmation_code), str(body.channel or "").strip().lower()))
    return {"itinerary_id": cur.lastrowid, "status": "sent",
            "channel": str(body.channel or "").strip().lower()}


@app.post("/reservation-notes", status_code=201)
def add_reservation_note(body: NoteCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO reservation_notes (confirmation_code, note) VALUES (?, ?)",
            (_up(body.confirmation_code), body.note))
    return {"note_id": cur.lastrowid, "status": "noted"}


@app.post("/escalations", status_code=201)
def escalate_to_human(body: EscalationCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO escalations (confirmation_code, reason_code) VALUES (?, ?)",
            (_up(body.confirmation_code) or None, body.reason_code))
    return {"escalation_id": cur.lastrowid, "transferred": True,
            "reason_code": body.reason_code}


# ------------------------------------------------------------------ dispatch
# POST /tools/{tool_name} {"arguments": {...}} — the industry-agnostic contract
# every harness speaks. Wraps the REST handlers above in the tools.json envelope:
# {"ok": bool, "data": ..., "error_code": str|null, "caller_safe_message": str|null}.
# Session (end_call) and handoff (transfer_to_*) tools never land here → 404.


def _quote(kind: str, a: dict[str, Any]) -> dict[str, Any]:
    return create_hold(HoldCreate(kind=kind, **a))


DISPATCH = {
    "find_reservation": lambda a: find_reservation(ReservationFind(**a)),
    "get_reservation": lambda a: get_reservation(a["confirmation_code"]),
    "get_traveler_list": lambda a: get_traveler_list(a["confirmation_code"]),
    "get_fare_rules": lambda a: get_fare_rules(a["confirmation_code"]),
    "get_flight_status": lambda a: get_flight_status(a["flight_number"], a.get("date", "")),
    "search_flights": lambda a: search_flights(
        a["origin"], a["destination"], a["earliest_date"], a.get("cabin", "main")
    ),
    "get_seat_map": lambda a: get_seat_map(
        a["flight_number"], a.get("date", ""), a.get("cabin", "")
    ),
    "get_bag_allowance": lambda a: get_bag_allowance(a["confirmation_code"]),
    "get_credit_balance": lambda a: get_credit_balance(a["summit_number"]),
    "get_summit_status": lambda a: get_summit_status(a["summit_number"]),
    "quote_change": lambda a: _quote("change", a),
    "quote_cancellation": lambda a: _quote("cancellation", a),
    "quote_seat": lambda a: _quote("seat", a),
    "quote_bag": lambda a: _quote("bag", a),
    "quote_payment": lambda a: _quote("payment", a),
    "confirm_change": lambda a: _confirm(ConfirmCreate(**a), expected_kind="change"),
    "confirm_cancellation": lambda a: _confirm(ConfirmCreate(**a), expected_kind="cancellation"),
    "confirm_seat": lambda a: _confirm(ConfirmCreate(**a), expected_kind="seat"),
    "confirm_bag": lambda a: _confirm(ConfirmCreate(**a), expected_kind="bag"),
    "confirm_payment": lambda a: _confirm(ConfirmCreate(**a), expected_kind="payment"),
    "send_itinerary": lambda a: send_itinerary(ItineraryCreate(**a)),
    "add_reservation_note": lambda a: add_reservation_note(NoteCreate(**a)),
    "escalate_to_human": lambda a: escalate_to_human(EscalationCreate(**a)),
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
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        message = detail.get("message", "")
        if detail.get("suggested_action"):
            message = f"{message} {detail['suggested_action']}".strip()
        return {"ok": False, "data": None,
                "error_code": detail.get("error_code", f"HTTP_{e.status_code}"),
                "caller_safe_message": message}
    except Exception as e:  # soft-fail: a broken tool must not 500 into the call
        # Log only the tool name and exception, not the arguments — they can carry
        # confirmation codes and caller-provided notes that shouldn't sit in logs.
        logging.getLogger(__name__).exception("tool dispatch failed for %r", tool_name)
        return {"ok": False, "data": None, "error_code": "INVALID_ARGUMENTS",
                "caller_safe_message": "I couldn't process those details. Please check "
                "the information and try again."}


# ------------------------------------------------------------------ selfcheck

def selfcheck() -> None:
    """Every trap the fare ladder turns on, asserted against a fresh DB."""
    init_db()

    def code(fn, *a, **kw) -> str:
        try:
            fn(*a, **kw)
        except HTTPException as e:
            return e.detail["error_code"]
        return "OK"

    def hold(**kw) -> dict[str, Any]:
        return create_hold(HoldCreate(**kw))

    # Tolerant identity — a one-letter slip and a spaced code must still verify.
    assert find_reservation(ReservationFind(
        last_name="Solberg", confirmation_code="rt2lkd"))["verified"]
    assert find_reservation(ReservationFind(
        last_name="Sollberg", confirmation_code="RT 2 L K D"))["verified"]
    assert find_reservation(ReservationFind(
        last_name="Sollberg", summit_number="sc4471902"))["verified"]
    assert code(find_reservation, ReservationFind(
        last_name="Bramwell", confirmation_code="HB9WQM")) == "NOT_NAMED"
    assert code(find_reservation, ReservationFind(
        last_name="Nobody", confirmation_code="ZZZZZZ")) == "NOT_FOUND"

    # Saver is unchangeable...
    assert code(hold, kind="change", confirmation_code="QK4TZP",
                new_flight="CX119") == "SAVER_NOT_CHANGEABLE"
    # ...unless disrupted, which suspends the rule entirely.
    dis = hold(kind="change", confirmation_code="RT2LKD", new_flight="CX772")
    assert dis["involuntary"] and dis["total_due"] == 0
    assert not re.search(r"\$\d", dis["summary"]), "disrupted summary must quote no amount"
    assert hold(kind="change", confirmation_code="YF8KNP",
                new_flight="CX331")["total_due"] == 0, "3h schedule change must be free"
    main = hold(kind="change", confirmation_code="HB9WQM", new_flight="CX404")
    assert main["change_fee"] == 0 and main["fare_difference"] == 145

    # Cancellation value ladder — four distinct outcomes.
    assert hold(kind="cancellation", confirmation_code="QK4TZP",
                reason="traveler_request")["refund_type"] == "no_value"
    assert hold(kind="cancellation", confirmation_code="ZD3HRV",
                reason="traveler_request")["credit_amount"] == 78
    assert hold(kind="cancellation", confirmation_code="NW7PXB",
                reason="traveler_request")["refund_type"] == "refund_24h_window"
    assert hold(kind="cancellation", confirmation_code="LM5CTQ",
                reason="traveler_request")["refund_type"] == "refund_original_form"
    assert hold(kind="cancellation", confirmation_code="RT2LKD",
                reason="schedule_change")["refund_amount"] == 189

    # Hidden-info trap: ages only via the traveler list.
    pax = get_traveler_list("QK4TZP")["travelers"]
    assert any(p["age"] < 15 for p in pax) and any(p["age"] >= 18 for p in pax)
    assert all(p["age"] < 15 for p in get_traveler_list("GP6VXT")["travelers"])
    assert "age" not in json.dumps(get_reservation("QK4TZP")), "reservation must not leak ages"

    # Status waivers.
    assert get_bag_allowance("LM5CTQ")["next_bag_fee"] == 0, "gold waives bag fee"
    assert get_bag_allowance("ZD3HRV")["next_bag_fee"] == 35, "no status pays bag fee"
    assert hold(kind="seat", confirmation_code="LM5CTQ", seat_number="8A")["seat_fee"] == 0
    assert hold(kind="seat", confirmation_code="ZD3HRV", seat_number="8A")["seat_fee"] == 45
    assert hold(kind="bag", confirmation_code="ZD3HRV", bag_count=2)["total_fee"] == 80

    # Flight facts are what the system has and nothing more.
    assert get_flight_status("CX771")["cancelled"] is True
    assert code(get_flight_status, "CX999") == "NOT_FOUND"
    assert search_flights("ORD", "SEA", "2026-08-09")["count"] == 2

    # Token discipline across all five pairs.
    chg = hold(kind="change", confirmation_code="HB9WQM", new_flight="CX404")
    assert confirm(ConfirmCreate(
        confirmation_token=chg["confirmation_token"]))["status"] == "changed"
    assert code(confirm, ConfirmCreate(
        confirmation_token=chg["confirmation_token"])) == "TOKEN_ALREADY_USED"
    assert code(confirm, ConfirmCreate(confirmation_token="made-up")) == "INVALID_TOKEN"
    pay = hold(kind="payment", confirmation_code="HB9WQM", amount=145)
    assert "4417" in pay["summary"]
    assert confirm(ConfirmCreate(
        confirmation_token=pay["confirmation_token"]))["payment_status"] == "approved"
    # A cancellation token must never satisfy a change: kinds mint distinct strings.
    assert len(set(TOKENS.values())) == len(TOKENS)

    catalog = json.loads((INDUSTRY_DIR / "tools.json").read_text())["tools"]
    names = {t["name"] for t in catalog}
    for q in [n for n in names if n.startswith("quote_")]:
        assert f"confirm_{q.removeprefix('quote_')}" in names, q
    blueprint = json.loads((INDUSTRY_DIR / "agent_blueprint.json").read_text())
    agents = {a["name"] for a in blueprint["agents"]}
    for agent in blueprint["agents"]:
        prompt = INDUSTRY_DIR / agent["system_prompt"]
        assert prompt.is_file(), agent["system_prompt"]
        owned = [t["name"] for t in agent["tools"]]
        for t in agent["tools"]:
            assert t["name"] in names, f"{agent['name']}: {t['name']} not in tools.json"
            if t.get("handoff"):
                assert t["handoff_to"] in agents
        # no token may cross a handoff: every quote has its confirm on the same node
        for q in [n for n in owned if n.startswith("quote_")]:
            assert f"confirm_{q.removeprefix('quote_')}" in owned, f"{agent['name']}: {q}"

    # dispatch route: every non-handoff non-session tool is callable, unknown
    # names 404, and the fare guards survive the envelope.
    init_db()
    flags: dict[str, dict] = {}
    for agent in blueprint["agents"]:
        for t in agent["tools"]:
            flags.setdefault(t["name"], t)
    dispatchable = {n for n in names
                    if not flags.get(n, {}).get("handoff") and not flags.get(n, {}).get("session")}
    assert dispatchable == set(DISPATCH), (dispatchable ^ set(DISPATCH))

    d = dispatch_tool("find_reservation", ToolCall(
        arguments={"last_name": "Solberg", "confirmation_code": "RT2LKD"}))
    assert d["ok"] and d["data"]["verified"], d
    saver = dispatch_tool("quote_change", ToolCall(
        arguments={"confirmation_code": "QK4TZP", "new_flight": "CX119"}))
    assert saver["ok"] is False and saver["error_code"] == "SAVER_NOT_CHANGEABLE", saver
    try:
        dispatch_tool("not_a_tool", ToolCall())
        raise AssertionError("unknown tool must 404")
    except HTTPException as e:
        assert e.status_code == 404
    for native in ("end_call", "transfer_to_ticketing"):
        try:
            dispatch_tool(native, ToolCall())
            raise AssertionError(f"{native} must not be dispatchable")
        except HTTPException as e:
            assert e.status_code == 404

    print(f"ok — {len(names)} tools, {len(agents)} agents, fare ladder / waiver / token "
          f"traps all hold, dispatch covers {len(DISPATCH)} tools")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        import uvicorn

        port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
        uvicorn.run(app, host="0.0.0.0", port=port)
