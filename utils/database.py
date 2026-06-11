"""Async SQLite storage layer.

A single Database instance is created by the bot and shared by every cog
via ``bot.db``. All timestamps are stored as UTC ISO-8601 strings.
"""

import sqlite3
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id  INTEGER NOT NULL,
    key       TEXT    NOT NULL,
    value     TEXT,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS monitored_sites (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    url                  TEXT    NOT NULL,
    added_by             INTEGER NOT NULL,
    added_at             TEXT    NOT NULL,
    is_up                INTEGER NOT NULL DEFAULT 1,
    last_status_code     INTEGER,
    last_response_ms     REAL,
    avg_response_ms      REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    was_slow             INTEGER NOT NULL DEFAULT 0,
    down_since           TEXT,
    last_checked         TEXT,
    UNIQUE (guild_id, url)
);

CREATE TABLE IF NOT EXISTS site_incidents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id    INTEGER NOT NULL REFERENCES monitored_sites(id) ON DELETE CASCADE,
    event      TEXT    NOT NULL,   -- down | up | status_change | slow
    detail     TEXT,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bug_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    user_id     INTEGER NOT NULL,
    user_name   TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL,
    steps       TEXT,
    severity    TEXT    NOT NULL DEFAULT 'medium',
    status      TEXT    NOT NULL DEFAULT 'open',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER,
    user_id    INTEGER NOT NULL,
    user_name  TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    message_id INTEGER,
    channel_id INTEGER,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS steam_watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    app_id        INTEGER NOT NULL,
    app_name      TEXT,
    last_news_gid TEXT,
    last_checked  TEXT,
    UNIQUE (guild_id, app_id)
);
"""


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (the storage format)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    # -- low-level helpers -------------------------------------------------

    async def execute(self, sql: str, *args) -> aiosqlite.Cursor:
        cur = await self._db.execute(sql, args)
        await self._db.commit()
        return cur

    async def fetchone(self, sql: str, *args) -> aiosqlite.Row | None:
        cur = await self._db.execute(sql, args)
        return await cur.fetchone()

    async def fetchall(self, sql: str, *args) -> list[aiosqlite.Row]:
        cur = await self._db.execute(sql, args)
        return list(await cur.fetchall())

    # -- per-guild settings (e.g. destination channels) ----------------------

    async def get_setting(self, guild_id: int, key: str) -> str | None:
        row = await self.fetchone(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            guild_id, key,
        )
        return row["value"] if row else None

    async def set_setting(self, guild_id: int, key: str, value: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
            guild_id, key, value,
        )

    # -- website monitoring --------------------------------------------------

    async def add_site(self, guild_id: int, url: str, added_by: int) -> int | None:
        """Insert a site; returns the new row id, or None if already monitored."""
        try:
            cur = await self.execute(
                "INSERT INTO monitored_sites (guild_id, url, added_by, added_at) VALUES (?, ?, ?, ?)",
                guild_id, url, added_by, utcnow(),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    async def get_site(self, guild_id: int, url: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM monitored_sites WHERE guild_id = ? AND url = ?", guild_id, url
        )

    async def remove_site(self, guild_id: int, url: str) -> bool:
        cur = await self.execute(
            "DELETE FROM monitored_sites WHERE guild_id = ? AND url = ?", guild_id, url
        )
        return cur.rowcount > 0

    async def list_sites(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM monitored_sites WHERE guild_id = ? ORDER BY id", guild_id
        )

    async def list_all_sites(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM monitored_sites ORDER BY id")

    async def count_sites(self, guild_id: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM monitored_sites WHERE guild_id = ?", guild_id
        )
        return row["n"]

    async def update_site(self, site_id: int, **fields) -> None:
        """Update arbitrary columns on a site row (keys come from code, not users)."""
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        await self.execute(
            f"UPDATE monitored_sites SET {assignments} WHERE id = ?",
            *fields.values(), site_id,
        )

    async def add_incident(self, site_id: int, event: str, detail: str) -> None:
        await self.execute(
            "INSERT INTO site_incidents (site_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            site_id, event, detail, utcnow(),
        )

    # -- bug reports -----------------------------------------------------------

    async def add_bug(
        self, guild_id: int | None, user_id: int, user_name: str,
        title: str, description: str, steps: str, severity: str,
    ) -> int:
        cur = await self.execute(
            "INSERT INTO bug_reports (guild_id, user_id, user_name, title, description, steps, severity, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            guild_id, user_id, user_name, title, description, steps, severity, utcnow(),
        )
        return cur.lastrowid

    async def list_bugs(self, guild_id: int, status: str = "open", limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM bug_reports WHERE guild_id = ? AND status = ? ORDER BY id DESC LIMIT ?",
            guild_id, status, limit,
        )

    async def set_bug_status(self, guild_id: int, bug_id: int, status: str) -> bool:
        cur = await self.execute(
            "UPDATE bug_reports SET status = ? WHERE id = ? AND guild_id = ?",
            status, bug_id, guild_id,
        )
        return cur.rowcount > 0

    # -- suggestions ------------------------------------------------------------

    async def add_suggestion(self, guild_id: int | None, user_id: int, user_name: str, content: str) -> int:
        cur = await self.execute(
            "INSERT INTO suggestions (guild_id, user_id, user_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            guild_id, user_id, user_name, content, utcnow(),
        )
        return cur.lastrowid

    async def set_suggestion_message(self, suggestion_id: int, message_id: int, channel_id: int) -> None:
        await self.execute(
            "UPDATE suggestions SET message_id = ?, channel_id = ? WHERE id = ?",
            message_id, channel_id, suggestion_id,
        )

    # -- steam update watches ------------------------------------------------------

    async def add_steam_watch(self, guild_id: int, app_id: int, app_name: str | None) -> int | None:
        """Insert a watch; returns the new row id, or None if already watched."""
        try:
            cur = await self.execute(
                "INSERT INTO steam_watches (guild_id, app_id, app_name) VALUES (?, ?, ?)",
                guild_id, app_id, app_name,
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    async def remove_steam_watch(self, guild_id: int, app_id: int) -> bool:
        cur = await self.execute(
            "DELETE FROM steam_watches WHERE guild_id = ? AND app_id = ?", guild_id, app_id
        )
        return cur.rowcount > 0

    async def list_steam_watches(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM steam_watches WHERE guild_id = ? ORDER BY id", guild_id
        )

    async def all_steam_watches(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM steam_watches ORDER BY id")

    async def update_steam_watch(self, watch_id: int, **fields) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        await self.execute(
            f"UPDATE steam_watches SET {assignments} WHERE id = ?",
            *fields.values(), watch_id,
        )
