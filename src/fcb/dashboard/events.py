"""Event management routes for the FCB dashboard.

Admin-only. Lists, creates, and edits rows in the ``events`` table. The
editable prompt is the primary field — it's what the vision agent
consumes each time an image lands (see ``.kiro/steering/bedrock.md``).

Dates are exchanged with the browser as ``YYYY-MM-DDTHH:MM`` (HTML
``datetime-local`` format). SQLite's ``datetime()`` normalization
already handles this alongside the older ``2026-07-01T00:00:00Z`` seed
row, so we don't need a migration.
"""

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from fcb import config, runtime_config
from fcb.dashboard.ui import admin_from_session, page
from fcb.db import dao

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_KINDS = [
    ("daily_1pct", "Daily 1% Better"),
    ("challenge", "Challenge (winners)"),
]

_METRICS = [
    ("submission_count", "Workouts logged (count)"),
    ("duration_seconds", "Total time"),
    ("distance_miles", "Total distance (mi)"),
    ("elevation_gain_feet", "Total elevation gain (ft)"),
    ("calories", "Total calories"),
    ("reps", "Total reps"),
    ("volume_lb", "Total volume (weight x reps x sets, lb-reps)"),
]

_PROMPT_PLACEHOLDER = (
    "Track total elevation gained this month via stairs, treadmill, or stairmaster. "
    "Extract the elevation gained in feet from each screenshot. "
    "Also capture duration and distance if visible. Ignore other stats unless they "
    "help disambiguate the workout type."
)


def _unlink_screenshots(image_paths: list[str]) -> int:
    """Best-effort disk cleanup. Returns count actually removed."""
    removed = 0
    for rel in image_paths:
        if not rel:
            continue
        path = Path(rel)
        if not path.is_absolute():
            path = config.REPO_ROOT / path
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            logger.warning(f"screenshot already gone: {path}")
    return removed


def _confirm_dialog(
    *,
    dialog_id: str,
    title: str,
    body: str,
    confirm_label: str,
    form_action: str,
    danger: bool = False,
) -> str:
    button_class = "btn danger" if danger else "btn"
    return f"""
<dialog id="{escape(dialog_id)}">
  <h3>{escape(title)}</h3>
  {body}
  <div class="dialog-actions">
    <button type="button" class="btn secondary"
            onclick="this.closest('dialog').close()">Cancel</button>
    <form method="post" action="{escape(form_action)}" style="display:inline;">
      <button type="submit" class="{button_class}">{escape(confirm_label)}</button>
    </form>
  </div>
</dialog>
"""


def _to_date(iso: str) -> str:
    """Turn a stored ISO timestamp into HTML date-input format (YYYY-MM-DD)."""
    return iso[:10] if len(iso) >= 10 else iso


def _from_date(date_str: str, *, end_of_day: bool = False) -> str:
    """Normalize a YYYY-MM-DD form value into a full ISO timestamp.

    Start dates land at 00:00:00; end dates land at 23:59:59 so an event
    ending on the 31st includes the whole 31st.
    """
    if len(date_str) == 10:
        return f"{date_str}T{'23:59:59' if end_of_day else '00:00:00'}"
    return date_str


def _is_active(row: dict) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return row["starts_at"] <= now <= row["ends_at"]


def _kind_options(selected: str) -> str:
    return "".join(
        f'<option value="{escape(key)}"{ " selected" if key == selected else ""}>{escape(label)}</option>'
        for key, label in _KINDS
    )


def _metric_options(selected: str) -> str:
    return "".join(
        f'<option value="{escape(key)}"{ " selected" if key == selected else ""}>{escape(label)}</option>'
        for key, label in _METRICS
    )


