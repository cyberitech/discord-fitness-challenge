-- Cache of Discord user identity so the dashboard can show avatars and
-- display names without going out to the Discord API on every request.
--
-- Populated by:
--   - The bot on every observed message (message author).
--   - The dashboard on every successful OAuth callback (logged-in user).
--
-- `last_seen_at` is refreshed on every upsert so we can display "last active".

CREATE TABLE users (
    discord_user_id  TEXT PRIMARY KEY,
    username         TEXT NOT NULL,
    display_name     TEXT,
    avatar_url       TEXT,
    first_seen_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
