# PostgreSQL guide for the Ops Bot

The bot stores everything in a local PostgreSQL database named **`opsbot`**,
owned by a role (user) also named **`opsbot`**. The connection string lives in
`.env` as `DATABASE_URL` and is never committed. The bot creates and updates
its own tables on startup, so a fresh database needs zero manual setup.

## Two ways to look inside

### Option A — pgAdmin 4 (graphical, easiest)

pgAdmin was installed alongside PostgreSQL (Start Menu → *pgAdmin 4*).

1. Expand **Servers → PostgreSQL 18** (enter the `postgres` password).
2. Expand **Databases → opsbot → Schemas → public → Tables**.
3. Right-click any table → **View/Edit Data → All Rows**.
4. For ad-hoc SQL: right-click the `opsbot` database → **Query Tool**.

### Option B — psql (terminal)

`psql` ships with PostgreSQL. From PowerShell or CMD:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U opsbot -d opsbot -h localhost
```

It prompts for the opsbot password (the one in your `DATABASE_URL`), then
drops you at an `opsbot=>` prompt. Type SQL there, ending each statement
with a semicolon. Tip: add `C:\Program Files\PostgreSQL\18\bin` to your PATH
so plain `psql` works anywhere.

## psql survival kit

Backslash commands are psql shortcuts (no semicolon needed):

| Command | What it does |
|---|---|
| `\l` | List all databases on the server |
| `\c opsbot` | Connect/switch to the `opsbot` database |
| `\dt` | List tables in the current database |
| `\d monitored_sites` | Describe one table: columns, types, indexes |
| `\x` | Toggle expanded output (great for wide rows) |
| `\timing` | Show how long each query takes |
| `\q` | Quit |

## The bot's tables

| Table | Holds | Written by |
|---|---|---|
| `guild_settings` | Per-server channel routing from `/setchannel` | admin cog |
| `monitored_sites` | URLs watched by `/checkweb`, with status + response times | webmonitor cog |
| `site_incidents` | Every down/up/status-change/slow event | webmonitor cog |
| `bug_reports` | `/bugreport` submissions and their open/resolved status | bugreport cog |
| `suggestions` | `/suggestions` posts and their Discord message IDs | suggestions cog |
| `steam_watches` | Apps watched by `/steam add` and the last news item seen | steam cog |

Notes on the column types:

- Discord IDs (`guild_id`, `user_id`, `message_id`, …) are `BIGINT` —
  Discord snowflakes are 19-digit numbers that don't fit in a 32-bit `INTEGER`.
- Timestamps (`created_at`, `last_checked`, …) are ISO-8601 **text** in UTC,
  e.g. `2026-06-11T20:15:04+00:00`. Cast with `::timestamptz` when you want
  date math (examples below).
- `is_up` / `was_slow` are `0`/`1` smallints.
- `id` columns are auto-generated (`GENERATED ALWAYS AS IDENTITY`) — never
  insert them yourself.

## Queries to try

```sql
-- Everything the bot is monitoring, nicest columns first
SELECT id, url, is_up, last_status_code, avg_response_ms, last_checked
FROM monitored_sites ORDER BY id;

-- Open bugs, newest first
SELECT id, severity, title, user_name, created_at
FROM bug_reports WHERE status = 'open' ORDER BY id DESC;

-- Incident history for one site, with the URL joined in
SELECT i.created_at, i.event, i.detail, s.url
FROM site_incidents i
JOIN monitored_sites s ON s.id = i.site_id
ORDER BY i.id DESC LIMIT 20;

-- How many incidents per site? (GROUP BY)
SELECT s.url, COUNT(*) AS incidents
FROM site_incidents i
JOIN monitored_sites s ON s.id = i.site_id
GROUP BY s.url ORDER BY incidents DESC;

-- Bugs filed in the last 7 days (casting the text timestamp)
SELECT id, title, created_at
FROM bug_reports
WHERE created_at::timestamptz > now() - interval '7 days';

-- Manually resolve bug #3 (the same thing /bugresolve does)
UPDATE bug_reports SET status = 'resolved' WHERE id = 3;

-- Careful: DELETE removes rows permanently. Deleting a site also deletes
-- its incidents automatically (ON DELETE CASCADE on site_incidents).
DELETE FROM monitored_sites WHERE url = 'https://example.com';
```

## Backup and restore

```powershell
# Backup (creates a plain-SQL dump you can read)
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -U opsbot -h localhost opsbot -f opsbot_backup.sql

# Restore into an empty database
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U opsbot -h localhost -d opsbot -f opsbot_backup.sql
```

## If something breaks

- **`connection refused`** — the PostgreSQL Windows service isn't running.
  Start it from *Services* (`postgresql-x64-18`) or reboot.
- **`password authentication failed`** — the password in `DATABASE_URL`
  doesn't match the `opsbot` role. Reset it as superuser:
  `ALTER ROLE opsbot PASSWORD 'new-password';` then update `.env`.
- **Start over with a clean database** — as superuser:
  `DROP DATABASE opsbot; CREATE DATABASE opsbot OWNER opsbot;`
  The bot rebuilds the tables on next start.
