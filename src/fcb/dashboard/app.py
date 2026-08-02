"""FastAPI application for the FCB admin dashboard.

Multi-guild flow:

1. ``/`` — public log-in prompt for anonymous, access-denied for
   non-allowlisted, server picker for authenticated, or the guild
   dashboard when a ``current_guild_id`` is stashed in the session.
2. ``/select-guild`` — POST from a picker row that sets the session's
   ``current_guild_id`` and redirects to home.
3. ``/switch-guild`` — clears ``current_guild_id`` and drops the user
   back on the picker.
4. ``/setup/{guild_id}`` — install instructions for a guild the user
   admins but the bot isn't in yet (implemented in
   :mod:`fcb.dashboard.setup`).
"""

import json
import logging
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from fcb import config, logging_setup
from fcb.dashboard import (
    admins,
    auth,
    events,
    logs,
    settings_general,
    settings_voice,
    setup,
    submissions,
)
from fcb.dashboard.ui import admin_from_session, current_guild, page
from fcb.db import dao

_STATIC_DIR = Path(__file__).resolve().parent / "static"

logging_setup.configure()
logger = logging.getLogger(__name__)

app = FastAPI(title="FCB Dashboard")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.DASHBOARD_SESSION_SECRET,
    https_only=True,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(auth.router)
app.include_router(setup.router)
app.include_router(events.router)
app.include_router(submissions.router)
app.include_router(admins.router)
app.include_router(logs.router)
app.include_router(settings_voice.router)
app.include_router(settings_general.router)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> RedirectResponse:
    return RedirectResponse("/static/favicon.png", status_code=308)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Guild-aware home: log-in prompt / server picker / guild dashboard
# ---------------------------------------------------------------------------


_LOGIN_BODY = """
<div class="card" style="text-align:center; margin-top:6rem;">
  <h1>FCB Admin Dashboard</h1>
  <p class="muted">Sign in with your Discord account to continue.</p>
  <p style="margin-top:2rem;"><a class="btn" href="/auth/login">Log in with Discord</a></p>
</div>
"""


def _guild_icon(guild_id: str, name: str, icon_hash: str | None) -> str:
    """Render either a Discord guild icon or a lettered fallback tile."""
    if icon_hash:
        url = (
            f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=128"
        )
        return f'<img src="{escape(url)}" alt="">'
    initials = "".join(w[0] for w in name.split()[:2]).upper()[:2] or "?"
    return escape(initials)


def _picker_row(
    *, guild_id: str, name: str, icon_hash: str | None,
    installed: bool, subtitle: str
) -> str:
    icon_html = _guild_icon(guild_id, name, icon_hash)
    if installed:
        href = f'/select-guild?guild_id={escape(guild_id)}'
        # Use a POST via form so we can trust the redirect target from a
        # server-set session value rather than a query param.
        return f"""
<form method="post" action="/select-guild" class="picker-row installed" style="width:100%;">
  <input type="hidden" name="guild_id" value="{escape(guild_id)}">
  <button type="submit" style="all:unset;width:100%;cursor:pointer;display:flex;align-items:center;gap:1rem;">
    <span class="icon">{icon_html}</span>
    <span class="meta">
      <span class="name">{escape(name)}</span>
      <span class="subtitle">{escape(subtitle)}</span>
    </span>
    <span class="action">Enter →</span>
  </button>
</form>
"""
    else:
        return f"""
<a class="picker-row" href="/setup/{escape(guild_id)}">
  <span class="icon">{icon_html}</span>
  <span class="meta">
    <span class="name">{escape(name)}</span>
    <span class="subtitle">{escape(subtitle)}</span>
  </span>
  <span class="action">Set up →</span>
</a>
"""


def _render_picker(admin: dict) -> str:
    admin_guilds = admin.get("admin_guilds") or []
    bot_guild_ids = {row["guild_id"] for row in dao.list_bot_guilds()}

    if not admin_guilds:
        return f"""
<h1>Welcome, {escape(admin['global_name'])}</h1>
<div class="card">
  <p>You're signed in, but you're not a Discord admin on any server the
  bot could serve. Give the bot a home and come back:</p>
  <p><a class="btn" href="/setup">Add the bot to a server</a></p>
</div>
"""

    rows = []
    for g in admin_guilds:
        gid = str(g["id"])
        installed = gid in bot_guild_ids
        subtitle = (
            "Bot installed. Click to enter."
            if installed
            else "Bot not installed yet. Click to set up."
        )
        rows.append(
            _picker_row(
                guild_id=gid,
                name=str(g["name"]),
                icon_hash=g.get("icon"),  # type: ignore[arg-type]
                installed=installed,
                subtitle=subtitle,
            )
        )
    picker_html = f'<div class="picker">{"".join(rows)}</div>'

    return f"""
<h1>Pick a server</h1>
<p class="muted">Servers you can administer on Discord.</p>
{picker_html}
<div class="card muted" style="margin-top:1.5rem;">
  Not seeing a server? You need Manage Server or Administrator permission
  on Discord for it to appear here. Or install the bot fresh via the
  <a href="/setup">setup page</a>.
</div>
"""


