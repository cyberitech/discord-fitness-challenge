"""SQLite access for fitness bingo: config, task pool, and per-member crossings.

Uses the shared ``dao.connect()`` so it inherits WAL, foreign keys, and the Row
factory. Board layouts are never stored — they are derived in ``scoring.derive_board``
from the frozen pool. Only the pool and the crossings live here.
"""

from __future__ import annotations

import json
import logging

from fcb.bingo.scoring import CARD_KEYS, is_card_key
from fcb.db import dao

logger = logging.getLogger(__name__)


def get_bingo_event_ids(guild_id: str) -> set[int]:
    """Event ids in this guild that are bingo challenges (have a bingo_config row)."""
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT bc.event_id AS id FROM bingo_config bc "
            "JOIN events e ON e.id = bc.event_id WHERE e.guild_id = ?",
            (guild_id,),
        ).fetchall()
        return {int(r["id"]) for r in rows}
    finally:
        conn.close()


def is_bingo_event(event_id: int) -> bool:
    conn = dao.connect()
    try:
        return (
            conn.execute(
                "SELECT 1 FROM bingo_config WHERE event_id = ?", (event_id,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def ensure_config(event_id: int) -> None:
    """Mark an event as a bingo challenge (idempotent)."""
    conn = dao.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO bingo_config (event_id) VALUES (?)", (event_id,))
        conn.commit()
    finally:
        conn.close()


def pool_frozen_at(event_id: int) -> str | None:
    conn = dao.connect()
    try:
        row = conn.execute(
            "SELECT pool_frozen_at FROM bingo_config WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row["pool_frozen_at"] if row else None
    finally:
        conn.close()


def freeze_pool(event_id: int) -> None:
    conn = dao.connect()
    try:
        conn.execute(
            "UPDATE bingo_config SET pool_frozen_at = COALESCE(pool_frozen_at, CURRENT_TIMESTAMP) "
            "WHERE event_id = ?",
            (event_id,),
        )
        conn.commit()
    finally:
        conn.close()


def pool_counts(event_id: int) -> dict[str, int]:
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT card_key, COUNT(*) AS n FROM bingo_tasks WHERE event_id = ? GROUP BY card_key",
            (event_id,),
        ).fetchall()
        return {r["card_key"]: int(r["n"]) for r in rows}
    finally:
        conn.close()


def replace_pool(event_id: int, tasks_by_card: dict[str, list[str]]) -> dict[str, int]:
    """Replace the entire pool for an event. Refused once the pool is frozen.

    Returns the per-card count written.
    """
    conn = dao.connect()
    try:
        frozen = conn.execute(
            "SELECT pool_frozen_at FROM bingo_config WHERE event_id = ?", (event_id,)
        ).fetchone()
        if frozen is not None and frozen["pool_frozen_at"]:
            raise ValueError("The bingo task pool is locked (challenge already opened).")
        conn.execute("INSERT OR IGNORE INTO bingo_config (event_id) VALUES (?)", (event_id,))
        conn.execute("DELETE FROM bingo_tasks WHERE event_id = ?", (event_id,))
        counts: dict[str, int] = {}
        for card_key in CARD_KEYS:
            tasks = tasks_by_card.get(card_key, [])
            counts[card_key] = len(tasks)
            for position, text in enumerate(tasks):
                conn.execute(
                    "INSERT INTO bingo_tasks (event_id, card_key, position, text) "
                    "VALUES (?, ?, ?, ?)",
                    (event_id, card_key, position, text),
                )
        conn.commit()
        logger.info(f"replaced bingo pool for event {event_id}: {counts}")
        return counts
    finally:
        conn.close()


def get_pool(event_id: int) -> dict[str, list[dict]]:
    """Return the pool as ``{card_key: [{"id", "text"}, ...]}`` ordered by position."""
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT id, card_key, position, text FROM bingo_tasks "
            "WHERE event_id = ? ORDER BY card_key, position",
            (event_id,),
        ).fetchall()
    finally:
        conn.close()
    pool: dict[str, list[dict]] = {card: [] for card in CARD_KEYS}
    for r in rows:
        if is_card_key(r["card_key"]):
            pool[r["card_key"]].append({"id": int(r["id"]), "text": r["text"]})
    return pool


def get_crossings(event_id: int, user_id: str) -> set[tuple[str, int]]:
    """Return the set of (card_key, cell_index) this member has crossed off."""
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT card_key, cell_index FROM bingo_crossings "
            "WHERE event_id = ? AND discord_user_id = ?",
            (event_id, user_id),
        ).fetchall()
        return {(r["card_key"], int(r["cell_index"])) for r in rows}
    finally:
        conn.close()


def add_crossing(
    *,
    event_id: int,
    user_id: str,
    card_key: str,
    cell_index: int,
    task_id: int | None,
    submission_id: int | None,
    reason: str | None,
) -> bool:
    """Cross off one square. Returns True if newly inserted, False if already crossed."""
    conn = dao.connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bingo_crossings "
            "(event_id, discord_user_id, card_key, cell_index, task_id, submission_id, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, user_id, card_key, cell_index, task_id, submission_id, reason),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def crossings_for_submission(submission_id: int) -> list[dict]:
    """Squares a given check-in crossed: [{"card_key", "cell_index", "task_id", "reason"}]."""
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT card_key, cell_index, task_id, reason FROM bingo_crossings "
            "WHERE submission_id = ? ORDER BY created_at, card_key, cell_index",
            (submission_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_open_clarification(clarification_message_id: str) -> dict | None:
    """The bingo submission whose clarification question is this message and is still
    awaiting an answer, or None.

    Bingo check-ins can be ``approved`` (some squares crossed) while a question about
    the rest is still open, so this keys on the ``clarification_open`` flag in
    ``extracted_stats`` instead of ``status = 'pending'``.
    """
    conn = dao.connect()
    try:
        row = conn.execute(
            "SELECT * FROM submissions WHERE clarification_message_id = ? "
            "AND json_extract(extracted_stats, '$.bingo') = 1 "
            "AND json_extract(extracted_stats, '$.clarification_open') = 1 "
            "ORDER BY id LIMIT 1",
            (clarification_message_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_submission_result(submission_id: int, *, status: str, extracted_stats: dict) -> None:
    """Record the outcome of a bingo clarification re-check on its submission."""
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status {status!r}")
    conn = dao.connect()
    try:
        conn.execute(
            "UPDATE submissions SET status = ?, extracted_stats = ?, reviewed_by = 'bot', "
            "reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, json.dumps(extracted_stats), submission_id),
        )
        conn.commit()
    finally:
        conn.close()


def remove_crossing(*, event_id: int, user_id: str, card_key: str, cell_index: int) -> bool:
    """Un-cross one square. Returns True if a crossing was removed."""
    conn = dao.connect()
    try:
        cur = conn.execute(
            "DELETE FROM bingo_crossings WHERE event_id = ? AND discord_user_id = ? "
            "AND card_key = ? AND cell_index = ?",
            (event_id, user_id, card_key, cell_index),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# Board assembly (pool + crossings + scoring) for rendering and matching
# ---------------------------------------------------------------------------------------

from fcb.bingo import scoring as _scoring  # noqa: E402


def member_board_state(event_id: int, user_id: str, event_name: str = "") -> dict:
    """Assemble a member's six-card board: deterministic layout + crossings + scores.

    Returns a plain-dict structure consumed by the card renderer and the open-square
    matcher. A card with fewer than a full board of pool tasks is marked unavailable.
    """
    pool = get_pool(event_id)
    crossings = get_crossings(event_id, user_id)
    completed_by_card: dict[str, set[int]] = {}
    cards: list[dict] = []
    for card_key in _scoring.CARD_KEYS:
        tasks = pool.get(card_key, [])
        crossed_idx = {idx for (c, idx) in crossings if c == card_key}
        completed_by_card[card_key] = set(crossed_idx)
        if len(tasks) < _scoring.BOARD_TASK_COUNT:
            cards.append(
                {
                    "card_key": card_key,
                    "label": _scoring.CARD_LABELS[card_key],
                    "available": False,
                    "cells": [],
                    "pattern": None,
                    "points": 0,
                    "highlight": set(),
                }
            )
            continue
        layout = _scoring.derive_board(event_id, user_id, card_key, tasks)
        cells = [
            {
                "index": i,
                "text": layout[i]["text"],
                "task_id": layout[i]["id"],
                "crossed": i in crossed_idx,
            }
            for i in range(_scoring.BOARD_TASK_COUNT)
        ]
        cards.append(
            {
                "card_key": card_key,
                "label": _scoring.CARD_LABELS[card_key],
                "available": True,
                "cells": cells,
                "pattern": None,
                "points": 0,
                "highlight": set(),
            }
        )
    scored = _scoring.score_event(completed_by_card)
    total = scored.total
    for card in cards:
        cs = scored.cards[card["card_key"]]
        card["pattern"] = cs.pattern
        card["points"] = cs.points
        card["bonus"] = cs.bonus
        card["highlight"] = set(cs.highlight_cells)
    return {"event_id": event_id, "event_name": event_name, "cards": cards, "total": total}


def board_summary(board_state: dict) -> dict:
    """Compact per-card score summary of a board state, for the chat agent's context."""
    return {
        "total_points": board_state["total"],
        "cards": [
            {
                "card": card["label"],
                "squares_crossed": sum(1 for c in card["cells"] if c["crossed"]),
                "best_pattern": (
                    _scoring.PATTERN_LABELS[card["pattern"]] if card["pattern"] else None
                ),
                "bonus": card.get("bonus", 0),
                "points": card["points"],
            }
            for card in board_state["cards"]
            if card["available"]
        ],
    }


def open_squares(board_state: dict) -> list[dict]:
    """Flatten a board state into the still-open squares for the matcher."""
    result: list[dict] = []
    for card in board_state["cards"]:
        if not card["available"]:
            continue
        for cell in card["cells"]:
            if not cell["crossed"]:
                result.append(
                    {
                        "cell_key": f"{card['card_key']}:{cell['index']}",
                        "card_key": card["card_key"],
                        "cell_index": cell["index"],
                        "task_id": cell["task_id"],
                        "task": cell["text"],
                    }
                )
    return result


def participants(event_id: int) -> list[str]:
    """Members who have crossed at least one square."""
    conn = dao.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT discord_user_id FROM bingo_crossings WHERE event_id = ?",
            (event_id,),
        ).fetchall()
        return [r["discord_user_id"] for r in rows]
    finally:
        conn.close()


def leaderboard(event_id: int) -> list[tuple[str, int]]:
    """Return [(user_id, total_points)] sorted high-to-low, then by user id."""
    totals = [
        (uid, member_board_state(event_id, uid)["total"]) for uid in participants(event_id)
    ]
    totals.sort(key=lambda t: (-t[1], t[0]))
    return totals


def member_rank(event_id: int, user_id: str) -> int | None:
    """Competition rank (ties share a rank: 1,1,3). None if the member has no points."""
    board = leaderboard(event_id)
    rank = 0
    prior: int | None = None
    for index, (uid, total) in enumerate(board):
        if prior is None or total != prior:
            rank = index + 1
        prior = total
        if uid == user_id:
            return rank if total > 0 else None
    return None


def leaderboard_rows(event_id: int) -> list[dict]:
    """Points leaderboard with display names and competition ranks (ties share a rank).

    Returns [{"rank", "user_id", "name", "total"}] sorted high-to-low.
    """
    totals = leaderboard(event_id)  # [(user_id, total)] sorted desc, participants only
    if not totals:
        return []
    conn = dao.connect()
    try:
        placeholders = ",".join("?" for _ in totals)
        name_rows = conn.execute(
            f"SELECT discord_user_id, COALESCE(display_name, username) AS nm "
            f"FROM users WHERE discord_user_id IN ({placeholders})",
            [uid for uid, _ in totals],
        ).fetchall()
    finally:
        conn.close()
    names = {r["discord_user_id"]: r["nm"] for r in name_rows}
    rows: list[dict] = []
    rank = 0
    prior: int | None = None
    for index, (uid, total) in enumerate(totals):
        if prior is None or total != prior:
            rank = index + 1
        prior = total
        rows.append(
            {"rank": rank, "user_id": uid, "name": names.get(uid) or f"id {uid}", "total": total}
        )
    return rows
