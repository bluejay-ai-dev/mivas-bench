-- Kestrel Electronics — SQLite schema.
-- Two groups, following legal and finance: seeded reference data (the replica's
-- world) and durable call artifacts (what calls write; GET /state and the e2e
-- assert on these).

-- ------------------------------------------------------------- seeded reference

CREATE TABLE stores (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL,
    hours       TEXT NOT NULL,
    departments TEXT NOT NULL
);

-- The published fee schedule, incl. membership pricing. `code` is canonical;
-- spoken aliases map in the server.
CREATE TABLE fees (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    amount_text TEXT NOT NULL,   -- spoken form, e.g. "$45.00 per item"
    conditions  TEXT NOT NULL DEFAULT ''
);

-- Published policy text. The numbers live here so the agent never has to
-- remember them; get_policy reads it out.
CREATE TABLE policies (
    topic       TEXT PRIMARY KEY,
    keywords    TEXT NOT NULL,   -- comma-separated match terms
    title       TEXT NOT NULL,
    body        TEXT NOT NULL
);

CREATE TABLE kb (
    topic       TEXT PRIMARY KEY,
    keywords    TEXT NOT NULL,
    answer      TEXT NOT NULL
);

-- Price-match qualified competitor list. Anything not here is not qualified.
CREATE TABLE competitors (
    name        TEXT PRIMARY KEY,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE customers (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    phone               TEXT NOT NULL,   -- digits only
    email               TEXT NOT NULL,
    postal_code         TEXT NOT NULL,
    card_last4          TEXT NOT NULL,   -- last four of the card on file; never more
    tier                TEXT NOT NULL,   -- standard | plus | total
    membership_start    TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD, '' when standard
    membership_paid_cents INTEGER NOT NULL DEFAULT 0,
    auto_renew          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    order_number    TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    order_date      TEXT NOT NULL,   -- YYYY-MM-DD
    status          TEXT NOT NULL,   -- processing | shipped | delivered | scheduled | cancelled
    fulfillment     TEXT NOT NULL,   -- kestrel | marketplace
    seller_name     TEXT NOT NULL DEFAULT '',  -- marketplace seller, '' when kestrel
    purchase_state  TEXT NOT NULL,   -- two-letter; drives the restocking-fee exclusion
    delivered_date  TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD, '' when not yet delivered
    delivery_date   TEXT NOT NULL DEFAULT '',  -- scheduled delivery, '' when none
    delivery_window TEXT NOT NULL DEFAULT '',
    install         INTEGER NOT NULL DEFAULT 0,
    haul_away       INTEGER NOT NULL DEFAULT 0,
    price_match_used INTEGER NOT NULL DEFAULT 0   -- one match per identical item per customer
);

CREATE TABLE order_items (
    id              TEXT PRIMARY KEY,
    order_number    TEXT NOT NULL REFERENCES orders(order_number),
    sku             TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    price_cents     INTEGER NOT NULL,
    opened          INTEGER NOT NULL DEFAULT 0,
    activatable     INTEGER NOT NULL DEFAULT 0,   -- 14-day window regardless of tier
    restock_class   TEXT NOT NULL DEFAULT 'none', -- none | activatable | percent_15
    condition_grade TEXT NOT NULL DEFAULT 'new',  -- new | open_box_excellent_certified |
                                                  -- open_box_excellent | open_box_satisfactory |
                                                  -- open_box_fair | clearance | refurbished
    recalled        INTEGER NOT NULL DEFAULT 0,
    hazmat          INTEGER NOT NULL DEFAULT 0    -- damaged-lithium class: no label, no bench
);

CREATE TABLE protection_plans (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    order_number    TEXT NOT NULL,
    sku             TEXT NOT NULL,
    plan_name       TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    deductible_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE service_slots (
    date            TEXT NOT NULL,
    service_type    TEXT NOT NULL,   -- bench | in_home | remote
    time_window     TEXT NOT NULL,
    available       INTEGER NOT NULL DEFAULT 1
);

-- Refunds already in flight before this call. Read-only; call-written refunds
-- land in `rmas`.
CREATE TABLE refunds (
    rma_number      TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    order_number    TEXT NOT NULL,
    received_date   TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    stage           TEXT NOT NULL,   -- received | inspecting | processing | posted
    posts_by        TEXT NOT NULL,
    method          TEXT NOT NULL
);

-- Every genuine outbound contact Kestrel made. Absence is the point: it is what
-- lets the fraud desk say "we never called you".
CREATE TABLE outbound_contacts (
    id              TEXT PRIMARY KEY,
    phone           TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    channel         TEXT NOT NULL,   -- phone | email | sms
    contact_date    TEXT NOT NULL,
    summary         TEXT NOT NULL
);

-- ------------------------------------------------------------- durable artifacts

-- Two-step write gates. Fixed tokens, spent exactly once.
CREATE TABLE holds (
    token       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,   -- return | price_match | delivery_change | upgrade | cancel
    customer_id TEXT NOT NULL,
    payload     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE rmas (
    rma_number          TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL,
    order_number        TEXT NOT NULL,
    sku                 TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    refund_cents        INTEGER NOT NULL,
    restock_fee_cents   INTEGER NOT NULL DEFAULT 0,
    method              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE return_labels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rma_number  TEXT NOT NULL,
    sent_to     TEXT NOT NULL,
    label_id    TEXT NOT NULL
);

CREATE TABLE price_matches (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id             TEXT NOT NULL,
    order_number            TEXT NOT NULL,
    sku                     TEXT NOT NULL,
    competitor              TEXT NOT NULL,
    competitor_price_cents  INTEGER NOT NULL,
    difference_cents        INTEGER NOT NULL,
    method                  TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'approved'
);

CREATE TABLE delivery_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number    TEXT NOT NULL,
    old_date        TEXT NOT NULL,
    new_date        TEXT NOT NULL,
    time_window     TEXT NOT NULL,
    fee_cents       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE order_cancellations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number    TEXT NOT NULL,
    refund_cents    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'cancelled'
);

CREATE TABLE service_appointments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     TEXT NOT NULL,
    sku             TEXT NOT NULL,
    service_type    TEXT NOT NULL,
    date            TEXT NOT NULL,
    time_window     TEXT NOT NULL,
    issue           TEXT NOT NULL DEFAULT '',
    payer           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'booked'
);

CREATE TABLE membership_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     TEXT NOT NULL,
    action          TEXT NOT NULL,   -- upgrade | cancel
    from_tier       TEXT NOT NULL,
    to_tier         TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    effective_date  TEXT NOT NULL
);

CREATE TABLE scam_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    phone               TEXT NOT NULL DEFAULT '',
    email               TEXT NOT NULL DEFAULT '',
    channel             TEXT NOT NULL,
    claimed_brand       TEXT NOT NULL DEFAULT '',
    amount_text         TEXT NOT NULL DEFAULT '',
    payment_requested   TEXT NOT NULL DEFAULT '',
    remote_access_given INTEGER NOT NULL DEFAULT 0,
    money_sent          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL
);
