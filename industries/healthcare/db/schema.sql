-- Every prompt says office address, floor, suite and hours come ONLY from
-- list_locations and must never be guessed, and scheduling must read the office
-- back "WITH THE FLOOR" before booking. So they have to live here: without them a
-- correct agent cannot complete a booking at all.
CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    zip TEXT NOT NULL,
    offers_cosmetic INTEGER NOT NULL DEFAULT 0,
    address TEXT NOT NULL DEFAULT '',
    floor TEXT NOT NULL DEFAULT '',
    suite TEXT NOT NULL DEFAULT '',
    hours TEXT NOT NULL DEFAULT '',
    services TEXT NOT NULL DEFAULT '',
    transit TEXT NOT NULL DEFAULT '',
    parking TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credentials TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES locations(id)
);

CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    dob TEXT NOT NULL,
    zip TEXT NOT NULL,
    phone_e164 TEXT,
    home_office_id TEXT REFERENCES locations(id),
    language TEXT NOT NULL DEFAULT 'en',
    balance_cents INTEGER NOT NULL DEFAULT 0,
    carrier TEXT,
    member_id TEXT,
    plan_name TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    location_id TEXT NOT NULL REFERENCES locations(id),
    provider_id TEXT NOT NULL REFERENCES providers(id),
    appointment_type_code TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_booked_allergy
    ON appointments(patient_id, location_id, appointment_type_code)
    WHERE status = 'booked'
      AND appointment_type_code LIKE 'ALLERGY_%'
      AND appointment_type_code != 'ALLERGY_EVAL';

CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    appointment_type_code TEXT NOT NULL,
    location_ids TEXT NOT NULL,
    earliest TEXT,
    latest TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Generic sink for dispatch tools with no dedicated table (messages, SMS,
-- callbacks, transfers, dispositions, ...) so GET /state still shows the write.
CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
