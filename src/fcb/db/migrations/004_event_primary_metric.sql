-- Add primary_metric to events so the leaderboard knows what to sort by.
--
-- Allowed values (validated in Python by dao.create_event / update_event):
--   submission_count      — count of approved rows (default; good for daily_1pct)
--   duration_seconds      — total time (sum)
--   distance_miles        — total distance (sum)
--   elevation_gain_feet   — total elevation (sum)
--   calories              — total calories (sum)
--   reps                  — total reps (sum)
--
-- Existing rows get 'submission_count' via the DEFAULT.

ALTER TABLE events
    ADD COLUMN primary_metric TEXT NOT NULL DEFAULT 'submission_count';
