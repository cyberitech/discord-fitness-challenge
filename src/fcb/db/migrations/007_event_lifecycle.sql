-- Event lifecycle bookkeeping.
--
-- announced_start_at / announced_end_at hold the timestamp of the moment the
-- bot posted the start / end announcement for the event. NULL means "not yet
-- announced". The lifecycle scheduler treats these as the source of truth for
-- "did we already announce this?" so restarts are safe.
--
-- Existing rows get NULL for both fields; the scheduler's grace-window check
-- will skip announcing events that ended long before this migration.

ALTER TABLE events ADD COLUMN announced_start_at TEXT;
ALTER TABLE events ADD COLUMN announced_end_at TEXT;
