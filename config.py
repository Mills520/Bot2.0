"""Central configuration, loaded once from environment variables / .env.

Every other module imports this instead of reading os.environ directly,
so all tunables live in one place.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int | None = None) -> int | None:
    """Read an integer env var, tolerating blank values in .env."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Config error: {name} must be an integer, got {raw!r}")


# --- Required -----------------------------------------------------------
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()

# --- Optional: instant slash-command sync to a single dev/home guild ----
GUILD_ID = _env_int("GUILD_ID")

# --- Optional role that may use admin commands (besides Manage Server) --
ADMIN_ROLE_ID = _env_int("ADMIN_ROLE_ID")

# --- Fallback channels; servers can override with /setchannel -----------
ALERT_CHANNEL_ID = _env_int("ALERT_CHANNEL_ID")
BUG_CHANNEL_ID = _env_int("BUG_CHANNEL_ID")
SUGGESTIONS_CHANNEL_ID = _env_int("SUGGESTIONS_CHANNEL_ID")
STEAM_CHANNEL_ID = _env_int("STEAM_CHANNEL_ID")

# --- Feature tuning ------------------------------------------------------
WEATHER_DEFAULT_LOCATION = os.getenv("WEATHER_DEFAULT_LOCATION", "17067").strip()
WEB_CHECK_INTERVAL_MINUTES = _env_int("WEB_CHECK_INTERVAL_MINUTES", 5)
STEAM_CHECK_INTERVAL_MINUTES = _env_int("STEAM_CHECK_INTERVAL_MINUTES", 15)
SLOW_RESPONSE_MS = _env_int("SLOW_RESPONSE_MS", 2000)
MAX_SITES_PER_GUILD = _env_int("MAX_SITES_PER_GUILD", 25)

# --- Storage / logging ---------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "data/opsbot.db").strip()
LOG_FILE = os.getenv("LOG_FILE", "data/logs/opsbot.log").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
