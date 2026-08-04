"""One-shot: connect to Discord, fetch specific messages by id, and
run them through the bot's normal ``_process_workout_images`` flow so
the bot behaves as if the message was just posted.

Usage:
    uv run python scripts/reprocess_messages.py <channel_id> <msg_id> [<msg_id> ...]
"""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import discord  # noqa: E402
from fcb import config  # noqa: E402
from fcb.bot.client import FCBClient, _image_format, _intents  # noqa: E402


def main(channel_id: int, message_ids: list[int]) -> None:
    intents = _intents()
    client = FCBClient(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if channel is None:
                print(f"Channel {channel_id} not accessible")
                return

            for msg_id in message_ids:
                print(f"\n=== fetching message {msg_id} ===")
                try:
                    message = await channel.fetch_message(msg_id)
                except discord.NotFound:
                    print(f"  message {msg_id} not found")
                    continue
                except discord.Forbidden:
                    print(f"  no permission to fetch {msg_id}")
                    continue

                image_attachments = [
                    a for a in message.attachments if _image_format(a) is not None
                ]
                if not image_attachments:
                    print(f"  no image attachments on {msg_id}")
                    continue

                print(f"  running _process_workout_images with {len(image_attachments)} image(s)")
                await client._process_workout_images(
                    guild_id=str(message.guild.id),
                    message=message,
                    attachments=image_attachments,
                )
                print(f"  done with {msg_id}")
        finally:
            await client.close()

    client.run(config.DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    ch = int(sys.argv[1])
    mids = [int(x) for x in sys.argv[2:]]
    main(ch, mids)
