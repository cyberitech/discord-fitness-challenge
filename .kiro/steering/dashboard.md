---
inclusion: always
---

# Dashboard

FCB's admin dashboard is a FastAPI application at
`https://fitness-challenge-bot.cyberian.me/`, reverse-proxied by the
existing nginx to `127.0.0.1:$FCB_WEB_PORT`. It renders server-side HTML
directly from Python — there is no JS build step and no SPA. Every page
follows the same shape and every mutation follows POST-then-303-redirect
to prevent double-submits.

This document is the authoritative guide for **modifying the dashboard**
or **adding a new page**. Cross-references: routing lives in
`.kiro/steering/architecture.md` (package layout); auth binding to
Discord in `discord.md`; deployment / systemd in `deployment.md`; how
runtime settings flow to the dashboard in `settings.md`.

## Package Layout

```
src/fcb/dashboard/
├── __init__.py
├── __main__.py             # `uv run python -m fcb.dashboard` — uvicorn entrypoint
├── app.py                  # FastAPI() instance, middleware, router registration, home
├── auth.py                 # /auth/login, /auth/callback, /auth/logout
├── ui.py                   # page() wrapper, nav, CSS, admin_from_session()
├── events.py               # /events, /events/new, /events/{id}, participants, CSV export
├── submissions.py          # /submissions, /submissions/{id}, /media/submissions/{id}/image
├── admins.py               # /admins, add/remove
├── logs.py                 # /logs (reads bot_events)
├── settings_general.py     # /settings, /settings/restart
├── settings_voice.py       # /settings/voice
└── static/                 # served at /static via StaticFiles
    ├── favicon.png
    └── logo.png
```

Each surface has its own router module. `app.py` imports each module and
calls `app.include_router(...)`. Add a new page by adding a new module,
importing it, and registering the router.

## URL Conventions

- `/` — home. Public for unauthenticated visitors (renders a Log-in
  page); authenticated admins see the summary view.
- `/auth/*` — OAuth flow. Publicly reachable by design.
- `/healthz` — liveness. Public, no auth. Returns `{"ok": true}`.
- `/static/*` — static assets. Public.
- `/favicon.ico` — 308-redirect to `/static/favicon.png`.
- Every other route MUST require an admin session.

## Authentication and the Admin Gate

Flow, top-to-bottom:

```
Browser → /auth/login  → generate random state, stash in session,
                          redirect to Discord authorize URL
Discord  → /auth/callback?code&state
                        → verify state, exchange code for token
                        → GET /users/@me, /users/@me/guilds
                        → require membership in bound guild AND row in admins
                        → set session cookie, upsert into users table
                        → 302 → /
```

The session cookie is signed by
`starlette.middleware.sessions.SessionMiddleware` using
`DASHBOARD_SESSION_SECRET`. `https_only=True` is set; uvicorn is launched
with `proxy_headers=True` and `forwarded_allow_ips="127.0.0.1"` so
Starlette recognizes nginx-terminated HTTPS.

### `ui.admin_from_session(request)`

The single source of truth for "is this request from a logged-in admin."
Every protected route MUST call it first, and every protected route MUST
redirect to `/` (307) when it returns `None`. This helper also
opportunistically upserts the admin's identity into the `users` table on
every hit so their avatar and display name stay fresh in the dashboard
even before they next log in.

Return shape:

```python
{
  "user_id": str,        # Discord user id
  "username": str,
  "global_name": str,    # display fallback if no guild nick
  "avatar_url": str | None,
}
```

Canonical protected-route pattern:

```python
@router.get("/some-page", response_class=HTMLResponse, response_model=None)
async def page_view(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    # ...
    return HTMLResponse(page(title="...", body=..., admin=admin, active="..."))
```

`response_model=None` is required whenever the return type is a union
including `HTMLResponse` and `RedirectResponse`; FastAPI tries to build
a Pydantic response model from the annotation otherwise and raises on
startup.

## Shared UI (`ui.py`)

Every rendered page passes through `ui.page(title, body, admin, active)`.
It emits the `<html>` shell, `<head>` (title, favicon, embedded CSS),
top nav, and wraps `body` in `<main>`. The `active` string highlights
the current nav item (`home`, `events`, `submissions`, `settings`,
`voice`, `admins`, `logs`).

### Adding a nav link

Edit `_nav()` in `ui.py`:

```python
{link('/foo', 'Foo', 'foo')}
```

Then pass `active="foo"` from every page in that section. Nav is flat —
no dropdowns.

### CSS

