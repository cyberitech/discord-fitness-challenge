"""Admin allowlist management routes.

Admin-only. Lists the current allowlist, lets an admin add a Discord user
by ID (with autocomplete against ``users``), and remove existing admins
with a confirm modal. The DAO layer refuses to remove the last remaining
admin, so the UI can't accidentally lock everyone out.
"""

import logging
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fcb.dashboard.ui import admin_from_session, page
from fcb.db import dao

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admins"])


def _default_avatar(discord_user_id: str) -> str:
    idx = (int(discord_user_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def _admin_row(row: dict, total_admins: int, current_user_id: str) -> tuple[str, str]:
    """Return (table_row_html, remove_dialog_html)."""
    user_id = row["discord_user_id"]
    display = row.get("user_display_name") or row.get("user_username")
    username = row.get("user_username")
    avatar = row.get("user_avatar_url") or _default_avatar(user_id)

    if display and username:
        label = f'<b>{escape(display)}</b> <span class="muted">(@{escape(username)})</span>'
    elif username:
        label = f'<b>@{escape(username)}</b>'
    else:
        label = f'<span class="muted">id {escape(user_id)}</span>'

    user_cell = (
        f'<div style="display:flex;align-items:center;gap:0.75rem;">'
        f'<img class="avatar" src="{escape(avatar)}" alt="">'
        f'<div>{label}</div></div>'
    )

    # Never allow removing the last admin, and never let a user remove themselves.
    can_remove = total_admins > 1 and user_id != current_user_id
    if can_remove:
        remove_button = (
            f'<button type="button" class="btn danger" '
            f'onclick="document.getElementById(\'remove-{escape(user_id)}\').showModal()">'
            f'Remove</button>'
        )
    else:
        reason = "last admin" if total_admins <= 1 else "you"
        remove_button = f'<span class="btn secondary" style="opacity:0.5;">Remove ({reason})</span>'

    note_html = (
        f'<span class="muted">{escape(row["note"])}</span>' if row.get("note") else ""
    )

    table_row = f"""
<tr>
  <td>{user_cell}</td>
  <td class="muted">{escape(row['added_by'])}</td>
  <td class="muted">{escape(row['added_at'])}</td>
  <td>{note_html}</td>
  <td style="text-align:right;">{remove_button}</td>
</tr>
"""

    dialog_html = ""
    if can_remove:
        dialog_html = f"""
<dialog id="remove-{escape(user_id)}">
  <h3>Remove admin?</h3>
  <p>Revoke dashboard access for <b>{escape(display or username or user_id)}</b>?</p>
  <p class="muted">Their submissions and history are untouched. They can be re-added at any time.</p>
  <div class="dialog-actions">
    <button type="button" class="btn secondary"
            onclick="this.closest('dialog').close()">Cancel</button>
    <form method="post" action="/admins/{escape(user_id)}/remove" style="display:inline;">
      <button type="submit" class="btn danger">Remove admin</button>
    </form>
  </div>
</dialog>
"""
    return table_row, dialog_html


@router.get("/admins", response_class=HTMLResponse, response_model=None)
async def admins_page(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    admins = dao.list_admins()
    users = dao.list_users(limit=200)
    admin_ids = {a["discord_user_id"] for a in admins}
    candidate_users = [u for u in users if u["discord_user_id"] not in admin_ids]

    table_rows: list[str] = []
    dialogs: list[str] = []
    for r in admins:
        row_html, dialog_html = _admin_row(r, len(admins), admin["user_id"])
        table_rows.append(row_html)
        if dialog_html:
            dialogs.append(dialog_html)

    datalist = "".join(
        f'<option value="{escape(u["discord_user_id"])}">'
        f'{escape(u.get("display_name") or u["username"])} '
        f'(@{escape(u["username"])})</option>'
        for u in candidate_users
    )

    body = f"""
<h1>Admins</h1>
<div class="card">
  <p class="muted" style="margin-top:0;">
    Admins can access the dashboard, edit events, review submissions, and manage this list.
  </p>
  <table>
    <tr><th>User</th><th>Added by</th><th>Added at</th><th>Note</th><th></th></tr>
    {''.join(table_rows)}
  </table>
</div>

<div class="card">
  <h2 style="margin-top:0;">Add admin</h2>
  <form method="post" action="/admins/add">
    <label>Discord user ID</label>
    <input list="known-users" type="text" name="discord_user_id" required
           pattern="[0-9]+" placeholder="e.g. 1271241319699845276">
    <datalist id="known-users">{datalist}</datalist>

    <label>Note (optional)</label>
    <input type="text" name="note" placeholder="e.g. co-organizer" maxlength="200">

    <div class="form-actions">
      <button type="submit" class="btn">Add admin</button>
    </div>
  </form>
  <p class="muted" style="margin-top:1rem;font-size:0.85rem;">
    Tip: enable Developer Mode in Discord (Settings → Advanced) then right-click a user
    and Copy User ID. Users the bot has already seen also appear as autocomplete suggestions.
  </p>
</div>

{''.join(dialogs)}
"""
    return HTMLResponse(page(title="Admins", body=body, admin=admin, active="admins"))


@router.post("/admins/add")
async def add_admin_route(
    request: Request,
    discord_user_id: str = Form(...),
    note: str | None = Form(default=None),
) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    target_id = discord_user_id.strip()
    if not target_id.isdigit():
        logger.warning(f"admin {admin['user_id']} submitted non-numeric id {target_id!r}")
        return RedirectResponse("/admins", status_code=303)

    added = dao.add_admin(
        discord_user_id=target_id,
        added_by=admin["user_id"],
        note=(note or "").strip() or None,
    )
    logger.info(
        f"admin {admin['user_id']} added={added} target={target_id}"
    )
    dao.record_event(
        level="INFO" if added else "WARNING",
        category="admin.added" if added else "admin.add_noop",
        message=(
            f"admin {admin['global_name']} "
            f"{'added' if added else 'attempted to add already-present admin'} {target_id}"
        ),
        actor=admin["user_id"],
        context={"target_id": target_id, "added": added},
    )
    return RedirectResponse("/admins", status_code=303)


@router.post("/admins/{target_id}/remove")
async def remove_admin_route(request: Request, target_id: str) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    if target_id == admin["user_id"]:
        logger.warning(f"admin {admin['user_id']} tried to remove themselves")
        return RedirectResponse("/admins", status_code=303)

    dao.remove_admin(target_id)
    logger.info(f"admin {admin['user_id']} removed admin {target_id}")
    dao.record_event(
        level="WARNING",
        category="admin.removed",
        message=f"admin {admin['global_name']} removed admin {target_id}",
        actor=admin["user_id"],
        context={"target_id": target_id},
    )
    return RedirectResponse("/admins", status_code=303)
