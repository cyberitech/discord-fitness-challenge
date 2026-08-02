-- Move the announce role IDs out of the voice prompt templates and into the
-- settings table so admins can change them from the dashboard.
--
-- Seed with the current Fitness Challenger role ID (see discord.md).
-- Stored as a JSON list so multiple roles can be configured later.

INSERT OR IGNORE INTO settings (key, value, updated_by)
VALUES (
    'discord.announce_role_ids',
    '["1521599778599862395"]',
    'system'
);
