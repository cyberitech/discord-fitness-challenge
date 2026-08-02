"""Bot-installation onboarding page.

Two entry points:

- ``GET /setup`` — generic install screen without a target guild
  preselected. Used when the user has no admin guilds yet.
- ``GET /setup/{guild_id}`` — install screen with the target guild
  preselected on the Discord OAuth URL. Reached from the server
  picker when the user clicks a guild the bot isn't in yet.

The install URL uses Discord's OAuth authorize flow with both the
``bot`` and ``applications.commands`` scopes plus the permissions
bitmask from ``config.FCB_BOT_PERMISSIONS``. Discord handles the
"pick a server" step and the permission review; once the user
confirms, the bot receives a GUILD_CREATE event, the bot registers
the new guild into ``bot_guilds``, and the picker on refresh shows
the server as installed.
"""

from __future__ import annotations

import logging
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fcb import config
from fcb.dashboard.ui import admin_from_session, page

logger = logging.getLogger(__name__)

router = APIRouter(tags=["setup"])


def _invite_url(guild_id: str | None = None) -> str:
    params: dict[str, str] = {
        "client_id": config.DISCORD_OAUTH_CLIENT_ID,
        "scope": "bot applications.commands",
        "permissions": str(config.FCB_BOT_PERMISSIONS),
    }
    if guild_id:
        params["guild_id"] = guild_id
        params["disable_guild_select"] = "true"
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


def _instructions(target_name: str | None, invite_url: str) -> str:
    if target_name:
        heading = f"Install the bot in <b>{escape(target_name)}</b>"
        note = (
            "Discord will show this exact server pre-selected. You just "
            "need to confirm the permissions."
        )
    else:
        heading = "Install FitnessChallengeBot in a server"
        note = "Discord will ask you which server to install into."

    return f"""
<h1 style="margin-bottom:0.5rem;">{heading}</h1>
<p class="muted">{note}</p>

<div class="card">
  <h2 style="margin-top:0;">1. Add the bot to your server</h2>
  <p>You need Manage Server permission on the target guild. Click the
  button below — it opens Discord in a new tab.</p>
  <p>
    <a class="btn" href="{escape(invite_url)}" target="_blank" rel="noopener">
      Add bot to a server
    </a>
  </p>
</div>

<div class="card">
  <h2 style="margin-top:0;">2. Confirm the permissions</h2>
  <p>Default permissions are the minimum FCB needs. Leave them checked
  and hit Authorize.</p>
  <p class="muted">The bot needs: view channels, send messages, add
  reactions, embed links, attach files, read message history, mention
  everyone (for role pings), and use application commands. It does
  <b>not</b> touch member management, channel management, or voice.</p>
</div>

<div class="card">
  <h2 style="margin-top:0;">3. Come back here</h2>
  <p>Once Discord confirms the install, the bot receives the join event
  and registers the server on its end. Refresh the dashboard — your
  server will appear in the picker.</p>
  <p><a class="btn secondary" href="/">Back to picker</a></p>
</div>

<div class="card">
  <h2 style="margin-top:0;">4. First-time setup in your server</h2>
  <p>Once your server is showing in the picker, click into it and:</p>
  <ul>
    <li><b>Settings</b> — pick the channel the bot listens in.</li>
    <li><b>Settings</b> — optional: pick roles to ping on lifecycle events.</li>
    <li><b>Events</b> — create your first challenge.</li>
  </ul>
</div>
"""


@router.get("/setup", response_class=HTMLResponse, response_model=None)
async def setup_index(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    body = _instructions(None, _invite_url())
    return HTMLResponse(page(title="Setup", body=body, admin=admin))


@router.get("/setup/{guild_id}", response_class=HTMLResponse, response_model=None)
async def setup_guild(
    request: Request, guild_id: str
) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    admin_guilds = admin.get("admin_guilds") or []
    target = next(
        (g for g in admin_guilds if str(g.get("id")) == guild_id), None
    )
    if target is None:
        # User isn't a Discord admin on that guild — send them back with
        # a generic setup screen rather than deep-linking to a random id.
        logger.warning(
            f"user {admin['user_id']} hit /setup/{guild_id} but doesn't admin it"
        )
        return RedirectResponse("/setup", status_code=303)

    body = _instructions(str(target["name"]), _invite_url(guild_id))
    return HTMLResponse(page(title=f"Setup — {target['name']}", body=body, admin=admin))