def _event_form(row: dict | None, action: str, submit_label: str) -> str:
    kind_val = row["kind"] if row else "challenge"
    metric_val = row.get("primary_metric", "submission_count") if row else "submission_count"
    name_val = escape(row["name"]) if row else ""
    prompt_val = escape(row["prompt"]) if row else ""
    starts_at = _to_date(row["starts_at"]) if row else ""
    ends_at = _to_date(row["ends_at"]) if row else ""

    return f"""
<form method="post" action="{escape(action)}">
  <label>Kind</label>
  <select name="kind">{_kind_options(kind_val)}</select>

  <label>Name</label>
  <input type="text" name="name" value="{name_val}" required maxlength="200"
         placeholder="e.g. July 2026 Elevation Challenge">

  <label>Starts</label>
  <input type="date" name="starts_at" value="{starts_at}" required>

  <label>Ends</label>
  <input type="date" name="ends_at" value="{ends_at}" required>

  <label>Leaderboard metric — what the /leaderboard command ranks by</label>
  <select name="primary_metric">{_metric_options(metric_val)}</select>

  <label>Nag threshold (days) — bot nags participants who haven't posted in this many days. Set to 0 to disable nagging for this event.</label>
  <input type="number" name="nag_threshold_days" min="0" max="365"
         value="{row.get('nag_threshold_days', 3) if row else 3}">

  <label>Prompt — what the vision agent should extract for this event</label>
  <textarea name="prompt" required placeholder="{escape(_PROMPT_PLACEHOLDER)}">{prompt_val}</textarea>

  <div class="form-actions">
    <button class="btn" type="submit">{escape(submit_label)}</button>
    <a class="btn secondary" href="/events">Cancel</a>
  </div>
</form>
"""


