---
inclusion: always
---

# Settings

FCB keeps two kinds of configuration:

1. **`.env` on the host** — secrets and startup-critical values that
   are read once at process import.
2. **`settings` SQLite table** — runtime-tunable values that an admin
   can edit from `/settings` and `/settings/voice` in the dashboard.

The `runtime_config.py` module is the bridge: every hot-editable value
in code goes through it and it reads the DB first, falling back to the
`.env` default when the setting is unset. This lets the dashboard edit
values without a service restart for most of them.

Cross-references: `deployment.md` for what stays in `.env` on the host;
`dashboard.md` for how the settings UI is built; `discord.md` for the
Discord-specific keys.

## The `settings` Table

```
key         TEXT PRIMARY KEY
value       TEXT NOT NULL
updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
updated_by  TEXT           -- discord user id, or 'system' for migrations
```

Everything is TEXT. Numeric and boolean values are stringified on
write and parsed by the `runtime_config` helpers on read. Lists are
JSON-encoded (e.g. `discord.announce_role_ids` = `'["12345", "67890"]'`).

## Key Prefixes

Prefixes namespace settings so `dao.get_settings(prefix=...)` scans a
coherent slice.

| Prefix         | Purpose                                       |
|----------------|-----------------------------------------------|
| `voice.*`      | Persona and per-mode overlays for the voice   |
|                | agent (base_persona, acknowledge_instruction, |
|                | describe_instruction, nag_instruction,        |
|                | announce_upcoming_instruction,                |
|                | announce_start_instruction,                   |
|                | announce_end_instruction,                     |
|                | remind_ending_instruction,                    |
|                | hardcore_riff_instruction,                    |
|                | status_instruction, list_instruction,         |
|                | chat_instruction).                            |
| `discord.*`    | Discord-facing values that used to live in    |
|                | `.env`: channel_id, announce_role_ids.        |
| `bot.*`        | Bot behavior knobs: nag_enabled,              |
|                | nag_interval_hours, nag_cooldown_hours,       |
|                | lifecycle_interval_minutes,                   |
|                | lifecycle_grace_hours, upcoming_lead_hours,   |
|                | reminders_enabled,                            |
|                | reply_only_on_challenge_match,                |
|                | command_cooldown_seconds,                     |
|                | chat_history_messages.                        |

New settings SHOULD reuse an existing prefix; introduce a new prefix
only when the new setting is unambiguously outside the existing groups.

## `runtime_config.py`

Located at `src/fcb/runtime_config.py`. Small module of typed accessors
with env-var fallback. Every function reads from the `settings` table
on every call; SQLite reads are local and cheap.

Canonical accessor pattern:

```python
def channel_id() -> str:
    return dao.get_setting("discord.channel_id") or config.DISCORD_CHANNEL_ID
```

Numeric / boolean / list accessors delegate to `_int_or`, `_float_or`,
`_bool_or` internal helpers with logged fallback on parse failure.

Bot and dashboard code MUST call `runtime_config.*()` (not
`config.DISCORD_CHANNEL_ID` directly) whenever the value is intended to
be admin-editable. `config.py` values remain valid seeds; they define
the fallback default for a fresh install.

## Hot vs Restart-Required

A setting is "hot" if updating the `settings` row takes effect on the
next call site read. It is "restart-required" if any consumer captured
the value at process startup.

**Currently hot:**

- `discord.channel_id` — every listener reads it per event.
- `discord.announce_role_ids` — read on each lifecycle announcement.
- `bot.nag_enabled` — checked inside every nag scan iteration.
- `bot.nag_cooldown_hours` — read per iteration.
- `bot.lifecycle_grace_hours` — read per scan (governs end
  announcements only; starts always fire).
- `bot.reminders_enabled` — checked at the top of the reminders block
  in each lifecycle scan.
- `bot.upcoming_lead_hours` — read per scan when deciding whether an
  event is inside the pre-start heads-up window; `0` disables.
- `bot.reply_only_on_challenge_match` — checked each image post to
  decide between silent-on-non-recognition (`true`, default) and
  hardcore-riff-on-non-recognition (`false`).
