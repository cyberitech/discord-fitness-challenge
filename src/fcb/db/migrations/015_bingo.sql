-- Fitness Bingo challenge support.
--
-- A bingo challenge is an ordinary `events` row (kind = 'challenge') that also has a
-- `bingo_config` row. Modelling it as a side table rather than a new `events.kind`
-- value avoids rebuilding the events table just to widen its inline CHECK, and keeps
-- every existing query working unchanged. Code treats an event as bingo iff a
-- `bingo_config` row exists for it.
--
-- Boards are per-player and deterministic: a member's six 5x5 cards are derived at any
-- time from a seed of (event_id, discord_user_id, card_key) over the event's FROZEN,
-- position-ordered task pool, so layouts are reproducible and never stored. Only the
-- task pool and per-square crossings are persisted.

CREATE TABLE bingo_config (
    event_id        INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    pool_frozen_at  TEXT,   -- set when the challenge opens; pool is immutable afterwards
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The task pool: six cards, ~50+ tasks each. `position` is the stable ordering the
-- deterministic board shuffle depends on.
CREATE TABLE bingo_tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    card_key   TEXT NOT NULL CHECK (card_key IN (
                   'chest_shoulders', 'back', 'legs', 'arms', 'core', 'cardio_conditioning'
               )),
    position   INTEGER NOT NULL CHECK (position >= 0),
    text       TEXT NOT NULL,
    UNIQUE (event_id, card_key, position)
);

CREATE INDEX idx_bingo_tasks_card ON bingo_tasks(event_id, card_key, position);

-- One row per crossed-off square. The UNIQUE primary key makes "a square is crossed
-- once" a database invariant. `submission_id` is the check-in that crossed it (kept for
-- provenance; nulled if the submission is later deleted).
CREATE TABLE bingo_crossings (
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    discord_user_id  TEXT NOT NULL,
    card_key         TEXT NOT NULL CHECK (card_key IN (
                         'chest_shoulders', 'back', 'legs', 'arms', 'core', 'cardio_conditioning'
                     )),
    cell_index       INTEGER NOT NULL CHECK (cell_index BETWEEN 0 AND 24),
    task_id          INTEGER REFERENCES bingo_tasks(id) ON DELETE SET NULL,
    submission_id    INTEGER REFERENCES submissions(id) ON DELETE SET NULL,
    reason           TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, discord_user_id, card_key, cell_index)
);

CREATE INDEX idx_bingo_crossings_member ON bingo_crossings(event_id, discord_user_id);