@router.get("/events", response_class=HTMLResponse, response_model=None)
async def list_events_page(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    if not admin.get("current_guild_id"):
        return RedirectResponse("/", status_code=307)

    rows = dao.list_events(admin["current_guild_id"])

    if not rows:
        body = """
<h1>Events</h1>
<div class="card">
  <p class="muted">No events yet.</p>
  <a class="btn" href="/events/new">Create the first event</a>
</div>
"""
        return HTMLResponse(page(title="Events", body=body, admin=admin, active="events"))

    table_rows: list[str] = []
    dialogs: list[str] = []
    for row in rows:
        active = _is_active(row)
        active_tag = '<span class="tag active">ACTIVE</span>' if active else ""
        kind_tag = f'<span class="tag kind-{escape(row["kind"])}">{escape(row["kind"])}</span>'
        prompt_preview = escape(row["prompt"][:120] + ("…" if len(row["prompt"]) > 120 else ""))
        eid = row["id"]

        table_rows.append(f"""
<tr>
  <td>#{eid}</td>
  <td>{kind_tag} {active_tag}</td>
  <td><b>{escape(row['name'])}</b><br><span class="muted">{prompt_preview}</span></td>
  <td class="muted">{escape(row['starts_at'])}<br>→ {escape(row['ends_at'])}</td>
  <td style="white-space:nowrap;">
    <a class="btn secondary" href="/events/{eid}">Edit</a>
    <button type="button" class="btn secondary"
            onclick="document.getElementById('reset-{eid}').showModal()">Reset</button>
    <button type="button" class="btn danger"
            onclick="document.getElementById('delete-{eid}').showModal()">Delete</button>
  </td>
</tr>""")

        dialogs.append(
            _confirm_dialog(
                dialog_id=f"reset-{eid}",
                title=f"Reset event #{eid}?",
                body=(
                    f'<p>All submissions, metrics, and participants for '
                    f'<b>{escape(row["name"])}</b> will be permanently deleted, '
                    f'along with their screenshot files.</p>'
                    f'<p class="muted">The event itself (name, prompt, dates) stays intact.</p>'
                ),
                confirm_label="Reset event",
                form_action=f"/events/{eid}/reset",
                danger=True,
            )
        )
        dialogs.append(
            _confirm_dialog(
                dialog_id=f"delete-{eid}",
                title=f"Delete event #{eid}?",
                body=(
                    f'<p>This permanently deletes <b>{escape(row["name"])}</b> and '
                    f'every submission, metric, participant, and screenshot file '
                    f'associated with it.</p>'
                    f'<p class="muted">This cannot be undone.</p>'
                ),
                confirm_label="Delete event",
                form_action=f"/events/{eid}/delete",
                danger=True,
            )
        )

    body = f"""
<h1>Events</h1>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
    <span class="muted">{len(rows)} total</span>
    <a class="btn" href="/events/new">New event</a>
  </div>
  <table>
    <tr><th></th><th>Kind</th><th>Name / Prompt</th><th>Window</th><th></th></tr>
    {''.join(table_rows)}
  </table>
</div>
{''.join(dialogs)}
"""
    return HTMLResponse(page(title="Events", body=body, admin=admin, active="events"))


@router.get("/events/new", response_class=HTMLResponse, response_model=None)
async def new_event_page(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    body = f"""
<h1>New event</h1>
<div class="card">
  {_event_form(None, "/events/new", "Create event")}
</div>
"""
    return HTMLResponse(page(title="New event", body=body, admin=admin, active="events"))


@router.post("/events/new")
async def create_event(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    prompt: str = Form(...),
    primary_metric: str = Form("submission_count"),
    nag_threshold_days: int = Form(3),
) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    guild_id = admin["current_guild_id"]
    channel_id = runtime_config.channel_id(guild_id) or ""

    event_id = dao.create_event(
        guild_id=guild_id,
        channel_id=channel_id,
        kind=kind,
        name=name.strip(),
        prompt=prompt.strip(),
        starts_at=_from_date(starts_at),
        ends_at=_from_date(ends_at, end_of_day=True),
        created_by=admin["user_id"],
        primary_metric=primary_metric,
        nag_threshold_days=nag_threshold_days,
    )
    logger.info(f"admin {admin['user_id']} created event #{event_id}: {name!r}")
    dao.record_event(
        level="INFO",
        category="event.created",
        message=f"admin {admin['global_name']} created event #{event_id}: {name!r}",
        actor=admin["user_id"],
        context={"event_id": event_id, "kind": kind},
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.get("/events/{event_id}", response_class=HTMLResponse, response_model=None)
async def edit_event_page(
    request: Request, event_id: int
) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    row = dao.get_event(event_id)
    if row is None:
        return HTMLResponse(
            page(
                title="Not found",
                body="<h1>Event not found</h1><p><a href='/events'>Back to events</a></p>",
                admin=admin,
                active="events",
            ),
            status_code=404,
        )

    active_note = ""
    if _is_active(row):
        active_note = (
            '<div class="card" style="border: 1px solid #22c55e;">'
            '<b>This event is currently active.</b> '
            '<span class="muted">The vision agent uses this prompt right now.</span>'
            "</div>"
        )

    reset_dialog = _confirm_dialog(
        dialog_id=f"reset-{event_id}",
        title=f"Reset event #{event_id}?",
        body=(
            f'<p>All submissions, metrics, and participants for '
            f'<b>{escape(row["name"])}</b> will be permanently deleted, '
            f'along with their screenshot files.</p>'
            f'<p class="muted">The event itself (name, prompt, dates) stays intact.</p>'
        ),
        confirm_label="Reset event",
        form_action=f"/events/{event_id}/reset",
        danger=True,
    )
    delete_dialog = _confirm_dialog(
        dialog_id=f"delete-{event_id}",
        title=f"Delete event #{event_id}?",
        body=(
            f'<p>This permanently deletes <b>{escape(row["name"])}</b> and '
            f'every submission, metric, participant, and screenshot file '
            f'associated with it.</p>'
            f'<p class="muted">This cannot be undone.</p>'
        ),
        confirm_label="Delete event",
        form_action=f"/events/{event_id}/delete",
        danger=True,
    )

    body = f"""
<h1>Edit event #{event_id}</h1>
{active_note}
<div class="card">
  {_event_form(row, f"/events/{event_id}", "Save changes")}
</div>
<div class="card muted">
  <b>Created by:</b> {escape(row['created_by'])} · <b>Created at:</b> {escape(row['created_at'])}
</div>
<div class="card">
  <h2 style="margin-top:0;">Event tools</h2>
  <div style="display:flex;gap:0.6rem;flex-wrap:wrap;">
    <a class="btn secondary" href="/events/{event_id}/participants">View participants</a>
    <a class="btn secondary" href="/submissions?event_id={event_id}">All submissions</a>
    <a class="btn secondary" href="/events/{event_id}/export.csv">Export CSV</a>
  </div>
</div>
<div class="card" style="border:1px solid var(--danger);">
  <h2 style="margin-top:0;color:var(--danger);">Danger zone</h2>
  <p class="muted">Reset clears the event's data but keeps its definition. Delete removes everything.</p>
  <div style="display:flex;gap:0.6rem;flex-wrap:wrap;">
    <button type="button" class="btn secondary"
            onclick="document.getElementById('reset-{event_id}').showModal()">Reset event</button>
    <button type="button" class="btn danger"
            onclick="document.getElementById('delete-{event_id}').showModal()">Delete event</button>
  </div>
</div>
{reset_dialog}
{delete_dialog}
"""
    return HTMLResponse(page(title=f"Event #{event_id}", body=body, admin=admin, active="events"))


@router.post("/events/{event_id}")
async def update_event(
    request: Request,
    event_id: int,
    kind: str = Form(...),
    name: str = Form(...),
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    prompt: str = Form(...),
    primary_metric: str = Form("submission_count"),
    nag_threshold_days: int = Form(3),
) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    dao.update_event(
        event_id,
        kind=kind,
        name=name.strip(),
        prompt=prompt.strip(),
        starts_at=_from_date(starts_at),
        ends_at=_from_date(ends_at, end_of_day=True),
        primary_metric=primary_metric,
        nag_threshold_days=nag_threshold_days,
    )
    logger.info(f"admin {admin['user_id']} updated event #{event_id}")
    dao.record_event(
        level="INFO",
        category="event.updated",
        message=f"admin {admin['global_name']} updated event #{event_id}",
        actor=admin["user_id"],
        context={"event_id": event_id, "kind": kind},
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/events/{event_id}/reset")
async def reset_event_route(request: Request, event_id: int) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    result = dao.reset_event(event_id)
    files_removed = _unlink_screenshots(result.pop("image_paths", []))
    result["files_removed"] = files_removed

    logger.info(f"admin {admin['user_id']} reset event #{event_id}: {result}")
    dao.record_event(
        level="WARNING",
        category="event.reset",
        message=(
            f"admin {admin['global_name']} reset event #{event_id} "
            f"(subs={result['submissions_deleted']}, files={files_removed})"
        ),
        actor=admin["user_id"],
        context={"event_id": event_id, **result},
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.post("/events/{event_id}/delete")
async def delete_event_route(request: Request, event_id: int) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    result = dao.delete_event(event_id)
    files_removed = _unlink_screenshots(result.pop("image_paths", []))
    result["files_removed"] = files_removed

    logger.info(f"admin {admin['user_id']} deleted event #{event_id}: {result}")
    dao.record_event(
        level="WARNING",
        category="event.deleted",
        message=(
            f"admin {admin['global_name']} deleted event #{event_id} "
            f"(subs={result['submissions_deleted']}, files={files_removed})"
        ),
        actor=admin["user_id"],
        context={"event_id": event_id, **result},
    )
    return RedirectResponse("/events", status_code=303)



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


@router.get(
    "/events/{event_id}/participants",
    response_class=HTMLResponse,
    response_model=None,
)
async def participants_page(
    request: Request, event_id: int
) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    event = dao.get_event(event_id)
    if event is None:
        return HTMLResponse(
            page(
                title="Not found",
                body="<h1>Event not found</h1><p><a href='/events'>Back</a></p>",
                admin=admin,
                active="events",
            ),
            status_code=404,
        )
    participants = dao.list_event_participants(event_id)

    if not participants:
        table_body = (
            '<tr><td colspan="5" class="muted" style="text-align:center;padding:2rem;">'
            "No participants yet."
            "</td></tr>"
        )
    else:
        rows_html: list[str] = []
        for p in participants:
            display = (
                p.get("user_display_name")
                or p.get("user_username")
                or p["discord_user_id"]
            )
            username = p.get("user_username")
            avatar = p.get("user_avatar_url") or _default_avatar(p["discord_user_id"])
            if username:
                label = f'<b>{escape(display)}</b> <span class="muted">(@{escape(username)})</span>'
            else:
                label = f'<b>{escape(display)}</b>'
            rows_html.append(f"""
<tr>
  <td>
    <div style="display:flex;align-items:center;gap:0.75rem;">
      <img class="avatar" src="{escape(avatar)}" alt="">
      <div>{label}</div>
    </div>
  </td>
  <td>{p['submission_count']}</td>
  <td class="muted">{_humanize_ago(p['last_submission_at'])}</td>
  <td class="muted">{escape(p['joined_at'])}</td>
  <td><a class="btn secondary" href="/submissions?event_id={event_id}">View submissions</a></td>
</tr>""")
        table_body = "".join(rows_html)

    body = f"""
<h1>Participants — {escape(event['name'])}</h1>
<p class="muted">
  <a href="/events/{event_id}">← Back to event</a> ·
  {len(participants)} participant{'s' if len(participants) != 1 else ''}
</p>
<div class="card">
  <table>
    <tr><th>User</th><th>Workouts</th><th>Last activity</th><th>Joined</th><th></th></tr>
    {table_body}
  </table>
</div>
"""
    return HTMLResponse(
        page(title=f"Participants #{event_id}", body=body, admin=admin, active="events")
    )


_CSV_STAT_FIELDS = (
    "workout_type",
    "duration_seconds",
    "distance_miles",
    "elevation_gain_feet",
    "calories",
    "heart_rate_avg_bpm",
    "reps",
    "weight_lb",
    "sets",
    "confidence",
    "notes",
    "is_workout_screenshot",
)


@router.get("/events/{event_id}/export.csv", response_model=None)
async def export_event_csv(request: Request, event_id: int) -> Response:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    event = dao.get_event(event_id)
    if event is None:
        return Response("Event not found", status_code=404)

    rows = dao.list_submissions_for_export(event_id)
    buf = io.StringIO()
    fieldnames = [
        "submission_id",
        "event_id",
        "event_name",
        "is_primary_event",
        "status",
        "discord_user_id",
        "username",
        "display_name",
        "posted_at",
        "reviewed_by",
        "reviewed_at",
        "message_id",
        "channel_id",
        "image_path",
        "raw_text",
        *_CSV_STAT_FIELDS,
        "extras",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        try:
            stats = json.loads(r.get("extracted_stats") or "{}")
        except json.JSONDecodeError:
            stats = {}
        row_out = {
            "submission_id": r["id"],
            "event_id": event_id,
            "event_name": r.get("event_name") or "",
            "is_primary_event": r.get("is_primary_event") or 0,
            "status": r["status"],
            "discord_user_id": r["discord_user_id"],
            "username": r.get("user_username") or "",
            "display_name": r.get("user_display_name") or "",
            "posted_at": r["posted_at"],
            "reviewed_by": r.get("reviewed_by") or "",
            "reviewed_at": r.get("reviewed_at") or "",
            "message_id": r["message_id"],
            "channel_id": r["channel_id"],
            "image_path": r.get("image_path") or "",
            "raw_text": r.get("raw_text") or "",
        }
        for f in _CSV_STAT_FIELDS:
            v = stats.get(f)
            row_out[f] = "" if v is None else v
        extras = stats.get("extras") or {}
        row_out["extras"] = json.dumps(extras) if extras else ""
        writer.writerow(row_out)

    logger.info(
        f"admin {admin['user_id']} exported event #{event_id} CSV — {len(rows)} rows"
    )
    dao.record_event(
        level="INFO",
        category="event.exported",
        message=f"admin {admin['global_name']} exported event #{event_id} CSV",
        actor=admin["user_id"],
        context={"event_id": event_id, "row_count": len(rows)},
    )

    safe_name = re.sub(r"[^\w.\-]", "_", event["name"])[:80] or f"event-{event_id}"
    filename = f"{safe_name}-submissions.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
