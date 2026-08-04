"""SQLite connection factory and migration runner for FCB.

Owns the single source of truth for how the database is opened (WAL
mode, foreign keys on, Row factory, path from config) and how migrations
under ``src/fcb/db/migrations/`` are discovered and applied.

Migration files are named ``NNN_description.sql``. Their ``stem`` is the
version key stored in ``schema_migrations``; the runner applies any file
whose version has not yet been recorded, in lexical order.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from fcb import config

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def connect() -> sqlite3.Connection:
    """Open a connection to the FCB database with the standard pragmas."""
    config.FCB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.FCB_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def apply_migrations() -> int:
    """Apply un-applied migrations in lexical order. Returns the count."""
    conn = connect()
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}

        migrations = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if not migrations:
            logger.warning(f"no migration files found under {_MIGRATIONS_DIR}")
            return 0

        count = 0
        for migration in migrations:
            version = migration.stem
            if version in applied:
                logger.debug(f"migration {version} already applied, skipping")
                continue

            logger.info(f"applying migration {version} from {migration.name}")
            sql = migration.read_text()
            # Bundle the record-insert into the same script so the whole
            # migration commits atomically.
            script = (
                sql
                + f"\nINSERT INTO schema_migrations (version) VALUES ('{version}');\n"
            )
            conn.executescript(script)
            count += 1
            logger.info(f"migration {version} complete")

        logger.info(f"migrations up to date — applied {count} this run")
        return count
    finally:
        conn.close()


def record_event(
    level: str,
    category: str,
    message: str,
    actor: str | None = None,
    context: Mapping[str, Any] | None = None,
    guild_id: str | None = None,
) -> None:
    """Append one row to bot_events.

    This is the dashboard-facing structured log (see architecture.md and
    logging-standards.md). Call it at meaningful decision points and on
    external-system boundaries.

    ``guild_id`` scopes the event to a specific guild. Cross-guild
    events (bot.ready, guild.joined) leave it null; the dashboard's
    logs page filters by the currently-selected guild plus null-guild
    rows so admins see both scopes.

    ``context`` is stored as a JSON string; keep it small and JSON-safe.
    """
    if level not in _VALID_LEVELS:
        raise ValueError(f"invalid level {level!r}; expected one of {_VALID_LEVELS}")
    context_json = json.dumps(context) if context is not None else None
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO bot_events (level, category, actor, message, context, guild_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (level, category, actor, message, context_json, guild_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# bot_guilds registry
# ---------------------------------------------------------------------------


def upsert_bot_guild(
    *, guild_id: str, name: str, icon_hash: str | None = None
) -> None:
    """Insert or refresh the bot_guilds registry row for a guild."""
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO bot_guilds (guild_id, name, icon_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                name = excluded.name,
                icon_hash = excluded.icon_hash,
                last_refreshed_at = CURRENT_TIMESTAMP
            """,
            (guild_id, name, icon_hash),
        )
        conn.commit()
    finally:
        conn.close()


def delete_bot_guild(guild_id: str) -> None:
    """Remove a guild registry row. Cascades to guild_cache and guild_settings."""
    conn = connect()
    try:
        conn.execute("DELETE FROM bot_guilds WHERE guild_id = ?", (guild_id,))
        conn.commit()
    finally:
        conn.close()


def list_bot_guilds() -> list[dict[str, Any]]:
    """All guilds the bot is currently registered in, oldest-first."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM bot_guilds ORDER BY joined_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_bot_guild(guild_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM bot_guilds WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_active_events(guild_id: str) -> list[dict[str, Any]]:
    """All events currently within their window for this guild.

    Filters by guild only. Events keep the channel_id they were created
    against for reference / display, but the guild's bound channel can
    move over time via settings, so filtering by channel would hide
    legitimate events posted against a prior channel.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
             WHERE guild_id = ?
               AND datetime('now') BETWEEN datetime(starts_at) AND datetime(ends_at)
             ORDER BY id DESC
            """,
            (guild_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_active_event(guild_id: str) -> dict[str, Any] | None:
    """Convenience: first active event or None. Prefer ``list_active_events``
    when the caller may need to reason about overlaps."""
    events = list_active_events(guild_id)
    return events[0] if events else None


def link_submission_to_event(
    *, submission_id: int, event_id: int, is_primary: bool = False
) -> None:
    """Attach a submission to an event in the junction table.

    Idempotent — the ``INSERT OR IGNORE`` treats duplicate links as a
    no-op. The AFTER-INSERT trigger in migration 006 will also add the
    submission's author to ``event_participants`` if the submission is
    already approved.
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO submission_events "
            "(submission_id, event_id, is_primary) VALUES (?, ?, ?)",
            (submission_id, event_id, 1 if is_primary else 0),
        )
        conn.commit()
    finally:
        conn.close()


