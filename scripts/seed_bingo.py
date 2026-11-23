"""Seed a fitness-bingo challenge: create (or reuse) a challenge event, mark it bingo,
and load its six card task pools from an activities JSON file.

Usage:
    uv run python scripts/seed_bingo.py \
        --guild 1541521807511912528 --channel 1542005976389910619 \
        --name "November Fitness BINGO!" --starts "2026-11-01 00:00:00" \
        --ends "2026-12-01 05:59:59" --activities /tmp/activities.json

Idempotent: re-running with the same guild+name reuses the existing bingo event and
replaces its pools (allowed until the pool is frozen on first check-in).
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "src")

from fcb import config  # noqa: E402
from fcb.bingo import store as bingo_store  # noqa: E402
from fcb.bingo.scoring import BOARD_TASK_COUNT, CARD_KEYS  # noqa: E402
from fcb.db import dao  # noqa: E402

THEME_TO_CARD = {
    "upper body push": "chest_shoulders",
    "back": "back",
    "lower body": "legs",
    "arms": "arms",
    "core": "core",
    "cardio and conditioning": "cardio_conditioning",
}

BINGO_PROMPT = (
    "Fitness Bingo: a six-card board of single-session tasks across Chest & Shoulders, "
    "Back, Legs, Arms, Core, and Cardio & Conditioning. Post workout screenshots to cross "
    "off matching squares. Every square scores points, and patterns earn bonuses."
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guild", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--starts", required=True)
    parser.add_argument("--ends", required=True)
    parser.add_argument("--activities", required=True)
    args = parser.parse_args()

    dao.apply_migrations()

    groups = json.load(open(args.activities))
    tasks_by_card: dict[str, list[str]] = {card: [] for card in CARD_KEYS}
    for group in groups:
        card = THEME_TO_CARD.get(str(group["theme"]).strip().lower())
        if not card:
            raise SystemExit(f"Unmapped theme: {group['theme']!r}")
        for routine in group["routines"]:
            text = str(routine).strip()
            if text:
                tasks_by_card[card].append(text)
    for card in CARD_KEYS:
        n = len(tasks_by_card[card])
        if n < BOARD_TASK_COUNT:
            raise SystemExit(f"Card {card} has only {n} tasks; need >= {BOARD_TASK_COUNT}")

    # Reuse an existing bingo event with the same guild+name, else create one.
    conn = dao.connect()
    try:
        row = conn.execute(
            "SELECT e.id FROM events e JOIN bingo_config bc ON bc.event_id = e.id "
            "WHERE e.guild_id = ? AND e.name = ?",
            (args.guild, args.name),
        ).fetchone()
        event_id = int(row["id"]) if row else None
    finally:
        conn.close()

    if event_id is None:
        event_id = dao.create_event(
            guild_id=args.guild,
            channel_id=args.channel,
            kind="challenge",
            name=args.name,
            prompt=BINGO_PROMPT,
            starts_at=args.starts,
            ends_at=args.ends,
            created_by=config.FCB_DEVELOPER_USER_ID,
            primary_metric="submission_count",
        )
        bingo_store.ensure_config(event_id)
        print(f"created bingo event id={event_id}")
    else:
        print(f"reusing bingo event id={event_id}")

    if bingo_store.pool_frozen_at(event_id):
        raise SystemExit(f"event {event_id} pool is frozen; cannot reseed")
    counts = bingo_store.replace_pool(event_id, tasks_by_card)
    print("pools:", counts)
    print("done")


if __name__ == "__main__":
    main()
