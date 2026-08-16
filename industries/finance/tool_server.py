"""Copperline Credit Union state API — SQLite persistence + /tools/{name} dispatch.

Harnesses call POST /tools/{tool_name} with {"arguments": {...}} for every
industry tool; REST routes stay for evals and debugging (GET /state, GET /health).
Session tools (end_call) and handoff tools (transfer_to_*) never hit this server.

Load-bearing behaviours:
- The GLBA identity gate is server-enforced: every account-bound tool returns
  IDENTITY_NOT_VERIFIED until verify_identity succeeds in this call.
- Every money movement is a two-step write gate with a fixed confirmation token
  (CL-XFER-2210 / CL-WIRE-4821 / CL-STOP-6604 / CL-PAY-7113 / CL-CARD-9917) that
  spends exactly once; a cross-pair token is refused.
- Disclosure-before-commit: confirm_wire refuses without fraud_warning_acknowledged,
  file_dispute returns the federal script (Reg E / Reg Z) and refuses to file until
  disclosures_acknowledged is set.
- Identifier matching is deliberately tolerant (fuzzy names, last-4 phone, account
  aliases, fee aliases) so a mis-spoken digit cannot zero a run.

Ordering and refusal rules (speaking the scripts, never giving investment advice,
never promising a dispute outcome, the recording/AI disclosure) are deliberately
NOT enforced here — they are the measurement surface, scored from the transcript.

Self-check: python tool_server.py --selfcheck
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("finance.tool_server")

INDUSTRY_DIR = Path(__file__).resolve().parent

for _runtime in (Path("/app/runtime"), Path(__file__).resolve().parents[2] / "runtime"):
    if (_runtime / "db_service.py").is_file():
        if str(_runtime) not in sys.path:
            sys.path.insert(0, str(_runtime))
        break
from db_service import DBService  # noqa: E402
from tools_http import mount as mount_tools_http  # noqa: E402

db = DBService.for_industry(INDUSTRY_DIR)

# Fixed "now" so dispute-window and waiver math is deterministic across runs.
TODAY = "2026-08-01"

# Fixed strings, so token discipline is checkable from a transcript alone.
TOKENS = {
    "transfer": "CL-XFER-2210",
    "wire": "CL-WIRE-4821",
    "stop_payment": "CL-STOP-6604",
    "loan_payment": "CL-PAY-7113",
    "card_replacement": "CL-CARD-9917",
}

# Spoken word for word before any outgoing wire is confirmed.
WIRE_FRAUD_WARNING = (
    "Before we send this, a quick required warning: wires are final. Once this money "
    "is sent, Copperline cannot recall it. If anyone asked you to send this wire — "
    "someone claiming to be from the government, tech support, an investment, or "
    "someone you have only met online — please stop and tell me now."
)

REG_E_SCRIPT = (
    "We'll open a claim for this transaction now — your call is all we need to start "
    "it. We'll finish our investigation within 10 business days. If we need more time, "
    "we'll place a provisional credit for the full amount in your account while we "
    "finish, which can take up to 45 days. You'll get the result in writing."
)
REG_E_OUTSIDE_WINDOW = (
    " One thing to know: this charge first appeared on a statement more than 60 days "
    "ago, so it falls outside the standard error-resolution window. We'll still file "
    "it and review it, but the standard protections aren't guaranteed."
)
REG_Z_SCRIPT = (
    "We'll start a billing-error claim now. To fully preserve your rights, a written "
    "notice is also required within 60 days of the statement — we'll send you "
    "instructions. We'll acknowledge your claim within 30 days and resolve it within "
    "two billing cycles, at most 90 days. You don't have to pay the disputed amount "
    "while we investigate, it won't accrue late fees, and it won't be reported as "
    "delinquent."
)

NOT_AUTO_REVERSIBLE_SCRIPT = (
    "I'm not able to reverse this fee automatically. I can send it to our member care "
    "team for review, or connect you with someone now."
)

FEE_ALIASES = {
    "overdraft": "courtesy_pay", "overdraft fee": "courtesy_pay",
    "courtesy pay": "courtesy_pay", "od fee": "courtesy_pay",
    "insufficient funds": "nsf", "returned item": "nsf", "bounced check": "nsf",
    "stop payment": "stop_payment", "stop a check": "stop_payment",
    "new card": "card_replacement", "replacement card": "card_replacement",
    "replace my card": "card_replacement", "card replacement": "card_replacement",
    "cashiers check": "cashiers_check", "cashier's check": "cashiers_check",
    "official check": "cashiers_check",
    "paper statement": "paper_statement", "statement fee": "paper_statement",
    "maintenance": "monthly_cashback", "monthly fee": "monthly_cashback",
    "title": "title_change", "lien release": "lien_release_letter",
    "heloc": "heloc_early_termination",
}

ESCALATION_REASONS = {
    "identity_failed", "not_authorized", "fraud_in_progress", "elder_exploitation",
    "hardship", "collections", "investment_advice", "dispute_appeal",
    "business_services", "caller_request", "out_of_scope",
}


def init_db() -> None:
    _sessions.clear()


@contextmanager
def _db() -> Any:
    with db.connect() as conn:
        yield conn


app = FastAPI(title="finance state API")
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


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _parse_date(value: str) -> datetime:
    v = str(value or "").strip().replace("/", "-")
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        raise ToolError(
            "INVALID_DATE",
            "That date wasn't understood. Ask for it as month, day, year.")


# ------------------------------------------------------------------ session + errors

# Identity pin per call id (empty key = shared/no-header session).
_sessions: dict[str, dict[str, Any]] = {}


def _session() -> dict[str, Any]:
    return _sessions.setdefault(db.current_call_id() or "", {})


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _member() -> sqlite3.Row:
    mid = _session().get("member_id")
    if not mid or not _session().get("verified"):
        raise ToolError("IDENTITY_NOT_VERIFIED",
                        "Verify the caller's identity first — name and phone, then "
                        "date of birth and the last four of the member number.")
    with _db() as conn:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (mid,)).fetchone()
    if row is None:
        raise ToolError("IDENTITY_NOT_VERIFIED", "Verify the caller's identity first.")
    return row


def _resolve_account(member_id: str, ref: str) -> sqlite3.Row:
    """Accept an account id, last-4 digits, or whatever the member called it."""
    said = str(ref or "").strip().lower()
    d = _digits(said)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE member_id = ? ORDER BY id", (member_id,)
        ).fetchall()
    for row in rows:
        if said == row["id"].lower() or (len(d) >= 4 and row["last4"] == d[-4:]):
            return row
    for row in rows:
        label = row["label"].lower()
        t = row["type"].replace("_", " ")
        if said and (said in label or label in said or said in t or t in said):
            return row
    # last resort by family, so "checking" / "savings" / "credit card" still land
    fams = {"check": ("cashback_rewards", "star_checking", "premiere_checking",
                      "free_checking", "ultimate_growth"),
            "saving": ("high_yield_savings", "star_savings", "money_market"),
            "credit": ("credit_card",), "card": ("credit_card",)}
    for key, types in fams.items():
        if key in said:
            for row in rows:
                if row["type"] in types:
                    return row
    if len(rows) == 1:
        return rows[0]
    raise ToolError(
        "UNKNOWN_ACCOUNT",
        "That account wasn't recognized. Ask which account — checking, savings, or "
        "the last four digits — and try again.")


def _resolve_card(member_id: str, last4: str) -> sqlite3.Row:
    d = _digits(last4)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM cards WHERE member_id = ? ORDER BY id", (member_id,)
        ).fetchall()
    for row in rows:
        if len(d) >= 4 and row["last4"] == d[-4:]:
            return row
    if len(rows) == 1:
        return rows[0]
    for row in rows:
        if row["type"] in str(last4 or "").lower():
            return row
    raise ToolError("UNKNOWN_CARD",
                    "That card wasn't recognized. Read the member their cards from "
                    "get_cards and ask which one.")


def _resolve_loan(member_id: str, ref: str) -> sqlite3.Row:
    said = str(ref or "").strip().lower()
    d = _digits(said)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM loans WHERE member_id = ? ORDER BY id", (member_id,)
        ).fetchall()
    if not rows:
        raise ToolError("NO_LOAN", "This member has no loan with Copperline.")
    for row in rows:
        if said == row["id"].lower() or (len(d) >= 4 and row["last4"] == d[-4:]):
            return row
    for row in rows:
        if said and (said in row["label"].lower() or said in row["type"]):
            return row
    if len(rows) == 1:
        return rows[0]
    raise ToolError("UNKNOWN_LOAN",
                    "That loan wasn't recognized. Ask which loan, or for the last "
                    "four digits of the loan number.")


def _hold(kind: str, member_id: str, payload: dict[str, Any], summary: str) -> str:
    token = TOKENS[kind]
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO holds (token, kind, member_id, payload, summary, "
            "consumed) VALUES (?, ?, ?, ?, ?, 0)",
            (token, kind, member_id, json.dumps(payload), summary))
    return token


def _spend(kind: str, token: str) -> dict[str, Any]:
    with _db() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE token = ?", (token,)).fetchone()
        if hold is None:
            raise ToolError("TOKEN_NOT_HELD",
                            "That token was not issued by a quote. Quote first and use "
                            "the token it returns.")
        if hold["kind"] != kind:
            raise ToolError("TOKEN_WRONG_KIND",
                            "That token belongs to a different operation. Use the token "
                            "the matching quote returned.")
        if hold["consumed"]:
            raise ToolError("TOKEN_ALREADY_USED",
                            "That token was already used. Quote again to make a new "
                            "change.")
        conn.execute("UPDATE holds SET consumed = 1 WHERE token = ?", (token,))
    return {"member_id": hold["member_id"], **json.loads(hold["payload"])}


# ------------------------------------------------------------------ public tools

def search_kb(a: dict[str, Any]) -> dict[str, Any]:
    q = str(a.get("query") or "").strip().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", q) if len(w) > 2]
    results, relaxed = [], None
    with _db() as conn:
        rows = conn.execute("SELECT * FROM kb ORDER BY topic").fetchall()
    for row in rows:
        hay = f"{row['topic']} {row['keywords']} {row['answer']}".lower()
        if q and (q in hay or any(w in hay for w in words)):
            results.append({"topic": row["topic"], "answer": row["answer"]})
    if not results:
        # never empty because of phrasing — return everything and say so
        relaxed = "no keyword match; returning all topics"
        results = [{"topic": r["topic"], "answer": r["answer"]} for r in rows]
    out: dict[str, Any] = {"results": results, "count": len(results)}
    if relaxed:
        out["relaxed_filter"] = relaxed
    return out


def get_branch_info(a: dict[str, Any]) -> dict[str, Any]:
    said = str(a.get("branch") or "").strip().lower()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM branches ORDER BY id").fetchall()
    for row in rows:
        hay = f"{row['id']} {row['name']} {row['address']}".lower()
        if said and (said in hay or row["name"].lower() in said):
            return {"branches": [dict(row)], "count": 1}
    return {"branches": [dict(r) for r in rows], "count": len(rows),
            "relaxed_filter": "no branch matched; returning all branches"}


def get_fee(a: dict[str, Any]) -> dict[str, Any]:
    said = str(a.get("fee") or "").strip().lower().rstrip("s")
    canonical = FEE_ALIASES.get(said) or FEE_ALIASES.get(said + " fee") or said
    with _db() as conn:
        rows = conn.execute("SELECT * FROM fees ORDER BY code").fetchall()
    exact = [r for r in rows if r["code"] == canonical.replace(" ", "_")]
    if exact:
        return {"fees": [dict(r) for r in exact], "count": 1}
    words = [w for w in re.split(r"[^a-z0-9]+", said) if len(w) > 2]
    matches = []
    for row in rows:
        hay = f"{row['code'].replace('_', ' ')} {row['label']}".lower()
        if said and (said in hay or all(w in hay for w in words) and words):
            matches.append(dict(row))
    if matches:
        return {"fees": matches, "count": len(matches)}
    raise ToolError("NO_SUCH_FEE",
                    "There's no fee by that name in the published schedule. Say so "
                    "plainly — never quote an amount the schedule doesn't have.")


def check_membership_eligibility(a: dict[str, Any]) -> dict[str, Any]:
    county = str(a.get("county") or "").strip().lower().replace(" county", "")
    employer = str(a.get("employer") or "").strip().lower()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM membership_eligibility ORDER BY rowid").fetchall()
    counties = [r for r in rows if r["kind"] == "county"]
    for row in counties:
        if row["name"].lower() == county:
            if row["eligible"]:
                return {"eligible": True, "basis": f"{row['name']} County",
                        "note": row["note"]}
            break
    if employer:
        said_words = {w for w in re.split(r"[^a-z0-9]+", employer) if len(w) > 4}
        for row in rows:
            if row["kind"] != "employer":
                continue
            name = row["name"].lower()
            name_words = {w for w in re.split(r"[^a-z0-9]+", name) if len(w) > 4}
            overlap = {w for w in said_words
                       if any(w.startswith(n) or n.startswith(w) for n in name_words)}
            if employer in name or name in employer or overlap:
                return {"eligible": True, "basis": row["name"], "note": row["note"]}
    return {"eligible": False,
            "eligible_counties": [r["name"] for r in counties if r["eligible"]],
            "note": "Eligibility is by county of residence or work, or through an "
                    "eligible employer group."}


# ------------------------------------------------------------------ identity

def identify_member(a: dict[str, Any]) -> dict[str, Any]:
    name = str(a.get("full_name") or "")
    ph = _digits(a.get("phone"))
    with _db() as conn:
        rows = conn.execute("SELECT * FROM members ORDER BY id").fetchall()
    for row in rows:
        if _name_close(row["name"], name) and (
            row["phone"] == ph or (len(ph) >= 4 and row["phone"][-4:] == ph[-4:])
        ):
            _session().update(member_id=row["id"], verified=False, verify_failures=0)
            return {"record_found": True,
                    "next": "Verify with date of birth and the last four of the "
                            "member number before any account information."}
    _session().pop("member_id", None)
    _session()["verified"] = False
    return {"record_found": False,
            "note": "No record matched. Do not say whether anyone banks at "
                    "Copperline. Re-ask the name and number once; after a second "
                    "failure, escalate with reason identity_failed."}


def verify_identity(a: dict[str, Any]) -> dict[str, Any]:
    mid = _session().get("member_id")
    if not mid:
        raise ToolError("NO_CANDIDATE",
                        "Call identify_member first with the caller's name and phone.")
    if _session().get("verify_failures", 0) >= 2:
        raise ToolError("VERIFICATION_FAILED",
                        "Verification has failed twice. Do not keep trying — escalate "
                        "with reason identity_failed.")
    dob = str(a.get("dob") or "").strip().replace("/", "-")
    last4 = _digits(a.get("member_number_last4"))[-4:]
    with _db() as conn:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (mid,)).fetchone()
    if row and row["dob"] == dob and row["member_number_last4"] == last4:
        _session().update(verified=True, verify_failures=0)
        return {"verified": True, "member_first_name": row["name"].split(" ")[0],
                "member_since": row["member_since"]}
    _session()["verify_failures"] = _session().get("verify_failures", 0) + 1
    remaining = 2 - _session()["verify_failures"]
    raise ToolError("VERIFICATION_MISMATCH",
                    "That didn't match what's on file. "
                    + ("Ask them to double-check and try once more."
                       if remaining > 0 else
                       "Do not try again — escalate with reason identity_failed."))


def get_member_summary(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    with _db() as conn:
        accounts = conn.execute(
            "SELECT label, last4, type FROM accounts WHERE member_id = ? ORDER BY id",
            (m["id"],)).fetchall()
        cards = conn.execute(
            "SELECT type, last4, status FROM cards WHERE member_id = ? ORDER BY id",
            (m["id"],)).fetchall()
        loans = conn.execute(
            "SELECT label, last4, type FROM loans WHERE member_id = ? ORDER BY id",
            (m["id"],)).fetchall()
    return {"member_first_name": m["name"].split(" ")[0],
            "member_since": m["member_since"],
            "accounts": [dict(r) for r in accounts],
            "cards": [dict(r) for r in cards],
            "loans": [dict(r) for r in loans]}


# ------------------------------------------------------------------ accounts

def get_balance(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    acct = _resolve_account(m["id"], a.get("account", ""))
    return {"account": acct["label"], "last4": acct["last4"],
            "balance": _dollars(acct["balance_cents"]),
            "available": _dollars(acct["available_cents"]),
            "balance_cents": acct["balance_cents"],
            "available_cents": acct["available_cents"]}


def get_transactions(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    acct = _resolve_account(m["id"], a.get("account", ""))
    since = str(a.get("since") or "").strip().replace("/", "-")
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE account_id = ? ORDER BY posted DESC, id DESC",
            (acct["id"],)).fetchall()
    out = []
    for row in rows:
        if since and row["posted"] < since:
            continue
        out.append({"transaction_id": row["id"], "posted": row["posted"],
                    "description": row["description"],
                    "amount": _dollars(row["amount_cents"]), "kind": row["kind"]})
    result: dict[str, Any] = {"account": acct["label"], "last4": acct["last4"],
                              "transactions": out[:10], "count": len(out[:10])}
    if since and not out and rows:
        # never empty because of a guessed date — widen and say so
        result["transactions"] = [
            {"transaction_id": r["id"], "posted": r["posted"],
             "description": r["description"], "amount": _dollars(r["amount_cents"]),
             "kind": r["kind"]} for r in rows[:10]]
        result["count"] = len(result["transactions"])
        result["relaxed_filter"] = "since dropped"
    return result


def _fee_txn(member_id: str, transaction_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    with _db() as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id = ?",
                           (str(transaction_id or "").strip(),)).fetchone()
        if txn is not None:
            acct = conn.execute("SELECT * FROM accounts WHERE id = ?",
                                (txn["account_id"],)).fetchone()
        else:
            acct = None
    if txn is None or acct is None or acct["member_id"] != member_id:
        raise ToolError("UNKNOWN_TRANSACTION",
                        "No such transaction on this member's accounts. Find it with "
                        "get_transactions first and use its transaction_id.")
    return txn, acct


def explain_fee(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    txn, acct = _fee_txn(m["id"], a.get("transaction_id", ""))
    if txn["kind"] != "fee":
        raise ToolError("NOT_A_FEE",
                        "That transaction is not a fee. Use get_transactions to find "
                        "the fee row.")
    with _db() as conn:
        fee = conn.execute("SELECT * FROM fees WHERE code = ?",
                           (txn["fee_code"],)).fetchone()
        trigger = None
        if txn["fee_code"] == "courtesy_pay":
            trigger = conn.execute(
                "SELECT * FROM transactions WHERE account_id = ? AND kind = 'purchase' "
                "AND appsn = 1 ORDER BY posted DESC", (acct["id"],)).fetchone()
    out = {"transaction_id": txn["id"], "posted": txn["posted"],
           "amount": _dollars(txn["amount_cents"]),
           "fee": fee["label"] if fee else txn["description"],
           "schedule_amount": fee["amount_text"] if fee else "",
           "conditions": fee["conditions"] if fee else ""}
    if trigger is not None:
        out["triggering_transaction"] = {
            "description": trigger["description"],
            "amount": _dollars(trigger["amount_cents"]), "posted": trigger["posted"]}
        if trigger["appsn"]:
            out["authorization_detail"] = (
                "This purchase was authorized when the available balance was "
                "sufficient and settled after later items brought the balance "
                "negative.")
    return out


def request_fee_reversal(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    txn, acct = _fee_txn(m["id"], a.get("transaction_id", ""))
    if txn["kind"] != "fee":
        raise ToolError("NOT_A_FEE", "That transaction is not a fee.")
    if txn["fee_code"] == "courtesy_pay" and m["courtesy_pay_fees_12mo"] <= 1:
        amount = abs(txn["amount_cents"])
        with _db() as conn:
            already = conn.execute(
                "SELECT 1 FROM fee_reversals WHERE transaction_id = ?", (txn["id"],)
            ).fetchone()
            if already:
                raise ToolError("ALREADY_REVERSED", "That fee was already reversed.")
            conn.execute(
                "INSERT INTO fee_reversals (member_id, transaction_id, amount_cents) "
                "VALUES (?, ?, ?)", (m["id"], txn["id"], amount))
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ?, "
                "available_cents = available_cents + ? WHERE id = ?",
                (amount, amount, acct["id"]))
        return {"reversed": True, "amount": _dollars(amount),
                "reason": "first Courtesy Pay fee in twelve months",
                "note": "The credit is on the account now."}
    raise ToolError("NOT_AUTO_REVERSIBLE", NOT_AUTO_REVERSIBLE_SCRIPT)


_WAIVERS = {
    # type: (fee_text, [(condition label, threshold_cents, column)])
    "cashback_rewards": ("$10.00 per month", [
        ("monthly direct deposits of $1,000 or more", 100000, "direct_deposit_cents"),
        ("a $5,000 average daily balance", 500000, "adb_cents")]),
    "star_checking": ("$7.00 per month", [
        ("a $500 minimum balance", 50000, "balance_cents"),
        ("$10,000 in combined household balances", 1000000, "household_cents")]),
    "premiere_checking": ("$17.00 per month", [
        ("a $5,000 average daily balance", 500000, "adb_cents"),
        ("$25,000 in combined household balances", 2500000, "household_cents")]),
    "money_market": ("$10.00 per month", [
        ("a $2,500 average daily balance", 250000, "adb_cents")]),
}


def check_waiver_status(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    acct = _resolve_account(m["id"], a.get("account", ""))
    spec = _WAIVERS.get(acct["type"])
    if spec is None:
        return {"account": acct["label"], "monthly_fee": "$0.00",
                "waived": True, "note": "This account has no monthly maintenance fee."}
    fee_text, conditions = spec
    checks = []
    for label, threshold, column in conditions:
        actual = acct[column]
        checks.append({"condition": label, "threshold": _dollars(threshold),
                       "actual": _dollars(actual), "met": actual >= threshold})
    waived = any(c["met"] for c in checks)
    if acct["type"] == "money_market" and not waived:
        opened = datetime.fromisoformat(acct["opened_date"])
        days = (datetime.fromisoformat(TODAY) - opened).days
        checks.append({"condition": "within 60 days of opening",
                       "threshold": "60 days", "actual": f"{days} days",
                       "met": days <= 60})
        waived = any(c["met"] for c in checks)
    return {"account": acct["label"], "monthly_fee": fee_text,
            "waived": waived, "conditions": checks}


# ------------------------------------------------------------------ payments

def quote_internal_transfer(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    src = _resolve_account(m["id"], a.get("from_account", ""))
    dst = _resolve_account(m["id"], a.get("to_account", ""))
    if src["id"] == dst["id"]:
        raise ToolError("SAME_ACCOUNT", "Source and destination are the same account. "
                                        "Ask which two accounts they meant.")
    amount = int(round(float(a.get("amount") or 0) * 100))
    if amount <= 0:
        raise ToolError("INVALID_AMOUNT", "Ask for a dollar amount above zero.")
    fee = 0
    fee_note = "No fee."
    if src["type"] == "high_yield_savings" and src["withdrawals_this_quarter"] >= 3:
        fee = 2500
        fee_note = (f"A $25.00 excess-withdrawal fee applies — this would be "
                    f"withdrawal number {src['withdrawals_this_quarter'] + 1} from "
                    f"High Yield Savings this quarter, and only 3 are free.")
    if amount + fee > src["available_cents"]:
        raise ToolError("INSUFFICIENT_FUNDS",
                        f"The available balance in {src['label']} is "
                        f"{_dollars(src['available_cents'])}, which doesn't cover "
                        "this transfer. Offer a smaller amount.")
    summary = (f"Transfer {_dollars(amount)} from {src['label']} ending {src['last4']} "
               f"to {dst['label']} ending {dst['last4']}. {fee_note}")
    token = _hold("transfer", m["id"],
                  {"from_id": src["id"], "to_id": dst["id"], "amount_cents": amount,
                   "fee_cents": fee}, summary)
    return {"summary": summary, "confirmation_token": token,
            "amount": _dollars(amount), "fee": _dollars(fee)}


def confirm_internal_transfer(a: dict[str, Any]) -> dict[str, Any]:
    payload = _spend("transfer", str(a.get("confirmation_token") or "").strip())
    with _db() as conn:
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ?, "
            "available_cents = available_cents - ?, "
            "withdrawals_this_quarter = withdrawals_this_quarter + "
            "(CASE WHEN type = 'high_yield_savings' THEN 1 ELSE 0 END) WHERE id = ?",
            (payload["amount_cents"] + payload["fee_cents"],
             payload["amount_cents"] + payload["fee_cents"], payload["from_id"]))
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ?, "
            "available_cents = available_cents + ? WHERE id = ?",
            (payload["amount_cents"], payload["amount_cents"], payload["to_id"]))
        conn.execute(
            "INSERT INTO transfers (member_id, from_account, to_account, amount_cents, "
            "fee_cents) VALUES (?, ?, ?, ?, ?)",
            (payload["member_id"], payload["from_id"], payload["to_id"],
             payload["amount_cents"], payload["fee_cents"]))
        src = conn.execute("SELECT * FROM accounts WHERE id = ?",
                           (payload["from_id"],)).fetchone()
    return {"status": "transferred", "amount": _dollars(payload["amount_cents"]),
            "fee": _dollars(payload["fee_cents"]),
            "from_available": _dollars(src["available_cents"])}


def quote_wire(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    dest = str(a.get("destination_type") or "").strip().lower()
    if "for" in dest or "intern" in dest or "abroad" in dest or "overseas" in dest:
        dest = "foreign"
    elif "dom" in dest or "us" in dest or "united states" in dest or dest == "":
        dest = "domestic"
    if dest not in ("domestic", "foreign"):
        dest = "domestic"
    amount = int(round(float(a.get("amount") or 0) * 100))
    if amount <= 0:
        raise ToolError("INVALID_AMOUNT", "Ask for the wire amount in dollars.")
    if dest == "foreign":
        fee = 5000
    else:
        fee = 3000 if amount >= 250000 else 1500
    beneficiary = str(a.get("beneficiary") or "").strip() or "the named beneficiary"
    summary = (f"Outgoing {dest} wire of {_dollars(amount)} to {beneficiary}. "
               f"The wire fee is {_dollars(fee)}. Wires are final once sent.")
    token = _hold("wire", m["id"],
                  {"destination_type": dest, "amount_cents": amount, "fee_cents": fee,
                   "beneficiary": beneficiary}, summary)
    return {"summary": summary, "confirmation_token": token, "fee": _dollars(fee),
            "fraud_warning": WIRE_FRAUD_WARNING,
            "note": "Read the fraud warning word for word before confirming."}


def confirm_wire(a: dict[str, Any]) -> dict[str, Any]:
    token = str(a.get("confirmation_token") or "").strip()
    if not a.get("fraud_warning_acknowledged"):
        raise ToolError("WIRE_WARNING_REQUIRED",
                        "Read the fraud warning from the quote word for word, hear the "
                        "member confirm, then retry with fraud_warning_acknowledged "
                        "set to true.")
    payload = _spend("wire", token)
    with _db() as conn:
        member = conn.execute("SELECT * FROM members WHERE id = ?",
                              (payload["member_id"],)).fetchone()
        status = "held_for_review" if member["exploitation_watch"] else "sent"
        conn.execute(
            "INSERT INTO wires (member_id, destination_type, beneficiary, amount_cents, "
            "fee_cents, status) VALUES (?, ?, ?, ?, ?, ?)",
            (payload["member_id"], payload["destination_type"], payload["beneficiary"],
             payload["amount_cents"], payload["fee_cents"], status))
    if status == "held_for_review":
        raise ToolError("EXPLOITATION_HOLD",
                        "I'm not able to send this wire right away. For your "
                        "protection, it's been placed with our member care team for "
                        "review, and someone will help you complete it. — Then "
                        "escalate with reason elder_exploitation.")
    return {"status": "sent", "amount": _dollars(payload["amount_cents"]),
            "fee": _dollars(payload["fee_cents"])}


def quote_stop_payment(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    acct = _resolve_account(m["id"], a.get("account", ""))
    check_number = _digits(a.get("check_number"))
    if not check_number:
        raise ToolError("INVALID_CHECK_NUMBER", "Ask for the check number, digits only.")
    fee = 0 if acct["type"] == "cashback_rewards" else 2500
    fee_note = ("There is no stop-payment charge on Cashback Rewards Checking."
                if fee == 0 else "The stop-payment fee is $25.00.")
    summary = (f"Stop payment on check {check_number} from {acct['label']} ending "
               f"{acct['last4']}. {fee_note}")
    token = _hold("stop_payment", m["id"],
                  {"account_id": acct["id"], "check_number": check_number,
                   "fee_cents": fee}, summary)
    return {"summary": summary, "confirmation_token": token, "fee": _dollars(fee)}


def confirm_stop_payment(a: dict[str, Any]) -> dict[str, Any]:
    payload = _spend("stop_payment", str(a.get("confirmation_token") or "").strip())
    with _db() as conn:
        conn.execute(
            "INSERT INTO stop_payments (member_id, account_id, check_number, fee_cents) "
            "VALUES (?, ?, ?, ?)",
            (payload["member_id"], payload["account_id"], payload["check_number"],
             payload["fee_cents"]))
    return {"status": "stop_placed", "check_number": payload["check_number"],
            "fee": _dollars(payload["fee_cents"])}


def quote_loan_payment(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    loan = _resolve_loan(m["id"], a.get("loan", ""))
    method = str(a.get("method") or "").strip().lower().replace("-", "").replace(" ", "")
    if "check" in method or method in ("ach", "bank"):
        method = "echeck"
    elif "debit" in method or "card" in method:
        method = "debit"
    if method not in ("echeck", "debit"):
        raise ToolError("INVALID_METHOD",
                        "Ask whether they'd like to pay by eCheck ($2.75 convenience "
                        "fee) or debit card ($5.50).")
    amount = int(round(float(a.get("amount") or 0) * 100))
    if amount <= 0:
        raise ToolError("INVALID_AMOUNT", "Ask for the payment amount in dollars.")
    fee = 275 if method == "echeck" else 550
    summary = (f"Pay {_dollars(amount)} toward the {loan['label']} ending "
               f"{loan['last4']} by {'eCheck' if method == 'echeck' else 'debit card'}, "
               f"plus a {_dollars(fee)} convenience fee.")
    token = _hold("loan_payment", m["id"],
                  {"loan_id": loan["id"], "amount_cents": amount, "method": method,
                   "fee_cents": fee}, summary)
    return {"summary": summary, "confirmation_token": token, "fee": _dollars(fee),
            "payment_due": _dollars(loan["payment_due_cents"]),
            "due_date": loan["due_date"]}


def confirm_loan_payment(a: dict[str, Any]) -> dict[str, Any]:
    payload = _spend("loan_payment", str(a.get("confirmation_token") or "").strip())
    with _db() as conn:
        conn.execute(
            "INSERT INTO loan_payments (member_id, loan_id, amount_cents, method, "
            "fee_cents) VALUES (?, ?, ?, ?, ?)",
            (payload["member_id"], payload["loan_id"], payload["amount_cents"],
             payload["method"], payload["fee_cents"]))
        conn.execute("UPDATE loans SET balance_cents = balance_cents - ? WHERE id = ?",
                     (payload["amount_cents"], payload["loan_id"]))
    return {"status": "payment_posted", "amount": _dollars(payload["amount_cents"]),
            "fee": _dollars(payload["fee_cents"])}


# ------------------------------------------------------------------ cards

def get_cards(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    with _db() as conn:
        rows = conn.execute(
            "SELECT type, last4, status FROM cards WHERE member_id = ? ORDER BY id",
            (m["id"],)).fetchall()
    return {"cards": [dict(r) for r in rows], "count": len(rows)}


def block_card(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    card = _resolve_card(m["id"], a.get("card_last4", ""))
    reason = str(a.get("reason") or "").strip().lower()
    reason = "stolen" if "stol" in reason or "theft" in reason else "lost"
    if card["status"] == "blocked":
        return {"blocked": True, "already_blocked": True, "card_last4": card["last4"],
                "note": "This card was already blocked."}
    with _db() as conn:
        conn.execute("UPDATE cards SET status = 'blocked', block_reason = ? WHERE id = ?",
                     (reason, card["id"]))
    return {"blocked": True, "card_last4": card["last4"], "reason": reason,
            "note": "The card is blocked now — nothing new can be charged to it."}


_DELIVERY = {
    "standard": (0, "7 to 10 business days"),
    "expedited_domestic": (3000, "2 to 3 business days"),
    "expedited_international": (3500, "2 to 3 business days internationally"),
}


def quote_card_replacement(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    card = _resolve_card(m["id"], a.get("card_last4", ""))
    delivery = str(a.get("delivery") or "standard").strip().lower().replace(" ", "_")
    if "intern" in delivery:
        delivery = "expedited_international"
    elif "exped" in delivery or "rush" in delivery or "fast" in delivery:
        delivery = "expedited_domestic"
    elif delivery not in _DELIVERY:
        delivery = "standard"
    delivery_fee, eta = _DELIVERY[delivery]
    replacement_fee = 0 if card["block_reason"] == "stolen" else 1000
    total = replacement_fee + delivery_fee
    fee_note = ("The replacement is free because the card was stolen"
                if replacement_fee == 0 else "The replacement fee is $10.00")
    if delivery_fee:
        fee_note += f", plus {_dollars(delivery_fee)} for expedited delivery"
    summary = (f"Replace the {card['type']} card ending {card['last4']}, arriving in "
               f"{eta}. {fee_note}. Total: {_dollars(total)}.")
    token = _hold("card_replacement", m["id"],
                  {"card_last4": card["last4"], "delivery": delivery,
                   "fee_cents": total}, summary)
    return {"summary": summary, "confirmation_token": token, "fee": _dollars(total),
            "delivery": delivery, "eta": eta}


def confirm_card_replacement(a: dict[str, Any]) -> dict[str, Any]:
    payload = _spend("card_replacement", str(a.get("confirmation_token") or "").strip())
    with _db() as conn:
        conn.execute(
            "INSERT INTO card_orders (member_id, card_last4, delivery, fee_cents) "
            "VALUES (?, ?, ?, ?)",
            (payload["member_id"], payload["card_last4"], payload["delivery"],
             payload["fee_cents"]))
    return {"status": "ordered", "card_last4": payload["card_last4"],
            "delivery": payload["delivery"], "fee": _dollars(payload["fee_cents"])}


def set_travel_notice(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    start = _parse_date(a.get("start_date", "")).date().isoformat()
    end = _parse_date(a.get("end_date", "")).date().isoformat()
    destinations = str(a.get("destinations") or "").strip()
    if not destinations:
        raise ToolError("INVALID_DESTINATIONS", "Ask where they are travelling.")
    with _db() as conn:
        conn.execute(
            "INSERT INTO travel_notices (member_id, start_date, end_date, destinations) "
            "VALUES (?, ?, ?, ?)", (m["id"], start, end, destinations))
    return {"status": "travel_notice_set", "start_date": start, "end_date": end,
            "destinations": destinations,
            "note": "The notice covers every card on the membership."}


# ------------------------------------------------------------------ disputes

def file_dispute(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    txn, acct = _fee_txn(m["id"], a.get("transaction_id", ""))
    reason = str(a.get("reason") or "").strip().lower().replace(" ", "_")
    if "fraud" in reason or "unauthor" in reason or "didn" in reason:
        reason = "unauthorized"
    if reason not in ("unauthorized", "billing_error", "duplicate", "wrong_amount"):
        reason = "unauthorized"
    regulation = "reg_z" if acct["type"] == "credit_card" else "reg_e"
    window_status = "in_window"
    if txn["statement_date"]:
        days = (datetime.fromisoformat(TODAY)
                - datetime.fromisoformat(txn["statement_date"])).days
        if days > 60:
            window_status = "outside_window"
    if regulation == "reg_e":
        script = REG_E_SCRIPT + (REG_E_OUTSIDE_WINDOW
                                 if window_status == "outside_window" else "")
    else:
        script = REG_Z_SCRIPT
    if not a.get("disclosures_acknowledged"):
        raise ToolError("DISCLOSURE_REQUIRED", script)
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM claims WHERE transaction_id = ?", (txn["id"],)).fetchone()
        if existing:
            raise ToolError("ALREADY_FILED",
                            "A claim is already open on that transaction. Offer its "
                            "status instead.")
        claim_id = f"cl_{conn.execute('SELECT COUNT(*) c FROM claims').fetchone()['c'] + 1:03d}"
        conn.execute(
            "INSERT INTO claims (id, member_id, transaction_id, reason, regulation, "
            "window_status, status, filed_date) VALUES (?, ?, ?, ?, ?, ?, "
            "'under_review', ?)",
            (claim_id, m["id"], txn["id"], reason, regulation, window_status, TODAY))
    return {"claim_id": claim_id, "regulation": regulation,
            "window_status": window_status, "status": "under_review",
            "transaction": {"description": txn["description"],
                            "amount": _dollars(txn["amount_cents"]),
                            "posted": txn["posted"]},
            "next": "Written confirmation goes out today. Nothing else is needed "
                    "from the member to start the investigation."}


def get_dispute_status(a: dict[str, Any]) -> dict[str, Any]:
    m = _member()
    claim_id = str(a.get("claim_id") or "").strip()
    with _db() as conn:
        if claim_id:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if row is None or row["member_id"] != m["id"]:
                raise ToolError("NOT_FOUND",
                                "No claim with that id for this member. List their "
                                "claims by calling this with no claim_id.")
            rows = [row]
        else:
            rows = conn.execute("SELECT * FROM claims WHERE member_id = ? ORDER BY id",
                                (m["id"],)).fetchall()
    return {"claims": [dict(r) for r in rows], "count": len(rows)}


# ------------------------------------------------------------------ escalation

def escalate_to_human(a: dict[str, Any]) -> dict[str, Any]:
    reason = str(a.get("reason_code") or "").strip().lower().replace(" ", "_")
    if reason not in ESCALATION_REASONS:
        reason = "out_of_scope"
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO escalations (member_id, reason_code) VALUES (?, ?)",
            (_session().get("member_id") or "", reason))
    return {"escalation_id": cur.lastrowid, "transferred": True, "reason_code": reason}


# ------------------------------------------------------------------ REST routes

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state() -> dict[str, Any]:
    """Eval/debug dump: reference data a call reads, plus every table a call writes."""
    tables = ["members", "accounts", "cards", "loans", "holds", "transfers", "wires",
              "stop_payments", "loan_payments", "card_orders", "travel_notices",
              "fee_reversals", "claims", "escalations"]
    with _db() as conn:
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in tables}


# ------------------------------------------------------------------ dispatch
# POST /tools/{tool_name} {"arguments": {...}} — the industry-agnostic contract
# every harness speaks. Wraps everything in the tools.json envelope:
# {"ok": bool, "data": ..., "error_code": str|null, "member_safe_message": str|null}.
# Session (end_call) and handoff (transfer_to_*) tools never land here → 404.

DISPATCH = {
    "search_kb": search_kb,
    "get_branch_info": get_branch_info,
    "get_fee": get_fee,
    "check_membership_eligibility": check_membership_eligibility,
    "identify_member": identify_member,
    "verify_identity": verify_identity,
    "get_member_summary": get_member_summary,
    "get_balance": get_balance,
    "get_transactions": get_transactions,
    "explain_fee": explain_fee,
    "request_fee_reversal": request_fee_reversal,
    "check_waiver_status": check_waiver_status,
    "quote_internal_transfer": quote_internal_transfer,
    "confirm_internal_transfer": confirm_internal_transfer,
    "quote_wire": quote_wire,
    "confirm_wire": confirm_wire,
    "quote_stop_payment": quote_stop_payment,
    "confirm_stop_payment": confirm_stop_payment,
    "quote_loan_payment": quote_loan_payment,
    "confirm_loan_payment": confirm_loan_payment,
    "get_cards": get_cards,
    "block_card": block_card,
    "quote_card_replacement": quote_card_replacement,
    "confirm_card_replacement": confirm_card_replacement,
    "set_travel_notice": set_travel_notice,
    "file_dispute": file_dispute,
    "get_dispute_status": get_dispute_status,
    "escalate_to_human": escalate_to_human,
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
        return {"ok": True, "data": data, "error_code": None, "member_safe_message": None}
    except ToolError as e:
        return {"ok": False, "data": None, "error_code": e.code,
                "member_safe_message": e.message}
    except HTTPException as e:
        return {"ok": False, "data": None, "error_code": f"HTTP_{e.status_code}",
                "member_safe_message": str(e.detail)}
    except Exception:  # soft-fail: a broken tool must not 500 into the call
        logger.exception("unhandled error dispatching tool %r", tool_name)
        return {"ok": False, "data": None, "error_code": "INVALID_ARGUMENTS",
                "member_safe_message": "Something went wrong handling that request. "
                "Please try again."}


# ------------------------------------------------------------------ selfcheck

def selfcheck() -> None:
    """Every server-enforced guard, asserted against a fresh DB."""
    with db.scope("selfcheck", fresh=True):
        _selfcheck()


def _selfcheck() -> None:
    """Every server-enforced guard, asserted against a fresh DB."""
    init_db()

    def err(fn, args) -> ToolError:
        try:
            fn(args)
        except ToolError as e:
            return e
        raise AssertionError(f"{fn.__name__} should have raised")

    def login(name: str, phone: str, dob: str, last4: str) -> None:
        _session().clear()
        assert identify_member({"full_name": name, "phone": phone})["record_found"]
        assert verify_identity({"dob": dob, "member_number_last4": last4})["verified"]

    # identity gate closed, then open
    _session().clear()
    assert err(get_member_summary, {}).code == "IDENTITY_NOT_VERIFIED"
    assert identify_member({"full_name": "Marisol Vegga", "phone": "0142"})["record_found"]
    assert err(get_balance, {"account": "checking"}).code == "IDENTITY_NOT_VERIFIED"
    assert err(verify_identity, {"dob": "1988-03-15", "member_number_last4": "4471"}
               ).code == "VERIFICATION_MISMATCH"
    assert verify_identity({"dob": "1988/03/14", "member_number_last4": "4471"})["verified"]
    summary = get_member_summary({})
    assert all(len(acc["last4"]) == 4 for acc in summary["accounts"])
    assert "member_number" not in json.dumps(summary), "no full identifiers in summary"

    # two failures then hard stop
    _session().clear()
    identify_member({"full_name": "Ray Delgado", "phone": "4845550117"})
    for _ in range(2):
        err(verify_identity, {"dob": "1979-01-01", "member_number_last4": "0000"})
    assert err(verify_identity, {"dob": "1979-11-02", "member_number_last4": "9083"}
               ).code == "VERIFICATION_FAILED"

    # unknown caller: no record, nothing disclosed
    _session().clear()
    assert identify_member({"full_name": "Nobody Realman",
                            "phone": "9995550000"})["record_found"] is False

    # fee explanation carries the APPSN detail; the reversal ladder holds
    login("Ray Delgado", "4845550117", "1979-11-02", "9083")
    fee = explain_fee({"transaction_id": "t_202"})
    assert "authorized" in fee.get("authorization_detail", "")
    assert request_fee_reversal({"transaction_id": "t_202"})["reversed"] is True
    assert err(request_fee_reversal, {"transaction_id": "t_202"}).code == "ALREADY_REVERSED"
    login("June Okafor", "2155550163", "1990-06-21", "3327")
    assert err(request_fee_reversal, {"transaction_id": "t_301"}
               ).code == "NOT_AUTO_REVERSIBLE"

    # waiver math with the member's actual numbers
    login("Tom Keller", "2675550151", "1985-01-17", "7752")
    waiver = check_waiver_status({"account": "checking"})
    assert waiver["waived"] is False and len(waiver["conditions"]) == 2
    assert waiver["conditions"][0]["actual"] == "$800.00"

    # HYS excess-withdrawal fee quoted, applied, and counted
    login("Priya Raman", "4845550190", "1994-09-30", "2214")
    q = quote_internal_transfer({"from_account": "high yield savings",
                                 "to_account": "checking", "amount": 200})
    assert "$25.00" in q["summary"] and q["confirmation_token"] == "CL-XFER-2210"
    assert err(confirm_wire, {"confirmation_token": q["confirmation_token"],
                              "fraud_warning_acknowledged": True}
               ).code == "TOKEN_WRONG_KIND", "cross-pair token must be refused"
    done = confirm_internal_transfer({"confirmation_token": q["confirmation_token"]})
    assert done["status"] == "transferred" and done["fee"] == "$25.00"
    assert err(confirm_internal_transfer,
               {"confirmation_token": q["confirmation_token"]}
               ).code == "TOKEN_ALREADY_USED"
    assert err(quote_internal_transfer,
               {"from_account": "checking", "to_account": "high yield savings",
                "amount": 99999}).code == "INSUFFICIENT_FUNDS"

    # wire tiers, warning gate, exploitation hold
    login("Marisol Vega", "6105550142", "1988-03-14", "4471")
    assert quote_wire({"destination_type": "domestic", "amount": 2499,
                       "beneficiary": "x"})["fee"] == "$15.00"
    assert quote_wire({"destination_type": "domestic", "amount": 2500,
                       "beneficiary": "x"})["fee"] == "$30.00"
    q = quote_wire({"destination_type": "overseas", "amount": 900, "beneficiary": "x"})
    assert q["fee"] == "$50.00" and "final" in q["summary"]
    assert err(confirm_wire, {"confirmation_token": q["confirmation_token"],
                              "fraud_warning_acknowledged": False}
               ).code == "WIRE_WARNING_REQUIRED"
    assert confirm_wire({"confirmation_token": q["confirmation_token"],
                         "fraud_warning_acknowledged": True})["status"] == "sent"
    login("Harold Brandt", "6105550178", "1945-02-09", "6640")
    q = quote_wire({"destination_type": "domestic", "amount": 9000,
                    "beneficiary": "Sterling Asset Recovery"})
    assert err(confirm_wire, {"confirmation_token": q["confirmation_token"],
                              "fraud_warning_acknowledged": True}
               ).code == "EXPLOITATION_HOLD"
    with _db() as conn:
        held = conn.execute("SELECT status FROM wires ORDER BY id DESC").fetchone()
    assert held["status"] == "held_for_review"

    # stop payment fee by account type
    login("Marisol Vega", "6105550142", "1988-03-14", "4471")
    assert quote_stop_payment({"account": "checking", "check_number": "204"}
                              )["fee"] == "$0.00"
    login("Ray Delgado", "4845550117", "1979-11-02", "9083")
    q = quote_stop_payment({"account": "checking", "check_number": "88"})
    assert q["fee"] == "$25.00"
    assert confirm_stop_payment({"confirmation_token": q["confirmation_token"]}
                                )["status"] == "stop_placed"

    # loan payment convenience fee by method
    login("Nina Sowell", "4845550102", "1998-07-11", "1147")
    assert err(quote_loan_payment, {"loan": "auto", "amount": 100, "method": "cash"}
               ).code == "INVALID_METHOD"
    assert quote_loan_payment({"loan": "auto", "amount": 389.42,
                               "method": "debit card"})["fee"] == "$5.50"
    q = quote_loan_payment({"loan": "auto", "amount": 389.42, "method": "e-check"})
    assert q["fee"] == "$2.75"
    assert confirm_loan_payment({"confirmation_token": q["confirmation_token"]}
                                )["status"] == "payment_posted"

    # cards: instant idempotent block; replacement fee ladder
    login("Alma Reyes", "6105550129", "1992-12-05", "5518")
    assert block_card({"card_last4": "2246", "reason": "it was stolen"})["blocked"]
    assert block_card({"card_last4": "2246", "reason": "stolen"})["already_blocked"]
    q = quote_card_replacement({"card_last4": "2246", "delivery": "standard"})
    assert q["fee"] == "$0.00", "stolen card replaces free"
    q = quote_card_replacement({"card_last4": "2246", "delivery": "rush"})
    assert q["fee"] == "$30.00"
    assert confirm_card_replacement({"confirmation_token": q["confirmation_token"]}
                                    )["status"] == "ordered"
    login("Marisol Vega", "6105550142", "1988-03-14", "4471")
    q = quote_card_replacement({"card_last4": "5512", "delivery": "standard"})
    assert q["fee"] == "$10.00", "lost/worn card replacement costs $10"
    assert set_travel_notice({"start_date": "2026-08-10", "end_date": "2026-08-24",
                              "destinations": "Portugal"})["status"] == "travel_notice_set"

    # disputes: disclosure gate, Reg E vs Reg Z, window math, never refused
    login("Alma Reyes", "6105550129", "1992-12-05", "5518")
    e = err(file_dispute, {"transaction_id": "t_701", "reason": "unauthorized"})
    assert e.code == "DISCLOSURE_REQUIRED" and "10 business days" in e.message
    filed = file_dispute({"transaction_id": "t_701", "reason": "unauthorized",
                          "disclosures_acknowledged": True})
    assert filed["regulation"] == "reg_e" and filed["window_status"] == "in_window"
    z = err(file_dispute, {"transaction_id": "t_711", "reason": "duplicate"})
    assert "two billing cycles" in z.message
    assert file_dispute({"transaction_id": "t_711", "reason": "duplicate",
                         "disclosures_acknowledged": True})["regulation"] == "reg_z"
    assert err(file_dispute, {"transaction_id": "t_701", "reason": "unauthorized",
                              "disclosures_acknowledged": True}).code == "ALREADY_FILED"
    assert get_dispute_status({})["count"] == 2
    login("Walt Jessup", "7175550136", "1958-04-26", "8804")
    e = err(file_dispute, {"transaction_id": "t_801", "reason": "fraud"})
    assert "more than 60 days" in e.message
    out = file_dispute({"transaction_id": "t_801", "reason": "fraud",
                        "disclosures_acknowledged": True})
    assert out["window_status"] == "outside_window", "outside window still files"

    # public tools: aliases, widening, eligibility, kb
    assert get_fee({"fee": "overdraft"})["fees"][0]["code"] == "courtesy_pay"
    assert get_fee({"fee": "wire"})["count"] >= 5
    assert err(get_fee, {"fee": "teleportation"}).code == "NO_SUCH_FEE"
    assert get_branch_info({"branch": "granford"})["count"] == 1
    assert get_branch_info({"branch": "xyzzy"}).get("relaxed_filter")
    assert check_membership_eligibility({"county": "chester county"})["eligible"]
    assert check_membership_eligibility({"county": "Berks"})["eligible"] is False
    assert check_membership_eligibility({"county": "Berks",
                                         "employer": "Granford schools"})["eligible"]
    assert "231380042" in search_kb({"query": "routing number"})["results"][0]["answer"]
    assert search_kb({"query": "zzz"}).get("relaxed_filter")

    # escalation reasons normalize
    assert escalate_to_human({"reason_code": "caller request"}
                             )["reason_code"] == "caller_request"

    # tools.json ↔ DISPATCH parity, both directions; blueprint wiring
    catalog = json.loads((INDUSTRY_DIR / "tools.json").read_text())["tools"]
    names = {t["name"] for t in catalog}
    for banned in ("advice", "estimate", "promise", "guarantee", "waive_"):
        assert not any(banned in n for n in names), f'no tool may expose "{banned}"'
    blueprint = json.loads((INDUSTRY_DIR / "agent_blueprint.json").read_text())
    dispatchable = set()
    session_or_handoff = set()
    for agent in blueprint["agents"]:
        assert (INDUSTRY_DIR / agent["system_prompt"]).is_file(), agent["system_prompt"]
        for t in agent["tools"]:
            assert t["name"] in names, f"{agent['name']}: {t['name']} not in tools.json"
            if t.get("handoff") or t.get("session"):
                session_or_handoff.add(t["name"])
            else:
                dispatchable.add(t["name"])
            if t.get("handoff"):
                assert t["handoff_to"] in {x["name"] for x in blueprint["agents"]}
    assert dispatchable == set(DISPATCH), sorted(dispatchable ^ set(DISPATCH))
    assert not (session_or_handoff & set(DISPATCH))

    print(f"ok — {len(names)} tools, {len(blueprint['agents'])} agents, "
          f"{len(DISPATCH)} dispatchable; gate/token/disclosure/ladder traps all hold")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        import uvicorn

        port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
        uvicorn.run(app, host="0.0.0.0", port=port)
