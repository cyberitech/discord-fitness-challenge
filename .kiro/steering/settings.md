---
inclusion: fileMatch
fileMatchPattern: 'discord_bot_fitness_challenge/**/*'
---

# Settings

FCB keeps three kinds of configuration:

1. **`.env` on the host** — secrets and process-startup values that
   are read once at import (`src/fcb/config.py`).
2. **`global_settings` SQLite table** — runtime-tunable process-wide
   values. Nag / lifecycle scheduler intervals live here today.
3. **`guild_settings` SQLite table** — runtime-tunable per-guild
   values. Channel binding, announce roles, voice persona, and every
   behavior toggle live here.

The `runtime_config.py` module is the bridge: every hot-editable
value in code goes through it. Global accessors take no arguments;
per-guild accessors take a `guild_id: str` and consult the guild's
row, falling back to the `.env` default when the setting is unset.
This lets admins edit values from the dashboard's `/settings` and
`/settings/voice` pages without a service restart for most keys.

Cross-references: `deployment.md` for what stays in `.env`;
`dashboard.md` for the settings UI shape; `discord.md` for the
multi-guild model these tables serve.

## The Two Settings Tables

`global_settings`:

```
key         TEXT PRIMARY KEY
value       TEXT NOT NULL
updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
updated_by  TEXT           -- discord user id, or 'system' for migrations
```

`guild_settings`:

```
guild_id    TEXT NOT NULL           -- FK -> bot_guilds(guild_id) ON DELETE CASCADE
key         TEXT NOT NULL
value       TEXT NOT NULL
updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
updated_by  TEXT
PRIMARY KEY (guild_id, key)
```

Everything is TEXT. Numeric and boolean values are stringified on
write and parsed by the `runtime_config` helpers on read. Lists are
JSON-encoded (e.g. `discord.announce_role_ids` = `'["12345","67890"]'`).

`guild_settings` rows are cascade-deleted when the corresponding
`bot_guilds` row is removed by `on_guild_remove`. This is deliberate:
a guild that removes the bot forfeits its persona / channel binding;
if the bot is re-invited, an admin re-picks the channel on
`/setup/{guild_id}`.

## Key Prefixes

Prefixes namespace keys so a UI slice reads coherently.

| Prefix       | Table            | Purpose                                       |
|--------------|------------------|-----------------------------------------------|
| `voice.*`    | `guild_settings` | Persona and per-mode voice overlays (base_persona, acknowledge_instruction, describe_instruction, nag_instruction, announce_upcoming_instruction, announce_start_instruction, announce_end_instruction, remind_ending_instruction, hardcore_riff_instruction, status_instruction, list_instruction, chat_instruction). |
| `discord.*`  | `guild_settings` | Discord-facing per-guild wiring: `channel_id`, `announce_role_ids`. |
| `bot.*` (per-guild) | `guild_settings` | Per-guild behavior: `nag_enabled`, `nag_cooldown_hours`, `lifecycle_grace_hours`, `reminders_enabled`, `upcoming_lead_hours`, `reply_only_on_challenge_match`, `command_cooldown_seconds`, `chat_history_messages`. |
| `bot.*` (global) | `global_settings` | Scheduler cadences that gate `discord.ext.tasks.loop`: `nag_interval_hours`, `lifecycle_interval_minutes`. |

New per-guild settings SHOULD reuse an existing prefix; introduce a
new prefix only when the new setting is unambiguously outside the
existing groups.

## `runtime_config.py`

Located at `src/fcb/runtime_config.py`. Small module of typed
accessors with env-var fallback. Every function reads from SQLite on
every call; reads are local and cheap.

Canonical patterns:

```python
# Per-guild — every hot-editable value.
def channel_id(guild_id: str) -> str | None:
    return dao.get_guild_setting(guild_id, "discord.channel_id")

def command_cooldown_seconds(guild_id: str) -> int:
    return _guild_int(guild_id, "bot.command_cooldown_seconds",
                      config.FCB_COMMAND_COOLDOWN_SECONDS)

# Global — restart-required scheduler cadences.
def lifecycle_interval_minutes() -> float:
    return _global_float("bot.lifecycle_interval_minutes",
                         config.FCB_LIFECYCLE_INTERVAL_MINUTES)
```

Numeric / boolean / list parsing delegates to `_int_from`,
`_float_from`, `_bool_from` internal helpers with logged fallback on
parse failure.

Bot and dashboard code MUST call `runtime_config.*()` (not the
matching `config.*` constant directly) whenever the value is intended
to be admin-editable. `config.py` values remain valid seeds; they
define the fallback default for a fresh install and for guilds that
haven't overridden the value.

`channel_id(guild_id)` returns `None` when the guild has not yet
picked a channel. Every caller MUST handle `None` — the bot's
`on_message` listener treats it as "silently inert in this guild."

## Hot vs Restart-Required

A setting is "hot" if updating the row takes effect on the next call
site read. It is "restart-required" if a consumer captured the value
at process startup.

**Currently hot (all per-guild via `guild_settings`):**

