#!/usr/bin/env python3
"""One-time backfill: re-run vision and route September submissions against the new events.

Run with:
    cd /home/ubuntu/discord_bot_fitness_challenge
    uv run python scripts/backfill_september.py

This script:
1. Loads all submissions from 2026-09-01 onward that have no event_id
2. Re-runs vision extraction on the stored images
3. Routes against the active September events
4. Updates submission status to approved and links to matched events
"""

import json
import logging
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fcb import config
from fcb.db import dao
from fcb.agents import router, vision

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _sniff_format(image_bytes: bytes) -> str | None:
    """Detect image format from magic bytes."""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if image_bytes[:2] == b"\xff\xd8":
        return "jpeg"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    return None


def main() -> None:
    # Get the September events
    guild_id = "1541521807511912528"
    events = dao.list_active_events(guild_id)
    
    if not events:
        logger.error("No active events found for guild")
        return
    
    logger.info(f"Found {len(events)} active events:")
    for e in events:
        logger.info(f"  - {e['id']}: {e['name']} ({e['primary_metric']})")
    
    # Get submissions since Sept 1st with no event_id
    conn = dao.connect()
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.discord_user_id, s.message_id, s.posted_at, 
                   s.extracted_stats, s.status, s.image_path,
                   u.display_name
              FROM submissions s
              LEFT JOIN users u ON s.discord_user_id = u.discord_user_id
             WHERE s.posted_at >= '2026-09-01'
               AND s.event_id IS NULL
             ORDER BY s.id
            """
        ).fetchall()
    finally:
        conn.close()
    
    submissions = [dict(r) for r in rows]
    logger.info(f"Found {len(submissions)} submissions to process")
    
    # Group submissions by message_id prefix (multi-image posts have _0, _1 suffixes)
    by_message: dict[str, list[dict]] = {}
    for sub in submissions:
        msg_id = sub["message_id"].split("_")[0]
        by_message.setdefault(msg_id, []).append(sub)
    
    logger.info(f"Grouped into {len(by_message)} unique messages")
    
    processed = 0
    approved = 0
    skipped = 0
    
    # Use Fitness Junkie prompt as the primary hint for vision
    primary_event = next((e for e in events if e["kind"] == "challenge"), events[0])
    
    for msg_id, subs in by_message.items():
        first_sub = subs[0]
        user_name = first_sub["display_name"] or first_sub["discord_user_id"]
        
        # Gather all images for this message
        images: list[tuple[bytes, str]] = []
        sub_ids: list[int] = []
        
        for sub in sorted(subs, key=lambda s: s["id"]):
            image_path = sub["image_path"]
            if not image_path:
                continue
            
            full_path = config.REPO_ROOT / image_path
            if not full_path.exists():
                logger.warning(f"  #{sub['id']}: image not found at {full_path}")
                continue
            
            image_bytes = full_path.read_bytes()
            fmt = _sniff_format(image_bytes)
            if not fmt:
                logger.warning(f"  #{sub['id']}: unknown image format")
                continue
            
            images.append((image_bytes, fmt))
            sub_ids.append(sub["id"])
        
        if not images:
            logger.info(f"  message {msg_id} ({user_name}): no valid images, skipping")
            skipped += len(subs)
            continue
        
        # Run vision extraction
        logger.info(f"  message {msg_id} ({user_name}): running vision on {len(images)} image(s)...")
        try:
            batch = vision.extract(
                images=images,
                event_prompt=primary_event["prompt"],
                caption=first_sub.get("raw_text"),
            )
        except Exception as e:
            logger.error(f"  message {msg_id}: vision failed: {e}")
            skipped += len(subs)
            continue
        
        # Process each session
        for session in batch.sessions:
            if not session.is_workout_screenshot:
                logger.info(f"  message {msg_id} ({user_name}): not a workout screenshot")
                skipped += 1
                continue
            
            stats = session.model_dump()
            
            # Route against active events
            logger.info(f"  message {msg_id} ({user_name}): routing workout...")
            try:
                decision = router.route_submission(stats=stats, events=events)
            except Exception as e:
                logger.error(f"  message {msg_id}: router failed: {e}")
                skipped += 1
                continue
            
            if not decision.matched_event_ids:
                logger.info(f"  message {msg_id} ({user_name}): no events matched - {decision.reasoning}")
                skipped += 1
                continue
            
            # Update the first submission in this message group
            sub_id = sub_ids[0] if sub_ids else subs[0]["id"]
            primary_event_id = decision.matched_event_ids[0]
            logger.info(
                f"  #{sub_id} ({user_name}): matched events {decision.matched_event_ids} "
                f"- {decision.reasoning}"
            )
            
            conn = dao.connect()
            try:
                # Update status, event_id, and extracted_stats
                conn.execute(
                    """
                    UPDATE submissions 
                       SET status = 'approved', 
                           event_id = ?,
                           extracted_stats = ?,
                           reviewed_by = 'backfill',
                           reviewed_at = CURRENT_TIMESTAMP
                     WHERE id = ?
                    """,
                    (primary_event_id, json.dumps(stats), sub_id),
                )
                conn.commit()
            finally:
                conn.close()
            
            # Link to all matched events via submission_events junction
            for i, event_id in enumerate(decision.matched_event_ids):
                dao.link_submission_to_event(
                    submission_id=sub_id,
                    event_id=event_id,
                    is_primary=(i == 0),
                )
            
            processed += 1
            approved += 1
            
            # Record in bot_events
            dao.record_event(
                level="INFO",
                category="backfill.approved",
                message=f"Backfill approved submission #{sub_id} for {user_name}",
                actor="backfill",
                guild_id=guild_id,
                context={
                    "submission_id": sub_id,
                    "matched_events": decision.matched_event_ids,
                    "reasoning": decision.reasoning,
                },
            )
    
    logger.info(f"Done. Processed: {processed}, Approved: {approved}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
