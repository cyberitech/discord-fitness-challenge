-- Multi-guild refactor.
--
-- The old schema hard-coded a single guild via DISCORD_GUILD_ID in .env.
-- This migration turns FCB into a multi-tenant bot: it serves every
-- guild it's a member of, and the dashboard admin picks which guild to
-- administer after logging in.
--
-- Since the deployment was nuked before this landed, there's no data to
-- preserve. The migration is destructive on the single-guild tables it
-- replaces and additive on the rest.

-- Registry of guilds the bot is currently in. Populated by the bot in
-- on_ready (upsert every guild in self.guilds) and maintained by
-- on_guild_join / on_guild_remove.
CREATE TABLE bot_guilds (
    guild_id           TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    icon_hash          TEXT,
    joined_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_refreshed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Rebuild guild_cache with a guild_id partition so we cache channels
-- and roles for every guild the bot is in, not just one.
DROP TABLE IF EXISTS guild_cache;
CREATE TABLE guild_cache (
    guild_id   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('channel', 'role')),
    id         TEXT NOT NULL,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, kind, id),
    FOREIGN KEY (guild_id) REFERENCES bot_guilds(guild_id) ON DELETE CASCADE
);
CREATE INDEX idx_guild_cache_lookup ON guild_cache(guild_id, kind, position);

-- Split the old flat settings table into truly-global keys and
-- per-guild keys. Global holds things that gate the whole process
-- (log level, model id, scheduler intervals). Per-guild holds
-- everything else — channel binding, announce roles, voice persona,
-- behavior toggles, cooldowns, etc.
DROP TABLE IF EXISTS settings;
CREATE TABLE global_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by  TEXT
);
CREATE TABLE guild_settings (
    guild_id    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by  TEXT,
    PRIMARY KEY (guild_id, key),
    FOREIGN KEY (guild_id) REFERENCES bot_guilds(guild_id) ON DELETE CASCADE
);

-- Direct guild scoping on submissions so orphan submissions (image
-- posted with no active event to attach to) still carry their guild.
-- Without this, an orphan submission has no anchor to any guild since
-- event_id is null.
ALTER TABLE submissions ADD COLUMN guild_id TEXT;
CREATE INDEX idx_submissions_guild ON submissions(guild_id, id);

-- Bot events can now optionally scope to a guild. Cross-guild events
-- (bot.ready, guild.joined, etc.) leave guild_id NULL. The logs panel
-- filters by the currently-selected guild_id in the dashboard session.
ALTER TABLE bot_events ADD COLUMN guild_id TEXT;
CREATE INDEX idx_bot_events_guild ON bot_events(guild_id, id);
