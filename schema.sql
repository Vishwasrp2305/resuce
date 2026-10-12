DROP TABLE IF EXISTS alerts;
DROP TABLE IF EXISTS ambulances;
DROP TABLE IF EXISTS police_stations;
DROP TABLE IF EXISTS hospitals;
DROP TABLE IF EXISTS regions;
DROP TABLE IF EXISTS users;

CREATE TABLE regions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    lat                 REAL NOT NULL,
    lon                 REAL NOT NULL,
    telegram_bot_token  TEXT,
    telegram_chat_id    TEXT
);

CREATE TABLE hospitals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id   INTEGER NOT NULL REFERENCES regions(id),
    name        TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    phone       TEXT
);

CREATE TABLE police_stations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id   INTEGER NOT NULL REFERENCES regions(id),
    name        TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    phone       TEXT
);

CREATE TABLE ambulances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    region_id   INTEGER NOT NULL REFERENCES regions(id),
    code        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'available',
    lat         REAL NOT NULL,
    lon         REAL NOT NULL
);

-- Public users who sign up on the /user page. The admin side still uses
-- the shared ADMIN_TOKEN passcode (see app.py) -- this table is only for
-- people reporting SOS events, so each one gets an ID the admin can see.
CREATE TABLE users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           TEXT,
    user_id             INTEGER REFERENCES users(id),
    guest_label         TEXT,
    victim_lat          REAL NOT NULL,
    victim_lon          REAL NOT NULL,
    message             TEXT,
    region_id           INTEGER REFERENCES regions(id),
    nearest_hospital_id INTEGER REFERENCES hospitals(id),
    nearest_police_id   INTEGER REFERENCES police_stations(id),
    ambulance_id        INTEGER REFERENCES ambulances(id),
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    telegram_sent       INTEGER DEFAULT 0,
    telegram_error      TEXT,
    ai_review_label      TEXT,
    ai_review_confidence REAL
);
