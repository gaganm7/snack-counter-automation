SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS offices (
    id TEXT PRIMARY KEY, currency TEXT NOT NULL, monthly_budget_minor INTEGER NOT NULL,
    default_cap_minor INTEGER NOT NULL, timezone_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY, home_office TEXT NOT NULL REFERENCES offices(id),
    payroll_currency TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS presence (
    employee_id TEXT NOT NULL REFERENCES employees(id),
    office_id TEXT NOT NULL REFERENCES offices(id), on_date TEXT NOT NULL,
    available INTEGER NOT NULL CHECK(available IN (0,1)),
    PRIMARY KEY(employee_id, office_id, on_date)
);
CREATE TABLE IF NOT EXISTS refills (
    id TEXT PRIMARY KEY, office_id TEXT NOT NULL REFERENCES offices(id),
    reporter_id TEXT NOT NULL REFERENCES employees(id),
    state TEXT NOT NULL CHECK(state IN ('OPEN','OFFERED','COMMITTED','PURCHASED','REIMBURSED')),
    cap_minor INTEGER NOT NULL CHECK(cap_minor > 0),
    reserved_minor INTEGER NOT NULL DEFAULT 0 CHECK(reserved_minor >= 0),
    period TEXT, assignee_id TEXT REFERENCES employees(id), offer_round INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, offered_at TEXT, offer_until TEXT,
    committed_at TEXT, commit_due TEXT, purchased_at TEXT,
    actual_minor INTEGER CHECK(actual_minor > 0), receipt_image BLOB, items TEXT,
    CHECK(reserved_minor = 0 OR state IN ('OFFERED','COMMITTED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_refill_per_office
ON refills(office_id) WHERE state IN ('OPEN','OFFERED','COMMITTED');
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, refill_id TEXT NOT NULL REFERENCES refills(id),
    employee_id TEXT NOT NULL REFERENCES employees(id), offer_round INTEGER NOT NULL,
    UNIQUE(refill_id, employee_id, offer_round)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, refill_id TEXT NOT NULL REFERENCES refills(id),
    kind TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
    recipient TEXT NOT NULL, body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claims (
    refill_id TEXT PRIMARY KEY REFERENCES refills(id),
    status TEXT NOT NULL CHECK(status IN ('READY','SENDING','UNKNOWN','SUBMITTED','APPROVED','PAID')),
    employee_id TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
    description TEXT NOT NULL, receipt_image BLOB NOT NULL,
    external_id TEXT UNIQUE, detail TEXT, updated_at TEXT NOT NULL
);
"""
