"""Environment configuration for FCB.

Values are loaded from the process environment, which is populated by
systemd's EnvironmentFile in production and by python-dotenv reading
`.env` when running out of a checked-out repo. Every required setting is
validated at import time; missing values fail loudly rather than surface
later as attribute errors.

The bot is multi-guild: it serves every guild it's a member of. There
is no single-guild binding in `.env` — per-guild settings (channel id,
announce roles, voice persona, behavior toggles) live in the
`guild_settings` table, read through `runtime_config`.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

_env_file = REPO_ROOT / ".env"
if _env_file.is_file():
    load_dotenv(_env_file)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


# --- Discord ---
DISCORD_BOT_TOKEN: str = _required("DISCORD_BOT_TOKEN")
DISCORD_OAUTH_CLIENT_ID: str = _required("DISCORD_OAUTH_CLIENT_ID")
DISCORD_OAUTH_CLIENT_SECRET: str = _required("DISCORD_OAUTH_CLIENT_SECRET")

# The developer's Discord user id. Always has full dashboard access
# regardless of the `admins` table; they alone can add or remove
# other admins. Baked into .env because nothing in the UI is allowed
# to modify it (that's the point).
FCB_DEVELOPER_USER_ID: str = _required("FCB_DEVELOPER_USER_ID")

# Bit sum of the Discord permissions FCB actually uses. View Channels
# + Send Messages + Add Reactions + Embed Links + Attach Files +
# Read Message History + Mention Everyone + Use Application Commands.
# See dashboard setup page which builds the bot-invite URL from this.
FCB_BOT_PERMISSIONS: int = int(_optional("FCB_BOT_PERMISSIONS", "2147732544"))

# --- Dashboard ---
DASHBOARD_SESSION_SECRET: str = _required("DASHBOARD_SESSION_SECRET")
FCB_WEB_PORT: int = int(_optional("FCB_WEB_PORT", "8765"))
# Public host used to construct the OAuth redirect URI and the bot-
# invite URL's redirect target. Something like
# "https://fitness-challenge-bot.cyberian.me" — no trailing slash.
FCB_PUBLIC_BASE_URL: str = _optional(
    "FCB_PUBLIC_BASE_URL", "https://fitness-challenge-bot.cyberian.me"
)

# --- Bedrock ---
BEDROCK_MODEL_ID: str = _optional("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
AWS_REGION: str = _optional("AWS_REGION", "us-west-2")

# --- Paths ---
FCB_DB_PATH: Path = _resolve_path(_optional("FCB_DB_PATH", "data/fcb.db"))
FCB_SCREENSHOTS_DIR: Path = _resolve_path(_optional("FCB_SCREENSHOTS_DIR", "data/screenshots"))

# --- Nag scheduler (process-wide interval; per-guild enable / cooldown live in guild_settings) ---
FCB_NAG_ENABLED: bool = _optional("FCB_NAG_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)
FCB_NAG_INTERVAL_HOURS: float = float(_optional("FCB_NAG_INTERVAL_HOURS", "1"))
FCB_NAG_COOLDOWN_HOURS: float = float(_optional("FCB_NAG_COOLDOWN_HOURS", "24"))

# --- Lifecycle scheduler (process-wide interval; per-guild toggles live in guild_settings) ---
FCB_LIFECYCLE_INTERVAL_MINUTES: float = float(_optional("FCB_LIFECYCLE_INTERVAL_MINUTES", "5"))
FCB_LIFECYCLE_GRACE_HOURS: float = float(_optional("FCB_LIFECYCLE_GRACE_HOURS", "24"))
FCB_REMINDERS_ENABLED: bool = _optional("FCB_REMINDERS_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)
FCB_UPCOMING_LEAD_HOURS: float = float(_optional("FCB_UPCOMING_LEAD_HOURS", "48"))
FCB_REPLY_ONLY_ON_CHALLENGE_MATCH: bool = _optional(
    "FCB_REPLY_ONLY_ON_CHALLENGE_MATCH", "true"
).lower() in ("true", "1", "yes", "on")

# --- Per-user command cooldown (per-guild override via bot.command_cooldown_seconds) ---
FCB_COMMAND_COOLDOWN_SECONDS: int = int(_optional("FCB_COMMAND_COOLDOWN_SECONDS", "30"))

# --- @-mention chat context (per-guild override via bot.chat_history_messages) ---
FCB_CHAT_HISTORY_MESSAGES: int = int(_optional("FCB_CHAT_HISTORY_MESSAGES", "100"))

# --- Logging ---
LOG_LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()


def log_summary() -> None:
    """Emit a one-line INFO summary of the resolved configuration."""
    logger.info(
        f"config loaded: developer={FCB_DEVELOPER_USER_ID} "
        f"db={FCB_DB_PATH} model={BEDROCK_MODEL_ID} region={AWS_REGION} "
        f"log_level={LOG_LEVEL}"
    )
