"""Read-only web dashboard for monitored sites, incidents, and bug reports.

Runs as a separate process from the bot and only reads the PostgreSQL
database (its connections are forced read-only server-side):

    uvicorn dashboard.app:app --host 127.0.0.1 --port 8080

Endpoints:
    GET /              HTML overview (auto-refreshes every 60 s)
    GET /api/sites     monitored sites as JSON
    GET /api/incidents recent incidents as JSON
    GET /api/bugs      open bug reports as JSON
"""

import html
import os

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://opsbot@localhost:5432/opsbot")

app = FastAPI(title="Ops Bot Dashboard")

_pool: asyncpg.Pool | None = None


async def query(sql: str, args: tuple = ()) -> list[dict]:
    """Run a read-only query; returns [] if the database isn't reachable."""
    global _pool
    try:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=4,
                server_settings={"default_transaction_read_only": "on"},
            )
        return [dict(row) for row in await _pool.fetch(sql, *args)]
    except (asyncpg.PostgresError, OSError):
        return []


@app.on_event("shutdown")
async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


@app.get("/api/sites")
async def api_sites() -> list[dict]:
    return await query(
        "SELECT guild_id, url, is_up, last_status_code, last_response_ms,"
        " avg_response_ms, consecutive_failures, last_checked"
        " FROM monitored_sites ORDER BY id"
    )


@app.get("/api/incidents")
async def api_incidents() -> list[dict]:
    return await query(
        "SELECT i.created_at, i.event, i.detail, s.url"
        " FROM site_incidents i JOIN monitored_sites s ON s.id = i.site_id"
        " ORDER BY i.id DESC LIMIT 50"
    )


@app.get("/api/bugs")
async def api_bugs() -> list[dict]:
    return await query(
        "SELECT id, guild_id, user_name, title, severity, status, created_at"
        " FROM bug_reports WHERE status = 'open' ORDER BY id DESC LIMIT 50"
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    sites = await api_sites()
    incidents = await api_incidents()
    bugs = await api_bugs()

    def esc(value) -> str:
        return html.escape(str(value if value is not None else "—"))

    site_rows = "".join(
        "<tr>"
        f"<td>{'🟢 up' if site['is_up'] else '🔴 down'}</td>"
        f"<td>{esc(site['url'])}</td>"
        f"<td>{esc(site['last_status_code'])}</td>"
        f"<td>{esc(site['last_response_ms'])}</td>"
        f"<td>{esc(site['avg_response_ms'])}</td>"
        f"<td>{esc(site['last_checked'])}</td>"
        "</tr>"
        for site in sites
    ) or "<tr><td colspan=6>No monitored sites yet.</td></tr>"

    incident_rows = "".join(
        "<tr>"
        f"<td>{esc(item['created_at'])}</td>"
        f"<td>{esc(item['event'])}</td>"
        f"<td>{esc(item['url'])}</td>"
        f"<td>{esc(item['detail'])}</td>"
        "</tr>"
        for item in incidents
    ) or "<tr><td colspan=4>No incidents recorded. 🎉</td></tr>"

    bug_rows = "".join(
        "<tr>"
        f"<td>#{esc(bug['id'])}</td>"
        f"<td>{esc(bug['severity'])}</td>"
        f"<td>{esc(bug['title'])}</td>"
        f"<td>{esc(bug['user_name'])}</td>"
        f"<td>{esc(bug['created_at'])}</td>"
        "</tr>"
        for bug in bugs
    ) or "<tr><td colspan=5>No open bug reports. 🎉</td></tr>"

    up_count = sum(1 for site in sites if site["is_up"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Ops Bot Dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #1e1f22; color: #dbdee1;
         max-width: 1000px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ color: #fff; }} h2 {{ color: #b5bac1; margin-top: 2rem; }}
  table {{ width: 100%; border-collapse: collapse; background: #2b2d31;
           border-radius: 8px; overflow: hidden; }}
  th, td {{ padding: .55rem .8rem; text-align: left; font-size: .9rem; }}
  th {{ background: #313338; color: #b5bac1; }}
  tr:nth-child(even) {{ background: #313338; }}
  .summary {{ color: #b5bac1; }}
</style>
</head>
<body>
<h1>🤖 Ops Bot Dashboard</h1>
<p class="summary">{up_count}/{len(sites)} sites up · {len(bugs)} open bug(s) ·
auto-refreshes every 60&nbsp;s</p>

<h2>Monitored sites</h2>
<table>
<tr><th>Status</th><th>URL</th><th>HTTP</th><th>Last ms</th><th>Avg ms</th><th>Last checked (UTC)</th></tr>
{site_rows}
</table>

<h2>Recent incidents</h2>
<table>
<tr><th>When (UTC)</th><th>Event</th><th>URL</th><th>Detail</th></tr>
{incident_rows}
</table>

<h2>Open bug reports</h2>
<table>
<tr><th>ID</th><th>Severity</th><th>Title</th><th>Reporter</th><th>Filed (UTC)</th></tr>
{bug_rows}
</table>
</body>
</html>"""
