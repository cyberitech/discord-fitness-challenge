"""Submissions view + approve/reject actions for the FCB dashboard.

Admin-only. Shows every submission the bot has recorded, joined with
cached user identity (``users`` table) and event context. Admins can
flip a submission's status between ``pending``, ``approved``, and
``rejected`` from the detail page.

Screenshot files live under ``data/screenshots/`` and are served via
``/media/submissions/{id}/image`` — the route is admin-gated and reads
the on-disk path from the ``submissions.image_path`` column, so path
traversal from outside is impossible.
"""

import json
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from fcb import config
from fcb.dashboard.ui import admin_from_session, page
from fcb.db import dao

logger = logging.getLogger(__name__)

_PST = ZoneInfo("America/Los_Angeles")


def _format_posted_at_pst(posted_at: str | None) -> str:
    """Format a UTC ISO timestamp as Pacific time for display.

    ``posted_at`` in the DB is stored as ISO 8601 UTC (with or without
    a trailing offset). Handles both forms. Returns an empty string on
    parse failure rather than blowing up the whole row.
    """
    if not posted_at:
        return ""
    try:
        # Python's fromisoformat handles the "+00:00" form directly.
        # Naive strings are treated as UTC.
        dt = datetime.fromisoformat(posted_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        pst = dt.astimezone(_PST)
        return pst.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return posted_at


router = APIRouter(tags=["submissions"])


_STATUS_TAGS = {
    "approved": '<span class="tag active">approved</span>',
    "rejected": '<span class="tag" style="background:#7f1d1d;color:#fecaca;">rejected</span>',
    "pending": '<span class="tag" style="background:#78350f;color:#fed7aa;">pending</span>',
}


def _event_links(events: list[dict[str, Any]], *, show_ids: bool = False) -> str:
    """Render all linked events, preserving the primary distinction."""
    if not events:
        return '<span class="muted">—</span>'
    multiple = len(events) > 1
    links: list[str] = []
    for event in events:
        prefix = f"#{event['id']} — " if show_ids else ""
        primary = (
            ' <span class="muted">(primary)</span>' if (multiple and event["is_primary"]) else ""
        )
        links.append(
            f'<a href="/events/{event["id"]}">'
            f'{escape(prefix + str(event["name"]))}</a>{primary}'
        )
    return "<br>".join(links)


def _default_avatar(discord_user_id: str) -> str:
    """Discord's default avatar for users we haven't cached yet."""
    idx = (int(discord_user_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def _user_cell(row: dict) -> str:
    display = row.get("user_display_name") or row.get("user_username")
    username = row.get("user_username")
    user_id = row["discord_user_id"]
    avatar = row.get("user_avatar_url") or _default_avatar(user_id)

    if display and username:
        label = f'<b>{escape(display)}</b> <span class="muted">(@{escape(username)})</span>'
    elif username:
        label = f"<b>@{escape(username)}</b>"
    else:
        label = f'<span class="muted">id {escape(user_id)}</span>'

    return (
        f'<div style="display:flex;align-items:center;gap:0.75rem;">'
        f'<img class="avatar" src="{escape(avatar)}" alt="">'
        f"<div>{label}</div></div>"
    )


def _images_gallery(submission_id: int) -> str:
    """Render every image linked to a submission as a horizontal gallery.

    Falls back to submissions.image_path for pre-migration rows that
    never got a ``submission_images`` entry with a real path.
    """
    images = dao.get_submission_images(submission_id)
    if not images:
        return ""

    thumbs: list[str] = []
    for img in images:
        # Legacy backfilled rows may have an empty hash; the URL still
        # works because we serve by position.
        img_url = f"/media/submissions/{submission_id}/image?position={img['position']}"
        thumbs.append(f"""
<div style="position:relative;display:inline-block;">
  <img src="{img_url}"
       style="max-height:340px;max-width:100%;border-radius:6px;display:block;">
  <a href="{img_url}" target="_blank" title="Open full size"
     style="position:absolute;top:0.5rem;right:0.5rem;
            background:rgba(0,0,0,0.55);color:white;padding:0.25rem 0.6rem;
            border-radius:4px;text-decoration:none;font-size:0.85rem;">⤢ expand</a>
</div>
""")

    return f"""
<div style="display:flex;flex-wrap:wrap;gap:0.75rem;margin-bottom:1rem;">
  {''.join(thumbs)}
</div>
"""


def _detail_block(row: dict, submission_id: int) -> str:
    """Inline detail block shown when a row is expanded."""
    stats_json = row.get("extracted_stats")
    stats_pretty = ""
    if stats_json:
        try:
            stats_pretty = json.dumps(json.loads(stats_json), indent=2)
        except json.JSONDecodeError:
            stats_pretty = stats_json

    image_block = _images_gallery(submission_id)

    review_meta = ""
    if row.get("reviewed_by"):
        review_meta = (
            f'<p class="muted" style="margin:0.25rem 0 0.75rem;">'
            f'Last reviewed by <code>{escape(row["reviewed_by"])}</code> at '
            f'{escape(row["reviewed_at"])}</p>'
        )

    def action_button(target_status: str, label: str) -> str:
        if row["status"] == target_status:
            return '<span class="btn secondary" style="opacity:0.5;">' f"{escape(label)}</span>"
        return (
            f'<form method="post" action="/submissions/{submission_id}/status" '
            f'style="display:inline;">'
            f'<input type="hidden" name="status" value="{target_status}">'
            f'<button class="btn secondary" type="submit">{escape(label)}</button>'
            f"</form>"
        )

    stats_content = escape(stats_pretty) or "(none)"
    return f"""
<div style="padding:0.5rem 0 0.25rem;">
  {image_block}
  <p class="muted">
    Posted: {escape(_format_posted_at_pst(row.get('posted_at')))}
    &middot; Message ID: <code>{escape(row.get('message_id') or '')}</code>
  </p>
  <h3 style="margin:1rem 0 0.5rem;">Extracted stats</h3>
  <pre style="background:var(--panel-2);padding:1rem;border-radius:6px;
              overflow-x:auto;white-space:pre-wrap;margin:0;">{stats_content}</pre>
  <h3 style="margin:1rem 0 0.5rem;">Admin actions</h3>
  {review_meta}
  <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">
    {action_button("approved", "Mark approved")}
    {action_button("rejected", "Mark rejected")}
    {action_button("pending", "Mark pending")}
  </div>
</div>
"""


def _stats_summary(extracted_stats_json: str | None) -> str:
    if not extracted_stats_json:
        return '<span class="muted">no stats</span>'
    try:
        stats = json.loads(extracted_stats_json)
    except json.JSONDecodeError:
        return '<span class="muted">invalid JSON</span>'
    parts: list[str] = []
    if stats.get("workout_type"):
        parts.append(escape(str(stats["workout_type"])))
    if stats.get("duration_seconds") is not None:
        parts.append(f"{int(round(float(stats['duration_seconds']) / 60))} min")
    if stats.get("distance_miles") is not None:
        parts.append(f"{float(stats['distance_miles']):.2f} mi")
    if stats.get("elevation_gain_feet") is not None:
        parts.append(f"{int(round(float(stats['elevation_gain_feet']))):,} ft")
    if stats.get("calories") is not None:
        parts.append(f"{stats['calories']} cal")
    if stats.get("reps") is not None:
        parts.append(f"{stats['reps']} reps")
    if stats.get("weight_lb") is not None:
        parts.append(f"{stats['weight_lb']} lb")
    return " · ".join(parts) if parts else '<span class="muted">no fields</span>'


@router.get("/submissions", response_class=HTMLResponse, response_model=None)
async def list_submissions_page(
    request: Request,
    status: str | None = None,
    event_id: int | None = None,
    include_non_workout: int = 0,
) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    guild_id = admin["current_guild_id"]
    rows = dao.list_submissions(
        guild_id=guild_id,
        status=status,
        event_id=event_id,
        include_non_workout=bool(include_non_workout),
    )
    events = dao.list_events(guild_id)

    def filter_link(label: str, params: dict[str, str], active: bool) -> str:
        # Preserve the event and non-workout filters across status changes.
        if event_id and "event_id" not in params:
            params = {**params, "event_id": str(event_id)}
        if include_non_workout and "include_non_workout" not in params:
            params = {**params, "include_non_workout": "1"}
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
        href = f"/submissions?{qs}" if qs else "/submissions"
        cls = "btn" if active else "btn secondary"
        return f'<a class="{cls}" href="{escape(href)}">{escape(label)}</a>'

    status_filters = " ".join(
        [
            filter_link("All", {}, status is None and event_id is None),
            filter_link("Approved", {"status": "approved"}, status == "approved"),
            filter_link("Rejected", {"status": "rejected"}, status == "rejected"),
            filter_link("Pending", {"status": "pending"}, status == "pending"),
        ]
    )

    # Non-workout toggle. Preserve current status when flipping the switch.
    toggle_params: dict[str, str] = {}
    if status:
        toggle_params["status"] = status
    if event_id:
        toggle_params["event_id"] = str(event_id)
    if not include_non_workout:
        toggle_params["include_non_workout"] = "1"
        toggle_label = "Show non-workouts"
    else:
        toggle_label = "Hide non-workouts"
    toggle_qs = "&".join(f"{k}={v}" for k, v in toggle_params.items())
    toggle_href = f"/submissions?{toggle_qs}" if toggle_qs else "/submissions"
    toggle_cls = "btn" if include_non_workout else "btn secondary"
    non_workout_toggle = (
        f'<a class="{toggle_cls}" href="{escape(toggle_href)}" '
        f'style="margin-left:0.5rem;">{escape(toggle_label)}</a>'
    )
    status_filters += non_workout_toggle

    event_options = ['<option value="">All events</option>']
    for e in events:
        selected = " selected" if event_id == e["id"] else ""
        event_options.append(
            f'<option value="{e["id"]}"{selected}>#{e["id"]} — {escape(e["name"])}</option>'
        )

    if not rows:
        body_rows = (
            '<tr><td colspan="7" class="muted" style="text-align:center;padding:2rem;">'
            "No submissions match this filter."
            "</td></tr>"
        )
    else:
        body_rows = ""
        for r in rows:
            linked_events = dao.get_submission_events(submission_id=r["id"], guild_id=guild_id)
            event_links = _event_links(linked_events)
            posted_pst = _format_posted_at_pst(r.get("posted_at"))
            body_rows += f"""
<tr class="sub-row" onclick="toggleSubRow(this)" style="cursor:pointer;">
  <td class="chev" style="width:1.5rem;color:var(--muted);">▸</td>
  <td>#{r['id']}</td>
  <td>{_user_cell(r)}</td>
  <td>{event_links}</td>
  <td>{_stats_summary(r['extracted_stats'])}</td>
  <td>{_STATUS_TAGS.get(r['status'], r['status'])}</td>
  <td class="muted" style="white-space:nowrap;font-size:0.85rem;">{escape(posted_pst)}</td>
</tr>
<tr class="sub-detail" hidden>
  <td colspan="7" style="background:#0b1220;">
    {_detail_block(r, r['id'])}
  </td>
</tr>"""

    body = f"""
<h1>Submissions</h1>
<div class="card">
  <div style="display:flex;gap:0.75rem;flex-wrap:wrap;align-items:center;margin-bottom:1rem;">
    {status_filters}
    <form method="get" action="/submissions"
          style="display:inline-flex;gap:0.5rem;align-items:center;margin-left:auto;">
      <select name="event_id" onchange="this.form.submit()"
              style="padding:0.5rem;background:var(--panel-2);color:var(--text);
                     border:1px solid var(--border);border-radius:6px;">
        {''.join(event_options)}
      </select>
      {'<input type="hidden" name="status" value="' + escape(status) + '">' if status else ''}
    </form>
  </div>
  <table>
    <tr><th></th><th></th><th>User</th><th>Attributions</th>
        <th>Stats</th><th>Status</th><th>Posted (PST)</th></tr>
    {body_rows}
  </table>
</div>
<script>
function toggleSubRow(row) {{
  const next = row.nextElementSibling;
  if (!next || !next.classList.contains('sub-detail')) return;
  next.hidden = !next.hidden;
  const chev = row.querySelector('.chev');
  if (chev) chev.textContent = next.hidden ? '\\u25B8' : '\\u25BE';
}}
</script>
"""
    return HTMLResponse(page(title="Submissions", body=body, admin=admin, active="submissions"))


@router.get("/submissions/{submission_id}", response_class=HTMLResponse, response_model=None)
async def submission_detail_page(
    request: Request, submission_id: int
) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    guild_id = admin["current_guild_id"]
    row = dao.get_submission(submission_id, guild_id=guild_id)
    if row is None:
        return HTMLResponse(
            page(
                title="Not found",
                body="<h1>Submission not found</h1><p><a href='/submissions'>Back</a></p>",
                admin=admin,
                active="submissions",
            ),
            status_code=404,
        )

    stats_json = row["extracted_stats"]
    stats_pretty = ""
    if stats_json:
        try:
            stats_pretty = json.dumps(json.loads(stats_json), indent=2)
        except json.JSONDecodeError:
            stats_pretty = stats_json

    review_meta = ""
    if row["reviewed_by"]:
        review_meta = (
            f'<p class="muted">Last reviewed by <code>{escape(row["reviewed_by"])}</code> '
            f'at {escape(row["reviewed_at"])}</p>'
        )

    gallery = _images_gallery(submission_id)
    image_block = f'<div class="card">{gallery}</div>' if gallery else ""

    linked_events = dao.get_submission_events(submission_id=submission_id, guild_id=guild_id)
    event_links = _event_links(linked_events, show_ids=True)

    def action_button(target_status: str, label: str) -> str:
        if row["status"] == target_status:
            return '<span class="btn secondary" style="opacity:0.5;">' f"{escape(label)}</span>"
        return (
            f'<form method="post" action="/submissions/{submission_id}/status" '
            f'style="display:inline;">'
            f'<input type="hidden" name="status" value="{target_status}">'
            f'<button class="btn secondary" type="submit">{escape(label)}</button>'
            f"</form>"
        )

    stats_content = escape(stats_pretty) or '<span class="muted">(none)</span>'
    body = f"""
<h1>Submission #{submission_id}</h1>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;
              gap:1rem;flex-wrap:wrap;">
    <div>{_user_cell(row)}</div>
    <div>{_STATUS_TAGS.get(row['status'], row['status'])}</div>
  </div>
  <p class="muted" style="margin-top:0.75rem;">
    Attributions: {event_links}<br>
    Posted: {escape(_format_posted_at_pst(row.get('posted_at')))} ·
    Message ID: <code>{escape(row.get('message_id') or '')}</code>
  </p>
</div>

{image_block}

<div class="card">
  <h2 style="margin-top:0;">Extracted stats</h2>
  <pre style="background:var(--panel-2);padding:1rem;border-radius:6px;overflow-x:auto;
              white-space:pre-wrap;">{stats_content}</pre>
</div>

<div class="card">
  <h2 style="margin-top:0;">Admin actions</h2>
  {review_meta}
  <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">
    {action_button("approved", "Mark approved")}
    {action_button("rejected", "Mark rejected")}
    {action_button("pending", "Mark pending")}
  </div>
</div>
"""
    rendered = page(
        title=f"Submission #{submission_id}",
        body=body,
        admin=admin,
        active="submissions",
    )
    return HTMLResponse(rendered)


@router.post("/submissions/{submission_id}/status")
async def flip_submission_status(
    request: Request,
    submission_id: int,
    status: str = Form(...),
) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    guild_id = admin["current_guild_id"]
    dao.update_submission_status(
        submission_id,
        guild_id=guild_id,
        status=status,
        reviewed_by=admin["user_id"],
    )
    logger.info(
        f"admin {admin['user_id']} flipped submission #{submission_id} "
        f"in guild {guild_id} to {status!r}"
    )
    dao.record_event(
        level="INFO",
        category="submission.review",
        message=f"admin {admin['global_name']} set submission #{submission_id} to {status}",
        actor=admin["user_id"],
        guild_id=guild_id,
        context={"submission_id": submission_id, "status": status},
    )
    return RedirectResponse(f"/submissions/{submission_id}", status_code=303)


@router.get("/media/submissions/{submission_id}/image")
async def submission_image(request: Request, submission_id: int, position: int = 0) -> FileResponse:
    """Serve one image belonging to a submission.

    ``position`` selects which image within the submission. Defaults to
    0 (the first / primary image). Legacy pre-migration submissions
    only have position 0, populated from the ``submissions.image_path``
    column via the backfill in migration 013.
    """
    admin = admin_from_session(request)
    if admin is None:
        raise HTTPException(status_code=403, detail="Not authenticated")

    guild_id = admin["current_guild_id"]
    row = dao.get_submission(submission_id, guild_id=guild_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    # Prefer submission_images row for the given position.
    images = dao.get_submission_images(submission_id)
    image_path: str | None = None
    for img in images:
        if img["position"] == position:
            image_path = img["image_path"]
            break

    # Fallback to legacy submissions.image_path when position=0 and
    # no submission_images row exists (shouldn't happen post-migration
    # but keep the guard).
    if image_path is None and position == 0:
        image_path = row.get("image_path")

    if not image_path:
        raise HTTPException(status_code=404, detail="No image")

    stored = Path(image_path)
    if stored.is_absolute():
        abs_path = stored
    else:
        abs_path = config.REPO_ROOT / stored

    screenshots_root = config.FCB_SCREENSHOTS_DIR.resolve()
    try:
        abs_path.resolve().relative_to(screenshots_root)
    except ValueError:
        logger.warning(f"submission {submission_id} image_path escapes screenshots dir")
        raise HTTPException(status_code=404, detail="Not found")

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(abs_path)
