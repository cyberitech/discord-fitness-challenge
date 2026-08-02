"""Discord OAuth flow and admin gate for the FCB dashboard.

Multi-guild access model:

- **Developer** (``config.FCB_DEVELOPER_USER_ID``) — always granted access,
  regardless of the ``admins`` allowlist. Only the developer sees
  ``/admins`` and can add / remove admins.
- **Admins** — Discord users on the ``admins`` allowlist. Full access to
  dashboard functionality except admin management.
- **Everyone else** — bounced with a "contact the developer" screen.

After login the user is shown a server picker (see ``dashboard/app.py``)
built from the intersection of the guilds they admin on Discord (via the
``guilds`` OAuth scope, filtered by ``MANAGE_GUILD`` / ``ADMINISTRATOR``)
and the guilds the bot is present in (via ``bot_guilds``). Guilds where
the user is admin but the bot isn't route into ``/setup/{guild_id}``.
"""

import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fcb import config
from fcb.db import dao

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_DISCORD_API = "https://discord.com/api/v10"
_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_TOKEN_URL = f"{_DISCORD_API}/oauth2/token"

_REDIRECT_URI = f"{config.FCB_PUBLIC_BASE_URL}/auth/callback"
_SCOPES = ("identify", "guilds")

# Discord permission bitmask flags relevant to "can this user admin this guild".
_MANAGE_GUILD = 0x20
_ADMINISTRATOR = 0x8


def _user_admins_guild(guild: dict) -> bool:
    """True when the OAuth-returned guild says the user has admin power."""
    try:
        perms = int(guild.get("permissions", 0))
    except (TypeError, ValueError):
        return False
    return bool(perms & (_MANAGE_GUILD | _ADMINISTRATOR)) or guild.get("owner", False)


def is_developer(user_id: str | None) -> bool:
    """True when the given user id is the hardcoded developer."""
    return user_id is not None and user_id == config.FCB_DEVELOPER_USER_ID


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Kick off Discord OAuth. Redirects the browser to Discord."""
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    params = {
        "client_id": config.DISCORD_OAUTH_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(_SCOPES),
        "state": state,
    }
    url = f"{_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info("redirecting to discord authorize endpoint")
    return RedirectResponse(url)


@router.get("/callback", response_model=None)
async def callback(
    request: Request, code: str, state: str
) -> HTMLResponse | RedirectResponse:
    """Complete OAuth exchange and populate the session."""
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        logger.warning(f"oauth state mismatch (got={state!r}, expected={expected_state!r})")
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT_URI,
            },
            auth=(config.DISCORD_OAUTH_CLIENT_ID, config.DISCORD_OAUTH_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data["access_token"]

        auth_headers = {"Authorization": f"Bearer {access_token}"}
        user_resp = await client.get(f"{_DISCORD_API}/users/@me", headers=auth_headers)
        user_resp.raise_for_status()
        user = user_resp.json()

        guilds_resp = await client.get(
            f"{_DISCORD_API}/users/@me/guilds", headers=auth_headers
        )
        guilds_resp.raise_for_status()
        guilds = guilds_resp.json()

    user_id = user["id"]
    username = user["username"]
    global_name = user.get("global_name") or username

    dev = is_developer(user_id)
    allowed = dev or dao.is_admin(user_id)

    logger.info(
        f"oauth callback: user={username} id={user_id} "
        f"is_developer={dev} on_allowlist={dao.is_admin(user_id)} allowed={allowed}"
    )
    dao.record_event(
        level="INFO",
        category="dashboard.oauth",
        message=(
            f"login attempt by {username} ({user_id}) "
            f"developer={dev} allowed={allowed}"
        ),
        actor=user_id,
    )

    # Cache the user identity regardless of whether they got in, so the
    # developer can see who tried when they check the logs.
    avatar_url = _avatar_url(user_id, user.get("avatar"))
    dao.upsert_user(
        discord_user_id=user_id,
        username=username,
        display_name=global_name,
        avatar_url=avatar_url,
    )

    if not allowed:
        request.session.clear()
        return HTMLResponse(
            _access_denied_page(username, user_id, avatar_url),
            status_code=403,
        )

    # Extract only the guilds where the user has admin permissions. Store
    # a compact form in the session — the picker reads it and cross-
    # references bot_guilds to decide which entries are click-to-enter
    # vs click-to-setup.
    admin_guilds: list[dict[str, object]] = []
    for g in guilds:
        if not _user_admins_guild(g):
            continue
        admin_guilds.append(
            {
                "id": str(g["id"]),
                "name": g["name"],
                "icon": g.get("icon"),  # hash string or None
                "owner": bool(g.get("owner", False)),
            }
        )
    # Sort: guilds where the user is owner first, then alphabetical.
    admin_guilds.sort(key=lambda g: (not g["owner"], str(g["name"]).lower()))

    request.session["user_id"] = user_id
    request.session["username"] = username
    request.session["global_name"] = global_name
    request.session["avatar_url"] = avatar_url
    request.session["is_developer"] = dev
    request.session["admin_guilds"] = admin_guilds
    # Clear any previously-selected guild so the user lands on the picker.
    request.session.pop("current_guild_id", None)

    return RedirectResponse("/", status_code=302)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=302)


def _avatar_url(user_id: str, avatar_hash: str | None) -> str:
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=128"
    idx = (int(user_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def _access_denied_page(username: str, user_id: str, avatar_url: str) -> str:
    from html import escape

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Access denied · FCB Dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
         margin: 0; padding: 3rem 1.5rem; text-align: center; }}
  .card {{ max-width: 520px; margin: 0 auto; background: #1e293b;
           border-radius: 8px; padding: 2rem; }}
  .avatar {{ width: 64px; height: 64px; border-radius: 50%; margin-bottom: 1rem; }}
  h1 {{ margin-top: 0; font-weight: 600; }}
  code {{ background: #273449; padding: 0.15rem 0.4rem; border-radius: 4px; }}
  a {{ color: #93c5fd; }}
  .btn {{ display: inline-block; margin-top: 1.5rem; padding: 0.6rem 1.2rem;
          background: #273449; color: #e2e8f0; text-decoration: none;
          border-radius: 6px; }}
</style>
</head>
<body>
<div class="card">
  <img class="avatar" src="{escape(avatar_url)}" alt="">
  <h1>You're not on the allowlist</h1>
  <p>The developer of this application has not allowlisted your Discord
     identity to use it. Contact them and ask for permission.</p>
  <p class="muted">Signed in as <b>{escape(username)}</b> — <code>{escape(user_id)}</code></p>
  <a class="btn" href="/auth/logout">Log out</a>
</div>
</body>
</html>
"""
