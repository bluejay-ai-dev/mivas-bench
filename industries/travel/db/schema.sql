-- Kestrel Air: seeded reference data + durable call artifacts (written at runtime).
--
-- Kestrel Air is a fictional replica of a real US ultra-low-cost carrier. Every
-- number here is structurally identical to that carrier's published policy; every
-- name and code is invented. See docs/RESEARCH.md for the replica map.

-- ===================================================================
-- SEEDED REFERENCE DATA: the replica's world, identical on every run
-- ===================================================================

-- One row per reservation. `fare_family` drives the whole fee ladder.
-- `booked_at` is what the 24-hour rule reads. No dollar amounts and no ages live
-- here: fares come from quote tools, ages only from `travelers`.
CREATE TABLE IF NOT EXISTS reservations (
    confirmation_code TEXT PRIMARY KEY,
    last_name         TEXT NOT NULL,
    miles_number      TEXT NOT NULL DEFAULT '',
    fare_family       TEXT NOT NULL,             -- basic | value | comfort | apex
    fare_paid         REAL NOT NULL,
    booked_at         TEXT NOT NULL,             -- ISO datetime, for the 24-hour rule
    card_last4        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'ticketed',
    legacy_code       TEXT NOT NULL DEFAULT ''   -- a defunct carrier's code, if the caller holds one
);

-- Flight segments. `is_international` selects the 180 vs 360 minute DOT threshold.
CREATE TABLE IF NOT EXISTS segments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL REFERENCES reservations(confirmation_code),
    flight_number     TEXT NOT NULL,
    origin            TEXT NOT NULL,
    destination       TEXT NOT NULL,
    departs_on        TEXT NOT NULL,             -- ISO date
    departs_at        TEXT NOT NULL,             -- HH:MM local
    is_international  INTEGER NOT NULL DEFAULT 0
);

-- The ONLY place ages exist. `get_reservation` returns a count, never this table,
-- so the unaccompanied-minor gate is unreachable without pulling the list.
CREATE TABLE IF NOT EXISTS travelers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL REFERENCES reservations(confirmation_code),
    full_name         TEXT NOT NULL,
    age               INTEGER NOT NULL,
    is_guardian       INTEGER NOT NULL DEFAULT 0
);

-- Change and cancellation fees per fare family. Bundles are 0 at every distance.
-- The three basic-fare bands are the published 60+ / 59-7 / 6-or-fewer ladder.
CREATE TABLE IF NOT EXISTS fare_rules (
    fare_family        TEXT PRIMARY KEY,
    change_fee_60plus  REAL NOT NULL,
    change_fee_59_7    REAL NOT NULL,
    change_fee_6_less  REAL NOT NULL,
    change_fee_sameday REAL NOT NULL,
    cancellation_fee   REAL NOT NULL,
    credit_months      INTEGER NOT NULL DEFAULT 12,
    residual_value     INTEGER NOT NULL DEFAULT 0  -- 0 = a cheaper new itinerary forfeits the difference
);

-- Live operational facts. Not every flight has a row: "no status on file" is a
-- real answer the agent has to give rather than reason around.
CREATE TABLE IF NOT EXISTS flight_status (
    flight_number TEXT NOT NULL,
    status_date   TEXT NOT NULL,
    status        TEXT NOT NULL,                 -- on_time | delayed | cancelled | schedule_change
    delay_minutes INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (flight_number, status_date)
);

-- Bookable inventory for rebooking and pass bookings.
CREATE TABLE IF NOT EXISTS inventory (
    flight_number    TEXT NOT NULL,
    departs_on       TEXT NOT NULL,
    origin           TEXT NOT NULL,
    destination      TEXT NOT NULL,
    departs_at       TEXT NOT NULL,
    fare             REAL NOT NULL,
    seats_available  INTEGER NOT NULL DEFAULT 0,
    is_international INTEGER NOT NULL DEFAULT 0,
    pass_eligible    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (flight_number, departs_on)
);

