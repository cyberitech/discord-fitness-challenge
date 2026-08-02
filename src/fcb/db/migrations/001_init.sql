-- Initial FCB schema.
--
-- Tables:
--   admins             dashboard allowlist
--   events             one row per fitness event (daily_1pct or challenge)
--   event_participants opt-in per user per event
--   submissions        every workout update posted in the channel
--   event_metrics      materialized per-user per-event totals
--   bot_events         structured log of bot actions and errors
--
-- All timestamp columns are ISO-8601 UTC strings (SQLite's
-- CURRENT_TIMESTAMP default is 'YYYY-MM-DD HH:MM:SS' in UTC).

CREATE TABLE admins (
    discord_user_id  TEXT PRIMARY KEY,
    added_by         TEXT NOT NULL,
    added_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    note             TEXT
);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('daily_1pct', 'challenge')),
    name        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by  TEXT NOT NULL
);

CREATE INDEX idx_events_active ON events(guild_id, starts_at, ends_at);

CREATE TABLE event_participants (
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    discord_user_id  TEXT NOT NULL,
    joined_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, discord_user_id)
);

CREATE TABLE submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         INTEGER REFERENCES events(id) ON DELETE SET NULL,
    discord_user_id  TEXT NOT NULL,
    message_id       TEXT NOT NULL UNIQUE,
    channel_id       TEXT NOT NULL,
    posted_at        TEXT NOT NULL,
    image_path       TEXT,
    raw_text         TEXT,
    extracted_stats  TEXT,   -- JSON, populated by the vision agent
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by      TEXT,
    reviewed_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_submissions_event  ON submissions(event_id);
CREATE INDEX idx_submissions_user   ON submissions(discord_user_id);
CREATE INDEX idx_submissions_status ON submissions(status);

CREATE TABLE event_metrics (
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    discord_user_id  TEXT NOT NULL,
    metrics          TEXT NOT NULL,   -- JSON
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, discord_user_id)
);

CREATE TABLE bot_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level     TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    category  TEXT NOT NULL,
    actor     TEXT,
    message   TEXT NOT NULL,
    context   TEXT             -- JSON
);

CREATE INDEX idx_bot_events_ts    ON bot_events(ts DESC);
CREATE INDEX idx_bot_events_level ON bot_events(level);

-- Seed the initial admin from steering/discord.md. Idempotent so this
-- migration remains re-runnable if edited.
INSERT OR IGNORE INTO admins (discord_user_id, added_by, note)
VALUES ('1271241319699845276', 'system', 'initial seed');
