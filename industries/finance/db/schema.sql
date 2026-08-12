-- Copperline Credit Union — SQLite schema.
-- Two groups, following legal: seeded reference data (the replica's world) and
-- durable call artifacts (what calls write; GET /state and the e2e assert on these).

-- ------------------------------------------------------------- seeded reference

CREATE TABLE branches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL,
    hours       TEXT NOT NULL,
    services    TEXT NOT NULL
);

-- The published fee schedule. `code` is canonical; spoken aliases map in the server.
CREATE TABLE fees (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    amount_text TEXT NOT NULL,   -- spoken form, e.g. "$33.00 per item"
    conditions  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE membership_eligibility (
    kind        TEXT NOT NULL,   -- county | employer
    name        TEXT NOT NULL,
    eligible    INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE kb (
    topic       TEXT PRIMARY KEY,
    keywords    TEXT NOT NULL,   -- comma-separated match terms
    answer      TEXT NOT NULL
);

CREATE TABLE members (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    phone                   TEXT NOT NULL,   -- digits only
    dob                     TEXT NOT NULL,   -- YYYY-MM-DD
    member_number_last4     TEXT NOT NULL,
    member_since            INTEGER NOT NULL,
    exploitation_watch      INTEGER NOT NULL DEFAULT 0,
    courtesy_pay_fees_12mo  INTEGER NOT NULL DEFAULT 0  -- count incl. any seeded fee
);

CREATE TABLE accounts (
    id                        TEXT PRIMARY KEY,
    member_id                 TEXT NOT NULL REFERENCES members(id),
    type                      TEXT NOT NULL,  -- cashback_rewards | star_checking | premiere_checking |
                                              -- free_checking | ultimate_growth | star_savings |
                                              -- high_yield_savings | money_market | credit_card
    label                     TEXT NOT NULL,  -- spoken name, e.g. "Cashback Rewards Checking"
    last4                     TEXT NOT NULL,
    balance_cents             INTEGER NOT NULL,
    available_cents           INTEGER NOT NULL,
    opened_date               TEXT NOT NULL,
    direct_deposit_cents      INTEGER NOT NULL DEFAULT 0,  -- this month
    adb_cents                 INTEGER NOT NULL DEFAULT 0,  -- average daily balance
    household_cents           INTEGER NOT NULL DEFAULT 0,  -- combined household balances
    withdrawals_this_quarter  INTEGER NOT NULL DEFAULT 0   -- high_yield_savings only
);

CREATE TABLE transactions (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    posted          TEXT NOT NULL,             -- YYYY-MM-DD
    description     TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,          -- negative = debit
    kind            TEXT NOT NULL,             -- purchase | fee | deposit | payment
    fee_code        TEXT NOT NULL DEFAULT '',  -- fees only, e.g. courtesy_pay
    appsn           INTEGER NOT NULL DEFAULT 0,-- authorized-positive, settled-negative
    statement_date  TEXT NOT NULL DEFAULT ''   -- first statement showing it (dispute window)
);

CREATE TABLE cards (
    id           TEXT PRIMARY KEY,
    member_id    TEXT NOT NULL REFERENCES members(id),
    type         TEXT NOT NULL,               -- debit | credit
    last4        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',  -- active | blocked
    block_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE loans (
    id                 TEXT PRIMARY KEY,
    member_id          TEXT NOT NULL REFERENCES members(id),
    type               TEXT NOT NULL,   -- auto | heloc | personal
    label              TEXT NOT NULL,
    last4              TEXT NOT NULL,
    balance_cents      INTEGER NOT NULL,
    payment_due_cents  INTEGER NOT NULL,
    due_date           TEXT NOT NULL
);

-- ------------------------------------------------------------- durable artifacts

CREATE TABLE holds (
    token     TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,    -- transfer | wire | stop_payment | loan_payment | card_replacement
    member_id TEXT NOT NULL,
    payload   TEXT NOT NULL,    -- JSON of the quoted operation
    summary   TEXT NOT NULL,
    consumed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE transfers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id    TEXT NOT NULL,
    from_account TEXT NOT NULL,
    to_account   TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    fee_cents    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE wires (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id        TEXT NOT NULL,
    destination_type TEXT NOT NULL,
    beneficiary      TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,
    fee_cents        INTEGER NOT NULL,
    status           TEXT NOT NULL    -- sent | held_for_review
);

CREATE TABLE stop_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id    TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    check_number TEXT NOT NULL,
    fee_cents    INTEGER NOT NULL
);

CREATE TABLE loan_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id    TEXT NOT NULL,
    loan_id      TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    method       TEXT NOT NULL,
    fee_cents    INTEGER NOT NULL
);

CREATE TABLE card_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id    TEXT NOT NULL,
    card_last4   TEXT NOT NULL,
    delivery     TEXT NOT NULL,
    fee_cents    INTEGER NOT NULL
);

CREATE TABLE travel_notices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id    TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    destinations TEXT NOT NULL
);

CREATE TABLE fee_reversals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id      TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL
);

CREATE TABLE claims (
    id             TEXT PRIMARY KEY,
    member_id      TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    reason         TEXT NOT NULL,
    regulation     TEXT NOT NULL,   -- reg_e | reg_z
    window_status  TEXT NOT NULL,   -- in_window | outside_window
    status         TEXT NOT NULL DEFAULT 'under_review',
    filed_date     TEXT NOT NULL
);

CREATE TABLE escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL
);
