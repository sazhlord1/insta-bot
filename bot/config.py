"""All settings come from environment variables (set them in Railway → Variables)."""
import os
from pathlib import Path


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN = _req("TELEGRAM_BOT_TOKEN")
    ADMIN_TELEGRAM_ID = int(_req("ADMIN_TELEGRAM_ID"))

    # Instagram destination page (the bot logs in as this page)
    IG_USERNAME = _req("IG_USERNAME")
    IG_PASSWORD = _req("IG_PASSWORD")
    # Optional: the "setup key" from Google Authenticator. If empty, the bot
    # asks you for the 6-digit code in Telegram when Instagram needs it.
    IG_TOTP_SECRET = os.getenv("IG_TOTP_SECRET", "").replace(" ", "").strip()
    # Optional proxy for Instagram, e.g. http://user:pass@host:port
    IG_PROXY = os.getenv("IG_PROXY", "").strip() or None

    # The personal Instagram account whose DMs (shared reels) go to the queue
    DM_SENDER_USERNAME = os.getenv("DM_SENDER_USERNAME", "").strip().lstrip("@").lower()
    DM_POLL_MINUTES = _int("DM_POLL_MINUTES", 5)

    # YouTube (optional helpers for when YouTube blocks server IPs)
    YT_COOKIES = os.getenv("YT_COOKIES", "")  # full content of a cookies.txt file
    YT_PROXY = os.getenv("YT_PROXY", "").strip() or None

    # Behaviour
    MAX_ITEMS_PER_SOURCE = _int("MAX_ITEMS_PER_SOURCE", 3)
    MAX_VIDEO_SECONDS = _int("MAX_VIDEO_SECONDS", 180)
    PUBLISH_GAP_SECONDS = _int("PUBLISH_GAP_SECONDS", 180)
    DAILY_PUBLISH_LIMIT = _int("DAILY_PUBLISH_LIMIT", 15)

    # Storage (mount a Railway volume at /data so data survives redeploys)
    DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
    MEDIA_DIR = DATA_DIR / "media"
    DB_PATH = DATA_DIR / "bot.sqlite3"


Config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