- `discord.channel_id` — read on every `on_message`, lifecycle scan,
  nag scan, and slash-command dispatch.
- `discord.announce_role_ids` — read on each lifecycle announcement.
- `bot.nag_enabled` — checked inside every nag scan iteration.
- `bot.nag_cooldown_hours` — read per nag iteration.
- `bot.lifecycle_grace_hours` — read per lifecycle scan (governs end
  announcements only; starts always fire).
- `bot.reminders_enabled` — checked at the top of the reminders block
  in each lifecycle scan.
- `bot.upcoming_lead_hours` — read per scan when deciding whether an
  event is inside the pre-start heads-up window; `0` disables.
- `bot.reply_only_on_challenge_match` — checked each image post to
  decide between silent-on-non-recognition (`true`, default) and
  hardcore-riff-on-non-recognition (`false`).
- `bot.command_cooldown_seconds` — the `_UserCooldowns` class takes a
  supplier callable that reads on every `check_and_stamp`.
- `bot.chat_history_messages` — read on each `_handle_mention` call.
- `voice.*` — read on every voice-agent invocation.

**Currently restart-required (global via `global_settings`):**

- `bot.nag_interval_hours` — the `discord.ext.tasks.loop(seconds=...)`
  interval for the nag loop is captured at class-definition time.
- `bot.lifecycle_interval_minutes` — same reason for the lifecycle
  loop (start / end announcements + ending-soon reminders).

Both restart-required intervals are labelled "restart required" on
the settings page. Editing the row stores the new value but the
scheduler keeps its previous cadence until the process restarts.

If a new hot-editable setting is added, the consumer MUST be
structured to re-read on each use (either directly via `runtime_config`
or through a supplier callable passed into a long-lived object).

## Voice Modes and Presets

Voice settings under `voice.*` follow a two-layer model per guild:

- `voice.base_persona` — the shared persona applied to every voice
  mode in that guild.
- `voice.<mode>_instruction` — per-mode overlay concatenated after
  the base persona for a specific situation (acknowledge, describe,
  nag, announce_upcoming, announce_start, announce_end, remind_ending,
  hardcore_riff, status, list, chat).

Presets (`fcb.agents.voice.PRESETS`, dict) bundle a full set of these
into named archetypes ("Gym Sister (default)", "Wholesome Cheerleader",
"Drill Sergeant", "Data Nerd"). The `/settings/voice` page ships them
as **buttons** that populate the textareas via JS without saving.
Saving is still an explicit action; presets are seed material.

When adding a new voice mode:

1. Add a `DEFAULT_<MODE>_INSTRUCTION` constant in `voice.py`.
2. Add its key to `_DEFAULTS`.
3. Add a matching entry to **every** preset in `PRESETS`.
4. Add the new key to `_FIELDS` in `settings_voice.py`.
5. Write the caller function (e.g. `voice.new_mode(...)`) that reads
   `get_voice_settings(guild_id)` and composes the system prompt from
   `base_persona + <mode>_instruction`.

## Adding a New Non-Voice Setting — End-to-End

1. Add an env-var default in `config.py`:

    ```python
    FCB_NEW_KNOB: int = int(_optional("FCB_NEW_KNOB", "42"))
    ```

2. Add a `runtime_config.py` accessor. Per-guild:

    ```python
    def new_knob(guild_id: str) -> int:
        return _guild_int(guild_id, "bot.new_knob", config.FCB_NEW_KNOB)
    ```

    Or global (restart-required knobs only):

    ```python
    def new_knob() -> int:
        return _global_int("bot.new_knob", config.FCB_NEW_KNOB)
    ```

3. Update every consumer in the bot / dashboard to call
   `runtime_config.new_knob(...)` instead of the `config` constant.

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
`sudo /usr/bin/systemctl restart fcb-bot.service fcb-web.service` via
a detached subprocess with a 3-second delay so the HTTP redirect
completes before the web service goes down. It relies on the standard
Ubuntu EC2 `NOPASSWD: ALL` sudoers grant — no dedicated sudoers rule
is required. A 15-second in-memory cooldown prevents click-spam.

## Migrations

Every schema change lives in `src/fcb/db/migrations/NNN_*.sql`.
Seeding default settings is legitimate migration work. Prefer
`INSERT OR IGNORE` / `INSERT ... ON CONFLICT DO NOTHING` so re-runs
are no-ops. The multi-guild split from a single `settings` table into
`global_settings` + `guild_settings` landed in migration
`012_multi_guild.sql`.

## Anti-Patterns

- Direct reads of `config.<CONSTANT>` for values that are meant to be
  admin-editable. Route through `runtime_config` instead.
- Caching a hot setting on `__init__` of a long-lived object. Pass a
  supplier callable so re-reads happen naturally.
- Adding a hot setting that a scheduler interval or gateway intent
  consumes at startup only. Either mark it restart-required in the
  UI, or refactor the consumer to hot-read.
- Storing secrets in `global_settings` or `guild_settings`. Secrets
  stay in `.env`.
- Reading a per-guild setting without a `guild_id`. Every per-guild
  accessor takes it as its first argument.
