---
inclusion: fileMatch
fileMatchPattern: 'discord_bot_fitness_challenge/**/*'
---

# Discord Integration

FCB is one Discord application (`FitnessChallengeBot`) that serves
every guild it's a member of. There is no single-guild binding at the
process level. Per-guild wiring — which channel the bot listens in,
which roles to ping on lifecycle announcements, voice persona,
behavior toggles — lives in the `guild_settings` table and is read
through `fcb.runtime_config`. The dashboard is gated by Discord OAuth
and an application-level admin allowlist.

Cross-references: `settings.md` for how `guild_settings` /
`global_settings` are structured; `dashboard.md` for the OAuth flow and
server picker; `deployment.md` for `.env` layout on the host.

## Application

| Property           | Value                                                     |
|--------------------|-----------------------------------------------------------|
| Application name   | FitnessChallengeBot                                       |
| Application ID     | `1531786958400127206`                                     |
| Bot user ID        | `1531786958400127206` (same as app id)                    |
| OAuth redirect URI | `https://fitness-challenge-bot.cyberian.me/auth/callback` |

The bot token and OAuth client secret are secrets. They live in `.env`
on the host as `DISCORD_BOT_TOKEN` and `DISCORD_OAUTH_CLIENT_SECRET`.
The interactions endpoint public key is unused because FCB is a
gateway bot.

## Multi-Guild Model

The bot is invited to a guild by an administrator of that guild via
the standard Discord OAuth bot-authorize URL. On `on_guild_join` the
bot inserts a row into `bot_guilds`; on `on_guild_remove` it deletes
that row (cascade removes `guild_settings` and `guild_cache` for that
guild).

Per-guild data lives in three tables:

| Table            | Purpose                                                  |
|------------------|----------------------------------------------------------|
| `bot_guilds`     | Registry — one row per guild the bot is currently in.    |
| `guild_settings` | Key/value config scoped to a guild (`discord.channel_id`, `discord.announce_role_ids`, `bot.*`, `voice.*`). |
| `guild_cache`    | Cached list of channels and roles per guild for the dashboard picker. |

The bot ignores every message whose `guild_id` is not registered in
`bot_guilds`, and every message whose `channel.id` does not match the
guild's `discord.channel_id` setting. Both checks live in the
`on_message` listener in `src/fcb/bot/client.py`.

Until a guild's `discord.channel_id` is set (either by an admin
picking a channel on `/setup/{guild_id}` or by direct SQL seed), the
bot is silently inert in that guild.

### Bot Invite URL

Built at runtime from `.env`:

```
https://discord.com/oauth2/authorize
  ?client_id=<DISCORD_OAUTH_CLIENT_ID>
  &scope=bot+applications.commands
  &permissions=<FCB_BOT_PERMISSIONS>
```

`FCB_BOT_PERMISSIONS` bit-sum: View Channels, Send Messages, Add
Reactions, Embed Links, Attach Files, Read Message History, Mention
Everyone, Use Application Commands. Default `2147732544`.

The dashboard's `/setup/{guild_id}` page also builds this URL for the
guilds a user admins but the bot isn't in yet.

## @-Mention Conversation Mode

When a user @-mentions the bot in the guild's configured channel (or
replies to a bot message, which Discord surfaces as a mention), the
bot answers with a persona-flavored reply. Scope is deliberately
narrow — a natural-language shortcut for `/status` and `/list`, not a
general chat bot. The `chat` voice mode is instructed to answer only
from the provided context (the user's own progress, active challenges
in that guild, recent channel history) and to redirect off-topic
questions back to challenge topics.

The bot detects addressing in two ways: formal Discord mentions
(`<@ID>`) and text-fallback matching against the bot's username or
guild display name. Either path routes to the same handler.

The chat handler loads the last `bot.chat_history_messages` messages
from the channel (default 100, per-guild) using
`channel.history(...).clean_content`. The current mention message is
excluded so the LLM doesn't see the question twice.

Rate limiting shares the LLM cooldown with `/status`, `/list`,
`/describe`, and `/debug` — a per-user in-memory window sourced from
`bot.command_cooldown_seconds` (per-guild, hot). Replies use Discord's
message-reply feature with `replied_user=True`.

