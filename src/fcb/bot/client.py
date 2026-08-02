"""discord.py client for FCB.

Owns the gateway connection. The bot is multi-guild: it processes
messages from every guild it's a member of. Per-guild settings —
channel binding, announce roles, voice persona, behavior toggles — are
read through ``runtime_config`` scoped by ``message.guild.id``.
"""

import asyncio
import io
import json
import logging
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord
from discord import app_commands
from discord.ext import tasks
from PIL import Image

from fcb import config, runtime_config
from fcb.agents import router, vision, voice
from fcb.agents.vision import WorkoutStats
from fcb.db import dao

logger = logging.getLogger(__name__)


def _intents() -> discord.Intents:
    """Return the exact set of intents declared in steering/discord.md."""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    intents.members = True
    return intents


def _serialize_attachment(a: discord.Attachment) -> dict[str, object]:
    return {
        "filename": a.filename,
        "content_type": a.content_type,
        "size": a.size,
    }


def _image_format(attachment: discord.Attachment) -> str | None:
    """Map a Discord attachment to a Strands image format, or None if not an image."""
    ct = (attachment.content_type or "").lower()
    if ct == "image/png":
        return "png"
    if ct in ("image/jpeg", "image/jpg"):
        return "jpeg"
    if ct == "image/gif":
        return "gif"
    if ct == "image/webp":
        return "webp"
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if ext == "png":
        return "png"
    if ext in ("jpg", "jpeg"):
        return "jpeg"
    if ext == "gif":
        return "gif"
    if ext == "webp":
        return "webp"
    return None


_MAX_IMAGE_BYTES = 4_500_000  # 4.5 MB — safe margin below Bedrock's 5 MB limit


