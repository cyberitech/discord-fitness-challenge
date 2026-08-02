"""Live runtime settings backed by the ``global_settings`` and
``guild_settings`` tables.

Multi-guild split:

- Process-wide values (Bedrock model id, log level, scheduler
  intervals) live in ``global_settings`` and are read without a guild
  argument.
- Everything else — Discord channel binding, announce roles, voice
  persona, behavior toggles, cooldowns, TTL windows — lives in
  ``guild_settings`` and is read per-guild.

Every value has an env-var fallback in :mod:`fcb.config`, so an
un-seeded install still starts up. When an admin edits a value from
the dashboard, the corresponding row updates and every subsequent
call here picks it up on the next SQLite read — no restart required
for the values this module returns.

Some settings are still restart-required because their consumer wires
the value at process startup (the ``discord.ext.tasks.loop`` cadence,
for example); those stay callable but the caller has to actually
restart for the change to take effect.
"""

import json
import logging

from fcb import config
from fcb.db import dao

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _int_from(raw: str | None, default: int, key: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"setting {key} is not an int: {raw!r}; using default {default}")
        return default


def _float_from(raw: str | None, default: float, key: str) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"setting {key} is not a float: {raw!r}; using default {default}")
        return default


def _bool_from(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _guild_int(guild_id: str, key: str, default: int) -> int:
    return _int_from(dao.get_guild_setting(guild_id, key), default, key)


def _guild_float(guild_id: str, key: str, default: float) -> float:
    return _float_from(dao.get_guild_setting(guild_id, key), default, key)


def _guild_bool(guild_id: str, key: str, default: bool) -> bool:
    return _bool_from(dao.get_guild_setting(guild_id, key), default)


def _global_int(key: str, default: int) -> int:
    return _int_from(dao.get_global_setting(key), default, key)


def _global_float(key: str, default: float) -> float:
    return _float_from(dao.get_global_setting(key), default, key)


def _global_bool(key: str, default: bool) -> bool:
    return _bool_from(dao.get_global_setting(key), default)


# ---------------------------------------------------------------------------
# Per-guild accessors
# ---------------------------------------------------------------------------


def channel_id(guild_id: str) -> str | None:
    """The Discord channel the bot listens to and posts in for this guild.

    Returns None when the guild has not yet been configured on the
    dashboard. Callers MUST handle None (the bot's message handler
    ignores messages posted outside the configured channel).
    """
    return dao.get_guild_setting(guild_id, "discord.channel_id")


def announce_role_ids(guild_id: str) -> list[str]:
    """Role IDs to ping on lifecycle announcements for this guild."""
    raw = dao.get_guild_setting(guild_id, "discord.announce_role_ids")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except json.JSONDecodeError:
        logger.warning(
            f"discord.announce_role_ids for guild {guild_id} is not valid JSON: "
            f"{raw!r}"
        )
    return []


def command_cooldown_seconds(guild_id: str) -> int:
    return _guild_int(
        guild_id, "bot.command_cooldown_seconds", config.FCB_COMMAND_COOLDOWN_SECONDS
    )


def chat_history_messages(guild_id: str) -> int:
    return _guild_int(
        guild_id, "bot.chat_history_messages", config.FCB_CHAT_HISTORY_MESSAGES
    )


def nag_enabled(guild_id: str) -> bool:
    return _guild_bool(guild_id, "bot.nag_enabled", config.FCB_NAG_ENABLED)


def nag_cooldown_hours(guild_id: str) -> float:
    return _guild_float(
        guild_id, "bot.nag_cooldown_hours", config.FCB_NAG_COOLDOWN_HOURS
    )


def lifecycle_grace_hours(guild_id: str) -> float:
    """Grace window for END announcements. Start announcements ignore this."""
    return _guild_float(
        guild_id, "bot.lifecycle_grace_hours", config.FCB_LIFECYCLE_GRACE_HOURS
    )


def reminders_enabled(guild_id: str) -> bool:
    return _guild_bool(
        guild_id, "bot.reminders_enabled", config.FCB_REMINDERS_ENABLED
    )


def upcoming_lead_hours(guild_id: str) -> float:
    """Hours before starts_at to post the pre-start heads-up. 0 disables."""
    return _guild_float(
        guild_id, "bot.upcoming_lead_hours", config.FCB_UPCOMING_LEAD_HOURS
    )


def reply_only_on_challenge_match(guild_id: str) -> bool:
    """True = silent on non-recognized images; False = hardcore-riff reply."""
    return _guild_bool(
        guild_id,
        "bot.reply_only_on_challenge_match",
        config.FCB_REPLY_ONLY_ON_CHALLENGE_MATCH,
    )


# ---------------------------------------------------------------------------
# Process-wide accessors (restart-required because they gate tasks.loop)
# ---------------------------------------------------------------------------


def nag_interval_hours() -> float:
    return _global_float("bot.nag_interval_hours", config.FCB_NAG_INTERVAL_HOURS)


def lifecycle_interval_minutes() -> float:
    return _global_float(
        "bot.lifecycle_interval_minutes", config.FCB_LIFECYCLE_INTERVAL_MINUTES
    )
