# jobwatch

A self-hosted Discord bot that watches a JSON feed or the SpeedyApply Markdown
job repository and posts newly-added, active roles to a Discord channel as
embeds.

## How it works

- A `tasks.loop` polls `LISTINGS_URL` every `POLL_MINUTES` minutes. GitHub
  repository URLs are resolved to the repository's current `README.md` table;
  JSON feeds remain supported.
- Each role's `id` is diffed against a SQLite table of previously-seen IDs.
- **Cold start**: on the very first poll, the feed already contains thousands
  of roles. All of their IDs are recorded, and the bot posts up to
  `BOOTSTRAP_POST_LIMIT` qualifying roles once as a smoke test. After that it
  sets a `meta.bootstrap_done` flag and only roles that appear in *later* polls
  get alerted.
- If `INTERNSHIPS_ONLY=true`, the bot keeps only listings that look like
  internships or co-ops and excludes obvious full-time roles.
- `BOOTSTRAP_POST_LIMIT` controls how many newest qualifying roles are posted
  on the first startup.
- If `LISTINGS_URL` changes, the bot treats the new feed as a bootstrap and
  applies the same limit; listings already stored in its database are never
  posted again.
- If `MIN_DATE_POSTED` is set, listings older than that timestamp are skipped.
- An ID is recorded as seen *before* the bot attempts to post it, so a crash
  mid-post can never cause a duplicate alert on the next run.
- Roles with `active: false` or `is_visible: false` are skipped.
- Posts are throttled to ~1/second to stay well under Discord rate limits.
- A failed fetch (network error, bad JSON, unexpected shape) logs a warning
  and is retried on the next cycle — it never crashes the loop.
- The `/faang` and `/quant` slash commands fetch the source on demand and show
  the five newest listings from those respective tiers. They honor the same
  filters configured for alerts.

## Requirements

- Python 3.10+
- A Discord bot application with **Send Messages** and **Embed Links**
  permissions in the target channel

## Setup

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
copy .env.example .env
# edit .env and fill in DISCORD_TOKEN and DISCORD_CHANNEL_ID
.\venv\Scripts\python.exe bot.py
```

On Linux:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env
venv/bin/python bot.py
```

## Configuration

All configuration is via environment variables (loaded from `.env` when run
directly, or from the systemd `EnvironmentFile` in production).

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | — | Bot token from the Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | yes | — | Channel ID to post role alerts to |
| `LISTINGS_URL` | no | `https://github.com/speedyapply/2027-SWE-College-Jobs` | GitHub repository or JSON/Markdown source URL |
| `POLL_MINUTES` | no | `30` | Minutes between polls |
| `DB_PATH` | no | `seen.db` | SQLite file for dedup state |
| `BOOTSTRAP_POST_LIMIT` | no | `3` | On the first startup, post up to this many newest qualifying roles; set to `0` to disable |
| `INTERNSHIPS_ONLY` | no | `true` | If `true`, only keep listings that look like internships or co-ops |
| `MIN_DATE_POSTED` | no | (blank) | Skip listings older than this timestamp; use ISO 8601 or Unix seconds |
| `TITLE_KEYWORDS` | no | (none) | Comma-separated substrings; only alert if the title contains one |
| `LOCATIONS` | no | (none) | Comma-separated substrings; only alert if a location contains one |
| `SPONSORSHIP_ONLY` | no | `false` | If `true`, skip roles that require citizenship or explicitly offer no sponsorship |

Filter matching is case-insensitive substring matching. Leave a filter blank
to disable it.

## Creating the Discord bot

1. Create a server (or use an existing one) where you're an admin.
2. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application** → name it `jobwatch`.
3. **Bot** tab → **Reset Token** → copy it into `DISCORD_TOKEN`. No privileged
   gateway intents are needed — the bot only uses `discord.Intents.default()`.
4. **OAuth2** tab → **URL Generator** → check scope `bot` → under Bot
   Permissions check **Send Messages** and **Embed Links** → copy the
   generated URL.
5. Open that URL in a browser, select your server, and authorize.
6. Enable **Developer Mode** in Discord (User Settings → Advanced), then
   right-click the target channel → **Copy Channel ID** → paste into
   `DISCORD_CHANNEL_ID`.

Once the bot is online, use `/faang` or `/quant` in a server channel to see
the five newest matching listings from that source tier. Discord slash-command
registration can take a few minutes to appear globally after the bot starts.

## Running with Docker

```bash
cp .env.example .env
# edit .env and fill in DISCORD_TOKEN and DISCORD_CHANNEL_ID
docker compose up -d --build
```

This builds the image, starts the bot in the background, and stores the
SQLite dedup database in a named volume (`jobwatch-data`) mounted at
`/app/data`, so `seen.db` survives container rebuilds/restarts. The compose
file forces `DB_PATH=/app/data/seen.db` regardless of what's in `.env`.

Useful commands:

```bash
docker compose logs -f      # follow logs
docker compose restart      # restart the bot
docker compose down         # stop (keeps the volume/data)
docker compose down -v      # stop and delete the dedup database too
```

## Deploying with systemd

```bash
sudo useradd -r -s /usr/sbin/nologin jobwatch
sudo mkdir -p /opt/jobwatch
sudo cp bot.py requirements.txt /opt/jobwatch/
sudo cp .env /opt/jobwatch/.env   # fill in real values first
python3 -m venv /opt/jobwatch/venv
/opt/jobwatch/venv/bin/pip install -r /opt/jobwatch/requirements.txt
sudo chown -R jobwatch:jobwatch /opt/jobwatch
sudo chmod 600 /opt/jobwatch/.env
sudo cp jobwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobwatch
```

The unit runs as the unprivileged `jobwatch` user, loads secrets from
`/opt/jobwatch/.env` via `EnvironmentFile`, restarts on failure, and logs to
journald:

```bash
journalctl -u jobwatch -f
```

## Files

| File | Purpose |
|---|---|
| `bot.py` | The bot |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for local/production config |
| `jobwatch.service` | systemd unit for deployment |
| `Dockerfile` | Container image definition |
| `docker-compose.yml` | Compose service + persistent volume for `seen.db` |
| `.dockerignore` | Excludes `venv/`, `.git/`, `.env`, etc. from the build context |
| `.gitignore` | Excludes `.env`, `seen.db`, `venv/` |

## Security notes

- Never commit `.env` or `seen.db`.
- Treat `DISCORD_TOKEN` like a password — anyone with it can control the bot.
- The systemd unit runs as a dedicated unprivileged user with `ProtectSystem=strict`
  and `ProtectHome=true`, limiting writable paths to `/opt/jobwatch`.
