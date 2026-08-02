-- Reminder bookkeeping for the lifecycle scheduler.
--
-- One row per (event, milestone) once we've posted (or explicitly skipped
-- because a later milestone superseded it). The presence of a row is the
-- source of truth for "we already handled this reminder for this event"
-- so restarts and repeated scans are idempotent.
--
-- kind values:
--   halfway     — midpoint between starts_at and ends_at
--   one_week    — 7 days before ends_at
--   three_day   — 3 days before ends_at
--   one_day     — 24 hours before ends_at
--
-- The scheduler only ever fires the LATEST milestone that has passed and
-- has no row here; earlier still-un-fired milestones for the same event
-- get a row inserted (silently skipped) so they never re-trigger.

CREATE TABLE event_reminders (
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL CHECK (kind IN ('halfway', 'one_week', 'three_day', 'one_day')),
    sent_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    posted    INTEGER NOT NULL DEFAULT 1,  -- 1 = message posted; 0 = silently skipped
    PRIMARY KEY (event_id, kind)
);

CREATE INDEX idx_event_reminders_event ON event_reminders(event_id);
