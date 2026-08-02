-- Bot-side cache of the bound guild's channels and roles.
--
-- The dashboard can't hit Discord's REST API for this data (the bot's
-- REST membership returns 404 despite an active gateway session — a
-- Discord oddity for some legacy bot invitations). The bot, however,
-- has full access to guild.channels and guild.roles through its
-- discord.py cache once GUILD_CREATE fires on startup.
--
-- The bot refreshes this table on on_ready and on guild membership /
-- channel / role change events. The dashboard reads from it to
-- populate the channel + announce-role dropdowns on the settings
-- page. Stale rows (removed channels / roles) are pruned on each
-- refresh by comparing against the current updated_at cutoff.

CREATE TABLE guild_cache (
    kind        TEXT NOT NULL CHECK (kind IN ('channel', 'role')),
    id          TEXT NOT NULL,
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, id)
);

CREATE INDEX idx_guild_cache_kind ON guild_cache(kind, position);
