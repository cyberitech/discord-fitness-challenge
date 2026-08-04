"""Cleanup script for multi-image submission groups.

Every message that produced N>1 submission rows under the old
per-image pipeline is now re-analyzed with the new multi-image vision
flow. The script runs in DIAGNOSE mode by default — it prints what
each group looks like now, what vision would produce today, and what
the proposed cleanup action is, WITHOUT touching the DB.

Pass a group's base_message_id with --apply to actually execute the
cleanup for that one group. The script will:
  - Compute phashes for all images in the group and populate the
    submission_images rows in place.
  - Re-run vision on the images together.
  - If vision returns a single merged session, consolidate into the
    lowest submission id: update its extracted_stats/status/event_id
    to the merged result, link all images to it, and mark the other
    submissions as 'rejected' with a note explaining the merge.
  - If vision returns multiple sessions, keep separate submissions and
    update each with the correct stats + image links.
  - If vision says "not a workout", flip everything to 'rejected'.

The script deliberately does NOT compute duplicates across message
groups — that's a separate follow-up. Cleanup targets ambiguity
inside a single Discord message, not across-message duplicate posts.

Usage:
    uv run python scripts/cleanup_multi_image_submissions.py           # diagnose all
    uv run python scripts/cleanup_multi_image_submissions.py 1533653... # diagnose one
    uv run python scripts/cleanup_multi_image_submissions.py 1533653... --apply
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Add repo root to path so we can import fcb
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Imports must happen after path setup
from fcb import config  # noqa: E402
from fcb.agents import vision  # noqa: E402
from fcb.db import dao  # noqa: E402
from PIL import Image  # noqa: E402
import imagehash  # noqa: E402


def phash_of(image_bytes: bytes) -> str:
    import io
    img = Image.open(io.BytesIO(image_bytes))
    return str(imagehash.phash(img))


def load_image(rel_path: str) -> tuple[bytes, str] | None:
    full = config.REPO_ROOT / rel_path
    if not full.exists():
        return None
    ext = full.suffix.lower()
    fmt_map = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
    return full.read_bytes(), fmt_map.get(ext, "jpeg")


def find_multi_image_groups() -> list[dict]:
    """Return every base message id with 2+ submission rows."""
    conn = sqlite3.connect(config.FCB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT SUBSTR(message_id, 1, INSTR(message_id, '_') - 1) AS base,
                   COUNT(*) AS n
              FROM submissions
             WHERE INSTR(message_id, '_') > 0
             GROUP BY base
             HAVING n > 1
             ORDER BY base
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_group_submissions(base_msg: str) -> list[dict]:
    conn = sqlite3.connect(config.FCB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT * FROM submissions
             WHERE message_id LIKE ?
             ORDER BY message_id
        """, (base_msg + '%',)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def print_current_state(subs: list[dict]) -> None:
    print("  CURRENT DB STATE:")
    for s in subs:
        stats = json.loads(s["extracted_stats"]) if s["extracted_stats"] else {}
        parts = []
        for k in ("distance_miles", "elevation_gain_feet", "duration_seconds",
                  "reps", "weight_lb", "sets", "volume_lb"):
            if stats.get(k) is not None:
                parts.append(f"{k}={stats[k]}")
        print(f"    #{s['id']} msg={s['message_id']} status={s['status']} event={s['event_id']}")
        print(f"      image: {s['image_path']}")
        print(f"      stats: {', '.join(parts) if parts else '(empty)'}")


def diagnose_group(subs: list[dict]) -> dict:
    """Return a dict describing what cleanup would do."""
    # Load images
    images: list[tuple[bytes, str]] = []
    missing: list[str] = []
    hashes: list[str] = []
    for s in subs:
        loaded = load_image(s["image_path"])
        if loaded is None:
            missing.append(s["image_path"])
            continue
        images.append(loaded)
        hashes.append(phash_of(loaded[0]))

    if missing:
        return {"error": f"missing images: {missing}"}

    # Need an event prompt — use the first submission's event, or fall back
    # to any active event, or bail
    event_id = next((s["event_id"] for s in subs if s["event_id"]), None)
    if event_id:
        event = dao.get_event(event_id)
        event_prompt = event["prompt"]
        event_name = event["name"]
    else:
        active = dao.list_active_events(subs[0]["guild_id"])
        if not active:
            return {"error": "no active event to attribute against"}
        event = active[0]
        event_prompt = event["prompt"]
        event_name = event["name"]

    print(f"  Re-analyzing against event: {event_name}")
    batch = vision.extract(images, event_prompt)

    return {
        "event_id": event_id or event["id"],
        "event_name": event_name,
        "hashes": hashes,
        "batch": batch,
    }


