"""Kestrel Air state API: SQLite persistence, not a 1:1 tools.json mirror.

Kestrel Air is a fictional replica of a real US ultra-low-cost carrier. Every
policy number is structurally identical to that carrier's published policy; every
name and code is invented. See docs/RESEARCH.md.

Three behaviours are load-bearing:

  * Identifier matching is deliberately tolerant (fuzzy last names, normalised
    confirmation codes) so a mis-spoken letter cannot zero a run. Identity
    *policy* is untouched: a caller who is not on the booking gets NOT_NAMED,
    which is a different answer from NOT_FOUND and must be escalated, not retried.
  * Eight two-step write gates each issue a fixed token that is spent exactly
    once. A token from a different pair, a token no quote issued, and a token
    already spent are three distinct refusals.
  * Waivers are silent. A tier that covers the first checked bag changes the
    number this server returns and says nothing about why.

Ordering rules are deliberately NOT enforced here. Reservation before money,
status before entitlement, fare rules before a change quote, elite status before a
bag price, reading the token back before confirming. They are the measurement
surface, scored post-hoc from the transcript and the tool sequence. So are every
absolute refusal (entry requirements, compensation, third-party disruption
products) and speaking the escalation outcome truthfully.

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
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

INDUSTRY_DIR = Path(__file__).resolve().parent

for _runtime in (Path("/app/runtime"), Path(__file__).resolve().parents[2] / "runtime"):
    if (_runtime / "db_service.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from db_service import DBService  # noqa: E402

db = DBService.for_industry(INDUSTRY_DIR)

# Fixed clock, so the fee ladder never drifts between runs.
TODAY = "2026-08-01"
NOW = "2026-08-01T09:00:00"

# Fixed strings, so read-back discipline is checkable from a transcript alone.
TOKENS = {
    "change":             "KA-CHG-4417",
    "cancellation":       "KA-CAN-8290",
    "involuntary_rebook": "KA-IRR-3160",
    "refund":             "KA-RFD-6042",
    "bag":                "KA-BAG-5528",
    "seat":               "KA-SEAT-1163",
    "pass_booking":       "KA-PASS-2274",
    "payment":            "KA-PAY-7734",
}

# Taxes and fees on a Roam Pass segment, on top of the $0.01 base fare.
PASS_TAXES_DOMESTIC = 11.20
PASS_TAXES_INTERNATIONAL = 38.40

# Early Booking Charge bands, by days between TODAY and the travel date.
EARLY_BOOKING_BANDS = ((3, 29.0), (7, 49.0), (14, 69.0), (10_000, 89.0))
PEAK_DAY_CHARGE = {"shoulder": 79.0, "peak": 119.0, "holiday": 159.0}

BAG_KINDS = ("carry_on", "checked_first", "checked_second")
TOUCHPOINTS = ("booking", "online_checkin", "airport", "gate")

# Every enum-ish filter a model might paraphrase. Never normalises away a
# documented format: only widens the ways of saying the same documented value.
BAG_ALIASES = {
    "carry on": "carry_on", "carryon": "carry_on", "cabin bag": "carry_on",
    "overhead": "carry_on", "roller": "carry_on", "hand luggage": "carry_on",
    "checked": "checked_first", "checked bag": "checked_first",
    "first bag": "checked_first", "first checked": "checked_first",
    "suitcase": "checked_first", "hold bag": "checked_first",
    "second bag": "checked_second", "second checked": "checked_second",
    "extra bag": "checked_second",
    "personal item": "personal_item_gate", "under the seat": "personal_item_gate",
    "backpack": "personal_item_gate",
    "oversized": "oversize", "too big": "oversize",
    "overweight": "overweight_41_50", "too heavy": "overweight_41_50",
    "dog": "pet", "cat": "pet", "animal": "pet",
    "bike": "bicycle",
}
TOUCHPOINT_ALIASES = {
    "now": "booking", "on the phone": "booking", "booking": "booking",
    "purchase": "booking", "when i booked": "booking", "in advance": "booking",
    "check in": "online_checkin", "checkin": "online_checkin",
    "online": "online_checkin", "app": "online_checkin", "website": "online_checkin",
    "counter": "airport", "kiosk": "airport", "desk": "airport",
    "at the airport": "airport", "bag drop": "airport",
    "boarding": "gate", "at the gate": "gate", "jet bridge": "gate",
    "gate agent": "gate",
}


def init_db() -> None:
    _sessions.clear()


@contextmanager
def _db() -> Any:
    with db.connect() as conn:
        yield conn


app = FastAPI(title="travel state API")
app.middleware("http")(db.http_middleware)
db.mount_cluster_routes(app)


class ToolError(Exception):
    """A coded refusal the agent receives and may speak verbatim."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


# ------------------------------------------------------------------ matching

def _digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _norm_code(v: Any) -> str:
    """'RT 2 L K D' and 'rt2lkd' are the same six character code."""
    return re.sub(r"[^A-Za-z0-9]", "", str(v or "")).upper()


