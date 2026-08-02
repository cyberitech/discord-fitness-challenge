"""Shared HTML layout for the FCB dashboard.

Phase 7b keeps this as server-rendered f-string HTML with a small design
system embedded in one block of CSS. A future phase replaces it with a
Vite/React SPA served from the same FastAPI app.
"""

from html import escape

_CSS = """
:root {
  --bg: #0f172a;
  --panel: #1e293b;
  --panel-2: #273449;
  --text: #e2e8f0;
  --muted: #94a3b8;
  --accent: #5865f2;
  --accent-hover: #4752c4;
  --border: #334155;
  --danger: #ef4444;
  --success: #22c55e;
}
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 0;
}
nav {
  display: flex; align-items: center; gap: 1.5rem;
  padding: 1rem 2rem; background: #0b1220;
  border-bottom: 1px solid var(--border);
}
nav a { color: var(--muted); text-decoration: none; font-weight: 500; }
nav a:hover, nav a.active { color: var(--text); }
nav .spacer { flex: 1; }
nav .me { color: var(--text); }
main a { color: #93c5fd; }
main a:hover { color: #bfdbfe; }
main .btn, main .btn:hover { color: white; }
main { max-width: 960px; margin: 2rem auto; padding: 0 2rem; }
h1 { font-weight: 600; letter-spacing: -0.02em; margin: 0 0 1.5rem; }
h2 { font-weight: 600; letter-spacing: -0.01em; margin: 2rem 0 1rem; }
.muted { color: var(--muted); font-size: 0.9rem; }
.card {
  background: var(--panel); border-radius: 8px;
  padding: 1.5rem; margin-bottom: 1.5rem;
}
.btn {
  display: inline-block; padding: 0.6rem 1.2rem;
  background: var(--accent); color: white;
  text-decoration: none; border: none; cursor: pointer;
  border-radius: 6px; font-weight: 600; font-size: 0.95rem;
}
.btn:hover { background: var(--accent-hover); }
.btn.secondary { background: var(--panel-2); }
.btn.secondary:hover { background: #34405a; }
.btn.danger { background: var(--danger); }
.btn.danger:hover { background: #dc2626; }
dialog {
  background: var(--panel); color: var(--text);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 1.5rem 1.75rem; min-width: 420px; max-width: 90vw;
}
dialog::backdrop { background: rgba(0,0,0,0.6); }
dialog h3 { margin: 0 0 0.75rem; font-weight: 600; }
dialog .dialog-actions {
  display: flex; gap: 0.6rem; justify-content: flex-end; margin-top: 1.5rem;
}
table { width: 100%; border-collapse: collapse; }
th, td {
  text-align: left; padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
th { color: var(--muted); font-weight: 500; font-size: 0.85rem;
     text-transform: uppercase; letter-spacing: 0.05em; }
tr:last-child td { border-bottom: none; }
.tag {
  display: inline-block; padding: 0.15rem 0.55rem;
  font-size: 0.75rem; font-weight: 600;
  border-radius: 999px; background: var(--panel-2); color: var(--muted);
}
.tag.active { background: var(--success); color: #04220b; }
.tag.kind-challenge { background: #a855f7; color: #1a0230; }
.tag.kind-daily_1pct { background: #06b6d4; color: #012029; }
form label {
  display: block; margin-top: 1rem;
  font-size: 0.85rem; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500;
}
form input[type="text"], form input[type="datetime-local"],
form select, form textarea {
  width: 100%; margin-top: 0.4rem; padding: 0.6rem 0.8rem;
  background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text);
  font-family: inherit; font-size: 1rem;
}
form textarea { min-height: 200px; resize: vertical; }
form input:focus, form select:focus, form textarea:focus {
  outline: none; border-color: var(--accent);
}
.form-actions { display: flex; gap: 0.75rem; margin-top: 1.5rem; }
.avatar { width: 32px; height: 32px; border-radius: 50%;
          vertical-align: middle; margin-right: 0.5rem; }
.guild-pill {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.35rem 0.7rem; border-radius: 999px;
  background: var(--panel-2); color: var(--text) !important;
  font-weight: 600; font-size: 0.9rem;
  text-decoration: none;
  border: 1px solid var(--border);
}
.guild-pill:hover { background: #34405a; }
.picker { display: flex; flex-direction: column; gap: 0.6rem; margin-top: 0.5rem; }
.picker-row {
  display: flex; align-items: center; gap: 1rem;
  padding: 1rem; background: var(--panel-2);
  border-radius: 8px; text-decoration: none; color: var(--text);
  border: 1px solid transparent;
}
.picker-row:hover { border-color: var(--accent); }
.picker-row .icon {
  width: 48px; height: 48px; border-radius: 12px; flex-shrink: 0;
  background: #0f172a; display: grid; place-items: center;
  color: var(--muted); font-weight: 600; font-size: 1.1rem;
  overflow: hidden;
}
.picker-row .icon img { width: 100%; height: 100%; object-fit: cover; }
.picker-row .meta { flex: 1; }
.picker-row .meta .name { font-weight: 600; }
.picker-row .meta .subtitle { color: var(--muted); font-size: 0.85rem; margin-top: 0.15rem; }
.picker-row .action { color: var(--muted); }
.picker-row.installed .action { color: var(--success); }
.hint {
  display: inline-block;
  margin-left: 0.35rem;
  font-size: 0.75rem;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 50%;
  width: 14px; height: 14px;
  line-height: 12px; text-align: center;
  cursor: help;
  user-select: none;
  vertical-align: middle;
  text-transform: none;
}
.hint:hover { color: var(--text); border-color: var(--text); }
form select[multiple] {
  min-height: 8em;
  padding: 0.4rem 0.6rem;
}
form select[multiple] option {
  padding: 0.25rem 0.4rem;
  border-radius: 3px;
}
form select[multiple] option:checked {
  background: var(--accent) linear-gradient(0deg, var(--accent), var(--accent));
  color: white;
}
form .row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}
"""