- `bot.command_cooldown_seconds` — the `_UserCooldowns` class takes a
  supplier callable that reads on every `check_and_stamp` call.
- `bot.chat_history_messages` — read on each `_handle_mention` call.

**Currently restart-required:**

- `bot.nag_interval_hours` — the `discord.ext.tasks.loop(seconds=...)`
  interval for the nag loop is captured at class-definition time.
- `bot.lifecycle_interval_minutes` — same reason for the lifecycle
  loop (start / end announcements + ending-soon reminders).

Both restart-required intervals are labelled "restart required" on the
Settings page. Editing the row stores the new value but the scheduler
keeps its previous cadence until the process restarts.

If a new hot-editable setting is added, the consumer MUST be structured
to re-read on each use (either directly via `runtime_config` or through
a supplier callable passed into a long-lived object).

## Voice Modes and Presets

Voice settings under `voice.*` follow a two-layer model:

- `voice.base_persona` — the shared persona applied to every voice
  mode.
- `voice.<mode>_instruction` — per-mode overlay concatenated after the
  base persona for a specific situation (acknowledge, describe, nag,
  announce_start, announce_end, status, list, chat).

Presets (`fcb.agents.voice.PRESETS`, dict) bundle a full set of these
into named archetypes ("Gym Sister (default)", "Wholesome Cheerleader",
"Drill Sergeant", "Data Nerd"). The `/settings/voice` page ships them
as **buttons** that populate the textareas via JS without saving.
Saving is still an explicit action; presets are seed material, not a
committed choice.

When adding a new voice mode:

1. Add a `DEFAULT_<MODE>_INSTRUCTION` constant in `voice.py`.
2. Add its key to `_DEFAULTS`.
3. Add a matching entry to **every** preset in `PRESETS` (default
   fallback happens per-field, but full preset coverage keeps clicks
   deterministic).
4. Add the new key to `_FIELDS` in `settings_voice.py` so the UI
   surfaces it.
5. Write the caller function (e.g. `voice.new_mode(...)`) that reads
   `get_voice_settings()` and composes system prompt from
   `base_persona + <mode>_instruction`.

## Adding a New Non-Voice Setting — End-to-End

1. Add an env-var default in `config.py`:

    ```python
    FCB_NEW_KNOB: int = int(_optional("FCB_NEW_KNOB", "42"))
    ```

2. Add a `runtime_config.py` accessor:

    ```python
    def new_knob() -> int:
        return _int_or("bot.new_knob", config.FCB_NEW_KNOB)
    ```

3. Update every consumer in the bot / dashboard to call
   `runtime_config.new_knob()` instead of the `config` constant.

4. Add a field to `settings_general.py`:
    - Add to the `_current()` dict.
    - Add a form input inside one of the cards.
    - Add key + value normalization to the `save_settings` handler.

5. If the value is restart-required, label it "restart required" on
   the form. Otherwise no annotation needed.

6. Update `.env.example` with a comment describing the knob and its
   default.

## Restart Button

The `/settings/restart` route triggers
`sudo /usr/bin/systemctl restart fcb-bot.service fcb-web.service` via a
detached subprocess with a 3-second delay so the HTTP redirect
completes before the web service goes down. It relies on the standard
Ubuntu EC2 `NOPASSWD: ALL` sudoers grant — no dedicated sudoers rule
is required. A 15-second in-memory cooldown prevents click-spam.

## Migrations

Every schema change lives in `src/fcb/db/migrations/NNN_*.sql`. Seeding
default settings is legitimate migration work — see
`008_seed_announce_roles.sql` for the pattern. Prefer
`INSERT OR IGNORE` so re-running the migration is a no-op.

## Anti-Patterns

- Direct reads of `config.<CONSTANT>` for values that are meant to be
  admin-editable. Route through `runtime_config` instead.
- Caching a hot setting on `__init__` of a long-lived object. Pass a
  supplier callable so re-reads happen naturally.
- Adding a hot setting that a scheduler interval or gateway intent
  consumes at startup only. Either mark it restart-required in the UI
  or refactor the consumer to hot-read.
- Storing secrets in the `settings` table. Secrets stay in `.env`
  (see `deployment.md`).
