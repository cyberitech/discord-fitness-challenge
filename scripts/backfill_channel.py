"""Backfill all image posts from a channel through the bot's normal processing pipeline.

Fetches every message with image attachments from the given channel,
skips any whose message_id already exists in the submissions table,
and runs the rest through _process_workout_images.

Usage:
    uv run python scripts/backfill_channel.py <channel_id> [--dry-run] [--limit N]

Options:
    --dry-run   Print what would be processed without actually running vision.
    --limit N   Stop after N messages (for testing).
"""

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import discord  # noqa: E402
from fcb import config  # noqa: E402
from fcb.bot.client import FCBClient, _image_format, _intents  # noqa: E402
from fcb.db import dao  # noqa: E402


def main(channel_id: int, dry_run: bool, limit: int | None) -> None:
    intents = _intents()
    client = FCBClient(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if channel is None:
                print(f"Channel {channel_id} not accessible")
                return

            # Collect all existing message_ids so we can skip already-processed ones.
            # Strip suffixes like "_s0", "_0" that multi-session submissions append.
            existing_raw = dao.list_all_submission_message_ids()
            existing = {mid.split("_")[0] for mid in existing_raw}
            print(f"Found {len(existing)} existing submission message IDs to skip")

            to_process: list[discord.Message] = []
            print(f"Scanning channel {channel_id} for image messages...")
            async for message in channel.history(limit=None, oldest_first=True):
                image_attachments = [
                    a for a in message.attachments if _image_format(a) is not None
                ]
                if not image_attachments:
                    continue
                if str(message.id) in existing:
                    print(f"  skip {message.id} (already processed)")
                    continue
                to_process.append(message)
                if limit and len(to_process) >= limit:
                    break

            print(f"\n{len(to_process)} messages to process")

            if dry_run:
                for msg in to_process:
                    img_count = sum(
                        1 for a in msg.attachments if _image_format(a) is not None
                    )
                    print(
                        f"  would process: {msg.id} "
                        f"by {msg.author.display_name} "
                        f"at {msg.created_at.isoformat()} "
                        f"({img_count} image(s))"
                    )
                return

            for i, message in enumerate(to_process, 1):
                image_attachments = [
                    a for a in message.attachments if _image_format(a) is not None
                ]
                print(
                    f"[{i}/{len(to_process)}] processing {message.id} "
                    f"by {message.author.display_name} "
                    f"({len(image_attachments)} image(s))"
                )
                await client._process_workout_images(
                    guild_id=str(message.guild.id),
                    message=message,
                    attachments=image_attachments,
                )
                print(f"  done")

            print(f"\nBackfill complete: {len(to_process)} messages processed")
        finally:
            await client.close()

    client.run(config.DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel_id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(args.channel_id, args.dry_run, args.limit)
