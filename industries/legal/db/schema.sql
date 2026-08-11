-- Halverson & Reed reference data (seeded) + durable call artifacts (written at runtime).

CREATE TABLE IF NOT EXISTS callers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS caller_matters (
    matter_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL REFERENCES callers(id),
    practice_area TEXT NOT NULL,
    represented INTEGER NOT NULL DEFAULT 0,
    firm TEXT
);

-- Opposing party (lowercased) -> conflict status. Anything unlisted is clear.
CREATE TABLE IF NOT EXISTS conflicts (
    party TEXT PRIMARY KEY,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS practice_areas (
    code TEXT PRIMARY KEY,
    accepted INTEGER NOT NULL,
    fee_type TEXT,
    pct_prefiling REAL NOT NULL DEFAULT 0,
    pct_litigation REAL NOT NULL DEFAULT 0,
    consult_fee INTEGER NOT NULL DEFAULT 0
);

-- practice_area 'default' is the firm-wide licence list; a practice area with its own
-- rows overrides it (med-mal is deliberately narrower than the firm footprint).
CREATE TABLE IF NOT EXISTS jurisdictions (
    practice_area TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (practice_area, state)
);

CREATE TABLE IF NOT EXISTS limitation_periods (
    state TEXT NOT NULL,
    practice_area TEXT NOT NULL,
    years REAL NOT NULL,
    PRIMARY KEY (state, practice_area)
);

CREATE TABLE IF NOT EXISTS attorneys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    practice_areas TEXT NOT NULL,
    bar_states TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    attorney_id TEXT NOT NULL REFERENCES attorneys(id),
    starts_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);

-- Status is only served for matters the FIRM handles. Another firm's matter has no row.
CREATE TABLE IF NOT EXISTS matter_status (
    matter_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL REFERENCES callers(id),
    status TEXT NOT NULL,
    status_text TEXT NOT NULL,
    case_manager TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id TEXT NOT NULL,
    practice_area TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',
    incident_date TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS intake_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Intake packets and medical-records authorizations.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Step one of the two-step write gate. `consumed` makes a token single-use, so
-- confirming the same token twice is a checkable failure rather than a silent rebook.
CREATE TABLE IF NOT EXISTS holds (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    caller_id TEXT NOT NULL DEFAULT '',
    slot_id TEXT,
    evaluation_id TEXT,
    practice_area TEXT,
    reason TEXT,
    summary TEXT NOT NULL DEFAULT '',
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL DEFAULT '',
    slot_id TEXT,
    attorney_id TEXT,
    starts_at TEXT,
    fee_type TEXT,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id TEXT NOT NULL,
    for_whom TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