def print_proposed_state(diagnosis: dict, subs: list[dict]) -> None:
    if "error" in diagnosis:
        print(f"  ERROR: {diagnosis['error']}")
        return

    batch = diagnosis["batch"]
    print(f"  PROPOSED NEW STATE:")
    print(f"    sessions: {len(batch.sessions)}  needs_clarification: {batch.needs_clarification}")
    print(f"    reasoning: {batch.reasoning}")
    if batch.clarification_question:
        print(f"    clarification: {batch.clarification_question}")

    for i, session in enumerate(batch.sessions):
        parts = []
        for k in ("distance_miles", "elevation_gain_feet", "duration_seconds",
                  "reps", "weight_lb", "sets", "volume_lb"):
            v = getattr(session, k, None)
            if v is not None:
                parts.append(f"{k}={v}")
        img_paths = [subs[idx]["image_path"] for idx in session.image_indices if 0 <= idx < len(subs)]
        print(f"    Session {i}: images={session.image_indices} type={session.workout_type} conf={session.confidence:.2f}")
        print(f"      stats: {', '.join(parts) if parts else '(empty)'}")
        print(f"      linked images: {img_paths}")


def apply_cleanup(subs: list[dict], diagnosis: dict) -> None:
    """Apply the proposed cleanup for one group.

    Strategy:
    - Compute phashes for every image, populate submission_images
      with the real hash (overwrite the backfilled empty hash).
    - Sort subs by id ascending so the earliest id is the "keeper".
    - For each session vision returned, pick a keeper submission
      (in-order) and update its stats/event/status. Link every image
      in image_indices to that keeper.
    - Any submission not assigned to a keeper role gets marked
      'rejected' with a note.
    """
    conn = sqlite3.connect(config.FCB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        batch = diagnosis["batch"]
        subs_sorted = sorted(subs, key=lambda s: s["id"])

        # Step 1: refresh submission_images with real hashes
        for s, h in zip(subs_sorted, diagnosis["hashes"]):
            # There should already be a row from the backfill; update it
            conn.execute("""
                UPDATE submission_images
                   SET image_hash = ?
                 WHERE submission_id = ? AND position = 0
            """, (h, s["id"]))

        # Step 2: assign sessions to keepers
        used_keeper_ids: set[int] = set()
        assignments: list[dict] = []
        for sess_idx, session in enumerate(batch.sessions):
            # Pick the lowest sub id whose image_index we own
            candidates = [subs_sorted[i] for i in session.image_indices if 0 <= i < len(subs_sorted)]
            keeper = None
            for c in candidates:
                if c["id"] not in used_keeper_ids:
                    keeper = c
                    break
            if keeper is None:
                # Shouldn't happen, but skip if it does
                continue
            used_keeper_ids.add(keeper["id"])
            assignments.append({
                "session": session,
                "keeper": keeper,
                "image_subs": candidates,
            })

        # Step 3: for each session, update keeper and link images
        # Also determine event mapping via router
        from fcb.agents import router
        active_events = dao.list_active_events(subs_sorted[0]["guild_id"]) or [
            dao.get_event(diagnosis["event_id"])
        ]

        for a in assignments:
            session = a["session"]
            keeper = a["keeper"]
            image_subs = a["image_subs"]

            matched_event_ids: list[int] = []
            if session.is_workout_screenshot:
                decision = router.route_submission(
                    stats=session.model_dump(), events=active_events
                )
                matched_event_ids = decision.matched_event_ids

            recognized = session.is_workout_screenshot and bool(matched_event_ids)
            new_status = "approved" if recognized else "rejected"
            primary_event_id = matched_event_ids[0] if matched_event_ids else None

            conn.execute("""
                UPDATE submissions
                   SET status = ?, event_id = ?, extracted_stats = ?,
                       reviewed_by = 'cleanup', reviewed_at = CURRENT_TIMESTAMP
                 WHERE id = ?
            """, (new_status, primary_event_id, json.dumps(session.model_dump()), keeper["id"]))

            # Wipe old submission_events links for the keeper, re-add fresh
            conn.execute("DELETE FROM submission_events WHERE submission_id = ?", (keeper["id"],))
            for i, eid in enumerate(matched_event_ids):
                conn.execute("""
                    INSERT OR IGNORE INTO submission_events (submission_id, event_id, is_primary)
                    VALUES (?, ?, ?)
                """, (keeper["id"], eid, 1 if i == 0 else 0))

            # Link every image in this session to the keeper
            # First drop existing submission_images rows for these image subs
            for isub in image_subs:
                conn.execute("DELETE FROM submission_images WHERE submission_id = ?", (isub["id"],))

            # Now re-add rows only under the keeper, in the session's image order
            for pos, idx in enumerate(session.image_indices):
                if not (0 <= idx < len(subs_sorted)):
                    continue
                img_sub = subs_sorted[idx]
                hash_at_idx = diagnosis["hashes"][idx]
                conn.execute("""
                    INSERT INTO submission_images
                        (submission_id, image_path, image_hash, position)
                    VALUES (?, ?, ?, ?)
                """, (keeper["id"], img_sub["image_path"], hash_at_idx, pos))

        # Step 4: any submission not a keeper gets rejected with a note
        non_keepers = [s for s in subs_sorted if s["id"] not in used_keeper_ids]
        for s in non_keepers:
            # Find which keeper's session claimed this image
            claimed_by = None
            for a in assignments:
                if s["id"] in [c["id"] for c in a["image_subs"]]:
                    claimed_by = a["keeper"]["id"]
                    break
            note = (
                f"merged into submission #{claimed_by} by cleanup"
                if claimed_by
                else "no session claimed this image after cleanup re-analysis"
            )
            merged_stats = {
                "is_workout_screenshot": False,
                "notes": note,
                "confidence": 0.0,
            }
            conn.execute("""
                UPDATE submissions
                   SET status = 'rejected', event_id = NULL,
                       extracted_stats = ?,
                       reviewed_by = 'cleanup', reviewed_at = CURRENT_TIMESTAMP
                 WHERE id = ?
            """, (json.dumps(merged_stats), s["id"]))
            # Drop old event links
            conn.execute("DELETE FROM submission_events WHERE submission_id = ?", (s["id"],))

        conn.commit()

        # Report
        print("\n  APPLIED CHANGES:")
        for a in assignments:
            print(f"    Kept #{a['keeper']['id']} (session with images {a['session'].image_indices})")
        for s in non_keepers:
            print(f"    Rejected #{s['id']} (merged/dropped by cleanup)")

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_msg", nargs="?", help="Base message id to process; omit to list all groups")
    parser.add_argument("--apply", action="store_true", help="Apply the cleanup (default: diagnose only)")
    args = parser.parse_args()

    groups = find_multi_image_groups()

    if args.base_msg is None:
        print(f"Found {len(groups)} multi-image message groups.\n")
        for g in groups:
            base = g["base"]
            n = g["n"]
            subs = get_group_submissions(base)
            print(f"=== base_msg={base} (n={n}) ===")
            print_current_state(subs)
            print()
        print("\nRun again with a specific base_msg to diagnose that group in detail.")
        print("Add --apply to a specific base_msg to apply the cleanup.")
        return

    subs = get_group_submissions(args.base_msg)
    if not subs:
        print(f"No submissions found for base message {args.base_msg}")
        return

    print(f"=== Group base_msg={args.base_msg} (n={len(subs)}) ===\n")
    print_current_state(subs)
    print()

    diagnosis = diagnose_group(subs)
    print_proposed_state(diagnosis, subs)

    if not args.apply:
        print("\n(Diagnose only. Add --apply to execute.)")
        return

    if "error" in diagnosis:
        print("\nCannot apply: diagnosis errored.")
        return

    apply_cleanup(subs, diagnosis)
    print("\nDone.")


if __name__ == "__main__":
    main()
