"""Read-only chat tools available to every member in an @-mention conversation.

Nothing in this module may change data. Write tools live in ``fcb.agents.owner`` and are
gated by ``fcb.agents.tool_policy``. Members may see anyone's bingo cards, points, and
rank; other members' submission details stay owner-only (``owner.member_report``).

Tools that post to Discord (card images, leaderboard) only queue the request on a
``ChatEffects`` object; the bot posts after the agent returns.
"""

from __future__ import annotations

import json
from typing import Any

from strands import tool

from fcb.agents.chat_effects import ChatEffects
from fcb.bingo import scoring as bingo_scoring
from fcb.bingo import store as bingo_store
from fcb.db import dao

# Members of a guild are users with a submission there or a bingo square in one of
# its events. Keeps name lookups from matching people the bot only knows elsewhere.
_GUILD_MEMBER_CLAUSE = (
    " AND (EXISTS (SELECT 1 FROM submissions s WHERE s.discord_user_id = u.discord_user_id"
    " AND s.guild_id = ?) OR EXISTS (SELECT 1 FROM bingo_crossings b JOIN events e"
    " ON e.id = b.event_id WHERE b.discord_user_id = u.discord_user_id AND e.guild_id = ?))"
)


def resolve_member(
    query: str, *, guild_id: str, scope_to_guild: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a name / username / mention / id to one user row, or an error string."""
    q = query.strip().lstrip("@")
    digits = q.strip("<@!>")
    scope = _GUILD_MEMBER_CLAUSE if scope_to_guild else ""
    scope_args: tuple[str, ...] = (guild_id, guild_id) if scope_to_guild else ()
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT u.discord_user_id, u.username, u.display_name FROM users u "
            "WHERE (u.discord_user_id = ? OR lower(u.username) = lower(?) "
            "OR lower(u.display_name) = lower(?))" + scope,
            (digits, q, q, *scope_args),
        ).fetchall()
        if not rows:
            like = f"%{q.lower()}%"
            rows = conn.execute(
                "SELECT u.discord_user_id, u.username, u.display_name FROM users u "
                "WHERE (lower(u.username) LIKE ? OR lower(u.display_name) LIKE ?)"
                + scope
                + " LIMIT 6",
                (like, like, *scope_args),
            ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None, f"No member matching {query!r} found."
    if len(rows) > 1:
        names = ", ".join(
            f"{r['display_name'] or r['username']} ({r['discord_user_id']})" for r in rows
        )
        return None, f"Ambiguous, several members match: {names}"
    return dict(rows[0]), None


def active_bingo_event(guild_id: str) -> dict[str, Any] | None:
    ids = bingo_store.get_bingo_event_ids(guild_id)
    return next((e for e in dao.list_active_events(guild_id) if int(e["id"]) in ids), None)


def _display(user: dict[str, Any]) -> str:
    return str(user.get("display_name") or user.get("username") or user["discord_user_id"])


def build_member_tools(
    guild_id: str, effects: ChatEffects, *, scope_to_guild: bool = True
) -> list[Any]:
    """Create the read-only tool set, bound to this guild and this reply's effects."""

    def _resolve(member: str) -> tuple[dict[str, Any] | None, str | None]:
        return resolve_member(member, guild_id=guild_id, scope_to_guild=scope_to_guild)

    @tool
    def find_member(query: str) -> str:
        """Find a member of this server by display name, username, mention, or Discord id.

        Args:
            query: Name fragment, username, <@id> mention, or numeric Discord id.
        """
        user, err = _resolve(query)
        return json.dumps(user) if user else (err or "not found")

    @tool
    def bingo_overview(member: str) -> str:
        """A member's bingo standing: total points, rank, and per-card squares crossed,
        best pattern, bonus, and points. Works for any member.

        Args:
            member: Name, username, mention, or Discord id of the member.
        """
        user, err = _resolve(member)
        if not user:
            return err or "not found"
        event = active_bingo_event(guild_id)
        if event is None:
            return "No bingo challenge is active."
        uid = user["discord_user_id"]
        board = bingo_store.member_board_state(int(event["id"]), uid, event["name"])
        summary = bingo_store.board_summary(board)
        summary["member"] = _display(user)
        summary["rank"] = bingo_store.member_rank(int(event["id"]), uid)
        return json.dumps(summary, default=str)

    @tool
    def bingo_card(member: str, card_key: str) -> str:
        """One of a member's bingo cards: each square's cell_index, task text, and whether
        it is crossed. Works for any member. Use this to plan which squares to go after.

        Args:
            member: Name, username, mention, or Discord id of the member.
            card_key: One of chest_shoulders, back, legs, arms, core, cardio_conditioning.
        """
        user, err = _resolve(member)
        if not user:
            return err or "not found"
        event = active_bingo_event(guild_id)
        if event is None:
            return "No bingo challenge is active."
        if not bingo_scoring.is_card_key(card_key):
            return f"card_key must be one of {list(bingo_scoring.CARD_KEYS)}"
        board = bingo_store.member_board_state(
            int(event["id"]), user["discord_user_id"], event["name"]
        )
        card = next(c for c in board["cards"] if c["card_key"] == card_key)
        if not card["available"]:
            return "That card is not available."
        return json.dumps(
            [
                {"cell_index": c["index"], "task": c["text"], "crossed": c["crossed"]}
                for c in card["cells"]
            ]
        )

    @tool
    def show_bingo_card(member: str) -> str:
        """Post a member's bingo board image in the channel, after your reply. Use when
        someone asks to see, show, or check a card (their own or another member's).
        At most 2 per reply.

        Args:
            member: Name, username, mention, or Discord id of the member.
        """
        user, err = _resolve(member)
        if not user:
            return err or "not found"
        event = active_bingo_event(guild_id)
        if event is None:
            return "No bingo challenge is active."
        uid = str(user["discord_user_id"])
        status = effects.add_card(uid, _display(user))
        # Give the model the real numbers so its accompanying line can't invent them.
        board = bingo_store.member_board_state(int(event["id"]), uid, event["name"])
        summary = bingo_store.board_summary(board)
        summary["rank"] = bingo_store.member_rank(int(event["id"]), uid)
        return f"{status} Board summary: {json.dumps(summary, default=str)}"

    @tool
    def show_leaderboard() -> str:
        """Post the bingo points leaderboard in the channel, after your reply. Use when
        someone asks for the leaderboard, standings, rankings, or who's winning."""
        if active_bingo_event(guild_id) is None:
            return "No bingo challenge is active; use the leaderboards in your context."
        return effects.request_leaderboard()

    return [find_member, bingo_overview, bingo_card, show_bingo_card, show_leaderboard]
