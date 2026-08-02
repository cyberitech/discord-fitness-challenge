-- Pre-start "upcoming challenge" announcement bookkeeping.
--
-- The lifecycle scheduler posts an "coming up soon" heads-up when we're
-- within bot.upcoming_lead_hours (default 48) of an event's starts_at.
-- announced_upcoming_at holds the moment we posted that message.
-- NULL means "not yet announced". The scan treats this column as the
-- source of truth so restarts and multiple scans stay idempotent.
--
-- Guardrail lives in the scanner: upcoming announcements only fire while
-- now < starts_at. Once the event window opens the start-announcement
-- path takes over, and upcoming stays NULL forever for that event.
--
-- Existing rows get NULL. Historical events whose start already passed
-- will never trigger an upcoming announcement — that's intentional.

ALTER TABLE events ADD COLUMN announced_upcoming_at TEXT;
