"""Dashboard access to the bot-populated guild cache.

The bot process refreshes the ``guild_cache`` table on ``on_ready`` and
on channel / role change events. The dashboard reads from that cache to
render channel + role dropdowns on the settings page. This bypasses
Discord's REST API entirely, which was returning 404 for this bot
despite an active gateway membership (legacy invite quirk).

If the cache is empty (first boot before the bot has connected), the
callers fall back to plain text inputs.
"""

from __future__ import annotations

import logging

from fcb.db import dao

logger = logging.getLogger(__name__)


async def list_guild_text_channels(guild_id: str) -> list[dict[str, str]]:
    """Text channels for one guild, position-sorted.

    Returns list of ``{"id": str, "name": str}``. Empty when the cache
    is empty; callers SHOULD render a plain text input as a fallback so
    the admin can still fix things.
    """
    rows = dao.list_guild_cache(guild_id, "channel")
    return [{"id": r["id"], "name": r["name"]} for r in rows]


async def list_guild_roles(guild_id: str) -> list[dict[str, str]]:
    """Assignable roles for one guild, position-sorted (high first)."""
    rows = dao.list_guild_cache(guild_id, "role")
    return [{"id": r["id"], "name": r["name"]} for r in rows]
