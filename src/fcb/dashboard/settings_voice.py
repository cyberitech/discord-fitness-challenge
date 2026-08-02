"""Bot voice settings page.

Editable base persona plus per-mode instruction overlays for acknowledge,
describe, and nag. Preset templates come from ``fcb.agents.voice.PRESETS``
and populate the form via JS on click. Nothing saves until the user
clicks Save changes.
"""

import json
import logging
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fcb.agents import voice
from fcb.dashboard.ui import admin_from_session, page
from fcb.db import dao

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


_FIELDS = [
    ("voice.base_persona", "Base persona",
     "The core personality the bot brings to every reply. Applied first, "
     "then the mode-specific instruction below layers on top."),
    ("voice.acknowledge_instruction", "Acknowledge workout",
     "Instruction added when replying to a user's workout submission."),
    ("voice.describe_instruction", "Describe (public)",
     "Instruction added when a user runs the public Describe context command."),
    ("voice.nag_instruction", "Nag slacker",
     "Instruction added when nudging a user who hasn't posted in a while."),
    ("voice.announce_start_instruction", "Announce challenge start",
     "Instruction added when the bot posts the opening announcement for a new challenge."),
    ("voice.announce_end_instruction", "Announce challenge end",
     "Instruction added when the bot posts the closing announcement with the winner."),
    ("voice.status_instruction", "/status commentary",
     "Instruction added when a user runs /status. Voice-flavored intro; "
     "the factual breakdown is rendered as a footer below."),
    ("voice.list_instruction", "/list commentary",
     "Instruction added when a user runs /list. Voice-flavored intro; "
     "the per-challenge lines are rendered as a footer below."),
    ("voice.chat_instruction", "@-mention chat reply",
     "Instruction added when a user @-mentions the bot with a question. "
     "The bot answers using their progress and the active challenges as "
     "context. Off-topic questions get a short redirect."),
]


@router.get("/settings/voice", response_class=HTMLResponse, response_model=None)
async def voice_settings_page(request: Request) -> HTMLResponse | RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    guild_id = admin.get("current_guild_id")
    if not guild_id:
        return RedirectResponse("/", status_code=307)

    current = voice.get_voice_settings(guild_id)
    stored = dao.get_guild_settings(guild_id, prefix="voice.")

    fields_html = ""
    for key, label, help_text in _FIELDS:
        value = current[key]
        is_overridden = key in stored
        default_note = (
            '<span class="tag active" style="margin-left:0.5rem;">customized</span>'
            if is_overridden
            else '<span class="tag" style="margin-left:0.5rem;">using default</span>'
        )
        fields_html += f"""
<label>{escape(label)} {default_note}</label>
<p class="muted" style="margin:0.25rem 0 0.4rem;font-size:0.85rem;">{escape(help_text)}</p>
<textarea name="{escape(key)}" style="min-height:180px;">{escape(value)}</textarea>
"""

    # Presets exported to JS so clicking a button rewrites the textareas.
    # Preset dicts don't carry announce_* keys yet, so fall back to the built-in
    # defaults for those fields when a preset is applied.
    presets_data = {
        key: {"label": p["label"], **{field: p.get(field, current[field]) for field, _, _ in _FIELDS}}
        for key, p in voice.PRESETS.items()
    }
    presets_json = json.dumps(presets_data)

    # Use single-quoted onclick attribute so json.dumps's double-quoted string
    # doesn't break the HTML.
    preset_buttons = "".join(
        f'<button type="button" class="btn secondary" '
        f"onclick='loadPreset({json.dumps(pk)})'>"
        f"{escape(p['label'])}</button> "
        for pk, p in voice.PRESETS.items()
    )

    body = f"""
<h1>Bot voice</h1>
<div class="card">
  <p class="muted" style="margin-top:0;">
    Presets fill the fields below when clicked. Nothing saves until you press
    <b>Save changes</b>. Leave a field blank to fall back to the built-in default.
  </p>
  <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">{preset_buttons}</div>
</div>

<form method="post" action="/settings/voice" id="voice-form">
  <div class="card">
    {fields_html}
    <div class="form-actions">
      <button type="submit" class="btn">Save changes</button>
      <button type="button" class="btn secondary"
              onclick="document.getElementById('reset-voice').showModal()">Reset all to defaults</button>
    </div>
  </div>
</form>

<dialog id="reset-voice">
  <h3>Reset bot voice to defaults?</h3>
  <p>All voice settings will be cleared. The bot will use the built-in defaults from code.</p>
  <div class="dialog-actions">
    <button type="button" class="btn secondary"
            onclick="this.closest('dialog').close()">Cancel</button>
    <form method="post" action="/settings/voice/reset" style="display:inline;">
      <button type="submit" class="btn danger">Reset to defaults</button>
    </form>
  </div>
</dialog>

<script>
const VOICE_PRESETS = {presets_json};
const VOICE_FIELDS = {json.dumps([field for field, _, _ in _FIELDS])};
function loadPreset(key) {{
  const p = VOICE_PRESETS[key];
  if (!p) return;
  for (const field of VOICE_FIELDS) {{
    const el = document.querySelector(`textarea[name="${{field}}"]`);
    if (el && p[field] !== undefined) el.value = p[field];
  }}
}}
</script>
"""
    return HTMLResponse(page(title="Bot voice", body=body, admin=admin, active="voice"))


@router.post("/settings/voice")
async def save_voice_settings(request: Request) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    guild_id = admin.get("current_guild_id")
    if not guild_id:
        return RedirectResponse("/", status_code=307)

    form = await request.form()
    updates: dict[str, str] = {}
    for key, _, _ in _FIELDS:
        value = str(form.get(key, "")).strip()
        if value:
            updates[key] = value

    # Clear keys the user emptied.
    stored = dao.get_guild_settings(guild_id, prefix="voice.")
    cleared: list[str] = []
    for key in stored:
        if key not in updates:
            cleared.append(key)

    if updates:
        dao.upsert_guild_settings(guild_id, updates, updated_by=admin["user_id"])

    if cleared:
        conn = dao.connect()
        try:
            conn.executemany(
                "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?",
                [(guild_id, k) for k in cleared],
            )
            conn.commit()
        finally:
            conn.close()

    logger.info(
        f"admin {admin['user_id']} updated voice settings "
        f"(saved={list(updates)}, cleared={cleared})"
    )
    dao.record_event(
        level="INFO",
        category="settings.voice.updated",
        message=f"admin {admin['global_name']} updated bot voice settings",
        actor=admin["user_id"],
        context={"saved": list(updates), "cleared": cleared},
    )
    return RedirectResponse("/settings/voice", status_code=303)


@router.post("/settings/voice/reset")
async def reset_voice_settings(request: Request) -> RedirectResponse:
    admin = admin_from_session(request)
    if admin is None:
        return RedirectResponse("/", status_code=307)
    guild_id = admin.get("current_guild_id")
    if not guild_id:
        return RedirectResponse("/", status_code=307)

    conn = dao.connect()
    try:
        conn.execute(
            "DELETE FROM guild_settings WHERE guild_id = ? AND key LIKE 'voice.%'",
            (guild_id,),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"admin {admin['user_id']} reset voice settings to defaults")
    dao.record_event(
        level="WARNING",
        category="settings.voice.reset",
        message=f"admin {admin['global_name']} reset bot voice to defaults",
        actor=admin["user_id"],
    )
    return RedirectResponse("/settings/voice", status_code=303)