def _nav(admin: dict | None, active: str, guild: dict | None = None) -> str:
    if admin is None:
        return ""
    avatar = admin.get("avatar_url", "")
    name = escape(admin.get("global_name") or "admin")

    def link(href: str, label: str, key: str) -> str:
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    # Guild switcher pill — shows the currently-selected guild and links
    # back to the picker. When no guild is picked, hide the whole
    # domain-scoped nav; the home page is a picker in that state.
    guild_pill = ""
    scoped_links = ""
    if guild is not None:
        guild_pill = (
            f'<a class="guild-pill" href="/switch-guild" '
            f'title="Switch server">◆ {escape(guild["name"])} <span class="muted">▾</span></a>'
        )
        scoped_links = "\n  ".join([
            link("/", "Home", "home"),
            link("/events", "Events", "events"),
            link("/submissions", "Submissions", "submissions"),
            link("/settings", "Settings", "settings"),
            link("/settings/voice", "Voice", "voice"),
            link("/logs", "Logs", "logs"),
        ])
        # Developer-only: admins management
        if admin.get("is_developer"):
            scoped_links += "\n  " + link("/admins", "Admins", "admins")
    else:
        scoped_links = link("/", "Home", "home")

    return f"""<nav>
  {guild_pill}
  {scoped_links}
  <span class="spacer"></span>
  <span class="me"><img class="avatar" src="{escape(avatar)}"> {name}</span>
  <a href="/auth/logout">Log out</a>
</nav>"""


def page(
    *,
    title: str,
    body: str,
    admin: dict | None = None,
    active: str = "",
    guild: dict | None = None,
) -> str:
    """Wrap body content in the shared shell.

    ``guild`` is the bot_guilds row for the currently-selected guild, if
    any. When omitted but admin has a current_guild_id, auto-resolves
    the guild so the nav renders correctly. Passing guild=None explicitly
    forces no guild context (picker page, access-denied).
    """
    if guild is None and admin is not None and admin.get("current_guild_id"):
        from fcb.db import dao
        guild = dao.get_bot_guild(admin["current_guild_id"])

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{escape(title)} · FCB Dashboard</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="apple-touch-icon" href="/static/logo.png">
<style>{_CSS}</style>
</head>
<body>
{_nav(admin, active, guild)}
<main>{body}</main>
</body>
</html>
"""


def admin_from_session(request) -> dict | None:  # type: ignore[no-untyped-def]
    """Read the authenticated dashboard user from the session, or None.

    Grants access to any Discord identity that is either the developer
    (``FCB_DEVELOPER_USER_ID``) or on the ``admins`` allowlist. The
    session gate here is a fast check — the OAuth callback in
    ``auth.py`` is where the actual authorization decision is made and
    the session gets populated.

    Returned shape includes:

    - ``user_id`` / ``username`` / ``global_name`` / ``avatar_url``
    - ``is_developer`` — bool; controls admin-management visibility
    - ``admin_guilds`` — list of {id, name, icon, owner} the user can admin
    - ``current_guild_id`` — the guild the user picked, or None
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return {
        "user_id": user_id,
        "username": request.session.get("username"),
        "global_name": request.session.get("global_name"),
        "avatar_url": request.session.get("avatar_url"),
        "is_developer": bool(request.session.get("is_developer", False)),
        "admin_guilds": list(request.session.get("admin_guilds") or []),
        "current_guild_id": request.session.get("current_guild_id"),
    }


def current_guild(request) -> dict | None:  # type: ignore[no-untyped-def]
    """Return the bot_guilds row for the session's current guild, or None.

    Every protected route that reads guild-scoped data SHOULD call this
    and redirect to ``/`` when it returns None. That's how we push the
    user through the server picker before showing them any data.
    """
    gid = request.session.get("current_guild_id")
    if not gid:
        return None
    from fcb.db import dao  # avoid module-level cycle

    return dao.get_bot_guild(gid)