def _downsize_image(image_bytes: bytes, image_format: str) -> tuple[bytes, str]:
    """Reduce image size to fit within Bedrock's limit.

    Converts to JPEG (lossy) and scales down progressively until under
    _MAX_IMAGE_BYTES. Returns (resized_bytes, format). The format may
    change to 'jpeg' even if the input was PNG/WebP since JPEG compresses
    better for photos.
    """
    if len(image_bytes) <= _MAX_IMAGE_BYTES:
        return image_bytes, image_format

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Try JPEG at quality 85 first at original resolution
    for quality in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _MAX_IMAGE_BYTES:
            logger.info(
                f"downsized image from {len(image_bytes)} to {buf.tell()} bytes "
                f"(quality={quality})"
            )
            return buf.getvalue(), "jpeg"

    # Still too large — scale down dimensions
    scale = 0.75
    while scale > 0.2:
        new_size = (int(img.width * scale), int(img.height * scale))
        resized = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=70, optimize=True)
        if buf.tell() <= _MAX_IMAGE_BYTES:
            logger.info(
                f"downsized image from {len(image_bytes)} to {buf.tell()} bytes "
                f"(scale={scale:.0%}, quality=70)"
            )
            return buf.getvalue(), "jpeg"
        scale -= 0.15

    # Last resort — heavily compressed thumbnail
    resized = img.resize((800, int(800 * img.height / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=50, optimize=True)
    logger.warning(
        f"downsized image from {len(image_bytes)} to {buf.tell()} bytes "
        f"(800px wide, quality=50 — heavy compression)"
    )
    return buf.getvalue(), "jpeg"


_METRIC_LABELS = {
    "submission_count": "workouts logged",
    "duration_seconds": "total time",
    "distance_miles": "total distance",
    "elevation_gain_feet": "total elevation gain",
    "calories": "total calories",
    "reps": "total reps",
    "volume_lb": "total volume (lb-reps)",
}


class _UserCooldowns:
    """Per-(guild, user) in-memory cooldown. Restart-safe.

    ``cooldown_supplier`` receives the guild_id on every check so per-
    guild cooldown settings take effect on the next invocation without
    a restart.
    """

    def __init__(self, cooldown_supplier: "Callable[[str], float]") -> None:
        self._cooldown_supplier = cooldown_supplier
        self._last: dict[tuple[str, str], float] = {}

    def check_and_stamp(self, guild_id: str, user_id: str) -> float | None:
        """Return seconds remaining if rate-limited; else stamp and return None."""
        cooldown = float(self._cooldown_supplier(guild_id))
        now = time.monotonic()
        key = (guild_id, user_id)
        last = self._last.get(key)
        if last is not None and now - last < cooldown:
            return cooldown - (now - last)
        self._last[key] = now
        return None


def _format_metric_label(metric: str) -> str:
    return _METRIC_LABELS.get(metric, metric)


def _format_metric_value(metric: str, value: float) -> str:
    if metric == "submission_count":
        return f"{int(value)} workouts"
    if metric == "duration_seconds":
        return f"{int(round(value / 60))} min"
    if metric == "distance_miles":
        return f"{value:.2f} mi"
    if metric == "elevation_gain_feet":
        return f"{int(round(value)):,} ft"
    if metric in ("calories", "reps"):
        return f"{int(value):,} {metric}"
    if metric == "volume_lb":
        return f"{int(round(value)):,} lb-reps"
    return f"{value:g}"


def _announce_role_prefix(guild_id: str) -> str:
    """Space-terminated role-mention prefix for this guild, or empty when none."""
    role_ids = runtime_config.announce_role_ids(guild_id)
    if not role_ids:
        return ""
    return " ".join(f"<@&{r}>" for r in role_ids) + " "


async def _notify_rate_limited_privately(
    message: discord.Message, remaining_seconds: float
) -> None:
    """Tell the user they hit the cooldown without cluttering the channel.

    Prefers a DM (fully private). Falls back to a ⏱️ reaction on their
    message if their DMs are closed to the bot. Silent no-op if both
    fail — we'd rather the user notice nothing than post publicly.
    """
    text = (
        f"Slow down — try again in {int(remaining_seconds) + 1}s. "
        "Rate limit for the fitness bot."
    )
    try:
        await message.author.send(text)
        return
    except discord.Forbidden:
        pass
    except discord.HTTPException as e:
        logger.warning(f"DM rate-limit notice failed for {message.author}: {e}")

    try:
        await message.add_reaction("⏱️")
    except (discord.Forbidden, discord.HTTPException):
        pass


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp string into an aware UTC datetime.

    Accepts both SQLite's 'YYYY-MM-DD HH:MM:SS' and ISO-format
    'YYYY-MM-DDTHH:MM:SS...' strings that our tables mix.
    """
    if not value:
        return None
    normalized = value.replace("T", " ")[:19]
    try:
        return datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _humanize_ago(iso_ts: str) -> str:
    """Turn a stored ISO timestamp into 'X hours ago' / 'X days ago'."""
    try:
        # Stored timestamps may be either 'YYYY-MM-DD HH:MM:SS' (CURRENT_TIMESTAMP)
        # or 'YYYY-MM-DDTHH:MM:SS...' (message.created_at.isoformat()).
        normalized = iso_ts.replace("T", " ")[:19]
        posted = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return iso_ts
    delta = datetime.now(timezone.utc) - posted
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    return f"{days}d ago"


def _summarize_totals(totals: dict[str, object]) -> str:
    """Render aggregated totals into a single one-liner."""
    parts: list[str] = []
    if "duration_seconds" in totals:
        minutes = int(round(float(totals["duration_seconds"]) / 60))
        parts.append(f"{minutes} min")
    if "distance_miles" in totals:
        parts.append(f"{float(totals['distance_miles']):.2f} mi")
    if "elevation_gain_feet" in totals:
        parts.append(f"{int(round(float(totals['elevation_gain_feet']))):,} ft")
    if "calories" in totals:
        parts.append(f"{int(totals['calories'])} cal")
    if "reps" in totals:
        parts.append(f"{int(totals['reps'])} reps")
    if "sets" in totals:
        parts.append(f"{int(totals['sets'])} sets")
    if "heart_rate_avg_bpm" in totals:
        parts.append(f"avg HR {int(totals['heart_rate_avg_bpm'])}")
    if "weight_lb_max" in totals:
        parts.append(f"max {float(totals['weight_lb_max']):g} lb")
    return " · ".join(parts) if parts else "no stats yet"


def _summarize_stats(stats: WorkoutStats) -> str:
    """Build a short human line like 'treadmill · 42 min · 3.10 mi · 1,240 ft elevation'."""
    parts: list[str] = []
    if stats.workout_type:
        parts.append(stats.workout_type)
    if stats.duration_seconds is not None:
        minutes = int(round(stats.duration_seconds / 60))
        parts.append(f"{minutes} min")
    if stats.distance_miles is not None:
        parts.append(f"{stats.distance_miles:.2f} mi")
    if stats.elevation_gain_feet is not None:
        parts.append(f"{int(round(stats.elevation_gain_feet)):,} ft elevation")
    if stats.calories is not None:
        parts.append(f"{stats.calories} cal")
    if stats.heart_rate_avg_bpm is not None:
        parts.append(f"HR {stats.heart_rate_avg_bpm}")
    if stats.reps is not None:
        parts.append(f"{stats.reps} reps")
    if stats.weight_lb is not None:
        parts.append(f"{stats.weight_lb:g} lb")
    if stats.sets is not None:
        parts.append(f"{stats.sets} sets")
    for k, v in (stats.extras or {}).items():
        parts.append(f"{k}: {v}")
    if not parts:
        return "no stats extracted"
    return " · ".join(parts)


class FCBClient(discord.Client):
    """Gateway client bound to one guild and one channel."""

    def __init__(self, *, intents: discord.Intents) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # Shared cooldown for LLM-invoking commands (/describe, /debug).
        # Reads runtime_config on every check so admin edits apply live.
        self._llm_cooldown = _UserCooldowns(runtime_config.command_cooldown_seconds)
        self._register_commands()

    async def _guild_id_or_reject(
        self, interaction: discord.Interaction, ephemeral: bool = True
    ) -> str | None:
        """Return interaction.guild_id as str, or reject with an ephemeral message.

        DMs (guild_id is None) get told the bot only works in servers.
        Guilds where the bot isn't registered yet also get rejected so we
        don't accidentally serve stale state before on_ready finishes.
        """
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This bot only works inside a server.", ephemeral=ephemeral
            )
            return None
        return str(interaction.guild_id)

    async def _wrong_channel_reject(
        self, interaction: discord.Interaction, guild_id: str
    ) -> bool:
        """Reject if interaction isn't in the guild's bound channel.

        Returns True when rejection was sent (caller should return
        immediately). False means the channel matches and the caller
        should proceed.
        """
        bound = runtime_config.channel_id(guild_id)
        if bound is None:
            await interaction.response.send_message(
                "This server hasn't picked a bot channel yet. An admin can "
                "set one on the dashboard.",
                ephemeral=True,
            )
            return True
        if str(interaction.channel_id) != bound:
            await interaction.response.send_message(
                f"This bot only responds in <#{bound}>.", ephemeral=True
            )
            return True
        return False

    def _register_commands(self) -> None:
        """Attach globally-synced slash commands to the tree.

        Commands are synced globally (see setup_hook) so any guild the
        bot joins gets them without a per-guild round-trip. Each
        handler derives the guild it's serving from interaction.guild_id
        and reads per-guild settings from that context.
        """

        @self.tree.command(
            name="ping",
            description="Health check — replies with a status message.",
        )
        async def ping(interaction: discord.Interaction) -> None:
            guild_id = await self._guild_id_or_reject(interaction)
            if guild_id is None:
                return
            if await self._wrong_channel_reject(interaction, guild_id):
                return
            logger.info(f"/ping from {interaction.user}")
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="command.ping",
                message=f"/ping from {interaction.user}",
                actor=str(interaction.user.id),
            )
            await interaction.response.send_message("FitnessBot up and healthy!")

        @self.tree.command(
            name="status",
            description="Show your progress across the active challenges (or someone else's).",
        )
        @app_commands.describe(user="Optional: show another user's status")
        async def status(
            interaction: discord.Interaction,
            user: discord.User | None = None,
        ) -> None:
            guild_id = await self._guild_id_or_reject(interaction)
            if guild_id is None:
                return
            if await self._wrong_channel_reject(interaction, guild_id):
                return

            target = user or interaction.user
            target_id = str(target.id)
            invoker_id = str(interaction.user.id)

            active_events = await asyncio.to_thread(
                dao.list_active_events,
                guild_id,
            )
            if not active_events:
                await interaction.response.send_message(
                    "No active challenge right now. Ask an admin to set one up."
                )
                return

            remaining = self._llm_cooldown.check_and_stamp(guild_id, invoker_id)
            if remaining is not None:
                await interaction.response.send_message(
                    f"Slow down — try again in {int(remaining) + 1}s.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=False, thinking=True)
            logger.info(f"/status invoked by {interaction.user} target={target}")

            # Build progress data for each active event.
            event_progress: list[dict[str, object]] = []
            footer_lines: list[str] = []
            for event in active_events:
                progress = await asyncio.to_thread(
                    dao.get_user_event_progress,
                    event_id=event["id"],
                    discord_user_id=target_id,
                )
                metric = event["primary_metric"]
                participated = progress["submission_count"] > 0
                if participated:
                    primary_value = (
                        progress["submission_count"]
                        if metric == "submission_count"
                        else float(progress["totals"].get(metric, 0.0))
                    )
                    primary_formatted = _format_metric_value(metric, primary_value)
                    last_ago = _humanize_ago(progress["last_submission_at"])
                    footer_lines.append(
                        f"-# **{event['name']}** — "
                        f"{progress['submission_count']} workout"
                        f"{'s' if progress['submission_count'] != 1 else ''} · "
                        f"{primary_formatted} ({_format_metric_label(metric)}) · "
                        f"last {last_ago}"
                    )
                    event_progress.append({
                        "name": event["name"],
                        "participated": True,
                        "submission_count": progress["submission_count"],
                        "primary_value_formatted": primary_formatted,
                        "primary_metric_label": _format_metric_label(metric),
                        "last_ago": last_ago,
                    })
                else:
                    footer_lines.append(
                        f"-# **{event['name']}** — no submissions yet"
                    )
                    event_progress.append({
                        "name": event["name"],
                        "participated": False,
                        "primary_metric_label": _format_metric_label(metric),
                    })

            try:
                voice_line = await asyncio.to_thread(
                    voice.status_report,
                    guild_id=guild_id,
                    user_display_name=target.display_name,
                    event_progress=event_progress,
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"/status voice failed: {e}")
                voice_line = f"Here's where {target.display_name} stands."

            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="command.status",
                message=f"/status from {interaction.user} for {target}",
                actor=invoker_id,
                context={
                    "target_user_id": target_id,
                    "active_event_ids": [int(e["id"]) for e in active_events],
                },
            )

            body = voice_line + "\n" + "\n".join(footer_lines)
            await interaction.followup.send(
                body,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(
            name="list",
            description="Show the challenges currently running.",
        )
        async def list_challenges_cmd(interaction: discord.Interaction) -> None:
            guild_id = await self._guild_id_or_reject(interaction)
            if guild_id is None:
                return
            if await self._wrong_channel_reject(interaction, guild_id):
                return

            invoker_id = str(interaction.user.id)

            active_events = await asyncio.to_thread(
                dao.list_active_events,
                guild_id,
            )
            if not active_events:
                await interaction.response.send_message(
                    "No challenges are running right now. Check back soon."
                )
                return

            remaining = self._llm_cooldown.check_and_stamp(guild_id, invoker_id)
            if remaining is not None:
                await interaction.response.send_message(
                    f"Slow down — try again in {int(remaining) + 1}s.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=False, thinking=True)
            logger.info(f"/list invoked by {interaction.user}")

            # Build compact per-event context for the voice call and a footer.
            events_data: list[dict[str, object]] = []
            footer_lines: list[str] = []
            for event in active_events:
                summary = await asyncio.to_thread(dao.get_event_summary, event["id"])
                participant_count = summary["participant_count"] if summary else 0
                submission_count = summary["submission_count"] if summary else 0
                metric_label = _format_metric_label(event["primary_metric"])

                ends_at = _parse_ts(event["ends_at"])
                days_left = (
                    max(0, (ends_at - datetime.now(timezone.utc)).days)
                    if ends_at is not None
                    else None
                )
                days_left_str = f"{days_left}d left" if days_left is not None else "no end date"

                events_data.append({
                    "name": event["name"],
                    "kind": event["kind"],
                    "prompt": event["prompt"][:200],
                    "primary_metric_label": metric_label,
                    "participant_count": participant_count,
                    "submission_count": submission_count,
                    "days_remaining": days_left,
                })
                footer_lines.append(
                    f"-# **{event['name']}** — ranked by {metric_label} · "
                    f"{participant_count} participant"
                    f"{'s' if participant_count != 1 else ''} · "
                    f"{submission_count} workout"
                    f"{'s' if submission_count != 1 else ''} · "
                    f"{days_left_str}"
                )

            try:
                voice_line = await asyncio.to_thread(
                    voice.list_challenges, guild_id=guild_id, events=events_data
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"/list voice failed: {e}")
                voice_line = (
                    f"{len(active_events)} challenge"
                    f"{'s' if len(active_events) != 1 else ''} running."
                )

            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="command.list",
                message=f"/list from {interaction.user}",
                actor=invoker_id,
                context={
                    "active_event_ids": [int(e["id"]) for e in active_events],
                },
            )

            body = voice_line + "\n" + "\n".join(footer_lines)
            await interaction.followup.send(
                body,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(
            name="leaderboard",
            description="Show the top participants in the active challenges.",
        )
        async def leaderboard(interaction: discord.Interaction) -> None:
            guild_id = await self._guild_id_or_reject(interaction)
            if guild_id is None:
                return
            if await self._wrong_channel_reject(interaction, guild_id):
                return

            active_events = await asyncio.to_thread(
                dao.list_active_events,
                guild_id,
            )
            if not active_events:
                await interaction.response.send_message(
                    "No active challenge right now."
                )
                return

            per_event_limit = 10 if len(active_events) == 1 else 5
            sections: list[str] = []
            total_entries = 0
            for event in active_events:
                board = await asyncio.to_thread(
                    dao.get_event_leaderboard, event["id"], per_event_limit
                )
                total_entries += len(board)
                metric = event["primary_metric"]
                lines = [
                    f"**Leaderboard — {event['name']}**",
                    f"-# ranked by {_format_metric_label(metric)}",
                ]
                if not board:
                    lines.append("_No approved submissions yet._")
                else:
                    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
                    for i, entry in enumerate(board):
                        rank_mark = medals.get(i, f"`{i + 1:>2}.`")
                        name = (
                            entry["user_display_name"]
                            or entry["user_username"]
                            or f"id {entry['discord_user_id']}"
                        )
                        value = _format_metric_value(metric, entry["primary_value"])
                        lines.append(f"{rank_mark} **{name}** — {value}")
                sections.append("\n".join(lines))

            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="command.leaderboard",
                message=f"/leaderboard from {interaction.user}",
                actor=str(interaction.user.id),
                context={
                    "active_event_ids": [int(e["id"]) for e in active_events],
                    "entries": total_entries,
                },
            )
            await interaction.response.send_message(
                "\n\n".join(sections),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(
            name="debug",
            description="Admin-only: run vision on an image and privately show the raw extraction.",
        )
        @app_commands.describe(
            image="The screenshot to analyze",
            prompt="Optional prompt override (default: active event's prompt)",
        )
        async def debug(
            interaction: discord.Interaction,
            image: discord.Attachment,
            prompt: str | None = None,
        ) -> None:
            user_id = str(interaction.user.id)

            guild_id = await self._guild_id_or_reject(interaction)
            if guild_id is None:
                return
            if await self._wrong_channel_reject(interaction, guild_id):
                return

            if not await asyncio.to_thread(dao.is_admin, user_id):
                logger.warning(f"/debug denied for non-admin {interaction.user} ({user_id})")
                await interaction.response.send_message(
                    "This is an admin-only debug command.", ephemeral=True
                )
                return

            remaining = self._llm_cooldown.check_and_stamp(guild_id, user_id)
            if remaining is not None:
                await interaction.response.send_message(
                    f"Cool your jets — try again in {int(remaining) + 1}s.",
                    ephemeral=True,
                )
                return

            image_format = _image_format(image)
            if image_format is None:
                await interaction.response.send_message(
                    f"Unsupported image format. content_type={image.content_type!r}, "
                    f"filename={image.filename!r}. Supported: png, jpeg, gif, webp.",
                    ephemeral=True,
                )
                return

            # Bedrock takes several seconds; defer so we don't hit the 3s ACK window.
            await interaction.response.defer(ephemeral=True, thinking=True)

            if prompt is None:
                event = await asyncio.to_thread(
                    dao.get_active_event,
                    guild_id,
                )
                if event is not None:
                    resolved_prompt = event["prompt"]
                    prompt_source = f"active event #{event['id']} ({event['name']})"
                else:
                    resolved_prompt = (
                        "Extract any fitness workout statistics you can see in the image."
                    )
                    prompt_source = "generic fallback (no active event)"
            else:
                resolved_prompt = prompt
                prompt_source = "user override"

            logger.info(
                f"/debug from {interaction.user} format={image_format} "
                f"size={image.size} prompt_source={prompt_source}"
            )
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="command.debug",
                message=f"/debug from {interaction.user} ({prompt_source})",
                actor=user_id,
                context={
                    "filename": image.filename,
                    "content_type": image.content_type,
                    "size": image.size,
                    "prompt_source": prompt_source,
                },
            )

            image_bytes = await image.read()

            try:
                stats = await asyncio.to_thread(
                    vision.extract, image_bytes, image_format, resolved_prompt
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"/debug vision failed for {interaction.user}: {e}")
                await asyncio.to_thread(
                    dao.record_event,
                    level="ERROR",
                    category="command.debug.failed",
                    message=f"/debug vision failed: {e}",
                    actor=user_id,
                    context={"filename": image.filename},
                )
                await interaction.followup.send(f"Vision failed: `{e}`", ephemeral=True)
                raise

            summary = _summarize_stats(stats)
            detail_json = stats.model_dump_json(indent=2)
            body = (
                f"**Summary:** {summary}\n"
                f"**Prompt source:** {prompt_source}\n"
                f"**Confidence:** {stats.confidence:.2f}\n"
                f"**Workout screenshot:** {stats.is_workout_screenshot}\n"
                f"**Notes:** {stats.notes or '_(none)_'}\n"
                f"```json\n{detail_json}\n```"
            )
            if len(body) > 1900:
                body = body[:1897] + "..."
            await interaction.followup.send(body, ephemeral=True)

        # -- Message context menu: "Describe" (right-click a message → Apps) --
        async def describe_message_impl(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            invoker_id = str(interaction.user.id)

            guild_id = await self._guild_id_or_reject(interaction)
            if guild_id is None:
                return
            if await self._wrong_channel_reject(interaction, guild_id):
                return

            image_atts = [a for a in message.attachments if _image_format(a) is not None]
            if not image_atts:
                await interaction.response.send_message(
                    "That message doesn't have an image I can describe.",
                    ephemeral=True,
                )
                return

            remaining = self._llm_cooldown.check_and_stamp(guild_id, invoker_id)
            if remaining is not None:
                await interaction.response.send_message(
                    f"Cool your jets — try again in {int(remaining) + 1}s.",
                    ephemeral=True,
                )
                return

            attachment = image_atts[0]
            image_format = _image_format(attachment)
            assert image_format is not None

            # Defer publicly so the follow-up reply is visible to the channel.
            await interaction.response.defer(ephemeral=False, thinking=True)

            image_bytes = await attachment.read()
            event = await asyncio.to_thread(
                dao.get_active_event,
                guild_id,
            )

            logger.info(
                f"describe context-menu invoked by {interaction.user} "
                f"on message {message.id} from {message.author}"
            )
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="command.describe",
                message=(
                    f"{interaction.user} asked for a description of "
                    f"{message.author}'s message"
                ),
                actor=invoker_id,
                context={
                    "target_message_id": str(message.id),
                    "target_author_id": str(message.author.id),
                    "event_id": event["id"] if event else None,
                },
            )

            try:
                voice_line = await asyncio.to_thread(
                    voice.describe_publicly,
                    guild_id=guild_id,
                    user_display_name=message.author.display_name,
                    event_name=event["name"] if event else None,
                    event_prompt=event["prompt"] if event else None,
                    image_bytes=image_bytes,
                    image_format=image_format,
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"describe failed for message {message.id}: {e}")
                await asyncio.to_thread(
                    dao.record_event,
                    level="ERROR",
                    category="command.describe.failed",
                    message=f"describe context-menu failed: {e}",
                    actor=invoker_id,
                    context={"target_message_id": str(message.id)},
                )
                await interaction.followup.send(
                    "I couldn't come up with anything for that image.",
                    ephemeral=True,
                )
                raise

            await interaction.followup.send(
                f"About [{message.author.display_name}'s post]({message.jump_url}): "
                f"{voice_line}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        self.tree.add_command(
            app_commands.ContextMenu(
                name="Describe",
                callback=describe_message_impl,
                type=discord.AppCommandType.message,
            ),
        )

    async def setup_hook(self) -> None:
        # Multi-guild: sync commands globally so every guild the bot joins
        # gets them without a per-guild sync round-trip. Global syncs take
        # a few minutes to propagate; that's acceptable for install-time.
        logger.info("syncing app commands globally")
        synced = await self.tree.sync()
        logger.info(f"synced {len(synced)} app command(s): {[c.name for c in synced]}")

        # Both schedulers always start. Per-guild toggles (nag_enabled,
        # reminders_enabled, etc.) are re-read at each scan and per each
        # guild, so an admin toggling a setting takes effect on the next
        # cycle without a restart. Scheduler intervals themselves are
        # process-wide and captured at class-definition time — those are
        # restart-required.
        logger.info(
            f"starting lifecycle scheduler: "
            f"interval={config.FCB_LIFECYCLE_INTERVAL_MINUTES}m (per-guild)"
        )
        self.lifecycle_scheduler.start()
        logger.info(
            f"starting nag scheduler: "
            f"interval={config.FCB_NAG_INTERVAL_HOURS}h (per-guild)"
        )
        self.nag_scheduler.start()

    @tasks.loop(seconds=int(config.FCB_LIFECYCLE_INTERVAL_MINUTES * 60))
    async def lifecycle_scheduler(self) -> None:
        try:
            await self._run_lifecycle_scan()
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error(f"lifecycle scan failed: {e}")
            await asyncio.to_thread(
                dao.record_event,
                level="ERROR",
                category="lifecycle.scan_failed",
                message=f"lifecycle scan failed: {e}",
                actor="bot",
            )

    @lifecycle_scheduler.before_loop
    async def _before_lifecycle_scheduler(self) -> None:
        await self.wait_until_ready()

    async def _run_lifecycle_scan(self) -> None:
        """Iterate every registered guild and run its lifecycle scan.

        Empty channel bindings and empty event lists are cheap no-ops so
        idle guilds don't cost anything.
        """
        guilds = await asyncio.to_thread(dao.list_bot_guilds)
        for row in guilds:
            await self._run_lifecycle_scan_for_guild(row["guild_id"])

    async def _run_lifecycle_scan_for_guild(self, guild_id: str) -> None:
        channel_id_str = runtime_config.channel_id(guild_id)
        if channel_id_str is None:
            return  # guild hasn't picked a channel yet; scan is a no-op
        channel = self.get_channel(int(channel_id_str))
        if channel is None:
            logger.error(
                f"lifecycle scan: cannot resolve channel {channel_id_str} for guild {guild_id}"
            )
            return

        lead_hours = runtime_config.upcoming_lead_hours(guild_id)
        if lead_hours > 0:
            upcoming = await asyncio.to_thread(
                dao.list_events_awaiting_upcoming_announcement,
                guild_id,
                lead_hours,
            )
        else:
            upcoming = []
        starts = await asyncio.to_thread(
            dao.list_events_awaiting_start_announcement,
            guild_id,
        )
        ends = await asyncio.to_thread(
            dao.list_events_awaiting_end_announcement,
            guild_id,
        )

        grace_seconds = runtime_config.lifecycle_grace_hours(guild_id) * 3600
        now = datetime.now(timezone.utc)

        # Upcoming pre-announcements fire when starts_at is within
        # bot.upcoming_lead_hours from now AND the event hasn't started yet.
        # The dao query already applies both filters, so no extra guard here
        # beyond re-checking starts_at for None (defensive).
        for event in upcoming:
            starts_at = _parse_ts(event["starts_at"])
            if starts_at is None:
                await asyncio.to_thread(dao.mark_event_announced_upcoming, event["id"])
                continue
            hours_until_start = (starts_at - now).total_seconds() / 3600.0
            if hours_until_start <= 0:
                # Race between dao query and now(); let the start-announce
                # path handle it. Do not stamp announced_upcoming_at so a
                # later admin who moves starts_at forward can still catch
                # the pre-announcement window if we want that behavior; for
                # now the start block fires immediately anyway.
                continue

            try:
                line = await asyncio.to_thread(
                    voice.announce_event_upcoming,
                    guild_id=guild_id,
                    event_name=event["name"],
                    event_prompt=event["prompt"],
                    primary_metric_label=_format_metric_label(event["primary_metric"]),
                    hours_until_start=hours_until_start,
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"upcoming announcement voice failed for #{event['id']}: {e}")
                continue
            role_prefix = _announce_role_prefix(guild_id)
            await channel.send(
                f"{role_prefix}📣 **{event['name']}** — {line}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=True
                ),
            )
            await asyncio.to_thread(dao.mark_event_announced_upcoming, event["id"])
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="lifecycle.announced_upcoming",
                message=(
                    f"announced upcoming event #{event['id']}: {event['name']} "
                    f"({hours_until_start:.1f}h out)"
                ),
                actor="bot",
                context={
                    "event_id": event["id"],
                    "hours_until_start": round(hours_until_start, 1),
                    "text": line,
                },
            )

        # Start announcements fire regardless of how long ago starts_at was.
        # An event created in the dashboard with a past start date announces
        # on the next scan — that's the intended "immediate" behavior.
        for event in starts:
            starts_at = _parse_ts(event["starts_at"])
            if starts_at is None:
                await asyncio.to_thread(dao.mark_event_announced_start, event["id"])
                continue

            try:
                line = await asyncio.to_thread(
                    voice.announce_event_start,
                    guild_id=guild_id,
                    event_name=event["name"],
                    event_prompt=event["prompt"],
                    primary_metric_label=_format_metric_label(event["primary_metric"]),
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"start announcement voice failed for #{event['id']}: {e}")
                continue
            role_prefix = _announce_role_prefix(guild_id)
            await channel.send(
                f"{role_prefix}🎬 **{event['name']}** — {line}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=True
                ),
            )
            await asyncio.to_thread(dao.mark_event_announced_start, event["id"])
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="lifecycle.announced_start",
                message=f"announced start of event #{event['id']}: {event['name']}",
                actor="bot",
                context={"event_id": event["id"], "text": line},
            )

        for event in ends:
            ends_at = _parse_ts(event["ends_at"])
            if ends_at is None:
                await asyncio.to_thread(dao.mark_event_announced_end, event["id"])
                continue
            if (now - ends_at).total_seconds() > grace_seconds:
                logger.info(
                    f"lifecycle: skipping stale end announcement for event #{event['id']}"
                )
                await asyncio.to_thread(dao.mark_event_announced_end, event["id"])
                await asyncio.to_thread(
                    dao.record_event,
                    level="INFO",
                    category="lifecycle.end_skipped_stale",
                    message=f"end announcement for #{event['id']} skipped (past grace window)",
                    actor="bot",
                    context={"event_id": event["id"], "ends_at": event["ends_at"]},
                )
                continue

            board = await asyncio.to_thread(dao.get_event_leaderboard, event["id"], 3)
            metric = event["primary_metric"]
            if board:
                winner_name = (
                    board[0]["user_display_name"]
                    or board[0]["user_username"]
                    or f"id {board[0]['discord_user_id']}"
                )
                winner_value = _format_metric_value(metric, board[0]["primary_value"])
                runner_ups: list[tuple[str, str]] = []
                for entry in board[1:]:
                    n = entry["user_display_name"] or entry["user_username"] or "?"
                    v = _format_metric_value(metric, entry["primary_value"])
                    runner_ups.append((n, v))
            else:
                winner_name = None
                winner_value = None
                runner_ups = []

            try:
                line = await asyncio.to_thread(
                    voice.announce_event_end,
                    guild_id=guild_id,
                    event_name=event["name"],
                    event_prompt=event["prompt"],
                    primary_metric_label=_format_metric_label(metric),
                    winner_name=winner_name,
                    winner_value_formatted=winner_value,
                    runner_ups=runner_ups,
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"end announcement voice failed for #{event['id']}: {e}")
                continue
            role_prefix = _announce_role_prefix(guild_id)
            await channel.send(
                f"{role_prefix}🏁 **{event['name']}** — {line}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=True
                ),
            )
            await asyncio.to_thread(dao.mark_event_announced_end, event["id"])
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="lifecycle.announced_end",
                message=(
                    f"announced end of event #{event['id']}: {event['name']} "
                    f"(winner={winner_name})"
                ),
                actor="bot",
                context={
                    "event_id": event["id"],
                    "winner": winner_name,
                    "winner_value": winner_value,
                    "text": line,
                },
            )

        reminders_posted = 0
        reminders_skipped = 0
        if runtime_config.reminders_enabled(guild_id):
            open_events = await asyncio.to_thread(
                dao.list_currently_open_events,
                guild_id,
            )
            for event in open_events:
                posted, skipped = await self._maybe_send_reminder(
                    guild_id=guild_id, event=event, now=now, channel=channel
                )
                reminders_posted += posted
                reminders_skipped += skipped

        if upcoming or starts or ends or reminders_posted or reminders_skipped:
            logger.info(
                f"lifecycle scan: upcoming={len(upcoming)} started={len(starts)} "
                f"ended={len(ends)} reminders_posted={reminders_posted} "
                f"reminders_skipped={reminders_skipped}"
            )

    async def _maybe_send_reminder(
        self,
        *,
        guild_id: str,
        event: dict,
        now: datetime,
        channel: discord.abc.Messageable,
    ) -> tuple[int, int]:
        """Post the freshest unsent ending-soon reminder for one open event.

        Returns (posted_count, skipped_count) — the outer scan aggregates
        these for the summary log line. At most one reminder ever posts
        per event per scan; earlier passed milestones that we bypass are
        recorded as silently-skipped so they never re-fire.
        """
        starts_at = _parse_ts(event["starts_at"])
        ends_at = _parse_ts(event["ends_at"])
        if starts_at is None or ends_at is None:
            return 0, 0

        halfway_at = starts_at + (ends_at - starts_at) / 2
        milestones: list[tuple[str, datetime]] = [
            ("halfway", halfway_at),
            ("one_week", ends_at - timedelta(days=7)),
            ("three_day", ends_at - timedelta(days=3)),
            ("one_day", ends_at - timedelta(days=1)),
        ]
        # Drop milestones whose trigger is before the event even starts —
        # short events won't hit every checkpoint.
        milestones = [(k, t) for k, t in milestones if t >= starts_at]
        milestones.sort(key=lambda kt: kt[1])

        passed = [(k, t) for k, t in milestones if t <= now]
        if not passed:
            return 0, 0

        latest_kind, _latest_trigger = passed[-1]

        # Mark earlier passed milestones as silently skipped so future scans
        # never revisit them. This is what implements the "only the freshest
        # reminder fires" rule from steering/architecture.md.
        skipped_count = 0
        for k, _t in passed[:-1]:
            already = await asyncio.to_thread(
                dao.has_reminder_been_sent, event_id=event["id"], kind=k
            )
            if already:
                continue
            await asyncio.to_thread(
                dao.mark_reminder_sent,
                event_id=event["id"],
                kind=k,
                posted=False,
            )
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category=f"lifecycle.reminder_{k}_skipped",
                message=(
                    f"skipped {k} reminder for event #{event['id']} — "
                    f"superseded by {latest_kind}"
                ),
                actor="bot",
                context={
                    "event_id": event["id"],
                    "kind": k,
                    "superseded_by": latest_kind,
                },
            )
            skipped_count += 1

        # Bail if the freshest milestone already fired.
        already_sent = await asyncio.to_thread(
            dao.has_reminder_been_sent, event_id=event["id"], kind=latest_kind
        )
        if already_sent:
            return 0, skipped_count

        remaining_seconds = int((ends_at - now).total_seconds())
        if remaining_seconds >= 86400:
            days = remaining_seconds // 86400
            time_remaining_label = f"{days} day{'s' if days != 1 else ''}"
        elif remaining_seconds >= 3600:
            hours = remaining_seconds // 3600
            time_remaining_label = f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            time_remaining_label = "under an hour"

        board = await asyncio.to_thread(dao.get_event_leaderboard, event["id"], 1)
        summary = await asyncio.to_thread(dao.get_event_summary, event["id"])
        metric = event["primary_metric"]
        if board:
            leader_name: str | None = (
                board[0]["user_display_name"]
                or board[0]["user_username"]
                or f"id {board[0]['discord_user_id']}"
            )
            leader_value: str | None = _format_metric_value(metric, board[0]["primary_value"])
        else:
            leader_name = None
            leader_value = None

        try:
            line = await asyncio.to_thread(
                voice.remind_event_ending,
                guild_id=guild_id,
                event_name=event["name"],
                event_prompt=event["prompt"],
                primary_metric_label=_format_metric_label(metric),
                reminder_kind=latest_kind,
                time_remaining_label=time_remaining_label,
                leader_name=leader_name,
                leader_value_formatted=leader_value,
                participant_count=summary["participant_count"] if summary else 0,
                submission_count=summary["submission_count"] if summary else 0,
            )
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error(
                f"reminder voice failed for #{event['id']}/{latest_kind}: {e}"
            )
            return 0, skipped_count

        emoji_map = {
            "halfway": "⏳",
            "one_week": "📅",
            "three_day": "⏰",
            "one_day": "🚨",
        }
        emoji = emoji_map.get(latest_kind, "⏳")
        role_prefix = _announce_role_prefix(guild_id)
        await channel.send(
            f"{role_prefix}{emoji} **{event['name']}** — {line}",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=True
            ),
        )
        await asyncio.to_thread(
            dao.mark_reminder_sent,
            event_id=event["id"],
            kind=latest_kind,
            posted=True,
        )
        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category=f"lifecycle.reminder_{latest_kind}",
            message=(
                f"posted {latest_kind} reminder for event #{event['id']}: "
                f"{event['name']}"
            ),
            actor="bot",
            context={
                "event_id": event["id"],
                "kind": latest_kind,
                "time_remaining": time_remaining_label,
                "text": line,
            },
        )
        return 1, skipped_count

    @tasks.loop(seconds=int(config.FCB_NAG_INTERVAL_HOURS * 3600))
    async def nag_scheduler(self) -> None:
        try:
            await self._run_nag_scan()
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error(f"nag scheduler scan failed: {e}")
            await asyncio.to_thread(
                dao.record_event,
                level="ERROR",
                category="nag.scan_failed",
                message=f"nag scan failed: {e}",
                actor="bot",
            )

    @nag_scheduler.before_loop
    async def _before_nag_scheduler(self) -> None:
        await self.wait_until_ready()

    async def _run_nag_scan(self) -> None:
        """Iterate every registered guild and run its nag scan."""
        guilds = await asyncio.to_thread(dao.list_bot_guilds)
        for row in guilds:
            await self._run_nag_scan_for_guild(row["guild_id"])

    async def _run_nag_scan_for_guild(self, guild_id: str) -> None:
        if not runtime_config.nag_enabled(guild_id):
            logger.debug(f"nag scan: disabled for guild {guild_id}")
            return

        event = await asyncio.to_thread(dao.get_active_event, guild_id)
        if event is None:
            return  # no active event; scan is a no-op for this guild

        threshold_days = int(event.get("nag_threshold_days") or 0)
        if threshold_days <= 0:
            return  # this event has nagging disabled

        participants = await asyncio.to_thread(
            dao.list_event_participants_from_submissions, event["id"]
        )
        if not participants:
            return

        channel_id_str = runtime_config.channel_id(guild_id)
        if channel_id_str is None:
            return
        channel = self.get_channel(int(channel_id_str))
        if channel is None:
            logger.error(
                f"nag scan: cannot resolve channel {channel_id_str} for guild {guild_id}"
            )
            return

        now = datetime.now(timezone.utc)
        cooldown = runtime_config.nag_cooldown_hours(guild_id)
        nagged = 0
        skipped_uptodate = 0
        skipped_cooldown = 0

        for p in participants:
            uid = p["discord_user_id"]
            last_post = _parse_ts(p["last_submission_at"])
            if last_post is None:
                skipped_uptodate += 1
                continue
            days_since = (now - last_post).total_seconds() / 86400.0
            if days_since < threshold_days:
                skipped_uptodate += 1
                continue

            last_nag_ts = await asyncio.to_thread(
                dao.get_last_nag_time, event_id=event["id"], discord_user_id=uid
            )
            last_nag = _parse_ts(last_nag_ts) if last_nag_ts else None
            if last_nag is not None and (now - last_nag).total_seconds() < cooldown * 3600:
                skipped_cooldown += 1
                continue

            user_row = await asyncio.to_thread(dao.get_user, uid)
            display_name = (
                user_row.get("display_name") if user_row else None
            ) or (user_row.get("username") if user_row else None) or "there"

            try:
                nag_line = await asyncio.to_thread(
                    voice.nag_slacker,
                    guild_id=guild_id,
                    user_display_name=display_name,
                    event_name=event["name"],
                    event_prompt=event["prompt"],
                    days_since_last_post=int(round(days_since)),
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"nag voice call failed for {uid}: {e}")
                await asyncio.to_thread(
                    dao.record_event,
                    level="ERROR",
                    category="nag.voice_failed",
                    message=f"nag voice generation failed for {display_name}: {e}",
                    actor="bot",
                    context={"event_id": event["id"], "user_id": uid},
                )
                continue

            await channel.send(
                f"<@{uid}> {nag_line}",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="nag.sent",
                message=(
                    f"nagged {display_name} ({uid}) — "
                    f"{int(round(days_since))}d since last post"
                ),
                actor="bot",
                context={
                    "event_id": event["id"],
                    "user_id": uid,
                    "days_since": int(round(days_since)),
                    "nag_text": nag_line,
                },
            )
            nagged += 1

        logger.info(
            f"nag scan complete: event=#{event['id']} participants={len(participants)} "
            f"nagged={nagged} skipped_uptodate={skipped_uptodate} "
            f"skipped_cooldown={skipped_cooldown}"
        )
        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category="nag.scan",
            message=(
                f"nag scan: nagged={nagged}, skipped_uptodate={skipped_uptodate}, "
                f"skipped_cooldown={skipped_cooldown}"
            ),
            actor="bot",
            context={
                "event_id": event["id"],
                "participants": len(participants),
                "nagged": nagged,
                "skipped_uptodate": skipped_uptodate,
                "skipped_cooldown": skipped_cooldown,
            },
        )

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info(f"gateway ready as {self.user} (id={self.user.id})")

        # Multi-guild: register every guild the bot is a member of into
        # bot_guilds so the dashboard's server picker sees them, then
        # refresh each guild's channel + role cache from the gateway
        # payload.
        guild_count = len(self.guilds)
        if guild_count == 0:
            invite_url = (
                f"https://discord.com/oauth2/authorize"
                f"?client_id={config.DISCORD_OAUTH_CLIENT_ID}"
                f"&permissions={config.FCB_BOT_PERMISSIONS}"
                f"&scope=bot%20applications.commands"
            )
            logger.warning(
                f"bot is not a member of any guild; invite it with: {invite_url}"
            )
            await asyncio.to_thread(
                dao.record_event,
                level="WARNING",
                category="bot.ready.no_guilds",
                message="bot connected but is not a member of any guild",
                actor="bot",
                context={"invite_url": invite_url},
            )
            return

        for guild in self.guilds:
            await self._refresh_guild_cache(guild)

        logger.info(f"gateway ready in {guild_count} guild(s)")
        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category="bot.ready",
            message=f"connected to gateway; serving {guild_count} guild(s)",
            actor="bot",
            context={
                "guild_count": guild_count,
                "bot_user_id": str(self.user.id),
                "guilds": [{"id": str(g.id), "name": g.name} for g in self.guilds],
            },
        )

    async def _refresh_guild_cache(self, guild: discord.Guild) -> None:
        """Register a guild and push its channels + roles into the DB caches.

        Called on on_ready (for every currently-known guild), on
        on_guild_join (for a newly-added guild), and on channel / role
        change events. The dashboard reads bot_guilds to render the
        server picker and guild_cache to render channel + role
        dropdowns.
        """
        guild_id = str(guild.id)
        icon_hash = str(guild.icon.key) if guild.icon else None
        await asyncio.to_thread(
            dao.upsert_bot_guild, guild_id=guild_id, name=guild.name, icon_hash=icon_hash
        )
        channels = [
            {"id": str(c.id), "name": c.name, "position": int(c.position)}
            for c in guild.channels
            if isinstance(c, discord.TextChannel)
        ]
        roles = [
            {"id": str(r.id), "name": r.name, "position": int(r.position)}
            for r in guild.roles
            if r.name != "@everyone"
        ]
        ch_upserted, ch_pruned = await asyncio.to_thread(
            dao.refresh_guild_cache, guild_id, "channel", channels
        )
        rl_upserted, rl_pruned = await asyncio.to_thread(
            dao.refresh_guild_cache, guild_id, "role", roles
        )
        logger.info(
            f"guild cache refreshed for {guild.name} (id={guild_id}): "
            f"channels(+{ch_upserted}/-{ch_pruned}) "
            f"roles(+{rl_upserted}/-{rl_pruned})"
        )
        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category="guild_cache.refreshed",
            message=(
                f"guild cache refreshed for {guild.name}: "
                f"{ch_upserted} channels, {rl_upserted} roles"
            ),
            actor="bot",
            guild_id=guild_id,
            context={
                "guild_name": guild.name,
                "channels_upserted": ch_upserted,
                "channels_pruned": ch_pruned,
                "roles_upserted": rl_upserted,
                "roles_pruned": rl_pruned,
            },
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """A new guild added the bot — register and cache it."""
        logger.info(f"joined guild {guild.name} (id={guild.id})")
        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category="bot.guild.joined",
            message=f"bot joined guild {guild.name}",
            actor="bot",
            guild_id=str(guild.id),
            context={"guild_name": guild.name, "member_count": guild.member_count},
        )
        await self._refresh_guild_cache(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """The bot was kicked or the guild was deleted — deregister it."""
        logger.info(f"removed from guild {guild.name} (id={guild.id})")
        await asyncio.to_thread(
            dao.record_event,
            level="WARNING",
            category="bot.guild.removed",
            message=f"bot removed from guild {guild.name}",
            actor="bot",
            guild_id=str(guild.id),
            context={"guild_name": guild.name},
        )
        # CASCADE clears guild_cache and guild_settings for this guild.
        await asyncio.to_thread(dao.delete_bot_guild, str(guild.id))

    def _is_known_guild(self, guild: discord.Guild | None) -> bool:
        """True if we should be reacting to events in this guild."""
        if guild is None:
            return False
        return dao.get_bot_guild(str(guild.id)) is not None

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if self._is_known_guild(channel.guild):
            await self._refresh_guild_cache(channel.guild)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self._is_known_guild(channel.guild):
            await self._refresh_guild_cache(channel.guild)

    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if self._is_known_guild(after.guild) and before.name != after.name:
            await self._refresh_guild_cache(after.guild)

    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._is_known_guild(role.guild):
            await self._refresh_guild_cache(role.guild)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._is_known_guild(role.guild):
            await self._refresh_guild_cache(role.guild)

    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        if self._is_known_guild(after.guild) and before.name != after.name:
            await self._refresh_guild_cache(after.guild)

    async def on_message(self, message: discord.Message) -> None:
        # Ignore self and other bots.
        if message.author.bot:
            return

        # Only process messages from guilds the bot is registered in
        # (excludes DMs and stale-state races before on_ready finishes).
        if not self._is_known_guild(message.guild):
            return
        assert message.guild is not None  # narrowed by _is_known_guild
        guild_id = str(message.guild.id)

        # Per-guild channel binding — if the guild hasn't picked a bot
        # channel yet or the message isn't in it, ignore silently.
        bound = runtime_config.channel_id(guild_id)
        if bound is None or str(message.channel.id) != bound:
            return

        author_id = str(message.author.id)
        attachments = [_serialize_attachment(a) for a in message.attachments]
        image_attachments = [a for a in message.attachments if _image_format(a) is not None]
        has_image = bool(image_attachments)

        logger.info(
            f"observed message id={message.id} author={message.author} "
            f"len={len(message.content)} attachments={len(attachments)} image={has_image}"
        )

        # Cache the author's identity so the dashboard can show avatars/names.
        await asyncio.to_thread(
            dao.upsert_user,
            discord_user_id=author_id,
            username=message.author.name,
            display_name=message.author.display_name,
            avatar_url=str(message.author.display_avatar.url),
        )

        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category="message.seen",
            message=f"message from {message.author.display_name} ({author_id})",
            actor=author_id,
            guild_id=guild_id,
            context={
                "message_id": str(message.id),
                "author_name": str(message.author),
                "content_length": len(message.content),
                "attachments": attachments,
                "has_image": has_image,
            },
        )

        if has_image:
            await self._process_workout_images(guild_id, message, image_attachments)
            return

        if self.user is not None and self._is_bot_addressed(message):
            await self._handle_mention(guild_id, message)
            return

    def _bot_names_lower(self, message: discord.Message) -> list[str]:
        """Names the bot goes by, for text-fallback mention detection."""
        assert self.user is not None
        names = {self.user.name.lower()}
        me = message.guild.me if message.guild is not None else None
        if me is not None and me.display_name:
            names.add(me.display_name.lower())
        return list(names)

    def _is_bot_addressed(self, message: discord.Message) -> bool:
        """True if the message @-mentions the bot OR types the bot's name with @.

        Discord only produces a formal mention (i.e. inserts <@ID> into the
        content) when the sender selects the bot from the autocomplete popup.
        People who type "@FitnessChallengeBot" as plain text produce no
        mention. The name-fallback below catches that.
        """
        assert self.user is not None
        if self.user.mentioned_in(message):
            return True
        text_lower = message.content.lower()
        return any(f"@{name}" in text_lower for name in self._bot_names_lower(message))

    async def _handle_mention(self, guild_id: str, message: discord.Message) -> None:
        """Reply to an @-mention (or reply-to-bot) with persona-flavored context."""
        invoker_id = str(message.author.id)
        assert self.user is not None

        remaining = self._llm_cooldown.check_and_stamp(guild_id, invoker_id)
        if remaining is not None:
            await _notify_rate_limited_privately(message, remaining)
            return

        # Strip both the formal mention (<@ID>) and text-fallback @BotName so
        # the LLM only sees the actual question.
        question = re.sub(rf"<@!?{self.user.id}>", "", message.content)
        for name in self._bot_names_lower(message):
            question = re.sub(
                rf"@{re.escape(name)}", "", question, flags=re.IGNORECASE
            )
        question = question.strip()
        if not question:
            question = "(no question, just a hello)"

        # Resolve a referenced image from either:
        # 1. The message being replied to (has image attachments)
        # 2. A Discord message link in the text (https://discord.com/channels/G/C/M)
        referenced_image_bytes: bytes | None = None
        referenced_image_format: str | None = None

        # Check reply reference first
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                for att in ref_msg.attachments:
                    fmt = _image_format(att)
                    if fmt:
                        referenced_image_bytes = await att.read()
                        referenced_image_format = fmt
                        break
            except (discord.NotFound, discord.HTTPException):
                pass

        # Check for Discord message link in text
        if referenced_image_bytes is None:
            link_match = re.search(
                r"https://(?:ptb\.|canary\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)",
                message.content,
            )
            if link_match:
                link_channel_id = int(link_match.group(2))
                link_message_id = int(link_match.group(3))
                try:
                    link_channel = self.get_channel(link_channel_id)
                    if link_channel:
                        link_msg = await link_channel.fetch_message(link_message_id)  # type: ignore[union-attr]
                        for att in link_msg.attachments:
                            fmt = _image_format(att)
                            if fmt:
                                referenced_image_bytes = await att.read()
                                referenced_image_format = fmt
                                break
                except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                    pass

        # Check for a raw message ID (bare snowflake number) in the text
        if referenced_image_bytes is None:
            # Match a standalone 17-20 digit number that looks like a Discord snowflake
            id_match = re.search(r"\b(\d{17,20})\b", question)
            if id_match:
                raw_msg_id = int(id_match.group(1))
                try:
                    ref_msg = await message.channel.fetch_message(raw_msg_id)
                    for att in ref_msg.attachments:
                        fmt = _image_format(att)
                        if fmt:
                            referenced_image_bytes = await att.read()
                            referenced_image_format = fmt
                            break
                except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                    pass

        # Downsize referenced image if over Bedrock's limit
        if referenced_image_bytes and referenced_image_format:
            referenced_image_bytes, referenced_image_format = _downsize_image(
                referenced_image_bytes, referenced_image_format
            )

        async with message.channel.typing():
            active_events = await asyncio.to_thread(
                dao.list_active_events,
                guild_id,
            )

            # Pull recent channel history so the bot can reference what's been
            # happening. Excludes the mention message itself. Oldest → newest.
            history_limit = runtime_config.chat_history_messages(guild_id)
            history_fetch: list[discord.Message] = [
                m async for m in message.channel.history(limit=history_limit + 1)
                if m.id != message.id
            ][:history_limit]
            history_fetch.reverse()
            channel_history: list[dict[str, str]] = []
            for m in history_fetch:
                content = m.clean_content or ""
                if m.attachments:
                    content = (
                        f"{content} [+{len(m.attachments)} attachment(s)]"
                        if content
                        else f"[+{len(m.attachments)} attachment(s)]"
                    )
                if not content:
                    continue
                channel_history.append({
                    "time_ago": _humanize_ago(m.created_at.isoformat()),
                    "author": m.author.display_name,
                    "content": content[:400],
                })

            # Progress snapshots for the invoker plus every OTHER user
            # @-mentioned in the question. This is what makes queries like
            # "how is @El Jefe doing" answerable.
            target_users: list[discord.abc.User] = [message.author]
            seen_ids: set[int] = {message.author.id}
            for u in message.mentions:
                if u.id == self.user.id or u.bot or u.id in seen_ids:
                    continue
                seen_ids.add(u.id)
                target_users.append(u)

            user_snapshots: list[dict[str, object]] = []
            for u in target_users:
                per_event: list[dict[str, object]] = []
                for event in active_events:
                    progress = await asyncio.to_thread(
                        dao.get_user_event_progress,
                        event_id=event["id"],
                        discord_user_id=str(u.id),
                    )
                    metric = event["primary_metric"]
                    if progress["submission_count"] > 0:
                        primary_value = (
                            progress["submission_count"]
                            if metric == "submission_count"
                            else float(progress["totals"].get(metric, 0.0))
                        )
                        primary_formatted = _format_metric_value(metric, primary_value)
                        last_ago = _humanize_ago(progress["last_submission_at"])
                    else:
                        primary_formatted = "none"
                        last_ago = "never"
                    per_event.append({
                        "event_name": event["name"],
                        "primary_metric_label": _format_metric_label(metric),
                        "primary_value": primary_formatted,
                        "submission_count": progress["submission_count"],
                        "last_ago": last_ago,
                    })
                user_snapshots.append({
                    "display_name": u.display_name,
                    "username": u.name,
                    "is_invoker": u.id == message.author.id,
                    "per_event": per_event,
                })

            # Top-10 leaderboard per active event so "who's winning" and
            # "what's the leaderboard" queries have real data to reference.
            leaderboards: list[dict[str, object]] = []
            for event in active_events:
                board = await asyncio.to_thread(
                    dao.get_event_leaderboard, event["id"], 10
                )
                metric = event["primary_metric"]
                entries = [
                    {
                        "rank": i + 1,
                        "display_name": (
                            entry.get("user_display_name")
                            or entry.get("user_username")
                            or f"id {entry['discord_user_id']}"
                        ),
                        "value": _format_metric_value(metric, entry["primary_value"]),
                        "submission_count": entry["submission_count"],
                    }
                    for i, entry in enumerate(board)
                ]
                leaderboards.append({
                    "event_name": event["name"],
                    "primary_metric_label": _format_metric_label(metric),
                    "entries": entries,
                })

            events_summary = [
                {
                    "name": e["name"],
                    "kind": e["kind"],
                    "prompt": e["prompt"][:200],
                    "primary_metric_label": _format_metric_label(e["primary_metric"]),
                }
                for e in active_events
            ]

            # Pull the invoker's last few image posts so the bot can answer
            # "why didn't you count my post?" questions with real reasons
            # from the extracted_stats notes/confidence and the submission
            # status (approved / rejected).
            raw_recent = await asyncio.to_thread(
                dao.list_recent_submissions_by_user,
                discord_user_id=invoker_id,
                limit=3,
            )
            invoker_recent_submissions: list[dict[str, object]] = []
            for r in raw_recent:
                stats_json = r.get("extracted_stats")
                try:
                    stats_dict = (
                        json.loads(stats_json)
                        if isinstance(stats_json, str) and stats_json
                        else {}
                    )
                except json.JSONDecodeError:
                    stats_dict = {}
                invoker_recent_submissions.append({
                    "submission_id": r["id"],
                    "status": r["status"],
                    "event_name": r.get("event_name"),
                    "is_workout_screenshot": stats_dict.get("is_workout_screenshot"),
                    "workout_type": stats_dict.get("workout_type"),
                    "confidence": stats_dict.get("confidence"),
                    "vision_notes": stats_dict.get("notes"),
                    "posted_ago": _humanize_ago(r["created_at"]),
                })

            try:
                reply_text = await asyncio.to_thread(
                    voice.chat_reply,
                    guild_id=guild_id,
                    invoker_display_name=message.author.display_name,
                    question=question,
                    active_events=events_summary,
                    user_snapshots=user_snapshots,
                    leaderboards=leaderboards,
                    channel_history=channel_history,
                    invoker_recent_submissions=invoker_recent_submissions,
                    image_bytes=referenced_image_bytes,
                    image_format=referenced_image_format,
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"chat_reply failed for message {message.id}: {e}")
                await asyncio.to_thread(
                    dao.record_event,
                    level="ERROR",
                    category="chat.failed",
                    message=f"chat reply failed: {e}",
                    actor=invoker_id,
                    context={"message_id": str(message.id)},
                )
                await message.reply(
                    "Ran into an error answering that. Try again in a moment.",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category="chat.replied",
            message=f"@-mention reply to {message.author} ({invoker_id})",
            actor=invoker_id,
            context={
                "message_id": str(message.id),
                "question": question[:500],
                "reply": reply_text[:500],
                "active_event_ids": [int(e["id"]) for e in active_events],
                "history_messages": len(channel_history),
            },
        )

        # Reply as a Discord reply so the author gets pinged naturally.
        await message.reply(
            reply_text,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=False, replied_user=True
            ),
        )

    async def _post_hardcore_riff(
        self,
        *,
        guild_id: str,
        message: discord.Message,
        image_bytes: bytes,
        image_format: str,
        active_event: dict | None,
        submission_id: int,
        reason: str,
    ) -> None:
        """Post the hardcore-riff reply for a non-recognized image.

        Called only when ``bot.reply_only_on_challenge_match`` is false.
        Sends the raw image to Bedrock through voice.hardcore_riff so the
        model can reference specifics visible in the image, then replies
        in-channel and records a ``submission.riff.<reason>`` event.
        """
        try:
            line = await asyncio.to_thread(
                voice.hardcore_riff,
                guild_id=guild_id,
                user_display_name=message.author.display_name,
                image_bytes=image_bytes,
                image_format=image_format,
                active_event_name=active_event["name"] if active_event else None,
                active_event_prompt=active_event["prompt"] if active_event else None,
            )
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error(f"hardcore_riff voice failed for message {message.id}: {e}")
            await asyncio.to_thread(
                dao.record_event,
                level="ERROR",
                category="submission.riff.voice_failed",
                message=f"hardcore_riff voice failed: {e}",
                actor=str(message.author.id),
                context={
                    "message_id": str(message.id),
                    "submission_id": submission_id,
                    "reason": reason,
                },
            )
            return

        await message.reply(f"<@{message.author.id}> {line}")
        await asyncio.to_thread(
            dao.record_event,
            level="INFO",
            category=f"submission.riff.{reason}",
            message=(
                f"posted hardcore riff for submission #{submission_id} "
                f"({reason})"
            ),
            actor=str(message.author.id),
            context={
                "message_id": str(message.id),
                "submission_id": submission_id,
                "reason": reason,
                "text": line,
            },
        )

    async def _process_workout_images(
        self, guild_id: str, message: discord.Message, attachments: list[discord.Attachment]
    ) -> None:
        """Process all image attachments in a message, post one consolidated reply."""
        results: list[dict] = []
        for i, attachment in enumerate(attachments):
            suffix = f"_{i}" if len(attachments) > 1 else ""
            result = await self._process_workout_image(guild_id, message, attachment, msg_id_suffix=suffix)
            if result:
                results.append(result)

        if not results:
            return

        # Build a single consolidated reply
        approved = [r for r in results if r["recognized"]]
        rejected = [r for r in results if not r["recognized"]]

        if not approved:
            # Nothing matched — stay silent in reply_only mode, or riff
            return

        # Single voice acknowledgment covering the batch
        if len(approved) == 1:
            voice_line = approved[0]["voice_line"]
        else:
            # Multiple approved images — generate a combined voice line
            active_events = await asyncio.to_thread(dao.list_active_events, guild_id)
            total_stats: dict[str, float] = {}
            event_names: set[str] = set()
            for r in approved:
                for key in ("elevation_gain_feet", "duration_seconds", "distance_miles", "calories", "reps", "volume_lb"):
                    val = r["stats"].get(key)
                    if val:
                        total_stats[key] = total_stats.get(key, 0) + float(val)
                event_names.add(r["event_name"])

            combined_stats = {**approved[0]["stats"], **total_stats}
            primary_event = approved[0]["primary_event"]
            voice_line = await asyncio.to_thread(
                voice.acknowledge_workout,
                guild_id=guild_id,
                user_display_name=message.author.display_name,
                event_name=primary_event["name"],
                event_prompt=primary_event["prompt"],
                stats=combined_stats,
            )

        submission_ids = [r["submission_id"] for r in approved]
        summaries = [r["summary"] for r in approved]
        footer_parts = [f"{len(approved)} images logged"]
        if len(summaries) == 1:
            footer_parts = [summaries[0]]
        footer_parts.append(f"submissions #{', #'.join(str(s) for s in submission_ids)}")

        await message.reply(
            f"<@{message.author.id}> {voice_line}\n"
            f"-# {' · '.join(footer_parts)}"
        )

    async def _process_workout_image(
        self, guild_id: str, message: discord.Message, attachment: discord.Attachment,
        msg_id_suffix: str = "",
    ) -> dict | None:
        """Process a single image. Returns a result dict if recognized, None otherwise."""
        author_id = str(message.author.id)
        image_format = _image_format(attachment)
        assert image_format is not None  # already filtered
        effective_message_id = str(message.id) + msg_id_suffix

        # Screenshot is always saved first so admins and the dashboard have
        # the artifact even when the bot goes silent on non-recognition.
        image_bytes = await attachment.read()
        safe_name = re.sub(r"[^\w.\-]", "_", attachment.filename)
        screenshot_path = config.FCB_SCREENSHOTS_DIR / f"{message.id}_{safe_name}"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(image_bytes)
        rel_image_path = str(screenshot_path.relative_to(config.REPO_ROOT))
        logger.info(f"saved screenshot to {screenshot_path} ({len(image_bytes)} bytes)")

        # Downsize if over Bedrock's limit before sending to vision
        image_bytes, image_format = _downsize_image(image_bytes, image_format)

        active_events = await asyncio.to_thread(
            dao.list_active_events,
            guild_id,
        )

        # Recognition rule: the bot only posts an acknowledgement when the
        # image is a workout screenshot AND it maps to an active challenge.
        # Every other outcome persists the submission for admin review but
        # produces no channel reply. Users who want to know why can
        # @-mention the bot; the chat_reply path reads recent submissions.
        if not active_events:
            submission_id = await asyncio.to_thread(
                dao.insert_submission,
                guild_id=guild_id,
                event_id=None,
                discord_user_id=author_id,
                message_id=effective_message_id,
                channel_id=str(message.channel.id),
                posted_at=message.created_at.isoformat(),
                image_path=rel_image_path,
                raw_text=message.content or None,
                extracted_stats=None,
                status="rejected",
            )
            if runtime_config.reply_only_on_challenge_match(guild_id):
                logger.info(
                    f"silent: no active event for message {message.id}; "
                    f"stored submission #{submission_id} rejected"
                )
                await asyncio.to_thread(
                    dao.record_event,
                    level="INFO",
                    category="submission.silent.no_event",
                    message=(
                        f"image from {message.author.display_name} received but "
                        f"no active event; stored submission #{submission_id}, no reply"
                    ),
                    actor=author_id,
                    context={"message_id": str(message.id), "submission_id": submission_id},
                )
            else:
                async with message.channel.typing():
                    await self._post_hardcore_riff(
                        guild_id=guild_id,
                        message=message,
                        image_bytes=image_bytes,
                        image_format=image_format,
                        active_event=None,
                        submission_id=submission_id,
                        reason="no_event",
                    )
            return

        primary_hint = active_events[0]

        async with message.channel.typing():
            try:
                stats = await asyncio.to_thread(
                    vision.extract, image_bytes, image_format, primary_hint["prompt"]
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"vision extraction failed for message {message.id}: {e}")
                submission_id = await asyncio.to_thread(
                    dao.insert_submission,
                    guild_id=guild_id,
                    event_id=None,
                    discord_user_id=author_id,
                    message_id=effective_message_id,
                    channel_id=str(message.channel.id),
                    posted_at=message.created_at.isoformat(),
                    image_path=rel_image_path,
                    raw_text=message.content or None,
                    extracted_stats={
                        "is_workout_screenshot": False,
                        "notes": f"vision extraction failed: {e}",
                        "confidence": 0.0,
                    },
                    status="rejected",
                )
                if runtime_config.reply_only_on_challenge_match(guild_id):
                    await asyncio.to_thread(
                        dao.record_event,
                        level="ERROR",
                        category="submission.silent.vision_failed",
                        message=(
                            f"vision failed for message {message.id}: {e}; "
                            f"stored submission #{submission_id}, no reply"
                        ),
                        actor=author_id,
                        context={
                            "message_id": str(message.id),
                            "submission_id": submission_id,
                            "image_path": rel_image_path,
                        },
                    )
                else:
                    await asyncio.to_thread(
                        dao.record_event,
                        level="ERROR",
                        category="submission.riff.vision_failed_source",
                        message=(
                            f"vision failed for message {message.id}: {e}; "
                            f"riff will still fire"
                        ),
                        actor=author_id,
                        context={
                            "message_id": str(message.id),
                            "submission_id": submission_id,
                        },
                    )
                    await self._post_hardcore_riff(
                        guild_id=guild_id,
                        message=message,
                        image_bytes=image_bytes,
                        image_format=image_format,
                        active_event=primary_hint,
                        submission_id=submission_id,
                        reason="vision_failed",
                    )
                return

            # Always ask the router whether this workout actually fits any
            # active event's prompt. The single-event case used to short-
            # circuit here, but that path skipped the relevance check and
            # let unrelated workouts (e.g. treadmill session in a leg-
            # tucks challenge) get auto-attributed. The router already
            # sees WORKOUT_DOMAIN_KNOWLEDGE and knows a specific-exercise
            # challenge isn't satisfied by a session lacking that
            # exercise.
            matched_event_ids: list[int]
            if not stats.is_workout_screenshot:
                matched_event_ids = []
            else:
                try:
                    decision = await asyncio.to_thread(
                        router.route_submission,
                        stats=stats.model_dump(),
                        events=active_events,
                    )
                    matched_event_ids = decision.matched_event_ids
                    logger.info(
                        f"router matched {matched_event_ids} for message {message.id}"
                    )
                    await asyncio.to_thread(
                        dao.record_event,
                        level="INFO",
                        category="router.decision",
                        message=(
                            f"router matched {matched_event_ids} "
                            f"({decision.reasoning})"
                        ),
                        actor="bot",
                        context={
                            "message_id": str(message.id),
                            "matched_event_ids": matched_event_ids,
                            "reasoning": decision.reasoning,
                            "active_event_ids": [int(e["id"]) for e in active_events],
                        },
                    )
                except Exception as e:
                    logger.error(traceback.format_exc())
                    logger.error(f"router failed for message {message.id}: {e}")
                    # Fail-closed: if the router can't decide, treat it as
                    # unmatched rather than fanning out to every active
                    # event. Fanning out was the old behavior and directly
                    # caused the leaderboard pollution bug on submission #18.
                    matched_event_ids = []
                    await asyncio.to_thread(
                        dao.record_event,
                        level="ERROR",
                        category="router.failed",
                        message=(
                            f"router failed, treating as unmatched "
                            f"(no attribution): {e}"
                        ),
                        actor="bot",
                        context={"message_id": str(message.id)},
                    )

            recognized = stats.is_workout_screenshot and bool(matched_event_ids)
            primary_event_id = matched_event_ids[0] if matched_event_ids else None
            submission_id = await asyncio.to_thread(
                dao.insert_submission,
                guild_id=guild_id,
                event_id=primary_event_id,
                discord_user_id=author_id,
                message_id=effective_message_id,
                channel_id=str(message.channel.id),
                posted_at=message.created_at.isoformat(),
                image_path=rel_image_path,
                raw_text=message.content or None,
                extracted_stats=stats.model_dump(),
                status="approved" if recognized else "rejected",
            )
            logger.info(
                f"stored submission #{submission_id} "
                f"status={'approved' if recognized else 'rejected'} "
                f"for user {author_id}, matched_events={matched_event_ids}"
            )
            for i, eid in enumerate(matched_event_ids):
                await asyncio.to_thread(
                    dao.link_submission_to_event,
                    submission_id=submission_id,
                    event_id=eid,
                    is_primary=(i == 0),
                )

            if not recognized:
                reason = "not_workout" if not stats.is_workout_screenshot else "no_match"
                if runtime_config.reply_only_on_challenge_match(guild_id):
                    await asyncio.to_thread(
                        dao.record_event,
                        level="INFO",
                        category=f"submission.silent.{reason}",
                        message=(
                            f"submission #{submission_id} silent ({reason})"
                        ),
                        actor=author_id,
                        context={
                            "message_id": str(message.id),
                            "submission_id": submission_id,
                            "is_workout_screenshot": stats.is_workout_screenshot,
                            "confidence": stats.confidence,
                            "workout_type": stats.workout_type,
                            "notes": stats.notes,
                        },
                    )
                else:
                    await self._post_hardcore_riff(
                        guild_id=guild_id,
                        message=message,
                        image_bytes=image_bytes,
                        image_format=image_format,
                        active_event=primary_hint,
                        submission_id=submission_id,
                        reason=reason,
                    )
                return None

            summary = _summarize_stats(stats)
            matched_by_id = {int(e["id"]): e for e in active_events}
            matched_events = [
                matched_by_id[eid] for eid in matched_event_ids if eid in matched_by_id
            ]

            await asyncio.to_thread(
                dao.record_event,
                level="INFO",
                category="submission.approved",
                message=f"submission #{submission_id} auto-approved for {author_id}: {summary}",
                actor=author_id,
                context={
                    "message_id": str(message.id),
                    "submission_id": submission_id,
                    "matched_event_ids": matched_event_ids,
                    "workout_type": stats.workout_type,
                    "confidence": stats.confidence,
                },
            )

            primary_event = matched_events[0]
            voice_line = await asyncio.to_thread(
                voice.acknowledge_workout,
                guild_id=guild_id,
                user_display_name=message.author.display_name,
                event_name=primary_event["name"],
                event_prompt=primary_event["prompt"],
                stats=stats.model_dump(),
            )

        return {
            "recognized": True,
            "submission_id": submission_id,
            "summary": summary,
            "voice_line": voice_line,
            "stats": stats.model_dump(),
            "matched_event_ids": matched_event_ids,
            "event_name": primary_event["name"],
            "primary_event": primary_event,
        }


def run() -> None:
    """Blocking entrypoint: connect and dispatch until interrupted."""
    client = FCBClient(intents=_intents())
    client.run(config.DISCORD_BOT_TOKEN, log_handler=None)
