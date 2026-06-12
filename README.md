# Discord Ops Bot

A multi-purpose utility and monitoring bot: website uptime monitoring with
alerts, bug reports, suggestions with voting, a webhook sender, Steam update
notifications, and weather — all as slash commands, backed by PostgreSQL, with
an optional read-only web dashboard.

Built with **discord.py 2.x**, `aiohttp`, and `asyncpg`. Requires
**Python 3.10+** and **PostgreSQL 12+**.

## Features

| Command | What it does |
|---|---|
| `/checkweb add <url>` | Monitor a URL every 5 minutes; alerts on **down**, **back up**, **status-code changes**, and **response-time spikes** |
| `/checkweb remove <url>` | Stop monitoring (with autocomplete) |
| `/checkweb list` | All monitored sites and their status |
| `/checkweb now <url>` | One-off check of any URL |
| `/bugreport [severity]` | Opens a modal; report is saved and posted to the bugs channel |
| `/buglist`, `/bugresolve <id>` | Admin: review and close bug reports |
| `/sendwebhook <url> …` | Send a message and/or embed through any Discord webhook (needs Manage Webhooks) |
| `/suggestions <text>` | Posts a numbered suggestion embed with 👍 / 👎 reactions |
| `/steam add/remove/list` | Watch Steam apps for updates/news (via the official ISteamNews feed) |
| `/forceupdate` | Check all watched Steam apps right now |
| `/weather [location]` | Weather via wttr.in — defaults to Myerstown, PA (17067) |
| `/setchannel <kind> <channel>` | Admin: route alerts / bugs / suggestions / steam posts |
| `/settings` | Admin: show current channel routing |
| `/botstatus` | Uptime, latency, and per-server statistics |

Everything users submit (monitored sites, bug reports, suggestions, Steam
watches) is persisted in a local PostgreSQL database (`opsbot`), so restarts
lose nothing. See `DATABASE.md` for how to browse and query it.

## Project structure

```
discord-ops-bot/
├── bot.py                 # entry point: bot class, cog loading, error handler
├── config.py              # all settings, loaded from .env
├── requirements.txt
├── .env.example           # copy to .env and fill in
├── cogs/
│   ├── admin.py           # /setchannel /settings /botstatus
│   ├── webmonitor.py      # /checkweb + 5-minute monitoring loop
│   ├── bugreport.py       # /bugreport /buglist /bugresolve (modal-based)
│   ├── webhooks.py        # /sendwebhook
│   ├── suggestions.py     # /suggestions with 👍👎 reactions
│   ├── steam.py           # /steam + /forceupdate + update loop
│   └── weather.py         # /weather via wttr.in
├── utils/
│   ├── database.py        # async PostgreSQL layer (schema + helpers)
│   ├── checks.py          # is_admin() permission check
│   └── logging_setup.py   # console + rotating file logging
├── dashboard/
│   └── app.py             # optional FastAPI read-only dashboard
├── data/                  # created at runtime: logs/ (gitignored)
├── Dockerfile
├── docker-compose.yml
└── ops-bot.service        # systemd unit template
```

