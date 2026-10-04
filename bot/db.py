"""Small storage layer.

- If DATABASE_URL is set (e.g. Supabase Postgres) the data lives there, so it
  survives restarts on hosts without a persistent disk (Render free).
- Otherwise a local SQLite file in DATA_DIR is used (Railway volume, local runs).

Calls are fast, so they run directly, guarded by a lock.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    id         {ID},
    platform   TEXT NOT NULL,            -- instagram | youtube
    handle     TEXT NOT NULL,            -- username or channel url
    ig_user_id TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL,
    UNIQUE(platform, handle)
);
CREATE TABLE IF NOT EXISTS seen (
    kind    TEXT NOT NULL,               -- ig_post | ig_story | yt | dm_msg
    ext_id  TEXT NOT NULL,
    seen_at BIGINT NOT NULL,
    PRIMARY KEY (kind, ext_id)
);
CREATE TABLE IF NOT EXISTS items (
    id           {ID},
    kind         TEXT NOT NULL,          -- reel | story
    origin       TEXT NOT NULL,          -- source | dm
    platform     TEXT NOT NULL DEFAULT 'instagram',  -- where the media came from
    source_label TEXT,
    ext_id       TEXT,                   -- media pk / story pk / youtube id (used to re-download)
    file_path    TEXT NOT NULL,
    media_type   TEXT NOT NULL,          -- video | photo
    status       TEXT NOT NULL,          -- review | queued | scheduled | publishing | published | rejected | failed
    error        TEXT,
    ig_code      TEXT,
    created_at   BIGINT NOT NULL,
    published_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""

DEFAULT_CAPTION = "🎬"


class DB:
    def __init__(self, sqlite_path: Path, database_url: str = ""):
        self._lock = threading.Lock()
        self._url = database_url
        self._sqlite_path = sqlite_path
        self.is_pg = bool(database_url)
        self._connect()
        id_col = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        schema = SCHEMA.replace("{ID}", id_col)
        with self._lock:
            if self.is_pg:
                with self._conn.cursor() as cur:
                    cur.execute(schema)
            else:
                self._conn.executescript(schema)
                self._conn.commit()

    def _connect(self) -> None:
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None keeps it compatible with Supabase's pooler
            self._conn = psycopg.connect(self._url, autocommit=True, row_factory=dict_row,
                                         prepare_threshold=None, connect_timeout=15)
        else:
            self._conn = sqlite3.connect(str(self._sqlite_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    # ---------- low level ----------
    def _run(self, sql: str, params: tuple, fetch: str):
        if self.is_pg:
            sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                with self._lock:
                    cur = self._conn.execute(sql, params)
                    if fetch == "all":
                        result = cur.fetchall()
                    elif fetch == "one":
                        result = cur.fetchone()
                    else:
                        result = cur.rowcount
                    if not self.is_pg:
                        self._conn.commit()
                    return result
            except Exception as exc:
                if not self.is_pg or attempt == 2 or not _is_connection_error(exc):
                    raise
                log.warning("Database connection lost (%s); reconnecting", exc)
                time.sleep(2)
                with self._lock:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._connect()

    def _exec(self, sql: str, params: tuple = ()) -> int:
        return self._run(sql, params, "count")

    def _all(self, sql: str, params: tuple = ()) -> list:
        return self._run(sql, params, "all")

    def _one(self, sql: str, params: tuple = ()):
        return self._run(sql, params, "one")

    # ---------- settings ----------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def caption(self) -> str:
        return self.get("caption", DEFAULT_CAPTION)

    # ---------- sources ----------
    def add_source(self, platform: str, handle: str) -> Optional[int]:
        row = self._one(
            "INSERT INTO sources(platform, handle, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(platform, handle) DO NOTHING RETURNING id",
            (platform, handle, int(time.time())),
        )
        return row["id"] if row else None

    def remove_source(self, source_id: int) -> bool:
        return self._exec("DELETE FROM sources WHERE id=?", (source_id,)) > 0

    def sources(self, platform: Optional[str] = None) -> list:
        if platform:
            return self._all("SELECT * FROM sources WHERE platform=? AND active=1 ORDER BY id", (platform,))
        return self._all("SELECT * FROM sources ORDER BY platform, id")

    def set_source_ig_id(self, source_id: int, ig_user_id: str) -> None:
        self._exec("UPDATE sources SET ig_user_id=? WHERE id=?", (ig_user_id, source_id))

    # ---------- seen ----------
    def is_seen(self, kind: str, ext_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM seen WHERE kind=? AND ext_id=?", (kind, str(ext_id))) is not None

    def mark_seen(self, kind: str, ext_id: str) -> None:
        self._exec(
            "INSERT INTO seen(kind, ext_id, seen_at) VALUES(?, ?, ?) ON CONFLICT DO NOTHING",
            (kind, str(ext_id), int(time.time())),
        )

    # ---------- items ----------
    def add_item(self, kind: str, origin: str, source_label: str, ext_id: str,
                 file_path: str, media_type: str, status: str, platform: str = "instagram") -> int:
        row = self._one(
            "INSERT INTO items(kind, origin, platform, source_label, ext_id, file_path, media_type, status, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (kind, origin, platform, source_label, str(ext_id), file_path, media_type, status, int(time.time())),
        )
        return row["id"]

    def item(self, item_id: int):
        return self._one("SELECT * FROM items WHERE id=?", (item_id,))

    def items_by_status(self, *statuses: str) -> list:
        marks = ",".join("?" * len(statuses))
        return self._all(f"SELECT * FROM items WHERE status IN ({marks}) ORDER BY id", statuses)

    def set_status(self, item_id: int, status: str, error: Optional[str] = None,
                   ig_code: Optional[str] = None) -> None:
        published_at = int(time.time()) if status == "published" else None
        self._exec(
            "UPDATE items SET status=?, error=?, ig_code=COALESCE(?, ig_code), "
            "published_at=COALESCE(?, published_at) WHERE id=?",
            (status, error, ig_code, published_at, item_id),
        )

    def set_file(self, item_id: int, file_path: str) -> None:
        self._exec("UPDATE items SET file_path=? WHERE id=?", (file_path, item_id))

    def published_last_24h(self) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM items WHERE status='published' AND published_at>=?",
            (int(time.time()) - 86400,),
        )
        return row["n"]

    def reset_interrupted(self) -> int:
        """Items that were mid-publish when the bot restarted go back to the queue."""
        return self._exec("UPDATE items SET status='queued' WHERE status IN ('scheduled', 'publishing')")

    def stats(self) -> dict:
        rows = self._all("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in ("OperationalError", "InterfaceError") or "closed" in str(exc).lower()
