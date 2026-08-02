"""General settings page.

One admin-facing page bundling the runtime-tunable settings that used to
live only in `.env`. Values are stored in the ``settings`` table and read
by :mod:`fcb.runtime_config`; most take effect immediately.

Fields marked "restart-required" are consumed at process start (the
`discord.ext.tasks.loop` decorator interval, for instance). The page
surfaces a restart button that fires `sudo systemctl restart fcb-bot
fcb-web` via a small sudoers rule.

Channel and role selectors are populated dynamically from Discord's REST
API through :mod:`fcb.dashboard.discord_api`. If Discord is unreachable
the page falls back to raw text inputs with the current stored IDs so
an admin can still fix things by hand.
"""

import json
import logging
import shlex
import subprocess
import time
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fcb import runtime_config
from fcb.dashboard import discord_api
from fcb.dashboard.ui import admin_from_session, page
from fcb.db import dao

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


# Guard the restart button against click-spamming (in-memory, process-local).
_last_restart_at: float = 0.0
_RESTART_COOLDOWN_SECONDS = 15


def _current(guild_id: str) -> dict[str, str]:
    """Resolve every setting on this page to its current effective value."""
    return {
        "discord.channel_id": runtime_config.channel_id(guild_id) or "",
        "discord.announce_role_ids": json.dumps(runtime_config.announce_role_ids(guild_id)),
        "bot.nag_enabled": "true" if runtime_config.nag_enabled(guild_id) else "false",
        "bot.nag_interval_hours": str(runtime_config.nag_interval_hours()),
        "bot.nag_cooldown_hours": str(runtime_config.nag_cooldown_hours(guild_id)),
        "bot.lifecycle_interval_minutes": str(runtime_config.lifecycle_interval_minutes()),
        "bot.lifecycle_grace_hours": str(runtime_config.lifecycle_grace_hours(guild_id)),
        "bot.upcoming_lead_hours": str(runtime_config.upcoming_lead_hours(guild_id)),
        "bot.reminders_enabled": "true" if runtime_config.reminders_enabled(guild_id) else "false",
        "bot.reply_only_on_challenge_match": (
            "true" if runtime_config.reply_only_on_challenge_match(guild_id) else "false"
        ),
        "bot.command_cooldown_seconds": str(runtime_config.command_cooldown_seconds(guild_id)),
        "bot.chat_history_messages": str(runtime_config.chat_history_messages(guild_id)),
    }


def _hint(text: str) -> str:
    """Render a tooltip icon per steering/dashboard.md."""
    return f'<span class="hint" title="{escape(text)}">&#9432;</span>'


def _channel_field(current_id: str, channels: list[dict[str, str]]) -> str:
    """Channel dropdown when Discord API is reachable; text fallback otherwise."""
    if not channels:
        return (
            f'<input type="text" name="discord.channel_id" pattern="[0-9]+" '
            f'value="{escape(current_id)}" required '
            f'placeholder="Discord channel ID (couldn\'t reach Discord API)">'
        )
    options = []
    seen_current = False
    for c in channels:
        selected = " selected" if c["id"] == current_id else ""
        if selected:
            seen_current = True
        options.append(
            f'<option value="{escape(c["id"])}"{selected}>'
            f'#{escape(c["name"])}</option>'
        )
    # If the stored channel isn't in the dropdown (renamed / deleted /
    # different guild), surface it so the admin can see and fix it.
    if current_id and not seen_current:
        options.insert(
            0,
            f'<option value="{escape(current_id)}" selected>'
            f'(unknown / not in guild — {escape(current_id)})</option>',
        )
    return (
        f'<select name="discord.channel_id" required>{"".join(options)}</select>'
    )


def _role_field(current_ids: list[str], roles: list[dict[str, str]]) -> str:
    """Role multi-select when Discord API is reachable; text fallback otherwise."""
    if not roles:
        return (
            f'<input type="text" name="discord.announce_role_ids_raw" '
            f'value="{escape(", ".join(current_ids))}" '
            f'placeholder="Discord role IDs, comma-separated '
            f'(couldn\'t reach Discord API)">'
        )
    current_set = set(current_ids)
    known_ids = {r["id"] for r in roles}
    options = []
    for r in roles:
        selected = " selected" if r["id"] in current_set else ""
        options.append(
            f'<option value="{escape(r["id"])}"{selected}>'
            f'{escape(r["name"])}</option>'
        )
    # Surface any stored role IDs that Discord didn't return, so admins can
    # see and remove them rather than have them silently vanish.
    orphaned = [rid for rid in current_ids if rid not in known_ids]
    for rid in orphaned:
        options.insert(
            0,
            f'<option value="{escape(rid)}" selected>'
            f'(unknown / not in guild — {escape(rid)})</option>',
        )
    return (
        '<select name="discord.announce_role_ids" multiple size="6">'
        f'{"".join(options)}</select>'
    )


