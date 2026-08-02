"""Bot event log panel.

Admin-only. Renders the ``bot_events`` table with level and category
filters. Each row is expandable to show the JSON ``context`` blob.
"""

import json
import logging
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fcb.dashboard.ui import admin_from_session, page
from fcb.db import dao

logger = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])

_LEVEL_STYLE = {
    "DEBUG": ("#334155", "#cbd5e1"),
    "INFO": ("#1e3a8a", "#dbeafe"),
    "WARNING": ("#78350f", "#fed7aa"),
    "ERROR": ("#7f1d1d", "#fecaca"),
}


def _level_tag(level: str) -> str:
    bg, fg = _LEVEL_STYLE.get(level, ("#334155", "#e2e8f0"))
    return f'<span class="tag" style="background:{bg};color:{fg};">{escape(level)}</span>'


@router.get("/logs", response_class=HTMLResponse, response_model=None)
async def logs_page(
    request: Request,
    level: str | None = None,
    category: str | None = None,
    actor: str | None = None,
) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)

    rows = dao.list_bot_events(level=level, category=category, actor=actor, limit=300)
    categories = dao.list_bot_event_categories()

    def level_button(want: str | None, label: str) -> str:
        active = level == want or (want is None and level is None)
        cls = "btn" if active else "btn secondary"
        qs_parts = []
        if want:
            qs_parts.append(f"level={want}")
        if category:
            qs_parts.append(f"category={category}")
        if actor:
            qs_parts.append(f"actor={actor}")
        qs = "&".join(qs_parts)
        href = f"/logs?{qs}" if qs else "/logs"
        return f'<a class="{cls}" href="{escape(href)}">{escape(label)}</a>'

    level_filters = " ".join(
        [
            level_button(None, "All"),
            level_button("INFO", "Info"),
            level_button("WARNING", "Warning"),
            level_button("ERROR", "Error"),
        ]
    )

    category_options = ['<option value="">All categories</option>']
    for c in categories:
        selected = " selected" if category == c else ""
        category_options.append(f'<option value="{escape(c)}"{selected}>{escape(c)}</option>')

    if not rows:
        body_rows = (
            '<tr><td colspan="5" class="muted" style="text-align:center;padding:2rem;">'
            "No log rows match this filter."
            "</td></tr>"
        )
    else:
        body_rows = ""
        for r in rows:
            ctx = r.get("context")
            ctx_pretty = ""
            if ctx:
                try:
                    ctx_pretty = json.dumps(json.loads(ctx), indent=2)
                except json.JSONDecodeError:
                    ctx_pretty = ctx
            has_context = bool(ctx_pretty)

            summary_row = f"""
<tr class="log-row" onclick="toggleLogRow(this)"
    style="cursor:pointer;">
  <td class="chev" style="width:1.5rem;color:var(--muted);">{'▸' if has_context else ' '}</td>
  <td class="muted" style="white-space:nowrap;">{escape(r['ts'])}</td>
  <td>{_level_tag(r['level'])}</td>
  <td><code>{escape(r['category'])}</code></td>
  <td>{escape(r['message'])}</td>
</tr>"""
            detail_row = ""
            if has_context:
                detail_row = f"""
<tr class="log-detail" hidden>
  <td colspan="5" style="background:#0b1220;">
    <pre style="margin:0;padding:1rem;background:var(--panel-2);border-radius:6px;
                overflow-x:auto;white-space:pre-wrap;">{escape(ctx_pretty)}</pre>
  </td>
</tr>"""
            body_rows += summary_row + detail_row

    body = f"""
<h1>Logs</h1>
<div class="card">
  <div style="display:flex;gap:0.75rem;flex-wrap:wrap;align-items:center;margin-bottom:1rem;">
    {level_filters}
    <form method="get" action="/logs" style="display:inline-flex;gap:0.5rem;align-items:center;margin-left:auto;">
      <select name="category" onchange="this.form.submit()"
              style="padding:0.5rem;background:var(--panel-2);color:var(--text);
                     border:1px solid var(--border);border-radius:6px;">
        {''.join(category_options)}
      </select>
      {'<input type="hidden" name="level" value="' + escape(level) + '">' if level else ''}
      {'<input type="hidden" name="actor" value="' + escape(actor) + '">' if actor else ''}
    </form>
  </div>
  <table>
    <tr><th></th><th>Time</th><th>Level</th><th>Category</th><th>Message</th></tr>
    {body_rows}
  </table>
</div>
<p class="muted" style="text-align:center;">Showing latest {len(rows)} rows.</p>
<script>
function toggleLogRow(row) {{
  const next = row.nextElementSibling;
  if (!next || !next.classList.contains('log-detail')) return;
  next.hidden = !next.hidden;
  const chev = row.querySelector('.chev');
  if (chev) chev.textContent = next.hidden ? '\\u25B8' : '\\u25BE';
}}
</script>
"""
    return HTMLResponse(page(title="Logs", body=body, admin=admin, active="logs"))