def _default_avatar(discord_user_id: str) -> str:
    idx = (int(discord_user_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def _humanize_ago(iso_ts: str | None) -> str:
    if not iso_ts:
        return "never"
    normalized = iso_ts.replace("T", " ")[:19]
    try:
        posted = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return iso_ts
    seconds = int((datetime.now(timezone.utc) - posted).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _guild_home(admin: dict, guild: dict) -> str:
    """The default view for a picked guild — active events + activity feed."""
    guild_id = guild["guild_id"]
    active = dao.list_active_events(guild_id)
    if not active:
        events_block = """
<div class="card" style="text-align:center;padding:2.5rem;">
  <h2 style="margin-top:0;">No active challenge</h2>
  <p class="muted">Nothing is running right now. Create an event to start tracking.</p>
  <a class="btn" href="/events/new">Create event</a>
</div>
"""
    else:
        rows = []
        for e in active:
            summary = dao.get_event_summary(e["id"])
            if summary is None:
                continue
            rows.append(f"""
<div class="card" style="border:1px solid var(--accent);">
  <h2 style="margin-top:0;">{escape(summary['name'])}</h2>
  <p class="muted">{summary['submission_count']} workouts · {summary['participant_count']} participants</p>
  <a class="btn secondary" href="/events/{summary['id']}">Manage event</a>
</div>
""")
        events_block = "\n".join(rows)

    recent = dao.get_recent_approved_submissions_in_guild(guild_id=guild_id, limit=8)
    if recent:
        items = []
        for r in recent:
            uid = r["discord_user_id"]
            display = (
                r.get("user_display_name")
                or r.get("user_username")
                or f"id {uid}"
            )
            avatar = r.get("user_avatar_url") or _default_avatar(uid)
            try:
                stats = json.loads(r.get("extracted_stats") or "{}")
            except json.JSONDecodeError:
                stats = {}
            summary_bits = []
            if stats.get("workout_type"):
                summary_bits.append(str(stats["workout_type"]))
            if stats.get("volume_lb"):
                summary_bits.append(f"{int(round(float(stats['volume_lb']))):,} lb-reps")
            elif stats.get("distance_miles"):
                summary_bits.append(f"{float(stats['distance_miles']):.2f} mi")
            summary_line = " · ".join(summary_bits) or "workout logged"
            items.append(f"""
<div style="display:flex;align-items:center;gap:0.75rem;padding:0.6rem 0;border-bottom:1px solid var(--border);">
  <img class="avatar" src="{escape(avatar)}" alt="" style="width:28px;height:28px;">
  <b>{escape(display)}</b>
  <span class="muted" style="flex:1;">{escape(summary_line)}</span>
  <span class="muted" style="font-size:0.85rem;">{_humanize_ago(r['created_at'])}</span>
</div>
""")
        activity_block = f"""
<div class="card">
  <h2 style="margin-top:0;">Recent activity</h2>
  {''.join(items)}
</div>
"""
    else:
        activity_block = ""

    return f"""
<h1 style="margin-bottom:0.25rem;">{escape(guild['name'])}</h1>
<p class="muted" style="margin-top:0;">Signed in as {escape(admin['global_name'])}</p>
{events_block}
{activity_block}
"""


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    admin = admin_from_session(request)
    if admin is None:
        return HTMLResponse(page(title="Sign in", body=_LOGIN_BODY))

    guild = current_guild(request)
    if guild is None:
        # No guild picked yet — render the server picker.
        return HTMLResponse(
            page(title="Pick a server", body=_render_picker(admin), admin=admin)
        )

    return HTMLResponse(
        page(
            title=guild["name"],
            body=_guild_home(admin, guild),
            admin=admin,
            active="home",
            guild=guild,
        )
    )


@app.post("/select-guild")
async def select_guild(request: Request, guild_id: str = Form(...)) -> RedirectResponse:
    """Set the session's current_guild_id and land on the guild home.

    Only permits guilds the user actually admins on Discord (from
    session.admin_guilds) and where the bot is installed (from
    bot_guilds). Anything else redirects back to the picker.
    """
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    admin_ids = {str(g["id"]) for g in admin.get("admin_guilds") or []}
    if guild_id not in admin_ids:
        logger.warning(
            f"user {admin['user_id']} tried to select guild {guild_id} "
            f"but doesn't admin it"
        )
        return RedirectResponse("/", status_code=303)
    if dao.get_bot_guild(guild_id) is None:
        # User admins the guild but the bot isn't in it — go to setup.
        return RedirectResponse(f"/setup/{guild_id}", status_code=303)

    request.session["current_guild_id"] = guild_id
    logger.info(f"admin {admin['user_id']} selected guild {guild_id}")
    dao.record_event(
        level="INFO",
        category="dashboard.guild_selected",
        message=f"{admin['global_name']} entered dashboard for guild {guild_id}",
        actor=admin["user_id"],
        guild_id=guild_id,
    )
    return RedirectResponse("/", status_code=303)


@app.get("/switch-guild")
async def switch_guild(request: Request) -> RedirectResponse:
    """Drop the current guild selection and return to the picker."""
    request.session.pop("current_guild_id", None)
    return RedirectResponse("/", status_code=303)
