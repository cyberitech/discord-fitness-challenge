-- Per-event nag threshold: how many days without a post before the bot
-- pokes a participant. 0 disables nagging for that event entirely.
--
-- Existing rows default to 3 days.

ALTER TABLE events
    ADD COLUMN nag_threshold_days INTEGER NOT NULL DEFAULT 3;
