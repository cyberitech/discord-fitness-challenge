-- Many-to-many between submissions and events.
--
-- A single workout submission can now count toward multiple simultaneously-
-- active challenges (e.g. a treadmill session that satisfies both a monthly
-- elevation challenge and a daily-cardio 1% routine).
--
-- `submissions.event_id` is retained as the "primary" event — used for the
-- bot's reply framing and the submissions detail page — but aggregation
-- queries (leaderboard, /status, home-page cards) MUST read through
-- `submission_events` to see every attribution.
--
-- Two triggers keep `event_participants` populated automatically:
--   - New link + already-approved submission → add participant.
--   - Existing submission flipped to approved → add participant for every
--     event it's linked to.
--
-- Backfill runs at the end so existing rows are consistent with the new
-- source of truth.

CREATE TABLE submission_events (
    submission_id  INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    event_id       INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    is_primary     INTEGER NOT NULL DEFAULT 0,
    linked_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (submission_id, event_id)
);

CREATE INDEX idx_submission_events_event ON submission_events(event_id);
CREATE INDEX idx_submission_events_submission ON submission_events(submission_id);

CREATE TRIGGER auto_participant_on_link
AFTER INSERT ON submission_events
BEGIN
    INSERT OR IGNORE INTO event_participants (event_id, discord_user_id)
    SELECT NEW.event_id, s.discord_user_id
      FROM submissions s
     WHERE s.id = NEW.submission_id AND s.status = 'approved';
END;

CREATE TRIGGER auto_participant_on_approval
AFTER UPDATE OF status ON submissions
WHEN NEW.status = 'approved' AND OLD.status != 'approved'
BEGIN
    INSERT OR IGNORE INTO event_participants (event_id, discord_user_id)
    SELECT se.event_id, NEW.discord_user_id
      FROM submission_events se
     WHERE se.submission_id = NEW.id;
END;

-- Backfill submission_events from the historical event_id column.
INSERT INTO submission_events (submission_id, event_id, is_primary)
SELECT id, event_id, 1
  FROM submissions
 WHERE event_id IS NOT NULL;

-- Backfill event_participants from all existing approved submissions.
INSERT OR IGNORE INTO event_participants (event_id, discord_user_id)
SELECT DISTINCT event_id, discord_user_id
  FROM submissions
 WHERE event_id IS NOT NULL AND status = 'approved';
