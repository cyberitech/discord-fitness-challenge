---
inclusion: always
---

# Architecture

The Fitness Challenge Bot (FCB) is a Discord bot plus admin dashboard that
tracks progress in monthly fitness events. It monitors a single channel in
a single Discord server, uses Amazon Bedrock (Claude Opus) via Strands
agents to read screenshots and generate messages, persists everything to
SQLite, and exposes a Discord-OAuth-gated web dashboard for administration.

The system is a monorepo with two long-running processes that share one
SQLite database and one nginx vhost.

## Process Model

```
   Discord API (WSS out)              Browser (HTTPS in, 443)
        │                                     │
        ▼                                     ▼
   ┌────────────┐                       existing nginx
   │  fcb-bot   │                             │
   │ discord.py │                             │ reverse proxy
   │ + Strands  │                             ▼
   │ + Bedrock  │                       ┌────────────────┐
   └─────┬──────┘                       │    fcb-web     │
         │                              │ FastAPI, server│
         │                              │ -rendered HTML │
         │                              │ Discord OAuth  │
         │                              │ + Strands      │
         │                              └──────┬─────────┘
         └───────────────┬─────────────────────┘
                         ▼
                  SQLite (WAL mode)
                  data/fcb.db
```

Two `systemd` units run as the same unprivileged service user and share
the SQLite file. Both processes MAY invoke Strands agents (e.g., the
dashboard re-runs vision on an admin's request).

### Why two processes

- The bot's event loop is dominated by websocket I/O and background jobs;
  the dashboard's is dominated by HTTP request/response cycles. Running
  them together forces one to block the other under load.
- A dashboard crash MUST NOT take the bot offline mid-event. Independent
  units keep the blast radius contained.
- Deploys can restart one without disturbing the other.

## Transport

- Bot ↔ Discord: gateway (outbound WebSocket) only. No inbound port.
- Browser ↔ dashboard: HTTPS on 443 via the pre-existing nginx, reverse-
  proxied to `127.0.0.1` on a private port owned by `fcb-web`.
- Discord Interactions HTTP endpoint is NOT used. Slash commands
  (`/ping`, `/status`, `/list`, `/leaderboard`, `/debug`) and the
  message context menu (`Describe`) are registered against the gateway
  and synced to the bound guild at process startup.

## Data Store

SQLite in WAL mode, single file at `data/fcb.db`. Both processes open the
file directly. WAL is required because bot and dashboard write
concurrently.

### Schema

| Table                | Purpose                                              |
|----------------------|------------------------------------------------------|
| `admins`             | Discord user IDs allowed into the dashboard          |
| `events`             | One row per challenge or 1% Better period            |
| `event_participants` | Auto-populated: users with at least one approved     |
|                      | submission for the event (via SQL triggers)          |
| `submissions`        | Every workout update — image ref, extracted stats    |
| `submission_events`  | Many-to-many between submissions and events, with an |
|                      | `is_primary` flag. Aggregate reads MUST go through   |
|                      | this table                                           |
| `event_metrics`      | Materialized per-user-per-event totals (reserved,    |
|                      | currently unused — aggregation is ad-hoc in Python)  |
| `bot_events`         | Structured log of bot actions and errors             |
| `users`              | Cached Discord identity (username, display, avatar)  |
| `settings`           | Key/value config (see `settings.md`)                 |
| `schema_migrations`  | Applied migration versions                           |

`events.prompt` holds the editable natural-language description of what
this month's challenge is. Vision and scoring agents read that prompt as
their spec for the current period. History is preserved by never mutating
prior `events` rows.

`submissions.status` MUST be one of `pending`, `approved`, `rejected`.
Bot-processed submissions are auto-classified: recognized workout
screenshots land as `approved`; non-workout images land as `rejected`
so a record still exists for admins to flip. Admins can change either
status from the dashboard. Approvals feed `event_metrics`; rejections
do not. `pending` is reserved for admin-initiated manual entries that
still need review.

The bot's response to a non-recognized image is controlled by the hot
setting `bot.reply_only_on_challenge_match` (default `true`).

An image is "recognized" when `is_workout_screenshot` is true AND at
least one active event matched via the single-event fast path or the
router. Recognized images always get the coach-voice acknowledgement.

When `bot.reply_only_on_challenge_match = true` (default): every
non-recognized outcome is silent in the channel. The screenshot still
lands on disk, a `submissions` row still gets inserted (status =
`rejected`), and a `bot_events` row is written under one of
`submission.silent.no_event`, `submission.silent.vision_failed`,
`submission.silent.not_workout`, or `submission.silent.no_match`. If
a user wants to know why a post wasn't counted they @-mention the bot;
the chat_reply path pulls `dao.list_recent_submissions_by_user` and
feeds status / confidence / vision notes into the LLM as the
`invoker_recent_submissions` bucket so it can answer with the real
reason.

When `bot.reply_only_on_challenge_match = false`: every non-recognized
image gets a `voice.hardcore_riff` reply instead. The riff sends the
raw image to Bedrock through its own multimodal call so it can
reference specifics visible in the image (numbers on a lab chart,
food on a plate, whatever) and ties them back to the user's
fitness/health arc in a driving, motivating tone. The riff prompt
explicitly permits OTC-supplement / diet / sleep / hydration
suggestions (normal gym-friend advice) and prohibits medical
diagnoses, prescription-med recommendations, and refusal
disclaimers. Categorized as `submission.riff.no_event`,
`submission.riff.vision_failed`, `submission.riff.not_workout`, or
`submission.riff.no_match` in `bot_events`. Submissions still
persist to the DB the same way as the silent path.

`bot_events` is the source of the dashboard's logs panel. It carries
level, actor (user id or `bot`), category, message, and JSON context.

`events.primary_metric` is the field that ranks the leaderboard for the
event. Values MUST be one of `submission_count`, `duration_seconds`,
`distance_miles`, `elevation_gain_feet`, `calories`, `reps`, or
`volume_lb`. The `/leaderboard` slash command, `/status` reply, and the
home page top-3 all read this field. Aggregation happens in Python off
the `submissions.extracted_stats` JSON — `event_metrics` is defined but
currently unused; it exists for later materialization.

`volume_lb` is `weight_lb × reps × sets`. The vision agent may write it
directly (e.g. reading a tonnage summary chart) or leave it null; the
DAO computes the fallback per-submission before summing, so weighted-
strength challenges rank correctly whether users post per-set numbers
or a summary graph. This is also what makes catch-up sessions "count":
doing 5×24 at the same weight contributes exactly 2× the volume of a
5×12, which is what a "1% better with catch-up" cadence expects.

## Multi-Event Attribution

More than one event MAY be active at the same time in the bound channel
(e.g. a monthly elevation challenge overlapping a daily 1% cardio
routine). The bot handles this via a many-to-many junction table
`submission_events(submission_id, event_id, is_primary)`:

- Aggregate reads (leaderboard, home page, `/status`, participant list)
  MUST go through `submission_events`. Reading `submissions.event_id`
  alone will miss secondary attributions.
- `submissions.event_id` is retained as the "primary event" (first in
  the router's ordered match list). It drives the reply framing and the
  submission-detail page header.
- When exactly one event is active, the router is NOT invoked; the
  submission is linked to that single event.
- When two or more are active, the `router` agent decides which subset
  (possibly all, possibly none) a submission counts toward.
- On router failure the bot falls back to attributing the submission to
  every active event so the data isn't lost, and logs an ERROR row
  (`router.failed`) so it surfaces in the logs panel.

`event_participants` is auto-populated by SQLite triggers whenever a
submission is linked to an event (and the submission is `approved`), or
whenever an existing submission is flipped to `approved`. Migration 006
also backfills the participants table from historical rows.

`events.nag_threshold_days` controls the nag scheduler. Users with at
least one approved submission for the event who haven't posted in this
many days receive a coach-voice nudge. `0` disables nagging for the
event without changing any global config.

## Nag Scheduler

The bot process runs a `discord.ext.tasks.loop` that periodically checks
the active event for slackers and posts a nudge generated by the voice
agent. The scheduler:

- Runs once shortly after `on_ready`, then every
  `FCB_NAG_INTERVAL_HOURS` (default 1 hour).
- Considers the active event only. If no event is active, or the event's
  `nag_threshold_days == 0`, the scan is a no-op.
- Treats a user as a "participant" if they have any approved submission
  for the event. Users who have never posted are not nagged.
- Enforces `FCB_NAG_COOLDOWN_HOURS` (default 24) as the minimum interval
  between nags to the same user for the same event, using
  `bot_events.category = 'nag.sent'` rows as the source of truth.
- Records every scan outcome (`nag.scan`, aggregate counts) and every
  individual nag (`nag.sent`, with the generated text) to `bot_events`,
  which powers the dashboard logs panel.

`FCB_NAG_ENABLED=false` disables the scheduler entirely without touching
any per-event field.

## Lifecycle Scheduler

The bot runs a lifecycle loop on its own `discord.ext.tasks.loop`,
separate from the nag scheduler. Its interval is
`FCB_LIFECYCLE_INTERVAL_MINUTES` (default 5, restart-required) — short
enough that a dashboard-created event announces effectively immediately.

The loop handles four concerns each scan, in this order: upcoming →
start → end → reminders.

**Upcoming pre-announcements.** Any event whose `starts_at` is within
`bot.upcoming_lead_hours` (default 48, hot) from now AND has not yet
started AND has `announced_upcoming_at IS NULL` gets a voice-generated
"coming up soon" heads-up posted to the channel, and its
`announced_upcoming_at` timestamp set. This fires exactly once per
event; edits to `starts_at` don't re-trigger it. Setting
`bot.upcoming_lead_hours = 0` disables the pre-announcement entirely
without touching any per-event field. Events created with `starts_at`
already in the past bypass this path and go straight to the start
announcement — the immediate-announce behavior is preserved.

**Start announcements.** Any event whose `starts_at` has passed and
whose `announced_start_at IS NULL` gets a voice-generated opening line
posted to the channel, and its `announced_start_at` timestamp set.
Start announcements fire regardless of how far in the past `starts_at`
is — an admin who creates a challenge in the dashboard with a
historical start date gets an immediate announcement. The grace window
does NOT apply to starts.

**End announcements.** Any event whose `ends_at` has passed and whose
`announced_end_at IS NULL` gets a closing line that names the winner
(top of the leaderboard) plus up to two runners-up.
`announced_end_at` is stamped. If the event ended more than
`FCB_LIFECYCLE_GRACE_HOURS` (default 24) ago, the timestamp is stamped
anyway but no message is posted — this prevents stale winner shoutouts
for events that were only created retroactively.

**Ending-soon reminders.** For each currently-open event
(`starts_at <= now < ends_at`), the loop computes four milestone
times:

| Kind        | Trigger time                     |
|-------------|----------------------------------|
| `halfway`   | midpoint of `[starts_at, ends_at]` |
| `one_week`  | `ends_at - 7 days`               |
| `three_day` | `ends_at - 3 days`               |
| `one_day`   | `ends_at - 1 day`                |

Milestones whose trigger is before `starts_at` are dropped (short
events skip the wider windows). Of the milestones whose trigger has
passed, the loop fires **only the latest** that hasn't already been
sent. Earlier passed milestones that never fired are recorded as
silently skipped in `event_reminders` (with `posted=0`) so they are
never revisited. This is what implements the "only the freshest
reminder fires" behavior: an event created mid-flight, or an event
whose reminders were disabled and re-enabled, catches up with at most
one message instead of blasting the channel with backlogged reminders.

The reminders scan is gated by the hot `bot.reminders_enabled` toggle
(default true).

The `event_reminders` table (migration `009_event_reminders.sql`) is
the source of truth for "did we handle this reminder for this event."
`(event_id, kind)` is the primary key; the `posted` column
distinguishes actually-posted rows from silent-skips in the audit log.

All three timestamps (`announced_start_at`, `announced_end_at`, and
`event_reminders` rows) live in the DB, so restarts don't cause
duplicate announcements or reminders.

## Rate Limiting

LLM-invoking commands share a single per-user in-memory cooldown of
`FCB_COMMAND_COOLDOWN_SECONDS` (default 30, hot-editable via `/settings`).
The scope covers:

- `/status`, `/list` — user-visible progress and challenge listing.
- `/debug` — admin-only raw vision inspector.
- `Describe` context menu — public voice-flavored image narration.
- `@-mention` conversation replies — the natural-language shortcut for
  the above.

Cooldown state is deliberately in-memory: a bot restart clears it,
which is fine given the short window. Requests inside the cooldown
never touch Bedrock.

Rate-limit responses are always private:

- Slash and context-menu commands respond with `ephemeral=True`.
- `@-mention` replies DM the user; if their DMs are closed, the bot
  falls back to a ⏱️ reaction on the message. If both fail, silent.

Image posts in the channel are not rate-limited — user cadence there is
naturally paced by "I did a workout, here's the screenshot" rather than
by scripted invocation.

`users` caches Discord identity (username, display name, avatar URL) so
the dashboard can render friendly rows without hitting the Discord API.
The bot upserts on every observed message; the dashboard upserts on every
successful OAuth callback and on every authenticated page load.

## Where Strands Agents Fit

Deterministic logic stays deterministic: SQL writes, "did user X post in
the last N days," leaderboards. Strands agents handle the fuzzy parts:

| Agent          | Input                        | Output                        |
|----------------|------------------------------|-------------------------------|
| `vision`       | image bytes + event prompt   | structured stats JSON         |
| `classify`     | message text + attachments   | is-this-a-workout-update flag |
| `voice`        | context (user, event, state) | reply / nag / hype text       |
| `router`       | extracted stats + active events | ordered list of matched event IDs |

All four share one Bedrock model client. See `bedrock.md`. The router
fires on every image that vision classifies as a workout, regardless
of how many events are active — the fast path that used to skip
routing when only one event was active was removed after it caused
mis-attribution (unrelated workouts got auto-credited to the single
active challenge). Router failure now fails CLOSED (no attribution)
rather than fanning out to every active event.

### Shared Workout Domain Knowledge

`agents/__init__.py` exports `WORKOUT_DOMAIN_KNOWLEDGE` — a factual
baseline every agent that reasons about workouts prepends to its
system prompt. `vision.py` and `router.py` both consume it today; new
agents should too when the concept applies.

It covers:

- Volume terminology: set volume, exercise volume, session volume, and
  the load-bearing rule that a specific-exercise challenge is scored
  from THAT exercise's volume only. A workout's session-total volume
  bar is NOT the target-exercise volume when other exercises share
  the session.
- Unit conventions: km→mi, m→ft, kg→lb, HH:MM:SS→seconds.
- Common apps: shape-level recognition of Hevy, Strong, JEFIT, Apple
  Fitness/Health, Garmin Connect, Strava, WHOOP, Fitbod, NTC.
- Anti-patterns: never invent numbers; never classify food photos /
  memes / selfies as workout screenshots.

This is code, not admin-tunable persona. Event prompts assume the
shared knowledge and stay focused on what's unique to the challenge
(target exercise, target metric, catch-up semantics). Restating volume
formulas or "count only my exercise" language in an event prompt is a
smell — extend `WORKOUT_DOMAIN_KNOWLEDGE` instead.

## Package Layout

```
discord_bot_fitness_challenge/
├── pyproject.toml           # uv-managed, single project, two entrypoints
├── src/fcb/
│   ├── config.py            # env loader, hard-fails on missing keys
│   ├── runtime_config.py    # DB-backed settings with env fallback (see settings.md)
│   ├── logging_setup.py     # per logging-standards.md
│   ├── db/
│   │   ├── migrations/      # NNN_*.sql, applied in order at startup
│   │   └── dao.py           # data-access functions
│   ├── agents/
│   │   ├── __init__.py      # shared Strands BedrockModel instance
│   │   ├── vision.py        # screenshot → structured stats
│   │   ├── router.py        # multi-active-event routing (see multi-event section)
│   │   └── voice.py         # persona-flavored text (all modes + presets)
│   ├── bot/                 # discord.py client, listeners, schedulers
│   │   ├── __main__.py      # `python -m fcb.bot`
│   │   └── client.py        # FCBClient class, all handlers and loops
│   └── dashboard/           # FastAPI dashboard (see dashboard.md)
│       ├── __main__.py      # `python -m fcb.dashboard`
│       ├── app.py           # FastAPI app + router registration
│       ├── auth.py          # Discord OAuth + allowlist gate
│       ├── ui.py            # page() wrapper, nav, CSS, admin_from_session()
│       ├── events.py        # /events surfaces + CSV export
│       ├── submissions.py   # /submissions surfaces + image serving
│       ├── admins.py        # /admins surfaces
│       ├── logs.py          # /logs (bot_events viewer)
│       ├── settings_general.py  # /settings, /settings/restart
│       ├── settings_voice.py    # /settings/voice
│       └── static/          # /static/{favicon.png,logo.png}
├── data/                    # runtime; fcb.db + screenshots/, gitignored
├── deploy/                  # nginx vhost, systemd units
└── .kiro/steering/          # this directory
```

Entrypoints:

- `python -m fcb.bot` — the gateway worker.
- `uvicorn fcb.dashboard.app:app --host 127.0.0.1 --port <PORT>` — the
  dashboard.

## Cross-Cutting Rules

- All Python code MUST pass `flake8`, `mypy`, `isort`, `black` (per
  `project-guidelines.md`).
- All output MUST route through a module-level logger (per
  `logging-standards.md`). `print` is prohibited.
- Secrets MUST live in `.env` at the repo root, mode 600, gitignored.
  Code reads them through `fcb.config`. Runtime-tunable non-secret
  config lives in the `settings` table and is read through
  `fcb.runtime_config` (see `settings.md`).
- Error handling follows fail-fast: catch only where recovery is real, and
  log stack traces on true failures (per `logging-standards.md`).
