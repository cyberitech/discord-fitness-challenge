---
inclusion: always
---

# Discord Integration

FCB is bound to one Discord application, one guild, and one channel. The
dashboard is gated by Discord OAuth against a database allowlist. This
document is the source of truth for those identifiers and the intents,
scopes, and policies around them.

## Application

| Property            | Value                                    |
|---------------------|------------------------------------------|
| Application name    | FitnessChallengeBot                      |
| Application ID      | `1531786958400127206`                    |
| Bot user ID         | `1531786958400127206` (same as app id)   |
| OAuth redirect URI  | `https://fitness-challenge-bot.cyberian.me/auth/callback` |

The bot token and OAuth client secret are secrets. They live in `.env` on
the host as `DISCORD_BOT_TOKEN` and `DISCORD_OAUTH_CLIENT_SECRET`. The
public key (used only for HTTP Interactions endpoint verification) is
NOT used by FCB because FCB is a gateway bot; no code path needs it.

## Bound Guild and Channel

| Property                       | ID                     |
|--------------------------------|------------------------|
| Guild ID                       | `1470536228284661782`  |
| Channel ID                     | `1520915057117237321`  |
| Announce role (Fitness Challenger) | `1521599778599862395` |

The announce role ID is embedded in the default voice prompts for
`announce_event_start` and `announce_event_end` (see
`src/fcb/agents/voice.py`) so lifecycle announcements ping the
Fitness Challenger role. Each of the four voice presets also carries
its own tone-matched announce instructions with the same role
reference, so switching presets never loses the ping. The bot's
`channel.send()` calls for these announcements set
`AllowedMentions(roles=True, users=False, everyone=False)` to make
the role mention fire notifications without allowing accidental
`@everyone` or user pings.

## @-Mention Conversation Mode

When a user @-mentions the bot in the bound channel (or replies to a
bot message, which Discord also surfaces as a mention), the bot answers
with a persona-flavored reply. The scope is deliberately narrow — this
is a natural-language shortcut for `/status` and `/list`, not a general
chat bot. The `chat` voice mode is instructed to answer only from the
provided context (the user's own progress, the active challenges, and
recent channel history) and to redirect off-topic questions back to
challenge topics.

The bot detects addressing in two ways: formal Discord mentions (which
Discord inserts as `<@ID>` when the sender selects the bot from
autocomplete) and text-fallback matching against the bot's username or
guild display name (for people who type `@BotName` as plain text).
Either path routes to the same handler.

The chat handler also loads the last `FCB_CHAT_HISTORY_MESSAGES`
messages from the channel (default 100) using
`channel.history(...).clean_content` so the LLM can reference what's
been going on. The current mention message is excluded so the LLM
doesn't see the question twice.

Rate limiting shares the LLM cooldown with `/status`, `/list`,
`/describe`, and `/debug`. Replies use Discord's message-reply feature
with `replied_user=True` so the asker gets a natural single ping.

The bot MUST ignore messages from any other guild it happens to be in and
from any other channel in the bound guild. This constraint is enforced at
the listener layer, not via Discord permissions alone.

Design supports future expansion to additional guilds/channels: table
columns for `guild_id` and `channel_id` are present in the schema even
though only these two values are populated today.

## Required Bot Intents

Enabled in the Discord Developer Portal AND declared at client
construction time:

- **Guilds** — required baseline.
- **Guild Messages** — receive messages in the monitored channel.
- **Message Content** — read message text and attachments.
- **Server Members** — resolve author display names and avatars for the
  dashboard.

Message Content and Server Members are privileged intents; both must be
toggled ON in the portal. Missing intent surfaces as a gateway close with
a clear error and MUST NOT be silently retried.

## OAuth for the Dashboard

The dashboard uses the Authorization Code flow with these scopes:

- `identify` — get the logged-in Discord user's ID, username, avatar.
- `guilds` — confirm the user is a member of the bound guild.

Membership in the bound guild is necessary but not sufficient. Access to
the dashboard requires the user's Discord ID to be present in the
`admins` table (see below).

## Admin Allowlist

Access to the dashboard is controlled by an application-level allowlist,
not by Discord server permissions. The `admins` table is the source of
truth.

Seed row (installed by the initial migration):

| discord_user_id       | added_by       | note                   |
|-----------------------|----------------|------------------------|
| `1271241319699845276` | `system`       | initial seed           |
|                       |                |                        |

Any existing admin MAY add or remove other admins through the dashboard.
The last admin MUST NOT be removable through the UI; the app rejects an
attempt that would empty the table.

## Environment Variables

Consumed from `.env` (see `deployment.md` for path and permissions):

| Variable                          | Purpose                                     |
|-----------------------------------|---------------------------------------------|
| `DISCORD_BOT_TOKEN`               | Gateway auth for the bot process.           |
| `DISCORD_OAUTH_CLIENT_ID`         | Public app id, used in OAuth redirect URL.  |
| `DISCORD_OAUTH_CLIENT_SECRET`     | Server-side OAuth exchange.                 |
| `DISCORD_GUILD_ID`                | Bound guild id.                             |
| `DISCORD_CHANNEL_ID`              | Bound channel id.                           |
| `DASHBOARD_SESSION_SECRET`        | Signing key for the dashboard's session cookie. |

Rotation policy: any credential that appears in agent chat history, in
committed source, or in a shared log MUST be rotated in the Discord
Developer Portal, and the new value written into `.env` before the
service is restarted.
