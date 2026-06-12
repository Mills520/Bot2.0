"""Async PostgreSQL storage layer (asyncpg).

A single Database instance is created by the bot and shared by every cog
via ``bot.db``. All timestamps are stored as UTC ISO-8601 strings.

Connects to the local PostgreSQL server using the DSN from config
(DATABASE_URL in .env), e.g. postgresql://opsbot:password@localhost:5432/opsbot
"""

from datetime import datetime, timezone

import asyncpg

# Discord IDs (snowflakes) need BIGINT — they overflow PostgreSQL's
# 32-bit INTEGER. is_up/was_slow stay 0/1 SMALLINTs and timestamps stay
# ISO-8601 TEXT so the cogs work unchanged.
SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id  BIGINT NOT NULL,
    key       TEXT   NOT NULL,
    value     TEXT,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS monitored_sites (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id             BIGINT NOT NULL,
    url                  TEXT   NOT NULL,
    added_by             BIGINT NOT NULL,
    added_at             TEXT   NOT NULL,
    is_up                SMALLINT NOT NULL DEFAULT 1,
    last_status_code     INTEGER,
    last_response_ms     DOUBLE PRECISION,
    avg_response_ms      DOUBLE PRECISION,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    was_slow             SMALLINT NOT NULL DEFAULT 0,
    down_since           TEXT,
    last_checked         TEXT,
    UNIQUE (guild_id, url)
);