One embedded stylesheet in `ui.py._CSS`. Variables at the top set the
theme; components (`.card`, `.btn`, `.tag`, `table`, `form`, `dialog`)
follow. Keep new styles inside this block so the page stays a single
self-contained document. Small per-element `style="..."` attributes are
fine for one-off layout.

## Forms and the POST-Redirect-GET Pattern

All mutating routes MUST be `POST` and MUST end with a `RedirectResponse`
using `status_code=303` back to the resource. This prevents browser
"resubmit form?" prompts on reload.

```python
@router.post("/events/{event_id}")
async def update_event(request: Request, event_id: int,
                       name: str = Form(...), ...) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    dao.update_event(event_id, name=name.strip(), ...)
    dao.record_event(level="INFO", category="event.updated", ...)
    return RedirectResponse(f"/events/{event_id}", status_code=303)
```

Every mutation SHOULD log a `bot_events` row via `dao.record_event`
carrying the actor's user id — the logs panel is the audit trail.

## Confirm Dialogs

Use the native HTML `<dialog>` element with the shared CSS in
`ui.py._CSS`. Opens via `element.showModal()` from an `onclick`. The
existing pattern for a destructive confirm is in
`events.py._confirm_dialog(...)`:

```
[button "Delete"]  →  <dialog>
                        <h3>Are you sure?</h3>
                        <p>Body…</p>
                        <div class="dialog-actions">
                          <button>Cancel</button>
                          <form method="post" action="…/delete">
                            <button class="btn danger">Confirm</button>
                          </form>
                        </div>
                      </dialog>
```

## Static Assets and Favicon

`app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")`
serves everything in `src/fcb/dashboard/static/`. Favicons live there,
committed at 128×128 (`favicon.png`) and 512×512 (`logo.png`). The
`page()` wrapper injects both `<link rel="icon">` and
`<link rel="apple-touch-icon">` on every page.

## Media (User-Submitted Content)

Screenshots from Discord submissions live outside the package in
`data/screenshots/`. `submissions.py` serves them through the
admin-gated route `/media/submissions/{id}/image`. The route MUST
resolve the file path from the `submissions.image_path` column (never
from a query parameter), and MUST verify the resolved file is under
`config.FCB_SCREENSHOTS_DIR` before returning it. This prevents path
traversal even if the DB is somehow corrupted.

## Rate Limiting Considerations

Dashboard mutations SHOULD limit their blast radius:

- Restart button: `settings_general.py` uses an in-memory 15-second
  cooldown so a stuck admin can't cycle-restart the services.
- Destructive routes (`/events/{id}/reset`, `/events/{id}/delete`) rely
  on the confirm dialog for friction; no explicit cooldown.

Bot-side rate limiting (LLM commands) is a separate concern documented
in `architecture.md`.

## Adding a New Page — End-to-End Checklist

1. Create `src/fcb/dashboard/<feature>.py` with an `APIRouter`.
2. Every route uses `admin_from_session(request)` first and returns
   `RedirectResponse("/", status_code=307)` on `None`.
3. GET routes: `response_class=HTMLResponse, response_model=None`.
4. Any HTML the route emits goes through `ui.page(...)` with an
   `active` slug.
5. POST routes: form-parse via `Form(...)`, call the appropriate `dao`
   helper, record a `bot_events` row, return
   `RedirectResponse(..., status_code=303)`.
6. Register the router in `app.py`: import and call
   `app.include_router(<feature>.router)`.
7. If the page belongs in top nav: add a `link(...)` entry to
   `_nav()` in `ui.py` with a matching `active` slug.
8. Update this document's "Package Layout" section and, if the page
   introduces a new user-facing capability, update
   `architecture.md`.

## Anti-Patterns

- Do not build a Response manually with hand-written HTTP headers when
  `HTMLResponse` / `RedirectResponse` / `FileResponse` cover it.
- Do not accept file paths, IDs, or URLs from user input and pass them
  through to the filesystem or a downstream service without going
  through the appropriate dao helper.
- Do not add a page that skips `admin_from_session`. If the page is
  public, prefix its route under `/auth/`, `/healthz`, `/static/`, or
  `/favicon.ico` — those are the sanctioned public paths.
- Do not introduce a JS build step or third-party frontend framework.
  The dashboard is intentionally zero-build server-rendered HTML with
  small inline `<script>` tags for interactivity (row expanders, dialog
  triggers, preset-loader). If the UI needs more, extend the inline
  JS; only propose a build step if the increment is genuinely
  unavoidable.
