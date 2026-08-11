-- Cascade Air reference data (seeded) + durable call artifacts (written at runtime).

CREATE TABLE IF NOT EXISTS reservations (
    confirmation_code TEXT PRIMARY KEY,
    summit_number TEXT,
    fare_brand TEXT NOT NULL,
    booked_at TEXT NOT NULL,
    disruption_status TEXT NOT NULL DEFAULT 'none',
    void_window_open INTEGER NOT NULL DEFAULT 0,
    bags_included INTEGER NOT NULL DEFAULT 0,
    fare_paid INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL REFERENCES reservations(confirmation_code),
    flight TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart TEXT NOT NULL
);

-- Ages and the guardian flag live ONLY here: get_reservation must not leak them, so the
-- unaccompanied-minor gate is only reachable by actually pulling the traveler list.
CREATE TABLE IF NOT EXISTS travelers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL REFERENCES reservations(confirmation_code),
    name TEXT NOT NULL,
    age INTEGER NOT NULL,
    guardian INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fare_rules (
    brand TEXT PRIMARY KEY,
    changeable INTEGER NOT NULL,
    refundable INTEGER NOT NULL,
    credit_pct_15plus_days INTEGER NOT NULL,
    credit_pct_under_15_days INTEGER NOT NULL,
    change_fee INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inventory (
    flight TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart TEXT NOT NULL,
    cabin TEXT NOT NULL,
    fare_diff INTEGER NOT NULL DEFAULT 0,
    seats INTEGER NOT NULL DEFAULT 0
);

-- Not every flight has a row; "no status on file" is a real answer the agent must give.
CREATE TABLE IF NOT EXISTS flight_status (
    flight TEXT PRIMARY KEY,
    scheduled TEXT NOT NULL,
    current TEXT,
    delay_minutes INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS summit_accounts (
    summit_number TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    waives_bag_fee INTEGER NOT NULL DEFAULT 0,
    waives_seat_fee INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS travel_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summit_number TEXT NOT NULL REFERENCES summit_accounts(summit_number),
    amount INTEGER NOT NULL,
    expires TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seat_inventory (
    seat TEXT PRIMARY KEY,
    seat_type TEXT NOT NULL,
    fee INTEGER NOT NULL DEFAULT 0
);

-- Scalar policy numbers the prompts read aloud (bag ladder, seat tiers, the fixed
-- "today" that keeps day-count math deterministic across runs).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Step one of the write gate. `consumed` makes a token single-use, so confirming twice
-- is a checkable failure rather than a silent double charge.
CREATE TABLE IF NOT EXISTS holds (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    confirmation_code TEXT,
    summary TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    confirmation_code TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS itineraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL,
    channel TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reservation_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