CREATE TABLE IF NOT EXISTS site_incidents (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id    BIGINT NOT NULL REFERENCES monitored_sites(id) ON DELETE CASCADE,
    event      TEXT   NOT NULL,   -- down | up | status_change | slow
    detail     TEXT,
    created_at TEXT   NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_site_incidents_site_id ON site_incidents (site_id);

CREATE TABLE IF NOT EXISTS bug_reports (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id    BIGINT,
    user_id     BIGINT NOT NULL,
    user_name   TEXT   NOT NULL,
    title       TEXT   NOT NULL,
    description TEXT   NOT NULL,
    steps       TEXT,
    severity    TEXT   NOT NULL DEFAULT 'medium',
    status      TEXT   NOT NULL DEFAULT 'open',
    created_at  TEXT   NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bug_reports_guild_status ON bug_reports (guild_id, status);

CREATE TABLE IF NOT EXISTS suggestions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id   BIGINT,
    user_id    BIGINT NOT NULL,
    user_name  TEXT   NOT NULL,
    content    TEXT   NOT NULL,
    message_id BIGINT,
    channel_id BIGINT,
    created_at TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS steam_watches (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    app_id        BIGINT NOT NULL,
    app_name      TEXT,
    last_news_gid TEXT,
    last_checked  TEXT,
    UNIQUE (guild_id, app_id)
);
"""


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (the storage format)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rowcount(status: str) -> int:
    """Rows affected, parsed from an asyncpg command tag like 'UPDATE 1'."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        await self._pool.execute(SCHEMA)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    # -- low-level helpers -------------------------------------------------
    # Placeholders are PostgreSQL-style ($1, $2, ...), not sqlite's ?.

    async def execute(self, sql: str, *args) -> str:
        """Run a statement; returns the command tag (e.g. 'DELETE 2')."""
        return await self._pool.execute(sql, *args)

    async def fetchone(self, sql: str, *args) -> asyncpg.Record | None:
        return await self._pool.fetchrow(sql, *args)

    async def fetchall(self, sql: str, *args) -> list[asyncpg.Record]:
        return list(await self._pool.fetch(sql, *args))

    async def fetchval(self, sql: str, *args):
        return await self._pool.fetchval(sql, *args)

    # -- per-guild settings (e.g. destination channels) ----------------------

    async def get_setting(self, guild_id: int, key: str) -> str | None:
        return await self.fetchval(
            "SELECT value FROM guild_settings WHERE guild_id = $1 AND key = $2",
            guild_id, key,
        )

    async def set_setting(self, guild_id: int, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES ($1, $2, $3)"
            " ON CONFLICT (guild_id, key) DO UPDATE SET value = EXCLUDED.value",
            guild_id, key, value,
        )

    # -- website monitoring --------------------------------------------------

    async def add_site(self, guild_id: int, url: str, added_by: int) -> int | None:
        """Insert a site; returns the new row id, or None if already monitored."""
        return await self.fetchval(
            "INSERT INTO monitored_sites (guild_id, url, added_by, added_at)"
            " VALUES ($1, $2, $3, $4) ON CONFLICT (guild_id, url) DO NOTHING"
            " RETURNING id",
            guild_id, url, added_by, utcnow(),
        )

    async def get_site(self, guild_id: int, url: str) -> asyncpg.Record | None:
        return await self.fetchone(
            "SELECT * FROM monitored_sites WHERE guild_id = $1 AND url = $2", guild_id, url
        )

    async def remove_site(self, guild_id: int, url: str) -> bool:
        status = await self.execute(
            "DELETE FROM monitored_sites WHERE guild_id = $1 AND url = $2", guild_id, url
        )
        return _rowcount(status) > 0

    async def list_sites(self, guild_id: int) -> list[asyncpg.Record]:
        return await self.fetchall(
            "SELECT * FROM monitored_sites WHERE guild_id = $1 ORDER BY id", guild_id
        )

    async def list_all_sites(self) -> list[asyncpg.Record]:
        return await self.fetchall("SELECT * FROM monitored_sites ORDER BY id")

    async def count_sites(self, guild_id: int) -> int:
        return await self.fetchval(
            "SELECT COUNT(*) FROM monitored_sites WHERE guild_id = $1", guild_id
        )

    async def update_site(self, site_id: int, **fields) -> None:
        """Update arbitrary columns on a site row (keys come from code, not users)."""
        if not fields:
            return
        assignments = ", ".join(
            f"{column} = ${position}" for position, column in enumerate(fields, start=1)
        )
        await self.execute(
            f"UPDATE monitored_sites SET {assignments} WHERE id = ${len(fields) + 1}",
            *fields.values(), site_id,
        )

    async def add_incident(self, site_id: int, event: str, detail: str) -> None:
        await self.execute(
            "INSERT INTO site_incidents (site_id, event, detail, created_at) VALUES ($1, $2, $3, $4)",
            site_id, event, detail, utcnow(),
        )

    # -- bug reports -----------------------------------------------------------

    async def add_bug(
        self, guild_id: int | None, user_id: int, user_name: str,
        title: str, description: str, steps: str, severity: str,
    ) -> int:
        return await self.fetchval(
            "INSERT INTO bug_reports (guild_id, user_id, user_name, title, description, steps, severity, created_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
            guild_id, user_id, user_name, title, description, steps, severity, utcnow(),
        )

    async def list_bugs(self, guild_id: int, status: str = "open", limit: int = 10) -> list[asyncpg.Record]:
        return await self.fetchall(
            "SELECT * FROM bug_reports WHERE guild_id = $1 AND status = $2 ORDER BY id DESC LIMIT $3",
            guild_id, status, limit,
        )

    async def set_bug_status(self, guild_id: int, bug_id: int, status: str) -> bool:
        tag = await self.execute(
            "UPDATE bug_reports SET status = $1 WHERE id = $2 AND guild_id = $3",
            status, bug_id, guild_id,
        )
        return _rowcount(tag) > 0

    # -- suggestions ------------------------------------------------------------

    async def add_suggestion(self, guild_id: int | None, user_id: int, user_name: str, content: str) -> int:
        return await self.fetchval(
            "INSERT INTO suggestions (guild_id, user_id, user_name, content, created_at)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING id",
            guild_id, user_id, user_name, content, utcnow(),
        )

    async def set_suggestion_message(self, suggestion_id: int, message_id: int, channel_id: int) -> None:
        await self.execute(
            "UPDATE suggestions SET message_id = $1, channel_id = $2 WHERE id = $3",
            message_id, channel_id, suggestion_id,
        )

    # -- steam update watches ------------------------------------------------------

    async def add_steam_watch(self, guild_id: int, app_id: int, app_name: str | None) -> int | None:
        """Insert a watch; returns the new row id, or None if already watched."""
        return await self.fetchval(
            "INSERT INTO steam_watches (guild_id, app_id, app_name) VALUES ($1, $2, $3)"
            " ON CONFLICT (guild_id, app_id) DO NOTHING RETURNING id",
            guild_id, app_id, app_name,
        )

    async def remove_steam_watch(self, guild_id: int, app_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM steam_watches WHERE guild_id = $1 AND app_id = $2", guild_id, app_id
        )
        return _rowcount(status) > 0

    async def list_steam_watches(self, guild_id: int) -> list[asyncpg.Record]:
        return await self.fetchall(
            "SELECT * FROM steam_watches WHERE guild_id = $1 ORDER BY id", guild_id
        )

    async def all_steam_watches(self) -> list[asyncpg.Record]:
        return await self.fetchall("SELECT * FROM steam_watches ORDER BY id")

    async def update_steam_watch(self, watch_id: int, **fields) -> None:
        if not fields:
            return
        assignments = ", ".join(
            f"{column} = ${position}" for position, column in enumerate(fields, start=1)
        )
        await self.execute(
            f"UPDATE steam_watches SET {assignments} WHERE id = ${len(fields) + 1}",
            *fields.values(), watch_id,
        )