@router.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_page(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    guild_id = admin.get("current_guild_id")
    if not guild_id:
        return RedirectResponse("/", status_code=307)

    cur = _current(guild_id)

    channels = await discord_api.list_guild_text_channels(guild_id)
    roles = await discord_api.list_guild_roles(guild_id)
    current_role_ids = runtime_config.announce_role_ids(guild_id)

    channel_field = _channel_field(cur["discord.channel_id"], channels)
    role_field = _role_field(current_role_ids, roles)

    def sel(key: str, value: str) -> str:
        return " selected" if cur[key] == value else ""

    body = f"""
<h1>Settings</h1>
<p class="muted" style="margin-top:0;">
  Runtime-tunable configuration. Hot fields take effect on the next call;
  restart-required fields need the services restarted.
</p>

<form method="post" action="/settings">
  <div class="card">
    <h2 style="margin-top:0;">Discord binding</h2>

    <label>Guild {_hint("Bound guild ID; edit in .env and restart to change.")}</label>
    <input type="text" value="{escape(guild_id)}" disabled style="opacity:0.6;">

    <label>Raffle / activity channel {_hint("Channel the bot listens to and posts announcements in.")}</label>
    {channel_field}

    <label>Announce roles {_hint("Roles pinged on start / end / reminder / upcoming announcements. Hold Cmd/Ctrl to multi-select.")}</label>
    {role_field}
  </div>

  <div class="card">
    <h2 style="margin-top:0;">Scheduling
      <span class="tag" style="background:#78350f;color:#fed7aa;">some fields restart-required</span>
    </h2>

    <label>Nag scheduler {_hint("Master switch for the periodic nudge to users who haven't posted in a while.")}</label>
    <select name="bot.nag_enabled">
      <option value="true"{sel("bot.nag_enabled", "true")}>Enabled</option>
      <option value="false"{sel("bot.nag_enabled", "false")}>Disabled</option>
    </select>

    <div class="row">
      <div>
        <label>Nag interval (hours) {_hint("How often the nag scanner runs. Restart required.")}</label>
        <input type="number" step="0.1" min="0.1" name="bot.nag_interval_hours"
               value="{escape(cur['bot.nag_interval_hours'])}" required>
      </div>
      <div>
        <label>Nag cooldown per user (hours) {_hint("Minimum hours between nags to the same person for the same event.")}</label>
        <input type="number" step="0.5" min="0" name="bot.nag_cooldown_hours"
               value="{escape(cur['bot.nag_cooldown_hours'])}" required>
      </div>
    </div>

    <label>Ending-soon reminders {_hint("Post halfway / 1w / 3d / 1d nudges for open challenges. Only the freshest milestone fires.")}</label>
    <select name="bot.reminders_enabled">
      <option value="true"{sel("bot.reminders_enabled", "true")}>Enabled</option>
      <option value="false"{sel("bot.reminders_enabled", "false")}>Disabled</option>
    </select>

    <div class="row">
      <div>
        <label>Upcoming lead time (hours) {_hint("Post a heads-up when a challenge starts within N hours. 0 disables.")}</label>
        <input type="number" step="1" min="0" name="bot.upcoming_lead_hours"
               value="{escape(cur['bot.upcoming_lead_hours'])}" required>
      </div>
      <div>
        <label>Lifecycle scan interval (minutes) {_hint("How often the bot checks for start / end / reminder events. Restart required.")}</label>
        <input type="number" step="1" min="1" name="bot.lifecycle_interval_minutes"
               value="{escape(cur['bot.lifecycle_interval_minutes'])}" required>
      </div>
    </div>

    <label>End-announcement grace (hours) {_hint("Skip the winner shoutout if the event ended more than N hours ago. Start announcements always fire.")}</label>
    <input type="number" step="1" min="0" name="bot.lifecycle_grace_hours"
           value="{escape(cur['bot.lifecycle_grace_hours'])}" required>
  </div>

  <div class="card">
    <h2 style="margin-top:0;">Image reply mode</h2>

    <label>Reply only on challenge match {_hint("On: bot stays silent on images that don't match an active challenge. Off: bot replies to every image with a hardcore-motivating riff.")}</label>
    <select name="bot.reply_only_on_challenge_match">
      <option value="true"{sel("bot.reply_only_on_challenge_match", "true")}>Enabled (silent on unrecognized)</option>
      <option value="false"{sel("bot.reply_only_on_challenge_match", "false")}>Disabled (hardcore riff on unrecognized)</option>
    </select>
  </div>

  <div class="card">
    <h2 style="margin-top:0;">Rate limiting</h2>

    <div class="row">
      <div>
        <label>LLM command cooldown (seconds) {_hint("Per-user cooldown shared by /status, /list, /debug, Describe, and @-mention.")}</label>
        <input type="number" step="1" min="0" name="bot.command_cooldown_seconds"
               value="{escape(cur['bot.command_cooldown_seconds'])}" required>
      </div>
      <div>
        <label>@-mention chat history size {_hint("How many recent channel messages the bot loads into context when someone @-mentions it.")}</label>
        <input type="number" step="10" min="0" max="500" name="bot.chat_history_messages"
               value="{escape(cur['bot.chat_history_messages'])}" required>
      </div>
    </div>
  </div>

  <div class="form-actions">
    <button type="submit" class="btn">Save all settings</button>
  </div>
</form>

<div class="card">
  <h2 style="margin-top:0;">Service control</h2>
  <p class="muted" style="margin-top:0;">
    Restart both processes. Runtime-hot changes don't need this; restart-required fields do.
  </p>
  <button type="button" class="btn danger"
          onclick="document.getElementById('restart-dialog').showModal()">Restart bot &amp; dashboard</button>
</div>

<dialog id="restart-dialog">
  <h3>Restart services?</h3>
  <p>This will restart <code>fcb-bot</code> and <code>fcb-web</code>. The bot drops from
  Discord for a few seconds; the dashboard is briefly unreachable.</p>
  <div class="dialog-actions">
    <button type="button" class="btn secondary"
            onclick="this.closest('dialog').close()">Cancel</button>
    <form method="post" action="/settings/restart" style="display:inline;">
      <button type="submit" class="btn danger">Restart</button>
    </form>
  </div>
</dialog>
"""
    return HTMLResponse(page(title="Settings", body=body, admin=admin, active="settings"))


@router.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    guild_id = admin.get("current_guild_id")
    if not guild_id:
        return RedirectResponse("/", status_code=307)

    form = await request.form()

    updates: dict[str, str] = {}

    channel_id = str(form.get("discord.channel_id", "")).strip()
    if channel_id.isdigit():
        updates["discord.channel_id"] = channel_id

    # Prefer the multi-select values; fall back to the raw CSV field only when
    # the dropdown wasn't rendered (Discord API unreachable at render time).
    role_ids: list[str] = [
        str(v).strip()
        for v in form.getlist("discord.announce_role_ids")
        if str(v).strip().isdigit()
    ]
    if not role_ids:
        raw_roles = str(form.get("discord.announce_role_ids_raw", "")).strip()
        if raw_roles:
            role_ids = [
                tok.strip()
                for tok in raw_roles.replace("\n", ",").split(",")
                if tok.strip().isdigit()
            ]
    # Only write when the user actually interacted with a rendered field.
    if (
        "discord.announce_role_ids" in form
        or "discord.announce_role_ids_raw" in form
    ):
        updates["discord.announce_role_ids"] = json.dumps(role_ids)

    for bool_key in (
        "bot.nag_enabled",
        "bot.reminders_enabled",
        "bot.reply_only_on_challenge_match",
    ):
        raw_bool = str(form.get(bool_key, "")).strip().lower()
        if raw_bool in ("true", "false"):
            updates[bool_key] = raw_bool

    for numeric_key in (
        "bot.nag_interval_hours",
        "bot.nag_cooldown_hours",
        "bot.lifecycle_interval_minutes",
        "bot.lifecycle_grace_hours",
        "bot.upcoming_lead_hours",
        "bot.command_cooldown_seconds",
        "bot.chat_history_messages",
    ):
        raw = str(form.get(numeric_key, "")).strip()
        if not raw:
            continue
        try:
            float(raw)
        except ValueError:
            logger.warning(f"non-numeric value for {numeric_key}: {raw!r}; skipping")
            continue
        updates[numeric_key] = raw

    if updates:
        global_keys = {"bot.nag_interval_hours", "bot.lifecycle_interval_minutes"}
        guild_updates = {k: v for k, v in updates.items() if k not in global_keys}
        global_updates = {k: v for k, v in updates.items() if k in global_keys}
        if guild_updates:
            dao.upsert_guild_settings(guild_id, guild_updates, updated_by=admin["user_id"])
        if global_updates:
            dao.upsert_global_settings(global_updates, updated_by=admin["user_id"])

    logger.info(f"admin {admin['user_id']} saved settings: {list(updates)}")
    dao.record_event(
        level="INFO",
        category="settings.general.updated",
        message=f"admin {admin['global_name']} updated general settings",
        actor=admin["user_id"],
        context={"keys": list(updates)},
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/restart")
async def restart_services(request: Request) -> RedirectResponse:
    global _last_restart_at
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    now = time.monotonic()
    if now - _last_restart_at < _RESTART_COOLDOWN_SECONDS:
        logger.warning("restart rejected: within cooldown")
        return RedirectResponse("/settings", status_code=303)
    _last_restart_at = now

    dao.record_event(
        level="WARNING",
        category="settings.restart",
        message=f"admin {admin['global_name']} triggered service restart",
        actor=admin["user_id"],
    )
    logger.warning(f"admin {admin['user_id']} triggered systemctl restart")

    # Detach so the HTTP response can finish before fcb-web itself gets kicked.
    # The 3s sleep gives the redirect + browser reload time to complete.
    cmd = (
        "sleep 3 && sudo /usr/bin/systemctl restart fcb-bot.service fcb-web.service"
    )
    subprocess.Popen(  # noqa: S603 - fixed command, no shell injection surface
        ["sh", "-c", cmd],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info(f"scheduled restart via: {shlex.quote(cmd)}")
    return RedirectResponse("/settings", status_code=303)