## Setup

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token (you'll put it in `.env`).
   No privileged intents are needed — leave them all off.
3. Invite the bot to your server with this URL (replace `CLIENT_ID` with the
   Application ID from the **General Information** tab):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=85056
   ```

   (That permission set is: View Channels, Send Messages, Embed Links,
   Add Reactions, Read Message History.)

### 2. Create the PostgreSQL database

With a local PostgreSQL server running, create a role and database for the
bot (one time, as the `postgres` superuser):

```sql
CREATE ROLE opsbot LOGIN PASSWORD 'your-password-here';
CREATE DATABASE opsbot OWNER opsbot;
```

The bot creates/updates its own tables on startup — no manual schema setup.

### 3. Configure

```bash
cp .env.example .env
# then edit .env:
#   DISCORD_TOKEN  — required
#   DATABASE_URL   — postgresql://opsbot:your-password-here@localhost:5432/opsbot
#   GUILD_ID       — recommended: your server ID, so slash commands appear
#                    instantly (global sync can take up to an hour)
```

Channel routing can be done entirely in Discord with `/setchannel` after the
bot is running — the `*_CHANNEL_ID` values in `.env` are just fallbacks.

### 4. Run locally (no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

You should see `Logged in as <botname>` in the console. Logs also go to
`data/logs/opsbot.log` (rotating, 5 × 1 MB).

First steps in Discord:

```
/setchannel kind:alerts      channel:#ops-alerts
/setchannel kind:bugs        channel:#bug-reports
/setchannel kind:suggestions channel:#suggestions
/checkweb add url:https://example.com
/weather
```

### 5. Optional: web dashboard

```bash
source .venv/bin/activate
uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080> — monitored sites, recent incidents, and open
bug reports, auto-refreshing every 60 s. It's read-only and separate from the
bot process; don't expose it to the internet without putting auth in front.

## Running 24/7

### Option A — systemd (Linux/WSL, no Docker)

Edit the paths in `ops-bot.service` to match where you cloned the project and
your venv, then:

```bash
sudo cp ops-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ops-bot
journalctl -u ops-bot -f        # follow logs
```

The unit restarts the bot automatically if it crashes.

### Option B — Docker (preferred for servers)

```bash
docker compose up -d            # starts the bot AND the dashboard on :8080
docker compose logs -f opsbot
```

The `./data` folder is mounted into the container so logs persist across
rebuilds. When the bot runs in Docker but PostgreSQL runs on the host, point
`DATABASE_URL` at `host.docker.internal` instead of `localhost`. If you don't
want the dashboard, delete its block from `docker-compose.yml`.

## How the monitoring works

- **Websites** — every `WEB_CHECK_INTERVAL_MINUTES` (default 5) each URL gets a
  GET request with a 15 s timeout, up to 10 sites concurrently. A site is
  considered *down* on a network error, timeout, or HTTP 5xx. A **DOWN alert**
  fires after 2 consecutive failures (avoids false alarms from one blip), and
  an **UP alert** with the outage duration fires on recovery. A 200 → 404 flip
  is reported as a **status-code change**, and a response slower than both
  `SLOW_RESPONSE_MS` and 3× the rolling average triggers a **slow-response
  alert**. Every event is also stored in the `site_incidents` table.
- **Steam** — detection is **news-feed based** (the official `ISteamNews` API,
  no key required): when a watched app publishes a new news item the channel is
  notified, and items tagged as patch notes are highlighted as updates. Adding
  a watch sets a baseline, so only *future* news triggers alerts.

## Security & rate limiting

- The token lives only in `.env` (gitignored); nothing is hardcoded.
- `/sendwebhook` requires the **Manage Webhooks** permission and replies
  ephemerally so the webhook URL is never echoed publicly.
- Admin commands accept **Manage Server** holders, or the role set in
  `ADMIN_ROLE_ID`.
- User-facing commands have per-user cooldowns (e.g. 4 weather lookups/min,
  2 suggestions per 2 min); exceeding one gets a friendly ephemeral notice.

## Troubleshooting

- **`connection refused` / `password authentication failed` on startup** —
  make sure the PostgreSQL service is running on port 5432 and that
  `DATABASE_URL` in `.env` has the right user, password, and database name.
- **Slash commands don't appear** — set `GUILD_ID` in `.env` for instant
  sync to that server; global sync can take up to an hour. Also confirm the
  invite used the `applications.commands` scope.
- **No alerts arriving** — run `/settings`; if a destination shows *not set*,
  configure it with `/setchannel`. The bot also needs Send Messages + Embed
  Links in that channel.
- **Weather says "unexpected response"** — wttr.in occasionally rate-limits;
  try again in a minute or use a more specific location.

## Extending

Add a file under `cogs/` with a `commands.Cog` subclass and an
`async def setup(bot)` that calls `bot.add_cog(...)`, then list it in `COGS`
in `bot.py`. The shared aiohttp session (`self.bot.session`) and database
(`self.bot.db`) are available on every cog.