def insert_submission_images(
    *,
    submission_id: int,
    images: list[tuple[str, str]],
) -> None:
    """Attach a list of images to a submission.

    Args:
        submission_id: the parent submission id.
        images: list of ``(image_path, image_hash)`` tuples. Order in
            the list defines the ``position`` column (0-based).

    Idempotent by ``(submission_id, position)``: re-inserting the same
    position for the same submission is a no-op via UNIQUE constraint.
    """
    conn = connect()
    try:
        for position, (image_path, image_hash) in enumerate(images):
            conn.execute(
                """
                INSERT OR IGNORE INTO submission_images
                    (submission_id, image_path, image_hash, position)
                VALUES (?, ?, ?, ?)
                """,
                (submission_id, image_path, image_hash, position),
            )
        conn.commit()
    finally:
        conn.close()


def get_submission_images(submission_id: int) -> list[dict[str, Any]]:
    """Return the image rows for a submission, ordered by position."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, submission_id, image_path, image_hash, position
              FROM submission_images
             WHERE submission_id = ?
             ORDER BY position
            """,
            (submission_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_duplicate_hashes(hashes: list[str]) -> dict[str, int]:
    """Look up which of the given hashes already exist on an APPROVED submission.

    Empty strings (historical backfill placeholders) are ignored — they
    can never match a real incoming hash.

    Returns a mapping from matched hash → submission_id of the earliest
    approved submission carrying that hash. Hashes with no match are
    absent from the returned dict.
    """
    real_hashes = [h for h in hashes if h]
    if not real_hashes:
        return {}
    placeholders = ",".join("?" for _ in real_hashes)
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT si.image_hash AS hash, MIN(si.submission_id) AS submission_id
              FROM submission_images si
              JOIN submissions s ON s.id = si.submission_id
             WHERE si.image_hash IN ({placeholders})
               AND si.image_hash != ''
               AND s.status = 'approved'
             GROUP BY si.image_hash
            """,
            real_hashes,
        ).fetchall()
        return {r["hash"]: r["submission_id"] for r in rows}
    finally:
        conn.close()


def set_clarification_message_id(submission_id: int, message_id: str) -> None:
    """Store the bot's clarification-question message id on a submission."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE submissions SET clarification_message_id = ? WHERE id = ?",
            (message_id, submission_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_submissions_by_clarification_message(
    message_id: str,
) -> list[dict[str, Any]]:
    """Return every pending submission tied to a bot clarification message.

    A single clarification message may cover multiple submissions when
    the batch resolved to more than one session, so we return a list.
    Only ``pending`` submissions are returned — once resolved, the
    clarification_message_id stays on the row for audit but the query
    filter drops it.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM submissions
             WHERE clarification_message_id = ?
               AND status = 'pending'
            """,
            (message_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_submission(
    *,
    guild_id: str,
    event_id: int | None,
    discord_user_id: str,
    message_id: str,
    channel_id: str,
    posted_at: str,
    image_path: str | None,
    raw_text: str | None,
    extracted_stats: Mapping[str, Any] | None,
    status: str = "pending",
) -> int:
    """Insert a submission row and return its id.

    ``guild_id`` is always populated — orphan submissions (event_id
    NULL because no active event matched) still carry their guild
    scope so the dashboard's guild filter can find them.

    ``status`` defaults to ``pending`` to match the schema, but the bot
    passes ``approved`` or ``rejected`` explicitly based on the vision
    agent's classification.
    """
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status {status!r}")
    stats_json = json.dumps(extracted_stats) if extracted_stats is not None else None
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO submissions (
                guild_id, event_id, discord_user_id, message_id, channel_id,
                posted_at, image_path, raw_text, extracted_stats, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                event_id,
                discord_user_id,
                message_id,
                channel_id,
                posted_at,
                image_path,
                raw_text,
                stats_json,
                status,
            ),
        )
        conn.commit()
        submission_id = cur.lastrowid
        assert submission_id is not None
        return submission_id
    finally:
        conn.close()


def is_admin(discord_user_id: str) -> bool:
    """True if the user is on the dashboard/debug allowlist."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE discord_user_id = ?", (discord_user_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_events(guild_id: str) -> list[dict[str, Any]]:
    """All events for this guild, newest first."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE guild_id = ? ORDER BY id DESC",
            (guild_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_event(event_id: int) -> dict[str, Any] | None:
    """One event by id, or None."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


VALID_PRIMARY_METRICS = (
    "submission_count",
    "duration_seconds",
    "distance_miles",
    "elevation_gain_feet",
    "calories",
    "reps",
    "volume_lb",
)


def create_event(
    *,
    guild_id: str,
    channel_id: str,
    kind: str,
    name: str,
    prompt: str,
    starts_at: str,
    ends_at: str,
    created_by: str,
    primary_metric: str = "submission_count",
    nag_threshold_days: int = 3,
) -> int:
    """Insert a new event and return its id."""
    if kind not in ("daily_1pct", "challenge"):
        raise ValueError(f"invalid kind {kind!r}")
    if primary_metric not in VALID_PRIMARY_METRICS:
        raise ValueError(f"invalid primary_metric {primary_metric!r}")
    if nag_threshold_days < 0:
        raise ValueError(f"nag_threshold_days must be >= 0, got {nag_threshold_days}")
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO events (
                guild_id, channel_id, kind, name, prompt,
                starts_at, ends_at, created_by, primary_metric,
                nag_threshold_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                kind,
                name,
                prompt,
                starts_at,
                ends_at,
                created_by,
                primary_metric,
                nag_threshold_days,
            ),
        )
        conn.commit()
        event_id = cur.lastrowid
        assert event_id is not None
        return event_id
    finally:
        conn.close()


def update_event(
    event_id: int,
    *,
    kind: str,
    name: str,
    prompt: str,
    starts_at: str,
    ends_at: str,
    primary_metric: str,
    nag_threshold_days: int,
) -> None:
    """Update the mutable fields of an event."""
    if kind not in ("daily_1pct", "challenge"):
        raise ValueError(f"invalid kind {kind!r}")
    if primary_metric not in VALID_PRIMARY_METRICS:
        raise ValueError(f"invalid primary_metric {primary_metric!r}")
    if nag_threshold_days < 0:
        raise ValueError(f"nag_threshold_days must be >= 0, got {nag_threshold_days}")
    conn = connect()
    try:
        cur = conn.execute(
            """
            UPDATE events
               SET kind = ?, name = ?, prompt = ?, starts_at = ?, ends_at = ?,
                   primary_metric = ?, nag_threshold_days = ?
             WHERE id = ?
            """,
            (
                kind, name, prompt, starts_at, ends_at,
                primary_metric, nag_threshold_days, event_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"event {event_id} not found")
    finally:
        conn.close()


def upsert_user(
    *,
    discord_user_id: str,
    username: str,
    display_name: str | None,
    avatar_url: str | None,
) -> None:
    """Insert or update the user cache row and bump ``last_seen_at``.

    Called from both the bot (on message observation) and the dashboard
    (on OAuth callback).
    """
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO users (discord_user_id, username, display_name, avatar_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                avatar_url = excluded.avatar_url,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (discord_user_id, username, display_name, avatar_url),
        )
        conn.commit()
    finally:
        conn.close()


def get_user(discord_user_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE discord_user_id = ?", (discord_user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_submissions(
    *,
    guild_id: str | None = None,
    event_id: int | None = None,
    status: str | None = None,
    include_non_workout: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Recent submissions joined with user + event context.

    ``guild_id`` restricts the query to submissions in that guild.
    When ``event_id`` is provided, filter through the many-to-many
    ``submission_events`` junction so submissions attributed to that
    event (primary OR secondary) are returned.

    ``include_non_workout`` defaults to False, hiding submissions whose
    vision extraction explicitly flagged them as
    ``is_workout_screenshot=false`` (body pics, memes, food photos,
    etc.). Set True to include them for admin audit.
    """
    where: list[str] = []
    params: list[Any] = []
    joins = ""
    if guild_id is not None:
        where.append("s.guild_id = ?")
        params.append(guild_id)
    if event_id is not None:
        joins += " JOIN submission_events se ON se.submission_id = s.id"
        where.append("se.event_id = ?")
        params.append(event_id)
    if status is not None:
        if status not in ("pending", "approved", "rejected"):
            raise ValueError(f"invalid status {status!r}")
        where.append("s.status = ?")
        params.append(status)
    if not include_non_workout:
        # Hide rows where vision explicitly said this is not a workout.
        # Rows with NULL extracted_stats or a missing key stay visible —
        # only the definitively-not-workout images get filtered out.
        where.append(
            "COALESCE(json_extract(s.extracted_stats, '$.is_workout_screenshot'), 1) = 1"
        )
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT s.*,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url,
                   e.name AS event_name,
                   e.kind AS event_kind
              FROM submissions s
              LEFT JOIN users u ON u.discord_user_id = s.discord_user_id
              LEFT JOIN events e ON e.id = s.event_id
              {joins}
              {where_sql}
             ORDER BY s.id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_recent_submissions_by_user(
    *, discord_user_id: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Most recent submissions from a user, any status, joined with event name.

    Used by the @-mention chat path so the bot can answer "why didn't
    you count my last post?" with the actual vision notes / confidence
    / rejection status. Ordered newest-first by created_at.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.*,
                   e.name AS event_name
              FROM submissions s
              LEFT JOIN events e ON e.id = s.event_id
             WHERE s.discord_user_id = ?
             ORDER BY s.id DESC
             LIMIT ?
            """,
            (discord_user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_submission(submission_id: int) -> dict[str, Any] | None:
    """One submission joined with user + event context, or None."""
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT s.*,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url,
                   e.name AS event_name,
                   e.kind AS event_kind,
                   e.prompt AS event_prompt
              FROM submissions s
              LEFT JOIN users u ON u.discord_user_id = s.discord_user_id
              LEFT JOIN events e ON e.id = s.event_id
             WHERE s.id = ?
            """,
            (submission_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_submission_status(
    submission_id: int, *, status: str, reviewed_by: str
) -> None:
    """Flip a submission's status. Records reviewer + timestamp."""
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status {status!r}")
    conn = connect()
    try:
        cur = conn.execute(
            """
            UPDATE submissions
               SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (status, reviewed_by, submission_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"submission {submission_id} not found")
    finally:
        conn.close()


def update_submission_after_clarification(
    submission_id: int,
    *,
    status: str,
    event_id: int | None,
    extracted_stats: Mapping[str, Any],
    reviewed_by: str = "bot",
) -> None:
    """Finalize a pending submission after clarification.

    Updates status, event_id (may change based on new understanding),
    and extracted_stats (may change if vision re-interpreted the batch).
    Marks reviewed_at.
    """
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status {status!r}")
    conn = connect()
    try:
        cur = conn.execute(
            """
            UPDATE submissions
               SET status = ?, event_id = ?, extracted_stats = ?,
                   reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (
                status,
                event_id,
                json.dumps(extracted_stats),
                reviewed_by,
                submission_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"submission {submission_id} not found")
    finally:
        conn.close()


def reset_event(event_id: int) -> dict[str, Any]:
    """Delete submissions, metrics, and participants for an event.

    Returns counts plus the list of image paths so the caller can also
    remove screenshot files from disk. Keeps the event row itself.
    """
    conn = connect()
    try:
        image_paths = [
            r["image_path"]
            for r in conn.execute(
                "SELECT image_path FROM submissions "
                "WHERE event_id = ? AND image_path IS NOT NULL",
                (event_id,),
            )
        ]
        sub_count = conn.execute(
            "DELETE FROM submissions WHERE event_id = ?", (event_id,)
        ).rowcount
        metrics_count = conn.execute(
            "DELETE FROM event_metrics WHERE event_id = ?", (event_id,)
        ).rowcount
        parts_count = conn.execute(
            "DELETE FROM event_participants WHERE event_id = ?", (event_id,)
        ).rowcount
        conn.commit()
        return {
            "submissions_deleted": sub_count,
            "metrics_deleted": metrics_count,
            "participants_deleted": parts_count,
            "image_paths": image_paths,
        }
    finally:
        conn.close()


def delete_event(event_id: int) -> dict[str, Any]:
    """Reset event data and then delete the event row itself."""
    result = reset_event(event_id)
    conn = connect()
    try:
        row_count = conn.execute(
            "DELETE FROM events WHERE id = ?", (event_id,)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    result["event_deleted"] = row_count
    return result


def list_admins() -> list[dict[str, Any]]:
    """All admins joined with cached user identity, newest first."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT a.*,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url
              FROM admins a
              LEFT JOIN users u ON u.discord_user_id = a.discord_user_id
             ORDER BY a.added_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_admin(*, discord_user_id: str, added_by: str, note: str | None = None) -> bool:
    """Add a Discord user to the admin allowlist. Returns True if new."""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO admins (discord_user_id, added_by, note) VALUES (?, ?, ?)",
            (discord_user_id, added_by, note),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_admin(discord_user_id: str) -> None:
    """Remove an admin. Refuses to remove the last remaining admin."""
    conn = connect()
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM admins WHERE discord_user_id != ?", (discord_user_id,)
        ).fetchone()[0]
        if remaining == 0:
            raise ValueError("cannot remove the last admin")
        cur = conn.execute(
            "DELETE FROM admins WHERE discord_user_id = ?", (discord_user_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"admin {discord_user_id} not found")
    finally:
        conn.close()


def list_users(limit: int = 200) -> list[dict[str, Any]]:
    """Recently seen users, for admin-add autocomplete etc."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_bot_events(
    *,
    guild_id: str | None = None,
    level: str | None = None,
    category: str | None = None,
    actor: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Recent bot_events rows for the dashboard logs panel.

    When ``guild_id`` is provided, filter to rows scoped to that guild
    plus rows where ``guild_id`` is null (cross-guild events like
    ``bot.ready``). This makes the logs page show both scopes without
    the admin having to switch views.
    """
    where: list[str] = []
    params: list[Any] = []
    if guild_id is not None:
        where.append("(guild_id = ? OR guild_id IS NULL)")
        params.append(guild_id)
    if level:
        if level not in _VALID_LEVELS:
            raise ValueError(f"invalid level {level!r}")
        where.append("level = ?")
        params.append(level)
    if category:
        where.append("category LIKE ?")
        params.append(f"{category}%")
    if actor:
        where.append("actor = ?")
        params.append(actor)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM bot_events {where_sql} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_bot_event_categories() -> list[str]:
    """Distinct categories present in bot_events, for filter dropdowns."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT category FROM bot_events ORDER BY category"
        ).fetchall()
        return [r["category"] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settings: global and per-guild
#
# Global keys gate the whole process (Bedrock model id, log level, scheduler
# intervals). Per-guild keys are everything else — channel bindings, announce
# role lists, voice persona, behavior toggles, cooldowns, ttl windows.
# ---------------------------------------------------------------------------


def get_global_setting(key: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM global_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def get_global_settings(prefix: str = "") -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT key, value FROM global_settings WHERE key LIKE ? ORDER BY key",
            (f"{prefix}%",),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def upsert_global_setting(*, key: str, value: str, updated_by: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO global_settings (key, value, updated_by)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = excluded.updated_by
            """,
            (key, value, updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_global_settings(values: Mapping[str, str], *, updated_by: str) -> None:
    conn = connect()
    try:
        conn.executemany(
            """
            INSERT INTO global_settings (key, value, updated_by)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = excluded.updated_by
            """,
            [(k, v, updated_by) for k, v in values.items()],
        )
        conn.commit()
    finally:
        conn.close()


def get_guild_setting(guild_id: str, key: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def get_guild_settings(guild_id: str, prefix: str = "") -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT key, value FROM guild_settings "
            "WHERE guild_id = ? AND key LIKE ? ORDER BY key",
            (guild_id, f"{prefix}%"),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def upsert_guild_setting(
    *, guild_id: str, key: str, value: str, updated_by: str
) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO guild_settings (guild_id, key, value, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = excluded.updated_by
            """,
            (guild_id, key, value, updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_guild_settings(
    guild_id: str, values: Mapping[str, str], *, updated_by: str
) -> None:
    conn = connect()
    try:
        conn.executemany(
            """
            INSERT INTO guild_settings (guild_id, key, value, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP,
                updated_by = excluded.updated_by
            """,
            [(guild_id, k, v, updated_by) for k, v in values.items()],
        )
        conn.commit()
    finally:
        conn.close()


def get_user_event_progress(*, event_id: int, discord_user_id: str) -> dict[str, Any]:
    """Aggregate a user's approved submissions for an event.

    Reads through ``submission_events`` so submissions attributed to
    ``event_id`` (primary or secondary) all count.

    Returns:
        submission_count: int
        totals: dict[str, float | int] — summed fields (missing fields skipped)
        last_submission_at: str | None — created_at of most recent approved row
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.extracted_stats, s.created_at
              FROM submissions s
              JOIN submission_events se ON se.submission_id = s.id
             WHERE se.event_id = ?
               AND s.discord_user_id = ?
               AND s.status = 'approved'
             ORDER BY s.created_at DESC
            """,
            (event_id, discord_user_id),
        ).fetchall()
    finally:
        conn.close()

    sum_fields = (
        "duration_seconds",
        "distance_miles",
        "elevation_gain_feet",
        "calories",
        "reps",
        "sets",
        "volume_lb",
    )
    totals: dict[str, float] = {f: 0.0 for f in sum_fields}
    counts: dict[str, int] = {f: 0 for f in sum_fields}
    hr_values: list[float] = []
    weight_values: list[float] = []

    for row in rows:
        raw = row["extracted_stats"]
        if not raw:
            continue
        try:
            stats = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # volume_lb falls back to weight * reps * sets if not directly extracted,
        # so weighted-strength challenges rank correctly whether the user posted
        # per-set numbers or a summary chart.
        if not isinstance(stats.get("volume_lb"), (int, float)):
            w_ = stats.get("weight_lb")
            r_ = stats.get("reps")
            s_ = stats.get("sets")
            if all(isinstance(x, (int, float)) for x in (w_, r_, s_)):
                stats["volume_lb"] = float(w_) * float(r_) * float(s_)
        for field in sum_fields:
            v = stats.get(field)
            if isinstance(v, (int, float)):
                totals[field] += float(v)
                counts[field] += 1
        hr = stats.get("heart_rate_avg_bpm")
        if isinstance(hr, (int, float)):
            hr_values.append(float(hr))
        w = stats.get("weight_lb")
        if isinstance(w, (int, float)):
            weight_values.append(float(w))

    # Drop fields with zero contributing rows so the reply stays clean.
    clean_totals: dict[str, float | int] = {
        f: (int(totals[f]) if f in ("calories", "reps", "sets") else totals[f])
        for f in sum_fields
        if counts[f] > 0
    }
    if hr_values:
        clean_totals["heart_rate_avg_bpm"] = int(round(sum(hr_values) / len(hr_values)))
    if weight_values:
        clean_totals["weight_lb_max"] = max(weight_values)

    return {
        "submission_count": len(rows),
        "totals": clean_totals,
        "last_submission_at": rows[0]["created_at"] if rows else None,
    }


def get_event_summary(event_id: int) -> dict[str, Any] | None:
    """Return an event row plus derived counts for the dashboard summary card."""
    conn = connect()
    try:
        event = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if event is None:
            return None
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS submission_count,
                COUNT(DISTINCT s.discord_user_id) AS participant_count,
                MAX(s.created_at) AS last_submission_at
              FROM submissions s
              JOIN submission_events se ON se.submission_id = s.id
             WHERE se.event_id = ? AND s.status = 'approved'
            """,
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    result = dict(event)
    result["submission_count"] = counts["submission_count"] or 0
    result["participant_count"] = counts["participant_count"] or 0
    result["last_submission_at"] = counts["last_submission_at"]
    return result


def get_event_leaderboard(event_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Aggregate per-user totals for an event, sorted by the event's primary metric.

    Returns a list of entries with:
      - discord_user_id, user_username, user_display_name, user_avatar_url
      - submission_count
      - totals: dict of summed sortable fields
      - primary_metric: the event's chosen sort key
      - primary_value: the value used to rank this entry
    """
    conn = connect()
    try:
        event = conn.execute(
            "SELECT primary_metric FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if event is None:
            return []
        primary_metric = event["primary_metric"]

        rows = conn.execute(
            """
            SELECT s.discord_user_id, s.extracted_stats,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url
              FROM submissions s
              JOIN submission_events se ON se.submission_id = s.id
              LEFT JOIN users u ON u.discord_user_id = s.discord_user_id
             WHERE se.event_id = ? AND s.status = 'approved'
            """,
            (event_id,),
        ).fetchall()
    finally:
        conn.close()

    sum_fields = (
        "duration_seconds",
        "distance_miles",
        "elevation_gain_feet",
        "calories",
        "reps",
        "volume_lb",
    )
    per_user: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row["discord_user_id"]
        entry = per_user.setdefault(
            uid,
            {
                "discord_user_id": uid,
                "user_username": row["user_username"],
                "user_display_name": row["user_display_name"],
                "user_avatar_url": row["user_avatar_url"],
                "submission_count": 0,
                "totals": {f: 0.0 for f in sum_fields},
            },
        )
        entry["submission_count"] += 1
        if row["extracted_stats"]:
            try:
                stats = json.loads(row["extracted_stats"])
            except json.JSONDecodeError:
                stats = {}
            # volume_lb falls back to weight * reps * sets when the vision agent
            # didn't compute it directly. Keeps the leaderboard correct for
            # weighted-strength challenges whether the user posted per-set
            # numbers or a summary chart.
            if not isinstance(stats.get("volume_lb"), (int, float)):
                w_ = stats.get("weight_lb")
                r_ = stats.get("reps")
                s_ = stats.get("sets")
                if all(isinstance(x, (int, float)) for x in (w_, r_, s_)):
                    stats["volume_lb"] = float(w_) * float(r_) * float(s_)
            for f in sum_fields:
                v = stats.get(f)
                if isinstance(v, (int, float)):
                    entry["totals"][f] += float(v)

    def primary_value(entry: dict[str, Any]) -> float:
        if primary_metric == "submission_count":
            return float(entry["submission_count"])
        return float(entry["totals"].get(primary_metric, 0.0))

    board = sorted(per_user.values(), key=primary_value, reverse=True)[:limit]
    for entry in board:
        entry["primary_metric"] = primary_metric
        entry["primary_value"] = primary_value(entry)
    return board


def get_recent_approved_submissions(*, event_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Latest approved submissions for an event, joined with user identity."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.*,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url
              FROM submissions s
              JOIN submission_events se ON se.submission_id = s.id
              LEFT JOIN users u ON u.discord_user_id = s.discord_user_id
             WHERE se.event_id = ? AND s.status = 'approved'
             ORDER BY s.created_at DESC
             LIMIT ?
            """,
            (event_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_event_participants_from_submissions(event_id: int) -> list[dict[str, Any]]:
    """Distinct users with any approved submission attributed to this event.

    Reads through ``submission_events`` so users are counted regardless
    of whether the event was primary or secondary on their submissions.
    Returns rows with ``discord_user_id`` and ``last_submission_at``.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.discord_user_id,
                   MAX(s.created_at) AS last_submission_at
              FROM submissions s
              JOIN submission_events se ON se.submission_id = s.id
             WHERE se.event_id = ? AND s.status = 'approved'
             GROUP BY s.discord_user_id
            """,
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_last_nag_time(*, event_id: int, discord_user_id: str) -> str | None:
    """Timestamp of the last ``nag.sent`` bot_events row for this user/event."""
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT MAX(ts) AS ts
              FROM bot_events
             WHERE category = 'nag.sent'
               AND json_extract(context, '$.event_id') = ?
               AND json_extract(context, '$.user_id') = ?
            """,
            (event_id, discord_user_id),
        ).fetchone()
        return row["ts"] if row and row["ts"] else None
    finally:
        conn.close()


def get_recent_approved_submissions_in_guild(
    *, guild_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Latest approved submissions in the bound channel, across all events.

    Used by the home-page activity feed so it doesn't fragment when multiple
    events are active simultaneously. A submission that counts for more than
    one event still appears once — the DISTINCT-by-id semantics come from
    joining on the ``submissions`` primary key and de-duplicating in Python.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.*,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url
              FROM submissions s
              LEFT JOIN users u ON u.discord_user_id = s.discord_user_id
             WHERE s.guild_id = ?
               AND s.status = 'approved'
             ORDER BY s.created_at DESC
             LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_events_awaiting_upcoming_announcement(
    guild_id: str, lead_hours: float
) -> list[dict[str, Any]]:
    """Events entering the pre-start "coming up soon" window for this guild."""
    modifier = f"+{lead_hours} hours"
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
             WHERE guild_id = ?
               AND announced_upcoming_at IS NULL
               AND datetime('now') < datetime(starts_at)
               AND datetime(starts_at) <= datetime('now', ?)
             ORDER BY id ASC
            """,
            (guild_id, modifier),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_event_announced_upcoming(event_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE events SET announced_upcoming_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_events_awaiting_start_announcement(guild_id: str) -> list[dict[str, Any]]:
    """Events whose start time has passed and haven't been announced yet."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
             WHERE guild_id = ?
               AND announced_start_at IS NULL
               AND datetime('now') >= datetime(starts_at)
             ORDER BY id ASC
            """,
            (guild_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_events_awaiting_end_announcement(guild_id: str) -> list[dict[str, Any]]:
    """Events whose end time has passed and haven't been closed out yet."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
             WHERE guild_id = ?
               AND announced_end_at IS NULL
               AND datetime('now') >= datetime(ends_at)
             ORDER BY id ASC
            """,
            (guild_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_event_announced_start(event_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE events SET announced_start_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_event_announced_end(event_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE events SET announced_end_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_currently_open_events(guild_id: str) -> list[dict[str, Any]]:
    """Events whose window straddles now — starts_at <= now < ends_at.

    Used by the reminder scan. Strict-less-than on ends_at so an event
    that ended in the current second doesn't linger for reminder
    purposes (the end-announcement path handles it).
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
             WHERE guild_id = ?
               AND datetime('now') >= datetime(starts_at)
               AND datetime('now') <  datetime(ends_at)
             ORDER BY id ASC
            """,
            (guild_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


REMINDER_KINDS = ("halfway", "one_week", "three_day", "one_day")


def has_reminder_been_sent(*, event_id: int, kind: str) -> bool:
    """True if event_reminders already has a row for (event_id, kind)."""
    if kind not in REMINDER_KINDS:
        raise ValueError(f"invalid reminder kind {kind!r}")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM event_reminders WHERE event_id = ? AND kind = ?",
            (event_id, kind),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def refresh_guild_cache(
    guild_id: str, kind: str, entries: list[dict[str, Any]]
) -> tuple[int, int]:
    """Replace ``kind`` rows for ``guild_id`` with the supplied entries.

    ``entries`` is a list of ``{"id": str, "name": str, "position": int}``.
    Rows for this (guild_id, kind) not present in the new list are
    deleted. Returns ``(upserted, pruned)``.
    """
    if kind not in ("channel", "role"):
        raise ValueError(f"invalid kind {kind!r}")
    keep_ids = {str(e["id"]) for e in entries}
    conn = connect()
    try:
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            pruned = conn.execute(
                f"DELETE FROM guild_cache WHERE guild_id = ? AND kind = ? "
                f"AND id NOT IN ({placeholders})",
                (guild_id, kind, *keep_ids),
            ).rowcount
        else:
            pruned = conn.execute(
                "DELETE FROM guild_cache WHERE guild_id = ? AND kind = ?",
                (guild_id, kind),
            ).rowcount
        for e in entries:
            conn.execute(
                """
                INSERT INTO guild_cache (guild_id, kind, id, name, position)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, kind, id) DO UPDATE SET
                    name = excluded.name,
                    position = excluded.position,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    kind,
                    str(e["id"]),
                    str(e["name"]),
                    int(e.get("position", 0)),
                ),
            )
        conn.commit()
        return len(entries), pruned
    finally:
        conn.close()


def list_guild_cache(guild_id: str, kind: str) -> list[dict[str, Any]]:
    """Return cached channels or roles for a guild, ordered by position."""
    if kind not in ("channel", "role"):
        raise ValueError(f"invalid kind {kind!r}")
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name, position FROM guild_cache "
            "WHERE guild_id = ? AND kind = ? ORDER BY position ASC, name ASC",
            (guild_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_reminder_sent(*, event_id: int, kind: str, posted: bool = True) -> None:
    """Record that a reminder for (event, kind) is handled.

    ``posted=True`` records that we actually posted a message. ``posted=False``
    records a silent-skip — the scan superseded this milestone with a later
    one, and we're marking it so future scans don't re-consider it.
    """
    if kind not in REMINDER_KINDS:
        raise ValueError(f"invalid reminder kind {kind!r}")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO event_reminders (event_id, kind, posted) "
            "VALUES (?, ?, ?)",
            (event_id, kind, 1 if posted else 0),
        )
        conn.commit()
    finally:
        conn.close()


def list_event_participants(event_id: int) -> list[dict[str, Any]]:
    """Rows from event_participants joined with cached user identity.

    Each row also carries the participant's submission_count and
    last_submission_at for the participants page.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT ep.event_id,
                   ep.discord_user_id,
                   ep.joined_at,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   u.avatar_url AS user_avatar_url,
                   (
                     SELECT COUNT(*)
                       FROM submissions s
                       JOIN submission_events se ON se.submission_id = s.id
                      WHERE se.event_id = ep.event_id
                        AND s.discord_user_id = ep.discord_user_id
                        AND s.status = 'approved'
                   ) AS submission_count,
                   (
                     SELECT MAX(s.created_at)
                       FROM submissions s
                       JOIN submission_events se ON se.submission_id = s.id
                      WHERE se.event_id = ep.event_id
                        AND s.discord_user_id = ep.discord_user_id
                        AND s.status = 'approved'
                   ) AS last_submission_at
              FROM event_participants ep
              LEFT JOIN users u ON u.discord_user_id = ep.discord_user_id
             WHERE ep.event_id = ?
             ORDER BY ep.joined_at DESC
            """,
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_submissions_for_export(event_id: int) -> list[dict[str, Any]]:
    """Every submission attributed to the event, with user + event context.

    Used by the CSV export. Includes approved, pending, and rejected rows so
    admins get the complete audit trail.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.*,
                   u.username AS user_username,
                   u.display_name AS user_display_name,
                   e.name AS event_name,
                   e.kind AS event_kind,
                   se.is_primary AS is_primary_event
              FROM submissions s
              JOIN submission_events se ON se.submission_id = s.id
              LEFT JOIN users u ON u.discord_user_id = s.discord_user_id
              LEFT JOIN events e ON e.id = se.event_id
             WHERE se.event_id = ?
             ORDER BY s.created_at ASC
            """,
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