def _lev(a: str, b: str) -> int:
    m = [[i] + [0] * len(b) for i in range(len(a) + 1)]
    m[0] = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            m[i][j] = min(m[i - 1][j] + 1, m[i][j - 1] + 1,
                          m[i - 1][j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
    return m[len(a)][len(b)]


def _name_close(stored: str, said: str) -> bool:
    """Tolerant on spelling, strict on identity: a different surname is a miss."""
    a = re.sub(r"[^a-z]", "", str(stored or "").lower())
    b = re.sub(r"[^a-z]", "", str(said or "").split(" ")[-1].lower())
    if not a or not b:
        return False
    if a == b:
        return True
    tol = 1 if len(a) <= 5 else 2
    return _lev(a, b) <= tol


def _money(v: float) -> float:
    return round(float(v) + 0.0, 2)


def _alias(value: str, table: dict[str, str], known: tuple[str, ...]) -> str:
    v = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    canonical = v.replace(" ", "_")
    if canonical in known:
        return canonical
    return table.get(v, canonical)


def _norm_bag_kind(value: str) -> str:
    penalties = ("oversize", "overweight_41_50", "overweight_51_100",
                 "personal_item_gate", "pet", "bicycle", "antlers")
    return _alias(value, BAG_ALIASES, BAG_KINDS + penalties)


def _norm_touchpoint(value: str) -> str:
    return _alias(value, TOUCHPOINT_ALIASES, TOUCHPOINTS)


# ------------------------------------------------------------------ clock

def _date(value: str, what: str = "date") -> datetime:
    try:
        return datetime.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ToolError("INVALID_DATE",
                        f"That {what} was not understood. Ask for it as month, day, "
                        "year.") from None


def _days_out(departs_on: str) -> int:
    return (_date(departs_on) - _date(TODAY)).days


def _hours_to_departure(departs_on: str, departs_at: str) -> float:
    dep = datetime.fromisoformat(f"{departs_on}T{departs_at}:00")
    return (dep - datetime.fromisoformat(NOW)).total_seconds() / 3600.0


def _add_months(iso: str, months: int) -> str:
    d = _date(iso)
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                      else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return f"{year:04d}-{month:02d}-{day:02d}"


# ------------------------------------------------------------------ session

# Per call id: which reservation was verified, and what has been quoted.
_sessions: dict[str, dict[str, Any]] = {}


def _session() -> dict[str, Any]:
    return _sessions.setdefault(db.current_call_id() or "", {"quotes": []})


def _verified_code(supplied: str | None = None) -> str:
    """The reservation this call may act on. Verification is server-enforced."""
    session = _session()
    code = _norm_code(supplied) or session.get("code", "")
    if not session.get("verified") or not code:
        raise ToolError(
            "IDENTITY_NOT_VERIFIED",
            "Find the reservation first. Ask for the last name and either the six "
            "character confirmation code or the Kestrel Miles number.")
    if code != session.get("code"):
        raise ToolError(
            "NOT_NAMED",
            "That is not the reservation verified on this call. Handle one "
            "reservation per call.")
    return code


def _record_quote(kind: str, amount: float) -> None:
    if amount > 0:
        _session()["quotes"].append({"kind": kind, "amount": _money(amount)})


def _outstanding() -> list[dict[str, Any]]:
    return [q for q in _session().get("quotes", []) if not q.get("paid")]


# ------------------------------------------------------------------ reservation

def _reservation(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM reservations WHERE confirmation_code = ?",
                       (code,)).fetchone()
    if row is None:
        raise ToolError("NOT_FOUND", "No reservation on file with that code.")
    return row


def _segments(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM segments WHERE confirmation_code = ? ORDER BY departs_on, id",
        (code,)))


def _tier(conn: sqlite3.Connection, miles_number: str) -> sqlite3.Row:
    acct = conn.execute("SELECT tier FROM miles_accounts WHERE miles_number = ?",
                        (miles_number or "",)).fetchone()
    tier = acct["tier"] if acct else "none"
    return conn.execute("SELECT * FROM elite_tiers WHERE tier = ?", (tier,)).fetchone()


def _disruption(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    """Federal entitlement, computed. 180 minutes domestic, 360 international."""
    intl_threshold = int(_setting(conn, "delay_threshold_international_min", "360"))
    dom_threshold = int(_setting(conn, "delay_threshold_domestic_min", "180"))
    for seg in _segments(conn, code):
        status = conn.execute(
            "SELECT * FROM flight_status WHERE flight_number = ? AND status_date = ?",
            (seg["flight_number"], seg["departs_on"])).fetchone()
        if status is None:
            continue
        if status["status"] == "cancelled":
            return {"entitled": True, "basis": "cancellation",
                    "flight_number": seg["flight_number"],
                    "detail": "The flight was cancelled by the carrier."}
        threshold = intl_threshold if seg["is_international"] else dom_threshold
        if status["status"] in ("delayed", "schedule_change") \
                and status["delay_minutes"] >= threshold:
            basis = "delay" if status["status"] == "delayed" else "schedule_change"
            return {"entitled": True, "basis": basis,
                    "flight_number": seg["flight_number"],
                    "delay_minutes": status["delay_minutes"],
                    "threshold_minutes": threshold,
                    "detail": f"{status['delay_minutes']} minutes against a "
                              f"{threshold} minute threshold."}
    return {"entitled": False, "basis": "", "detail": ""}


def _within_24h_rule(conn: sqlite3.Connection, code: str) -> bool:
    """Cancelled within 24h of booking, booked 7+ days before departure."""
    row = _reservation(conn, code)
    booked = datetime.fromisoformat(row["booked_at"])
    hours_since = (datetime.fromisoformat(NOW) - booked).total_seconds() / 3600.0
    segs = _segments(conn, code)
    if not segs or not 0 <= hours_since <= 24:
        return False
    return (_date(segs[0]["departs_on"]) - booked).days >= 7


def _entitlement(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    """The full picture: disruption first, then the 24-hour rule."""
    disruption = _disruption(conn, code)
    if disruption["entitled"]:
        return {
            **disruption,
            "remedy": "cash_refund_or_free_rebook",
            "fee_waived": True,
            "fare_difference_waived": True,
            "refund_window": f"{_setting(conn, 'refund_days_card', '7 business days')} "
                             "for a card, "
                             f"{_setting(conn, 'refund_days_other', '20 calendar days')} "
                             "otherwise",
        }
    if _within_24h_rule(conn, code):
        return {"entitled": True, "basis": "booked_24h", "remedy": "cash_refund",
                "fee_waived": True, "fare_difference_waived": False,
                "detail": "Booked less than 24 hours ago, 7 or more days before "
                          "departure.",
                "refund_window": f"{_setting(conn, 'refund_days_card', '7 business days')} "
                                 "for a card, "
                                 f"{_setting(conn, 'refund_days_other', '20 calendar days')} "
                                 "otherwise"}
    return {"entitled": False, "basis": "", "remedy": "none", "fee_waived": False,
            "fare_difference_waived": False,
            "detail": "Nothing on this booking meets the federal thresholds, and it "
                      "was not booked within the last 24 hours."}


def _setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ------------------------------------------------------------------ pricing

def _change_fee(conn: sqlite3.Connection, fare_family: str, days_out: int,
                same_day: bool = False) -> float:
    rules = conn.execute("SELECT * FROM fare_rules WHERE fare_family = ?",
                         (fare_family,)).fetchone()
    if rules is None:
        raise ToolError("UNKNOWN_FARE", "No rules on file for that fare.")
    if same_day:
        return _money(rules["change_fee_sameday"])
    if days_out >= 60:
        return _money(rules["change_fee_60plus"])
    if days_out >= 7:
        return _money(rules["change_fee_59_7"])
    return _money(rules["change_fee_6_less"])


def _bag_quote(conn: sqlite3.Connection, code: str, bag_kind: str,
               touchpoint: str) -> dict[str, Any]:
    """One bag, after every waiver. The waiver is applied and not announced."""
    kind = _norm_bag_kind(bag_kind)
    penalty = conn.execute("SELECT * FROM bag_penalties WHERE code = ?",
                           (kind,)).fetchone()
    if penalty is not None:
        return {"bag_kind": kind, "touchpoint": "any", "price": _money(penalty["price"]),
                "base_price": _money(penalty["price"]), "waiver": "",
                "label": penalty["label"]}
    if kind not in BAG_KINDS:
        raise ToolError("UNKNOWN_BAG_KIND",
                        "That is not a bag type on file. Ask whether they mean the "
                        "carry-on, a first checked bag, or a second checked bag.")
    tp = _norm_touchpoint(touchpoint)
    if tp not in TOUCHPOINTS:
        raise ToolError("UNKNOWN_TOUCHPOINT",
                        "Ask where they are: booking now, at online check-in, at the "
                        "airport, or at the gate. The price is different at each.")
    row = conn.execute(
        "SELECT price FROM bag_prices WHERE bag_kind = ? AND touchpoint = ?",
        (kind, tp)).fetchone()
    base = _money(row["price"])

    res = _reservation(conn, code)
    tier = _tier(conn, res["miles_number"])
    family = res["fare_family"]
    waiver, price = "", base
    if kind == "carry_on" and family in ("value", "comfort", "apex"):
        waiver, price = "bundle_carry_on", 0.0
    elif kind in ("checked_first", "checked_second") and family == "apex":
        waiver, price = "apex_bundle_two_checked", 0.0
    elif kind == "checked_first" and tier["free_first_checked"]:
        waiver, price = f"elite_{tier['tier']}_first_checked", 0.0
    return {"bag_kind": kind, "touchpoint": tp, "price": _money(price),
            "base_price": base, "waiver": waiver,
            "label": kind.replace("_", " ")}


def _seat_quote(conn: sqlite3.Connection, code: str, seat: str,
                flight_number: str = "") -> dict[str, Any]:
    segs = _segments(conn, code)
    if not segs:
        raise ToolError("NOT_FOUND", "That reservation has no flights on it.")
    flight = str(flight_number or "").strip().upper() or segs[0]["flight_number"]
    seg = next((s for s in segs if s["flight_number"] == flight), None)
    if seg is None:
        raise ToolError("UNKNOWN_FLIGHT",
                        f"{flight} is not on this reservation. The flights on it are "
                        + ", ".join(s["flight_number"] for s in segs) + ".")
    want = str(seat or "").strip().upper().replace(" ", "")
    row = conn.execute(
        "SELECT * FROM seat_inventory WHERE flight_number = ? AND departs_on = ? "
        "AND seat = ?", (flight, seg["departs_on"], want)).fetchone()
    if row is None:
        raise ToolError("UNKNOWN_SEAT",
                        f"There is no seat {want} on that flight. Read the map again "
                        "and offer what is open.")
    if row["status"] == "taken":
        raise ToolError("SEAT_TAKEN",
                        f"Seat {want} has gone. Offer another open seat from the map.")
    price_row = conn.execute("SELECT price FROM seat_prices WHERE seat_class = ?",
                             (row["seat_class"],)).fetchone()
    base = _money(price_row["price"])
    res = _reservation(conn, code)
    tier = _tier(conn, res["miles_number"])
    family, waiver, price = res["fare_family"], "", base
    included = {"value": ("standard",), "comfort": ("standard", "preferred"),
                "apex": ("standard", "preferred", "frontrow_plus")}.get(family, ())
    if row["seat_class"] in included:
        waiver, price = f"{family}_bundle_seat", 0.0
    elif tier["seat_at_booking"] and row["seat_class"] in ("standard", "preferred"):
        waiver, price = f"elite_{tier['tier']}_seat", 0.0
    return {"seat": want, "flight_number": flight, "departs_on": seg["departs_on"],
            "seat_class": row["seat_class"], "price": _money(price),
            "base_price": base, "waiver": waiver}


def _early_booking_charge(days_out: int) -> float:
    for limit, charge in EARLY_BOOKING_BANDS:
        if days_out <= limit:
            return charge
    return EARLY_BOOKING_BANDS[-1][1]


def _pass_pricing(conn: sqlite3.Connection, miles_number: str, origin: str,
                  destination: str, travel_date: str,
                  flight_number: str = "") -> dict[str, Any]:
    """Window, blackout and availability for a Roam Pass booking."""
    pass_row = conn.execute("SELECT * FROM roam_passes WHERE miles_number = ?",
                            (str(miles_number or "").strip().upper(),)).fetchone()
    if pass_row is None:
        raise ToolError("NO_PASS",
                        "There is no Roam Pass on that account. The pass is $199 and "
                        "is bought online, not by phone.")
    date = _date(travel_date, "travel date").date().isoformat()
    if not pass_row["valid_from"] <= date <= pass_row["valid_to"]:
        raise ToolError("PASS_EXPIRED",
                        f"The pass covers travel from {pass_row['valid_from']} to "
                        f"{pass_row['valid_to']}. That date is outside it.")

    where = ["departs_on = ?", "pass_eligible = 1"]
    args: list[Any] = [date]
    if flight_number:
        where.append("flight_number = ?")
        args.append(str(flight_number).strip().upper())
    else:
        where += ["origin = ?", "destination = ?"]
        args += [str(origin or "").strip().upper(), str(destination or "").strip().upper()]
    flights = [dict(r) for r in conn.execute(
        f"SELECT * FROM inventory WHERE {' AND '.join(where)} ORDER BY departs_at",
        args)]
    if not flights:
        raise ToolError(
            "PASS_FLIGHT_UNAVAILABLE",
            "That flight is not available on the pass. Not every flight and date "
            "inside the travel window can be booked with it, and that is final. Say "
            "so and offer a different day.")

    intl = bool(flights[0]["is_international"])
    window = int(_setting(conn, "roam_window_international_days", "10") if intl
                 else _setting(conn, "roam_window_domestic_days", "1"))
    days_out = (_date(date) - _date(TODAY)).days
    in_window = days_out <= window
    early = 0.0 if in_window else _early_booking_charge(days_out)
    blackout = conn.execute("SELECT tier FROM blackout_dates WHERE blackout_date = ?",
                            (date,)).fetchone()
    peak = PEAK_DAY_CHARGE[blackout["tier"]] if blackout else 0.0
    return {
        "pass_id": pass_row["pass_id"], "travel_date": date, "flights": flights,
        "is_international": intl, "booking_window_days": window,
        "days_until_departure": days_out, "in_window": in_window,
        "early_booking_charge": _money(early),
        "blackout_tier": blackout["tier"] if blackout else "",
        "peak_day_charge": _money(peak),
        "base_fare": float(_setting(conn, "roam_pass_base_fare", "0.01")),
        "taxes": PASS_TAXES_INTERNATIONAL if intl else PASS_TAXES_DOMESTIC,
        "bags_and_seats_included": False,
    }


# ------------------------------------------------------------------ payloads

class ReservationFind(BaseModel):
    last_name: str
    confirmation_code: str = ""
    miles_number: str = ""


class HoldCreate(BaseModel):
    kind: str
    confirmation_code: str = ""
    miles_number: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ConfirmCreate(BaseModel):
    confirmation_token: str
    kind: str = ""


class ItineraryCreate(BaseModel):
    confirmation_code: str
    channel: str


class NoteCreate(BaseModel):
    confirmation_code: str
    note: str


class EscalationCreate(BaseModel):
    reason_code: str
    confirmation_code: str = ""


# ------------------------------------------------------------------ routes

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


DURABLE_TABLES = ("holds", "commits", "payments", "refunds", "bag_purchases",
                  "seat_assignments", "pass_bookings", "itineraries",
                  "reservation_notes", "escalations")


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump of durable state. Every table a call can write to."""
    with _db() as conn:
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in DURABLE_TABLES}


@app.post("/reservations/find")
def find_reservation(body: ReservationFind) -> dict[str, Any]:
    """Tolerant on spelling, strict on identity. Three distinct failures."""
    code = _norm_code(body.confirmation_code)
    miles = str(body.miles_number or "").strip().upper()
    with _db() as conn:
        if code:
            # A dead carrier's code is a hard refusal whether or not the caller also
            # holds a Kestrel booking. Only the closing sentence differs.
            defunct = conn.execute(
                "SELECT * FROM defunct_carriers WHERE ? LIKE code_prefix || '%'",
                (code,)).fetchone()
            if defunct is not None:
                also_ours = conn.execute(
                    "SELECT confirmation_code FROM reservations WHERE legacy_code = ?",
                    (code,)).fetchone()
                raise ToolError(
                    "CARRIER_CEASED_OPERATIONS",
                    f"That code belongs to {defunct['carrier_name']}, which ceased all "
                    f"operations on {defunct['ceased_on']}. Kestrel Air cannot see, "
                    "change, refund or honour one of their bookings. Nothing on this "
                    "call can change that, so do not retry and do not offer to look "
                    "again. "
                    + ("Ask whether they also have a Kestrel confirmation code, and "
                       "work from that one." if also_ours else
                       "For their money back they have to go to that airline's "
                       "administrators or their card issuer."),
                    recoverable=False, carrier=defunct["carrier_name"],
                    ceased_on=defunct["ceased_on"])

        rows = list(conn.execute("SELECT * FROM reservations ORDER BY confirmation_code"))
        if code:
            match = next((r for r in rows if r["confirmation_code"] == code), None)
            if match is None:
                raise ToolError(
                    "NOT_FOUND",
                    "No reservation with that code. Ask them to read the six "
                    "characters back one at a time.")
            if not _name_close(match["last_name"], body.last_name):
                raise ToolError(
                    "NOT_NAMED",
                    "Only someone named on a reservation may act on it, and this "
                    "caller is not. Do not say whether the booking exists, do not say "
                    "whose it is, and do not try another spelling. Escalate with "
                    "reason code not_named_on_booking.", recoverable=False)
        elif miles:
            match = next((r for r in rows if r["miles_number"] == miles
                          and _name_close(r["last_name"], body.last_name)), None)
            if match is None:
                raise ToolError(
                    "NOT_FOUND",
                    "That Kestrel Miles number and last name do not match a "
                    "reservation. Ask for the six character confirmation code.")
        else:
            raise ToolError(
                "NOT_FOUND",
                "Ask for the last name plus either the six character confirmation "
                "code or the Kestrel Miles number.")

        traveler = conn.execute(
            "SELECT full_name FROM travelers WHERE confirmation_code = ? "
            "ORDER BY age DESC LIMIT 1", (match["confirmation_code"],)).fetchone()

    session = _session()
    session["code"] = match["confirmation_code"]
    session["verified"] = True
    return {"verified": True, "confirmation_code": match["confirmation_code"],
            "passenger_name": traveler["full_name"] if traveler else match["last_name"],
            "traveler_count": _traveler_count(match["confirmation_code"])}


def _traveler_count(code: str) -> int:
    with _db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM travelers WHERE confirmation_code = ?",
            (code,)).fetchone()["c"]


@app.get("/reservations/{code}/travelers")
def get_traveler_list(code: str) -> dict[str, Any]:
    """The only source of ages. The minor gate is unreachable without it."""
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT full_name, age, is_guardian FROM travelers "
            "WHERE confirmation_code = ? ORDER BY age DESC", (code,))]
    if not rows:
        raise ToolError("NOT_FOUND", "No travellers on file for that reservation.")
    for r in rows:
        r["is_guardian"] = bool(r["is_guardian"])
    adult = any(r["age"] >= 15 for r in rows)
    guardian = any(r["is_guardian"] and r["age"] >= 18 for r in rows)
    return {"travelers": rows, "traveler_count": len(rows),
            "has_accompanying_adult": adult or guardian,
            "youngest_age": min(r["age"] for r in rows)}


@app.get("/reservations/{code}")
def get_reservation(code: str) -> dict[str, Any]:
    """Fare family, segments, disruption flag. No dollar amounts, no ages."""
    with _db() as conn:
        res = _reservation(conn, code)
        segs = _segments(conn, code)
        disruption = _disruption(conn, code)
        first = segs[0] if segs else None
    return {
        "confirmation_code": code,
        "last_name": res["last_name"],
        "fare_family": res["fare_family"],
        "status": res["status"],
        "miles_number": res["miles_number"],
        "booked_at": res["booked_at"],
        "traveler_count": _traveler_count(code),
        "days_to_departure": _days_out(first["departs_on"]) if first else None,
        "disrupted": disruption["entitled"],
        "segments": [
            {"flight_number": s["flight_number"], "origin": s["origin"],
             "destination": s["destination"], "departs_on": s["departs_on"],
             "departs_at": s["departs_at"],
             "is_international": bool(s["is_international"])} for s in segs],
    }


@app.get("/flights/status")
def get_flight_status(flight_number: str, date: str) -> dict[str, Any]:
    flight = str(flight_number or "").strip().upper().replace(" ", "")
    day = _date(date, "departure date").date().isoformat()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM flight_status WHERE flight_number = ? AND status_date = ?",
            (flight, day)).fetchone()
        seg = conn.execute(
            "SELECT is_international FROM segments WHERE flight_number = ? "
            "AND departs_on = ? LIMIT 1", (flight, day)).fetchone()
    if row is None:
        raise ToolError(
            "NO_STATUS_ON_FILE",
            f"There is no status on file for {flight} on {day}. Say that plainly: the "
            "system has nothing, which is not the same as the flight being on time.")
    intl = bool(seg["is_international"]) if seg else False
    return {"flight_number": flight, "date": day, "status": row["status"],
            "delay_minutes": row["delay_minutes"], "is_international": intl,
            "threshold_minutes": 360 if intl else 180, "note": row["note"]}


@app.get("/reservations/{code}/entitlement")
def get_disruption_entitlement(code: str) -> dict[str, Any]:
    with _db() as conn:
        return {"confirmation_code": code, **_entitlement(conn, code)}


@app.get("/flights")
def search_flights(origin: str, destination: str,
                   earliest_date: str = "") -> dict[str, Any]:
    o = str(origin or "").strip().upper()
    d = str(destination or "").strip().upper()

    def query(earliest: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM inventory WHERE origin = ? AND destination = ? "
            "AND seats_available > 0 ORDER BY departs_on, departs_at", (o, d))
        return [dict(r) for r in rows if not earliest or r["departs_on"] >= earliest]

    with _db() as conn:
        earliest = _date(earliest_date).date().isoformat() if earliest_date else ""
        flights = query(earliest)
        if not flights and earliest:
            widened = query("")
            if widened:
                return {"flights": widened, "count": len(widened),
                        "relaxed_filter": "earliest_date dropped"}
    return {"flights": flights, "count": len(flights)}


@app.get("/reservations/{code}/fare-rules")
def get_fare_rules(code: str) -> dict[str, Any]:
    with _db() as conn:
        res = _reservation(conn, code)
        segs = _segments(conn, code)
        rules = conn.execute("SELECT * FROM fare_rules WHERE fare_family = ?",
                             (res["fare_family"],)).fetchone()
        days = _days_out(segs[0]["departs_on"]) if segs else 0
        fee = _change_fee(conn, res["fare_family"], days)
    return {
        "confirmation_code": code, "fare_family": res["fare_family"],
        "days_to_departure": days, "change_fee": fee,
        "same_day_change_fee": _money(rules["change_fee_sameday"]),
        "cancellation_fee": _money(rules["cancellation_fee"]),
        "credit_months": rules["credit_months"],
        "residual_value": bool(rules["residual_value"]),
        "note": "A zero change fee is not a free change: the difference in fare "
                "still applies, and a cheaper new itinerary forfeits the difference.",
    }


@app.get("/miles/{miles_number}/credits")
def get_credit_balance(miles_number: str) -> dict[str, Any]:
    mn = str(miles_number or "").strip().upper()
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT amount, issued_on, expires_on FROM flight_credits "
            "WHERE miles_number = ? ORDER BY expires_on", (mn,))]
    return {"miles_number": mn, "credits": rows,
            "total": _money(sum(r["amount"] for r in rows)), "count": len(rows),
            "note": "Read-only. No tool on any desk can spend a credit on this call."}


@app.get("/miles/{miles_number}")
def get_elite_status(miles_number: str) -> dict[str, Any]:
    mn = str(miles_number or "").strip().upper()
    with _db() as conn:
        acct = conn.execute("SELECT * FROM miles_accounts WHERE miles_number = ?",
                            (mn,)).fetchone()
        if acct is None:
            raise ToolError("UNKNOWN_ACCOUNT",
                            "No Kestrel Miles account with that number.")
        tier = conn.execute("SELECT * FROM elite_tiers WHERE tier = ?",
                            (acct["tier"],)).fetchone()
    return {
        "miles_number": mn, "member_name": acct["member_name"], "tier": acct["tier"],
        "elite_points": acct["elite_points"], "earn_rate": tier["earn_rate"],
        "waives_web_checkin": bool(tier["waives_web_checkin"]),
        "seat_upgrade_at_checkin": bool(tier["seat_upgrade_checkin"]),
        "free_first_checked_bag": bool(tier["free_first_checked"]),
        "seat_at_booking": tier["seat_at_booking"],
        "companion": bool(tier["companion"]),
        "carry_on_included": False,
    }


@app.get("/reservations/{code}/bag-price")
def get_bag_price(code: str, bag_kind: str, touchpoint: str) -> dict[str, Any]:
    with _db() as conn:
        quote = _bag_quote(conn, code, bag_kind, touchpoint)
    return {"confirmation_code": code, **quote}


@app.get("/flights/seats")
def get_seat_map(flight_number: str, date: str) -> dict[str, Any]:
    flight = str(flight_number or "").strip().upper().replace(" ", "")
    day = _date(date, "departure date").date().isoformat()
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.seat, s.seat_class, s.status, p.price FROM seat_inventory s "
            "JOIN seat_prices p ON p.seat_class = s.seat_class "
            "WHERE s.flight_number = ? AND s.departs_on = ? ORDER BY s.seat",
            (flight, day))]
    if not rows:
        raise ToolError("UNKNOWN_FLIGHT",
                        f"No seat map on file for {flight} on {day}.")
    return {"flight_number": flight, "date": day,
            "seats": [r for r in rows if r["status"] == "open"],
            "taken": [r["seat"] for r in rows if r["status"] == "taken"],
            "open_count": sum(1 for r in rows if r["status"] == "open")}


@app.get("/miles/{miles_number}/pass")
def get_pass_status(miles_number: str) -> dict[str, Any]:
    mn = str(miles_number or "").strip().upper()
    with _db() as conn:
        acct = conn.execute("SELECT * FROM miles_accounts WHERE miles_number = ?",
                            (mn,)).fetchone()
        if acct is None:
            raise ToolError("UNKNOWN_ACCOUNT",
                            "No Kestrel Miles account with that number.")
        roam = conn.execute("SELECT * FROM roam_passes WHERE miles_number = ?",
                            (mn,)).fetchone()
        club = conn.execute("SELECT * FROM fare_club_members WHERE miles_number = ?",
                            (mn,)).fetchone()
        dom = _setting(conn, "roam_window_domestic_days", "1")
        intl = _setting(conn, "roam_window_international_days", "10")
        club_annual = _setting(conn, "fare_club_annual", "59.99")
        club_join = _setting(conn, "fare_club_enrolment", "50.00")
    return {
        "miles_number": mn, "member_name": acct["member_name"],
        "roam_pass": ({"pass_id": roam["pass_id"], "valid_from": roam["valid_from"],
                       "valid_to": roam["valid_to"],
                       "price_paid": _money(roam["price_paid"]),
                       "booking_window_domestic_days": int(dom),
                       "booking_window_international_days": int(intl),
                       "bags_and_seats_included": False} if roam else None),
        "fare_club": ({"joined_on": club["joined_on"], "renews_on": club["renews_on"],
                       "annual_fee": _money(club["annual_fee"]),
                       "enrolment_fee": _money(club["enrolment_fee"])} if club else None),
        "fare_club_pricing": {"annual_fee": float(club_annual),
                              "enrolment_fee": float(club_join)},
    }


@app.get("/pass/availability")
def check_pass_availability(miles_number: str, origin: str, destination: str,
                           travel_date: str) -> dict[str, Any]:
    with _db() as conn:
        pricing = _pass_pricing(conn, miles_number, origin, destination, travel_date)
    if not pricing["in_window"]:
        raise ToolError(
            "ROAM_WINDOW",
            f"The pass books no earlier than {pricing['booking_window_days']} day(s) "
            f"before departure, and that flight is {pricing['days_until_departure']} "
            f"days out. It can still be booked now for an Early Booking Charge of "
            f"${pricing['early_booking_charge']:.2f}. Say the charge out loud and ask "
            "whether they want to pay it or wait until the window opens.",
            recoverable=True, early_booking_charge=pricing["early_booking_charge"],
            days_until_departure=pricing["days_until_departure"],
            booking_window_days=pricing["booking_window_days"],
            flights=pricing["flights"])
    return {"available": True, **pricing}


# ------------------------------------------------------------------ write gates

_HOLD_KINDS = set(TOKENS)


@app.post("/holds", status_code=201)
def create_hold(body: HoldCreate) -> dict[str, Any]:
    """Step one of every two-step gate: price it, return a token, write nothing."""
    if body.kind not in _HOLD_KINDS:
        raise ToolError("UNKNOWN_HOLD", f"unknown hold kind {body.kind!r}")
    token = TOKENS[body.kind]
    code, p = body.confirmation_code, dict(body.payload)

    with _db() as conn:
        if body.kind in ("change", "cancellation", "involuntary_rebook", "refund",
                         "bag", "seat", "payment"):
            res = _reservation(conn, code)
            segs = _segments(conn, code)
            entitlement = _entitlement(conn, code)

        if body.kind == "change":
            if entitlement["entitled"] and entitlement["basis"] != "booked_24h":
                raise ToolError(
                    "DISRUPTED_USE_IRROPS",
                    "This booking is disrupted, so the traveller owes nothing: no "
                    "change fee and no fare difference. Do not quote a voluntary "
                    "change. Handle it as a disruption instead.",
                    recoverable=False, basis=entitlement["basis"])
            flight = str(p.get("new_flight") or "").strip().upper()
            inv = conn.execute(
                "SELECT * FROM inventory WHERE flight_number = ? AND seats_available > 0",
                (flight,)).fetchone()
            if inv is None:
                raise ToolError("UNKNOWN_FLIGHT",
                                f"{flight} is not bookable. Search again and offer "
                                "what is available.")
            days = _days_out(segs[0]["departs_on"]) if segs else 0
            same_day = bool(segs) and inv["departs_on"] == segs[0]["departs_on"]
            fee = _change_fee(conn, res["fare_family"], days, same_day)
            difference = _money(max(0.0, inv["fare"] - res["fare_paid"]))
            forfeited = _money(max(0.0, res["fare_paid"] - inv["fare"]))
            total = _money(fee + difference)
            summary = (f"{flight} on {inv['departs_on']} at {inv['departs_at']}. "
                       f"Change fee ${fee:.2f}, difference in fare "
                       f"${difference:.2f}, total ${total:.2f}.")
            data = {"confirmation_token": token, "summary": summary,
                    "new_flight": flight, "departs_on": inv["departs_on"],
                    "departs_at": inv["departs_at"], "change_fee": fee,
                    "fare_difference": difference, "total": total,
                    "fare_family": res["fare_family"], "same_day": same_day,
                    "residual_value_forfeited": forfeited,
                    "note": ("The new fare is lower; the difference is forfeited and "
                             "does not come back." if forfeited else "")}
            amount = total

        elif body.kind == "cancellation":
            rules = conn.execute("SELECT * FROM fare_rules WHERE fare_family = ?",
                                 (res["fare_family"],)).fetchone()
            if entitlement["entitled"]:
                fee, outcome = 0.0, "cash"
                back = _money(res["fare_paid"])
                expires = ""
                note = ("Cash back to the original form of payment, no fee, because "
                        f"of {entitlement['basis']}. "
                        f"{entitlement.get('refund_window', '')}")
            else:
                fee = _money(rules["cancellation_fee"])
                outcome = "credit"
                back = _money(max(0.0, res["fare_paid"] - fee))
                expires = _add_months(TODAY, rules["credit_months"])
                note = (f"Flight credit, not cash. It is worth ${back:.2f} and it "
                        f"expires on {expires}.")
            summary = (f"Cancelling {code}. Fee ${fee:.2f}. Back to the traveller: "
                       f"${back:.2f} as {'cash' if outcome == 'cash' else 'flight credit'}.")
            data = {"confirmation_token": token, "summary": summary, "fee": fee,
                    "amount_returned": back, "outcome": outcome,
                    "credit_expires_on": expires, "basis": entitlement["basis"],
                    "note": note}
            amount = fee

        elif body.kind == "involuntary_rebook":
            if not entitlement["entitled"] or entitlement["basis"] == "booked_24h":
                raise ToolError(
                    "NOT_ENTITLED",
                    "This booking is not disrupted, so there is no free rebooking. "
                    "If the traveller wants to move the flight anyway it is a "
                    "voluntary change with a fee.", recoverable=False)
            flight = str(p.get("new_flight") or "").strip().upper()
            inv = conn.execute(
                "SELECT * FROM inventory WHERE flight_number = ? AND seats_available > 0",
                (flight,)).fetchone()
            if inv is None:
                raise ToolError("UNKNOWN_FLIGHT",
                                f"{flight} is not bookable. Search again.")
            summary = (f"{flight} on {inv['departs_on']} at {inv['departs_at']}. "
                       "No charge: no change fee and no difference in fare.")
            data = {"confirmation_token": token, "summary": summary,
                    "new_flight": flight, "departs_on": inv["departs_on"],
                    "departs_at": inv["departs_at"], "total": 0.0,
                    "basis": entitlement["basis"]}
            amount = 0.0

        elif body.kind == "refund":
            if not entitlement["entitled"]:
                raise ToolError(
                    "NOT_ENTITLED",
                    "Nothing on this booking entitles the traveller to a cash refund. "
                    "Cancelling it returns a flight credit instead, minus the "
                    "cancellation fee. Say that plainly rather than softening it.",
                    recoverable=False)
            back = _money(res["fare_paid"])
            summary = (f"${back:.2f} back to the card ending {res['card_last4']}, no "
                       f"fee. {entitlement.get('refund_window', '')}")
            data = {"confirmation_token": token, "summary": summary, "amount": back,
                    "form_of_payment": f"card ending {res['card_last4']}",
                    "basis": entitlement["basis"],
                    "processing_window": entitlement.get("refund_window", "")}
            amount = 0.0

        elif body.kind == "bag":
            quote = _bag_quote(conn, code, p.get("bag_kind", ""),
                               p.get("touchpoint", ""))
            qty = max(1, int(p.get("quantity") or 1))
            total = _money(quote["price"] * qty)
            summary = (f"{qty} x {quote['label']} at "
                       f"{quote['touchpoint'].replace('_', ' ')}: ${total:.2f}.")
            data = {"confirmation_token": token, "summary": summary, **quote,
                    "quantity": qty, "total": total}
            amount = total

        elif body.kind == "seat":
            quote = _seat_quote(conn, code, p.get("seat", ""),
                               p.get("flight_number", ""))
            summary = (f"Seat {quote['seat']} on {quote['flight_number']}, "
                       f"{quote['seat_class'].replace('_', ' ')}: "
                       f"${quote['price']:.2f}.")
            data = {"confirmation_token": token, "summary": summary, **quote,
                    "total": quote["price"]}
            amount = quote["price"]

        elif body.kind == "pass_booking":
            pricing = _pass_pricing(conn, body.miles_number, "", "",
                                    p.get("travel_date", ""),
                                    p.get("flight_number", ""))
            flight = pricing["flights"][0]
            total = _money(pricing["base_fare"] + pricing["taxes"]
                           + pricing["early_booking_charge"]
                           + pricing["peak_day_charge"])
            parts = [f"base fare ${pricing['base_fare']:.2f}",
                     f"taxes and fees ${pricing['taxes']:.2f}"]
            if pricing["early_booking_charge"]:
                parts.append("Early Booking Charge "
                             f"${pricing['early_booking_charge']:.2f}")
            if pricing["peak_day_charge"]:
                parts.append(f"Peak Day Charge ${pricing['peak_day_charge']:.2f}")
            summary = (f"{flight['flight_number']} on {pricing['travel_date']}: "
                       + ", ".join(parts) + f". Total ${total:.2f}. Bags and seats "
                       "are not included.")
            data = {"confirmation_token": token, "summary": summary, "total": total,
                    "flight_number": flight["flight_number"],
                    "travel_date": pricing["travel_date"],
                    "base_fare": pricing["base_fare"], "taxes": pricing["taxes"],
                    "early_booking_charge": pricing["early_booking_charge"],
                    "peak_day_charge": pricing["peak_day_charge"],
                    "blackout_tier": pricing["blackout_tier"],
                    "bags_and_seats_included": False}
            amount = total

        else:  # payment
            want = _money(float(p.get("amount") or 0))
            outstanding = _outstanding()
            allowed = [q["amount"] for q in outstanding]
            total_outstanding = _money(sum(allowed))
            if want <= 0 or not (any(abs(want - a) < 0.01 for a in allowed)
                                or abs(want - total_outstanding) < 0.01):
                raise ToolError(
                    "AMOUNT_NOT_QUOTED",
                    "That amount was not quoted on this call. Charge only an amount a "
                    "quote produced, or the sum of them. "
                    + (f"Outstanding: {', '.join(f'${a:.2f}' for a in allowed)} "
                       f"(total ${total_outstanding:.2f})." if allowed
                       else "Nothing has been quoted yet."),
                    recoverable=True, outstanding=allowed,
                    total_outstanding=total_outstanding)
            summary = (f"${want:.2f} to the card ending {res['card_last4']}.")
            data = {"confirmation_token": token, "summary": summary, "amount": want,
                    "card_last4": res["card_last4"]}
            amount = 0.0  # the payment itself is not a new quote

        conn.execute(
            "INSERT OR REPLACE INTO holds (token, kind, confirmation_code, "
            "miles_number, payload, amount, summary, consumed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (token, body.kind, code, body.miles_number, json.dumps(data),
             amount, summary))

    if body.kind != "payment":
        _record_quote(body.kind, amount)
    return data


@app.post("/confirmations", status_code=201)
def confirm(body: ConfirmCreate) -> dict[str, Any]:
    """Step two: spend the token once. Three distinct refusals."""
    token = str(body.confirmation_token or "").strip().upper()
    with _db() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE token = ?", (token,)).fetchone()
        if hold is None:
            raise ToolError(
                "TOKEN_NOT_ISSUED",
                "No quote issued that confirmation token on this call. Quote it "
                "first and use the token that comes back.")
        if body.kind and hold["kind"] != body.kind:
            raise ToolError(
                "TOKEN_WRONG_PAIR",
                f"That token belongs to the {hold['kind'].replace('_', ' ')} quote, "
                f"not to this one. Quote the {body.kind.replace('_', ' ')} and use "
                "its own token.")
        if hold["consumed"]:
            raise ToolError(
                "TOKEN_ALREADY_USED",
                "That token has already been used. Nothing was charged twice. Quote "
                "again if the caller wants another change.")
        conn.execute("UPDATE holds SET consumed = 1 WHERE token = ?", (token,))

        kind, code = hold["kind"], hold["confirmation_code"]
        payload = json.loads(hold["payload"] or "{}")
        detail, result = hold["summary"], {}

        if kind == "change":
            conn.execute(
                "UPDATE segments SET flight_number = ?, departs_on = ?, departs_at = ? "
                "WHERE confirmation_code = ? AND id = "
                "(SELECT MIN(id) FROM segments WHERE confirmation_code = ?)",
                (payload["new_flight"], payload["departs_on"], payload["departs_at"],
                 code, code))
            result = {"confirmation_code": code, "status": "changed",
                      "new_flight": payload["new_flight"],
                      "departs_on": payload["departs_on"],
                      "amount_due": payload["total"]}

        elif kind == "involuntary_rebook":
            conn.execute(
                "UPDATE segments SET flight_number = ?, departs_on = ?, departs_at = ? "
                "WHERE confirmation_code = ? AND id = "
                "(SELECT MIN(id) FROM segments WHERE confirmation_code = ?)",
                (payload["new_flight"], payload["departs_on"], payload["departs_at"],
                 code, code))
            result = {"confirmation_code": code, "status": "rebooked",
                      "new_flight": payload["new_flight"],
                      "departs_on": payload["departs_on"], "amount_due": 0.0}

        elif kind == "cancellation":
            conn.execute("UPDATE reservations SET status = 'cancelled' "
                         "WHERE confirmation_code = ?", (code,))
            if payload["outcome"] == "cash":
                conn.execute(
                    "INSERT INTO refunds (confirmation_code, amount, form_of_payment, "
                    "basis) VALUES (?, ?, ?, ?)",
                    (code, payload["amount_returned"], "original form of payment",
                     payload.get("basis") or "cancellation"))
            else:
                res = conn.execute("SELECT miles_number FROM reservations "
                                   "WHERE confirmation_code = ?", (code,)).fetchone()
                conn.execute(
                    "INSERT INTO flight_credits (miles_number, amount, issued_on, "
                    "expires_on) VALUES (?, ?, ?, ?)",
                    (res["miles_number"], payload["amount_returned"], TODAY,
                     payload["credit_expires_on"]))
            result = {"confirmation_code": code, "status": "cancelled",
                      "outcome": payload["outcome"],
                      "amount_returned": payload["amount_returned"],
                      "credit_expires_on": payload.get("credit_expires_on", "")}

        elif kind == "refund":
            conn.execute(
                "INSERT INTO refunds (confirmation_code, amount, form_of_payment, "
                "basis) VALUES (?, ?, ?, ?)",
                (code, payload["amount"], payload["form_of_payment"],
                 payload["basis"]))
            result = {"confirmation_code": code, "status": "refunded",
                      "amount": payload["amount"],
                      "processing_window": payload.get("processing_window", "")}

        elif kind == "bag":
            conn.execute(
                "INSERT INTO bag_purchases (confirmation_code, bag_kind, touchpoint, "
                "quantity, amount) VALUES (?, ?, ?, ?, ?)",
                (code, payload["bag_kind"], payload["touchpoint"],
                 payload["quantity"], payload["total"]))
            result = {"confirmation_code": code, "status": "bag_added",
                      "bag_kind": payload["bag_kind"],
                      "quantity": payload["quantity"],
                      "amount_due": payload["total"]}

        elif kind == "seat":
            conn.execute(
                "UPDATE seat_inventory SET status = 'taken' WHERE flight_number = ? "
                "AND departs_on = ? AND seat = ?",
                (payload["flight_number"], payload["departs_on"], payload["seat"]))
            conn.execute(
                "INSERT INTO seat_assignments (confirmation_code, flight_number, "
                "seat, amount) VALUES (?, ?, ?, ?)",
                (code, payload["flight_number"], payload["seat"], payload["price"]))
            result = {"confirmation_code": code, "status": "seat_assigned",
                      "seat": payload["seat"], "amount_due": payload["price"]}

        elif kind == "pass_booking":
            n = conn.execute("SELECT COUNT(*) c FROM pass_bookings").fetchone()["c"]
            new_code = f"RP{n + 1:04d}"
            conn.execute(
                "INSERT INTO pass_bookings (miles_number, new_code, flight_number, "
                "travel_date, base_fare, taxes, charges) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hold["miles_number"], new_code, payload["flight_number"],
                 payload["travel_date"], payload["base_fare"], payload["taxes"],
                 _money(payload["early_booking_charge"] + payload["peak_day_charge"])))
            result = {"status": "pass_booked", "confirmation_code": new_code,
                      "flight_number": payload["flight_number"],
                      "travel_date": payload["travel_date"],
                      "amount_due": payload["total"],
                      "bags_and_seats_included": False}

        else:  # payment
            conn.execute(
                "INSERT INTO payments (confirmation_code, amount, card_last4) "
                "VALUES (?, ?, ?)",
                (code, payload["amount"], payload["card_last4"]))
            for q in _outstanding():
                q["paid"] = True
            result = {"confirmation_code": code, "status": "paid",
                      "amount": payload["amount"],
                      "card_last4": payload["card_last4"]}

        conn.execute(
            "INSERT INTO commits (kind, confirmation_code, token, detail, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, code, token, detail, hold["amount"]))
    return result


@app.post("/itineraries", status_code=201)
def send_itinerary(body: ItineraryCreate) -> dict[str, Any]:
    channel = str(body.channel or "").strip().lower()
    channel = {"e-mail": "email", "mail": "email", "text": "sms",
               "message": "sms"}.get(channel, channel)
    if channel not in ("email", "sms"):
        raise ToolError("UNKNOWN_CHANNEL", "Ask whether they want it by email or text.")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO itineraries (confirmation_code, channel) VALUES (?, ?)",
            (body.confirmation_code, channel))
    return {"itinerary_id": cur.lastrowid, "status": "sent", "channel": channel}


@app.post("/reservation-notes", status_code=201)
def add_reservation_note(body: NoteCreate) -> dict[str, Any]:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO reservation_notes (confirmation_code, note) VALUES (?, ?)",
            (body.confirmation_code, body.note))
    return {"note_id": cur.lastrowid, "status": "noted"}


@app.post("/escalations", status_code=201)
def escalate_to_human(body: EscalationCreate) -> dict[str, Any]:
    """A live person only for an elite caller or one inside the 24 hour window."""
    code = body.confirmation_code
    reason, tier, hours = body.reason_code, "none", None
    with _db() as conn:
        window = float(_setting(conn, "live_agent_window_hours", "24"))
        if code:
            res = conn.execute("SELECT * FROM reservations WHERE confirmation_code = ?",
                               (code,)).fetchone()
            if res is not None:
                tier = _tier(conn, res["miles_number"])["tier"]
                segs = _segments(conn, code)
                if segs:
                    hours = round(_hours_to_departure(segs[0]["departs_on"],
                                                      segs[0]["departs_at"]), 2)
        eligible = tier != "none" or (hours is not None and abs(hours) <= window)
        outcome = "live_agent" if eligible else "callback_scheduled"
        cur = conn.execute(
            "INSERT INTO escalations (confirmation_code, reason_code, outcome) "
            "VALUES (?, ?, ?)", (code, reason, outcome))
    return {
        "escalation_id": cur.lastrowid, "transferred": True, "reason_code": reason,
        "outcome": outcome,
        "basis": ("elite status" if tier != "none"
                  else "within the 24 hour window" if eligible else "not eligible"),
        "hours_to_departure": hours,
        "script": ("Putting you through to a colleague now."
                   if outcome == "live_agent" else
                   "I can't put you through to a person on this one. What I can do is "
                   "book you a callback, and someone will ring you back on this "
                   "number."),
    }


# ------------------------------------------------------------------ dispatch
# POST /tools/{tool_name} {"arguments": {...}}: the industry-agnostic contract
# every harness speaks. Wraps the handlers above in the tools.json envelope:
# {"ok": bool, "data": ..., "error_code": str|null, "caller_safe_message": str|null}.
# Session (end_call) and handoff (transfer_to_*) tools never land here → 404.


def _d_find_reservation(a: dict[str, Any]) -> dict[str, Any]:
    return find_reservation(ReservationFind(
        last_name=a.get("last_name", ""),
        confirmation_code=a.get("confirmation_code", "") or "",
        miles_number=a.get("miles_number", "") or ""))


def _d_hold(kind: str, *keys: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def run(a: dict[str, Any]) -> dict[str, Any]:
        code = "" if kind == "pass_booking" else _verified_code(
            a.get("confirmation_code"))
        return create_hold(HoldCreate(
            kind=kind, confirmation_code=code,
            miles_number=str(a.get("miles_number", "") or "").strip().upper(),
            payload={k: a.get(k) for k in keys if a.get(k) is not None}))
    return run


def _d_confirm(kind: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda a: confirm(ConfirmCreate(
        confirmation_token=a.get("confirmation_token", ""), kind=kind))


DISPATCH: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "find_reservation": _d_find_reservation,
    "get_traveler_list": lambda a: get_traveler_list(
        _verified_code(a.get("confirmation_code"))),
    "get_reservation": lambda a: get_reservation(
        _verified_code(a.get("confirmation_code"))),
    "get_flight_status": lambda a: get_flight_status(
        a.get("flight_number", ""), a.get("date", "")),
    "get_disruption_entitlement": lambda a: get_disruption_entitlement(
        _verified_code(a.get("confirmation_code"))),
    "search_flights": lambda a: search_flights(
        a.get("origin", ""), a.get("destination", ""),
        a.get("earliest_date", "") or ""),
    "quote_involuntary_rebook": _d_hold("involuntary_rebook", "new_flight"),
    "confirm_involuntary_rebook": _d_confirm("involuntary_rebook"),
    "quote_refund": _d_hold("refund"),
    "confirm_refund": _d_confirm("refund"),
    "get_fare_rules": lambda a: get_fare_rules(
        _verified_code(a.get("confirmation_code"))),
    "quote_change": _d_hold("change", "new_flight"),
    "confirm_change": _d_confirm("change"),
    "quote_cancellation": _d_hold("cancellation"),
    "confirm_cancellation": _d_confirm("cancellation"),
    "get_credit_balance": lambda a: get_credit_balance(a.get("miles_number", "")),
    "get_elite_status": lambda a: get_elite_status(a.get("miles_number", "")),
    "get_bag_price": lambda a: get_bag_price(
        _verified_code(a.get("confirmation_code")), a.get("bag_kind", ""),
        a.get("touchpoint", "")),
    "get_seat_map": lambda a: get_seat_map(a.get("flight_number", ""),
                                           a.get("date", "")),
    "quote_bag": _d_hold("bag", "bag_kind", "touchpoint", "quantity"),
    "confirm_bag": _d_confirm("bag"),
    "quote_seat": _d_hold("seat", "seat", "flight_number"),
    "confirm_seat": _d_confirm("seat"),
    "get_pass_status": lambda a: get_pass_status(a.get("miles_number", "")),
    "check_pass_availability": lambda a: check_pass_availability(
        a.get("miles_number", ""), a.get("origin", ""), a.get("destination", ""),
        a.get("travel_date", "")),
    "quote_pass_booking": _d_hold("pass_booking", "flight_number", "travel_date"),
    "confirm_pass_booking": _d_confirm("pass_booking"),
    "quote_payment": _d_hold("payment", "amount"),
    "confirm_payment": _d_confirm("payment"),
    "send_itinerary": lambda a: send_itinerary(ItineraryCreate(
        confirmation_code=_verified_code(a.get("confirmation_code")),
        channel=a.get("channel", ""))),
    "add_reservation_note": lambda a: add_reservation_note(NoteCreate(
        confirmation_code=_verified_code(a.get("confirmation_code")),
        note=a.get("note", ""))),
    "escalate_to_human": lambda a: escalate_to_human(EscalationCreate(
        reason_code=a.get("reason_code", "caller_request"),
        confirmation_code=_session().get("code", ""))),
}


class ToolCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.post("/tools/{tool_name}")
def dispatch_tool(tool_name: str, body: ToolCall) -> dict[str, Any]:
    handler = DISPATCH.get(tool_name)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown tool {tool_name!r}: session and handoff tools are "
            "harness-native and industry tools must be listed in DISPATCH",
        )
    try:
        data = handler(dict(body.arguments or {}))
        return {"ok": True, "data": data, "error_code": None,
                "caller_safe_message": None}
    except ToolError as e:
        return {"ok": False, "data": e.extra or None, "error_code": e.code,
                "caller_safe_message": e.message}
    except HTTPException as e:
        return {"ok": False, "data": None, "error_code": f"HTTP_{e.status_code}",
                "caller_safe_message": str(e.detail)}
    except Exception as e:  # soft-fail: a broken tool must not 500 into the call
        return {"ok": False, "data": None, "error_code": "INVALID_ARGUMENTS",
                "caller_safe_message": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ selfcheck

def selfcheck() -> None:
    with db.scope("selfcheck"):
        _selfcheck()


def _err(fn: Callable[..., Any], *a: Any, **kw: Any) -> ToolError:
    """Call something that must refuse, and hand back the refusal."""
    try:
        fn(*a, **kw)
    except ToolError as e:
        return e
    raise AssertionError(f"{getattr(fn, '__name__', fn)} should have refused")


def _tool(name: str, **args: Any) -> dict[str, Any]:
    return dispatch_tool(name, ToolCall(arguments=args))


def _verify(code: str, last_name: str) -> None:
    got = _tool("find_reservation", last_name=last_name, confirmation_code=code)
    assert got["ok"] and got["data"]["confirmation_code"] == code, got


def _selfcheck() -> None:
    """Every server-enforced guard, asserted against a fresh DB."""
    init_db()

    # ---------------------------------------------------------- identity
    assert find_reservation(ReservationFind(
        last_name="Solberg", confirmation_code="RT2LKD"))["verified"] is True
    assert find_reservation(ReservationFind(
        last_name="Sollberg", confirmation_code="rt 2 l k d"))[
        "confirmation_code"] == "RT2LKD", "fuzzy name + spaced code must verify"
    assert find_reservation(ReservationFind(
        last_name="Ingersoll", miles_number="KM4471902"))[
        "confirmation_code"] == "ZC8MRF", "miles number must resolve"
    assert _err(find_reservation, ReservationFind(
        last_name="Nobody", confirmation_code="RT2LKD")).code == "NOT_NAMED"
    assert _err(find_reservation, ReservationFind(
        last_name="Solberg", confirmation_code="ZZ9ZZZ")).code == "NOT_FOUND"
    ceased = _err(find_reservation, ReservationFind(
        last_name="Quintero-Namm", confirmation_code="VA774193"))
    assert ceased.code == "CARRIER_CEASED_OPERATIONS"
    assert ceased.extra.get("recoverable") is False, "dead carrier must not be retried"

    # gated data is closed until a reservation is verified in THIS call
    init_db()
    assert _tool("get_reservation")["error_code"] == "IDENTITY_NOT_VERIFIED"
    assert _tool("get_bag_price", bag_kind="carry_on",
                 touchpoint="gate")["error_code"] == "IDENTITY_NOT_VERIFIED"
    _verify("RT2LKD", "Solberg")
    assert _tool("get_reservation")["ok"], "gate must open after verification"
    # one reservation per call: a second code is refused, not silently swapped
    assert _tool("get_reservation",
                 confirmation_code="QK4TZP")["error_code"] == "NOT_NAMED"

    # ---------------------------------------------------------- no leaks
    res = get_reservation("LN6BKP")
    assert "age" not in json.dumps(res) and res["traveler_count"] == 2, \
        "get_reservation must not leak ages"
    minors = get_traveler_list("LN6BKP")
    assert minors["has_accompanying_adult"] is False and minors["youngest_age"] == 9
    # The negative control for the gate. Note this passes on the 44-year-old's age
    # alone: `is_guardian` only adds reach for a guardian who is NOT already a 15+
    # traveller on the booking, and no fixture exercises that. Documented in
    # docs/SPEC.md §6 rather than asserted here as though it were covered.
    guarded = get_traveler_list("TY7MBX")
    assert guarded["has_accompanying_adult"] is True, "an adult on the booking clears"

    # ---------------------------------------------------------- fee ladder
    assert get_fare_rules("NB4RQC")["change_fee"] == 0, "61 days out is free of fee"
    assert get_fare_rules("MR4KLD")["change_fee"] == 79, "42 days out is the mid band"
    assert get_fare_rules("QK4TZP")["change_fee"] == 129, "3 days out is the inner band"
    assert get_fare_rules("HB9WQM")["change_fee"] == 0, "a bundle never pays a fee"
    assert get_fare_rules("QK4TZP")["residual_value"] is False

    # ---------------------------------------------------------- disruption
    assert get_flight_status("KA771", "2026-08-09")["status"] == "cancelled"
    assert _err(get_flight_status, "KA214", "2026-10-01").code == "NO_STATUS_ON_FILE"
    assert get_disruption_entitlement("RT2LKD")["basis"] == "cancellation"
    assert get_disruption_entitlement("WD7NCE")["entitled"] is True, "195 >= 180"
    assert get_disruption_entitlement("VP3XHB")["entitled"] is False, "140 < 180"
    assert get_disruption_entitlement("GX9TSA")["entitled"] is False, \
        "45 minutes on an international segment is far below 360"
    assert get_disruption_entitlement("KF2DVR")["basis"] == "booked_24h"
    assert get_disruption_entitlement("MR4KLD")["entitled"] is False

    # the precedence trap: a disrupted booking must never be quoted a voluntary fee
    init_db()
    _verify("RT2LKD", "Solberg")
    blocked = _tool("quote_change", new_flight="KA775")
    assert blocked["error_code"] == "DISRUPTED_USE_IRROPS", blocked
    assert blocked["data"]["recoverable"] is False
    # cash refund on a basic fare, because the carrier cancelled
    refund = _tool("quote_refund")
    assert refund["ok"] and refund["data"]["amount"] == 129.0, refund
    assert _tool("confirm_refund",
                 confirmation_token=refund["data"]["confirmation_token"]
                 )["data"]["status"] == "refunded"
    # the free rebook is the other half of the same choice, and it is also zero
    rebook = _tool("quote_involuntary_rebook", new_flight="KA775")
    assert rebook["ok"] and rebook["data"]["total"] == 0.0, rebook
    assert _tool("confirm_involuntary_rebook",
                 confirmation_token=rebook["data"]["confirmation_token"]
                 )["data"]["status"] == "rebooked"
    # rebooking onto a flight that is operating resolves the disruption, so both
    # remedies are now correctly refused: they are a choice, not a sequence.
    assert _tool("quote_refund")["error_code"] == "NOT_ENTITLED", \
        "a rebooked traveller is no longer owed a refund"
    assert _tool("quote_involuntary_rebook",
                 new_flight="KA779")["error_code"] == "NOT_ENTITLED"

    # an undisrupted booking gets NOT_ENTITLED for both irrops paths
    init_db()
    _verify("MR4KLD", "Brennecke")
    assert _tool("quote_involuntary_rebook",
                 new_flight="KA340")["error_code"] == "NOT_ENTITLED"
    assert _tool("quote_refund")["error_code"] == "NOT_ENTITLED"

    # ---------------------------------------------------------- change gate
    init_db()
    _verify("HB9WQM", "Vasquez-Hail")
    dearer = _tool("quote_change", new_flight="KA509")
    assert dearer["ok"] and dearer["data"]["change_fee"] == 0.0
    assert dearer["data"]["fare_difference"] == 41.5, dearer
    assert dearer["data"]["total"] == 41.5
    cheaper = _tool("quote_change", new_flight="KA505")
    assert cheaper["data"]["fare_difference"] == 0.0
    assert cheaper["data"]["residual_value_forfeited"] == 76.5, \
        "a cheaper itinerary forfeits the difference"
    token = cheaper["data"]["confirmation_token"]
    assert _tool("confirm_cancellation",
                 confirmation_token=token)["error_code"] == "TOKEN_WRONG_PAIR"
    assert _tool("confirm_change",
                 confirmation_token=token)["data"]["status"] == "changed"
    assert _tool("confirm_change",
                 confirmation_token=token)["error_code"] == "TOKEN_ALREADY_USED"
    assert _tool("confirm_change",
                 confirmation_token="KA-CHG-0000")["error_code"] == "TOKEN_NOT_ISSUED"

    # ---------------------------------------------------------- cancellation
    init_db()
    _verify("QK4TZP", "Ferreira")
    credit = _tool("quote_cancellation")
    assert credit["data"]["outcome"] == "credit" and credit["data"]["fee"] == 129.0
    assert credit["data"]["amount_returned"] == 14.9, credit
    assert credit["data"]["credit_expires_on"] == "2027-08-01", "12 months"
    assert _tool("confirm_cancellation",
                 confirmation_token=credit["data"]["confirmation_token"]
                 )["data"]["outcome"] == "credit"

    init_db()
    _verify("KF2DVR", "Adeyemi")
    cash = _tool("quote_cancellation")
    assert cash["data"]["outcome"] == "cash" and cash["data"]["fee"] == 0.0, cash
    assert cash["data"]["basis"] == "booked_24h"

    # ---------------------------------------------------------- silent waivers
    init_db()
    _verify("ZC8MRF", "Ingersoll")            # platinum
    assert get_elite_status("KM4471902")["free_first_checked_bag"] is True
    assert get_elite_status("KM4471902")["carry_on_included"] is False
    first = _tool("get_bag_price", bag_kind="checked_first", touchpoint="booking")
    assert first["data"]["price"] == 0.0 and first["data"]["base_price"] == 30.0
    assert first["data"]["waiver"] == "elite_platinum_first_checked"
    carry = _tool("get_bag_price", bag_kind="carry on", touchpoint="at the gate")
    assert carry["data"]["price"] == 79.0, "no tier ever covers the carry-on"
    assert carry["data"]["touchpoint"] == "gate", "alias must resolve"
    second = _tool("get_bag_price", bag_kind="checked_second", touchpoint="airport")
    assert second["data"]["price"] == 75.0, "only the FIRST checked bag is waived"

    init_db()
    _verify("PW8HJL", "Fournier-Oduya")       # gold: the tier-boundary negative
    assert get_elite_status("KM3318640")["free_first_checked_bag"] is False
    gold = _tool("get_bag_price", bag_kind="checked_first", touchpoint="booking")
    assert gold["data"]["price"] == 30.0 and not gold["data"]["waiver"], gold

    # touchpoint escalation, and the gate-only personal item charge
    init_db()
    _verify("MR4KLD", "Brennecke")
    prices = [_tool("get_bag_price", bag_kind="carry_on", touchpoint=t
                    )["data"]["price"] for t in TOUCHPOINTS]
    assert prices == [35.0, 50.0, 65.0, 79.0], prices
    assert prices == sorted(prices), "the gate must never be cheaper"
    assert _tool("get_bag_price", bag_kind="personal item",
                 touchpoint="gate")["data"]["price"] == 99.0
    assert _tool("get_bag_price", bag_kind="dog",
                 touchpoint="booking")["data"]["price"] == 149.0
    assert _tool("get_bag_price", bag_kind="nonsense",
                 touchpoint="gate")["error_code"] == "UNKNOWN_BAG_KIND"
    assert _tool("get_bag_price", bag_kind="carry_on",
                 touchpoint="tuesday")["error_code"] == "UNKNOWN_TOUCHPOINT"

    # a bundle covers the carry-on, which is a different waiver from the elite one
    init_db()
    _verify("HB9WQM", "Vasquez-Hail")
    bundled = _tool("get_bag_price", bag_kind="carry_on", touchpoint="gate")
    assert bundled["data"]["price"] == 0.0
    assert bundled["data"]["waiver"] == "bundle_carry_on", bundled

    # ---------------------------------------------------------- seats
    init_db()
    _verify("ZC8MRF", "Ingersoll")
    assert get_seat_map("KA812", "2026-08-18")["open_count"] == 3
    taken = _tool("quote_seat", seat="14C")
    assert taken["error_code"] == "SEAT_TAKEN", taken
    front = _tool("quote_seat", seat="3A")
    assert front["data"]["price"] == 50.0, "platinum does not cover front row"
    std = _tool("quote_seat", seat="14B")
    assert std["data"]["price"] == 0.0 and std["data"]["waiver"] == \
        "elite_platinum_seat", std
    assert _tool("confirm_seat", confirmation_token=std["data"]["confirmation_token"]
                 )["data"]["status"] == "seat_assigned"
    assert _tool("quote_seat", seat="14B")["error_code"] == "SEAT_TAKEN", \
        "confirming a seat must close it"

    # ---------------------------------------------------------- roam pass
    init_db()
    _verify("JT5QWD", "Ramanathan-Cole")
    window = _tool("check_pass_availability", miles_number="KM8827104", origin="TPA",
                   destination="DEN", travel_date="2026-08-07")
    assert window["error_code"] == "ROAM_WINDOW", window
    assert window["data"]["early_booking_charge"] == 49.0, window
    assert window["data"]["recoverable"] is True, "paying the charge is a way through"
    priced = _tool("quote_pass_booking", miles_number="KM8827104",
                   flight_number="KA332", travel_date="2026-08-07")
    assert priced["ok"] and priced["data"]["base_fare"] == 0.01
    assert priced["data"]["total"] == _money(0.01 + 11.20 + 49.0), priced
    assert priced["data"]["bags_and_seats_included"] is False
    booked = _tool("confirm_pass_booking",
                   confirmation_token=priced["data"]["confirmation_token"])
    assert booked["ok"] and booked["data"]["status"] == "pass_booked", booked
    # a pass-ineligible flight is a final answer, and an account with no pass refuses
    assert _tool("quote_pass_booking", miles_number="KM8827104",
                 flight_number="KA334",
                 travel_date="2026-08-07")["error_code"] == "PASS_FLIGHT_UNAVAILABLE"
    assert _tool("check_pass_availability", miles_number="KM4471902", origin="TPA",
                 destination="DEN", travel_date="2026-08-07")["error_code"] == "NO_PASS"
    assert _tool("check_pass_availability", miles_number="KM8827104", origin="TPA",
                 destination="DEN", travel_date="2027-06-01"
                 )["error_code"] == "PASS_EXPIRED"

    # ---------------------------------------------------------- payment
    init_db()
    _verify("MR4KLD", "Brennecke")
    assert _tool("quote_payment", amount=500)["error_code"] == "AMOUNT_NOT_QUOTED", \
        "an invented amount must be refused"
    bag = _tool("quote_bag", bag_kind="checked_first", touchpoint="booking")
    assert bag["data"]["total"] == 30.0
    assert _tool("confirm_bag", confirmation_token=bag["data"]["confirmation_token"]
                 )["data"]["status"] == "bag_added"
    change = _tool("quote_change", new_flight="KA340")
    assert change["data"]["change_fee"] == 79.0
    assert change["data"]["total"] == 103.8, change
    single = _tool("quote_payment", amount=30.0)
    assert single["ok"], "a single quoted amount is payable"
    both = _tool("quote_payment", amount=133.8)
    assert both["ok"], "the sum of outstanding quotes is payable"
    paid = _tool("confirm_payment",
                 confirmation_token=both["data"]["confirmation_token"])
    assert paid["ok"] and paid["data"]["amount"] == 133.8, paid
    assert _tool("quote_payment", amount=133.8)["error_code"] == "AMOUNT_NOT_QUOTED", \
        "quotes already paid must not be chargeable again"

    # ---------------------------------------------------------- escalation
    init_db()
    _verify("ZC8MRF", "Ingersoll")
    elite = _tool("escalate_to_human", reason_code="caller_request")
    assert elite["data"]["outcome"] == "live_agent", "elite gets a person"
    init_db()
    _verify("WD7NCE", "Kastner")
    soon = _tool("escalate_to_human", reason_code="irrops")
    assert soon["data"]["outcome"] == "live_agent", "inside 24 hours gets a person"
    init_db()
    _verify("MR4KLD", "Brennecke")
    later = _tool("escalate_to_human", reason_code="caller_request")
    assert later["data"]["outcome"] == "callback_scheduled", later
    assert "callback" in later["data"]["script"].lower()

    # ---------------------------------------------------------- widening
    assert search_flights("ORD", "SEA", "2026-08-01")["count"] == 2
    wide = search_flights("ORD", "SEA", "2027-01-01")
    assert wide["count"] > 0 and wide.get("relaxed_filter"), \
        "an empty-by-filter search must widen and say so"

    # ---------------------------------------------------------- single-step writes
    init_db()
    _verify("MR4KLD", "Brennecke")
    assert _tool("send_itinerary", channel="e-mail")["data"]["channel"] == "email"
    assert _tool("add_reservation_note", note="Called about bags.")["ok"]
    assert _tool("send_itinerary", channel="pigeon")["error_code"] == "UNKNOWN_CHANNEL"

    # ---------------------------------------------------------- durable state
    dump = state()
    assert set(dump) == set(DURABLE_TABLES), sorted(set(dump) ^ set(DURABLE_TABLES))
    assert dump["itineraries"] and dump["reservation_notes"], "writes must persist"

    # ---------------------------------------------------------- catalog parity
    # Prompt structure, in the healthcare section format: seven shared sections,
    # a numbered role divider, then the per-agent sections. WHO YOU ARE and the
    # no-tool facts list must be identical everywhere; PERSONALITY, GUARDRAILS,
    # HARD RULES and SECURITY are deliberately tailored per node.
    prompts = sorted((INDUSTRY_DIR / "system-prompts").glob("*.md"))
    assert len(prompts) == 6, [p.name for p in prompts]
    shared: dict[str, set[str]] = {"WHO YOU ARE": set(), "FACTS": set()}
    for path in prompts:
        text = path.read_text()
        heads = [ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")]
        for required in ("WHO YOU ARE", "PERSONALITY", "GUARDRAILS",
                         "HANDOFFS ARE INVISIBLE", "HARD RULES", "SECURITY",
                         "AIRLINE FACTS YOU MAY STATE WITHOUT A TOOL", "GOAL",
                         "DESCRIPTION", "TOOLS AT THIS STAGE", "HANDING OFF",
                         "RECEIVING CONTEXT", "GLOBAL TOOLS"):
            assert required in heads, f"{path.name}: missing section {required!r}"
        assert heads[0] == "WHO YOU ARE", f"{path.name}: must open with WHO YOU ARE"
        role = [h for h in heads if "YOUR CURRENT ROLE:" in h]
        assert len(role) == 1, f"{path.name}: expected one role divider, got {role}"
        # Every node except the entry states where the caller already is.
        entry = json.loads(
            (INDUSTRY_DIR / "agent_blueprint.json").read_text())["agents"][0]["name"]
        if path.stem != entry:
            assert "WHERE YOU ARE IN THE CALL" in heads, \
                f"{path.name}: a non-entry node must say where the call already is"
        assert "Frankie" in text, f"{path.name}: the agent is called Frankie"
        shared["WHO YOU ARE"].add(text.split("\n# PERSONALITY")[0])
        shared["FACTS"].add(
            text.split("# AIRLINE FACTS YOU MAY STATE WITHOUT A TOOL")[1]
                .split("\n# ─")[0])
    for block, seen in shared.items():
        assert len(seen) == 1, f"{block} drifted across prompts ({len(seen)} variants)"

    # House style, pack-wide: no em or en dashes anywhere a human will read.
    # Written as codepoints so this file stays clean of the characters it bans.
    em, en = chr(0x2014), chr(0x2013)
    dashed = [
        p.relative_to(INDUSTRY_DIR).as_posix()
        for p in sorted(INDUSTRY_DIR.rglob("*"))
        if p.is_file() and p.suffix in {".md", ".py", ".sql", ".json", ".mmd", ".txt"}
        and "__pycache__" not in p.parts
        and (em in p.read_text() or en in p.read_text())
    ]
    assert not dashed, f"em or en dash found in {dashed}"

    catalog = json.loads((INDUSTRY_DIR / "tools.json").read_text())["tools"]
    names = {t["name"] for t in catalog}
    for banned in ("compensation", "voucher", "goodwill", "visa", "passport",
                   "waypoint", "spend_credit", "predict"):
        assert not any(banned in n for n in names), f'no tool may expose "{banned}"'
    blueprint = json.loads((INDUSTRY_DIR / "agent_blueprint.json").read_text())
    agent_names = {a["name"] for a in blueprint["agents"]}
    for agent in blueprint["agents"]:
        assert (INDUSTRY_DIR / agent["system_prompt"]).is_file(), agent["system_prompt"]
        for t in agent["tools"]:
            assert t["name"] in names, f"{agent['name']}: {t['name']} not in tools.json"
            if t.get("handoff"):
                assert t["handoff_to"] in agent_names

    flags: dict[str, dict] = {}
    for agent in blueprint["agents"]:
        for t in agent["tools"]:
            flags.setdefault(t["name"], t)
    dispatchable = {n for n in names
                    if not flags.get(n, {}).get("handoff")
                    and not flags.get(n, {}).get("session")}
    assert dispatchable == set(DISPATCH), sorted(dispatchable ^ set(DISPATCH))

    for absent in ("not_a_tool", "end_call", "transfer_to_irrops"):
        try:
            dispatch_tool(absent, ToolCall())
        except HTTPException as e:
            assert e.status_code == 404
        else:
            raise AssertionError(f"{absent} must not be dispatchable")

    print(f"ok: {len(names)} tools, {len(blueprint['agents'])} agents, "
          f"{len(TOKENS)} write gates, identity/entitlement/waiver/token/window "
          f"guards all hold, dispatch covers {len(DISPATCH)} tools")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        import uvicorn

        port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
        uvicorn.run(app, host="0.0.0.0", port=port)