Messages from any other channel in the same guild are ignored at the
listener layer, not via Discord permissions.

## Workout Acknowledgements

Approved workout replies contain two layers:

- Generated coach prose is framed by the router's primary event.
- Deterministic footer text lists every event credited to each approved
  submission. When several events match, the first is labelled primary.

Multi-session batches preserve the submission-to-event mapping instead of
flattening all event names into one ambiguous list. Pending submissions
receive a clarification prompt rather than an approval acknowledgement;
rejected submissions receive no credit.

Event names in acknowledgement footers are escaped as Discord Markdown
and mentions. Replies permit the intended submitting-user mention while
disabling role and `@everyone` mentions.

## Announcement Role Pings

Lifecycle announcements (upcoming, start, end, ending-soon reminders)
optionally ping one or more roles listed in the guild's
`discord.announce_role_ids` setting (JSON-encoded list of role IDs).
The bot's `channel.send()` for these announcements sets
`AllowedMentions(roles=True, users=False, everyone=False)` so the role
mention fires notifications without allowing accidental `@everyone`
or user pings.

The default voice prompts in `src/fcb/agents/voice.py` embed a role
mention token that the voice agent may reference; when the guild's
`discord.announce_role_ids` is empty, the bot strips the role
placeholder before posting.

## Required Bot Intents

Enabled in the Discord Developer Portal AND declared at client
construction time:

- **Guilds** — required baseline.
- **Guild Messages** — receive messages in monitored channels.
- **Message Content** — read message text and attachments.
- **Server Members** — resolve author display names and avatars for
  the dashboard.

Message Content and Server Members are privileged intents; both must
be toggled ON in the portal. Missing intent surfaces as a gateway
close with a clear error and MUST NOT be silently retried.

## OAuth for the Dashboard

The dashboard uses the Authorization Code flow with these scopes:

- `identify` — get the logged-in Discord user's ID, username, avatar.
- `guilds` — enumerate the user's Discord guilds and their permission
  bitmask so the server picker can show guilds where they hold
  `MANAGE_GUILD` or `ADMINISTRATOR` (or `owner`).

Access model:

- The **developer** (`config.FCB_DEVELOPER_USER_ID`) always has full
  access, including `/admins`.
- **Admins** (rows in the `admins` table) have full access except
  admin management.
- **Everyone else** is bounced with a "contact the developer" screen.

After login the user is shown a server picker built from the
intersection of the guilds they administer on Discord and the guilds
in `bot_guilds`. Guilds where the user is admin but the bot isn't
route to `/setup/{guild_id}`, which surfaces the bot-invite URL.

## Admin Allowlist

Access is controlled by the `admins` table, not by Discord server
permissions.

Seed row (installed by the initial migration):

| discord_user_id       | added_by | note         |
|-----------------------|----------|--------------|
| `1271241319699845276` | `system` | initial seed |

Any existing admin MAY add or remove other admins through the
dashboard. The last admin MUST NOT be removable through the UI.

## Environment Variables

Consumed from `.env` (see `deployment.md`):

| Variable                      | Purpose                                              |
|-------------------------------|------------------------------------------------------|
| `DISCORD_BOT_TOKEN`           | Gateway auth for the bot process.                    |
| `DISCORD_OAUTH_CLIENT_ID`     | Public app id; used in OAuth redirect + invite URL.  |
| `DISCORD_OAUTH_CLIENT_SECRET` | Server-side OAuth exchange.                          |
| `FCB_DEVELOPER_USER_ID`       | Discord user id with unconditional dashboard access. |
| `FCB_BOT_PERMISSIONS`         | Permission bitsum used to build the invite URL.      |
| `FCB_PUBLIC_BASE_URL`         | Public base URL for OAuth redirect + invite target.  |
| `DASHBOARD_SESSION_SECRET`    | Signing key for the dashboard's session cookie.      |

`DISCORD_GUILD_ID` and `DISCORD_CHANNEL_ID` are NOT read — the bot is
multi-guild and channel binding lives in `guild_settings`.

Rotation policy: any credential that appears in agent chat history,
committed source, or shared logs MUST be rotated in the Discord
Developer Portal, and the new value written into `.env` before the
service is restarted.
