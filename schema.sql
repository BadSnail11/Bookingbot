
PRAGMA foreign_keys = ON;

-- Tables in the venue (seed with your real layout)
CREATE TABLE IF NOT EXISTS tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0)
);

-- Users of the bot (for convenience)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    first_name TEXT,
    last_name TEXT,
    username TEXT
);

-- Reservations
-- status: 'pending', 'confirmed', 'canceled'
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    table_id INTEGER REFERENCES tables(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    starts_at TEXT NOT NULL, -- ISO 8601 timestamp (UTC)
    ends_at TEXT NOT NULL,   -- ISO 8601 timestamp (UTC)
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','canceled', 'stopped')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_res_start_end ON reservations(starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_res_status ON reservations(status);