-- Kestrel Miles elite matrix. The two load-bearing boundaries: the free first
-- checked bag starts at platinum, and no tier ever includes the carry-on.
CREATE TABLE IF NOT EXISTS elite_tiers (
    tier                 TEXT PRIMARY KEY,
    elite_points         INTEGER NOT NULL,
    earn_rate            INTEGER NOT NULL,
    waives_web_checkin   INTEGER NOT NULL DEFAULT 0,
    seat_upgrade_checkin INTEGER NOT NULL DEFAULT 0,
    free_first_checked   INTEGER NOT NULL DEFAULT 0,
    seat_at_booking      TEXT NOT NULL DEFAULT '',
    companion            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS miles_accounts (
    miles_number TEXT PRIMARY KEY,
    member_name  TEXT NOT NULL,
    tier         TEXT NOT NULL DEFAULT 'none' REFERENCES elite_tiers(tier),
    elite_points INTEGER NOT NULL DEFAULT 0
);

-- Bag prices by touchpoint. The gate is always worst; this is the whole point.
CREATE TABLE IF NOT EXISTS bag_prices (
    bag_kind       TEXT NOT NULL,                -- carry_on | checked_first | checked_second
    touchpoint     TEXT NOT NULL,                -- booking | online_checkin | airport | gate
    price          REAL NOT NULL,
    PRIMARY KEY (bag_kind, touchpoint)
);

-- Touchpoint-independent bag and special-item charges.
CREATE TABLE IF NOT EXISTS bag_penalties (
    code   TEXT PRIMARY KEY,
    price  REAL NOT NULL,
    label  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seat_prices (
    seat_class TEXT PRIMARY KEY,                 -- standard | preferred | frontrow_plus
    price      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seat_inventory (
    flight_number TEXT NOT NULL,
    departs_on    TEXT NOT NULL,
    seat          TEXT NOT NULL,
    seat_class    TEXT NOT NULL REFERENCES seat_prices(seat_class),
    status        TEXT NOT NULL DEFAULT 'open',  -- open | taken
    PRIMARY KEY (flight_number, departs_on, seat)
);

-- Roam Pass: $199, $0.01 base fare, 1-day domestic / 10-day international window.
CREATE TABLE IF NOT EXISTS roam_passes (
    miles_number   TEXT PRIMARY KEY REFERENCES miles_accounts(miles_number),
    pass_id        TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT NOT NULL,
    price_paid     REAL NOT NULL
);

-- Blackout dates carry a tier, which sets the Peak Day Charge.
CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_date TEXT PRIMARY KEY,
    tier          TEXT NOT NULL                  -- shoulder | peak | holiday
);

CREATE TABLE IF NOT EXISTS fare_club_members (
    miles_number  TEXT PRIMARY KEY REFERENCES miles_accounts(miles_number),
    joined_on     TEXT NOT NULL,
    renews_on     TEXT NOT NULL,
    annual_fee    REAL NOT NULL,
    enrolment_fee REAL NOT NULL
);

-- Flight credits. Nothing in this pack spends one. The absence is the rule.
CREATE TABLE IF NOT EXISTS flight_credits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    miles_number      TEXT NOT NULL DEFAULT '',
    confirmation_code TEXT NOT NULL DEFAULT '',
    amount            REAL NOT NULL,
    issued_on         TEXT NOT NULL,
    expires_on        TEXT NOT NULL
);

-- Carriers that no longer exist. A code from one of these is a hard refusal.
CREATE TABLE IF NOT EXISTS defunct_carriers (
    code_prefix   TEXT PRIMARY KEY,
    carrier_name  TEXT NOT NULL,
    ceased_on     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ===================================================================
-- DURABLE CALL ARTIFACTS: empty at seed, written during a call.
-- These are what GET /state and the e2e assert on.
-- ===================================================================

-- Step one of every two-step write gate. `consumed` makes a token single-use, so
-- confirming twice is a checkable failure rather than a silent double-charge.
CREATE TABLE IF NOT EXISTS holds (
    token             TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    confirmation_code TEXT NOT NULL DEFAULT '',
    miles_number      TEXT NOT NULL DEFAULT '',
    payload           TEXT NOT NULL DEFAULT '{}',
    amount            REAL NOT NULL DEFAULT 0,
    summary           TEXT NOT NULL DEFAULT '',
    consumed          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS commits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,
    confirmation_code TEXT NOT NULL DEFAULT '',
    token             TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    amount            REAL NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    amount            REAL NOT NULL,
    card_last4        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refunds (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    amount            REAL NOT NULL,
    form_of_payment   TEXT NOT NULL DEFAULT '',
    basis             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bag_purchases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    bag_kind          TEXT NOT NULL,
    touchpoint        TEXT NOT NULL,
    quantity          INTEGER NOT NULL DEFAULT 1,
    amount            REAL NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seat_assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    flight_number     TEXT NOT NULL DEFAULT '',
    seat              TEXT NOT NULL,
    amount            REAL NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pass_bookings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    miles_number   TEXT NOT NULL,
    new_code       TEXT NOT NULL,
    flight_number  TEXT NOT NULL,
    travel_date    TEXT NOT NULL,
    base_fare      REAL NOT NULL,
    taxes          REAL NOT NULL,
    charges        REAL NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS itineraries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    channel           TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reservation_notes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- `outcome` records what the server actually granted: a live agent only for a
-- caller who is elite or within 24 hours of departure, a callback otherwise.
CREATE TABLE IF NOT EXISTS escalations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL DEFAULT '',
    reason_code       TEXT NOT NULL,
    outcome           TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
