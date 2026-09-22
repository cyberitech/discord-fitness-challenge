---
inclusion: fileMatch
fileMatchPattern: 'discord_bot_fitness_challenge/**/*'
---

# Deployment

FCB runs on a single EC2 instance that also hosts a separate CTF platform.
This document captures the host, its coexisting services, and where FCB's
pieces land on it.

## Host

| Property        | Value                                     |
|-----------------|-------------------------------------------|
| Hostname (int)  | `ip-172-31-25-202`                        |
| Public FQDN     | `fitness-challenge-bot.cyberian.me`       |
| Public IP       | `35.82.216.29`                            |
| OS              | Ubuntu 24.04 LTS (Noble)                  |
| Kernel          | Linux 6.17.0-aws                          |
| Python          | 3.12.3                                    |
| Region          | `us-west-2`                               |
| IAM role        | `BedrockforEC2` (attached to instance)    |
| Service user    | `ubuntu` (until we split, see below)      |
| Repo path       | `/home/ubuntu/discord_bot_fitness_challenge` |

The `BedrockforEC2` role grants Bedrock InvokeModel to this instance. No
AWS access keys are provisioned. Application code MUST use the default
credential chain and MUST NOT read `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` from `.env`.

## Coexisting Services

The host is NOT dedicated to FCB. The following services are already
running and MUST NOT be disturbed:

| Unit                          | Purpose                              |
|-------------------------------|--------------------------------------|
| `nginx.service`               | Fronts 80/443 for existing sites     |
| `ctf-backend.service`         | CTF Platform Backend API             |
| `ctf-frontend.service`        | CTF Platform Frontend                |
| `postgresql@16-main.service`  | Postgres for CTF (localhost only)    |

nginx already serves `ctf.cyberitech.biz`, `labs.cyberitech.biz`, and
`borscht.potatoesandbeetroot.com` via certbot-issued certs.

FCB adds:

- One nginx vhost for `fitness-challenge-bot.cyberian.me`.
- One TLS cert issued via `certbot --nginx`.
- Two systemd units: `fcb-bot.service`, `fcb-web.service`.
- One SQLite file at `data/fcb.db`. FCB MUST NOT touch the CTF Postgres.

## Network

The bot process has no listener. The dashboard listens on
`127.0.0.1:<port>` (port pinned in `.env` as `FCB_WEB_PORT`); nginx
reverse-proxies `https://fitness-challenge-bot.cyberian.me/` to it. No
new public listener is opened.

## Files

| Path                                                            | Purpose                                     |
|-----------------------------------------------------------------|---------------------------------------------|
| `/home/ubuntu/discord_bot_fitness_challenge/.env`               | Secrets and per-host config. Mode 600. Gitignored. |
| `/home/ubuntu/discord_bot_fitness_challenge/data/fcb.db`        | SQLite database, WAL mode.                  |
| `/home/ubuntu/discord_bot_fitness_challenge/data/screenshots/`  | Persisted user-submitted images.            |
| `/home/ubuntu/discord_bot_fitness_challenge/deploy/nginx-vhost.conf` | Source-controlled nginx vhost, installed into `/etc/nginx/sites-available/fitness-challenge-bot`. |
| `/home/ubuntu/discord_bot_fitness_challenge/deploy/fcb-bot.service`  | Source-controlled systemd unit for the bot. |
| `/home/ubuntu/discord_bot_fitness_challenge/deploy/fcb-web.service`  | Source-controlled systemd unit for the dashboard. |

Logs from `journalctl -u fcb-bot -u fcb-web` are the human-facing view.
The `bot_events` SQLite table is the dashboard's view.

## Cert Issuance

Certificate issuance is done once with certbot in nginx mode after the
vhost is in place and DNS resolves:

```
sudo certbot --nginx -d fitness-challenge-bot.cyberian.me
```

Renewal piggy-backs on the certbot systemd timer already installed for
the other domains.

## systemd Units

Both units MUST:

- Run as the service user with the working directory set to the repo.
- Source `EnvironmentFile=/home/ubuntu/discord_bot_fitness_challenge/.env`.
- Restart on failure with a small backoff.
- Depend on `network-online.target`.

## Deploy Loop

The intended dev-to-prod loop on this same host:

1. Edit code in `/home/ubuntu/discord_bot_fitness_challenge/`.
2. `uv sync` for dependency changes.
3. `sudo systemctl restart fcb-bot fcb-web` for code changes.
4. `sudo systemctl reload nginx` after vhost changes.
